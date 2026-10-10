"""Get at Sentinel-2 band files (cloud-optimised GeoTIFFs).

Normally the big 10 m bands are *streamed*: GDAL reads just the parts of the file a
scan needs (sea inside the area, small patches around ships) with HTTP range requests.
The whole file is downloaded only with ``--keep-tiles`` or if streaming fails; such
downloads go to ``<file>.part`` and resume after an interruption.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

import os

import rasterio
import requests

from .stac import sign_href

log = logging.getLogger(__name__)

CHUNK = 1 << 20


def _https(href: str) -> str:
    if href.startswith("s3://"):
        bucket, _, key = href[5:].partition("/")
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    return href


def download_file(href: str, dest: Path, source_cfg: dict, retries: int = 5) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    session = requests.Session()
    session.headers["User-Agent"] = "ship-tracker/2.0"

    for attempt in range(retries):
        try:
            url = sign_href(_https(href), source_cfg)
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            with session.get(url, stream=True, timeout=(30, 120), headers=headers) as r:
                if r.status_code == 416:  # already complete
                    break
                r.raise_for_status()
                mode = "ab" if have and r.status_code == 206 else "wb"
                total = int(r.headers.get("Content-Length", 0)) + (have if mode == "ab" else 0)
                done = have if mode == "ab" else 0
                last_log = time.time()
                with open(part, mode) as fh:
                    for chunk in r.iter_content(CHUNK):
                        fh.write(chunk)
                        done += len(chunk)
                        if time.time() - last_log > 10:
                            pct = f"{100 * done / total:.0f}%" if total else f"{done >> 20} MB"
                            log.info("  %s: %s", dest.name, pct)
                            last_log = time.time()
            break
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
            if attempt == retries - 1:
                raise
            wait = 2 ** (attempt + 1)
            log.warning("Download of %s failed (%s); retrying in %ss", dest.name, exc, wait)
            time.sleep(wait)

    shutil.move(part, dest)
    log.info("  downloaded %s (%.1f MB)", dest.name, dest.stat().st_size / 1e6)
    return dest


def remote_href(href: str, source_cfg: dict) -> str:
    """URL GDAL can read the file from directly (signed where the source needs it)."""
    return sign_href(_https(href), source_cfg)


def gdal_options() -> dict:
    """GDAL settings for reading COGs over HTTP quickly and robustly."""
    opts = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",   # don't list the bucket "directory"
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF,.tiff",
        "GDAL_HTTP_MULTIRANGE": "YES",                 # fetch a window's blocks in parallel
        "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
        "GDAL_HTTP_MULTIPLEX": "YES",
        "GDAL_HTTP_VERSION": "2",
        "GDAL_HTTP_MAX_RETRY": "6",
        "GDAL_HTTP_RETRY_DELAY": "2",
        "GDAL_HTTP_TIMEOUT": "120",
        "GDAL_HTTP_USERAGENT": "ship-tracker/2.0",
        "VSI_CACHE": "TRUE",
        "VSI_CACHE_SIZE": str(64 << 20),
        # Decoded blocks kept in memory, enough for a whole tile (~250 MB), so the overlap
        # between neighbouring processing blocks is never fetched twice.
        "GDAL_CACHEMAX": 1 << 30,
    }
    if not any(os.environ.get(k) for k in ("CURL_CA_BUNDLE", "SSL_CERT_FILE")):
        try:  # same certificates requests uses; GDAL's own may be missing on Windows
            import certifi
            opts["CURL_CA_BUNDLE"] = certifi.where()
        except ImportError:
            pass
    return opts


def gdal_env() -> rasterio.Env:
    return rasterio.Env(**gdal_options())


def scene_dir(tiles_dir: Path, scene_id: str) -> Path:
    return tiles_dir / scene_id


def remove_scene(tiles_dir: Path, scene_id: str) -> None:
    shutil.rmtree(scene_dir(tiles_dir, scene_id), ignore_errors=True)
