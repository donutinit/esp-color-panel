#!/usr/bin/env python3
"""
fake_panel.py: the fake Micro Color Panel's "firmware", running on the computer.

On Linux it talks through /dev/hidgN with the gadget set up by `sudo tools/mcp_gadget.sh up`; on macOS, with the
virtual HID created by macos/mcp_hid_mac (see the README). On both:
  * it preloads the replies to Resolve's GET_REPORTs with the bytes from ra100's capture
    (SystemInfo, BatteryStatus...). They stay in the kernel even after this script exits.
  * it logs what Resolve sends: brightness, session and the key LEDs (decoded).
  * it sends knobs (report 0x06: 16 x int32 LE, one slot per report, multiples of 360) and
    keys (report 0x02: 64-bit bitmap; 0-11 = knob push buttons, 12-51 = the 40 keys).

Usage:
  python3 tools/fake_panel.py                  interactive (keyboard)
  python3 tools/fake_panel.py --map            print the key map and exit
  python3 tools/fake_panel.py --listen         no keyboard: only log what Resolve sends
  python3 tools/fake_panel.py --send 9 +5      5 steps (×360) on knob 9 and exit (12-14: rings)
  python3 tools/fake_panel.py --press 9        press and release knob 9 and exit
  python3 tools/fake_panel.py --button 13      press and release the key at bit 13 (OFFSET) and exit
  python3 tools/fake_panel.py --simulate       the UI without a gadget (sends nothing), to try it out
  python3 tools/fake_panel.py --c3             also read the ESP32-C3 (3 encoders = rings, 4 buttons);
                                               the mapping is in tools/c3_input.py

UI: the status, the selected knob and the key map stay pinned at the top (Tab hides the map); the log goes
below. Keys whose LED Resolve turned on show in green, a latched SHIFT in yellow; the key you just pressed
flashes.

NOT FULLY TESTED: the knobs already moved Resolve; each key's bit comes from ra100 and the capture, and
still has to be confirmed key by key.
"""
import argparse
import curses
import fcntl
import glob
import locale
import os
import re
import select
import struct
import subprocess
import sys
import threading
import time
from collections import deque

FUNC = "/sys/kernel/config/usb_gadget/esp_color_panel/functions/hid.mcp"
# include/uapi/linux/usb/g_hid.h
GADGET_HID_READ_GET_REPORT_ID = 0x80016741       # _IOR('g', 0x41, __u8)
GADGET_HID_WRITE_GET_REPORT = 0x40486742         # _IOW('g', 0x42, struct usb_hidg_report) (72 B)

UNIT = 360                                       # the real panel sends multiples of 360
STEPS = [10, 50, 100, 300, 1000]                 # multiples of UNIT (×100 = 36,000 = one knob "tap")
RING_STEPS = [5, 10, 25, 50, 100]                # the rings are separate: ×25 (= 2 × 4096) feels right
# Measured in Resolve 21.2 (Sat, node 2): it accelerates with speed. A lone report yields ~11 Sat per million
# units; back-to-back reports every 24 ms, ~24. A real 1 s turn in the capture adds up to 0.7–1.3 million.
TICK = 0.008                                     # the real panel reports every ~8 ms
PRESS_TIME = 0.12                                # how long a key stays "pressed"

# Left-to-right order per Blackmagic's manual (confirmed in Resolve: 3, 5 and 9)
KNOBS = ["Y LIFT", "Y GAMMA", "Y GAIN", "CONTRAST", "PIVOT", "MID DETAIL",
         "COLOR BOOST", "SHADOWS", "HIGHLIGHTS", "SATURATION", "HUE", "LUM MIX"]
# Controls 12-14: the rings of the three trackballs (report 0x05 = 9 x int32: X1 Y1 ring1 X2 Y2 ring2 X3 Y3
# ring3). In the capture the rings send ±1-2 x 4096 every 8 ms. Left/center/right order [INFERRED].
RINGS = {12: 2, 13: 5, 14: 8}                    # control → slot in report 0x05
RING_UNIT = 4096
# Confirmed in Resolve 21.2: with OFFSET on and the HDR palette open, the rings drive the Global
# HDR: left = Temp, center = Tint, right = Exp.
CONTROLS = KNOBS + ["LEFT RING (Temp)", "CENTER RING (Tint)", "RIGHT RING (Exp)"]

# The 40 keys: bit in report 0x02 → label from the manual. Each bit comes from ra100's
# davinci_panel_mapping.json (its index − 8) and from the order they were pressed in the capture.
PANEL_KEYS = {
    12: "AUTO COLOR", 13: "OFFSET", 14: "COPY", 15: "PASTE", 16: "UNDO", 17: "REDO",
    18: "DELETE", 19: "RESET", 20: "BYPASS", 21: "DISABLE", 22: "USER", 23: "LOOP",
    24: "SHIFT UP", 25: "SHIFT DOWN",
    26: "PLAY STILL", 27: "WIPE STILL", 28: "GRAB STILL", 29: "H/LITE", 30: "VIEWER",
    31: "CURSOR", 32: "SELECT", 33: "RESET LIFT", 34: "RESET GAMMA", 35: "RESET GAIN",
    36: "ADD NODE", 37: "ADD WINDOW", 38: "ADD KEYFRM", 39: "PREV STILL", 40: "NEXT STILL",
    41: "PREV KEYFRM", 42: "NEXT KEYFRM", 43: "PREV NODE", 44: "NEXT NODE",
    45: "PREV FRAME", 46: "NEXT FRAME", 47: "PREV CLIP", 48: "NEXT CLIP",
    49: "PLAY REVERSE", 50: "PLAY", 51: "STOP",
}
# In the capture, with bit 24 held Resolve sends 04 10 and with 25, 04 20: the modifier's green light.
SHIFTS = {24: 0x10, 25: 0x20}

# Latin American keyboard layout, 4 rows of 10
ROWS = [
    ("row 1-0 · the panel's top row",          "1234567890", [26, 27, 28, 29, 30, 31, 32, 36, 37, 38]),
    ("row Q-P · PREV/NEXT pairs",              "qwertyuiop", [39, 40, 41, 42, 43, 44, 45, 46, 47, 48]),
    ("row A-Ñ · left block",                   "asdfghjklñ", [12, 13, 14, 15, 16, 17, 18, 19, 20, 21]),
    ("row Z-  · USER/LOOP, shifts, resets, transport",
                                               "zxcvbnm,.-", [22, 23, 24, 25, 33, 34, 35, 49, 51, 50]),
]
KEY_TO_BIT = {c: b for _, chars, bits in ROWS for c, b in zip(chars, bits)}
KEY_TO_BIT.update({";": 21, "/": 50})            # the same physical keys on a US layout

# Short labels for the screen
SHORT = ["Y LIFT", "Y GAMMA", "Y GAIN", "CONTRAST", "PIVOT", "MID DET", "C.BOOST", "SHADOWS",
         "HIGHLIGHT", "SAT", "HUE", "LUM MIX", "⊙ TEMP", "⊙ TINT", "⊙ EXP"]
LABEL = dict(PANEL_KEYS)
LABEL.update({49: "◀ PLAY", 50: "▶ PLAY", 51: "■ STOP"})

SHIFT_UP, SHIFT_DOWN = 24, 25
# What each key does with (SHIFT UP, SHIFT DOWN), per the Micro Color Panel manual. None = nothing.
SHIFT_FUNCTIONS = {
    26: ("Split scrn", "Gallery"),         # PLAY STILL: split screen / gallery
    27: ("Wipe mode", None),               # WIPE STILL: cycles through the wipe options
    29: ("H/Lite mode", None),             # H/LITE: cycles through the highlight modes
    30: ("Show clips", "Lightbox"),        # VIEWER
    36: ("Parallel N.", "Layer node"),     # ADD NODE
    37: ("Linear win", "Track win"),       # ADD WINDOW: linear / tracks the window both ways
    38: ("Static kfrm", None),             # ADD KEYFRM: static keyframe
    12: ("Grade -2", "Prev grade"),        # AUTO COLOR: applies the grade from 2 clips back / from the previous clip
    13: ("Log mode", None),                # OFFSET
    14: ("Copy node", None),               # COPY: copies the node's correction
    15: ("Paste still", None),             # PASTE: applies the selected still's grade
    18: ("Del window", "Del still"),       # DELETE
    19: ("Rst palette", "⚠ Rst clip"),     # RESET: the open palette / the whole clip (base mem)
    23: ("Show wins", "Mute audio"),       # LOOP
    33: ("RGB Lift", "Lift level"),        # RESET LIFT: only the RGB balance (ball) / only the level (master, ring)
    34: ("RGB Gamma", "Gamma level"),      # RESET GAMMA
    35: ("RGB Gain", "Gain level"),        # RESET GAIN
    39: ("Prev album", "Add flag"),        # PREV STILL
    40: ("Next album", None),              # NEXT STILL
    41: ("Prev marker", "Add marker"),     # PREV KEYFRM
    42: ("Next marker", "Show marker"),    # NEXT KEYFRM
    43: (None, "First node"),              # PREV NODE
    44: (None, "Last node"),               # NEXT NODE
    45: (None, "First frame"),             # PREV FRAME: of the clip
    46: (None, "Last frame"),              # NEXT FRAME
    47: ("Prev vers.", "TL start"),        # PREV CLIP: grade version / timeline start
    48: ("Next vers.", "TL end"),          # NEXT CLIP
    49: (None, "Track ◀"),                 # ◀ PLAY: tracks the window backwards
    50: (None, "Track ▶"),                 # ▶ PLAY
    51: (None, "Stop track"),              # STOP
}


def label(bit, m):
    """Key text for the SHIFT latched (bitmap m): (text, "normal" | "shift" | "none")."""
    if bit in SHIFTS or not m:
        return LABEL[bit], "normal"
    up, down = SHIFT_FUNCTIONS.get(bit, (None, None))
    is_up, is_down = m >> SHIFT_UP & 1, m >> SHIFT_DOWN & 1
    f = None if is_up and is_down else up if is_up else down
    return (f, "shift") if f else (LABEL[bit], "none")

# Replies to GET_REPORT. The kernel indexes them by report ID only and sends them as they are
# (the first byte is the ID), trimmed to wLength.
GET_REPLIES = {
    0x01: bytes.fromhex("01 15 5b 3b fd 02 02 00"),   # SystemInfo (capture t=7.785): u32 + firmware 2.2.0
    0x08: bytes.fromhex("08 01 63"),                  # BatteryStatus: "charging, 99 %" (t=7.870)
    0x03: bytes.fromhex("03 64 64"),                  # Brightness: the last value Resolve set
    0x0A: bytes.fromhex("0a 01"),                     # SessionEnabled
    0x02: bytes([0x02]) + bytes(8),                   # inputs: in case someone asks for them over control
    0x05: bytes([0x05]) + bytes(36),
    0x06: bytes([0x06]) + bytes(64),
}

stop = threading.Event()
lock = threading.Lock()
pending = [0] * 15                   # units still to send, per control (0-11 knobs, 12-14 rings)
button_queue = deque()               # (bitmap, seconds it lasts before the next one) still to send
state = {"last_set": None, "session": False, "blocked": False, "sent": [0] * 15,
         "slot": 9, "step": 2, "ring_step": 2, "disconnected": False, "held": 0, "previous": {},
         "leds": 0, "indicators": 0, "flash": {}, "map": True, "path": "", "c3": ""}
log_lines = deque(maxlen=500)        # (line, kind) for the UI's log area
_out = threading.Lock()
_interactive = False


def bit_name(bit):
    if bit < len(KNOBS):
        return f"[{KNOBS[bit]}] (knob)"
    return PANEL_KEYS.get(bit, f"bit {bit}")


def log(msg, kind="info"):
    """kind: info, ok, warn, resolve, key (it only changes the color in the UI)."""
    line = time.strftime("%H:%M:%S ") + msg
    with _out:
        if _interactive:
            log_lines.append((line, kind))
            return
    print(line, flush=True)


def map_text():
    out = ["Knobs (arrows only):",
           "  ← →        turn                 Shift+← →   turn ×10",
           "  ↑ ↓        select knob          Shift+↑ ↓   change the step size",
           "             (after LUM MIX come the rings: with OFFSET (S) and the HDR palette = Global Temp, Tint, Exp)",
           "  space      press the selected knob",
           "  (hold the arrow: Resolve accelerates with back-to-back reports, like a fast turn)",
           "",
           "Panel keys (Latin American keyboard; · = no function with that Shift):"]
    for title, m in (("normal", 0), ("with SHIFT UP (C)", 1 << SHIFT_UP), ("with SHIFT DOWN (V)", 1 << SHIFT_DOWN)):
        out.append(f"  {title}")
        for _, chars, bits in ROWS:
            cells = []
            for c, b in zip(chars, bits):
                txt, mode = label(b, m)
                cells.append(f"{c.upper()} {'·' if mode == 'none' else txt:<12}")
            out.append("   " + " ".join(cells).rstrip())
    out += ["",
            "  C and V (SHIFT UP/DOWN) stay latched until the next key, like on the panel",
            "  (press them again to release them). E.g.: C then 8 = parallel node.",
            "  Tab: hide/show the map · Ctrl+C or Ctrl+D: quit"]
    return "\n".join(out)


# ---------------------------------------------------------------- the "wire": Linux gadget, macOS virtual HID or none
# The three classes share the same shape: path, send(report), set_get_reply(id, bytes) and events() (a thread that
# passes what Resolve sends to handle_set).
def find_hidg():
    try:
        mm = open(f"{FUNC}/dev").read().strip()
    except OSError:
        sys.exit("Gadget not found. First: sudo tools/mcp_gadget.sh up")
    for c in glob.glob("/sys/class/hidg/hidg*"):
        if open(f"{c}/dev").read().strip() == mm:
            return "/dev/" + os.path.basename(c)
    sys.exit(f"The gadget exists but its /dev/hidgN was not found (dev {mm}).")


class LinuxGadget:
    """Linux: the /dev/hidgN of the gadget set up by tools/mcp_gadget.sh (dummy_hcd + configfs + f_hid)."""

    def __init__(self, path=None):
        self.path = path or find_hidg()
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            sys.exit(f"No access to {self.path}. 'up' chowns it to your user; did you sudo it from your own session?")

    def send(self, rep):
        os.write(self.fd, rep)          # BlockingIOError: nobody reads the hidraw; OSError: gadget not configured

    def set_get_reply(self, rid, data, wait_user=False):
        """GADGET_HID_WRITE_GET_REPORT. wait_user=False: the kernel replies on its own, right away."""
        s = struct.pack("<BBH64s4x", rid, 1 if wait_user else 0, len(data), data)
        fcntl.ioctl(self.fd, GADGET_HID_WRITE_GET_REPORT, s)

    def events(self):
        """SET_REPORT/OUT from Resolve (POLLIN) and GET_REPORT with no reply prepared (POLLPRI)."""
        p = select.poll()
        p.register(self.fd, select.POLLIN | select.POLLPRI)
        while not stop.is_set():
            for _, ev in p.poll(200):
                if ev & select.POLLPRI:
                    b = bytearray(1)
                    fcntl.ioctl(self.fd, GADGET_HID_READ_GET_REPORT_ID, b, True)
                    log(f"Resolve → unknown GET_REPORT 0x{b[0]:02x}: replying with zeros", "warn")
                    self.set_get_reply(b[0], bytes([b[0]]), wait_user=True)
                if ev & select.POLLIN:
                    try:
                        d = os.read(self.fd, 256)
                    except BlockingIOError:
                        continue
                    except OSError:                 # ENOMEM: the host disabled the function
                        if not state["disconnected"]:
                            state["disconnected"] = True
                            log("The host disconnected the panel (replug or down?). Waiting…", "warn")
                        time.sleep(0.3)
                        continue
                    if state["disconnected"]:
                        state["disconnected"] = False
                        log("The host reconnected the panel.", "ok")
                    handle_set(self, d)


MAC_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "macos", "mcp_hid_mac")
MAC_HELP = ("See the README (macOS section): build it with macos/build.sh, which signs it with the virtual HID\n"
            "entitlement. It needs SIP disabled and the boot-arg amfi_get_out_of_my_way=1: with SIP off alone,\n"
            "AMFI still kills the ad hoc signed helper (Killed: 9).")


class HidMac:
    """macOS: the virtual HID (IOHIDUserDevice) is created by macos/mcp_hid_mac. We talk to it over stdin/stdout
    in text lines: we send "IN <hex>" and "GET <hex>"; it replies "SET <type> <hex>", "GETREQ <type> <id>"
    and "ERR …". Tested 2026-11-09 on macOS 26.3 (Apple Silicon) with Resolve Studio 21.0.2: with SIP off and
    amfi_get_out_of_my_way=1 the helper prints READY, the system sees 1edb:da0f "DaVinci Resolve Micro Color
    Panel" and Resolve opens a session (SessionEnabled, brightness, LEDs). Knob and key effects in Resolve on
    macOS are not confirmed yet."""

    def __init__(self, helper):
        helper = os.path.normpath(helper)
        if not os.access(helper, os.X_OK):
            sys.exit(f"{helper} not found.\n{MAC_HELP}")
        self.path = f"macOS virtual HID ({os.path.basename(helper)})"
        self.p = subprocess.Popen([helper], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._lock = threading.Lock()
        first = self.p.stdout.readline().decode(errors="replace").strip()
        if first != "READY":
            self.p.kill()
            sys.exit(f"The macOS virtual HID didn't start: {first or f'exited with code {self.p.wait()}'}\n{MAC_HELP}")

    def _line(self, txt):
        with self._lock:                         # BrokenPipeError (OSError) if the helper exited
            self.p.stdin.write(txt.encode() + b"\n")
            self.p.stdin.flush()

    def send(self, rep):
        self._line("IN " + rep.hex())

    def set_get_reply(self, rid, data, wait_user=False):
        self._line("GET " + data.hex())         # data's first byte is the ID

    def events(self):
        for raw in self.p.stdout:
            p = raw.decode(errors="replace").split()
            if len(p) == 3 and p[0] == "SET":
                handle_set(self, bytes.fromhex(p[2]))
            elif len(p) == 3 and p[0] == "GETREQ":
                log(f"Resolve → unknown GET_REPORT (type {p[1]}) 0x{int(p[2]):02x}: replying with zeros", "warn")
            elif p:
                log("mcp_hid_mac: " + " ".join(p), "warn" if p[0] == "ERR" else "info")
        state["disconnected"] = True
        log("The macOS virtual HID closed.", "warn")


class Simulated:
    """No gadget and no Resolve: the UI works and the reports are dropped."""
    path = "(simulated: sends nothing)"

    def send(self, rep):
        pass

    def set_get_reply(self, rid, data, wait_user=False):
        pass


def prepare(dev):
    for rid, data in GET_REPLIES.items():
        dev.set_get_reply(rid, data)


def handle_set(dev, d):
    if not d:
        return
    rid = d[0]
    state["last_set"] = time.monotonic()
    repeated = state["previous"].get(rid) == d
    state["previous"][rid] = d
    if rid in (0x03, 0x0A):                       # so a later GET returns the last value set
        dev.set_get_reply(rid, d)
        if not repeated:                          # the keepalive every 30 s isn't printed again
            log(f"Resolve → {'Brightness' if rid == 3 else 'SessionEnabled'}: {d.hex(' ')}", "resolve")
    elif rid == 0x02 and len(d) >= 9:
        if not repeated:
            b = int.from_bytes(d[1:9], "little")
            state["leds"] = b
            on = [i for i in range(64) if b >> i & 1]
            txt = ", ".join(f"{bit_name(i)} (bit {i})" for i in on) if on else "all off"
            log(f"Resolve → LEDs: {txt}   [{d[1:9].hex(' ')}]", "resolve")
    elif rid == 0x04:
        if not repeated:
            b = d[1] if len(d) > 1 else 0
            state["indicators"] = b
            on = [PANEL_KEYS[k] for k, m in SHIFTS.items() if b & m]
            other = b & ~0x30
            txt = ", ".join(on) if on else "off"
            log(f"Resolve → indicators: {txt}{f' + 0x{other:02x}' if other else ''}   [{d.hex(' ')}]", "resolve")
    else:
        log(f"Resolve → unknown SET 0x{rid:02x}: {d.hex(' ')}", "warn")
    if rid == 0x0A and not state["session"]:
        state["session"] = True
        log("✔ Resolve opened a session with the panel (SessionEnabled).", "ok")


def sender_thread(dev):
    blocked_since = None
    next_button = 0.0
    while not stop.is_set():
        time.sleep(TICK)
        with lock:
            if button_queue and time.monotonic() >= next_button:
                bm, delay = button_queue[0]
                rep, slot, n = bytes([0x02]) + bm.to_bytes(8, "little"), None, 0
            else:
                slot = next((i for i, v in enumerate(pending) if v), None)
                if slot is None:
                    if not button_queue:
                        blocked_since = None
                    continue
                n = pending[slot]
                if slot in RINGS:
                    v = [0] * 9
                    v[RINGS[slot]] = n
                    rep = bytes([0x05]) + struct.pack("<9i", *v)    # 37 B
                else:
                    v = [0] * 16
                    v[slot] = n
                    rep = bytes([0x06]) + struct.pack("<16i", *v)   # 65 B
        try:
            dev.send(rep)
        except BlockingIOError:                   # the host still hasn't read the previous report
            now = time.monotonic()
            blocked_since = blocked_since or now
            if now - blocked_since > 0.5:
                with lock:
                    pending[:] = [0] * len(pending)   # don't pile up turns only to release them all at once later
                    button_queue.clear()
                    state["held"] = 0
                if not state["blocked"]:
                    state["blocked"] = True
                    log("Nobody is reading the panel: Resolve (or another program) hasn't opened its hidraw. Dropping what's pending.", "warn")
            continue
        except OSError as e:                      # ESHUTDOWN: the host hasn't configured the gadget
            with lock:
                pending[:] = [0] * len(pending)
                button_queue.clear()
                state["held"] = 0
            log(f"Couldn't send the report ({e.strerror}). Is the gadget connected?", "warn")
            time.sleep(0.5)
            continue
        blocked_since = None
        if state["blocked"]:
            state["blocked"] = False
            log("The host is reading the panel again.", "ok")
        with lock:
            if slot is None:
                button_queue.popleft()
                next_button = time.monotonic() + delay
            else:
                pending[slot] -= n
                state["sent"][slot] += n


def units(ctrl, steps):
    """steps × 360 on the knobs; on the rings, the same rounded to multiples of 4096."""
    if ctrl in RINGS:
        return round(steps * UNIT / RING_UNIT) * RING_UNIT or (RING_UNIT if steps > 0 else -RING_UNIT)
    return steps * UNIT


def turn(slot, steps):
    with lock:
        pending[slot] += units(slot, steps)


def tap(bit):
    """Press and release a key (bit 0-11 = knob push button, 12-51 = keys). If a SHIFT is latched, the key
    goes together with it, and releasing the key releases the SHIFT too."""
    with lock:
        m = state["held"]
        button_queue.append((m | 1 << bit, PRESS_TIME))
        if m:
            button_queue.append((m, 0.05))
            button_queue.append((0, 0.0))
            state["held"] = 0
        else:
            button_queue.append((0, 0.0))
    return m


def combo(mods, bit, duration=PRESS_TIME):
    """Hold the mods (SHIFT UP/DOWN), press bit and release everything, like with fingers. At the end it
    re-latches whatever SHIFTs the keyboard had on."""
    m = sum(1 << b for b in mods)
    state["flash"][bit] = time.monotonic() + 0.35
    with lock:
        button_queue.extend([(m, 0.05), (m | 1 << bit, duration), (m, 0.05), (state["held"], 0.0)])


def toggle_shift(bit):
    with lock:
        state["held"] ^= 1 << bit
        button_queue.append((state["held"], 0.0))
        return bool(state["held"] >> bit & 1)


def key(bit):
    state["flash"][bit] = time.monotonic() + 0.35
    if bit in SHIFTS:
        on = toggle_shift(bit)
        log(f"{'▼' if on else '▲'} {PANEL_KEYS[bit]} {'on (press another key)' if on else 'released'}", "key")
    else:
        m = tap(bit)
        mods = " + ".join(PANEL_KEYS[b] for b in SHIFTS if m >> b & 1)
        if mods:
            txt, mode = label(bit, m)
            res = f" → {txt}" if mode == "shift" else "  (no function with Shift)"
            log(f"▸ {mods} + {bit_name(bit)}{res}  (bit {bit})", "key")
        else:
            log(f"▸ {bit_name(bit)}  (bit {bit})", "key")


def press(slot):
    tap(slot)


def wait_idle(timeout=3.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        with lock:
            if not any(pending) and not button_queue:
                return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- keyboard
def _move(d):
    state["slot"] = (state["slot"] + d) % len(CONTROLS)


def current_mult():
    if state["slot"] in RINGS:
        return RING_STEPS[state["ring_step"]]
    return STEPS[state["step"]]


def _step(d):
    k, options = ("ring_step", RING_STEPS) if state["slot"] in RINGS else ("step", STEPS)
    state[k] = max(0, min(len(options) - 1, state[k] + d))


def _turn(n):
    turn(state["slot"], n * current_mult())


SEQUENCES = {
    "\x1b[1;2C": lambda: _turn(10), "\x1b[1;2D": lambda: _turn(-10),
    "\x1b[1;2A": lambda: _step(1), "\x1b[1;2B": lambda: _step(-1),
    "\x1b[C": lambda: _turn(1), "\x1b[D": lambda: _turn(-1),
    "\x1bOC": lambda: _turn(1), "\x1bOD": lambda: _turn(-1),
    "\x1b[A": lambda: _move(-1), "\x1b[B": lambda: _move(1),
    "\x1bOA": lambda: _move(-1), "\x1bOB": lambda: _move(1),
}
RE_ESC = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|O.)?")


SPECIAL_KEYS = {                                 # curses.keyname() of the special keys
    "KEY_LEFT": lambda: _turn(-1), "KEY_RIGHT": lambda: _turn(1),
    "KEY_UP": lambda: _move(-1), "KEY_DOWN": lambda: _move(1),
    "KEY_SLEFT": lambda: _turn(-10), "KEY_SRIGHT": lambda: _turn(10),
    "kLFT2": lambda: _turn(-10), "kRIT2": lambda: _turn(10),
    "KEY_SR": lambda: _step(1), "KEY_SF": lambda: _step(-1),          # Shift+↑ / Shift+↓ on xterm
    "kUP2": lambda: _step(1), "kDN2": lambda: _step(-1),
}


def _put(scr, y, x, txt, attr=0):
    h, w = scr.getmaxyx()
    if 0 <= y < h and 0 <= x < w - 1:
        try:
            scr.addnstr(y, x, txt, w - 1 - x, attr)
        except curses.error:
            pass
    return x + len(txt)


def _colors():
    attrs = {"key_letter": 0, "led": 0, "shift": 0, "warn": curses.A_BOLD, "ok": curses.A_BOLD,
             "resolve": 0, "dim": curses.A_DIM, "key": curses.A_BOLD, "info": 0,
             "shift_txt": curses.A_BOLD | curses.A_UNDERLINE}
    try:
        curses.start_color()
        curses.use_default_colors()
        pairs = [("key_letter", curses.COLOR_CYAN, -1, curses.A_BOLD),
                 ("led", curses.COLOR_BLACK, curses.COLOR_GREEN, 0),
                 ("shift", curses.COLOR_BLACK, curses.COLOR_YELLOW, 0),
                 ("warn", curses.COLOR_RED, -1, curses.A_BOLD),
                 ("ok", curses.COLOR_GREEN, -1, curses.A_BOLD),
                 ("resolve", curses.COLOR_MAGENTA, -1, 0),
                 ("shift_txt", curses.COLOR_YELLOW, -1, curses.A_BOLD)]
        for i, (k, fg, bg, extra) in enumerate(pairs, start=1):
            curses.init_pair(i, fg, bg)
            attrs[k] = curses.color_pair(i) | extra
    except curses.error:
        pass
    return attrs


def _grid(scr, y, cells, width, C):
    """cells: (letter or None, text, attribute). The whole row if it fits; otherwise even halves (5+5, 6+6)."""
    w = scr.getmaxyx()[1]
    fits = max(1, (w - 2) // width)
    n = len(cells)
    per_row = n if fits >= n else n // 2 if fits >= n // 2 else fits
    for i, (letter, txt, at) in enumerate(cells):
        row, col = divmod(i, per_row)
        x = 1 + col * width
        if letter:
            _put(scr, y + row, x, letter, C["key_letter"])
            _put(scr, y + row, x + 2, txt[:width - 3], at)
        else:
            _put(scr, y + row, x, f" {txt} "[:width - 1], at)
    return y + (len(cells) + per_row - 1) // per_row


def draw(scr, C):
    scr.erase()
    h, w = scr.getmaxyx()
    now = time.monotonic()
    s, m = state["slot"], state["held"]
    # title and connection
    x = _put(scr, 0, 0, " Fake Micro Color Panel ", curses.A_REVERSE | curses.A_BOLD)
    x = _put(scr, 0, x + 1, state["path"], C["dim"])
    if state["disconnected"]:
        txt, at = "HOST DISCONNECTED", C["warn"]
    elif state["blocked"]:
        txt, at = "NOBODY IS READING THE PANEL (Resolve didn't open it?)", C["warn"]
    elif state["last_set"] is None:
        txt, at = "Resolve: nothing heard yet", C["dim"]
    else:
        txt = f"Resolve: {'✔ session open' if state['session'] else 'connected'} · last message {now - state['last_set']:.0f} s ago"
        at = C["ok"]
    _put(scr, 0, x + 3, txt, at)
    if state["c3"]:
        _put(scr, 1, 1, state["c3"], C["dim"])
    # selected control
    mult = current_mult()
    x = _put(scr, 2, 1, "Ring" if s in RINGS else "Knob", C["dim"])
    x = _put(scr, 2, x + 1, f" {CONTROLS[s]} ", curses.A_REVERSE | curses.A_BOLD)
    x = _put(scr, 2, x + 2, f"step ×{mult} = {abs(units(s, mult)):,} per tap")
    x = _put(scr, 2, x + 3, f"sent {state['sent'][s]:+,}", C["dim"])
    if m:
        _put(scr, 2, x + 3, " " + " + ".join(PANEL_KEYS[b] for b in SHIFTS if m >> b & 1) + " on ", C["shift"])
    _put(scr, 3, 1, "←→ turn · ↑↓ select · Shift+←→ ×10 · Shift+↑↓ step · space press · Tab map · Ctrl+C quit",
         C["dim"])
    y = 4
    if state["map"]:
        sel = lambda i: curses.A_REVERSE | curses.A_BOLD if i == s else 0
        y = _grid(scr, y + 1, [(None, SHORT[i], sel(i)) for i in range(len(KNOBS))], 11, C)
        x = _put(scr, y, 1, "Rings", C["dim"])
        for i in RINGS:
            x = _put(scr, y, x + 2, f" {SHORT[i]} ", sel(i))
        _put(scr, y, x + 3, "with OFFSET (S) and the HDR palette: Global Temp · Tint · Exp", C["dim"])
        y += 2
        x = _put(scr, y, 1, "Keys", curses.A_BOLD)
        latched = [b for b in SHIFTS if m >> b & 1]
        if latched:
            x = _put(scr, y, x + 2, " " + " + ".join(PANEL_KEYS[b] for b in latched) + " on ", C["shift"])
            _put(scr, y, x + 2, "in yellow, what each key does with Shift · in gray, keys with no function", C["dim"])
        else:
            x = _put(scr, y, x + 2, " Resolve LED ", C["led"])
            x = _put(scr, y, x + 1, " SHIFT on ", C["shift"])
            _put(scr, y, x + 2, "C/V = SHIFT UP/DOWN: they stay latched and change the key labels", C["dim"])
        y += 1
        width = max(8, min(16, (w - 2) // 10))
        for _, chars, bits in ROWS:
            x = 1
            for c, b in zip(chars, bits):
                txt, mode = label(b, m)
                if state["flash"].get(b, 0) > now:
                    at = curses.A_REVERSE | curses.A_BOLD
                elif b in SHIFTS and m >> b & 1:
                    at = C["shift"]
                elif mode == "shift":
                    at = C["shift_txt"]
                elif mode == "none":
                    at = C["dim"]
                elif state["leds"] >> b & 1:
                    at = C["led"]
                else:
                    at = 0
                _put(scr, y, x, c.upper(), C["key_letter"])
                _put(scr, y, x + 2, txt[:width - 3], at)
                x += width
            y += 1
    # log
    y += 1
    _put(scr, y, 0, "─" * (w - 1), C["dim"])
    _put(scr, y, 2, " Log ", C["dim"])
    y += 1
    rows = h - y
    with _out:
        lines = list(log_lines)[-rows:] if rows > 0 else []
    for i, (txt, kind) in enumerate(lines):
        _put(scr, y + i, 1, txt, C.get(kind, 0))
    scr.noutrefresh()
    curses.doupdate()


def _loop(scr):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.keypad(True)
    scr.timeout(50)
    C = _colors()
    while not stop.is_set():
        draw(scr, C)
        try:
            k = scr.get_wch()
        except curses.error:
            continue
        if isinstance(k, int):
            action = SPECIAL_KEYS.get(curses.keyname(k).decode(errors="ignore"))
            if action:
                action()
            continue
        if k == "\x1b":                         # a sequence curses doesn't know: gather it so it isn't read as keys
            buf = k
            scr.nodelay(True)
            for _ in range(32):
                try:
                    c = scr.get_wch()
                except curses.error:
                    break
                if isinstance(c, str):
                    buf += c
            scr.timeout(50)
            handle_input(buf)
        else:
            handle_input(k)


def interactive():
    global _interactive
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    _interactive = True
    try:
        curses.wrapper(_loop)
    finally:
        with _out:
            _interactive = False
            last = list(log_lines)[-12:]
        for line, _ in last:                     # so the log isn't lost on exit
            print(line)


def handle_input(buf):
    i = 0
    while i < len(buf):
        c = buf[i]
        if c == "\x1b":                           # arrows and other sequences; unknown ones are ignored
            m = RE_ESC.match(buf, i)
            action = SEQUENCES.get(m.group(0))
            if action:
                action()
            i = m.end()
            continue
        i += 1
        if c == "\x04":                           # Ctrl+D
            stop.set()
            return
        if c == "\t":
            state["map"] = not state["map"]
        elif c == " ":
            if state["slot"] in RINGS:
                log("The rings can't be pressed; space only works on the knobs.", "warn")
            else:
                key(state["slot"])
        elif c.lower() in KEY_TO_BIT:
            key(KEY_TO_BIT[c.lower()])


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Fake Micro Color Panel firmware (gadget side).")
    ap.add_argument("--hidg", help="/dev/hidgN node (looked up in configfs by default)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--map", action="store_true", help="print the key map and exit")
    g.add_argument("--listen", action="store_true", help="only log what Resolve sends")
    g.add_argument("--send", nargs=2, metavar=("KNOB", "STEPS"), help="e.g. --send 9 +5")
    g.add_argument("--press", type=int, metavar="KNOB")
    g.add_argument("--button", type=int, metavar="BIT", help="0-11 knob push buttons, 12-51 keys")
    ap.add_argument("--simulate", action="store_true", help="no gadget: try out the UI, sends nothing")
    ap.add_argument("--c3", nargs="?", const="auto", metavar="PORT",
                    help="also read the ESP32-C3's encoders and buttons (see tools/c3_input.py); defaults to "
                         "/dev/ttyACM0 on Linux and the first /dev/cu.usbmodem* on macOS")
    ap.add_argument("--mac-helper", metavar="PATH",
                    help="use the macOS virtual HID with this helper (default macos/mcp_hid_mac, macOS only)")
    ap.add_argument("--one-per-report", action="store_true",
                    help="with --send: one report per step (by default, all in one)")
    a = ap.parse_args()

    if a.map:
        print(map_text())
        return

    global _interactive
    keyboard_mode = not (a.send or a.press is not None or a.button is not None or a.listen)
    if keyboard_mode and not sys.stdin.isatty():
        sys.exit("No terminal: use --listen, --send, --press or --button.")
    _interactive = keyboard_mode                 # from here on the log goes to the bottom area

    if a.simulate:
        dev = Simulated()
        log("Simulated mode: the UI works but there's no gadget and no Resolve.", "warn")
    else:
        if a.mac_helper or sys.platform == "darwin":
            dev = HidMac(a.mac_helper or MAC_HELPER)
        else:
            dev = LinuxGadget(a.hidg)
        prepare(dev)
        log(f"Fake panel on {dev.path}. GET replies ready: " + ", ".join(f"0x{r:02x}" for r in GET_REPLIES))
        threading.Thread(target=dev.events, daemon=True).start()
    state["path"] = dev.path
    threading.Thread(target=sender_thread, args=(dev,), daemon=True).start()
    if a.c3:
        import c3_input
        threading.Thread(target=c3_input.run, args=(a.c3, sys.modules[__name__]), daemon=True).start()

    try:
        if a.send:
            slot, steps = int(a.send[0]), int(a.send[1])
            if not 0 <= slot < len(CONTROLS):
                sys.exit("KNOB is 0-14: 0-11 knobs, 12-14 rings (steps are ×360; on rings, rounded to ×4096)")
            if a.one_per_report:
                for _ in range(abs(steps)):
                    turn(slot, 1 if steps > 0 else -1)
                    wait_idle()
            else:
                turn(slot, steps)
            ok = wait_idle()
            log(f"{CONTROLS[slot]}: {state['sent'][slot]:+,} units sent" + ("" if ok else " (incomplete: nobody read)"))
            sys.exit(0 if ok else 1)
        elif a.press is not None or a.button is not None:
            bit = a.press if a.press is not None else a.button
            if not 0 <= bit < 64:
                sys.exit("the bit goes from 0 to 63")
            tap(bit)
            ok = wait_idle()
            log(f"{bit_name(bit)} (bit {bit}): pressed and released" + ("" if ok else " (incomplete: nobody read)"))
            sys.exit(0 if ok else 1)
        elif a.listen:
            log("Listening (Ctrl+C to quit)…")
            while True:
                time.sleep(1)
        else:
            interactive()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()


if __name__ == "__main__":
    main()
