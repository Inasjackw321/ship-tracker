# Ship Tracker: Sentinel-2 ship detection and sizing

This tool finds ships at sea in free **Sentinel-2** satellite imagery and measures each one: length, beam and hull axis. You get the same kind of number as laying the Google Maps ruler bow to stern. It covers the **Strait of Hormuz, the Gulf of Oman and the Arabian Sea** (`config/aoi.geojson`). Results are stored in SQLite and shown on a web map, with an image chip of every ship that has a ruler drawn along the hull.

```
STAC search ──► download tiles ──► sea/cloud masks ──► bright-hull detection ──► measure ──► SQLite ──► web map
(Earth Search)  (SCL, B08, TCI)    (SCL + NIR)          (local CFAR, NIR B08)    (profiles)             + chips
```

## Quick start: one command

```
py run.py          # Windows (or double-click run.bat)
python3 run.py     # macOS / Linux
```

The first run creates `.venv` and installs everything. After that it starts the map, opens your browser at http://127.0.0.1:8000 and scans the **whole area**. Every Sentinel-2 tile uses its most recent usable image from the last 30 days. Open sea that Sentinel-2 never photographs is then checked with Sentinel-3. Ships appear on the map as each tile finishes.

Three tiles are processed in parallel, and each tile is deleted once it is processed (the ship chips are kept). Useful options are `--days 45`, `--max-cloud 50`, `--workers 4`, `--all-passes`, `--no-s3`, `--keep-tiles` and `--no-scan`.

## Manual setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# list available imagery over the area for the last 5 days (no download)
.venv/bin/python -m shiptracker search --days 5

# download tiles and detect ships (newest 10 tiles in the last 5 days)
.venv/bin/python -m shiptracker scan --days 5 --limit 10

# open the map at http://127.0.0.1:8000
.venv/bin/python -m shiptracker serve
```

You can also start scans from the web page: pick dates and click **Scan**. Progress shows live and ships appear on the map as each tile finishes.

You don't need any accounts or API keys. Imagery comes from the public [Element 84 Earth Search](https://earth-search.aws.element84.com/v1) catalogue (`sentinel-2-c1-l2a` on AWS). Add `--source planetary-computer` to use Microsoft Planetary Computer instead; it signs URLs anonymously.

## Commands

| Command | What it does |
|---|---|
| `search [--start D --end D \| --days N] [--max-cloud P]` | List scenes intersecting the area |
| `scan  [same] [--limit N] [--all-passes] [--workers 3] [--no-rgb] [--delete-tiles] [--reprocess]` | Download, detect and store. By default this is the newest image of every tile, covering the whole area once. |
| `watch [--every-hours 6] [--days 3]` | Keep scanning for new passes |
| `serve [--host --port]` | Web map and JSON API |
| `detect B08.tif SCL.tif [--rgb TCI.tif --chips DIR]` | Run detection on GeoTIFFs you already have |

Global options are `--data DIR` (default `./data`), `--aoi file.geojson` and `--source earth-search|planetary-computer`.

### What gets downloaded

For each tile (an MGRS square about 110 × 110 km), files go to `data/tiles/<scene-id>/`:

1. **`SCL.tif`** (20 m scene classification, a few MB). This is checked first. A tile with no cloud-free sea inside the area is marked *skipped* and nothing else is downloaded.
2. **`B08.tif`** (10 m near-infrared, roughly 100–200 MB). Detection runs on this band.
3. **`TCI.tif`** (10 m true colour). This is downloaded only when ships were found, and only for the image chips. Pass `--no-rgb` to skip it; chips then use the NIR band.

Downloads resume after an interruption (`.part` files with HTTP Range) and are retried with backoff. A scene that has already been processed is never processed again, so you can re-run `scan` safely. `--delete-tiles` removes each tile once it is processed, so disk use stays small.

## How detection and measurement work (`shiptracker/detect.py`, `shiptracker/verify.py`)

0. **Image edges.** Seeds within 300 m of the edge of the image data are ignored, as is anything touching nodata. Swath edges and tile borders produce bright artifacts, and the overlapping neighbour tile sees that strip from its interior.
1. **Sea mask.** Pixels count as sea if SCL calls them water or their NIR reflectance is very dark. Small enclosed non-water blobs, up to ship size, are filled back in, because SCL often labels the ships themselves as cloud or land. Vegetated patches are never filled, so mangrove islets and strips stay land. A 50 m coastal buffer is removed, as are SCL clouds and cloud shadows plus 100 m around them. Cloud blobs the size of a ship are not treated as cloud.
2. **Candidates.** In the 10 m NIR band, open water is near zero and hulls are bright. Each pixel is compared with the mean and standard deviation of the water around it (a 610 m window). It must be at least 5σ brighter and at least 0.025 reflectance brighter. Bright objects are excluded from the background first, using a coarse median, so large ships don't hide themselves. The statistics are then recomputed without first-pass hits, which copes with sun glint gradients.
3. **Hull extraction.** Each candidate is grown to every connected pixel brighter than 25 % of its peak, so the whole hull is captured rather than just the brightest part.
4. **Measurement.** PCA gives the hull axis. The image is resampled onto a grid aligned with the hull, and the along-hull and across-hull brightness profiles give **length and beam**. Each end is placed where the profile drops to 30 % of the typical deck level, and the 10 m sensor blur is corrected for. The deck level is a median rather than the peak, so a bright superstructure does not cut off the bow. The hull axis is reported as a true-north bearing (0–180°, since bow and stern can't be told apart).
5. **Filters on the object.**
   - Length must be 25–420 m and beam ≤ 90 m.
   - Objects ≥ 40 m must be at least twice as long as they are wide.
   - **Sharp ends:** objects ≥ 80 m need hull-like ends that drop off sharply. Cloud puffs fade out gradually.
   - **Clear water:** the object must not touch land or a large cloud.
   - **Clean surroundings:** a ring of water around it must hold almost no cloud, cloud shadow or other bright clutter. Ships sit in clean water, while clouds come in fields.
   - A confidence score combines contrast, elongation and size.
6. **Second-stage checks (`verify.py`).** These use the true-colour image, which is downloaded for the chips anyway, plus a global 1 km land map.
   - **Vegetation:** objects much brighter in NIR than in red (NDVI > 0.5) are vegetation.
   - **Cloud:** objects that are bright and grey-white in every band are cloud.
   - **Inland:** objects with land 1 km away in every direction are inland, e.g. salt flats.

   Each tile's log line, and its tooltip on the map, lists how many objects each rule rejected.
7. **Bookkeeping.**
   - A ship seen in two overlapping tiles of the same satellite pass is stored once.
   - A detection at the same spot on another date is flagged **stationary**: oil platforms, anchored ships or islets. The map can hide these.

**Accuracy.** On synthetic ships rendered with the Sentinel-2 10 m point-spread function, measured length is within about 5 % for 35–400 m vessels, e.g. a 121.5 m ship measures 116–122 m, and the hull axis is within 2°. Real-world error will be larger, roughly ±10–20 m. Causes include hull paint and cargo, wakes, and ships under 30 m that are only 2–3 pixels long. Beam is less reliable than length because most beams are only 2–5 pixels.

When the detector changes, tiles analysed by an older version are redone automatically on the next scan. Their old results are replaced.

### Open sea: Sentinel-3 (`shiptracker/s3.py`)

Sentinel-2 does not photograph most of the open Arabian Sea. After the Sentinel-2 part of a scan, the remaining sea is checked with **Sentinel-3 OLCI**. This is the 300 m full-resolution water product, from the last 2 days, served by Microsoft Planetary Computer with no account needed. The newest image of each part of that sea is used.

A 300 m pixel is larger than any ship, so this is a different, weaker kind of detection:

- **Detected:** a large ship (roughly 150 m and up, in clear sky) brightens its pixel in the 865 nm band enough to stand out as an isolated **bright speck**, often helped by its wake. Specks are rejected if they are larger than about 6 pixels (cloud or land), close to land, or surrounded by cloud or clutter.
- **Size:** **cannot be measured**, and position is good to about ±300 m. Small ships are invisible.
- **On the map:** these show as purple rings labelled *possible large ship (Sentinel-3)*, in their own layer and with their own image chips.

Each image downloads about 100–150 MB: the 865 nm band, latitude/longitude and quality flags. Turn this off with `--no-s3`, or untick it in the scan form.

## Covering the whole area

By default a scan uses the **most recent usable image of each MGRS tile** within the look-back window (30 days):

- If a tile's newest image has no cloud-free sea, the scan falls back to the next older pass, up to 3 times.
- Some tiles sit at the edge of a satellite swath, where each pass images only part of the tile. For those, older passes are added until the whole tile is covered.
- `--all-passes` processes every pass instead.

The map shows the **latest image per tile** by default: only results from each tile's most recent analysed image, not every pass ever scanned. Untick it under Filter to see everything.

Each scan also measures how much of the search area Sentinel-2 photographed in the window. The figure appears in the log and as **area imaged** on the map, and the unimaged part is shaded dark.

## Coverage caveat

Sentinel-2 does **not** image the whole open ocean. It images land, coastal water out to about 20 km, enclosed seas and some extra areas. The Strait of Hormuz, the Gulf of Oman and the coastal strips are covered regularly, about every 5 days. Much of the central Arabian Sea is rarely or never imaged. `search` shows what is actually available. On the map, the dark **Not imaged** layer marks sea that had no image to scan, and **Scanned tiles** outlines what has been analysed.

## API

| Endpoint | |
|---|---|
| `GET /api/detections?view=latest\|all&start=&end=&min_length=&max_length=&min_confidence=&stationary=0\|1` | GeoJSON of ships: length/width/heading, bow and stern points, chip URL |
| `GET /api/scenes` | Processed tiles with status and footprint |
| `GET /api/stats` | Counts |
| `GET /api/aoi` | Search area polygon |
| `GET /api/s3/detections?view=latest\|all`, `GET /api/s3/granules` | Sentinel-3 possible large ships (no size) and the images used |
| `GET /api/coverage` | Imaged and not-imaged parts of the area from the last scan's search |
| `POST /api/scan` `{start, end, max_cloud, limit, all_passes, workers, keep_tiles}` / `GET /api/scan` | Start a background scan or check its progress |
| `GET /chips/<scene>/<n>.png` | Ship image chip with ruler |

## Changing the area

Edit `config/aoi.geojson` (lon, lat order) or pass `--aoi other.geojson`. The default polygon traces the hand-drawn region. It can overlap land because land is masked automatically.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

`tests/test_detect.py` checks detection and length accuracy on synthetic ships, and checks for no false alarms on empty sea, land and cloud. `tests/test_pipeline.py` runs the whole chain end to end: a local HTTP server plays the STAC API and the imagery bucket, and the test checks search with pagination, tile download, detection, de-duplication across tiles, chips and the web API.
