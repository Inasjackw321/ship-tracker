"""Ship detection and size measurement on Sentinel-2 L2A imagery.

Method (per 10 m NIR band B08, where open water is dark and hulls are bright):

1. Sea mask from the Scene Classification Layer (SCL water class, plus very dark
   NIR pixels), with small enclosed "holes" (the ships themselves, which SCL often
   labels as something other than water) filled back in. A buffer is removed along
   the coast and around clouds/cloud shadows.
2. Local background mean/std of NIR reflectance in a sliding window (two passes,
   the second excluding first-pass detections), giving a CFAR-style contrast test.
3. Connected bright components are seeds; each seed is grown to all connected
   pixels brighter than a fraction of its peak excess, which captures the whole hull.
4. Size, first estimate: the hull orientation comes from PCA of the grown pixels; the
   along- and across-hull intensity profiles give length and beam at a fixed fraction
   of the deck level, corrected for the sensor's blur.
5. Size, refined: a model of a hull (rectangle with a pointed bow, blurred exactly as
   the 10 m sensor blurs) is fitted to the pixels by robust least squares, solving
   for centre, axis, length, beam and brightness together. This removes the bias of
   the profile method (pointed bows read short; narrow beams read wide) and gives
   an uncertainty for each size from the fit, plus a calibrated model-error floor.
"""
from __future__ import annotations

import logging
import math
import warnings
from collections import Counter
from dataclasses import asdict, dataclass

import numpy as np
from scipy import ndimage
from scipy.optimize import least_squares
from scipy.special import ndtr, ndtri

log = logging.getLogger(__name__)

# SCL classes: 0 nodata, 1 saturated/defective, 2 dark/topographic shadow,
# 3 cloud shadow, 4 vegetation, 5 bare, 6 water, 7 unclassified,
# 8 cloud medium prob, 9 cloud high prob, 10 thin cirrus, 11 snow.
SCL_CLOUD = (1, 3, 8, 9, 10)
SCL_WATER = 6
SCL_VEGETATION = 4
SCL_SHADOW = (2, 3)
SCL_CLOUDY = (3, 8, 9, 10)  # used for the "is there cloud around it?" test

# Bump when detection changes enough that old results should be recomputed.
DETECTOR_VERSION = 5


@dataclass
class DetectParams:
    block: int = 2048          # processing block (px, must be even)
    halo: int = 96             # overlap around each block (px, must be even)
    bg_window: int = 61        # background window (px)
    k_sigma: float = 5.0       # CFAR threshold in local standard deviations
    min_contrast: float = 0.025  # minimum NIR reflectance above background
    std_floor: float = 0.003   # floor for the local std (reflectance)
    grow_frac: float = 0.25    # grow seeds to this fraction of the peak excess
    profile_frac: float = 0.3  # profile level used for the ends of the hull
    psf_sigma_px: float = 0.53  # Sentinel-2 10 m PSF (Gaussian sigma, px)
    coast_buffer_px: int = 5
    cloud_buffer_px: int = 10
    min_cloud_px: int = 400    # SCL cloud blobs smaller than this may be ships
    # Enclosed non-water blobs up to this size are treated as sea (they are usually the
    # ships themselves). Kept close to the largest hull (~400 x 60 m plus blur) so that
    # islets, sandbars and mangrove patches stay land.
    max_hole_px: int = 600
    water_nir_max: float = 0.04
    min_pixels: int = 3
    min_length_m: float = 25.0
    max_length_m: float = 420.0
    max_width_m: float = 90.0
    min_aspect: float = 2.0    # applied to objects >= aspect_from_m long
    aspect_from_m: float = 40.0
    # Context test: a ring around each object (gap px from the hull end, width px).
    # No seeds this close to the edge of the image data (swath edges, tile borders and
    # nodata gaps produce bright artifacts; overlapping neighbour tiles cover the strip).
    edge_buffer_px: int = 30
    ring_gap_px: int = 10
    ring_width_px: int = 50
    max_ring_cloud: float = 0.05    # share of SCL cloud/shadow pixels allowed in the ring
    max_ring_clutter: float = 0.03  # share of other bright pixels allowed in the ring
    # Hull ends are sharp; cloud puffs fade out. Ratio of the hull length measured at
    # 30 % and at 70 % of the deck level: ~1.05-1.15 for hulls, ~1.4-1.5 for cloud puffs.
    max_edge_softness: float = 1.35
    edge_test_from_px: float = 8.0  # only for objects at least this long (px)
    fill_vegetation_holes: bool = False  # never relabel vegetated islets/strips as sea
    # Hull model fit (refines length/beam; see fit_hull) and its error model. The floors
    # were calibrated on synthetic ships and set conservatively for real imagery.
    model_fit: bool = True
    wake_model: bool = True        # separate a moving ship's wake from its hull
    wake_min_gain: float = 0.40    # the wake model must fit >= 40 % better to be used
    wake_soft_end_px: float = 1.5  # ...and is only tried when one end fades out this gradually
    max_plausible_aspect: float = 9.5  # length/beam above this (150 m+) flags the length as doubtful
    wake_min_length: float = 0.2   # wake fades over >= 0.2 x hull length (shorter = superstructure)
    wake_max_brightness: float = 2.0  # wake at most 2 x the deck brightness
    bow_taper: float = 0.12        # pointed part of the bow, as a fraction of length
    length_err_floor_m: float = 4.0
    length_err_rel: float = 0.015
    width_err_floor_m: float = 3.0


@dataclass
class Detection:
    row: float          # hull centre, pixel coordinates (pixel centres at integers)
    col: float
    length_px: float
    width_px: float
    axis_r: float       # unit vector along the hull, pixel space
    axis_c: float
    peak_reflectance: float
    contrast: float     # peak excess over background (reflectance)
    snr: float          # contrast / local std
    npix: int
    length_m: float = 0.0
    width_m: float = 0.0
    heading_deg: float = 0.0  # hull axis, degrees from grid north, 0-180
    confidence: float = 0.0
    length_err_m: float = 0.0  # 1-sigma uncertainty
    width_err_m: float = 0.0
    method: str = "profile"    # "fit" (hull model) or "profile" (fallback)
    wake_m: float = 0.0        # visible wake behind the stern (ship under way); 0 = none found

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# masks


def _component_sizes(mask: np.ndarray, structure=None) -> tuple[np.ndarray, np.ndarray]:
    labels, n = ndimage.label(mask, structure=structure)
    sizes = np.bincount(labels.ravel(), minlength=n + 1)
    sizes[0] = 0
    return labels, sizes


def build_masks(refl: np.ndarray, scl: np.ndarray, valid: np.ndarray, aoi: np.ndarray,
                p: DetectParams) -> tuple[np.ndarray, np.ndarray]:
    """Return (search, sea, land): where to look for ship seeds, where hulls may extend,
    and land (including large clouds) that a hull must not touch."""
    cloud_raw = np.isin(scl, SCL_CLOUD) & valid
    labels, sizes = _component_sizes(cloud_raw)
    cloud = (sizes >= p.min_cloud_px)[labels]
    if p.cloud_buffer_px and cloud.any():
        cloud = ndimage.maximum_filter(cloud, size=2 * p.cloud_buffer_px + 1)

    water = valid & ((scl == SCL_WATER) |
                     ((refl < p.water_nir_max) & ~np.isin(scl, SCL_SHADOW) & (scl != 0)))
    nonwater = valid & ~water
    labels, sizes = _component_sizes(nonwater)
    border = np.unique(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]))
    hole = sizes < p.max_hole_px
    hole[border] = False
    if not p.fill_vegetation_holes:
        veg = np.bincount(labels.ravel(), weights=(scl == SCL_VEGETATION).ravel(), minlength=sizes.size)
        hole &= veg < 0.5 * np.maximum(sizes, 1)
    hole[0] = False
    sea = water | hole[labels]

    land = valid & ~sea
    if p.coast_buffer_px and land.any():
        land = ndimage.maximum_filter(land, size=2 * p.coast_buffer_px + 1)
    land_raw = (valid & ~sea) | ~valid  # hulls may not touch land, big cloud or nodata
    search = sea & valid & aoi & ~land & ~cloud
    if p.edge_buffer_px and (~valid).any():
        search &= ~ndimage.maximum_filter(~valid, size=2 * p.edge_buffer_px + 1)
    return search, sea & valid & ~cloud, land_raw


def _local_stats(img: np.ndarray, mask: np.ndarray, win: int):
    m = mask.astype(np.float32)
    x = img * m
    n = ndimage.uniform_filter(m, win)
    s1 = ndimage.uniform_filter(x, win)
    s2 = ndimage.uniform_filter(x * img, win)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = s1 / n
        var = s2 / n - mean * mean
    std = np.sqrt(np.clip(var, 0, None))
    return np.nan_to_num(mean), np.nan_to_num(std), n


def _coarse_background(refl: np.ndarray, mask: np.ndarray, block: int = 32) -> np.ndarray:
    """Robust local water level: median per 32 px block, then a 3x3 median over blocks.

    Unlike a moving mean, this is not pulled up by large bright objects (a 400 m ship
    fills whole blocks, but not most of a 3x3 neighbourhood of them).
    """
    H, W = refl.shape
    hb, wb = -(-H // block), -(-W // block)
    pad = np.full((hb * block, wb * block), np.nan, np.float32)
    pad[:H, :W] = np.where(mask, refl, np.nan)
    blocks = pad.reshape(hb, block, wb, block).transpose(0, 2, 1, 3).reshape(hb, wb, -1)
    with warnings.catch_warnings():  # all-NaN blocks (land, cloud) are expected
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(blocks, axis=2)
    fill = np.nanmedian(med) if np.isfinite(med).any() else 0.0
    med = np.where(np.isfinite(med), med, fill)
    med = ndimage.median_filter(med, size=3, mode="nearest")
    return np.repeat(np.repeat(med, block, 0), block, 1)[:H, :W]


def _candidates(refl, search, bgmask, p: DetectParams):
    mean, std, n = _local_stats(refl, bgmask, p.bg_window)
    excess = refl - mean
    snr = excess / np.maximum(std, p.std_floor)
    cand = search & (n > 0.25) & (snr > p.k_sigma) & (excess > p.min_contrast)
    return cand, excess, snr


# ---------------------------------------------------------------------------
# measurement


def _crossings(prof: np.ndarray, coords: np.ndarray, level: float) -> tuple[float, float] | None:
    above = np.nonzero(prof >= level)[0]
    if above.size == 0:
        return None
    i0, i1 = above[0], above[-1]

    def interp(a: int, b: int) -> float:
        pa, pb = prof[a], prof[b]
        if pb == pa:
            return coords[b]
        return coords[a] + (level - pa) / (pb - pa) * (coords[b] - coords[a])

    lo = interp(i0 - 1, i0) if i0 > 0 else coords[0]
    hi = interp(i1 + 1, i1) if i1 < prof.size - 1 else coords[-1]
    return lo, hi


def _plateau(prof: np.ndarray) -> float:
    """Typical level of the hull in a profile: the median of its upper part.

    Using the median rather than the peak keeps a bright superstructure from
    pushing the end-of-hull level up and clipping the bow.
    """
    core = prof[prof >= 0.25 * prof.max()]
    return float(np.median(core)) if core.size else float(prof.max())


def measure(excess: np.ndarray, mask: np.ndarray, p: DetectParams) -> dict | None:
    """Measure one hull. ``excess`` is background-subtracted reflectance around it,
    ``mask`` the grown hull pixels (same shape)."""
    rr, cc = np.nonzero(mask)
    if rr.size < p.min_pixels:
        return None
    w = np.clip(excess[rr, cc], 0, None)
    if w.sum() <= 0:
        return None
    cr, cc0 = np.average(rr, weights=w), np.average(cc, weights=w)
    cov = np.cov(np.vstack([rr - rr.mean(), cc - cc.mean()])) if rr.size > 1 else np.eye(2) * 0.1
    evals, evecs = np.linalg.eigh(cov)
    u = evecs[:, 1]  # major axis (dr, dc)
    if u[0] > 0:  # canonical direction: pointing "up" in the image
        u = -u
    v = np.array([-u[1], u[0]])

    # Restrict the signal to this hull (dilated) so neighbouring ships don't leak in.
    sig = np.where(ndimage.binary_dilation(mask, iterations=2), np.clip(excess, 0, None), 0.0)
    half_l = math.sqrt(12 * max(evals[1], 0.1)) / 2 + 4
    half_w = math.sqrt(12 * max(evals[0], 0.1)) / 2 + 3
    ds, dt = 0.25, 0.25
    s = np.arange(-half_l, half_l + ds, ds)
    t = np.arange(-half_w, half_w + dt, dt)
    S, T = np.meshgrid(s, t, indexing="ij")
    R = cr + S * u[0] + T * v[0]
    C = cc0 + S * u[1] + T * v[1]
    grid = ndimage.map_coordinates(sig, [R, C], order=1, mode="constant", cval=0.0)

    z = float(ndtri(1 - p.profile_frac))
    # Blur seen by the profile: optics + pixel box + bilinear resampling.
    sigma_eff = math.sqrt(p.psf_sigma_px ** 2 + 1 / 12 + 1 / 6)
    corr = 2 * z * sigma_eff

    along = grid.sum(axis=1)
    ends = _crossings(along, s, p.profile_frac * _plateau(along))
    if ends is None:
        return None
    s_lo, s_hi = ends
    length = max(s_hi - s_lo - corr, 1.0)
    core = _crossings(along, s, 0.7 * _plateau(along))
    if core:
        # Judge the sharper end: a moving ship's wake softens one end, but its bow stays
        # sharp; a cloud is soft all round.
        span = max(core[1] - core[0], 0.5)
        edges = (max(core[0] - s_lo, 0.0), max(s_hi - core[1], 0.0))
        softness = (span + 2 * min(edges)) / span
    else:
        softness, edges = 9.0, (0.0, 0.0)

    inside = (S >= s_lo) & (S <= s_hi)
    across = np.where(inside, grid, 0).sum(axis=0)
    sides = _crossings(across, t, p.profile_frac * _plateau(across))
    t_lo, t_hi = sides if sides else (-0.5, 0.5)
    width = max(t_hi - t_lo - corr, 0.5)

    s_mid, t_mid = (s_lo + s_hi) / 2, (t_lo + t_hi) / 2
    return {
        "row": cr + s_mid * u[0] + t_mid * v[0],
        "col": cc0 + s_mid * u[1] + t_mid * v[1],
        "length_px": float(length),
        "width_px": float(width),
        "axis_r": float(u[0]),
        "axis_c": float(u[1]),
        "softness": float(softness),
        "end_edges_px": edges,  # 30->70 % rise at each end; a wake makes one end long
    }


def _hull_model(params, rr, cc, taper: float, sign: int, sigma: float) -> np.ndarray:
    """Blurred hull: a rectangle with a pointed bow (at +s if sign=1), convolved with a
    Gaussian of width ``sigma`` px. Separable in the hull frame, so it is evaluated
    analytically at pixel centres (no supersampling).

    With 9 parameters a wake is added behind the stern: foam that starts at the stern
    with brightness ``fa`` x the deck, fades exponentially over ``fl`` x the hull length,
    and has its own width ``ww``. Expressing the wake relative to the hull keeps it
    physical: a short, very bright blob is the superstructure, not a wake.
    """
    r0, c0, th, L, W, A = params[:6]
    ur, uc = -math.cos(th), math.sin(th)  # along hull; th = axis angle from north
    s = (rr - r0) * ur + (cc - c0) * uc
    t = (rr - r0) * uc - (cc - c0) * ur
    bow0 = L / 2 - taper * L
    sb = sign * s
    w = np.where(sb > bow0, W * np.clip(1 - (sb - bow0) / max(taper * L, 1e-6), 0.05, 1), W)
    gs = ndtr((s + L / 2) / sigma) - ndtr((s - L / 2) / sigma)
    gt = ndtr((t + w / 2) / sigma) - ndtr((t - w / 2) / sigma)
    out = A * gs * gt
    if len(params) > 6:
        fa, fl, ww = params[6:]
        aw, lam = fa * A, fl * L
        d = -sb - L / 2  # distance behind the stern (px)
        onset = ndtr(d / sigma)
        fade = np.exp(-np.clip(d, 0, None) / max(lam, 1e-3))
        out = out + aw * onset * fade * (ndtr((t + ww / 2) / sigma) - ndtr((t - ww / 2) / sigma))
    return out


def fit_hull(excess: np.ndarray, mask: np.ndarray, init: dict, p: DetectParams) -> dict | None:
    """Refine length, beam, centre and axis by fitting the blurred hull model.

    A moving ship trails a bright wake that would otherwise be measured as hull (a
    150 m tanker reading 300 m+). When a hull-plus-wake model explains the pixels
    clearly better, the wake is separated out and only the hull is measured.

    Returns None (keep the profile estimate) when the fit fails or runs into its bounds.
    """
    region = ndimage.binary_dilation(mask, iterations=4)
    around = region & ~ndimage.binary_dilation(mask, iterations=2)
    vals = excess[around]
    noise = float(1.4826 * np.median(np.abs(vals - np.median(vals)))) if vals.size > 10 else 0.003
    rr, cc = np.nonzero(region)
    y = excess[rr, cc].astype(np.float64)
    sigma = math.sqrt(p.psf_sigma_px ** 2 + 1 / 12)  # optics + pixel footprint
    th0 = math.atan2(init["axis_c"], -init["axis_r"])
    ur, uc = -math.cos(th0), math.sin(th0)
    L0, W0 = init["length_px"], max(init["width_px"], 1.0)
    inside = mask[rr, cc]
    A0 = max(float(np.percentile(y[inside], 60)) if inside.any() else float(y.max()), 0.01)
    lb6 = np.array([init["row"] - 3, init["col"] - 3, th0 - 0.35, 1.5, 0.3, 0.003])
    ub6 = np.array([init["row"] + 3, init["col"] + 3, th0 + 0.35, L0 * 1.6 + 4, max(W0 * 1.6, 3), 1.0])
    f_scale = max(2 * noise, 0.004)

    def solve(fun, x0, lb, ub):
        try:
            return least_squares(fun, np.clip(x0, lb + 1e-9, ub - 1e-9), bounds=(lb, ub), loss="soft_l1",
                                 f_scale=f_scale, x_scale="jac")
        except (ValueError, np.linalg.LinAlgError):
            return None

    x0 = np.array([init["row"], init["col"], th0, L0 * 1.03, max(W0 * 0.7, 0.6), A0])
    best = None
    for sign in (1, -1):  # bow at either end
        res = solve(lambda q, sign=sign: _hull_model(q, rr, cc, p.bow_taper, sign, sigma) - y, x0, lb6, ub6)
        if res is not None and res.success and (best is None or res.cost < best.cost):
            best = res
    if best is None:
        return None

    wake = None
    soft_end = max(init.get("end_edges_px", (0.0, 0.0)))
    if p.wake_model and L0 >= 6 and soft_end >= p.wake_soft_end_px:
        # Wake behind the stern. The first estimate may include the wake, so also start
        # from shorter hulls shifted towards the bow.
        lbw = np.concatenate([lb6, [0.0, p.wake_min_length, 0.3]])
        ubw = np.concatenate([ub6, [p.wake_max_brightness, 6.0, max(3 * W0, 4)]])
        lbw[0] -= L0 / 2
        lbw[1] -= L0 / 2
        ubw[0] += L0 / 2
        ubw[1] += L0 / 2
        # The wake trails from the end that fades out; the bow is the other end.
        # (Profile and model share the hull axis direction: +s is the "hi" end.)
        e_lo, e_hi = init["end_edges_px"]
        for sign in ((-1,) if e_hi > e_lo else (1,)):
            for frac in (1.0, 0.6, 0.35):
                Ls = L0 * frac
                shift = sign * (L0 - Ls) / 2
                xw = np.array([init["row"] + ur * shift, init["col"] + uc * shift, th0, Ls, max(W0 * 0.7, 0.6),
                               A0, 0.5, max(1.0, p.wake_min_length * 1.5), W0])
                res = solve(lambda q, sign=sign: _hull_model(q, rr, cc, p.bow_taper, sign, sigma) - y, xw, lbw, ubw)
                if res is not None and res.success and (wake is None or res.cost < wake[0].cost):
                    wake = (res, sign)
        # Keep the wake only if it explains the pixels clearly better than a plain hull
        # and is a real tail (not a sliver of the hull itself).
        if wake is not None:
            res = wake[0]
            fa = res.x[6]
            plausible = res.x[3] >= 0.35 * best.x[3]  # a wake inflates a hull by ~1/3, not 5x
            if res.cost < (1 - p.wake_min_gain) * best.cost and fa > 0.15 and plausible:
                best = res
            else:
                wake = None

    r0, c0, th, L, W, A = best.x[:6]
    ub = ubw if wake is not None else ub6
    if L >= ub[3] * 0.99 or W >= ub[4] * 0.99 or L < W:
        return None
    n_par = len(best.x)
    dof = max(len(best.fun) - n_par, 1)
    try:
        cov = np.linalg.pinv(best.jac.T @ best.jac) * float(np.sum(best.fun ** 2) / dof)
        sl, sw = math.sqrt(max(cov[3, 3], 0.0)), math.sqrt(max(cov[4, 4], 0.0))
    except np.linalg.LinAlgError:
        sl = sw = 0.5
    ar, ac = -math.cos(th), math.sin(th)
    if ar > 0:  # same canonical direction as the profile estimate
        ar, ac = -ar, -ac
    out = {"row": r0, "col": c0, "length_px": L, "width_px": W, "axis_r": ar, "axis_c": ac,
           "sigma_length_px": sl, "sigma_width_px": sw, "wake_px": 0.0}
    if wake is not None:
        out["wake_px"] = float(3 * best.x[7] * L)  # visible wake length (~95 % faded)
    return out


def _confidence(snr: float, aspect: float, length_m: float) -> float:
    c = 0.4 * min(snr / 15, 1) + 0.3 * min(max(aspect - 1, 0) / 4, 1) + 0.3 * min(length_m / 100, 1)
    return round(float(np.clip(c, 0, 1)), 3)


def _ring(shape, r: float, c: float, inner: float, outer: float):
    """Slices and boolean ring mask (inner <= distance <= outer) around (r, c)."""
    H, W = shape
    r0, r1 = max(int(r - outer), 0), min(int(r + outer) + 1, H)
    c0, c1 = max(int(c - outer), 0), min(int(c + outer) + 1, W)
    yy, xx = np.mgrid[r0:r1, c0:c1]
    d = np.hypot(yy - r, xx - c)
    return (slice(r0, r1), slice(c0, c1)), (d >= inner) & (d <= outer)


def detect_in_array(refl: np.ndarray, scl: np.ndarray, valid: np.ndarray, aoi: np.ndarray,
                    p: DetectParams, pixel_m: float = 10.0, rejected: Counter | None = None) -> list[Detection]:
    """Detect and measure ships in one in-memory block.

    refl: NIR (B08) reflectance; scl: SCL classes resampled to the same grid;
    valid: data mask; aoi: True inside the area of interest.
    ``rejected`` (optional) counts candidates dropped, by reason.
    """
    rejected = rejected if rejected is not None else Counter()
    search, sea, land = build_masks(refl, scl, valid, aoi, p)
    if not search.any():
        return []

    water_ok = search & (refl < _coarse_background(refl, search) + p.min_contrast)
    cand, _, _ = _candidates(refl, search, water_ok, p)
    bgmask = water_ok & ~ndimage.maximum_filter(cand, size=7) if cand.any() else water_ok
    cand, excess, snr = _candidates(refl, search, bgmask, p)
    if not cand.any():
        return []

    labels, n = ndimage.label(cand, structure=np.ones((3, 3)))
    peaks = ndimage.maximum(excess, labels, index=np.arange(1, n + 1))
    claimed = np.zeros(refl.shape, bool)
    out: list[Detection] = []
    # Window around each seed: the seed may be only the brightest part of the hull
    # (e.g. the superstructure at one end), so allow a full hull length either side.
    pad = int(p.max_length_m / pixel_m) + 6
    slices = ndimage.find_objects(labels)
    H, W = refl.shape

    for idx in np.argsort(-np.asarray(peaks)):
        lab = idx + 1
        sl = slices[idx]
        if sl is None:
            continue
        r0, r1 = max(sl[0].start - pad, 0), min(sl[0].stop + pad, H)
        c0, c1 = max(sl[1].start - pad, 0), min(sl[1].stop + pad, W)
        seed = labels[r0:r1, c0:c1] == lab
        if (claimed[r0:r1, c0:c1] & seed).any():
            continue
        ex = excess[r0:r1, c0:c1]
        peak = float(peaks[idx])
        grow = (ex > max(p.grow_frac * peak, p.min_contrast / 2)) & sea[r0:r1, c0:c1]
        glab, _ = ndimage.label(grow | seed, structure=np.ones((3, 3)))
        hull = np.isin(glab, np.unique(glab[seed]))
        hull &= ~claimed[r0:r1, c0:c1]
        claimed[r0:r1, c0:c1] |= hull

        m = measure(ex, hull, p)
        if m is None:
            continue
        method = "profile"
        f = fit_hull(ex, hull, m, p) if p.model_fit and hull.sum() >= p.min_pixels else None
        if f is not None:
            m.update(f)
            method = "fit"
        length_m, width_m = m["length_px"] * pixel_m, m["width_px"] * pixel_m
        if method == "fit":
            length_err = math.hypot(m["sigma_length_px"] * pixel_m, p.length_err_floor_m, p.length_err_rel * length_m)
            width_err = math.hypot(m["sigma_width_px"] * pixel_m, p.width_err_floor_m)
        else:  # the profile method's own bias (bows short, beams wide) is folded in
            length_err = math.hypot(1.5 * p.length_err_floor_m, 0.05 * length_m)
            width_err = math.hypot(2 * p.width_err_floor_m, 0.3 * width_m)
        # Size limits respect the uncertainty: drop only what is confidently outside them.
        if (length_m + length_err < p.min_length_m or length_m - length_err > p.max_length_m
                or width_m - width_err > p.max_width_m):
            rejected["size"] += 1
            continue
        aspect = length_m / max(width_m, 1e-6)
        # No real ship of 150 m+ is more than ~9x as long as it is wide (supertankers ~5.5,
        # container ships ~7). Longer and thinner means wake or streak still counted as
        # hull: say so through a wider uncertainty and lower confidence.
        implausible = length_m >= 150 and aspect > p.max_plausible_aspect
        if implausible:
            length_err = max(length_err, length_m - 7 * width_m)
        if length_m >= p.aspect_from_m and aspect < p.min_aspect:
            rejected["shape"] += 1
            continue
        # Hulls are surrounded by water: an object running into land/cloud is an edge.
        if m["length_px"] >= p.edge_test_from_px and m["softness"] > p.max_edge_softness:
            rejected["soft edges (cloud)"] += 1
            continue
        if (ndimage.binary_dilation(hull, iterations=2) & land[r0:r1, c0:c1]).any():
            rejected["touches land/cloud"] += 1
            continue
        # A ship sits in clean water; clouds come in fields with more cloud, haze and
        # shadows around them. Look at a ring beyond the hull ends.
        cr, cc_ = m["row"] + r0, m["col"] + c0
        inner = m["length_px"] / 2 + p.ring_gap_px
        sl2, ring = _ring(refl.shape, cr, cc_, inner, inner + p.ring_width_px)
        # Only sea and cloud count: bright land near the coast is not "clutter".
        ring &= valid[sl2] & ~(land[sl2] & ~np.isin(scl[sl2], SCL_CLOUDY))
        if ring.sum() > 50:
            cloud_frac = float(np.isin(scl[sl2], SCL_CLOUDY)[ring].mean())
            clutter = float(((excess[sl2] > p.min_contrast) & ~claimed[sl2])[ring].mean())
            if cloud_frac > p.max_ring_cloud:
                rejected["cloud nearby"] += 1
                continue
            if clutter > p.max_ring_clutter:
                rejected["cluttered (cloud/glint)"] += 1
                continue
        rr, cc = np.nonzero(hull)
        s = float(snr[r0:r1, c0:c1][hull].max())
        heading = math.degrees(math.atan2(m["axis_c"], -m["axis_r"])) % 180.0
        out.append(Detection(
            row=m["row"] + r0, col=m["col"] + c0,
            length_px=m["length_px"], width_px=m["width_px"],
            axis_r=m["axis_r"], axis_c=m["axis_c"],
            peak_reflectance=float(refl[r0:r1, c0:c1][hull].max()),
            contrast=peak, snr=s, npix=int(rr.size),
            length_m=round(length_m, 1), width_m=round(width_m, 1),
            heading_deg=round(heading, 1),
            confidence=round(_confidence(s, aspect, length_m) * (0.6 if implausible else 1.0), 3),
            length_err_m=round(length_err, 1), width_err_m=round(width_err, 1), method=method,
            wake_m=round(m.get("wake_px", 0.0) * pixel_m, 0),
        ))
    return out
