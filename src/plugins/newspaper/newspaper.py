from plugins.base_plugin.base_plugin import BasePlugin
from datetime import datetime, timedelta
from utils.image_utils import get_image
from PIL import Image
import logging
from plugins.newspaper.constants import NEWSPAPERS
from plugins.newspaper.prajavani import fetch_page_image as fetch_prajavani_page

logger = logging.getLogger(__name__)

FREEDOM_FORUM_URL = "https://cdn.freedomforum.org/dfp/jpg{}/lg/{}.jpg"

# Freedom Forum only carries front pages of US/international titles. Papers
# served by their own e-paper API (currently Prajavani) are handled
# separately - see plugins/newspaper/prajavani.py.
PRAJAVANI_SLUG = "PRAJAVANI"


class Newspaper(BasePlugin):
    def generate_image(self, settings, device_config):
        newspaper_slug = settings.get('newspaperSlug')

        if not newspaper_slug:
            raise RuntimeError("Newspaper input not provided.")
        newspaper_slug = newspaper_slug.upper()

        if newspaper_slug == PRAJAVANI_SLUG:
            image = self._fetch_prajavani(settings, device_config)
        else:
            image = self._fetch_freedom_forum(newspaper_slug, device_config)

        return self._fit_to_display(image, device_config)

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    def _fetch_prajavani(self, settings, device_config):
        """Random page of the current Prajavani edition, front page excluded."""
        safe_area = self.get_safe_area(device_config)
        usable_w = safe_area["usable_width"]
        usable_h = safe_area["usable_height"]

        # A newspaper page is tall and narrow (~1:1.57). Fitted whole into a
        # landscape 800x480 frame it becomes a ~300px sliver with huge white
        # margins either side, so on a landscape panel the default is to crop
        # a band off the top instead - it fills the frame and the headlines
        # are actually legible. Portrait panels can take the whole page.
        landscape = usable_w >= usable_h
        crop_mode = settings.get("prajavaniCrop") or ("top" if landscape else "full")

        top_fraction = None
        if crop_mode == "top":
            # Take exactly the band whose aspect matches the frame, so the
            # result fills it edge to edge with nothing left over.
            page_aspect = 1285 / 2014          # the API's nominal page size
            top_fraction = min(1.0, page_aspect * (usable_h / usable_w))

        image, _meta = fetch_prajavani_page(
            target_height=usable_h,
            top_fraction=top_fraction,
            skip_first_page=settings.get("prajavaniSkipFirst", "true") != "false",
            skip_ads=settings.get("prajavaniSkipAds", "true") != "false",
        )
        return image

    def _fetch_freedom_forum(self, newspaper_slug, device_config):
        today = datetime.today()

        # check the next day, then today, then prior day
        days = [today + timedelta(days=diff) for diff in [1, 0, -1, -2]]

        image = None
        for date in days:
            image_url = FREEDOM_FORUM_URL.format(date.day, newspaper_slug)
            image = get_image(image_url)
            if image:
                logger.info(f"Found {newspaper_slug} front cover for {date.strftime('%Y-%m-%d')}")
                break

        if not image:
            raise RuntimeError("Newspaper front cover not found.")

        # expand height if newspaper is wider than resolution
        img_width, img_height = image.size

        dimensions = device_config.get_resolution()
        if device_config.get_config("orientation") == "horizontal":
            dimensions = dimensions[::-1]

        desired_width, desired_height = dimensions

        img_ratio = img_width / img_height
        desired_ratio = desired_width / desired_height

        if img_ratio < desired_ratio:
            new_height = int((img_width * desired_width) / desired_height)
            new_image = Image.new("RGB", (img_width, new_height), (255, 255, 255))
            new_image.paste(image, (0, 0))
            image = new_image

        return image

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _fit_to_display(self, image, device_config):
        safe_area = self.get_safe_area(device_config)
        is_vertical = device_config.get_config("orientation") == "vertical"
        full_w, full_h = (480, 800) if is_vertical else (800, 480)
        background = Image.new('RGB', (full_w, full_h), color='white')

        usable_w = safe_area['usable_width']
        usable_h = safe_area['usable_height']
        start_x = safe_area['start_x']
        start_y = safe_area['start_y']

        image.thumbnail((usable_w, usable_h), Image.Resampling.LANCZOS)

        x_offset = start_x + (usable_w - image.width) // 2
        y_offset = start_y + (usable_h - image.height) // 2

        if image.mode == 'RGBA':
            background.paste(image, (x_offset, y_offset), image)
        else:
            background.paste(image, (x_offset, y_offset))

        return background

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params['newspapers'] = sorted(NEWSPAPERS, key=lambda n: n['name'])
        return template_params
