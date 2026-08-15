"""
Apple Health - Activity rings.

Reads whatever the iOS Shortcuts automation last POSTed to
/api/apple_health/push (see blueprints/apple_health_blueprint.py). Apple
Health has no cloud API, so there is nothing to fetch here - this plugin
is a pure renderer over the last pushed payload.

Ring colours on a tri-color BWR panel: Apple's Move/Exercise/Stand are
red/green/blue, and only red survives. Rather than render two identical
black rings, Stand is drawn as discrete segments - one per hour - which
is both distinguishable at a glance and truer to what that ring actually
counts than a continuous sweep would be.
"""

import json
import logging
import math
import time
from pathlib import Path

from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_utils import stem_darken

logger = logging.getLogger(__name__)

DATA_FILE = Path("/home/zenith/InkyPi/src/config/apple_health.json")

# Ring geometry, outermost first. Radii are chosen so the 26px strokes sit
# with a 5px gap between rings, matching Apple's proportions.
RING_STROKE = 26
RINGS = [
    {"key": "move", "label": "MOVE", "unit": "KCAL", "radius": 128, "color": "#d40000",
     "segmented": False, "default_goal": 500.0},
    {"key": "exercise", "label": "EXERCISE", "unit": "MIN", "radius": 97, "color": "#000000",
     "segmented": False, "default_goal": 30.0},
    {"key": "stand", "label": "STAND", "unit": "HRS", "radius": 66, "color": "#000000",
     "segmented": True, "default_goal": 12.0},
]

CENTER = 150


class AppleHealth(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== Apple Health: Starting ===")

        data = self._load_data()
        safe = self.get_safe_area(device_config)

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]

        rings = [self._ring_geometry(r, data) for r in RINGS]

        template_params = {
            "rings": rings,
            "stats": self._stats(data),
            "stale_note": self._stale_note(data),
            "today": time.strftime("%a %d %b").upper(),
            "usable_width": safe.get("usable_width", dimensions[0]),
            "usable_height": safe.get("usable_height", dimensions[1]),
            "svg_size": CENTER * 2,
            "plugin_settings": settings,
        }

        image = self.render_image(
            dimensions,
            "apple_health.html",
            "apple_health.css",
            template_params
        )

        if not image:
            raise RuntimeError("Failed to render Apple Health image.")

        # Vector rings + text only, no photographic content - safe to
        # stem darken (see the plugin conventions in docs/ZENBOARD.md).
        image = stem_darken(image)

        logger.info("=== Apple Health: Complete ===")
        return image

    # ------------------------------------------------------------------
    # Ring geometry
    # ------------------------------------------------------------------

    def _ring_geometry(self, ring, data):
        value = float(data.get(ring["key"], 0) or 0)
        goal = float(data.get(f"{ring['key']}_goal", 0) or 0)
        if goal <= 0:
            # Standard Apple targets - a "/1" placeholder in the empty state
            # reads as a broken render rather than "nothing synced yet".
            goal = ring["default_goal"]

        fraction = value / goal
        radius = ring["radius"]
        circumference = 2 * math.pi * radius

        geo = {
            "label": ring["label"],
            "unit": ring["unit"],
            "color": ring["color"],
            "radius": radius,
            "stroke": RING_STROKE,
            "value": int(round(value)),
            "goal": int(round(goal)),
            "percent": int(round(fraction * 100)),
            "closed": fraction >= 1.0,
            "full_circle": fraction >= 1.0,
            "path": None,
            "dasharray": None,
        }

        # Apple lets you overshoot a ring; the arc still stops at 100% so a
        # 180% Move day doesn't wrap round and read as 80%.
        drawn = max(0.0, min(fraction, 1.0))

        if ring["segmented"]:
            # One dash per unit of the goal (one per stand hour), sized off
            # the FULL circumference so a segment always means the same
            # thing regardless of how far round the arc has got.
            gap = 5.0
            segment = circumference / max(goal, 1.0)
            geo["dasharray"] = f"{max(segment - gap, 1.0):.2f} {gap:.2f}"

        if drawn <= 0:
            geo["path"] = None
        elif drawn >= 1.0:
            geo["full_circle"] = True
        else:
            geo["path"] = self._arc_path(CENTER, CENTER, radius, drawn)

        return geo

    @staticmethod
    def _arc_path(cx, cy, radius, fraction):
        """Clockwise arc starting at 12 o'clock, as an SVG path. Progress is
        encoded in the path geometry rather than a stroke-dashoffset so the
        segmented Stand ring can still use dasharray for its hour gaps."""
        start_angle = -math.pi / 2
        end_angle = start_angle + (2 * math.pi * fraction)

        x0 = cx + radius * math.cos(start_angle)
        y0 = cy + radius * math.sin(start_angle)
        x1 = cx + radius * math.cos(end_angle)
        y1 = cy + radius * math.sin(end_angle)

        large_arc = 1 if fraction > 0.5 else 0

        return f"M {x0:.2f} {y0:.2f} A {radius} {radius} 0 {large_arc} 1 {x1:.2f} {y1:.2f}"

    # ------------------------------------------------------------------
    # Secondary stats
    # ------------------------------------------------------------------

    @staticmethod
    def _stats(data):
        steps = float(data.get("steps", 0) or 0)
        distance = float(data.get("distance_km", 0) or 0)
        sleep = float(data.get("sleep_hours", 0) or 0)
        resting = float(data.get("resting_hr", 0) or 0)
        flights = float(data.get("flights", 0) or 0)

        sleep_h = int(sleep)
        sleep_m = int(round((sleep - sleep_h) * 60))

        stats = [
            {"label": "STEPS", "value": f"{int(steps):,}", "unit": ""},
            {"label": "DISTANCE", "value": f"{distance:.1f}", "unit": "KM"},
            {"label": "SLEEP", "value": f"{sleep_h}h {sleep_m:02d}m" if sleep > 0 else "--", "unit": ""},
        ]

        # Only shown when the Shortcut actually sent them - a hardcoded
        # "0 BPM" tile would read as a real (alarming) measurement.
        if resting > 0:
            stats.append({"label": "RESTING HR", "value": f"{int(resting)}", "unit": "BPM"})
        if flights > 0:
            stats.append({"label": "FLIGHTS", "value": f"{int(flights)}", "unit": ""})

        return stats

    @staticmethod
    def _stale_note(data):
        """The frame should never quietly show yesterday's rings as if they
        were today's - if the automation stops firing, say so."""
        updated = float(data.get("updated_at", 0) or 0)
        if updated <= 0:
            return "NO DATA PUSHED YET"

        age_hours = (time.time() - updated) / 3600.0
        if age_hours > 6:
            if age_hours >= 48:
                return f"LAST SYNC {int(age_hours / 24)} DAYS AGO"
            return f"LAST SYNC {int(age_hours)}H AGO"
        return None

    @staticmethod
    def _load_data():
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning("No Apple Health data pushed yet")
            return {}
        except Exception as e:
            logger.error(f"Failed to read Apple Health data: {e}")
            return {}
