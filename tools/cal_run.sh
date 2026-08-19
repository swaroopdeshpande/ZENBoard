#!/bin/bash
# Show the calibration ruler. inkypi is stopped first so a scheduled refresh
# cannot repaint over it mid-read, then restored on exit.
restore() {
  echo; echo "  restarting inkypi..."
  systemctl start inkypi
}
trap restore EXIT INT TERM
echo "  stopping inkypi..."
systemctl stop inkypi
sleep 2
/usr/local/inkypi/venv_inkypi/bin/python /usr/local/bin/calibrate_ruler.py "$@"
echo
echo "  Read the LOWEST fully-visible number on each edge, then run:"
echo "    ssh zenboard 'sudo /usr/local/inkypi/venv_inkypi/bin/python /usr/local/bin/calibrate_ruler.py --apply TOP RIGHT BOTTOM LEFT'"
