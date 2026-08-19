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

# Air quality comes from Open-Meteo's CAMS-backed endpoint: keyless, no account,
# and it carries the pollutants OpenWeatherMap's free tier does not expose in a
# usable form. Note carbon_dioxide here is *modelled outdoor ambient*, roughly
# 420-450 ppm globally - it is not room air. Indoor CO2, the number that
# actually tracks ventilation and gets into the hundreds-to-thousands, needs a
# real sensor (SCD41 on the free I2C bus).
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

AIR_FIELDS = ("us_aqi,european_aqi,pm2_5,pm10,carbon_dioxide,"
              "carbon_monoxide,nitrogen_dioxide,ozone,sulphur_dioxide,uv_index")

# US AQI bands. The scale is non-linear by design, so the position marker is
# computed per band rather than as a flat percentage of 0-500.
AQI_BANDS = [
    (0, 50, "GOOD"),
    (51, 100, "MODERATE"),
    (101, 150, "POOR"),
    (151, 200, "UNHEALTHY"),
    (201, 300, "SEVERE"),
    (301, 500, "HAZARDOUS"),
]

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
        dark = settings.get("displayMode", "dark") != "light"

        if not lat or not lon:
            # fall back to Bengaluru if nothing picked yet
            lat, lon = KARNATAKA_CITIES[0]["lat"], KARNATAKA_CITIES[0]["lon"]

        data = self._fetch(api_key, float(lat), float(lon), units, dark)

        # Air quality is additive: if it fails the weather still renders. A
        # second network dependency must not be able to blank the panel.
        try:
            air = self._fetch_air(float(lat), float(lon))
        except Exception as e:
            logger.warning("Weather: air quality unavailable: %s", e)
            air = None

        tz_name = device_config.get_config("timezone") or "UTC"
        try:
            tz = pytz.timezone(tz_name)
        except Exception:
            tz = pytz.utc
        now = datetime.now(tz)

        dimensions = device_config.get_resolution()
        # Portrait means a physically rotated frame, so the canvas rotates
        # with it. Without this the plugin lays out for 800x480 and is then
        # squeezed into a 480x800 panel.
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
        is_landscape = dimensions[0] > dimensions[1]
        if device_config.get_config("orientation") == "vertical":
            is_landscape = False

        safe = self.get_safe_area(device_config)
        template = "weather_landscape.html" if is_landscape else "weather_portrait.html"

        graph = self._build_graph(
            data["hourly"], units,
            width=(safe["usable_width"] - 40) if is_landscape else (safe["usable_width"] - 40),
            height=92 if is_landscape else 110,
            dark=dark,
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
                "air": air,
                "hourly": data["hourly"][:6],
                "graph_svg": graph,
                "unit_label": "F" if units == "imperial" else "C",
                "dark": dark,
                "frame_w": safe["usable_width"],
                "frame_h": safe["usable_height"],
                "plugin_settings": {
                    "topMargin": safe.get("top", 12),
                    "bottomMargin": safe.get("bottom", 0),
                    "leftMargin": safe.get("left", 8),
                    "rightMargin": safe.get("right", 11),
                    "textColor": "#ffffff" if dark else "#000000",
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

    def _fetch(self, api_key, lat, lon, units, dark=True):
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

        current["icon_svg"] = self._icon_svg(current["icon"], 84, is_night=weather.get("icon", "").endswith("n"), dark=dark)
        for h in hourly:
            h["icon_svg"] = self._icon_svg(h["icon"], 20, is_night=h["is_night"], dark=dark)

        return {"current": current, "hourly": hourly}

    def _fetch_air(self, lat, lon):
        resp = requests.get(
            AIR_URL,
            params={"latitude": lat, "longitude": lon,
                    "current": AIR_FIELDS, "timezone": "auto"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        cur = resp.json().get("current") or {}

        def num(key, dp=0):
            v = cur.get(key)
            if v is None:
                return None
            return f"{v:,.{dp}f}"

        aqi = cur.get("us_aqi")
        band, pos = self._aqi_band(aqi)

        return {
            "aqi": int(aqi) if aqi is not None else None,
            "band": band,
            "pos": pos,
            "eu_aqi": int(cur["european_aqi"]) if cur.get("european_aqi") is not None else None,
            "pm25": num("pm2_5", 1),
            "pm10": num("pm10", 1),
            "co2": num("carbon_dioxide"),
            "co": num("carbon_monoxide"),
            "no2": num("nitrogen_dioxide", 1),
            "o3": num("ozone"),
            "so2": num("sulphur_dioxide", 1),
            "uv": num("uv_index", 1),
        }

    @staticmethod
    def _aqi_band(aqi):
        """Band name plus 0-100 position for the scale marker.

        Each band gets an equal share of the bar rather than its true numeric
        width. The AQI scale is non-linear - GOOD spans 50 points and HAZARDOUS
        spans 200 - so a linear marker would sit almost hard left for every
        reading a person in Bengaluru actually sees.
        """
        if aqi is None:
            return "--", None
        n = len(AQI_BANDS)
        for i, (lo, hi, name) in enumerate(AQI_BANDS):
            if aqi <= hi:
                within = (aqi - lo) / float(hi - lo) if hi > lo else 0
                return name, round((i + max(0.0, min(1.0, within))) / n * 100, 1)
        return AQI_BANDS[-1][2], 100.0

    @staticmethod
    def _icon_key(owm_icon):
        return OWM_ICON_MAP.get(owm_icon[:2], "cloudy")

    # ------------------------------------------------------------------
    # Icons - hand-built inline SVG line-art (white on transparent), not
    # emoji/photographic. Matches the dark reference design's icon style.
    # ------------------------------------------------------------------

    @staticmethod
    def _icon_svg(key, size, is_night=False, dark=True):
        fg = "#ffffff" if dark else "#000000"
        s = size / 24.0  # viewBox is 0..24, scale via width/height only
        stroke = f'stroke="{fg}" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"'
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
                f'<path d="M13 13.5l-3 5h3.2l-2.4 5.5 6-6.5h-3.3l2.5-4z" fill="{fg}" stroke="{fg}" stroke-width="1" stroke-linejoin="round"/>'
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
    def _build_graph(hourly, units, width, height, dark=True):
        """Temperature bars for the coming hours.

        Bars rather than a line: on a 1-bit panel a solid block survives the
        display's quantisation intact, whereas a 2px stroke is the first thing
        to break up into dashes.

        The scale is zoomed to the data, not zeroed. An overnight spread is
        typically 6-7 degrees, so bars measured from 0 would all be within a few
        percent of each other and the shape of the night would vanish. One
        degree of headroom is added below the low so the coolest hour still
        shows a visible stub instead of nothing, and the HIGH/LOW callouts carry
        the absolute numbers.
        """
        if not hourly or len(hourly) < 2:
            return ""

        fg = "#ffffff" if dark else "#000000"
        accent = "#d40000"

        temps = [h["temp"] for h in hourly]
        lo, hi = min(temps), max(temps)
        lo_idx, hi_idx = temps.index(lo), temps.index(hi)

        base = lo - 1                      # headroom, see docstring
        span = (hi - base) or 1

        pad_x, pad_top, pad_bottom = 6, 26, 17
        n = len(hourly)
        plot_h = height - pad_top - pad_bottom
        slot = (width - 2 * pad_x) / n
        bar_w = max(6.0, slot * 0.66)
        baseline = height - pad_bottom

        parts = [
            f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'xmlns="http://www.w3.org/2000/svg">'
        ]

        bar_top = {}
        for i, t in enumerate(temps):
            cx = pad_x + slot * (i + 0.5)
            bh = max(2.0, plot_h * (t - base) / span)
            x = cx - bar_w / 2
            y = baseline - bh
            bar_top[i] = (cx, y)
            # The current hour is the red bar - it is the one reading a glance
            # is looking for, and it needs no legend to be understood.
            fill = accent if i == 0 else fg
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                f'height="{bh:.1f}" fill="{fill}"/>'
            )
            parts.append(
                f'<text x="{cx:.1f}" y="{y - 4:.1f}" fill="{fg}" font-size="11" '
                f'font-weight="700" text-anchor="middle">{t}&#176;</text>'
            )

        # Baseline, so the bars sit on something rather than floating.
        parts.append(
            f'<rect x="{pad_x:.1f}" y="{baseline:.1f}" '
            f'width="{width - 2 * pad_x:.1f}" height="2" fill="{fg}"/>'
        )

        # Only NOW is labelled. The hourly strip immediately below this graph
        # already carries the times, and printing them twice, four pixels apart,
        # just made both harder to read.
        parts.append(
            f'<text x="{pad_x + slot * 0.5:.1f}" y="{baseline + 13:.1f}" '
            f'fill="{accent}" font-size="10" font-weight="700" '
            f'text-anchor="middle" letter-spacing="0.6">NOW</text>'
        )

        # HIGH / LOW callouts sit directly above the bar they describe, above
        # that bar's own value label. Pinning them to a fixed height near the
        # top left the word stranded in empty space with nothing under it,
        # which is what happened to LOW - its bar is short, so the label was
        # floating half the graph away from the thing it labelled.
        for idx, word, colour in ((hi_idx, "HIGH", accent), (lo_idx, "LOW", fg)):
            if idx == 0:
                continue          # the NOW bar is already the loudest thing here
            cx, top = bar_top[idx]
            anchor = "middle"
            if idx == 0:
                anchor = "start"
            elif idx == n - 1:
                anchor = "end"
            parts.append(
                f'<text x="{cx:.1f}" y="{max(10.0, top - 17):.1f}" fill="{colour}" '
                f'font-size="11" font-weight="700" text-anchor="{anchor}" '
                f'letter-spacing="1.2">{word}</text>'
            )

        parts.append("</svg>")
        return "".join(parts)
