"""Find Sentinel-2 L2A scenes over the AOI through a STAC API."""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass

import requests
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)


@dataclass
class Scene:
    id: str
    datetime: str
    cloud_cover: float | None
    tile: str | None
    epsg: int | None
    nir_href: str
    scl_href: str
    rgb_href: str | None
    nir_scale: float
    nir_offset: float
    aoi_overlap: float
    geometry: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "ship-tracker/2.0"
    return s


def _request(session: requests.Session, method: str, url: str, **kw) -> dict:
    for attempt in range(5):
        try:
            r = session.request(method, url, timeout=60, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} from {url}")
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            if attempt == 4:
                raise
            wait = 2 ** (attempt + 1)
            log.warning("STAC request failed (%s); retrying in %ss", exc, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _nir_scaling(item: dict, nir_key: str) -> tuple[float, float]:
    """Return (scale, offset) that turns NIR digital numbers into reflectance."""
    bands = item["assets"][nir_key].get("raster:bands") or []
    if bands and "scale" in bands[0]:
        return float(bands[0]["scale"]), float(bands[0].get("offset", 0.0))
    # Processing baseline 04.00+ (Jan 2022 onward) stores DN with a +1000 offset.
    baseline = str(item["properties"].get("s2:processing_baseline", "00.00"))
    try:
        newer = float(baseline) >= 4.0
    except ValueError:
        newer = False
    return 1e-4, (-0.1 if newer else 0.0)


def item_to_scene(item: dict, assets: dict, aoi: BaseGeometry) -> Scene | None:
    a = item.get("assets", {})
    if assets["nir"] not in a or assets["scl"] not in a:
        log.debug("Skipping %s: missing NIR/SCL assets", item.get("id"))
        return None
    props = item["properties"]
    geom = shape(item["geometry"])
    overlap = geom.intersection(aoi).area / geom.area if geom.area else 0.0
    scale, offset = _nir_scaling(item, assets["nir"])
    tile = props.get("s2:mgrs_tile") or props.get("grid:code") or (
        f"{props.get('mgrs:utm_zone', '')}{props.get('mgrs:latitude_band', '')}{props.get('mgrs:grid_square', '')}" or None
    )
    return Scene(
        id=item["id"],
        datetime=props["datetime"],
        cloud_cover=props.get("eo:cloud_cover"),
        tile=tile,
        epsg=props.get("proj:epsg") or props.get("proj:code"),
        nir_href=a[assets["nir"]]["href"],
        scl_href=a[assets["scl"]]["href"],
        rgb_href=a[assets["rgb"]]["href"] if assets.get("rgb") in a else None,
        nir_scale=scale,
        nir_offset=offset,
        aoi_overlap=round(overlap, 4),
        geometry=mapping(geom),
    )


def search_scenes(
    source_cfg: dict,
    aoi: BaseGeometry,
    start: str,
    end: str,
    max_cloud: float = 30.0,
    limit: int | None = None,
) -> list[Scene]:
    """Return scenes intersecting the AOI between ``start`` and ``end`` (ISO dates).

    Results are sorted newest first.
    """
    session = _session()
    url = source_cfg["stac_url"].rstrip("/") + "/search"
    body = {
        "collections": [source_cfg["collection"]],
        "intersects": mapping(aoi.simplify(0.01)),
        "datetime": f"{_iso(start)}/{_iso(end, end_of_day=True)}",
        "limit": 100,
        "query": {"eo:cloud_cover": {"lte": max_cloud}},
    }
    scenes: list[Scene] = []
    seen: set[str] = set()
    method, next_url, next_body = "POST", url, body
    while next_url:
        page = _request(session, method, next_url, json=next_body) if method == "POST" else _request(session, "GET", next_url)
        for item in page.get("features", []):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            scene = item_to_scene(item, source_cfg["assets"], aoi)
            if scene and scene.aoi_overlap > 0:
                scenes.append(scene)
        log.info("STAC: %d scenes so far", len(scenes))
        if limit and len(scenes) >= limit:
            break
        method, next_url, next_body = _next_link(page, next_body)

    scenes.sort(key=lambda s: s.datetime, reverse=True)
    return scenes[:limit] if limit else scenes


def search_items(stac_url: str, body: dict, max_items: int | None = None) -> list[dict]:
    """POST a STAC search and follow pagination; returns raw items (de-duplicated)."""
    session = _session()
    url = stac_url.rstrip("/") + "/search"
    items: list[dict] = []
    seen: set[str] = set()
    method, next_url, next_body = "POST", url, body
    while next_url:
        page = _request(session, method, next_url, json=next_body) if method == "POST" else _request(session, "GET", next_url)
        for item in page.get("features", []):
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
        if max_items and len(items) >= max_items:
            break
        method, next_url, next_body = _next_link(page, next_body)
    return items


def _next_link(page: dict, prev_body: dict | None) -> tuple[str, str | None, dict | None]:
    for link in page.get("links", []):
        if link.get("rel") != "next":
            continue
        if link.get("method", "GET").upper() == "POST":
            body = link.get("body") or {}
            if link.get("merge") and prev_body:
                body = {**prev_body, **body}
            return "POST", link["href"], body
        return "GET", link["href"], None
    return "GET", None, None


def _iso(date: str, end_of_day: bool = False) -> str:
    if "T" in date:
        return date
    return f"{date}T23:59:59Z" if end_of_day else f"{date}T00:00:00Z"


_sas_cache: dict[str, tuple[str, float]] = {}


def sign_href(href: str, source_cfg: dict) -> str:
    """Append a Planetary Computer SAS token when needed; other sources pass through."""
    sas_url = source_cfg.get("sas_url")
    if not sas_url or "blob.core.windows.net" not in href:
        return href
    collection = source_cfg["collection"]
    token, expiry = _sas_cache.get(collection, ("", 0.0))
    if time.time() > expiry - 300:
        data = _request(_session(), "GET", sas_url.format(collection=collection))
        token = data["token"]
        _sas_cache[collection] = (token, time.time() + 45 * 60)
    return f"{href}{'&' if '?' in href else '?'}{token}"
