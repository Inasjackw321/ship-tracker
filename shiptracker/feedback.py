"""Learn from the detections you mark as "not a ship" (or "ship").

Each detection keeps a short fingerprint: size, shape, edge softness, brightness and
its colour in the true-colour image. On later scans a new detection is rejected when
it is very close to something you marked as not a ship, unless it is even closer to
something you confirmed as a ship. Clouds in your areas look alike, so a few marks go
a long way, and the filter adapts to real imagery rather than assumptions.
"""
from __future__ import annotations

import json
import math

import numpy as np

# (name, value from a fingerprint dict, scale: a difference of one "scale" is a clear one)
FEATURES = (
    ("length", lambda f: math.log(max(f["length_m"], 1.0)), 0.2),
    ("beam", lambda f: math.log(max(f["width_m"], 1.0)), 0.25),
    ("shape", lambda f: math.log(max(f["length_m"], 1.0) / max(f["width_m"], 1.0)), 0.2),
    ("brightness", lambda f: math.log(max(f["contrast"], 1e-3)), 0.35),
    ("edges", lambda f: f["softness"], 0.07),
    ("blue", lambda f: f["blue"], 0.03),
    ("whiteness", lambda f: f["flatness"], 0.12),
)
SIMILAR = 1.0  # average distance (in scales) below which two fingerprints look alike


def fingerprint(d) -> dict:
    """Fingerprint of a Detection (or of a dict with the same fields)."""
    get = (lambda k: d.get(k)) if isinstance(d, dict) else (lambda k: getattr(d, k, None))
    return {k: get(k) for k in ("length_m", "width_m", "contrast", "softness", "blue", "flatness", "ndvi")}


def _vector(f: dict) -> np.ndarray:
    out = []
    for _, fn, scale in FEATURES:
        try:
            v = fn(f)
            out.append(np.nan if v is None else v / scale)
        except (KeyError, TypeError, ValueError):
            out.append(np.nan)
    return np.array(out, float)


def _distance(a: np.ndarray, b: np.ndarray) -> float:
    ok = ~(np.isnan(a) | np.isnan(b))
    return float(np.sqrt(np.mean((a[ok] - b[ok]) ** 2))) if ok.sum() >= 4 else math.inf


class Feedback:
    def __init__(self, examples: list[tuple[dict, str]]):
        self.bad = [_vector(f) for f, flag in examples if flag == "not_ship"]
        self.good = [_vector(f) for f, flag in examples if flag == "ship"]

    def __bool__(self) -> bool:
        return bool(self.bad)

    def looks_like_marked_not_ship(self, fp: dict) -> bool:
        if not self.bad:
            return False
        v = _vector(fp)
        bad = min(_distance(v, b) for b in self.bad)
        good = min((_distance(v, g) for g in self.good), default=math.inf)
        return bad <= SIMILAR and bad < good

    @classmethod
    def from_store(cls, store) -> "Feedback":
        rows = store._query("SELECT features_json, flag FROM feedback")
        return cls([(json.loads(r["features_json"]), r["flag"]) for r in rows])
