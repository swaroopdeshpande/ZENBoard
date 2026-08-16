"""
ZenBoard corner mark.

Stamped onto every plugin's output centrally from refresh_task rather than
added to each template: several plugins (ai_image, unsplash, newspaper,
spotify_now_playing) return a PIL image directly and never render HTML, so a
template-level watermark could never cover them.

Artwork
-------
Drop your own file at LOGO_PATH. Anything PIL can open works; transparency is
respected. Optionally add LOGO_PATH_LIGHT for a hand-made version to use on
dark backgrounds - otherwise the light variant is derived automatically by
swapping black and white while leaving red alone.

If neither file exists it falls back to a built-in ensō so the feature still
works out of the box.

Placement
---------
The emptiest of the four corners is chosen per render, and the ink is picked
from that corner's own brightness, so the mark stays visible whether it lands
on blank margin or on top of a photograph.

Everything is snapped to pure black / white / red with a binary alpha. The
panel is tri-colour BWR: soft anti-aliased edges dither into visible noise at
this size, so hard edges look cleaner on the real display.
"""

import logging
import math
import os

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

BLACK = (0, 0, 0)
RED = (212, 0, 0)
WHITE = (255, 255, 255)

STATIC_DIR = "/home/zenith/InkyPi/src/static/images"
LOGO_PATH = os.path.join(STATIC_DIR, "zenboard_logo.png")
LOGO_PATH_LIGHT = os.path.join(STATIC_DIR, "zenboard_logo_light.png")

SS = 6                    # supersampling for the fallback mark
_CACHE = {}               # (size, dark_bg, mtime) -> RGBA


# ----------------------------------------------------------------------
# Colour handling
# ----------------------------------------------------------------------

def _snap(img, dark_bg):
    """Pure colours, binary alpha.

    Reddish pixels stay red - it survives on either ground. Everything else
    resolves to a single ink: white on a dark corner, black on a light one.
    Resizing reintroduces greys every time, so this runs after any resize.
    """
    img = img.convert("RGBA")
    px = img.load()
    ink = WHITE if dark_bg else BLACK
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 110:
                px[x, y] = (0, 0, 0, 0)
            elif r > g + 40 and r > b + 40:
                px[x, y] = RED + (255,)
            else:
                px[x, y] = ink + (255,)
    return img


def _fit(img, size):
    """Scale to fit a size x size box, preserving aspect."""
    w, h = img.size
    if w >= h:
        new = (size, max(1, round(h * size / w)))
    else:
        new = (max(1, round(w * size / h)), size)
    return img.resize(new, Image.LANCZOS)


# ----------------------------------------------------------------------
# Artwork
# ----------------------------------------------------------------------

def _fallback_enso(size, dark_bg):
    """Built-in mark: an ensō left open, with the accent where the brush
    touched down."""
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink = WHITE if dark_bg else BLACK

    stroke = max(int(s * 0.11), SS)
    pad = stroke // 2 + int(s * 0.06)
    box = (pad, pad, s - pad - 1, s - pad - 1)
    START, END = 288, 243

    d.arc(box, start=START, end=END, fill=ink + (255,), width=stroke)

    cx = cy = s / 2
    r = (box[2] - box[0]) / 2

    def at(a):
        a = math.radians(a)
        return cx + r * math.cos(a), cy + r * math.sin(a)

    rr = stroke / 2
    for ang in (START, END):
        ex, ey = at(ang)
        d.ellipse((ex - rr, ey - rr, ex + rr, ey + rr), fill=ink + (255,))

    gx, gy = at(START)
    dot = stroke * 0.78
    d.ellipse((gx - dot, gy - dot, gx + dot, gy + dot), fill=RED + (255,))

    return _snap(img.resize((size, size), Image.LANCZOS), dark_bg)


def get_logo(size, dark_bg):
    path = LOGO_PATH_LIGHT if (dark_bg and os.path.exists(LOGO_PATH_LIGHT)) else LOGO_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        path, mtime = None, 0

    key = (size, dark_bg, path, mtime)
    if key in _CACHE:
        return _CACHE[key]

    if path:
        try:
            art = Image.open(path).convert("RGBA")
            logo = _snap(_fit(art, size), dark_bg)
        except Exception as e:
            logger.warning(f"ZenBoard mark: could not read {path} ({e}), using built-in")
            logo = _fallback_enso(size, dark_bg)
    else:
        logo = _fallback_enso(size, dark_bg)

    _CACHE[key] = logo
    return logo


# ----------------------------------------------------------------------
# Placement
# ----------------------------------------------------------------------

def _corner_stats(image, box):
    """(busyness, mean brightness) for a region.

    Busyness is the fraction of pixels whose brightness is far from the
    region's own median, so a flat area scores near zero whatever its colour,
    while text or detail scores high.
    """
    region = image.crop(box).convert("L").resize((24, 24), Image.BILINEAR)
    vals = list(region.getdata())
    median = sorted(vals)[len(vals) // 2]
    busy = sum(1 for v in vals if abs(v - median) > 40) / len(vals)
    return busy, sum(vals) / len(vals)


def _with_halo(logo, halo_ink):
    """Ring the mark in the opposite ink.

    Choosing one ink from the average brightness is only safe when the area
    behind the mark is uniform. Over mixed content - a photo, a half-dark
    panel - part of the mark would otherwise land on same-coloured pixels and
    disappear. A halo guarantees an edge against both blacks and whites.
    """
    from PIL import ImageFilter

    pad = 3
    w, h = logo.size
    canvas = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    canvas.paste(logo, (pad, pad), logo)

    alpha = canvas.split()[3]
    grown = alpha.filter(ImageFilter.MaxFilter(5))
    # Binary again: MaxFilter on a soft edge would reintroduce partial alpha.
    grown = grown.point(lambda v: 255 if v >= 110 else 0)

    plate = Image.new("RGBA", canvas.size, halo_ink + (255,))
    plate.putalpha(grown)
    return Image.alpha_composite(plate, canvas)


def stamp(image, size=40, margin=12, mixed_threshold=0.08):
    """Composite the mark into the emptiest corner. Returns the image."""
    try:
        if image.mode != "RGB":
            image = image.convert("RGB")
        w, h = image.size
        if w < size * 3 or h < size * 3:
            return image

        pad = size + margin * 2
        boxes = {
            "tl": (0, 0, pad, pad),
            "tr": (w - pad, 0, w, pad),
            "bl": (0, h - pad, pad, h),
            "br": (w - pad, h - pad, w, h),
        }
        scored = {k: _corner_stats(image, b) for k, b in boxes.items()}

        # Density alone cannot tell a heading from a blank margin: on a full
        # dashboard the top-left measured *emptiest* and the mark landed on
        # the title. Top corners carry a penalty so they only win when they
        # are dramatically cleaner, and ties fall to the bottom right - the
        # least likely place for a plugin's own heading or branding.
        TOP_PENALTY = 0.06
        order = ["br", "bl", "tr", "tl"]
        best = min(order, key=lambda k: (
            round(scored[k][0] + (TOP_PENALTY if k in ("tl", "tr") else 0.0), 3),
            order.index(k)))

        bx = boxes[best]
        probe = get_logo(size, dark_bg=False)
        lw, lh = probe.size
        x = bx[0] + margin if best in ("tl", "bl") else bx[2] - margin - lw
        y = bx[1] + margin if best in ("tl", "tr") else bx[3] - margin - lh

        # Judge the ink from the pixels the mark will actually cover, not the
        # whole corner - a bright corner with a dark strip under the logo
        # would otherwise get black on black.
        foot_busy, foot_mean = _corner_stats(image, (int(x), int(y), int(x) + lw, int(y) + lh))
        dark_bg = foot_mean < 128

        logo = get_logo(size, dark_bg=dark_bg)
        if foot_busy > mixed_threshold:
            # Halo is the opposite of the ink, not the same as it.
            logo = _with_halo(logo, BLACK if dark_bg else WHITE)
            x -= 3
            y -= 3

        image.paste(logo, (int(x), int(y)), logo)
        logger.debug(
            f"ZenBoard mark: corner={best} busy={foot_busy:.3f} mean={foot_mean:.0f} "
            f"ink={'white' if dark_bg else 'black'} halo={foot_busy > mixed_threshold}")
        return image
    except Exception as e:
        # A watermark must never be the reason a refresh fails.
        logger.warning(f"ZenBoard mark: skipped ({e})")
        return image
