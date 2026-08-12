#!/usr/bin/env python3
"""
ZenBoard E-Reader GPIO Button Service
Runs as a systemd service, listens for GPIO button presses
and sends page turn commands to InkyPi API.

Buttons:
  GPIO27 → Previous page
  GPIO22 → Next page
  GPIO23 → (reserved for future use / home)

Install:
  sudo cp ereader_gpio.py /usr/local/bin/zenboard_gpio.py
  sudo cp zenboard_gpio.service /etc/systemd/system/
  sudo systemctl enable zenboard_gpio
  sudo systemctl start zenboard_gpio
"""

import time
import requests
import logging
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ereader_gpio")

INKYPI_URL = "http://localhost"
BTN_PREV = 27
BTN_NEXT = 22
BTN_HOME = 23
DEBOUNCE_MS = 300

def get_active_ereader_instance():
    """Find the active e-reader plugin instance name."""
    try:
        r = requests.get(f"{INKYPI_URL}/api/config", timeout=5)
        config = r.json()
        playlist_cfg = config.get("playlist_config", {})
        active = playlist_cfg.get("active_playlist")
        playlists = playlist_cfg.get("playlists", [])
        for pl in playlists:
            if pl["name"] == active:
                for plugin in pl.get("plugins", []):
                    if plugin.get("plugin_id") == "ereader":
                        return plugin.get("instance_name")
    except Exception as e:
        logger.warning(f"Could not get active instance: {e}")
    return None

def send_page_action(action):
    """Send next/prev page action to InkyPi."""
    try:
        instance = get_active_ereader_instance()
        if not instance:
            logger.info("No active ereader instance found")
            return

        data = {
            "plugin_id": "ereader",
            "instance_name": instance,
            "settings": {"action": action}
        }
        r = requests.post(f"{INKYPI_URL}/update_now", json=data, timeout=10)
        logger.info(f"Page {action}: {r.status_code}")
    except Exception as e:
        logger.error(f"Failed to send page action: {e}")

def main():
    try:
        import RPi.GPIO as GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(BTN_PREV, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(BTN_NEXT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(BTN_HOME, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        last_press = {BTN_PREV: 0, BTN_NEXT: 0, BTN_HOME: 0}

        def on_prev(channel):
            now = time.time() * 1000
            if now - last_press[channel] > DEBOUNCE_MS:
                last_press[channel] = now
                logger.info("PREV button pressed")
                send_page_action("prev")

        def on_next(channel):
            now = time.time() * 1000
            if now - last_press[channel] > DEBOUNCE_MS:
                last_press[channel] = now
                logger.info("NEXT button pressed")
                send_page_action("next")

        def on_home(channel):
            now = time.time() * 1000
            if now - last_press[channel] > DEBOUNCE_MS:
                last_press[channel] = now
                logger.info("HOME button pressed")

        GPIO.add_event_detect(BTN_PREV, GPIO.FALLING, callback=on_prev, bouncetime=DEBOUNCE_MS)
        GPIO.add_event_detect(BTN_NEXT, GPIO.FALLING, callback=on_next, bouncetime=DEBOUNCE_MS)
        GPIO.add_event_detect(BTN_HOME, GPIO.FALLING, callback=on_home, bouncetime=DEBOUNCE_MS)

        logger.info("ZenBoard GPIO service started. Listening for button presses...")
        while True:
            time.sleep(0.1)

    except ImportError:
        logger.error("RPi.GPIO not available. Running in test mode.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            GPIO.cleanup()
        except Exception:
            pass

if __name__ == "__main__":
    main()
