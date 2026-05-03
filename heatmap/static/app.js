// Initialize the map, default to a view over the US (will center on data soon)
const map = L.map('map').setView([39.8283, -98.5795], 4);

// Use a dark-mode mapping tile layer for a premium look (CartoDB Dark Matter)
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 20
}).addTo(map);

let markersLayer = L.layerGroup().addTo(map);

// Save map position on user interactions to persist zoom
map.on('moveend', () => {
    localStorage.setItem('mapPos', JSON.stringify({
        lat: map.getCenter().lat,
        lng: map.getCenter().lng,
        zoom: map.getZoom()
    }));
});

let savedPos = null;
try {
    const saved = localStorage.getItem('mapPos');
    if (saved) savedPos = JSON.parse(saved);
} catch (e) {}

let firstLoad = !savedPos;
if (savedPos) {
    map.setView([savedPos.lat, savedPos.lng], savedPos.zoom);
}

// Keep track of active markers so we don't destroy open popups on autorefresh
const activeMarkers = {};

// SVG tower icon for repeaters
const towerSvg = `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" xmlns="http://www.w3.org/2000/svg">
  <path d="M12 2C12 2 8 6 8 8.5C8 9.88 9.12 11 10.5 11H13.5C14.88 11 16 9.88 16 8.5C16 6 12 2 12 2Z" fill="#ff6b6b" opacity="0.3"/>
  <circle cx="12" cy="5" r="2" fill="#ff6b6b"/>
  <path d="M6 8C4.5 9.5 3.5 11.5 3.5 14" stroke="#ff6b6b" stroke-width="1.5" stroke-linecap="round" fill="none"/>
  <path d="M18 8C19.5 9.5 20.5 11.5 20.5 14" stroke="#ff6b6b" stroke-width="1.5" stroke-linecap="round" fill="none"/>
  <path d="M8.5 6.5C7.5 7.5 7 9 7 10.5" stroke="#ff6b6b" stroke-width="1.5" stroke-linecap="round" fill="none"/>
  <path d="M15.5 6.5C16.5 7.5 17 9 17 10.5" stroke="#ff6b6b" stroke-width="1.5" stroke-linecap="round" fill="none"/>
  <line x1="12" y1="7" x2="12" y2="22" stroke="#ff6b6b" stroke-width="2"/>
  <line x1="8" y1="22" x2="16" y2="22" stroke="#ff6b6b" stroke-width="2" stroke-linecap="round"/>
  <line x1="9" y1="15" x2="15" y2="15" stroke="#ff6b6b" stroke-width="1.5" stroke-linecap="round"/>
</svg>`;

// Node type icon definitions
// Types: local, user, repeater, room, sensor, unknown
function getNodeIcon(nodeType, isLocal) {
    if (isLocal) {
        return L.divIcon({
            className: 'node-marker local-node-marker',
            html: '<div class="marker-icon local-icon">📍</div>',
            iconSize: [28, 28],
            iconAnchor: [14, 14]
        });
    }
    if (nodeType === 'repeater') {
        return L.divIcon({
            className: 'node-marker repeater-marker',
            html: `<div class="marker-icon repeater-icon">${towerSvg}</div>`,
            iconSize: [28, 28],
            iconAnchor: [14, 22]
        });
    }
    if (nodeType === 'room') {
        return L.divIcon({
            className: 'node-marker room-marker',
            html: '<div class="marker-icon room-icon">💬</div>',
            iconSize: [24, 24],
            iconAnchor: [12, 12]
        });
    }
    if (nodeType === 'sensor') {
        return L.divIcon({
            className: 'node-marker sensor-marker',
            html: '<div class="marker-icon sensor-icon">🌡️</div>',
            iconSize: [24, 24],
            iconAnchor: [12, 12]
        });
    }
    // Default: user/client
    return L.divIcon({
        className: 'node-marker user-marker',
        html: '<div class="marker-icon user-icon">👤</div>',
        iconSize: [24, 24],
        iconAnchor: [12, 12]
    });
}

// Node type labels for popups
const typeLabels = {
    'local':    '📍 Local Node',
    'user':     '👤 User',
    'repeater': '🗼 Repeater',
    'room':     '💬 Room Server',
    'sensor':   '🌡️ Sensor',
    'unknown':  '❓ Unknown'
};

async function fetchHeatmapData() {
    try {
        const response = await fetch('/api/heatmap');
        const nodes = await response.json();
        
        // Count only nodes with valid positions
        const positionedNodes = nodes.filter(n => n.latitude && n.longitude);
        document.getElementById('node-count').innerText = positionedNodes.length;

        // Count by type
        const repeaterCount = positionedNodes.filter(n => n.node_type === 'repeater').length;
        const userCount = positionedNodes.filter(n => n.node_type === 'user').length;
        const roomCount = positionedNodes.filter(n => n.node_type === 'room').length;
        const sensorCount = positionedNodes.filter(n => n.node_type === 'sensor').length;
        
        document.getElementById('repeater-count').innerText = repeaterCount;
        document.getElementById('user-count').innerText = userCount;
        
        // Show room/sensor counts only if they exist
        const extraEl = document.getElementById('extra-counts');
        if (roomCount > 0 || sensorCount > 0) {
            let extra = [];
            if (roomCount > 0) extra.push(`💬 Rooms: ${roomCount}`);
            if (sensorCount > 0) extra.push(`🌡️ Sensors: ${sensorCount}`);
            extraEl.innerText = extra.join('  |  ');
            extraEl.style.display = 'block';
        } else {
            extraEl.style.display = 'none';
        }

        const latLngs = [];
        let localGpsLocked = false;
        const newKeys = new Set();

        positionedNodes.forEach(node => {
            if (node.is_local) {
                const statusStr = document.getElementById('gps-status');
                statusStr.innerHTML = `<span style="color: #00ffaa;">Locked</span>`;
                localGpsLocked = true;
            }

            const locKey = node.latitude.toFixed(6) + "," + node.longitude.toFixed(6) + "_" + (node.id || '');
            newKeys.add(locKey);
            latLngs.push([node.latitude, node.longitude]);

            // Build popup content
            const tag = node.is_local ? ' <span style="color:#00f2fe; font-size: 0.8em;">(You)</span>' : '';
            const nodeType = node.is_local ? 'local' : (node.node_type || 'unknown');
            const typeLabel = typeLabels[nodeType] || typeLabels['unknown'];
            
            let popupContent = `
                <div>
                    <span class="popup-title">${node.name}${tag}</span>
                    <span class="popup-type">${typeLabel}</span><br/>
                    <b>ID:</b> ${node.id}<br/>
                    <div style="font-size: 0.85em; color: #aaa; margin-top: 6px;">
                        <b>Lat/Lon:</b> ${node.latitude.toFixed(4)}, ${node.longitude.toFixed(4)}
                    </div>
                </div>
            `;

            if (activeMarkers[locKey]) {
                activeMarkers[locKey].setPopupContent(popupContent);
            } else {
                const icon = getNodeIcon(nodeType, node.is_local);
                const marker = L.marker([node.latitude, node.longitude], { icon: icon });
                marker.bindPopup(popupContent, { maxHeight: 300 });
                marker.addTo(markersLayer);
                activeMarkers[locKey] = marker;
            }
        });

        // Clean up markers for nodes no longer present
        Object.keys(activeMarkers).forEach(locKey => {
            if (!newKeys.has(locKey)) {
                markersLayer.removeLayer(activeMarkers[locKey]);
                delete activeMarkers[locKey];
            }
        });

        // Auto-fit on first load
        if (firstLoad && latLngs.length > 0) {
            map.fitBounds(L.latLngBounds(latLngs), { padding: [50, 50], maxZoom: 12 });
            firstLoad = false;
        }

        if (!localGpsLocked) {
            document.getElementById('gps-status').innerHTML = `<span style="color: #ffaa00;">Searching...</span>`;
        }
    } catch (error) {
        console.error("Error fetching heatmap data:", error);
    }
}

// Fetch immediately, then every 5 seconds for "real-time" feel
fetchHeatmapData();
setInterval(fetchHeatmapData, 5000);
