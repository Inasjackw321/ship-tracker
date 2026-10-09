"""Benchmark set of realistic synthetic ships for measurement accuracy."""
from __future__ import annotations

import math

import numpy as np
from scipy import ndimage

SS = 10  # 1 m sub-pixels


def render_ship(L, W, heading, dx, dy, size, rng, wake=None, wake_rng=None):
    """Excess-reflectance image (10 m pixels) of one ship with deck structure."""
    n = size * SS
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    cy, cx = n / 2 + dy * SS, n / 2 + dx * SS  # true centre, sub-pixel offset
    th = math.radians(heading)
    ux, uy = math.sin(th), -math.cos(th)
    s = ((xx - cx) * ux + (yy - cy) * uy) / SS * 10
    t = ((xx - cx) * -uy + (yy - cy) * ux) / SS * 10
    hl, hw = L / 2, W / 2
    bow_len = rng.uniform(0.08, 0.16) * L
    bow = hl - bow_len
    stern_round = rng.uniform(0.0, 0.04) * L
    taper = np.where(s > bow, hw * np.clip(1 - (s - bow) / bow_len, 0, 1) ** rng.uniform(0.6, 1.0), hw)
    taper = np.where(s < -hl + stern_round, hw * 0.85, taper)
    hull = (np.abs(s) <= hl) & (np.abs(t) <= taper)
    deck = rng.uniform(0.05, 0.16)
    img = hull * deck
    # hatch covers: alternating brightness along the deck
    if rng.random() < 0.6:
        period = rng.uniform(12, 25)
        hatch = (np.sin(s / period * 2 * np.pi) > 0.3) & (np.abs(t) < hw * 0.7) & hull
        img = np.where(hatch, deck * rng.uniform(0.6, 1.8), img)
    # superstructure near the stern (or occasionally forward)
    if rng.random() < 0.85:
        pos = -hl + rng.uniform(0.03, 0.12) * L if rng.random() < 0.85 else hl - 0.25 * L
        ln = rng.uniform(0.06, 0.12) * L
        sup = (s > pos) & (s < pos + ln) & (np.abs(t) <= hw * 0.9)
        img = np.where(sup, rng.uniform(0.2, 0.45), img)
    if wake:
        # turbulent wake behind the stern: bright foam that fades and spreads with distance
        d = np.clip(-hl - s, 0, None)                 # metres behind the stern
        wl = wake["length"]
        spread = hw * (0.8 + 1.5 * d / max(wl, 1))
        fade = np.exp(-d / (wl / 2.5))
        texture = np.clip(ndimage.gaussian_filter(1 + wake_rng.normal(0, 5.0, d.shape), 15), 0.3, 1.7)
        foam = (s < -hl) & (d < wl) & (np.abs(t) < spread)
        img = img + np.where(foam, deck * wake["brightness"] * fade * texture, 0)
    small = img.reshape(size, SS, size, SS).mean(axis=(1, 3))
    return ndimage.gaussian_filter(small, 0.53)


def make_benchmark(n=200, seed=42, wake_fraction=0.0, wake_seed=7):
    """``wake_fraction`` of the ships are moving and trail a wake 0.5-3x their length."""
    rng = np.random.default_rng(seed)
    wrng = np.random.default_rng(wake_seed)  # separate stream: the ships themselves stay identical
    cases = []
    for _ in range(n):
        L = float(np.exp(rng.uniform(np.log(25), np.log(400))))
        W = float(np.clip(L / rng.uniform(4.5, 8.0), 6, 62))
        heading = float(rng.uniform(0, 180))
        dx, dy = rng.uniform(-0.5, 0.5, 2)
        wake = None
        if wrng.random() < wake_fraction:
            wake = {"length": L * wrng.uniform(0.5, 3.0), "brightness": wrng.uniform(0.3, 1.0)}
        size = int(L / 10 * (1.5 + (2 * wake["length"] / L if wake else 0))) + 40
        size += size % 2
        excess = render_ship(L, W, heading, dx, dy, size, rng, wake, wrng)
        water = 0.012 + rng.uniform(0, 0.02)
        noise = rng.uniform(0.0015, 0.004)
        noise_rng = np.random.default_rng(int(rng.integers(1 << 31)))  # image size must not shift later ships
        refl = (water + excess + noise_rng.normal(0, noise, excess.shape)).astype(np.float32)
        truth_rc = (size / 2 - 0.5 + dy, size / 2 - 0.5 + dx)
        cases.append(dict(refl=refl, L=L, W=W, heading=heading, rc=truth_rc, wake=wake))
    return cases


def run(cases, params=None):
    from shiptracker.detect import DetectParams, detect_in_array

    p = params or DetectParams()
    out = []
    for c in cases:
        refl = c["refl"]
        scl = np.full(refl.shape, 6, np.uint8)
        valid = np.ones(refl.shape, bool)
        dets = detect_in_array(refl, scl, valid, valid, p)
        if not dets:
            out.append(None)
            continue
        d = min(dets, key=lambda d: math.hypot(d.row - c["rc"][0], d.col - c["rc"][1]))
        out.append(d)
    return out


def summary(cases, dets):
    rows = []
    for c, d in zip(cases, dets):
        if d is None:
            continue
        dh = abs((d.heading_deg - c["heading"] + 90) % 180 - 90)
        rows.append((c["L"], d.length_m - c["L"], d.width_m - c["W"], dh,
                     getattr(d, "length_err_m", float("nan")), getattr(d, "width_err_m", float("nan"))))
    a = np.array(rows)
    res = {"found": f"{len(rows)}/{len(cases)}"}
    for name, lo, hi in (("all", 0, 1e9), ("<60 m", 0, 60), ("60-150 m", 60, 150), (">150 m", 150, 1e9)):
        m = (a[:, 0] >= lo) & (a[:, 0] < hi)
        if not m.any():
            continue
        dl, dw, dh = a[m, 1], a[m, 2], a[m, 3]
        rel = dl / a[m, 0]
        res[name] = dict(n=int(m.sum()), len_bias=round(float(dl.mean()), 1), len_rmse=round(float(np.sqrt((dl ** 2).mean())), 1),
                         len_rel_rmse=f"{100 * np.sqrt((rel ** 2).mean()):.1f}%", beam_bias=round(float(dw.mean()), 1),
                         beam_rmse=round(float(np.sqrt((dw ** 2).mean())), 1), heading_med=round(float(np.median(dh)), 1))
        if not np.isnan(a[m, 4]).all():
            # share of cases where truth lies within the reported +-1 sigma
            res[name]["len_within_1sigma"] = f"{100 * np.mean(np.abs(dl) <= a[m, 4]):.0f}%"
            res[name]["beam_within_1sigma"] = f"{100 * np.mean(np.abs(dw) <= a[m, 5]):.0f}%"
    return res
