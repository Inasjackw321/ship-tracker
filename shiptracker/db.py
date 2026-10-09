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
    chip TEXT
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
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # scenes ---------------------------------------------------------------

    def scene_status(self, scene_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM scenes WHERE id=?", (scene_id,)).fetchone()
        return row["status"] if row else None

    def save_scene(self, scene, status: str, note: str = "", sea_fraction: float | None = None,
                   n_ships: int = 0) -> None:
        with self.lock, self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO scenes
                   (id, datetime, ts, tile, cloud_cover, aoi_overlap, sea_fraction, status, note,
                    n_ships, processed_at, geometry)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (scene.id, scene.datetime, _ts(scene.datetime), scene.tile, scene.cloud_cover,
                 scene.aoi_overlap, sea_fraction, status, note, n_ships, time.time(),
                 json.dumps(scene.geometry)),
            )

    def scenes(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, datetime, tile, cloud_cover, aoi_overlap, sea_fraction, status, note, n_ships,"
            " processed_at, geometry FROM scenes ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
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
                        npix, confidence, stationary, chip)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (scene.id, scene.datetime, ts, g.lon, g.lat, d.length_m, d.width_m, d.heading_deg,
                     g.bow_lon, g.bow_lat, g.stern_lon, g.stern_lat, round(d.peak_reflectance, 4),
                     round(d.contrast, 4), round(d.snr, 1), d.npix, d.confidence, int(bool(seen)), chip),
                )
                added += 1
        return added

    def delete_scene_detections(self, scene_id: str) -> None:
        with self.lock, self.conn:
            self.conn.execute("DELETE FROM detections WHERE scene_id=?", (scene_id,))

    def detections(self, start: str | None = None, end: str | None = None, min_length: float = 0,
                   max_length: float = 1e9, min_confidence: float = 0, include_stationary: bool = True,
                   limit: int = 20000) -> list[dict]:
        q = ["SELECT * FROM detections WHERE length_m BETWEEN ? AND ? AND confidence >= ?"]
        args: list = [min_length, max_length, min_confidence]
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
        return [dict(r) for r in self.conn.execute(" ".join(q), args).fetchall()]

    def stats(self) -> dict:
        c = self.conn
        return {
            "scenes_done": c.execute("SELECT COUNT(*) FROM scenes WHERE status='done'").fetchone()[0],
            "scenes_skipped": c.execute("SELECT COUNT(*) FROM scenes WHERE status='skipped'").fetchone()[0],
            "scenes_failed": c.execute("SELECT COUNT(*) FROM scenes WHERE status='failed'").fetchone()[0],
            "detections": c.execute("SELECT COUNT(*) FROM detections").fetchone()[0],
            "first": c.execute("SELECT MIN(datetime) FROM detections").fetchone()[0],
            "last": c.execute("SELECT MAX(datetime) FROM detections").fetchone()[0],
        }
