import logging
import time
import threading
from collections import deque
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import meshtastic
import meshtastic.serial_interface
from pubsub import pub
from google.protobuf.json_format import MessageToDict
import json
import os

# Create log file before configuring logging
logfile = 'daemon.log'

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(logfile, mode='w'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('daemon')

app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS(app)

# Global constants and state
BROADCAST_ADDRS = ('^all', '^local', '!ffffffff')
nodes_data = {}
nodes_lock = threading.Lock()
events = deque(maxlen=10) # User requested 10 recent messages limit
events_lock = threading.Lock()
MESSAGES_FILE = 'messages.json'
interface = None

def save_events():
    try:
        tmp_file = MESSAGES_FILE + ".tmp"
        with events_lock:
            data = list(events)
        with open(tmp_file, 'w') as f:
            json.dump(data, f)
        os.replace(tmp_file, MESSAGES_FILE)
    except Exception as e:
        logger.error(f"Error saving messages: {e}")

def load_events():
    if not os.path.exists(MESSAGES_FILE):
        return
    try:
        with open(MESSAGES_FILE, 'r') as f:
            saved = json.load(f)
            with events_lock:
                events.clear()
                events.extend(saved)
            logger.info(f"Loaded {len(events)} (out of {len(saved)} on disk) messages into recent history.")
    except Exception as e:
        logger.error(f"Error loading messages: {e}")

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
        clean_dict = proto_to_dict(msg_or_dict)
        clean_dict['time'] = time.time()
        with events_lock:
            events.append(clean_dict)
    else:
        with events_lock:
            events.append({'time': time.time(), 'message': str(msg_or_dict), 'type': 'unknown'})
    save_events()

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
def api_stream():
    since = request.args.get('since', 0, type=float)
    with events_lock:
        new_events = [e for e in events if e.get('time', 0.0) > since]
    return jsonify(new_events)

@app.route('/api/state')
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
        'hop_limit': getattr(lora_config, 'hopLimit', 3), # Expose hop limit
        'server_time': time.time()
    }
    return jsonify(state)

@app.route('/api/nodes', methods=['GET'])
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
    section_map = {
        'device': node.localConfig.device,
        'lora': node.localConfig.lora,
        'network': node.localConfig.network,
        'display': node.localConfig.display,
        'position': node.localConfig.position,
        'power': node.localConfig.power,
        'security': node.localConfig.security,
        'telemetry': node.moduleConfig.telemetry,
        'mqtt': node.moduleConfig.mqtt,
        'serial': node.moduleConfig.serial,
        'external_notification': node.moduleConfig.external_notification,
        'store_forward': node.moduleConfig.store_forward,
        'range_test': node.moduleConfig.range_test,
        'canned_message': node.moduleConfig.canned_message,
        'audio': node.moduleConfig.audio,
        'remote_hardware': node.moduleConfig.remote_hardware,
        'neighbor_info': node.moduleConfig.neighbor_info,
        'ambient_lighting': node.moduleConfig.ambient_lighting,
        'paxcounter': node.moduleConfig.paxcounter,
    }

    if '.' not in key:
        raise ValueError("Key must be 'section.parameter'")
    
    section_name, field_name = key.split('.', 1)
    if section_name not in section_map:
        raise ValueError(f"Unknown section: {section_name}")
    
    section = section_map[section_name]
    
    # CLI sync logic: Request config if we don't have its fields yet
    try:
        if len(section.ListFields()) == 0:
            logger.info(f"Section {section_name} is empty, requesting from radio...")
            field_desc = node.localConfig.DESCRIPTOR.fields_by_name.get(section_name)
            if not field_desc:
                field_desc = node.moduleConfig.DESCRIPTOR.fields_by_name.get(section_name)
            
            if field_desc:
                node.requestConfig(field_desc)
                # Short wait for response
                time.sleep(0.5)
    except Exception as e:
        logger.warning(f"Failed to requestConfig for {section_name}: {e}")

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
    load_events()
    connect_radio()
    logger.info("Daemon starting on 0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
