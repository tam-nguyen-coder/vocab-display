#!/usr/bin/env python3
"""Pull the T-Display's framebuffer over serial and save it as a PNG.

Needs the debug-screenshot build:

    pio run -e debug-screenshot -t upload
    python3 tools/screenshot.py /dev/cu.usbserial-XXXX 'card-{}.png' --sides front,back

Commands the debug build understands: f/b select the side, n moves to the next card,
q reports state and free heap, s renders and dumps the framebuffer as RGB565 hex.
"""
import argparse
import sys
import time

import serial
from PIL import Image


def rgb565_to_rgb888(value):
    # No byte swap: TFT_eSprite::readPixel already unswaps, so swapping again here turns
    # amber into magenta and the grey anti-aliased edges of glyphs into red/blue fringes.
    r, g, b = (value >> 11) & 0x1F, (value >> 5) & 0x3F, value & 0x1F
    # Replicate high bits into low ones so full-scale stays full-scale.
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def open_port(port, baud):
    """Opens without pulsing DTR/RTS, which is wired to the ESP32 reset line."""
    link = serial.Serial()
    link.port, link.baudrate, link.timeout = port, baud, 2
    link.dtr = link.rts = False
    link.open()
    return link


def wait_for_boot(link, budget):
    """Opening the port resets the board anyway on this hardware, so wait for its banner.

    Scans a rolling buffer instead of calling readline(): the ROM chatter at boot arrives
    at a different baud rate as long runs with no newlines, and a readline-based wait
    spends its whole budget blocked on timeouts.
    """
    deadline = time.time() + budget
    seen = ""
    while time.time() < deadline:
        chunk = link.read(4096).decode("utf-8", "replace")
        if chunk:
            seen = (seen + chunk)[-8192:]
            if "[boot] vocab-display" in seen:
                return True
    return False


def grab(link, path):
    link.reset_input_buffer()
    link.write(b"s")

    header = None
    deadline = time.time() + 20
    while time.time() < deadline:
        line = link.readline().decode("utf-8", "replace").strip()
        if line.startswith("<<<SCREEN"):
            header = line.split()
            break
    if not header:
        sys.exit("timed out waiting for the framebuffer -- is the debug build flashed?")

    w, h = int(header[1]), int(header[2])
    image = Image.new("RGB", (w, h))
    pixels = image.load()
    for y in range(h):
        row = link.readline().decode("ascii", "replace").strip()
        if len(row) != w * 4:
            sys.exit(f"row {y} was {len(row)} chars, expected {w * 4}")
        for x in range(w):
            pixels[x, y] = rgb565_to_rgb888(int(row[x * 4:x * 4 + 4], 16))
    image.save(path)
    print(f"{path}  {w}x{h}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("out", help="format string, e.g. 'card-{}.png'")
    ap.add_argument("--sides", default="front,back")
    ap.add_argument("--card", type=int, default=0, help="advance N cards before capturing")
    ap.add_argument("--baud", type=int, default=460800)
    ap.add_argument("--settle", type=int, default=15)
    args = ap.parse_args()

    with open_port(args.port, args.baud) as link:
        time.sleep(0.3)
        if not wait_for_boot(link, args.settle):
            sys.exit(f"no boot banner within {args.settle}s; nothing captured")
        for _ in range(args.card):
            link.write(b"n")
            time.sleep(0.3)
        for side in args.sides.split(","):
            link.write(b"f" if side == "front" else b"b")
            time.sleep(0.6)
            grab(link, args.out.format(side))
