"""Central settings. Every value can be overridden with an environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# STAC endpoints for Sentinel-2 L2A. Both are free; Earth Search needs no signing,
# Planetary Computer hands out short-lived SAS tokens anonymously.
SOURCES = {
    "earth-search": {
        "stac_url": "https://earth-search.aws.element84.com/v1",
        "collection": "sentinel-2-c1-l2a",
        "assets": {"nir": "nir", "scl": "scl", "rgb": "visual"},
    },
    "planetary-computer": {
        "stac_url": "https://planetarycomputer.microsoft.com/api/stac/v1",
        "collection": "sentinel-2-l2a",
        "assets": {"nir": "B08", "scl": "SCL", "rgb": "visual"},
        "sas_url": "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}",
    },
}


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("SHIPTRACKER_DATA", str(ROOT / "data"))))
    aoi_path: Path = field(default_factory=lambda: Path(_env("SHIPTRACKER_AOI", str(ROOT / "config" / "aoi.geojson"))))
    source: str = field(default_factory=lambda: _env("SHIPTRACKER_SOURCE", "earth-search"))
    # Overrides for the selected source (handy for mirrors and tests).
    stac_url: str | None = field(default_factory=lambda: os.environ.get("SHIPTRACKER_STAC_URL"))
    collection: str | None = field(default_factory=lambda: os.environ.get("SHIPTRACKER_COLLECTION"))

    @property
    def tiles_dir(self) -> Path:
        return self.data_dir / "tiles"

    @property
    def chips_dir(self) -> Path:
        return self.data_dir / "chips"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ships.db"

    def source_config(self) -> dict:
        if self.source not in SOURCES:
            raise ValueError(f"Unknown source {self.source!r}; choose from {sorted(SOURCES)}")
        cfg = dict(SOURCES[self.source])
        if self.stac_url:
            cfg["stac_url"] = self.stac_url
        if self.collection:
            cfg["collection"] = self.collection
        return cfg

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.tiles_dir, self.chips_dir):
            d.mkdir(parents=True, exist_ok=True)
