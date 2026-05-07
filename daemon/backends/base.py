import logging
import threading
import time
import json
import os
from collections import deque

logger = logging.getLogger('daemon')

class RadioBackend:
    def __init__(self, port=None):
        self.port = port
        self.nodes_data = {}
        self.nodes_lock = threading.Lock()
        self.events = deque(maxlen=10)
        self.events_lock = threading.Lock()
        self.messages_file = 'messages.json'
        self.load_events()

    def connect(self):
        """Establish connection to the radio."""
        raise NotImplementedError()

    def disconnect(self):
        """Cleanly disconnect from the radio."""
        pass

    def get_state(self):
        """
        Return a dictionary of the current radio state.
        Guaranteed to contain:
        - battery_level, uptime, nodes_online
        - radio_freq, radio_bw, radio_sf, radio_cr, tx_power
        - channels: list of dicts [{'index': int, 'name': str}]
        """
        raise NotImplementedError()

    def get_nodes(self):
        """Return a list of all known nodes for the TUI list view."""
        raise NotImplementedError()

    def get_heatmap_nodes(self):
        """Return a list of mapped nodes for the heatmap."""
        with self.nodes_lock:
            return list(self.nodes_data.values())

    def get_events(self, since=0.0):
        """Return a list of events/messages since a specific timestamp."""
        with self.events_lock:
            return [e for e in self.events if e.get('time', 0.0) > since]

    def send_message(self, text, destination=None, channel=0):
        """Send a text message to the mesh."""
        raise NotImplementedError()

    def apply_config(self, config_dict):
        """Apply a configuration change to the radio."""
        raise NotImplementedError()

    def set_channel(self, index: int, name: str, ch_type: str = 'public', secret_hex: str = ''):
        """Set a channel index to a specific name, type, and secret."""
        pass

    def save_events(self):
        """Save text messages to disk."""
        try:
            tmp_file = self.messages_file + ".tmp"
            with self.events_lock:
                data = [e for e in self.events if e.get('type') == 'text']
            with open(tmp_file, 'w') as f:
                json.dump(data, f)
            os.replace(tmp_file, self.messages_file)
        except Exception as e:
            logger.error(f"Error saving messages: {e}")

    def load_events(self):
        """Load recent text messages from disk."""
        if not os.path.exists(self.messages_file):
            return
        try:
            with open(self.messages_file, 'r') as f:
                saved = json.load(f)
                with self.events_lock:
                    self.events.clear()
                    self.events.extend(saved)
                logger.info(f"Loaded {len(self.events)} (out of {len(saved)} on disk) messages into recent history.")
        except Exception as e:
            logger.error(f"Error loading messages: {e}")

    def add_event(self, event_dict):
        """Thread-safe method to add an event and trigger a save."""
        if 'time' not in event_dict:
            event_dict['time'] = time.time()
        with self.events_lock:
            self.events.append(event_dict)
        self.save_events()
