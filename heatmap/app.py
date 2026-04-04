import time
import threading
from collections import deque
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS
import meshtastic
import meshtastic.serial_interface
from pubsub import pub
from google.protobuf.json_format import MessageToDict

app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS(app)

# Global dictionaries
nodes_data = {}
events = deque(maxlen=200)

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
        events.append(clean_dict)
    else:
        events.append({'time': time.time(), 'message': str(msg_or_dict), 'type': 'unknown'})

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

        # Packets from our own radio sometimes arrive with fromId=None
        if not from_id:
            from_id = local_id

        name = from_id
        if from_id == local_id:
            # For the local node, use our specialized name resolver
            try:
                my_user = interface.getMyUser()
                if my_user:
                    u = proto_to_dict(my_user)
                    name = u.get('longName', u.get('shortName', from_id))
            except:
                pass
        
        # Fallback to general nodes list if name is still ID
        if name == from_id and hasattr(interface, 'nodes') and from_id in interface.nodes:
            user_info = interface.nodes[from_id].get('user', {})
            name = user_info.get('longName', user_info.get('shortName', from_id))

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
                    'fromId': from_id,
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
                    'fromId': from_id,
                    'telemetry': telemetry,
                    'message': f"[TELEMETRY] From: {name} -> {telemetry}"
                })
                
            elif portnum == 'NODEINFO_APP':
                user = decoded.get('user', {})
                add_event({
                    'type': 'nodeinfo',
                    'from': name,
                    'fromId': from_id,
                    'user': user,
                    'message': f"[NODE INFO] From: {name} -> {user.get('longName')} ({user.get('shortName')})"
                })

            # Update heatmap if position
            if portnum == 'POSITION_APP':
                raw_pos = decoded.get('position', {})
                pos = normalize_pos(raw_pos)
                print(f"DEBUG_GPS: id={from_id}, is_local={from_id == local_id}, resolved_name={name}, pos={pos}", flush=True)
                
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
                        'is_local': (from_id == local_id),
                        'name': name,
                        'latitude': pos['latitude'],
                        'longitude': pos['longitude'],
                        'sats': pos.get('satsInView', 0),
                        'pdop': pos.get('PDOP', pos.get('HDOP', 0)),
                        'snr': rxSnr,
                        'rssi': rxRssi,
                        'last_updated': time.time()
                    }
            elif from_id in nodes_data:
                nodes_data[from_id]['snr'] = rxSnr
                nodes_data[from_id]['rssi'] = rxRssi
                nodes_data[from_id]['last_updated'] = time.time()
                
    except Exception as e:
        print(f"Error handling packet: {e}")

def connect_radio():
    global interface
    print("Connecting to Meshtastic...")
    try:
        interface = meshtastic.serial_interface.SerialInterface()
        init_node_data(interface)
        pub.subscribe(on_receive, "meshtastic.receive")
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
    new_events = [e for e in events if e['time'] > since]
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

    # Live GPS fallback: nodes_data is updated in real-time by on_receive
    # whereas getMyNodeInfo() position only updates on full node-info packet refresh.
    live_pos = nodes_data.get(local_id, {}) if local_id else {}

    state = {
        'nodes_online': len(nodes_data),
        'local_id': local_id or "Unknown",
        'uptime': metrics.get('uptimeSeconds', 0),
        'battery_voltage': metrics.get('voltage', 0.0),
        'battery_level': metrics.get('batteryLevel', 0),
        'chutil': metrics.get('channelUtilization', 0.0),
        # Prefer the live nodes_data values if available, fall back to cached node_info
        'sats': live_pos.get('sats') or pos.get('satsInView', 0),
        'pdop': live_pos.get('pdop') or pos.get('PDOP', pos.get('HDOP', 0)),
        'latitude': live_pos.get('latitude') or pos.get('latitude', 0.0),
        'longitude': live_pos.get('longitude') or pos.get('longitude', 0.0),
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
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    connect_radio()
    print("Starting map & daemon server at http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
