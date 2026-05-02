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
        self.config_mode = False
        self.config_idx = 0
        self.config_buffers = ["", "", "", ""] # 4 fields now
        self.config_status = ""
        self.config_status_time = 0
        self.config_saving = False
        self.unread_tabs = set()
        self.node_list = []
        self.node_mode = False
        self.node_idx = 0
        self.last_event_time = 0.0
        self.last_server_time = 0.0
        self.offline_mode = False
        self.running = True

    def fetch_data(self):
        while self.running:
            try:
                # Fetch state
                r_state = requests.get(f"{API_URL}/api/state", timeout=2)
                if r_state.status_code == 200:
                    self.offline_mode = False
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

    def draw_sidebar(self, h, mid_x, split1, split2, split3):
        # Radio Stats
        self.safe_addstr(0, 2, " Radio Stats ", curses.color_pair(1) | curses.A_BOLD)
        self.safe_addstr(2, 2, f"Device: {self.state.get('name', 'Unknown')}")
        self.safe_addstr(3, 2, f"Local ID: {self.state.get('local_id', 'Unknown')}")
        uptime = self.state.get('uptime', 0)
        uptime_str = f"{uptime//3600:,}h {(uptime%3600)//60}m"
        self.safe_addstr(4, 2, f"Uptime: {uptime_str}")
        self.safe_addstr(5, 2, f"Battery: {self.state.get('battery_level', 0)}% ({self.state.get('battery_voltage', 0.0)}V)")
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
        max_tel = split3 - split2 - 2
        for t in self.neighbor_events[-max_tel:]:
            sender = t.get('from', 'Unknown')
            pos = t.get('pos', {})
            line = f"{sender}: {pos.get('latitude', 0.0):.4f}, {pos.get('longitude', 0.0):.4f}"
            self.safe_addstr(tel_y, 2, line[:mid_x-4])
            tel_y += 1
            
        # Device Config
        status_line = ""
        status_color = 0
        if self.config_saving:
            status_line = "[ SENDING... ]"
            status_color = curses.color_pair(3) | curses.A_BOLD
        elif self.config_status and time.time() - self.config_status_time < 3:
            status_line = f"[ {self.config_status} ]"
            status_color = curses.color_pair(2) | curses.A_BOLD

        self.safe_addstr(split3, 2, " Device Config ", curses.color_pair(4) | curses.A_BOLD)
        if status_line:
            self.safe_addstr(split3, mid_x - len(status_line) - 2, status_line, status_color)
            
        conf_y = split3 + 1
        fields = ["Long Name", "Short Name", "Hop Limit", "CLI Command"]
        for i, field in enumerate(fields):
            color = curses.color_pair(1) if (self.config_mode and self.config_idx == i) else 0
            label = f"{field}: "
            self.safe_addstr(conf_y + i, 2, label)
            
            if self.config_mode:
                val = self.config_buffers[i]
            else:
                if i == 0: val = self.state.get('long_name')
                elif i == 1: val = self.state.get('short_name')
                elif i == 2: val = self.state.get('hop_limit', 3)
                else: val = ""
                
            self.safe_addstr(conf_y + i, 2 + len(label), str(val or "")[:mid_x-len(label)-3], color)

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
        else:
            curses.curs_set(0)
            available_w = w - mid_x - 4
            help_text = "[ENTER to msg] [TAB ch/DM] [C config] [L find nodes]"
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
        split1, split2, split3 = h // 4, (h * 2) // 4, (h * 3) // 4
        
        # Borders and Frames
        try:
            self.stdscr.box()
            self.stdscr.vline(1, mid_x, curses.ACS_VLINE, h - 2)
            self.stdscr.hline(split1, 1, curses.ACS_HLINE, mid_x - 1)
            self.stdscr.hline(split2, 1, curses.ACS_HLINE, mid_x - 1)
            self.stdscr.hline(split3, 1, curses.ACS_HLINE, mid_x - 1)
        except: pass
        
        if self.offline_mode:
            self.safe_addstr(0, w - 20, " [ DAEMON OFFLINE ] ", curses.color_pair(4) | curses.A_BLINK | curses.A_BOLD)

        self.draw_sidebar(h, mid_x, split1, split2, split3)
        self.draw_messages(h, w, mid_x)
        self.draw_input(h, mid_x, w)
        self.stdscr.refresh()

    def save_config_async(self, payload, idx):
        self.config_saving = True
        try:
            # Optimistic Update for immediate feedback
            if idx == 0: self.state['long_name'] = payload['long_name']
            elif idx == 1: self.state['short_name'] = payload['short_name']
            elif idx == 2: self.state['hop_limit'] = payload['hop_limit']
            
            resp = requests.post(f"{API_URL}/api/config/apply", json=payload, timeout=10)
            if resp.status_code == 200:
                self.config_status = "Saved!"
            else:
                self.config_status = f"Err: {resp.status_code}"
        except Exception as e:
            self.config_status = f"Error: {str(e)[:10]}"
        
        self.config_saving = False
        self.config_status_time = time.time()

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
                    elif self.config_mode:
                        if c == 27: # ESC
                            self.config_mode = False
                        elif c == 9: # TAB
                            self.config_idx = (self.config_idx + 1) % 4
                        elif c in (10, 13): # ENTER
                            payload = {}
                            field_val = self.config_buffers[self.config_idx].strip()
                            if self.config_idx == 0: payload['long_name'] = field_val
                            elif self.config_idx == 1: payload['short_name'] = field_val
                            elif self.config_idx == 2: 
                                try: payload['hop_limit'] = int(field_val or 3)
                                except: payload['hop_limit'] = 3
                            elif self.config_idx == 3: payload['command'] = field_val
                            
                            # Start non-blocking save
                            threading.Thread(target=self.save_config_async, args=(payload, self.config_idx), daemon=True).start()
                            
                            if self.config_idx == 3: 
                                self.config_buffers[3] = "" 
                            else: 
                                self.config_mode = False
                        elif c in (curses.KEY_BACKSPACE, 127, 8):
                            self.config_buffers[self.config_idx] = self.config_buffers[self.config_idx][:-1]
                        elif 32 <= c <= 126:
                            self.config_buffers[self.config_idx] += chr(c)
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
                        elif c == ord('c') or c == ord('C'):
                            self.config_mode = True
                            self.config_idx = 0
                            self.config_buffers = [
                                self.state.get('long_name', ''), 
                                self.state.get('short_name', ''), 
                                str(self.state.get('hop_limit', 3)), 
                                ''
                            ]
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
