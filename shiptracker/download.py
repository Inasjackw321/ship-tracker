"""Download Sentinel-2 band files (COGs) to the local tile cache.

Downloads are streamed to ``<file>.part`` and resumed with HTTP Range requests,
so an interrupted scan picks up where it left off.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

import requests

from .stac import sign_href

log = logging.getLogger(__name__)

CHUNK = 1 << 20


def _https(href: str) -> str:
    if href.startswith("s3://"):
        bucket, _, key = href[5:].partition("/")
        return f"https://{bucket}.s3.amazonaws.com/{key}"
    return href


def _get(session: requests.Session, url: str, headers: dict, follow_with_auth: bool):
    """GET (streamed). With auth, follow redirects by hand: requests drops the
    Authorization header when a redirect changes host, as Copernicus downloads do."""
    if not follow_with_auth:
        return session.get(url, stream=True, timeout=(30, 120), headers=headers)
    for _ in range(10):
        r = session.get(url, stream=True, timeout=(30, 120), headers=headers, allow_redirects=False)
        if r.status_code not in (301, 302, 303, 307, 308):
            return r
        url = requests.compat.urljoin(url, r.headers["Location"])
        r.close()
    raise requests.HTTPError("too many redirects")


def download_file(href: str, dest: Path, source_cfg: dict, retries: int = 5) -> Path:
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    session = requests.Session()
    session.headers["User-Agent"] = "ship-tracker/2.0"

    auth = source_cfg.get("auth")  # e.g. CdseAuth: bearer token for Copernicus Data Space
    refreshed = False
    for attempt in range(retries):
        try:
            url = sign_href(_https(href), source_cfg)
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            if auth is not None:
                headers.update(auth.headers())
            with _get(session, url, headers, follow_with_auth=auth is not None) as r:
                if r.status_code == 401 and auth is not None and not refreshed:
                    auth.headers(refresh=True)
                    refreshed = True
                    raise requests.HTTPError("401 Unauthorized (token refreshed, retrying)")
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


def scene_dir(tiles_dir: Path, scene_id: str) -> Path:
    return tiles_dir / scene_id


def remove_scene(tiles_dir: Path, scene_id: str) -> None:
    shutil.rmtree(scene_dir(tiles_dir, scene_id), ignore_errors=True)
