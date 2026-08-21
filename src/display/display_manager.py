import fnmatch
import json
import logging

from utils.image_utils import resize_image, change_orientation, apply_image_enhancement
from utils.refresh_stats import increment_daily_refresh_count
from display.mock_display import MockDisplay

logger = logging.getLogger(__name__)

# Try to import hardware displays, but don't fail if they're not available
try:
    from display.inky_display import InkyDisplay
except ImportError:
    logger.info("Inky display not available, hardware support disabled")

try:
    from display.waveshare_display import WaveshareDisplay
except ImportError:
    logger.info("Waveshare display not available, hardware support disabled")

class DisplayManager:

    """Manages the display and rendering of images."""

    def __init__(self, device_config):

        """
        Initializes the display manager and selects the correct display type 
        based on the configuration.

        Args:
            device_config (object): Configuration object containing display settings.

        Raises:
            ValueError: If an unsupported display type is specified.
        """
        
        self.device_config = device_config
     
        display_type = device_config.get_config("display_type", default="inky")

        if display_type == "mock":
            self.display = MockDisplay(device_config)
        elif display_type == "inky":
            self.display = InkyDisplay(device_config)
        elif fnmatch.fnmatch(display_type, "epd*in*"):  
            # derived from waveshare epd - we assume here that will be consistent
            # otherwise we will have to enshring the manufacturer in the 
            # display_type and then have a display_model parameter.  Will leave
            # that for future use if the need arises.
            #
            # see https://github.com/waveshareteam/e-Paper
            self.display = WaveshareDisplay(device_config)
        else:
            raise ValueError(f"Unsupported display type: {display_type}")

    def display_image(self, image, image_settings=[]):
        # Trigger LED flash for full e-ink refresh
        try:
            import requests as _req
            _req.post("http://127.0.0.1/api/led/flash",
                      json={"duration": 0.45}, timeout=1)
        except Exception as e:
            pass
        
        """
        Delegates image rendering to the appropriate display instance.

        Args:
            image (PIL.Image): The image to be displayed.
            image_settings (list, optional): List of settings to modify image rendering.

        Raises:
            ValueError: If no valid display instance is found.
        """

        if not hasattr(self, "display"):
            raise ValueError("No valid display instance initialized.")
        
        # Save partial flags before PIL processing destroys them
        use_partial = getattr(image, '_partial', False)
        use_regions = getattr(image, '_partial_regions', False)
        logger.info(f"Partial refresh flag: {use_partial}, region flag: {use_regions}")

        # Save the image
        logger.info(f"Saving image to {self.device_config.current_image_file}")
        image.save(self.device_config.current_image_file)

        # Resize and adjust orientation
        image = change_orientation(image, self.device_config.get_config("orientation"))
        image = resize_image(image, self.device_config.get_resolution(), image_settings)
        if self.device_config.get_config("inverted_image"): image = image.rotate(180)
        image = apply_image_enhancement(image, self.device_config.get_config("image_settings"))

        # Region partial: refresh only the boxes that changed, leaving the rest
        # of the frame - including its red - untouched.
        if use_regions and hasattr(self.display, 'display_image_regions'):
            logger.info("Using region partial refresh")
            self.display.display_image_regions(image)
            return

        # Use partial refresh if image was flagged
        if use_partial and hasattr(self.display, 'display_image_partial'):
            logger.info("Using partial refresh for page turn")
            self.display.display_image_partial(image)
        else:
            # Only FULL refreshes count toward the daily cycle-count stat -
            # that's what actually wears the panel; partial (page-turn)
            # refreshes are excluded on purpose.
            increment_daily_refresh_count()
            self.display.display_image(image, image_settings)