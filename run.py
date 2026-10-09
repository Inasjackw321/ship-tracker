"""One-command launcher: sets up the environment, starts the map, scans recent imagery.

    python run.py                 (Windows: py run.py, or double-click run.bat)
    python run.py --days 45 --max-cloud 50
    python run.py --no-scan       (just open the map with existing results)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import socket
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


def free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """First port from ``preferred`` upward that can be bound, else one the OS picks.

    On Windows a port can be blocked even when nothing listens on it (Hyper-V, WSL and
    Docker reserve ranges), which shows up as "access a socket in a way forbidden".
    """
    for port in [*range(preferred, preferred + 20), 0]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            return s.getsockname()[1]
    raise RuntimeError("no free local port found")


def main() -> int:
    ap = argparse.ArgumentParser(description="Start the ship tracker map and scan recent Sentinel-2 imagery")
    ap.add_argument("--days", type=int, default=30,
                    help="look this many days back for each tile's most recent image (default 30)")
    ap.add_argument("--limit", type=int, default=0, help="max tiles to download this run (default 0 = whole area)")
    ap.add_argument("--all-passes", action="store_true",
                    help="scan every pass in the window instead of the newest image of each tile")
    ap.add_argument("--workers", type=int, default=3, help="tiles processed in parallel (default 3)")
    ap.add_argument("--keep-tiles", action="store_true",
                    help="keep downloaded tiles (default: delete each after processing; chips are kept)")
    ap.add_argument("--max-cloud", type=float, default=30.0, help="max cloud cover %% (default 30)")
    ap.add_argument("--priority-only", action="store_true",
                    help="scan only the priority regions in config/priority.geojson (they are always scanned first)")
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
    port = free_port(args.port)
    if port != args.port:
        print(f"Port {args.port} is unavailable on this computer; using {port} instead.", flush=True)
    server = make_server("127.0.0.1", port, app, threaded=True)
    url = f"http://127.0.0.1:{port}"
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
            "priority_only": args.priority_only,
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
