"""
Weather - ZENBoard
Rebuilt from scratch: dark-mode-only, graphical (no ASCII), matching a
dark weather-app reference design. Waveshare 7.5" BWR, 800x480 landscape
/ 480x800 portrait.

Design notes
------------
* Data: OpenWeatherMap free tier (current weather + 5-day/3-hour forecast +
  geocoding search). Same `apiKey` settings field as the old plugin, so an
  already-saved key carries over without re-entry.
* Location: a curated Karnataka city list (primary use case), a free-text
  search against OWM's geocoding API, or "Use my location" via the
  browser's Geolocation API in the settings page - whichever the user
  picked last is stored as lat/lon+label in plugin_settings.
* The trend graph and High/Low labels are built from the forecast list's
  upcoming ~24h of 3-hour steps. OpenWeatherMap's free tier has no
  historical-intraday endpoint (that needs a paid One Call subscription),
  so unlike the reference screenshot this shows the upcoming trend rather
  than "today including hours already passed" - the honest version of the
  same idea given the data actually available.
* Every icon is hand-built inline SVG (white line-art on black), not
  photographic/emoji - keeps it crisp on e-ink and matches the dark
  reference exactly.
"""

import logging
import time
from datetime import datetime

import pytz
import requests

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
GEOCODE_URL = "https://api.openweathermap.org/geo/1.0/direct"

HTTP_TIMEOUT = 10

# Curated Karnataka cities - the primary/default pick list per the request.
KARNATAKA_CITIES = [
    {"name": "Bengaluru", "lat": 12.9716, "lon": 77.5946},
    {"name": "Mysuru", "lat": 12.2958, "lon": 76.6394},
    {"name": "Mangaluru", "lat": 12.9141, "lon": 74.8560},
    {"name": "Hubballi", "lat": 15.3647, "lon": 75.1240},
    {"name": "Belagavi", "lat": 15.8497, "lon": 74.4977},
    {"name": "Kalaburagi", "lat": 17.3297, "lon": 76.8343},
    {"name": "Davanagere", "lat": 14.4644, "lon": 75.9218},
    {"name": "Ballari", "lat": 15.1394, "lon": 76.9214},
    {"name": "Shivamogga", "lat": 13.9299, "lon": 75.5681},
    {"name": "Tumakuru", "lat": 13.3379, "lon": 77.1173},
    {"name": "Udupi", "lat": 13.3409, "lon": 74.7421},
    {"name": "Hassan", "lat": 13.0072, "lon": 76.0962},
    {"name": "Mandya", "lat": 12.5242, "lon": 76.8958},
    {"name": "Chikkamagaluru", "lat": 13.3161, "lon": 75.7720},
    {"name": "Coorg (Madikeri)", "lat": 12.4244, "lon": 75.7382},
]

# OpenWeatherMap icon code (first 2 chars = condition, ignore day/night d/n)
# -> our SVG icon key.
OWM_ICON_MAP = {
    "01": "clear", "02": "partly-cloudy", "03": "cloudy", "04": "cloudy",
    "09": "rain", "10": "rain", "11": "storm", "13": "snow", "50": "mist",
}


class WeatherTerminal(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        template_params["karnataka_cities"] = KARNATAKA_CITIES
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== Weather: Starting ===")

        api_key = (settings.get("apiKey") or "").strip()
        if not api_key:
            raise RuntimeError("OpenWeatherMap API key not set in plugin settings")

        lat = settings.get("lat")
        lon = settings.get("lon")
        location_name = settings.get("locationName") or "Bengaluru"
        units = settings.get("units", "metric")

        if not lat or not lon:
            # fall back to Bengaluru if nothing picked yet
            lat, lon = KARNATAKA_CITIES[0]["lat"], KARNATAKA_CITIES[0]["lon"]

        data = self._fetch(api_key, float(lat), float(lon), units)

        tz_name = device_config.get_config("timezone") or "UTC"
        try:
            tz = pytz.timezone(tz_name)
        except Exception:
            tz = pytz.utc
        now = datetime.now(tz)

        dimensions = device_config.get_resolution()
        is_landscape = dimensions[0] > dimensions[1]
        if device_config.get_config("orientation") == "vertical":
            is_landscape = False

        safe = self.get_safe_area(device_config)
        template = "weather_landscape.html" if is_landscape else "weather_portrait.html"

        graph = self._build_graph(
            data["hourly"], units,
            width=(safe["usable_width"] - 40) if is_landscape else (safe["usable_width"] - 40),
            height=92 if is_landscape else 110,
        )

        image = self.render_image(
            dimensions,
            template,
            "weather.css",
            {
                "location_name": location_name,
                "date_str": now.strftime("%A").upper() + " · " + now.strftime("%b %d").upper(),
                "updated_str": now.strftime("%I:%M %p").lstrip("0"),
                "current": data["current"],
                "hourly": data["hourly"][:6],
                "graph_svg": graph,
                "unit_label": "F" if units == "imperial" else "C",
                "frame_w": safe["usable_width"],
                "frame_h": safe["usable_height"],
                "plugin_settings": {
                    "topMargin": safe.get("top", 12),
                    "bottomMargin": safe.get("bottom", 0),
                    "leftMargin": safe.get("left", 8),
                    "rightMargin": safe.get("right", 11),
                    "backgroundOption": "color",
                    "backgroundColor": "#000000",
                    "textColor": "#ffffff",
                    "selectedFrame": "None",
                },
            },
        )

        if not image:
            raise RuntimeError("Failed to render Weather image")

        logger.info(f"=== Weather: Complete ({location_name}) ===")
        return image

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _fetch(self, api_key, lat, lon, units):
        cur_resp = requests.get(
            CURRENT_URL,
            params={"lat": lat, "lon": lon, "units": units, "appid": api_key},
            timeout=HTTP_TIMEOUT,
        )
        cur_resp.raise_for_status()
        cur = cur_resp.json()

        fc_resp = requests.get(
            FORECAST_URL,
            params={"lat": lat, "lon": lon, "units": units, "appid": api_key},
            timeout=HTTP_TIMEOUT,
        )
        fc_resp.raise_for_status()
        fc = fc_resp.json()

        weather = (cur.get("weather") or [{}])[0]
        current = {
            "temp": round(cur["main"]["temp"]),
            "feels_like": round(cur["main"]["feels_like"]),
            "humidity": cur["main"]["humidity"],
            "wind_speed": round(cur["wind"]["speed"] * (3.6 if units == "metric" else 1)),
            "wind_unit": "km/h" if units == "metric" else "mph",
            "condition": weather.get("main", ""),
            "description": weather.get("description", "").title(),
            "icon": self._icon_key(weather.get("icon", "")),
            "rain_chance": round((fc["list"][0].get("pop", 0) if fc.get("list") else 0) * 100),
        }

        tz_offset = cur.get("timezone", 0)
        hourly = []
        for entry in fc.get("list", [])[:10]:
            dt = datetime.utcfromtimestamp(entry["dt"] + tz_offset)
            w = (entry.get("weather") or [{}])[0]
            hourly.append({
                "time_label": dt.strftime("%I %p").lstrip("0"),
                "hour24": dt.hour,
                "temp": round(entry["main"]["temp"]),
                "icon": self._icon_key(w.get("icon", "")),
                "rain_chance": round(entry.get("pop", 0) * 100),
                "is_night": w.get("icon", "").endswith("n"),
            })

        current["icon_svg"] = self._icon_svg(current["icon"], 56, is_night=weather.get("icon", "").endswith("n"))
        for h in hourly:
            h["icon_svg"] = self._icon_svg(h["icon"], 20, is_night=h["is_night"])

        return {"current": current, "hourly": hourly}

    @staticmethod
    def _icon_key(owm_icon):
        return OWM_ICON_MAP.get(owm_icon[:2], "cloudy")

    # ------------------------------------------------------------------
    # Icons - hand-built inline SVG line-art (white on transparent), not
    # emoji/photographic. Matches the dark reference design's icon style.
    # ------------------------------------------------------------------

    @staticmethod
    def _icon_svg(key, size, is_night=False):
        s = size / 24.0  # viewBox is 0..24, scale via width/height only
        stroke = 'stroke="#ffffff" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"'
        head = f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        tail = '</svg>'

        if key == "clear" and is_night:
            body = f'<path d="M15 3.5a7.5 7.5 0 1 0 5.5 12.6A9 9 0 0 1 15 3.5z" {stroke}/>'
            return head + body + tail

        if key == "clear":
            body = (
                f'<circle cx="12" cy="12" r="4.6" {stroke}/>'
                f'<path d="M12 2v2.4M12 19.6V22M22 12h-2.4M4.4 12H2'
                f'M18.8 5.2l-1.7 1.7M6.9 17.1l-1.7 1.7M18.8 18.8l-1.7-1.7M6.9 6.9L5.2 5.2" {stroke}/>'
            )
            return head + body + tail

        if key == "partly-cloudy":
            body = (
                f'<circle cx="8.5" cy="8.5" r="3.6" {stroke}/>'
                f'<path d="M8.5 2.8v1.6M8.5 12.6v1M2.8 8.5h1.6M13.8 5.2l-1.1 1.1M3.9 13.1l1.1-1.1" {stroke}/>'
                f'<path d="M9 20.5h8.5a3.7 3.7 0 0 0 .6-7.35 4.6 4.6 0 0 0-8.6-1.8 3.9 3.9 0 0 0-3.5 3.9 3.9 3.9 0 0 0 3 5.25z" {stroke}/>'
            )
            return head + body + tail

        if key == "cloudy":
            body = f'<path d="M6.5 19h11a4 4 0 0 0 .7-7.94A5.2 5.2 0 0 0 8.3 9.1 4.4 4.4 0 0 0 5 13.2 4.4 4.4 0 0 0 6.5 19z" {stroke}/>'
            return head + body + tail

        if key == "rain":
            body = (
                f'<path d="M6.5 14.5h11a4 4 0 0 0 .7-7.94A5.2 5.2 0 0 0 8.3 4.6 4.4 4.4 0 0 0 5 8.7 4.4 4.4 0 0 0 6.5 14.5z" {stroke}/>'
                f'<path d="M8.5 17.5l-1.2 3M12.5 17.5l-1.2 3M16.5 17.5l-1.2 3" {stroke}/>'
            )
            return head + body + tail

        if key == "storm":
            body = (
                f'<path d="M6.5 13h10.7a4 4 0 0 0 .7-7.94A5.2 5.2 0 0 0 8.5 3.1 4.4 4.4 0 0 0 5 7.2 4.4 4.4 0 0 0 6.5 13z" {stroke}/>'
                f'<path d="M13 13.5l-3 5h3.2l-2.4 5.5 6-6.5h-3.3l2.5-4z" fill="#ffffff" stroke="#ffffff" stroke-width="1" stroke-linejoin="round"/>'
            )
            return head + body + tail

        if key == "snow":
            body = (
                f'<path d="M6.5 13h10.7a4 4 0 0 0 .7-7.94A5.2 5.2 0 0 0 8.5 3.1 4.4 4.4 0 0 0 5 7.2 4.4 4.4 0 0 0 6.5 13z" {stroke}/>'
                f'<path d="M8 17v5M6 18.5l4 3M10 18.5l-4 3M16 17v5M14 18.5l4 3M18 18.5l-4 3" {stroke}/>'
            )
            return head + body + tail

        # mist / fallback
        body = f'<path d="M4 8h16M4 12h16M4 16h10M4 20h13" {stroke}/>'
        return head + body + tail

    # ------------------------------------------------------------------
    # Trend graph (inline SVG - no image libraries)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_graph(hourly, units, width, height):
        if not hourly or len(hourly) < 2:
            return ""

        temps = [h["temp"] for h in hourly]
        lo, hi = min(temps), max(temps)
        span = (hi - lo) or 1
        lo_idx, hi_idx = temps.index(lo), temps.index(hi)

        pad_x, pad_top, pad_bottom = 6, 30, 20
        n = len(hourly)
        step = (width - 2 * pad_x) / (n - 1)
        plot_h = height - pad_top - pad_bottom

        pts = []
        for i, t in enumerate(temps):
            x = pad_x + i * step
            y = pad_top + plot_h * (1 - (t - lo) / span)
            pts.append((x, y))

        line_pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

        # Now-marker at the first point (index 0 = current/nearest slot)
        now_x, now_y = pts[0]

        parts = [
            f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'xmlns="http://www.w3.org/2000/svg">'
        ]

        # dashed "now" vertical line
        parts.append(
            f'<line x1="{now_x:.1f}" y1="{pad_top - 14:.1f}" x2="{now_x:.1f}" '
            f'y2="{height - pad_bottom + 6:.1f}" stroke="#ffffff" stroke-width="1.5" '
            f'stroke-dasharray="3,4" opacity="0.85" />'
        )
        parts.append(
            f'<text x="{now_x:.1f}" y="{pad_top - 20:.1f}" fill="#ffffff" '
            f'font-size="12" font-weight="700" text-anchor="middle" '
            f'letter-spacing="1.5">NOW</text>'
        )

        # trend line + points
        parts.append(
            f'<polyline points="{line_pts}" fill="none" stroke="#ffffff" '
            f'stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" />'
        )
        for x, y in pts:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="#ffffff" />')

        # High label
        hx, hy = pts[hi_idx]
        anchor = "start" if hi_idx < n - 1 else "end"
        dx = 6 if hi_idx < n - 1 else -6
        parts.append(
            f'<text x="{hx+dx:.1f}" y="{hy-22:.1f}" fill="#ffffff" font-size="12" '
            f'font-weight="700" text-anchor="{anchor}" letter-spacing="1">HIGH</text>'
        )
        parts.append(
            f'<text x="{hx+dx:.1f}" y="{hy-6:.1f}" fill="#ffffff" font-size="20" '
            f'font-weight="800" text-anchor="{anchor}">{hi}&#176;</text>'
        )
        parts.append(
            f'<text x="{hx+dx:.1f}" y="{hy+12:.1f}" fill="#aaaaaa" font-size="11" '
            f'font-weight="600" text-anchor="{anchor}">{hourly[hi_idx]["time_label"]}</text>'
        )

        # Low label - flip above the point if there isn't enough room below
        # to fit LOW/value/time without clipping past the SVG's bottom edge.
        lx, ly = pts[lo_idx]
        anchor2 = "start" if lo_idx < n - 1 else "end"
        dx2 = 6 if lo_idx < n - 1 else -6
        if height - ly < 62:
            parts.append(
                f'<text x="{lx+dx2:.1f}" y="{ly-24:.1f}" fill="#ffffff" font-size="12" '
                f'font-weight="700" text-anchor="{anchor2}" letter-spacing="1">LOW</text>'
            )
            parts.append(
                f'<text x="{lx+dx2:.1f}" y="{ly-8:.1f}" fill="#ffffff" font-size="20" '
                f'font-weight="800" text-anchor="{anchor2}">{lo}&#176;</text>'
            )
        else:
            parts.append(
                f'<text x="{lx+dx2:.1f}" y="{ly+22:.1f}" fill="#ffffff" font-size="12" '
                f'font-weight="700" text-anchor="{anchor2}" letter-spacing="1">LOW</text>'
            )
            parts.append(
                f'<text x="{lx+dx2:.1f}" y="{ly+40:.1f}" fill="#ffffff" font-size="20" '
                f'font-weight="800" text-anchor="{anchor2}">{lo}&#176;</text>'
            )
            parts.append(
                f'<text x="{lx+dx2:.1f}" y="{ly+56:.1f}" fill="#aaaaaa" font-size="11" '
                f'font-weight="600" text-anchor="{anchor2}">{hourly[lo_idx]["time_label"]}</text>'
            )

        parts.append("</svg>")
        return "".join(parts)
