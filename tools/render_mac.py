#!/usr/bin/env python3
"""Render plugins to PNG on the Mac. Never touches the panel.

    tools/render_mac.py weather_terminal [more_plugin_ids...]
    tools/render_mac.py weather_terminal --only horizontal

Uses each plugin's real saved settings from device.json, unlike
verify_plugins.py, which overwrites `displayMode` with the orientation and so
renders light/dark plugins in the wrong theme.

Output: renders/<plugin>_<orientation>.png
"""
import importlib, json, os, sys, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("SRC_DIR", os.path.join(ROOT, "src"))

from config import Config

OUT = os.path.join(ROOT, "renders")
CFG = os.path.join(ROOT, "src", "config", "device.json")
PLUGIN_DIR = os.path.join(ROOT, "src", "plugins")


def saved_settings(dev, pid):
    for pl in dev.get("playlist_config", {}).get("playlists", []):
        for it in pl.get("plugins", []):
            if it["plugin_id"] == pid:
                return dict(it.get("plugin_settings") or {})
    return {}


def main():
    argv = sys.argv[1:]
    only = None
    if "--only" in argv:
        i = argv.index("--only")
        only = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]          # drop the flag AND its value
    args = [a for a in argv if not a.startswith("--")]

    os.makedirs(OUT, exist_ok=True)
    dev = json.load(open(CFG))
    cfg = Config()

    if not args:
        sys.exit("usage: render_mac.py <plugin_id> [...] [--only horizontal|vertical]")

    for pid in args:
        info_path = os.path.join(PLUGIN_DIR, pid, "plugin-info.json")
        if not os.path.exists(info_path):
            print(f"  {pid}: no plugin-info.json")
            continue
        info = json.load(open(info_path))
        st = saved_settings(dev, pid)

        for orient in ("horizontal", "vertical"):
            if only and orient != only:
                continue
            m = dev["display_margins"][orient]
            expect = tuple(m["resolution"])
            try:
                cfg.update_value("orientation", orient)
                mod = importlib.import_module(f"plugins.{pid}.{pid}")
                cls = getattr(mod, info["class"])
                img = cls({"id": pid}).generate_image(dict(st), cfg)
                if img.size != expect:
                    print(f"  {pid:22s} {orient:10s} WRONG CANVAS {img.size} != {expect}")
                    continue
                path = os.path.join(OUT, f"{pid}_{orient}.png")
                img.save(path)
                print(f"  {pid:22s} {orient:10s} {img.size[0]}x{img.size[1]}  -> {path}")
            except Exception as e:
                print(f"  {pid:22s} {orient:10s} FAILED: {type(e).__name__}: {e}")
                if os.environ.get("VERBOSE"):
                    traceback.print_exc()


if __name__ == "__main__":
    main()
