"""
TV Character Quotes
Shows a character portrait + real quote from a TV show.
Uses a shuffle-bag (draw-without-replacement) so no quote repeats until
every quote for that character has been shown once.
"""

import json
import logging
import random
from pathlib import Path

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
QUOTES_FILE = PLUGIN_DIR / "quotes.json"
PORTRAIT_DIR = PLUGIN_DIR / "render" / "portraits"
LOGO_DIR = PLUGIN_DIR / "render" / "logos"

# Show name -> logo asset. Pure BWR PNG, corner-badge sized already.
SHOW_LOGOS = {
    "The Big Bang Theory": "tbbt_logo.png",
}

# Persistent so the shuffle-bag survives restarts/reboots (NOT /tmp - that's
# tmpfs and gets wiped on reboot, learned that the hard way on this device).
STATE_DIR = PLUGIN_DIR / ".state"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "shuffle_state.json"


class TVQuotes(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        characters = self._load_quotes()
        template_params["characters"] = [
            {"value": key, "label": f"{c['display_name']} ({c['show']})"}
            for key, c in characters.items()
        ]
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== TV Quotes: Starting ===")

        characters = self._load_quotes()
        character_key = settings.get("character", "random")

        if character_key == "random" or character_key not in characters:
            character_key = random.choice(list(characters.keys()))

        character = characters[character_key]
        quote = self._draw(f"quote:{character_key}", character["quotes"])

        portraits = character.get("portraits") or ([character["portrait"]] if character.get("portrait") else [])
        portrait_file = self._draw(f"portrait:{character_key}", portraits) if portraits else None
        portrait_uri = self._portrait_data_uri(PORTRAIT_DIR / portrait_file) if portrait_file else None

        logo_file = SHOW_LOGOS.get(character["show"])
        logo_uri = self._portrait_data_uri(LOGO_DIR / logo_file) if logo_file else None

        dimensions = device_config.get_resolution()
        is_landscape = dimensions[0] > dimensions[1]
        if device_config.get_config("orientation") == "vertical":
            is_landscape = False

        template = "tv_quotes_landscape.html" if is_landscape else "tv_quotes_portrait.html"

        image = self.render_image(
            dimensions,
            template,
            "tv_quotes.css",
            {
                "quote": quote,
                "character_name": character["display_name"],
                "show": character["show"],
                "portrait_uri": portrait_uri,
                "logo_uri": logo_uri,
                "plugin_settings": settings,
            }
        )

        if not image:
            raise RuntimeError("Failed to render image")

        logger.info(f"=== TV Quotes: Complete ({character['display_name']}) ===")
        return image

    # ------------------------------------------------------------------
    # Quote data
    # ------------------------------------------------------------------

    @staticmethod
    def _load_quotes():
        with open(QUOTES_FILE, "r") as f:
            return json.load(f)

    # ------------------------------------------------------------------
    # Shuffle-bag: draw without replacement, reshuffle only once the bag
    # empties. Guarantees no repeat until every item in `pool` has been
    # shown once. Used for both quotes and portraits (separate namespaces
    # so a character's quote cycle and portrait cycle don't collide).
    # ------------------------------------------------------------------

    def _draw(self, bag_key, pool):
        if len(pool) == 1:
            return pool[0]

        state = self._load_state()
        bag = state.get(bag_key, [])

        if not bag:
            bag = list(range(len(pool)))
            random.shuffle(bag)
            logger.info(f"Reshuffled bag '{bag_key}' ({len(bag)} items)")

        idx = bag.pop()
        state[bag_key] = bag
        self._save_state(state)

        return pool[idx]

    @staticmethod
    def _load_state():
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"State read failed: {e}")
        return {}

    @staticmethod
    def _save_state(state):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logger.warning(f"State save failed: {e}")

    # ------------------------------------------------------------------
    # Portrait
    # ------------------------------------------------------------------

    @staticmethod
    def _portrait_data_uri(path):
        """Return a data: URI for the portrait, or None if not supplied yet
        (falls back to an initials placeholder in the template)."""
        if not path.exists():
            return None
        try:
            import base64
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except Exception as e:
            logger.warning(f"Portrait load failed: {e}")
            return None
