"""Downloadable ship images with their coordinates printed on them, plus zip bundles.

Each image carries the coordinates three ways: printed in the picture, in the PNG
metadata ("Coordinates", "Latitude", "Longitude"), and in the file name. The zip bundle
adds georeferenced GeoTIFFs (where saved), a CSV and a KML for Google Earth.
"""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo

from .chips import _font

WIDTH = 560
BG = (14, 18, 24)
FG = (235, 240, 245)
MUTED = (150, 162, 175)
ACCENT = (57, 211, 83)


def dms(value: float, pos: str, neg: str) -> str:
    hemi = pos if value >= 0 else neg
    v = abs(value)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    return f"{d}°{m:02d}'{s:05.2f}\" {hemi}"


def coord_text(lat: float, lon: float) -> str:
    """Decimal degrees, the format Google Maps and most tools accept when pasted."""
    return f"{lat:.5f}, {lon:.5f}"


def coord_dms(lat: float, lon: float) -> str:
    return f"{dms(lat, 'N', 'S')}  {dms(lon, 'E', 'W')}"


def file_stem(rec: dict) -> str:
    lat, lon = rec["lat"], rec["lon"]
    ns, ew = ("N" if lat >= 0 else "S"), ("E" if lon >= 0 else "W")
    return f"ship_{abs(lat):.5f}{ns}_{abs(lon):.5f}{ew}_{rec['datetime'][:10]}_{rec['length_m']:.0f}m"


def _lines(rec: dict) -> tuple[list[tuple[str, int, tuple]], list[tuple[str, int, tuple]]]:
    when = rec["datetime"].replace("T", " ")[:16] + " UTC"
    axis = rec.get("heading_deg") or 0.0
    header = [
        (f"Ship  {rec['length_m']:.0f} m x {rec['width_m']:.0f} m   hull axis {axis:.0f}° / {(axis + 180) % 360:.0f}°", 20, FG),
        (f"{when}   Sentinel-2 (10 m)   {rec['scene_id']}", 13, MUTED),
    ]
    footer = [
        (coord_text(rec["lat"], rec["lon"]), 26, ACCENT),
        (coord_dms(rec["lat"], rec["lon"]), 16, FG),
    ]
    if rec.get("bow_lat") is not None:
        footer.append((f"Hull ends: {coord_text(rec['stern_lat'], rec['stern_lon'])}  to  "
                       f"{coord_text(rec['bow_lat'], rec['bow_lon'])}", 13, MUTED))
    footer.append((f"Confidence {rec['confidence'] * 100:.0f}%   SNR {rec['snr']}   (WGS84 lat, lon)", 13, MUTED))
    return header, footer


def annotated_png(rec: dict, chips_dir: Path) -> bytes:
    """The ship's image chip framed with its coordinates, as PNG bytes."""
    chip_path = chips_dir / rec["chip"].replace("\\", "/") if rec.get("chip") else None
    if chip_path is not None and chip_path.exists():
        chip = Image.open(chip_path).convert("RGB")
    else:
        chip = Image.new("RGB", (360, 360), (0, 0, 0))
    chip = chip.resize((WIDTH, round(chip.height * WIDTH / chip.width)), Image.LANCZOS)

    header, footer = _lines(rec)
    pad = 12
    h_head = pad + sum(size + 8 for _, size, _ in header) + pad // 2
    h_foot = pad + sum(size + 8 for _, size, _ in footer) + pad // 2
    canvas = Image.new("RGB", (WIDTH, h_head + chip.height + h_foot), BG)
    canvas.paste(chip, (0, h_head))
    draw = ImageDraw.Draw(canvas)
    y = pad
    for text, size, color in header:
        draw.text((pad, y), text, font=_font(size), fill=color)
        y += size + 8
    y = h_head + chip.height + pad
    for text, size, color in footer:
        draw.text((pad, y), text, font=_font(size), fill=color)
        y += size + 8

    info = PngInfo()
    info.add_text("Coordinates", coord_text(rec["lat"], rec["lon"]))
    info.add_text("Latitude", f"{rec['lat']:.6f}")
    info.add_text("Longitude", f"{rec['lon']:.6f}")
    info.add_text("Description", " | ".join(t for t, _, _ in header + footer))
    buf = io.BytesIO()
    canvas.save(buf, "PNG", pnginfo=info, optimize=True)
    return buf.getvalue()


def geotiff_path(rec: dict, chips_dir: Path) -> Path | None:
    if not rec.get("chip"):
        return None
    p = (chips_dir / rec["chip"].replace("\\", "/")).with_suffix(".tif")
    return p if p.exists() else None


def bundle_zip(recs: list[dict], chips_dir: Path) -> bytes:
    """Zip of annotated PNGs (+ GeoTIFFs where saved), coordinates.csv and ships.kml."""
    buf = io.BytesIO()
    rows = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rec in recs:
            stem = file_stem(rec)
            z.writestr(f"{stem}.png", annotated_png(rec, chips_dir))
            tif = geotiff_path(rec, chips_dir)
            if tif:
                z.write(tif, f"{stem}.tif")
            rows.append({
                "latitude": f"{rec['lat']:.6f}", "longitude": f"{rec['lon']:.6f}",
                "coordinates": coord_text(rec["lat"], rec["lon"]),
                "coordinates_dms": coord_dms(rec["lat"], rec["lon"]),
                "length_m": rec["length_m"], "beam_m": rec["width_m"],
                "hull_axis_deg": rec.get("heading_deg", ""),
                "datetime_utc": rec["datetime"], "image": rec["scene_id"],
                "file": f"{stem}.png",
            })
        out = io.StringIO()
        if rows:
            w = csv.DictWriter(out, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        z.writestr("coordinates.csv", out.getvalue())
        z.writestr("ships.kml", _kml(rows))
    return buf.getvalue()


def _kml(rows: list[dict]) -> str:
    marks = []
    for r in rows:
        size = f"{float(r['length_m']):.0f} m x {float(r['beam_m']):.0f} m"
        marks.append(
            f"<Placemark><name>{escape(size)}</name><description>Ship, "
            f"{escape(r['datetime_utc'])}, {escape(r['coordinates'])}</description>"
            f"<Point><coordinates>{r['longitude']},{r['latitude']},0</coordinates></Point></Placemark>")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            "<name>Ships</name>" + "".join(marks) + "</Document></kml>\n")
