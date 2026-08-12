"""
Apple Calendar - ZENBoard
Month-grid calendar styled after iOS Calendar: today as a filled red circle,
events as small colored bars, strict bordered grid. Imports any number of
ICS/iCal URLs (Google, Apple iCloud share links, Outlook, any webcal://) -
no OAuth, just a public/secret .ics feed URL pasted in settings.

Two themes: "red" (white bg, black text, red accents) and "black" (black bg,
white text, red accents) - selectable in settings.
"""

import logging
from datetime import datetime, timedelta

import icalendar
import pytz
import recurring_ical_events
import requests

from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_utils import stem_darken

logger = logging.getLogger(__name__)

DOT_COLORS = ["#d40000", "#000000", "#d40000", "#000000", "#d40000", "#000000"]
WEEKDAY_LABELS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]


class AppleCalendar(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== Apple Calendar: Starting ===")

        calendar_urls = settings.get("calendarURLs[]") or []
        if isinstance(calendar_urls, str):
            calendar_urls = [calendar_urls]
        calendar_urls = [u.strip() for u in calendar_urls if u and u.strip()]

        theme = settings.get("theme", "red")

        timezone = device_config.get_config("timezone", default="Asia/Kolkata") or "Asia/Kolkata"
        try:
            tz = pytz.timezone(timezone)
        except Exception:
            tz = pytz.timezone("Asia/Kolkata")
        now = datetime.now(tz)

        month_start = datetime(now.year, now.month, 1, tzinfo=tz)
        offset = (month_start.weekday() + 1) % 7  # days since Sunday
        grid_start = month_start - timedelta(days=offset)
        grid_end = grid_start + timedelta(days=42)

        events_by_day = {}
        if calendar_urls:
            try:
                events_by_day = self._fetch_events(calendar_urls, tz, grid_start, grid_end)
            except Exception as e:
                logger.error(f"Apple Calendar: event fetch failed: {e}")

        dimensions = device_config.get_resolution()
        is_landscape = dimensions[0] > dimensions[1]
        if device_config.get_config("orientation") == "vertical":
            is_landscape = False

        max_shown = 2 if is_landscape else 4
        weeks = self._build_weeks(grid_start, now, month_start.month, events_by_day, max_shown)

        safe = self.get_safe_area(device_config)
        template = "apple_calendar_landscape.html" if is_landscape else "apple_calendar_portrait.html"

        image = self.render_image(
            dimensions,
            template,
            "apple_calendar.css",
            {
                "dark": theme == "black",
                "month_label": now.strftime("%B %Y").upper(),
                "weekday_labels": WEEKDAY_LABELS,
                "weeks": weeks,
                "frame_w": safe["usable_width"],
                "frame_h": safe["usable_height"],
                "plugin_settings": {
                    "topMargin": safe.get("top", 0),
                    "bottomMargin": safe.get("bottom", 0),
                    "leftMargin": safe.get("left", 0),
                    "rightMargin": safe.get("right", 0),
                    "backgroundOption": "color",
                    "backgroundColor": "#000000" if theme == "black" else "#ffffff",
                    "textColor": "#ffffff" if theme == "black" else "#000000",
                    "selectedFrame": "None",
                },
            },
        )

        if not image:
            raise RuntimeError("Failed to render Apple Calendar image")

        image = stem_darken(image)

        logger.info("=== Apple Calendar: Complete ===")
        return image

    # ------------------------------------------------------------------

    def _fetch_events(self, urls, tz, start, end):
        events_by_day = {}
        for i, url in enumerate(urls):
            color = DOT_COLORS[i % len(DOT_COLORS)]
            try:
                cal = self._fetch_ics(url)
            except Exception as e:
                logger.warning(f"Apple Calendar: skipping unreachable calendar {url}: {e}")
                continue
            try:
                occurrences = recurring_ical_events.of(cal).between(start, end)
            except Exception as e:
                logger.warning(f"Apple Calendar: could not expand events for {url}: {e}")
                continue
            for ev in occurrences:
                dtstart = ev.decoded("dtstart")
                if isinstance(dtstart, datetime):
                    local = dtstart.astimezone(tz)
                else:
                    local = datetime(dtstart.year, dtstart.month, dtstart.day, tzinfo=tz)
                key = local.strftime("%Y-%m-%d")
                title = str(ev.get("summary") or "Untitled")
                events_by_day.setdefault(key, []).append({"title": title, "color": color})
        return events_by_day

    @staticmethod
    def _fetch_ics(url):
        if url.startswith("webcal://"):
            url = url.replace("webcal://", "https://")
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        return icalendar.Calendar.from_ical(resp.text)

    @staticmethod
    def _build_weeks(grid_start, now, current_month, events_by_day, max_shown=3):
        weeks = []
        d = grid_start
        today_key = now.strftime("%Y-%m-%d")
        for _ in range(6):
            week = []
            for _ in range(7):
                key = d.strftime("%Y-%m-%d")
                all_events = events_by_day.get(key, [])
                week.append({
                    "day": d.day,
                    "in_month": d.month == current_month,
                    "is_today": key == today_key,
                    "events": all_events[:max_shown],
                    "overflow": max(0, len(all_events) - max_shown),
                })
                d += timedelta(days=1)
            weeks.append(week)
        return weeks
