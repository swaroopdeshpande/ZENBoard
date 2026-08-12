#!/usr/bin/env python3
"""
ZenBoard E-Reader Pre-Cache Script
Runs at scheduled time to pre-cache all books with all settings combinations.
Called by cron or systemd timer based on web UI setting.
"""

import hashlib
import json
import logging
import os
import re
import sys
from pathlib import Path

# Add InkyPi to path
sys.path.insert(0, '/home/zenith/InkyPi/src')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
logger = logging.getLogger("ereader_precache")

BOOKS_DIR = "/home/zenith/InkyPi/books"
CACHE_CONFIG = "/home/zenith/InkyPi/books/.precache_config.json"

FONT_OPTIONS = {
    "serif":      "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "serif-bold": "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "sans":       "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "mono":       "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "liberation": "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
}

FONT_SIZES = [16, 20, 24, 28, 32]
ORIENTATIONS = [False, True]  # landscape, portrait

MARGIN = 40
W, H = 800, 480


def load_config():
    try:
        with open(CACHE_CONFIG) as f:
            return json.load(f)
    except Exception:
        return {
            "enabled": False,
            "time": "02:00",
            "fonts": ["serif"],
            "sizes": [20, 24],
            "orientations": ["landscape"],
        }


def get_books():
    books = []
    for f in sorted(Path(BOOKS_DIR).iterdir()):
        if f.suffix.lower() in (".epub", ".pdf", ".txt"):
            books.append(str(f))
    return books


def clean_orphan_caches(books):
    """Remove cache files for deleted books."""
    existing_books = set(books)
    removed = 0
    for f in Path(BOOKS_DIR).glob(".cache_*.json"):
        try:
            with open(f) as cf:
                data = json.load(cf)
            book_path = data.get("book_path", "")
            if book_path and book_path not in existing_books:
                f.unlink()
                logger.info(f"Removed orphan cache: {f.name}")
                removed += 1
        except Exception:
            pass
    logger.info(f"Cleaned {removed} orphan cache files")


def wrap_text(text, font, max_width):
    words = text.split()
    lines = []
    current_line = []
    for word in words:
        test_line = ' '.join(current_line + [word])
        bbox = font.getbbox(test_line)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(' '.join(current_line))
            current_line = [word]
    if current_line:
        lines.append(' '.join(current_line))
    return lines if lines else [""]


def paginate_text(text, font, font_size, portrait):
    from PIL import ImageFont
    line_height = font_size + 6
    if portrait:
        text_w = H - MARGIN * 2
        lines_per_page = (W - MARGIN * 2 - 40) // line_height
    else:
        text_w = W - MARGIN * 2
        lines_per_page = (H - MARGIN * 2 - 40) // line_height

    paragraphs = text.split('\n')
    all_lines = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            all_lines.append("")
            continue
        wrapped = wrap_text(para, font, text_w)
        all_lines.extend(wrapped)

    pages = []
    for i in range(0, len(all_lines), lines_per_page):
        pages.append('\n'.join(all_lines[i:i + lines_per_page]))
    return pages if pages else ["Empty"]


def extract_text(book_path):
    ext = Path(book_path).suffix.lower()
    try:
        if ext == ".txt":
            with open(book_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        elif ext == ".epub":
            import ebooklib
            from ebooklib import epub
            from html.parser import HTMLParser

            class MLStripper(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.fed = []
                def handle_data(self, d):
                    self.fed.append(d)
                def get_data(self):
                    return ' '.join(self.fed)

            book = epub.read_epub(book_path)
            full_text = []
            for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
                content = item.get_content().decode("utf-8", errors="ignore")
                s = MLStripper()
                s.feed(content)
                text = s.get_data()
                text = re.sub(r'\s+', ' ', text).strip()
                if text:
                    full_text.append(text)
            return "\n\n".join(full_text)
        elif ext == ".pdf":
            import fitz
            doc = fitz.open(book_path)
            return "\n\n".join(page.get_text() for page in doc)
    except Exception as e:
        logger.error(f"Failed to extract text from {book_path}: {e}")
    return ""


def precache_book(book_path, font_name, font_size, portrait):
    from PIL import ImageFont

    cache_key = hashlib.md5(f"{book_path}{font_name}{font_size}{portrait}".encode()).hexdigest()
    cache_file = f"{BOOKS_DIR}/.cache_{cache_key}.json"

    # Check if already cached and valid
    try:
        book_mtime = os.path.getmtime(book_path)
        with open(cache_file) as f:
            cached = json.load(f)
        if cached.get("mtime") == book_mtime:
            logger.info(f"  Already cached: {Path(book_path).name} {font_name} {font_size}px {'P' if portrait else 'L'}")
            return True
    except Exception:
        pass

    logger.info(f"  Caching: {Path(book_path).name} | {font_name} {font_size}px | {'Portrait' if portrait else 'Landscape'}")

    # Load font
    font_path = FONT_OPTIONS.get(font_name, FONT_OPTIONS["serif"])
    try:
        font = ImageFont.truetype(font_path, font_size)
    except Exception:
        font = ImageFont.load_default()

    # Extract and paginate
    text = extract_text(book_path)
    if not text:
        return False

    pages = paginate_text(text, font, font_size, portrait)

    # Save cache
    try:
        with open(cache_file, "w") as f:
            json.dump({
                "mtime": os.path.getmtime(book_path),
                "book_path": book_path,
                "pages": pages
            }, f)
        logger.info(f"  → Cached {len(pages)} pages to {Path(cache_file).name}")
        return True
    except Exception as e:
        logger.error(f"  → Cache save failed: {e}")
        return False


def main():
    config = load_config()
    if not config.get("enabled") and "--force" not in sys.argv:
        logger.info("Pre-cache disabled. Use --force to run anyway.")
        return

    books = get_books()
    logger.info(f"Found {len(books)} books")

    # Clean orphan caches first
    clean_orphan_caches(books)

    # Determine what to cache
    fonts = config.get("fonts", ["serif"])
    sizes = config.get("sizes", [20, 24])
    orientations_cfg = config.get("orientations", ["landscape"])
    orientations = []
    if "landscape" in orientations_cfg:
        orientations.append(False)
    if "portrait" in orientations_cfg:
        orientations.append(True)

    total = len(books) * len(fonts) * len(sizes) * len(orientations)
    done = 0

    for book in books:
        logger.info(f"\nBook: {Path(book).name}")
        for font in fonts:
            for size in sizes:
                for portrait in orientations:
                    precache_book(book, font, size, portrait)
                    done += 1
                    logger.info(f"Progress: {done}/{total}")

    logger.info(f"\nPre-cache complete. {done} combinations cached.")


if __name__ == "__main__":
    main()
