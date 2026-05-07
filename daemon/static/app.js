// Initialize the map, default to a view over the US (will center on data soon)
const map = L.map('map').setView([39.8283, -98.5795], 4);

// Use a dark-mode mapping tile layer for a premium look (CartoDB Dark Matter)
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 20
}).addTo(map);

let heatLayer = null;
let markersLayer = L.layerGroup().addTo(map);
let currentCore = 'meshtastic';

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

// SVG tower icon for MeshCore repeaters
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

// MeshCore node type icons
function getMeshcoreIcon(nodeType, isLocal) {
    if (isLocal) {
        return L.divIcon({
            className: 'node-marker local-node-marker-emoji',
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
    return L.divIcon({
        className: 'node-marker user-marker',
        html: '<div class="marker-icon user-icon">👤</div>',
        iconSize: [24, 24],
        iconAnchor: [12, 12]
    });
}

const meshcoreTypeLabels = {
    'local':    '📍 Local Node',
    'user':     '👤 User',
    'repeater': '🗼 Repeater',
    'room':     '💬 Room Server',
    'sensor':   '🌡️ Sensor',
    'unknown':  '❓ Unknown'
};

// Helper to calculate heat intensity based on SNR
// Typical LoRa SNR is -20 (terrible) to +10 (excellent)
function getHeatIntensity(snr) {
    let minSnr = -20;
    let maxSnr = 10;
    let intensity = (snr - minSnr) / (maxSnr - minSnr);
    return Math.max(0, Math.min(1, intensity));
}

async function fetchHeatmapData() {
    try {
        const response = await fetch('/api/heatmap');
        const nodes = await response.json();

        // -------------------------------------------------------
        // MESHTASTIC MODE — original SNR heatmap logic
        // -------------------------------------------------------
        if (currentCore === 'meshtastic') {
            document.getElementById('node-count').innerText = nodes.length;

            const heatPoints = [];
            const latLngs = [];
            let localGpsLocked = false;
            const groupedNodes = {};

            nodes.forEach(node => {
                if (node.is_local) {
                    if (node.latitude && node.longitude) {
                        const satText = node.sats ? ` (${node.sats} Sats)` : '';
                        document.getElementById('gps-status').innerHTML =
                            `<span style="color: #00ffaa;">Locked${satText}</span>`;
                        localGpsLocked = true;
                    }
                }

                if (node.latitude && node.longitude) {
                    const intensity = getHeatIntensity(node.snr);

                    if (!node.is_local) {
                        heatPoints.push([node.latitude, node.longitude, intensity]);
                    }
                    latLngs.push([node.latitude, node.longitude]);

                    const locKey = node.latitude.toFixed(6) + "," + node.longitude.toFixed(6);
                    if (!groupedNodes[locKey]) {
                        groupedNodes[locKey] = { latitude: node.latitude, longitude: node.longitude, nodes: [], has_local: false };
                    }
                    groupedNodes[locKey].nodes.push(node);
                    if (node.is_local) groupedNodes[locKey].has_local = true;
                }
            });

            const newKeys = new Set();
            Object.keys(groupedNodes).forEach(locKey => {
                const group = groupedNodes[locKey];
                newKeys.add(locKey);

                let popupContent = group.nodes.map(n => {
                    const satsInfo = n.sats ? `<b>Satellites:</b> ${n.sats}<br/>` : '';
                    const formatPdop = n.pdop ? `<b>Precision (PDOP):</b> ${(n.pdop / 100).toFixed(2)}<br/>` : '';
                    const tag = n.is_local ? ' <span style="color:#00f2fe; font-size: 0.8em;">(You)</span>' : '';
                    return `
                        <div style="margin-bottom: 5px;">
                            <span class="popup-title">${n.name}${tag}</span>
                            <b>ID:</b> ${n.id}<br/>
                            <b>SNR:</b> ${n.snr} dB &nbsp;|&nbsp; <b>RSSI:</b> ${n.rssi !== undefined ? n.rssi : 'N/A'} dBm<br/>
                            ${satsInfo}${formatPdop}
                        </div>
                    `;
                }).join('<hr style="border: 1px solid #333; margin: 8px 0;" />');

                popupContent += `
                    <div style="font-size: 0.85em; color: #aaa; margin-top: 8px;">
                        <b>Lat/Lon:</b> ${group.latitude.toFixed(4)}, ${group.longitude.toFixed(4)}
                        <br/><span style="color: #ffaa00">Nodes at this location: ${group.nodes.length}</span>
                    </div>
                `;

                if (activeMarkers[locKey]) {
                    activeMarkers[locKey].setPopupContent(popupContent);
                } else {
                    let circle;
                    if (group.has_local) {
                        const icon = L.divIcon({ className: 'local-node-marker', iconSize: [16, 16] });
                        circle = L.marker([group.latitude, group.longitude], { icon: icon });
                    } else {
                        circle = L.circleMarker([group.latitude, group.longitude], {
                            radius: 12 + (group.nodes.length * 2),
                            color: 'transparent',
                            fillColor: 'transparent'
                        });
                    }
                    circle.bindPopup(popupContent, { maxHeight: 300 });
                    circle.addTo(markersLayer);
                    activeMarkers[locKey] = circle;
                }
            });

            Object.keys(activeMarkers).forEach(locKey => {
                if (!newKeys.has(locKey)) {
                    markersLayer.removeLayer(activeMarkers[locKey]);
                    delete activeMarkers[locKey];
                }
            });

            if (heatLayer) map.removeLayer(heatLayer);

            if (heatPoints.length > 0) {
                heatLayer = L.heatLayer(heatPoints, {
                    radius: 35, blur: 25, maxZoom: 14,
                    gradient: { 0.2: 'blue', 0.4: 'cyan', 0.6: 'lime', 0.8: 'yellow', 1.0: 'red' }
                }).addTo(map);

                if (firstLoad) {
                    map.fitBounds(L.latLngBounds(latLngs), { padding: [50, 50], maxZoom: 12 });
                    firstLoad = false;
                }
            }

            if (!localGpsLocked) {
                document.getElementById('gps-status').innerHTML = `<span style="color: #ffaa00;">Searching...</span>`;
            }

        // -------------------------------------------------------
        // MESHCORE MODE — categorized node icons, no heat
        // -------------------------------------------------------
        } else {
            const positionedNodes = nodes.filter(n => n.latitude && n.longitude);
            document.getElementById('node-count').innerText = positionedNodes.length;

            const repeaterCount = positionedNodes.filter(n => n.node_type === 'repeater').length;
            const userCount = positionedNodes.filter(n => n.node_type === 'user').length;
            const roomCount = positionedNodes.filter(n => n.node_type === 'room').length;
            const sensorCount = positionedNodes.filter(n => n.node_type === 'sensor').length;

            const repEl = document.getElementById('repeater-count');
            const userEl = document.getElementById('user-count');
            if (repEl) repEl.innerText = repeaterCount;
            if (userEl) userEl.innerText = userCount;

            const extraEl = document.getElementById('extra-counts');
            if (extraEl) {
                if (roomCount > 0 || sensorCount > 0) {
                    let extra = [];
                    if (roomCount > 0) extra.push(`💬 Rooms: ${roomCount}`);
                    if (sensorCount > 0) extra.push(`🌡️ Sensors: ${sensorCount}`);
                    extraEl.innerText = extra.join('  |  ');
                    extraEl.style.display = 'block';
                } else {
                    extraEl.style.display = 'none';
                }
            }

            const latLngs = [];
            let localGpsLocked = false;
            const newKeys = new Set();

            positionedNodes.forEach(node => {
                if (node.is_local) {
                    document.getElementById('gps-status').innerHTML = `<span style="color: #00ffaa;">Locked</span>`;
                    localGpsLocked = true;
                }

                const nodeKey = node.id || (node.latitude.toFixed(6) + "," + node.longitude.toFixed(6));
                newKeys.add(nodeKey);
                latLngs.push([node.latitude, node.longitude]);

                const tag = node.is_local ? ' <span style="color:#00f2fe; font-size: 0.8em;">(You)</span>' : '';
                const nodeType = node.is_local ? 'local' : (node.node_type || 'unknown');
                const typeLabel = meshcoreTypeLabels[nodeType] || meshcoreTypeLabels['unknown'];

                const popupContent = `
                    <div>
                        <span class="popup-title">${node.name}${tag}</span>
                        <span class="popup-type">${typeLabel}</span><br/>
                        <b>ID:</b> ${node.id}
                        <div style="font-size: 0.85em; color: #aaa; margin-top: 6px;">
                            <b>Lat/Lon:</b> ${node.latitude.toFixed(4)}, ${node.longitude.toFixed(4)}
                        </div>
                    </div>
                `;

                if (activeMarkers[nodeKey]) {
                    activeMarkers[nodeKey].setPopupContent(popupContent);
                    activeMarkers[nodeKey].setIcon(getMeshcoreIcon(nodeType, node.is_local));
                } else {
                    const icon = getMeshcoreIcon(nodeType, node.is_local);
                    const marker = L.marker([node.latitude, node.longitude], { icon: icon });
                    marker.bindPopup(popupContent, { maxHeight: 300 });
                    marker.addTo(markersLayer);
                    activeMarkers[nodeKey] = marker;
                }
            });

            Object.keys(activeMarkers).forEach(nodeKey => {
                if (!newKeys.has(nodeKey)) {
                    markersLayer.removeLayer(activeMarkers[nodeKey]);
                    delete activeMarkers[nodeKey];
                }
            });

            if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }

            if (firstLoad && latLngs.length > 0) {
                map.fitBounds(L.latLngBounds(latLngs), { padding: [50, 50], maxZoom: 12 });
                firstLoad = false;
            }

            if (!localGpsLocked) {
                document.getElementById('gps-status').innerHTML = `<span style="color: #ffaa00;">Searching...</span>`;
            }
        }
    } catch (error) {
        console.error("Error fetching heatmap data:", error);
    }
}

fetchHeatmapData();
setInterval(fetchHeatmapData, 5000);

// Detect backend type and configure UI accordingly
fetch('/api/info')
    .then(r => r.json())
    .then(info => {
        currentCore = info.core;
        const names = { 'meshcore': 'MeshCore', 'meshtastic': 'Meshtastic' };
        const label = names[currentCore] || currentCore;

        document.title = `${label} Map`;
        const heading = document.querySelector('#ui-overlay h1');
        if (heading) heading.textContent = `${label} Node Map`;
        const subtitle = document.querySelector('.subtitle');
        if (subtitle) subtitle.textContent = currentCore === 'meshtastic'
            ? 'Real-time signal coverage visualization'
            : `Live from your ${label} Radio`;

        const snrLegend = document.getElementById('snr-legend');
        const nodeLegend = document.getElementById('node-legend');
        const meshcoreStats = document.getElementById('meshcore-stats');

        if (currentCore === 'meshtastic') {
            if (snrLegend) snrLegend.style.display = 'block';
            if (nodeLegend) nodeLegend.style.display = 'none';
            if (meshcoreStats) meshcoreStats.style.display = 'none';
        } else {
            if (snrLegend) snrLegend.style.display = 'none';
            if (nodeLegend) nodeLegend.style.display = 'block';
            if (meshcoreStats) meshcoreStats.style.display = 'block';
        }
    })
    .catch(() => {});
