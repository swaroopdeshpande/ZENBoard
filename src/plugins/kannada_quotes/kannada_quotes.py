import json
import logging
import re

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

DEFAULT_PERSON = "Basavanna"

SYSTEM_PROMPT = (
    "You are a Kannada literature and quotes assistant. "
    "When asked for a quote by a specific person, reply with ONLY a single JSON object "
    "and nothing else, no markdown, no code fences, no explanation. "
    "The JSON object must have exactly two keys: \"quote\" and \"author\". "
    "The \"quote\" value must be a well-known quote or saying by the requested person, "
    "written in the Kannada script (ಕನ್ನಡ ಲಿಪಿ). "
    "The \"author\" value must be the person's name written in Kannada script. "
    "Do not include any text before or after the JSON object."
)


class KannadaQuotes(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        api_key = settings.get("apiKey", "").strip()
        if not api_key:
            try:
                api_key = device_config.load_env_key("SARVAM_API_KEY")
            except Exception:
                api_key = None

        if not api_key:
            raise RuntimeError(
                "Sarvam AI API key is required. Enter it in the plugin settings."
            )

        person = settings.get("person", "").strip() or DEFAULT_PERSON

        try:
            from sarvamai import SarvamAI
        except ImportError:
            raise RuntimeError(
                "The 'sarvamai' Python package is not installed in the InkyPi environment."
            )

        try:
            client = SarvamAI(api_subscription_key=api_key)
            response = client.chat.completions(
                model="sarvam-105b",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Give me one famous quote by {person}.",
                    },
                ],
            )
            content = response.choices[0].message.content
        except Exception as e:
            raise RuntimeError(f"Failed to fetch quote from Sarvam AI: {e}")

        quote, author = self._parse_response(content, person)

        dimensions = device_config.get_resolution()
        try:
            if device_config.get_config("orientation") == "vertical":
                dimensions = dimensions[::-1]
        except Exception:
            pass

        template_params = {
            "quote": quote,
            "author": author,
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

        return image

    @staticmethod
    def _parse_response(content, fallback_author):
        if not content:
            raise RuntimeError("Empty response from Sarvam AI.")

        text = content.strip()
        text = re.sub(r"^```(json)?", "", text.strip())
        text = re.sub(r"```$", "", text.strip())
        text = text.strip()

        try:
            data = json.loads(text)
            quote = str(data.get("quote", "")).strip()
            author = str(data.get("author", "")).strip()
            if quote:
                return quote, author or fallback_author
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

        # Fallback: model didn't return clean JSON, use raw text as the quote
        cleaned = text.strip().strip('"')
        if not cleaned:
            raise RuntimeError("Could not parse a quote from Sarvam AI response.")
        return cleaned, fallback_author
