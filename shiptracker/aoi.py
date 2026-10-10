"""The areas ships are tracked in (config/areas.geojson). Each is scanned on demand."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


@dataclass(frozen=True)
class Area:
    id: str
    name: str
    group: str
    geom: BaseGeometry


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> tuple[Area, ...]:
    data = json.loads(Path(path).read_text())
    feats = data.get("features", [data] if data.get("type") == "Feature" else [])
    out = []
    for i, f in enumerate(feats):
        props = f.get("properties", {})
        g = shape(f["geometry"])
        out.append(Area(str(props.get("id") or f"area-{i + 1}"), props.get("name") or f"Area {i + 1}",
                        props.get("group", ""), g if g.is_valid else g.buffer(0)))
    return tuple(out)


def load_areas(path: Path) -> tuple[Area, ...]:
    """All areas; re-read automatically when the file is edited."""
    p = Path(path)
    return _load(str(p), p.stat().st_mtime)


def get_area(path: Path, area_id: str) -> Area:
    for a in load_areas(path):
        if a.id == area_id:
            return a
    raise KeyError(f"unknown area {area_id!r}; choose from {', '.join(a.id for a in load_areas(path))}")


def all_areas(path: Path) -> BaseGeometry:
    return unary_union([a.geom for a in load_areas(path)])
