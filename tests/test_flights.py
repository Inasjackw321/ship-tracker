"""H-60 tracking from public ADS-B aggregators (served here by a local fake)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from shiptracker.config import Settings
from shiptracker.db import Store
from shiptracker.flights import FlightTracker, parse
from shiptracker.server import create_app

NOW = time.time()


def response(lat, lon, seen_pos=1.0, flight="RIPP21  ", alt=1200, now=NOW):
    return {"now": now * 1000, "total": 2, "ac": [
        {"hex": "ae5432", "type": "adsb_icao", "flight": flight, "r": "168123", "t": "H60",
         "desc": "SIKORSKY MH-60R Seahawk", "dbFlags": 1, "alt_baro": alt, "gs": 118.4, "track": 271.5,
         "lat": lat, "lon": lon, "seen_pos": seen_pos, "seen": 0.4, "squawk": "4421"},
        {"hex": "ae9999", "t": "H60", "flight": "NOPOS", "seen": 3},  # no position: skipped
    ]}


class Fake(BaseHTTPRequestHandler):
    replies: list = []
    hits: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Fake.hits.append(self.path)
        code, body = Fake.replies.pop(0) if Fake.replies else (200, response(36.95, -76.33))
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def fake():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    Fake.replies, Fake.hits = [], []
    yield (("first", base + "/a/v2/type/{type}"), ("second", base + "/b/v2/type/{type}"))
    srv.shutdown()


def test_parse_adsb_exchange_v2():
    ac = parse(response(36.95, -76.33), "test")
    assert len(ac) == 1
    a = ac[0]
    assert (a["hex"], a["flight"], a["reg"], a["type"], a["military"]) == ("ae5432", "RIPP21", "168123", "H60", 1)
    assert a["alt_ft"] == 1200 and a["track"] == 271.5 and abs(a["seen"] - (NOW - 1)) < 0.01
    assert parse(response(1, 2, alt="ground"))[0]["on_ground"] == 1


def test_tracker_keeps_last_position_and_trail(fake, tmp_path):
    store = Store(tmp_path / "ships.db")
    tr = FlightTracker(store, sources=fake, types=("H60",))
    Fake.replies = [(500, {}), (200, response(36.95, -76.33))]  # first source down: falls back
    assert tr.poll_once() == 1 and tr.status["source"] == "second" and "/type/H60" in Fake.hits[-1]
    Fake.replies = [(200, response(36.99, -76.20, flight="", now=NOW + 60))]  # a minute later, ~12 km on, no callsign
    tr.poll_once()
    (a,) = tr.aircraft()
    assert a["flight"] == "RIPP21"  # identity kept from earlier reports
    assert abs(a["lat"] - 36.99) < 1e-9 and len(a["trail"]) == 2 and a["age_s"] < 60


def test_aircraft_api_and_failure_status(fake, tmp_path):
    app = create_app(Settings(data_dir=tmp_path / "data"))  # tracking is off unless asked for
    client = app.test_client()
    body = client.get("/api/aircraft").get_json()
    assert body["aircraft"] == [] and body["status"]["enabled"] is False and body["status"]["types"] == ["H60", "V22"]
    app.extensions["flights"].sources = fake
    assert client.post("/api/aircraft/refresh").get_json()["seen"] == 1
    (a,) = client.get("/api/aircraft").get_json()["aircraft"]
    assert a["reg"] == "168123" and a["descr"].startswith("SIKORSKY MH-60R")
    Fake.replies = [(503, {}), (503, {})]  # both aggregators down: a clear error, last position kept
    st = client.post("/api/aircraft/refresh").get_json()["status"]
    assert "first" in st["error"] and "second" in st["error"]
    assert len(client.get("/api/aircraft").get_json()["aircraft"]) == 1
