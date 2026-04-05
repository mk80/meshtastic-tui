import sys
import os
import subprocess
import time
import requests

def start_daemon():
    # Only allow 1 to run
    try:
        if requests.get('http://localhost:5000/api/heatmap').status_code == 200:
            print("Daemon is already running!")
            return
    except requests.exceptions.ConnectionError:
        pass

    print("Starting background daemon...")
    # Run app.py in background, redirecting output to a log file
    log = open('daemon.log', 'w')
    # Use cwd to run the flask app correctly relative to its static folder
    proc = subprocess.Popen([sys.executable, 'app.py'], stdout=log, stderr=subprocess.STDOUT, cwd='heatmap')
    with open('.daemon.pid', 'w') as f:
        f.write(str(proc.pid))
    
    # Wait to confirm startup
    time.sleep(3)
    try:
        for _ in range(5):
            try:
                response = requests.get('http://localhost:5000/api/heatmap')
                if response.status_code == 200:
                    print(f"\nDaemon successfully started (PID {proc.pid})!")
                    print("You can now securely run tui.py, send.py, or view http://localhost:5000 simultaneously.")
                    return
            except:
                pass
            time.sleep(2)
            
    except requests.exceptions.ConnectionError:
        pass
        
    print("Daemon might have failed to start or is still booting. Check daemon.log for details.")

def stop_daemon():
    if os.path.exists('.daemon.pid'):
        with open('.daemon.pid', 'r') as f:
            pid = f.read().strip()
        try:
            os.kill(int(pid), 15) # SIGTERM
            print(f"Sent termination signal to daemon (PID {pid}).")
        except ProcessLookupError:
            print("Daemon process not found. It might have already crashed or stopped.")
        os.remove('.daemon.pid')
    else:
        # Fallback check
        try:
            requests.get('http://localhost:5000/api/heatmap')
            print("A daemon is running, but no .daemon.pid found. You may need to manually 'kill' it.")
        except:
            print("No .daemon.pid file found, and daemon appears offline.")

def show_status():
    try:
        response = requests.get('http://localhost:5000/api/heatmap')
        nodes = response.json()
        print(f"Daemon is ONLINE. Tracking {len(nodes)} mapped nodes.")
    except Exception:
        print("Daemon is OFFLINE.")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python manager.py [start|stop|status]")
        sys.exit(1)
        
    cmd = sys.argv[1].lower()
    if cmd == 'start':
        start_daemon()
    elif cmd == 'stop':
        stop_daemon()
    elif cmd == 'status':
        show_status()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python manager.py [start|stop|status]")
