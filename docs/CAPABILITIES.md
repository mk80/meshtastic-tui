# Meshtastic Capability Gap Analysis

A snapshot of what this project does today against what the upstream `meshtastic` Python library / CLI exposes and what the official Meshtastic Web Client offers, with implementation sketches for the gaps.

## What we do today

**Connection.** Single `SerialInterface` with port auto-detect or env-pinned port. Reconnect monitor on disconnect.

**Receive (subscribed via `meshtastic.receive`).** We parse four portnums: `TEXT_MESSAGE_APP`, `POSITION_APP`, `NODEINFO_APP`, `TELEMETRY_APP`. Everything else is dropped on the floor.

**Send.** `interface.sendText(msg, destinationId=…)`. No `wantAck`, no `replyId`, no `channelIndex`, no `priority`.

**Read.** `interface.nodes`, `getMyNodeInfo`, `localConfig.lora.hop_limit`, owner names.

**Write.** `localNode.setOwner(long_name, short_name)`. Generic protobuf `setattr` + `writeConfig(section)` exists in the daemon (`set_pref`), but only long_name / short_name / hop_limit have UI; everything else requires typing a raw `section.field value` into the CLI textbox in the TUI.

**Persistence.** Last 10 events to `messages.json` (text only), no telemetry history, no node history.

**UI.** TUI: 8 channel tabs (hardcoded names) + dynamic DM tabs, simple node list, config pane with 3 fields + raw command. Heatmap web: SNR-colored glow per node, local GPS, popups, dark map.

---

## What we don't do — by area, with implementation sketches

### Tier 1 — high impact, low/medium cost

**Full config UI (the biggest gap).** We expose 3 of ~22 config sections. The daemon's `set_pref` already handles arbitrary protobuf writes — the gap is purely UI/schema. Sections to surface:

- `localConfig`: device, lora, network, display, position, power, security, bluetooth (we have only lora.hop_limit)
- `moduleConfig`: telemetry, mqtt, serial, external_notification, store_forward, range_test, canned_message, audio, remote_hardware, neighbor_info, ambient_lighting, detection_sensor, paxcounter

*How.* Add `GET /api/config` returning the full localConfig + moduleConfig as JSON (use `MessageToDict`). Add `POST /api/config` that takes a section name + dict of fields and applies them via the existing `set_pref` loop, then calls `writeConfig(section)` once. Web UI gets a Settings page with collapsible sections; field types come from the protobuf descriptors so you can render booleans/ints/enums automatically. The TUI gets a paged "Settings" mode (`S` key, list of sections). Use **`beginSettingsTransaction` / `commitSettingsTransaction`** (`node.py:629`) for multi-field changes so the radio reboots once.

**Channel admin.** No way to add, delete, rename, or rekey channels. Library has it all (`node.py:108–383`):

- `setChannels`, `requestChannels`, `writeChannel`, `deleteChannel`
- `getURL` / `setURL` for QR-shareable channel sets
- PSK control via the `psk` field on each channel

*How.* `GET/POST/DELETE /api/channels` and `GET /api/channels/url` (returns the meshtastic.org/e/# URL + a server-rendered QR). Web UI: Channels page with role/name/PSK fields, "Generate PSK" button, "Share" button that opens the URL+QR. TUI: a Channels tab parallel to Settings.

**Traceroute.** Completely unused. `sendTraceRoute(dest, hopLimit)` (`mesh_interface.py:669`) returns a `RouteDiscovery` with the per-hop SNR list.

*How.* `POST /api/traceroute {destination, hopLimit}` returns `{forward: [{nodeId, snr}], return: [...]}`. Web: a "Trace" button on the node popup → modal with the hop chain. TUI: `T` key in the node selector.

**Admin actions.** Library has `reboot`, `shutdown`, `factoryReset(full)`, `enterDFUMode`, `setTime`, `resetNodeDb`, `getMetadata`, plus node-DB hygiene (`removeNode`, `setFavorite`, `setIgnored`). All are on `interface.localNode`.

*How.* Single `POST /api/admin/{action}` endpoint dispatching to the right method, with confirmation required in the UI for destructive ones. Add `getMetadata()` output (firmware version, hw model) to `/api/state` so we display it.

**Set / clear fixed position.** `node.setFixedPosition(lat, lon, alt)` and `removeFixedPosition`. Useful for any stationary radio without a GPS fix.

*How.* On the heatmap, right-click → "Set this as my fixed position" → `POST /api/position/fixed`. Mirrors the web client's recent feature.

**Persistent message history beyond 10.** Hard cap at `deque(maxlen=10)` is the limiting factor, plus we only save `type=='text'`. Switch to either a higher cap or SQLite (`messages.db`) keyed by time, with a per-channel/DM index. Then `GET /api/stream?since=…` and `GET /api/history?channel=N&before=…&limit=…` for backfill on TUI scrollback.

---

### Tier 2 — meaningful upgrades, more work

**Reliable messaging.** `sendText(..., wantAck=True)` returns a packet ID; the radio publishes acks via `meshtastic.receive.data.ROUTING_APP`. Track packet IDs → display per-message state (Pending / Delivered / Failed) like the web client. Threading via `replyId` is supported but no major client uses it yet.

**Telemetry graphs.** We receive `TELEMETRY_APP` packets but throw them in a 10-deep deque. The library parses `deviceMetrics`, `environmentMetrics`, `powerMetrics`, `airQualityMetrics` into typed objects.

*How.* Persist telemetry to SQLite (one row per packet, indexed by node + time). `GET /api/telemetry/{nodeId}?since=…` returns time-series. Web page adds a Charts tab with line plots (Chart.js or uPlot). TUI keeps the existing live readout. Also expose `--request-telemetry` via `node.requestTelemetry(destNode, telemetryType)` so users can poll a remote node.

**Node management.** `setFavorite` / `setIgnored` / `removeNode` / `resetNodeDb` are all in `node.py`. UI: star and mute icons in the node list, plus a "Forget" action.

**Multi-transport.** We hard-bind to `SerialInterface`. Library has `TCPInterface(hostname=…)` and `BLEInterface(name=…)` with `BLEInterface.scan()`. Useful for ESP32 radios with WiFi or BLE-only setups.

*How.* Daemon takes `MESHTASTIC_TRANSPORT={serial,tcp,ble}` plus `MESHTASTIC_HOST` / `MESHTASTIC_BLE_NAME`. `manager.py start --tcp 192.168.1.10`. Doesn't change the API surface, just the connection.

**Neighbor info.** `NEIGHBORINFO_APP` (portnum 71) carries each node's known neighbors with SNR. We don't subscribe.

*How.* Add a handler that updates a `neighbors[from_id] = [{neighbor_id, snr}]` table. Heatmap draws thin lines between nodes that hear each other (separate Leaflet layer toggle). Mirrors the web client's "Direct/Remote Neighbors" layers.

**Waypoints.** `WAYPOINT_APP` (portnum 8) — drop a named pin on the mesh that everyone sees. Different from a node position.

*How.* `POST /api/waypoint {name, lat, lon, expire, icon}`. Render as a separate marker style on the map. Subscribe to incoming waypoints.

**Hop / hardware info on the map and node list.** The packets already carry `hopLimit`, `hopStart`, hardware model, firmware version. We discard these. A two-line popup addition.

**Position broadcast control.** Position config exposes `position_broadcast_secs`, `smart_broadcast`, `gps_mode`. Often a user's first config tweak. Add to the Settings UI.

---

### Tier 3 — niche / overlap with web client

**PKI admin and remote node admin.** `ensureSessionKey` (`node.py:1052`) + admin keys on the security config let you reach into another node's settings via the admin channel. The web client surfaces this; we'd need a "Connect As Admin" mode. Probably skip unless you fleet-manage.

**OTA firmware update.** `node.startOTA(firmware_file)` works only over TCP/WiFi for ESP32. Web client doesn't even do this. Low priority.

**Remote hardware (GPIO).** `RemoteHardwareClient` for `writeGPIOs` / `readGPIOs` / `watchGPIOs`. Niche — only useful if you actually have radios with the remote_hardware module enabled.

**MQTT bridge config.** `moduleConfig.mqtt` is a normal config section — falls under "Full config UI." No separate work needed.

**Tunnel.** `tunnel.py` provides IP-over-mesh on Linux. Pure novelty unless you have a real use case.

**Logs / packet sniffer.** `meshtastic.log.line` carries raw firmware logs; every received packet has full structure available pre-`on_receive`. A "raw packets" page (web) or pane (TUI) would help debugging. Cheap to add — just stream them to a new endpoint.

**i18n, themes, command palette.** Polish features; defer.

---

## Recommended order if shipping in chunks

1. **Persistent history + full config UI.** Both unblock everyday use and are mostly server-side / forms work.
2. **Channel admin + QR share/import.** Single biggest "I can't do my normal Meshtastic things in your tool" gap.
3. **Traceroute + admin actions + set-fixed-position.** Small, high-visibility additions.
4. **Telemetry persistence + graphs + node management.** Turns the heatmap into an actual dashboard.
5. **Multi-transport + neighbor lines + waypoints.** Polish for serious users.
