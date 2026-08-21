import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from plugins.base_plugin.base_plugin import BasePlugin
from utils.image_utils import stem_darken

logger = logging.getLogger(__name__)

BOOKS_DIR = "/home/zenith/InkyPi/books"
STATE_FILE = "/home/zenith/InkyPi/books/.ereader_state.json"

# Physical display dimensions (always 800x480 landscape)
W, H = 800, 480
MARGIN = 40
STATUS_BAR_H = 36

FONT_OPTIONS = {
    "serif":      "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "serif-bold": "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "sans":       "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "mono":       "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "liberation": "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
}

BG_OPTIONS = {
    "white":     (255, 255, 255),
    "cream":     (252, 248, 240),
    "lightgrey": (240, 240, 235),
    "black":     (0, 0, 0),
}

TEXT_COLOR = {
    "white":     (0, 0, 0),
    "cream":     (0, 0, 0),
    "lightgrey": (0, 0, 0),
    "black":     (255, 255, 255),
}


class Ereader(BasePlugin):

    def generate_settings_template(self):
        template_params = super().generate_settings_template()
        template_params["style_settings"] = False
        template_params["plugin_settings"] = {}
        template_params["plugin_instance"] = ""
        template_params["refresh_settings"] = {}
        template_params["books"] = self._list_books()
        return template_params

    def generate_image(self, settings, device_config):
        book_file  = settings.get("bookFile", "").strip()
        font_name  = settings.get("fontName", "serif")
        font_size  = int(settings.get("fontSize", "20"))
        bg_name    = settings.get("bgColor", "white")
        action     = settings.get("action", "current")
        target_pg  = int(settings.get("targetPage", "0"))
        portrait   = settings.get("orientation", "landscape") == "portrait"
        partial_ok = settings.get("partialRefresh", "true") == "true"

        # Load reading state
        state = self._load_state(book_file)
        page  = state.get("page", 0)

        # Navigate
        if   action == "next": page += 1
        elif action == "prev": page  = max(0, page - 1)
        elif action == "jump": page  = target_pg

        # Get pages (portrait uses different text width → different cache)
        pages = self._get_pages(book_file, font_name, font_size, portrait)
        if not pages:
            raise RuntimeError(f"Could not parse book: {book_file}")

        page = max(0, min(page, len(pages) - 1))
        state["page"] = page
        self._save_state(book_file, state)

        # Render
        img = self._render_page(
            pages[page], font_name, font_size, bg_name,
            page, len(pages), portrait
        )

        # Tag page turns for partial refresh. The display layer now performs a
        # genuine partial refresh - black/white plane only, roughly 1.4s against
        # 21s, with no flashing - rather than the previous "full refresh minus
        # Clear()", which redrew every pixel and was never actually faster.
        #
        # The 15-turn counter that used to live here is gone. Waveshare put a
        # number on this: clear the screen after 5 rounds of partial refreshing,
        # otherwise "the display effect will be abnormal". That budget belongs to
        # the display layer, which owns the panel and now enforces it; keeping a
        # second, larger counter here would only override it.
        if action in ("next", "prev") and partial_ok:
            img._partial = True

        # Notify LED service of orientation
        try:
            import requests as _req
            _req.post("http://127.0.0.1/api/led/orientation",
                      json={"orientation": "portrait" if portrait else "landscape"}, timeout=1)
        except Exception:
            pass

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

    # ── Book list ──────────────────────────────────────────────────────
    def _list_books(self):
        os.makedirs(BOOKS_DIR, exist_ok=True)
        books = []
        for f in sorted(Path(BOOKS_DIR).iterdir()):
            if f.suffix.lower() in (".epub", ".pdf", ".txt"):
                books.append({"name": f.name, "path": str(f),
                               "size": f"{f.stat().st_size // 1024}KB"})
        return books

    # ── Reading state ──────────────────────────────────────────────────
    def _load_state(self, book_file):
        try:
            with open(STATE_FILE) as f:
                return json.load(f).get(book_file, {"page": 0})
        except Exception:
            return {"page": 0}

    def _save_state(self, book_file, state):
        try:
            all_states = self._load_all_states()
            all_states[book_file] = state
            self._save_all_states(all_states)
        except Exception as e:
            logger.warning(f"State save failed: {e}")

    @staticmethod
    def _load_all_states():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    @staticmethod
    def _save_all_states(all_states):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(all_states, f)
        except Exception as e:
            logger.warning(f"State save failed: {e}")

    # ── Page cache ─────────────────────────────────────────────────────
    def _get_pages(self, book_file, font_name, font_size, portrait):
        if not book_file or not os.path.exists(book_file):
            return ["Book not found.\n\nUpload via web UI."]

        cache_key  = hashlib.md5(f"{book_file}{font_name}{font_size}{portrait}".encode()).hexdigest()
        cache_file = f"{BOOKS_DIR}/.cache_{cache_key}.json"

        try:
            book_mtime = os.path.getmtime(book_file)
            with open(cache_file) as f:
                cached = json.load(f)
            if cached.get("mtime") == book_mtime:
                logger.info(f"Cache hit: {len(cached['pages'])} pages")
                return cached["pages"]
        except Exception as e:
            logger.info(f"Cache miss: {e}")

        pages = self._parse_book(book_file, font_name, font_size, portrait)

        try:
            with open(cache_file, "w") as f:
                json.dump({"mtime": os.path.getmtime(book_file),
                           "book_path": book_file, "pages": pages}, f)
            logger.info(f"Cached {len(pages)} pages")
        except Exception as e:
            logger.warning(f"Cache save failed: {e}")

        return pages

    # ── Book parsing ───────────────────────────────────────────────────
    def _parse_book(self, book_file, font_name, font_size, portrait):
        ext = Path(book_file).suffix.lower()
        try:
            if ext == ".txt":
                with open(book_file, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
            elif ext == ".epub":
                text = self._extract_epub(book_file)
            elif ext == ".pdf":
                text = self._extract_pdf(book_file)
            else:
                return [f"Unsupported format: {ext}"]
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return [f"Error: {e}"]

        return self._paginate(text, font_name, font_size, portrait)

    def _extract_epub(self, book_file):
        import ebooklib
        from ebooklib import epub
        from html.parser import HTMLParser

        class Stripper(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
            def handle_data(self, d):
                self.parts.append(d)
            def get_data(self):
                return " ".join(self.parts)

        book  = epub.read_epub(book_file)
        parts = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            html = item.get_content().decode("utf-8", errors="ignore")
            s = Stripper()
            s.feed(html)
            t = re.sub(r"\s+", " ", s.get_data()).strip()
            if t:
                parts.append(t)
        return "\n\n".join(parts)

    def _extract_pdf(self, book_file):
        import fitz
        doc = fitz.open(book_file)
        try:
            return "\n\n".join(p.get_text() for p in doc)
        finally:
            doc.close()

    # ── Pagination ─────────────────────────────────────────────────────
    def _paginate(self, text, font_name, font_size, portrait):
        """
        Portrait:  canvas is H×W (480×800), rotated 90° to fit display.
                   text_width  = H - 2*MARGIN = 400px
                   lines/page  = (W - 2*MARGIN - STATUS_BAR_H) / line_height
        Landscape: canvas is W×H (800×480).
                   text_width  = W - 2*MARGIN = 720px
                   lines/page  = (H - 2*MARGIN - STATUS_BAR_H) / line_height
        """
        font = self._load_font(font_name, font_size)
        lh   = font_size + 6

        if portrait:
            text_w     = H - MARGIN * 2          # 400
            lines_per  = (W - MARGIN * 2 - STATUS_BAR_H) // lh
        else:
            text_w     = W - MARGIN * 2          # 720
            lines_per  = (H - MARGIN * 2 - STATUS_BAR_H) // lh

        lines_per = max(1, lines_per)

        all_lines = []
        for para in text.split("\n"):
            para = para.strip()
            if not para:
                all_lines.append("")
                continue
            all_lines.extend(self._wrap(para, font, text_w))

        pages = []
        for i in range(0, len(all_lines), lines_per):
            pages.append("\n".join(all_lines[i : i + lines_per]))
        return pages or ["Empty document"]

    def _wrap(self, text, font, max_w):
        """Greedy word wrap, measured per word rather than per prefix.

        The obvious version measures the candidate line - " ".join(cur + [word]) -
        on every word, so a twelve-word line is measured twelve times at growing
        length, and the work is quadratic in line length. Over a whole novel that
        was 62s of pagination on this Pi.

        Instead each word is measured once and the widths are added. getlength()
        returns the advance width, which is the quantity that actually composes
        additively - unlike getbbox(), which returns inked extents and cannot be
        summed meaningfully. Word widths are memoised too: prose reuses the same
        few thousand words constantly, so nearly every lookup after the first
        chapter is a dict hit.
        """
        try:
            cache = self._wcache
        except AttributeError:
            cache = self._wcache = {}
        key = (font.path if hasattr(font, "path") else id(font),
               getattr(font, "size", 0))
        widths = cache.setdefault(key, {})

        def width_of(w):
            v = widths.get(w)
            if v is None:
                v = widths[w] = font.getlength(w)
            return v

        space = width_of(" ")

        # Advance width is what the renderer steps by, but a glyph can ink a
        # couple of pixels past its advance (italic f, some serifs), and the old
        # getbbox() wrap measured that inked extent. Across a whole novel the
        # difference showed up on 5 lines out of 2408, at most 2px - well inside
        # the margin, but shave it so this is strictly no wider than before.
        max_w -= 2

        lines, cur, cur_w = [], [], 0.0
        for word in text.split():
            ww = width_of(word)
            # cur_w already includes the trailing space for each word present
            if cur and cur_w + ww > max_w:
                lines.append(" ".join(cur))
                cur, cur_w = [word], ww + space
            else:
                cur.append(word)
                cur_w += ww + space
        if cur:
            lines.append(" ".join(cur))
        return lines or [""]

    # ── Rendering ──────────────────────────────────────────────────────
    def _render_page(self, text, font_name, font_size, bg_name,
                     page_num, total_pages, portrait):
        bg    = BG_OPTIONS.get(bg_name, (255, 255, 255))
        black = TEXT_COLOR.get(bg_name, (0, 0, 0))
        grey  = (255, 255, 255) if bg_name == "black" else (0, 0, 0)  # was low-contrast gray - dithers into visible noise on this BWR panel

        if portrait:
            # Draw on a 480×800 canvas, then rotate 90° → 800×480
            cw, ch = H, W   # 480 × 800
        else:
            cw, ch = W, H   # 800 × 480

        img  = Image.new("RGB", (cw, ch), bg)
        draw = ImageDraw.Draw(img)
        font = self._load_font(font_name, font_size)
        sfont = self._load_font("sans", 13)
        lh   = font_size + 6

        # Text
        y = MARGIN
        for line in text.split("\n"):
            draw.text((MARGIN, y), line, font=font, fill=black)
            y += lh

        # Status bar
        bar_y = ch - STATUS_BAR_H
        draw.line([(MARGIN, bar_y), (cw - MARGIN, bar_y)], fill=black, width=1)
        draw.text((MARGIN, bar_y + 8),
                  f"Page {page_num + 1} of {total_pages}",
                  font=sfont, fill=grey)

        # Progress bar
        if total_pages > 1:
            bx = MARGIN + 110
            bw = cw - MARGIN * 2 - 120
            prog = int((page_num / (total_pages - 1)) * bw)
            draw.rectangle([(bx, bar_y + 14), (bx + bw, bar_y + 18)], outline=black, width=1)
            draw.rectangle([(bx, bar_y + 14), (bx + prog, bar_y + 18)], fill=black)

        # Rotate for portrait
        if portrait:
            img = img.rotate(90, expand=True)  # 480×800 → 800×480

        return stem_darken(img)

    # ── Font loader ────────────────────────────────────────────────────
    def _load_font(self, font_name, font_size):
        path = FONT_OPTIONS.get(font_name, FONT_OPTIONS["serif"])
        try:
            return ImageFont.truetype(path, font_size)
        except Exception:
            return ImageFont.load_default()
