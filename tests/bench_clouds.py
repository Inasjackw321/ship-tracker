"""Scenes of scattered fair-weather cumulus with ships among them.

Cumulus over the sea is the main source of false ships: puffs from tens of metres to
half a kilometre, lumpy and irregular, sometimes lined up in wind streets. Sentinel-2's
scene classification (SCL) marks the big, bright ones as cloud but misses many small or
faint ones, which then look like bright objects on dark water. Some puffs cast shadows.
"""
from __future__ import annotations

import math

import numpy as np

from tests.synth import ship_footprint


def _puff(shape, r, c, size, amp, aspect, angle, lumps, rng):
    """A lumpy cumulus puff: a few Gaussian blobs, blurred edge (excess reflectance)."""
    H, W = shape
    pad = int(size * 4) + 4
    r0, r1, c0, c1 = max(int(r) - pad, 0), min(int(r) + pad, H), max(int(c) - pad, 0), min(int(c) + pad, W)
    yy, xx = np.mgrid[r0:r1, c0:c1]
    out = np.zeros((r1 - r0, c1 - c0), np.float32)
    ca, sa = math.cos(angle), math.sin(angle)
    for _ in range(lumps):
        dr, dc = rng.normal(0, size * 0.6, 2)
        s = size * rng.uniform(0.5, 1.0)
        u = ((yy - r - dr) * ca + (xx - c - dc) * sa) / (s * aspect)
        v = (-(yy - r - dr) * sa + (xx - c - dc) * ca) / s
        out = np.maximum(out, amp * rng.uniform(0.6, 1.0) * np.exp(-(u * u + v * v) / 2))
    return (slice(r0, r1), slice(c0, c1)), out


def make_cloud_scene(seed: int, size: int = 900, n_clouds: int = 70, n_ships: int = 8):
    rng = np.random.default_rng(seed)
    refl = np.full((size, size), 0.015 + rng.uniform(0, 0.015), np.float32)
    refl += np.linspace(0, rng.uniform(0, 0.01), size, dtype=np.float32)[None, :]  # glint
    refl += rng.normal(0, rng.uniform(0.0015, 0.004), refl.shape).astype(np.float32)
    scl = np.full(refl.shape, 6, np.uint8)
    cloud = np.zeros(refl.shape, np.float32)  # cloud brightness alone, to tell hidden ships
    sun = rng.uniform(0, 2 * math.pi)
    streets = rng.random() < 0.4
    street_angle = rng.uniform(0, math.pi)
    for _ in range(n_clouds):
        r, c = rng.uniform(40, size - 40, 2)
        sz = float(np.exp(rng.uniform(np.log(1.5), np.log(22))))  # 15 m .. 220 m sigma
        amp = float(rng.uniform(0.04, 0.6))
        aspect = rng.uniform(1.5, 3.5) if streets else rng.uniform(1.0, 1.8)
        ang = street_angle if streets else rng.uniform(0, math.pi)
        sl, puff = _puff(refl.shape, r, c, sz, amp, aspect, ang, int(rng.integers(1, 6)), rng)
        refl[sl] += puff
        cloud[sl] = np.maximum(cloud[sl], puff)
        # Sen2Cor finds big bright clouds; small/faint ones stay "water" (or unclassified)
        if sz * amp > 1.2:
            scl[sl][puff > 0.12] = 9 if amp > 0.35 else 8
        elif rng.random() < 0.3:
            scl[sl][puff > 0.1] = 7
        if rng.random() < 0.5 and sz > 3:  # shadow, offset away from the sun
            d = sz * rng.uniform(3, 8)
            sr, sc = int(round(d * math.cos(sun))), int(round(d * math.sin(sun)))
            shadow = np.roll(np.roll(puff, sr, 0), sc, 1)
            refl[sl] *= (1 - 0.5 * np.clip(shadow / max(amp, 1e-6), 0, 1)).astype(np.float32)
    truth = []
    for _ in range(n_ships):
        L = float(np.exp(rng.uniform(np.log(30), np.log(330))))
        Wd = float(np.clip(L / rng.uniform(4.5, 7.5), 6, 60))
        hd = float(rng.uniform(0, 180))
        r, c = (int(v) for v in rng.uniform(60, size - 60, 2))
        fp = ship_footprint(L, Wd, hd, int(L / 10 * 1.6) + 12 + (int(L / 10 * 1.6) % 2))
        h = fp.shape[0] // 2
        if r - h < 0 or c - h < 0 or r + h > size or c + h > size:
            continue
        # a ship under cloud cannot be seen: only ships on clear water count as missed
        if cloud[max(r - h, 0):r + h, max(c - h, 0):c + h].max() > 0.01:
            continue
        refl[r - h:r - h + fp.shape[0], c - h:c - h + fp.shape[1]] += fp * rng.uniform(0.6, 1.4)
        truth.append((r, c, L))
    return refl, scl, truth


def evaluate(params=None, n_scenes: int = 24, seed0: int = 100):
    """(false ships per scene, share of ships found) over ``n_scenes`` cloud scenes."""
    from shiptracker.detect import DetectParams, detect_in_array

    p = params or DetectParams()
    false, found, total = 0, 0, 0
    for k in range(n_scenes):
        refl, scl, truth = make_cloud_scene(seed0 + k)
        v = np.ones(refl.shape, bool)
        dets = detect_in_array(refl, scl, v, v, p, 10.0)
        hit = set()
        for d in dets:
            near = [i for i, (r, c, L) in enumerate(truth) if math.hypot(d.row - r, d.col - c) < max(8, L / 20)]
            if near:
                hit.update(near)
            else:
                false += 1
        found += len(hit)
        total += len(truth)
    return false / n_scenes, found / max(total, 1)
