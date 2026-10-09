"""Command line: python -m shiptracker <command> ..."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import SOURCES, Settings


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _add_scan_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--start", default=None, help="start date YYYY-MM-DD (default: 7 days ago)")
    p.add_argument("--end", default=None, help="end date YYYY-MM-DD (default: today)")
    p.add_argument("--days", type=int, default=30,
                   help="look-back window when --start is omitted (each tile uses its latest image in it)")
    p.add_argument("--max-cloud", type=float, default=30.0, help="max scene cloud cover %% (default 30)")
    p.add_argument("--limit", type=int, default=None, help="process at most N scenes (newest first)")


def _settings(args) -> Settings:
    s = Settings()
    if args.data:
        s.data_dir = Path(args.data)
    if args.aoi:
        s.aoi_path = Path(args.aoi)
    if args.source:
        s.source = args.source
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="shiptracker", description="Sentinel-2 ship detection and sizing")
    ap.add_argument("--data", help="data directory (tiles, chips, database)")
    ap.add_argument("--aoi", help="AOI GeoJSON (default: config/aoi.geojson)")
    ap.add_argument("--source", choices=sorted(SOURCES), help="imagery source (default: earth-search)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", help="list Sentinel-2 scenes over the area (no download)")
    _add_scan_args(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--all-passes", action="store_true", help="list every pass, not just the newest per tile")

    p = sub.add_parser("scan", help="download tiles, detect and measure ships")
    _add_scan_args(p)
    p.add_argument("--no-rgb", action="store_true", help="skip the true-colour download; chips use NIR")
    p.add_argument("--delete-tiles", action="store_true", help="delete each tile after it is processed")
    p.add_argument("--reprocess", action="store_true", help="re-run scenes that were already processed")
    p.add_argument("--all-passes", action="store_true",
                   help="scan every pass in the date range, not just the newest image of each tile")
    p.add_argument("--workers", type=int, default=3, help="tiles processed in parallel (default 3)")
    p.add_argument("--no-s3", action="store_true", help="skip the Sentinel-3 open-sea check")
    p.add_argument("--priority-only", action="store_true", help="scan only the priority regions")

    p = sub.add_parser("watch", help="scan for new imagery repeatedly")
    p.add_argument("--every-hours", type=float, default=6.0)
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--days", type=int, default=30, help="look-back window on each run")
    p.add_argument("--no-s3", action="store_true")
    p.add_argument("--max-cloud", type=float, default=30.0)
    p.add_argument("--no-rgb", action="store_true")
    p.add_argument("--delete-tiles", action="store_true")

    p = sub.add_parser("detect", help="run detection on local B08 + SCL GeoTIFFs")
    p.add_argument("nir", help="B08 (10 m NIR) GeoTIFF")
    p.add_argument("scl", help="SCL GeoTIFF")
    p.add_argument("--rgb", help="true-colour GeoTIFF for chips")
    p.add_argument("--scale", type=float, default=1e-4)
    p.add_argument("--offset", type=float, default=-0.1, help="-0.1 for processing baseline >= 04.00, else 0")
    p.add_argument("--chips", help="directory to write chips into")

    p = sub.add_parser("serve", help="web map and API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    settings = _settings(args)

    if args.cmd in ("search", "scan"):
        from .pipeline import ScanOptions, find_scenes, scan
        opts = ScanOptions(
            start=args.start or _days_ago(args.days), end=args.end or date.today().isoformat(),
            max_cloud=args.max_cloud, limit=args.limit,
        )
        if args.cmd == "search":
            opts.mode = "all" if args.all_passes else "latest"
            scenes = find_scenes(settings, opts)
            if args.json:
                print(json.dumps([s.to_dict() for s in scenes], indent=2))
            else:
                for s in scenes:
                    print(f"{s.datetime[:19]}  {s.id:45s} tile={s.tile} cloud={s.cloud_cover}% "
                          f"in-area={s.aoi_overlap:.0%}")
                print(f"{len(scenes)} scenes")
            return 0
        opts.rgb_chips = not args.no_rgb
        opts.keep_tiles = not args.delete_tiles
        opts.reprocess = args.reprocess
        opts.mode = "all" if args.all_passes else "latest"
        opts.workers = args.workers
        opts.sentinel3 = not args.no_s3
        opts.priority_only = args.priority_only
        rep = scan(settings, opts)
        return 1 if rep.failed and not rep.processed else 0

    if args.cmd == "watch":
        from .pipeline import ScanOptions, scan
        while True:
            now = datetime.now(timezone.utc)
            opts = ScanOptions(start=(now - timedelta(days=args.days)).date().isoformat(),
                               end=now.date().isoformat(), max_cloud=args.max_cloud,
                               rgb_chips=not args.no_rgb, keep_tiles=not args.delete_tiles,
                               workers=args.workers, sentinel3=not args.no_s3)
            try:
                scan(settings, opts)
            except Exception:
                logging.exception("Scan failed; will retry next cycle")
            logging.info("Next scan in %.1f h", args.every_hours)
            time.sleep(args.every_hours * 3600)

    if args.cmd == "detect":

        from .chips import render_chips
        from .scene import detect_scene
        dets = detect_scene(args.nir, args.scl, settings.search_area(), args.scale, args.offset)
        if args.chips:
            render_chips(Path(args.rgb or args.nir), dets, Path(args.chips))
        out = [{"lon": round(g.lon, 6), "lat": round(g.lat, 6), "length_m": g.det.length_m,
                "width_m": g.det.width_m, "heading_deg": g.det.heading_deg,
                "confidence": g.det.confidence} for g in dets]
        print(json.dumps(out, indent=2))
        return 0

    if args.cmd == "serve":
        from .server import create_app
        create_app(settings).run(host=args.host, port=args.port, threaded=True)
        return 0
    return 2


def run() -> int:
    import requests
    try:
        return main()
    except requests.RequestException as exc:
        logging.error("Network error talking to the imagery source: %s", exc)
        logging.error("Check internet access to the STAC API / imagery bucket, or try --source planetary-computer")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(run())
