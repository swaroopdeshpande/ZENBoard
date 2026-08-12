"""
Spotify authorization routes.

Spotify's current rules only allow HTTPS redirect URIs, or an explicit
loopback address (http://127.0.0.1:PORT/...). A LAN address like
http://<pi-ip>/callback is rejected, and `localhost` was removed in
Nov 2025 - so a callback that lands back on the Pi is not possible.

Instead we use the standard headless/device flow: the user authorizes in
whatever browser they have, gets bounced to a dead loopback URL that
carries ?code=..., and pastes that URL back here. We exchange it for a
refresh token server-side and store it in the plugin's persistent cache.
"""

import base64
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger(__name__)

spotify_bp = Blueprint("spotify", __name__, url_prefix="/spotify")

# Same persistent location the plugin reads from (NOT /tmp - tmpfs here)
CACHE_DIR = Path(__file__).parent.parent / "plugins" / "spotify_now_playing" / ".cache"
TOKEN_FILE = CACHE_DIR / "spotify_token.json"

# State pushed locally from a Mac (or any machine) running Spotify desktop.
# Bypasses Spotify's Web API /me/player entirely, which is gated behind a
# Premium subscription - this reads the OS-level app state instead, so it
# works on any account tier.
PUSH_FILE = CACHE_DIR / "spotify_push_state.json"
PUSH_STALE_SECONDS = 120  # if nothing's pushed in this long, treat as offline

REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-read-currently-playing user-library-read"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"


def _creds():
    cfg = current_app.config["DEVICE_CONFIG"]
    return (
        cfg.load_env_key("SPOTIFY_CLIENT_ID"),
        cfg.load_env_key("SPOTIFY_CLIENT_SECRET"),
    )


@spotify_bp.route("/auth-url", methods=["GET"])
def auth_url():
    """Build the Spotify consent URL."""
    client_id, _ = _creds()
    if not client_id:
        return jsonify({
            "error": "SPOTIFY_CLIENT_ID is not set. Add it on the API Keys page first."
        }), 400

    url = AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        # force the consent screen so re-auth always works
        "show_dialog": "true",
    })
    return jsonify({"url": url, "redirect_uri": REDIRECT_URI})


@spotify_bp.route("/exchange", methods=["POST"])
def exchange():
    """Exchange a pasted redirect URL (or bare code) for a refresh token."""
    client_id, client_secret = _creds()
    if not client_id or not client_secret:
        return jsonify({"error": "Client ID/secret not configured"}), 400

    pasted = (request.get_json(silent=True) or {}).get("pasted", "").strip()
    if not pasted:
        return jsonify({"error": "Nothing pasted"}), 400

    # Accept either the full redirect URL or just the code
    code = pasted
    if "code=" in pasted:
        try:
            qs = parse_qs(urlparse(pasted).query)
            code = (qs.get("code") or [""])[0]
        except Exception:
            return jsonify({"error": "Could not parse that URL"}), 400

    if not code:
        return jsonify({"error": "No authorization code found in what you pasted"}), 400

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15,
        )
    except Exception as e:
        logger.error(f"Spotify token exchange request failed: {e}")
        return jsonify({"error": f"Network error: {e}"}), 502

    if resp.status_code != 200:
        logger.error(f"Spotify token exchange rejected: {resp.status_code} {resp.text[:300]}")
        return jsonify({
            "error": "Spotify rejected the code. Codes are single-use and expire "
                     "quickly - re-authorize and paste a fresh URL. Also confirm "
                     f"{REDIRECT_URI} is registered in your Spotify app."
        }), 400

    data = resp.json()
    if not data.get("refresh_token"):
        return jsonify({"error": "Spotify did not return a refresh token"}), 400

    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump({
                "refresh_token": data["refresh_token"],
                "access_token": data.get("access_token"),
                "expires_at": time.time() + data.get("expires_in", 3600),
            }, f)
        os.chmod(TOKEN_FILE, 0o600)
    except Exception as e:
        logger.error(f"Could not save Spotify token: {e}")
        return jsonify({"error": f"Could not save token: {e}"}), 500

    logger.info("Spotify authorized; refresh token stored")
    return jsonify({"success": True})


@spotify_bp.route("/status", methods=["GET"])
def status():
    client_id, client_secret = _creds()
    return jsonify({
        "connected": TOKEN_FILE.exists(),
        "credentials_set": bool(client_id and client_secret),
        "redirect_uri": REDIRECT_URI,
    })


@spotify_bp.route("/disconnect", methods=["POST"])
def disconnect():
    try:
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        logger.info("Spotify token cleared")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@spotify_bp.route("/push", methods=["POST"])
def push():
    """Receive Now Playing state pushed from a local Spotify desktop app
    (AppleScript on Mac). No auth on this endpoint - it's Tailscale-only
    reachable and just overwrites a status cache, nothing sensitive."""
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "Invalid JSON"}), 400

    payload["_pushed_at"] = time.time()

    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(PUSH_FILE, "w") as f:
            json.dump(payload, f)
    except Exception as e:
        logger.error(f"Could not save pushed Spotify state: {e}")
        return jsonify({"error": str(e)}), 500

    return jsonify({"success": True})
