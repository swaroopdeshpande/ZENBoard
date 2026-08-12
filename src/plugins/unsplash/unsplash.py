from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_loader import _is_low_resource_device
from utils.http_client import get_http_session
from PIL import Image
import logging
import random

logger = logging.getLogger(__name__)

class Unsplash(BasePlugin):
    def generate_image(self, settings, device_config):
        logger.info("=== Unsplash Plugin: Starting image generation ===")

        access_key = device_config.load_env_key("UNSPLASH_ACCESS_KEY")
        if not access_key:
            logger.error("Unsplash Access Key not found in environment")
            raise RuntimeError("'Unsplash Access Key' not found.")

        search_query = settings.get('search_query')
        collections = settings.get('collections')
        content_filter = settings.get('content_filter', 'low')
        color = settings.get('color')
        orientation = settings.get('orientation')

        # Automatically determine image size based on device capabilities
        is_low_resource = _is_low_resource_device()
        image_size = 'regular' if is_low_resource else 'full'
        logger.info(f"Device type: {'low-resource' if is_low_resource else 'standard'}, using image size: '{image_size}'")

        logger.info(f"Settings: image_size='{image_size}', content_filter='{content_filter}'")
        if search_query:
            logger.info(f"Search query: '{search_query}'")
        if collections:
            logger.info(f"Collections: {collections}")
        if color:
            logger.debug(f"Color filter: {color}")
        if orientation:
            logger.debug(f"Orientation: {orientation}")

        params = {
            'client_id': access_key,
            'content_filter': content_filter,
            'per_page': 100,
        }

        if search_query:
            url = f"https://api.unsplash.com/search/photos"
            params['query'] = search_query
            logger.debug(f"Using search endpoint: {url}")
        else:
            url = f"https://api.unsplash.com/photos/random"
            logger.debug(f"Using random photo endpoint: {url}")

        if collections:
            params['collections'] = collections
        if color:
            params['color'] = color
        if orientation:
            params['orientation'] = orientation

        try:
            logger.debug("Fetching image from Unsplash API...")
            session = get_http_session()
            response = session.get(url, params=params)
            response.raise_for_status()
            data = response.json()

            if search_query:
                results = data.get("results")
                if not results:
                    logger.warning(f"No images found for search query: '{search_query}'")
                    raise RuntimeError("No images found for the given search query.")
                logger.info(f"Found {len(results)} images matching search query")
                # Use selected image size (with automatic downgrade for low-RAM devices)
                selected_photo = random.choice(results)
                image_url = selected_photo["urls"][image_size]
                logger.debug(f"Selected random image from {len(results)} results")
            else:
                # Use selected image size (with automatic downgrade for low-RAM devices)
                image_url = data["urls"][image_size]
                logger.debug("Retrieved random image URL")

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching image from Unsplash API: {e}")
            raise RuntimeError("Failed to fetch image from Unsplash API, please check logs.")
        except (KeyError, IndexError) as e:
            logger.error(f"Error parsing Unsplash API response: {e}")
            raise RuntimeError("Failed to parse Unsplash API response, please check logs.")


        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "vertical":
            dimensions = dimensions[::-1]
            logger.debug(f"Vertical orientation detected, dimensions: {dimensions[0]}x{dimensions[1]}")

        logger.info(f"Fetching image (size: {image_size}): {image_url}")

        # Use adaptive image loader for memory-efficient processing
        image = self.image_loader.from_url(image_url, dimensions, timeout_ms=40000)

        if not image:
            logger.error("Failed to load and process image")
            raise RuntimeError("Failed to load image, please check logs.")

        # Apply display margins (safe area)
        safe_area = self.get_safe_area(device_config)

        # Create white background with display margins
        is_vertical = device_config.get_config("orientation") == "vertical"
        full_w, full_h = (480, 800) if is_vertical else (800, 480)
        background = Image.new('RGB', (full_w, full_h), color='white')

        usable_w = safe_area['usable_width']
        usable_h = safe_area['usable_height']
        start_x = safe_area['start_x']
        start_y = safe_area['start_y']

        # Resize image to fit safe area (maintain aspect ratio)
        image.thumbnail((usable_w, usable_h), Image.Resampling.LANCZOS)

        # Center image within safe area
        x_offset = start_x + (usable_w - image.width) // 2
        y_offset = start_y + (usable_h - image.height) // 2

        # Paste into background
        background.paste(image, (x_offset, y_offset))

        logger.info("=== Unsplash Plugin: Image generation complete ===")
        return background
