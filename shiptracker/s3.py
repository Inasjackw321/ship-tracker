"""Sentinel-3 OLCI (300 m) "bright speck" detection for open sea that Sentinel-2 never images.

What this can and cannot do
---------------------------
An OLCI full-resolution pixel is ~300 m across. A large ship (say 300 x 50 m) fills a
fifth of one pixel, which still lifts that pixel's near-infrared reflectance well above
the dark open sea, often helped by its bright wake. So big ships (roughly 150 m and
up, in clear sky) can show up as isolated bright specks. Their *size cannot be
measured* at this resolution, positions are good to a few hundred metres, and small
ships are invisible. Results are therefore stored separately as "possible large ships".

Data: the OLCI Level-2 water product (WFR) from Microsoft Planetary Computer: the
865 nm band (Oa17), per-pixel latitude/longitude, and the quality flags (land, cloud).
"""
from __future__ import annotations

import logging
import shutil
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import h5py
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage
from shapely.geometry import Point, mapping, shape
from shapely.geometry.base import BaseGeometry

from .chips import _font
from .download import download_file
from .stac import search_items

log = logging.getLogger(__name__)


@dataclass
class S3Params:
    bg_block: int = 16           # coarse background block (px)
    bg_window: int = 15          # local statistics window (px)
    k_sigma: float = 6.0
    min_contrast: float = 0.005  # reflectance above background
    std_floor: float = 0.0008
    max_pixels: int = 6          # ships are specks; bigger bright blobs are cloud/land
    coast_buffer_px: int = 3     # ~1 km
    cloud_min_px: int = 4        # flagged-cloud blobs this big count as cloud
    cloud_buffer_px: int = 3
    ring_inner: int = 3
    ring_outer: int = 10
    max_ring_cloud: float = 0.05
    max_ring_clutter: float = 0.03


@dataclass
class Granule:
    id: str
    datetime: str
    geometry: dict
    hrefs: dict  # refl / geo / flags -> URL


@dataclass
class S3Detection:
    lon: float
    lat: float
    row: float
    col: float
    contrast: float
    snr: float
    npix: int
    confidence: float


# --------------------------------------------------------------------------- search


def _find_href(assets: dict, filename: str) -> str | None:
    for a in assets.values():
        href = a.get("href", "")
        if href.split("?")[0].endswith(filename):
            return href
    return None


def search_granules(cfg: dict, region: BaseGeometry, start: str, end: str) -> list[Granule]:
    # A simple outline: the open-sea region can be a complicated multipolygon, which STAC
    # APIs may reject; granules are matched against the real region afterwards.
    body = {
        "collections": [cfg["collection"]],
        "intersects": mapping(region.convex_hull.simplify(0.05)),
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": 100,
    }
    out = []
    items = search_items(cfg["stac_url"], body)
    for item in items:
        hrefs = {k: _find_href(item.get("assets", {}), name) for k, name in cfg["files"].items()}
        if all(hrefs.values()):
            out.append(Granule(item["id"], item["properties"]["datetime"], item["geometry"], hrefs))
    if items and not out:
        sample = items[0]
        hrefs = [a.get("href", "").split("?")[0].rsplit("/", 1)[-1] for a in sample.get("assets", {}).values()]
        raise RuntimeError(f"{len(items)} Sentinel-3 images found but none has the files "
                           f"{sorted(cfg['files'].values())}; first image ({sample['id']}) has: {sorted(hrefs)}")
    out.sort(key=lambda g: g.datetime, reverse=True)
    return out


def select_granules(granules: list[Granule], region: BaseGeometry, min_gain: float = 0.1) -> list[Granule]:
    """Newest granules first, each kept only if at least ``min_gain`` of its part of the
    region has not been seen in a newer granule."""
    total = region.area
    covered = None
    chosen = []
    for g in granules:
        geom = shape(g.geometry).intersection(region)
        if geom.is_empty:
            continue
        gain = geom.area if covered is None else geom.difference(covered).area
        if gain >= min_gain * geom.area:
            chosen.append(g)
            covered = geom if covered is None else covered.union(geom)
        if covered is not None and covered.area >= 0.98 * total:
            break
    return chosen


# --------------------------------------------------------------------------- reading


def _attr(ds, name, default=None):
    v = ds.attrs.get(name, default)
    if isinstance(v, np.ndarray) and v.size == 1:
        v = v.item()
    if isinstance(v, bytes):
        v = v.decode()
    return v


def read_scaled(path: Path, var: str) -> np.ndarray:
    """Read a netCDF variable (via HDF5) applying fill value, scale and offset."""
    with h5py.File(path, "r") as f:
        ds = f[var]
        raw = ds[...]
        fill = _attr(ds, "_FillValue")
        scale = _attr(ds, "scale_factor", 1.0)
        offset = _attr(ds, "add_offset", 0.0)
        log10 = str(_attr(ds, "log10_scaled", "")).lower() == "true"
    data = raw.astype(np.float64) * scale + offset
    if fill is not None:
        data[raw == fill] = np.nan
    if log10:
        data = 10 ** data
    return data.astype(np.float32) if var not in ("latitude", "longitude") else data


def read_flags(path: Path) -> tuple[np.ndarray, dict[str, int]]:
    """Quality flags and their bit masks. Handles both the single 64-bit ``WQSF``
    variable and older products that split it into ``WQSF_lsb`` / ``WQSF_msb``."""
    def table(ds, shift=0):
        names = str(_attr(ds, "flag_meanings", "")).split()
        masks = np.atleast_1d(ds.attrs.get("flag_masks", []))
        return {n: int(m) << shift for n, m in zip(names, masks)}

    with h5py.File(path, "r") as f:
        if "WQSF" in f:
            return f["WQSF"][...].astype(np.uint64), table(f["WQSF"])
        if "WQSF_lsb" in f:
            lsb = f["WQSF_lsb"]
            flags = lsb[...].astype(np.uint64)
            tab = table(lsb)
            if "WQSF_msb" in f:
                msb = f["WQSF_msb"]
                flags |= msb[...].astype(np.uint64) << np.uint64(32)
                tab.update(table(msb, 32))
            return flags, tab
        raise KeyError(f"no WQSF flags in {Path(path).name}; variables: {sorted(f.keys())}")


def _flag(flags: np.ndarray, table: dict[str, int], *names: str) -> np.ndarray:
    bits = 0
    for n in names:
        bits |= table.get(n, 0)
    return (flags & np.uint64(bits)) != 0 if bits else np.zeros(flags.shape, bool)


# --------------------------------------------------------------------------- detection


def _coarse_median(img: np.ndarray, mask: np.ndarray, block: int) -> np.ndarray:
    H, W = img.shape
    hb, wb = -(-H // block), -(-W // block)
    pad = np.full((hb * block, wb * block), np.nan, np.float32)
    pad[:H, :W] = np.where(mask, img, np.nan)
    blocks = pad.reshape(hb, block, wb, block).transpose(0, 2, 1, 3).reshape(hb, wb, -1)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(blocks, axis=2)
    fill = np.nanmedian(med) if np.isfinite(med).any() else 0.0
    med = ndimage.median_filter(np.where(np.isfinite(med), med, fill), size=3, mode="nearest")
    return np.repeat(np.repeat(med, block, 0), block, 1)[:H, :W]


def detect_specks(refl: np.ndarray, land: np.ndarray, cloud_flag: np.ndarray, cloud_any: np.ndarray,
                  p: S3Params, rejected: Counter | None = None) -> list[tuple[float, float, float, float, int]]:
    """Return (row, col, contrast, snr, npix) of isolated bright specks over open water."""
    rejected = rejected if rejected is not None else Counter()
    valid = np.isfinite(refl)
    r = np.where(valid, refl, 0).astype(np.float32)
    water = valid & ~land

    labels, n = ndimage.label(cloud_flag)
    sizes = np.bincount(labels.ravel(), minlength=n + 1)
    sizes[0] = 0
    cloud = (sizes >= p.cloud_min_px)[labels]
    if cloud.any():
        cloud = ndimage.maximum_filter(cloud, size=2 * p.cloud_buffer_px + 1)
    near_land = ndimage.maximum_filter(land | ~valid, size=2 * p.coast_buffer_px + 1)
    search = water & ~cloud & ~near_land
    if not search.any():
        return []

    bg = _coarse_median(r, search, p.bg_block)
    ok = search & (r < bg + p.min_contrast)
    m = ok.astype(np.float32)
    n_ = ndimage.uniform_filter(m, p.bg_window)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = ndimage.uniform_filter(r * m, p.bg_window) / n_
        var = ndimage.uniform_filter(r * r * m, p.bg_window) / n_ - mean ** 2
    mean, std = np.nan_to_num(mean), np.sqrt(np.clip(np.nan_to_num(var), 0, None))
    excess = r - mean
    snr = excess / np.maximum(std, p.std_floor)
    cand = search & (n_ > 0.3) & (snr > p.k_sigma) & (excess > p.min_contrast)

    labels, n = ndimage.label(cand, structure=np.ones((3, 3)))
    out = []
    for i, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        blob = labels[sl] == i
        npix = int(blob.sum())
        if npix > p.max_pixels:
            rejected["too big (cloud/land)"] += 1
            continue
        w = np.clip(excess[sl], 0, None) * blob
        rr, cc = np.nonzero(blob)
        wr = w[rr, cc]
        row = sl[0].start + float(np.average(rr, weights=wr))
        col = sl[1].start + float(np.average(cc, weights=wr))
        H, W = r.shape
        ys, xs = np.mgrid[max(int(row) - p.ring_outer, 0):min(int(row) + p.ring_outer + 1, H),
                          max(int(col) - p.ring_outer, 0):min(int(col) + p.ring_outer + 1, W)]
        d = np.hypot(ys - row, xs - col)
        ring = (d >= p.ring_inner) & (d <= p.ring_outer) & valid[ys, xs] & ~land[ys, xs]
        if ring.sum() > 20:
            if cloud_any[ys, xs][ring].mean() > p.max_ring_cloud:
                rejected["cloud nearby"] += 1
                continue
            if (excess[ys, xs][ring] > p.min_contrast).mean() > p.max_ring_clutter:
                rejected["cluttered"] += 1
                continue
        out.append((row, col, float(excess[sl][blob].max()), float(snr[sl][blob].max()), npix))
    return out


def _bilinear(arr: np.ndarray, row: float, col: float) -> float:
    return float(ndimage.map_coordinates(arr, [[row], [col]], order=1, mode="nearest")[0])


def render_s3_chip(refl: np.ndarray, row: float, col: float, out: Path, caption: str) -> Path:
    half, scale = 20, 8
    r0, c0 = int(row) - half, int(col) - half
    H, W = refl.shape
    crop = np.zeros((2 * half + 1, 2 * half + 1), np.float32)
    rs, cs = max(r0, 0), max(c0, 0)
    re, ce = min(r0 + 2 * half + 1, H), min(c0 + 2 * half + 1, W)
    crop[rs - r0:re - r0, cs - c0:ce - c0] = np.nan_to_num(refl[rs:re, cs:ce])
    lo, hi = float(np.percentile(crop, 50)), float(crop.max())  # dark sea, speck at full white
    img8 = (np.clip((crop - lo) / max(hi - lo, 1e-6), 0, 1) * 255).astype(np.uint8)
    img = Image.fromarray(img8).convert("RGB").resize((img8.shape[1] * scale, img8.shape[0] * scale), Image.NEAREST)
    d = ImageDraw.Draw(img)
    cx, cy = (col - c0 + 0.5) * scale, (row - r0 + 0.5) * scale
    d.ellipse([cx - 22, cy - 22, cx + 22, cy + 22], outline=(200, 120, 255), width=3)
    small = _font(13)
    d.rectangle([0, img.height - 22, img.width, img.height], fill=(0, 0, 0))
    d.text((6, img.height - 11), caption, fill=(255, 255, 255), font=small, anchor="lm")
    bar = 3000 / 300 * scale  # 3 km
    d.line([(img.width - 10 - bar, 12), (img.width - 10, 12)], fill=(255, 255, 255), width=3)
    d.text((img.width - 10 - bar / 2, 24), "3 km", fill=(255, 255, 255), font=small, anchor="mm")
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, optimize=True)
    return out


def detect_granule(refl_path: Path, geo_path: Path, flags_path: Path, region: BaseGeometry,
                   chips_dir: Path | None = None, label: str = "", p: S3Params | None = None,
                   rejected: Counter | None = None) -> list[tuple[S3Detection, str | None]]:
    p = p or S3Params()
    rejected = rejected if rejected is not None else Counter()
    refl = read_scaled(refl_path, "Oa17_reflectance")
    lat = read_scaled(geo_path, "latitude")
    lon = read_scaled(geo_path, "longitude")
    flags, table = read_flags(flags_path)
    land = _flag(flags, table, "LAND", "COASTLINE", "INLAND_WATER", "SNOW_ICE")
    cloud_flag = _flag(flags, table, "CLOUD", "CLOUD_MARGIN")
    cloud_any = _flag(flags, table, "CLOUD", "CLOUD_AMBIGUOUS", "CLOUD_MARGIN")

    results = []
    for row, col, contrast, snr, npix in detect_specks(refl, land, cloud_flag, cloud_any, p, rejected):
        la, lo = _bilinear(lat, row, col), _bilinear(lon, row, col)
        if not region.contains(Point(lo, la)):
            rejected["outside open-sea area"] += 1
            continue
        conf = round(float(np.clip(0.5 * min(snr / 20, 1) + 0.5 * min(contrast / 0.03, 1), 0, 1)), 3)
        det = S3Detection(lo, la, row, col, contrast, snr, npix, conf)
        chip = None
        if chips_dir is not None:
            chip = render_s3_chip(refl, row, col, chips_dir / f"{len(results):04d}.png",
                                  f"Sentinel-3 300 m  possible large ship  {label}")
        results.append((det, chip))
    return results


def process_granule(settings, store, g: Granule, region: BaseGeometry, keep_files: bool) -> int:
    cfg = settings.s3_config()
    gdir = settings.tiles_dir / "s3" / g.id
    paths = {k: download_file(href, gdir / cfg["files"][k], cfg) for k, href in g.hrefs.items()}
    rejected: Counter = Counter()
    chips_dir = settings.chips_dir / "s3" / g.id
    found = detect_granule(paths["refl"], paths["geo"], paths["flags"], region, chips_dir,
                           g.datetime[:10], rejected=rejected)
    rows = [(d, Path(c).relative_to(settings.chips_dir).as_posix() if c else None) for d, c in found]
    added = store.add_s3_detections(g, rows)
    why = ", ".join(f"{n} {r}" for r, n in rejected.most_common())
    store.save_s3_granule(g, "done", f"{len(found)} specks, {added} new" + (f"; rejected {why}" if why else ""), added)
    if not keep_files:
        shutil.rmtree(gdir, ignore_errors=True)
    return added


def diagnose(settings, region: BaseGeometry, days: int = 7, out=print) -> int:
    """Step-by-step Sentinel-3 check against the live service; prints OK / FAILED per step.

    Returns 0 when a real image was downloaded, read and analysed.
    """
    import traceback
    from datetime import date, timedelta

    from .stac import sign_href

    cfg = settings.s3_config()
    end = date.today()
    start = end - timedelta(days=days)

    def step(name, fn):
        out(f"- {name} ...")
        try:
            res = fn()
            out("  OK" + (f": {res}" if isinstance(res, str) and res else ""))
            return res, True
        except Exception as exc:
            out(f"  FAILED: {type(exc).__name__}: {exc}")
            out("  " + traceback.format_exc().strip().splitlines()[-1])
            return None, False

    out(f"Sentinel-3 check: {cfg['stac_url']} collection {cfg['collection']}, {start} .. {end}")
    granules, ok = step("search", lambda: search_granules(cfg, region, start.isoformat(), end.isoformat()))
    if not ok:
        return 1
    out(f"  {len(granules)} images with the needed files")
    for g in granules[:5]:
        out(f"    {g.datetime[:16]}  {g.id}")
    if not granules:
        out("  Nothing to test: Planetary Computer has no images for this window yet. Try --days 14.")
        return 1
    g = granules[0]
    _, ok = step("access token", lambda: "signed" if "?" in sign_href(g.hrefs["flags"], cfg) else "not needed")
    if not ok:
        return 1
    gdir = settings.tiles_dir / "s3" / g.id
    paths = {}
    for key in ("flags", "refl", "geo"):
        p, ok = step(f"download {cfg['files'][key]}",
                     lambda key=key: download_file(g.hrefs[key], gdir / cfg["files"][key], cfg))
        if not ok:
            return 1
        paths[key] = p
        out(f"    {p.stat().st_size / 1e6:.1f} MB")
    refl, ok = step("read Oa17 reflectance", lambda: read_scaled(paths["refl"], "Oa17_reflectance"))
    if not ok:
        return 1
    finite = np.isfinite(refl)
    out(f"    {refl.shape[0]} x {refl.shape[1]} pixels, {finite.mean():.0%} valid, "
        f"median {np.nanmedian(refl):.4f}, 99.9% {np.nanpercentile(refl, 99.9):.4f}")
    flags, ok = step("read quality flags", lambda: read_flags(paths["flags"]))
    if not ok:
        return 1
    fl, table = flags
    out(f"    flags: {', '.join(n for n in ('LAND', 'CLOUD', 'CLOUD_AMBIGUOUS', 'CLOUD_MARGIN', 'COASTLINE') if n in table)}"
        f"{'' if 'LAND' in table else '  (LAND flag missing!)'}")
    _, ok = step("read latitude / longitude", lambda: read_scaled(paths["geo"], "latitude").shape)
    if not ok:
        return 1
    rejected = Counter()
    found, ok = step("detect bright specks", lambda: detect_granule(paths["refl"], paths["geo"], paths["flags"],
                                                                    region, rejected=rejected))
    if not ok:
        return 1
    out(f"    {len(found)} possible large ships in the area; rejected: "
        f"{', '.join(f'{n} {r}' for r, n in rejected.most_common()) or 'none'}")
    out("Sentinel-3 works on this computer.")
    return 0
