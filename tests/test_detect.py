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


def test_rejects_cumulus_field_but_keeps_isolated_ship():
    from tests.synth import add_cumulus_field

    refl, scl, truth = make_scene(ships=[(480, 480, 150, 25, 30)], land=False, cloud=False)
    refl, scl = add_cumulus_field(refl, scl, center=(180, 200), radius=120, n=60)
    dets = _run(refl, scl)
    assert len(dets) == 1, [(round(d.row), round(d.col), d.length_m) for d in dets]
    _match(dets, *truth[0][:2])


def _vegetation_strip():
    # Mangrove forest shore with a detached 450 x 40 m vegetated strip just offshore,
    # like the false positive at 22.393 N 69.070 E (459 m x 45 m along a mangrove edge).
    refl, scl, _ = make_scene(ships=[], land=False, cloud=False)
    refl[:, :150] = 0.30
    scl[:, :150] = 4
    refl[280:325, 156:160] = 0.30
    scl[280:325, 156:160] = 4
    return refl, scl


def test_vegetation_strip_is_not_a_ship():
    refl, scl = _vegetation_strip()
    assert _run(refl, scl) == []


def test_vegetation_strip_was_a_false_positive_with_old_settings():
    # Guards the test above: the old sea mask turned the strip into a ~450 m "ship".
    refl, scl = _vegetation_strip()
    valid = np.ones(refl.shape, bool)
    old = DetectParams(max_hole_px=3000, max_length_m=500, coast_buffer_px=3, fill_vegetation_holes=True)
    dets = detect_in_array(refl, scl, valid, valid, old)
    assert len(dets) == 1 and dets[0].length_m > 400
