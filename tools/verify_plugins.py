#!/usr/bin/env python3
"""Render every plugin in both orientations and report border violations.

Checks two different failures, which are not the same thing:

  bleed   - ink inside the calibrated dead zone, i.e. painting under the
            wooden mount. Measured directly.
  clipped - ink hard against the inner edge of the safe area, which on a
            fixed-size layout means content ran out of room and overflow:hidden
            cut it. A layout that fits leaves a little air at its edges.

Renders only. Nothing is sent to the panel.
"""
import importlib
import json
import os
import sys
import traceback

sys.path.insert(0, "/usr/local/inkypi/src")
import numpy as np
from PIL import Image
from config import Config
from utils.image_utils import stem_darken

OUT = "/tmp/verify"
CFG = "/usr/local/inkypi/src/config/device.json"
PLUGIN_DIR = "/usr/local/inkypi/src/plugins"


def saved_settings(dev, pid):
    for pl in dev.get("playlist_config", {}).get("playlists", []):
        for it in pl.get("plugins", []):
            if it["plugin_id"] == pid:
                return dict(it.get("plugin_settings") or {})
    return {}


def analyse(img, margins):
    a = np.array(stem_darken(img).convert("L"))
    h, w = a.shape
    t, r, b, l = margins["top"], margins["right"], margins["bottom"], margins["left"]
    bleed = max(
        (a[:t] < 128).mean() if t else 0,
        (a[-b:] < 128).mean() if b else 0,
        (a[:, :l] < 128).mean() if l else 0,
        (a[:, -r:] < 128).mean() if r else 0,
    ) * 100
    inner = a[t:h - b, l:w - r]
    edge = max(
        (inner[0] < 128).mean(), (inner[-1] < 128).mean(),
        (inner[:, 0] < 128).mean(), (inner[:, -1] < 128).mean(),
    ) * 100
    return bleed, edge, (a < 128).mean() * 100


def main():
    os.makedirs(OUT, exist_ok=True)
    dev = json.load(open(CFG))
    cfg = Config()
    only = sys.argv[1:] or None

    plugins = []
    for p in sorted(os.listdir(PLUGIN_DIR)):
        info = os.path.join(PLUGIN_DIR, p, "plugin-info.json")
        if p == "base_plugin" or not os.path.exists(info):
            continue
        if only and p not in only:
            continue
        plugins.append((p, json.load(open(info))))

    print(f"{'plugin':24s} {'orient':9s} {'bleed':>7s} {'edge':>7s} {'ink':>7s}  verdict")
    print("-" * 74)
    for pid, info in plugins:
        st = saved_settings(dev, pid)
        for orient in ("horizontal", "vertical"):
            m = dev["display_margins"][orient]
            dims = tuple(m["resolution"])
            st2 = dict(st)
            st2["displayMode"] = "landscape" if orient == "horizontal" else "portrait"
            saved_orient = dev.get("orientation")
            try:
                dev_cfg = cfg
                dev_cfg.update_value("orientation",
                                     "horizontal" if orient == "horizontal" else "vertical")
                mod = importlib.import_module(f"plugins.{pid}.{pid}")
                cls = getattr(mod, info["class"])
                img = cls({"id": pid}).generate_image(st2, dev_cfg)
                if img.size != dims:
                    img = img.resize(dims)
                bleed, edge, ink = analyse(img, m)
                verdict = "OK"
                if bleed > 0.5:
                    verdict = "BLEEDS past border"
                elif edge > 8:
                    verdict = "likely CLIPPED"
                elif ink < 0.5:
                    verdict = "blank?"
                img.save(f"{OUT}/{pid}_{orient[:4]}.png")
                print(f"{pid:24s} {orient:9s} {bleed:6.1f}% {edge:6.1f}% {ink:6.1f}%  {verdict}")
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                print(f"{pid:24s} {orient:9s} {'-':>7s} {'-':>7s} {'-':>7s}  FAIL {msg[:38]}")
            finally:
                if saved_orient is not None:
                    cfg.update_value("orientation", saved_orient)


if __name__ == "__main__":
    main()
