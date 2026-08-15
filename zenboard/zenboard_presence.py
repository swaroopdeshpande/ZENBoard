#!/usr/bin/env python3
"""
ZenBoard Presence Monitor - Waveshare HMMD mmWave sensor (S3KM1110, 24GHz FMCW)

Reads the sensor's digital OUT pin (active-high on presence - the module
handles its own FMCW processing and debounce internally, so this stays a
simple, reliable digital read rather than parsing the undocumented UART
frame protocol). TX/RX are wired but unused for now - available later for
sensitivity/distance-range configuration via UART if wanted.

On each presence transition:
  - drives LED effects (reuses the existing zenboard_led.py runtime config
    file - no changes needed to that script, it already watches this file)
  - publishes MQTT state with Home Assistant MQTT-discovery, so HA picks up
    a binary_sensor automatically whenever it's up, even if it was down
    when this started. Retained messages + a Last-Will-and-Testament
    availability topic mean HA always has a correct picture on reconnect.

Wiring (3.3V logic only):
  Sensor VCC -> Pi 3.3V (physical pin 1)
  Sensor GND -> Pi GND
  Sensor OUT -> Pi GPIO4 (physical pin 7)
  Sensor TX  -> Pi GPIO15/RXD (physical pin 10)  [unused for now]
  Sensor RX  -> Pi GPIO14/TXD (physical pin 8)   [unused for now]
"""

import json
import logging
import threading
import time

import signal
import sys

import RPi.GPIO as GPIO
import paho.mqtt.client as mqtt
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("presence")

OUT_PIN = 4
POLL_HZ = 10
CONFIRM_READS = 3          # consecutive same-state reads before accepting a transition
ENTER_PULSE_SECONDS = 3.0  # fast "welcome" breathe before settling to steady glow
LEAVE_FADE_SECONDS = 2.5   # slow fade-out before going idle
POEM_DWELL_SECONDS = 45    # lingering (not just passing through) before a poem triggers
POEM_TRIGGER_URL = "http://localhost/update_now"

PRESENCE_STATE_FILE = "/tmp/zenboard_presence.json"
PRESENCE_HEARTBEAT_SECONDS = 30   # refresh_task treats a stale file as "sensor dead" and stops gating
REFRESH_WAKE_URL = "http://localhost/api/presence/changed"

LED_RUNTIME_FILE = "/tmp/led_config.json"
LED_PERSIST_FILE = "/home/zenith/InkyPi/src/config/led_config.json"

MQTT_HOST = "localhost"
MQTT_PORT = 1883
BASE_TOPIC = "zenboard/presence"
STATE_TOPIC = f"{BASE_TOPIC}/state"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/availability"
DISCOVERY_TOPIC = "homeassistant/binary_sensor/zenboard_presence/config"


# ── LED effects ──────────────────────────────────────────────────────────

def _load_persisted_led_config():
    """The user's actual chosen baseline (set via the web UI) - what we
    restore to once presence effects are done, not just an in-memory
    snapshot that could drift/be lost on a crash mid-sequence."""
    try:
        with open(LED_PERSIST_FILE) as f:
            return json.load(f)
    except Exception:
        return {
            "mode": "warm_glow", "color": "#FF6B35", "brightness": 128,
            "breathe_speed": "medium", "refresh_flash": True,
            "presence_enabled": True, "presence_color_on": "#FF8C42",
            "presence_color_off": "#001133", "enabled": True,
        }


def _write_runtime_led(cfg):
    try:
        with open(LED_RUNTIME_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception as e:
        logger.warning(f"LED runtime write failed: {e}")


def led_presence_sequence(entering: bool):
    """Runs in its own thread so it never blocks the sensor poll loop."""
    baseline = _load_persisted_led_config()
    if not baseline.get("presence_enabled", True):
        return  # presence LED effects turned off in settings - respect it

    if entering:
        # Bright, fast "someone's here" pulse
        pulse = dict(baseline)
        pulse.update({
            "mode": "breathe",
            "color": baseline.get("presence_color_on", "#FF8C42"),
            "breathe_speed": "fast",
            "brightness": 255,
            "enabled": True,
        })
        _write_runtime_led(pulse)
        time.sleep(ENTER_PULSE_SECONDS)

        # Settle into a calm steady glow for as long as presence holds
        hold = dict(baseline)
        hold.update({
            "mode": "static",
            "color": baseline.get("presence_color_on", "#FF8C42"),
            "brightness": 140,
            "enabled": True,
        })
        _write_runtime_led(hold)
    else:
        # Slow fade using the "someone left" color, then back to baseline
        fade = dict(baseline)
        fade.update({
            "mode": "breathe",
            "color": baseline.get("presence_color_off", "#001133"),
            "breathe_speed": "slow",
            "brightness": 90,
            "enabled": True,
        })
        _write_runtime_led(fade)
        time.sleep(LEAVE_FADE_SECONDS)
        _write_runtime_led(baseline)


# ── MQTT ─────────────────────────────────────────────────────────────────

def make_mqtt_client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="zenboard-presence")
    client.will_set(AVAILABILITY_TOPIC, payload="offline", retain=True)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        logger.info(f"MQTT connected: {reason_code}")
        client.publish(AVAILABILITY_TOPIC, "online", retain=True)
        # HA MQTT discovery - auto-creates the entity whenever HA is up,
        # no manual YAML needed on the HA side.
        discovery_payload = {
            "name": "Presence",
            "unique_id": "zenboard_presence",
            "device_class": "occupancy",
            "state_topic": STATE_TOPIC,
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": {
                "identifiers": ["zenboard"],
                "name": "ZenBoard",
                "manufacturer": "DIY",
                "model": "InkyPi + HMMD mmWave",
            },
        }
        client.publish(DISCOVERY_TOPIC, json.dumps(discovery_payload), retain=True)

    client.on_connect = on_connect
    return client


def write_presence_state(present, last_seen):
    """Breadcrumb for refresh_task's presence-gated refresh. Deliberately in
    /tmp: on reboot it vanishes, and a missing file means "no idea" which
    fails OPEN (refresh normally). A dead sensor must never freeze the frame.
    updated_at is a heartbeat, not just a change marker, so a hung service is
    distinguishable from a genuinely empty room."""
    try:
        with open(PRESENCE_STATE_FILE, "w") as f:
            json.dump({
                "present": bool(present),
                "last_seen": last_seen,
                "updated_at": time.time(),
            }, f)
    except Exception as e:
        logger.error(f"Failed writing presence state: {e}")


def wake_refresh():
    """Tell the frame someone walked in, so it can catch up on a refresh it
    skipped while the room was empty. Fire-and-forget - the frame updating
    is a nice-to-have, never worth blocking presence polling over."""
    try:
        requests.post(REFRESH_WAKE_URL, timeout=5)
    except Exception as e:
        logger.warning(f"Refresh wake failed: {e}")


def trigger_poem():
    """Runs in its own thread - the render takes 15-20s (AI call + headless
    Chromium), must never block presence polling."""
    try:
        logger.info("Presence: dwell threshold reached, triggering poem")
        r = requests.post(POEM_TRIGGER_URL, data={"plugin_id": "presence_poem"}, timeout=45)
        logger.info(f"Poem trigger response: {r.status_code}")
    except Exception as e:
        logger.error(f"Poem trigger failed: {e}")


# ── Main loop ────────────────────────────────────────────────────────────

def _handle_signal(signum, frame):
    logger.info("Shutting down, releasing GPIO...")
    GPIO.cleanup()
    sys.exit(0)


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(OUT_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)  # LOW/no-presence if sensor unwired

    client = make_mqtt_client()
    # Keep retrying in the background if the broker (or HA) isn't up yet -
    # never block presence detection on MQTT being available.
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    present = False
    candidate = None
    candidate_count = 0
    entered_at = None
    poem_triggered = False
    # Assume recently-seen on startup rather than "empty since epoch" - after
    # a restart we genuinely don't know the room's history, and guessing
    # "empty" would gate refreshes instantly. Consistent with the rest of the
    # presence gating, which fails open wherever it lacks information.
    last_seen = time.time()
    last_heartbeat = 0.0
    write_presence_state(present, last_seen)

    logger.info(f"Presence monitor started, watching GPIO{OUT_PIN}")

    while True:
        try:
            reading = bool(GPIO.input(OUT_PIN))

            if reading == present:
                candidate = None
                candidate_count = 0
            else:
                if reading == candidate:
                    candidate_count += 1
                else:
                    candidate = reading
                    candidate_count = 1

                if candidate_count >= CONFIRM_READS:
                    present = reading
                    candidate = None
                    candidate_count = 0

                    if present:
                        logger.info("Presence: ENTERED")
                        entered_at = time.time()
                        last_seen = time.time()
                        poem_triggered = False
                    else:
                        logger.info("Presence: LEFT")
                        entered_at = None
                        last_seen = time.time()
                        poem_triggered = False

                    write_presence_state(present, last_seen)
                    if present:
                        threading.Thread(target=wake_refresh, daemon=True).start()

                    client.publish(STATE_TOPIC, "ON" if present else "OFF", retain=True)
                    threading.Thread(
                        target=led_presence_sequence, args=(present,), daemon=True
                    ).start()

            # Lingering, not just passing through - one poem per visit.
            if present and not poem_triggered and entered_at is not None:
                if time.time() - entered_at >= POEM_DWELL_SECONDS:
                    poem_triggered = True
                    threading.Thread(target=trigger_poem, daemon=True).start()

            now = time.time()
            if present:
                last_seen = now
            if now - last_heartbeat >= PRESENCE_HEARTBEAT_SECONDS:
                last_heartbeat = now
                write_presence_state(present, last_seen)

        except Exception as e:
            logger.error(f"Poll error: {e}")

        time.sleep(1.0 / POLL_HZ)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.cleanup()
