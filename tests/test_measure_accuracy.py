"""Measurement accuracy on 200 realistic synthetic ships (see tests/bench_ships.py).

Guards against regressions; numbers are for synthetic ships, real imagery is noisier.
"""
import numpy as np
import pytest

from tests.bench_ships import make_benchmark, run


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
