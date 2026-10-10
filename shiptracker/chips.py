"""Image chips of each detection with a ruler drawn along the measured hull."""
from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.windows import Window, transform as window_transform

from .scene import GeoDetection, apply_affine

log = logging.getLogger(__name__)

OUT_PX = 360


def _stretch(arr: np.ndarray) -> np.ndarray:
    """Per-chip contrast stretch to 8 bit (arr: bands x H x W)."""
    valid = arr[arr > 0]
    if valid.size == 0:
        return np.zeros(arr.shape, np.uint8)
    lo, hi = np.percentile(valid, [1, 99.7])
    hi = max(hi, lo + 1e-6)
    return (np.clip((arr - lo) / (hi - lo), 0, 1) ** 0.8 * 255).astype(np.uint8)


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def write_geotiff(path: Path, rgb: np.ndarray, crs, transform, g: GeoDetection) -> Path:
    """Clean (no overlay) georeferenced chip: opens in place in QGIS / Google Earth."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", width=rgb.shape[2], height=rgb.shape[1], count=3,
                       dtype="uint8", crs=crs, transform=transform, compress="deflate",
                       photometric="RGB") as dst:
        dst.write(rgb)
        dst.update_tags(SHIP_LAT=f"{g.lat:.6f}", SHIP_LON=f"{g.lon:.6f}", LENGTH_M=str(g.det.length_m),
                        BEAM_M=str(g.det.width_m), HULL_AXIS_DEG=str(g.det.heading_deg))
    return path


def draw_ruler(img: Image.Image, cx_px: float, cy_px: float, ax: float, ay: float, length_px: float,
               width_px: float, label: str, caption: str, pixel_m: float, color=(255, 255, 255)) -> Image.Image:
    """Upscale a chip to OUT_PX and draw the ruler along the hull, its label, a caption and
    a 100 m scale bar. (cx_px, cy_px): hull centre in chip pixels; (ax, ay): unit vector
    along the hull in image x/y."""
    scale = OUT_PX / img.width
    img = img.resize((OUT_PX, round(img.height * scale)), Image.LANCZOS)
    draw = ImageDraw.Draw(img)
    cx, cy = cx_px * scale, cy_px * scale
    px, py = -ay, ax                                # across hull
    off = (width_px / 2 + 3) * scale
    hl = length_px / 2 * scale
    x0, y0 = cx - ax * hl + px * off, cy - ay * hl + py * off
    x1, y1 = cx + ax * hl + px * off, cy + ay * hl + py * off
    shadow, white = (0, 0, 0), (255, 255, 255)
    # The measured hull itself (length x beam) drawn on the ship, so you can see exactly
    # where the bow, stern and sides were put, joined to the ruler by extension lines.
    hw = width_px / 2 * scale
    corners = [(cx + sa * ax * hl + sb * px * hw, cy + sa * ay * hl + sb * py * hw)
               for sa, sb in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    draw.polygon(corners, outline=color, width=1)
    for sa, (ex, ey) in ((-1, (x0, y0)), (1, (x1, y1))):
        hx, hy = cx + sa * ax * hl + px * hw, cy + sa * ay * hl + py * hw
        draw.line([(hx, hy), (ex + px * 4, ey + py * 4)], fill=color, width=1)
    draw.line([(x0, y0), (x1, y1)], fill=shadow, width=5)
    draw.line([(x0, y0), (x1, y1)], fill=color, width=2)
    for i in range(1, 10):  # ruler ticks
        f = i / 10
        tx, ty = x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
        draw.line([(tx, ty), (tx - px * 5, ty - py * 5)], fill=color, width=1)
    for (ex, ey) in ((x0, y0), (x1, y1)):
        draw.ellipse([ex - 5, ey - 5, ex + 5, ey + 5], fill=color, outline=shadow, width=2)

    font = _font(16)
    lx, ly = x1 + px * 14, y1 + py * 14
    tb = draw.textbbox((lx, ly), label, font=font, anchor="mm")
    draw.rectangle([tb[0] - 3, tb[1] - 2, tb[2] + 3, tb[3] + 2], fill=(0, 0, 0))
    draw.text((lx, ly), label, fill=white, font=font, anchor="mm")

    small = _font(13)
    h = img.height
    draw.rectangle([0, h - 22, OUT_PX, h], fill=(0, 0, 0))
    draw.text((6, h - 11), caption, fill=white, font=small, anchor="lm")
    bar = 100 / pixel_m * scale  # 100 m scale bar
    draw.line([(OUT_PX - 10 - bar, 12), (OUT_PX - 10, 12)], fill=white, width=3)
    draw.text((OUT_PX - 10 - bar / 2, 24), "100 m", fill=white, font=small, anchor="mm")
    return img


def render_from_geotiff(tif: Path, rec: dict) -> Image.Image:
    """Re-draw a ship's chip from its clean GeoTIFF with the ruler at the stored hull
    ends (used after a measurement was adjusted by hand)."""
    from pyproj import Transformer

    with rasterio.open(tif) as src:
        rgb = src.read((1, 2, 3))
        to_px = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        inv = ~src.transform
        pts = []
        for lat, lon in ((rec["stern_lat"], rec["stern_lon"]), (rec["bow_lat"], rec["bow_lon"])):
            x, y = to_px.transform(lon, lat)
            pts.append(apply_affine(inv, x, y))
        pixel_m = abs(src.res[0])
    (c0, r0), (c1, r1) = pts
    length_px = math.hypot(c1 - c0, r1 - r0)
    ax, ay = ((c1 - c0) / length_px, (r1 - r0) / length_px) if length_px else (0.0, -1.0)
    img = Image.fromarray(np.moveaxis(rgb, 0, -1), "RGB")
    manual = rec.get("method") == "manual"
    err = f" ±{rec['length_err_m']:.0f}" if rec.get("length_err_m") else ""
    return draw_ruler(img, (c0 + c1) / 2, (r0 + r1) / 2, ax, ay, length_px, rec["width_m"] / pixel_m,
                      f"{rec['length_m']:.0f}{err} m",
                      f"L {rec['length_m']:.0f}{err} m  W {rec['width_m']:.0f} m  axis {rec['heading_deg']:.0f}°"
                      + ("  adjusted by hand" if manual else "") + f"  {rec['datetime'][:10]}",
                      pixel_m, color=(255, 214, 10) if manual else (255, 255, 255))


def render_chip(src, g: GeoDetection, out_path: Path, label: str = "") -> Path:
    d = g.det
    col, row = apply_affine(~src.transform, g.x, g.y)  # fractional pixel position of the centre
    half = max(32, int(math.ceil(d.length_px * 0.8)) + 8)
    c0, r0 = int(math.floor(col)) - half, int(math.floor(row)) - half
    win = Window(c0, r0, 2 * half, 2 * half)
    bands = (1, 2, 3) if src.count >= 3 else (1,)
    arr = src.read(bands, window=win, boundless=True, fill_value=0).astype(np.float32)
    img8 = _stretch(arr)
    mode_img = np.repeat(img8, 3, axis=0) if img8.shape[0] == 1 else img8
    write_geotiff(out_path.with_suffix(".tif"), mode_img, src.crs, window_transform(win, src.transform), g)
    img = Image.fromarray(np.moveaxis(mode_img, 0, -1), "RGB")
    err = f" ±{d.length_err_m:.0f}" if d.length_err_m else ""
    werr = f" ±{d.width_err_m:.0f}" if d.width_err_m else ""
    img = draw_ruler(img, col - c0, row - r0, d.axis_c, d.axis_r, d.length_px, d.width_px,
                     f"{d.length_m:.0f}{err} m",
                     f"L {d.length_m:.0f}{err} m  W {d.width_m:.0f}{werr} m  axis {d.heading_deg:.0f}°  {label}".strip(),
                     abs(src.res[0]))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, optimize=True)
    return out_path


def render_chips(image_path: Path, detections: list[GeoDetection], out_dir: Path, label: str = "") -> list[Path | None]:
    paths: list[Path | None] = []
    with rasterio.open(image_path) as src:
        for i, g in enumerate(detections):
            try:
                paths.append(render_chip(src, g, out_dir / f"{i:04d}.png", label))
            except Exception as exc:  # a broken chip must not lose the detection
                log.warning("Chip %d failed: %s", i, exc)
                paths.append(None)
    return paths
