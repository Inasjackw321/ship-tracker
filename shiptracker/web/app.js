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
    </table></div>`;
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
    rulerLayer.addLayer(line);
    for (const pt of [[p.stern_lat, p.stern_lon], [p.bow_lat, p.bow_lon]]) {
      rulerLayer.addLayer(L.circleMarker(pt, { pane: 'ships', radius: 4, color: '#000', weight: 1, fillColor: '#fff', fillOpacity: 1 }));
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
    }).bindPopup(popupHtml(p, lat, lon), { maxWidth: 320 });
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
      f._marker.openPopup();
    };
    list.appendChild(li);
  }
  $('s-shown').textContent = features.length;
  drawRulers();
}

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
    </table></div>`;
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
    }).bindPopup(s3PopupHtml(f.properties, lat, lon), { maxWidth: 360 }));
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
