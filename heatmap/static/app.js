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

// Helper to calculate heat intensity based on SNR
// Typical LoRa SNR is -20 (terrible) to +10 (excellent)
function getHeatIntensity(snr) {
    // Normalize SNR to a 0.0 - 1.0 range based on -20 to 10 limits
    let minSnr = -20;
    let maxSnr = 10;
    let intensity = (snr - minSnr) / (maxSnr - minSnr);
    // Clamp between 0 and 1
    return Math.max(0, Math.min(1, intensity));
}

async function fetchHeatmapData() {
    try {
        const response = await fetch('/api/heatmap');
        const nodes = await response.json();
        
        document.getElementById('node-count').innerText = nodes.length;

        // Arrays for heat layer and bounding box
        const heatPoints = [];
        const latLngs = [];

        let localGpsLocked = false;
        const groupedNodes = {};

        nodes.forEach(node => {
            if (node.is_local) {
                const statusStr = document.getElementById('gps-status');
                if (node.latitude && node.longitude) {
                    const satText = node.sats ? ` (${node.sats} Sats)` : '';
                    statusStr.innerHTML = `<span style="color: #00ffaa;">Locked${satText}</span>`;
                    localGpsLocked = true;
                }
            }

            if (node.latitude && node.longitude) {
                // Determine heat level
                const intensity = getHeatIntensity(node.snr);
                
                // Add to heat layer (exclude local node from skewing its own heatmap)
                if (!node.is_local) {
                    heatPoints.push([node.latitude, node.longitude, intensity]);
                }
                latLngs.push([node.latitude, node.longitude]);

                const locKey = node.latitude.toFixed(6) + "," + node.longitude.toFixed(6);
                if (!groupedNodes[locKey]) {
                    groupedNodes[locKey] = {
                        latitude: node.latitude,
                        longitude: node.longitude,
                        nodes: [],
                        has_local: false
                    };
                }
                groupedNodes[locKey].nodes.push(node);
                if (node.is_local) {
                    groupedNodes[locKey].has_local = true;
                }
            }
        });

        // Render markers based on grouped locations to solve overlapping nodes
        const newKeys = new Set();
        Object.keys(groupedNodes).forEach(locKey => {
            const group = groupedNodes[locKey];
            newKeys.add(locKey);

            // Create combined tooltip content
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

            // Add Lat/Lon once at the bottom
            popupContent += `
                <div style="font-size: 0.85em; color: #aaa; margin-top: 8px;">
                    <b>Lat/Lon:</b> ${group.latitude.toFixed(4)}, ${group.longitude.toFixed(4)}
                    <br/><span style="color: #ffaa00">Nodes at this location: ${group.nodes.length}</span>
                </div>
            `;

            if (activeMarkers[locKey]) {
                // Instantly update data without destroying marker (preserves the open popup if active)
                activeMarkers[locKey].setPopupContent(popupContent);
            } else {
                let circle;
                if (group.has_local) {
                    // Draw a visible dot for the local radio
                    const icon = L.divIcon({
                        className: 'local-node-marker',
                        iconSize: [16, 16]
                    });
                    circle = L.marker([group.latitude, group.longitude], {icon: icon});
                } else {
                    // Add an invisible circle marker for hovering tooltips for standard nodes
                    circle = L.circleMarker([group.latitude, group.longitude], {
                        radius: 12 + (group.nodes.length * 2), // dynamically size interaction area based on density
                        color: 'transparent',
                        fillColor: 'transparent'
                    });
                }
                
                circle.bindPopup(popupContent, { maxHeight: 300 });
                circle.addTo(markersLayer);
                activeMarkers[locKey] = circle;
            }
        });

        // Clean up map markers that completely stopped broadcasting coordinates
        Object.keys(activeMarkers).forEach(locKey => {
            if (!newKeys.has(locKey)) {
                markersLayer.removeLayer(activeMarkers[locKey]);
                delete activeMarkers[locKey];
            }
        });

        // Update the heatmap layer
        if (heatLayer) {
            map.removeLayer(heatLayer);
        }
        
        if (heatPoints.length > 0) {
            heatLayer = L.heatLayer(heatPoints, {
                radius: 35,
                blur: 25,
                maxZoom: 14,
                gradient: {
                    0.2: 'blue', 
                    0.4: 'cyan', 
                    0.6: 'lime', 
                    0.8: 'yellow', 
                    1.0: 'red'
                }
            }).addTo(map);

            if (firstLoad) {
                // Fit bounds to show all data gracefully
                map.fitBounds(L.latLngBounds(latLngs), { padding: [50, 50], maxZoom: 12 });
                firstLoad = false;
            }
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
