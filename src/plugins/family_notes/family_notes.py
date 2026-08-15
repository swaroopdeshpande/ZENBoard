"""
Family Notes - ZENBoard
Shows the last few messages pinned via the /notes web form, each in its own
row on the board. No API, no key - purely local, driven by whoever on the
household WiFi visits http://<pi>/notes on their phone.

Full refresh only. Real hardware partial refresh was tried and reverted:
confirmed via Waveshare's own demo (display_Partial shipped commented-out
for this exact bi-color model) and ESPHome's driver list (partial refresh
only listed for the plain black/white 7.5" variant, not the BWR tri-color
one) that this isn't reliably supported on tri-color panels - not worth
the corruption risk. See docs/ZENBOARD.md.
"""

import json
import logging
from datetime import datetime

from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_utils import stem_darken

logger = logging.getLogger(__name__)

NOTES_FILE = "/tmp/zenboard_family_notes.json"
MAX_ROWS_LANDSCAPE = 8
MAX_ROWS_PORTRAIT = 13


class FamilyNotes(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== Family Notes: Starting ===")

        notes = self._load_notes()

        dimensions = device_config.get_resolution()
        is_landscape = dimensions[0] > dimensions[1]
        if device_config.get_config("orientation") == "vertical":
            is_landscape = False

        max_rows = MAX_ROWS_LANDSCAPE if is_landscape else MAX_ROWS_PORTRAIT
        rows = self._build_rows(notes, max_rows, device_config)

        safe = self.get_safe_area(device_config)
        template = "family_notes_landscape.html" if is_landscape else "family_notes_portrait.html"

        image = self.render_image(
            dimensions,
            template,
            "family_notes.css",
            {
                "rows": rows,
                "has_notes": len(rows) > 0,
                "frame_w": safe["usable_width"],
                "frame_h": safe["usable_height"],
                "plugin_settings": {
                    "topMargin": safe.get("top", 0),
                    "bottomMargin": safe.get("bottom", 0),
                    "leftMargin": safe.get("left", 0),
                    "rightMargin": safe.get("right", 0),
                    "backgroundOption": "color",
                    "backgroundColor": "#ffffff",
                    "textColor": "#000000",
                    "selectedFrame": "None",
                },
            },
        )

        if not image:
            raise RuntimeError("Failed to render Family Notes image")

        image = stem_darken(image)

        logger.info("=== Family Notes: Complete ===")
        return image

    @staticmethod
    def _load_notes():
        try:
            with open(NOTES_FILE) as f:
                return json.load(f)
        except Exception:
            return []

    @staticmethod
    def _build_rows(notes, max_rows, device_config):
        if not notes:
            return []
        tz_name = device_config.get_config("timezone") or "Asia/Kolkata"
        try:
            import pytz
            tz = pytz.timezone(tz_name)
        except Exception:
            tz = None

        latest_first = list(reversed(notes[-max_rows:]))
        rows = []
        for i, n in enumerate(latest_first):
            when = ""
            try:
                dt = datetime.fromtimestamp(n["timestamp"], tz) if tz else datetime.fromtimestamp(n["timestamp"])
                when = dt.strftime("%I:%M %p").lstrip("0") + " · " + dt.strftime("%b %d")
            except Exception:
                pass
            rows.append({
                "from_name": n["from_name"],
                "message": n["message"],
                "when": when,
                "is_latest": i == 0,
                "done": n.get("done", False),
            })
        return rows
