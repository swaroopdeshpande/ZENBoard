#!/bin/bash
# Verify in small batches with pauses.
#
# A single unbroken sweep of 78 renders took the board down hard enough to
# reboot it: one headless Chromium render costs ~80MB against ~145MB of
# headroom, and back-to-back renders never let the memory come back. Three at a
# time with a pause between keeps it inside its budget.
OUT=/home/zenith/verify_report.txt
mkdir -p /home/zenith/verify
: > "$OUT"
BATCHES=(
  "newspaper oil_price_tracker presence_poem"
  "rss space_overview spotify_now_playing"
  "stock_tracker system_health todo_list"
  "tv_quotes unsplash weather"
  "weather_terminal wifi_qr wpotd"
  "year_progress screenshot"
)
for b in "${BATCHES[@]}"; do
  echo "  --- batch: $b" | tee -a "$OUT"
  timeout 600 /usr/local/inkypi/venv_inkypi/bin/python /usr/local/bin/verify_plugins.py $b 2>&1 \
    | grep -E '^[a-z_]+ +(horizontal|vertical)' | tee -a "$OUT"
  free -m | awk '/^Mem:/{printf "      mem avail after batch: %sMB\n", $7}' | tee -a "$OUT"
  sleep 20
done
echo "  ALL BATCHES DONE" | tee -a "$OUT"
