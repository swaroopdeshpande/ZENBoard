#!/usr/bin/env python3
"""
ZenBoard WiFi Monitor
Runs on boot. Waits for WiFi connection.
If no known WiFi found within timeout, switches to AP mode
and signals InkyPi to show QR code.
"""

import json
import logging
import os
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("wifi_monitor")

WIFI_TIMEOUT   = 45
CHECK_INTERVAL = 3
AP_SSID        = "ZenBoard-Setup"
AP_IP          = "192.168.4.1"

# The setup-AP password is deliberately NOT hardcoded - this repo is public.
# Real value lives in /etc/zenboard/ap_password (root-only, 0600) on the
# device. Order: env override -> that file -> placeholder.
#
# The placeholder is a last-resort fallback so the WiFi *recovery* path still
# comes up on a fresh/incomplete install rather than crashing - if you ever
# see ZenBoard-Setup accepting it, the password file is missing.
AP_PASSWORD_FILE = "/etc/zenboard/ap_password"


def _load_ap_password():
    value = os.environ.get("ZENBOARD_AP_PASSWORD")
    if value:
        return value.strip()
    try:
        with open(AP_PASSWORD_FILE) as f:
            value = f.read().strip()
        if value:
            return value
        logger.warning(f"{AP_PASSWORD_FILE} is empty, using placeholder AP password")
    except FileNotFoundError:
        logger.warning(f"{AP_PASSWORD_FILE} not found, using placeholder AP password")
    except Exception as e:
        logger.warning(f"Could not read {AP_PASSWORD_FILE} ({e}), using placeholder")
    return "changeme-zenboard"


AP_PASSWORD = _load_ap_password()
STATUS_FILE    = "/tmp/zenboard_wifi_status.json"
INKYPI_URL     = "http://127.0.0.1"


def is_wifi_connected():
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "STATE", "general"],
            capture_output=True, text=True, timeout=5
        )
        return "connected" in result.stdout.lower()
    except Exception:
        return False


def get_current_ssid():
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1]
    except Exception:
        pass
    return None


def start_ap_mode():
    logger.info(f"Starting AP mode: {AP_SSID}")
    try:
        subprocess.run(["nmcli", "connection", "delete", "ZenBoard-AP"], capture_output=True)
        result = subprocess.run([
            "nmcli", "connection", "add",
            "type", "wifi", "ifname", "wlan0",
            "con-name", "ZenBoard-AP",
            "autoconnect", "no",
            "ssid", AP_SSID,
            "--",
            "wifi.mode", "ap",
            "wifi-sec.key-mgmt", "wpa-psk",
            "wifi-sec.psk", AP_PASSWORD,
            "ipv4.method", "shared",
            "ipv4.addresses", f"{AP_IP}/24",
        ], capture_output=True, text=True, timeout=15)

        if result.returncode != 0:
            logger.error(f"Failed to create AP: {result.stderr}")
            return False

        result = subprocess.run(
            ["nmcli", "connection", "up", "ZenBoard-AP"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            logger.error(f"Failed to start AP: {result.stderr}")
            return False

        logger.info("AP mode started")
        return True
    except Exception as e:
        logger.error(f"AP mode error: {e}")
        return False


def stop_ap_mode():
    logger.info("Stopping AP mode")
    try:
        subprocess.run(["nmcli", "connection", "down", "ZenBoard-AP"], capture_output=True, timeout=10)
        subprocess.run(["nmcli", "connection", "delete", "ZenBoard-AP"], capture_output=True, timeout=10)
        subprocess.run(["nmcli", "device", "connect", "wlan0"], capture_output=True, timeout=30)
    except Exception as e:
        logger.error(f"Stop AP error: {e}")


def write_status(mode, ssid=None, ap_ip=None, ap_ssid=None):
    status = {
        "mode": mode,
        "ssid": ssid,
        "ap_ip": ap_ip,
        "ap_ssid": ap_ssid,
        "ap_password": AP_PASSWORD if mode == "ap" else None,
        "timestamp": time.time(),
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f)
    logger.info(f"Status: {status}")


def signal_inkypi_qr():
    try:
        import requests
        requests.post(f"{INKYPI_URL}/api/wifi_setup/show_qr",
                     json={"ap_ssid": AP_SSID, "ap_ip": AP_IP}, timeout=5)
    except Exception as e:
        logger.warning(f"Could not signal InkyPi: {e}")


def main():
    logger.info("ZenBoard WiFi Monitor starting")
    write_status("searching")

    elapsed = 0
    while elapsed < WIFI_TIMEOUT:
        if is_wifi_connected():
            ssid = get_current_ssid()
            logger.info(f"Connected to WiFi: {ssid}")
            write_status("connected", ssid=ssid)
            sys.exit(0)
        logger.info(f"Waiting for WiFi... {elapsed}/{WIFI_TIMEOUT}s")
        time.sleep(CHECK_INTERVAL)
        elapsed += CHECK_INTERVAL

    logger.warning("No WiFi found, switching to AP mode")
    success = start_ap_mode()

    if success:
        write_status("ap", ap_ssid=AP_SSID, ap_ip=AP_IP)
        time.sleep(3)
        signal_inkypi_qr()
        logger.info("AP mode active, waiting for user config")

        while True:
            try:
                with open(STATUS_FILE) as f:
                    status = json.load(f)
                if status.get("mode") == "connecting":
                    logger.info("New WiFi configured, stopping AP")
                    stop_ap_mode()
                    for _ in range(20):
                        time.sleep(3)
                        if is_wifi_connected():
                            ssid = get_current_ssid()
                            write_status("connected", ssid=ssid)
                            logger.info(f"Connected to {ssid}")
                            break
                    break
            except Exception:
                pass
            time.sleep(5)
    else:
        logger.error("Failed to start AP mode")
        write_status("error")


if __name__ == "__main__":
    main()
