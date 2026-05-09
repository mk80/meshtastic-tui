# Meshtastic RF Coverage Heatmap & Universal Radio Daemon

A powerful, full-stack visualization and management tool for your Meshtastic radios. 

This project solves the "USB Port Lock" problem by initializing a background Local API Daemon that maintains a constant connection with your serial radio. It continuously builds a real-time, dark-mode Leaflet.js RF Heatmap while simultaneously allowing you to securely transmit and receive messages from multiple terminal windows!

---

## ⚡ Features
- **Real-Time RF Heatmap**: Automatically plots nodes on a premium dark-mode map, updating signal strength (SNR, RSSI) completely live.
- **Precision Tracking**: Dynamically reads local GPS status, displaying your exact position, precision (PDOP), and satellite lock count on the dashboard.
- **Universal Hardware Daemon**: No more "Address Already in Use" errors. A built-in Flask API securely manages the single Serial USB connection and routes commands from other scripts.
- **Terminal Integration**: Includes `listen.py` and `send.py` clients that allow you to seamlessly broadcast messages and watch real-time telemetry from external terminal windows while the heatmap still runs smoothly.

---

## 🚀 Quick Start Setup

### 1. Requirements and Dependencies
You must have Python 3 installed. This tool connects to a local Meshtastic node via a standard USB connection.

Install the required Python modules from the `heatmap` directory:
```bash
pip install -r heatmap/requirements.txt
```
*(Dependencies include `flask`, `flask-cors`, `pubsub`, and the official `meshtastic` python library)*

### 2. Connect Your Radio & OS Specifics
Ensure your Meshtastic device is plugged in via USB and powered on.

**Windows Users:**
- No special permissions are needed. It will connect via a `COM` port. 
- *Note:* If you intend to use the TUI (`tui.py`), Windows requires `windows-curses`. This should install automatically via the requirements file, but if you get a `no module named '_curses'` error, run: `pip install windows-curses`.
- *Note:* If your computer doesn't recognize the radio, you may need to download the standard **CH340** or **CP210x** serial drivers depending on your specific board. Also, you may need to type `python` instead of `python3` for the commands below.

**macOS Users:**
- Generally works out-of-the-box (`/dev/cu.usbserial` or `/dev/cu.usbmodem`). 
- *Note:* Similar to Windows, if your radio is completely invisible to your Mac, install the macOS **CH340** drivers.

**Linux Users:**
- You must have permission to read/write to serial ports (usually `/dev/ttyACM0` or `/dev/ttyUSB0`). If you get a `Permission denied` error, add your user to the `dialout` group (and `tty` if necessary) by running:
```bash
sudo usermod -aG dialout $USER
sudo usermod -aG tty $USER
```
*(Note: You will need to log out and log back in, or completely restart your computer, for these permission changes to take effect.)*

### 3. Start the Daemon
To boot up the universal background daemon, run the `manager.py` script from the root folder:
```bash
python manager.py start
```
*Wait a few seconds for it to report `Daemon successfully started (PID XXXX)!`*

*Note:* By default, the daemon runs quietly and only logs essential events and errors to `daemon.log`. If you need to troubleshoot issues or want to see the full HTTP request logs, you can start the daemon with the `--debug` flag:
```bash
python manager.py start --debug
```

At this point, the radio is locked by the daemon, the internal cache has been fetched, and data is aggressively tracking.

### 4. View the Heatmap
Open your favorite web browser and navigate to:
**http://localhost:5000**
You will see the dynamic dashboard loading. Leave this running in the background to continuously build coverage bubbles!

---

## 💻 Interacting with the Mesh

Because the daemon is running in the background, you can utilize the included helper scripts interchangeably across as many separate terminal windows as you wish!

### Monitoring & Chatting (TUI)
For the best experience, use the Terminal Dashboard to watch the live feed and chat:
```bash
python tui.py
```

### Sending Messages
Broadcast a message to your default channel:
```bash
python send.py "Hello from my custom daemon!"
```
Or send a direct message to a specific Node ID (`!XXXXXXXX`):
```bash
python send.py "Are you receiving this?" "!1234abcd"
```

---

## 📟 Terminal Dashboard (TUI)

For a powerful, all-in-one console experience, use the built-in Terminal User Interface (TUI). This allows you to monitor your radio's health and chat with the mesh without ever leaving your terminal.

```bash
python tui.py
```

### **TUI Features:**
- **Live Messaging**: Tabbed interface for all 8 channels + dynamic Direct Message (DM) tabs.
- **Unread Indicators**: Alerts you when messages arrive on other channels.
- **Hardware Stats**: Real-time battery voltage, channel utilization, and uptime.
- **GPS Dashboard**: Detailed satellite lock count and precision (PDOP).
- **Device Configuration**: Change your Long/Short names or send raw CLI configuration commands directly from the UI.

### **Keyboard Shortcuts:**
*   `ENTER`: Start typing a message / Send message.
*   `ESC`: Cancel typing / Exit configuration mode.
*   `TAB`: Cycle through Channels and active Direct Message tabs.
*   `C`: Open the **Configuration Pane** (edit names or run CLI commands).
*   `Q`: Quit the TUI.

---

## 🛑 Shutting Down

When you are finished profiling RF coverage, cleanly kill the background daemon to release your USB serial connection:
```bash
python manager.py stop
```

To quickly verify if your background daemon is running and how many nodes it is actively holding in cache, type:
```bash
python manager.py status
```

---

## 🔒 Security Model

The daemon listens on `0.0.0.0:5000` so any device on your LAN can view the heatmap. Only two routes are exposed to the LAN:

- `GET /` — the heatmap UI
- `GET /api/heatmap` — the node data the map renders

Everything else (`/api/send`, `/api/config/apply`, `/api/state`, `/api/stream`, `/api/nodes`) is **loopback-only** and rejects non-`127.0.0.1` peers with `403`. That means:

- A browser on your LAN can see the map but cannot transmit on your radio or change its config.
- The TUI must run on the same machine as the daemon — SSH into the host and launch `tui.py` there. `send.py` likewise.
- A malicious page in any browser on your LAN cannot reach the command endpoints.

If you ever want to expose the heatmap beyond your LAN (e.g. via a tunnel), reverse-proxy only `/` and `/api/heatmap`, not the whole port.

---

## 🌱 Future Enhancements (Roadmap)
- **Breadcrumb Trails**: Currently handles raw snapshots. Future support for local SQLite/JSON caching will allow saving actual driving loops and graphing dense historical RF shadow zones over time!
