"""Search -> download tiles -> detect & measure ships -> store."""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
import numpy as np
import shapely
from shapely.ops import unary_union

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
    # "latest": newest images that together cover every tile of the area once.
    # "all": every pass in the date range (several looks at the same place).
    mode: str = "latest"
    workers: int = 3             # tiles downloaded/processed in parallel
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
    area_imaged: float | None = None  # share of the search area with any imagery in the window
    log: list[str] = field(default_factory=list)


_TO_EQUAL_AREA = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True)


def area_km2(geom: BaseGeometry) -> float:
    if geom.is_empty:
        return 0.0
    projected = shapely.transform(geom, lambda xy: np.column_stack(_TO_EQUAL_AREA.transform(xy[:, 0], xy[:, 1])))
    return projected.area / 1e6


def select_latest_coverage(scenes: list[Scene], aoi: BaseGeometry, min_gain: float = 0.02) -> list[Scene]:
    """For each MGRS tile, take the newest scenes until the tile's part of the area is covered.

    A tile at the edge of an orbit swath is only partly imaged on each pass, so a
    second (older) pass from the neighbouring orbit is added when it fills a gap.
    """
    by_tile: dict[str, list[Scene]] = defaultdict(list)
    for sc in scenes:
        by_tile[sc.tile or sc.id].append(sc)
    chosen: list[Scene] = []
    for group in by_tile.values():
        group.sort(key=lambda sc: sc.datetime, reverse=True)
        geoms = [shape(sc.geometry).intersection(aoi) for sc in group]
        total = unary_union(geoms).area
        if total <= 0:
            continue
        covered = None
        for sc, g in zip(group, geoms):
            gain = g.area if covered is None else g.difference(covered).area
            if gain >= min_gain * total:
                chosen.append(sc)
                covered = g if covered is None else covered.union(g)
            if covered is not None and covered.area >= 0.98 * total:
                break
    chosen.sort(key=lambda sc: sc.datetime, reverse=True)
    return chosen


def find_scenes(settings: Settings, opts: ScanOptions) -> list[Scene]:
    aoi = load_aoi(settings.aoi_path)
    scenes = search_scenes(settings.source_config(), aoi, opts.start, opts.end, opts.max_cloud)
    if opts.mode == "latest":
        scenes = select_latest_coverage(scenes, aoi)
    return scenes[: opts.limit] if opts.limit else scenes


def imagery_coverage(scenes: list[Scene], aoi: BaseGeometry) -> dict:
    """Which part of the search area has any imagery among ``scenes``."""
    imaged = unary_union([shape(sc.geometry) for sc in scenes]).intersection(aoi) if scenes else None
    total = area_km2(aoi)
    got = area_km2(imaged) if imaged is not None else 0.0
    missing = aoi.difference(imaged) if imaged is not None else aoi
    return {
        "fraction": round(got / total, 6) if total else 0.0,
        "imaged_km2": round(got),
        "area_km2": round(total),
        "imaged": mapping(imaged.simplify(0.005)) if imaged is not None and not imaged.is_empty else None,
        "missing": mapping(missing.simplify(0.005)) if not missing.is_empty else None,
    }


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
    keep = store.chips_for_scene(scene_id)
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

    lock = threading.Lock()
    try:
        note(f"Searching {settings.source} for Sentinel-2 scenes {opts.start} .. {opts.end} (cloud <= {opts.max_cloud}%)")
        aoi = load_aoi(settings.aoi_path)
        found = search_scenes(settings.source_config(), aoi, opts.start, opts.end, opts.max_cloud)
        cov = imagery_coverage(found, aoi)
        cov.update(start=opts.start, end=opts.end, max_cloud=opts.max_cloud)
        store.set_meta("coverage", cov)
        report.area_imaged = cov["fraction"]
        scenes = select_latest_coverage(found, aoi) if opts.mode == "latest" else found
        if opts.limit:
            scenes = scenes[: opts.limit]
        report.found = len(scenes)
        note(f"Found {len(found)} images; {len(scenes)} selected "
             f"({'newest per tile' if opts.mode == 'latest' else 'every pass'}"
             f"{f', limited to {opts.limit}' if opts.limit else ''})")
        note(f"Sentinel-2 imaged {cov['fraction']:.0%} of the search area in this window "
             f"({cov['imaged_km2']:,} of {cov['area_km2']:,} km2); the rest has no images to scan")

        todo = []
        for sc in scenes:
            if not opts.reprocess and store.scene_status(sc.id) in ("done", "skipped"):
                report.already_done += 1
            else:
                todo.append(sc)
        if report.already_done:
            note(f"{report.already_done} already processed earlier; {len(todo)} to go")

        def work(sc: Scene):
            with lock:
                report.current = sc.id
            note(f"start {sc.id} ({sc.datetime[:16]}, cloud {sc.cloud_cover}%)")
            return process_scene(settings, store, sc, opts)

        done_n = 0
        with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
            futures = {pool.submit(work, sc): sc for sc in todo}
            for fut in as_completed(futures):
                sc = futures[fut]
                done_n += 1
                prefix = f"[{done_n}/{len(todo)}] {sc.id}"
                try:
                    status, added = fut.result()
                except Exception as exc:
                    log.exception("Scene %s failed", sc.id)
                    store.save_scene(sc, "failed", str(exc)[:500])
                    with lock:
                        report.failed += 1
                    note(f"{prefix}: failed: {exc}")
                    continue
                with lock:
                    if status == "skipped":
                        report.skipped += 1
                    else:
                        report.processed += 1
                        report.ships += added
                note(f"{prefix}: " + ("skipped, no cloud-free sea" if status == "skipped" else f"{added} ships"))
        report.current = ""
        note(f"Scan finished: {report.processed} processed, {report.skipped} skipped, "
             f"{report.failed} failed, {report.already_done} already done, {report.ships} ships")
    finally:
        if own_store:
            store.close()
    return report
