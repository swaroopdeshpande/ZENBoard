"""
Apple Health push endpoint.

Apple Health has no cloud API - the data lives on the iPhone and never
leaves it unless something on the device pushes it out. So this is an
inbound endpoint, not a fetcher: an iOS Shortcuts personal automation
reads the Health samples on a schedule and POSTs them here, same shape as
spotify_now_playing (Mac pushes via AppleScript) rather than a normal
polling plugin.

Deliberately does NOT refresh the display on every push - the automation
runs hourly and each BWR refresh is ~25s of panel activity. The plugin
just reads the last stored payload whenever the playlist gets to it.
Pass ?refresh=1 on the push to force an immediate update.
"""

import json
import logging
import time

from flask import Blueprint, request, jsonify, current_app

logger = logging.getLogger(__name__)

apple_health_bp = Blueprint("apple_health", __name__)

# NOT /tmp - that's tmpfs on this device and gets wiped on reboot, which
# would blank the frame after every power cycle until the next push.
DATA_FILE = "/home/zenith/InkyPi/src/config/apple_health.json"

# Shortcuts' "Get Contents of URL" sends everything through its own JSON
# encoder, and Health values arrive as strings surprisingly often
# ("7,421", "5.3 km", "" for a sample with no data). Everything numeric
# goes through _num so one weird field can't 500 the whole push.
def _num(value, default=0.0):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return default
    cleaned = "".join(c for c in text if c.isdigit() or c in ".-")
    try:
        return float(cleaned)
    except ValueError:
        return default


FIELDS = [
    # (payload key, default)
    ("move", 0.0), ("move_goal", 500.0),
    ("exercise", 0.0), ("exercise_goal", 30.0),
    ("stand", 0.0), ("stand_goal", 12.0),
    ("steps", 0.0),
    ("distance_km", 0.0),
    ("sleep_hours", 0.0),
    ("resting_hr", 0.0),
    ("flights", 0.0),
    ("hr", 0.0),
]


@apple_health_bp.route("/api/apple_health/push", methods=["POST"])
def push():
    try:
        payload = request.get_json(force=True, silent=True)
        if payload is None:
            # Shortcuts can be talked into sending form-encoded too
            payload = request.form.to_dict() or {}

        if not isinstance(payload, dict):
            return jsonify({"error": "expected a JSON object"}), 400

        data = {key: _num(payload.get(key), default) for key, default in FIELDS}

        # A goal of 0 would divide-by-zero the ring math downstream and is
        # never a real Health value - fall back rather than trust it.
        for goal_key, fallback in (("move_goal", 500.0), ("exercise_goal", 30.0), ("stand_goal", 12.0)):
            if data[goal_key] <= 0:
                data[goal_key] = fallback

        data["updated_at"] = time.time()

        with open(DATA_FILE, "w") as f:
            json.dump(data, f)

        logger.info(
            f"Apple Health push: move={data['move']:.0f}/{data['move_goal']:.0f} "
            f"exercise={data['exercise']:.0f}/{data['exercise_goal']:.0f} "
            f"stand={data['stand']:.0f}/{data['stand_goal']:.0f} steps={data['steps']:.0f}"
        )

        if request.args.get("refresh") == "1":
            try:
                from model import RefreshInfo  # noqa: F401  (import kept local, refresh is optional)
                from refresh_task import ManualRefresh

                refresh_task = current_app.config["REFRESH_TASK"]
                if refresh_task.running:
                    refresh_task.manual_update(ManualRefresh("apple_health", {}))
            except Exception as e:
                logger.warning(f"Health push stored, but refresh failed: {e}")

        return jsonify({"success": True, "stored": data}), 200

    except Exception as e:
        logger.exception(f"Apple Health push failed: {e}")
        return jsonify({"error": str(e)}), 500


@apple_health_bp.route("/api/apple_health/latest", methods=["GET"])
def latest():
    """Read back what's stored - for checking the Shortcut actually works
    without having to wait for the frame to cycle round to this plugin."""
    try:
        with open(DATA_FILE) as f:
            return jsonify(json.load(f)), 200
    except FileNotFoundError:
        return jsonify({"error": "no health data pushed yet"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500
