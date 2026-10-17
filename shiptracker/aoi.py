"""The areas ships are tracked in. Each is scanned on demand.

Built-in areas come from config/areas.geojson; areas you draw on the map are kept in
data/custom_areas.geojson (with your results, so a git pull never touches them).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry


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


CUSTOM_GROUP = "My areas"


def load_areas(path: Path) -> tuple[Area, ...]:
    """All areas in a file; re-read automatically when the file is edited."""
    p = Path(path)
    if not p.exists():
        return ()
    return _load(str(p), p.stat().st_mtime)


def save_areas(path: Path, areas) -> None:
    from shapely.geometry import mapping

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    feats = [{"type": "Feature", "properties": {"id": a.id, "name": a.name, "group": a.group},
              "geometry": mapping(a.geom)} for a in areas]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, indent=1))
    tmp.replace(p)


def new_area_id(name: str, taken) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "area"
    base, n = f"my-{slug}", 2
    out = base
    while out in taken:
        out, n = f"{base}-{n}", n + 1
    return out
