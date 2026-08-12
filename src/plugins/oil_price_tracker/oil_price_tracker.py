import logging
from datetime import datetime

import requests

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)

API_URL = "https://api.oilpriceapi.com/v1/prices/latest"

OIL_CODES = {
    "BRENT_CRUDE_USD": "Brent Crude",
    "WTI_USD": "WTI Crude",
    "NATURAL_GAS_USD": "Natural Gas",
}


class OilPriceTracker(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["oil_codes"] = OIL_CODES
        template_params["style_settings"] = False
        return template_params

    def generate_image(self, settings, device_config):
        api_key = settings.get("apiKey", "").strip()
        if not api_key:
            # fall back to .env if user stored it there instead
            try:
                api_key = device_config.load_env_key("OIL_PRICE_API_KEY")
            except Exception:
                api_key = None

        if not api_key:
            raise RuntimeError(
                "Oil Price API key is required. Enter it in the plugin settings."
            )

        show_all = settings.get("showAll") in ("true", "on", True, "True")

        codes_to_fetch = list(OIL_CODES.keys()) if show_all else [settings.get("oilCode", "BRENT_CRUDE_USD")]

        cards = []
        for code in codes_to_fetch:
            cards.append(self._fetch_price(api_key, code))

        dimensions = device_config.get_resolution()
        try:
            if device_config.get_config("orientation") == "vertical":
                dimensions = dimensions[::-1]
        except Exception:
            pass

        template_params = {
            "cards": cards,
            "multi": show_all,
            "plugin_settings": settings,
        }

        image = self.render_image(
            dimensions,
            "oil_price_tracker.html",
            "oil_price_tracker.css",
            template_params
        )

        if not image:
            raise RuntimeError("Failed to render oil price image.")

        return image

    def _fetch_price(self, api_key, oil_code):
        try:
            response = requests.get(
                API_URL,
                params={"by_code": oil_code},
                headers={"Authorization": f"Token {api_key}"},
                timeout=10,
            )
            response.raise_for_status()
        except requests.exceptions.Timeout:
            raise RuntimeError("Oil Price API request timed out.")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status == 401:
                raise RuntimeError("Oil Price API rejected the API key (401 Unauthorized).")
            raise RuntimeError(f"Oil Price API returned an error (HTTP {status}).")
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Failed to reach Oil Price API: {e}")

        try:
            data = response.json()["data"]
            price = float(data["price"])
            code = data.get("code", oil_code)
            created_at = data.get("created_at", "")
        except (KeyError, ValueError, TypeError) as e:
            raise RuntimeError(f"Unexpected response from Oil Price API: {e}")

        return {
            "label": OIL_CODES.get(code, code),
            "price": f"{price:,.2f}",
            "updated": self._format_timestamp(created_at),
        }

    @staticmethod
    def _format_timestamp(created_at):
        if not created_at:
            return ""
        try:
            cleaned = created_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            return dt.strftime("%b %d, %Y %I:%M %p")
        except ValueError:
            return created_at
