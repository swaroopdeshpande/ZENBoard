#!/usr/bin/env python3
import colorsys
import json
import logging
import math
import os
import random
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

LED_COUNT = 10

# Refresh strobe. 0.01s half-period gives a 50Hz on/off cycle, which reads as a
# strobe rather than a blink; 0.3s is long enough to register out of the corner
# of the eye and short enough not to become annoying on every refresh.
STROBE_HALF_PERIOD = 0.01
STROBE_SECONDS = 0.30

# Tubelight. Mostly a steady lamp, interrupted by the stutter of a fluorescent
# tube whose starter is going. Probabilities are per frame at ~33Hz, so
# TUBE_FAULT_CHANCE of 0.004 works out to a fault roughly every 8 seconds -
# frequent enough to notice, rare enough that the lamp still reads as "on"
# rather than "broken".
TUBE_FAULT_CHANCE = 0.004
TUBE_HUM_DEPTH = 0.04        # mains ripple on a steady tube, barely visible
_tube_state = "steady"
_tube_frames = 0
_tube_level = 1.0

# Strike-on-entry, triggered by the sensor. Seeded from the first config seen
# rather than from zero, so restarting the service does not replay a stale
# trigger the instant it comes up.
_last_strike = None

# Neon: a bar sign fails in patches, not all at once - a section of tube goes
# dark and stutters back. Tubelight's whole-strip failure is a different thing.
_neon_out_at = -1
_neon_out_len = 0
_neon_frames = 0

# Filament: thermal mass. An incandescent bulb has no instant state; it warms
# up and cools down, which is the entire character. Held between frames.
_fil_temp = 0.0
_fil_sag = 0

# Projector: film runs at 24fps against this 33Hz loop, so the shutter is
# tracked in its own accumulator rather than by frame parity.
_proj_phase = 0.0
_proj_weave = 0

# CRT: a television holds a scene then cuts. Level is held for a random run of
# frames, which is what separates it from any smooth breathing effect.
_crt_level = 0.5
_crt_hold = 0

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


# The sensor writes proximity at discrete intervals, so reading it raw makes
# every non-animated distance effect hold its value and then jump. Easing
# toward the target here, every rendered frame, decouples visual smoothness
# from how often the sensor happens to write - raising the frame rate alone
# just renders the same stale number more times.
_prox_shown = 0.0
PROX_EASE = 0.22          # per frame at ~33Hz -> about a 0.9s glide


def _eased_proximity(target):
    global _prox_shown
    _prox_shown += (target - _prox_shown) * PROX_EASE
    if abs(target - _prox_shown) < 0.0005:
        _prox_shown = target
    return _prox_shown


def run_frame():
    global frame
    if not config.get("enabled", True):
        pixels.fill((0, 0, 0))
        pixels.show()
        frame += 1
        return

    mode = config.get("mode", "warm_glow")
    active = get_active_range()

    # Entry strike. The sensor publishes a timestamp, never a mode - writing
    # the mode from there is what previously hijacked the user's choice within
    # 50ms. Playing it here leaves the selected mode untouched.
    global _last_strike
    strike = config.get("strike", 0)
    if _last_strike is None:
        _last_strike = strike
    elif strike != _last_strike:
        _last_strike = strike
        if mode == "tubelight":
            r0, g0, b0 = hex_to_rgb(config["color"])
            r0, g0, b0 = scale_color(r0, g0, b0, config["brightness"])
            # A tube starting: several failed strikes with uneven pauses, then
            # it catches. The unevenness is the whole character - regular
            # flashes read as a strobe, which is a different effect entirely.
            for on_t, off_t in ((0.04, 0.18), (0.03, 0.09), (0.06, 0.22),
                                (0.03, 0.05), (0.05, 0.14), (0.10, 0.04)):
                set_range(r0, g0, b0)
                pixels.show()
                time.sleep(on_t)
                pixels.fill((0, 0, 0))
                pixels.show()
                time.sleep(off_t)
            set_range(r0, g0, b0)
            pixels.show()
            time.sleep(0.35)

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


    # ── distance-reactive effects ────────────────────────────────────────
    # Driven by "proximity" in the config: 0.0 = far away / nobody there,
    # 1.0 = right in front of the sensor. zenboard_distance_led.py writes it
    # from the mmWave range readings; if that service is not running the key
    # is absent and these all sit at their idle state rather than misbehaving.

    elif mode.startswith("dist_"):
        # Distance-reactive. zenboard_sensor.py publishes:
        #   proximity  0.0 far/empty .. 1.0 right in front
        #   velocity   cm/s, negative approaching, positive receding
        # Absent keys mean the sensor service is not running; everything below
        # then sits at its idle state rather than misbehaving.
        #
        # These all act on the WHOLE strip. Earlier attempts that lit
        # individual pixels - bar meters, tracking comets - read as fussy on a
        # 10-LED run behind a picture frame. Colour and intensity across the
        # entire run are legible from anywhere in the room without inviting
        # you to stare at the LEDs.
        prox = _eased_proximity(max(0.0, min(1.0, float(config.get("proximity", 0.0) or 0.0))))
        vel = float(config.get("velocity", 0.0) or 0.0)
        br = config["brightness"]
        r0, g0, b0 = hex_to_rgb(config["color"])

        if mode == "dist_fade":
            # The chosen colour, simply swelling and receding with distance.
            # Unlike the other distance effects this keeps the exact colour
            # picked in the UI and varies only its intensity, and it goes
            # fully dark at range rather than holding an idle floor.
            lvl = prox ** 1.6
            set_range(*scale_color(r0, g0, b0, int(br * lvl)))

        elif mode == "dist_ember":
            # Fire seen from across the room: deep ember at range, flaring to
            # white-hot as you arrive, with a slow flicker that grows with it.
            flicker = 1.0 + 0.05 * prox * math.sin(frame * 0.21) \
                          + 0.03 * prox * math.sin(frame * 0.37)
            r = 255
            g = int((40 + 175 * prox) * flicker)
            b = int((0 + 120 * (prox ** 2.2)) * flicker)
            lvl = 0.18 + 0.82 * prox
            set_range(*scale_color(r, min(255, g), min(255, b), int(br * lvl)))

        elif mode == "dist_breathe":
            # Breathing that quickens and deepens as you approach: barely
            # moving from across the room, urgent up close.
            speed = 0.012 + 0.085 * prox
            depth = 0.15 + 0.65 * prox
            beat = (math.sin(frame * speed) + 1) / 2
            lvl = (0.12 + 0.88 * prox) * (1.0 - depth + depth * beat)
            set_range(*scale_color(r0, g0, b0, int(br * lvl)))

        elif mode == "dist_aurora":
            # Whole-strip colour that drifts on its own and tightens as you
            # near: wide wandering hues far away, settling toward the chosen
            # colour's hue when you are in front of it.
            import colorsys as _cs
            h0, _, _ = _cs.rgb_to_hsv(r0 / 255.0, g0 / 255.0, b0 / 255.0)
            wander = (1.0 - prox) * 0.42
            hue = h0 + wander * math.sin(frame * 0.018)
            lvl = 0.15 + 0.85 * prox
            set_range(*scale_color(*hsv(hue, 0.9, lvl), br))

        elif mode == "dist_velocity":
            # Reacts to movement rather than position: cools blue as you back
            # away, flares warm as you close in, and rests on the configured
            # colour when you hold still.
            v = max(-1.0, min(1.0, -vel / 60.0))     # -1 receding .. +1 approaching
            if v >= 0:
                rr = int(r0 + (255 - r0) * v)
                gg = int(g0 * (1 - 0.6 * v))
                bb = int(b0 * (1 - 0.8 * v))
            else:
                k = -v
                rr = int(r0 * (1 - 0.85 * k))
                gg = int(g0 * (1 - 0.3 * k) + 60 * k)
                bb = int(b0 * (1 - 0.2 * k) + 255 * k)
            lvl = 0.2 + 0.8 * prox
            set_range(*scale_color(max(0, rr), max(0, gg), min(255, max(0, bb)), int(br * lvl)))

        else:
            # dist_hue - the original, and still the most legible: cool blue at
            # range, through green, to warm red on arrival.
            hue = 0.58 * (1.0 - prox)
            lvl = 0.15 + 0.85 * prox
            set_range(*scale_color(*hsv(hue, 0.95, lvl), br))

    elif mode == "tubelight":
        # A fluorescent tube does not fade, it snaps. So brightness is chosen
        # per frame from discrete levels rather than interpolated - anything
        # smooth here immediately reads as "breathing", which is a different
        # effect entirely.
        global _tube_state, _tube_frames, _tube_level
        r, g, b = hex_to_rgb(config["color"])

        if _tube_frames <= 0:
            if _tube_state == "steady":
                # Two failure modes, because a real tube has both: a brief
                # stutter, and a full restrike where it drops out and has to
                # strike again.
                if random.random() < 0.35:
                    _tube_state, _tube_frames = "restrike", random.randint(6, 14)
                else:
                    _tube_state, _tube_frames = "stutter", random.randint(3, 9)
            else:
                _tube_state = "steady"
                # Long steady stretches are what sells it. Without them the
                # strip just looks like it is malfunctioning constantly.
                _tube_frames = random.randint(40, 400)

        if _tube_state == "steady":
            # Mains hum: a shallow ripple so a "steady" tube is not perfectly
            # flat, which reads as LED rather than fluorescent.
            _tube_level = 1.0 - TUBE_HUM_DEPTH * (0.5 + 0.5 * math.sin(frame * 1.7))
            if random.random() < TUBE_FAULT_CHANCE:
                _tube_frames = 0
        elif _tube_state == "stutter":
            _tube_level = random.choice([0.0, 0.0, 0.15, 1.0, 1.0, 0.6])
        else:  # restrike - dark, then a couple of failed strikes, then back
            _tube_level = 0.0 if _tube_frames > 4 else random.choice([0.0, 1.0, 0.3])

        _tube_frames -= 1
        lvl = max(0.0, min(1.0, _tube_level))
        br = int(config["brightness"] * lvl)
        rr, gg, bb = scale_color(r, g, b, br)
        set_range(rr, gg, bb)

    elif mode == "neon":
        # Steady sign with a faint mains buzz, punctuated by one section going
        # dark and stuttering back. The dropout is spatial - that is what makes
        # it read as neon rather than as the tubelight effect.
        global _neon_out_at, _neon_out_len, _neon_frames
        r, g, b = hex_to_rgb(config["color"])
        hum = 1.0 - 0.05 * (0.5 + 0.5 * math.sin(frame * 2.3))
        base = int(config["brightness"] * hum)
        rr, gg, bb = scale_color(r, g, b, base)

        if _neon_frames <= 0:
            if _neon_out_at >= 0:
                _neon_out_at, _neon_frames = -1, random.randint(60, 400)
            elif random.random() < 0.03:
                _neon_out_len = random.randint(2, max(2, LED_COUNT // 3))
                _neon_out_at = random.randint(0, max(0, LED_COUNT - _neon_out_len))
                _neon_frames = random.randint(4, 16)
            else:
                _neon_frames = 8
        _neon_frames -= 1

        set_range(rr, gg, bb)
        if _neon_out_at >= 0:
            # the failing section gutters rather than switching cleanly off
            for i in range(_neon_out_at, min(LED_COUNT, _neon_out_at + _neon_out_len)):
                if i in get_active_range():
                    pixels[i] = (0, 0, 0) if random.random() < 0.75 else (rr // 3, gg // 3, bb // 3)

    elif mode == "filament":
        # Thermal lag both ways: it glows up rather than switching on, and the
        # colour runs deep orange while cold, reaching the chosen colour only at
        # full temperature. Occasional voltage sag dips it and it recovers
        # slowly, because a hot filament cannot dim quickly.
        global _fil_temp, _fil_sag
        r, g, b = hex_to_rgb(config["color"])

        target = 1.0
        if _fil_sag > 0:
            _fil_sag -= 1
            target = 0.45
        elif random.random() < 0.002:
            _fil_sag = random.randint(8, 30)

        _fil_temp += (target - _fil_temp) * 0.035
        t = max(0.0, min(1.0, _fil_temp))

        # cold filament is redder: pull green and blue down harder than red
        cr = r
        cg = int(g * (0.25 + 0.75 * t))
        cb = int(b * (0.05 + 0.95 * (t ** 2)))
        ripple = 1.0 - 0.02 * (0.5 + 0.5 * math.sin(frame * 3.1))
        rr, gg, bb = scale_color(cr, cg, cb, int(config["brightness"] * t * ripple))
        set_range(rr, gg, bb)

    elif mode == "projector":
        # 24fps shutter against a 33Hz render loop, tracked in its own phase
        # accumulator so the flicker rate stays filmic instead of aliasing with
        # the frame rate. Exposure varies slightly per frame, and every so often
        # the gate weaves and the whole image jumps.
        global _proj_phase, _proj_weave
        r, g, b = hex_to_rgb(config["color"])
        _proj_phase += 24.0 / 33.0
        shutter = 0.30 if (_proj_phase % 1.0) < 0.22 else 1.0

        exposure = 1.0 + random.uniform(-0.07, 0.07)
        if _proj_weave > 0:
            _proj_weave -= 1
            exposure *= 1.25
        elif random.random() < 0.01:
            _proj_weave = random.randint(2, 5)

        lvl = max(0.0, min(1.0, shutter * exposure))
        rr, gg, bb = scale_color(r, g, b, int(config["brightness"] * lvl))
        set_range(rr, gg, bb)
        if _proj_weave > 0:
            # the frame slips in the gate: darken one end so the light shifts
            act = sorted(get_active_range())
            for i in act[:max(1, len(act) // 6)]:
                pixels[i] = (0, 0, 0)

    elif mode == "crt":
        # A television in a dark room. Brightness is held for a run of frames
        # and then cuts to a new level - scene changes, not a smooth curve -
        # with the cool cast of a screen rather than the chosen colour.
        global _crt_level, _crt_hold
        if _crt_hold <= 0:
            _crt_hold = random.randint(4, 40)
            # mostly mid levels, occasionally a bright cut
            _crt_level = random.uniform(0.25, 0.75) if random.random() < 0.85 \
                else random.uniform(0.85, 1.0)
        _crt_hold -= 1

        jitter = 1.0 + random.uniform(-0.04, 0.04)
        lvl = max(0.0, min(1.0, _crt_level * jitter))
        bright = int(config["brightness"] * lvl)
        # screen light is blue-white; the identity of this effect is its cast,
        # so it is not taken from the configured colour
        rr, gg, bb = scale_color(190, 215, 255, bright)
        set_range(rr, gg, bb)
        # a screen is not uniform - vary a couple of pixels each frame
        for i in get_active_range():
            if random.random() < 0.15:
                f = random.uniform(0.75, 1.0)
                pixels[i] = (int(rr * f), int(gg * f), int(bb * f))

    elif mode == "refresh_flash":
        # A real strobe, run as one blocking burst rather than as a pattern
        # spread across the 33Hz render loop.
        #
        # The old version alternated every 10 frames, which at 0.03s a frame is
        # a 1.7Hz blink - it read as slow winking, not a strobe. The loop rate
        # caps frame-based flashing at 16Hz even at its theoretical fastest, so
        # the burst runs its own tight timing instead and reaches ~50Hz.
        #
        # Blocking for STROBE_SECONDS is deliberate and safe: it is shorter than
        # a single frame budget matters for, and nothing else may drive the
        # strip concurrently anyway.
        r, g, b = hex_to_rgb(config["color"])
        r, g, b = scale_color(r, g, b, config["brightness"])
        # A colour set to near-black would make the strobe invisible, so fall
        # back to white - the point of the flash is that it is noticed.
        if r + g + b < 40:
            r = g = b = 255

        half = STROBE_HALF_PERIOD
        cycles = max(1, int(STROBE_SECONDS / (half * 2)))
        for _ in range(cycles):
            set_range(r, g, b)
            pixels.show()
            time.sleep(half)
            pixels.fill((0, 0, 0))
            pixels.show()
            time.sleep(half)
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
    time.sleep(0.03)   # ~33Hz; 20Hz made the slower effects visibly step
