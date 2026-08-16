#!/usr/bin/env python3
"""
Waveshare HMMD mmWave Sensor - protocol tool.

Datasheet: https://www.waveshare.com/wiki/HMMD_mmWave_Sensor
  115200 baud, 8N1, little-endian.
  Command frame : FD FC FB FA <len:2> <cmd:2> <value...> 04 03 02 01
                  len counts cmd + value bytes.
  Report frame  : F4 F3 F2 F1 <len:2> <present:1> <dist:2> <16 gates x 2> F8 F7 F6 F5

Three output modes, selected with command 0x0012:
  0x64 normal  - ASCII "ON"/"OFF" plus a distance gate (factory default)
  0x04 report  - binary frames with distance and per-gate energy  <- what we want
  0x00 debug   - raw RDMAP dump, far too much for our purposes

Two settings decide whether the thing is usable indoors:
  max distance gate      - one gate is 70cm, range 0..15, so default reaches
                           well over 10m and will see the whole house
  disappearance delay(s) - how long presence is held after the target leaves

Usage:
  sudo python3 hmmd_tool.py info
  sudo python3 hmmd_tool.py listen --seconds 30
  sudo python3 hmmd_tool.py mode report
  sudo python3 hmmd_tool.py set --gate 4 --delay 15
"""

import argparse
import struct
import sys
import time

try:
    import serial
except ImportError:
    sys.exit("pyserial missing: sudo apt install python3-serial")

PORT_DEFAULT = "/dev/serial0"
BAUD = 115200

CMD_HEAD, CMD_TAIL = b"\xfd\xfc\xfb\xfa", b"\x04\x03\x02\x01"
RPT_HEAD, RPT_TAIL = b"\xf4\xf3\xf2\xf1", b"\xf8\xf7\xf6\xf5"

# Standard HLK-style config gate: the module ignores most commands unless
# configuration mode has been entered first.
CMD_ENABLE_CONF = 0x00FF
CMD_END_CONF = 0x00FE
CMD_VERSION = 0x0000
CMD_READ_PARAM = 0x0008
CMD_WRITE_PARAM = 0x0007
CMD_MODE = 0x0012

PARAM_MAX_GATE = 0x0001
PARAM_DELAY = 0x0004      # disappearance delay, seconds

MODES = {"normal": 0x64, "report": 0x04, "debug": 0x00}


def frame(cmd, value=b""):
    body = struct.pack("<H", cmd) + value
    return CMD_HEAD + struct.pack("<H", len(body)) + body + CMD_TAIL


def txrx(ser, cmd, value=b"", wait=0.35, label=""):
    ser.reset_input_buffer()
    ser.write(frame(cmd, value))
    ser.flush()
    time.sleep(wait)
    resp = ser.read(512)
    if label:
        print(f"  {label:26s} -> {resp.hex(' ') if resp else '(no reply)'}")
    return resp


def with_config(ser, fn):
    """Wrap an operation in enable/end configuration."""
    txrx(ser, CMD_ENABLE_CONF, struct.pack("<H", 1), label="enable config")
    try:
        return fn()
    finally:
        txrx(ser, CMD_END_CONF, label="end config")


def parse_report(buf):
    """Pull one report frame out of buf. Returns (info, bytes_consumed)."""
    i = buf.find(RPT_HEAD)
    if i < 0:
        return None, max(0, len(buf) - 4)
    j = buf.find(RPT_TAIL, i)
    if j < 0:
        return None, i
    p = buf[i + 4: j]
    if len(p) < 5:
        return None, j + 4
    # len:2, present:1, distance:2, then 16 gate energies as uint16
    present = p[2]
    dist = struct.unpack("<H", p[3:5])[0]
    gates = []
    rest = p[5:]
    for g in range(min(16, len(rest) // 2)):
        gates.append(struct.unpack("<H", rest[g * 2:g * 2 + 2])[0])
    return {"present": bool(present), "distance": dist, "gates": gates}, j + 4


def cmd_info(ser):
    print("Reading module identity and settings:")

    def inner():
        txrx(ser, CMD_VERSION, label="firmware version")
        for name, pid in (("max distance gate", PARAM_MAX_GATE),
                          ("disappearance delay", PARAM_DELAY)):
            r = txrx(ser, CMD_READ_PARAM, struct.pack("<H", pid), label=f"read {name}")
            if r and len(r) >= 18:
                try:
                    val = struct.unpack("<I", r[10:14])[0]
                    unit = " gates (%.1f m)" % (val * 0.7) if pid == PARAM_MAX_GATE else " s"
                    print(f"      => {val}{unit}")
                except Exception:
                    pass
    with_config(ser, inner)


def cmd_mode(ser, mode):
    val = MODES[mode]
    print(f"Switching to {mode} mode (0x{val:02x}):")
    with_config(ser, lambda: txrx(
        ser, CMD_MODE, struct.pack("<HI", 0x0000, val), label=f"set {mode}"))


def cmd_set(ser, gate, delay):
    print("Writing parameters:")

    def inner():
        if gate is not None:
            txrx(ser, CMD_WRITE_PARAM,
                 struct.pack("<HI", PARAM_MAX_GATE, gate),
                 label=f"max gate = {gate} ({gate * 0.7:.1f} m)")
        if delay is not None:
            txrx(ser, CMD_WRITE_PARAM,
                 struct.pack("<HI", PARAM_DELAY, delay),
                 label=f"delay = {delay} s")
    with_config(ser, inner)


def cmd_listen(ser, seconds):
    print(f"Listening {seconds}s. Anything the module says, in any format.\n")
    start = time.time()
    buf = bytearray()
    ascii_acc = bytearray()
    reports = 0
    dists = []
    ser.reset_input_buffer()

    while time.time() - start < seconds:
        chunk = ser.read(256)
        if not chunk:
            continue
        buf.extend(chunk)

        while True:
            info, consumed = parse_report(buf)
            if info is None:
                if consumed:
                    del buf[:consumed]
                break
            del buf[:consumed]
            reports += 1
            if info["distance"]:
                dists.append(info["distance"])
            top = max(range(len(info["gates"])), key=lambda g: info["gates"][g]) if info["gates"] else -1
            print(f"  {time.time()-start:6.2f}s  present={info['present']!s:5} "
                  f"dist={info['distance']:5}  strongest gate={top}")

        # anything that is printable is probably normal-mode ASCII
        ascii_acc.extend(c for c in chunk if 32 <= c < 127 or c in (10, 13))
        while b"\n" in ascii_acc:
            line, _, rest = bytes(ascii_acc).partition(b"\n")
            ascii_acc = bytearray(rest)
            line = line.strip()
            if line:
                print(f"  {time.time()-start:6.2f}s  ASCII: {line.decode('ascii','replace')}")

    print(f"\n  report frames: {reports}")
    if dists:
        print(f"  distance: min={min(dists)} max={max(dists)} mean={sum(dists)//len(dists)}")
    if not reports and not dists:
        print("  nothing received")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["info", "listen", "mode", "set", "raw"])
    ap.add_argument("mode_value", nargs="?", choices=list(MODES))
    ap.add_argument("--port", default=PORT_DEFAULT)
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--gate", type=int)
    ap.add_argument("--delay", type=int)
    args = ap.parse_args()

    with serial.Serial(args.port, BAUD, timeout=0.3) as ser:
        if args.action == "info":
            cmd_info(ser)
        elif args.action == "listen":
            cmd_listen(ser, args.seconds)
        elif args.action == "mode":
            if not args.mode_value:
                sys.exit("give a mode: normal | report | debug")
            cmd_mode(ser, args.mode_value)
        elif args.action == "set":
            if args.gate is None and args.delay is None:
                sys.exit("nothing to set: use --gate and/or --delay")
            cmd_set(ser, args.gate, args.delay)
        elif args.action == "raw":
            start = time.time()
            while time.time() - start < args.seconds:
                b = ser.read(128)
                if b:
                    print(f"  {time.time()-start:6.2f}s  {b.hex(' ')}")


if __name__ == "__main__":
    main()
