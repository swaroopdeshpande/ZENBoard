# InkyPi Oil Price Tracker

Displays the latest oil price (Brent, WTI, or Natural Gas) from [oilpriceapi.com](https://oilpriceapi.com) on your InkyPi e-ink display.

Built and styled for the Waveshare 7.5" 3-color (Black/White/Red) display.

## Install

1. Copy the `oil_price_tracker` folder into `src/plugins/` on your InkyPi (or unzip this archive there so the result is `src/plugins/oil_price_tracker/`).
2. Restart InkyPi:
   ```bash
   sudo systemctl restart inkypi.service
   ```
3. In the InkyPi web UI, add the "Oil Price Tracker" plugin to a playlist.
4. Paste your API key into the "Oil Price API Key" field, pick an oil type from the dropdown, and save.

## Getting an API key

Sign up at [oilpriceapi.com](https://oilpriceapi.com) — free tier available. Copy your API token and paste it into the plugin settings field (no need to type "Token", just the key itself).

## Notes

- API docs: https://docs.oilpriceapi.com
- The key is stored per plugin-instance in InkyPi's settings. If you'd rather keep it out of `device.json`, add `OIL_PRICE_API_KEY=<your key>` to InkyPi's `.env` file instead and leave the field blank.
- Status: actively maintained by user.
