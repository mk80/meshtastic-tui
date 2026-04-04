import curses
import time
import requests
import threading

API_URL = "http://localhost:5000"

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
        self.config_buffers = ["", "", ""]
        self.config_status = ""
        self.config_status_time = 0
        self.unread_tabs = set()
        self.last_event_time = 0.0
        self.running = True

    def fetch_data(self):
        while self.running:
            try:
                # Fetch state
                r_state = requests.get(f"{API_URL}/api/state", timeout=2)
                if r_state.status_code == 200:
                    self.state = r_state.json()
                    
                # Fetch stream
                r_stream = requests.get(f"{API_URL}/api/stream?since={self.last_event_time}", timeout=2)
                if r_stream.status_code == 200:
                    events = r_stream.json()
                    for e in events:
                        self.last_event_time = max(self.last_event_time, e['time'])
                        if e.get('type') == 'text':
                            # DM Detection: if toId is not a broadcast ID
                            ch = e.get('channel', 0)
                            from_id = e.get('fromId')
                            to_id = e.get('toId')
                            local_id = self.state.get('local_id')
                            
                            # Is it a DM?
                            is_dm = to_id not in ('^all', '^local', '!ffffffff')
                            partner_id = from_id if from_id != local_id else to_id
                            tab_id = partner_id if is_dm else ch
                            
                            # Skip unread for our own messages
                            if tab_id != self.active_channel and from_id != local_id:
                                self.unread_tabs.add(tab_id)
                            
                            # Add new DM nodes to manager if we are talking to them
                            if is_dm:
                                target = from_id if from_id != local_id else to_id
                                if target and target not in self.dm_nodes:
                                    self.dm_nodes[target] = e.get('from', target) if from_id != local_id else target
                            
                            self.messages.append(e)
                        elif e.get('type') == 'position':
                            self.neighbor_events.append(e)
            except Exception:
                pass
            time.sleep(1)

    def safe_addstr(self, y, x, text, attr=0):
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        
        if w < 60 or h < 15:
            self.safe_addstr(0, 0, "Terminal too small!")
            self.stdscr.refresh()
            return
            
        mid_x = w // 2
        split1 = h // 4
        split2 = (h * 2) // 4
        split3 = (h * 3) // 4
        
        # Draw Borders
        self.stdscr.hline(0, 0, curses.ACS_HLINE, w)
        self.stdscr.hline(h - 1, 0, curses.ACS_HLINE, w) # Bottom border
        self.stdscr.vline(0, 0, curses.ACS_VLINE, h)
        self.stdscr.vline(0, w - 1, curses.ACS_VLINE, h)
        
        # Split vertical
        self.stdscr.vline(0, mid_x, curses.ACS_VLINE, h)
        # Left side horizontal splits
        self.stdscr.hline(split1, 0, curses.ACS_HLINE, mid_x)
        self.stdscr.hline(split2, 0, curses.ACS_HLINE, mid_x)
        self.stdscr.hline(split3, 0, curses.ACS_HLINE, mid_x)
        
        # T-Junctions
        try:
            self.stdscr.addch(0, mid_x, curses.ACS_TTEE)
            self.stdscr.addch(h - 1, mid_x, curses.ACS_BTEE)
            self.stdscr.addch(split1, 0, curses.ACS_LTEE)
            self.stdscr.addch(split1, mid_x, curses.ACS_RTEE)
            self.stdscr.addch(split2, 0, curses.ACS_LTEE)
            self.stdscr.addch(split2, mid_x, curses.ACS_RTEE)
            self.stdscr.addch(split3, 0, curses.ACS_LTEE)
            self.stdscr.addch(split3, mid_x, curses.ACS_RTEE)
        except curses.error:
            pass
        
        self.safe_addstr(0, 2, " Radio Stats ", curses.color_pair(1) | curses.A_BOLD)
        self.safe_addstr(split1, 2, " GPS Status ", curses.color_pair(2) | curses.A_BOLD)
        self.safe_addstr(split2, 2, " Node Neighbors ", curses.color_pair(3) | curses.A_BOLD)
        self.safe_addstr(split3, 2, " Device Config ", curses.color_pair(4) | curses.A_BOLD)
        if isinstance(self.active_channel, int):
            ch_name = self.channels.get(self.active_channel, str(self.active_channel))
        else:
            ch_name = self.dm_nodes.get(self.active_channel, self.active_channel)
            
        # Message header
        unread_str = ""
        if self.unread_tabs:
            unread_list = []
            for ut in sorted(list(self.unread_tabs), key=lambda x: str(x)):
                if isinstance(ut, int):
                    unread_list.append(str(ut))
                else:
                    # truncate node ID or use name if available
                    name = self.dm_nodes.get(ut, ut[-4:])
                    unread_list.append(name)
            unread_str = f" [Unread: {', '.join(unread_list)}]"

        header_text = f" Messages ({'Channel' if isinstance(self.active_channel, int) else 'DM'}: {ch_name}){unread_str} "
        self.safe_addstr(0, mid_x + 2, header_text, curses.color_pair(2) | curses.A_BOLD)
        
        if unread_str:
            # Highlight unread count in red if any
            self.safe_addstr(0, mid_x + 2 + header_text.find("[Unread:"), unread_str, curses.color_pair(4) | curses.A_BOLD)
        
        # Populate Radio Stats
        self.safe_addstr(2, 2, f"Device: {self.state.get('name', 'Unknown')}")
        self.safe_addstr(3, 2, f"Local ID: {self.state.get('local_id', 'Unknown')}")
        
        uptime = self.state.get('uptime', 0)
        uptime_str = f"{uptime//3600}h {(uptime%3600)//60}m"
        self.safe_addstr(4, 2, f"Uptime: {uptime_str}")
        
        self.safe_addstr(5, 2, f"Battery: {self.state.get('battery_level', 0)}% ({self.state.get('battery_voltage', 0.0)}V)")
        self.safe_addstr(6, 2, f"ChUtil: {self.state.get('chutil', 0.0):.2f}%")
        self.safe_addstr(7, 2, f"Nodes Online: {self.state.get('nodes_online', 0)}")
        
        # Populate GPS
        sats = self.state.get('sats', 0)
        gps_live = self.state.get('gps_live', False)
        color = curses.color_pair(1) if sats >= 3 else curses.color_pair(3)
        self.safe_addstr(split1 + 2, 2, f"Sats In View: {sats}", color)
        
        lat = self.state.get('latitude', 0.0)
        lon = self.state.get('longitude', 0.0)
        if lat == 0.0:
            status_text = "Acquiring..."
        elif not gps_live:
            status_text = "Cached (awaiting live fix)"
        else:
            status_text = "Locked"
        self.safe_addstr(split1 + 3, 2, f"Lat: {lat:.5f}")
        self.safe_addstr(split1 + 4, 2, f"Lon: {lon:.5f}")
        
        pdop = self.state.get('pdop', 0) / 100.0
        self.safe_addstr(split1 + 6, 2, f"Precision: {pdop:.2f}m")
        self.safe_addstr(split1 + 7, 2, f"Status: {status_text}", color)
        
        # Populate Node Neighbors
        tel_y = split2 + 2
        max_tel_msgs = split3 - split2 - 2
        
        show_tel = self.neighbor_events[-max_tel_msgs:]
        for t in show_tel:
            sender = t.get('from', 'Unknown')
            pos = t.get('pos', {})
            
            lat = pos.get('latitude', 0.0)
            lon = pos.get('longitude', 0.0)
            
            hop = t.get('hopLimit')
            hop_str = f"({hop})" if hop is not None else ""
            line_str = f"{sender}{hop_str}: {lat:.4f}, {lon:.4f}"
            
            # Show Sats if available
            sats = pos.get('satsInView')
            if sats is not None:
                line_str += f" ({sats}s)"

            max_len = mid_x - 4
            if len(line_str) > max_len:
                line_str = line_str[:max_len-3] + "..."
                
            self.safe_addstr(tel_y, 2, line_str)
            tel_y += 1
            
        # Draw Configuration Pane
        conf_y = split3 + 1
        fields = ["Long Name", "Short Name", "CLI Command"]
        for i, field in enumerate(fields):
            color = curses.color_pair(1) if (self.config_mode and self.config_idx == i) else 0
            label = f"{field}: "
            self.safe_addstr(conf_y + i, 2, label)
            
            val = self.config_buffers[i] if self.config_mode else (self.state.get('long_name') if i == 0 else (self.state.get('short_name') if i == 1 else ""))
            
            # Ensure val is a string
            val_str = str(val) if val is not None else ""
            self.safe_addstr(conf_y + i, 2 + len(label), val_str, color)
            
        if self.config_status and time.time() - self.config_status_time < 3:
            self.safe_addstr(split3, mid_x - len(self.config_status) - 2, f" {self.config_status} ", curses.color_pair(2) | curses.A_BOLD)
        elif not self.config_mode:
            self.safe_addstr(h - 2, 2, "Press 'C' to Config", curses.A_DIM)
        
        # Populate Messages
        msg_y = 2
        max_msgs = h - 6 # account for headers and input bar
        
        local_id = self.state.get('local_id')
        if isinstance(self.active_channel, int):
            # Show only BROADCAST messages for this channel
            filtered = [m for m in self.messages if m.get('channel') == self.active_channel and m.get('toId') in ('^all', '^local', '!ffffffff')]
        else:
            # Show only DMs between us and this specific node
            filtered = [m for m in self.messages if (m.get('fromId') == self.active_channel and m.get('toId') == local_id) or (m.get('fromId') == local_id and m.get('toId') == self.active_channel)]

        show_msgs = filtered[-max_msgs:]
        
        for m in show_msgs:
            sender = m.get('from', 'Unknown')
            text = m.get('text', '')
            
            # Use different colors for names
            if sender == 'You':
                name_color = curses.color_pair(1) | curses.A_BOLD
            elif not isinstance(self.active_channel, int):
                name_color = curses.color_pair(3) | curses.A_BOLD # Yellow for DM partner
            else:
                name_color = curses.color_pair(2) | curses.A_BOLD # Cyan for channel members

            # Truncate to fit inside the pane cleanly
            max_len = w - mid_x - 3
            hop = m.get('hopLimit')
            hop_str = f"({hop})" if hop is not None else ""
            display_name = f"{sender}{hop_str}: "
            if len(display_name + text) > max_len:
                text = text[:max_len - len(display_name) - 3] + "..."
            
            self.safe_addstr(msg_y, mid_x + 2, display_name, name_color)
            self.safe_addstr(msg_y, mid_x + 2 + len(display_name), text)
            msg_y += 1
            
        # Draw Input Bar
        self.stdscr.hline(h-4, mid_x + 1, curses.ACS_HLINE, w - mid_x - 2)
        if self.input_mode:
            prompt = "Type message: "
            text = self.input_text
            self.safe_addstr(h-3, mid_x + 2, prompt + text)
            # Show simulated cursor
            curses.curs_set(1)
            try:
                self.stdscr.move(h-3, mid_x + 2 + len(prompt) + len(text))
            except curses.error:
                pass
        else:
            curses.curs_set(0)
            self.safe_addstr(h-3, mid_x + 2, "[ENTER to chat] [TAB channel] [Q quit]", curses.color_pair(3))

        self.stdscr.refresh()

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
                                    
                                requests.post(f"{API_URL}/api/send", json=payload)
                                
                                # local echo
                                self.messages.append({
                                    'time': time.time(), 
                                    'channel': self.active_channel if isinstance(self.active_channel, int) else 0, 
                                    'from': 'You', 
                                    'fromId': self.state.get('local_id'),
                                    'toId': '^all' if isinstance(self.active_channel, int) else self.active_channel,
                                    'text': self.input_text.strip(),
                                    'hopLimit': 3 # Default local hop limit
                                })
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
                            self.config_idx = (self.config_idx + 1) % 3
                        elif c in (10, 13): # ENTER
                            payload = {}
                            if self.config_idx == 0: payload['long_name'] = self.config_buffers[0]
                            elif self.config_idx == 1: payload['short_name'] = self.config_buffers[1]
                            elif self.config_idx == 2: payload['command'] = self.config_buffers[2]
                            
                            try:
                                requests.post(f"{API_URL}/api/config/apply", json=payload)
                                self.config_status = "Saved!"
                            except:
                                self.config_status = "Error!"
                            self.config_status_time = time.time()
                            if self.config_idx == 2: self.config_buffers[2] = ""
                            else: self.config_mode = False
                        elif c in (curses.KEY_BACKSPACE, 127, 8):
                            self.config_buffers[self.config_idx] = self.config_buffers[self.config_idx][:-1]
                        elif 32 <= c <= 126:
                            self.config_buffers[self.config_idx] += chr(c)
                    else:
                        if c == ord('q') or c == ord('Q'):
                            self.running = False
                        elif c == ord('c') or c == ord('C'):
                            self.config_mode = True
                            self.config_idx = 0
                            self.config_buffers = [self.state.get('long_name', ''), self.state.get('short_name', ''), '']
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
