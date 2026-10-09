"""Web map + JSON API for ship detections."""
from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from .aoi import aoi_geojson
from .config import Settings
from .db import Store
from .pipeline import ScanOptions, ScanReport, scan

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


def _feature(d: dict) -> dict:
    props = {k: v for k, v in d.items() if k not in ("lon", "lat")}
    props["chip_url"] = f"/chips/{d['chip']}" if d.get("chip") else None
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
            "properties": props}


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or Settings()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    app = Flask(__name__, static_folder=None)
    job: dict = {"thread": None, "report": None, "error": None, "options": None}

    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(WEB_DIR, name)

    @app.get("/chips/<path:name>")
    def chips(name):
        return send_from_directory(settings.chips_dir, name)

    @app.get("/api/aoi")
    def aoi():
        return jsonify({"type": "Feature", "properties": {}, "geometry": aoi_geojson(settings.aoi_path)})

    @app.get("/api/detections")
    def detections():
        a = request.args
        try:
            rows = store.detections(
                start=a.get("start") or None, end=a.get("end") or None,
                min_length=float(a.get("min_length", 0) or 0),
                max_length=float(a.get("max_length", 1e9) or 1e9),
                min_confidence=float(a.get("min_confidence", 0) or 0),
                include_stationary=a.get("stationary", "1") != "0",
                limit=int(a.get("limit", 20000)),
            )
        except ValueError as exc:
            abort(400, str(exc))
        return jsonify({"type": "FeatureCollection", "features": [_feature(r) for r in rows]})

    @app.get("/api/scenes")
    def scenes():
        return jsonify(store.scenes(int(request.args.get("limit", 500))))

    @app.get("/api/coverage")
    def coverage():
        cov = store.get_meta("coverage")
        if cov is None:
            return jsonify({"fraction": None, "imaged": None, "missing": None})
        return jsonify(cov)

    @app.get("/api/stats")
    def stats():
        return jsonify({**store.stats(), "source": settings.source})

    @app.get("/api/scan")
    def scan_status():
        t = job["thread"]
        rep: ScanReport | None = job["report"]
        return jsonify({
            "running": bool(t and t.is_alive()),
            "options": job["options"],
            "error": job["error"],
            "report": asdict(rep) if rep else None,
        })

    @app.post("/api/scan")
    def scan_start():
        if job["thread"] and job["thread"].is_alive():
            return jsonify({"error": "a scan is already running"}), 409
        body = request.get_json(silent=True) or {}
        today = date.today()
        try:
            opts = ScanOptions(
                start=body.get("start") or (today - timedelta(days=7)).isoformat(),
                end=body.get("end") or today.isoformat(),
                max_cloud=float(body.get("max_cloud", 30)),
                limit=int(body["limit"]) if body.get("limit") else None,
                rgb_chips=bool(body.get("rgb_chips", True)),
                keep_tiles=bool(body.get("keep_tiles", True)),
                mode="all" if body.get("all_passes") else "latest",
                workers=int(body.get("workers", 3)),
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        job.update(report=ScanReport(), error=None,
                   options={k: v for k, v in asdict(opts).items() if k != "params"})

        def run():
            try:
                scan(settings, opts, store=store, on_progress=lambda r: job.update(report=r))
            except Exception as exc:  # surfaced to the UI
                log.exception("Scan failed")
                job["error"] = str(exc)

        job["thread"] = threading.Thread(target=run, daemon=True)
        job["thread"].start()
        return jsonify({"started": True, "options": job["options"]}), 202

    return app
