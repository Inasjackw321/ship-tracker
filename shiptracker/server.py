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
from shapely.geometry import mapping, shape

from .annotate import annotated_png, bundle_zip, chip_image, file_stem, geotiff_path, quality
from .config import Settings
from .db import Store
from .aoi import CUSTOM_GROUP, Area, new_area_id, save_areas
from .pipeline import ScanOptions, ScanReport, area_km2, current_scene_ids, reset_data, scan

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "web"
MAX_AREA_KM2 = 3_000_000  # larger than the biggest built-in area; more is a mistake


def _feature(d: dict) -> dict:
    props = {k: v for k, v in d.items() if k not in ("lon", "lat")}
    if d.get("chip"):
        props["chip"] = d["chip"].replace("\\", "/")  # rows written on Windows before paths were normalised
    props["chip_url"] = f"/chips/{props['chip']}" if d.get("chip") else None
    if d.get("method") == "manual":  # re-drawn with the hand-placed ruler
        props["chip_url"] = f"/api/detections/{d['id']}/chip.png?v={int(d.get('updated_at') or 0)}"
    props["adjusted"] = d.get("method") == "manual"
    props["quality"], props["quality_why"] = quality(d)
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
        clean = bool(request.args.get("clean"))
        return Response(annotated_png(rec, settings.chips_dir, clean), mimetype="image/png",
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
        chip_image(rec, settings.chips_dir, clean=bool(request.args.get("clean"))).save(buf, "PNG")
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
        return Response(bundle_zip(ships, settings.chips_dir, bool(body.get("clean"))), mimetype="application/zip",
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
        custom = {a.id for a in settings.custom_areas()}
        feats = []
        for a in settings.areas():
            feats.append(_area_feature(a, a.id in custom, lons, lats, running))
        return jsonify({"type": "FeatureCollection", "features": feats})

    def _area_feature(a: Area, custom: bool, lons, lats, running: bool) -> dict:
        status = store.get_meta(f"area:{a.id}") or {}
        if status.get("status") == "scanning" and not (running and job["options"].get("area") == a.id):
            status["status"] = "interrupted"  # the app was closed during that scan
        n = int(contains_xy(a.geom, lons, lats).sum()) if lons else 0
        return {"type": "Feature", "geometry": mapping(a.geom), "properties": {
            "id": a.id, "name": a.name, "group": a.group, "ships": n, "custom": custom,
            "km2": round(area_km2(a.geom)), **status}}

    @app.post("/api/areas")
    def add_area():
        """Save an area drawn on the map: {name, geometry (GeoJSON Polygon)}."""
        body = request.get_json(silent=True) or {}
        name = str(body.get("name") or "").strip()[:80] or f"My area {len(settings.custom_areas()) + 1}"
        try:
            geom = shape(body["geometry"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return jsonify({"error": "draw an area on the map first"}), 400
        if geom.geom_type not in ("Polygon", "MultiPolygon"):
            return jsonify({"error": "an area must be a polygon"}), 400
        minx, miny, maxx, maxy = geom.bounds
        if not (-180 <= minx <= maxx <= 180 and -85 <= miny <= maxy <= 85):
            return jsonify({"error": "the area is outside the map"}), 400
        if not geom.is_valid:
            geom = geom.buffer(0)  # e.g. a shape whose outline crosses itself
        km2 = area_km2(geom)
        if km2 < 1:
            return jsonify({"error": "the area is too small; click at least three points around it"}), 400
        if km2 > MAX_AREA_KM2:
            return jsonify({"error": f"the area is too big ({km2:,.0f} km²); draw at most "
                                     f"{MAX_AREA_KM2:,} km²"}), 400
        taken = {a.id for a in settings.areas()}
        area = Area(new_area_id(name, taken), name, CUSTOM_GROUP, geom)
        save_areas(settings.custom_areas_path, list(settings.custom_areas()) + [area])
        return jsonify(_area_feature(area, True, [], [], False)), 201

    @app.delete("/api/areas/<area_id>")
    def delete_area(area_id: str):
        """Remove an area you drew. Ships already found stay on the map."""
        mine = list(settings.custom_areas())
        if area_id not in {a.id for a in mine}:
            return jsonify({"error": "only areas you drew can be removed"}), 400
        if job["thread"] and job["thread"].is_alive() and job["options"].get("area") == area_id:
            return jsonify({"error": "this area is being scanned"}), 409
        save_areas(settings.custom_areas_path, [a for a in mine if a.id != area_id])
        for key in (f"area:{area_id}", f"coverage:{area_id}"):
            store.delete_meta(key)
        return jsonify({"deleted": area_id})

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
                workers=int(body.get("workers") or 0),
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
