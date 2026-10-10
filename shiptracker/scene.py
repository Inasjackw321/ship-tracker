"""Run ship detection over a Sentinel-2 tile (local file or streamed COG), block by block."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from types import SimpleNamespace

import numpy as np
import rasterio
from affine import Affine
from pyproj import Geod, Transformer
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.warp import transform_geom
from rasterio.windows import Window, bounds as window_bounds, from_bounds, transform as window_transform
from shapely.geometry import box, mapping, shape
from shapely.geometry.base import BaseGeometry

from .detect import SCL_WATER, DetectParams, Detection, detect_in_array

log = logging.getLogger(__name__)
_GEOD = Geod(ellps="WGS84")


@dataclass
class GeoDetection:
    det: Detection
    lon: float
    lat: float
    bow_lon: float     # ends of the measured hull axis (bow/stern are ambiguous)
    bow_lat: float
    stern_lon: float
    stern_lat: float
    x: float           # projected (UTM) centre, used for chips
    y: float


def apply_affine(t: Affine, col: float, row: float) -> tuple[float, float]:
    """Map fractional pixel coordinates to (x, y) with a geotransform."""
    return t.a * col + t.b * row + t.c, t.d * col + t.e * row + t.f


def _int_window(w: Window) -> Window:
    return Window(int(round(w.col_off)), int(round(w.row_off)), int(round(w.width)), int(round(w.height)))


def _read_scl(scl_src, win: Window, transform, shape) -> np.ndarray:
    sw = _int_window(from_bounds(*window_bounds(win, transform), transform=scl_src.transform))
    return scl_src.read(1, window=sw, out_shape=shape, resampling=Resampling.nearest,
                        boundless=True, fill_value=0)


def clear_sea_fraction(scl_path, aoi: BaseGeometry) -> float:
    """Fraction of the tile (inside the AOI) that is cloud-free water, from SCL alone.

    Cheap pre-check (SCL is ~20x smaller than the 10 m bands) used to skip tiles
    that are all land or all cloud before downloading the large files.
    """
    with rasterio.open(scl_path) as src:
        step = max(1, src.width // 1000)
        scl = src.read(1, out_shape=(src.height // step, src.width // step), resampling=Resampling.nearest)
        tr = Affine(src.transform.a * src.width / scl.shape[1], 0, src.transform.c,
                    0, src.transform.e * src.height / scl.shape[0], src.transform.f)
        aoi_proj = transform_geom("EPSG:4326", src.crs, mapping(aoi))
        inside = geometry_mask([aoi_proj], out_shape=scl.shape, transform=tr, invert=True)
    valid = inside & (scl != 0)
    if not valid.any():
        return 0.0
    return float(((scl == SCL_WATER) & valid).sum() / valid.sum())


def detect_scene(nir_path, scl_path, aoi: BaseGeometry, nir_scale: float, nir_offset: float,
                 params: DetectParams | None = None, rejected=None) -> list[GeoDetection]:
    p = params or DetectParams()
    results: list[GeoDetection] = []
    with rasterio.open(nir_path) as src, rasterio.open(scl_path) as scl_src:
        H, W = src.height, src.width
        pixel_m = abs(src.res[0])
        aoi_proj = shape(transform_geom("EPSG:4326", src.crs, mapping(aoi)))
        to_wgs = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
        nodata = src.nodata if src.nodata is not None else 0
        B, halo = p.block, p.halo
        n_blocks = math.ceil(H / B) * math.ceil(W / B)
        done = 0
        for r0 in range(0, H, B):
            for c0 in range(0, W, B):
                done += 1
                rr0, cc0 = max(r0 - halo, 0), max(c0 - halo, 0)
                rr1, cc1 = min(r0 + B + halo, H), min(c0 + B + halo, W)
                win = Window(cc0, rr0, cc1 - cc0, rr1 - rr0)
                wb = box(*window_bounds(win, src.transform))
                if not wb.intersects(aoi_proj):
                    continue
                # Decide from the small SCL band whether this block has any sea in the area
                # before reading the big NIR band (which may be streamed over the network).
                shape_ = (int(win.height), int(win.width))
                scl = _read_scl(scl_src, win, src.transform, shape_)
                wt = window_transform(win, src.transform)
                aoi_mask = geometry_mask([mapping(aoi_proj)], out_shape=shape_, transform=wt, invert=True)
                if not ((scl == SCL_WATER) & aoi_mask).any():
                    continue
                dn = src.read(1, window=win)
                valid = dn != nodata
                # Treat the outer rim of the tile like nodata: edge artifacts live there,
                # and the overlapping neighbour tile sees that strip from its interior.
                e = p.edge_buffer_px
                if rr0 < e:
                    valid[: e - rr0] = False
                if cc0 < e:
                    valid[:, : e - cc0] = False
                if rr1 > H - e:
                    valid[H - e - rr1:] = False
                if cc1 > W - e:
                    valid[:, W - e - cc1:] = False
                if not ((scl == SCL_WATER) & valid).any():
                    continue
                refl = dn.astype(np.float32) * nir_scale + nir_offset
                dets = detect_in_array(refl, scl, valid, aoi_mask, p, pixel_m, rejected)
                for d in dets:
                    fr, fc = d.row + rr0, d.col + cc0
                    # Keep each ship once: only in the block whose core holds its centre.
                    if not (r0 <= fr < r0 + B and c0 <= fc < c0 + B):
                        continue
                    d.row, d.col = fr, fc
                    d.hull_nir_dn = _hull_mean(dn, d, rr0, cc0)
                    results.append(_georef(d, src.transform, to_wgs))
                if done % 10 == 0:
                    log.info("  block %d/%d, %d ships so far", done, n_blocks, len(results))
    return results


def _hull_mean(dn: np.ndarray, d: Detection, r_off: int, c_off: int) -> float | None:
    """Mean DN over the hull samples the colour checks use (see verify.hull_pixels), taken
    from the block already in memory so the NIR band is not fetched a second time."""
    from .verify import hull_pixels

    rows, cols = hull_pixels(SimpleNamespace(det=d))
    ri = np.floor(rows + 0.5).astype(int) - r_off  # sample point -> pixel holding it
    ci = np.floor(cols + 0.5).astype(int) - c_off
    inside = (ri >= 0) & (ri < dn.shape[0]) & (ci >= 0) & (ci < dn.shape[1])
    vals = np.zeros(ri.shape, np.float32)
    vals[inside] = dn[ri[inside], ci[inside]]
    ok = vals > 0
    return float(vals[ok].mean()) if ok.any() else None


def _georef(d: Detection, transform, to_wgs: Transformer) -> GeoDetection:
    def xy(r: float, c: float) -> tuple[float, float]:
        return apply_affine(transform, c + 0.5, r + 0.5)

    half = d.length_px / 2
    x, y = xy(d.row, d.col)
    bx, by = xy(d.row + d.axis_r * half, d.col + d.axis_c * half)
    sx, sy = xy(d.row - d.axis_r * half, d.col - d.axis_c * half)
    lon, lat = to_wgs.transform(x, y)
    blon, blat = to_wgs.transform(bx, by)
    slon, slat = to_wgs.transform(sx, sy)
    # True-north hull axis (the grid heading ignores UTM meridian convergence).
    az, _, _ = _GEOD.inv(slon, slat, blon, blat)
    d.heading_deg = round(az % 180.0, 1)
    return GeoDetection(d, lon, lat, blon, blat, slon, slat, x, y)

