"""Second-stage checks on detections, using data the detector itself does not look at.

* Colour (from the true-colour image, B04/B03/B02): vegetation is far brighter in NIR
  than in red (high NDVI), while hull paint and steel are not; a cloud is bright and
  grey-white in every band.
* A global 1 km land map: anything well inside the coastline (salt pans, rooftops,
  inland water) is not a ship at sea.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

from .scene import GeoDetection, apply_affine

log = logging.getLogger(__name__)

TCI_FULL_SCALE = 0.2  # the true-colour image maps reflectance 0-0.2 onto 0-255


@dataclass
class VerifyParams:
    max_ndvi: float = 0.5        # above this the object is vegetation
    cloud_min_blue: float = 0.16  # cloud: blue reflectance at least this ...
    cloud_flatness: float = 0.75  # ... and min/max over R, G, B, NIR at least this
    inland_km: float = 1.0       # land in every direction this far = inland


def hull_pixels(g: GeoDetection, step: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Fractional (row, col) samples covering the measured hull rectangle."""
    d = g.det
    s = np.arange(-d.length_px / 2, d.length_px / 2 + 1e-6, step)
    t = np.arange(-max(d.width_px, 1.0) / 2, max(d.width_px, 1.0) / 2 + 1e-6, step)
    S, T = np.meshgrid(s, t, indexing="ij")
    vr, vc = -d.axis_c, d.axis_r
    return (d.row + S * d.axis_r + T * vr).ravel(), (d.col + S * d.axis_c + T * vc).ravel()


def _sample(src, g: GeoDetection, rows: np.ndarray, cols: np.ndarray, grid_transform, bands) -> np.ndarray:
    """Mean value per band at hull samples given in the detection (NIR) pixel grid."""
    xs, ys = apply_affine(grid_transform, cols + 0.5, rows + 0.5)
    inv = ~src.transform
    c, r = apply_affine(inv, xs, ys)
    ri, ci = np.floor(r).astype(int), np.floor(c).astype(int)
    r0, c0 = ri.min(), ci.min()
    win = Window(c0, r0, ci.max() - c0 + 1, ri.max() - r0 + 1)
    arr = src.read(bands, window=win, boundless=True, fill_value=0).astype(np.float32)
    vals = arr[:, ri - r0, ci - c0]
    ok = (vals > 0).all(axis=0)
    return vals[:, ok].mean(axis=1) if ok.any() else np.full(len(bands), np.nan)


def _inland(dets: list[GeoDetection], km: float) -> np.ndarray:
    try:
        from global_land_mask import globe
    except ImportError:  # optional: skip the check rather than fail the scan
        log.warning("global-land-mask not installed; inland check skipped")
        return np.zeros(len(dets), bool)
    lat = np.array([g.lat for g in dets])
    lon = np.array([g.lon for g in dets])
    dlat = km / 111.32
    dlon = dlat / np.maximum(np.cos(np.radians(lat)), 0.01)
    land = np.ones(len(dets), bool)
    for oy, ox in ((0, 0), (dlat, 0), (-dlat, 0), (0, dlon), (0, -dlon)):
        la = np.clip(lat + oy, -89.99, 89.99)
        lo = ((lon + ox + 180) % 360) - 180
        land &= globe.is_land(la, lo)
    return land


def verify_detections(dets: list[GeoDetection], nir_path: Path, tci_path: Path | None,
                      nir_scale: float, nir_offset: float, rejected: Counter | None = None,
                      params: VerifyParams | None = None) -> list[GeoDetection]:
    p = params or VerifyParams()
    rejected = rejected if rejected is not None else Counter()
    if not dets:
        return dets
    inland = _inland(dets, p.inland_km)
    keep: list[GeoDetection] = []
    nir_src = rasterio.open(nir_path)
    tci_src = rasterio.open(tci_path) if tci_path else None
    try:
        for g, is_inland in zip(dets, inland):
            if is_inland:
                rejected["inland"] += 1
                continue
            if tci_src is not None:
                rows, cols = hull_pixels(g)
                (nir_dn,) = _sample(nir_src, g, rows, cols, nir_src.transform, (1,))
                rgb = _sample(tci_src, g, rows, cols, nir_src.transform, (1, 2, 3))
                if np.isfinite(nir_dn) and np.isfinite(rgb).all():
                    nir = nir_dn * nir_scale + nir_offset
                    red, green, blue = rgb / 255.0 * TCI_FULL_SCALE
                    ndvi = (nir - red) / max(nir + red, 1e-6)
                    bands = np.array([red, green, blue, min(nir, TCI_FULL_SCALE)])
                    flat = bands.min() / max(bands.max(), 1e-6)
                    if ndvi > p.max_ndvi:
                        rejected["vegetation"] += 1
                        continue
                    if blue >= p.cloud_min_blue and flat >= p.cloud_flatness:
                        rejected["white like cloud"] += 1
                        continue
            keep.append(g)
    finally:
        nir_src.close()
        if tci_src is not None:
            tci_src.close()
    return keep
