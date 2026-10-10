"""Search -> download tiles -> detect & measure ships -> store.

A scan covers one chosen area (config/areas.geojson). Each Sentinel-2 MGRS tile in it
uses its most recent usable image, falling back to older passes when that is clouded out.
"""
from __future__ import annotations

import logging
import shutil
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Callable

import numpy as np
import shapely
from pyproj import Transformer
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from .chips import render_chips
from .config import Settings
from .db import Store
from .detect import DETECTOR_VERSION, DetectParams
from .download import download_file, remove_scene, scene_dir
from .scene import clear_sea_fraction, detect_scene
from .stac import Scene, search_scenes
from .verify import verify_detections

log = logging.getLogger(__name__)


@dataclass
class ScanOptions:
    start: str
    end: str
    max_cloud: float = 30.0
    limit: int | None = None
    # "latest": the most recent usable image of every tile, covering the area once.
    # "all": every pass in the date range (several looks at the same place).
    mode: str = "latest"
    workers: int = 3             # tiles downloaded/processed in parallel
    min_sea_fraction: float = 0.01
    rgb_chips: bool = True       # download the true-colour image for chips and colour checks
    keep_tiles: bool = True
    reprocess: bool = False
    fallback_rounds: int = 3     # older passes tried when a tile's newest image is clouded out
    area: str | None = None      # id of the area to scan (config/areas.geojson); None = all areas
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
    area: str = ""
    area_imaged: float | None = None  # share of the area with any Sentinel-2 imagery
    log: list[str] = field(default_factory=list)


_TO_EQUAL_AREA = Transformer.from_crs("EPSG:4326", "EPSG:6933", always_xy=True)


def area_km2(geom: BaseGeometry) -> float:
    if geom.is_empty:
        return 0.0
    projected = shapely.transform(geom, lambda xy: np.column_stack(_TO_EQUAL_AREA.transform(xy[:, 0], xy[:, 1])))
    return projected.area / 1e6


def select_latest_coverage(scenes, aoi: BaseGeometry, min_gain: float = 0.02) -> list:
    """For each MGRS tile, take the newest scenes until the tile's part of the area is covered.

    A tile at the edge of an orbit swath is only partly imaged on each pass, so a
    second (older) pass from the neighbouring orbit is added when it fills a gap.
    Works on anything with ``id``, ``tile``, ``datetime`` and ``geometry``.
    """
    by_tile: dict[str, list] = defaultdict(list)
    for sc in scenes:
        by_tile[sc.tile or sc.id].append(sc)
    chosen: list = []
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


def current_scene_ids(store: Store, aoi: BaseGeometry) -> set[str]:
    """Scenes making up the "latest picture" view: per tile, the most recent analysed
    image(s) that had clear sea."""
    done = [SimpleNamespace(**r) for r in store.scenes(limit=1_000_000) if r["status"] == "done" and r["geometry"]]
    return {sc.id for sc in select_latest_coverage(done, aoi)}


def reset_data(settings: Settings, store: Store) -> dict:
    """Start fresh: delete every tracked ship, its images, the analysed-tile records and
    the downloaded tiles, so the next scan of an area analyses everything again."""
    removed = store.reset()
    for d in (settings.chips_dir, settings.tiles_dir):
        shutil.rmtree(d, ignore_errors=True)
    settings.ensure_dirs()
    log.info("Reset: removed %d ships from %d tiles", removed["ships"], removed["tiles"])
    return removed


def scan_region(settings: Settings, opts: ScanOptions) -> BaseGeometry:
    return settings.area(opts.area).geom if opts.area else settings.search_area()


def find_scenes(settings: Settings, opts: ScanOptions) -> list[Scene]:
    aoi = scan_region(settings, opts)
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
    # Every area the tile touches, not just the one being scanned: a tile is analysed
    # once, so a scan of the neighbouring area must not find it half done.
    aoi = settings.search_area()
    sdir = scene_dir(settings.tiles_dir, scene.id)

    scl_path = download_file(scene.scl_href, sdir / "SCL.tif", src_cfg)
    sea = clear_sea_fraction(scl_path, aoi)
    if sea < opts.min_sea_fraction:
        store.save_scene(scene, "skipped", f"clear sea {sea:.1%}", sea_fraction=sea,
                         detector_version=DETECTOR_VERSION)
        if not opts.keep_tiles:
            remove_scene(settings.tiles_dir, scene.id)
        return "skipped", 0

    nir_path = download_file(scene.nir_href, sdir / "B08.tif", src_cfg)
    log.info("  detecting ships (clear sea %.0f%%)", sea * 100)
    rejected: Counter = Counter()
    dets = detect_scene(nir_path, scl_path, aoi, scene.nir_scale, scene.nir_offset, opts.params, rejected)

    tci = None
    if dets and opts.rgb_chips and scene.rgb_href:
        try:
            tci = download_file(scene.rgb_href, sdir / "TCI.tif", src_cfg)
        except Exception as exc:
            log.warning("  true-colour download failed (%s); colour checks skipped, chips use NIR", exc)
    dets = verify_detections(dets, nir_path, tci, scene.nir_scale, scene.nir_offset, rejected)
    if rejected:
        log.info("  %s: %d ships kept; rejected %s", scene.id, len(dets),
                 ", ".join(f"{n} {why}" for why, n in rejected.most_common()))

    chips: list = [None] * len(dets)
    if dets:
        image = tci or nir_path
        paths = render_chips(image, dets, settings.chips_dir / scene.id, scene.datetime[:10])
        chips = [p.relative_to(settings.chips_dir).as_posix() if p else None for p in paths]

    store.delete_scene_detections(scene.id)  # replaces results of an earlier run
    added = store.add_detections(scene, dets, chips)
    _prune_chips(settings, store, scene.id)
    why = ", ".join(f"{n} {r}" for r, n in rejected.most_common())
    store.save_scene(scene, "done", f"{len(dets)} detected, {added} new" + (f"; rejected {why}" if why else ""),
                     sea_fraction=sea, n_ships=added, detector_version=DETECTOR_VERSION)
    if not opts.keep_tiles:
        remove_scene(settings.tiles_dir, scene.id)
    return "done", added


def _prune_chips(settings: Settings, store: Store, scene_id: str) -> None:
    """Delete chips of detections that were dropped as duplicates of another tile."""
    keep = store.chips_for_scene(scene_id)
    folder = settings.chips_dir / scene_id
    for f in folder.glob("*.png") if folder.exists() else []:
        if f.relative_to(settings.chips_dir).as_posix() not in keep:
            f.unlink()
            f.with_suffix(".tif").unlink(missing_ok=True)


def scan(settings: Settings, opts: ScanOptions, store: Store | None = None,
         on_progress: Callable[[ScanReport], None] | None = None) -> ScanReport:
    settings.ensure_dirs()
    own_store = store is None
    store = store or Store(settings.db_path)
    report = ScanReport()
    lock = threading.Lock()

    def note(msg: str) -> None:
        log.info(msg)
        with lock:
            report.log.append(msg)
            del report.log[:-200]
        if on_progress:
            on_progress(report)

    try:
        area = settings.area(opts.area) if opts.area else None
        aoi = area.geom if area else settings.search_area()
        name = area.name if area else "all areas"
        report.area = opts.area or ""
        note(f"{name}: searching {settings.source} for Sentinel-2 images {opts.start} .. {opts.end} "
             f"(cloud <= {opts.max_cloud}%)")
        found = search_scenes(settings.source_config(), aoi, opts.start, opts.end, opts.max_cloud)
        cov = imagery_coverage(found, aoi)
        cov.update(start=opts.start, end=opts.end, max_cloud=opts.max_cloud)
        if area:
            store.set_meta(f"coverage:{area.id}", cov)
            store.set_meta(f"area:{area.id}", {"status": "scanning", "started": time.time(),
                                               "start": opts.start, "end": opts.end})
        report.area_imaged = cov["fraction"]
        scenes = select_latest_coverage(found, aoi) if opts.mode == "latest" else list(found)
        if opts.limit:
            scenes = scenes[: opts.limit]
        report.found = len(scenes)
        n_tiles = len({sc.tile for sc in found})
        note(f"Found {len(found)} images of {n_tiles} tiles; {len(scenes)} selected "
             f"({'most recent per tile' if opts.mode == 'latest' else 'every pass'}"
             f"{f', limited to {opts.limit}' if opts.limit else ''})")
        note(f"Sentinel-2 imaged {cov['fraction']:.0%} of {name} in this window "
             f"({cov['imaged_km2']:,} of {cov['area_km2']:,} km2)")

        statuses: dict[str, str] = {}
        _run_batch(settings, store, scenes, opts, report, note, statuses, lock)

        # A tile whose newest image was all cloud: fall back to its next older pass.
        if opts.mode == "latest" and not opts.limit:
            tried = {sc.id for sc in scenes}
            tiles = {sc.tile for sc in scenes}
            for _ in range(opts.fallback_rounds):
                good_tiles = {sc.tile for sc in found if statuses.get(sc.id) == "done"}
                need = tiles - good_tiles
                cands = [sc for sc in found if sc.tile in need and sc.id not in tried]
                nxt = select_latest_coverage(cands, aoi)
                if not nxt:
                    break
                note(f"{len(need)} tiles had no clear sea in their newest image; trying {len(nxt)} older passes")
                tried |= {sc.id for sc in nxt}
                report.found += len(nxt)
                _run_batch(settings, store, nxt, opts, report, note, statuses, lock)

        note(f"Finished {name}: {report.processed} analysed, {report.skipped} cloud/land only, "
             f"{report.failed} failed, {report.already_done} already done, {report.ships} new ships")
        if area:
            store.set_meta(f"area:{area.id}", {
                "status": "done", "finished": time.time(), "start": opts.start, "end": opts.end,
                "tiles": report.processed + report.already_done, "failed": report.failed,
                "imaged": cov["fraction"]})
        report.current = ""
    except Exception as exc:
        if opts.area:
            store.set_meta(f"area:{opts.area}", {"status": "failed", "finished": time.time(),
                                                 "start": opts.start, "end": opts.end, "error": str(exc)[:200]})
        raise
    finally:
        if own_store:
            store.close()
    return report


def _run_batch(settings, store, scenes, opts, report, note, statuses, lock) -> None:
    todo, outdated = [], []
    for sc in scenes:
        status = store.scene_status(sc.id)
        current = store.scene_version(sc.id) >= DETECTOR_VERSION
        if not opts.reprocess and status in ("done", "skipped") and current:
            report.already_done += 1
            statuses[sc.id] = status
        else:
            todo.append(sc)
            if status in ("done", "skipped") and not current:
                outdated.append(sc)
    # Clear old results first so they can't shadow new detections as cross-tile duplicates.
    for sc in outdated:
        store.delete_scene_detections(sc.id)
    if outdated:
        note(f"{len(outdated)} tiles were analysed by an older detector and will be redone")
    if not todo:
        return

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
                statuses[sc.id] = "failed"
                with lock:
                    report.failed += 1
                note(f"{prefix}: failed: {exc}")
                continue
            statuses[sc.id] = status
            with lock:
                if status == "skipped":
                    report.skipped += 1
                else:
                    report.processed += 1
                    report.ships += added
            note(f"{prefix}: " + ("no clear sea (cloud/land)" if status == "skipped" else f"{added} ships"))

