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

// Helper to calculate heat intensity based on SNR.
// Range tuned to real-world Meshtastic reception, not theoretical LoRa limits:
// -22 dB is the practical decode floor; -10 dB is a strong direct neighbor.
// A passing/co-located radio that exceeds -10 dB just clamps to red.
function getHeatIntensity(snr) {
    let minSnr = -22;
    let maxSnr = -10;
    let intensity = (snr - minSnr) / (maxSnr - minSnr);
    return Math.max(0, Math.min(1, intensity));
}

// HSL hue 240 (blue) → 0 (red), matching the legend gradient stops.
function snrToColor(snr) {
    const hue = 240 - getHeatIntensity(snr) * 240;
    return `hsl(${hue}, 100%, 55%)`;
}

// Dedicated pane for the SNR glow blobs so we can blur them
// without affecting tiles, markers, or popups.
map.createPane('snrGlow');
map.getPane('snrGlow').style.filter = 'blur(18px)';
map.getPane('snrGlow').style.zIndex = '405';
map.getPane('snrGlow').style.pointerEvents = 'none';

const snrBlobs = {};

async function fetchHeatmapData() {
    try {
        const response = await fetch('/api/heatmap');
        const nodes = await response.json();
        
        document.getElementById('node-count').innerText = nodes.length;

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

        // Render one SNR-colored, CSS-blurred blob per location group.
        // Color is the strongest SNR among co-located nodes — the "best path here."
        // Done as plain circles in a blurred pane (not leaflet.heat) so neighboring
        // nodes don't sum into false hotspots; each node's SNR is honest.
        const newBlobKeys = new Set();
        Object.entries(groupedNodes).forEach(([locKey, group]) => {
            if (group.has_local) return;
            newBlobKeys.add(locKey);
            const maxSnr = Math.max(...group.nodes.map(n => n.snr));
            const color = snrToColor(maxSnr);
            const radius = 22 + Math.min(group.nodes.length - 1, 4) * 3;

            if (snrBlobs[locKey]) {
                snrBlobs[locKey].setStyle({fillColor: color});
                snrBlobs[locKey].setRadius(radius);
            } else {
                snrBlobs[locKey] = L.circleMarker([group.latitude, group.longitude], {
                    radius: radius,
                    fillColor: color,
                    fillOpacity: 0.75,
                    weight: 0,
                    pane: 'snrGlow',
                    interactive: false
                }).addTo(map);
            }
        });
        Object.keys(snrBlobs).forEach(k => {
            if (!newBlobKeys.has(k)) {
                map.removeLayer(snrBlobs[k]);
                delete snrBlobs[k];
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
                    // Invisible circle marker — exists to host the popup; the heat layer carries the SNR signal.
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

        // Clean up map markers that completely stopped broadcasting coordinates
        Object.keys(activeMarkers).forEach(locKey => {
            if (!newKeys.has(locKey)) {
                markersLayer.removeLayer(activeMarkers[locKey]);
                delete activeMarkers[locKey];
            }
        });

        if (latLngs.length > 0 && firstLoad) {
            // Fit bounds to show all data gracefully on first load
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
