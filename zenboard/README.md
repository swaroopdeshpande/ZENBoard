# ZenBoard system services

These run **outside** the InkyPi Flask app, as systemd services on the Pi.
They are vendored here because they otherwise exist only at `/usr/local/bin/`
on the device — an SD card failure would lose them.

| Script | Service | GPIO | Purpose |
|---|---|---|---|
| `zenboard_led.py` | `zenboard_led.service` | 13 | WS2812B strip (22 LEDs). Watches `/tmp/led_config.json`, renders effects at 20fps. |
| `zenboard_led_mqtt.py` | `zenboard_led_mqtt.service` | — | Bridges the strip to Home Assistant as an MQTT JSON light. |
| `zenboard_presence.py` | `zenboard_presence.service` | 4 | mmWave presence sensor. Drives LED reactions, publishes MQTT discovery, triggers the presence poem on dwell, and writes the state file that gates display refreshes. |
| `zenboard_wifi_monitor.py` | `zenboard_wifi_monitor.service` | — | On boot, falls back to a setup access point if no known WiFi is found. |

## Install

    sudo cp zenboard_*.py /usr/local/bin/
    sudo cp systemd/*.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now zenboard_led zenboard_led_mqtt zenboard_presence zenboard_wifi_monitor

## Setup-AP password

The fallback access-point password is **not** stored in this repo. Both
`zenboard_wifi_monitor.py` and the Flask WiFi-setup blueprint read it from
`/etc/zenboard/ap_password` (root-only, mode 0600), or from the
`ZENBOARD_AP_PASSWORD` environment variable.

    sudo mkdir -p /etc/zenboard
    printf 'your-password-here' | sudo tee /etc/zenboard/ap_password
    sudo chmod 600 /etc/zenboard/ap_password

If neither source is present the services still start, using the placeholder
`changeme-zenboard` — so if `ZenBoard-Setup` ever accepts that value, the
password file is missing.

## Notes

- The effect list for the LED strip is duplicated in three places and must be
  kept in sync: `zenboard_led.py`, `EFFECT_LIST` in `zenboard_led_mqtt.py`
  (which feeds Home Assistant), and the `ledMode` dropdown in
  `src/templates/inky.html`.
- `zenboard_presence.py` writes `/tmp/zenboard_presence.json` and POSTs
  `/api/presence/changed` on entry. It writes the state file *before* posting;
  reversing that order makes the catch-up refresh skip itself.
