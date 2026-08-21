import logging
import os
from utils.app_utils import resolve_path, get_fonts
from utils.image_utils import take_screenshot_html
from utils.image_loader import AdaptiveImageLoader
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path
import asyncio
import base64

logger = logging.getLogger(__name__)

STATIC_DIR = resolve_path("static")
PLUGINS_DIR = resolve_path("plugins")
BASE_PLUGIN_DIR =  os.path.join(PLUGINS_DIR, "base_plugin")
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config")
BASE_PLUGIN_RENDER_DIR = os.path.join(BASE_PLUGIN_DIR, "render")

FRAME_STYLES = [
    {
        "name": "None",
        "icon": "frames/blank.png"
    },
    {
        "name": "Corner",
        "icon": "frames/corner.png"
    },
    {
        "name": "Top and Bottom",
        "icon": "frames/top_and_bottom.png"
    },
    {
        "name": "Rectangle",
        "icon": "frames/rectangle.png"
    }
]

class BasePlugin:
    """Base class for all plugins."""

    # Partial refresh drives the panel in the UC8179's KW mode, which has no
    # red plane at all - R00H bit 4 swaps the second SRAM plane from RED to
    # NEW, so the third colour and the previous-frame history are the same
    # memory. A plugin opts in only if its layout is genuinely black and white.
    # The display layer re-checks the rendered frame for red and falls back to
    # a full refresh regardless, so this flag is a declaration of intent, not
    # the safety mechanism.
    SUPPORTS_PARTIAL_REFRESH = False

    def wants_partial_refresh(self, settings):
        """Should this particular render take the partial path?

        Default is the class flag. Plugins whose frames are only sometimes
        partial-worthy override this - the ereader wants it for page turns but
        not for a font change, where a clean full refresh is the right call.
        """
        return self.SUPPORTS_PARTIAL_REFRESH

    def __init__(self, config, **dependencies):
        self.config = config

        # Initialize adaptive image loader for device-aware image processing
        self.image_loader = AdaptiveImageLoader()

        self.render_dir = self.get_plugin_dir("render")
        if os.path.exists(self.render_dir):
            # instantiate jinja2 env with base plugin and current plugin render directories
            loader = FileSystemLoader([self.render_dir, BASE_PLUGIN_RENDER_DIR])
            self.env = Environment(
                loader=loader,
                autoescape=select_autoescape(['html', 'xml'])
            )

    def generate_image(self, settings, device_config):
        raise NotImplementedError("generate_image must be implemented by subclasses")

    def cleanup(self, settings):
        """Optional cleanup method that plugins can override to delete associated resources.

        Called when a plugin instance is deleted. Plugins should override this to clean up
        any files, external resources, or other data associated with the plugin instance.

        Args:
            settings: The plugin instance's settings dict, which may contain file paths or other resources
        """
        pass  # Default implementation does nothing

    def get_plugin_id(self):
        return self.config.get("id")

    def get_plugin_dir(self, path=None):
        plugin_dir = os.path.join(PLUGINS_DIR, self.get_plugin_id())
        if path:
            plugin_dir = os.path.join(plugin_dir, path)
        return plugin_dir

    def generate_settings_template(self):
        template_params = {"settings_template": "base_plugin/settings.html"}

        settings_path = self.get_plugin_dir("settings.html")
        if Path(settings_path).is_file():
            template_params["settings_template"] = f"{self.get_plugin_id()}/settings.html"

        template_params['frame_styles'] = FRAME_STYLES
        return template_params


    def get_safe_area(self, device_config):
        """Get safe rendering area respecting frame margins for current orientation.

        Returns dict with margins and usable dimensions based on current orientation.

        Args:
            device_config: Device configuration object

        Returns:
            dict with top, bottom, left, right, usable_width, usable_height, start_x, start_y
        """
        orientation = device_config.get_config('orientation')
        margins_config = device_config.get_config('display_margins', {})

        # Default fallback
        if not margins_config:
            return {
                'top': 0, 'bottom': 0, 'left': 0, 'right': 0,
                'usable_width': 800, 'usable_height': 480,
                'start_x': 0, 'start_y': 0,
                'orientation': orientation
            }

        # Get orientation-specific margins
        mode = 'horizontal' if orientation != 'vertical' else 'vertical'
        safe_area = margins_config.get(mode, {}).copy()
        safe_area['orientation'] = orientation
        return safe_area

    @staticmethod
    def _safe_area_css(dimensions):
        """Margins for the current orientation, as CSS custom properties.

        Read from device.json rather than passed in, because render_image has
        no device_config and every caller would otherwise have to remember to
        forward it - which is exactly the kind of per-plugin duty that let the
        old hardcoded margins drift apart.
        """
        import json as _json
        try:
            with open(os.path.join(CONFIG_DIR, "device.json")) as fh:
                m = _json.load(fh).get("display_margins", {})
        except Exception:
            m = {}
        key = "horizontal" if dimensions[0] >= dimensions[1] else "vertical"
        a = m.get(key) or {}
        t, r, b, l = (a.get("top", 0), a.get("right", 0),
                      a.get("bottom", 0), a.get("left", 0))
        return {
            "safe_top": t, "safe_right": r, "safe_bottom": b, "safe_left": l,
            "safe_width": a.get("usable_width", dimensions[0] - l - r),
            "safe_height": a.get("usable_height", dimensions[1] - t - b),
        }

    def render_image(self, dimensions, html_file, css_file=None, template_params={}):
        # load the base plugin and current plugin css files
        css_files = [os.path.join(BASE_PLUGIN_RENDER_DIR, "plugin.css")]
        if css_file:
            plugin_css = os.path.join(self.render_dir, css_file)
            css_files.append(plugin_css)

        template_params["style_sheets"] = css_files
        template_params["width"] = dimensions[0]
        template_params["height"] = dimensions[1]
        template_params["font_faces"] = get_fonts()
        template_params["static_dir"] = STATIC_DIR
        # Never let a plugin override these - they are device state, not style.
        template_params.update(self._safe_area_css(dimensions))

        # load and render the given html template
        template = self.env.get_template(html_file)
        rendered_html = template.render(template_params)

        pil_image = take_screenshot_html(rendered_html, dimensions)
        
        return pil_image
