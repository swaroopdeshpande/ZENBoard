import inspect
import importlib
import logging
from .waveshare_epd import epdconfig
import sys

from display.abstract_display import AbstractDisplay
import numpy as np
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


    # ── Region partial refresh ─────────────────────────────────────────
    #
    # Partial refresh runs the panel in the UC8179's KW mode, which uses 7 of
    # the 10 waveform groups; the three it drops are the ones that drive the
    # red pigment. Anything driven in KW mode therefore renders black as
    # maroon, and red inside a refreshed area is destroyed outright.
    #
    # The escape is that a partial refresh only drives the window set by R90H.
    # Pixels outside it are never touched, so red elsewhere on the frame
    # survives intact. Confine the window to areas that are black-on-white and
    # the damage is limited to glyph strokes, which at value-text size is not
    # noticeable - verified on the panel before this was written.
    REGION_PARTIAL_BEFORE_FULL = 5

    # Beyond this many windows the per-window overhead stops being worth it and
    # a full refresh is simpler and cleaner.
    REGION_MAX_WINDOWS = 6

    # If this much of the panel changed, the frame is not a value update - it is
    # a different screen. Take a full refresh.
    REGION_MAX_AREA = 0.25

    # Separate boxes closer than this get merged, so adjacent digits do not each
    # become their own window.
    REGION_MERGE_GAP = 28

    _region_count = 0
    _last_image = None

    @staticmethod
    def _runs(flags):
        """Contiguous True runs in a 1-D boolean array, as (start, end) pairs."""
        runs, start = [], None
        for i, v in enumerate(flags):
            if v and start is None:
                start = i
            elif not v and start is not None:
                runs.append((start, i))
                start = None
        if start is not None:
            runs.append((start, len(flags)))
        return runs

    @classmethod
    def _changed_boxes(cls, prev_img, new_img, w, h):
        """Bounding boxes of what changed between two frames, byte-aligned.

        Rows are grouped into bands first, then columns within each band, which
        keeps a line of changed text as one box rather than one box per glyph.
        X is snapped outwards to multiples of 8 because R90H addresses
        horizontal channel *banks* - HRST[9:3] - so a window cannot start or end
        mid-byte.
        """
        a = np.array(prev_img.convert("1"), dtype=bool)
        b = np.array(new_img.convert("1"), dtype=bool)
        diff = a ^ b
        if not diff.any():
            return []

        boxes = []
        for y0, y1 in cls._runs(diff.any(axis=1)):
            band = diff[y0:y1]
            for x0, x1 in cls._runs(band.any(axis=0)):
                boxes.append([x0, y0, x1, y1])

        # merge boxes that are near each other, repeatedly until stable
        merged = True
        while merged and len(boxes) > 1:
            merged = False
            out = []
            for bx in boxes:
                for ob in out:
                    if (bx[0] < ob[2] + cls.REGION_MERGE_GAP and
                            ob[0] < bx[2] + cls.REGION_MERGE_GAP and
                            bx[1] < ob[3] + cls.REGION_MERGE_GAP and
                            ob[1] < bx[3] + cls.REGION_MERGE_GAP):
                        ob[0], ob[1] = min(ob[0], bx[0]), min(ob[1], bx[1])
                        ob[2], ob[3] = max(ob[2], bx[2]), max(ob[3], bx[3])
                        merged = True
                        break
                else:
                    out.append(list(bx))
            boxes = out

        pad = 2
        final = []
        for x0, y0, x1, y1 in boxes:
            x0 = int(max(0, x0 - pad) // 8 * 8)
            x1 = int(min(w, ((x1 + pad) + 7) // 8 * 8))
            y0 = int(max(0, y0 - pad))
            y1 = int(min(h, y1 + pad))
            if x1 > x0 and y1 > y0:
                final.append((x0, y0, x1, y1))
        return final

    @staticmethod
    def _box_has_red(rgb, box, threshold=60):
        x0, y0, x1, y1 = box
        sub = rgb[y0:y1, x0:x1]
        if not sub.size:
            return False
        excess = sub[:, :, 0] - np.maximum(sub[:, :, 1], sub[:, :, 2])
        return bool((excess > threshold).any())

    def display_image_regions(self, image):
        """Refresh only the parts of the frame that changed.

        Falls back to a full refresh whenever that cannot be done safely, which
        is the common case for anything other than a value update.
        """
        if not image:
            raise ValueError("No image provided.")

        w, h = self.epd_display.width, self.epd_display.height
        if image.size != (w, h):
            image = image.resize((w, h))

        prev = self._last_image
        self._region_count += 1

        reason = None
        boxes = []
        if prev is None or prev.size != image.size:
            reason = "no known previous frame"
        elif self._region_count > self.REGION_PARTIAL_BEFORE_FULL:
            reason = "partial budget spent (%d)" % self.REGION_PARTIAL_BEFORE_FULL
        else:
            boxes = self._changed_boxes(prev, image, w, h)
            if not boxes:
                reason = "nothing changed"
            elif len(boxes) > self.REGION_MAX_WINDOWS:
                reason = "%d changed regions, too scattered" % len(boxes)
            else:
                area = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes)
                if area > self.REGION_MAX_AREA * w * h:
                    reason = "%.0f%% of the panel changed" % (area * 100.0 / (w * h))
                else:
                    # Red anywhere in a window would be driven in KW mode and
                    # destroyed. Check both frames: red could be arriving or
                    # leaving.
                    old_rgb = np.array(prev.convert("RGB"), dtype=np.int16)
                    new_rgb = np.array(image.convert("RGB"), dtype=np.int16)
                    for bx in boxes:
                        if self._box_has_red(old_rgb, bx) or self._box_has_red(new_rgb, bx):
                            reason = "a changed region contains red"
                            break

        if reason:
            logger.info("Full refresh: %s.", reason)
            self._region_count = 0
            self.display_image(image)
            return

        logger.info("Region partial %d/%d: %d window(s), %s",
                    self._region_count, self.REGION_PARTIAL_BEFORE_FULL, len(boxes),
                    ", ".join("%dx%d@%d,%d" % (x1 - x0, y1 - y0, x0, y0)
                              for x0, y0, x1, y1 in boxes))

        prev_buf = prev.convert("1").tobytes()
        new_buf = image.convert("1").tobytes()
        stride = w // 8

        def window_bytes(buf, box):
            x0, y0, x1, y1 = box
            out = bytearray()
            for y in range(y0, y1):
                out += buf[y * stride + x0 // 8: y * stride + x1 // 8]
            return out

        epd = self.epd_display
        # One init and one sleep for the whole set: that overhead, not the
        # waveform, is what a small window actually costs.
        epd.init_part()
        epd.send_command(0x91)                       # partial in
        for box in boxes:
            x0, y0, x1, y1 = box
            epd.send_command(0x90)                   # window
            for v in (x0 // 256, x0 % 256, (x1 - 1) // 256, (x1 - 1) % 256,
                      y0 // 256, y0 % 256, (y1 - 1) // 256, (y1 - 1) % 256):
                epd.send_data(int(v))
            epd.send_data(0x01)
            epd.send_command(0x10)                   # OLD - what is on the glass
            epd.send_data2(bytearray(window_bytes(prev_buf, box)))
            epd.send_command(0x13)                   # NEW
            epd.send_data2(bytearray(window_bytes(new_buf, box)))
            epd.send_command(0x12)
            epdconfig.delay_ms(100)
            epd.ReadBusy()
        epd.sleep()

        self._last_image = image.copy()

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

        # Reference frame for any region partial that follows. The budget
        # resets too: a full refresh has cleared whatever the partials left, and
        # this is also how a plugin change starts the next plugin from scratch.
        self._region_count = 0
        try:
            self._last_image = image.copy()
        except Exception:
            self._last_image = None

        # Put device into low power mode (EPD displays maintain image when powered off)
        logger.info("Putting Waveshare display into sleep mode for power saving.")
        self.epd_display.sleep()
