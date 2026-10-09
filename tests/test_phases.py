"""Region 1 is finished completely (Sentinel-2 and Sentinel-3) before anything else starts."""
import json
import threading
import time

from shapely.geometry import box, mapping

from shiptracker import pipeline, s3
from shiptracker.config import Settings
from shiptracker.pipeline import ScanOptions, scan
from tests.test_coverage import _scene

R1 = box(60, 21, 63, 24)    # first region (open sea, partly imaged by Sentinel-2)
R2 = box(56, 26, 57, 27)    # second region (Hormuz)


def _settings(tmp_path):
    pri = tmp_path / "priority.geojson"
    pri.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "first", "order": 1}, "geometry": mapping(R1)},
        {"type": "Feature", "properties": {"name": "second", "order": 2}, "geometry": mapping(R2)},
    ]}))
    return Settings(data_dir=tmp_path / "data", priority_path=pri)


def test_first_region_finishes_before_anything_else(tmp_path, monkeypatch):
    found = [  # newest first, deliberately mixing regions
        _scene("rest-1", "2026-10-08T06:00:00Z", "T-REST1", box(67, 18, 68, 19)),
        _scene("second-1", "2026-10-08T06:00:00Z", "T-SEC1", box(56.2, 26.2, 56.8, 26.8)),
        _scene("first-1", "2026-10-07T06:00:00Z", "T-FIR1", box(60.0, 23.0, 61.0, 24.0)),
        _scene("first-2", "2026-10-07T06:00:00Z", "T-FIR2", box(61.0, 23.0, 62.0, 24.0)),
        _scene("first-3", "2026-10-06T06:00:00Z", "T-FIR3", box(62.0, 23.0, 63.0, 24.0)),
        _scene("rest-2", "2026-10-05T06:00:00Z", "T-REST2", box(68, 18, 69, 19)),
    ]
    events, lock = [], threading.Lock()

    def fake_process(settings, store, sc, opts):
        with lock:
            events.append(("start", sc.id))
        time.sleep(0.05)  # let parallel workers overlap if the phases leak
        with lock:
            events.append(("end", sc.id))
        store.save_scene(sc, "done", "", detector_version=pipeline.DETECTOR_VERSION)
        return "done", 0

    granules = [
        s3.Granule("S3-first", "2026-10-08T05:00:00Z", mapping(box(59, 20, 64, 22.9)), {}),
        s3.Granule("S3-rest", "2026-10-08T05:10:00Z", mapping(box(63, 8, 72, 20)), {}),
    ]

    def fake_s3_process(settings, store, g, region, keep):
        with lock:
            events.append(("s3", g.id))
        store.save_s3_granule(g, "done", "")
        return 0

    monkeypatch.setattr(pipeline, "search_scenes", lambda *a, **k: list(found))
    monkeypatch.setattr(pipeline, "process_scene", fake_process)
    monkeypatch.setattr(s3, "search_granules", lambda cfg, region, start, end: list(granules))
    monkeypatch.setattr(s3, "process_granule", fake_s3_process)

    rep = scan(_settings(tmp_path), ScanOptions(start="2026-10-01", end="2026-10-08", workers=3))
    order = [e for e in events if e[0] in ("start", "s3")]
    names = [i for _, i in order]

    first = [i for i in names if i.startswith("first")] + ["S3-first"]
    last_first = max(names.index(i) for i in first)
    first_other = min(names.index(i) for i in names if i not in first)
    assert last_first < first_other, names
    # every first-region image had *finished* before the second region started
    second_start = events.index(("start", "second-1"))
    assert all(events.index(("end", i)) < second_start for i in first if i.startswith("first"))
    # second region before the rest of the area
    assert names.index("second-1") < min(names.index("rest-1"), names.index("rest-2"))
    assert rep.processed == 6 and rep.s3_granules == 2
    assert any("Phase 1/3: first" in m for m in rep.log)


def test_priority_only_stops_after_regions(tmp_path, monkeypatch):
    found = [_scene("first-1", "2026-10-07T06:00:00Z", "T-FIR1", box(60.0, 23.0, 61.0, 24.0)),
             _scene("rest-1", "2026-10-08T06:00:00Z", "T-REST1", box(67, 18, 68, 19))]
    seen = []
    monkeypatch.setattr(pipeline, "search_scenes", lambda *a, **k: list(found))
    monkeypatch.setattr(pipeline, "process_scene", lambda st, store, sc, o: (seen.append(sc.id), ("done", 0))[1])
    rep = scan(_settings(tmp_path), ScanOptions(start="2026-10-01", end="2026-10-08", priority_only=True,
                                                sentinel3=False))
    assert seen == ["first-1"] and rep.processed == 1


def test_s3_reports_when_planetary_computer_has_nothing_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "search_scenes", lambda *a, **k: [])
    monkeypatch.setattr(s3, "search_granules", lambda *a, **k: [])
    rep = scan(_settings(tmp_path), ScanOptions(start="2026-10-01", end="2026-10-08", priority_only=True))
    assert any("Planetary Computer has no recent images here" in m and "--cdse-login" in m for m in rep.log)
