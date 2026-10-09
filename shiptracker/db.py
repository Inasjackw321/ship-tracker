"""SQLite storage for processed scenes and ship detections."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS scenes (
    id TEXT PRIMARY KEY,
    datetime TEXT NOT NULL,
    ts REAL NOT NULL,
    tile TEXT,
    cloud_cover REAL,
    aoi_overlap REAL,
    sea_fraction REAL,
    status TEXT NOT NULL,          -- done | skipped | failed
    note TEXT,
    n_ships INTEGER DEFAULT 0,
    processed_at REAL,
    detector_version INTEGER DEFAULT 1,
    geometry TEXT
);
CREATE TABLE IF NOT EXISTS detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id TEXT NOT NULL REFERENCES scenes(id),
    datetime TEXT NOT NULL,
    ts REAL NOT NULL,
    lon REAL NOT NULL,
    lat REAL NOT NULL,
    length_m REAL NOT NULL,
    width_m REAL NOT NULL,
    heading_deg REAL,
    bow_lon REAL, bow_lat REAL, stern_lon REAL, stern_lat REAL,
    peak_reflectance REAL,
    contrast REAL,
    snr REAL,
    npix INTEGER,
    confidence REAL,
    stationary INTEGER DEFAULT 0,  -- seen at the same spot on another date
    chip TEXT,
    length_err_m REAL,             -- 1-sigma uncertainty
    width_err_m REAL,
    method TEXT,                   -- fit | profile | manual
    auto_json TEXT,                -- the automatic measurement, kept when adjusted by hand
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_det_ts ON detections(ts);
CREATE INDEX IF NOT EXISTS idx_det_pos ON detections(lat, lon);
"""

DUP_SECONDS = 180      # same datatake: overlapping tiles see the same ship
DUP_METERS = 60
STATIONARY_METERS = 40


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _deg(meters: float, lat: float) -> tuple[float, float]:
    dlat = meters / 111_320
    return dlat, dlat / max(math.cos(math.radians(lat)), 0.01)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()  # one connection shared by scan workers and web requests
        with self.lock:
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(scenes)")}
            if "detector_version" not in cols:  # databases from before versioning
                self.conn.execute("ALTER TABLE scenes ADD COLUMN detector_version INTEGER DEFAULT 1")
            dcols = {r[1] for r in self.conn.execute("PRAGMA table_info(detections)")}
            for name, typ in (("length_err_m", "REAL"), ("width_err_m", "REAL"), ("method", "TEXT"),
                              ("auto_json", "TEXT"), ("updated_at", "REAL")):
                if name not in dcols:  # databases from before measurement uncertainties
                    self.conn.execute(f"ALTER TABLE detections ADD COLUMN {name} {typ}")
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # scenes ---------------------------------------------------------------

    def _query(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def set_meta(self, key: str, value) -> None:
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def get_meta(self, key: str, default=None):
        rows = self._query("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def scene_status(self, scene_id: str) -> str | None:
        rows = self._query("SELECT status FROM scenes WHERE id=?", (scene_id,))
        row = rows[0] if rows else None
        return row["status"] if row else None

    def scene_version(self, scene_id: str) -> int:
        rows = self._query("SELECT detector_version FROM scenes WHERE id=?", (scene_id,))
        return int(rows[0]["detector_version"] or 1) if rows else 0

    def save_scene(self, scene, status: str, note: str = "", sea_fraction: float | None = None,
                   n_ships: int = 0, detector_version: int = 1) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO scenes
                   (id, datetime, ts, tile, cloud_cover, aoi_overlap, sea_fraction, status, note,
                    n_ships, processed_at, detector_version, geometry)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (scene.id, scene.datetime, _ts(scene.datetime), scene.tile, scene.cloud_cover,
                 scene.aoi_overlap, sea_fraction, status, note, n_ships, time.time(),
                 detector_version, json.dumps(scene.geometry)),
            )

    def scenes(self, limit: int = 500) -> list[dict]:
        rows = self._query(
            "SELECT id, datetime, tile, cloud_cover, aoi_overlap, sea_fraction, status, note, n_ships,"
            " processed_at, geometry FROM scenes ORDER BY ts DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            d["geometry"] = json.loads(d["geometry"]) if d["geometry"] else None
            out.append(d)
        return out

    # detections -----------------------------------------------------------

    def add_detections(self, scene, geodets, chips) -> int:
        """Insert detections, skipping ships already recorded from an overlapping tile
        of the same pass. Returns the number inserted."""
        ts = _ts(scene.datetime)
        added = 0
        with self.lock, self.conn:
            for g, chip in zip(geodets, chips):
                d = g.det
                dlat, dlon = _deg(DUP_METERS, g.lat)
                dup = self.conn.execute(
                    "SELECT 1 FROM detections WHERE ABS(ts-?)<? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
                    " AND scene_id<>? LIMIT 1",
                    (ts, DUP_SECONDS, g.lat - dlat, g.lat + dlat, g.lon - dlon, g.lon + dlon, scene.id),
                ).fetchone()
                if dup:
                    continue
                slat, slon = _deg(STATIONARY_METERS, g.lat)
                box = (g.lat - slat, g.lat + slat, g.lon - slon, g.lon + slon)
                seen = self.conn.execute(
                    "SELECT id FROM detections WHERE ABS(ts-?)>=? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                    (ts, DUP_SECONDS, *box),
                ).fetchall()
                if seen:
                    self.conn.executemany("UPDATE detections SET stationary=1 WHERE id=?", [(r["id"],) for r in seen])
                self.conn.execute(
                    """INSERT INTO detections
                       (scene_id, datetime, ts, lon, lat, length_m, width_m, heading_deg,
                        bow_lon, bow_lat, stern_lon, stern_lat, peak_reflectance, contrast, snr,
                        npix, confidence, stationary, chip, length_err_m, width_err_m, method)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (scene.id, scene.datetime, ts, g.lon, g.lat, d.length_m, d.width_m, d.heading_deg,
                     g.bow_lon, g.bow_lat, g.stern_lon, g.stern_lat, round(d.peak_reflectance, 4),
                     round(d.contrast, 4), round(d.snr, 1), d.npix, d.confidence, int(bool(seen)), chip,
                     d.length_err_m, d.width_err_m, d.method),
                )
                added += 1
        return added

    def delete_scene_detections(self, scene_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM detections WHERE scene_id=?", (scene_id,))

    def detections(self, start: str | None = None, end: str | None = None, min_length: float = 0,
                   max_length: float = 1e9, min_confidence: float = 0, include_stationary: bool = True,
                   limit: int = 20000, scene_ids: set[str] | None = None) -> list[dict]:
        q = ["SELECT * FROM detections WHERE length_m BETWEEN ? AND ? AND confidence >= ?"]
        args: list = [min_length, max_length, min_confidence]
        if scene_ids is not None:
            if not scene_ids:
                return []
            q.append(f"AND scene_id IN ({','.join('?' * len(scene_ids))})")
            args.extend(sorted(scene_ids))
        if start:
            q.append("AND ts >= ?")
            args.append(_ts(start if "T" in start else start + "T00:00:00+00:00"))
        if end:
            q.append("AND ts <= ?")
            args.append(_ts(end if "T" in end else end + "T23:59:59+00:00"))
        if not include_stationary:
            q.append("AND stationary = 0")
        q.append("ORDER BY ts DESC, length_m DESC LIMIT ?")
        args.append(limit)
        return [dict(r) for r in self._query(" ".join(q), args)]

    def chips_for_scene(self, scene_id: str) -> set[str]:
        return {r["chip"] for r in self._query("SELECT chip FROM detections WHERE scene_id=?", (scene_id,))}

    def stats(self) -> dict:
        def one(sql: str):
            return self._query(sql)[0][0]

        return {
            "scenes_done": one("SELECT COUNT(*) FROM scenes WHERE status='done'"),
            "scenes_skipped": one("SELECT COUNT(*) FROM scenes WHERE status='skipped'"),
            "scenes_failed": one("SELECT COUNT(*) FROM scenes WHERE status='failed'"),
            "detections": one("SELECT COUNT(*) FROM detections"),
            "first": one("SELECT MIN(datetime) FROM detections"),
            "last": one("SELECT MAX(datetime) FROM detections"),
        }

    def get_detection(self, det_id: int) -> dict | None:
        rows = self._query("SELECT * FROM detections WHERE id=?", (det_id,))
        return dict(rows[0]) if rows else None

    # hand corrections -----------------------------------------------------

    _GEOM = ("lat", "lon", "length_m", "width_m", "heading_deg", "bow_lat", "bow_lon", "stern_lat",
             "stern_lon", "length_err_m", "width_err_m", "method")

    def set_manual_measurement(self, det_id: int, stern: tuple[float, float], bow: tuple[float, float],
                               width_m: float | None = None) -> dict | None:
        """Replace a ship's measurement with hull ends placed by hand (lat, lon each).
        The automatic measurement is kept and can be restored."""
        from pyproj import Geod

        rec = self.get_detection(det_id)
        if rec is None:
            return None
        az, _, dist = Geod(ellps="WGS84").inv(stern[1], stern[0], bow[1], bow[0])
        auto = rec["auto_json"] or json.dumps({k: rec[k] for k in self._GEOM})
        width = float(width_m) if width_m is not None else rec["width_m"]
        with self.lock, self.conn:
            self.conn.execute(
                """UPDATE detections SET lat=?, lon=?, length_m=?, width_m=?, heading_deg=?, bow_lat=?, bow_lon=?,
                   stern_lat=?, stern_lon=?, length_err_m=?, width_err_m=?, method='manual', auto_json=?, updated_at=?
                   WHERE id=?""",
                ((stern[0] + bow[0]) / 2, (stern[1] + bow[1]) / 2, round(dist, 1), round(width, 1), round(az % 180, 1),
                 bow[0], bow[1], stern[0], stern[1], None, None, auto, time.time(), det_id))
        return self.get_detection(det_id)

    def reset_measurement(self, det_id: int) -> dict | None:
        rec = self.get_detection(det_id)
        if rec is None or not rec["auto_json"]:
            return rec
        auto = json.loads(rec["auto_json"])
        sets = ", ".join(f"{k}=?" for k in auto)
        with self.lock, self.conn:
            self.conn.execute(f"UPDATE detections SET {sets}, auto_json=NULL, updated_at=? WHERE id=?",
                              (*auto.values(), time.time(), det_id))
        return self.get_detection(det_id)

