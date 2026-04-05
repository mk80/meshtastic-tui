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

app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS(app)

# Global dictionaries
nodes_data = {}
events = deque(maxlen=200)
events_lock = threading.Lock()
MESSAGES_FILE = 'messages.json'

def save_events():
    try:
        # Use a temporary file and rename to ensure atomic write
        tmp_file = MESSAGES_FILE + ".tmp"
        with events_lock:
            data = list(events)
        with open(tmp_file, 'w') as f:
            json.dump(data, f)
        os.replace(tmp_file, MESSAGES_FILE)
    except Exception as e:
        print(f"Error saving messages: {e}")

def load_events():
    if os.path.exists(MESSAGES_FILE):
        try:
            with open(MESSAGES_FILE, 'r') as f:
                saved = json.load(f)
                with events_lock:
                    events.clear()
                    events.extend(saved)
                print(f"Loaded {len(saved)} messages from history.")
        except Exception as e:
            print(f"Error loading messages: {e}")

interface = None

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
    local_id = None
    try:
        local_node = interface.getMyNodeInfo()
        if local_node and 'user' in local_node:
            local_id = local_node['user'].get('id')
        if not local_id and hasattr(interface, 'myId'):
            local_id = interface.myId
    except Exception:
        pass

    if interface.nodes:
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
                    'rssi': -100, # default weak rssi
                    'source': 'init', # seeded from cached mesh DB — may be stale
                    'last_updated': time.time()
                }

def on_receive(packet, interface):
    try:
        from_id = packet.get('fromId')
        rxSnr = packet.get('rxSnr', -10)
        rxRssi = packet.get('rxRssi', -100)
        hopLimit = packet.get('hopLimit')
        
        # Consistent local_id detection
        local_id = None
        try:
            if hasattr(interface, 'myId'):
                local_id = interface.myId
            else:
                l_info = interface.getMyNodeInfo() or {}
                local_id = l_info.get('user', {}).get('id')
        except:
            pass

        # NOTE: Do NOT substitute None fromId here globally - only POSITION_APP
        # packets reliably come from the local radio with no fromId. Text/telemetry
        # packets with missing fromId should safely fall back to 'Unknown'.
        display_from_id = from_id or 'Unknown'

        name = display_from_id
        if display_from_id == local_id:
            # For the local node, use our specialized name resolver
            try:
                my_user = interface.getMyUser()
                if my_user:
                    u = proto_to_dict(my_user)
                    name = u.get('longName', u.get('shortName', display_from_id))
            except:
                pass
        
        # Fallback to general nodes list if name is still ID
        if name == display_from_id and hasattr(interface, 'nodes') and display_from_id in interface.nodes:
            user_info = interface.nodes[display_from_id].get('user', {})
            name = user_info.get('longName', user_info.get('shortName', display_from_id))

        if 'decoded' in packet:
            decoded = packet['decoded']
            portnum = decoded.get('portnum')
            
            if portnum == 'TEXT_MESSAGE_APP':
                text = decoded.get('text', '')
                if not text and 'payload' in decoded:
                    text = decoded['payload'].decode('utf-8', errors='replace')
                add_event({
                    'type': 'text',
                    'channel': packet.get('channel', 0),
                    'from': name,
                    'fromId': display_from_id,
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
                    'fromId': display_from_id,
                    'telemetry': telemetry,
                    'message': f"[TELEMETRY] From: {name} -> {telemetry}"
                })
                
            elif portnum == 'NODEINFO_APP':
                user = decoded.get('user', {})
                add_event({
                    'type': 'nodeinfo',
                    'from': name,
                    'fromId': display_from_id,
                    'user': user,
                    'message': f"[NODE INFO] From: {name} -> {user.get('longName')} ({user.get('shortName')})"
                })

            # Update heatmap if position
            if portnum == 'POSITION_APP':
                raw_pos = decoded.get('position', {})
                pos = normalize_pos(raw_pos)

                # Drop packets with no sender ID entirely.
                # A None fromId can come from ANY relayed/corrupted mesh packet —
                # not just our own radio. Our local GPS is now handled solely by
                # local_gps_poller reading interface.nodes[myId] directly.
                if not from_id:
                    print(f"DEBUG_GPS: dropping no-sender position packet (pos={pos})", flush=True)
                else:
                    position_is_local = (from_id == local_id)
                    print(f"DEBUG_GPS: id={from_id}, is_local={position_is_local}, name={name}, pos={pos}", flush=True)

                    add_event({
                        'type': 'position',
                        'from': name,
                        'fromId': from_id,
                        'pos': pos,
                        'hopLimit': hopLimit,
                        'message': f"[POSITION] From: {name} -> Lat: {pos.get('latitude')}, Lon: {pos.get('longitude')}"
                    })
                    if 'latitude' in pos and 'longitude' in pos:
                        nodes_data[from_id] = {
                            'id': from_id,
                            'is_local': position_is_local,
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
            elif display_from_id in nodes_data:
                nodes_data[display_from_id]['snr'] = rxSnr
                nodes_data[display_from_id]['rssi'] = rxRssi
                nodes_data[display_from_id]['last_updated'] = time.time()
                
    except Exception as e:
        print(f"Error handling packet: {e}")

def local_gps_poller():
    """
    Poll the Meshtastic library's internal node state every 15 seconds for the
    local node's GPS. The library keeps interface.nodes[myId] updated via its
    serial thread, independent of the mesh broadcast timer (position_broadcast_secs).
    This means we get updated GPS within ~15s of a fix, not up to 15 minutes.
    """
    while True:
        time.sleep(15)
        if not interface:
            continue
        try:
            local_id = getattr(interface, 'myId', None)
            if not local_id:
                continue
            if not hasattr(interface, 'nodes') or local_id not in interface.nodes:
                continue

            pos = normalize_pos(interface.nodes[local_id].get('position', {}))
            lat = pos.get('latitude')
            lon = pos.get('longitude')

            if not lat or not lon:
                continue

            existing = nodes_data.get(local_id, {})
            # Promote to 'live' if: we only had stale init data, or coords changed
            if (existing.get('source') == 'init'
                    or lat != existing.get('latitude')
                    or lon != existing.get('longitude')):
                nodes_data[local_id] = {
                    **existing,   # preserve is_local, name, snr, rssi
                    'latitude': lat,
                    'longitude': lon,
                    'sats': pos.get('satsInView', existing.get('sats', 0)),
                    'pdop': pos.get('PDOP', pos.get('HDOP', existing.get('pdop', 0))),
                    'source': 'live',
                    'last_updated': time.time(),
                }
                print(f"GPS_POLL: local GPS updated from library state → "
                      f"{lat:.5f}, {lon:.5f}  sats={pos.get('satsInView', '?')}", flush=True)
        except Exception as e:
            print(f"GPS poll error: {e}", flush=True)

def connect_radio():
    global interface
    print("Connecting to Meshtastic...")
    try:
        interface = meshtastic.serial_interface.SerialInterface()
        init_node_data(interface)
        pub.subscribe(on_receive, "meshtastic.receive")
        # Start background GPS poller for the local node
        t = threading.Thread(target=local_gps_poller, daemon=True)
        t.start()
        print(f"Connected! Seeded map with {len(nodes_data)} nodes.")
    except Exception as e:
        print(f"Failed to connect to radio: {e}")


@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/heatmap')
def api_heatmap():
    return jsonify(list(nodes_data.values()))

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
        
    local_id = None
    node_info = {}
    name = None
    
    try:
        # Try multiple ways to get local node info
        node_info = interface.getMyNodeInfo() or {}
        local_id = node_info.get('user', {}).get('id')
        
        # Fallback to interface.myId
        if not local_id and hasattr(interface, 'myId'):
            local_id = interface.myId
            
        # Try getMyUser()
        try:
            my_user = interface.getMyUser()
            if my_user:
                my_user_dict = proto_to_dict(my_user)
                if isinstance(my_user_dict, dict):
                    name = my_user_dict.get('longName', my_user_dict.get('shortName'))
        except:
            pass
            
        if not name and local_id in interface.nodes:
            user_info = interface.nodes[local_id].get('user', {})
            name = user_info.get('longName', user_info.get('shortName'))
            
        pass
    except:
        pass
        
    # Final fallback: check nodes_data if we have it
    if not name and local_id in nodes_data:
        name = nodes_data[local_id].get('name')
    
    if not name:
        name = "Unknown"

    pos = node_info.get('position', {})
    metrics = node_info.get('deviceMetrics', {})
    
    user_info = node_info.get('user', {})
    long_name = user_info.get('longName', name)
    short_name = user_info.get('shortName', "Unknown")

    # Only use nodes_data for GPS if it came from a live POSITION_APP packet.
    # 'init' entries are seeded from the cached mesh DB and may be stale.
    live_gps = nodes_data.get(local_id, {}) if local_id else {}
    use_live_gps = live_gps.get('source') == 'live'

    state = {
        'nodes_online': len(nodes_data),
        'local_id': local_id or "Unknown",
        'uptime': metrics.get('uptimeSeconds', 0),
        'battery_voltage': metrics.get('voltage', 0.0),
        'battery_level': metrics.get('batteryLevel', 0),
        'chutil': metrics.get('channelUtilization', 0.0),
        'sats': live_gps.get('sats') if use_live_gps else pos.get('satsInView', 0),
        'pdop': live_gps.get('pdop') if use_live_gps else pos.get('PDOP', pos.get('HDOP', 0)),
        'latitude': live_gps.get('latitude') if use_live_gps else pos.get('latitude', 0.0),
        'longitude': live_gps.get('longitude') if use_live_gps else pos.get('longitude', 0.0),
        'gps_live': use_live_gps, # let TUI know if GPS data is fresh
        'name': name,
        'long_name': long_name,
        'short_name': short_name,
    }

    return jsonify(state)

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
    
    # Persist change - node.iface.localNode points back to same object usually
    if section_name in ['device', 'lora', 'network', 'display', 'position', 'power', 'security']:
        node.iface.localNode.writeConfig(section_name)
    else:
        node.iface.localNode.writeConfig("moduleConfig")

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
            
        # Log the sent message to local history so it shows up in TUI/heatmap
        local_id = getattr(interface, 'myId', 'Local')
        name = "Local"
        try:
            my_user = interface.getMyUser()
            if my_user:
                # User objects are protobufs, they don't have .get()
                name = getattr(my_user, 'longName', getattr(my_user, 'shortName', 'Local'))
        except:
            pass

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
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    load_events()
    connect_radio()
    print("Starting map & daemon server at http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
