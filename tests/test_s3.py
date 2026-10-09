import math
from collections import Counter

from shapely.geometry import box

from shiptracker.s3 import detect_granule
from tests.synth import write_s3_granule


def test_finds_large_ship_specks_and_rejects_cloud_and_land(tmp_path):
    truth, _ = write_s3_granule(tmp_path)
    region = box(60, 10, 70, 20)
    rejected = Counter()
    found = detect_granule(tmp_path / "Oa17_reflectance.nc", tmp_path / "geo_coordinates.nc",
                           tmp_path / "wqsf.nc", region, tmp_path / "chips", "2026-10-08", rejected=rejected)
    assert len(found) == len(truth), [(d.lat, d.lon) for d, _ in found]
    for lat, lon in truth:
        d = min((d for d, _ in found), key=lambda d: math.hypot(d.lat - lat, d.lon - lon))
        assert math.hypot((d.lat - lat) * 111, (d.lon - lon) * 107) < 0.4  # within 400 m
    assert all(c and c.exists() for _, c in found)
    assert rejected  # the puffs next to the cloud were seen and rejected


def test_only_reports_inside_open_sea_region(tmp_path):
    write_s3_granule(tmp_path)
    found = detect_granule(tmp_path / "Oa17_reflectance.nc", tmp_path / "geo_coordinates.nc",
                           tmp_path / "wqsf.nc", box(63.5, 10, 64.0, 20))
    assert all(63.5 <= d.lon <= 64.0 for d, _ in found) and len(found) >= 1


def test_granule_selection_prefers_newest_and_skips_redundant():
    from shapely.geometry import mapping

    from shiptracker.s3 import Granule, select_granules

    region = box(60, 10, 70, 20)
    g = lambda gid, dt, geom: Granule(gid, dt, mapping(geom), {})
    granules = [
        g("new-west", "2026-10-08T06:00:00Z", box(60, 10, 65, 20)),
        g("old-west", "2026-10-07T06:00:00Z", box(60, 10, 65, 20)),   # nothing new
        g("old-east", "2026-10-07T05:00:00Z", box(64, 10, 70, 20)),   # fills the east
        g("corner", "2026-10-06T05:00:00Z", box(69.5, 19.5, 71, 21)),  # small, but already covered
    ]
    assert [x.id for x in select_granules(granules, region)] == ["new-west", "old-east"]
