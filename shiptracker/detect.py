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
4. Size: the hull orientation comes from PCA of the grown pixels. The excess image is
   resampled onto a grid aligned with the hull; the along-axis and across-axis
   intensity profiles give length and beam at a fixed fraction of the peak, with a
   correction for the sensor's blur. This is the satellite equivalent of laying a
   ruler bow-to-stern on the image.
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy import ndimage
from scipy.special import ndtri

log = logging.getLogger(__name__)

# SCL classes: 0 nodata, 1 saturated/defective, 2 dark/topographic shadow,
# 3 cloud shadow, 4 vegetation, 5 bare, 6 water, 7 unclassified,
# 8 cloud medium prob, 9 cloud high prob, 10 thin cirrus, 11 snow.
SCL_CLOUD = (1, 3, 8, 9, 10)
SCL_WATER = 6
SCL_SHADOW = (2, 3)


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
    coast_buffer_px: int = 3
    cloud_buffer_px: int = 10
    min_cloud_px: int = 400    # SCL cloud blobs smaller than this may be ships
    max_hole_px: int = 3000    # enclosed non-water blobs smaller than this are sea
    water_nir_max: float = 0.04
    min_pixels: int = 3
    min_length_m: float = 25.0
    max_length_m: float = 500.0
    max_width_m: float = 90.0
    min_aspect: float = 2.0    # applied to objects >= aspect_from_m long
    aspect_from_m: float = 50.0


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
    """Return (search, sea): where to look for ship seeds, and where hulls may extend."""
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
    hole[0] = False
    sea = water | hole[labels]

    land = valid & ~sea
    if p.coast_buffer_px and land.any():
        land = ndimage.maximum_filter(land, size=2 * p.coast_buffer_px + 1)
    search = sea & valid & aoi & ~land & ~cloud
    return search, sea & valid & ~cloud


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
    }


def _confidence(snr: float, aspect: float, length_m: float) -> float:
    c = 0.4 * min(snr / 15, 1) + 0.3 * min(max(aspect - 1, 0) / 4, 1) + 0.3 * min(length_m / 100, 1)
    return round(float(np.clip(c, 0, 1)), 3)


def detect_in_array(refl: np.ndarray, scl: np.ndarray, valid: np.ndarray, aoi: np.ndarray,
                    p: DetectParams, pixel_m: float = 10.0) -> list[Detection]:
    """Detect and measure ships in one in-memory block.

    refl: NIR (B08) reflectance; scl: SCL classes resampled to the same grid;
    valid: data mask; aoi: True inside the area of interest.
    """
    search, sea = build_masks(refl, scl, valid, aoi, p)
    if not search.any():
        return []

    cand, _, _ = _candidates(refl, search, search, p)
    bgmask = search & ~ndimage.maximum_filter(cand, size=7) if cand.any() else search
    cand, excess, snr = _candidates(refl, search, bgmask, p)
    if not cand.any():
        return []

    labels, n = ndimage.label(cand, structure=np.ones((3, 3)))
    peaks = ndimage.maximum(excess, labels, index=np.arange(1, n + 1))
    claimed = np.zeros(refl.shape, bool)
    out: list[Detection] = []
    max_px = p.max_length_m / pixel_m
    pad = int(max_px / 2) + 6
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
        length_m, width_m = m["length_px"] * pixel_m, m["width_px"] * pixel_m
        aspect = length_m / max(width_m, 1e-6)
        if not (p.min_length_m <= length_m <= p.max_length_m) or width_m > p.max_width_m:
            continue
        if length_m >= p.aspect_from_m and aspect < p.min_aspect:
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
            confidence=_confidence(s, aspect, length_m),
        ))
    return out
