from flask import Blueprint, request, jsonify, current_app, render_template, send_file
import os
from datetime import datetime
from utils.refresh_stats import get_daily_refresh_count
from refresh_task import ManualRefresh

main_bp = Blueprint("main", __name__)

@main_bp.route('/')
def main_page():
    device_config = current_app.config['DEVICE_CONFIG']
    return render_template('inky.html', config=device_config.get_config(), plugins=device_config.get_plugins())

@main_bp.route('/api/refresh_count')
def refresh_count():
    """Today's full-refresh count for the e-ink cycle-wear indicator.
    Resets automatically at midnight local time."""
    return jsonify(get_daily_refresh_count())

@main_bp.route('/api/current_image')
def get_current_image():
    """Serve current_image.png with conditional request support (If-Modified-Since)."""
    image_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'images', 'current_image.png')
    
    if not os.path.exists(image_path):
        return jsonify({"error": "Image not found"}), 404
    
    # Get the file's last modified time (truncate to seconds to match HTTP header precision)
    file_mtime = int(os.path.getmtime(image_path))
    last_modified = datetime.fromtimestamp(file_mtime)
    
    # Check If-Modified-Since header
    if_modified_since = request.headers.get('If-Modified-Since')
    if if_modified_since:
        try:
            # Parse the If-Modified-Since header
            client_mtime = datetime.strptime(if_modified_since, '%a, %d %b %Y %H:%M:%S %Z')
            client_mtime_seconds = int(client_mtime.timestamp())
            
            # Compare (both now in seconds, no sub-second precision)
            if file_mtime <= client_mtime_seconds:
                return '', 304
        except (ValueError, AttributeError):
            pass
    
    # Send the file with Last-Modified header
    response = send_file(image_path, mimetype='image/png')
    response.headers['Last-Modified'] = last_modified.strftime('%a, %d %b %Y %H:%M:%S GMT')
    response.headers['Cache-Control'] = 'no-cache'
    return response


@main_bp.route('/api/plugin_order', methods=['POST'])
def save_plugin_order():
    """Save the custom plugin order."""
    device_config = current_app.config['DEVICE_CONFIG']

    data = request.get_json() or {}
    order = data.get('order', [])

    if not isinstance(order, list):
        return jsonify({"error": "Order must be a list"}), 400

    device_config.set_plugin_order(order)

    return jsonify({"success": True})
import subprocess

@main_bp.route('/api/settings/welcome', methods=['GET'])
def get_welcome_name():
    device_config = current_app.config['DEVICE_CONFIG']
    name = device_config.get_config("welcome_name") or "Swaroop's TRMNL"
    return jsonify({"name": name})

@main_bp.route('/api/settings/welcome', methods=['POST'])
def save_welcome_name():
    device_config = current_app.config['DEVICE_CONFIG']
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({"success": False, "error": "Name cannot be empty"}), 400
    device_config.update_value("welcome_name", name, write=True)
    return jsonify({"success": True, "name": name})

@main_bp.route('/api/wifi/scan', methods=['GET'])
def scan_wifi():
    try:
        result = subprocess.run(
            ['sudo', 'nmcli', '-t', '-f', 'SSID,SIGNAL', 'dev', 'wifi', 'list'],
            capture_output=True, text=True, timeout=15
        )
        networks = []
        seen = set()
        for line in result.stdout.strip().split('\n'):
            parts = line.split(':')
            if len(parts) >= 2:
                ssid = parts[0].strip()
                signal = parts[1].strip()
                if ssid and ssid not in seen:
                    seen.add(ssid)
                    networks.append({"ssid": ssid, "signal": signal})
        networks.sort(key=lambda x: int(x['signal']) if x['signal'].isdigit() else 0, reverse=True)
        return jsonify({"networks": networks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@main_bp.route('/api/wifi/connect', methods=['POST'])
def connect_wifi():
    data = request.get_json() or {}
    ssid = data.get('ssid', '').strip()
    password = data.get('password', '').strip()
    if not ssid or not password:
        return jsonify({"success": False, "error": "SSID and password required"}), 400
    try:
        result = subprocess.run(
            ['sudo', 'nmcli', 'dev', 'wifi', 'connect', ssid, 'password', password],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return jsonify({"success": True, "message": f"Connected to {ssid}"})
        else:
            return jsonify({"success": False, "error": result.stderr.strip()}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

import os
from pathlib import Path

BOOKS_DIR = "/home/zenith/InkyPi/books"

@main_bp.route('/api/ereader/upload', methods=['POST'])
def ereader_upload():
    os.makedirs(BOOKS_DIR, exist_ok=True)
    if 'book' not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files['book']
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in ('.epub', '.pdf', '.txt'):
        return jsonify({"error": "Only EPUB, PDF, TXT supported"}), 400
    dest = os.path.join(BOOKS_DIR, f.filename)
    f.save(dest)
    size = f"{os.path.getsize(dest) // 1024}KB"
    return jsonify({"filename": f.filename, "path": dest, "size": size})

@main_bp.route('/api/ereader/list', methods=['GET'])
def ereader_list():
    os.makedirs(BOOKS_DIR, exist_ok=True)
    books = []
    for f in sorted(Path(BOOKS_DIR).iterdir()):
        if f.suffix.lower() in ('.epub', '.pdf', '.txt'):
            books.append({"name": f.name, "path": str(f), "size": f"{f.stat().st_size // 1024}KB"})
    return jsonify({"books": books})

@main_bp.route('/api/ereader/delete', methods=['POST'])
def ereader_delete():
    import hashlib
    data = request.get_json() or {}
    path = data.get('path', '').strip()
    if not path or not path.startswith(BOOKS_DIR):
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    os.remove(path)
    
    for font in ["serif", "serif-bold", "sans", "mono", "liberation"]:
        for size in [16, 20, 24, 28, 32]:
            for portrait in [True, False]:
                cache_key = hashlib.md5(f"{path}{font}{size}{portrait}".encode()).hexdigest()
                cache_file = f"{BOOKS_DIR}/.cache_{cache_key}.json"
                if os.path.exists(cache_file):
                    os.remove(cache_file)
    
    return jsonify({"success": True})

@main_bp.route('/api/presence/changed', methods=['POST'])
def presence_changed():
    """Posted by zenboard_presence.service when someone walks in.

    Only matters if a scheduled refresh was skipped while the room was empty
    (see RefreshTask._presence_allows_refresh) - then the frame catches up so
    what you walk up to is current, rather than whatever was last painted
    before the room emptied.
    """
    refresh_task = current_app.config['REFRESH_TASK']

    if not refresh_task.running:
        return jsonify({"success": False, "error": "Refresh task not running"}), 503

    woke = refresh_task.presence_wake()
    return jsonify({"success": True, "refreshed": woke}), 200

# ── LED ROUTES ── Append to ~/InkyPi/src/blueprints/main.py

import time

LED_CONFIG_FILE = "/tmp/led_config.json"
LED_DEFAULT = {
    "mode":             "warm_glow",
    "color":            "#FF6B35",
    "brightness":       128,
    "breathe_speed":    "medium",
    "refresh_flash":    True,
    "presence_enabled": False,
    "presence_color_on":  "#FF8C42",
    "presence_color_off": "#001133",
    "enabled":          True,
}
LED_PERSIST_FILE = "/home/zenith/InkyPi/src/config/led_config.json"

def _load_led_config():
    import json
    # Try persistent config first
    try:
        with open(LED_PERSIST_FILE) as f:
            cfg = LED_DEFAULT.copy()
            cfg.update(json.load(f))
            return cfg
    except Exception:
        return LED_DEFAULT.copy()

def _save_led_config(cfg):
    import json
    # Write to runtime file (picked up by LED service instantly)
    with open(LED_CONFIG_FILE, 'w') as f:
        json.dump(cfg, f)
    # Write to persistent file (survives reboots)
    os.makedirs(os.path.dirname(LED_PERSIST_FILE), exist_ok=True)
    with open(LED_PERSIST_FILE, 'w') as f:
        json.dump(cfg, f)

@main_bp.route('/api/led/config', methods=['GET'])
def led_get_config():
    return jsonify(_load_led_config())

@main_bp.route('/api/led/config', methods=['POST'])
def led_set_config():
    import json
    data = request.get_json() or {}
    cfg = _load_led_config()
    cfg.update(data)
    _save_led_config(cfg)
    return jsonify({"success": True, "config": cfg})

@main_bp.route('/api/led/flash', methods=['POST'])
def led_flash():
    """Trigger refresh flash effect temporarily."""
    import json, threading
    data = request.get_json() or {}
    duration = float(data.get('duration', 3.0))

    cfg = _load_led_config()
    if not cfg.get('refresh_flash', True):
        return jsonify({"success": False, "reason": "refresh_flash disabled"})

    prev_mode = cfg.get('mode', 'warm_glow')

    def flash_then_restore():
        flash_cfg = cfg.copy()
        flash_cfg['mode'] = 'refresh_flash'
        _save_led_config(flash_cfg)
        time.sleep(duration)
        flash_cfg['mode'] = prev_mode
        _save_led_config(flash_cfg)

    threading.Thread(target=flash_then_restore, daemon=True).start()
    return jsonify({"success": True})

@main_bp.route('/api/led/on', methods=['POST'])
def led_on():
    cfg = _load_led_config()
    cfg['enabled'] = True
    _save_led_config(cfg)
    return jsonify({"success": True})

@main_bp.route('/api/led/off', methods=['POST'])
def led_off():
    cfg = _load_led_config()
    cfg['enabled'] = False
    _save_led_config(cfg)
    return jsonify({"success": True})

@main_bp.route('/api/led/orientation', methods=['POST'])
def led_orientation():
    data = request.get_json() or {}
    orientation = data.get('orientation', 'landscape')
    cfg = _load_led_config()
    cfg['orientation'] = orientation
    _save_led_config(cfg)
    return jsonify({"success": True})
"""
WiFi Setup Captive Portal Routes
Add to InkyPi's Flask app.
Accessible at http://192.168.4.1 when Pi is in AP mode.
"""

import json
import logging
import subprocess
import time
import threading
from flask import Blueprint, request, jsonify, render_template_string

logger = logging.getLogger(__name__)

wifi_setup_bp = Blueprint('wifi_setup', __name__)

STATUS_FILE = "/tmp/zenboard_wifi_status.json"

PORTAL_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>ZenBoard WiFi Setup</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family: -apple-system, sans-serif; background: #0a0a0a; color: #fff; min-height: 100vh; display: flex; align-items: center; justify-content: center; }
        .card { background: #1a1a1a; border: 1px solid #333; border-radius: 16px; padding: 32px; width: 90%; max-width: 400px; }
        h1 { font-size: 24px; font-weight: 800; color: #fff; margin-bottom: 4px; }
        .sub { font-size: 13px; color: #888; margin-bottom: 24px; }
        label { font-size: 12px; font-weight: 700; letter-spacing: 1px; color: #aaa; text-transform: uppercase; display: block; margin-bottom: 6px; }
        select, input { width: 100%; background: #0a0a0a; border: 1px solid #444; border-radius: 8px; color: #fff; padding: 12px 14px; font-size: 15px; margin-bottom: 16px; outline: none; }
        select:focus, input:focus { border-color: #CC0000; }
        button { width: 100%; background: #CC0000; color: #fff; border: none; border-radius: 8px; padding: 14px; font-size: 16px; font-weight: 800; cursor: pointer; letter-spacing: 0.5px; }
        button:disabled { background: #444; cursor: default; }
        .status { margin-top: 16px; font-size: 13px; text-align: center; color: #aaa; min-height: 20px; }
        .status.ok { color: #4ade80; }
        .status.err { color: #CC0000; }
        .scanning { color: #888; font-size: 13px; margin-bottom: 8px; }
        .refresh-btn { background: none; border: 1px solid #444; color: #aaa; padding: 8px 16px; border-radius: 6px; font-size: 12px; cursor: pointer; margin-bottom: 16px; width: auto; }
    </style>
</head>
<body>
<div class="card">
    <h1>⚡ ZenBoard</h1>
    <div class="sub">Connect to WiFi to get started</div>

    <label>Available Networks</label>
    <div class="scanning" id="scanStatus">Scanning...</div>
    <button class="refresh-btn" onclick="scanNetworks()">↻ Refresh</button>

    <select id="ssidSelect" onchange="checkCustom()">
        <option value="">Select a network...</option>
    </select>

    <div id="customSsidGroup" style="display:none;">
        <label>Network Name (SSID)</label>
        <input type="text" id="customSsid" placeholder="Enter network name">
    </div>

    <label>Password</label>
    <input type="password" id="password" placeholder="WiFi password">

    <button id="connectBtn" onclick="connect()" disabled>Connect</button>
    <div class="status" id="statusMsg"></div>
</div>

<script>
async function scanNetworks() {
    document.getElementById('scanStatus').textContent = 'Scanning...';
    document.getElementById('ssidSelect').innerHTML = '<option value="">Select a network...</option>';
    document.getElementById('connectBtn').disabled = true;

    try {
        const r = await fetch('/api/wifi_setup/scan');
        const data = await r.json();
        const select = document.getElementById('ssidSelect');
        document.getElementById('scanStatus').textContent = `Found ${data.networks.length} networks`;

        data.networks.forEach(n => {
            const opt = document.createElement('option');
            opt.value = n.ssid;
            opt.textContent = `${n.ssid} (${n.signal}%)`;
            select.appendChild(opt);
        });

        const opt = document.createElement('option');
        opt.value = '__other__';
        opt.textContent = 'Other (enter manually)...';
        select.appendChild(opt);

        document.getElementById('connectBtn').disabled = false;
    } catch(e) {
        document.getElementById('scanStatus').textContent = 'Scan failed';
    }
}

function checkCustom() {
    const val = document.getElementById('ssidSelect').value;
    document.getElementById('customSsidGroup').style.display = val === '__other__' ? 'block' : 'none';
}

async function connect() {
    const selectVal = document.getElementById('ssidSelect').value;
    const ssid = selectVal === '__other__'
        ? document.getElementById('customSsid').value.trim()
        : selectVal;
    const password = document.getElementById('password').value;

    if (!ssid) { setStatus('Please select or enter a network', 'err'); return; }

    const btn = document.getElementById('connectBtn');
    btn.disabled = true;
    btn.textContent = 'Connecting...';
    setStatus('Sending credentials to ZenBoard...', '');

    try {
        const r = await fetch('/api/wifi_setup/connect', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ssid, password})
        });
        const data = await r.json();
        if (data.success) {
            setStatus('✓ Credentials saved! ZenBoard is connecting to WiFi. This hotspot will close shortly.', 'ok');
            btn.textContent = 'Done!';
        } else {
            setStatus('Error: ' + (data.error || 'Unknown error'), 'err');
            btn.disabled = false;
            btn.textContent = 'Connect';
        }
    } catch(e) {
        setStatus('Connection lost (ZenBoard may be switching networks)', 'ok');
        btn.textContent = 'Done!';
    }
}

function setStatus(msg, cls) {
    const el = document.getElementById('statusMsg');
    el.textContent = msg;
    el.className = 'status ' + cls;
}

// Scan on load
scanNetworks();
</script>
</body>
</html>"""


def _ap_password():
    """Setup-AP password, read from the same root-only file
    zenboard_wifi_monitor.py uses. Deliberately not hardcoded - this repo is
    public. Falls back to a placeholder so the QR screen still renders on an
    incomplete install rather than 500ing during WiFi recovery."""
    value = os.environ.get("ZENBOARD_AP_PASSWORD")
    if value:
        return value.strip()
    try:
        with open("/etc/zenboard/ap_password") as f:
            value = f.read().strip()
        if value:
            return value
    except Exception:
        pass
    logger.warning("AP password file missing/empty, showing placeholder on QR screen")
    return "changeme-zenboard"


@wifi_setup_bp.route('/api/wifi_setup/show_qr', methods=['POST'])
def show_qr():
    """Called by wifi_monitor to trigger QR code display on e-ink."""
    data = request.get_json() or {}
    with open(STATUS_FILE, 'w') as f:
        json.dump({
            "mode": "ap",
            "ap_ssid": data.get("ap_ssid", "ZenBoard-Setup"),
            "ap_ip": data.get("ap_ip", "192.168.4.1"),
            "ap_password": _ap_password(),
            "timestamp": time.time(),
        }, f)

    try:
        refresh_task = current_app.config['REFRESH_TASK']
        if refresh_task.running:
            refresh_task.manual_update(ManualRefresh('wifi_qr', {}))
            logger.info('wifi_qr: triggered display refresh for AP mode QR')
    except Exception as e:
        logger.error(f'wifi_qr: failed to trigger display refresh: {e}')

    return jsonify({"success": True})


@wifi_setup_bp.route('/api/wifi_setup/scan', methods=['GET'])
def wifi_scan():
    """Scan for available WiFi networks."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes"],
            capture_output=True, text=True, timeout=20
        )
        networks = []
        seen = set()
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2:
                ssid = parts[0].strip()
                if ssid and ssid not in seen and not ssid.startswith("ZenBoard"):
                    seen.add(ssid)
                    try:
                        signal = int(parts[1].strip())
                    except Exception:
                        signal = 0
                    networks.append({"ssid": ssid, "signal": signal})
        networks.sort(key=lambda x: x["signal"], reverse=True)
        return jsonify({"networks": networks[:20]})
    except Exception as e:
        logger.error(f"WiFi scan error: {e}")
        return jsonify({"networks": [], "error": str(e)})


@wifi_setup_bp.route('/api/wifi_setup/connect', methods=['POST'])
def wifi_connect():
    """Save WiFi credentials and trigger reconnection."""
    data = request.get_json() or {}
    ssid     = data.get("ssid", "").strip()
    password = data.get("password", "").strip()

    if not ssid:
        return jsonify({"success": False, "error": "SSID required"}), 400

    def do_connect():
        time.sleep(2)  # Give response time to reach browser
        try:
            # Add new WiFi connection
            subprocess.run([
                "nmcli", "device", "wifi", "connect", ssid,
                "password", password,
                "ifname", "wlan0",
            ], capture_output=True, text=True, timeout=30)

            # Signal monitor to stop AP mode
            with open(STATUS_FILE, 'w') as f:
                json.dump({"mode": "connecting", "ssid": ssid, "timestamp": time.time()}, f)
        except Exception as e:
            logger.error(f"Connect error: {e}")

    threading.Thread(target=do_connect, daemon=True).start()
    return jsonify({"success": True})


@wifi_setup_bp.route('/api/wifi_setup/status', methods=['GET'])
def wifi_status():
    try:
        with open(STATUS_FILE) as f:
            return jsonify(json.load(f))
    except Exception:
        return jsonify({"mode": "unknown"})


# Captive portal — redirect all unknown requests to setup page
@wifi_setup_bp.route('/setup')
@wifi_setup_bp.route('/wifi')
def portal():
    return render_template_string(PORTAL_HTML)
