#!/usr/bin/env python3
"""
ZenBoard LED <-> MQTT bridge - exposes the LED strip to Home Assistant as a
native Light entity (on/off, brightness, RGB color, effect dropdown), via
HA's MQTT Light JSON schema. Doesn't touch zenboard_led.py's render loop at
all - just translates HA commands into the same led_config.json file that
script already watches, and watches that file right back for changes made
elsewhere (the web UI, or a presence-effect settling back to baseline) so
HA's light card stays in sync no matter who changed it.

Effect list is our existing LED modes, minus "off" (that's the entity's
on/off state, not an effect) and "refresh_flash" (internal-only, triggered
by display refreshes, not something to expose as a user-selectable effect).
"""

import json
import logging
import time

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("led_mqtt")

RUNTIME_FILE = "/tmp/led_config.json"
PERSIST_FILE = "/home/zenith/InkyPi/src/config/led_config.json"

MQTT_HOST = "localhost"
MQTT_PORT = 1883
BASE_TOPIC = "zenboard/led"
COMMAND_TOPIC = f"{BASE_TOPIC}/set"
STATE_TOPIC = f"{BASE_TOPIC}/state"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/availability"
DISCOVERY_TOPIC = "homeassistant/light/zenboard_led/config"

EFFECT_LIST = [
    "static", "breathe", "warm_glow", "rainbow", "chase",
    # smooth/ambient set, modelled on the WLED effects of the same names
    "pacifica", "aurora", "sunrise", "candle", "sinelon", "plasma",
    "lake", "twinklefox",
]

DEFAULT_CONFIG = {
    "mode": "warm_glow", "color": "#FF6B35", "brightness": 128,
    "breathe_speed": "medium", "refresh_flash": True,
    "presence_enabled": True, "presence_color_on": "#FF8C42",
    "presence_color_off": "#001133", "enabled": True,
}


def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(r, g, b):
    return "#{:02X}{:02X}{:02X}".format(r, g, b)


def load_config(path):
    try:
        with open(path) as f:
            cfg = DEFAULT_CONFIG.copy()
            cfg.update(json.load(f))
            return cfg
    except Exception:
        return DEFAULT_CONFIG.copy()


def write_config(cfg):
    """Mirrors main.py's _save_led_config - both files together, matching
    what a change via the web UI does, so a command from HA behaves
    identically to one from ZenBoard's own settings page."""
    with open(RUNTIME_FILE, "w") as f:
        json.dump(cfg, f)
    with open(PERSIST_FILE, "w") as f:
        json.dump(cfg, f)


def config_to_ha_state(cfg):
    r, g, b = hex_to_rgb(cfg.get("color", "#FF6B35"))
    mode = cfg.get("mode", "warm_glow")
    return {
        "state": "ON" if cfg.get("enabled", True) and mode != "off" else "OFF",
        "brightness": int(cfg.get("brightness", 128)),
        "color": {"r": r, "g": g, "b": b},
        "effect": mode if mode in EFFECT_LIST else "static",
    }


def make_client():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="zenboard-led-bridge")
    client.will_set(AVAILABILITY_TOPIC, payload="offline", retain=True)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        logger.info(f"MQTT connected: {reason_code}")
        client.publish(AVAILABILITY_TOPIC, "online", retain=True)

        discovery_payload = {
            "name": "LED Strip",
            "unique_id": "zenboard_led",
            "schema": "json",
            "command_topic": COMMAND_TOPIC,
            "state_topic": STATE_TOPIC,
            "availability_topic": AVAILABILITY_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
            "brightness": True,
            "color_mode": True,
            "supported_color_modes": ["rgb"],
            "effect": True,
            "effect_list": EFFECT_LIST,
            "device": {
                "identifiers": ["zenboard"],
                "name": "ZenBoard",
                "manufacturer": "DIY",
                "model": "InkyPi + WS2812B",
            },
        }
        client.publish(DISCOVERY_TOPIC, json.dumps(discovery_payload), retain=True)
        client.subscribe(COMMAND_TOPIC)

        # publish current state immediately so the card isn't blank on load
        client.publish(STATE_TOPIC, json.dumps(config_to_ha_state(load_config(PERSIST_FILE))), retain=True)

    def on_message(client, userdata, msg):
        try:
            cmd = json.loads(msg.payload.decode())
        except Exception as e:
            logger.error(f"Bad command payload: {e}")
            return

        cfg = load_config(PERSIST_FILE)
        logger.info(f"HA command: {cmd}")

        if "state" in cmd:
            if cmd["state"] == "OFF":
                cfg["enabled"] = False
            else:
                cfg["enabled"] = True
                if cfg.get("mode") == "off":
                    cfg["mode"] = "static"

        if "brightness" in cmd:
            cfg["brightness"] = int(cmd["brightness"])

        if "color" in cmd and cmd["color"]:
            cfg["color"] = rgb_to_hex(cmd["color"]["r"], cmd["color"]["g"], cmd["color"]["b"])

        if "effect" in cmd and cmd["effect"] in EFFECT_LIST:
            cfg["mode"] = cmd["effect"]

        write_config(cfg)
        client.publish(STATE_TOPIC, json.dumps(config_to_ha_state(cfg)), retain=True)

    client.on_connect = on_connect
    client.on_message = on_message
    return client


def main():
    client = make_client()
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()

    logger.info("LED MQTT bridge started")

    last_mtime = 0
    while True:
        try:
            import os
            mtime = os.path.getmtime(PERSIST_FILE)
            if mtime != last_mtime:
                last_mtime = mtime
                cfg = load_config(PERSIST_FILE)
                client.publish(STATE_TOPIC, json.dumps(config_to_ha_state(cfg)), retain=True)
                logger.info(f"Config changed externally, republished state: mode={cfg.get('mode')}")
        except Exception as e:
            logger.error(f"Watch error: {e}")

        time.sleep(2)


if __name__ == "__main__":
    main()
