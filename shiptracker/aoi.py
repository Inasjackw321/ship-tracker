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


def aoi_geojson(path: Path) -> dict:
    return mapping(load_aoi(path))
