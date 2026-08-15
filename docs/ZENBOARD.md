# ZenBoard — Custom InkyPi Build

This is a customized fork of [InkyPi](https://github.com/fatihak/InkyPi) running on a
Raspberry Pi Zero 2W, driving a Waveshare 7.5" tri-color (black/white/red) e-ink panel
(`epd7in5b_V2`) mounted in a wooden frame, with a WS2812B LED accent strip.

## Hardware

- **Board:** Raspberry Pi Zero 2W
- **Display:** Waveshare 7.5" epd7in5b_V2, BWR (black/white/red) tri-color e-ink,
  800×480 landscape native / 480×800 portrait
- **LEDs:** WS2812B strip mounted below the panel, driven via `board.D13` (GPIO13),
  controlled by `zenboard_led.service` (`/usr/local/bin/zenboard_led.py`)
- **Mount:** physical frame is mounted rotated 180° from the panel's native orientation —
  compensated in software via `inverted_image: true` in `device.json` (see Calibration).

## Display Calibration

`device.json → display_margins` holds calibrated safe-area margins per orientation,
measured empirically with the ring/grid calibration scripts (`~/calibrate_*.py`) against
the physical bezel of this specific frame.

Current horizontal (landscape) margins: `T=0, B=10, L=8, R=11` (usable 781×470).
Vertical (portrait) margins are the pre-180°-flip values and **have not been
recalibrated** since the mount was rotated — treat portrait output as unverified.

**All plugins must read margins via `BasePlugin.get_safe_area(device_config)`** and pass
the returned `usable_width`/`usable_height` as explicit inline pixel styles
(`style="width:{{frame_w}}px;height:{{frame_h}}px;"`) on the root content div. Do not
rely on percentage/flex-chain sizing — that was the root cause of repeated sizing bugs
this project hit early on (stock tracker, weather).

### `image_settings` (global post-render enhancement)

`device.json → image_settings` (brightness/contrast/saturation/sharpness) is applied to
**every** plugin's output before it hits the panel. It is currently locked at neutral
`1.0/1.0/1.0/1.0`. It was previously `2.0/2.0/2.0/1.0`, which washed out anti-aliased
text edges and thin borders (pushed them toward white before BWR quantization) —
don't raise these again without re-testing text-heavy plugins.

### `stem_darken()` (text-crispening)

`src/utils/image_utils.py::stem_darken(img, threshold=200)` — a real technique
("stem darkening", used by e-reader font engines) adapted as a post-render curve: any
pixel below the threshold snaps to pure black, compensating for anti-aliased glyph edges
that e-ink quantization otherwise lightens/loses. Applied to plugins that are pure
UI/text (`system_health`, `stock_tracker`, `apple_calendar`, `ai_text`). **Do not apply
it to photo-heavy plugins** (`ai_image`, `tv_quotes`, `spotify_now_playing`,
`unsplash`, `image_folder`) — it will crush intentional grayscale/gradient content to
black. Also skipped on `weather_terminal`, which has an intentional opacity-based label
that needs individual handling before this is safe there.

**Never use low-contrast grays** (mid-tone hex like `#7a7a7a`) for design elements on
this panel — they dither/ghost badly under BWR quantization even though they look fine
in a pre-quantization PNG preview. Use pure black/white/red, and for "dimmed" UI states
use font-weight/size instead of opacity/gray.

## Custom Plugins

| Plugin | What it does |
|---|---|
| `apple_calendar` | iOS-style month grid. Imports any number of ICS/iCal feed URLs (Google, iCloud, Outlook — no OAuth). Red/black theme toggle. Today = filled red circle. |
| `system_health` | Pi vitals dashboard: CPU/temp/mem/disk, per-core bars, throttle/undervoltage status, network, daily refresh count, service health. |
| `stock_tracker` | Ticker + inline SVG sparkline chart, Yahoo Finance (free, no key), light/dark mode. |
| `weather_terminal` | Dark-mode-only weather dashboard, Open-Meteo (free), Karnataka-focused searchable locations, SVG hourly graph. |
| `tv_quotes` | Random TBBT (The Big Bang Theory) quote + character portrait, TBBT logo watermark, shuffle-bag no-repeat selection. |
| `spotify_now_playing` | Reads Now Playing from local Mac Spotify.app via AppleScript, pushed to the Pi over `/spotify/push` (works around Spotify API's Premium-only `/me/player` requirement). |
| `wifi_qr` | Shown automatically when the Pi can't find a known WiFi network — welcome banner, big QR to a captive setup page, IP/credentials. Paired with `zenboard_wifi_monitor.service` (AP fallback) and `wifi_setup_bp` (Flask routes for scan/connect). |
| `ai_image` | Free image generation via Hugging Face Inference Providers (fal-ai) + Pollinations.ai fallback — migrated off paid OpenAI. |
| `ai_text` | Free text generation via OpenRouter (`openai/gpt-oss-20b:free`) — migrated off paid OpenAI. |

## Daily Refresh Counter

`src/utils/refresh_stats.py` tracks full-refresh count, date-reset at midnight (not
cron-dependent, robust to the device being off at midnight). Hooked into
`display_manager.py`'s full-refresh path only (partial/fast refreshes don't count).
Shown as a persistent badge in the web UI header (`templates/inky.html`), polled every
30s via `/api/refresh_count`. Context: the panel has a rated ~100,000 full-refresh
lifetime; this makes usage visible.

## WiFi Fallback / AP Setup

`zenboard_wifi_monitor.service` (`/usr/local/bin/zenboard_wifi_monitor.py`) runs at
boot: waits up to 45s for a known WiFi connection. If none found, spins up an
`nmcli`-managed AP (`ZenBoard-Setup`, password read from `/etc/zenboard/ap_password`, `192.168.4.1`), signals InkyPi via
`POST /api/wifi_setup/show_qr` (which now also triggers an actual display refresh — this
was a real bug, the route used to only write a status file and never pushed to the
panel), and serves a captive setup page (`wifi_setup_bp` in `blueprints/main.py`) where
you pick a network and password. On success the Pi reconnects and the monitor exits.
Bringing the known connection `down` (not deleting it) to test this is safe — autoconnect
brings it back on reboot regardless of AP-mode testing.

## Known Gotchas

- **New/changed plugin files require `sudo systemctl restart inkypi`** — plugin
  discovery and Python module imports both happen once at process startup, not
  read live from disk.
- **`device.json` config values also require a restart** — `Config()` loads the file
  once at process init and caches it in memory for the process lifetime.
- **Manual `/update_now` pushes dedupe by content hash** — if you change only
  `image_settings` (enhancement, not plugin output), the hash is identical and the
  push silently no-ops ("Image already displayed, skipping refresh"). To force a real
  hardware write for testing enhancement-only changes: stop `inkypi.service`, call
  `DisplayManager(device_config).display_image(image)` directly in a one-off script
  (bypasses `refresh_task`'s dedup), then restart the service.
- **GPIO conflicts**: any direct hardware-access test script (calibration scripts,
  force-push scripts) must run with `inkypi.service` stopped first — the display GPIO
  is exclusively held by whichever process initializes it.
- **`/tmp` test/calibration scripts run as the deploying user, but plugin/config files
  are root-owned** — writes to `src/plugins/*/` or `src/config/device.json` need `sudo`.
