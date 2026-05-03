import asyncio
import threading
import time
import logging
from meshcore import MeshCore, EventType
from .base import RadioBackend

logger = logging.getLogger('daemon')

class MeshcoreBackend(RadioBackend):
    def __init__(self, port=None):
        super().__init__(port)
        self.mc = None
        self.loop = None
        self.thread = None
        self.running = False
        self.channels = []

    def connect(self):
        self.running = True
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="meshcore_loop")
        self.thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._async_connect())
        if self.running:
            self.loop.run_forever()

    async def _async_connect(self):
        logger.info(f"Connecting to MeshCore on {self.port or 'auto'}...")
        try:
            if self.port:
                self.mc = await MeshCore.create_serial(self.port, default_timeout=5)
            else:
                self.mc = await MeshCore.create_serial('/dev/ttyACM0', default_timeout=5)
                
            logger.info(f"Connected to MeshCore! Name: {self.mc.self_info.get('name')}")
            
            # Subscribe to events
            self.mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_message)
            self.mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_message)
            self.mc.subscribe(EventType.CONTACTS, self._on_contacts_update)
            self.mc.subscribe(EventType.MESSAGES_WAITING, self._on_messages_waiting)
            
            # Fetch full contact list
            try:
                await self.mc.commands.get_contacts()
            except Exception as e:
                logger.warning(f"Error fetching contacts: {e}")
                
            # Fetch channels dynamically
            await self._fetch_channels()
                
            # Fetch pending messages on connect
            try:
                while True:
                    res = await self.mc.commands.get_msg()
                    if not res or res.type == EventType.NO_MORE_MSGS or res.type == EventType.ERROR:
                        break
            except Exception as e:
                logger.warning(f"Error fetching pending messages on connect: {e}")
            
        except Exception as e:
            logger.error(f"Failed to connect to MeshCore: {e}")

    def disconnect(self):
        self.running = False
        if self.loop and self.mc:
            asyncio.run_coroutine_threadsafe(self.mc.disconnect(), self.loop)
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)
        logger.info("Disconnected from MeshCore.")

    async def _on_message(self, event):
        try:
            msg_text = event.payload.get('text', '')
            if not msg_text:
                return
                
            is_channel = event.type == EventType.CHANNEL_MSG_RECV
            from_id = event.payload.get('pubkey_prefix', '')
            
            sender = 'Unknown'
            
            if is_channel and not from_id:
                # MeshCore embeds the sender name in the text as "name: message"
                if ': ' in msg_text:
                    sender, msg_text = msg_text.split(': ', 1)
                    from_id = sender  # Use name as fallback ID
            else:
                sender = from_id
                with self.nodes_lock:
                    for full_key, data in self.nodes_data.items():
                        if from_id and (full_key.startswith(from_id) or from_id.startswith(full_key[:8])):
                            sender = data.get('name', from_id)
                            from_id = full_key
                            break
                        
            channel = event.payload.get('channel_idx', 0)
            to_id = '^all' if is_channel else self.mc.self_info.get('public_key', 'me')
            
            self.add_event({
                'type': 'text',
                'channel': channel,
                'from': sender,
                'fromId': from_id,
                'toId': to_id,
                'text': msg_text,
                'message': msg_text
            })
        except Exception as e:
            logger.error(f"Error handling MeshCore message: {e}")

    async def _on_messages_waiting(self, event):
        try:
            logger.info("Messages waiting on MeshCore, fetching...")
            while True:
                res = await self.mc.commands.get_msg()
                if not res or res.type == EventType.NO_MORE_MSGS or res.type == EventType.ERROR:
                    break
        except Exception as e:
            logger.error(f"Error fetching pending messages: {e}")

    async def _on_contacts_update(self, event):
        self._update_contacts(self.mc.contacts)

    def _update_contacts(self, contacts_dict):
        with self.nodes_lock:
            for key, info in contacts_dict.items():
                self.nodes_data[key] = {
                    'id': key[:8] if isinstance(key, str) else str(key),
                    'is_local': False,
                    'name': info.get('adv_name', key),
                    'latitude': info.get('lat'),
                    'longitude': info.get('lon'),
                    'sats': 0,
                    'pdop': 0,
                    'snr': info.get('snr', 0),
                    'rssi': info.get('rssi', 0),
                    'source': 'live',
                    'last_updated': time.time()
                }

    def get_state(self):
        if not self.mc or not self.mc.is_connected:
            return None
            
        info = self.mc.self_info
        return {
            'nodes_online': len(self.nodes_data),
            'local_id': info.get('public_key', 'mock_1'),
            'latitude': info.get('adv_lat', 0.0),
            'longitude': info.get('adv_lon', 0.0),
            'gps_live': True,
            'name': info.get('name', 'Unknown'),
            'long_name': info.get('name', 'Unknown'),
            'short_name': info.get('name', 'Unknown')[:4],
            'hop_limit': 'Dynamic (Max 64)',
            'radio_freq': info.get('radio_freq', 0.0),
            'radio_bw': info.get('radio_bw', 0.0),
            'radio_sf': info.get('radio_sf', 0),
            'radio_cr': info.get('radio_cr', 0),
            'tx_power': info.get('tx_power', 0),
            'channels': self.channels,
            'server_time': time.time()
        }

    def get_nodes(self):
        if not self.mc or not self.mc.is_connected:
            return []
            
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
        if not self.mc or not self.mc.is_connected:
            raise Exception("Radio not connected")
            
        if destination:
            asyncio.run_coroutine_threadsafe(self.mc.commands.send_msg(destination, text), self.loop)
        else:
            asyncio.run_coroutine_threadsafe(self.mc.commands.send_chan_msg(channel, text), self.loop)
            
        self.add_event({
            'type': 'text',
            'channel': channel,
            'from': self.mc.self_info.get('name', 'Me'),
            'fromId': self.mc.self_info.get('public_key', 'me'),
            'toId': destination or '^all',
            'text': text,
            'message': text
        })

    async def _fetch_channels(self):
        if not self.mc or not self.mc.is_connected:
            return
        try:
            channels = []
            for i in range(8): # Fetch first 8 channels for TUI
                ch_event = await self.mc.commands.get_channel(i)
                if ch_event and ch_event.type != EventType.ERROR:
                    name = ch_event.attributes.get('channel_name')
                    if name:
                        channels.append({'index': i, 'name': name})
            self.channels = channels
            logger.info(f"Updated channels: {self.channels}")
        except Exception as e:
            logger.warning(f"Error fetching channels: {e}")

    def set_channel(self, index: int, name: str, ch_type: str = 'public', secret_hex: str = ''):
        if not self.mc or not self.mc.is_connected:
            raise Exception("Radio not connected")
            
        async def _do_set():
            nonlocal name
            try:
                if not name:
                    logger.info(f"Leaving channel {index} (clearing name)")
                    await self.mc.commands.set_channel(index, "")
                else:
                    secret_bytes = None
                    if ch_type == 'public':
                        secret_bytes = b"\x01" + (b"\x00" * 15)
                    elif ch_type == 'hashtag':
                        if not name.startswith('#'):
                            name = '#' + name
                        # meshcore auto-hashes if secret_bytes is None
                    elif ch_type == 'private':
                        if not secret_hex:
                            raise ValueError("Private channels require a secret key")
                        try:
                            secret_bytes = bytes.fromhex(secret_hex)
                        except ValueError:
                            raise ValueError("Invalid hex string for secret key")
                        if len(secret_bytes) != 16:
                            raise ValueError(f"Secret must be exactly 16 bytes (32 hex characters), got {len(secret_bytes)}")
                    
                    logger.info(f"Joining channel {index} with name '{name}', type '{ch_type}'")
                    await self.mc.commands.set_channel(index, name, channel_secret=secret_bytes)
                # Refresh channels
                await asyncio.sleep(1) # Give radio a moment to apply
                await self._fetch_channels()
            except Exception as e:
                logger.error(f"Error setting channel {index}: {e}")
                
        asyncio.run_coroutine_threadsafe(_do_set(), self.loop)

    def apply_config(self, config_dict):
        if not self.mc or not self.mc.is_connected:
            raise Exception("Radio not connected")
            
        if 'long_name' in config_dict:
            asyncio.run_coroutine_threadsafe(self.mc.commands.set_name(config_dict['long_name']), self.loop)
            
        logger.info(f"MeshCore config applied: {config_dict}")
