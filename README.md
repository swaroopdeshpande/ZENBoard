# ZenBoard

A personal e-ink dashboard, built on top of [InkyPi](https://github.com/fatihak/InkyPi),
running on a Raspberry Pi Zero 2W with a Waveshare 7.5" tri-color (black/white/red)
e-ink panel in a wooden frame, plus a WS2812B LED accent strip.

Full build documentation — hardware, display calibration, every custom plugin, and the
gotchas we hit along the way — lives in **[docs/ZENBOARD.md](./docs/ZENBOARD.md)**.

## What it shows

Custom plugins built for this board, cycling on a schedule:

- **Apple Calendar** — iOS-style month grid, imports any ICS/iCal feed (Google, iCloud,
  Outlook), red/black theme
- **System Health** — Pi vitals: CPU/temp/memory/disk, per-core load, power/throttle
  status, network, daily e-ink refresh count
- **Stock Tracker** — ticker + sparkline chart, free Yahoo Finance data, light/dark mode
- **Weather** — dark-mode dashboard, free Open-Meteo data, Karnataka-focused searchable
  locations, hourly graph
- **TV Quotes** — random quotes with character art
- **Spotify Now Playing** — mirrors what's playing from a local Mac, since Spotify's
  official API gates the live-playback endpoint behind Premium
- **WiFi Setup** — shown automatically if the Pi can't find a known network: QR code to
  a captive setup portal, no monitor/keyboard needed
- **AI Image / AI Text** — free-tier generation (Hugging Face + Pollinations.ai,
  OpenRouter), no paid API required

## Hardware

- Raspberry Pi Zero 2W
- Waveshare 7.5" epd7in5b_V2 (black/white/red e-ink), 800×480
- WS2812B LED strip, driven off GPIO13
- Wooden picture frame

## Setup

This is a fork of InkyPi, so the underlying install process is the same:

```bash
git clone https://github.com/swaroopdeshpande/ZENBoard.git
cd ZENBoard
sudo bash install/install.sh -W epd7in5b_V2
```

See [installation.md](./docs/installation.md) for the general InkyPi install walkthrough,
and [ZENBOARD.md](./docs/ZENBOARD.md) for the calibration and configuration specific to
this build (display margins, `image_settings`, the plugins above, etc.) — you'll want to
recalibrate the display margins for your own frame rather than reuse the values checked
in here.

## Credits

Built on [InkyPi](https://github.com/fatihak/InkyPi) by [fatihak](https://github.com/fatihak)
— an open-source, plugin-based e-ink dashboard framework. See
[building_plugins.md](./docs/building_plugins.md) if you want to write your own plugin,
and the original project for the general plugin ecosystem, playlist scheduling, and
supported display list.

## License

Distributed under the GPL 3.0 License, see [LICENSE](./LICENSE) for more information.
This project includes fonts and icons with separate licensing and attribution
requirements — see [Attribution](./docs/attribution.md) for details.
