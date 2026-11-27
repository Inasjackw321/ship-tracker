from collections import Counter

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from shiptracker.detect import Detection
from shiptracker.scene import GeoDetection
from shiptracker.verify import verify_detections

# Each object: (row, col, NIR reflectance, (R, G, B) reflectance)
OBJECTS = {
    "grey hull": (50, 50, 0.12, (0.10, 0.10, 0.09)),
    "red-deck hull": (50, 150, 0.20, (0.16, 0.06, 0.05)),
    "mangrove": (150, 50, 0.32, (0.03, 0.06, 0.03)),
    "cloud": (150, 150, 0.45, (0.40, 0.40, 0.42)),
}


def _det(row, col, lat=24.5, lon=59.0):
    d = Detection(row=row, col=col, length_px=12, width_px=2, axis_r=-1.0, axis_c=0.0,
                  peak_reflectance=0.3, contrast=0.2, snr=40, npix=24, length_m=120, width_m=20)
    return GeoDetection(d, lon, lat, lon, lat, lon, lat, 0, 0)


@pytest.fixture()
def tiles(tmp_path):
    nir = np.full((200, 200), 0.01, np.float32)
    rgb = np.zeros((3, 200, 200), np.float32) + np.array([0.02, 0.04, 0.05])[:, None, None]
    for r, c, n, col in OBJECTS.values():
        nir[r - 6:r + 7, c - 1:c + 2] = n
        for i in range(3):
            rgb[i, r - 6:r + 7, c - 1:c + 2] = col[i]
    tr = from_origin(500000, 2700000, 10, 10)
    kw = dict(driver="GTiff", crs="EPSG:32640", transform=tr, width=200, height=200)
    with rasterio.open(tmp_path / "B08.tif", "w", count=1, dtype="uint16", nodata=0, **kw) as dst:
        dst.write(((nir + 0.1) / 1e-4).astype(np.uint16), 1)
    with rasterio.open(tmp_path / "TCI.tif", "w", count=3, dtype="uint8", nodata=0, **kw) as dst:
        dst.write(np.clip(rgb / 0.2 * 255, 1, 255).astype(np.uint8))
    return tmp_path / "B08.tif", tmp_path / "TCI.tif"


def test_colour_checks_keep_hulls_and_drop_vegetation_and_cloud(tiles):
    nir, tci = tiles
    dets = {name: _det(r, c) for name, (r, c, _, _) in OBJECTS.items()}
    rejected = Counter()
    kept = verify_detections(list(dets.values()), nir, tci, 1e-4, -0.1, rejected)
    assert {id(g) for g in kept} == {id(dets["grey hull"]), id(dets["red-deck hull"])}
    assert rejected == Counter({"vegetation": 1, "white like cloud": 1})


def test_without_colour_image_only_land_check_runs(tiles):
    nir, _ = tiles
    dets = [_det(r, c) for (r, c, _, _) in OBJECTS.values()]
    assert len(verify_detections(dets, nir, None, 1e-4, -0.1)) == 4


def test_inland_detection_is_rejected(tiles):
    nir, tci = tiles
    sea = _det(50, 50, lat=24.5, lon=59.0)          # Gulf of Oman
    salt_flat = _det(50, 50, lat=23.75, lon=71.0)   # Little Rann of Kutch
    rejected = Counter()
    kept = verify_detections([sea, salt_flat], nir, tci, 1e-4, -0.1, rejected)
    assert kept == [sea] and rejected["inland"] == 1
