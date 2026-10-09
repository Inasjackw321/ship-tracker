"""Search -> download tiles -> detect & measure ships -> store."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from .aoi import load_aoi
from .chips import render_chips
from .config import Settings
from .db import Store
from .detect import DetectParams
from .download import download_file, remove_scene, scene_dir
from .scene import clear_sea_fraction, detect_scene
from .stac import Scene, search_scenes

log = logging.getLogger(__name__)


@dataclass
class ScanOptions:
    start: str
    end: str
    max_cloud: float = 30.0
    limit: int | None = None
    min_sea_fraction: float = 0.01
    rgb_chips: bool = True       # download the true-colour image for chips
    keep_tiles: bool = True
    reprocess: bool = False
    params: DetectParams = field(default_factory=DetectParams)


@dataclass
class ScanReport:
    found: int = 0
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    already_done: int = 0
    ships: int = 0
    current: str = ""
    log: list[str] = field(default_factory=list)


def find_scenes(settings: Settings, opts: ScanOptions) -> list[Scene]:
    aoi = load_aoi(settings.aoi_path)
    return search_scenes(settings.source_config(), aoi, opts.start, opts.end, opts.max_cloud, opts.limit)


def process_scene(settings: Settings, store: Store, scene: Scene, opts: ScanOptions) -> tuple[str, int]:
    """Download and analyse one scene. Returns (status, ships_added)."""
    src_cfg = settings.source_config()
    aoi = load_aoi(settings.aoi_path)
    sdir = scene_dir(settings.tiles_dir, scene.id)

    scl_path = download_file(scene.scl_href, sdir / "SCL.tif", src_cfg)
    sea = clear_sea_fraction(scl_path, aoi)
    if sea < opts.min_sea_fraction:
        store.save_scene(scene, "skipped", f"clear sea {sea:.1%}", sea_fraction=sea)
        if not opts.keep_tiles:
            remove_scene(settings.tiles_dir, scene.id)
        return "skipped", 0

    nir_path = download_file(scene.nir_href, sdir / "B08.tif", src_cfg)
    log.info("  detecting ships (clear sea %.0f%%)", sea * 100)
    dets = detect_scene(nir_path, scl_path, aoi, scene.nir_scale, scene.nir_offset, opts.params)

    chips: list = [None] * len(dets)
    if dets:
        image = nir_path
        if opts.rgb_chips and scene.rgb_href:
            try:
                image = download_file(scene.rgb_href, sdir / "TCI.tif", src_cfg)
            except Exception as exc:
                log.warning("  true-colour download failed (%s); chips will use NIR", exc)
        label = scene.datetime[:10]
        paths = render_chips(image, dets, settings.chips_dir / scene.id, label)
        chips = [str(p.relative_to(settings.chips_dir)) if p else None for p in paths]

    if opts.reprocess:
        store.delete_scene_detections(scene.id)
    added = store.add_detections(scene, dets, chips)
    _prune_chips(settings, store, scene.id)
    store.save_scene(scene, "done", f"{len(dets)} detected, {added} new", sea_fraction=sea, n_ships=added)
    if not opts.keep_tiles:
        remove_scene(settings.tiles_dir, scene.id)
    return "done", added


def _prune_chips(settings: Settings, store: Store, scene_id: str) -> None:
    """Delete chips of detections that were dropped as duplicates of another tile."""
    keep = {r[0] for r in store.conn.execute("SELECT chip FROM detections WHERE scene_id=?", (scene_id,))}
    folder = settings.chips_dir / scene_id
    for f in folder.glob("*.png") if folder.exists() else []:
        if str(f.relative_to(settings.chips_dir)) not in keep:
            f.unlink()


def scan(settings: Settings, opts: ScanOptions, store: Store | None = None,
         on_progress: Callable[[ScanReport], None] | None = None) -> ScanReport:
    settings.ensure_dirs()
    own_store = store is None
    store = store or Store(settings.db_path)
    report = ScanReport()

    def note(msg: str) -> None:
        log.info(msg)
        report.log.append(msg)
        del report.log[:-200]
        if on_progress:
            on_progress(report)

    try:
        note(f"Searching {settings.source} for Sentinel-2 scenes {opts.start} .. {opts.end} (cloud <= {opts.max_cloud}%)")
        scenes = find_scenes(settings, opts)
        report.found = len(scenes)
        note(f"Found {len(scenes)} scenes over the area")
        for i, scene in enumerate(scenes, 1):
            if not opts.reprocess and store.scene_status(scene.id) in ("done", "skipped"):
                report.already_done += 1
                continue
            report.current = scene.id
            note(f"[{i}/{len(scenes)}] {scene.id} ({scene.datetime[:16]}, cloud {scene.cloud_cover}%)")
            try:
                status, added = process_scene(settings, store, scene, opts)
            except Exception as exc:
                log.exception("Scene %s failed", scene.id)
                store.save_scene(scene, "failed", str(exc)[:500])
                report.failed += 1
                note(f"  failed: {exc}")
                continue
            if status == "skipped":
                report.skipped += 1
                note("  skipped: no cloud-free sea in the area")
            else:
                report.processed += 1
                report.ships += added
                note(f"  {added} ships recorded")
        report.current = ""
        note(f"Scan finished: {report.processed} processed, {report.skipped} skipped, "
             f"{report.failed} failed, {report.already_done} already done, {report.ships} ships")
    finally:
        if own_store:
            store.close()
    return report
