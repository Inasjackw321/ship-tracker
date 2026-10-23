"""Measurement accuracy on 200 realistic synthetic ships (see tests/bench_ships.py).

Guards against regressions; numbers are for synthetic ships, real imagery is noisier.
"""
import numpy as np
import pytest

from tests.bench_ships import make_benchmark, make_hard_benchmark, run


@pytest.fixture(scope="module")
def results():
    cases = make_benchmark()
    dets = run(cases)
    rows = [(c["L"], c["W"], d) for c, d in zip(cases, dets) if d is not None]
    return cases, rows


def _rmse(x):
    return float(np.sqrt(np.mean(np.square(x))))


def test_finds_nearly_all_ships(results):
    cases, rows = results
    assert len(rows) >= 0.92 * len(cases)


def test_length_and_beam_accuracy(results):
    _, rows = results
    dl = np.array([d.length_m - L for L, W, d in rows])
    dw = np.array([d.width_m - W for L, W, d in rows])
    assert _rmse(dl) < 5.0 and abs(dl.mean()) < 3.5      # metres (was 8.3 m / -5.5 m before the hull fit)
    assert _rmse(dw) < 3.5 and abs(dw.mean()) < 2.5      # metres (was 7.3 m / +5.3 m)
    big = np.array([(d.length_m - L) / L for L, W, d in rows if L > 150])
    assert _rmse(big) < 0.03                              # large ships within ~2-3 %


def test_uncertainties_are_honest(results):
    _, rows = results
    within_l = np.mean([abs(d.length_m - L) <= d.length_err_m for L, W, d in rows])
    within_w = np.mean([abs(d.width_m - W) <= d.width_err_m for L, W, d in rows])
    assert within_l >= 0.68 and within_w >= 0.68         # +-1 sigma brackets the truth
    assert np.median([d.length_err_m for _, _, d in rows]) < 8  # ...without being uselessly wide


def test_hull_fit_is_used(results):
    _, rows = results
    assert np.mean([d.method == "fit" for _, _, d in rows]) > 0.9


@pytest.fixture(scope="module")
def moving():
    """The same ships, every one under way with a bright wake 0.5-3x its length."""
    cases = make_benchmark(wake_fraction=1.0)
    return [(c, d) for c, d in zip(cases, run(cases)) if d is not None], len(cases)


def test_moving_ships_are_found(moving):
    rows, n = moving
    assert len(rows) >= 0.9 * n  # a wake must not make a ship look like a soft-edged cloud


def test_wakes_are_not_measured_as_hull(moving):
    rows, _ = moving
    dl = np.array([d.length_m - c["L"] for c, d in rows])
    assert np.median(np.abs(dl)) < 10       # was ~17 m before wakes were modelled
    assert np.percentile(np.abs(dl), 90) < 35  # was ~110 m
    assert np.mean([d.wake_m > 0 for c, d in rows if c["L"] > 60]) > 0.6  # wakes are recognised


def test_no_fake_supertankers(moving):
    """Before wakes were modelled, most "300 m+" ships here were 150-250 m ships plus wake."""
    rows, _ = moving
    reported = [(c, d) for c, d in rows if d.length_m >= 300]
    fake = [d for c, d in reported if c["L"] < 260]
    assert len(fake) <= 0.1 * max(len(reported), 1)
    real = [c for c, d in rows if c["L"] >= 320]
    assert all(d.length_m >= 280 for c, d in rows if c["L"] >= 320) and real  # real VLCCs/ULCVs are kept


@pytest.fixture(scope="module")
def hard():
    """Varying image sharpness, dark hull sides, very bright superstructures, glint,
    noisy water and ships moored alongside."""
    cases = make_hard_benchmark(200)
    return [(c, d) for c, d in zip(cases, run(cases)) if d is not None], len(cases)


def test_hard_conditions_found_and_not_grossly_wrong(hard):
    rows, n = hard
    assert len(rows) >= 0.93 * n  # was 177/200: bright superstructures read as "soft cloud edges"
    gross = [1 for c, d in rows if abs(d.length_m - c["L"]) > max(20, 0.15 * c["L"])]
    assert len(gross) <= 0.02 * n  # was 6: ships alongside cut hulls short
    big = [d.length_m - c["L"] for c, d in rows if c["L"] >= 150]
    assert _rmse(big) < 8  # was 20 m


def test_hard_conditions_error_bars_are_honest(hard):
    """The +- shown is a 1-sigma uncertainty: at least ~68 % of true sizes lie inside it."""
    rows, _ = hard
    in_len = np.mean([abs(d.length_m - c["L"]) <= d.length_err_m for c, d in rows])
    in_beam = np.mean([abs(d.width_m - c["W"]) <= d.width_err_m for c, d in rows])
    assert in_len >= 0.75 and in_beam >= 0.7


def test_moving_ships_error_bars_are_honest(moving):
    """Where a wake meets the hull is the least certain part of a measurement; the +-
    says so (it held only 45 % of true lengths before)."""
    rows, _ = moving
    assert np.mean([abs(d.length_m - c["L"]) <= d.length_err_m for c, d in rows]) >= 0.68
