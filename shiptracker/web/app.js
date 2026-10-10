/* Ship Tracker front end: map of Sentinel-2 ship detections. */
const $ = (id) => document.getElementById(id);

const map = L.map('map', { zoomControl: true }).setView([18.5, 63], 5);
const imagery = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19, attribution: 'Imagery © Esri, Maxar, Earthstar Geographics' }).addTo(map);
const labels = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19 }).addTo(map);

// Ships draw in their own pane above the tile outlines, so tiles never block clicks.
map.createPane('ships').style.zIndex = 650;
map.createPane('tiles').style.zIndex = 390;
map.createPane('areas').style.zIndex = 395; // above tile outlines so areas stay clickable

// The areas you can scan. Click one (on the map or in the sidebar) to analyse it.
let areas = [];          // GeoJSON features from /api/areas
let scanJob = { running: false, area: null };
const areaById = new Map();
const AREA_STYLE = {
  idle: { color: '#39ff5a', weight: 2, fillColor: '#39ff5a', fillOpacity: 0.06 },
  scanning: { color: '#ffb000', weight: 3, dashArray: '8 6', fillColor: '#ffb000', fillOpacity: 0.12 },
  hover: { weight: 3.5, fillOpacity: 0.16 },
};
const areaLayer = L.geoJSON(null, {
  pane: 'areas',
  style: (f) => (f.properties.id === scanJob.area && scanJob.running ? AREA_STYLE.scanning : AREA_STYLE.idle),
  onEachFeature: (f, l) => {
    areaById.set(f.properties.id, l);
    l.bindTooltip(() => `<b>${esc(f.properties.name)}</b><br>${esc(areaStatus(currentArea(f.properties.id)))}<br><i>click to scan</i>`, { sticky: true });
    l.on('mouseover', () => l.setStyle(AREA_STYLE.hover));
    l.on('mouseout', () => areaLayer.resetStyle(l));
    l.on('click', (e) => {
      if (measure.on || editingId != null) return;
      L.DomEvent.stopPropagation(e);
      l.closeTooltip();
      openAreaPopup(f.properties.id, e.latlng);
    });
  },
}).addTo(map);

const gapLayer = L.geoJSON(null, {
  pane: 'tiles',
  style: { color: '#000', weight: 0, fillColor: '#000', fillOpacity: 0.45 },
  onEachFeature: (f, l) => l.bindTooltip('No Sentinel-2 image of this part in the scan window — nothing to scan here', { sticky: true }),
}).addTo(map);
const sceneLayer = L.geoJSON(null, {
  pane: 'tiles',
  style: (f) => ({ color: f.properties.status === 'done' ? '#58a6ff' : '#8b98a5', weight: 1, fillOpacity: 0.03 }),
  onEachFeature: (f, l) => l.bindTooltip(`${f.properties.id}<br>${f.properties.status}: ${f.properties.note || ''}`),
}).addTo(map);
const shipLayer = L.layerGroup().addTo(map);
const rulerLayer = L.layerGroup().addTo(map);
L.control.layers({ 'Satellite': imagery }, { 'Labels': labels, 'Areas': areaLayer,
  'Not imaged': gapLayer, 'Scanned tiles': sceneLayer, 'Ships': shipLayer,
  'Rulers': rulerLayer }).addTo(map);

function colorFor(len) {
  if (len >= 250) return '#ff4d4d';
  if (len >= 150) return '#ff9f1a';
  if (len >= 80) return '#ffd60a';
  return '#4cc9f0';
}
const legend = L.control({ position: 'bottomright' });
legend.onAdd = () => {
  const d = L.DomUtil.create('div', 'legend');
  d.innerHTML = [['#ff4d4d', '≥ 250 m'], ['#ff9f1a', '150–250 m'], ['#ffd60a', '80–150 m'], ['#4cc9f0', '< 80 m']]
    .map(([c, t]) => `<i style="background:${c}"></i>${t}`).join('<br>');
  return d;
};
legend.addTo(map);

const fmtDate = (iso) => iso.replace('T', ' ').slice(0, 16) + ' UTC';
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function dms(v, pos, neg) {
  const h = v >= 0 ? pos : neg;
  const a = Math.abs(v);
  const d = Math.floor(a);
  const m = Math.floor((a - d) * 60);
  const s = (a - d - m / 60) * 3600;
  return `${d}°${String(m).padStart(2, '0')}'${s.toFixed(2).padStart(5, '0')}" ${h}`;
}
const coordText = (lat, lon) => `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
const coordDms = (lat, lon) => `${dms(lat, 'N', 'S')} ${dms(lon, 'E', 'W')}`;
const apiBase = (p) => `/api/detections/${p.id}`;

function coordsHtml(p, lat, lon) {
  const c = coordText(lat, lon);
  const base = apiBase(p);
  return `<div class="coords">
      <input readonly value="${c}" title="Click to select" onclick="this.select()">
      <button class="mini" data-copy="${c}">Copy</button>
    </div>
    <div class="dms"><span>${esc(coordDms(lat, lon))}</span>
      <button class="mini ghost" data-copy="${esc(coordDms(lat, lon))}">Copy</button></div>
    <div class="dl">
      <a class="mini" href="${base}/image.png?dl=1" download title="Image with the coordinates printed on it">Download image</a>
      ${p.has_tif ? `<a class="mini" href="${base}/image.tif" download title="Georeferenced: opens in place in Google Earth / QGIS">GeoTIFF</a>` : ''}
      <a class="mini ghost" href="${base}/image.png" target="_blank" rel="noopener">Open large</a>
    </div>`;
}

function popupHtml(p, lat, lon) {
  return `<div class="popup">
    ${p.chip_url ? `<img src="${esc(p.chip_url)}" alt="ship chip">` : ''}
    <table>
      <tr><td>Length</td><td><b>${lenText(p)}</b></td></tr>
      <tr><td>Beam</td><td>${beamText(p)}</td></tr>
      <tr><td>Measured</td><td>${esc(METHOD[p.method] || '')}${p.adjusted && p.auto
        ? ` <span style="color:#666">(automatic: ${lenText(p.auto)} × ${beamText(p.auto)})</span>` : ''}</td></tr>
      ${p.wake_m ? `<tr><td>Under way</td><td>wake ≈ ${Math.round(p.wake_m)} m behind the stern (not counted in the length)</td></tr>` : ''}
      <tr><td>Hull axis</td><td>${p.heading_deg.toFixed(0)}° / ${((p.heading_deg + 180) % 360).toFixed(0)}°</td></tr>
      <tr><td>Seen</td><td>${esc(fmtDate(p.datetime))}</td></tr>
      <tr><td>Position</td><td>${lat.toFixed(5)}, ${lon.toFixed(5)}</td></tr>
      <tr><td>Confidence</td><td>${(p.confidence * 100).toFixed(0)}%  (SNR ${p.snr})</td></tr>
      ${p.stationary ? '<tr><td>Note</td><td>also seen here on another date (platform / anchored?)</td></tr>' : ''}
      <tr><td>Tile</td><td style="font-size:11px">${esc(p.scene_id)}</td></tr>
    </table>${coordsHtml(p, lat, lon)}
    <div class="dl">
      <button class="mini" data-adjust="${p.id}" title="Drag the ruler ends onto the bow and stern">Adjust measurement</button>
      ${p.adjusted ? `<button class="mini ghost" data-reset="${p.id}">Reset to automatic</button>` : ''}
    </div></div>`;
}

const METHOD = { fit: 'hull-model fit', profile: 'profile estimate (less precise)', manual: 'adjusted by hand' };
const pm = (e) => (e ? ` ± ${Math.round(e)}` : '');
const lenText = (p) => `${p.length_m.toFixed(0)}${pm(p.length_err_m)} m`;
const beamText = (p) => `${p.width_m.toFixed(0)}${pm(p.width_err_m)} m`;
// Geodesic distance on the WGS84 ellipsoid (Vincenty), matching the server's pyproj
// lengths; Leaflet's map.distance uses a sphere and reads ~0.3 % differently.
function geoDist(p1, p2) {
  const a = 6378137, f = 1 / 298.257223563, b = a * (1 - f), rad = Math.PI / 180;
  const L = (p2.lng - p1.lng) * rad;
  const U1 = Math.atan((1 - f) * Math.tan(p1.lat * rad)), U2 = Math.atan((1 - f) * Math.tan(p2.lat * rad));
  const sU1 = Math.sin(U1), cU1 = Math.cos(U1), sU2 = Math.sin(U2), cU2 = Math.cos(U2);
  let lam = L, sinS, cosS, sig, cos2a, cos2sm;
  for (let i = 0; i < 200; i++) {
    const sl = Math.sin(lam), cl = Math.cos(lam);
    sinS = Math.hypot(cU2 * sl, cU1 * sU2 - sU1 * cU2 * cl);
    if (sinS === 0) return 0;
    cosS = sU1 * sU2 + cU1 * cU2 * cl;
    sig = Math.atan2(sinS, cosS);
    const sa = (cU1 * cU2 * sl) / sinS;
    cos2a = 1 - sa * sa;
    cos2sm = cos2a ? cosS - (2 * sU1 * sU2) / cos2a : 0;
    const C = (f / 16) * cos2a * (4 + f * (4 - 3 * cos2a));
    const prev = lam;
    lam = L + (1 - C) * f * sa * (sig + C * sinS * (cos2sm + C * cosS * (-1 + 2 * cos2sm * cos2sm)));
    if (Math.abs(lam - prev) < 1e-12) break;
  }
  const u2 = (cos2a * (a * a - b * b)) / (b * b);
  const A = 1 + (u2 / 16384) * (4096 + u2 * (-768 + u2 * (320 - 175 * u2)));
  const B = (u2 / 1024) * (256 + u2 * (-128 + u2 * (74 - 47 * u2)));
  const dS = B * sinS * (cos2sm + (B / 4) * (cosS * (-1 + 2 * cos2sm * cos2sm)
    - (B / 6) * cos2sm * (-3 + 4 * sinS * sinS) * (-3 + 4 * cos2sm * cos2sm)));
  return b * A * (sig - dS);
}
const fmtDist = (d) => (d < 1000 ? `${d.toFixed(1)} m` : `${(d / 1000).toFixed(3)} km`);

let features = [];
const featuresById = new Map();
let editingId = null;

function drawRulers() {
  rulerLayer.clearLayers();
  if (map.getZoom() < 13) return;
  const bounds = map.getBounds().pad(0.2);
  for (const f of features) {
    const p = f.properties;
    if (p.bow_lat == null || p.id === editingId) continue;
    const [lon, lat] = f.geometry.coordinates;
    if (!bounds.contains([lat, lon])) continue;
    const line = L.polyline([[p.stern_lat, p.stern_lon], [p.bow_lat, p.bow_lon]],
      { pane: 'ships', color: p.adjusted ? '#ffd60a' : '#fff', weight: 3, opacity: 0.95 });
    line.bindTooltip(`${p.length_m.toFixed(0)} m`, { permanent: true, direction: 'right', className: 'ruler-label' });
    // The ruler lies over the ship: clicking it opens the ship like clicking the marker.
    line.on('click', () => openShip(f));
    rulerLayer.addLayer(line);
    for (const pt of [[p.stern_lat, p.stern_lon], [p.bow_lat, p.bow_lon]]) {
      rulerLayer.addLayer(L.circleMarker(pt, { pane: 'ships', interactive: false, radius: 4, color: '#000', weight: 1, fillColor: '#fff', fillOpacity: 1 }));
    }
  }
}
map.on('zoomend moveend', drawRulers);

function render() {
  shipLayer.clearLayers();
  featuresById.clear();
  for (const f of features) featuresById.set(f.properties.id, f);
  const list = $('list');
  list.innerHTML = '';
  const sorted = [...features].sort((a, b) => b.properties.length_m - a.properties.length_m);
  for (const f of features) {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    const m = L.circleMarker([lat, lon], {
      pane: 'ships',
      radius: Math.max(4, Math.min(12, p.length_m / 30)), color: '#000', weight: 1,
      fillColor: colorFor(p.length_m), fillOpacity: 0.9,
    }).on('click', () => openShip(f));
    f._marker = m;
    shipLayer.addLayer(m);
  }
  for (const f of sorted.slice(0, 500)) {
    const p = f.properties;
    const li = document.createElement('li');
    li.innerHTML = `${p.chip_url ? `<img loading="lazy" src="${esc(p.chip_url)}" alt="">` : '<img alt="">'}
      <div><div class="len">${lenText(p)} × ${beamText(p)}${p.adjusted ? '<span class="tag">adjusted</span>' : ''}
      ${p.stationary ? '<span class="tag">stationary</span>' : ''}</div>
      <div class="meta">${esc(fmtDate(p.datetime))}</div>
      <div class="meta">${f.geometry.coordinates[1].toFixed(4)}, ${f.geometry.coordinates[0].toFixed(4)} · conf ${(p.confidence * 100).toFixed(0)}%</div></div>`;
    li.onclick = () => {
      const [lon, lat] = f.geometry.coordinates;
      map.setView([lat, lon], 15);
      openShip(f);
    };
    list.appendChild(li);
  }
  $('s-shown').textContent = features.length;
  drawRulers();
}

// ---- several ships open at once: independent popups + the "Opened images" tray ----
// Popups are their own map layers (not bound to markers), so they stay open while
// the markers are redrawn during a scan, and opening one never closes another.
const openPopups = new Map();
const tray = new Map();
const keyOf = (f) => String(f.properties.id);

function openShip(f) {
  if (measure.on) return; // clicks add ruler points while measuring
  const key = keyOf(f);
  const [lon, lat] = f.geometry.coordinates;
  if (openPopups.has(key)) { addToTray(f); return; }
  addToTray(f); // first, so the tray's height is known for panning
  const trayH = $('tray').hidden ? 0 : $('tray').offsetHeight;
  const pop = L.popup({ autoClose: false, closeOnClick: false, maxWidth: 280, className: 'ship-popup',
    autoPanPaddingTopLeft: [20, 70], autoPanPaddingBottomRight: [20, trayH + 34] })
    .setLatLng([lat, lon])
    .setContent(popupHtml(f.properties, lat, lon));
  pop.on('remove', () => openPopups.delete(key));
  openPopups.set(key, pop);
  pop.openOn(map);
}

function closeAllPopups() {
  for (const pop of [...openPopups.values()]) map.removeLayer(pop);
}

function addToTray(f) {
  tray.set(keyOf(f), f);
  renderTray();
}

function renderTray() {
  const box = $('tray');
  box.hidden = tray.size === 0;
  $('tray-count').textContent = tray.size;
  const cards = $('tray-cards');
  cards.innerHTML = '';
  for (const [key, f] of tray) {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    const c = coordText(lat, lon);
    const title = `${lenText(p)} × ${beamText(p)}${p.adjusted ? ' (adjusted)' : ''}`;
    const base = apiBase(p);
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <button class="x" title="Remove from tray" data-remove="${key}">×</button>
      <img src="${esc(p.chip_url || '')}" alt="" title="Show on map">
      <div class="t">${esc(title)}</div>
      <div class="meta">${esc(fmtDate(p.datetime))}</div>
      <div class="coords"><input readonly value="${c}" title="${c}" onclick="this.select()"></div>
      <div class="dl">
        <button class="mini" data-copy="${c}">Copy</button>
        <a class="mini" href="${base}/image.png?dl=1" download>Image</a>
        ${p.has_tif ? `<a class="mini" href="${base}/image.tif" download>GeoTIFF</a>` : ''}
      </div>`;
    card.querySelector('img').onclick = () => { map.setView([lat, lon], Math.max(map.getZoom(), 14)); openShip(f); };
    cards.appendChild(card);
  }
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  if (btn) {
    const old = btn.textContent;
    btn.textContent = 'Copied';
    setTimeout(() => { btn.textContent = old; }, 1200);
  }
}

// Capture phase: Leaflet stops clicks inside popups from bubbling.
document.addEventListener('click', (e) => {
  const copy = e.target.closest('[data-copy]');
  if (copy) { e.preventDefault(); copyText(copy.dataset.copy, copy); return; }
  const rm = e.target.closest('[data-remove]');
  if (rm) { tray.delete(rm.dataset.remove); renderTray(); return; }
  const adj = e.target.closest('[data-adjust]');
  if (adj) { startAdjust(featuresById.get(Number(adj.dataset.adjust))); return; }
  const rs = e.target.closest('[data-reset]');
  if (rs) { saveMeasurement(Number(rs.dataset.reset), null); return; }
  const sc = e.target.closest('[data-scan-area]');
  if (sc) { e.preventDefault(); e.stopPropagation(); startScan(sc.dataset.scanArea); return; }
  const zm = e.target.closest('[data-zoom-area]');
  if (zm) { zoomArea(zm.dataset.zoomArea); }
}, true);

function trayLines() {
  return [...tray.values()].map((f) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    const what = `${p.length_m.toFixed(0)} m x ${p.width_m.toFixed(0)} m`;
    return `${coordText(lat, lon)}\t${what}\t${fmtDate(p.datetime)}`;
  }).join('\n');
}

async function downloadTrayZip(btn) {
  const body = { ids: [...tray.values()].map((f) => f.properties.id) };
  const old = btn.textContent;
  btn.textContent = 'Preparing…';
  btn.disabled = true;
  try {
    const r = await fetch('/api/download.zip', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(await r.text());
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = `ships_${tray.size}.zip`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (e) { alert('Download failed: ' + e.message); }
  btn.textContent = old;
  btn.disabled = false;
}

$('tray-copy').onclick = (e) => copyText(trayLines(), e.currentTarget);
$('tray-zip').onclick = (e) => downloadTrayZip(e.currentTarget);
$('tray-close').onclick = closeAllPopups;
$('tray-clear').onclick = () => { tray.clear(); renderTray(); };
$('tray-toggle').onclick = () => {
  const body = $('tray-cards');
  body.hidden = !body.hidden;
  $('tray-toggle').textContent = body.hidden ? 'Show' : 'Hide';
};

// ---- Measure tool: click points on the map, like the Google Maps ruler ----
const measure = { on: false, done: false, pts: [], layer: L.layerGroup().addTo(map) };
map.createPane('measure').style.zIndex = 660;

function measureRedraw() {
  measure.layer.clearLayers();
  const pts = measure.pts;
  if (!pts.length) { $('measure-box').hidden = !measure.on; $('measure-total').textContent = '0 m'; return; }
  L.polyline(pts, { pane: 'measure', color: '#000', weight: 6, opacity: 0.5, interactive: false }).addTo(measure.layer);
  L.polyline(pts, { pane: 'measure', color: '#fff', weight: 3, interactive: false }).addTo(measure.layer);
  let total = 0;
  pts.forEach((pt, i) => {
    if (i) total += geoDist(pts[i - 1], pt);
    const m = L.circleMarker(pt, { pane: 'measure', radius: 5, color: '#000', weight: 2, fillColor: '#fff', fillOpacity: 1, interactive: false });
    if (i) m.bindTooltip(fmtDist(total), { permanent: true, direction: 'right', className: 'ruler-label' });
    m.addTo(measure.layer);
  });
  $('measure-box').hidden = false;
  $('measure-total').textContent = fmtDist(total);
  $('measure-help').textContent = measure.done ? 'Finished. Click Measure to start again.'
    : 'Click to add points, double-click to finish, Esc to clear.';
}

function setMeasure(on) {
  measure.on = on;
  measure.done = false;
  measure.pts = [];
  map.getContainer().style.cursor = on ? 'crosshair' : '';
  if (on) map.doubleClickZoom.disable(); else map.doubleClickZoom.enable();
  document.querySelector('.measure-btn')?.classList.toggle('active', on);
  measureRedraw();
  if (!on) $('measure-box').hidden = true;
}

map.on('click', (e) => {
  if (!measure.on || measure.done) return;
  measure.pts.push(e.latlng);
  measureRedraw();
});
map.on('dblclick', () => { if (measure.on) { measure.done = true; measureRedraw(); } });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && measure.on) setMeasure(false);
  if (e.key === 'Escape' && editingId != null) stopAdjust();
});

const MeasureControl = L.Control.extend({
  options: { position: 'topleft' },
  onAdd() {
    const box = L.DomUtil.create('div', 'leaflet-bar');
    const a = L.DomUtil.create('a', 'measure-btn', box);
    a.href = '#';
    a.title = 'Measure a distance';
    a.innerHTML = '&#128207;';
    L.DomEvent.disableClickPropagation(box);
    L.DomEvent.on(a, 'click', (e) => { L.DomEvent.preventDefault(e); setMeasure(!measure.on); });
    return box;
  },
});
new MeasureControl().addTo(map);
$('measure-close').onclick = () => setMeasure(false);

// ---- Adjust a ship's measurement by dragging its hull ends ----
map.createPane('edit').style.zIndex = 680;
const edit = { f: null, layer: L.layerGroup().addTo(map), ends: [] };

function editRedraw() {
  const [a, b] = edit.ends.map((m) => m.getLatLng());
  edit.line.setLatLngs([a, b]);
  $('adjust-length').textContent = fmtDist(geoDist(a, b));
}

function startAdjust(f) {
  if (!f) return;
  if (measure.on) setMeasure(false);
  stopAdjust();
  const p = f.properties;
  const key = keyOf(f);
  if (openPopups.has(key)) map.removeLayer(openPopups.get(key)); // it would cover the handles
  edit.f = f;
  editingId = p.id;
  drawRulers();
  const handle = (ll, label) => L.marker(ll, {
    pane: 'edit', draggable: true, autoPan: true,
    icon: L.divIcon({ className: 'edit-handle', iconSize: [16, 16] }), title: label,
  }).on('drag', editRedraw).addTo(edit.layer);
  edit.line = L.polyline([], { pane: 'edit', color: '#ffd60a', weight: 3, interactive: false }).addTo(edit.layer);
  edit.ends = [handle([p.stern_lat, p.stern_lon], 'stern'), handle([p.bow_lat, p.bow_lon], 'bow')];
  $('adjust-width').value = p.width_m.toFixed(0);
  $('adjust-auto').textContent = p.adjusted && p.auto ? `automatic: ${lenText(p.auto)}` : `automatic: ${lenText(p)}`;
  $('adjust-box').hidden = false;
  editRedraw();
  map.fitBounds(L.latLngBounds(edit.ends.map((m) => m.getLatLng())).pad(1.5), { maxZoom: 17 });
}

function stopAdjust() {
  edit.layer.clearLayers();
  edit.f = null;
  editingId = null;
  $('adjust-box').hidden = true;
  drawRulers();
}

async function saveMeasurement(id, body) {
  const url = `/api/detections/${id}/measurement` + (body ? '' : '/reset');
  try {
    const f = await getJSON(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}) });
    const i = features.findIndex((x) => x.properties.id === id);
    if (i >= 0) features[i] = f;
    const key = keyOf(f);
    if (openPopups.has(key)) map.removeLayer(openPopups.get(key));
    if (tray.has(key)) tray.set(key, f);
    stopAdjust();
    render();
    renderTray();
    openShip(f);
  } catch (e) { alert('Could not save the measurement: ' + e.message); }
}

$('adjust-save').onclick = () => {
  const [a, b] = edit.ends.map((m) => m.getLatLng());
  saveMeasurement(edit.f.properties.id, { stern: [a.lat, a.lng], bow: [b.lat, b.lng], width_m: $('adjust-width').value });
};
$('adjust-cancel').onclick = stopAdjust;

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || r.statusText);
  return body;
}

async function loadDetections() {
  const q = new URLSearchParams();
  if ($('f-start').value) q.set('start', $('f-start').value);
  if ($('f-end').value) q.set('end', $('f-end').value);
  q.set('min_length', $('f-min').value || 0);
  if ($('f-max').value) q.set('max_length', $('f-max').value);
  q.set('min_confidence', $('f-conf').value || 0);
  q.set('stationary', $('f-stat').checked ? '1' : '0');
  q.set('view', $('f-latest').checked ? 'latest' : 'all');
  const fc = await getJSON('/api/detections?' + q);
  features = fc.features;
  render();
}

async function loadStats() {
  const s = await getJSON('/api/stats');
  $('s-ships').textContent = s.detections;
  $('s-scenes').textContent = s.scenes_done;
}

async function loadCoverage() {
  const all = await getJSON('/api/coverage');
  gapLayer.clearLayers();
  for (const c of Object.values(all)) {
    if (c && c.missing) gapLayer.addData({ type: 'Feature', geometry: c.missing, properties: {} });
  }
}

const currentArea = (id) => areas.find((f) => f.properties.id === id)?.properties || {};
const dayText = (t) => new Date(t * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });

function areaStatus(p) {
  if (scanJob.running && scanJob.area === p.id) return scanJob.text || 'scanning…';
  switch (p.status) {
    case 'done': {
      const imaged = p.imaged == null ? '' : `, ${p.imaged > 0 && p.imaged < 0.01 ? '<1' : Math.round(p.imaged * 100)}% imaged`;
      return `scanned ${dayText(p.finished)} (images ${p.start} → ${p.end}${imaged}) · ${p.ships} ships`;
    }
    case 'failed': return `last scan failed${p.error ? ': ' + p.error : ''}`;
    case 'interrupted': return `last scan was interrupted · ${p.ships} ships`;
    default: return p.ships ? `${p.ships} ships` : 'not scanned yet';
  }
}

function openAreaPopup(id, latlng) {
  const p = currentArea(id);
  const layer = areaById.get(id);
  if (!layer) return;
  const busy = scanJob.running;
  const html = `<div class="area-pop"><b>${esc(p.name)}</b>
    <div class="st">${esc(areaStatus(p))}</div>
    <button data-scan-area="${esc(id)}" ${busy ? 'disabled' : ''}>${busy ? (scanJob.area === id ? 'Scanning…' : 'Another scan is running') : (p.status === 'done' ? 'Scan again' : 'Scan this area')}</button>
    <div class="st">Newest image of each tile from the last ${Number($('scan-days').value || 30)} days.</div></div>`;
  L.popup({ autoPan: true }).setLatLng(latlng || layer.getBounds().getCenter()).setContent(html).openOn(map);
}

function zoomArea(id) {
  const layer = areaById.get(id);
  if (layer) map.fitBounds(layer.getBounds(), { padding: [20, 20] });
}

function renderAreas() {
  const ul = $('areas');
  ul.innerHTML = '';
  let group = null;
  let scanned = 0;
  for (const f of areas) {
    const p = f.properties;
    if (p.status === 'done') scanned += 1;
    if (p.group !== group) {
      group = p.group;
      const g = document.createElement('li');
      g.className = 'grp';
      g.textContent = group;
      ul.appendChild(g);
    }
    const li = document.createElement('li');
    const scanningThis = scanJob.running && scanJob.area === p.id;
    li.className = 'area' + (scanningThis ? ' scanning' : '');
    li.dataset.zoomArea = p.id;
    li.title = 'Click to zoom to this area';
    li.innerHTML = `<div class="nm"><b>${esc(p.name)}</b><span>${esc(areaStatus(p))}</span></div>
      <button data-scan-area="${esc(p.id)}" ${scanJob.running ? 'disabled' : ''}>${scanningThis ? '…' : 'Scan'}</button>`;
    ul.appendChild(li);
  }
  $('s-areas').textContent = `${scanned}/${areas.length}`;
  areaLayer.eachLayer((l) => areaLayer.resetStyle(l));
}

async function loadAreas() {
  const fc = await getJSON('/api/areas');
  const first = !areas.length;
  areas = fc.features;
  if (first) {
    areaById.clear();
    areaLayer.clearLayers();
    areaLayer.addData(fc);
  }
  renderAreas();
  return first;
}

async function loadScenes() {
  const scenes = await getJSON('/api/scenes?view=' + ($('f-latest').checked ? 'latest' : 'all'));
  sceneLayer.clearLayers();
  sceneLayer.addData(scenes.filter((s) => s.geometry).map((s) => ({
    type: 'Feature', geometry: s.geometry, properties: s })));
}

let polling = null;
let defaultsSet = false;
async function pollScan() {
  const s = await getJSON('/api/scan');
  if (!defaultsSet && s.defaults) {  // the look-back and cloud limit run.py was started with
    defaultsSet = true;
    $('scan-days').value = s.defaults.days;
    $('scan-cloud').value = s.defaults.max_cloud;
  }
  const running = s.running;
  const r = s.report;
  const area = s.options?.area || null;
  const name = currentArea(area).name || area || '';
  scanJob = {
    running, area,
    text: running && r ? `scanning… ${r.processed + r.skipped + r.failed + r.already_done}/${r.found || '?'} tiles, ${r.ships} ships` : 'scanning…',
  };
  $('scan-state').textContent = running
    ? `${name}: ${scanJob.text}`
    : (s.error ? `error: ${s.error}` : (r && area ? `${name}: finished` : 'Press an area to scan it.'));
  if (r) {
    $('status').textContent = r.log.slice(-12).join('\n');
    $('status').scrollTop = 1e9;
  }
  renderAreas();
  if (running) {
    if (!polling) polling = setInterval(() => pollScan().catch(console.error), 3000);
    loadDetections(); loadStats(); loadScenes(); loadCoverage();
  } else if (polling) {
    clearInterval(polling); polling = null;
    loadDetections(); loadStats(); loadScenes(); loadCoverage(); loadAreas();
  }
}

async function startScan(id) {
  map.closePopup();
  try {
    await getJSON('/api/scan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        area: id,
        days: Number($('scan-days').value || 30),
        max_cloud: Number($('scan-cloud').value || 30),
      }),
    });
    zoomArea(id);
  } catch (e) { $('scan-state').textContent = 'error: ' + e.message; }
  pollScan().catch(console.error);
}

$('f-apply').onclick = () => Promise.all([loadDetections(), loadScenes()]).catch((e) => alert(e.message));
$('f-latest').onchange = $('f-apply').onclick;

(function init() {
  loadAreas().then(() => {
    if (areaLayer.getLayers().length) map.fitBounds(areaLayer.getBounds(), { padding: [20, 20] });
    return pollScan();
  }).catch(console.error);
  loadDetections().catch(console.error);
  loadStats().catch(console.error);
  loadScenes().catch(console.error);
  loadCoverage().catch(console.error);
})();
