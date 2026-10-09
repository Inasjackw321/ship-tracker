"""Image chips of each detection with a ruler drawn along the measured hull."""
from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFont
from rasterio.windows import Window

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
    img = Image.fromarray(np.moveaxis(mode_img, 0, -1), "RGB")
    scale = OUT_PX / img.width
    img = img.resize((OUT_PX, OUT_PX), Image.LANCZOS)
    draw = ImageDraw.Draw(img)

    cx, cy = (col - c0) * scale, (row - r0) * scale
    ax, ay = d.axis_c, d.axis_r                     # along hull (image x, y)
    px, py = -ay, ax                                # across hull
    off = (d.width_px / 2 + 3) * scale
    hl = d.length_px / 2 * scale
    x0, y0 = cx - ax * hl + px * off, cy - ay * hl + py * off
    x1, y1 = cx + ax * hl + px * off, cy + ay * hl + py * off
    shadow, white = (0, 0, 0), (255, 255, 255)
    draw.line([(x0, y0), (x1, y1)], fill=shadow, width=5)
    draw.line([(x0, y0), (x1, y1)], fill=white, width=2)
    for i in range(1, 10):  # ruler ticks
        f = i / 10
        tx, ty = x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
        draw.line([(tx, ty), (tx - px * 5, ty - py * 5)], fill=white, width=1)
    for (ex, ey) in ((x0, y0), (x1, y1)):
        draw.ellipse([ex - 5, ey - 5, ex + 5, ey + 5], fill=white, outline=shadow, width=2)

    font = _font(16)
    text = f"{d.length_m:.0f} m"
    lx, ly = x1 + px * 14, y1 + py * 14
    tb = draw.textbbox((lx, ly), text, font=font, anchor="mm")
    draw.rectangle([tb[0] - 3, tb[1] - 2, tb[2] + 3, tb[3] + 2], fill=(0, 0, 0))
    draw.text((lx, ly), text, fill=white, font=font, anchor="mm")

    caption = f"L {d.length_m:.0f} m  W {d.width_m:.0f} m  axis {d.heading_deg:.0f}°  {label}".strip()
    small = _font(13)
    draw.rectangle([0, OUT_PX - 22, OUT_PX, OUT_PX], fill=(0, 0, 0))
    draw.text((6, OUT_PX - 11), caption, fill=white, font=small, anchor="lm")
    # 100 m scale bar
    bar = 100 / abs(src.res[0]) * scale
    draw.line([(OUT_PX - 10 - bar, 12), (OUT_PX - 10, 12)], fill=white, width=3)
    draw.text((OUT_PX - 10 - bar / 2, 24), "100 m", fill=white, font=small, anchor="mm")

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
