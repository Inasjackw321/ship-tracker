from shapely.geometry import box, mapping

from shiptracker.pipeline import imagery_coverage, select_latest_coverage
from shiptracker.stac import Scene


def _scene(sid, dt, tile, geom):
    return Scene(id=sid, datetime=dt, cloud_cover=1.0, tile=tile, epsg=32641, nir_href="", scl_href="",
                 rgb_href=None, nir_scale=1e-4, nir_offset=-0.1, aoi_overlap=1.0, geometry=mapping(geom))


AOI = box(60, 20, 64, 24)


def test_one_image_per_fully_imaged_tile():
    scenes = [
        _scene("a-new", "2026-10-08T06:00:00Z", "41QKL", box(60, 20, 61, 21)),
        _scene("a-old", "2026-10-03T06:00:00Z", "41QKL", box(60, 20, 61, 21)),
        _scene("b-new", "2026-10-07T06:00:00Z", "41QLL", box(61, 20, 62, 21)),
    ]
    assert [s.id for s in select_latest_coverage(scenes, AOI)] == ["a-new", "b-new"]


def test_swath_edge_tile_gets_second_pass_to_fill_gap():
    scenes = [
        _scene("east-half", "2026-10-08T06:00:00Z", "41QKL", box(60.5, 20, 61, 21)),
        _scene("west-half", "2026-10-06T06:00:00Z", "41QKL", box(60, 20, 60.5, 21)),
        _scene("older-full", "2026-10-01T06:00:00Z", "41QKL", box(60, 20, 61, 21)),
    ]
    assert [s.id for s in select_latest_coverage(scenes, AOI)] == ["east-half", "west-half"]


def test_coverage_reports_unimaged_part():
    cov = imagery_coverage([_scene("a", "2026-10-08T06:00:00Z", "T", box(60, 20, 62, 24))], AOI)
    assert 0.45 < cov["fraction"] < 0.55
    assert cov["missing"] is not None
    assert imagery_coverage([], AOI)["fraction"] == 0.0


def test_priority_regions_scanned_first_in_order():
    from pathlib import Path

    from shiptracker.aoi import load_priority
    from shiptracker.pipeline import in_priority, prioritize

    regions = load_priority(Path(__file__).resolve().parent.parent / "config" / "priority.geojson")
    assert [n for n, _ in regions] == ["Strait of Hormuz", "Gulf of Oman and Makran coast"]
    scenes = [
        _scene("central-sea", "2026-10-08T06:00:00Z", "42QXX", box(62.5, 17.5, 63.5, 18.5)),
        _scene("gwadar", "2026-10-07T06:00:00Z", "41RNH", box(61.8, 24.5, 62.8, 25.3)),
        _scene("hormuz", "2026-10-06T06:00:00Z", "40RDQ", box(56.0, 26.0, 57.0, 27.0)),
        _scene("chabahar", "2026-10-05T06:00:00Z", "41RLH", box(60.2, 24.7, 61.0, 25.4)),
    ]
    assert [s.id for s in prioritize(scenes, regions)] == ["hormuz", "gwadar", "chabahar", "central-sea"]
    assert [s.id for s in scenes if in_priority(s, regions)] == ["gwadar", "hormuz", "chabahar"]
