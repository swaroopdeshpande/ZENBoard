#!/bin/bash
# Stop everything that drives the strip, walk it, then restore.
#
# The trap matters: if the SSH session drops or the walk is interrupted, the
# services would otherwise stay stopped and the frame would lose presence
# detection and LED control silently.

SERVICES="zenboard_sensor zenboard_led_mqtt zenboard_led"

restore() {
  echo
  echo "  restoring services..."
  for s in zenboard_led zenboard_led_mqtt zenboard_sensor; do
    systemctl start "$s" 2>/dev/null
  done
  sleep 2
  for s in zenboard_led zenboard_led_mqtt zenboard_sensor; do
    printf '    %-20s %s\n' "$s" "$(systemctl is-active $s)"
  done
}
trap restore EXIT INT TERM

echo "  stopping: $SERVICES"
systemctl stop $SERVICES
sleep 1

/usr/local/inkypi/venv_inkypi/bin/python3 /usr/local/bin/led_count.py "$@"
