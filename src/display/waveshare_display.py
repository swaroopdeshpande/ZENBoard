import inspect
import importlib
import logging
from .waveshare_epd import epdconfig
import sys

from display.abstract_display import AbstractDisplay
from PIL import Image, ImageChops
from pathlib import Path
from plugins.plugin_registry import get_plugin_instance

logger = logging.getLogger(__name__)


def split_image_for_bi_color_epd(image):
    """
    Convert image into two 1-bit layers for bi-color (black and red) e-paper displays.
    """
    black = (0, 0, 0)
    white = (255, 255, 255)
    red = (255, 0, 0)

    palette_data = [*black, *white, *red]
    palette_img = Image.new('P', (1, 1))
    palette_img.putpalette(palette_data)

    indexed_img = image.quantize(palette=palette_img, dither=Image.Dither.FLOYDSTEINBERG)
    black_layer = indexed_img.point(lambda p: 0 if p == 0 else 1, mode='1')
    red_layer = indexed_img.point(lambda p: 0 if p == 2 else 1, mode='1')
    return black_layer, red_layer


class WaveshareDisplay(AbstractDisplay):
    """
    Handles Waveshare e-paper display dynamically based on device type.

    This class loads the appropriate display driver dynamically based on the 
    `display_type` specified in the device configuration, allowing support for 
    multiple Waveshare EPD models.  

    The module drivers are in display.waveshare_epd.
    """

    def initialize_display(self):
        
        """
        Initializes the Waveshare display device.

        Retrieves the display type from the device configuration and dynamically 
        loads the corresponding Waveshare EPD driver from display.waveshare_epd.

        Raises:
            ValueError: If `display_type` is missing or the specified module is 
                        not found.
        """
        
        logger.info("Initializing Waveshare display")

        # get the device type which should be the model number of the device.
        display_type = self.device_config.get_config("display_type")  
        logger.info(f"Loading EPD display for {display_type} display")

        if not display_type:
            raise ValueError("Waveshare driver but 'display_type' not specified in configuration.")

        # Construct module path dynamically - e.g. "display.waveshare_epd.epd7in3e"
        module_name = f"display.waveshare_epd.{display_type}" 

        # Workaround for some Waveshare drivers using 'import epdconfig' causing import errors
        epd_dir = Path(__file__).parent / "waveshare_epd"
        if str(epd_dir) not in sys.path:
            sys.path.insert(0, str(epd_dir))

        try:
            # Dynamically load module
            epd_module = importlib.import_module(module_name)  
            self.epd_display = epd_module.EPD()
            # Workaround for init functions with inconsistent casing
            self.epd_display_init = getattr(self.epd_display, "Init", getattr(self.epd_display, "init", None))

            if not callable(self.epd_display_init):
                raise AttributeError("No Init/init method found")

            self.epd_display_init()

            display_args_spec = inspect.getfullargspec(self.epd_display.display)
        except ModuleNotFoundError:
            raise ValueError(f"Unsupported Waveshare display type: {display_type}")
        except AttributeError:
            raise ValueError(f"Display does not support required methods: {display_type}")

        self.bi_color_display = len(display_args_spec.args) > 2

        # update the resolution directly from the loaded device context
        if not self.device_config.get_config("resolution"):
            w, h = int(self.epd_display.width), int(self.epd_display.height)
            resolution = [w, h] if w >= h else [h, w]
            self.device_config.update_value(
                "resolution",
                resolution,
                write=True)


    # Waveshare's manual: "you cannot refresh them with the partial refresh
    # mode all the time. After refreshing partially several times, you need to
    # fully refresh EPD once. Otherwise, the display effect will be abnormal."
    # Their FAQ puts a number on it - clear the screen after 5 rounds of
    # partial refreshing - so every 5th turn goes through a full refresh.
    PARTIAL_BEFORE_FULL = 5
    _partial_count = 0
    _last_buf = None

    @staticmethod
    def _has_red(image, threshold=60):
        """True if any pixel is meaningfully red rather than grey.

        Compares the red channel against the brighter of green and blue, so
        greys and near-whites (where all three track together) score zero and
        only genuinely red ink trips it. Runs in C over the whole frame; the
        cost is a few milliseconds against a 1.4s refresh.
        """
        try:
            r, g, b = image.convert("RGB").split()
            excess = ImageChops.subtract(r, ImageChops.lighter(g, b))
            return excess.getextrema()[1] > threshold
        except Exception:
            # Cannot tell - assume red and take the safe path.
            return True

    def _partial_frame(self, prev_buf, new_buf, w, h):
        """One partial refresh over the full screen, with an honest prior frame.

        The driver's display_Partial() cannot be used as-is. It keys off partFlag,
        which init_part() resets to 0, so the first partial after every init
        writes the 0x10 plane as solid 0xff - it tells the panel the glass was
        blank. The panel drives each pixel from its 0x10 -> 0x13 transition, so
        under that lie a pixel that is currently black and should turn white
        reads as white -> white and is never driven. The old text stays on the
        glass. That is the ghosting, and no full-refresh budget can fix it,
        because every turn re-tells the same lie.

        Here 0x10 carries the frame actually on the panel. Both planes use the
        PIL "1" convention (1 = white), which is what the partial path wants -
        unlike the full display() path, where 0x13 is the red plane.
        """
        epd = self.epd_display
        epd.send_command(0x91)                      # enter partial mode
        epd.send_command(0x90)                      # window: full screen
        for v in (0, 0, (w - 1) // 256, (w - 1) % 256,
                  0, 0, (h - 1) // 256, (h - 1) % 256):
            epd.send_data(v)
        epd.send_data(0x01)

        epd.send_command(0x10)                      # what is on the glass now
        epd.send_data2(bytearray(prev_buf))
        epd.send_command(0x13)                      # what should be there next
        epd.send_data2(bytearray(new_buf))

        epd.send_command(0x12)
        epdconfig.delay_ms(100)
        epd.ReadBusy()

    def display_image_partial(self, image):
        if not image:
            raise ValueError("No image provided.")
        if not self.bi_color_display or not hasattr(self.epd_display, "display_Partial"):
            logger.info("Partial refresh unavailable on this panel; full refresh.")
            self.display_image(image)
            return

        # Partial refresh cannot carry red - not a library limitation, a
        # controller one. The UC8179 has two SRAM planes and R00H bit 4 (KW/R)
        # decides what they mean. init() sends PSR 0x0F (bit 4 = 0, KWR): plane
        # 0x10 holds K/W, plane 0x13 holds RED. init_part() sends PSR 0x1F
        # (bit 4 = 1, KW): plane 0x10 holds OLD, plane 0x13 holds NEW. The red
        # channel and the previous-frame history are the same physical plane, so
        # a differential refresh and a third colour are mutually exclusive.
        #
        # Nothing enforces that in software, and the failure is silent: red would
        # go through convert("1") as black, print black for the whole partial
        # run, then snap back to red at the next full refresh. So refuse.
        if self._has_red(image):
            logger.info("Image contains red; partial refresh is black/white only "
                        "(UC8179 KW mode). Full refresh.")
            self._partial_count = 0
            self.display_image(image)
            return

        w, h = self.epd_display.width, self.epd_display.height
        if image.size != (w, h):
            image = image.resize((w, h))
        new_buf = image.convert("1").tobytes()

        self._partial_count += 1
        # A differential refresh is only as good as its reference. Without one -
        # after a restart, or once the budget is spent - take a full refresh,
        # which both clears accumulated ghosting and re-establishes the frame.
        if self._last_buf is None or self._partial_count > self.PARTIAL_BEFORE_FULL:
            logger.info("Full refresh: %s.",
                        "no known previous frame" if self._last_buf is None
                        else "partial budget spent (%d)" % self.PARTIAL_BEFORE_FULL)
            self._partial_count = 0
            self.display_image(image)
            return

        logger.info("Partial refresh %d/%d", self._partial_count, self.PARTIAL_BEFORE_FULL)
        self.epd_display.init_part()
        self._partial_frame(self._last_buf, new_buf, w, h)
        self.epd_display.sleep()
        self._last_buf = new_buf

    def display_image(self, image, image_settings=[]):
        
        """
        Displays an image on the Waveshare display.

        The image has been processed by adjusting orientation, resizing, and converting it
        into the buffer format required for e-paper rendering.

        Args:
            image (PIL.Image): The image to be displayed.
            image_settings (list, optional): Additional settings to modify image rendering.

        Raises:
            ValueError: If no image is provided.
        """

        logger.info("Displaying image to Waveshare display.")
        if not image:
            raise ValueError(f"No image provided.")

        # Assume device was in sleep mode.
        self.epd_display_init()

        # Clear residual pixels before updating the image.
        self.epd_display.Clear()

        # Display the image on the WS display.
        if not self.bi_color_display:
            self.epd_display.display(self.epd_display.getbuffer(image))
        else:
            black_layer, red_layer = split_image_for_bi_color_epd(image)

            self.epd_display.display(
                self.epd_display.getbuffer(black_layer),
                self.epd_display.getbuffer(red_layer),
            )

        # Whatever a later partial refresh diffs against, it diffs against this.
        # The 0x10 plane physically holds the black/white layer, so that - not
        # the composite - is the reference.
        try:
            ref = black_layer if self.bi_color_display else image
            self._last_buf = ref.convert("1").tobytes()
        except Exception:
            self._last_buf = None

        # Put device into low power mode (EPD displays maintain image when powered off)
        logger.info("Putting Waveshare display into sleep mode for power saving.")
        self.epd_display.sleep()
