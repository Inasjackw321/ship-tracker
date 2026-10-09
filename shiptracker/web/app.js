/* Ship Tracker front end: map of Sentinel-2 ship detections. */
const $ = (id) => document.getElementById(id);

const map = L.map('map', { zoomControl: true }).setView([18.5, 63], 5);
const imagery = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19, attribution: 'Imagery © Esri, Maxar, Earthstar Geographics' }).addTo(map);
const labels = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}',
  { maxZoom: 19 }).addTo(map);

const aoiLayer = L.geoJSON(null, { style: { color: '#39ff5a', weight: 3, fill: false } }).addTo(map);
const priorityLayer = L.geoJSON(null, {
  pane: 'tiles',
  style: { color: '#f8ff4d', weight: 2.5, dashArray: '8 6', fill: false },
  onEachFeature: (f, l) => l.bindTooltip(`Priority ${f.properties.order}: ${f.properties.name} (scanned first)`, { sticky: true }),
});
// Ships draw in their own pane above the tile outlines, so tiles never block clicks.
map.createPane('ships').style.zIndex = 650;
map.createPane('tiles').style.zIndex = 390;

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
const s3Layer = L.layerGroup().addTo(map);
L.control.layers({ 'Satellite': imagery }, { 'Labels': labels, 'Search area': aoiLayer, 'Priority regions': priorityLayer,
  'Not imaged': gapLayer, 'Scanned tiles': sceneLayer, 'Ships (Sentinel-2)': shipLayer,
  'Rulers': rulerLayer, 'Possible large ships (Sentinel-3)': s3Layer }).addTo(map);

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
    .map(([c, t]) => `<i style="background:${c}"></i>${t}`).join('<br>')
    + '<br><i style="background:transparent;border:3px solid #c77dff;box-sizing:border-box"></i>open sea, S3 (no size)';
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
const apiBase = (kind, p) => (kind === 's2' ? `/api/detections/${p.id}` : `/api/s3/detections/${p.id}`);

function coordsHtml(kind, p, lat, lon) {
  const c = coordText(lat, lon);
  const base = apiBase(kind, p);
  return `<div class="coords">
      <input readonly value="${c}" title="Click to select" onclick="this.select()">
      <button class="mini" data-copy="${c}">Copy</button>
    </div>
    <div class="dms"><span>${esc(coordDms(lat, lon))}</span>
      <button class="mini ghost" data-copy="${esc(coordDms(lat, lon))}">Copy</button></div>
    <div class="dl">
      <a class="mini" href="${base}/image.png?dl=1" download title="Image with the coordinates printed on it">Download image</a>
      ${kind === 's2' && p.has_tif ? `<a class="mini" href="${base}/image.tif" download title="Georeferenced: opens in place in Google Earth / QGIS">GeoTIFF</a>` : ''}
      <a class="mini ghost" href="${base}/image.png" target="_blank" rel="noopener">Open large</a>
    </div>`;
}

function popupHtml(p, lat, lon) {
  return `<div class="popup">
    ${p.chip_url ? `<img src="${esc(p.chip_url)}" alt="ship chip">` : ''}
    <table>
      <tr><td>Length</td><td><b>${p.length_m.toFixed(0)} m</b></td></tr>
      <tr><td>Beam</td><td>${p.width_m.toFixed(0)} m</td></tr>
      <tr><td>Hull axis</td><td>${p.heading_deg.toFixed(0)}° / ${((p.heading_deg + 180) % 360).toFixed(0)}°</td></tr>
      <tr><td>Seen</td><td>${esc(fmtDate(p.datetime))}</td></tr>
      <tr><td>Position</td><td>${lat.toFixed(5)}, ${lon.toFixed(5)}</td></tr>
      <tr><td>Confidence</td><td>${(p.confidence * 100).toFixed(0)}%  (SNR ${p.snr})</td></tr>
      ${p.stationary ? '<tr><td>Note</td><td>also seen here on another date (platform / anchored?)</td></tr>' : ''}
      <tr><td>Tile</td><td style="font-size:11px">${esc(p.scene_id)}</td></tr>
    </table>${coordsHtml('s2', p, lat, lon)}</div>`;
}

let features = [];

function drawRulers() {
  rulerLayer.clearLayers();
  if (map.getZoom() < 13) return;
  const bounds = map.getBounds().pad(0.2);
  for (const f of features) {
    const p = f.properties;
    if (p.bow_lat == null) continue;
    const [lon, lat] = f.geometry.coordinates;
    if (!bounds.contains([lat, lon])) continue;
    const line = L.polyline([[p.stern_lat, p.stern_lon], [p.bow_lat, p.bow_lon]],
      { pane: 'ships', color: '#fff', weight: 3, opacity: 0.95 });
    line.bindTooltip(`${p.length_m.toFixed(0)} m`, { permanent: true, direction: 'right', className: 'ruler-label' });
    // The ruler lies over the ship: clicking it opens the ship like clicking the marker.
    line.on('click', () => openShip('s2', f));
    rulerLayer.addLayer(line);
    for (const pt of [[p.stern_lat, p.stern_lon], [p.bow_lat, p.bow_lon]]) {
      rulerLayer.addLayer(L.circleMarker(pt, { pane: 'ships', interactive: false, radius: 4, color: '#000', weight: 1, fillColor: '#fff', fillOpacity: 1 }));
    }
  }
}
map.on('zoomend moveend', drawRulers);

function render() {
  shipLayer.clearLayers();
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
    }).on('click', () => openShip('s2', f));
    f._marker = m;
    shipLayer.addLayer(m);
  }
  for (const f of sorted.slice(0, 500)) {
    const p = f.properties;
    const li = document.createElement('li');
    li.innerHTML = `${p.chip_url ? `<img loading="lazy" src="${esc(p.chip_url)}" alt="">` : '<img alt="">'}
      <div><div class="len">${p.length_m.toFixed(0)} m × ${p.width_m.toFixed(0)} m
      ${p.stationary ? '<span class="tag">stationary</span>' : ''}</div>
      <div class="meta">${esc(fmtDate(p.datetime))}</div>
      <div class="meta">${f.geometry.coordinates[1].toFixed(4)}, ${f.geometry.coordinates[0].toFixed(4)} · conf ${(p.confidence * 100).toFixed(0)}%</div></div>`;
    li.onclick = () => {
      const [lon, lat] = f.geometry.coordinates;
      map.setView([lat, lon], 15);
      openShip('s2', f);
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
const keyOf = (kind, f) => `${kind}:${f.properties.id}`;

function openShip(kind, f) {
  const key = keyOf(kind, f);
  const [lon, lat] = f.geometry.coordinates;
  addToTray(kind, f);
  if (openPopups.has(key)) return;
  const pop = L.popup({ autoClose: false, closeOnClick: false, maxWidth: 280, className: 'ship-popup' })
    .setLatLng([lat, lon])
    .setContent(kind === 's2' ? popupHtml(f.properties, lat, lon) : s3PopupHtml(f.properties, lat, lon));
  pop.on('remove', () => openPopups.delete(key));
  openPopups.set(key, pop);
  pop.openOn(map);
}

function closeAllPopups() {
  for (const pop of [...openPopups.values()]) map.removeLayer(pop);
}

function addToTray(kind, f) {
  tray.set(keyOf(kind, f), { kind, f });
  renderTray();
}

function renderTray() {
  const box = $('tray');
  box.hidden = tray.size === 0;
  $('tray-count').textContent = tray.size;
  const cards = $('tray-cards');
  cards.innerHTML = '';
  for (const [key, { kind, f }] of tray) {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    const c = coordText(lat, lon);
    const title = kind === 's2' ? `${p.length_m.toFixed(0)} m × ${p.width_m.toFixed(0)} m` : 'Possible large ship (S3)';
    const base = apiBase(kind, p);
    const card = document.createElement('div');
    card.className = 'card' + (kind === 's3' ? ' s3' : '');
    card.innerHTML = `
      <button class="x" title="Remove from tray" data-remove="${key}">×</button>
      <img src="${esc(p.chip_url || '')}" alt="" title="Show on map">
      <div class="t">${esc(title)}</div>
      <div class="meta">${esc(fmtDate(p.datetime))}</div>
      <div class="coords"><input readonly value="${c}" title="${c}" onclick="this.select()"></div>
      <div class="dl">
        <button class="mini" data-copy="${c}">Copy</button>
        <a class="mini" href="${base}/image.png?dl=1" download>Image</a>
        ${kind === 's2' && p.has_tif ? `<a class="mini" href="${base}/image.tif" download>GeoTIFF</a>` : ''}
      </div>`;
    card.querySelector('img').onclick = () => { map.setView([lat, lon], Math.max(map.getZoom(), 14)); openShip(kind, f); };
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
  if (rm) { tray.delete(rm.dataset.remove); renderTray(); }
}, true);

function trayLines() {
  return [...tray.values()].map(({ kind, f }) => {
    const p = f.properties;
    const [lon, lat] = f.geometry.coordinates;
    const what = kind === 's2' ? `${p.length_m.toFixed(0)} m x ${p.width_m.toFixed(0)} m` : 'possible large ship (S3, +-300 m)';
    return `${coordText(lat, lon)}\t${what}\t${fmtDate(p.datetime)}`;
  }).join('\n');
}

async function downloadTrayZip(btn) {
  const body = { s2: [], s3: [] };
  for (const { kind, f } of tray.values()) body[kind].push(f.properties.id);
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
  const c = await getJSON('/api/coverage');
  gapLayer.clearLayers();
  if (c.missing) gapLayer.addData({ type: 'Feature', geometry: c.missing, properties: {} });
  $('s-imaged').textContent = c.fraction == null ? '–' : `${Math.round(c.fraction * 100)}%`;
}

function s3PopupHtml(p, lat, lon) {
  return `<div class="popup">
    ${p.chip_url ? `<img src="${esc(p.chip_url)}" alt="Sentinel-3 chip">` : ''}
    <table>
      <tr><td>What</td><td><b>Possible large ship</b> (bright speck)</td></tr>
      <tr><td>Size</td><td>not measurable: Sentinel-3 pixels are 300 m</td></tr>
      <tr><td>Seen</td><td>${esc(fmtDate(p.datetime))}</td></tr>
      <tr><td>Position</td><td>${lat.toFixed(4)}, ${lon.toFixed(4)} (± ~300 m)</td></tr>
      <tr><td>Confidence</td><td>${(p.confidence * 100).toFixed(0)}%  (SNR ${p.snr})</td></tr>
      <tr><td>Image</td><td style="font-size:11px">${esc(p.granule_id)}</td></tr>
    </table>${coordsHtml('s3', p, lat, lon)}</div>`;
}

let s3Features = [];
async function loadS3() {
  const view = $('f-latest').checked ? 'latest' : 'all';
  const fc = await getJSON('/api/s3/detections?view=' + view);
  s3Features = fc.features;
  s3Layer.clearLayers();
  for (const f of s3Features) {
    const [lon, lat] = f.geometry.coordinates;
    s3Layer.addLayer(L.circleMarker([lat, lon], {
      pane: 'ships', radius: 7, color: '#c77dff', weight: 3, fillColor: '#c77dff', fillOpacity: 0.25,
    }).on('click', () => openShip('s3', f)));
  }
  $('s-s3').textContent = s3Features.length;
}

async function loadScenes() {
  const scenes = await getJSON('/api/scenes?view=' + ($('f-latest').checked ? 'latest' : 'all'));
  sceneLayer.clearLayers();
  sceneLayer.addData(scenes.filter((s) => s.geometry).map((s) => ({
    type: 'Feature', geometry: s.geometry, properties: s })));
}

let polling = null;
async function pollScan() {
  const s = await getJSON('/api/scan');
  const running = s.running;
  $('scan-btn').disabled = running;
  const r = s.report;
  $('scan-state').textContent = running
    ? `running… ${r ? `${r.processed + r.skipped + r.failed + r.already_done}/${r.found} tiles, ${r.ships} ships` : ''}`
    : (s.error ? `error: ${s.error}` : (r ? 'finished' : ''));
  if (r) {
    $('status').textContent = r.log.slice(-12).join('\n');
    $('status').scrollTop = 1e9;
  }
  if (running) {
    if (!polling) polling = setInterval(() => pollScan().catch(console.error), 3000);
    loadDetections(); loadStats(); loadScenes(); loadCoverage(); loadS3();
  } else if (polling) {
    clearInterval(polling); polling = null;
    loadDetections(); loadStats(); loadScenes(); loadCoverage(); loadS3();
  }
}

$('scan-btn').onclick = async () => {
  try {
    await getJSON('/api/scan', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start: $('scan-start').value, end: $('scan-end').value,
        max_cloud: Number($('scan-cloud').value || 30),
        limit: $('scan-limit').value ? Number($('scan-limit').value) : null,
        all_passes: $('scan-all').checked,
        sentinel3: $('scan-s3').checked,
        priority_only: $('scan-priority').checked,
      }),
    });
  } catch (e) { $('scan-state').textContent = 'error: ' + e.message; }
  pollScan();
};
$('f-apply').onclick = () => Promise.all([loadDetections(), loadScenes(), loadS3()]).catch((e) => alert(e.message));
$('f-latest').onchange = $('f-apply').onclick;

(function init() {
  const today = new Date();
  const weekAgo = new Date(today - 30 * 864e5);
  $('scan-end').value = today.toISOString().slice(0, 10);
  $('scan-start').value = weekAgo.toISOString().slice(0, 10);
  getJSON('/api/aoi').then((g) => {
    aoiLayer.addData(g);
    return getJSON('/api/priority');
  }).then((fc) => {
    priorityLayer.addData(fc).addTo(map);
    map.fitBounds((fc.features.length ? priorityLayer : aoiLayer).getBounds(), { padding: [20, 20] });
  }).catch(console.error);
  loadDetections().catch(console.error);
  loadStats().catch(console.error);
  loadScenes().catch(console.error);
  loadS3().catch(console.error);
  loadCoverage().catch(console.error);
  pollScan().catch(console.error);
})();
