#!/usr/bin/env python3
"""Single-shot border calibration: read all four margins from one render.

The existing calibrate_*.py scripts converge by trial and error, and every
attempt costs a full ~40s repaint plus the flashing invert sequence. This puts
a numbered ladder against each edge instead, so one repaint yields all four
numbers directly.

How to read it: each edge carries lines at 0,2,4..30 px inset, each labelled
with its inset. Find the lowest number you can see completely on that edge -
that is the margin for it. Anything below that number is under the frame.

Usage:
    sudo python3 calibrate_ruler.py            # show the ruler
    sudo python3 calibrate_ruler.py --apply T R B L   # write the numbers
"""

import argparse
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 800, 480
MAX_INSET = 30
STEP = 2
DEVICE_JSON = "/usr/local/inkypi/src/config/device.json"

FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size):
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def build():
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f = _font(13)
    fbig = _font(20)
    RED = (212, 0, 0)

    n = MAX_INSET // STEP + 1
    # Marks are staggered along each edge so no two share a position and the
    # labels cannot collide. Lines run *parallel* to their edge: a line being
    # hidden is exactly the thing being measured.
    span, gap = 16, 21

    for k in range(n):
        inset = k * STEP
        colour = RED if inset % 10 == 0 else (0, 0, 0)
        lbl = str(inset)

        # left / right: vertical lines, staggered down the edge
        y0 = 60 + k * gap
        d.line([(inset, y0), (inset, y0 + span)], fill=colour, width=1)
        d.text((inset + 4, y0 + 1), lbl, font=f, fill=colour)

        d.line([(W - 1 - inset, y0), (W - 1 - inset, y0 + span)], fill=colour, width=1)
        tw = d.textlength(lbl, font=f)
        d.text((W - 1 - inset - 4 - tw, y0 + 1), lbl, font=f, fill=colour)

        # top / bottom: horizontal lines, staggered across the edge
        x0 = 150 + k * gap
        d.line([(x0, inset), (x0 + span, inset)], fill=colour, width=1)
        d.text((x0 + 1, inset + 3), lbl, font=f, fill=colour)

        d.line([(x0, H - 1 - inset), (x0 + span, H - 1 - inset)], fill=colour, width=1)
        d.text((x0 + 1, H - 1 - inset - 17), lbl, font=f, fill=colour)

    lines = [
        "BORDER CALIBRATION",
        "",
        "On each edge, find the LOWEST number",
        "you can see COMPLETELY.",
        "That number is the margin for that edge.",
        "",
        "Red marks are 0, 10, 20, 30.",
        "",
        "Then run:",
        "sudo python3 calibrate_ruler.py --apply T R B L",
    ]
    y = 150
    for i, t in enumerate(lines):
        fo = fbig if i == 0 else f
        tw = d.textlength(t, font=fo)
        d.text(((W - tw) / 2, y), t, font=fo,
               fill=RED if i == 0 else (0, 0, 0))
        y += 30 if i == 0 else 18

    # A box on the true safe area currently configured, for comparison.
    try:
        cfg = json.load(open(DEVICE_JSON))["display_margins"]["horizontal"]
        d.rectangle([cfg["left"], cfg["top"], W - 1 - cfg["right"], H - 1 - cfg["bottom"]],
                    outline=RED, width=2)
        # Sits in the middle, not at the corner - against the top edge it
        # overlapped the very ladder it is meant to be compared against.
        cur = f"current: T{cfg['top']} R{cfg['right']} B{cfg['bottom']} L{cfg['left']}"
        d.text(((W - d.textlength(cur, font=f)) / 2, 400), cur, font=f, fill=RED)
    except Exception:
        pass

    return img


def apply(t, r, b, l):
    with open(DEVICE_JSON) as fh:
        cfg = json.load(fh)
    m = cfg.setdefault("display_margins", {})
    m["horizontal"] = {
        "top": t, "bottom": b, "left": l, "right": r,
        "usable_width": W - l - r, "usable_height": H - t - b,
        "start_x": l, "start_y": t, "resolution": [W, H],
    }
    # Portrait is the same physical frame rotated, so the margins rotate with
    # it. Deriving it here keeps the two from drifting apart, which they did
    # last time they were maintained by hand.
    m["vertical"] = {
        "top": l, "bottom": r, "left": b, "right": t,
        "usable_width": H - b - t, "usable_height": W - l - r,
        "start_x": b, "start_y": l, "resolution": [H, W],
    }
    m["notes"] = "Calibrated with calibrate_ruler.py (single-shot edge ladders)."
    tmp = DEVICE_JSON + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cfg, fh, indent=4)
    os.replace(tmp, DEVICE_JSON)
    print(f"  written: T{t} R{r} B{b} L{l}  ->  usable {W-l-r}x{H-t-b}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", nargs=4, type=int, metavar=("TOP", "RIGHT", "BOTTOM", "LEFT"))
    ap.add_argument("--out", default="/tmp/calibration_ruler.png")
    args = ap.parse_args()

    if args.apply:
        apply(*args.apply)
        print("  restart inkypi for plugins to pick it up:")
        print("    sudo systemctl restart inkypi")
        return

    img = build()
    img.save(args.out)
    print(f"  ruler written to {args.out}")

    sys.path.insert(0, "/usr/local/inkypi/src")
    from display.display_manager import DisplayManager
    from config import Config
    DisplayManager(Config()).display_image(img)
    print("  displayed. Read the lowest fully-visible number on each edge.")


if __name__ == "__main__":
    main()
