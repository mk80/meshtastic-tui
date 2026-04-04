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

At this point, the radio is locked by the daemon, the internal cache has been fetched, and data is aggressively tracking.

### 4. View the Heatmap
Open your favorite web browser and navigate to:
**http://localhost:5000**
You will see the dynamic dashboard loading. Leave this running in the background to continuously build coverage bubbles!

---

## 💻 Interacting with the Mesh

Because the daemon is running in the background, you can utilize the included helper scripts interchangeably across as many separate terminal windows as you wish!

### Listening to Telemetry & Messages
Watch the live feed of all text messages, node information, and position updates coming across your local device:
```bash
python listen.py
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

## 🌱 Future Enhancements (Roadmap)
- **Breadcrumb Trails**: Currently handles raw snapshots. Future support for local SQLite/JSON caching will allow saving actual driving loops and graphing dense historical RF shadow zones over time!
