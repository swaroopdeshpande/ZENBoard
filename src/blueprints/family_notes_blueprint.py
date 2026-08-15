"""
Family Notes - a tiny web form anyone on the WiFi can use to leave (or edit)
a short message that shows up on the e-ink frame. Every other plugin on
this board is a one-way automated feed; this is the one where a person
actually puts something on the frame themselves.

Full refresh only. Real hardware partial refresh was tried and reverted -
confirmed via Waveshare's own demo (display_Partial shipped commented-out
for this exact bi-color model) and ESPHome's driver list (partial refresh
only listed for the plain black/white 7.5" variant, not tri-color BWR)
that it isn't reliably supported on this panel. See docs/ZENBOARD.md.
"""

import json
import logging
import time
import uuid

from flask import Blueprint, request, jsonify, render_template_string, current_app

logger = logging.getLogger(__name__)

family_notes_bp = Blueprint("family_notes", __name__)

NOTES_FILE = "/tmp/zenboard_family_notes.json"
MAX_HISTORY = 20
MAX_LEN = 220

FORM_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>ZenBoard notes</title>
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family: -apple-system, sans-serif; background:#0a0a0a; color:#fff;
               min-height:100vh; padding:20px 0; }
        .card { background:#1a1a1a; border:1px solid #333; border-radius:16px; padding:28px;
                width:90%; max-width:440px; margin:0 auto 20px auto; }
        h1 { font-size:22px; font-weight:800; margin-bottom:4px; }
        h2 { font-size:15px; font-weight:800; margin-bottom:12px; display:flex;
             align-items:center; justify-content:space-between; }
        .sub { font-size:13px; color:#888; margin-bottom:20px; }
        label { font-size:12px; font-weight:700; letter-spacing:1px; color:#aaa;
                text-transform:uppercase; display:block; margin-bottom:6px; }
        input, textarea { width:100%; background:#0a0a0a; border:1px solid #444; border-radius:8px;
                color:#fff; padding:12px 14px; font-size:15px; margin-bottom:16px; outline:none;
                font-family:inherit; }
        textarea { resize:vertical; min-height:90px; }
        input:focus, textarea:focus { border-color:#CC0000; }
        button { width:100%; background:#CC0000; color:#fff; border:none; border-radius:8px;
                 padding:14px; font-size:16px; font-weight:800; cursor:pointer; margin-bottom:10px; }
        button.secondary { background:#333; width:auto; padding:8px 14px; font-size:12px;
                            margin-bottom:0; }
        button:disabled { background:#444; }
        .status { margin-top:4px; font-size:13px; text-align:center; min-height:18px; color:#aaa; }
        .status.ok { color:#4ade80; }
        .status.err { color:#f87171; }
        .count { text-align:right; font-size:11px; color:#666; margin-top:-12px; margin-bottom:16px; }
        .empty { font-size:13px; color:#666; text-align:center; padding:16px 0; }
        .note-row { display:flex; align-items:flex-start; gap:10px; padding:10px 0;
                    border-top:1px solid #2a2a2a; }
        .note-row:first-child { border-top:none; }
        .note-check { width:20px; height:20px; margin:2px 0 0 0; accent-color:#CC0000;
                      flex:0 0 auto; cursor:pointer; }
        .note-body { flex:1 1 auto; min-width:0; }
        .note-from { font-size:12px; font-weight:800; color:#CC0000; }
        .note-msg { font-size:14px; font-weight:600; color:#fff; word-break:break-word; }
        .note-done .note-msg, .note-done .note-from { text-decoration:line-through; color:#666; }
        .note-del { background:none; border:none; color:#666; font-size:18px; cursor:pointer;
                    padding:0 4px; flex:0 0 auto; width:auto; margin:0; }
        .note-del:hover { color:#f87171; }
    </style>
</head>
<body>

<div class="card">
    <h1>Board note</h1>
    <div class="sub">Nothing hits the frame until you press Update Display below.</div>

    <label>Your name</label>
    <input type="text" id="fromName" maxlength="30" placeholder="e.g. Amma">

    <label>New message</label>
    <textarea id="msg" maxlength="{max_len}" placeholder="Pick up milk on your way back :)&#10;One line = one note - add several at once"></textarea>
    <div class="count"><span id="charCount">0</span>/{max_len}</div>

    <button id="sendBtn" onclick="send()">Add note(s)</button>
    <button class="secondary" style="width:100%; margin-top:0;" onclick="pushDisplay(this)">Update display</button>
    <div class="status" id="statusMsg"></div>
</div>

<div class="card">
    <h2>
        <span>All notes</span>
        <div style="display:flex; gap:8px;">
            <button class="secondary" id="clearBtn" onclick="clearAll()">Clear all</button>
        </div>
    </h2>
    <div class="sub" style="margin-bottom:14px;">Ticking, deleting, or clearing doesn't push to the frame right
        away - hit Update below once you're done.</div>
    <div id="notesList"></div>
    <button onclick="pushDisplay(this)">Update display</button>
    <div class="status" id="listStatus"></div>
</div>

<script>
function esc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function loadList() {
    fetch('/api/family_notes/latest').then(r => r.json()).then(data => {
        const notes = (data.notes || []).slice().reverse();
        const el = document.getElementById('notesList');
        if (notes.length === 0) {
            el.innerHTML = '<div class="empty">No notes yet</div>';
            return;
        }
        el.innerHTML = notes.map(n => `
            <div class="note-row ${n.done ? 'note-done' : ''}">
                <input type="checkbox" class="note-check" ${n.done ? 'checked' : ''}
                       onchange="toggleNote('${n.id}')">
                <div class="note-body">
                    <div class="note-from">${esc(n.from_name)}</div>
                    <div class="note-msg">${esc(n.message)}</div>
                </div>
                <button class="note-del" onclick="deleteNote('${n.id}')" title="Remove">&times;</button>
            </div>
        `).join('');
    }).catch(() => {
        document.getElementById('notesList').innerHTML = '<div class="empty">Could not load notes</div>';
    });
}
loadList();

document.getElementById('msg').addEventListener('input', function() {
    document.getElementById('charCount').textContent = this.value.length;
});

function toggleNote(id) {
    fetch('/api/family_notes/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: id})
    }).then(() => loadList()).catch(e => setStatus('listStatus', 'Failed: ' + e.message, 'err'));
}

function deleteNote(id) {
    fetch('/api/family_notes/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: id})
    }).then(() => loadList()).catch(e => setStatus('listStatus', 'Failed: ' + e.message, 'err'));
}

function clearAll() {
    if (!confirm('Remove all notes from the board?')) return;
    fetch('/api/family_notes/clear', { method: 'POST' })
        .then(() => loadList())
        .catch(e => setStatus('listStatus', 'Failed: ' + e.message, 'err'));
}

function pushDisplay(btn) {
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Updating...';
    fetch('/api/family_notes/refresh', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            setStatus('listStatus', data.success ? 'Sent to the frame' : ('Error: ' + data.error), data.success ? 'ok' : 'err');
            setStatus('statusMsg', data.success ? 'Sent to the frame' : ('Error: ' + data.error), data.success ? 'ok' : 'err');
            btn.disabled = false;
            btn.textContent = origText;
        })
        .catch(e => {
            setStatus('listStatus', 'Failed: ' + e.message, 'err');
            setStatus('statusMsg', 'Failed: ' + e.message, 'err');
            btn.disabled = false;
            btn.textContent = origText;
        });
}

function send() {
    const fromName = document.getElementById('fromName').value.trim();
    const msg = document.getElementById('msg').value.trim();
    if (!msg) { setStatus('statusMsg', 'Write something first', 'err'); return; }

    const btn = document.getElementById('sendBtn');
    btn.disabled = true;
    btn.textContent = 'Pinning...';

    fetch('/api/family_notes/post', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({from_name: fromName, message: msg})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            setStatus('statusMsg', 'Added - press Update Display to push it.', 'ok');
            document.getElementById('msg').value = '';
            document.getElementById('charCount').textContent = '0';
            loadList();
        } else {
            setStatus('statusMsg', 'Error: ' + (data.error || 'unknown'), 'err');
        }
        btn.disabled = false;
        btn.textContent = 'Add note(s)';
    })
    .catch(e => {
        setStatus('statusMsg', 'Could not reach ZenBoard: ' + e.message, 'err');
        btn.disabled = false;
        btn.textContent = 'Add note(s)';
    });
}

function setStatus(id, msg, cls) {
    const el = document.getElementById(id);
    el.textContent = msg;
    el.className = 'status ' + cls;
}
</script>
</body>
</html>""".replace("{max_len}", str(MAX_LEN))


def _load_notes():
    try:
        with open(NOTES_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_notes(notes):
    try:
        with open(NOTES_FILE, "w") as f:
            json.dump(notes[-MAX_HISTORY:], f)
    except Exception as e:
        logger.error(f"family_notes: failed to save: {e}")


@family_notes_bp.route("/notes")
@family_notes_bp.route("/note")
def notes_form():
    return render_template_string(FORM_HTML)


@family_notes_bp.route("/api/family_notes/post", methods=["POST"])
def post_note():
    data = request.get_json() or {}
    raw_message = (data.get("message") or "").strip()
    from_name = (data.get("from_name") or "").strip()[:30] or "Someone"

    if not raw_message:
        return jsonify({"success": False, "error": "Message required"}), 400

    # One line = one point. Lets someone add several notes in a single
    # submit instead of round-tripping the form per point.
    points = [line.strip()[:MAX_LEN] for line in raw_message.splitlines() if line.strip()]
    if not points:
        return jsonify({"success": False, "error": "Message required"}), 400

    notes = _load_notes()
    now = time.time()
    for i, point in enumerate(points):
        notes.append({
            "id": str(uuid.uuid4())[:8],
            "from_name": from_name,
            "message": point,
            "done": False,
            # keep multi-point submits in the order typed, all just now
            "timestamp": now + i * 0.001,
        })
    _save_notes(notes)
    logger.info(f"family_notes: {len(points)} new note(s) from {from_name}")

    return jsonify({"success": True, "count": len(points)})


@family_notes_bp.route("/api/family_notes/update", methods=["POST"])
def update_note():
    data = request.get_json() or {}
    note_id = data.get("id")
    message = (data.get("message") or "").strip()[:MAX_LEN]
    from_name = (data.get("from_name") or "").strip()[:30]

    if not message or not note_id:
        return jsonify({"success": False, "error": "id and message required"}), 400

    notes = _load_notes()
    if not notes or notes[-1]["id"] != note_id:
        return jsonify({"success": False, "error": "That note is no longer the latest one - refresh the page"}), 409

    notes[-1]["message"] = message
    if from_name:
        notes[-1]["from_name"] = from_name
    notes[-1]["timestamp"] = time.time()
    _save_notes(notes)
    logger.info(f"family_notes: edited note {note_id}")

    return jsonify({"success": True})


@family_notes_bp.route("/api/family_notes/toggle", methods=["POST"])
def toggle_note():
    data = request.get_json() or {}
    note_id = data.get("id")
    if not note_id:
        return jsonify({"success": False, "error": "id required"}), 400

    notes = _load_notes()
    for n in notes:
        if n["id"] == note_id:
            n["done"] = not n.get("done", False)
            _save_notes(notes)
            logger.info(f"family_notes: toggled {note_id} -> done={n['done']}")
            return jsonify({"success": True, "done": n["done"]})

    return jsonify({"success": False, "error": "note not found"}), 404


@family_notes_bp.route("/api/family_notes/delete", methods=["POST"])
def delete_note():
    data = request.get_json() or {}
    note_id = data.get("id")
    if not note_id:
        return jsonify({"success": False, "error": "id required"}), 400

    notes = _load_notes()
    remaining = [n for n in notes if n["id"] != note_id]
    if len(remaining) == len(notes):
        return jsonify({"success": False, "error": "note not found"}), 404

    _save_notes(remaining)
    logger.info(f"family_notes: deleted {note_id}")
    return jsonify({"success": True})


@family_notes_bp.route("/api/family_notes/clear", methods=["POST"])
def clear_notes():
    _save_notes([])
    logger.info("family_notes: cleared all notes")
    return jsonify({"success": True})


@family_notes_bp.route("/api/family_notes/refresh", methods=["POST"])
def refresh_display():
    """Explicit push - ticking/deleting/clearing don't auto-refresh the
    panel (each is cheap and someone might do several in a row), this is
    the button that actually sends the current state to the display."""
    _trigger_refresh()
    logger.info("family_notes: manual display refresh requested")
    return jsonify({"success": True})


@family_notes_bp.route("/api/family_notes/latest", methods=["GET"])
def latest_notes():
    return jsonify({"notes": _load_notes()})


def _trigger_refresh():
    try:
        refresh_task = current_app.config["REFRESH_TASK"]
        if refresh_task.running:
            from refresh_task import ManualRefresh
            refresh_task.manual_update(ManualRefresh("family_notes", {}))
    except Exception as e:
        logger.error(f"family_notes: failed to trigger display refresh: {e}")
