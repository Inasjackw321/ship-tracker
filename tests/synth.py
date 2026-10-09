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


S3_FLAGS = {"LAND": 1 << 31, "CLOUD": 1 << 27, "CLOUD_AMBIGUOUS": 1 << 28, "CLOUD_MARGIN": 1 << 29,
            "COASTLINE": 1 << 30, "WATER": 1 << 0}


def write_s3_granule(dirpath, lat0=15.0, lon0=64.0, size=200, seed=0):
    """Write an OLCI-WFR-like granule (3 netCDF/HDF5 files) and return the true ship specks.

    Open water at 300 m with three large-ship specks, a cloud field with bright specks
    in it, and a land corner.
    """
    import h5py

    rng = np.random.default_rng(seed)
    rows, cols = np.mgrid[0:size, 0:size]
    lat = lat0 + (size / 2 - rows) * 0.0027
    lon = lon0 + (cols - size / 2) * 0.0028
    refl = 0.002 + rng.normal(0, 0.0003, (size, size))
    flags = np.full((size, size), S3_FLAGS["WATER"], np.uint64)

    ships = [(40, 50), (120, 160), (160, 60)]
    for r, c in ships:
        refl[r, c] += 0.03
        refl[r, c + 1] += 0.008  # a little spill into the neighbouring pixel (wake)
    # cloud field: a flagged cloud with bright puffs around it
    cy, cx = 60, 140
    yy, xx = np.mgrid[0:size, 0:size]
    blob = (yy - cy) ** 2 + (xx - cx) ** 2 < 5 ** 2
    refl[blob] += 0.3
    flags[blob] |= np.uint64(S3_FLAGS["CLOUD"])
    for dr, dc in ((-8, 3), (6, -7), (2, 9), (-4, -9)):
        refl[cy + dr, cx + dc] += 0.04
        flags[cy + dr, cx + dc] |= np.uint64(S3_FLAGS["CLOUD_AMBIGUOUS"])
    # land corner
    land = (yy > 175) & (xx > 175)
    refl[land] = 0.2
    flags[land] = np.uint64(S3_FLAGS["LAND"])

    def var(f, name, data, dtype, scale, offset=0.0, fill=None):
        raw = np.round((data - offset) / scale).astype(dtype)
        ds = f.create_dataset(name, data=raw, compression="gzip")
        ds.attrs["scale_factor"] = scale
        ds.attrs["add_offset"] = offset
        if fill is not None:
            ds.attrs["_FillValue"] = np.array(fill, dtype=dtype)

    with h5py.File(dirpath / "Oa17_reflectance.nc", "w") as f:
        var(f, "Oa17_reflectance", refl, np.uint16, 1e-5, 0.0, fill=65535)
    with h5py.File(dirpath / "geo_coordinates.nc", "w") as f:
        var(f, "latitude", lat, np.int32, 1e-6)
        var(f, "longitude", lon, np.int32, 1e-6)
    with h5py.File(dirpath / "wqsf.nc", "w") as f:
        ds = f.create_dataset("WQSF", data=flags)
        ds.attrs["flag_masks"] = np.array(list(S3_FLAGS.values()), np.uint64)
        ds.attrs["flag_meanings"] = " ".join(S3_FLAGS)
    footprint = [[lon.min(), lat.min()], [lon.max(), lat.min()], [lon.max(), lat.max()],
                 [lon.min(), lat.max()], [lon.min(), lat.min()]]
    truth = [(float(lat[r, c]), float(lon[r, c] + 0.0028 * 0.2)) for r, c in ships]
    return truth, {"type": "Polygon", "coordinates": [footprint]}
