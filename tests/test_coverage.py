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
