#!/bin/bash
# Stops the sensor service (it owns the serial port), runs the calibration,
# restores the service on exit however the script ends.
restore() { echo; echo '  restarting zenboard_sensor...'; systemctl start zenboard_sensor; }
trap restore EXIT INT TERM
systemctl stop zenboard_sensor
sleep 1
python3 /usr/local/bin/calibrate_presence.py
