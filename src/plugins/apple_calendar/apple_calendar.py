"""
Apple Calendar - ZENBoard

Two-week strip across the top (this week and next, Monday-first) with today
marked as a filled circle and a dot under any day that has events, then the
next few days that actually have something on them listed underneath in
columns.

Imports any number of ICS/iCal URLs (Google, Apple iCloud share links,
Outlook, any webcal://) - no OAuth, just a public/secret .ics feed URL
pasted in settings.

Note on colour: the panel is tri-colour BWR, so the greys in a typical
calendar design are not available - a mid grey dithers into visible noise.
Hierarchy is carried by weight and size instead, with red kept for genuine
accents. All-day events use a hard-edged hatch (pure black lines on white)
rather than a grey fill, which stays crisp under quantisation.
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

WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

MAX_UPCOMING_DAYS = 3        # columns underneath the strip
MAX_EVENTS_PER_DAY = 4       # per column, before "+N more"
LOOKAHEAD_DAYS = 21          # how far forward to hunt for days with events
MAX_DOTS = 3                 # dots drawn under a day in the strip


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
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Monday of the current week, then a fortnight from there.
        strip_start = today - timedelta(days=today.weekday())
        strip_end = strip_start + timedelta(days=14)
        fetch_end = today + timedelta(days=LOOKAHEAD_DAYS)

        events_by_day = {}
        if calendar_urls:
            try:
                events_by_day = self._fetch_events(
                    calendar_urls, tz, strip_start, max(strip_end, fetch_end))
            except Exception as e:
                logger.error(f"Apple Calendar: event fetch failed: {e}")
        else:
            logger.warning("Apple Calendar: no calendar URLs configured")

        dimensions = device_config.get_resolution()
        is_landscape = dimensions[0] > dimensions[1]
        if device_config.get_config("orientation") == "vertical":
            is_landscape = False

        strip = self._build_strip(strip_start, today, events_by_day)
        upcoming = self._build_upcoming(
            today, events_by_day,
            max_days=MAX_UPCOMING_DAYS if is_landscape else 2)

        safe = self.get_safe_area(device_config)
        template = ("apple_calendar_landscape.html" if is_landscape
                    else "apple_calendar_portrait.html")

        image = self.render_image(
            dimensions,
            template,
            "apple_calendar.css",
            {
                "dark": theme == "black",
                "month_name": now.strftime("%B"),
                "year": now.strftime("%Y"),
                "weekday_labels": WEEKDAY_LABELS,
                "strip": strip,
                "upcoming": upcoming,
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

        logger.info(f"=== Apple Calendar: Complete ({len(upcoming)} upcoming days) ===")
        return image

    # ------------------------------------------------------------------
    # Event fetching
    # ------------------------------------------------------------------

    def _fetch_events(self, urls, tz, start, end):
        """{ 'YYYY-MM-DD': [ {title, all_day, start, end, sort}, ... ] }"""
        events_by_day = {}
        for url in urls:
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
                try:
                    dtstart = ev.decoded("dtstart")
                except Exception:
                    continue
                try:
                    dtend = ev.decoded("dtend")
                except Exception:
                    dtend = None

                # A bare date (no time component) is an all-day event.
                all_day = not isinstance(dtstart, datetime)

                if all_day:
                    local = datetime(dtstart.year, dtstart.month, dtstart.day, tzinfo=tz)
                    time_label = ""
                    sort_key = -1          # all-day events sit above timed ones
                else:
                    local = dtstart.astimezone(tz)
                    time_label = self._fmt_time(local)
                    if isinstance(dtend, datetime):
                        time_label += " — " + self._fmt_time(dtend.astimezone(tz))
                    sort_key = local.hour * 60 + local.minute

                events_by_day.setdefault(local.strftime("%Y-%m-%d"), []).append({
                    "title": str(ev.get("summary") or "Untitled"),
                    "all_day": all_day,
                    "time": time_label,
                    "sort": sort_key,
                })

        for key in events_by_day:
            events_by_day[key].sort(key=lambda e: e["sort"])
        return events_by_day

    @staticmethod
    def _fmt_time(dt):
        # %-I is a GNU extension - fine on the Pi, and avoids a leading zero.
        return dt.strftime("%-I:%M %p")

    @staticmethod
    def _fetch_ics(url):
        if url.startswith("webcal://"):
            url = url.replace("webcal://", "https://")
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        return icalendar.Calendar.from_ical(resp.text)

    # ------------------------------------------------------------------
    # Layout data
    # ------------------------------------------------------------------

    @staticmethod
    def _build_strip(strip_start, today, events_by_day):
        """Two Monday-first weeks: this one and the next."""
        weeks = []
        d = strip_start
        today_key = today.strftime("%Y-%m-%d")
        for _ in range(2):
            week = []
            for _ in range(7):
                key = d.strftime("%Y-%m-%d")
                count = len(events_by_day.get(key, []))
                week.append({
                    "day": d.day,
                    "is_today": key == today_key,
                    "is_past": d < today,
                    "dots": min(count, MAX_DOTS),
                })
                d += timedelta(days=1)
            weeks.append(week)
        return weeks

    @staticmethod
    def _build_upcoming(today, events_by_day, max_days=MAX_UPCOMING_DAYS):
        """The next few days that actually have events, today included.

        Deliberately skips empty days rather than showing blank columns -
        with only three slots, a column reading 'nothing on' is wasted space.
        """
        out = []
        for offset in range(0, LOOKAHEAD_DAYS + 1):
            if len(out) >= max_days:
                break
            d = today + timedelta(days=offset)
            evs = events_by_day.get(d.strftime("%Y-%m-%d"))
            if not evs:
                continue
            out.append({
                "label": d.strftime("%a %-d %b"),
                "events": evs[:MAX_EVENTS_PER_DAY],
                "overflow": max(0, len(evs) - MAX_EVENTS_PER_DAY),
            })
        return out
