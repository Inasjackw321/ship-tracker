"""Copernicus Data Space Ecosystem (CDSE): ESA's free official source for Sentinel-3.

Searching is open; downloading needs a free account (https://dataspace.copernicus.eu).
Credentials come from CDSE_USERNAME / CDSE_PASSWORD or from ``<data>/cdse.json``,
which ``py run.py --cdse-login`` writes.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

CDSE = {
    "catalogue": "https://catalogue.dataspace.copernicus.eu/odata/v1",
    "download": "https://download.dataspace.copernicus.eu/odata/v1",
    "token_url": "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
    "product_type": "OL_2_WFR___",  # OLCI Level-2 water, full resolution (300 m)
}

SIGNUP_HELP = ("Sentinel-3 from Copernicus Data Space needs a free account: register at "
               "https://dataspace.copernicus.eu, then run  py run.py --cdse-login")


def credentials_path(data_dir: Path) -> Path:
    return Path(data_dir) / "cdse.json"


def load_credentials(data_dir: Path) -> tuple[str, str] | None:
    import os

    user, pw = os.environ.get("CDSE_USERNAME"), os.environ.get("CDSE_PASSWORD")
    if user and pw:
        return user, pw
    p = credentials_path(data_dir)
    if p.exists():
        try:
            d = json.loads(p.read_text())
            if d.get("username") and d.get("password"):
                return d["username"], d["password"]
        except (OSError, ValueError):
            log.warning("Could not read %s", p)
    return None


def save_credentials(data_dir: Path, username: str, password: str) -> Path:
    p = credentials_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"username": username, "password": password}))
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return p


def login_interactive(data_dir: Path, token_url: str = CDSE["token_url"], ask=input) -> int:
    """Ask for the Copernicus Data Space login, check it, and save it for later scans."""
    import getpass

    print("Copernicus Data Space login (free account: https://dataspace.copernicus.eu)")
    print("Used only to download Sentinel-3 images; saved on this computer in", credentials_path(data_dir))
    username = ask("E-mail / username: ").strip()
    password = getpass.getpass("Password (not shown): ")
    try:
        CdseAuth(username, password, token_url).token()
    except PermissionError as exc:
        print("Login failed:", exc)
        return 1
    except requests.RequestException as exc:
        print("Could not reach Copernicus Data Space:", exc)
        return 1
    path = save_credentials(data_dir, username, password)
    print(f"Login OK and saved to {path}. Sentinel-3 will now come from Copernicus Data Space.")
    return 0


class CdseAuth:
    """Access tokens for downloads (they last ~10 minutes; refreshed as needed)."""

    def __init__(self, username: str, password: str, token_url: str = CDSE["token_url"]):
        self.username, self.password, self.token_url = username, password, token_url
        self._token: str | None = None
        self._expires = 0.0
        self._lock = threading.Lock()

    def token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.time() > self._expires - 60:
                r = requests.post(self.token_url, timeout=60, data={
                    "client_id": "cdse-public", "grant_type": "password",
                    "username": self.username, "password": self.password})
                if r.status_code in (400, 401):
                    raise PermissionError("Copernicus Data Space rejected the username/password "
                                          "(run  py run.py --cdse-login  again)")
                r.raise_for_status()
                data = r.json()
                self._token = data["access_token"]
                self._expires = time.time() + float(data.get("expires_in", 600))
            return self._token

    def headers(self, refresh: bool = False) -> dict:
        return {"Authorization": f"Bearer {self.token(force=refresh)}"}
