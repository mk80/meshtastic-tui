import time
import logging
import threading
import os
from collections import deque
from pubsub import pub
from google.protobuf.json_format import MessageToDict
from .base import RadioBackend

logger = logging.getLogger('daemon')

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
    result = dict(pos)
    if 'latitude' not in result and 'latitudeI' in result:
        result['latitude'] = result['latitudeI'] / 1e7
    if 'longitude' not in result and 'longitudeI' in result:
        result['longitude'] = result['longitudeI'] / 1e7
    return result

class MeshtasticBackend(RadioBackend):
    def __init__(self, port=None):
        super().__init__(port)
        self.interface = None
        self.running = False
        self.gps_poller_thread = None
        self.conn_monitor_thread = None

    def connect(self):
        import meshtastic.serial_interface
        self.running = True
        self.conn_monitor_thread = threading.Thread(target=self._connection_monitor, daemon=True, name="conn_monitor")
        self.conn_monitor_thread.start()

    def disconnect(self):
        self.running = False
        if self.interface:
            try:
                self.interface.close()
            except:
                pass
            self.interface = None

    def _connect_radio(self):
        import meshtastic.serial_interface
        logger.info("Connecting to Meshtastic...")
        try:
            if self.port:
                logger.info(f"Using explicitly selected port: {self.port}")
                self.interface = meshtastic.serial_interface.SerialInterface(devPath=self.port)
            else:
                self.interface = meshtastic.serial_interface.SerialInterface()
                
            self._init_node_data()
            pub.subscribe(self._on_receive, "meshtastic.receive")
            
            if not any(t.name == "gps_poller" for t in threading.enumerate()):
                self.gps_poller_thread = threading.Thread(target=self._local_gps_poller, daemon=True, name="gps_poller")
                self.gps_poller_thread.start()
                
            logger.info(f"Connected! Initialized with {len(self.nodes_data)} nodes.")
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.interface = None

    def _connection_monitor(self):
        self._connect_radio()
        while self.running:
            time.sleep(2)
            is_alive = False
            try:
                if self.interface and hasattr(self.interface, 'nodes') and self.interface.nodes is not None:
                    is_alive = True
            except:
                is_alive = False

            if not is_alive and self.running:
                logger.warning("Radio connection lost. Attempting to reconnect...")
                if self.interface:
                    try: self.interface.close()
                    except: pass
                    self.interface = None
                self._connect_radio()

    def _get_local_node_id(self):
        if not self.interface: return None
        if hasattr(self.interface, 'myId') and self.interface.myId:
            return self.interface.myId
        try:
            info = self.interface.getMyNodeInfo()
            return info.get('user', {}).get('id')
        except:
            return None

    def _get_display_name(self, node_id):
        if not node_id or not self.interface: return "Unknown"
        try:
            if node_id in self.interface.nodes:
                user = self.interface.nodes[node_id].get('user', {})
                name = user.get('longName') or user.get('shortName')
                if name: return name
            if node_id == self._get_local_node_id():
                my_user = self.interface.getMyUser()
                if my_user:
                    return getattr(my_user, 'longName', getattr(my_user, 'shortName', node_id))
        except Exception:
            pass
            
        with self.nodes_lock:
            if node_id in self.nodes_data:
                return self.nodes_data[node_id].get('name', node_id)
        return node_id

    def _init_node_data(self):
        local_id = self._get_local_node_id()
        if self.interface.nodes:
            with self.nodes_lock:
                for node_id, info in self.interface.nodes.items():
                    user = info.get('user', {})
                    pos = normalize_pos(info.get('position', {}))
                    snr = info.get('snr', -10)
                    
                    if 'latitude' in pos and 'longitude' in pos:
                        self.nodes_data[node_id] = {
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

    def _local_gps_poller(self):
        while self.running:
            time.sleep(15)
            if not self.interface: continue
            try:
                local_id = self._get_local_node_id()
                if not local_id or not hasattr(self.interface, 'nodes') or local_id not in self.interface.nodes:
                    continue

                pos = normalize_pos(self.interface.nodes[local_id].get('position', {}))
                lat, lon = pos.get('latitude'), pos.get('longitude')
                if not lat or not lon: continue

                with self.nodes_lock:
                    existing = self.nodes_data.get(local_id, {})
                    if (existing.get('source') == 'init' or lat != existing.get('latitude') or lon != existing.get('longitude')):
                        self.nodes_data[local_id] = {
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

    def _on_receive(self, packet, interface):
        try:
            from_id = packet.get('fromId')
            rxSnr = packet.get('rxSnr', -10)
            rxRssi = packet.get('rxRssi', -100)
            hopLimit = packet.get('hopLimit')
            local_id = self._get_local_node_id()
            name = self._get_display_name(from_id)

            if 'decoded' in packet:
                decoded = packet['decoded']
                portnum = decoded.get('portnum')
                
                if from_id == local_id and portnum == 'TEXT_MESSAGE_APP':
                    return

                if portnum == 'TEXT_MESSAGE_APP':
                    text = decoded.get('text', '')
                    if not text and 'payload' in decoded:
                        text = decoded['payload'].decode('utf-8', errors='replace')
                    self.add_event({
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
                    self.add_event({
                        'type': 'telemetry',
                        'from': name,
                        'fromId': from_id or 'Unknown',
                        'telemetry': telemetry,
                        'message': f"[TELEMETRY] From: {name} -> {telemetry}"
                    })
                elif portnum == 'NODEINFO_APP':
                    user = decoded.get('user', {})
                    self.add_event({
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
                        self.add_event({
                            'type': 'position',
                            'from': name,
                            'fromId': from_id,
                            'pos': pos,
                            'hopLimit': hopLimit,
                            'message': f"[POSITION] From: {name} -> Lat: {pos.get('latitude')}, Lon: {pos.get('longitude')}"
                        })
                        if 'latitude' in pos and 'longitude' in pos:
                            with self.nodes_lock:
                                self.nodes_data[from_id] = {
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
                    with self.nodes_lock:
                        if from_id in self.nodes_data:
                            self.nodes_data[from_id]['snr'] = rxSnr
                            self.nodes_data[from_id]['rssi'] = rxRssi
                            self.nodes_data[from_id]['last_updated'] = time.time()
                    
        except Exception as e:
            logger.error(f"Error handling packet: {e}")

    def get_state(self):
        if not self.interface:
            return None
            
        local_id = self._get_local_node_id() or "Unknown"
        name = self._get_display_name(local_id)
        
        node_info = {}
        try:
            node_info = self.interface.getMyNodeInfo() or {}
        except:
            pass

        pos = node_info.get('position', {})
        metrics = node_info.get('deviceMetrics', {})
        user_info = node_info.get('user', {})
        
        with self.nodes_lock:
            live_gps = self.nodes_data.get(local_id, {})
        use_live_gps = live_gps.get('source') == 'live'

        lora_config = {}
        try:
            lora_config = self.interface.localNode.localConfig.lora
        except:
            pass

        channels = []
        try:
            if hasattr(self.interface.localNode, 'channels'):
                for ch in self.interface.localNode.channels:
                    if ch.role != 0: # 0 is DISABLED
                        name = ch.settings.name if ch.settings and ch.settings.name else f"Channel {ch.index}"
                        channels.append({'index': ch.index, 'name': name})
        except:
            pass

        return {
            'nodes_online': len(self.nodes_data),
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
            'hop_limit': getattr(lora_config, 'hop_limit', 3),
            'radio_freq': 0.0, # Raw freq mapping is complex in Meshtastic, returning 0.0
            'radio_bw': getattr(lora_config, 'bandwidth', 0.0),
            'radio_sf': getattr(lora_config, 'spread_factor', 0),
            'radio_cr': getattr(lora_config, 'coding_rate', 0),
            'tx_power': getattr(lora_config, 'tx_power', 0),
            'channels': channels,
            'server_time': time.time()
        }

    def get_nodes(self):
        if not self.interface or not hasattr(self.interface, 'nodes') or not self.interface.nodes:
            return []
        
        nodes_list = []
        with self.nodes_lock:
            for node_id, info in self.interface.nodes.items():
                user = info.get('user', {})
                live_data = self.nodes_data.get(node_id, {})
                
                nodes_list.append({
                    'id': node_id,
                    'long_name': user.get('longName', node_id),
                    'short_name': user.get('shortName', "??"),
                    'last_heard': info.get('lastHeard', 0),
                    'snr': live_data.get('snr', info.get('snr', -10)),
                    'rssi': live_data.get('rssi', info.get('rssi', -100))
                })
        
        nodes_list.sort(key=lambda x: x['last_heard'] or 0, reverse=True)
        return nodes_list

    def send_message(self, text, destination=None, channel=0):
        if not self.interface:
            raise Exception("Radio not connected")
        
        if destination:
            self.interface.sendText(text, destinationId=destination)
        else:
            self.interface.sendText(text, channelIndex=channel)
            
        local_id = self._get_local_node_id() or 'Local'
        name = self._get_display_name(local_id)

        self.add_event({
            'type': 'text',
            'from': name,
            'fromId': local_id,
            'toId': destination or 'All',
            'text': text,
            'message': f"[SENT] To: {destination or 'All'} -> {text}"
        })

    def apply_config(self, config_dict):
        if not self.interface or not self.interface.localNode:
            raise Exception("Radio not connected")
            
        if 'long_name' in config_dict or 'short_name' in config_dict:
            self.interface.localNode.setOwner(
                long_name=config_dict.get('long_name'),
                short_name=config_dict.get('short_name')
            )
            
        if 'hop_limit' in config_dict:
            self._set_pref(self.interface.localNode, 'lora.hop_limit', config_dict['hop_limit'])
        
        if 'command' in config_dict:
            cmd = config_dict['command'].strip()
            if cmd:
                parts = cmd.split(maxsplit=1)
                if len(parts) != 2:
                    raise ValueError('Invalid command format. Use: section.parameter value')
                key, val = parts
                self._set_pref(self.interface.localNode, key, val)
                
    def _set_pref(self, node, key, val):
        import meshtastic.util
        try:
            if hasattr(node, 'set_simple_preference'):
                logger.info(f"Using set_simple_preference for {key} = {val}")
                node.set_simple_preference(key, val)
                return True
        except Exception as e:
            logger.warning(f"set_simple_preference failed for {key}: {e}")

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
        
        try:
            if len(section.ListFields()) == 0:
                logger.info(f"Section {section_name} is empty, requesting from radio...")
                field_desc = node.localConfig.DESCRIPTOR.fields_by_name.get(section_name)
                if not field_desc:
                    field_desc = node.moduleConfig.DESCRIPTOR.fields_by_name.get(section_name)
                
                if field_desc:
                    node.requestConfig(field_desc)
                    time.sleep(0.5)
        except Exception as e:
            logger.warning(f"Failed to requestConfig for {section_name}: {e}")

        field_name_snake = meshtastic.util.camel_to_snake(field_name)
        
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
        node.iface.localNode.writeConfig(section_name)
        return True
