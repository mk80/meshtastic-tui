import logging
import os
from flask import Flask, jsonify, send_from_directory, request
from flask_cors import CORS

from backends.meshtastic_backend import MeshtasticBackend
from backends.meshcore_backend import MeshcoreBackend

log_level = logging.DEBUG if os.environ.get('MESHTASTIC_DEBUG') == '1' else logging.INFO

logging.basicConfig(
    level=log_level,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('daemon')

if os.environ.get('MESHTASTIC_DEBUG') != '1':
    logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__, static_folder='static', static_url_path='/static')
CORS(app)

backend = None

def init_backend():
    global backend
    core = os.environ.get('RADIO_CORE', 'meshtastic').lower()
    port = os.environ.get('MESHTASTIC_PORT')
    
    if core == 'meshcore':
        logger.info("Initializing MeshCore backend...")
        backend = MeshcoreBackend(port=port)
    else:
        logger.info("Initializing Meshtastic backend...")
        backend = MeshtasticBackend(port=port)
        
    backend.connect()

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/api/heatmap')
def api_heatmap():
    if not backend:
        return jsonify([])
    return jsonify(backend.get_heatmap_nodes())

@app.route('/api/stream')
def api_stream():
    since = request.args.get('since', 0, type=float)
    if not backend:
        return jsonify([])
    return jsonify(backend.get_events(since))

@app.route('/api/state')
def api_state():
    if not backend:
        return jsonify({'error': 'Backend not initialized'}), 500
    state = backend.get_state()
    if not state:
        return jsonify({'error': 'Not connected'}), 500
    return jsonify(state)

@app.route('/api/nodes', methods=['GET'])
def api_nodes():
    if not backend:
        return jsonify([])
    return jsonify(backend.get_nodes())

@app.route('/api/channel/set', methods=['POST'])
def api_channel_set():
    data = request.json
    index = data.get('index')
    name = data.get('name', '')
    ch_type = data.get('type', 'public')
    secret = data.get('secret', '')
    if index is None:
        return jsonify({"error": "Missing channel index"}), 400
    try:
        backend.set_channel(int(index), name, ch_type=ch_type, secret_hex=secret)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/config/apply', methods=['POST'])
def api_config_apply():
    data = request.json
    if not backend:
        return jsonify({'error': 'Backend not initialized'}), 500
    try:
        backend.apply_config(data)
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Config apply failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/send', methods=['POST'])
def api_send():
    data = request.json
    if not backend:
        return jsonify({'error': 'Backend not initialized'}), 500
    
    msg = data.get('message')
    dest = data.get('destination')
    channel = data.get('channel', 0)
    try:
        backend.send_message(msg, dest, channel=channel)
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.error(f"Send failed: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    init_backend()
    logger.info("Daemon starting on 0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)
