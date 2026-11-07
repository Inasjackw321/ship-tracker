"""Step-by-step test of everything a scan needs from the internet.

    py run.py --check        (or: python -m shiptracker check)

Prints each step and, if one fails, exactly which one and the error, so a problem
(firewall, antivirus, proxy, a server change) can be pinned down.
"""
from __future__ import annotations

import tempfile
import time
import traceback
from datetime import date, timedelta
from pathlib import Path

from .config import Settings


def run_check(settings: Settings, say=print) -> int:
    import rasterio
    import requests
    from rasterio.windows import Window

    from .download import _https, download_file, gdal_env, remote_href
    from .stac import search_scenes

    cfg = settings.source_config()
    say(f"Ship Tracker connection check ({settings.source}: {cfg['stac_url']})\n")
    ok = True

    def step(name, fn):
        nonlocal ok
        say(f"- {name} ... ", end="")
        t = time.time()
        try:
            result = fn()
        except Exception as exc:  # report and keep going where possible
            ok = False
            say(f"FAILED ({time.time() - t:.1f} s)\n    {type(exc).__name__}: {exc}")
            last = traceback.extract_tb(exc.__traceback__)[-1]
            say(f"    at {Path(last.filename).name}:{last.lineno}")
            return None
        say(f"ok ({time.time() - t:.1f} s){': ' + result if isinstance(result, str) else ''}")
        return result if result is not None else True

    area = settings.areas()[0]
    start = (date.today() - timedelta(days=10)).isoformat()
    scenes = step(f"1. search the image catalogue ({area.name}, last 10 days)",
                  lambda: search_scenes(cfg, area.geom, start, date.today().isoformat(), 100))
    if not scenes:
        if scenes is not None:
            say("    The catalogue answered but listed no images for the last 10 days.")
        say("\nCannot continue without a catalogue result. Check the internet connection, and that no "
            "firewall, antivirus or proxy blocks earth-search.aws.element84.com.")
        return 1
    sc = scenes[0]
    say(f"    {len(scenes)} images found; testing the newest: {sc.id} ({sc.datetime[:16]})")

    tmp = Path(tempfile.mkdtemp(prefix="shiptracker-check-"))
    step("2. download the small scene-classification band (SCL)",
         lambda: f"{download_file(sc.scl_href, tmp / 'SCL.tif', cfg).stat().st_size / 1e6:.1f} MB")

    def stream(href):
        url = remote_href(href, cfg)
        with gdal_env(), rasterio.open(url) as src:
            w = Window(src.width // 2 - 256, src.height // 2 - 256, 512, 512)
            arr = src.read(1, window=w)
        return f"read a 512 x 512 pixel window of a {src.width} x {src.height} image (values {arr.min()}-{arr.max()})"

    nir = step("3. stream part of the near-infrared band (B08), as scans do", lambda: stream(sc.nir_href))
    if sc.rgb_href:
        step("4. stream part of the true-colour image (TCI)", lambda: stream(sc.rgb_href))
    if not nir:
        def plain():
            r = requests.get(remote_href(sc.nir_href, cfg), headers={"Range": "bytes=0-65535"}, timeout=60)
            return f"HTTP {r.status_code}, {len(r.content)} bytes"
        say("    Streaming failed; testing a plain download of the same file to see why:")
        if step("   plain HTTP range request", plain):
            say("    Plain downloads work, so scans fall back to downloading whole files (slower, but they work).")
        say(f"    File: {_https(sc.nir_href)}")

    def processes():
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing
        with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context("spawn")) as pool:
            return f"worker process answered {pool.submit(pow, 2, 10).result()}"
    step("5. start a worker process (analysis uses one per CPU core)", processes)

    say("\nAll good: imagery can be found and read. Note that scans stream only the parts of each image "
        "they need, so data\\tiles stays empty; that is normal." if ok else
        "\nSomething failed above. Please send this whole output.")
    return 0 if ok else 1
