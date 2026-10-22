# Ship Tracker: Sentinel-2 ship detection and sizing

This tool finds ships at sea in free **Sentinel-2** satellite imagery and measures each one: length, beam and hull axis. You get the same kind of number as laying the Google Maps ruler bow to stern. It covers seven areas (`config/areas.geojson`), and **you choose which one to analyse** by clicking it on the map. Results are stored in SQLite and shown on a web map, with an image chip of every ship that has a ruler drawn along the hull.

```
STAC search ──► download tiles ──► sea/cloud masks ──► bright-hull detection ──► measure ──► SQLite ──► web map
(Earth Search)  (SCL, B08, TCI)    (SCL + NIR)          (local CFAR, NIR B08)    (profiles)             + chips
```

## Quick start: one command

```
py run.py          # Windows (or double-click run.bat)
python3 run.py     # macOS / Linux
```

The first run creates `.venv` and installs everything. After that it starts the map and opens your browser at http://127.0.0.1:8000. **Nothing is downloaded until you pick an area:**

1. Click an area on the map (the green outlines), or find it in the **Areas** list in the sidebar.
2. Press **Scan this area** (or **Scan** in the list).
3. Each Sentinel-2 tile of that area uses its most recent usable image from the last 30 days. Ships appear on the map as each tile finishes, and the area turns orange and dashed while it is being scanned.

| Group | Area (id) |
|---|---|
| Middle East | Persian Gulf & Strait of Hormuz (`persian-gulf`) |
| | Gulf of Oman & Makran coast (`gulf-of-oman`) |
| | Southern Red Sea & Bab-el-Mandeb (`red-sea-south`) |
| Europe | Dover Strait & Thames (`dover-strait`) |
| | Baltic: Danish straits to St Petersburg (`baltic`) |
| | Black Sea north: Crimea, Kerch & Azov (`black-sea-north`) |
| East Asia | Yellow Sea, East China Sea & Taiwan (`east-china-sea`) |

**Your own areas.** To scan somewhere else, or just part of an area, press **＋ Draw your own area** (or the ⬠ button under the zoom buttons). Click points around the sea you want, then double-click, click the first point or press Enter. Give it a name and it appears on the map (light blue) and under **My areas** in the list, ready to scan like the others. Draw it over the sea you care about; land inside it is ignored anyway. ✕ removes an area you drew; ships already found there stay. Your areas are saved in `data/custom_areas.geojson`, so updating the app never touches them. Tiles analysed before you drew an area over them are analysed again for the new part when you scan it.

Each area shows when it was last scanned, the image dates, how much of it Sentinel-2 photographed and how many ships it holds. Pressing **Scan again** later picks up only the new images. One area is scanned at a time. **Look back (days)** and **Max cloud %** in the sidebar apply to the next scan.

**To start fresh**, press **Reset ships & start fresh** under the area list (or run `py run.py --reset`). It deletes every tracked ship, their images and the scan history, so the next scan of an area analyses all of its tiles again. You'll be asked to confirm first, and hand-adjusted measurements are deleted too.

Several tiles are analysed at once, one per spare CPU core (2–4), each in its own process. Images are **streamed** rather than downloaded: only the sea inside the area is read, plus small patches around each ship for the colour checks and image chips (see *What gets downloaded*). `py run.py --scan gulf-of-oman` also starts scanning that area straight away, and `py run.py --areas` lists the ids. Other options are `--days 45`, `--max-cloud 50`, `--workers 4`, `--all-passes` and `--keep-tiles` (download and keep the whole image files); they also apply to scans started from the map.

## Manual setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# the areas you can scan
.venv/bin/python -m shiptracker areas

# list available imagery over one area for the last 5 days (no download)
.venv/bin/python -m shiptracker search dover-strait --days 5

# download tiles and detect ships (newest 10 tiles in the last 5 days)
.venv/bin/python -m shiptracker scan dover-strait --days 5 --limit 10

# open the map at http://127.0.0.1:8000
.venv/bin/python -m shiptracker serve
```

Scans can also be started from the web page by clicking an area and pressing **Scan**. Progress shows live and ships appear on the map as each tile finishes.

You don't need any accounts or API keys. Imagery comes from the public [Element 84 Earth Search](https://earth-search.aws.element84.com/v1) catalogue (`sentinel-2-c1-l2a` on AWS). Add `--source planetary-computer` to use Microsoft Planetary Computer instead; it signs URLs anonymously.

## Commands

| Command | What it does |
|---|---|
| `areas` | List the area ids |
| `check` | Test step by step that imagery can be found, downloaded and streamed |
| `reset [--yes]` | Delete all tracked ships, images and scan history (asks first unless `--yes`) |
| `search AREA [--start D --end D \| --days N] [--max-cloud P]` | List scenes intersecting the area |
| `scan AREA [same] [--limit N] [--all-passes] [--workers N] [--no-rgb] [--keep-tiles] [--reprocess]` | Download, detect and store. By default this is the newest image of every tile, covering the area once. |
| `watch AREA [--every-hours 6] [--days 3]` | Keep scanning an area for new passes |
| `serve [--host --port]` | Web map and JSON API |
| `detect B08.tif SCL.tif [--rgb TCI.tif --chips DIR]` | Run detection on GeoTIFFs you already have |

Global options are `--data DIR` (default `./data`), `--areas-file file.geojson` and `--source earth-search|planetary-computer`.

### What gets downloaded

Sentinel-2 images are cloud-optimised GeoTIFFs, so a scan reads just the parts it needs with HTTP range requests instead of downloading whole files. For each tile (an MGRS square about 110 × 110 km):

1. **`SCL`** (20 m scene classification, a few MB) is downloaded first. A tile with no cloud-free sea inside your areas is marked *skipped* and nothing else is read.
2. **`B08`** (10 m near-infrared, 100–200 MB as a file) is where detection runs. It is read in 2 km blocks, and only blocks with clear sea inside your areas are fetched: land, cloud, nodata and anything outside the areas are never downloaded.
3. **`TCI`** (10 m true colour, about 100 MB as a file) is read only in small patches around the ships found, for the colour checks and the image chips. Pass `--no-rgb` to skip it; chips then use the NIR band.

Nothing large is written to disk, so **`data\tiles` stays empty during a scan; that is normal**. The scan log says how much of each image was read, e.g. `streamed 64% of the image (the sea in your areas)`. If scans seem to download nothing, run `py run.py --check`: it tests the catalogue search, a download, streaming and a worker process one by one, and says exactly which step fails, then opens the map as usual. If streaming fails (an unusual proxy or firewall, for example), that tile falls back to downloading whole files, which resume after an interruption and are retried with backoff. `--keep-tiles` always downloads the whole files into `data/tiles/<scene-id>/` and keeps them. A scene that has already been processed is never processed again, so you can re-run `scan` safely.

## How detection and measurement work (`shiptracker/detect.py`, `shiptracker/verify.py`)

0. **Image edges.** Seeds within 300 m of the edge of the image data are ignored, as is anything touching nodata. Swath edges and tile borders produce bright artifacts, and the overlapping neighbour tile sees that strip from its interior.
1. **Sea mask.** Pixels count as sea if SCL calls them water or their NIR reflectance is very dark. Small enclosed non-water blobs, up to ship size, are filled back in, because SCL often labels the ships themselves as cloud or land. Vegetated patches are never filled, so mangrove islets and strips stay land. A 50 m coastal buffer is removed, as are SCL clouds and cloud shadows plus 100 m around them. Cloud blobs the size of a ship are not treated as cloud.
2. **Candidates.** In the 10 m NIR band, open water is near zero and hulls are bright. Each pixel is compared with the mean and standard deviation of the water around it (a 610 m window). It must be at least 5σ brighter and at least 0.025 reflectance brighter. Bright objects are excluded from the background first, using a coarse median, so large ships don't hide themselves. The statistics are then recomputed without first-pass hits, which copes with sun glint gradients.
3. **Hull extraction.** Each candidate is grown to every connected pixel brighter than 25 % of its peak, so the whole hull is captured rather than just the brightest part.
4. **Measurement, first estimate.** PCA gives the hull axis. The along-hull and across-hull brightness profiles give a first length and beam: each end is placed where the profile drops to 30 % of the typical deck level, with a correction for blur.
4b. **Measurement, refined (hull-model fit).** A model hull is fitted to the pixels by robust least squares. The model is a rectangle with a pointed bow, blurred exactly as the 10 m sensor blurs. The fit solves for centre, axis, length, beam and brightness together, which removes the profile method's biases (pointed bows read short, narrow beams read wide). Bright superstructures and hatch covers are down-weighted rather than trusted. Each size comes with a **± uncertainty**: the fit's own statistical error, plus a model-error floor of 4 m + 1.5 % of length for length and 3 m for beam. If a fit fails, the first estimate is kept, with wider error bars. The hull axis is a true-north bearing (0–180°, since bow and stern can't be told apart).
4c. **Wakes.** A ship under way trails a bright wake, often hundreds of metres long, which is bright in the near-infrared too. Counted as hull, it turns a 180 m tanker into a "350 m supertanker". When one end of the hull's brightness profile fades out gradually, the fit also tries a hull-plus-wake model, with a wake behind the stern that fades with distance. The wake is kept only if it explains the pixels at least 40 % better than a plain hull and behaves like a wake: it fades over at least 0.2× the hull length (a short bright blob is the superstructure) and is no more than twice as bright as the deck. Then only the hull is measured, and the popup shows "Under way: wake ≈ … m". As a last check, a 150 m+ object more than 9.5× longer than its width gets a wider ± and lower confidence, since no real ship is that slender. Ships of 300–400 m are still reported: supertankers (~330 m) and the largest container ships (~400 m) do sail through Hormuz.
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

**Accuracy.** `tests/bench_ships.py` renders 200 synthetic ships through the Sentinel-2 10 m blur. They range from 25 to 400 m with random headings and sub-pixel positions, and include pointed or full bows, bright superstructures, hatch patterns, dim hulls and sensor noise. Typical (RMS) errors:

| | length, before → now | beam, before → now |
|---|---|---|
| all ships | 8.3 m → **3.8 m** | 7.3 m → **2.8 m** |
| < 60 m | 3.3 m → 3.8 m | 11.7 m → **4.1 m** |
| 60–150 m | 4.2 m → **3.2 m** (3.9 %) | 7.0 m → **2.9 m** |
| > 150 m | 12.3 m → **4.2 m** (1.5 %) | 1.7 m → 1.1 m |

Moving ships, the same 200 with a wake 0.5–3× their length:

| moving ships | without wake model | now |
|---|---|---|
| found | 175 / 200 | **193 / 200** |
| median length error | 16.7 m | **6.7 m** |
| 90 % of ships within | 109 m | **28 m** |
| reported ≥ 300 m, of which really ≥ 300 m | 25, **8** | 22, **19** |

For stationary ships the ± brackets the true length for 84 % of ships and the true beam for 91 % (deliberately conservative). The hull axis is typically within 0.2°. Real imagery is messier than this: wakes, atmosphere, and boats under 30 m that are only 2–3 pixels long. Expect somewhat larger errors, and treat the ± as a guide. `tests/test_measure_accuracy.py` fails if accuracy regresses.

When the detector changes, tiles analysed by an older version are redone automatically on the next scan. Their old results are replaced.

## Covering the whole area

By default a scan uses the **most recent usable image of each MGRS tile** within the look-back window (30 days):

- If a tile's newest image has no cloud-free sea, the scan falls back to the next older pass, up to 3 times.
- Some tiles sit at the edge of a satellite swath, where each pass images only part of the tile. For those, older passes are added until the whole tile is covered.
- `--all-passes` processes every pass instead.

The map shows the **latest image per tile** by default: only results from each tile's most recent analysed image, not every pass ever scanned. Untick it under Filter to see everything.

Each scan also measures how much of the area Sentinel-2 photographed in the window. The figure appears in the log and in the area's status line, and the unimaged part is shaded dark.

## Coverage caveat

Sentinel-2 does **not** image the whole open ocean. It images land, coastal water out to about 20 km, enclosed seas and some extra areas. All seven areas are mostly coastal or enclosed seas and are covered regularly, about every 5 days. Open water far from land (the middle of the Gulf of Oman mouth, the outer East China Sea) can be imaged less often. `search` shows what is actually available. On the map, the dark **Not imaged** layer marks sea that had no image to scan, and **Scanned tiles** outlines what has been analysed.

## API

| Endpoint | |
|---|---|
| `GET /api/detections?view=latest\|all&start=&end=&min_length=&max_length=&min_confidence=&stationary=0\|1` | GeoJSON of ships: length/width/heading, bow and stern points, chip URL |
| `GET /api/scenes` | Processed tiles with status and footprint |
| `GET /api/stats` | Counts |
| `GET /api/areas` | The areas as GeoJSON, with last-scan status, image window, share imaged, ship count, size and whether you drew it |
| `POST /api/areas` `{name, geometry}` / `DELETE /api/areas/<id>` | Save an area you drew (GeoJSON polygon, up to 3,000,000 km²) / remove one |
| `POST /api/reset` | Delete all tracked ships and start fresh (refused while a scan runs) |
| `GET /api/coverage` | `{area: {...}}` imaged and not-imaged parts of each scanned area |
| `POST /api/scan` `{area, days \| start+end, max_cloud, limit, all_passes, workers, keep_tiles}` / `GET /api/scan` | Start a background scan of one area (required) or check its progress |
| `GET /chips/<scene>/<n>.png` | Ship image chip with ruler |
| `GET /api/detections/<id>/image.png[?dl=1][&clean=1]` | Ship image with coordinates printed on it (and in its metadata); `clean=1` without the ruler |
| `GET /api/detections/<id>/image.tif` | Georeferenced GeoTIFF of the ship chip |
| `GET /api/detections/<id>/chip.png[?clean=1]` | The ship's chip with its current ruler (or without any measurement) |
| `POST /api/detections/<id>/measurement` `{stern: [lat, lon], bow: [lat, lon], width_m}` | Save a hand measurement |
| `POST /api/detections/<id>/measurement/reset` | Back to the automatic measurement |
| `POST /api/download.zip` `{ids: [...]}` | Zip of images, GeoTIFFs, `coordinates.csv`, `ships.kml` |

## How reliable are the measurements?

Every ship has a **reliability** rating in its popup, list entry and downloaded image, with the reason:

- **Good**: a clear hull that the hull model fits, length known to within about 6 %.
- **Fair**: usable, but less certain (e.g. a boat only 4–6 pixels long, or a wake separated from the hull).
- **Rough**: treat as an estimate. Typical reasons are a hull only 2–3 pixels long, the hull model not fitting, or a shape so slender it may include a wake.

**Measurement quality** under *Filter* hides the rough (or the fair) ones. The **±** after each size is a 1-sigma uncertainty, tested to be honest: on the hard benchmark 87 % of true lengths and 79 % of true beams fall inside it (a 1-sigma range should hold at least 68 %). The beam is the less certain of the two. Dark hull paint next to a bright deck looks like water in the near-infrared, so a beam can read narrower than it really is, never wider.

These numbers come from synthetic ships, not real ones with known sizes. Besides the standard benchmark, `tests/test_measure_accuracy.py` runs a **hard benchmark** of 200 ships:

- image sharpness varying around Sentinel-2's nominal value;
- dark hull sides;
- superstructures up to 3× brighter than the deck;
- sun-glint gradients and noisier water;
- a quarter of the ships with another ship close alongside.

It finds 189/200, with 1 gross length error (more than 15 % off) and a big-ship length RMSE of 4.4 m. Before the fixes it found 177, with 6 gross errors and a 20 m RMSE. Three things were fixed:

- A bright superstructure made the deck look like the soft edge of a cloud, so the ship was thrown away.
- A ship moored alongside could swallow part of its neighbour's hull.
- The beam ± ignored dark hull sides.

**Hide the measurement on the images.** Untick **show measurements on images** under *Filter* to see every ship image, the opened-images tray and the map without rulers or outlines. Downloads (single images and the zip) then have no ruler either, though the coordinates and sizes are still printed around the image. **Hide measurement** / **Show measurement** in a popup does the same for one ship. Ships found before GeoTIFF chips were kept have no clean copy and always show their ruler.

## Measuring on the map

- **Ruler:** the 📏 button under the zoom buttons. Click points on the map to measure, like the Google Maps ruler. Each point shows the running distance and the panel shows the total. Double-click to finish, Esc to clear. Distances are geodesic, on the WGS84 ellipsoid.
- **Adjust a ship's measurement:** click **Adjust measurement** in a ship's popup. Drag the yellow handles onto the stern and bow, optionally type the beam, and click **Save**. The length updates live as you drag.
  - The ship is then marked *adjusted*, and its ruler turns yellow.
  - Its image is re-drawn from the GeoTIFF with your ruler, for ships detected since GeoTIFFs were added.
  - Downloads state "adjusted by hand" and still show the automatic value.
  - **Reset to automatic** restores the automatic measurement.

## Opening several ships, copying coordinates, downloading images

- **Open as many ships as you like.** Click ships on the map, or in the list. Each opens its own popup, and opening one doesn't close the others. Popups also stay open while a scan refreshes the map.
- **Opened images tray.** Every ship you open also goes into a tray at the bottom of the map, so you can compare them side by side. Click a card's picture to fly back to that ship. The tray has these buttons:
  - **Copy all coordinates:** one line per ship, as `lat, lon<TAB>size<TAB>date`.
  - **Download all (.zip):** every image, the GeoTIFFs, `coordinates.csv` and `ships.kml` (opens in Google Earth).
  - **Close popups**, **Clear** and **Hide**.
- **Copy:** each popup and card shows the coordinates in a box you can select, with a **Copy** button. They're in decimal degrees (`22.96002, 61.04447`), which pastes straight into Google Maps. Popups also have a second Copy button for degrees/minutes/seconds.
- **Download image:** a PNG of the ship with its coordinates printed on it. That covers the centre in decimal and DMS, plus the hull end points, size, date and source image. The coordinates are also stored in the PNG metadata (`Coordinates`, `Latitude`, `Longitude`) and in the file name, e.g. `ship_22.96002N_61.04447E_2026-09-30_382m.png`.
- **GeoTIFF:** a clean, georeferenced copy of the image chip that opens in place in QGIS or Google Earth Pro. It is saved for ships detected from now on. Older detections only have the PNG.

## Changing the areas

Edit `config/areas.geojson` (lon, lat order), or set `SHIPTRACKER_AREAS` / pass `--areas-file other.geojson`. Each feature needs `id`, `name` and `group` properties; the map picks up changes on reload. Polygons can overlap land because land is masked automatically.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

`tests/test_detect.py` checks detection and length accuracy on synthetic ships, and checks for no false alarms on empty sea, land and cloud. `tests/test_pipeline.py` runs the whole chain end to end: a local HTTP server plays the STAC API and the imagery bucket, and the test checks search with pagination, tile download, detection, de-duplication across tiles, chips and the web API.
