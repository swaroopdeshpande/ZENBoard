#!/usr/bin/env python3
"""Walk the LED strip one pixel at a time so the physical count can be read off.

The configured LED_COUNT is 22, which was assumed rather than measured. This
addresses each index in turn and prints it, so whatever lights up can be
counted by eye. Addressing an index beyond the end of a real strip is harmless -
those writes simply go nowhere, which is itself the signal: the number stops
moving on the wall while the terminal keeps counting.

Nothing else may hold the strip while this runs. Two NeoPixel objects on the
same pin corrupt WS2812 timing and produce garbage on the strip - that has
already happened once on this build. The caller is responsible for stopping
zenboard_led, zenboard_led_mqtt and zenboard_sensor first.

Usage:  sudo python3 led_count.py [--max 24] [--hold 1.5]
"""

import argparse
import signal
import sys
import time

import board
import neopixel

PIN = board.D13          # same pin as zenboard_led.py
ORDER = neopixel.GRB

_stop = False


def _sig(_s, _f):
    global _stop
    _stop = True


signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=24,
                    help="highest index to probe (default 24, above the "
                         "configured 22 so an undercount is visible too)")
    ap.add_argument("--hold", type=float, default=1.5,
                    help="seconds to hold each pixel")
    ap.add_argument("--brightness", type=float, default=0.35,
                    help="0..1, kept low so a close look does not dazzle")
    args = ap.parse_args()

    px = neopixel.NeoPixel(PIN, args.max, brightness=args.brightness,
                           auto_write=False, pixel_order=ORDER)
    try:
        px.fill((0, 0, 0))
        px.show()
        time.sleep(0.5)

        print("\n  PASS 1 - one pixel at a time, white.")
        print("  Watch the strip. The terminal number is the index being driven.\n")
        for i in range(args.max):
            if _stop:
                break
            px.fill((0, 0, 0))
            px[i] = (255, 255, 255)
            px.show()
            print(f"    index {i:2d}   (pixel #{i + 1})", flush=True)
            time.sleep(args.hold)

        if not _stop:
            # Cumulative pass. Counting isolated flashes is error-prone; a
            # growing bar is far easier to read a total off, and the moment it
            # stops growing is the real answer.
            print("\n  PASS 2 - cumulative. Lit count grows by one each step.")
            print("  The last number where the lit length still grows is your total.\n")
            px.fill((0, 0, 0))
            px.show()
            time.sleep(0.5)
            for i in range(args.max):
                if _stop:
                    break
                # red at the head, white behind, so the growing edge is obvious
                for j in range(i):
                    px[j] = (255, 255, 255)
                px[i] = (255, 0, 0)
                px.show()
                print(f"    {i + 1:2d} lit", flush=True)
                time.sleep(args.hold)

        if not _stop:
            print("\n  PASS 3 - all indices on, 3 seconds. Count the lit pixels.\n")
            px.fill((255, 255, 255))
            px.show()
            time.sleep(3)
    finally:
        px.fill((0, 0, 0))
        px.show()
        px.deinit()
        print("\n  strip cleared\n")


if __name__ == "__main__":
    main()
