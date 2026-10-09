import math

import numpy as np
import pytest

from shiptracker.detect import DetectParams, detect_in_array
from tests.synth import make_scene

SHIPS = [
    (150, 150, 121.5, 20, 8),     # the reference vessel: 121.5 m, nearly north-south
    (300, 300, 250, 40, 45),
    (450, 200, 60, 12, 100),
    (450, 450, 400, 60, 170),
    (250, 480, 35, 8, 70),
    (520, 320, 180, 30, 135),
]


def _run(refl, scl):
    valid = np.ones(refl.shape, bool)
    return detect_in_array(refl, scl, valid, valid, DetectParams())


def _match(dets, r, c):
    best = min(dets, key=lambda d: math.hypot(d.row - r, d.col - c))
    assert math.hypot(best.row - r, best.col - c) < 3, "ship not found near its true position"
    return best


def test_detects_and_measures_each_ship():
    refl, scl, truth = make_scene(ships=SHIPS)
    dets = _run(refl, scl)
    assert len(dets) == len(SHIPS)
    for r, c, L, W, hd in truth:
        d = _match(dets, r, c)
        assert abs(d.length_m - L) / L < 0.10, f"{L} m ship measured {d.length_m} m"
        diff = abs((d.heading_deg - hd + 90) % 180 - 90)
        assert diff < 4, f"axis {hd} measured {d.heading_deg}"


def test_reference_ship_length():
    refl, scl, _ = make_scene(ships=[(300, 300, 121.53, 20, 3)], land=False, cloud=False)
    (d,) = _run(refl, scl)
    assert d.length_m == pytest.approx(121.53, rel=0.06)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_no_false_alarms_on_empty_sea(seed):
    refl, scl, _ = make_scene(ships=[], seed=seed)
    assert _run(refl, scl) == []


def test_ignores_land_and_clouds():
    refl, scl, _ = make_scene(ships=[])
    rng = np.random.default_rng(5)
    # bright speckle on land and a cloud edge must not become ships
    refl[100:110, 20:30] += 0.4
    scl[100:110, 20:30] = 5
    refl[80:84, 455:470] += 0.3
    refl += rng.normal(0, 0.001, refl.shape).astype(np.float32)
    assert _run(refl, scl) == []
