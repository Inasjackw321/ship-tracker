"""Area of interest: the sea region ships are searched for in."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


@lru_cache(maxsize=4)
def load_aoi(path: Path) -> BaseGeometry:
    data = json.loads(Path(path).read_text())
    if data.get("type") == "FeatureCollection":
        geoms = [shape(f["geometry"]) for f in data["features"]]
    elif data.get("type") == "Feature":
        geoms = [shape(data["geometry"])]
    else:
        geoms = [shape(data)]
    geom = unary_union(geoms)
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def load_priority(path: Path) -> list[tuple[str, BaseGeometry]]:
    """Priority regions, scanned first, in their ``order`` property (then file order)."""
    path = Path(path)
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    feats = data.get("features", [data] if data.get("type") == "Feature" else [])
    feats = sorted(enumerate(feats), key=lambda t: (t[1].get("properties", {}).get("order", 1e9), t[0]))
    out = []
    for i, f in feats:
        g = shape(f["geometry"])
        out.append((f.get("properties", {}).get("name") or f"region {i + 1}", g if g.is_valid else g.buffer(0)))
    return out


def search_area(aoi_path: Path, priority_path: Path | None = None) -> BaseGeometry:
    """The AOI plus the priority regions, so a priority region is always fully scanned
    even where it reaches beyond the AOI outline."""
    geom = load_aoi(aoi_path)
    if priority_path is not None:
        regions = [g for _, g in load_priority(priority_path)]
        if regions:
            geom = unary_union([geom, *regions])
    return geom if geom.is_valid else geom.buffer(0)


def aoi_geojson(path: Path, priority_path: Path | None = None) -> dict:
    return mapping(search_area(path, priority_path))
