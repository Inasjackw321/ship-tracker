"""End-to-end: STAC search -> tile download -> detection -> database -> web API.

A local HTTP server plays the part of the STAC API and the imagery bucket, serving
synthetic Sentinel-2 tiles that sit inside the default search area.
"""
import json
import math
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from shiptracker.config import Settings
from shiptracker.scene import apply_affine
from shiptracker.pipeline import ScanOptions, scan
from shiptracker.server import create_app
from tests.synth import make_scene, write_s3_granule

EPSG = 32641            # UTM 41N
LON, LAT = 61.0, 23.0   # Gulf of Oman, inside config/aoi.geojson
SHIPS = [(150, 150, 121.5, 20, 8), (300, 300, 250, 40, 45), (450, 450, 400, 60, 170),
         (520, 320, 180, 30, 135)]


def _write_tiles(dirpath: Path, origin_xy):
    refl, scl10, truth = make_scene(ships=SHIPS)
    H, W = refl.shape
    tr10 = from_origin(origin_xy[0], origin_xy[1], 10, 10)
    tr20 = from_origin(origin_xy[0], origin_xy[1], 20, 20)
    dn = np.clip((refl + 0.1) / 1e-4, 1, 65535).astype(np.uint16)
    dn[:, -20:] = 0  # nodata edge, as on real tiles
    common = dict(driver="GTiff", crs=f"EPSG:{EPSG}", tiled=True, blockxsize=256, blockysize=256,
                  compress="deflate")
    with rasterio.open(dirpath / "B08.tif", "w", height=H, width=W, count=1, dtype="uint16",
                       transform=tr10, nodata=0, **common) as dst:
        dst.write(dn, 1)
    with rasterio.open(dirpath / "SCL.tif", "w", height=H // 2, width=W // 2, count=1, dtype="uint8",
                       transform=tr20, nodata=0, **common) as dst:
        dst.write(scl10[::2, ::2], 1)
    rgb = np.clip(np.stack([refl * 900, refl * 1000, refl * 1100]), 0, 255).astype(np.uint8)
    with rasterio.open(dirpath / "TCI.tif", "w", height=H, width=W, count=3, dtype="uint8",
                       transform=tr10, nodata=0, **common) as dst:
        dst.write(rgb)
    bounds = transform_bounds(f"EPSG:{EPSG}", "EPSG:4326", *rasterio.open(dirpath / "B08.tif").bounds)
    return truth, tr10, bounds


def _item(item_id, dt, base_url, sub, bounds, tile="41QKL"):
    w, s, e, n = bounds
    return {
        "type": "Feature", "stac_version": "1.0.0", "id": item_id,
        "geometry": {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
        "bbox": [w, s, e, n],
        "properties": {"datetime": dt, "eo:cloud_cover": 3.2, "s2:mgrs_tile": tile, "proj:epsg": EPSG},
        "assets": {
            "nir": {"href": f"{base_url}/{sub}/B08.tif", "raster:bands": [{"scale": 0.0001, "offset": -0.1}]},
            "scl": {"href": f"{base_url}/{sub}/SCL.tif"},
            "visual": {"href": f"{base_url}/{sub}/TCI.tif"},
        },
        "links": [],
    }


class Handler(SimpleHTTPRequestHandler):
    pages: list = []
    s3_items: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        page = body.get("page", 0)
        assert body["intersects"]["type"] in ("Polygon", "MultiPolygon")
        if body["collections"] == ["sentinel-3-olci-wfr-l2-netcdf"]:
            out = {"type": "FeatureCollection", "features": self.s3_items, "links": []}
        else:
            assert body["collections"] == ["sentinel-2-c1-l2a"]
            out = {"type": "FeatureCollection", "features": self.pages[page], "links": []}
        if body["collections"] == ["sentinel-2-c1-l2a"] and page + 1 < len(self.pages):
            out["links"].append({"rel": "next", "href": f"http://{self.headers['Host']}/search",
                                 "method": "POST", "body": {"page": page + 1}, "merge": True})
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/geo+json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def world(tmp_path):
    served = tmp_path / "served"
    x, y = Transformer.from_crs("EPSG:4326", f"EPSG:{EPSG}", always_xy=True).transform(LON, LAT)
    origin = (round(x, -1), round(y, -1))
    (served / "a").mkdir(parents=True)
    (served / "b").mkdir()
    truth, tr, bounds = _write_tiles(served / "a", origin)
    _write_tiles(served / "b", origin)  # overlapping tile from the same pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(served)))
    base = f"http://127.0.0.1:{server.server_port}"
    Handler.pages = [
        [_item("S2A_41QKL_20260930_0_L2A", "2026-09-30T06:40:11Z", base, "a", bounds)],
        [_item("S2A_41QKM_20260930_0_L2A", "2026-09-30T06:40:15Z", base, "b", bounds, tile="41QKM")],
    ]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (served / "s3").mkdir()
    s3_truth, s3_geom = write_s3_granule(served / "s3")
    Handler.s3_items = [{
        "type": "Feature", "id": "S3A_OL_2_WFR____20260930T060000", "geometry": s3_geom,
        "properties": {"datetime": "2026-09-30T06:00:00Z"},
        "assets": {k: {"href": f"{base}/s3/{f}"} for k, f in
                   (("oa17", "Oa17_reflectance.nc"), ("geo", "geo_coordinates.nc"), ("wqsf", "wqsf.nc"))},
    }]
    settings = Settings(data_dir=tmp_path / "data", stac_url=base, s3_stac_url=base)
    yield settings, truth, tr
    server.shutdown()


def test_scan_end_to_end(world):
    settings, truth, tr = world
    opts = ScanOptions(start="2026-09-29", end="2026-10-01", mode="all")
    rep = scan(settings, opts)
    assert (rep.found, rep.processed, rep.failed) == (2, 2, 0)
    assert rep.ships == len(SHIPS), "overlapping tile from the same pass must not double count"

    # tiles were downloaded to the cache
    assert (settings.tiles_dir / "S2A_41QKL_20260930_0_L2A" / "B08.tif").exists()
    assert (settings.tiles_dir / "S2A_41QKL_20260930_0_L2A" / "TCI.tif").exists()

    client = create_app(settings).test_client()
    fc = client.get("/api/detections").get_json()
    assert len(fc["features"]) == len(SHIPS)
    to_wgs = Transformer.from_crs(f"EPSG:{EPSG}", "EPSG:4326", always_xy=True)
    for r, c, L, _, _ in truth:
        lon, lat = to_wgs.transform(*apply_affine(tr, c + 0.5, r + 0.5))
        f = min(fc["features"], key=lambda f: math.dist(f["geometry"]["coordinates"], (lon, lat)))
        assert math.dist(f["geometry"]["coordinates"], (lon, lat)) < 0.0003  # ~30 m
        assert abs(f["properties"]["length_m"] - L) / L < 0.10
        chip = client.get(f["properties"]["chip_url"])
        assert chip.status_code == 200 and chip.data[:4] == b"\x89PNG"

    assert len(client.get("/api/detections?min_length=200").get_json()["features"]) == 2
    assert client.get("/api/stats").get_json()["scenes_done"] == 2
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/api/aoi").get_json()["geometry"]["type"] == "Polygon"

    n_chips = len([p for p in settings.chips_dir.rglob("*.png") if "s3" not in p.parts])
    assert n_chips == len(SHIPS), "chips of de-duplicated detections should be removed"

    # Sentinel-3 ran on the open sea Sentinel-2 did not cover
    s3 = client.get("/api/s3/detections").get_json()["features"]
    assert len(s3) == 3 and all(f["properties"]["chip_url"] for f in s3)
    assert rep.s3_granules == 1 and rep.s3_ships == 3

    cov = client.get("/api/coverage").get_json()
    assert 0 < cov["fraction"] < 0.01  # one small synthetic tile in a huge area
    assert cov["missing"]["type"] in ("Polygon", "MultiPolygon")

    again = scan(settings, opts)
    assert again.already_done == 2 and again.ships == 0

    # Results from an older detector version are recomputed, not kept or duplicated.
    from shiptracker.db import Store
    st = Store(settings.db_path)
    with st.conn:
        st.conn.execute("UPDATE scenes SET detector_version=1")
    st.close()
    redo = scan(settings, opts)
    assert redo.processed == 2 and redo.already_done == 0
    assert len(client.get("/api/detections").get_json()["features"]) == len(SHIPS)


def test_latest_mode_processes_each_tile_and_latest_view(world):
    settings, _, _ = world
    rep = scan(settings, ScanOptions(start="2026-09-29", end="2026-10-01"))
    assert (rep.found, rep.processed) == (2, 2)  # two overlapping tiles, one image each
    assert rep.area_imaged is not None
    client = create_app(settings).test_client()
    latest = client.get("/api/detections").get_json()["features"]
    every = client.get("/api/detections?view=all").get_json()["features"]
    assert len(latest) == len(every) == len(SHIPS)
    assert len(client.get("/api/scenes").get_json()) == 2


def test_tile_falls_back_to_older_pass_when_newest_is_cloud(world):
    """Newest image of tile 41QKL is clouded over: use its previous pass instead, and
    show that pass on the map."""
    import shutil

    settings, _, _ = world
    served = settings.data_dir.parent / "served"
    shutil.copytree(served / "a", served / "c")
    with rasterio.open(served / "c" / "SCL.tif", "r+") as dst:
        dst.write(np.full((dst.height, dst.width), 9, np.uint8), 1)  # all cloud
    older = Handler.pages[0][0]
    w, s, e, n = older["bbox"]
    Handler.pages = [[_item("S2A_41QKL_20261005_0_L2A", "2026-10-05T06:40:11Z", settings.stac_url, "c",
                            (w, s, e, n)), older]]

    rep = scan(settings, ScanOptions(start="2026-09-29", end="2026-10-06", sentinel3=False))
    assert (rep.skipped, rep.processed) == (1, 1)
    assert rep.ships == len(SHIPS)
    client = create_app(settings).test_client()
    feats = client.get("/api/detections").get_json()["features"]
    assert len(feats) == len(SHIPS)
    assert {f["properties"]["scene_id"] for f in feats} == {"S2A_41QKL_20260930_0_L2A"}


def test_priority_only_skips_tiles_outside_priority_regions(world, tmp_path):
    import json

    settings, _, _ = world
    far_away = tmp_path / "priority.geojson"
    far_away.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"name": "Hormuz only"},
        "geometry": {"type": "Polygon", "coordinates": [[[56, 26], [57, 26], [57, 27], [56, 27], [56, 26]]]}}]}))
    settings.priority_path = far_away
    rep = scan(settings, ScanOptions(start="2026-09-29", end="2026-10-01", priority_only=True, sentinel3=False))
    assert rep.found == 0 and rep.processed == 0
    rep = scan(settings, ScanOptions(start="2026-09-29", end="2026-10-01", sentinel3=False))
    assert rep.processed == 2  # without priority_only the rest of the area still follows


def test_downloads_carry_coordinates(world, tmp_path):
    import io
    import zipfile

    from PIL import Image

    settings, _, _ = world
    scan(settings, ScanOptions(start="2026-09-29", end="2026-10-01"))
    client = create_app(settings).test_client()
    ship = client.get("/api/detections").get_json()["features"][0]
    lon, lat = ship["geometry"]["coordinates"]
    sid = ship["properties"]["id"]
    assert ship["properties"]["has_tif"]

    # PNG: coordinates in the metadata and the file name
    r = client.get(f"/api/detections/{sid}/image.png?dl=1")
    assert r.status_code == 200 and r.mimetype == "image/png"
    assert "attachment" in r.headers["Content-Disposition"] and f"{lat:.5f}N" in r.headers["Content-Disposition"]
    img = Image.open(io.BytesIO(r.data))
    assert img.text["Coordinates"] == f"{lat:.5f}, {lon:.5f}"
    assert img.height > img.width  # framed with header and footer text

    # GeoTIFF: the centre pixel is the ship
    r = client.get(f"/api/detections/{sid}/image.tif")
    assert r.status_code == 200
    tif = tmp_path / "ship.tif"
    tif.write_bytes(r.data)
    with rasterio.open(tif) as src:
        cx, cy = apply_affine(src.transform, src.width / 2, src.height / 2)
        tlon, tlat = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True).transform(cx, cy)
        assert src.count == 3 and float(src.tags()["SHIP_LAT"]) == pytest.approx(lat, abs=1e-5)
    assert abs(tlat - lat) < 0.0002 and abs(tlon - lon) < 0.0002  # within ~20 m

    # Sentinel-3 PNG and a zip of everything
    s3 = client.get("/api/s3/detections").get_json()["features"]
    assert client.get(f"/api/s3/detections/{s3[0]['properties']['id']}/image.png").status_code == 200
    s2_ids = [f["properties"]["id"] for f in client.get("/api/detections").get_json()["features"]]
    r = client.post("/api/download.zip", json={"s2": s2_ids, "s3": [s3[0]["properties"]["id"]]})
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = z.namelist()
    assert sum(n.endswith(".png") for n in names) == len(s2_ids) + 1
    assert sum(n.endswith(".tif") for n in names) == len(s2_ids)
    csv_rows = z.read("coordinates.csv").decode().strip().splitlines()
    assert len(csv_rows) == len(s2_ids) + 2  # header + ships
    assert f"{lat:.5f}, {lon:.5f}" in z.read("coordinates.csv").decode()
    assert z.read("ships.kml").decode().count("<Placemark>") == len(s2_ids) + 1
    assert client.post("/api/download.zip", json={}).status_code == 400


def test_s3_check_runs_every_step(world, capsys):
    from shapely.geometry import box

    from shiptracker.s3 import diagnose

    settings, _, _ = world
    lines = []
    code = diagnose(settings, box(60, 10, 70, 20), out=lines.append)
    text = "\n".join(lines)
    assert code == 0, text
    for step in ("search", "download Oa17_reflectance.nc", "read quality flags", "detect bright specks"):
        assert f"- {step} ..." in text
    assert "FAILED" not in text and "3 possible large ships" in text
