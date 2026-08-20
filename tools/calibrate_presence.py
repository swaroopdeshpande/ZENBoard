#!/usr/bin/env python3
"""Map the sensor against real positions in the room.

NEAR_CM and FAR_CM are currently 100 and 400 - picked from a single walk-in
capture, not from this room. If the room is shallower than 4m the proximity
value never reaches zero, so the LEDs never fully idle; if it is deeper, they
saturate long before you arrive. Either way the response feels wrong in a way
that no amount of easing fixes.

This samples the sensor at positions you actually occupy and derives the two
thresholds from the readings.

It also answers a question the earlier capture left open: whether standing
outside the room is distinguishable from standing in it. 24GHz passes through
walls, and if the doorway and the corridor read the same, presence gating can
never be made reliable - worth knowing rather than assuming.

Owns /dev/serial0 exclusively; zenboard_sensor must be stopped first.

Usage:  sudo python3 calibrate_presence.py
"""

import json
import re
import statistics
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial missing: sudo apt install python3-serial")

PORT, BAUD = "/dev/serial0", 115200
RANGE_RE = re.compile(rb"Range\s+(-?\d+)", re.I)
ON_RE = re.compile(rb"\bON\b", re.I)
OFF_RE = re.compile(rb"\bOFF\b", re.I)

SAMPLE_SECONDS = 8      # the module's estimate updates every 1-3s, so a short
                        # sample would capture one or two plateaus at most

POSITIONS = [
    ("frame", "Stand right in front of the frame, arm's length."),
    ("desk", "Sit where you normally sit at the desk."),
    ("mid", "Stand in the middle of the room."),
    ("far", "Stand at the far wall of the room."),
    ("door", "Stand in the doorway."),
    ("outside", "Step OUTSIDE the room and stand still. Close the door if you normally would."),
    ("empty", "Leave the room entirely. Nobody inside."),
]


def sample(ser, seconds):
    vals, pres = [], []
    ser.reset_input_buffer()
    buf = bytearray()
    end = time.time() + seconds
    while time.time() < end:
        chunk = ser.read(256)
        if not chunk:
            continue
        buf.extend(chunk)
        while b"\n" in buf:
            line, _, _r = bytes(buf).partition(b"\n")
            del buf[:len(line) + 1]
            line = line.strip()
            m = RANGE_RE.search(line)
            if m:
                v = int(m.group(1))
                if 0 < v < 10000:
                    vals.append(v)
            if ON_RE.search(line):
                pres.append(1)
            elif OFF_RE.search(line):
                pres.append(0)
    return vals, pres


def main():
    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.05)
    except Exception as e:
        sys.exit(f"cannot open {PORT}: {e}")

    print("\n  PRESENCE CALIBRATION")
    print("  Each position samples for %ds. Stay still while it counts.\n" % SAMPLE_SECONDS)

    results = {}
    for key, prompt in POSITIONS:
        print(f"  >>> {prompt}")
        input("      press Enter when you are in position... ")
        print("      sampling", end="", flush=True)
        vals, pres = sample(ser, SAMPLE_SECONDS)
        print(" done")
        if not vals:
            print("      no readings at all\n")
            results[key] = None
            continue
        med = statistics.median(vals)
        results[key] = {
            "median": med,
            "min": min(vals),
            "max": max(vals),
            "n": len(vals),
            "present_pct": round(100 * sum(pres) / len(pres), 1) if pres else None,
        }
        r = results[key]
        print(f"      median {med:.0f}cm  range {r['min']}-{r['max']}  "
              f"presence {r['present_pct']}%  ({r['n']} readings)\n")

    ser.close()

    print("\n  ---- SUMMARY ----")
    for key, _ in POSITIONS:
        r = results.get(key)
        if r:
            print(f"    {key:8s} median {r['median']:6.0f}cm   presence {r['present_pct']:5.1f}%")
        else:
            print(f"    {key:8s} no data")

    near = results.get("frame") or results.get("desk")
    far = results.get("door") or results.get("far")
    if near and far:
        n = int(near["median"] * 1.3)      # a little above the closest position
        f = int(far["median"])
        print(f"\n  Suggested:  NEAR_CM = {n}   FAR_CM = {f}")
        print("  (NEAR is where the effect should be at full strength,")
        print("   FAR is where it should have faded out entirely.)")

    out = results.get("outside")
    emp = results.get("empty")
    if out and emp:
        print(f"\n  Through-wall check:")
        print(f"    outside the room: presence {out['present_pct']}%, median {out['median']:.0f}cm")
        print(f"    room empty:       presence {emp['present_pct']}%, median {emp['median']:.0f}cm")
        if out["present_pct"] and out["present_pct"] > 50:
            print("    -> the sensor sees you through the wall. Presence gating cannot")
            print("       be made reliable on distance alone; a PIR would fix it.")
        else:
            print("    -> outside reads as absent. Distance gating is viable here.")

    with open("/home/zenith/presence_calibration.json", "w") as fh:
        json.dump(results, fh, indent=2)
    print("\n  saved to /home/zenith/presence_calibration.json\n")


if __name__ == "__main__":
    main()
