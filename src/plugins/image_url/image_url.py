from plugins.base_plugin.base_plugin import BasePlugin
import logging

logger = logging.getLogger(__name__)

class ImageURL(BasePlugin):
    def generate_image(self, settings, device_config):
        logger.info("=== Image URL Plugin: Starting image generation ===")

        url = settings.get('url')
        if not url:
            logger.error("No URL provided in settings")
            raise RuntimeError("URL is required.")

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
            logger.debug(f"Vertical orientation detected, dimensions: {dimensions[0]}x{dimensions[1]}")

        logger.info(f"Fetching image from URL: {url}")
        logger.debug(f"Target dimensions: {dimensions[0]}x{dimensions[1]}")

        # Use adaptive image loader for memory-efficient processing
        image = self.image_loader.from_url(url, dimensions, timeout_ms=40000)

        if not image:
            logger.error("Failed to load image from URL")
            raise RuntimeError("Failed to load image, please check logs.")

        logger.info("=== Image URL Plugin: Image generation complete ===")
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
