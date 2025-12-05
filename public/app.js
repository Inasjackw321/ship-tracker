const API_BASE = window.location.origin;

let map;
let markers = new Map();
let polylines = new Map();
let currentShips = [];

// Initialize the map centered on Ireland
function initMap() {
    map = L.map('map').setView([53.4, -8.0], 7);

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '© OpenStreetMap contributors',
        maxZoom: 18
    }).addTo(map);

    // Add Ireland bounds rectangle
    const bounds = [[51.0, -11.0], [55.5, -5.5]];
    L.rectangle(bounds, {
        color: '#4fc3f7',
        weight: 2,
        fill: false,
        dashArray: '5, 5'
    }).addTo(map);
}

// Create ship icon based on type
function getShipIcon(shipType) {
    const colors = {
        'Cargo': '#ff9800',
        'Tanker': '#f44336',
        'Passenger': '#4caf50',
        'Fishing': '#2196f3'
    };

    const color = colors[shipType] || '#666';

    return L.divIcon({
        className: 'ship-marker',
        html: `<div style="background: ${color}; width: 12px; height: 12px; border-radius: 50%; border: 2px solid white; box-shadow: 0 0 4px rgba(0,0,0,0.5);"></div>`,
        iconSize: [16, 16],
        iconAnchor: [8, 8]
    });
}

// Create ship popup content
function createPopupContent(ship) {
    return `
        <div style="font-family: Arial, sans-serif;">
            <h3 style="margin: 0 0 10px 0; color: #333;">${ship.name || 'Unknown'}</h3>
            <p style="margin: 5px 0;"><strong>MMSI:</strong> ${ship.mmsi}</p>
            ${ship.shipType ? `<p style="margin: 5px 0;"><strong>Type:</strong> ${ship.shipType}</p>` : ''}
            ${ship.speed !== undefined ? `<p style="margin: 5px 0;"><strong>Speed:</strong> ${ship.speed.toFixed(1)} knots</p>` : ''}
            ${ship.course !== undefined ? `<p style="margin: 5px 0;"><strong>Course:</strong> ${ship.course.toFixed(0)}°</p>` : ''}
            ${ship.destination ? `<p style="margin: 5px 0;"><strong>Destination:</strong> ${ship.destination}</p>` : ''}
            <p style="margin: 5px 0; font-size: 11px; color: #666;">
                ${new Date(ship.timestamp).toLocaleString()}
            </p>
            <button onclick="showShipHistory('${ship.mmsi}')"
                    style="margin-top: 10px; padding: 5px 10px; background: #4fc3f7; color: white; border: none; border-radius: 3px; cursor: pointer;">
                Show History
            </button>
        </div>
    `;
}

// Update ship markers on map
function updateShipMarkers(ships) {
    // Clear existing markers
    markers.forEach(marker => map.removeLayer(marker));
    markers.clear();

    ships.forEach(ship => {
        if (ship.latitude && ship.longitude) {
            const marker = L.marker([ship.latitude, ship.longitude], {
                icon: getShipIcon(ship.shipType)
            }).addTo(map);

            marker.bindPopup(createPopupContent(ship));
            markers.set(ship.mmsi, marker);
        }
    });
}

// Draw ship history track
function drawShipHistory(positions) {
    // Clear existing polylines
    polylines.forEach(line => map.removeLayer(line));
    polylines.clear();

    if (positions.length === 0) return;

    // Group positions by MMSI
    const shipTracks = new Map();
    positions.forEach(pos => {
        if (!shipTracks.has(pos.mmsi)) {
            shipTracks.set(pos.mmsi, []);
        }
        shipTracks.get(pos.mmsi).push(pos);
    });

    // Draw track for each ship
    shipTracks.forEach((track, mmsi) => {
        if (track.length < 2) return;

        const latLngs = track.map(pos => [pos.latitude, pos.longitude]);
        const polyline = L.polyline(latLngs, {
            color: '#4fc3f7',
            weight: 2,
            opacity: 0.7
        }).addTo(map);

        polylines.set(mmsi, polyline);

        // Add markers for start and end
        const start = track[0];
        const end = track[track.length - 1];

        L.circleMarker([start.latitude, start.longitude], {
            radius: 5,
            color: '#4caf50',
            fillColor: '#4caf50',
            fillOpacity: 0.8
        }).addTo(map).bindPopup(`Start: ${new Date(start.timestamp).toLocaleString()}`);

        L.circleMarker([end.latitude, end.longitude], {
            radius: 5,
            color: '#f44336',
            fillColor: '#f44336',
            fillOpacity: 0.8
        }).addTo(map).bindPopup(`End: ${new Date(end.timestamp).toLocaleString()}`);
    });

    // Fit map to show all tracks
    if (positions.length > 0) {
        const bounds = L.latLngBounds(positions.map(p => [p.latitude, p.longitude]));
        map.fitBounds(bounds, { padding: [50, 50] });
    }
}

// Fetch current ships
async function fetchCurrentShips() {
    try {
        document.getElementById('status').textContent = 'Loading...';
        const response = await fetch(`${API_BASE}/api/ships/current`);
        const data = await response.json();

        currentShips = data.ships;
        updateShipMarkers(data.ships);
        updateStats(data.ships.length);
        updateShipList(data.ships);

        document.getElementById('status').textContent = 'Live';
    } catch (error) {
        console.error('Error fetching current ships:', error);
        document.getElementById('status').textContent = 'Error';
    }
}

// Fetch historical ships
async function fetchHistoricalShips(hours) {
    try {
        document.getElementById('status').textContent = 'Loading history...';
        const endTime = Date.now();
        const startTime = endTime - (hours * 3600000);

        const response = await fetch(`${API_BASE}/api/ships/history?start=${startTime}&end=${endTime}`);
        const data = await response.json();

        drawShipHistory(data.ships);
        updateStats(data.count);
        updateShipList(data.ships);

        document.getElementById('status').textContent = `History (${hours}h)`;
    } catch (error) {
        console.error('Error fetching historical ships:', error);
        document.getElementById('status').textContent = 'Error';
    }
}

// Fetch specific ship history
async function fetchShipHistory(mmsi, hours = 168) {
    try {
        const endTime = Date.now();
        const startTime = endTime - (hours * 3600000);

        const response = await fetch(`${API_BASE}/api/ships/${mmsi}/history?start=${startTime}&end=${endTime}`);
        const data = await response.json();

        // Clear current view
        markers.forEach(marker => map.removeLayer(marker));
        markers.clear();

        drawShipHistory(data.positions);
        updateStats(data.count);

        document.getElementById('status').textContent = `Ship ${mmsi}`;
    } catch (error) {
        console.error('Error fetching ship history:', error);
    }
}

// Update statistics
function updateStats(count) {
    document.getElementById('totalShips').textContent = count;
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
}

// Update ship list in sidebar
function updateShipList(ships) {
    const listEl = document.getElementById('shipList');

    if (ships.length === 0) {
        listEl.innerHTML = '<div class="loading">No ships found</div>';
        return;
    }

    // Get unique ships (latest position for each MMSI)
    const uniqueShips = new Map();
    ships.forEach(ship => {
        if (!uniqueShips.has(ship.mmsi) || ship.timestamp > uniqueShips.get(ship.mmsi).timestamp) {
            uniqueShips.set(ship.mmsi, ship);
        }
    });

    listEl.innerHTML = Array.from(uniqueShips.values())
        .sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0))
        .map(ship => `
            <div class="ship-item" onclick="focusOnShip('${ship.mmsi}')">
                <div class="ship-name">${ship.name || 'Unknown Ship'}</div>
                <div class="ship-info">
                    MMSI: ${ship.mmsi}<br>
                    ${ship.speed !== undefined ? `Speed: ${ship.speed.toFixed(1)} knots<br>` : ''}
                    ${ship.destination ? `Dest: ${ship.destination}` : ''}
                </div>
                ${ship.shipType ? `<span class="ship-type type-${ship.shipType.toLowerCase()}">${ship.shipType}</span>` : ''}
            </div>
        `).join('');
}

// Focus on specific ship
function focusOnShip(mmsi) {
    const marker = markers.get(mmsi);
    if (marker) {
        map.setView(marker.getLatLng(), 10);
        marker.openPopup();
    }
}

// Show ship history (called from popup)
window.showShipHistory = function(mmsi) {
    document.getElementById('selectedShip').value = mmsi;
    fetchShipHistory(mmsi);
};

// Load all ships for selector
async function loadShipSelector() {
    try {
        const response = await fetch(`${API_BASE}/api/ships/list`);
        const data = await response.json();

        const selector = document.getElementById('selectedShip');
        selector.innerHTML = '<option value="">All Ships</option>' +
            data.ships.map(mmsi => `<option value="${mmsi}">Ship ${mmsi}</option>`).join('');
    } catch (error) {
        console.error('Error loading ship list:', error);
    }
}

// Event listeners
document.getElementById('showCurrentBtn').addEventListener('click', () => {
    document.getElementById('timeRange').value = 'current';
    document.getElementById('shipSelector').style.display = 'none';
    polylines.forEach(line => map.removeLayer(line));
    polylines.clear();
    fetchCurrentShips();
});

document.getElementById('refreshBtn').addEventListener('click', () => {
    const timeRange = document.getElementById('timeRange').value;
    if (timeRange === 'current') {
        fetchCurrentShips();
    } else {
        fetchHistoricalShips(parseInt(timeRange));
    }
});

document.getElementById('timeRange').addEventListener('change', (e) => {
    const value = e.target.value;
    if (value === 'current') {
        document.getElementById('shipSelector').style.display = 'none';
        fetchCurrentShips();
    } else {
        document.getElementById('shipSelector').style.display = 'block';
        fetchHistoricalShips(parseInt(value));
    }
});

document.getElementById('selectedShip').addEventListener('change', (e) => {
    const mmsi = e.target.value;
    if (mmsi) {
        const hours = parseInt(document.getElementById('timeRange').value) || 168;
        fetchShipHistory(mmsi, hours);
    } else {
        const hours = parseInt(document.getElementById('timeRange').value);
        fetchHistoricalShips(hours);
    }
});

// Initialize
initMap();
fetchCurrentShips();
loadShipSelector();

// Auto-refresh every 5 minutes
setInterval(() => {
    if (document.getElementById('timeRange').value === 'current') {
        fetchCurrentShips();
    }
}, 300000);
