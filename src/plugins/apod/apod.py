"""
APOD Plugin for InkyPi
This plugin fetches the Astronomy Picture of the Day (APOD) from NASA's API
and displays it on the InkyPi device. It supports optional manual date selection or random dates.
For the API key, set `NASA_SECRET={API_KEY}` in your .env file.
"""

from plugins.base_plugin.base_plugin import BasePlugin
from PIL import Image
from io import BytesIO
from utils.http_client import get_http_session
import logging
from random import randint
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class Apod(BasePlugin):
    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['api_key'] = {
            "required": True,
            "service": "NASA",
            "expected_key": "NASA_SECRET"
        }
        template_params['style_settings'] = False
        return template_params

    def generate_image(self, settings, device_config):
        logger.info("=== APOD Plugin: Starting image generation ===")

        api_key = device_config.load_env_key("NASA_SECRET")
        if not api_key:
            logger.error("NASA API Key not configured")
            raise RuntimeError("NASA API Key not configured.")

        params = {"api_key": api_key}

        # Determine date to fetch
        if settings.get("randomizeApod") == "true":
            start = datetime(2015, 1, 1)
            end = datetime.today()
            delta_days = (end - start).days
            random_date = start + timedelta(days=randint(0, delta_days))
            params["date"] = random_date.strftime("%Y-%m-%d")
            logger.info(f"Fetching random APOD from date: {params['date']}")
        elif settings.get("customDate"):
            params["date"] = settings["customDate"]
            logger.info(f"Fetching APOD from custom date: {params['date']}")
        else:
            logger.info("Fetching today's APOD")

        logger.debug("Requesting NASA APOD API...")
        session = get_http_session()
        response = session.get("https://api.nasa.gov/planetary/apod", params=params)

        if response.status_code != 200:
            logger.error(f"NASA API error (status {response.status_code}): {response.text}")
            raise RuntimeError("Failed to retrieve NASA APOD.")

        data = response.json()
        logger.debug(f"APOD API response received: {data.get('title', 'No title')}")

        if data.get("media_type") != "image":
            logger.warning(f"APOD media type is '{data.get('media_type')}', not 'image'")
            raise RuntimeError("APOD is not an image today.")

        image_url = data.get("hdurl") or data.get("url")
        logger.info(f"APOD image URL: {image_url}")
        logger.debug(f"Using {'HD URL' if data.get('hdurl') else 'standard URL'}")

        # Get target dimensions
        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
            logger.debug(f"Vertical orientation detected, dimensions: {dimensions[0]}x{dimensions[1]}")

        # Use adaptive image loader for memory-efficient processing
        image = self.image_loader.from_url(image_url, dimensions, timeout_ms=40000)

        if not image:
            logger.error("Failed to load APOD image")
            raise RuntimeError("Failed to load APOD image.")

        logger.info("=== APOD Plugin: Image generation complete ===")
        # Apply display margins (safe area)
        if image:
            safe_area = self.get_safe_area(device_config)
            is_vertical = device_config.get_config("orientation") == "vertical"
            full_w, full_h = (480, 800) if is_vertical else (800, 480)
            background = Image.new('RGB', (full_w, full_h), color='white')

            usable_w = safe_area['usable_width']
            usable_h = safe_area['usable_height']
            start_x = safe_area['start_x']
            start_y = safe_area['start_y']

            # Resize to fit safe area
            if hasattr(image, 'thumbnail'):
                image.thumbnail((usable_w, usable_h), Image.Resampling.LANCZOS)

            # Center in safe area
            x_offset = start_x + (usable_w - image.width) // 2
            y_offset = start_y + (usable_h - image.height) // 2

            # Paste with alpha support
            if hasattr(image, 'mode') and image.mode == 'RGBA':
                background.paste(image, (x_offset, y_offset), image)
            else:
                background.paste(image, (x_offset, y_offset))

            return background
        return image
