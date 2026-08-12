
# ── E-READER ROUTES ── Add these to ~/InkyPi/src/blueprints/main.py

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

@main_bp.route('/api/ereader/delete', methods=['POST'])
def ereader_delete():
    data = request.get_json() or {}
    path = data.get('path', '').strip()
    if not path or not path.startswith(BOOKS_DIR):
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    os.remove(path)
    return jsonify({"success": True})

@main_bp.route('/api/ereader/list', methods=['GET'])
def ereader_list():
    os.makedirs(BOOKS_DIR, exist_ok=True)
    books = []
    for f in sorted(Path(BOOKS_DIR).iterdir()):
        if f.suffix.lower() in ('.epub', '.pdf', '.txt'):
            books.append({"name": f.name, "path": str(f), "size": f"{f.stat().st_size // 1024}KB"})
    return jsonify({"books": books})

PRECACHE_SCRIPT = "/home/zenith/InkyPi/src/plugins/ereader/ereader_precache.py"
PRECACHE_CONFIG = "/home/zenith/InkyPi/books/.precache_config.json"

@main_bp.route('/api/ereader/precache/config', methods=['POST'])
def ereader_precache_config():
    import json
    data = request.get_json() or {}
    os.makedirs(BOOKS_DIR, exist_ok=True)
    with open(PRECACHE_CONFIG, 'w') as f:
        json.dump(data, f)
    return jsonify({"success": True})

@main_bp.route('/api/ereader/precache/cron', methods=['POST'])
def ereader_precache_cron():
    import subprocess
    data = request.get_json() or {}
    enabled = data.get('enabled', False)
    time_str = data.get('time', '02:00')
    hour, minute = time_str.split(':')

    # Remove existing cron job
    subprocess.run(['crontab', '-l'], capture_output=True)
    result = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    lines = [l for l in result.stdout.splitlines() if 'ereader_precache' not in l]

    if enabled:
        lines.append(f"{minute} {hour} * * * /usr/local/inkypi/venv_inkypi/bin/python3 {PRECACHE_SCRIPT} >> /tmp/ereader_precache.log 2>&1")

    new_cron = '\n'.join(lines) + '\n'
    subprocess.run(['crontab', '-'], input=new_cron, text=True)
    return jsonify({"success": True, "enabled": enabled})

@main_bp.route('/api/ereader/precache/run', methods=['POST'])
def ereader_precache_run():
    import subprocess, threading
    def run():
        subprocess.run([
            '/usr/local/inkypi/venv_inkypi/bin/python3',
            PRECACHE_SCRIPT, '--force'
        ])
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"message": "Pre-cache started in background. Check /tmp/ereader_precache.log for progress."})

@main_bp.route('/api/ereader/delete', methods=['POST'])
def ereader_delete():
    import glob
    data = request.get_json() or {}
    path = data.get('path', '').strip()
    if not path or not path.startswith(BOOKS_DIR):
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    os.remove(path)

    # Clean up all related cache files
    import hashlib
    from PIL import ImageFont
    fonts = ["serif", "serif-bold", "sans", "mono", "liberation"]
    sizes = [16, 20, 24, 28, 32]
    portraits = [True, False]
    removed = 0
    for font in fonts:
        for size in sizes:
            for portrait in portraits:
                cache_key = hashlib.md5(f"{path}{font}{size}{portrait}".encode()).hexdigest()
                cache_file = f"{BOOKS_DIR}/.cache_{cache_key}.json"
                if os.path.exists(cache_file):
                    os.remove(cache_file)
                    removed += 1

    return jsonify({"success": True, "caches_removed": removed})
