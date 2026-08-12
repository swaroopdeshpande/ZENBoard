"""
Stock Tracker - ZENBoard
Waveshare 7.5" BWR, 800x480 landscape / 480x800 portrait.

Rebuilt lightweight: no yfinance/pandas/matplotlib/numpy (those pulled
~100MB+ of dependencies and were part of why this Pi Zero 2W kept running
out of memory). Data comes from Yahoo Finance's public chart endpoint
(no API key, no auth) via plain `requests`. The trend chart is a hand-built
inline SVG polyline - no image encoding/decoding, no extra libraries.

Sizing follows the calibrated safe-area convention (get_safe_area) used by
the other working plugins here, rather than relying on percentage-height
flex chains, which is what made the previous version render at the wrong
size.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests

from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_utils import stem_darken

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
CACHE_DIR = PLUGIN_DIR / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HTTP_TIMEOUT = 10

PERIOD_LABELS = {
    "1d": "1D", "5d": "1W", "1mo": "1M", "3mo": "3M",
    "6mo": "6M", "1y": "1Y", "5y": "5Y", "max": "MAX",
}

PERIOD_INTERVAL = {
    "1d": "5m", "5d": "30m", "1mo": "1d", "3mo": "1d",
    "6mo": "1d", "1y": "1wk", "5y": "1wk", "max": "1mo",
}


class StockTracker(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== Stock Tracker: Starting ===")

        ticker = (settings.get("ticker") or "AAPL").strip().upper()
        period = settings.get("period", "1mo")
        cache_ttl = int(settings.get("cacheTtl", "300"))
        dark = settings.get("displayMode", "light") == "dark"

        data = self._get_data(ticker, period, cache_ttl)
        if not data:
            raise RuntimeError(f"Could not fetch data for {ticker}")

        dimensions = device_config.get_resolution()
        is_landscape = dimensions[0] > dimensions[1]
        if device_config.get_config("orientation") == "vertical":
            is_landscape = False

        safe = self.get_safe_area(device_config)
        chart_w = safe["usable_width"] - (350 if is_landscape else 32)
        chart_h = (safe["usable_height"] - 210) if is_landscape else 200
        chart_svg = self._build_sparkline(
            data["closes"], chart_w, max(chart_h, 60), dark
        )

        template = "stock_landscape.html" if is_landscape else "stock_portrait.html"

        image = self.render_image(
            dimensions,
            template,
            "stock_tracker.css",
            {
                **data,
                "period_label": PERIOD_LABELS.get(period, period.upper()),
                "chart_svg": chart_svg,
                "dark": dark,
                # Explicit pixel dims from the calibrated safe area - not
                # percentage/flex sizing, which is what made the previous
                # version render at the wrong size.
                "frame_w": safe["usable_width"],
                "frame_h": safe["usable_height"],
                "plugin_settings": {
                    "topMargin": safe.get("top", 12),
                    "bottomMargin": safe.get("bottom", 0),
                    "leftMargin": safe.get("left", 8),
                    "rightMargin": safe.get("right", 11),
                    "backgroundOption": "color",
                    "backgroundColor": "#000000" if dark else "#ffffff",
                    "textColor": "#ffffff" if dark else "#000000",
                    "selectedFrame": "None",
                },
            },
        )

        if not image:
            raise RuntimeError("Failed to render Stock Tracker image")

        logger.info(f"=== Stock Tracker: Complete ({ticker}) ===")
        image = stem_darken(image)
        return image

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _get_data(self, ticker, period, cache_ttl):
        cache_file = CACHE_DIR / f"{ticker}_{period}.json"

        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                if time.time() - cached.get("_ts", 0) < cache_ttl:
                    logger.info(f"Using cached data for {ticker}")
                    return cached["data"]
            except Exception as e:
                logger.debug(f"Cache read failed: {e}")

        try:
            data = self._fetch(ticker, period)
            cache_file.write_text(json.dumps({"_ts": time.time(), "data": data}))
            return data
        except Exception as e:
            logger.error(f"Fetch failed for {ticker}: {e}")
            if cache_file.exists():
                try:
                    return json.loads(cache_file.read_text())["data"]
                except Exception:
                    pass
            return None

    def _fetch(self, ticker, period):
        interval = PERIOD_INTERVAL.get(period, "1d")
        resp = requests.get(
            CHART_URL.format(ticker=ticker),
            params={"interval": interval, "range": period},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()

        result = (payload.get("chart") or {}).get("result")
        if not result:
            err = (payload.get("chart") or {}).get("error")
            raise RuntimeError(f"Yahoo Finance error: {err}")

        r = result[0]
        meta = r["meta"]

        closes_raw = r.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        closes = [c for c in closes_raw if c is not None]
        if not closes:
            closes = [meta.get("regularMarketPrice", 0)]

        price = meta.get("regularMarketPrice", closes[-1])
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose") or closes[0]
        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0

        market_status = self._market_status(meta)

        return {
            "ticker": meta.get("symbol", ticker),
            "company_name": meta.get("longName") or meta.get("shortName") or ticker,
            "exchange": meta.get("fullExchangeName") or meta.get("exchangeName", ""),
            "currency": meta.get("currency", "USD"),
            "current_price": self._fmt_price(price),
            "change": change,
            "change_positive": change >= 0,
            "change_str": f"{abs(change):.2f} ({abs(change_pct):.2f}%)",
            "day_high": self._fmt_price(meta.get("regularMarketDayHigh")),
            "day_low": self._fmt_price(meta.get("regularMarketDayLow")),
            "prev_close": self._fmt_price(prev_close),
            "week52_high": self._fmt_price(meta.get("fiftyTwoWeekHigh")),
            "week52_low": self._fmt_price(meta.get("fiftyTwoWeekLow")),
            "volume": self._fmt_volume(meta.get("regularMarketVolume")),
            "market_status": market_status,
            "closes": closes[-120:],  # cap point count - keeps SVG light
        }

    @staticmethod
    def _market_status(meta):
        period = meta.get("currentTradingPeriod", {}).get("regular", {})
        now = time.time()
        start, end = period.get("start"), period.get("end")
        if start and end and start <= now <= end:
            return "MARKET OPEN"
        return "MARKET CLOSED"

    @staticmethod
    def _fmt_price(v):
        if v is None:
            return "--"
        return f"{v:,.2f}"

    @staticmethod
    def _fmt_volume(v):
        if not v:
            return "--"
        if v >= 1_000_000_000:
            return f"{v / 1_000_000_000:.2f}B"
        if v >= 1_000_000:
            return f"{v / 1_000_000:.2f}M"
        if v >= 1_000:
            return f"{v / 1_000:.1f}K"
        return str(v)

    # ------------------------------------------------------------------
    # Chart (inline SVG - no image libraries, tiny memory footprint)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_sparkline(closes, width, height, dark):
        if not closes or len(closes) < 2:
            return ""

        lo, hi = min(closes), max(closes)
        span = (hi - lo) or 1

        pad = 4
        w, h = width, height
        step = (w - 2 * pad) / (len(closes) - 1)

        pts = []
        for i, c in enumerate(closes):
            x = pad + i * step
            y = pad + (h - 2 * pad) * (1 - (c - lo) / span)
            pts.append((x, y))

        line_color = "#ff2020" if dark else "#000000"
        fill_color = "rgba(255,255,255,0.08)" if dark else "rgba(0,0,0,0.06)"

        line_pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        area_pts = f"{pad:.1f},{h - pad:.1f} " + line_pts + f" {w - pad:.1f},{h - pad:.1f}"

        last_x, last_y = pts[-1]
        dot_color = "#d40000"

        return (
            f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<polygon points="{area_pts}" fill="{fill_color}" />'
            f'<polyline points="{line_pts}" fill="none" stroke="{line_color}" '
            f'stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" />'
            f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4.2" fill="{dot_color}" />'
            f'</svg>'
        )
