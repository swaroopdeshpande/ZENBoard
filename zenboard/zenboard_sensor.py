#!/usr/bin/env python3
"""
ZenBoard mmWave sensor service - presence + distance, single owner.

Replaces zenboard_presence.py. That read the OT2 digital pin; this reads the
UART instead, which carries the same presence state *plus* target range, and
is far more trustworthy on this unit - OT2 has been observed latched high
indefinitely while the UART reported clean ON/OFF transitions.

Everything to do with the sensor lives here on purpose. Two services both
reading /dev/serial0, or both writing /tmp/led_config.json, would fight:
one owner, no conflict.

Responsibilities
----------------
  - parse the sensor stream (ASCII "ON"/"OFF"/"Range N", or binary report frames)
  - presence state machine with debounce
  - /tmp/zenboard_presence.json   - consumed by refresh_task's presence gating
  - /tmp/zenboard_distance.json   - distance, for anything else that wants it
  - /tmp/led_config.json          - publishes "proximity" for the dist_* LED effects
  - MQTT presence binary_sensor + distance sensor, with HA discovery
  - poem trigger on a genuine dwell, and a refresh wake on arrival
"""

import json
import logging
import os
import re
import signal
import sys
import threading
import urllib.request
import time

import paho.mqtt.client as mqtt
import requests
import serial

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("zb_sensor")

PORT, BAUD = "/dev/serial0", 115200

RUNTIME_LED = "/tmp/led_config.json"
PERSIST_LED = "/home/zenith/InkyPi/src/config/led_config.json"
PRESENCE_FILE = "/tmp/zenboard_presence.json"
DISTANCE_FILE = "/tmp/zenboard_distance.json"

# Presence drives the LEDs and *gates* display refreshes; by default it never
# *causes* one. Walking in and out of a room should not repaint an e-ink panel
# - each repaint is ~25s of panel activity and visible flashing. Flip these on
# if you want the frame to react to arrival.
POEM_ON_DWELL = False        # AI micro-poem after a long dwell (superseded by greeting)
WAKE_REFRESH_ON_ENTRY = False  # unconditional refresh on entry - use the tiers below instead

# Returning after a real absence is worth a repaint; nipping out of the room
# is not. Two tiers, measured from when the room actually emptied.
# Phone notification on arrival. Gated on the same absence threshold as the
# refresh, and for the same reason: raw presence transitions are far too noisy
# to alert on. A single overnight capture logged 99 of them, because 24GHz sees
# through walls and picks up movement in adjoining rooms. Notifying on every
# transition would train you to ignore the notifications.
NOTIFY_ON_ARRIVAL = True

# Fluorescent-strike on entry. Gated on a short absence rather than fired on
# every presence transition: 24GHz sees through walls and one overnight capture
# logged 99 transitions, so an ungated strike would be flickering at the room
# more or less continuously. A minute is long enough to mean "went away and
# came back" and short enough that walking in still feels immediate.
STRIKE_ON_ENTRY = True
STRIKE_AFTER_AWAY = 60
NTFY_CONFIG = "/etc/zenboard/ntfy.json"

REFRESH_AFTER_AWAY = 30 * 60   # away this long -> plain refresh on return
GREET_AFTER_AWAY = 60 * 60     # away this long -> AI greeting instead

# Once the room has been empty a while the strip fades out rather than
# burning all night. Fade rather than snap, so it is not startling.
DIM_START_AFTER = 30 * 60      # begin fading once empty this long
DIM_DURATION = 10 * 60         # fully dark this long after fading starts

# Free OpenRouter models come and go: rate-limited upstream (429) or with no
# provider available (404) at any given moment. Tried in order until one
# answers, so a greeting does not silently fail because one provider is busy.
# Verified live: gemma-4-26b and gpt-oss-20b answered, gemma-4-31b was 429,
# both nemotrons 404.
GREET_MODELS = [
    "google/gemma-4-26b-a4b-it:free",
    "openai/gpt-oss-20b:free",
    "google/gemma-4-31b-it:free",
]

POEM_DWELL_SECONDS = 45
# A full e-ink repaint is ~25s of panel activity, so the poem is capped
# regardless of how many times someone comes and goes.
POEM_MIN_INTERVAL = 3600      # at most one poem an hour
# mmWave stops seeing a *motionless* target, so raw presence drops out while
# someone sits still and then re-arms - which was firing a fresh poem every
# few minutes. Hold presence for a while after the last positive reading so
# stillness is not mistaken for leaving.
PRESENCE_HOLD = 120
POEM_TRIGGER_URL = "http://localhost/update_now"
REFRESH_WAKE_URL = "http://localhost/api/presence/changed"

MQTT_HOST, MQTT_PORT = "localhost", 1883
BASE_TOPIC = "zenboard/presence"
STATE_TOPIC = f"{BASE_TOPIC}/state"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/availability"
DISCOVERY_TOPIC = "homeassistant/binary_sensor/zenboard_presence/config"
DIST_TOPIC = "zenboard/distance/state"
DIST_DISCOVERY = "homeassistant/sensor/zenboard_distance/config"

# Distance mapping, in centimetres. Tuned from a real walk-in capture where
# readings spanned ~9 to ~454 and tracked smoothly from 220 down to 105.
NEAR_CM, FAR_CM = 100, 400   # full effect at ~1m, idle beyond ~4m
GAMMA = 2.0
DEFAULT_MODE = "dist_glow"

EMA_ALPHA = 0.40        # distance smoothing (less lag; easing absorbs the noise)
VEL_ALPHA = 0.20        # velocity smoothing - noisier still, needs more damping
# Writes are throttled: the loop runs at 20Hz but re-serialising JSON that
# often, three times over, is wasted work on a Pi Zero. Only write when the
# value actually moved enough to matter.
PROX_WRITE_EPS = 0.0015  # finer: 0.004 quantised the fade into visible steps
STATUS_HZ = 4           # distance/presence files for the UI
MAX_STEP = 60 / 255.0   # near-instant; renderer easing does the smoothing
FADE_STEP = 3 / 255.0   # slower fade once the target is gone
TICK = 0.03
LOST_TIMEOUT = 3.0      # no reading for this long -> treat as absent

# The sensor emits ON/OFF continuously; require a few consistent readings
# before believing a change, so a single dropped line cannot flap presence.
CONFIRM_READS = 4

REPORT_HEAD, REPORT_TAIL = b"\xf4\xf3\xf2\xf1", b"\xf8\xf7\xf6\xf5"
ASCII_RANGE = re.compile(rb"Range\s+(\d+)", re.I)

_stop = False


def _sig(_s, _f):
    global _stop
    _stop = True


signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)


# ── files ─────────────────────────────────────────────────────────────────

def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def load_persisted_led():
    try:
        with open(PERSIST_LED) as f:
            return json.load(f)
    except Exception:
        return {"mode": DEFAULT_MODE, "color": "#FF6B35", "brightness": 128,
                "enabled": True}


def write_presence_state(present, last_seen):
    """Breadcrumb for refresh_task's presence gating. Deliberately in /tmp:
    it vanishes on reboot, and a missing file fails OPEN (refresh normally),
    so a dead sensor can never freeze the frame. updated_at is a heartbeat,
    which is how a hung service is told apart from an empty room."""
    try:
        _atomic_write(PRESENCE_FILE, {"present": bool(present),
                                      "last_seen": last_seen,
                                      "updated_at": time.time()})
    except Exception as e:
        logger.error(f"presence state write failed: {e}")


# ── sensor parsing ────────────────────────────────────────────────────────

def parse(buf):
    """Newest (distance_cm, present) from buf; either may be None."""
    dist = present = None

    while True:
        i = buf.find(REPORT_HEAD)
        if i < 0:
            break
        j = buf.find(REPORT_TAIL, i)
        if j < 0:
            break
        p = buf[i + 4:j]
        if len(p) >= 5:
            present = bool(p[2])
            dist = p[3] | (p[4] << 8)
        del buf[:j + 4]

    while b"\n" in buf:
        line, _, _ = bytes(buf).partition(b"\n")
        del buf[:len(line) + 1]
        s = line.strip()
        if not s:
            continue
        m = ASCII_RANGE.search(s)
        if m:
            dist = int(m.group(1))
        if re.search(rb"\bON\b", s, re.I):
            present = True
        elif re.search(rb"\bOFF\b", s, re.I):
            present = False

    if len(buf) > 4096:
        del buf[:-1024]
    return dist, present


def proximity_for(dist_cm):
    """0.0 far / absent .. 1.0 right in front."""
    if dist_cm is None:
        return 0.0
    d = max(NEAR_CM, min(FAR_CM, float(dist_cm)))
    return ((FAR_CM - d) / float(FAR_CM - NEAR_CM)) ** GAMMA


def notify(message, title=None, tags=None):
    """Fire-and-forget phone notification. Never raises, never blocks.

    Config is read fresh each call rather than cached, so the feed can be turned
    off by editing the file without restarting the sensor - and the sensor is
    the one service that should not be bounced casually, since it owns the
    serial port.
    """
    def _send():
        try:
            with open(NTFY_CONFIG) as f:
                cfg = json.load(f)
            if not cfg.get("enabled", True) or not cfg.get("notify_presence", True):
                return
            url, topic = cfg.get("url"), cfg.get("topic")
            if not url or not topic:
                return
            req = urllib.request.Request(
                f"{url.rstrip('/')}/{topic}",
                data=message.encode("utf-8"), method="POST")
            if title:
                req.add_header("Title", title)
            if tags:
                req.add_header("Tags", tags)
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:
            logger.debug(f"ntfy: {e}")

    threading.Thread(target=_send, daemon=True).start()


# ── MQTT ──────────────────────────────────────────────────────────────────

def make_mqtt():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id="zenboard-sensor")
    client.will_set(AVAILABILITY_TOPIC, payload="offline", retain=True)

    def on_connect(c, userdata, flags, reason_code, properties=None):
        logger.info(f"MQTT connected: {reason_code}")
        c.publish(AVAILABILITY_TOPIC, "online", retain=True)
        device = {"identifiers": ["zenboard"], "name": "ZenBoard",
                  "manufacturer": "DIY", "model": "InkyPi + HMMD mmWave"}
        c.publish(DISCOVERY_TOPIC, json.dumps({
            "name": "Presence", "unique_id": "zenboard_presence",
            "device_class": "occupancy", "state_topic": STATE_TOPIC,
            "payload_on": "ON", "payload_off": "OFF",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online", "payload_not_available": "offline",
            "device": device,
        }), retain=True)
        c.publish(DIST_DISCOVERY, json.dumps({
            "name": "Distance", "unique_id": "zenboard_distance",
            "state_topic": DIST_TOPIC, "unit_of_measurement": "cm",
            "device_class": "distance", "state_class": "measurement",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online", "payload_not_available": "offline",
            "device": device,
        }), retain=True)

    client.on_connect = on_connect
    return client


# ── actions ───────────────────────────────────────────────────────────────

def wake_refresh():
    """Tell the frame someone arrived so it can catch up a skipped refresh.
    Fire and forget - the display updating is never worth blocking polling."""
    try:
        requests.post(REFRESH_WAKE_URL, timeout=5)
    except Exception as e:
        logger.warning(f"refresh wake failed: {e}")


def _time_of_day():
    h = time.localtime().tm_hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 22:
        return "evening"
    return "night"


def trigger_greeting(away_minutes):
    """AI greeting for someone back after a long absence."""
    period = _time_of_day()
    prompt = (f"Write one short, warm greeting for someone arriving home in the {period}, "
              f"after being out for about {int(away_minutes)} minutes. "
              f"Maximum 14 words. Plain sentence, no quotation marks, no emoji.")
    logger.info(f"greeting: back after {int(away_minutes)}m ({period})")
    for model in GREET_MODELS:
        try:
            r = requests.post(POEM_TRIGGER_URL, data={
                "plugin_id": "ai_text",
                "title": f"Good {period}",
                "textModel": model,
                "textPrompt": prompt,
            }, timeout=120)
            if r.status_code == 200:
                logger.info(f"greeting shown via {model}")
                return
            logger.warning(f"greeting model {model} failed: {r.status_code} {r.text[:120]}")
        except Exception as e:
            logger.warning(f"greeting model {model} errored: {e}")

    # Every model refused. Still worth repainting - the point of the tier is
    # that a long absence deserves something fresh on the frame.
    logger.error("all greeting models failed; falling back to a plain refresh")
    wake_refresh()


def trigger_poem():
    try:
        logger.info("dwell threshold reached, triggering poem")
        r = requests.post(POEM_TRIGGER_URL,
                          data={"plugin_id": "presence_poem"}, timeout=60)
        logger.info(f"poem trigger: {r.status_code}")
    except Exception as e:
        logger.error(f"poem trigger failed: {e}")


# ── main ──────────────────────────────────────────────────────────────────

def main():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.05)
    except Exception as e:
        sys.exit(f"cannot open {PORT}: {e}")

    client = make_mqtt()
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    logger.info(f"sensor service started: near={NEAR_CM}cm far={FAR_CM}cm")

    buf = bytearray()
    smoothed = None
    prox = 0.0
    present = False
    raw_present = False
    candidate = None
    candidate_n = 0
    last_reading = 0.0
    last_seen = time.time()
    entered_at = None
    poem_done = False
    last_persist = 0.0
    last_heartbeat = 0.0
    last_mqtt_dist = None
    last_positive = 0.0
    last_poem = 0.0
    velocity = 0.0
    prev_dist = None
    prev_dist_t = 0.0
    last_written_prox = None
    last_written_mode = None
    pending_strike = 0.0
    last_written_strike = 0.0
    last_status = 0.0
    absent_since = time.time()   # assume empty at startup until proven otherwise
    base = load_persisted_led()
    write_presence_state(False, last_seen)

    while not _stop:
        try:
            chunk = ser.read(256)
            if chunk:
                buf.extend(chunk)
                dist, pres = parse(buf)
                if dist is not None and 0 < dist < 10000:
                    smoothed = (dist if smoothed is None
                                else EMA_ALPHA * dist + (1 - EMA_ALPHA) * smoothed)
                    tnow = time.time()
                    # cm/s, negative = approaching. Derived from the smoothed
                    # value; raw range is far too jumpy to differentiate.
                    if prev_dist is not None and tnow > prev_dist_t:
                        raw_v = (smoothed - prev_dist) / (tnow - prev_dist_t)
                        velocity = VEL_ALPHA * raw_v + (1 - VEL_ALPHA) * velocity
                    prev_dist, prev_dist_t = smoothed, tnow
                    last_reading = tnow
                if pres is not None:
                    raw_present = pres

            now = time.time()
            stale = (now - last_reading) > LOST_TIMEOUT
            if raw_present and not stale:
                last_positive = now
            # Hold on to presence through short dropouts; only genuinely
            # falling quiet for PRESENCE_HOLD counts as having left.
            wanted = (now - last_positive) < PRESENCE_HOLD

            # debounce presence transitions
            if wanted == present:
                candidate, candidate_n = None, 0
            else:
                if wanted == candidate:
                    candidate_n += 1
                else:
                    candidate, candidate_n = wanted, 1
                if candidate_n >= CONFIRM_READS:
                    present = wanted
                    candidate, candidate_n = None, 0
                    last_seen = now
                    if present:
                        away = (now - absent_since) if absent_since else 0.0
                        logger.info(f"Presence: ENTERED (away {away/60:.1f}m)")
                        entered_at = now
                        poem_done = False
                        absent_since = None
                        if STRIKE_ON_ENTRY and away >= STRIKE_AFTER_AWAY:
                            # A timestamp, not a mode. Writing the mode from
                            # here is what previously hijacked whatever the user
                            # had chosen in the web UI, within 50ms. The LED
                            # renderer watches this value change and plays the
                            # strike itself, so the selected mode is untouched.
                            pending_strike = now

                        if NOTIFY_ON_ARRIVAL and away >= REFRESH_AFTER_AWAY:
                            notify(f"Someone entered the room - away {away/60:.0f} min",
                                   title="ZenBoard presence", tags="wave")
                        if away >= GREET_AFTER_AWAY:
                            threading.Thread(target=trigger_greeting,
                                             args=(away / 60.0,), daemon=True).start()
                        elif away >= REFRESH_AFTER_AWAY:
                            threading.Thread(target=wake_refresh, daemon=True).start()
                        elif WAKE_REFRESH_ON_ENTRY:
                            threading.Thread(target=wake_refresh, daemon=True).start()
                    else:
                        logger.info("Presence: LEFT")
                        entered_at = None
                        poem_done = False
                        absent_since = now
                    write_presence_state(present, last_seen)
                    client.publish(STATE_TOPIC, "ON" if present else "OFF", retain=True)

            if present:
                last_seen = now

            # one poem per visit, only for a genuine lingering stay
            if (POEM_ON_DWELL and present and not poem_done and entered_at
                    and (now - entered_at) >= POEM_DWELL_SECONDS
                    and (now - last_poem) >= POEM_MIN_INTERVAL):
                poem_done = True
                last_poem = now
                threading.Thread(target=trigger_poem, daemon=True).start()

            if now - last_heartbeat >= 30:
                last_heartbeat = now
                write_presence_state(present, last_seen)

            if now - last_persist > 2.0:
                base = load_persisted_led()
                last_persist = now

            # proximity, slew-limited centrally so every LED effect inherits
            # the damping rather than each implementing its own
            target = proximity_for(smoothed) if present else 0.0
            step = MAX_STEP if present else FADE_STEP
            if prox < target:
                prox = min(target, prox + step)
            elif prox > target:
                prox = max(target, prox - step)

            if not present:
                velocity = 0.0

            if base.get("enabled", True):
                cfg = dict(base)
                # Publish sensor data, never override the chosen mode. An
                # earlier version forced a dist_* mode here, which silently
                # hijacked Static/Breathe/Rainbow the moment they were picked
                # and left the strip dim, because every dist_* effect scales
                # its output by proximity.
                cfg["proximity"] = round(prox, 4)
                cfg["velocity"] = round(velocity, 2)

                # progressive fade once the room has been empty a while
                if not present and absent_since:
                    empty_for = now - absent_since
                    if empty_for >= DIM_START_AFTER:
                        f = 1.0 - (empty_for - DIM_START_AFTER) / float(DIM_DURATION)
                        f = max(0.0, min(1.0, f))
                        if f <= 0.01:
                            cfg["mode"] = "off"
                        else:
                            cfg["brightness"] = int(cfg.get("brightness", 128) * f)
                # only rewrite when something visibly changed
                if (last_written_prox is None
                        or abs(cfg["proximity"] - last_written_prox) >= PROX_WRITE_EPS
                        or cfg.get("mode") != last_written_mode
                        or abs(velocity) > 1.0
                        or pending_strike != last_written_strike):
                    if pending_strike:
                        cfg["strike"] = pending_strike
                    _atomic_write(RUNTIME_LED, cfg)
                    last_written_prox = cfg["proximity"]
                    last_written_mode = cfg.get("mode")
                    last_written_strike = pending_strike

            shown = round(smoothed) if (present and smoothed) else None
            if now - last_status >= 1.0 / STATUS_HZ:
                last_status = now
                _atomic_write(DISTANCE_FILE, {
                    "distance_cm": shown,
                    "present": present,
                    "proximity": round(prox, 4),
                    "velocity": round(velocity, 1),
                    "mode": base.get("mode"),
                    "away_seconds": int(now - absent_since) if (absent_since and not present) else 0,
                    "updated_at": now})
            if shown != last_mqtt_dist:
                last_mqtt_dist = shown
                client.publish(DIST_TOPIC, shown if shown is not None else "")

        except Exception as e:
            logger.error(f"loop error: {e}")
            time.sleep(0.5)

        time.sleep(TICK)

    logger.info("stopping; restoring persisted LED config")
    try:
        _atomic_write(RUNTIME_LED, load_persisted_led())
        client.publish(AVAILABILITY_TOPIC, "offline", retain=True)
        client.loop_stop()
    except Exception:
        pass


if __name__ == "__main__":
    main()
