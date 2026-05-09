import logging
import time
import threading
import sqlite3
from flask import Flask, jsonify, send_from_directory, request, abort
from functools import wraps
import meshtastic
import meshtastic.serial_interface
from pubsub import pub
from google.protobuf.json_format import MessageToDict
import json
import os

# Create log file before configuring logging
logfile = 'daemon.log'

# Configure Logging
log_level = logging.DEBUG if os.environ.get('MESHTASTIC_DEBUG') == '1' else logging.INFO

logging.basicConfig(
    level=log_level,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(logfile, mode='w'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('daemon')

# Quiet down werkzeug (Flask's server logger) unless in debug mode
if os.environ.get('MESHTASTIC_DEBUG') != '1':
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__, static_folder='static', static_url_path='/static')

# Global constants and state
BROADCAST_ADDRS = ('^all', '^local', '!ffffffff')
LOCAL_ADDRS = frozenset({'127.0.0.1', '::1', '::ffff:127.0.0.1'})

def local_only(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if request.remote_addr not in LOCAL_ADDRS:
            abort(403)
        return f(*args, **kwargs)
    return wrapper


nodes_data = {}
nodes_lock = threading.Lock()
interface = None

# Message/event persistence: a SQLite store keyed by event time. The full event
# JSON is stored in `payload` and replayed verbatim by /api/stream and /api/history,
# while the indexed columns let us answer per-channel and DM backfill queries fast.
MESSAGES_DB = 'messages.db'
MESSAGES_FILE = 'messages.json'  # legacy; migrated on first start with new code
db_lock = threading.Lock()
db_conn = None
STREAM_LIMIT = 500
HISTORY_LIMIT = 500

def init_db():
    global db_conn
    db_conn = sqlite3.connect(MESSAGES_DB, check_same_thread=False)
    db_conn.row_factory = sqlite3.Row
    db_conn.execute("PRAGMA journal_mode=WAL")
    db_conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            time      REAL    NOT NULL,
            type      TEXT    NOT NULL,
            from_id   TEXT,
            to_id     TEXT,
            from_name TEXT,
            channel   INTEGER,
            text      TEXT,
            hop_limit INTEGER,
            payload   TEXT    NOT NULL
        )
    """)
    db_conn.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(time)")
    db_conn.execute("CREATE INDEX IF NOT EXISTS idx_events_channel_time ON events(channel, time)")
    db_conn.execute("CREATE INDEX IF NOT EXISTS idx_events_dm_time ON events(from_id, to_id, time)")
    db_conn.commit()

    # One-time migration from the legacy messages.json.
    if os.path.exists(MESSAGES_FILE):
        try:
            existing = db_conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            with open(MESSAGES_FILE) as f:
                old = json.load(f)
            if existing == 0 and old:
                logger.info(f"Migrating {len(old)} events from {MESSAGES_FILE} into {MESSAGES_DB}")
                for e in old:
                    _insert_event_row(e)
            os.rename(MESSAGES_FILE, MESSAGES_FILE + '.bak')
            logger.info(f"Renamed {MESSAGES_FILE} to {MESSAGES_FILE}.bak")
        except Exception as ex:
            logger.warning(f"Legacy messages.json migration skipped: {ex}")

def _insert_event_row(event):
    db_conn.execute(
        "INSERT INTO events (time, type, from_id, to_id, from_name, channel, text, hop_limit, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.get('time', time.time()),
            event.get('type', 'unknown'),
            event.get('fromId'),
            event.get('toId'),
            event.get('from'),
            event.get('channel'),
            event.get('text'),
            event.get('hopLimit'),
            json.dumps(event),
        ),
    )

def get_local_node_id():
    """Robustly fetch the local node's ID from the interface."""
    if not interface: return None
    if hasattr(interface, 'myId') and interface.myId:
        return interface.myId
    try:
        info = interface.getMyNodeInfo()
        return info.get('user', {}).get('id')
    except:
        return None

def get_display_name(node_id):
    """Resolve a node ID to its LongName, ShortName, or Fallback to ID."""
    if not node_id or not interface: return "Unknown"
    
    # Try the connected radio's cache first
    try:
        if node_id in interface.nodes:
            user = interface.nodes[node_id].get('user', {})
            name = user.get('longName') or user.get('shortName')
            if name: return name
            
        # If it's the local node, getMyUser often has fresher data
        if node_id == get_local_node_id():
            my_user = interface.getMyUser()
            if my_user:
                return getattr(my_user, 'longName', getattr(my_user, 'shortName', node_id))
    except Exception:
        pass
        
    # Final fallback to our own tracked nodes_data
    with nodes_lock:
        if node_id in nodes_data:
            return nodes_data[node_id].get('name', node_id)
            
    return node_id

def proto_to_dict(obj):
    if hasattr(obj, 'DESCRIPTOR'):
        try:
            return MessageToDict(obj)
        except:
            return str(obj)
    elif isinstance(obj, dict):
        return {k: proto_to_dict(v) for k, v in obj.items()}
    elif isinstance(obj, list) or isinstance(obj, tuple):
        return [proto_to_dict(v) for v in obj]
    return obj

def normalize_pos(pos):
    """Normalize a position dict: convert latitudeI/longitudeI (int * 1e7) to float lat/lon."""
    result = dict(pos)
    if 'latitude' not in result and 'latitudeI' in result:
        result['latitude'] = result['latitudeI'] / 1e7
    if 'longitude' not in result and 'longitudeI' in result:
        result['longitude'] = result['longitudeI'] / 1e7
    return result

def add_event(msg_or_dict):
    if isinstance(msg_or_dict, dict):
        event = proto_to_dict(msg_or_dict)
        event['time'] = time.time()
    else:
        event = {'time': time.time(), 'message': str(msg_or_dict), 'type': 'unknown'}
    try:
        with db_lock:
            _insert_event_row(event)
            db_conn.commit()
    except Exception as e:
        logger.error(f"Failed to persist event: {e}")

def init_node_data(interface):
    local_id = get_local_node_id()
    if interface.nodes:
        with nodes_lock:
            for node_id, info in interface.nodes.items():
                user = info.get('user', {})
                pos = normalize_pos(info.get('position', {}))
                snr = info.get('snr', -10)
                
                if 'latitude' in pos and 'longitude' in pos:
                    nodes_data[node_id] = {
                        'id': node_id,
                        'is_local': (node_id == local_id),
                        'name': user.get('longName', user.get('shortName', node_id)),
                        'latitude': pos.get('latitude'),
                        'longitude': pos.get('longitude'),
                        'sats': pos.get('satsInView', 0),
                        'pdop': pos.get('PDOP', pos.get('HDOP', 0)),
                        'snr': snr,
                        'rssi': -100,
                        'source': 'init',
                        'last_updated': time.time()
                    }

def on_receive(packet, interface):
    try:
        from_id = packet.get('fromId')
        rxSnr = packet.get('rxSnr', -10)
        rxRssi = packet.get('rxRssi', -100)
        hopLimit = packet.get('hopLimit')
        local_id = get_local_node_id()
        name = get_display_name(from_id)

        if 'decoded' in packet:
            decoded = packet['decoded']
            portnum = decoded.get('portnum')
            
            # Skip local echo from the radio firmware to avoid duplication
            if from_id == local_id and portnum == 'TEXT_MESSAGE_APP':
                return

            if portnum == 'TEXT_MESSAGE_APP':
                text = decoded.get('text', '')
                if not text and 'payload' in decoded:
                    text = decoded['payload'].decode('utf-8', errors='replace')
                add_event({
                    'type': 'text',
                    'channel': packet.get('channel', 0),
                    'from': name,
                    'fromId': from_id or 'Unknown',
                    'toId': packet.get('toId', 'Unknown'),
                    'text': text,
                    'hopLimit': hopLimit,
                    'message': f"[TEXT MESSAGE] From: {name} -> {text}"
                })
            elif portnum == 'TELEMETRY_APP':
                telemetry = decoded.get('telemetry', {})
                add_event({
                    'type': 'telemetry',
                    'from': name,
                    'fromId': from_id or 'Unknown',
                    'telemetry': telemetry,
                    'message': f"[TELEMETRY] From: {name} -> {telemetry}"
                })
            elif portnum == 'NODEINFO_APP':
                user = decoded.get('user', {})
                add_event({
                    'type': 'nodeinfo',
                    'from': name,
                    'fromId': from_id or 'Unknown',
                    'user': user,
                    'message': f"[NODE INFO] From: {name} -> {user.get('longName')} ({user.get('shortName')})"
                })
            elif portnum == 'POSITION_APP':
                raw_pos = decoded.get('position', {})
                pos = normalize_pos(raw_pos)

                if not from_id:
                    logger.debug(f"Missing fromId in position packet: {pos}")
                else:
                    is_local = (from_id == local_id)
                    logger.debug(f"Position Update: id={from_id}, is_local={is_local}, name={name}")
                    add_event({
                        'type': 'position',
                        'from': name,
                        'fromId': from_id,
                        'pos': pos,
                        'hopLimit': hopLimit,
                        'message': f"[POSITION] From: {name} -> Lat: {pos.get('latitude')}, Lon: {pos.get('longitude')}"
                    })
                    if 'latitude' in pos and 'longitude' in pos:
                        with nodes_lock:
                            nodes_data[from_id] = {
                                'id': from_id,
                                'is_local': is_local,
                                'name': name,
                                'latitude': pos['latitude'],
                                'longitude': pos['longitude'],
                                'sats': pos.get('satsInView', 0),
                                'pdop': pos.get('PDOP', pos.get('HDOP', 0)),
                                'snr': rxSnr,
                                'rssi': rxRssi,
                                'source': 'live',
                                'last_updated': time.time()
                            }
            elif from_id:
                with nodes_lock:
                    if from_id in nodes_data:
                        nodes_data[from_id]['snr'] = rxSnr
                        nodes_data[from_id]['rssi'] = rxRssi
                        nodes_data[from_id]['last_updated'] = time.time()
                
    except Exception as e:
        logger.error(f"Error handling packet: {e}")
                
    except Exception as e:
        print(f"Error handling packet: {e}")

def local_gps_poller():
    while True:
        time.sleep(15)
        if not interface: continue
        try:
            local_id = get_local_node_id()
            if not local_id or not hasattr(interface, 'nodes') or local_id not in interface.nodes:
                continue

            pos = normalize_pos(interface.nodes[local_id].get('position', {}))
            lat, lon = pos.get('latitude'), pos.get('longitude')
            if not lat or not lon: continue

            with nodes_lock:
                existing = nodes_data.get(local_id, {})
                if (existing.get('source') == 'init' or lat != existing.get('latitude') or lon != existing.get('longitude')):
                    nodes_data[local_id] = {
                        **existing,
                        'latitude': lat,
                        'longitude': lon,
                        'sats': pos.get('satsInView', existing.get('sats', 0)),
                        'pdop': pos.get('PDOP', pos.get('HDOP', existing.get('pdop', 0))),
                        'source': 'live',
                        'last_updated': time.time(),
                    }
                    logger.info(f"Local GPS update: {lat:.5f}, {lon:.5f} (sats={pos.get('satsInView', '?')})")
        except Exception as e:
            logger.error(f"GPS poller error: {e}")

def connection_monitor():
    global interface
    while True:
        time.sleep(2) # Faster detection for snappier UI recovery
        is_alive = False
        try:
            if interface and interface.nodes:
                is_alive = True
        except:
            is_alive = False

        if not is_alive:
            logger.warning("Radio connection lost. Attempting to reconnect...")
            if interface:
                try: interface.close()
                except: pass
                interface = None
            connect_radio()

def connect_radio():
    global interface
    logger.info("Connecting to Meshtastic...")
    try:
        port = os.environ.get('MESHTASTIC_PORT')
        if port:
            logger.info(f"Using explicitly selected port: {port}")
            interface = meshtastic.serial_interface.SerialInterface(devPath=port)
        else:
            interface = meshtastic.serial_interface.SerialInterface()
        init_node_data(interface)
        pub.subscribe(on_receive, "meshtastic.receive")
        
        # Start background threads if not already running
        # We use a simple flag check to avoid duplicate threads on reconnect
        if not any(t.name == "gps_poller" for t in threading.enumerate()):
            threading.Thread(target=local_gps_poller, daemon=True, name="gps_poller").start()
        if not any(t.name == "conn_monitor" for t in threading.enumerate()):
            threading.Thread(target=connection_monitor, daemon=True, name="conn_monitor").start()
            
        logger.info(f"Connected! Initialized with {len(nodes_data)} nodes.")
    except Exception as e:
        logger.error(f"Connection failed: {e}")


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/heatmap')
def api_heatmap():
    with nodes_lock:
        data = list(nodes_data.values())
    return jsonify(data)

@app.route('/api/stream')
@local_only
def api_stream():
    since = request.args.get('since', 0, type=float)
    with db_lock:
        rows = db_conn.execute(
            "SELECT payload FROM events WHERE time > ? ORDER BY time LIMIT ?",
            (since, STREAM_LIMIT),
        ).fetchall()
    return jsonify([json.loads(r['payload']) for r in rows])

@app.route('/api/history')
@local_only
def api_history():
    """Backfill text history for a channel or DM thread, oldest-first."""
    channel = request.args.get('channel', type=int)
    dm = request.args.get('dm')
    before = request.args.get('before', type=float, default=time.time())
    limit = min(request.args.get('limit', 100, type=int), HISTORY_LIMIT)

    with db_lock:
        if channel is not None:
            rows = db_conn.execute(
                "SELECT payload FROM events "
                "WHERE type='text' AND channel=? AND time<? "
                "  AND (to_id IS NULL OR to_id IN ('^all','^local','!ffffffff')) "
                "ORDER BY time DESC LIMIT ?",
                (channel, before, limit),
            ).fetchall()
        elif dm:
            local_id = get_local_node_id() or ''
            rows = db_conn.execute(
                "SELECT payload FROM events "
                "WHERE type='text' AND time<? "
                "  AND ((from_id=? AND to_id=?) OR (from_id=? AND to_id=?)) "
                "ORDER BY time DESC LIMIT ?",
                (before, dm, local_id, local_id, dm, limit),
            ).fetchall()
        else:
            return jsonify({'error': 'channel or dm parameter required'}), 400

    return jsonify([json.loads(r['payload']) for r in reversed(rows)])

@app.route('/api/state')
@local_only
def api_state():
    if not interface:
        return jsonify({'error': 'Not connected'}), 500
        
    local_id = get_local_node_id() or "Unknown"
    name = get_display_name(local_id)
    
    # Get official protobuf state
    node_info = {}
    try:
        node_info = interface.getMyNodeInfo() or {}
    except:
        pass

    pos = node_info.get('position', {})
    metrics = node_info.get('deviceMetrics', {})
    user_info = node_info.get('user', {})
    
    # Check our own tracked live data
    with nodes_lock:
        live_gps = nodes_data.get(local_id, {})
    use_live_gps = live_gps.get('source') == 'live'

    # Get Lora Config
    lora_config = {}
    try:
        lora_config = interface.localNode.localConfig.lora
    except:
        pass

    state = {
        'nodes_online': len(nodes_data),
        'local_id': local_id,
        'uptime': metrics.get('uptimeSeconds', 0),
        'battery_voltage': metrics.get('voltage', 0.0),
        'battery_level': metrics.get('batteryLevel', 0),
        'chutil': metrics.get('channelUtilization', 0.0),
        'sats': live_gps.get('sats') if use_live_gps else pos.get('satsInView', 0),
        'pdop': live_gps.get('pdop') if use_live_gps else pos.get('PDOP', pos.get('HDOP', 0)),
        'latitude': live_gps.get('latitude') if use_live_gps else pos.get('latitude', 0.0),
        'longitude': live_gps.get('longitude') if use_live_gps else pos.get('longitude', 0.0),
        'gps_live': use_live_gps,
        'name': name,
        'long_name': user_info.get('longName', name),
        'short_name': user_info.get('shortName', "Unknown"),
        'hop_limit': getattr(lora_config, 'hop_limit', 3), # Expose hop limit
        'server_time': time.time()
    }
    return jsonify(state)

@app.route('/api/nodes', methods=['GET'])
@local_only
def api_nodes():
    if not interface or not interface.nodes:
        return jsonify([])
    
    nodes_list = []
    with nodes_lock:
        for node_id, info in interface.nodes.items():
            user = info.get('user', {})
            # Use the snr from our live tracked data if available, else from the nodeDB
            live_data = nodes_data.get(node_id, {})
            
            nodes_list.append({
                'id': node_id,
                'long_name': user.get('longName', node_id),
                'short_name': user.get('shortName', "??"),
                'last_heard': info.get('lastHeard', 0),
                'snr': live_data.get('snr', info.get('snr', -10)),
                'rssi': live_data.get('rssi', info.get('rssi', -100))
            })
    
    # Sort by last heard (most recently active first)
    nodes_list.sort(key=lambda x: x['last_heard'] or 0, reverse=True)
    return jsonify(nodes_list)

@app.route('/api/config/apply', methods=['POST'])
@local_only
def api_config_apply():
    data = request.json
    if not interface or not interface.localNode:
        return jsonify({'error': 'Radio not connected'}), 500
    
    try:
        if 'long_name' in data or 'short_name' in data:
            interface.localNode.setOwner(
                long_name=data.get('long_name'),
                short_name=data.get('short_name')
            )
            
        if 'hop_limit' in data:
            set_pref(interface.localNode, 'lora.hop_limit', data['hop_limit'])
        
        if 'command' in data:
            cmd = data['command'].strip()
            if cmd:
                parts = cmd.split(maxsplit=1)
                if len(parts) != 2:
                    return jsonify({'error': 'Invalid command format. Use: section.parameter value'}), 400
                
                key, val = parts
                set_pref(interface.localNode, key, val)
        
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

LOCAL_CONFIG_SECTIONS = ('device', 'position', 'power', 'network', 'display',
                        'lora', 'bluetooth', 'security')
MODULE_CONFIG_SECTIONS = ('mqtt', 'serial', 'external_notification', 'store_forward',
                         'range_test', 'telemetry', 'canned_message', 'audio',
                         'remote_hardware', 'neighbor_info', 'ambient_lighting',
                         'detection_sensor', 'paxcounter')

# Wire-type integer → friendly name. Mirrors google.protobuf.descriptor.FieldDescriptor.TYPE_*.
PROTO_TYPE_NAMES = {
    1: 'double', 2: 'float', 3: 'int64', 4: 'uint64',
    5: 'int32', 6: 'fixed64', 7: 'fixed32', 8: 'bool',
    9: 'string', 10: 'group', 11: 'message', 12: 'bytes',
    13: 'uint32', 14: 'enum', 15: 'sfixed32', 16: 'sfixed64',
    17: 'sint32', 18: 'sint64',
}

def _config_section_map(node):
    """Section name → protobuf section message, covering localConfig + moduleConfig."""
    sections = {}
    for name in LOCAL_CONFIG_SECTIONS:
        if hasattr(node.localConfig, name):
            sections[name] = getattr(node.localConfig, name)
    for name in MODULE_CONFIG_SECTIONS:
        if hasattr(node.moduleConfig, name):
            sections[name] = getattr(node.moduleConfig, name)
    return sections

def _ensure_section_loaded(node, section_msg, section_name):
    """If the local cache for this section is empty, ask the radio for it."""
    try:
        if len(section_msg.ListFields()) > 0:
            return
        field_desc = node.localConfig.DESCRIPTOR.fields_by_name.get(section_name) \
                  or node.moduleConfig.DESCRIPTOR.fields_by_name.get(section_name)
        if field_desc:
            logger.info(f"Section {section_name} cache empty; requesting from radio")
            node.requestConfig(field_desc)
            time.sleep(0.5)
    except Exception as e:
        logger.warning(f"requestConfig({section_name}) failed: {e}")

def describe_section(section_msg):
    """Walk a section message's descriptor and emit typed field info + current values."""
    out = []
    for fd in section_msg.DESCRIPTOR.fields:
        type_name = PROTO_TYPE_NAMES.get(fd.type, 'unknown')
        try:
            value = getattr(section_msg, fd.name)
        except AttributeError:
            value = None

        info = {'name': fd.name, 'type': type_name}
        is_repeated = getattr(fd, 'is_repeated', False) or getattr(fd, 'label', None) == 3
        if is_repeated:
            info['type'] = 'repeated_' + type_name
            info['value'] = list(value) if value else []
        elif fd.type == fd.TYPE_ENUM:
            info['enum_values'] = [v.name for v in fd.enum_type.values]
            try:
                info['value'] = fd.enum_type.values_by_number[value].name
            except (KeyError, TypeError):
                info['value'] = value
        elif fd.type == fd.TYPE_BYTES:
            info['value'] = value.hex() if value else ''
        elif fd.type == fd.TYPE_MESSAGE:
            # Nested messages (e.g. position.fixed_position struct) are not editable
            # generically; surface as opaque so the TUI can show them read-only.
            info['type'] = 'message'
            info['skipped'] = True
            info['value'] = None
        else:
            info['value'] = value
        out.append(info)
    return out

@app.route('/api/config', methods=['GET'])
@local_only
def api_config_get():
    if not interface or not interface.localNode:
        return jsonify({'error': 'Radio not connected'}), 500
    node = interface.localNode
    out = {'localConfig': {}, 'moduleConfig': {}, 'user': []}
    sections = _config_section_map(node)
    for name, msg in sections.items():
        _ensure_section_loaded(node, msg, name)
        bucket = 'localConfig' if name in LOCAL_CONFIG_SECTIONS else 'moduleConfig'
        out[bucket][name] = describe_section(msg)
    try:
        user = (interface.getMyNodeInfo() or {}).get('user', {})
        out['user'] = [
            {'name': 'long_name',       'type': 'string', 'value': user.get('longName', '')},
            {'name': 'short_name',      'type': 'string', 'value': user.get('shortName', '')},
            {'name': 'is_licensed',     'type': 'bool',   'value': bool(user.get('isLicensed', False))},
            {'name': 'is_unmessagable', 'type': 'bool',   'value': bool(user.get('isUnmessagable', False))},
        ]
    except Exception:
        pass
    return jsonify(out)

@app.route('/api/config', methods=['POST'])
@local_only
def api_config_post():
    """Apply a batch of field changes to one config section, atomically when supported."""
    data = request.json or {}
    section = data.get('section')
    fields = data.get('fields') or {}
    if not section or not isinstance(fields, dict):
        return jsonify({'error': 'body must be {"section": "...", "fields": {...}}'}), 400
    if not interface or not interface.localNode:
        return jsonify({'error': 'Radio not connected'}), 500
    node = interface.localNode

    try:
        if section == 'user':
            kwargs = {k: fields[k] for k in ('long_name', 'short_name', 'is_licensed', 'is_unmessagable')
                      if k in fields}
            if not kwargs:
                return jsonify({'error': 'no recognized user fields'}), 400
            node.setOwner(**kwargs)
            return jsonify({'status': 'ok'})

        if section not in _config_section_map(node):
            return jsonify({'error': f'unknown section: {section}'}), 400

        in_tx = False
        try:
            node.beginSettingsTransaction()
            in_tx = True
        except Exception as e:
            logger.warning(f"beginSettingsTransaction unavailable: {e}")

        try:
            for fname, val in fields.items():
                set_pref(node, f"{section}.{fname}", val)
        finally:
            if in_tx:
                try:
                    node.commitSettingsTransaction()
                except Exception as e:
                    logger.warning(f"commitSettingsTransaction failed: {e}")
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"config write failed for section={section}: {e}")
        return jsonify({'error': str(e)}), 500

# ----- Channel CRUD + URL share/import -----

CHANNEL_ROLE_NAMES = {0: 'DISABLED', 1: 'PRIMARY', 2: 'SECONDARY'}
CHANNEL_ROLE_VALUES = {v: k for k, v in CHANNEL_ROLE_NAMES.items()}

def _classify_psk(b):
    """Best-effort summary of a PSK byte string for the UI."""
    if not b:
        return 'none'
    if len(b) == 1:
        if b[0] == 0:
            return 'none'
        if b[0] == 1:
            return 'default'
        return f'simple{b[0] - 1}'
    return f'aes{len(b) * 8}'

def _ensure_channels_loaded(node):
    if not node.channels:
        try:
            node.requestChannels()
            time.sleep(0.5)
        except Exception as e:
            logger.warning(f"requestChannels failed: {e}")

def _serialize_channel(ch):
    s = ch.settings
    psk = bytes(s.psk) if s.psk else b''
    return {
        'index':             ch.index,
        'role':              CHANNEL_ROLE_NAMES.get(ch.role, str(ch.role)),
        'name':              s.name or '',
        'psk_hex':           psk.hex(),
        'psk_kind':          _classify_psk(psk),
        'uplink_enabled':    bool(s.uplink_enabled),
        'downlink_enabled':  bool(s.downlink_enabled),
    }

@app.route('/api/channels', methods=['GET'])
@local_only
def api_channels_get():
    if not interface or not interface.localNode:
        return jsonify({'error': 'Radio not connected'}), 500
    node = interface.localNode
    _ensure_channels_loaded(node)
    return jsonify({'channels': [_serialize_channel(c) for c in (node.channels or [])]})

@app.route('/api/channels', methods=['POST'])
@local_only
def api_channels_post():
    """Modify a channel by index. Body: {index, role?, name?, psk?, uplink_enabled?, downlink_enabled?}.

    psk accepts: 'none', 'default', 'random', 'simple<N>' (0-9), or a hex/0x-hex key.
    """
    import meshtastic.util
    data = request.json or {}
    idx = data.get('index')
    if not isinstance(idx, int) or idx < 0:
        return jsonify({'error': 'integer "index" required'}), 400
    if not interface or not interface.localNode:
        return jsonify({'error': 'Radio not connected'}), 500
    node = interface.localNode
    _ensure_channels_loaded(node)

    if not node.channels or idx >= len(node.channels):
        return jsonify({'error': f'channel index {idx} out of range'}), 400
    ch = node.channels[idx]

    try:
        if 'role' in data:
            role = str(data['role']).upper()
            if role not in CHANNEL_ROLE_VALUES:
                return jsonify({'error': f'role must be one of {list(CHANNEL_ROLE_VALUES)}'}), 400
            ch.role = CHANNEL_ROLE_VALUES[role]
        if 'name' in data:
            ch.settings.name = str(data['name'] or '')
        if 'psk' in data:
            psk_in = data['psk']
            if isinstance(psk_in, str):
                ch.settings.psk = meshtastic.util.fromPSK(psk_in)
            elif isinstance(psk_in, list):
                ch.settings.psk = bytes(psk_in)
            else:
                return jsonify({'error': 'psk must be a string or byte list'}), 400
        if 'uplink_enabled' in data:
            ch.settings.uplink_enabled = bool(data['uplink_enabled'])
        if 'downlink_enabled' in data:
            ch.settings.downlink_enabled = bool(data['downlink_enabled'])

        node.writeChannel(idx)
        return jsonify({'status': 'ok', 'channel': _serialize_channel(ch)})
    except Exception as e:
        logger.error(f"channel write failed for idx={idx}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/channels/<int:idx>', methods=['DELETE'])
@local_only
def api_channels_delete(idx):
    if not interface or not interface.localNode:
        return jsonify({'error': 'Radio not connected'}), 500
    try:
        interface.localNode.deleteChannel(idx)
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"channel delete failed for idx={idx}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/channels/url', methods=['GET'])
@local_only
def api_channels_url():
    """Shareable meshtastic.org/e/# URL encoding the current channel set."""
    if not interface or not interface.localNode:
        return jsonify({'error': 'Radio not connected'}), 500
    try:
        include_all = request.args.get('all', 'true').lower() != 'false'
        return jsonify({'url': interface.localNode.getURL(includeAll=include_all)})
    except Exception as e:
        logger.error(f"getURL failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/channels/import', methods=['POST'])
@local_only
def api_channels_import():
    """Apply a meshtastic share URL. mode='replace' (default) overwrites; 'add' merges new channels."""
    data = request.json or {}
    url = (data.get('url') or '').strip()
    mode = (data.get('mode') or 'replace').lower()
    if not url:
        return jsonify({'error': '"url" required'}), 400
    if not interface or not interface.localNode:
        return jsonify({'error': 'Radio not connected'}), 500
    try:
        interface.localNode.setURL(url, addOnly=(mode == 'add'))
        return jsonify({'status': 'ok'})
    except SystemExit as e:
        # The library uses our_exit() for invalid URLs — turn that into a 400.
        return jsonify({'error': str(e) or 'Invalid URL'}), 400
    except Exception as e:
        logger.error(f"setURL failed: {e}")
        return jsonify({'error': str(e)}), 500

def set_pref(node, key, val):
    import meshtastic.util

    # Preferred way: use library helper if available
    try:
        if hasattr(node, 'set_simple_preference'):
            logger.info(f"Using set_simple_preference for {key} = {val}")
            node.set_simple_preference(key, val)
            return True
    except Exception as e:
        logger.warning(f"set_simple_preference failed for {key}: {e}")

    # Fallback: Manual protobuf manipulation
    section_map = _config_section_map(node)

    if '.' not in key:
        raise ValueError("Key must be 'section.parameter'")
    
    section_name, field_name = key.split('.', 1)
    if section_name not in section_map:
        raise ValueError(f"Unknown section: {section_name}")
    
    section = section_map[section_name]
    _ensure_section_loaded(node, section, section_name)

    field_name_snake = meshtastic.util.camel_to_snake(field_name)
    
    # Try both snake and camel case for the attribute
    target_field = None
    if hasattr(section, field_name_snake):
        target_field = field_name_snake
    else:
        field_name_camel = meshtastic.util.snake_to_camel(field_name)
        if hasattr(section, field_name_camel):
            target_field = field_name_camel
    
    if not target_field:
        raise ValueError(f"Field {field_name} not found in {section_name}")

    field_desc = section.DESCRIPTOR.fields_by_name.get(target_field)
    if not field_desc:
        raise ValueError(f"Protobuf metadata not found for {target_field}")

    logger.info(f"Manual fallback config: {section_name}.{target_field} = {val} (type={field_desc.type})")

    # Convert value based on protobuf type
    if field_desc.type in [field_desc.TYPE_INT32, field_desc.TYPE_UINT32, field_desc.TYPE_INT64, field_desc.TYPE_UINT64]:
        val = int(val)
    elif field_desc.type == field_desc.TYPE_BOOL:
        val = str(val).lower() in ['true', '1', 't', 'y', 'yes']
    elif field_desc.type in [field_desc.TYPE_FLOAT, field_desc.TYPE_DOUBLE]:
        val = float(val)
    elif field_desc.type == field_desc.TYPE_ENUM:
        if isinstance(val, str):
            enum_name = val.upper()
            if enum_name in field_desc.enum_type.values_by_name:
                val = field_desc.enum_type.values_by_name[enum_name].number
            else:
                val = int(val)

    setattr(section, target_field, val)
    
    # Persist change
    node.iface.localNode.writeConfig(section_name)
    return True

@app.route('/api/send', methods=['POST'])
@local_only
def api_send():
    data = request.json
    if not interface:
        return jsonify({'error': 'Radio not connected'}), 500
    
    msg = data.get('message')
    dest = data.get('destination')
    try:
        if dest:
            interface.sendText(msg, destinationId=dest)
        else:
            interface.sendText(msg)
            
        local_id = get_local_node_id() or 'Local'
        name = get_display_name(local_id)

        add_event({
            'type': 'text',
            'from': name,
            'fromId': local_id,
            'toId': dest or 'All',
            'text': msg,
            'message': f"[SENT] To: {dest or 'All'} -> {msg}"
        })
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Send failed: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    init_db()
    connect_radio()
    logger.info("Daemon starting on 0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
