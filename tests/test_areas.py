"""The scannable areas, and that a scan only searches the area that was chosen."""
from shapely.geometry import Point

from shiptracker import pipeline
from shiptracker.config import Settings
from shiptracker.pipeline import ScanOptions, scan

IDS = ["persian-gulf", "gulf-of-oman", "red-sea-south", "dover-strait", "baltic", "black-sea-north",
       "east-china-sea"]


def test_areas_file_lists_the_seven_areas():
    areas = Settings().areas()
    assert [a.id for a in areas] == IDS
    assert {a.group for a in areas} == {"Middle East", "Europe", "East Asia"}
    assert all(a.geom.is_valid and not a.geom.is_empty for a in areas)


def test_places_fall_in_the_right_area():
    s = Settings()
    inside = {
        "persian-gulf": [(56.3, 26.5), (50.6, 26.2)],       # Strait of Hormuz, Bahrain
        "gulf-of-oman": [(56.4, 25.1), (62.3, 25.1)],       # Fujairah, Gwadar
        "red-sea-south": [(43.4, 12.6)],                    # Bab-el-Mandeb
        "dover-strait": [(1.5, 51.0)],
        "baltic": [(10.9, 55.5), (29.5, 59.95)],            # Great Belt, St Petersburg
        "black-sea-north": [(36.5, 45.3)],                  # Kerch Strait
        "east-china-sea": [(122.5, 31.0), (120.0, 24.5), (129.1, 35.05)],  # Shanghai, Taiwan Strait, Busan
    }
    for area_id, pts in inside.items():
        geom = s.area(area_id).geom
        for lon, lat in pts:
            assert geom.contains(Point(lon, lat)), (area_id, lon, lat)
    union = s.search_area()
    for lon, lat in [(18.0, 35.0), (135.0, 40.0), (114.0, 15.0)]:  # Mediterranean, Sea of Japan, S China Sea
        assert not union.contains(Point(lon, lat))


def test_scan_searches_only_the_chosen_area(tmp_path, monkeypatch):
    searched = []
    monkeypatch.setattr(pipeline, "search_scenes", lambda cfg, aoi, *a, **k: (searched.append(aoi), [])[1])
    settings = Settings(data_dir=tmp_path / "data")
    rep = scan(settings, ScanOptions(area="dover-strait", start="2026-10-01", end="2026-10-08"))
    assert rep.found == 0
    assert searched and all(g.equals(settings.area("dover-strait").geom) for g in searched)


def test_unknown_area_is_rejected(tmp_path):
    import pytest

    with pytest.raises(KeyError, match="gulf-of-oman"):
        scan(Settings(data_dir=tmp_path / "data"), ScanOptions(area="atlantis", start="2026-10-01", end="2026-10-08"))
