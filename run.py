"""One-command launcher: sets up the environment, starts the map, scans recent imagery.

    python run.py                 (Windows: py run.py, or double-click run.bat)
    python run.py --days 15 --max-cloud 50
    python run.py --no-scan       (just open the map with existing results)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import threading
import time
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQS = ROOT / "requirements.txt"
VENV_PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def in_venv() -> bool:
    return Path(sys.prefix).resolve() == VENV.resolve()


def bootstrap() -> int:
    """Create .venv and install requirements if needed, then re-run inside it."""
    if not VENV_PY.exists():
        print("Creating virtual environment in .venv ...", flush=True)
        venv.create(VENV, with_pip=True)
    stamp = VENV / ".requirements.sha1"
    digest = hashlib.sha1(REQS.read_bytes()).hexdigest()
    if not stamp.exists() or stamp.read_text().strip() != digest:
        print("Installing dependencies (first run only) ...", flush=True)
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install", "-q", "--disable-pip-version-check",
                               "-r", str(REQS)])
        stamp.write_text(digest)
    return subprocess.call([str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=ROOT)


def main() -> int:
    ap = argparse.ArgumentParser(description="Start the ship tracker map and scan recent Sentinel-2 imagery")
    ap.add_argument("--days", type=int, default=10, help="scan imagery from the last N days (default 10)")
    ap.add_argument("--limit", type=int, default=0, help="max tiles to download this run (default 0 = whole area)")
    ap.add_argument("--all-passes", action="store_true",
                    help="scan every pass in the window instead of the newest image of each tile")
    ap.add_argument("--workers", type=int, default=3, help="tiles processed in parallel (default 3)")
    ap.add_argument("--keep-tiles", action="store_true",
                    help="keep downloaded tiles (default: delete each after processing; chips are kept)")
    ap.add_argument("--max-cloud", type=float, default=30.0, help="max cloud cover %% (default 30)")
    ap.add_argument("--no-scan", action="store_true", help="don't scan, just open the map")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    import logging
    from datetime import date, timedelta

    from werkzeug.serving import make_server

    from shiptracker.server import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    app = create_app()
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    url = f"http://127.0.0.1:{args.port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"\n  Ship Tracker map: {url}   (press Ctrl+C to stop)\n", flush=True)

    if not args.no_scan:
        body = {
            "start": (date.today() - timedelta(days=args.days)).isoformat(),
            "end": date.today().isoformat(),
            "max_cloud": args.max_cloud,
            "limit": args.limit or None,
            "all_passes": args.all_passes,
            "workers": args.workers,
            "keep_tiles": args.keep_tiles,
        }
        resp = app.test_client().post("/api/scan", json=body)
        if resp.status_code != 202:
            print("Could not start scan:", resp.get_json(), flush=True)
    if not args.no_browser:
        webbrowser.open(url)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()
    return 0


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10 or newer is required.")
    sys.exit(main() if in_venv() else bootstrap())
