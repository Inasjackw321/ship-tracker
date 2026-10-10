"""End-to-end: STAC search -> tile download -> detection -> database -> web API.

A local HTTP server plays the part of the STAC API and the imagery bucket, serving
synthetic Sentinel-2 tiles that sit inside the Gulf of Oman area.
"""
import json
import math
import os
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
from tests.synth import make_scene

EPSG = 32641            # UTM 41N
LON, LAT = 61.0, 23.0   # inside the gulf-of-oman area of config/areas.geojson
AREA = "gulf-of-oman"
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
    """Fake STAC API + imagery bucket (with HTTP range requests, like S3)."""
    pages: list = []
    served: dict = {}  # file name -> bytes sent

    def log_message(self, *a):
        pass

    def send_head(self):
        rng = self.headers.get("Range", "")
        if not rng.startswith("bytes=") or "," in rng:
            self._left = None
            return super().send_head()
        try:
            f = open(self.translate_path(self.path), "rb")
        except OSError:
            self.send_error(404)
            return None
        size = os.fstat(f.fileno()).st_size
        a, _, b = rng[6:].partition("-")
        start = int(a) if a else max(size - int(b), 0)
        end = min(int(b), size - 1) if a and b else size - 1
        if start >= size:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        self.send_response(206)
        self.send_header("Content-Type", "image/tiff")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        f.seek(start)
        self._left = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        name = Path(self.path).name
        if self._left is None:
            data = source.read()
        else:
            data = source.read(self._left)
        Handler.served[name] = Handler.served.get(name, 0) + len(data)
        outputfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        page = body.get("page", 0)
        assert body["intersects"]["type"] in ("Polygon", "MultiPolygon")
        assert body["collections"] == ["sentinel-2-c1-l2a"]
        out = {"type": "FeatureCollection", "features": self.pages[page], "links": []}
        if page + 1 < len(self.pages):
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
    settings = Settings(data_dir=tmp_path / "data", stac_url=base)
    yield settings, truth, tr
    server.shutdown()


def test_scan_end_to_end(world):
    settings, truth, tr = world
    opts = ScanOptions(area=AREA, start="2026-09-29", end="2026-10-01", mode="all", keep_tiles=True)
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
    areas = {f["properties"]["id"]: f["properties"] for f in client.get("/api/areas").get_json()["features"]}
    assert len(areas) == 7
    assert areas[AREA]["status"] == "done" and areas[AREA]["ships"] == len(SHIPS)
    assert areas["baltic"]["ships"] == 0 and "status" not in areas["baltic"]

    n_chips = len(list(settings.chips_dir.rglob("*.png")))
    assert n_chips == len(SHIPS), "chips of de-duplicated detections should be removed"

    cov = client.get("/api/coverage").get_json()
    assert list(cov) == [AREA]
    cov = cov[AREA]
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
    rep = scan(settings, ScanOptions(area=AREA, start="2026-09-29", end="2026-10-01"))
    assert (rep.found, rep.processed) == (2, 2)  # two overlapping tiles, one image each
    assert rep.area_imaged is not None
    client = create_app(settings).test_client()
    latest = client.get("/api/detections").get_json()["features"]
    every = client.get("/api/detections?view=all").get_json()["features"]
    assert len(latest) == len(every) == len(SHIPS)
    assert len(client.get("/api/scenes").get_json()) == 2


def test_streaming_reads_parts_without_downloading_files(world):
    """By default the big bands are streamed with range requests: no B08/TCI files are
    written, and the results are the same as from downloaded files."""
    from shiptracker.db import Store
    from shiptracker.pipeline import reset_data

    settings, _, _ = world
    Handler.served = {}
    rep = scan(settings, ScanOptions(area=AREA, start="2026-09-29", end="2026-10-01", workers=2))
    assert (rep.processed, rep.ships) == (2, len(SHIPS))
    assert not list(settings.tiles_dir.rglob("B08.tif*")) and not list(settings.tiles_dir.rglob("TCI.tif*"))
    assert Handler.served.get("B08.tif") and Handler.served.get("TCI.tif")

    def sizes():
        feats = create_app(settings).test_client().get("/api/detections").get_json()["features"]
        return sorted((f["properties"]["length_m"], f["properties"]["width_m"]) for f in feats)

    streamed = sizes()
    st = Store(settings.db_path)
    reset_data(settings, st)
    st.close()
    rep = scan(settings, ScanOptions(area=AREA, start="2026-09-29", end="2026-10-01", keep_tiles=True))
    assert rep.ships == len(SHIPS) and sizes() == streamed


def test_falls_back_to_download_when_streaming_fails(world, monkeypatch):
    from shiptracker import pipeline

    settings, _, _ = world
    monkeypatch.setattr(pipeline, "remote_href", lambda href, cfg: "http://127.0.0.1:9/unreachable.tif")
    rep = scan(settings, ScanOptions(area=AREA, start="2026-09-29", end="2026-10-01", processes=False))
    assert (rep.processed, rep.failed, rep.ships) == (2, 0, len(SHIPS))


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

    rep = scan(settings, ScanOptions(area=AREA, start="2026-09-29", end="2026-10-06"))
    assert (rep.skipped, rep.processed) == (1, 1)
    assert rep.ships == len(SHIPS)
    client = create_app(settings).test_client()
    feats = client.get("/api/detections").get_json()["features"]
    assert len(feats) == len(SHIPS)
    assert {f["properties"]["scene_id"] for f in feats} == {"S2A_41QKL_20260930_0_L2A"}


def test_scan_only_covers_the_chosen_area(world):
    """A tile in the Gulf of Oman is not analysed when another area is scanned."""
    settings, _, _ = world
    rep = scan(settings, ScanOptions(area="baltic", start="2026-09-29", end="2026-10-01"))
    assert rep.found == 0 and rep.processed == 0
    client = create_app(settings).test_client()
    areas = {f["properties"]["id"]: f["properties"] for f in client.get("/api/areas").get_json()["features"]}
    assert areas["baltic"]["status"] == "done" and areas["baltic"]["imaged"] == 0
    assert "status" not in areas[AREA]


def test_tile_shared_by_two_areas_is_analysed_whole(world, tmp_path):
    """A tile on the border between two areas: scanning one area finds the ships in both
    halves, so the later scan of the other area (which skips the tile) misses nothing."""
    settings, _, _ = world
    w, s, e, n = Handler.pages[0][0]["bbox"]
    mid = (w + e) / 2
    halves = tmp_path / "areas.geojson"
    halves.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"id": i, "name": i, "group": "test"}, "geometry": {
            "type": "Polygon", "coordinates": [[[a, s - 1], [b, s - 1], [b, n + 1], [a, n + 1], [a, s - 1]]]}}
        for i, a, b in (("west", w - 1, mid), ("east", mid, e + 1))]}))
    settings.areas_path = halves
    rep = scan(settings, ScanOptions(area="west", start="2026-09-29", end="2026-10-01"))
    assert rep.processed == 2 and rep.ships == len(SHIPS)
    rep = scan(settings, ScanOptions(area="east", start="2026-09-29", end="2026-10-01"))
    assert rep.already_done == 2 and rep.processed == 0
    client = create_app(settings).test_client()
    areas = {f["properties"]["id"]: f["properties"] for f in client.get("/api/areas").get_json()["features"]}
    assert areas["west"]["ships"] + areas["east"]["ships"] == len(SHIPS)
    assert areas["west"]["ships"] and areas["east"]["ships"]


def test_reset_starts_fresh(world):
    settings, _, _ = world
    scan(settings, ScanOptions(area=AREA, start="2026-09-29", end="2026-10-01", keep_tiles=True))
    client = create_app(settings).test_client()
    assert len(client.get("/api/detections").get_json()["features"]) == len(SHIPS)
    assert list(settings.chips_dir.rglob("*.png")) and list(settings.tiles_dir.iterdir())

    r = client.post("/api/reset")
    assert r.status_code == 200 and r.get_json()["ships"] == len(SHIPS) and r.get_json()["tiles"] == 2
    assert client.get("/api/detections?view=all").get_json()["features"] == []
    assert client.get("/api/scenes").get_json() == [] and client.get("/api/coverage").get_json() == {}
    assert client.get("/api/stats").get_json()["detections"] == 0
    areas = {f["properties"]["id"]: f["properties"] for f in client.get("/api/areas").get_json()["features"]}
    assert "status" not in areas[AREA] and areas[AREA]["ships"] == 0
    assert not list(settings.chips_dir.rglob("*")) and not list(settings.tiles_dir.iterdir())

    # the next scan analyses everything again instead of skipping it as already done
    rep = scan(settings, ScanOptions(area=AREA, start="2026-09-29", end="2026-10-01"))
    assert rep.processed == 2 and rep.already_done == 0 and rep.ships == len(SHIPS)
    assert len(client.get("/api/detections").get_json()["features"]) == len(SHIPS)


def test_reset_from_the_command_line(world, monkeypatch, capsys):
    from shiptracker.__main__ import main as cli

    settings, _, _ = world
    scan(settings, ScanOptions(area=AREA, start="2026-09-29", end="2026-10-01"))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli(["--data", str(settings.data_dir), "reset"]) == 1
    assert create_app(settings).test_client().get("/api/stats").get_json()["detections"] == len(SHIPS)
    assert cli(["--data", str(settings.data_dir), "reset", "--yes"]) == 0
    assert f"Deleted {len(SHIPS)} ships" in capsys.readouterr().out
    assert create_app(settings).test_client().get("/api/stats").get_json()["detections"] == 0


def test_scan_api_requires_an_area(world):
    settings, _, _ = world
    client = create_app(settings).test_client()
    r = client.post("/api/scan", json={"days": 7})
    assert r.status_code == 400 and AREA in r.get_json()["areas"]
    assert client.post("/api/scan", json={"area": "atlantis"}).status_code == 400
    r = client.post("/api/scan", json={"area": AREA, "days": 5})
    assert r.status_code == 202 and r.get_json()["options"]["area"] == AREA
    import time
    for _ in range(600):
        if not client.get("/api/scan").get_json()["running"]:
            break
        time.sleep(0.1)
    status = client.get("/api/scan").get_json()
    assert status["error"] is None and status["report"]["ships"] == len(SHIPS)


def test_downloads_carry_coordinates(world, tmp_path):
    import io
    import zipfile

    from PIL import Image

    settings, _, _ = world
    scan(settings, ScanOptions(area=AREA, start="2026-09-29", end="2026-10-01"))
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

    # a zip of everything
    s2_ids = [f["properties"]["id"] for f in client.get("/api/detections").get_json()["features"]]
    r = client.post("/api/download.zip", json={"ids": s2_ids})
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = z.namelist()
    assert sum(n.endswith(".png") for n in names) == len(s2_ids)
    assert sum(n.endswith(".tif") for n in names) == len(s2_ids)
    csv_rows = z.read("coordinates.csv").decode().strip().splitlines()
    assert len(csv_rows) == len(s2_ids) + 1  # header + ships
    assert f"{lat:.5f}, {lon:.5f}" in z.read("coordinates.csv").decode()
    assert z.read("ships.kml").decode().count("<Placemark>") == len(s2_ids)
    assert client.post("/api/download.zip", json={}).status_code == 400



def test_adjust_measurement_by_hand_and_reset(world):
    import io

    from PIL import Image
    from pyproj import Geod

    settings, _, _ = world
    scan(settings, ScanOptions(area=AREA, start="2026-09-29", end="2026-10-01"))
    client = create_app(settings).test_client()
    ship = max(client.get("/api/detections").get_json()["features"], key=lambda f: f["properties"]["length_m"])
    p = ship["properties"]
    sid, auto_len = p["id"], p["length_m"]
    assert p["method"] == "fit" and p["length_err_m"] > 0 and not p["adjusted"]

    # move the bow ~20 m further out along the hull
    g = Geod(ellps="WGS84")
    az, _, _ = g.inv(p["stern_lon"], p["stern_lat"], p["bow_lon"], p["bow_lat"])
    blon, blat, _ = g.fwd(p["bow_lon"], p["bow_lat"], az, 20)
    r = client.post(f"/api/detections/{sid}/measurement",
                    json={"stern": [p["stern_lat"], p["stern_lon"]], "bow": [blat, blon], "width_m": 55})
    adj = r.get_json()["properties"]
    assert r.status_code == 200 and adj["adjusted"] and adj["method"] == "manual"
    assert adj["length_m"] == pytest.approx(auto_len + 20, abs=0.6) and adj["width_m"] == 55
    assert adj["auto"]["length_m"] == auto_len
    assert adj["chip_url"].startswith(f"/api/detections/{sid}/chip.png")

    chip = client.get(adj["chip_url"])
    assert chip.status_code == 200 and Image.open(io.BytesIO(chip.data)).size[0] == 360
    png = Image.open(io.BytesIO(client.get(f"/api/detections/{sid}/image.png").data))
    assert "adjusted by hand" in png.text["Description"] and "Automatic measurement was" in png.text["Description"]

    # list and filters see the corrected length
    lengths = {f["properties"]["id"]: f["properties"]["length_m"] for f in client.get("/api/detections").get_json()["features"]}
    assert lengths[sid] == adj["length_m"]

    back = client.post(f"/api/detections/{sid}/measurement/reset").get_json()["properties"]
    assert back["length_m"] == auto_len and back["method"] == "fit" and not back["adjusted"]
    assert client.post(f"/api/detections/{sid}/measurement", json={"stern": [1]}).status_code == 400
