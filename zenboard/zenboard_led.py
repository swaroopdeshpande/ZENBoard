#!/usr/bin/env python3
import colorsys
import json
import logging
import math
import os
import time
import signal
import sys
import board
import neopixel

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("led_service")

CONFIG_FILE = "/tmp/led_config.json"
DEFAULT_CONFIG = {
    "mode": "warm_glow",
    "color": "#FF6B35",
    "brightness": 128,
    "breathe_speed": "medium",
    "refresh_flash": True,
    "presence_enabled": False,
    "presence_color_on": "#FF8C42",
    "presence_color_off": "#001133",
    "enabled": True,
}

LED_COUNT = 22

# auto_write=False + a single show() per frame. The per-pixel effects below
# touch every LED individually, and with auto_write on that was one full
# strip write per pixel - visible tearing and needless work on a Pi Zero.
pixels = neopixel.NeoPixel(board.D13, LED_COUNT, brightness=1.0, auto_write=False, pixel_order=neopixel.GRB)

def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))

def scale_color(r, g, b, brightness):
    factor = brightness / 255.0
    return int(r * factor), int(g * factor), int(b * factor)

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        result = DEFAULT_CONFIG.copy()
        result.update(cfg)
        return result
    except Exception:
        return DEFAULT_CONFIG.copy()

config = load_config()
last_mtime = 0
frame = 0

logger.info(f"LED strip initialized: {LED_COUNT} LEDs on GPIO13")
logger.info("LED service started")

def check_config_update():
    global config, last_mtime
    try:
        mtime = os.path.getmtime(CONFIG_FILE)
        if mtime != last_mtime:
            last_mtime = mtime
            config = load_config()
            logger.info(f"Config updated: mode={config['mode']}")
    except Exception:
        pass

def wheel(pos):
    pos = pos % 256
    if pos < 85:
        return int(pos * 3), int(255 - pos * 3), 0
    elif pos < 170:
        pos -= 85
        return int(255 - pos * 3), 0, int(pos * 3)
    else:
        pos -= 170
        return 0, int(pos * 3), int(255 - pos * 3)

def get_active_range():
    """Return LED range based on orientation."""
    orientation = config.get("orientation", "horizontal")
    if orientation in ("portrait", "vertical"):
        return range(10, 17)   # LEDs 11-17 (0-indexed: 10-16)
    else:
        return range(0, 10)    # LEDs 1-10 (0-indexed: 0-9)

def set_range(r, g, b, led_range=None):
    """Set color for active range only, turn off rest."""
    active = led_range or get_active_range()
    for i in range(LED_COUNT):
        if i in active:
            pixels[i] = (r, g, b)
        else:
            pixels[i] = (0, 0, 0)


# ── Helpers for the smooth/ambient effects ───────────────────────────────
# These are re-implementations of well-known WLED/FastLED effects
# (Pacifica, Aurora, Sunrise, Candle, Sinelon, Plasma, Lake, Twinklefox),
# adapted for a short ~10-LED run rather than a long strip: the originals
# lean on spatial detail across hundreds of pixels, so here the emphasis
# shifts to smooth motion over time instead.
#
# All of them are computed analytically from the frame counter - no
# persistent per-LED state. That means a mode switch or a config reload
# can never leave a stale trail behind, and the effect is identical after
# a service restart.

def hsv(h, s, v):
    """h/s/v in 0..1 -> 0..255 RGB tuple."""
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v)))
    return int(r * 255), int(g * 255), int(b * 255)

def ramp(stops, t):
    """Interpolate an ordered list of (position, (r,g,b)) colour stops."""
    t = max(0.0, min(1.0, t))
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if p0 <= t <= p1:
            span = (p1 - p0) or 1.0
            f = (t - p0) / span
            return tuple(int(c0[j] + (c1[j] - c0[j]) * f) for j in range(3))
    return stops[-1][1]

def paint(active, fn):
    """Run fn(index_within_active, fraction_along_run) -> (r,g,b) for each
    active LED, blanking everything outside the active range."""
    active_list = list(active)
    n = len(active_list)
    last = max(n - 1, 1)
    for i in range(LED_COUNT):
        pixels[i] = (0, 0, 0)
    for idx, i in enumerate(active_list):
        r, g, b = fn(idx, idx / last)
        pixels[i] = scale_color(r, g, b, config["brightness"])


def run_frame():
    global frame
    if not config.get("enabled", True):
        pixels.fill((0, 0, 0))
        pixels.show()
        frame += 1
        return

    mode = config.get("mode", "warm_glow")
    active = get_active_range()

    if mode == "off":
        pixels.fill((0, 0, 0))
    elif mode == "static":
        r, g, b = hex_to_rgb(config["color"])
        r, g, b = scale_color(r, g, b, config["brightness"])
        set_range(r, g, b)
    elif mode == "breathe":
        speeds = {"slow": 0.02, "medium": 0.04, "fast": 0.08}
        speed = speeds.get(config["breathe_speed"], 0.04)
        t = frame * speed
        factor = (math.sin(t) + 1) / 2
        br = int(config["brightness"] * factor)
        r, g, b = hex_to_rgb(config["color"])
        r, g, b = scale_color(r, g, b, br)
        set_range(r, g, b)
    elif mode == "warm_glow":
        base_br = config["brightness"]
        flicker = math.sin(frame * 0.1) * 5 + math.sin(frame * 0.23) * 3
        br = max(0, min(255, base_br + int(flicker)))
        r, g, b = scale_color(255, 180, 80, br)
        set_range(r, g, b)
    elif mode == "rainbow":
        offset = frame * 2
        for i in range(LED_COUNT):
            if i in active:
                hue = (i * 256 // len(active) + offset) % 256
                r, g, b = wheel(hue)
                br = config["brightness"]
                pixels[i] = scale_color(r, g, b, br)
            else:
                pixels[i] = (0, 0, 0)
    elif mode == "chase":
        active_list = list(active)
        pos = active_list[frame % len(active_list)]
        r, g, b = hex_to_rgb(config["color"])
        br = config["brightness"]
        for i in range(LED_COUNT):
            if i == pos:
                pixels[i] = scale_color(r, g, b, br)
            elif i == active_list[(active_list.index(pos) - 1) % len(active_list)]:
                pixels[i] = scale_color(r, g, b, br // 3)
            else:
                pixels[i] = (0, 0, 0)

    # ── smooth / ambient effects ─────────────────────────────────────────

    elif mode == "pacifica":
        # Gentle blue-green ocean waves. Three sine layers at different
        # spatial scales and drift speeds, so crests never line up and the
        # motion doesn't visibly repeat.
        t = frame * 0.045
        def f(idx, pos):
            w = (math.sin(pos * 3.1 + t * 0.9)
                 + math.sin(pos * 5.3 - t * 0.62)
                 + math.sin(pos * 1.7 + t * 0.33))
            lum = ((w / 3.0) + 1) / 2
            lum = 0.12 + 0.88 * (lum ** 1.7)   # deep troughs, bright crests
            return ramp([(0.0, (0, 20, 70)), (0.55, (0, 120, 170)), (1.0, (70, 230, 255))], lum)
        paint(active, f)

    elif mode == "aurora":
        # Slow curtains of green shading into violet over a dark blue base.
        t = frame * 0.028
        def f(idx, pos):
            curtain = math.sin(pos * 2.2 + t) * math.sin(pos * 1.3 - t * 0.66)
            lum = max(0.0, (curtain + 1) / 2) ** 1.5
            hue = 0.36 + 0.20 * math.sin(pos * 1.1 + t * 0.4)   # green -> teal -> violet
            r, g, b = hsv(hue, 0.85, 0.15 + 0.85 * lum)
            return r, g, b
        paint(active, f)

    elif mode == "sunrise":
        # Gradual dawn then dusk, looping. WLED drives this off a duration
        # in minutes; same idea here, as a triangle wave over the whole run.
        minutes = float(config.get("sunrise_minutes", 10) or 10)
        period = max(minutes * 60.0 * 20.0, 1.0)   # 20 fps
        tri = (frame % period) / period
        progress = tri * 2 if tri < 0.5 else (1.0 - tri) * 2
        base = ramp([
            (0.00, (0, 0, 0)),
            (0.18, (50, 4, 0)),
            (0.42, (160, 40, 0)),
            (0.70, (255, 130, 30)),
            (1.00, (255, 200, 120)),
        ], progress)
        def f(idx, pos):
            # A touch brighter toward the middle of the run, like a sun
            # sitting on the horizon rather than a flat wash.
            lift = 0.85 + 0.15 * math.sin(pos * math.pi)
            return tuple(int(c * lift) for c in base)
        paint(active, f)

    elif mode == "candle":
        # Per-LED independent flicker (WLED's "Candle Multi"), built from
        # three incommensurate sines so no two LEDs flicker in step.
        def f(idx, pos):
            p = idx * 1.73
            fl = (math.sin(frame * 0.13 + p) * 0.35
                  + math.sin(frame * 0.31 + p * 2.1) * 0.22
                  + math.sin(frame * 0.07 + p * 0.6) * 0.43)
            lum = max(0.15, min(1.0, 0.62 + 0.38 * fl))
            return int(255 * lum), int(147 * lum), int(41 * lum)
        paint(active, f)

    elif mode == "sinelon":
        # A sinusoidally moving eye with a soft trail - the smooth cousin of
        # the existing hard-edged "chase".
        active_list = list(active)
        n = len(active_list)
        eye = (math.sin(frame * 0.038) + 1) / 2 * (n - 1)
        r0, g0, b0 = hex_to_rgb(config["color"])
        def f(idx, pos):
            # Wide, gently-falling trail. On a ~10 LED run a tight trail
            # makes the eye visibly hop between pixels; spreading it over
            # ~4 LEDs keeps the movement reading as continuous.
            d = abs(idx - eye)
            lum = max(0.0, 1.0 - d / 4.0) ** 1.6
            return int(r0 * lum), int(g0 * lum), int(b0 * lum)
        paint(active, f)

    elif mode == "plasma":
        # Interfering waves through the full hue circle - a plasma lamp.
        def f(idx, pos):
            x = idx * 0.6
            v = (math.sin(x + frame * 0.05)
                 + math.sin(x * 0.5 + frame * 0.031)
                 + math.sin((x + frame * 0.021) * 0.7))
            hue = (((v / 3.0) + 1) / 2) * 0.8
            return hsv(hue, 0.9, 0.35 + 0.65 * abs(math.sin(v)))
        paint(active, f)

    elif mode == "lake":
        # Calm waving around whatever colour is configured - keeps the user's
        # chosen hue rather than imposing a palette, just drifts either side
        # of it and breathes slowly.
        r0, g0, b0 = hex_to_rgb(config["color"])
        h0, s0, v0 = colorsys.rgb_to_hsv(r0 / 255.0, g0 / 255.0, b0 / 255.0)
        t = frame * 0.022
        def f(idx, pos):
            wave = math.sin(pos * 2.4 + t) * 0.5 + math.sin(pos * 1.1 - t * 0.7) * 0.5
            hue = h0 + 0.045 * wave
            val = v0 * (0.45 + 0.55 * ((wave + 1) / 2))
            return hsv(hue, s0, val)
        paint(active, f)

    elif mode == "twinklefox":
        # Gentle twinkling, slow fade in and out, each LED on its own cycle
        # (offset by the golden ratio so they never sync up).
        r0, g0, b0 = hex_to_rgb(config["color"])
        seconds = frame / 20.0
        def f(idx, pos):
            period = 3.0 + (idx * 0.7) % 2.5
            phase = (seconds / period + idx * 0.6180339887) % 1.0
            lum = math.sin(math.pi * phase) ** 2
            lum = 0.08 + 0.92 * lum
            return int(r0 * lum), int(g0 * lum), int(b0 * lum)
        paint(active, f)

    elif mode == "refresh_flash":
        flash_period = 20
        t = frame % flash_period
        if t < flash_period // 2:
            set_range(255, 255, 255)
        else:
            pixels.fill((0, 0, 0))
    else:
        pixels.fill((0, 0, 0))

    pixels.show()
    frame += 1

def handle_signal(sig, frame_sig):
    pixels.fill((0, 0, 0))
    pixels.show()
    logger.info("LEDs off, service stopped")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

while True:
    try:
        check_config_update()
        run_frame()
    except Exception as e:
        logger.error(f"Error: {e}")
    time.sleep(0.05)
