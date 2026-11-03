"""Track H-60 helicopters (MH-60R/S Seahawk and the rest of the Sikorsky H-60 family) and
V-22 Osprey tiltrotors from public ADS-B data, keeping each one's last known position
and recent trail.

Positions come from free community aggregators with the ADS-B Exchange v2 API
(adsb.lol, airplanes.live). Only aircraft that broadcast ADS-B (or are located by
multilateration) appear; military aircraft often switch it off, so a helicopter can
vanish for long stretches: its last known position stays on the map, greyed, with how
long ago it was seen.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import requests

log = logging.getLogger(__name__)

SOURCES = (  # tried in order; the first that answers is used
    ("adsb.lol", "https://api.adsb.lol/v2/type/{type}"),
    ("airplanes.live", "https://api.airplanes.live/v2/type/{type}"),
)
# H60: Sikorsky H-60 family (MH-60R/S Seahawk, Black Hawk...); V22: Bell-Boeing V-22 Osprey
TYPES = tuple(t.strip().upper() for t in os.environ.get("SHIPTRACKER_AIRCRAFT_TYPES", "H60,V22").split(",") if t.strip())
POLL_SECONDS = 60
KEEP_DAYS = 7
MOVE_M = 300          # store a new trail point after this much movement ...
REFRESH_S = 300       # ... or this long without one

SCHEMA = """
CREATE TABLE IF NOT EXISTS aircraft (
    hex TEXT PRIMARY KEY,          -- ICAO 24-bit address
    flight TEXT, reg TEXT, type TEXT, descr TEXT, military INTEGER,
    lat REAL, lon REAL, alt_ft INTEGER, on_ground INTEGER, gs_kt REAL, track REAL, squawk TEXT,
    seen REAL,                     -- unix time of the last position
    source TEXT
);
CREATE TABLE IF NOT EXISTS aircraft_track (
    hex TEXT NOT NULL, ts REAL NOT NULL, lat REAL, lon REAL, alt_ft INTEGER,
    PRIMARY KEY (hex, ts)
);
"""


def parse(data: dict, source: str = "") -> list[dict]:
    """Aircraft with a position from an ADS-B Exchange v2 style response."""
    now = (data.get("now") or time.time() * 1000) / 1000
    out = []
    for ac in data.get("ac") or data.get("aircraft") or []:
        if ac.get("lat") is None or ac.get("lon") is None or not ac.get("hex"):
            continue
        alt = ac.get("alt_baro", ac.get("alt_geom"))
        on_ground = alt == "ground"
        out.append({
            "hex": str(ac["hex"]).lower().lstrip("~"),
            "flight": (ac.get("flight") or "").strip() or None,
            "reg": ac.get("r") or None,
            "type": ac.get("t") or None,
            "descr": ac.get("desc") or None,
            "military": int(bool((ac.get("dbFlags") or 0) & 1)),
            "lat": float(ac["lat"]), "lon": float(ac["lon"]),
            "alt_ft": 0 if on_ground else (int(alt) if isinstance(alt, (int, float)) else None),
            "on_ground": int(on_ground),
            "gs_kt": ac.get("gs"), "track": ac.get("track", ac.get("true_heading")),
            "squawk": ac.get("squawk"),
            "seen": now - float(ac.get("seen_pos") or ac.get("seen") or 0),
            "source": source,
        })
    return out


def _dist_m(lat1, lon1, lat2, lon2) -> float:
    import math
    k = math.pi / 180
    x = (lon2 - lon1) * k * math.cos((lat1 + lat2) / 2 * k)
    return 6371000 * math.hypot(x, (lat2 - lat1) * k)


class FlightTracker:
    """Polls the aggregators in a background thread and stores what it sees."""

    def __init__(self, store, sources=SOURCES, types=TYPES, interval: float = POLL_SECONDS):
        self.store, self.sources, self.types, self.interval = store, sources, types, interval
        self.status = {"enabled": False, "last_poll": None, "source": None, "error": None, "types": list(types)}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        with store.lock, store.conn:
            store.conn.executescript(SCHEMA)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.status["enabled"] = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="flights")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.status["enabled"] = False

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval)

    def poll_once(self) -> int:
        """Fetch all aircraft of the tracked types once; returns how many were seen."""
        session = requests.Session()
        session.headers["User-Agent"] = "ship-tracker/2.0 (personal use)"
        errors = []
        for name, url in self.sources:
            try:
                seen = []
                for t in self.types:
                    r = session.get(url.format(type=t), timeout=20)
                    r.raise_for_status()
                    seen += parse(r.json(), name)
                self._save(seen)
                self.status.update(last_poll=time.time(), source=name, error=None)
                return len(seen)
            except (requests.RequestException, ValueError) as exc:
                errors.append(f"{name}: {exc}")
        self.status.update(last_poll=time.time(), error="; ".join(errors)[:400])
        log.warning("Flight data unavailable (%s)", self.status["error"])
        return 0

    def _save(self, seen: list[dict]) -> None:
        st = self.store
        cols = ("hex", "flight", "reg", "type", "descr", "military", "lat", "lon", "alt_ft", "on_ground",
                "gs_kt", "track", "squawk", "seen", "source")
        with st.lock, st.conn:
            for a in seen:
                prev = st.conn.execute("SELECT lat, lon, seen, flight, reg, type, descr FROM aircraft WHERE hex=?",
                                       (a["hex"],)).fetchone()
                if prev:  # keep identity fields a source left out this time
                    for k in ("flight", "reg", "type", "descr"):
                        a[k] = a[k] or prev[k]
                st.conn.execute(f"INSERT OR REPLACE INTO aircraft ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                tuple(a[c] for c in cols))
                last = st.conn.execute("SELECT ts, lat, lon FROM aircraft_track WHERE hex=? ORDER BY ts DESC LIMIT 1",
                                       (a["hex"],)).fetchone()
                if (last is None or a["seen"] - last["ts"] >= REFRESH_S
                        or _dist_m(last["lat"], last["lon"], a["lat"], a["lon"]) >= MOVE_M):
                    st.conn.execute("INSERT OR IGNORE INTO aircraft_track VALUES (?,?,?,?,?)",
                                    (a["hex"], a["seen"], a["lat"], a["lon"], a["alt_ft"]))
            cutoff = time.time() - KEEP_DAYS * 86400
            st.conn.execute("DELETE FROM aircraft_track WHERE ts < ?", (cutoff,))
            st.conn.execute("DELETE FROM aircraft WHERE seen < ?", (cutoff,))

    def aircraft(self, trail_hours: float = 24) -> list[dict]:
        """Every aircraft seen in the last KEEP_DAYS days: last position and recent trail."""
        rows = [dict(r) for r in self.store._query("SELECT * FROM aircraft ORDER BY seen DESC")]
        since = time.time() - trail_hours * 3600
        for a in rows:
            pts = self.store._query("SELECT ts, lat, lon, alt_ft FROM aircraft_track WHERE hex=? AND ts>=? ORDER BY ts",
                                    (a["hex"], since))
            a["trail"] = [[p["lat"], p["lon"]] for p in pts]
            a["age_s"] = round(time.time() - a["seen"])
        return rows
