"""
Kannada Quotes
Shows a real, verified Kannada quote/vachana from a curated set of authors
(Jnanpith winners, Vachana poets, and others). Previously called Sarvam AI
live per-refresh - dropped that: an LLM in a low-resource script like
Kannada produced grammatically broken/hallucinated quotes far too often.
Every quote in quotes_kn.json was sourced from kn.wikiquote.org /
kn.wikisource.org / other verifiable pages, not generated.

Uses a shuffle-bag (draw-without-replacement) so no quote repeats until
every quote for the selected author (or, in random-author mode, every
quote across all authors) has been shown once - same pattern as tv_quotes.
"""

import json
import logging
import random
from pathlib import Path

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).parent
QUOTES_FILE = PLUGIN_DIR / "quotes_kn.json"

# Persistent so the shuffle-bag survives restarts/reboots (NOT /tmp - that's
# tmpfs and gets wiped on reboot).
STATE_DIR = PLUGIN_DIR / ".state"
STATE_DIR.mkdir(exist_ok=True)
STATE_FILE = STATE_DIR / "shuffle_state.json"


class KannadaQuotes(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        authors = self._load_quotes()
        template_params["authors"] = [
            {"value": key, "label": a["name_english"]}
            for key, a in authors.items()
        ]
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== Kannada Quotes: Starting ===")

        authors = self._load_quotes()
        author_key = settings.get("person", "").strip() or "random"

        if author_key == "random" or author_key not in authors:
            author_key = self._draw("author", list(authors.keys()))

        author = authors[author_key]
        quote = self._draw(f"quote:{author_key}", author["quotes"])

        dimensions = device_config.get_resolution()
        try:
            if device_config.get_config("orientation") == "vertical":
                dimensions = dimensions[::-1]
        except Exception:
            pass

        template_params = {
            "quote": quote,
            "author": author["name_kannada"],
            "quote_font_size": self._quote_font_size(quote),
            "plugin_settings": settings,
        }

        image = self.render_image(
            dimensions,
            "kannada_quotes.html",
            "kannada_quotes.css",
            template_params
        )

        if not image:
            raise RuntimeError("Failed to render Kannada quote image.")

        logger.info(f"=== Kannada Quotes: Complete ({author['name_english']}) ===")
        return image

    # ------------------------------------------------------------------
    # Longer vachanas/quotes need a smaller point size to fit inside the
    # fixed-height frame - the manuscript box has overflow:hidden (a flex
    # min-height:auto quirk otherwise lets long text silently grow it past
    # the calibrated border), so the wrong size crops text instead of
    # just looking cramped. Tiered by character count rather than a true
    # auto-fit measurement - simple and matches every quote in the set.
    # ------------------------------------------------------------------

    @staticmethod
    def _quote_font_size(quote):
        length = len(quote)
        if length <= 55:
            return 36
        if length <= 90:
            return 30
        if length <= 130:
            return 26
        if length <= 170:
            return 22
        return 19

    # ------------------------------------------------------------------
    # Quote data
    # ------------------------------------------------------------------

    @staticmethod
    def _load_quotes():
        with open(QUOTES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------------
    # Shuffle-bag: draw without replacement, reshuffle only once the bag
    # empties. Guarantees no repeat until every item in `pool` has been
    # shown once. Separate namespaces per author (and one for author
    # selection itself) so cycles don't collide.
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
            except Exception:
                return {}
        return {}

    @staticmethod
    def _save_state(state):
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
