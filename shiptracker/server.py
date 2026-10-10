"""Web map + JSON API for ship detections."""
from __future__ import annotations

import io
import json
import logging
import threading
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, send_from_directory
from shapely import contains_xy
from shapely.geometry import mapping

from .annotate import annotated_png, bundle_zip, chip_image, file_stem, geotiff_path
from .config import Settings
from .db import Store
from .pipeline import ScanOptions, ScanReport, current_scene_ids, reset_data, scan

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"


def _feature(d: dict) -> dict:
    props = {k: v for k, v in d.items() if k not in ("lon", "lat")}
    if d.get("chip"):
        props["chip"] = d["chip"].replace("\\", "/")  # rows written on Windows before paths were normalised
    props["chip_url"] = f"/chips/{props['chip']}" if d.get("chip") else None
    if d.get("method") == "manual":  # re-drawn with the hand-placed ruler
        props["chip_url"] = f"/api/detections/{d['id']}/chip.png?v={int(d.get('updated_at') or 0)}"
    props["adjusted"] = d.get("method") == "manual"
    props.pop("auto_json", None)
    if d.get("auto_json"):
        props["auto"] = json.loads(d["auto_json"])
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
            "properties": props}


def create_app(settings: Settings | None = None, scan_defaults: dict | None = None) -> Flask:
    """``scan_defaults`` fill in whatever a scan request from the map leaves out
    (``days``, ``max_cloud``, ``workers``, ``keep_tiles``, ``all_passes``, ``limit``)."""
    settings = settings or Settings()
    defaults = {"days": 30, "max_cloud": 30, "keep_tiles": False, **(scan_defaults or {})}
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

    def latest_view() -> bool:
        return request.args.get("view", "latest") != "all"

    @app.get("/api/detections")
    def detections():
        a = request.args
        ids = current_scene_ids(store, settings.search_area()) if latest_view() else None
        try:
            rows = store.detections(
                start=a.get("start") or None, end=a.get("end") or None,
                min_length=float(a.get("min_length", 0) or 0),
                max_length=float(a.get("max_length", 1e9) or 1e9),
                min_confidence=float(a.get("min_confidence", 0) or 0),
                include_stationary=a.get("stationary", "1") != "0",
                limit=int(a.get("limit", 20000)),
                scene_ids=ids,
            )
        except ValueError as exc:
            abort(400, str(exc))
        feats = [_feature(r) for r in rows]
        for f in feats:  # tell the UI which ships have a clean GeoTIFF to download
            f["properties"]["has_tif"] = geotiff_path(f["properties"], settings.chips_dir) is not None
        return jsonify({"type": "FeatureCollection", "features": feats})

    @app.get("/api/scenes")
    def scenes():
        rows = store.scenes(int(request.args.get("limit", 5000)))
        if latest_view():
            ids = current_scene_ids(store, settings.search_area())
            rows = [r for r in rows if r["id"] in ids]
        return jsonify(rows)

    def _png_response(rec: dict | None) -> Response:
        if rec is None:
            abort(404)
        name = file_stem(rec) + ".png"
        disposition = "attachment" if request.args.get("dl") else "inline"
        return Response(annotated_png(rec, settings.chips_dir), mimetype="image/png",
                        headers={"Content-Disposition": f'{disposition}; filename="{name}"'})

    @app.get("/api/detections/<int:det_id>/image.png")
    def detection_png(det_id: int):
        return _png_response(store.get_detection(det_id))

    @app.get("/api/detections/<int:det_id>/chip.png")
    def detection_chip(det_id: int):
        rec = store.get_detection(det_id)
        if rec is None:
            abort(404)
        buf = io.BytesIO()
        chip_image(rec, settings.chips_dir).save(buf, "PNG")
        return Response(buf.getvalue(), mimetype="image/png")

    def _feature_response(rec):
        if rec is None:
            abort(404)
        f = _feature(rec)
        f["properties"]["has_tif"] = geotiff_path(rec, settings.chips_dir) is not None
        return jsonify(f)

    @app.post("/api/detections/<int:det_id>/measurement")
    def set_measurement(det_id: int):
        """Body: {"stern": [lat, lon], "bow": [lat, lon], "width_m": optional}."""
        body = request.get_json(silent=True) or {}
        try:
            stern = tuple(float(v) for v in body["stern"])
            bow = tuple(float(v) for v in body["bow"])
            width = float(body["width_m"]) if body.get("width_m") not in (None, "") else None
            if len(stern) != 2 or len(bow) != 2 or (width is not None and not 0 < width < 200):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            abort(400, "expected stern and bow as [lat, lon], and an optional width_m")
        return _feature_response(store.set_manual_measurement(det_id, stern, bow, width))

    @app.post("/api/detections/<int:det_id>/measurement/reset")
    def reset_measurement(det_id: int):
        return _feature_response(store.reset_measurement(det_id))

    @app.get("/api/detections/<int:det_id>/image.tif")
    def detection_tif(det_id: int):
        rec = store.get_detection(det_id)
        tif = geotiff_path(rec, settings.chips_dir) if rec else None
        if tif is None:
            abort(404, "No GeoTIFF saved for this ship (detected before GeoTIFFs were added)")
        return send_file(tif, mimetype="image/tiff", as_attachment=True,
                         download_name=file_stem(rec) + ".tif")

    @app.post("/api/download.zip")
    def download_zip():
        body = request.get_json(silent=True) or {}
        try:
            ships = [r for r in (store.get_detection(int(i)) for i in body.get("ids", body.get("s2", []))[:500]) if r]
        except (TypeError, ValueError):
            abort(400, "ids must be integers")
        if not ships:
            abort(400, "nothing selected")
        return Response(bundle_zip(ships, settings.chips_dir), mimetype="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="ships_{len(ships)}.zip"'})

    @app.get("/api/coverage")
    def coverage():
        """Imaged / not-imaged parts of each scanned area (from its last scan's search)."""
        return jsonify({a.id: store.get_meta(f"coverage:{a.id}") for a in settings.areas()
                        if store.get_meta(f"coverage:{a.id}")})

    @app.get("/api/areas")
    def areas():
        """The areas, with their scan status and how many ships the map currently shows in each."""
        ids = current_scene_ids(store, settings.search_area())
        ships = store.detections(scene_ids=ids)
        lons = [r["lon"] for r in ships]
        lats = [r["lat"] for r in ships]
        running = job["thread"] is not None and job["thread"].is_alive()
        feats = []
        for a in settings.areas():
            status = store.get_meta(f"area:{a.id}") or {}
            if status.get("status") == "scanning" and not (running and job["options"].get("area") == a.id):
                status["status"] = "interrupted"  # the app was closed during that scan
            n = int(contains_xy(a.geom, lons, lats).sum()) if ships else 0
            feats.append({"type": "Feature", "geometry": mapping(a.geom), "properties": {
                "id": a.id, "name": a.name, "group": a.group, "ships": n, **status}})
        return jsonify({"type": "FeatureCollection", "features": feats})

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
            "defaults": {"days": defaults["days"], "max_cloud": defaults["max_cloud"]},
        })

    @app.post("/api/reset")
    def reset():
        """Delete all tracked ships and start fresh."""
        if job["thread"] and job["thread"].is_alive():
            return jsonify({"error": "wait for the running scan to finish first"}), 409
        removed = reset_data(settings, store)
        job.update(report=None, error=None, options=None)
        return jsonify({"reset": True, **removed})

    @app.post("/api/scan")
    def scan_start():
        if job["thread"] and job["thread"].is_alive():
            return jsonify({"error": "a scan is already running"}), 409
        body = {**defaults, **{k: v for k, v in (request.get_json(silent=True) or {}).items() if v is not None}}
        today = date.today()
        try:
            settings.area(body.get("area") or "")
        except KeyError:
            return jsonify({"error": "choose an area to scan", "areas": [a.id for a in settings.areas()]}), 400
        try:
            if body.get("days") and not body.get("start"):
                body["start"] = (today - timedelta(days=int(body["days"]))).isoformat()
            opts = ScanOptions(
                area=body["area"],
                start=body.get("start") or (today - timedelta(days=30)).isoformat(),
                end=body.get("end") or today.isoformat(),
                max_cloud=float(body.get("max_cloud", 30)),
                limit=int(body["limit"]) if body.get("limit") else None,
                rgb_chips=bool(body.get("rgb_chips", True)),
                keep_tiles=bool(body.get("keep_tiles")),
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
