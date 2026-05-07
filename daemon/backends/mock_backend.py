import time
import random
import logging
from .base import RadioBackend

logger = logging.getLogger('daemon')


class MockMeshtasticBackend(RadioBackend):
    """Mock backend that faithfully simulates a Meshtastic radio for device-free testing."""
    
    def __init__(self, port=None):
        super().__init__(port)
    
    def connect(self):
        logger.info("Mock Meshtastic backend connected (no radio required).")
        self._generate_mock_data()
    
    def disconnect(self):
        logger.info("Mock Meshtastic backend disconnected.")
    
    def _generate_mock_data(self):
        """Generate data matching the real MeshtasticBackend output format."""
        mock_nodes = [
            {'id': '!a1b2c3d4', 'long_name': 'BaseStation-01', 'short_name': 'BS01',
             'lat': 32.826, 'lon': -117.046, 'snr': 7.5, 'rssi': -72},
            {'id': '!e5f6a7b8', 'long_name': 'MobileNode-02', 'short_name': 'MN02',
             'lat': 32.900, 'lon': -117.100, 'snr': 3.2, 'rssi': -89},
            {'id': '!c9d0e1f2', 'long_name': 'Relay-Hilltop', 'short_name': 'RLYH',
             'lat': 33.050, 'lon': -117.070, 'snr': -2.0, 'rssi': -105},
            {'id': '!11223344', 'long_name': 'Hiker-Alice', 'short_name': 'ALIC',
             'lat': 32.850, 'lon': -117.080, 'snr': 5.0, 'rssi': -80},
            {'id': '!55667788', 'long_name': 'Vehicle-Bob', 'short_name': 'VBOB',
             'lat': 32.780, 'lon': -117.020, 'snr': -8.5, 'rssi': -115},
            {'id': '!99aabbcc', 'long_name': 'HomeNode-03', 'short_name': 'HN03',
             'lat': 32.870, 'lon': -117.060, 'snr': 9.0, 'rssi': -65},
            {'id': '!ddeeff00', 'long_name': 'NoGPS-Node', 'short_name': 'NOGP',
             'lat': None, 'lon': None, 'snr': -5.0, 'rssi': -110},
        ]
        
        local_id = '!deadbeef'
        
        with self.nodes_lock:
            # Local node (matches meshtastic format: no node_type, has snr/rssi/sats/pdop)
            self.nodes_data[local_id] = {
                'id': local_id,
                'is_local': True,
                'name': 'MockMesh-Local',
                'latitude': 32.840,
                'longitude': -117.055,
                'sats': 8,
                'pdop': 120,
                'snr': 0,
                'rssi': 0,
                'source': 'live',
                'last_updated': time.time()
            }
            
            for node in mock_nodes:
                if node['lat'] is not None:
                    self.nodes_data[node['id']] = {
                        'id': node['id'],
                        'is_local': False,
                        'name': node['long_name'],
                        'latitude': node['lat'],
                        'longitude': node['lon'],
                        'sats': random.randint(0, 12),
                        'pdop': random.randint(80, 300),
                        'snr': node['snr'],
                        'rssi': node['rssi'],
                        'source': 'init',
                        'last_updated': time.time() - random.randint(0, 3600)
                    }
        
        # Mock messages in meshtastic format
        self.add_event({
            'type': 'text',
            'channel': 0,
            'from': 'Hiker-Alice',
            'fromId': '!11223344',
            'toId': '^all',
            'text': 'Anyone on the trail today?',
            'hopLimit': 3,
            'message': '[TEXT MESSAGE] From: Hiker-Alice -> Anyone on the trail today?'
        })
        self.add_event({
            'type': 'text',
            'channel': 0,
            'from': 'Vehicle-Bob',
            'fromId': '!55667788',
            'toId': '^all',
            'text': 'Heading north on the 15!',
            'hopLimit': 3,
            'message': '[TEXT MESSAGE] From: Vehicle-Bob -> Heading north on the 15!'
        })
        self.add_event({
            'type': 'text',
            'channel': 0,
            'from': 'BaseStation-01',
            'fromId': '!a1b2c3d4',
            'toId': '!deadbeef',
            'text': 'DM test to local node',
            'hopLimit': 3,
            'message': '[TEXT MESSAGE] From: BaseStation-01 -> DM test to local node'
        })
        
        # Emit mock position events (matching real MeshtasticBackend format)
        # These populate the "Node Neighbors" sidebar in the TUI
        for node in mock_nodes:
            if node['lat'] is not None:
                self.add_event({
                    'type': 'position',
                    'from': node['long_name'],
                    'fromId': node['id'],
                    'pos': {
                        'latitude': node['lat'],
                        'longitude': node['lon'],
                        'satsInView': random.randint(4, 12),
                        'PDOP': random.randint(80, 250),
                    },
                    'hopLimit': random.randint(1, 5),
                    'message': f"[POSITION] From: {node['long_name']} -> Lat: {node['lat']}, Lon: {node['lon']}"
                })
        
        logger.info(f"Generated {len(self.nodes_data)} mock meshtastic nodes.")
    
    def get_state(self):
        """Return state matching real MeshtasticBackend format."""
        return {
            'nodes_online': len(self.nodes_data),
            'local_id': '!deadbeef',
            'uptime': 86400,
            'battery_voltage': 4.05,
            'battery_level': 87,
            'chutil': 12.5,
            'sats': 8,
            'pdop': 120,
            'latitude': 32.840,
            'longitude': -117.055,
            'gps_live': True,
            'name': 'MockMesh-Local',
            'long_name': 'MockMesh-Local',
            'short_name': 'MOCK',
            'hop_limit': 3,
            'radio_freq': 915.0,
            'radio_bw': 250.0,
            'radio_sf': 11,
            'radio_cr': 5,
            'tx_power': 27,
            'channels': [
                {'index': 0, 'name': 'LongFast'},
                {'index': 1, 'name': 'Admin'},
            ],
            'server_time': time.time()
        }
    
    def get_nodes(self):
        """Return node list matching real MeshtasticBackend format."""
        nodes_list = []
        with self.nodes_lock:
            for node_id, data in self.nodes_data.items():
                nodes_list.append({
                    'id': node_id,
                    'long_name': data.get('name', node_id),
                    'short_name': data.get('name', node_id)[:4],
                    'last_heard': data.get('last_updated', 0),
                    'snr': data.get('snr', -10),
                    'rssi': data.get('rssi', -100),
                    'latitude': data.get('latitude'),
                    'longitude': data.get('longitude')
                })
        nodes_list.sort(key=lambda x: x['last_heard'] or 0, reverse=True)
        return nodes_list
    
    def send_message(self, text, destination=None, channel=0):
        self.add_event({
            'type': 'text',
            'channel': channel,
            'from': 'MockMesh-Local',
            'fromId': '!deadbeef',
            'toId': destination or 'All',
            'text': text,
            'message': f"[SENT] To: {destination or 'All'} -> {text}"
        })
    
    def apply_config(self, config_dict):
        logger.info(f"Mock Meshtastic config applied: {config_dict}")


class MockMeshcoreBackend(RadioBackend):
    """Mock backend that faithfully simulates a MeshCore radio for device-free testing."""
    
    # MeshCore contact types: 0=NONE, 1=CLI(user), 2=REP(repeater), 3=ROOM, 4=SENS(sensor)
    NODE_TYPE_MAP = {0: 'unknown', 1: 'user', 2: 'repeater', 3: 'room', 4: 'sensor'}
    
    def __init__(self, port=None):
        super().__init__(port)
        self.channels = []
    
    def connect(self):
        logger.info("Mock MeshCore backend connected (no radio required).")
        self._generate_mock_data()
    
    def disconnect(self):
        logger.info("Mock MeshCore backend disconnected.")
    
    def _generate_mock_data(self):
        """Generate data matching the real MeshcoreBackend output format."""
        mock_nodes = [
            {'name': 'ca.sd.mission-trails', 'type': 2, 'lat': 32.826, 'lon': -117.046},
            {'name': 'Orange Hill Rpt', 'type': 2, 'lat': 33.770, 'lon': -117.780},
            {'name': 'Battle Mountain', 'type': 2, 'lat': 33.048, 'lon': -117.068},
            {'name': 'WristMesh', 'type': 1, 'lat': 33.833, 'lon': -118.321},
            {'name': 'RedHot-Operator', 'type': 1, 'lat': 33.890, 'lon': -117.311},
            {'name': 'ChatRoom-SD', 'type': 3, 'lat': 32.870, 'lon': -117.060},
            {'name': 'TempSensor-Roof', 'type': 4, 'lat': 32.830, 'lon': -117.040},
            {'name': 'WindSensor-Coast', 'type': 4, 'lat': 32.910, 'lon': -117.090},
            {'name': 'NoPos-Repeater', 'type': 2, 'lat': 0.0, 'lon': 0.0},
            {'name': 'NoPos-User', 'type': 1, 'lat': 0.0, 'lon': 0.0},
        ]
        
        self.channels = [
            {'index': 0, 'name': 'Public'},
            {'index': 1, 'name': '#meshcore'},
            {'index': 2, 'name': '#test'},
        ]
        
        local_key = 'ab' * 16  # 32 char hex string like a real public key
        
        with self.nodes_lock:
            # Local node (matches meshcore format: has node_type, snr/rssi = 0)
            self.nodes_data[local_key] = {
                'id': local_key[:8],
                'is_local': True,
                'name': 'MockCore-Local',
                'latitude': 32.840,
                'longitude': -117.055,
                'node_type': 'local',
                'sats': 0,
                'pdop': 0,
                'snr': 0,
                'rssi': 0,
                'source': 'live',
                'last_updated': time.time()
            }
            
            for i, node in enumerate(mock_nodes):
                lat = node['lat']
                lon = node['lon']
                has_pos = lat != 0.0 or lon != 0.0
                key = f'{i:02x}' * 16  # fake 32 char hex public key
                
                self.nodes_data[key] = {
                    'id': key[:8],
                    'is_local': False,
                    'name': node['name'],
                    'latitude': lat if has_pos else None,
                    'longitude': lon if has_pos else None,
                    'node_type': self.NODE_TYPE_MAP.get(node['type'], 'unknown'),
                    'sats': 0,
                    'pdop': 0,
                    'snr': 0,
                    'rssi': 0,
                    'source': 'live',
                    'last_updated': time.time() - random.randint(0, 3600)
                }
        
        # Mock messages in meshcore format (plain text, no prefixes)
        self.add_event({
            'type': 'text',
            'channel': 0,
            'from': 'WristMesh',
            'fromId': '03' * 16,
            'toId': '^all',
            'text': 'Hello from the mesh!',
            'message': 'Hello from the mesh!'
        })
        self.add_event({
            'type': 'text',
            'channel': 1,
            'from': 'RedHot-Operator',
            'fromId': '04' * 16,
            'toId': '^all',
            'text': 'Testing #meshcore channel',
            'message': 'Testing #meshcore channel'
        })
        
        logger.info(f"Generated {len(self.nodes_data)} mock meshcore nodes and {len(self.channels)} channels.")
    
    def get_state(self):
        """Return state matching real MeshcoreBackend format (no battery/uptime/chutil)."""
        return {
            'nodes_online': len(self.nodes_data),
            'local_id': 'ab' * 16,
            'latitude': 32.840,
            'longitude': -117.055,
            'gps_live': True,
            'name': 'MockCore-Local',
            'long_name': 'MockCore-Local',
            'short_name': 'Mock',
            'hop_limit': 'Dynamic (Max 64)',
            'radio_freq': 906.875,
            'radio_bw': 250.0,
            'radio_sf': 11,
            'radio_cr': 5,
            'tx_power': 17,
            'channels': self.channels,
            'server_time': time.time()
        }
    
    def get_nodes(self):
        """Return node list matching real MeshcoreBackend format."""
        nodes_list = []
        with self.nodes_lock:
            for node_id, data in self.nodes_data.items():
                nodes_list.append({
                    'id': node_id,
                    'long_name': data.get('name', node_id),
                    'short_name': data.get('name', node_id)[:4],
                    'last_heard': data.get('last_updated', 0),
                    'snr': data.get('snr', 0),
                    'rssi': data.get('rssi', 0)
                })
        nodes_list.sort(key=lambda x: x['last_heard'] or 0, reverse=True)
        return nodes_list
    
    def send_message(self, text, destination=None, channel=0):
        self.add_event({
            'type': 'text',
            'channel': channel,
            'from': 'MockCore-Local',
            'fromId': 'ab' * 16,
            'toId': destination or '^all',
            'text': text,
            'message': text
        })
    
    def set_channel(self, index: int, name: str, ch_type: str = 'public', secret_hex: str = ''):
        logger.info(f"Mock MeshCore set_channel: index={index}, name={name}, type={ch_type}")
        if name:
            self.channels.append({'index': index, 'name': name})
        else:
            self.channels = [ch for ch in self.channels if ch['index'] != index]
    
    def apply_config(self, config_dict):
        logger.info(f"Mock MeshCore config applied: {config_dict}")
