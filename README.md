# ZENBoard
A customized Raspberry Pi e-ink dashboard with a modern web UI, 6 color themes, real-time Amazon order tracking via Gmail, AI Kannada quotes, oil prices, WiFi management, and presence-aware LED lighting. Built on InkyPi, designed to sit on your desk and show everything you need at a glance.


# ZenBoard

> A customized Raspberry Pi e-ink dashboard with a modern web UI, 6 color themes, real-time Amazon order tracking via Gmail, AI Kannada quotes, oil prices, WiFi management, and presence-aware LED lighting. Built on InkyPi, designed to sit on your desk and show everything you need at a glance.

![Display](docs/display.png)

---

## Features

- **Modern Web UI** — 6 color themes (Light, Dark, Ocean, Forest, Sunset, Nord), rounded design, mobile friendly
- **Amazon Order Tracker** — Gmail OAuth2 integration, real product images, live status via email parsing
- **Kannada Quotes** — AI-generated quotes using Sarvam AI's `sarvam-105b` model
- **Oil Price Tracker** — Live Brent, WTI, and Natural Gas prices
- **WiFi Manager** — Scan and switch networks directly from the browser
- **Configurable Welcome Message** — Personalize your dashboard header
- **Presence-aware LEDs** — WS2812B integration with mmWave sensor (warm glow on enter, red flash on leave)
- **E-ink Display** — Waveshare 7.5" BWR (800×480) on Raspberry Pi Zero 2W

---

## Hardware

| Component | Details |
|---|---|
| Raspberry Pi | Zero 2W |
| Display | Waveshare 7.5" BWR V2 (epd7in5b_V2) |
| LEDs | WS2812B × 16 |
| Presence sensor | mmWave (LD2410 or similar) |
| LED power | External 5V 3A supply (shared GND with Pi) |

---

## Custom Plugins

### Amazon Order Tracker
Connects to Gmail via OAuth2, parses Amazon India order emails, extracts product images, and shows live delivery status on the display. Supports multi-order display, cancellation detection, and today-only filtering.

**Setup:**
1. Create a Google Cloud project and enable Gmail API
2. Run `auth_gmail.py` on your desktop to generate `gmail_token.json`
3. Paste the token JSON into the plugin settings

### Kannada Quotes
Generates a fresh Kannada philosophical quote on every refresh using Sarvam AI. Displays in authentic Kannada script with a temple-inspired border design.

**Requires:** Sarvam AI API key

### Oil Price Tracker
Fetches live crude oil prices from OilPriceAPI. Supports Brent Crude, WTI, and Natural Gas with a multi-panel display option.

**Requires:** OilPriceAPI key

---

## Installation

```bash
# Clone the repo
git clone https://github.com/YOUR_USERNAME/zenboard.git
cd zenboard

# Install dependencies
pip install -r requirements.txt

# Install plugin-specific dependencies
pip install --break-system-packages sarvamai google-auth-oauthlib google-api-python-client

# Run
python src/inkypi.py
```

Access the web UI at `http://<your-pi-ip>`

---

## Web UI

Open the dashboard in any browser on your local network. Use the ⚙️ button to:
- Change the welcome message
- Switch color themes
- Connect to a new WiFi network
- Manage plugins and playlists

---

## GPIO Pin Allocation

| GPIO | Use |
|---|---|
| SPI0 (19,21,23,24) | Waveshare display (hat) |
| GPIO17, 25, 24 | Display RST/DC/CS (hat) |
| GPIO18 (PWM0) | WS2812B LED data |
| GPIO14/15 (UART) | mmWave sensor TX/RX |

---

## Credits

Built on top of [InkyPi](https://github.com/fatihak/InkyPi) by fatihak.  
Custom plugins, UI redesign, and hardware integrations by [Swaroop B Deshpande](https://github.com/YOUR_USERNAME).

---

## License

MIT
