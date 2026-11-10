"""
c3_input.py: the ESP32-C3 (firmware/c3) as input for the fake panel.

Started by `python3 tools/fake_panel.py --c3 [PORT]`. Reads the firmware lines (E1 +1 q+4, P1 1, B2 0…)
and turns them into actions of the fake Micro Color Panel:

  Encoder 1 / 2 / 3     left / center / right ring
                        (with OFFSET and the HDR palette open: Global Temp / Tint / Exp)
  Push 1 / 2            RESET LIFT / RESET GAMMA (no Shift: that way they reset Temp and Tint, tested)
  Push 3                SHIFT DOWN + RESET GAIN: per the manual, resets only the level (the ring)
  Button 1  single tap  SHIFT UP + OFFSET: toggles Primaries / Log (fires 0.2 s later, waiting for a double tap)
            double tap  click on the HDR palette button (no panel key or Resolve shortcut does that)
            hold        1.5 s with the pointer over the HDR button: saves where it is
  Button 2              DISABLE (turns the node off/on)
  Button 3              UNDO
  Button 4  single tap  OFFSET (fires 0.2 s later, waiting for a double tap)
            double tap  VIEWER: the large viewer

The click: the cursor goes to the button, presses and goes back to where it was.
  Linux   Hyprland (IPC) to move the cursor + a uinput virtual mouse (/dev/uinput belongs to the input group).
          With another compositor the click doesn't work; everything else does.
  macOS   Quartz (python3 -m pip install --user --break-system-packages pyobjc-framework-Quartz). The terminal
          needs Accessibility permission (System Settings → Privacy & Security → Accessibility).
          2026-11-09, macOS 26.3 (Apple Silicon): the C3 works over /dev/cu.usbmodem* and Quartz reads the cursor.
          The HDR click itself is not confirmed in Resolve yet.
"""
import fcntl
import glob
import json
import os
import select
import socket
import struct
import sys
import termios
import time

CONFIG = os.path.expanduser("~/.config/esp-color-panel/c3.json")
HOLD_CALIBRATE = 1.5                             # seconds holding button 1 to save the HDR position
DOUBLE_TAP = 0.2                                 # seconds between releasing and pressing a button again = double tap

SHIFT_UP, SHIFT_DOWN, OFFSET, UNDO, DISABLE, VIEWER = 24, 25, 13, 16, 21, 30
ENCODERS = {1: 12, 2: 13, 3: 14}                 # encoder → fake_panel ring (left, center, right)
RESETS = {"P1": (33, "RESET LIFT", []), "P2": (34, "RESET GAMMA", []), "P3": (35, "RESET GAIN", [SHIFT_DOWN])}
SHORT = {1: "TEMP", 2: "TINT", 3: "EXP"}


# ---------------------------------------------------------------- Hyprland
def _hypr(cmd):
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        raise OSError("no Hyprland session")
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(os.path.join(runtime, "hypr", sig, ".socket.sock"))
        s.sendall(cmd.encode())
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    return out.decode()


def cursor():
    d = json.loads(_hypr("j/cursorpos"))
    return d["x"], d["y"]


def move(x, y):
    # Hyprland 0.56 with Lua config: dispatch evaluates Lua. The old "movecursor x y" replies with a text error
    # (the socket doesn't fail), so the reply has to be checked.
    r = _hypr(f"dispatch hl.dsp.cursor.move({{x={round(x)}, y={round(y)}}})").strip()
    if r != "ok":
        r2 = _hypr(f"dispatch movecursor {round(x)} {round(y)}").strip()   # Hyprland with hyprland.conf
        if r2 != "ok":
            raise OSError(f"Hyprland didn't move the cursor: {r.splitlines()[0] if r else r2}")


def resolve_windows():
    return [c for c in json.loads(_hypr("j/clients"))
            if c.get("class") == "resolve" and c.get("mapped") and not c.get("hidden")]


# ---------------------------------------------------------------- virtual mouse (uinput, no python-evdev)
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 0x40045564, 0x40045565, 0x40045566
UI_DEV_SETUP, UI_DEV_CREATE = 0x405C5503, 0x5501          # _IOW('U', 3, struct uinput_setup) of 92 B
EV_SYN, EV_KEY, EV_REL, BTN_LEFT, REL_X, REL_Y = 0, 1, 2, 0x110, 0, 1


class Mouse:
    def __init__(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_LEFT)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)     # without relative axes libinput doesn't take it as a mouse
        fcntl.ioctl(self.fd, UI_SET_RELBIT, REL_X)
        fcntl.ioctl(self.fd, UI_SET_RELBIT, REL_Y)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack("<4H80sI", 0x06, 0, 0, 1, b"esp-color-panel-mouse", 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)

    def _ev(self, typ, code, value):
        os.write(self.fd, struct.pack("llHHi", 0, 0, typ, code, value))

    def click(self):
        for v in (1, 0):
            self._ev(EV_KEY, BTN_LEFT, v)
            self._ev(EV_SYN, 0, 0)
            time.sleep(0.04)


# ---------------------------------------------------------------- macOS: Quartz
def _quartz():
    try:
        import Quartz
    except ImportError:
        raise OSError("missing pyobjc: python3 -m pip install --user --break-system-packages "
                      "pyobjc-framework-Quartz") from None
    return Quartz


def _cursor_mac():
    Q = _quartz()
    p = Q.CGEventGetLocation(Q.CGEventCreate(None))   # global coordinates, origin top left
    return p.x, p.y


def _move_mac(x, y):
    Q = _quartz()                                     # a move event (not a warp) so Resolve sees it
    Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (x, y), Q.kCGMouseButtonLeft))


def _windows_mac():
    Q = _quartz()
    windows = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements,
                                           Q.kCGNullWindowID) or []
    out = []
    for w in windows:
        if "Resolve" in str(w.get(Q.kCGWindowOwnerName, "")) and w.get(Q.kCGWindowLayer, 1) == 0:
            b = w[Q.kCGWindowBounds]
            out.append({"at": [int(b["X"]), int(b["Y"])], "size": [int(b["Width"]), int(b["Height"])]})
    return out


class MouseMac:
    def __init__(self):
        self.Q = _quartz()

    def click(self):
        Q = self.Q
        x, y = _cursor_mac()
        for typ in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, typ, (x, y), Q.kCGMouseButtonLeft))
            time.sleep(0.04)


if sys.platform == "darwin":
    cursor, move, resolve_windows, Mouse = _cursor_mac, _move_mac, _windows_mac, MouseMac


def default_port():
    """Linux: /dev/ttyACM0. macOS: the first /dev/cu.usbmodem* (the C3's USB Serial/JTAG)."""
    if sys.platform == "darwin":
        c = sorted(glob.glob("/dev/cu.usbmodem*"))
        return c[0] if c else "/dev/cu.usbmodem*"
    return "/dev/ttyACM0"


# ---------------------------------------------------------------- HDR button
def calibrate(log):
    x, y = cursor()
    for c in resolve_windows():
        (vx, vy), (w, h) = c["at"], c["size"]
        if vx <= x < vx + w and vy <= y < vy + h:
            os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
            with open(CONFIG, "w") as f:
                json.dump({"hdr": {"dx": x - vx, "dy": y - vy, "size": [w, h]}}, f, indent=1)
            log(f"C3: saved the HDR button at ({x - vx}, {y - vy}) in the {w}×{h} Resolve window", "ok")
            return
    log("C3: the pointer isn't over a Resolve window; saved nothing", "warn")


def click_hdr(mouse, log):
    if mouse is None:
        log("C3: no virtual mouse, can't click HDR", "warn")
        return
    try:
        with open(CONFIG) as f:
            cal = json.load(f)["hdr"]
    except (OSError, ValueError, KeyError):
        log("C3: don't know where the HDR button is yet. Put the pointer over it and hold button 1 for 1.5 s", "warn")
        return
    v = next((c for c in resolve_windows() if c["size"] == cal["size"]), None)
    if v is None:
        log(f"C3: can't find the {cal['size'][0]}×{cal['size'][1]} Resolve window where HDR was calibrated. "
            "Recalibrate: pointer over HDR and hold button 1 for 1.5 s", "warn")
        return
    x0, y0 = cursor()
    move(v["at"][0] + cal["dx"], v["at"][1] + cal["dy"])
    time.sleep(0.04)
    mouse.click()
    time.sleep(0.04)
    move(x0, y0)
    log("C3 button 1 double tap → click on HDR", "key")


# ---------------------------------------------------------------- serial port
def open_port(port):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0                                  # raw: no echo or translations
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[4] = a[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, a)
    return fd


def run(port, panel):
    """port: path or "auto" (default_port). panel: the fake_panel module (turn, key, combo, log, state,
    stop, RING_STEPS)."""
    log, st = panel.log, panel.state
    dev = {"path": port}
    totals = {1: 0, 2: 0, 3: 0}
    # button 1: since = when it was pressed (None = released); pending = time the single tap fires if no
    # second one arrives; double = this press is the second tap (releasing it doesn't count as a new tap)
    b1 = {"since": None, "calibrated": False, "pending": None, "double": False}
    b4 = {"pending": None, "double": False}           # button 4: same as button 1, without the hold

    def show(connected):
        st["c3"] = (f"C3 {dev['path']} {'✔' if connected else '✘ disconnected'} · "
                    + " · ".join(f"⊙ {SHORT[n]} {totals[n]:+d}" for n in totals) + " clicks")

    try:
        mouse = Mouse()
    except OSError as e:
        mouse = None
        log(f"C3: couldn't create the virtual mouse ({e.strerror or e}); the double tap (HDR) won't work", "warn")

    def process(line):
        p = line.split()
        if len(p) == 3 and p[0] in ("E1", "E2", "E3"):
            n, clicks = int(p[0][1]), int(p[1])
            if clicks:
                totals[n] += clicks
                panel.turn(ENCODERS[n], clicks * panel.RING_STEPS[st["ring_step"]])
                show(True)
        elif len(p) == 2 and p[0] in RESETS and p[1] == "1":
            bit, txt, mods = RESETS[p[0]]
            panel.combo(mods, bit)
            log(f"C3 push {p[0][1]} → {'SHIFT DOWN + ' if mods else ''}{txt}", "key")
        elif len(p) == 2 and p[0] == "B1":
            now = time.monotonic()
            if p[1] == "1":
                b1["double"] = b1["pending"] is not None       # a tap was waiting: this is the second one
                b1["pending"] = None
                b1["since"], b1["calibrated"] = now, False
                if b1["double"]:
                    click_hdr(mouse, log)
            else:
                if b1["since"] is not None and not b1["double"] and not b1["calibrated"]:
                    b1["pending"] = now + DOUBLE_TAP
                b1["since"], b1["double"] = None, False
        elif len(p) == 2 and p[0] == "B2" and p[1] == "1":
            panel.key(DISABLE)
        elif len(p) == 2 and p[0] == "B3" and p[1] == "1":
            panel.key(UNDO)
        elif len(p) == 2 and p[0] == "B4":
            if p[1] == "1":
                b4["double"] = b4["pending"] is not None       # a tap was waiting: this is the second one
                b4["pending"] = None
                if b4["double"]:
                    panel.key(VIEWER)
            else:
                if not b4["double"]:
                    b4["pending"] = time.monotonic() + DOUBLE_TAP
                b4["double"] = False

    warned = False
    while not panel.stop.is_set():
        dev["path"] = default_port() if port in (None, "auto") else port
        try:
            fd = open_port(dev["path"])
        except OSError as e:
            show(False)
            if not warned:
                log(f"C3: can't open {dev['path']} ({e.strerror}); retrying every second", "warn")
                warned = True
            time.sleep(1)
            continue
        warned = False
        log(f"C3 connected on {dev['path']}", "ok")
        show(True)
        buf = b""
        try:
            while not panel.stop.is_set():
                ready, _, _ = select.select([fd], [], [], 0.02)
                now = time.monotonic()
                if (b1["since"] is not None and not b1["calibrated"] and not b1["double"]
                        and now - b1["since"] >= HOLD_CALIBRATE):
                    b1["calibrated"] = True
                    calibrate(log)
                if b1["pending"] is not None and now >= b1["pending"]:   # the second tap didn't come
                    b1["pending"] = None
                    panel.combo([SHIFT_UP], OFFSET, 0.3)                 # 0.3 s hold
                    log("C3 button 1 → SHIFT UP + OFFSET (Primaries ⇄ Log)", "key")
                if b4["pending"] is not None and now >= b4["pending"]:
                    b4["pending"] = None
                    panel.key(OFFSET)
                if not ready:
                    continue
                d = os.read(fd, 256)
                if not d:
                    raise OSError("closed")
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        process(line.decode(errors="replace").strip())
                    except (ValueError, OSError) as e:
                        log(f"C3: couldn't process {line!r}: {e}", "warn")
        except OSError:
            log("C3 disconnected; waiting for it to come back…", "warn")
            show(False)
            time.sleep(1)
        finally:
            os.close(fd)
