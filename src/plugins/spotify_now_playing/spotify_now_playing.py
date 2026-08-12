"""
Spotify Now Playing - ZENBoard
Renders the current Spotify playback state for a Waveshare 7.5" BWR panel
(800x480 landscape / 480x800 portrait).

Design notes
------------
* Playback state comes from a LOCAL PUSH, not Spotify's Web API. Spotify
  gates /me/player (currently-playing, progress, shuffle/repeat, device,
  volume) behind a Premium subscription - a free-tier account gets a flat
  403 no matter what. A small script on a Mac running the Spotify desktop
  app reads Now Playing via AppleScript (OS-level app state, unaffected by
  account tier) and POSTs it to /spotify/push every ~10-15s. This plugin
  just reads that pushed cache. See spotify_blueprint.py for the endpoint.
* OAuth (SPOTIFY_CLIENT_ID/SECRET + refresh token) is kept only for the
  "saved/liked" heart status via /me/tracks/contains, which is NOT
  Premium-gated. It's best-effort - if not configured, the heart just
  never lights up.
* Album art is downloaded once per album-art URL, converted to a 3-colour
  (black/white/red) dithered image at EXACTLY its final on-screen pixel
  size, and cached. Dithering at final size keeps the pattern 1:1 with
  panel pixels, which is what keeps it looking clean on e-ink.
* Nothing here is allowed to take down the InkyPi service. Every network /
  missing-push failure degrades to a rendered "state card" instead of
  raising.
"""

import base64
import io
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import pytz
import requests
from PIL import Image, ImageEnhance

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent

# Persistent (NOT /tmp - tmpfs, wiped on reboot)
CACHE_DIR = PLUGIN_DIR / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
TOKEN_FILE = CACHE_DIR / "spotify_token.json"
ART_CACHE_DIR = CACHE_DIR / "art"
ART_CACHE_DIR.mkdir(exist_ok=True)

PUSH_FILE = CACHE_DIR / "spotify_push_state.json"
PUSH_STALE_SECONDS = 120  # matches spotify_blueprint.py

TOKEN_URL = "https://accounts.spotify.com/api/token"
SAVED_URL = "https://api.spotify.com/v1/me/tracks/contains"

# Album art box size per orientation (must match spotify.css exactly - the
# art is dithered at final pixel size so the pattern stays 1:1 with panel
# pixels, which is what keeps it clean on e-ink).
ART_PX_LANDSCAPE = 326
ART_PX_PORTRAIT = 360

HTTP_TIMEOUT = 10


class SpotifyNowPlaying(BasePlugin):

    # ------------------------------------------------------------------
    # Settings UI
    # ------------------------------------------------------------------

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        template_params["authorized"] = TOKEN_FILE.exists()
        return template_params

    # ------------------------------------------------------------------
    # Render
    # ------------------------------------------------------------------

    def generate_image(self, settings, device_config):
        logger.info("=== Spotify Now Playing: Starting ===")

        dimensions = device_config.get_resolution()
        is_landscape = dimensions[0] > dimensions[1]
        if device_config.get_config("orientation") == "vertical":
            is_landscape = False

        art_px = ART_PX_LANDSCAPE if is_landscape else ART_PX_PORTRAIT
        state = self._build_state(device_config, art_px)

        safe = self.get_safe_area(device_config)
        template = "spotify.html" if is_landscape else "spotify_portrait.html"

        image = self.render_image(
            dimensions,
            template,
            "spotify.css",
            {
                **state,
                "clock": self._local_clock(device_config),
                # Calibrated wooden-frame margins. plugin.html applies these
                # as body margins, which is how every other plugin here
                # honours the safe area.
                "plugin_settings": {
                    "topMargin": safe.get("top", 12),
                    "bottomMargin": safe.get("bottom", 0),
                    "leftMargin": safe.get("left", 8),
                    "rightMargin": safe.get("right", 11),
                    "backgroundOption": "color",
                    "backgroundColor": "#ffffff",
                    "textColor": "#000000",
                    "selectedFrame": "None",
                },
            },
        )

        if not image:
            raise RuntimeError("Failed to render Spotify image")

        logger.info(f"=== Spotify Now Playing: Complete ({state['status']}) ===")
        return image

    # ------------------------------------------------------------------
    # State assembly - never raises on network/auth trouble
    # ------------------------------------------------------------------

    def _build_state(self, device_config, art_px):
        """Returns a dict the template can always render, whatever happened."""
        blank = {
            "status": "NOTHING PLAYING",
            "has_track": False,
            "is_playing": False,
            "title": "",
            "artist": "",
            "album": "",
            "art_uri": None,
            "progress_pct": 0,
            "elapsed": "0:00",
            "duration": "0:00",
            "shuffle": False,
            "repeat": "off",
            "device": "",
            "volume": None,
            "saved": False,
            "message": "",
        }

        if not PUSH_FILE.exists():
            blank["status"] = "NOT CONNECTED"
            blank["message"] = "Run the Spotify push script on a Mac with Spotify open (see settings)"
            return blank

        try:
            pushed = json.loads(PUSH_FILE.read_text())
        except Exception as e:
            logger.error(f"Push state read failed: {e}")
            blank["status"] = "SPOTIFY UNAVAILABLE"
            blank["message"] = "Could not read pushed Spotify state"
            return blank

        age = time.time() - pushed.get("_pushed_at", 0)
        if age > PUSH_STALE_SECONDS:
            blank["status"] = "MAC OFFLINE"
            blank["message"] = f"No update from the push script in {int(age)}s"
            return blank

        if not pushed.get("has_track"):
            return blank

        duration_ms = pushed.get("duration_ms") or 0
        progress_ms = pushed.get("progress_ms") or 0
        is_playing = bool(pushed.get("is_playing"))

        saved = self._check_saved(device_config, pushed.get("track_id"))

        art_uri = None
        art_url = pushed.get("art_url")
        if art_url:
            art_uri = self._album_art_data_uri(art_url, art_px)

        return {
            "status": "NOW PLAYING" if is_playing else "PAUSED",
            "has_track": True,
            "is_playing": is_playing,
            "title": pushed.get("title", ""),
            "artist": pushed.get("artist", ""),
            "album": pushed.get("album", ""),
            "art_uri": art_uri,
            "progress_pct": (progress_ms / duration_ms * 100) if duration_ms else 0,
            "elapsed": self._fmt_ms(progress_ms),
            "duration": self._fmt_ms(duration_ms),
            "shuffle": bool(pushed.get("shuffle")),
            "repeat": "context" if pushed.get("repeat") else "off",
            "device": pushed.get("device", ""),
            "volume": pushed.get("volume"),
            "saved": saved,
            "message": "",
        }

    def _check_saved(self, device_config, track_id):
        """Best-effort 'liked' status via OAuth - /me/tracks/contains is NOT
        Premium-gated, unlike /me/player. Silently returns False if OAuth
        isn't configured or anything goes wrong."""
        if not track_id or not TOKEN_FILE.exists():
            return False
        client_id = device_config.load_env_key("SPOTIFY_CLIENT_ID")
        client_secret = device_config.load_env_key("SPOTIFY_CLIENT_SECRET")
        if not client_id or not client_secret:
            return False
        try:
            access_token = self._get_access_token(client_id, client_secret)
            res = self._api_get(f"{SAVED_URL}?ids={track_id}", access_token)
            return bool(isinstance(res, list) and res and res[0])
        except Exception as e:
            logger.debug(f"Saved-state check failed (non-fatal): {e}")
            return False

    # ------------------------------------------------------------------
    # Spotify auth / API
    # ------------------------------------------------------------------

    def _get_access_token(self, client_id, client_secret):
        """Return a valid access token, refreshing it if needed."""
        with open(TOKEN_FILE, "r") as f:
            tok = json.load(f)

        # 60s safety margin
        if tok.get("access_token") and tok.get("expires_at", 0) > time.time() + 60:
            return tok["access_token"]

        refresh_token = tok.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("No refresh token stored")

        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        resp = requests.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Token refresh failed: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        tok["access_token"] = data["access_token"]
        tok["expires_at"] = time.time() + data.get("expires_in", 3600)
        # Spotify may hand back a rotated refresh token
        if data.get("refresh_token"):
            tok["refresh_token"] = data["refresh_token"]

        self._write_token(tok)
        logger.info("Spotify access token refreshed")
        return tok["access_token"]

    @staticmethod
    def _write_token(tok):
        with open(TOKEN_FILE, "w") as f:
            json.dump(tok, f)
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except Exception:
            pass

    @staticmethod
    def _api_get(url, access_token):
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=HTTP_TIMEOUT,
        )
        # 204 = nothing currently playing
        if resp.status_code == 204 or not resp.content:
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"{url} -> {resp.status_code} {resp.text[:200]}")
        return resp.json()

    # ------------------------------------------------------------------
    # Album art -> BWR
    # ------------------------------------------------------------------

    def _album_art_data_uri(self, url, art_px):
        """Download + BWR-convert album art, caching by URL+size. Returns a
        data: URI, or None on any failure (template shows a placeholder)."""
        key = base64.urlsafe_b64encode(url.encode()).decode()[-40:]
        cached = ART_CACHE_DIR / f"{key}_{art_px}.png"

        try:
            if not cached.exists():
                resp = requests.get(url, timeout=HTTP_TIMEOUT)
                if resp.status_code != 200:
                    return None
                src = Image.open(io.BytesIO(resp.content)).convert("RGB")
                self._to_bwr(src, art_px).save(cached, format="PNG")
                self._prune_art_cache()

            with open(cached, "rb") as f:
                return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            logger.warning(f"Album art conversion failed: {e}")
            return None

    @staticmethod
    def _to_bwr(src, size):
        """Convert an RGB image to black/white/red for the BWR panel.

        Red is applied only where the source is genuinely, strongly red -
        so red stays an intentional accent instead of leaking into skin
        tones and warm midtones. Everything else is Floyd-Steinberg
        dithered to pure black/white at final pixel size.
        """
        img = src.resize((size, size), Image.LANCZOS)

        # Contrast lift stops broad midtones from becoming one flat field of
        # 50% dither noise.
        img = ImageEnhance.Contrast(img).enhance(1.35)

        r, g, b = [c.load() for c in img.split()]

        # 1-bit dithered luminance (PIL uses Floyd-Steinberg for mode "1")
        bw = img.convert("L").convert("1")
        bw_px = bw.load()

        out = Image.new("RGB", (size, size), (255, 255, 255))
        out_px = out.load()

        for y in range(size):
            for x in range(size):
                rv, gv, bv = r[x, y], g[x, y], b[x, y]
                mx, mn = max(rv, gv, bv), min(rv, gv, bv)
                sat = (mx - mn) / mx if mx else 0
                # Only genuinely saturated red becomes red ink. These
                # thresholds are deliberately strict so warm skin tones and
                # lips (which sit around sat 0.4-0.55 with a red-green gap
                # under ~110) stay black/white - red must read as an
                # intentional accent, not as blotches on faces.
                is_red = rv > 120 and sat > 0.62 and rv - max(gv, bv) > 95
                if is_red:
                    out_px[x, y] = (220, 0, 0)
                else:
                    out_px[x, y] = (0, 0, 0) if bw_px[x, y] == 0 else (255, 255, 255)

        return out

    @staticmethod
    def _prune_art_cache(keep=40):
        """Keep the art cache bounded (SD card hygiene)."""
        try:
            files = sorted(ART_CACHE_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
            for old in files[:-keep]:
                old.unlink()
        except Exception as e:
            logger.debug(f"Art cache prune skipped: {e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt_ms(ms):
        total = int((ms or 0) / 1000)
        return f"{total // 60}:{total % 60:02d}"

    @staticmethod
    def _local_clock(device_config):
        tz_name = device_config.get_config("timezone") or "UTC"
        try:
            now = datetime.now(pytz.timezone(tz_name))
        except Exception:
            now = datetime.now()
        fmt = "%I:%M %p" if device_config.get_config("time_format") == "12h" else "%H:%M"
        return now.strftime(fmt).lstrip("0")
