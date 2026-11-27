"""Synthetic Sentinel-2-like scenes with ships of known size, for tests."""
from __future__ import annotations

import math

import numpy as np
from scipy import ndimage

SS = 10  # supersampling factor (1 m sub-pixels at 10 m GSD)


def ship_footprint(length_m: float, width_m: float, heading_deg: float, size_px: int,
                   superstructure: bool = True) -> np.ndarray:
    """Hull reflectance excess rendered at 10 m pixels, centred in a size_px square.

    The hull has a pointed bow (last 12 % of length) and, optionally, a brighter
    superstructure block near the stern, like the vessel in the reference image.
    """
    n = size_px * SS
    yy, xx = np.mgrid[0:n, 0:n] + 0.5
    cy = cx = n / 2
    th = math.radians(heading_deg)
    ux, uy = math.sin(th), -math.cos(th)  # along-hull unit vector (east, south-down)
    s = ((xx - cx) * ux + (yy - cy) * uy) / SS * 10  # metres along hull
    t = ((xx - cx) * -uy + (yy - cy) * ux) / SS * 10  # metres across
    half_l, half_w = length_m / 2, width_m / 2
    bow = half_l - 0.12 * length_m
    taper = np.where(s > bow, half_w * (1 - (s - bow) / (half_l - bow)), half_w)
    hull = (np.abs(s) <= half_l) & (np.abs(t) <= taper)
    img = hull * 0.12
    if superstructure:
        sup = (s > -half_l + 0.03 * length_m) & (s < -half_l + 0.15 * length_m) & (np.abs(t) <= half_w * 0.8)
        img = np.where(sup, 0.30, img)
    small = img.reshape(size_px, SS, size_px, SS).mean(axis=(1, 3))
    return ndimage.gaussian_filter(small, 0.53)


def make_scene(shape=(600, 600), ships=(), seed=0, glint=True, land=True, cloud=True):
    """Return (refl, scl, truth) with ships as (row, col, length_m, width_m, heading)."""
    rng = np.random.default_rng(seed)
    H, W = shape
    refl = np.full(shape, 0.015, np.float32)
    if glint:
        refl += np.linspace(0, 0.02, W, dtype=np.float32)[None, :]
    refl += rng.normal(0, 0.002, shape).astype(np.float32)
    scl = np.full(shape, 6, np.uint8)
    if land:
        refl[:, :60] = 0.25 + rng.normal(0, 0.03, (H, 60))
        scl[:, :60] = 5
    if cloud:
        yy, xx = np.mgrid[0:H, 0:W]
        blob = (yy - 80) ** 2 + (xx - 500) ** 2 < 40 ** 2
        refl[blob] = 0.6
        scl[blob] = 9
    truth = []
    for (r, c, L, Wd, hd) in ships:
        size = int(L / 10 * 1.6) + 12
        size += size % 2
        fp = ship_footprint(L, Wd, hd, size)
        r0, c0 = r - size // 2, c - size // 2
        refl[r0:r0 + size, c0:c0 + size] += fp.astype(np.float32)
        # SCL tends to mislabel hull pixels: mark the brightest ones as non-water.
        scl[r0:r0 + size, c0:c0 + size][fp > 0.05] = 8
        truth.append((r - 0.5, c - 0.5, L, Wd, hd))
    return refl, scl, truth


def add_cumulus_field(refl, scl, center, radius, n=40, seed=0):
    """Scatter small fair-weather cumulus (Gaussian puffs) with SCL marking their cores."""
    rng = np.random.default_rng(seed)
    H, W = refl.shape
    yy, xx = np.mgrid[0:H, 0:W]
    for _ in range(n):
        r = center[0] + rng.uniform(-radius, radius)
        c = center[1] + rng.uniform(-radius, radius)
        sy, sx = rng.uniform(1.5, 8, 2)
        amp = rng.uniform(0.05, 0.5)
        puff = amp * np.exp(-(((yy - r) / sy) ** 2 + ((xx - c) / sx) ** 2) / 2)
        refl += puff.astype(np.float32)
        scl[puff > 0.15] = 8
    return refl, scl



def make_harbour(seed=0):
    """Naval-base-like harbour: land to the east with piers reaching west into the water,
    grey ships moored alongside the piers (touching them) and one ship out at sea.

    Returns (refl, scl_today, berth, moored, offshore): in today's scene classification
    the moored ships merge with their piers (non-water); ``berth`` marks the pixels that
    were open water on other passes, when the berths were empty."""
    rng = np.random.default_rng(seed)
    H, W = 600, 600
    refl = np.full((H, W), 0.02, np.float32) + rng.normal(0, 0.002, (H, W)).astype(np.float32)
    scl = np.full((H, W), 6, np.uint8)
    refl[:, 450:] = 0.22 + rng.normal(0, 0.03, (H, 150))
    scl[:, 450:] = 5
    moored = []
    for k, r in enumerate(range(80, 560, 60)):
        refl[r:r + 3, 380:450] = 0.25  # pier, 30 m wide, 700 m long
        scl[r:r + 3, 380:450] = 5
        if k % 2 == 0:  # a grey warship (~200 x 25 m) moored along the north side of the pier
            L, Wd = rng.uniform(150, 250), 25
            fp = ship_footprint(L, Wd, 90.0, int(L / 10 * 1.6) + 12 + (int(L / 10 * 1.6) % 2))
            fp *= 0.8
            h = fp.shape[0] // 2
            cr, cc = r - 2, 445 - int(L / 20) - 2  # hull centre: touching the pier
            refl[cr - h:cr - h + fp.shape[0], cc - h:cc - h + fp.shape[1]] += fp
            scl[cr - h:cr - h + fp.shape[0], cc - h:cc - h + fp.shape[1]][fp > 0.02] = 5
            moored.append((cr, cc, L))
    berth = (scl != 6) & (np.arange(W)[None, :] < 450)  # not water today, water on other days
    for r in range(80, 560, 60):
        berth[r:r + 3, 380:450] = False  # piers are never water
    offshore = (300, 150, 180.0)
    fp = ship_footprint(180, 30, 20.0, 40)
    refl[280:320, 130:170] += fp
    return refl, scl, berth, moored, offshore
