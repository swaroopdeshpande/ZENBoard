# InkyPi Kannada Quotes

Displays beautiful Kannada quotes from famous poets and authors using Sarvam AI on your InkyPi e-ink display.

Built for Waveshare 7.5" 3-color (BWR) display with ancient temple-inspired design.

## Install

1. Copy `kannada_quotes` folder into `src/plugins/` on your InkyPi.
2. Restart InkyPi:
   ```bash
   sudo systemctl restart inkypi.service
   ```
3. In the InkyPi web UI, add "Kannada Quotes" plugin to a playlist.
4. Enter your Sarvam AI API key and a poet/author name (e.g., Basavanna, Pampa, Ranna).
5. Save and refresh.

## Requirements

- `sarvamai` Python package must be installed (you already did this).
- Sarvam AI API key from [sarvam.ai](https://sarvam.ai)

## Design

- Temple-style decorative top/bottom borders (repeating pattern inspired by Indian architecture).
- Vertical accent bars on left/right.
- Red diamond divider and red ornamental accents.
- Kannada text rendered natively (requires Noto Sans Kannada font).

## Notes

- The plugin fetches a new quote every refresh.
- Quotes are returned in Kannada script from the LLM.
- Author/poet name is also in Kannada.
- Stores API key in plugin settings (or use `SARVAM_API_KEY` env var).
