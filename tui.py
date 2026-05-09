import curses
import time
import requests
import threading
import textwrap

API_URL = "http://localhost:5000"
BROADCAST_ADDRS = ('^all', '^local', '!ffffffff')

class MeshTUI:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        curses.curs_set(0)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_RED, -1)
        self.state = {}
        self.messages = []
        self.neighbor_events = []
        self.active_channel = 0
        self.channels = {0: "LongFast", 1: "Ch 1", 2: "Ch 2", 3: "Ch 3", 4: "Ch 4", 5: "Ch 5", 6: "Ch 6", 7: "Ch 7"}
        self.dm_nodes = {}
        
        self.input_mode = False
        self.input_text = ""

        # Settings (full config) mode — replaces the old 3-field inline config_mode.
        # View states: 'sections' (list of config sections), 'fields' (fields of one
        # section), 'edit' (editing one field). Loaded lazily from /api/config.
        self.settings_mode = False
        self.settings_view = 'sections'
        self.settings_loading = False
        self.settings_data = None       # dict from GET /api/config
        self.settings_section_list = [] # [(group, name, label_for_header)]
        self.settings_section_idx = 0
        self.settings_field_idx = 0
        self.settings_edit_buffer = ""  # text-typed value during edit
        self.settings_edit_idx = 0      # selected index for enum edit
        self.settings_edit_bool = False # current bool value during edit
        self.settings_status = ""
        self.settings_status_time = 0.0
        self.settings_saving = False

        # Channels mode (key C). Views: 'list' | 'edit' | 'share' | 'import'.
        self.channels_mode = False
        self.channels_view = 'list'
        self.channels_loading = False
        self.channels_data = []          # list of channel dicts from /api/channels
        self.channels_idx = 0            # selected channel in list view
        self.channels_edit_field_idx = 0 # which row in edit form is focused
        self.channels_edit_buffers = {}  # mutated values during edit
        self.channels_share_url = ''
        self.channels_share_qr = []      # list of pre-rendered QR rows
        self.channels_import_buffer = ''
        self.channels_import_mode = 'replace'  # or 'add'
        self.channels_status = ''
        self.channels_status_time = 0.0
        self.channels_saving = False

        self.unread_tabs = set()
        self.node_list = []
        self.node_mode = False
        self.node_idx = 0
        self.last_event_time = 0.0
        self.last_server_time = 0.0
        self.offline_mode = False
        self.radio_offline = False
        self.running = True

    def fetch_data(self):
        while self.running:
            try:
                # Fetch state
                r_state = requests.get(f"{API_URL}/api/state", timeout=2)
                if r_state.status_code == 200:
                    self.offline_mode = False
                    self.radio_offline = False
                    new_state = r_state.json()
                    
                    # Detect Daemon Restart
                    server_time = new_state.get('server_time', 0.0)
                    if server_time < self.last_server_time:
                        # Daemon likely restarted, reset sync
                        self.last_event_time = 0.0
                        self.messages = []
                        self.neighbor_events = []
                    self.last_server_time = server_time
                    self.state = new_state
                    # Update tab labels from radio's actual channel names.
                    # The Meshtastic primary channel often has an empty name field
                    # (the modem-preset default — e.g. "LongFast"), so don't
                    # overwrite the existing label when name is blank.
                    for ch in new_state.get('channels') or []:
                        idx = ch.get('index')
                        name = (ch.get('name') or '').strip()
                        role = ch.get('role', 'DISABLED')
                        if not isinstance(idx, int) or not (0 <= idx < 8):
                            continue
                        if role == 'DISABLED':
                            self.channels[idx] = f"Ch {idx} (off)"
                        elif name:
                            self.channels[idx] = name
                        # else: leave the existing label (preserves "LongFast" for primary)
                elif r_state.status_code == 500:
                    self.offline_mode = False
                    self.radio_offline = True
                else:
                    self.offline_mode = True
                    
                # Fetch stream
                r_stream = requests.get(f"{API_URL}/api/stream?since={self.last_event_time}", timeout=2)
                if r_stream.status_code == 200:
                    events = r_stream.json()
                    for e in events:
                        self.last_event_time = max(self.last_event_time, e.get('time', 0.0))
                        if e.get('type') == 'text':
                            ch = e.get('channel', 0)
                            from_id = e.get('fromId')
                            to_id = e.get('toId')
                            local_id = self.state.get('local_id')
                            
                            is_dm = to_id not in BROADCAST_ADDRS
                            partner_id = from_id if from_id != local_id else to_id
                            tab_id = partner_id if is_dm else ch
                            
                            if tab_id != self.active_channel and from_id != local_id:
                                self.unread_tabs.add(tab_id)
                            
                            if is_dm:
                                target = from_id if from_id != local_id else to_id
                                if target and target not in self.dm_nodes:
                                    self.dm_nodes[target] = e.get('from', target) if from_id != local_id else target
                            
                            # Handle local messages: label history as 'You', skip live echoes
                            if from_id == local_id:
                                if self.last_event_time == 0.0: # This is a history fetch
                                    e['from'] = 'You'
                                else:
                                    continue # Skip brand new local echoes from the radio
                            
                            # Deduplicate by time if needed, but for now just append
                            self.messages.append(e)
                        elif e.get('type') == 'position':
                            self.neighbor_events.append(e)
                    
                    # Fetch nodes
                    r_nodes = requests.get(f"{API_URL}/api/nodes", timeout=2)
                    if r_nodes.status_code == 200:
                        self.node_list = r_nodes.json()
                else:
                    self.offline_mode = True
            except Exception:
                self.offline_mode = True
            time.sleep(1)

    def safe_addstr(self, y, x, text, attr=0):
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def draw_sidebar(self, h, mid_x, split1, split2):
        # Radio Stats
        self.safe_addstr(0, 2, " Radio Stats ", curses.color_pair(1) | curses.A_BOLD)
        self.safe_addstr(2, 2, f"Device: {self.state.get('name', 'Unknown')}")
        self.safe_addstr(3, 2, f"Local ID: {self.state.get('local_id', 'Unknown')}")
        uptime = self.state.get('uptime', 0)
        uptime_str = f"{uptime//3600:,}h {(uptime%3600)//60}m"
        self.safe_addstr(4, 2, f"Uptime: {uptime_str}")
        batt_level = min(100, self.state.get('battery_level', 0))
        self.safe_addstr(5, 2, f"Battery: {batt_level}% ({self.state.get('battery_voltage', 0.0)}V)")
        self.safe_addstr(6, 2, f"ChUtil: {self.state.get('chutil', 0.0):.2f}%")
        self.safe_addstr(7, 2, f"Nodes Online: {self.state.get('nodes_online', 0)}")
        
        # GPS Status
        self.safe_addstr(split1, 2, " GPS Status ", curses.color_pair(2) | curses.A_BOLD)
        sats = self.state.get('sats', 0)
        gps_live = self.state.get('gps_live', False)
        color = curses.color_pair(1) if sats >= 3 else curses.color_pair(3)
        self.safe_addstr(split1 + 2, 2, f"Sats In View: {sats}", color)
        
        lat = self.state.get('latitude', 0.0)
        lon = self.state.get('longitude', 0.0)
        status_text = "Acquiring..." if lat == 0.0 else ("Locked" if gps_live else "Cached (awaiting live fix)")
        self.safe_addstr(split1 + 3, 2, f"Lat: {lat:.5f}")
        self.safe_addstr(split1 + 4, 2, f"Lon: {lon:.5f}")
        pdop = self.state.get('pdop', 0) / 100.0
        self.safe_addstr(split1 + 6, 2, f"Precision: {pdop:.2f}m")
        self.safe_addstr(split1 + 7, 2, f"Status: {status_text}", color)
        
        # Node Neighbors
        self.safe_addstr(split2, 2, " Node Neighbors ", curses.color_pair(3) | curses.A_BOLD)
        tel_y = split2 + 2
        max_tel = h - split2 - 3
        for t in self.neighbor_events[-max_tel:]:
            sender = t.get('from', 'Unknown')
            pos = t.get('pos', {})
            line = f"{sender}: {pos.get('latitude', 0.0):.4f}, {pos.get('longitude', 0.0):.4f}"
            self.safe_addstr(tel_y, 2, line[:mid_x-4])
            tel_y += 1

    def draw_messages(self, h, w, mid_x):
        ch_name = self.channels.get(self.active_channel) if isinstance(self.active_channel, int) else self.dm_nodes.get(self.active_channel, self.active_channel)
        header = f" Messages ({'Channel' if isinstance(self.active_channel, int) else 'DM'}: {ch_name}) "
        self.safe_addstr(0, mid_x + 2, header, curses.color_pair(2) | curses.A_BOLD)
        
        if self.unread_tabs:
            unread_str = f" [Unread: {len(self.unread_tabs)}] "
            self.safe_addstr(0, w - len(unread_str) - 2, unread_str, curses.color_pair(4) | curses.A_BOLD)

        if self.node_mode:
            self.draw_node_selection(h, w, mid_x)
            return

        local_id = self.state.get('local_id')
        if isinstance(self.active_channel, int):
            filtered = [m for m in self.messages if m.get('channel') == self.active_channel and m.get('toId') in BROADCAST_ADDRS]
        else:
            filtered = [m for m in self.messages if (m.get('fromId') == self.active_channel and m.get('toId') == local_id) or (m.get('fromId') == local_id and m.get('toId') == self.active_channel)]

        msg_y = h - 6 # Start from bottom and work up
        max_y = 2
        
        # Reverse the list so we can draw from bottom up more easily, or just limit total lines
        for m in reversed(filtered):
            if msg_y <= max_y: break
            
            sender = m.get('from', 'Unknown')
            name_color = curses.color_pair(1)|curses.A_BOLD if sender == 'You' else (curses.color_pair(3)|curses.A_BOLD if not isinstance(self.active_channel, int) else curses.color_pair(2)|curses.A_BOLD)
            hop = m.get('hopLimit')
            display_name = f"{sender}{f'({hop})' if hop is not None else ''}: "
            text = m.get('text', '')
            
            available_w = w - mid_x - len(display_name) - 5
            wrapped = textwrap.wrap(text, available_w) or [""]
            
            # Draw lines from bottom of current message up
            for i, line in enumerate(reversed(wrapped)):
                if msg_y <= max_y: break
                if i == len(wrapped) - 1: # First line of message (has name)
                    self.safe_addstr(msg_y, mid_x + 2, display_name, name_color)
                    self.safe_addstr(msg_y, mid_x + 2 + len(display_name), line)
                else: # Continuation lines
                    self.safe_addstr(msg_y, mid_x + 2 + len(display_name), line)
                msg_y -= 1

    def draw_node_selection(self, h, w, mid_x):
        self.safe_addstr(2, mid_x + 2, "Discovery: SELECT NODE TO DM", curses.color_pair(3) | curses.A_BOLD)
        self.safe_addstr(3, mid_x + 2, f"{'ID':<10} {'NAME':<15} {'SNR':<5} {'LAST HEARD':<10}")
        self.safe_addstr(4, mid_x + 2, "-" * (w - mid_x - 5))
        
        start_y = 5
        max_rows = h - 10
        for i, node in enumerate(self.node_list[:max_rows]):
            attr = curses.A_REVERSE if i == self.node_idx else 0
            node_id = node.get('id', 'Unknown')
            name = node.get('long_name', node_id)
            snr = node.get('snr', 0)
            lh = node.get('last_heard', 0)
            lh_str = f"{int(time.time() - lh)}s ago" if lh > 0 else "Never"
            
            line = f"{node_id:<10} {name[:15]:<15} {snr:<5} {lh_str:<10}"
            self.safe_addstr(start_y + i, mid_x + 2, line[:w-mid_x-5], attr)

    def draw_input(self, h, mid_x, w):
        self.stdscr.hline(h-4, mid_x + 1, curses.ACS_HLINE, w - mid_x - 2)
        if self.input_mode:
            prompt = "Type message: "
            available_w = w - mid_x - len(prompt) - 4
            # Scroll input if too long
            display_text = self.input_text
            if len(display_text) > available_w:
                display_text = "..." + display_text[-(available_w-3):]
            
            self.safe_addstr(h-3, mid_x + 2, prompt + display_text)
            curses.curs_set(1)
            try: self.stdscr.move(h-3, mid_x + 2 + len(prompt) + len(display_text))
            except: pass
        elif self.node_mode:
            self.safe_addstr(h-3, mid_x + 2, "[UP/DOWN arrows] [ENTER to DM] [L to cancel]", curses.color_pair(3))
        elif self.settings_mode:
            available_w = w - mid_x - 4
            if self.settings_view == 'sections':
                hint = "[↑↓] section  [ENTER] open  [ESC] exit Settings"
            elif self.settings_view == 'fields':
                hint = "[↑↓] field    [ENTER] edit  [ESC] back"
            else:
                hint = "[ENTER] save  [ESC] cancel"
            self.safe_addstr(h-3, mid_x + 2, hint[:available_w], curses.color_pair(3))
        elif self.channels_mode:
            available_w = w - mid_x - 4
            if self.channels_view == 'list':
                hint = "[↑↓] select  [ENTER] edit  [U] share  [I] import  [ESC] exit"
            elif self.channels_view == 'edit':
                hint = "[↑↓] field  [←→/SPACE] toggle/cycle  [ENTER on Save/Delete]  [ESC] cancel"
            elif self.channels_view == 'share':
                hint = "[ESC] back"
            else:
                hint = "[type] URL  [M] toggle mode  [ENTER] import  [ESC] cancel"
            self.safe_addstr(h-3, mid_x + 2, hint[:available_w], curses.color_pair(3))
        else:
            curses.curs_set(0)
            available_w = w - mid_x - 4
            help_text = "[ENTER msg] [TAB ch/DM] [C channels] [O options] [L find nodes] [Q quit]"
            self.safe_addstr(h-3, mid_x + 2, help_text[:available_w], curses.color_pair(3))

    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        if w < 115 or h < 35:
            msg1 = " Terminal window too small "
            msg2 = " Please resize to at least 115x35 "
            msg3 = f" Current size: {w}x{h} "
            self.safe_addstr(max(0, h // 2 - 1), max(0, w // 2 - len(msg1) // 2), msg1, curses.color_pair(4) | curses.A_BOLD | curses.A_REVERSE)
            self.safe_addstr(max(1, h // 2), max(0, w // 2 - len(msg2) // 2), msg2)
            self.safe_addstr(max(2, h // 2 + 1), max(0, w // 2 - len(msg3) // 2), msg3)
            self.stdscr.refresh()
            return
            
        mid_x = w // 2
        # Fixed-size sidebar panes: Radio Stats and GPS each have ~8 rows of
        # static content; add 1 row of breathing room before each divider.
        # Node Neighbors absorbs the remaining vertical space.
        split1 = 9
        split2 = 18

        # Borders and Frames
        try:
            self.stdscr.box()
            self.stdscr.vline(1, mid_x, curses.ACS_VLINE, h - 2)
            self.stdscr.hline(split1, 1, curses.ACS_HLINE, mid_x - 1)
            self.stdscr.hline(split2, 1, curses.ACS_HLINE, mid_x - 1)
        except: pass
        
        self.draw_sidebar(h, mid_x, split1, split2)
        if self.settings_mode:
            self.draw_settings(h, w, mid_x)
        elif self.channels_mode:
            self.draw_channels(h, w, mid_x)
        else:
            self.draw_messages(h, w, mid_x)
        self.draw_input(h, mid_x, w)

        # Offline / reconnect banner overlays the right-pane header so the user
        # can't miss it. Reverse video instead of blink — many terminals don't
        # render A_BLINK (and it's visually noisy where they do).
        banner = None
        if self.offline_mode:
            banner = "  DAEMON OFFLINE — start it with: python manager.py start  "
        elif self.radio_offline:
            banner = "  RADIO OFFLINE — daemon reconnecting (config writes can trigger a reboot, ~30s)  "
        if banner:
            pane_x = mid_x + 2
            pane_w = max(1, w - mid_x - 4)
            banner = banner[:pane_w]
            self.safe_addstr(0, pane_x, banner,
                             curses.color_pair(4) | curses.A_BOLD | curses.A_REVERSE)
        self.stdscr.refresh()

    # ----- Settings mode (full config) -----

    INT_TYPES = {'int32', 'int64', 'uint32', 'uint64', 'sint32', 'sint64',
                 'fixed32', 'fixed64', 'sfixed32', 'sfixed64'}
    FLOAT_TYPES = {'float', 'double'}

    def fetch_settings_async(self):
        self.settings_loading = True
        self.settings_data = None
        try:
            r = requests.get(f"{API_URL}/api/config", timeout=30)
            if r.status_code == 200:
                self.settings_data = r.json()
                self._build_section_list()
                self.settings_section_idx = 0
                self.settings_field_idx = 0
            else:
                self.settings_status = f"Load error {r.status_code}"
                self.settings_status_time = time.time()
        except Exception as e:
            self.settings_status = f"Load error: {str(e)[:24]}"
            self.settings_status_time = time.time()
        self.settings_loading = False

    def save_field_async(self, section_key, field_name, value):
        self.settings_saving = True
        self.settings_status = "Saving..."
        self.settings_status_time = time.time()
        try:
            r = requests.post(f"{API_URL}/api/config",
                              json={'section': section_key, 'fields': {field_name: value}},
                              timeout=15)
            if r.status_code == 200:
                self.settings_status = "Saved — may reboot"
                # Patch the local cache so the field list reflects the new value immediately.
                fields = self._current_section_data()
                for f in fields:
                    if f.get('name') == field_name:
                        f['value'] = value
                        break
            else:
                err = ''
                try: err = r.json().get('error', '')
                except Exception: err = r.text[:40]
                self.settings_status = f"Err: {err[:24]}"
        except Exception as e:
            self.settings_status = f"Err: {str(e)[:24]}"
        self.settings_status_time = time.time()
        self.settings_saving = False

    def _build_section_list(self):
        out = []
        if self.settings_data.get('user'):
            out.append(('user', 'user', 'User'))
        for name in sorted(self.settings_data.get('localConfig', {}).keys()):
            out.append(('localConfig', name, 'Device'))
        for name in sorted(self.settings_data.get('moduleConfig', {}).keys()):
            out.append(('moduleConfig', name, 'Module'))
        self.settings_section_list = out

    def _current_section_data(self):
        if not self.settings_data or not self.settings_section_list:
            return []
        g, n, _ = self.settings_section_list[self.settings_section_idx]
        if g == 'user':
            return self.settings_data.get('user', [])
        return self.settings_data.get(g, {}).get(n, [])

    def _current_field(self):
        fields = self._current_section_data()
        if 0 <= self.settings_field_idx < len(fields):
            return fields[self.settings_field_idx]
        return None

    def _fmt_value(self, f):
        if f.get('skipped'):
            return '(complex)'
        t = f.get('type', '')
        v = f.get('value')
        if t == 'bool':
            return '[true]' if v else '[false]'
        if t == 'enum':
            return f"<{v}>"
        if t == 'string':
            return v if v else '(empty)'
        if t == 'bytes':
            s = str(v) if v else ''
            return (s[:18] + '…') if len(s) > 18 else (s or '(empty)')
        if t.startswith('repeated_'):
            return repr(v) if v else '[]'
        if v is None:
            return ''
        return str(v)

    def draw_settings(self, h, w, mid_x):
        # Header
        if self.settings_view == 'sections':
            header = " Settings — sections "
        elif self.settings_view == 'fields' and self.settings_section_list:
            sec = self.settings_section_list[self.settings_section_idx][1]
            header = f" Settings — {sec} "
        elif self.settings_view == 'edit' and self._current_field():
            header = f" Settings — editing {self._current_field()['name']} "
        else:
            header = " Settings "
        self.safe_addstr(0, mid_x + 2, header[:w - mid_x - 4],
                         curses.color_pair(2) | curses.A_BOLD)

        # Status indicator (top-right of the right pane)
        if self.settings_saving:
            tag = " SAVING "
            self.safe_addstr(0, w - len(tag) - 2, tag, curses.color_pair(3) | curses.A_BOLD)
        elif self.settings_status and time.time() - self.settings_status_time < 3:
            tag = f" {self.settings_status[:32]} "
            color = curses.color_pair(4) if self.settings_status.startswith('Err') else curses.color_pair(1)
            self.safe_addstr(0, w - len(tag) - 2, tag, color | curses.A_BOLD)

        if self.settings_loading:
            self.safe_addstr(h // 2, mid_x + 4, "Loading config from radio…", curses.color_pair(3))
            return
        if not self.settings_data:
            self.safe_addstr(h // 2, mid_x + 4, "No config available — ESC to exit",
                             curses.color_pair(4))
            return

        pane_x = mid_x + 2
        pane_top = 2
        pane_bot = h - 5
        pane_h = max(1, pane_bot - pane_top)
        pane_w = max(1, w - mid_x - 4)

        if self.settings_view == 'sections':
            self._draw_sections_view(pane_x, pane_top, pane_w, pane_h)
        elif self.settings_view == 'fields':
            self._draw_fields_view(pane_x, pane_top, pane_w, pane_h)
        else:
            self._draw_edit_view(pane_x, pane_top, pane_w, pane_h)

    def _draw_sections_view(self, x, y, w, h):
        # Build display rows: group headers interleaved with section names.
        rows = []
        last_label = None
        for i, (g, n, label) in enumerate(self.settings_section_list):
            if label != last_label:
                rows.append(('header', label))
                last_label = label
            rows.append(('item', i, n))
        # Map idx → display row
        target_row = next((r for r, v in enumerate(rows)
                           if v[0] == 'item' and v[1] == self.settings_section_idx), 0)
        start = max(0, target_row - h // 2)
        end = min(len(rows), start + h)
        for offs, row in enumerate(rows[start:end]):
            if row[0] == 'header':
                self.safe_addstr(y + offs, x, f"[{row[1]}]"[:w],
                                 curses.color_pair(3) | curses.A_BOLD)
            else:
                _, sect_idx, name = row
                attr = curses.A_REVERSE if sect_idx == self.settings_section_idx else 0
                self.safe_addstr(y + offs, x + 2, name[:w - 2], attr)

    def _draw_fields_view(self, x, y, w, h):
        fields = self._current_section_data()
        if not fields:
            self.safe_addstr(y, x, "(no fields)")
            return
        # Keep cursor visible
        if self.settings_field_idx < 0:
            self.settings_field_idx = 0
        if self.settings_field_idx >= len(fields):
            self.settings_field_idx = len(fields) - 1
        start = max(0, self.settings_field_idx - h + 1)
        if self.settings_field_idx < start:
            start = self.settings_field_idx
        end = min(len(fields), start + h)

        label_w = min(max(len(f.get('name', '')) for f in fields) + 2, w // 2)
        for offs, f in enumerate(fields[start:end]):
            idx = start + offs
            attr = curses.A_REVERSE if idx == self.settings_field_idx else 0
            label = f.get('name', '')[:label_w - 1]
            val = self._fmt_value(f)
            line = f"{label:<{label_w}}{val}"
            self.safe_addstr(y + offs, x, line[:w], attr)

    def _draw_edit_view(self, x, y, w, h):
        f = self._current_field()
        if not f:
            return
        t = f.get('type', '')
        self.safe_addstr(y + 1, x, f"Field: {f.get('name')}",
                         curses.color_pair(3) | curses.A_BOLD)
        self.safe_addstr(y + 2, x, f"Type:  {t}")

        if t == 'bool':
            val = '[TRUE]' if self.settings_edit_bool else '[FALSE]'
            self.safe_addstr(y + 4, x, f"Value: {val}",
                             curses.color_pair(1) | curses.A_BOLD)
            self.safe_addstr(y + 6, x, "[SPACE / ← →] toggle   [ENTER] save   [ESC] cancel",
                             curses.color_pair(3))
        elif t == 'enum':
            opts = f.get('enum_values') or []
            cur = opts[self.settings_edit_idx] if 0 <= self.settings_edit_idx < len(opts) else '?'
            self.safe_addstr(y + 4, x, f"Value: < {cur} >",
                             curses.color_pair(1) | curses.A_BOLD)
            self.safe_addstr(y + 5, x,
                             f"({self.settings_edit_idx + 1}/{len(opts)})", curses.color_pair(3))
            self.safe_addstr(y + 6, x, "[← →] cycle   [ENTER] save   [ESC] cancel",
                             curses.color_pair(3))
        else:
            prompt = "Value: "
            self.safe_addstr(y + 4, x, prompt + self.settings_edit_buffer,
                             curses.color_pair(1) | curses.A_BOLD)
            self.safe_addstr(y + 6, x, "[ENTER] save   [ESC] cancel", curses.color_pair(3))
            try:
                self.stdscr.move(y + 4, x + len(prompt) + len(self.settings_edit_buffer))
                curses.curs_set(1)
            except curses.error:
                pass

    def _enter_field_edit(self):
        f = self._current_field()
        if not f:
            return
        if f.get('skipped'):
            self.settings_status = "Read-only field"
            self.settings_status_time = time.time()
            return
        t = f.get('type', '')
        if t.startswith('repeated_') or t == 'message':
            self.settings_status = "Editing repeated/message fields not supported"
            self.settings_status_time = time.time()
            return
        v = f.get('value')
        if t == 'bool':
            self.settings_edit_bool = bool(v)
        elif t == 'enum':
            opts = f.get('enum_values') or []
            self.settings_edit_idx = opts.index(v) if v in opts else 0
        else:
            self.settings_edit_buffer = '' if v is None else str(v)
        self.settings_view = 'edit'

    def _submit_field_edit(self):
        f = self._current_field()
        if not f:
            return
        t = f.get('type', '')
        if t == 'bool':
            value = self.settings_edit_bool
        elif t == 'enum':
            opts = f.get('enum_values') or []
            if not opts:
                return
            value = opts[self.settings_edit_idx]
        elif t in self.INT_TYPES:
            try:
                value = int(self.settings_edit_buffer)
            except ValueError:
                self.settings_status = "Invalid integer"
                self.settings_status_time = time.time()
                return
        elif t in self.FLOAT_TYPES:
            try:
                value = float(self.settings_edit_buffer)
            except ValueError:
                self.settings_status = "Invalid number"
                self.settings_status_time = time.time()
                return
        else:
            value = self.settings_edit_buffer

        section_key = self.settings_section_list[self.settings_section_idx][1]
        threading.Thread(target=self.save_field_async,
                         args=(section_key, f['name'], value), daemon=True).start()
        self.settings_view = 'fields'
        curses.curs_set(0)

    def handle_settings_key(self, c):
        if self.settings_loading:
            if c == 27:  # ESC during load just exits
                self.settings_mode = False
            return

        if self.settings_view == 'sections':
            if c == 27:
                self.settings_mode = False
                return
            n = len(self.settings_section_list)
            if c == curses.KEY_UP:
                self.settings_section_idx = max(0, self.settings_section_idx - 1)
            elif c == curses.KEY_DOWN:
                self.settings_section_idx = min(max(0, n - 1), self.settings_section_idx + 1)
            elif c in (curses.KEY_ENTER, 10, 13) and n > 0:
                self.settings_view = 'fields'
                self.settings_field_idx = 0
            return

        if self.settings_view == 'fields':
            fields = self._current_section_data()
            if c == 27:
                self.settings_view = 'sections'
                return
            if c == curses.KEY_UP:
                self.settings_field_idx = max(0, self.settings_field_idx - 1)
            elif c == curses.KEY_DOWN:
                self.settings_field_idx = min(max(0, len(fields) - 1),
                                              self.settings_field_idx + 1)
            elif c in (curses.KEY_ENTER, 10, 13) and fields:
                self._enter_field_edit()
            return

        # 'edit' view
        f = self._current_field()
        if not f:
            self.settings_view = 'fields'
            return
        t = f.get('type', '')
        if c == 27:
            self.settings_view = 'fields'
            curses.curs_set(0)
            return
        if t == 'bool':
            if c in (32, curses.KEY_LEFT, curses.KEY_RIGHT):
                self.settings_edit_bool = not self.settings_edit_bool
            elif c in (curses.KEY_ENTER, 10, 13):
                self._submit_field_edit()
        elif t == 'enum':
            opts = f.get('enum_values') or []
            if opts:
                if c == curses.KEY_LEFT:
                    self.settings_edit_idx = (self.settings_edit_idx - 1) % len(opts)
                elif c == curses.KEY_RIGHT:
                    self.settings_edit_idx = (self.settings_edit_idx + 1) % len(opts)
                elif c in (curses.KEY_ENTER, 10, 13):
                    self._submit_field_edit()
        else:
            if c in (curses.KEY_ENTER, 10, 13):
                self._submit_field_edit()
            elif c in (curses.KEY_BACKSPACE, 127, 8):
                self.settings_edit_buffer = self.settings_edit_buffer[:-1]
            elif 32 <= c <= 126:
                self.settings_edit_buffer += chr(c)

    # ----- Channels mode -----

    CHANNEL_ROLES = ['DISABLED', 'PRIMARY', 'SECONDARY']
    CHANNEL_EDIT_ROWS = ['role', 'name', 'psk', 'uplink_enabled',
                         'downlink_enabled', '__save__', '__delete__']

    def _channels_set_status(self, msg):
        self.channels_status = msg
        self.channels_status_time = time.time()

    def fetch_channels_async(self):
        self.channels_loading = True
        try:
            r = requests.get(f"{API_URL}/api/channels", timeout=15)
            if r.status_code == 200:
                self.channels_data = r.json().get('channels', [])
                if self.channels_idx >= len(self.channels_data):
                    self.channels_idx = 0
            else:
                self._channels_set_status(f"Load error {r.status_code}")
        except Exception as e:
            self._channels_set_status(f"Load error: {str(e)[:24]}")
        self.channels_loading = False

    def save_channel_async(self, payload):
        self.channels_saving = True
        self._channels_set_status("Saving...")
        try:
            r = requests.post(f"{API_URL}/api/channels", json=payload, timeout=15)
            if r.status_code == 200:
                self._channels_set_status("Saved!")
                # Refresh list after a successful write
                self.fetch_channels_async()
            else:
                err = ''
                try: err = r.json().get('error', '')
                except Exception: err = r.text[:40]
                self._channels_set_status(f"Err: {err[:24]}")
        except Exception as e:
            self._channels_set_status(f"Err: {str(e)[:24]}")
        self.channels_saving = False

    def delete_channel_async(self, idx):
        self.channels_saving = True
        self._channels_set_status("Deleting...")
        try:
            r = requests.delete(f"{API_URL}/api/channels/{idx}", timeout=10)
            if r.status_code == 200:
                self._channels_set_status("Deleted")
                self.fetch_channels_async()
            else:
                self._channels_set_status(f"Err: {r.status_code}")
        except Exception as e:
            self._channels_set_status(f"Err: {str(e)[:24]}")
        self.channels_saving = False

    def fetch_share_url_async(self):
        self.channels_loading = True
        self.channels_share_url = ''
        self.channels_share_qr = []
        try:
            r = requests.get(f"{API_URL}/api/channels/url", timeout=10)
            if r.status_code == 200:
                url = r.json().get('url', '')
                self.channels_share_url = url
                self.channels_share_qr = self._render_qr_ascii(url)
            else:
                self._channels_set_status(f"URL load err {r.status_code}")
        except Exception as e:
            self._channels_set_status(f"URL err: {str(e)[:24]}")
        self.channels_loading = False

    def import_channel_url_async(self, url, mode):
        self.channels_saving = True
        self._channels_set_status("Importing...")
        try:
            r = requests.post(f"{API_URL}/api/channels/import",
                              json={'url': url, 'mode': mode}, timeout=15)
            if r.status_code == 200:
                self._channels_set_status("Imported")
                self.fetch_channels_async()
            else:
                err = ''
                try: err = r.json().get('error', '')
                except Exception: err = r.text[:40]
                self._channels_set_status(f"Err: {err[:24]}")
        except Exception as e:
            self._channels_set_status(f"Err: {str(e)[:24]}")
        self.channels_saving = False

    @staticmethod
    def _render_qr_ascii(text):
        try:
            import qrcode
        except ImportError:
            return ['(install "qrcode" for QR rendering)']
        qr = qrcode.QRCode(border=1, box_size=1)
        qr.add_data(text)
        qr.make(fit=True)
        m = qr.modules
        out = []
        for y in range(0, len(m), 2):
            row = []
            for x in range(len(m[0])):
                top = m[y][x]
                bot = m[y + 1][x] if y + 1 < len(m) else False
                row.append('█' if top and bot else ('▀' if top else ('▄' if bot else ' ')))
            out.append(''.join(row))
        return out

    def _enter_channel_edit(self):
        if not self.channels_data:
            return
        ch = self.channels_data[self.channels_idx]
        self.channels_edit_buffers = {
            'role':              ch.get('role', 'DISABLED'),
            'name':              ch.get('name', ''),
            'psk':               '',  # blank means "leave PSK unchanged"
            'uplink_enabled':    bool(ch.get('uplink_enabled', False)),
            'downlink_enabled':  bool(ch.get('downlink_enabled', False)),
        }
        self.channels_edit_field_idx = 0
        self.channels_view = 'edit'

    def _submit_channel_edit(self):
        if not self.channels_data:
            return
        ch = self.channels_data[self.channels_idx]
        b = self.channels_edit_buffers
        payload = {
            'index':            ch['index'],
            'role':             b['role'],
            'name':             b['name'],
            'uplink_enabled':   bool(b['uplink_enabled']),
            'downlink_enabled': bool(b['downlink_enabled']),
        }
        if b['psk'].strip():
            payload['psk'] = b['psk'].strip()
        threading.Thread(target=self.save_channel_async, args=(payload,), daemon=True).start()
        self.channels_view = 'list'
        curses.curs_set(0)

    def _delete_current_channel(self):
        if not self.channels_data:
            return
        idx = self.channels_data[self.channels_idx]['index']
        threading.Thread(target=self.delete_channel_async, args=(idx,), daemon=True).start()
        self.channels_view = 'list'
        curses.curs_set(0)

    def draw_channels(self, h, w, mid_x):
        # Header
        if self.channels_view == 'list':
            header = " Channels — list "
        elif self.channels_view == 'edit' and self.channels_data:
            ch = self.channels_data[self.channels_idx]
            header = f" Channels — edit #{ch['index']} "
        elif self.channels_view == 'share':
            header = " Channels — share "
        elif self.channels_view == 'import':
            header = " Channels — import URL "
        else:
            header = " Channels "
        self.safe_addstr(0, mid_x + 2, header[:w - mid_x - 4],
                         curses.color_pair(2) | curses.A_BOLD)

        # Status indicator
        if self.channels_saving:
            tag = " SAVING "
            self.safe_addstr(0, w - len(tag) - 2, tag, curses.color_pair(3) | curses.A_BOLD)
        elif self.channels_status and time.time() - self.channels_status_time < 3:
            tag = f" {self.channels_status[:32]} "
            color = curses.color_pair(4) if self.channels_status.startswith('Err') else curses.color_pair(1)
            self.safe_addstr(0, w - len(tag) - 2, tag, color | curses.A_BOLD)

        if self.channels_loading and self.channels_view in ('list', 'share'):
            self.safe_addstr(h // 2, mid_x + 4, "Loading…", curses.color_pair(3))
            return

        pane_x = mid_x + 2
        pane_top = 2
        pane_h = max(1, h - 5 - pane_top)
        pane_w = max(1, w - mid_x - 4)

        if self.channels_view == 'list':
            self._draw_channels_list(pane_x, pane_top, pane_w, pane_h)
        elif self.channels_view == 'edit':
            self._draw_channels_edit(pane_x, pane_top, pane_w, pane_h)
        elif self.channels_view == 'share':
            self._draw_channels_share(pane_x, pane_top, pane_w, pane_h)
        elif self.channels_view == 'import':
            self._draw_channels_import(pane_x, pane_top, pane_w, pane_h)

    def _draw_channels_list(self, x, y, w, h):
        if not self.channels_data:
            self.safe_addstr(y, x, "(no channels — radio not loaded)")
            return
        self.safe_addstr(y, x, f"{'#':<3} {'role':<10} {'name':<14} psk", curses.color_pair(3))
        for offs, ch in enumerate(self.channels_data[:h - 1]):
            attr = curses.A_REVERSE if offs == self.channels_idx else 0
            line = f"{ch['index']:<3} {ch.get('role','?')[:10]:<10} {(ch.get('name','') or '—')[:14]:<14} {ch.get('psk_kind','?')}"
            self.safe_addstr(y + 1 + offs, x, line[:w], attr)

    def _draw_channels_edit(self, x, y, w, h):
        if not self.channels_data:
            return
        ch = self.channels_data[self.channels_idx]
        b = self.channels_edit_buffers
        self.safe_addstr(y, x, f"Editing channel #{ch['index']}",
                         curses.color_pair(3) | curses.A_BOLD)

        rows = []
        rows.append(('role',              f"< {b['role']} >"))
        rows.append(('name',              b['name']))
        rows.append(('psk',               b['psk'] or "(unchanged)"))
        rows.append(('uplink_enabled',    '[true]' if b['uplink_enabled'] else '[false]'))
        rows.append(('downlink_enabled',  '[true]' if b['downlink_enabled'] else '[false]'))
        rows.append(('__save__',          '[ Save ]'))
        rows.append(('__delete__',        '[ Delete channel ]'))

        cur_field = self.CHANNEL_EDIT_ROWS[self.channels_edit_field_idx]
        for i, (key, disp) in enumerate(rows):
            attr = curses.A_REVERSE if key == cur_field else 0
            label = "" if key.startswith('__') else f"{key}: "
            self.safe_addstr(y + 2 + i, x, (label + disp)[:w], attr)

        # Contextual hint for the focused field
        hint = None
        if cur_field == 'psk':
            hint = "blank=keep current, none, default, random, simple0-9, or hex"
        elif cur_field == 'role':
            hint = "PRIMARY (default channel) / SECONDARY (extra) / DISABLED (off)"
        if hint:
            self.safe_addstr(y + 2 + len(rows) + 1, x, hint[:w], curses.color_pair(3))

        # Cursor on text fields
        if cur_field in ('name', 'psk'):
            row_idx = ['role', 'name', 'psk', 'uplink_enabled', 'downlink_enabled'].index(cur_field)
            label_len = len(cur_field) + 2  # "<name>: "
            try:
                self.stdscr.move(y + 2 + row_idx, x + label_len + len(b[cur_field]))
                curses.curs_set(1)
            except curses.error:
                pass
        else:
            curses.curs_set(0)

    def _draw_channels_share(self, x, y, w, h):
        url = self.channels_share_url or '(no URL)'
        self.safe_addstr(y, x, "Shareable URL:", curses.color_pair(3) | curses.A_BOLD)
        # Wrap URL across lines for the narrow pane
        cur_y = y + 1
        for i in range(0, len(url), w):
            self.safe_addstr(cur_y, x, url[i:i+w])
            cur_y += 1
        cur_y += 1
        if self.channels_share_qr:
            self.safe_addstr(cur_y, x, "Scan QR:", curses.color_pair(3) | curses.A_BOLD)
            cur_y += 1
            for line in self.channels_share_qr:
                if cur_y >= y + h:
                    break
                self.safe_addstr(cur_y, x, line[:w])
                cur_y += 1

    def _draw_channels_import(self, x, y, w, h):
        self.safe_addstr(y, x, "Paste a meshtastic.org/e/# URL to import.",
                         curses.color_pair(3))
        mode_disp = f"< {self.channels_import_mode} >"
        self.safe_addstr(y + 2, x, f"Mode: {mode_disp}    [M] toggle replace/add",
                         curses.color_pair(2))
        self.safe_addstr(y + 4, x, "URL: " + self.channels_import_buffer,
                         curses.color_pair(1) | curses.A_BOLD)
        try:
            self.stdscr.move(y + 4, x + 5 + len(self.channels_import_buffer))
            curses.curs_set(1)
        except curses.error:
            pass

    def handle_channels_key(self, c):
        if self.channels_loading and self.channels_view in ('list', 'share'):
            if c == 27:
                self.channels_mode = False
            return

        if self.channels_view == 'list':
            n = len(self.channels_data)
            if c == 27:
                self.channels_mode = False
            elif c == curses.KEY_UP:
                self.channels_idx = max(0, self.channels_idx - 1)
            elif c == curses.KEY_DOWN:
                self.channels_idx = min(max(0, n - 1), self.channels_idx + 1)
            elif c in (curses.KEY_ENTER, 10, 13) and n > 0:
                self._enter_channel_edit()
            elif c in (ord('u'), ord('U')):
                self.channels_view = 'share'
                threading.Thread(target=self.fetch_share_url_async, daemon=True).start()
            elif c in (ord('i'), ord('I')):
                self.channels_view = 'import'
                self.channels_import_buffer = ''
                self.channels_import_mode = 'replace'
            return

        if self.channels_view == 'edit':
            if c == 27:
                self.channels_view = 'list'
                curses.curs_set(0)
                return
            n = len(self.CHANNEL_EDIT_ROWS)
            cur = self.CHANNEL_EDIT_ROWS[self.channels_edit_field_idx]

            if c == curses.KEY_UP:
                self.channels_edit_field_idx = max(0, self.channels_edit_field_idx - 1)
                return
            if c == curses.KEY_DOWN:
                self.channels_edit_field_idx = min(n - 1, self.channels_edit_field_idx + 1)
                return

            b = self.channels_edit_buffers
            if cur == 'role':
                if c == curses.KEY_LEFT:
                    i = self.CHANNEL_ROLES.index(b['role'])
                    b['role'] = self.CHANNEL_ROLES[(i - 1) % len(self.CHANNEL_ROLES)]
                elif c == curses.KEY_RIGHT:
                    i = self.CHANNEL_ROLES.index(b['role'])
                    b['role'] = self.CHANNEL_ROLES[(i + 1) % len(self.CHANNEL_ROLES)]
            elif cur in ('uplink_enabled', 'downlink_enabled'):
                if c in (curses.KEY_LEFT, curses.KEY_RIGHT, 32):
                    b[cur] = not b[cur]
            elif cur in ('name', 'psk'):
                if c in (curses.KEY_BACKSPACE, 127, 8):
                    b[cur] = b[cur][:-1]
                elif 32 <= c <= 126:
                    b[cur] += chr(c)
            elif cur == '__save__':
                if c in (curses.KEY_ENTER, 10, 13):
                    self._submit_channel_edit()
            elif cur == '__delete__':
                if c in (curses.KEY_ENTER, 10, 13):
                    self._delete_current_channel()
            return

        if self.channels_view == 'share':
            if c == 27:
                self.channels_view = 'list'
            return

        if self.channels_view == 'import':
            if c == 27:
                self.channels_view = 'list'
                curses.curs_set(0)
                return
            if c in (ord('m'), ord('M')) and not self.channels_import_buffer:
                # Allow toggling mode only when buffer is empty so it doesn't conflict with typing
                self.channels_import_mode = 'add' if self.channels_import_mode == 'replace' else 'replace'
                return
            if c in (curses.KEY_ENTER, 10, 13):
                if self.channels_import_buffer.strip():
                    threading.Thread(target=self.import_channel_url_async,
                                     args=(self.channels_import_buffer.strip(),
                                           self.channels_import_mode),
                                     daemon=True).start()
                    self.channels_view = 'list'
                    curses.curs_set(0)
                return
            if c in (curses.KEY_BACKSPACE, 127, 8):
                self.channels_import_buffer = self.channels_import_buffer[:-1]
            elif 32 <= c <= 126:
                self.channels_import_buffer += chr(c)

    def run(self):
        self.stdscr.nodelay(True)
        self.stdscr.timeout(500)
        
        # Start fetch thread
        t = threading.Thread(target=self.fetch_data, daemon=True)
        t.start()
        
        while self.running:
            try:
                self.draw()
                c = self.stdscr.getch()
                if c != -1:
                    if self.input_mode:
                        if c in (curses.KEY_ENTER, 10, 13):
                            # Send message
                            if self.input_text.strip():
                                payload = {"message": self.input_text.strip()}
                                if not isinstance(self.active_channel, int):
                                    payload["destination"] = self.active_channel
                                    
                                try:
                                    requests.post(f"{API_URL}/api/send", json=payload, timeout=2)
                                    
                                    # local echo
                                    self.messages.append({
                                        'time': time.time(), 
                                        'channel': self.active_channel if isinstance(self.active_channel, int) else 0, 
                                        'from': 'You', 
                                        'fromId': self.state.get('local_id'),
                                        'toId': '^all' if isinstance(self.active_channel, int) else self.active_channel,
                                        'text': self.input_text.strip(),
                                        'hopLimit': self.state.get('hop_limit', 3) 
                                    })
                                except:
                                    pass # suppress crash, UI will just not show the echo if it fails
                                    
                                self.input_text = ""
                            self.input_mode = False
                        elif c == 27: # ESC
                            self.input_mode = False
                            self.input_text = ""
                        elif c in (curses.KEY_BACKSPACE, 127, 8):
                            self.input_text = self.input_text[:-1]
                        elif 32 <= c <= 126: # Printable chars
                            self.input_text += chr(c)
                    elif self.settings_mode:
                        self.handle_settings_key(c)
                    elif self.channels_mode:
                        self.handle_channels_key(c)
                    elif self.node_mode:
                        if c == ord('l') or c == ord('L') or c == 27: # ESC
                            self.node_mode = False
                        elif c == curses.KEY_UP:
                            self.node_idx = max(0, self.node_idx - 1)
                        elif c == curses.KEY_DOWN:
                            self.node_idx = min(len(self.node_list) - 1, self.node_idx + 1)
                        elif c in (10, 13): # ENTER
                            if self.node_list:
                                target = self.node_list[self.node_idx]
                                node_id = target.get('id')
                                self.dm_nodes[node_id] = target.get('long_name', node_id)
                                self.active_channel = node_id
                                self.node_mode = False
                    else:
                        if c == ord('q') or c == ord('Q'):
                            self.running = False
                        elif c == ord('o') or c == ord('O'):
                            self.settings_mode = True
                            self.settings_view = 'sections'
                            self.settings_section_idx = 0
                            self.settings_field_idx = 0
                            threading.Thread(target=self.fetch_settings_async,
                                             daemon=True).start()
                        elif c == ord('c') or c == ord('C'):
                            self.channels_mode = True
                            self.channels_view = 'list'
                            self.channels_idx = 0
                            threading.Thread(target=self.fetch_channels_async,
                                             daemon=True).start()
                        elif c == ord('l') or c == ord('L'):
                            self.node_mode = True
                            self.node_idx = 0
                        elif c == 9: # TAB
                            # Cycle channels (0-7 standard) + DM nodes
                            dm_list = list(self.dm_nodes.keys())
                            tab_order = list(range(8)) + dm_list
                            try:
                                curr_idx = tab_order.index(self.active_channel)
                                self.active_channel = tab_order[(curr_idx + 1) % len(tab_order)]
                            except ValueError:
                                self.active_channel = 0
                            
                            if self.active_channel in self.unread_tabs:
                                self.unread_tabs.remove(self.active_channel)
                        elif c in (curses.KEY_ENTER, 10, 13):
                            self.input_mode = True
                            self.input_text = ""
            except curses.error:
                pass

def main():
    try:
        curses.wrapper(lambda stdscr: MeshTUI(stdscr).run())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
