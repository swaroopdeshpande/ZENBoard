import logging
import time
import requests
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from plugins.base_plugin.base_plugin import BasePlugin

logger = logging.getLogger(__name__)


class LinesPlugin(BasePlugin):
    """Displays time, weather, or week number with minimalist design"""

    def generate_image(self, settings, device_config):
        width, height = self._get_resolution(device_config)
        
        mode = settings.get("mode", "temperature")
        color_scheme = settings.get("color_scheme", "white")
        lat_lon = settings.get("lat_lon", "0,0")
        unit = settings.get("unit", "celsius")
        time_format = settings.get("time_format", "24h")
        custom_text = settings.get("custom_text", "Hello")
        
        # Get display text
        display_text = self._get_display_text(mode, time_format, lat_lon, unit, custom_text)
        
        # Create image
        if color_scheme == "black":
            bg_color = "black"
            text_color = "white"
        else:
            bg_color = "white"
            text_color = "black"
        
        img = Image.new("RGB", (width, height), bg_color)
        draw = ImageDraw.Draw(img)
        
        # Load font
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 120)
        except:
            font = ImageFont.load_default()
        
        # Draw text centered
        bbox = draw.textbbox((0, 0), display_text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (width - text_width) // 2
        y = (height - text_height) // 2
        
        draw.text((x, y), display_text, fill=text_color, font=font)
        
        # Apply display margins (safe area)
        if img:
            safe_area = self.get_safe_area(device_config)
            is_vertical = device_config.get_config("orientation") == "vertical"
            full_w, full_h = (480, 800) if is_vertical else (800, 480)
            background = Image.new('RGB', (full_w, full_h), color='white')

            usable_w = safe_area['usable_width']
            usable_h = safe_area['usable_height']
            start_x = safe_area['start_x']
            start_y = safe_area['start_y']

            # Resize to fit safe area
            if hasattr(img, 'thumbnail'):
                img.thumbnail((usable_w, usable_h), Image.Resampling.LANCZOS)

            # Center in safe area
            x_offset = start_x + (usable_w - img.width) // 2
            y_offset = start_y + (usable_h - img.height) // 2

            # Paste with alpha support
            if hasattr(img, 'mode') and img.mode == 'RGBA':
                background.paste(img, (x_offset, y_offset), img)
            else:
                background.paste(img, (x_offset, y_offset))

            return background
        return img

    def _get_resolution(self, device_config):
        for attr in ("get_resolution",):
            fn = getattr(device_config, attr, None)
            if callable(fn):
                try:
                    dims = fn()
                    if dims and len(dims) == 2:
                        return int(dims[0]), int(dims[1])
                except Exception as e:
                    logger.warning(f"Could not get resolution via {attr}: {e}")

        get_config = getattr(device_config, "get_config", None)
        if callable(get_config):
            try:
                dims = get_config("resolution")
                if dims and len(dims) == 2:
                    return int(dims[0]), int(dims[1])
            except Exception as e:
                logger.warning(f"Could not get resolution via get_config: {e}")

        logger.warning("Falling back to default 800x480 resolution")
        return 800, 480

    def _get_display_text(self, mode, time_format, lat_lon, unit, custom_text):
        """Get text to display based on mode"""
        
        if mode == "time":
            now = datetime.now()
            if time_format == "12h":
                return now.strftime("%I:%M %p")
            else:
                return now.strftime("%H:%M")
        
        elif mode == "weeknumber":
            now = datetime.now()
            week = now.isocalendar()[1]
            return str(week)
        
        elif mode == "custom_text":
            return custom_text
        
        else:  # temperature
            try:
                lat, lon = lat_lon.split(",")
                lat, lon = float(lat.strip()), float(lon.strip())
                
                unit_param = "celsius" if unit == "celsius" else "fahrenheit"
                url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m&temperature_unit={unit_param}&timezone=auto"
                
                resp = requests.get(url, timeout=5)
                data = resp.json()
                temp = data["current"]["temperature_2m"]
                symbol = "°C" if unit == "celsius" else "°F"
                return f"{temp}{symbol}"
            except Exception as e:
                logger.warning(f"Temperature fetch failed: {e}")
                return "N/A"
