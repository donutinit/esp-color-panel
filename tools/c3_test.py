#!/usr/bin/env python3
"""c3_test.py: prints what you do with each encoder and button of the ESP32-C3 (firmware/c3).

Usage:  python3 tools/c3_test.py [PORT]      Ctrl+C to quit
        (default: /dev/ttyACM0 on Linux and the first /dev/cu.usbmodem* on macOS)
"""
import os, sys, termios, time

from c3_input import default_port

PORT = sys.argv[1] if len(sys.argv) > 1 else default_port()
NAME = {"P1": "Encoder 1 push", "P2": "Encoder 2 push", "P3": "Encoder 3 push",
        "B1": "Button 1", "B2": "Button 2", "B3": "Button 3", "B4": "Button 4"}
total = {1: 0, 2: 0, 3: 0}


def open_port():
    fd = os.open(PORT, os.O_RDWR | os.O_NOCTTY)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0                                  # raw: no echo or translations
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[4] = a[5] = termios.B115200
    a[6][termios.VMIN], a[6][termios.VTIME] = 1, 0
    termios.tcsetattr(fd, termios.TCSANOW, a)
    os.write(fd, b"?\n")                                     # ask for the pin states
    return fd


def show(line):
    t = time.strftime("%H:%M:%S")
    p = line.split()
    if not p:
        return
    if p[0] in ("E1", "E2", "E3") and len(p) == 3:
        n, clicks, quarters = int(p[0][1]), int(p[1]), int(p[2][1:])
        if clicks:
            total[n] += clicks
            arrow = "▶ +" if clicks > 0 else "◀ "
            print(f"{t}  Encoder {n}  {arrow}{clicks} click   (quarters {quarters:+d}, total {total[n]:+d})")
        else:
            print(f"{t}  Encoder {n}  · moved {quarters:+d} quarters without reaching a detent")
    elif p[0] in NAME and len(p) == 2:
        print(f"{t}  {NAME[p[0]]}  {'PRESSED' if p[1] == '1' else 'released'}")
    elif p[0] == "PINS":
        print(f"{t}  Pins at rest (1 = open, 0 = shorted to GND):")
        for field in p[1:]:
            name, rest = field.split(":")
            gpio, level = rest.split("=")
            warn = "" if level == "1" or name[0] == "E" else "   ← pressed or shorted to GND?"
            print(f"            {name:4} GPIO{gpio:>2} = {level}{warn}")
        print("          Turn and press whatever you like:")
    else:
        print(f"{t}  {line}")


def main():
    print(f"Listening on {PORT}  (Ctrl+C to quit)")
    while True:
        try:
            fd = open_port()
        except OSError as e:
            print(f"  can't open {PORT}: {e.strerror}; retrying…")
            time.sleep(1)
            continue
        buf = b""
        try:
            while True:
                d = os.read(fd, 256)
                if not d:
                    raise OSError("closed")
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    show(line.decode(errors="replace").strip())
        except OSError:
            print("  disconnected; waiting for it to come back…")
            os.close(fd)
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
