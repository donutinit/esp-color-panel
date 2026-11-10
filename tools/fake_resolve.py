#!/usr/bin/env python3
"""
fake_resolve.py: plays "Resolve" against the Micro Color Panel (fake or real) over hidraw.

Tests the whole cycle without opening Resolve:
  1. Finds the hidraw with HID_ID 0003:00001EDB:0000DA0F (prefers the one under dummy_hcd).
  2. HIDIOCGRAWINFO: the same check Resolve does in HIDLinuxManager::OnDeviceAdded.
  3. Replays the startup from the ra100 capture: SET F3, OUT 04, OUT 02, GET F1, SET F0A, GET F8, SET F3.
  4. Decodes what the panel sends (0x02 buttons, 0x05 trackballs, 0x06 knobs).

Usage:
  python3 tools/fake_resolve.py                startup + read until Ctrl+C
  python3 tools/fake_resolve.py --seconds 5    startup + read for 5 s
  python3 tools/fake_resolve.py --no-startup   read only

Best with Resolve closed: if both ask for features at once, the replies get crossed.
"""
import argparse
import fcntl
import glob
import os
import select
import struct
import sys
import time

HIDIOCGRAWINFO = 0x80084803
KNOBS = ["Y Lift", "Y Gamma", "Y Gain", "Contrast", "Pivot", "Mid Detail",
         "Color Boost", "Shadows", "Highlights", "Saturation", "Hue", "Lum Mix"]
F1_CAPTURE = bytes.fromhex("01 15 5b 3b fd 02 02 00")
F8_CAPTURE = bytes.fromhex("08 01 63")


def hidiocgfeature(n):
    return (3 << 30) | (n << 16) | (ord("H") << 8) | 0x07


def hidiocsfeature(n):
    return (3 << 30) | (n << 16) | (ord("H") << 8) | 0x06


def find_panel():
    found = []
    for d in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            if "HID_ID=0003:00001EDB:0000DA0F" in open(f"{d}/device/uevent").read():
                found.append(("dummy_hcd" in os.path.realpath(d), "/dev/" + os.path.basename(d)))
        except OSError:
            pass
    if not found:
        sys.exit("No 1edb:da0f hidraw found. Did you run sudo tools/mcp_gadget.sh up?")
    found.sort(reverse=True)
    fake, path = found[0]
    return path, fake


def get_feature(fd, rid, n):
    buf = bytearray([rid] + [0] * (n - 1))
    t = time.monotonic()
    r = fcntl.ioctl(fd, hidiocgfeature(n), buf, True)
    return bytes(buf[:r if r > 0 else n]), time.monotonic() - t


def set_feature(fd, data):
    fcntl.ioctl(fd, hidiocsfeature(len(data)), bytearray(data), True)


def step(desc, fn):
    try:
        r = fn()
        print(f"  ok   {desc}" + (f"  {r}" if r else ""))
        return True
    except OSError as e:
        print(f"  FAIL {desc}: {e}")
        return False


def startup(fd):
    print("Startup as in the capture (t=7.77 s):")
    step("SET Feature 3  03 00 00", lambda: set_feature(fd, b"\x03\x00\x00"))
    step("OUT 0x04       04 00 00 00 00 00 00", lambda: os.write(fd, bytes.fromhex("04 00 00 00 00 00 00")) and None)
    step("OUT 0x02       02 + 8 zeros", lambda: os.write(fd, b"\x02" + bytes(8)) and None)

    def f1():
        d, dt = get_feature(fd, 0x01, 8)
        note = "matches the capture" if d == F1_CAPTURE else f"DIFFERS from the capture ({F1_CAPTURE.hex(' ')})"
        slow = f" · took {dt:.2f} s (fake_panel.py never ran?)" if dt > 1 else ""
        return f"{d.hex(' ')}  ({note}{slow})"
    step("GET Feature 1  (SystemInfo)", f1)
    step("SET Feature 0x0A  0a 01", lambda: set_feature(fd, b"\x0a\x01"))

    def f8():
        d, _ = get_feature(fd, 0x08, 3)
        return f"{d.hex(' ')}  ({'matches the capture' if d == F8_CAPTURE else 'differs: ' + F8_CAPTURE.hex(' ')})"
    step("GET Feature 8  (BatteryStatus)", f8)
    step("SET Feature 3  03 64 64", lambda: set_feature(fd, b"\x03\x64\x64"))


def read_reports(fd, seconds):
    print("\nReading panel reports (Ctrl+C to quit)…")
    totals = [0] * 16
    end = time.monotonic() + seconds if seconds else None
    prev = 0
    while end is None or time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if not r:
            continue
        d = os.read(fd, 128)
        t = time.strftime("%H:%M:%S")
        if d[0] == 0x06:
            v = struct.unpack("<16i", d[1:65])
            nz = [(i, x) for i, x in enumerate(v) if x]
            for i, x in nz:
                totals[i] += x
                name = KNOBS[i] if i < 12 else "?"
                extra = "" if x % 360 == 0 else "  (not a multiple of 360!)"
                print(f"{t} knob {i:2d} {name:<11} {x:+8d} = {x // 360:+d} steps · total {totals[i] // 360:+d}{extra}")
            if len(nz) != 1:
                print(f"{t} 0x06 with {len(nz)} nonzero slots")
            if len(d) != 65:
                print(f"{t} 0x06 was {len(d)} B, expected 65!")
        elif d[0] == 0x02:
            b = int.from_bytes(d[1:9], "little")
            on = [i for i in range(64) if (b >> i) & 1 and not (prev >> i) & 1]
            off = [i for i in range(64) if not (b >> i) & 1 and (prev >> i) & 1]
            print(f"{t} buttons: pressed {on or '-'} · released {off or '-'}")
            prev = b
        elif d[0] == 0x05:
            print(f"{t} trackballs {struct.unpack('<9i', d[1:37])}")
        else:
            print(f"{t} unknown report {d.hex(' ')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-startup", action="store_true")
    ap.add_argument("--seconds", type=float, default=0)
    a = ap.parse_args()

    path, fake = find_panel()
    print(f"{path} ({'fake, on dummy_hcd' if fake else 'looks like a real panel'})")
    try:
        fd = os.open(path, os.O_RDWR)
    except PermissionError:
        sys.exit(f"No permission for {path}: the udev rule with uaccess is missing (mcp_gadget.sh up installs it).")
    info = bytearray(8)
    fcntl.ioctl(fd, HIDIOCGRAWINFO, info, True)
    bus, vid, pid = struct.unpack("<IHH", info)
    ok = bus == 3 and vid == 0x1EDB and pid == 0xDA0F
    print(f"HIDIOCGRAWINFO: bus {bus} vid {vid:04x} pid {pid:04x} ({'what Resolve expects' if ok else 'NOT what Resolve expects'})")
    if not a.no_startup:
        startup(fd)
    try:
        read_reports(fd, a.seconds)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
