#!/usr/bin/env python3
"""
mmWave sensor probe - standalone characterisation tool.

Deliberately independent of InkyPi: run it, learn exactly what the sensor
does, and only then decide what the plugin side should look like. Nothing
here imports or touches the ZenBoard code.

Sensor pin-out being tested: 3V3, GND, RX, TX, OT2 (3.3V logic only).

    3V3  -> Pi pin 1        GND -> Pi pin 6
    OT2  -> GPIO4 (pin 7)   [or 22/23/27, all free]
    TX   -> GPIO15/RXD pin 10      RX -> GPIO14/TXD pin 8   (note the cross)

Modes
-----
  gpio     watch OT2: state, edge timing, hold durations, duty cycle
  scan     try a range of baud rates, report which produces sane framing
  raw      timestamped hex dump of whatever arrives on the UART
  decode   attempt known 24GHz protocols (LD2410 / LD2450 / MR24HPC1)
  both     gpio + decode together, and measure the lag between them

Examples
--------
  python3 zenboard_sensor_probe.py gpio --gpio 4
  python3 zenboard_sensor_probe.py scan --port /dev/serial0
  python3 zenboard_sensor_probe.py raw --port /dev/serial0 --baud 256000
  python3 zenboard_sensor_probe.py decode --port /dev/serial0 --baud 256000
  python3 zenboard_sensor_probe.py both --port /dev/serial0 --baud 256000 --gpio 4

Dependencies
------------
  sudo apt install python3-serial python3-rpi.gpio
     (or: pip install pyserial RPi.GPIO)

UART prerequisites on the Pi - all three, or you will read nothing:
  1. enable_uart=1        in /boot/firmware/config.txt
  2. dtoverlay=disable-bt in /boot/firmware/config.txt   (Zero 2W: moves the
     real PL011 onto GPIO14/15; the mini-UART's baud drifts with CPU clock)
  3. NO console=serial0,... in /boot/firmware/cmdline.txt, or a login shell
     fights the sensor for the port
  ...then reboot. raspi-config > Interface > Serial: login shell NO, hw YES.
"""

import argparse
import collections
import json
import signal
import sys
import time

# Both optional: serial-only use should work on a laptop with a USB-TTL
# adapter, and GPIO-only use should work without pyserial installed.
try:
    import serial
except ImportError:
    serial = None

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None


COMMON_BAUDS = [9600, 19200, 38400, 57600, 115200, 230400, 256000, 460800]

_stop = False


def _sigint(_sig, _frm):
    global _stop
    _stop = True


signal.signal(signal.SIGINT, _sigint)


# ----------------------------------------------------------------------
# OT2 digital output
# ----------------------------------------------------------------------

def watch_gpio(pin, duration, pull, poll_hz=200):
    """Log every edge on OT2 with timing, then summarise.

    Poll rather than use edge callbacks: this also measures how *stable* the
    line is. A sensor that chatters at the threshold shows up as a burst of
    very short holds, which is exactly the thing that would make a naive
    presence service flap.
    """
    if GPIO is None:
        sys.exit("RPi.GPIO not available - run this on the Pi, or use --port only")

    pud = {"down": GPIO.PUD_DOWN, "up": GPIO.PUD_UP, "off": GPIO.PUD_OFF}[pull]
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(pin, GPIO.IN, pull_up_down=pud)

    print(f"Watching GPIO{pin} (pull={pull}) for {duration}s. Ctrl-C to stop early.")
    print("Walk in, stand still, leave the room, and note what you did when.\n")

    start = time.time()
    state = bool(GPIO.input(pin))
    t_state = start
    holds = {True: [], False: []}
    edges = 0
    high_time = 0.0
    print(f"  {0:7.2f}s  initial state: {'HIGH (presence)' if state else 'LOW (clear)'}")

    try:
        while not _stop and (time.time() - start) < duration:
            now = time.time()
            cur = bool(GPIO.input(pin))
            if cur != state:
                held = now - t_state
                holds[state].append(held)
                if state:
                    high_time += held
                edges += 1
                print(f"  {now - start:7.2f}s  -> {'HIGH (presence)' if cur else 'LOW  (clear)':16}"
                      f" after {held:6.2f}s")
                state, t_state = cur, now
            time.sleep(1.0 / poll_hz)
    finally:
        held = time.time() - t_state
        holds[state].append(held)
        if state:
            high_time += held
        GPIO.cleanup()

    elapsed = time.time() - start
    print("\n--- OT2 summary ---")
    print(f"  elapsed        : {elapsed:.1f}s")
    print(f"  edges          : {edges}")
    print(f"  time HIGH      : {high_time:.1f}s  ({100 * high_time / elapsed:.1f}% duty)")
    for st, label in ((True, "HIGH"), (False, "LOW ")):
        hs = holds[st]
        if hs:
            print(f"  {label} holds    : n={len(hs)}  min={min(hs):.2f}s  "
                  f"max={max(hs):.2f}s  mean={sum(hs) / len(hs):.2f}s")
    short = [h for hs in holds.values() for h in hs if h < 0.5]
    if short:
        print(f"  NOTE: {len(short)} hold(s) under 0.5s - the line chatters, so any "
              f"consumer needs debouncing (the ZenBoard service uses 3 confirming reads).")
    return {"edges": edges, "duty": high_time / elapsed if elapsed else 0}


# ----------------------------------------------------------------------
# UART
# ----------------------------------------------------------------------

def _open(port, baud, timeout=0.3):
    if serial is None:
        sys.exit("pyserial not installed - sudo apt install python3-serial")
    return serial.Serial(port, baud, timeout=timeout)


def _score(buf):
    """Crude 'does this look like framed data' score.

    Real framing repeats a header, so a few byte-values dominate and the data
    is not uniformly random. Garbage from a wrong baud rate spreads roughly
    evenly across all 256 values.
    """
    if len(buf) < 32:
        return 0.0
    counts = collections.Counter(buf)
    top = sum(c for _, c in counts.most_common(4))
    printable_run = sum(1 for b in buf if 32 <= b < 127) / len(buf)
    return (top / len(buf)) + 0.3 * printable_run


def scan_bauds(port, per_baud=2.0):
    print(f"Scanning {port} - {per_baud}s per baud rate.\n")
    results = []
    for baud in COMMON_BAUDS:
        try:
            with _open(port, baud) as ser:
                ser.reset_input_buffer()
                end = time.time() + per_baud
                buf = bytearray()
                while time.time() < end:
                    buf.extend(ser.read(256))
        except Exception as e:
            print(f"  {baud:>7}  ERROR {e}")
            continue

        score = _score(buf)
        head = buf[:16].hex(" ")
        results.append((score, baud, len(buf)))
        print(f"  {baud:>7}  {len(buf):5d} bytes  score={score:4.2f}  {head}")

    print()
    if not results or max(r[0] for r in results) == 0:
        print("Nothing readable on any baud rate. Check:")
        print("  - TX/RX crossed over (sensor TX -> Pi RXD)")
        print("  - serial console disabled, enable_uart=1, disable-bt, rebooted")
        print("  - /dev/serial0 exists and you have permission (try sudo, or the dialout group)")
        return None

    best = max(results)
    print(f"Best guess: {best[1]} baud (score {best[0]:.2f}).")
    print(f"Next: python3 {sys.argv[0]} decode --port {port} --baud {best[1]}")
    return best[1]


def raw_dump(port, baud, duration):
    print(f"Raw dump {port} @ {baud} for {duration}s. Ctrl-C to stop.\n")
    start = time.time()
    with _open(port, baud) as ser:
        ser.reset_input_buffer()
        while not _stop and (time.time() - start) < duration:
            chunk = ser.read(64)
            if chunk:
                print(f"  {time.time() - start:7.2f}s  {len(chunk):3d}B  {chunk.hex(' ')}")


# ---- known protocol decoders ----------------------------------------
# Each returns a dict of fields, or None if the buffer is not its format.
# Kept small and independent so an unknown sensor still degrades to raw.

def _u16(b, i):
    return b[i] | (b[i + 1] << 8)


def decode_ld2410(buf):
    """HLK-LD2410: F4F3F2F1 <len:2> <payload> F8F7F6F5. 256000 baud default."""
    h, t = b"\xf4\xf3\xf2\xf1", b"\xf8\xf7\xf6\xf5"
    i = buf.find(h)
    if i < 0:
        return None
    j = buf.find(t, i)
    if j < 0 or j - i < 10:
        return None
    p = buf[i + 6: j]
    if len(p) < 9:
        return None
    states = {0: "no target", 1: "moving", 2: "static", 3: "moving+static"}
    return {
        "protocol": "LD2410",
        "target": states.get(p[1], f"0x{p[1]:02x}"),
        "moving_cm": _u16(p, 2),
        "moving_energy": p[4],
        "static_cm": _u16(p, 5),
        "static_energy": p[7],
        "detect_cm": _u16(p, 8) if len(p) >= 10 else None,
        "_consumed": j + 4,
    }


def decode_ld2450(buf):
    """HLK-LD2450: AA FF 03 00 <3 targets x 8 bytes> 55 CC. Tracks x/y/speed."""
    h, t = b"\xaa\xff\x03\x00", b"\x55\xcc"
    i = buf.find(h)
    if i < 0:
        return None
    j = buf.find(t, i)
    if j < 0 or j - i < 28:
        return None
    p = buf[i + 4: j]

    def signed(v):
        # 15-bit magnitude, top bit is the sign flag (not two's complement)
        return (v & 0x7FFF) * (1 if v & 0x8000 else -1)

    targets = []
    for n in range(3):
        o = n * 8
        if o + 8 > len(p):
            break
        x, y = signed(_u16(p, o)), signed(_u16(p, o + 2))
        if x or y:
            targets.append({
                "x_mm": x, "y_mm": y,
                "speed_cms": signed(_u16(p, o + 4)),
                "dist_mm": int((x * x + y * y) ** 0.5),
            })
    return {"protocol": "LD2450", "targets": targets, "_consumed": j + 2}


def decode_mr24hpc1(buf):
    """Seeed MR24HPC1 / similar: 53 59 <ctrl> <cmd> <len:2> <data> <sum> 54 43."""
    h, t = b"\x53\x59", b"\x54\x43"
    i = buf.find(h)
    if i < 0:
        return None
    j = buf.find(t, i)
    if j < 0 or j - i < 8:
        return None
    p = buf[i + 2: j]
    return {
        "protocol": "MR24HPC1",
        "control": f"0x{p[0]:02x}",
        "command": f"0x{p[1]:02x}",
        "data": p[4:-1].hex(" ") if len(p) > 5 else "",
        "_consumed": j + 2,
    }


DECODERS = [decode_ld2410, decode_ld2450, decode_mr24hpc1]


def decode_stream(port, baud, duration, gpio_pin=None, pull="down"):
    print(f"Decoding {port} @ {baud} for {duration}s. Ctrl-C to stop.\n")

    if gpio_pin is not None:
        if GPIO is None:
            print("  (RPi.GPIO unavailable - UART only)\n")
            gpio_pin = None
        else:
            pud = {"down": GPIO.PUD_DOWN, "up": GPIO.PUD_UP, "off": GPIO.PUD_OFF}[pull]
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(gpio_pin, GPIO.IN, pull_up_down=pud)

    start = time.time()
    buf = bytearray()
    frames = 0
    protocols = collections.Counter()
    dists = []
    last_ot2 = None
    unknown_reported = False

    try:
        with _open(port, baud) as ser:
            ser.reset_input_buffer()
            while not _stop and (time.time() - start) < duration:
                buf.extend(ser.read(256))
                if len(buf) > 4096:
                    del buf[:-2048]

                progressed = True
                while progressed:
                    progressed = False
                    for dec in DECODERS:
                        try:
                            got = dec(buf)
                        except Exception:
                            got = None
                        if not got:
                            continue
                        consumed = got.pop("_consumed", len(buf))
                        del buf[:consumed]
                        frames += 1
                        protocols[got["protocol"]] += 1
                        progressed = True

                        for k in ("moving_cm", "static_cm", "detect_cm"):
                            if got.get(k):
                                dists.append(got[k])
                        for tgt in got.get("targets", []):
                            dists.append(tgt["dist_mm"] / 10.0)

                        line = f"  {time.time() - start:7.2f}s  " + json.dumps(got)
                        if gpio_pin is not None:
                            ot2 = bool(GPIO.input(gpio_pin))
                            if ot2 != last_ot2:
                                line += f"   [OT2 -> {'HIGH' if ot2 else 'LOW'}]"
                                last_ot2 = ot2
                        print(line)
                        break

                if not frames and not unknown_reported and len(buf) > 256:
                    print("  No known protocol matched. First 64 bytes:")
                    print("   ", buf[:64].hex(" "))
                    print("  -> capture with 'raw' mode and we can work the framing out.")
                    unknown_reported = True
    finally:
        if gpio_pin is not None and GPIO is not None:
            GPIO.cleanup()

    elapsed = max(time.time() - start, 0.001)
    print("\n--- UART summary ---")
    print(f"  elapsed   : {elapsed:.1f}s")
    print(f"  frames    : {frames}  ({frames / elapsed:.1f}/s)")
    print(f"  protocols : {dict(protocols) or 'none recognised'}")
    if dists:
        print(f"  distance  : min={min(dists):.0f}  max={max(dists):.0f}  "
              f"mean={sum(dists) / len(dists):.0f}  (cm)")
        print("  Use the min/max to pick sensible near/far thresholds for the")
        print("  distance-aware behaviour, rather than guessing them.")


# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Characterise a 24GHz mmWave sensor before wiring it into ZenBoard.")
    ap.add_argument("mode", choices=["gpio", "scan", "raw", "decode", "both"])
    ap.add_argument("--port", default="/dev/serial0")
    ap.add_argument("--baud", type=int, default=256000)
    ap.add_argument("--gpio", type=int, default=4, help="BCM pin wired to OT2")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--pull", choices=["down", "up", "off"], default="down")
    args = ap.parse_args()

    if args.mode == "gpio":
        watch_gpio(args.gpio, args.duration, args.pull)
    elif args.mode == "scan":
        scan_bauds(args.port)
    elif args.mode == "raw":
        raw_dump(args.port, args.baud, args.duration)
    elif args.mode == "decode":
        decode_stream(args.port, args.baud, args.duration)
    elif args.mode == "both":
        decode_stream(args.port, args.baud, args.duration,
                      gpio_pin=args.gpio, pull=args.pull)


if __name__ == "__main__":
    main()
