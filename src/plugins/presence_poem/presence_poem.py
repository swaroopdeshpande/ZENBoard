"""
Presence Poem - ZENBoard
Triggered by the mmWave presence sensor when someone lingers in the room
(not just passes through) - the board quietly composes a short AI-generated
poem/thought and reveals it. Ties presence + free AI text-gen + the
ceremonial feel of an e-ink reveal into something that notices you, rather
than another live dashboard.

Auto-fits type size to the actual poem length so a two-word fragment and a
four-line poem both fill the frame properly - never a huge slab of dead
space, never overflowing.
"""

import logging
import random
from datetime import datetime

from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont

from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_utils import stem_darken

logger = logging.getLogger(__name__)

FONT_ITALIC = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"
FONT_SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

MODEL = "openai/gpt-oss-20b:free"

TIME_MOODS = {
    "dawn":    "the quiet hour just before a house wakes up",
    "morning": "morning light, the start of something",
    "midday":  "the stillness of the middle of the day",
    "evening": "evening settling in, the day loosening its grip",
    "night":   "a house gone quiet, night pressing at the windows",
}


class PresencePoem(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== Presence Poem: Starting ===")

        api_key = device_config.load_env_key("OPEN_AI_SECRET")
        poem_lines = None
        if api_key:
            try:
                poem_lines = self._generate_poem(api_key)
            except Exception as e:
                logger.error(f"Poem generation failed, using fallback: {e}")
        if not poem_lines:
            poem_lines = self._fallback_poem()

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
        frame_w, frame_h = safe["usable_width"], safe["usable_height"]

        img = self._render(poem_lines, frame_w, frame_h)

        background = Image.new("RGB", dimensions, "white")
        background.paste(img, (safe.get("start_x", 0), safe.get("start_y", 0)))

        background = stem_darken(background)

        logger.info("=== Presence Poem: Complete ===")
        return background

    # ------------------------------------------------------------------

    @staticmethod
    def _time_mood():
        h = datetime.now().hour
        if 4 <= h < 7:
            return TIME_MOODS["dawn"]
        if 7 <= h < 12:
            return TIME_MOODS["morning"]
        if 12 <= h < 17:
            return TIME_MOODS["midday"]
        if 17 <= h < 21:
            return TIME_MOODS["evening"]
        return TIME_MOODS["night"]

    def _generate_poem(self, api_key):
        client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")

        system_content = (
            "You write extremely short, quiet, evocative micro-poems - in the spirit of "
            "haiku or a single held breath. 2 to 4 short lines, plain everyday words, no "
            "titles, no rhyme requirement, no explanation, no quotation marks. Never "
            "mention AI, sensors, or technology. Respond with ONLY the poem, one line per "
            "line, nothing else."
        )
        user_content = f"Write a micro-poem inspired by: {self._time_mood()}."

        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content},
            ],
            temperature=1.1,
            max_tokens=1400,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError(f"empty content (finish_reason={response.choices[0].finish_reason})")
        text = content.strip()
        lines = [l.strip().strip('"').strip("'") for l in text.split("\n") if l.strip()]
        lines = lines[:5]
        if not lines:
            raise RuntimeError("empty poem response")
        return lines

    @staticmethod
    def _fallback_poem():
        options = [
            ["the room", "notices", "you've returned"],
            ["light shifts", "a step in the doorway", "something settles"],
            ["quiet held", "then a footstep", "the day continues"],
            ["still air", "moves", "you are here"],
        ]
        return random.choice(options)

    # ------------------------------------------------------------------
    # Auto-fit rendering - tries font sizes from large to small, picks the
    # biggest that fits the frame with generous but not empty margins.
    # ------------------------------------------------------------------

    def _render(self, lines, frame_w, frame_h):
        img = Image.new("RGB", (frame_w, frame_h), (255, 255, 255))
        draw = ImageDraw.Draw(img)

        margin_x = int(frame_w * 0.12)
        margin_top = int(frame_h * 0.16)
        margin_bottom = int(frame_h * 0.14)
        max_text_w = frame_w - 2 * margin_x
        max_text_h = frame_h - margin_top - margin_bottom

        wrapped, font, line_h = self._fit_text(draw, lines, max_text_w, max_text_h)

        total_h = len(wrapped) * line_h
        y = margin_top + (max_text_h - total_h) // 2

        for line in wrapped:
            bbox = draw.textbbox((0, 0), line, font=font)
            w = bbox[2] - bbox[0]
            x = (frame_w - w) // 2
            draw.text((x, y), line, font=font, fill=(0, 0, 0))
            y += line_h

        # Small red accent mark above the poem - drawn as two bars (not a
        # text glyph - the "pause mark" unicode character isn't in this
        # font, rendered as tofu boxes when tried as text).
        bar_h = max(22, frame_h // 14)
        bar_w = max(5, bar_h // 9)
        gap = bar_w * 2
        mark_y0 = margin_top + (max_text_h - total_h) // 2 - int(frame_h * 0.10) - bar_h
        cx = frame_w // 2
        draw.rounded_rectangle(
            [cx - gap - bar_w, mark_y0, cx - gap, mark_y0 + bar_h],
            radius=bar_w // 2, fill=(212, 0, 0),
        )
        draw.rounded_rectangle(
            [cx + gap - bar_w, mark_y0, cx + gap, mark_y0 + bar_h],
            radius=bar_w // 2, fill=(212, 0, 0),
        )

        # Bottom caption
        cap_font = ImageFont.truetype(FONT_SANS, max(11, frame_h // 40))
        caption = "SENSED · " + datetime.now().strftime("%-I:%M %p").upper()
        cbbox = draw.textbbox((0, 0), caption, font=cap_font)
        cw = cbbox[2] - cbbox[0]
        draw.text(((frame_w - cw) // 2, frame_h - margin_bottom + int(frame_h * 0.03)),
                   caption, font=cap_font, fill=(0, 0, 0))

        # Thin outer border - frames it like a small art card
        border = max(2, frame_h // 240)
        draw.rectangle([(0, 0), (frame_w - 1, frame_h - 1)], outline=(0, 0, 0), width=border)

        return img

    @staticmethod
    def _fit_text(draw, lines, max_w, max_h):
        """Try decreasing font sizes; for each, wrap each input line to
        max_w and see if the total wrapped block fits max_h. Returns the
        largest size that fits (or the smallest tried, un-fit, as a
        last resort so it never raises)."""
        text = " / ".join(lines) if len(lines) == 1 else None

        for size in range(96, 20, -4):
            font = ImageFont.truetype(FONT_ITALIC, size)
            line_h = int(size * 1.4)

            wrapped = []
            for src_line in lines:
                wrapped.extend(PresencePoem._wrap(draw, src_line, font, max_w))

            total_h = len(wrapped) * line_h
            widest = max(
                (draw.textbbox((0, 0), l, font=font)[2] for l in wrapped), default=0
            )

            if total_h <= max_h and widest <= max_w:
                return wrapped, font, line_h

        # smallest size as a guaranteed-fit fallback
        size = 20
        font = ImageFont.truetype(FONT_ITALIC, size)
        line_h = int(size * 1.5)
        wrapped = []
        for src_line in lines:
            wrapped.extend(PresencePoem._wrap(draw, src_line, font, max_w))
        return wrapped, font, line_h

    @staticmethod
    def _wrap(draw, text, font, max_w):
        words = text.split()
        if not words:
            return [""]
        lines, cur = [], []
        for word in words:
            test = " ".join(cur + [word])
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] - bbox[0] <= max_w or not cur:
                cur.append(word)
            else:
                lines.append(" ".join(cur))
                cur = [word]
        if cur:
            lines.append(" ".join(cur))
        return lines
