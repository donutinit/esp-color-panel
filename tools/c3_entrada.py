"""
c3_entrada.py: la ESP32-C3 (firmware/c3) como entrada del panel falso.

Lo arranca `python3 tools/mcp_falso.py --c3 [PUERTO]`. Lee las líneas del firmware (E1 +1 q+4, P1 1, B2 0…)
y las convierte en acciones del Micro Color Panel falso:

  Encoder 1 / 2 / 3     anillo izquierdo / central / derecho
                        (con OFFSET y la paleta HDR abierta: Global Temp / Tint / Exp)
  Pulsador 1 / 2       RESET LIFT / RESET GAMMA (sin Shift: así resetean Temp y Tint, probado)
  Pulsador 3           SHIFT DOWN + RESET GAIN: según el manual, resetea solo el nivel (el anillo)
  Botón 1   un toque    SHIFT UP + OFFSET: alterna entre Primaries y Log (sale 0.2 s después, por el doble toque)
            doble toque clic en el botón de la paleta HDR (no hay tecla del panel ni atajo de Resolve para eso)
            mantenido   1.5 s con el puntero sobre el botón HDR: guarda dónde está
  Botón 2               DISABLE (apaga/prende el nodo)
  Botón 3               UNDO
  Botón 4   un toque    OFFSET (sale 0.2 s después, por el doble toque)
            doble toque VIEWER: el visor grande

El clic: el cursor va al botón, aprieta y regresa a donde estaba.
  Linux   Hyprland (IPC) para mover el cursor + un mouse virtual de uinput (/dev/uinput es del grupo input).
          Con otro compositor el clic no funciona; lo demás sí.
  macOS   Quartz (pip3 install pyobjc-framework-Quartz). La terminal necesita permiso de Accesibilidad
          (Ajustes del Sistema → Privacidad y seguridad → Accesibilidad). SIN PROBAR.
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

CONFIG = os.path.expanduser("~/.config/mcp-falso/c3.json")
MANTENER_CALIBRA = 1.5                           # segundos con el botón 1 apretado para guardar la posición de HDR
DOBLE = 0.2                                      # segundos entre soltar y volver a apretar un botón = doble toque

SHIFT_UP, SHIFT_DOWN, OFFSET, UNDO, DISABLE, VIEWER = 24, 25, 13, 16, 21, 30
ENCODERS = {1: 12, 2: 13, 3: 14}                 # encoder → anillo de mcp_falso (izq, centro, der)
RESETS = {"P1": (33, "RESET LIFT", []), "P2": (34, "RESET GAMMA", []), "P3": (35, "RESET GAIN", [SHIFT_DOWN])}
CORTO = {1: "TEMP", 2: "TINT", 3: "EXP"}


# ---------------------------------------------------------------- Hyprland
def _hypr(cmd):
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        raise OSError("no hay sesión de Hyprland")
    run = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(os.path.join(run, "hypr", sig, ".socket.sock"))
        s.sendall(cmd.encode())
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    return out.decode()


def cursor():
    d = json.loads(_hypr("j/cursorpos"))
    return d["x"], d["y"]


def mover(x, y):
    # Hyprland 0.56 con config en Lua: dispatch evalúa Lua. El "movecursor x y" de antes contesta con un error
    # en texto (no falla el socket), así que hay que revisar la respuesta.
    r = _hypr(f"dispatch hl.dsp.cursor.move({{x={round(x)}, y={round(y)}}})").strip()
    if r != "ok":
        r2 = _hypr(f"dispatch movecursor {round(x)} {round(y)}").strip()   # Hyprland con hyprland.conf
        if r2 != "ok":
            raise OSError(f"Hyprland no movió el cursor: {r.splitlines()[0] if r else r2}")


def ventanas_resolve():
    return [c for c in json.loads(_hypr("j/clients"))
            if c.get("class") == "resolve" and c.get("mapped") and not c.get("hidden")]


# ---------------------------------------------------------------- mouse virtual (uinput, sin python-evdev)
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 0x40045564, 0x40045565, 0x40045566
UI_DEV_SETUP, UI_DEV_CREATE = 0x405C5503, 0x5501          # _IOW('U', 3, struct uinput_setup) de 92 B
EV_SYN, EV_KEY, EV_REL, BTN_LEFT, REL_X, REL_Y = 0, 1, 2, 0x110, 0, 1


class Mouse:
    def __init__(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_LEFT)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)     # sin ejes relativos libinput no lo toma como mouse
        fcntl.ioctl(self.fd, UI_SET_RELBIT, REL_X)
        fcntl.ioctl(self.fd, UI_SET_RELBIT, REL_Y)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack("<4H80sI", 0x06, 0, 0, 1, b"mcp-falso-mouse", 0))
        fcntl.ioctl(self.fd, UI_DEV_CREATE)

    def _ev(self, tipo, codigo, valor):
        os.write(self.fd, struct.pack("llHHi", 0, 0, tipo, codigo, valor))

    def clic(self):
        for v in (1, 0):
            self._ev(EV_KEY, BTN_LEFT, v)
            self._ev(EV_SYN, 0, 0)
            time.sleep(0.04)


# ---------------------------------------------------------------- macOS: Quartz (SIN PROBAR)
def _quartz():
    try:
        import Quartz
    except ImportError:
        raise OSError("falta pyobjc: pip3 install pyobjc-framework-Quartz") from None
    return Quartz


def _cursor_mac():
    Q = _quartz()
    p = Q.CGEventGetLocation(Q.CGEventCreate(None))   # coordenadas globales, origen arriba a la izquierda
    return p.x, p.y


def _mover_mac(x, y):
    Q = _quartz()                                     # un evento de movimiento (no un warp) para que Resolve lo vea
    Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (x, y), Q.kCGMouseButtonLeft))


def _ventanas_mac():
    Q = _quartz()
    lista = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements,
                                         Q.kCGNullWindowID) or []
    out = []
    for w in lista:
        if "Resolve" in str(w.get(Q.kCGWindowOwnerName, "")) and w.get(Q.kCGWindowLayer, 1) == 0:
            b = w[Q.kCGWindowBounds]
            out.append({"at": [int(b["X"]), int(b["Y"])], "size": [int(b["Width"]), int(b["Height"])]})
    return out


class MouseMac:
    def __init__(self):
        self.Q = _quartz()

    def clic(self):
        Q = self.Q
        x, y = _cursor_mac()
        for tipo in (Q.kCGEventLeftMouseDown, Q.kCGEventLeftMouseUp):
            Q.CGEventPost(Q.kCGHIDEventTap, Q.CGEventCreateMouseEvent(None, tipo, (x, y), Q.kCGMouseButtonLeft))
            time.sleep(0.04)


if sys.platform == "darwin":
    cursor, mover, ventanas_resolve, Mouse = _cursor_mac, _mover_mac, _ventanas_mac, MouseMac


def puerto_por_defecto():
    """Linux: /dev/ttyACM0. macOS: el primer /dev/cu.usbmodem* (el USB Serial/JTAG del C3)."""
    if sys.platform == "darwin":
        c = sorted(glob.glob("/dev/cu.usbmodem*"))
        return c[0] if c else "/dev/cu.usbmodem*"
    return "/dev/ttyACM0"


# ---------------------------------------------------------------- botón HDR
def calibrar(log):
    x, y = cursor()
    for c in ventanas_resolve():
        (vx, vy), (w, h) = c["at"], c["size"]
        if vx <= x < vx + w and vy <= y < vy + h:
            os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
            with open(CONFIG, "w") as f:
                json.dump({"hdr": {"dx": x - vx, "dy": y - vy, "tam": [w, h]}}, f, indent=1)
            log(f"C3: guardé el botón HDR en ({x - vx}, {y - vy}) de la ventana de Resolve de {w}×{h}", "ok")
            return
    log("C3: el puntero no está sobre una ventana de Resolve; no guardé nada", "aviso")


def clic_hdr(mouse, log):
    if mouse is None:
        log("C3: sin mouse virtual no puedo hacer clic en HDR", "aviso")
        return
    try:
        with open(CONFIG) as f:
            cal = json.load(f)["hdr"]
    except (OSError, ValueError, KeyError):
        log("C3: aún no sé dónde está el botón HDR. Pon el puntero sobre él y mantén el botón 1 apretado 1.5 s", "aviso")
        return
    v = next((c for c in ventanas_resolve() if c["size"] == cal["tam"]), None)
    if v is None:
        log(f"C3: no encuentro la ventana de Resolve de {cal['tam'][0]}×{cal['tam'][1]} donde calibraste HDR. "
            "Recalibra: puntero sobre HDR y botón 1 apretado 1.5 s", "aviso")
        return
    x0, y0 = cursor()
    mover(v["at"][0] + cal["dx"], v["at"][1] + cal["dy"])
    time.sleep(0.04)
    mouse.clic()
    time.sleep(0.04)
    mover(x0, y0)
    log("C3 botón 1 doble toque → clic en HDR", "tecla")


# ---------------------------------------------------------------- puerto serie
def abrir(puerto):
    fd = os.open(puerto, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0                                  # crudo: sin eco ni traducciones
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[4] = a[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, a)
    return fd


def hilo(puerto, panel):
    """puerto: ruta o "auto" (puerto_por_defecto). panel: el módulo mcp_falso (girar, tecla, combo, log, estado,
    salir, PASOS_ANILLO)."""
    log, st = panel.log, panel.estado
    ruta = {"actual": puerto}
    totales = {1: 0, 2: 0, 3: 0}
    # botón 1: desde = cuándo se apretó (None = suelto); pendiente = hora en que sale el toque simple si no
    # llega un segundo; doble = este apretón es el segundo toque (al soltarlo no cuenta como toque nuevo)
    b1 = {"desde": None, "calibrado": False, "pendiente": None, "doble": False}
    b4 = {"pendiente": None, "doble": False}          # botón 4: igual que el 1, sin el mantenido

    def mostrar(conectado):
        st["c3"] = (f"C3 {ruta['actual']} {'✔' if conectado else '✘ sin conexión'} · "
                    + " · ".join(f"⊙ {CORTO[n]} {totales[n]:+d}" for n in totales) + " clics")

    try:
        mouse = Mouse()
    except OSError as e:
        mouse = None
        log(f"C3: no pude crear el mouse virtual ({e.strerror or e}); el doble toque (HDR) no va a funcionar", "aviso")

    def procesar(linea):
        p = linea.split()
        if len(p) == 3 and p[0] in ("E1", "E2", "E3"):
            n, clics = int(p[0][1]), int(p[1])
            if clics:
                totales[n] += clics
                panel.girar(ENCODERS[n], clics * panel.PASOS_ANILLO[st["paso_anillo"]])
                mostrar(True)
        elif len(p) == 2 and p[0] in RESETS and p[1] == "1":
            bit, txt, mods = RESETS[p[0]]
            panel.combo(mods, bit)
            log(f"C3 pulsador {p[0][1]} → {'SHIFT DOWN + ' if mods else ''}{txt}", "tecla")
        elif len(p) == 2 and p[0] == "B1":
            ahora = time.monotonic()
            if p[1] == "1":
                b1["doble"] = b1["pendiente"] is not None      # había un toque esperando: este es el segundo
                b1["pendiente"] = None
                b1["desde"], b1["calibrado"] = ahora, False
                if b1["doble"]:
                    clic_hdr(mouse, log)
            else:
                if b1["desde"] is not None and not b1["doble"] and not b1["calibrado"]:
                    b1["pendiente"] = ahora + DOBLE
                b1["desde"], b1["doble"] = None, False
        elif len(p) == 2 and p[0] == "B2" and p[1] == "1":
            panel.tecla(DISABLE)
        elif len(p) == 2 and p[0] == "B3" and p[1] == "1":
            panel.tecla(UNDO)
        elif len(p) == 2 and p[0] == "B4":
            if p[1] == "1":
                b4["doble"] = b4["pendiente"] is not None      # había un toque esperando: este es el segundo
                b4["pendiente"] = None
                if b4["doble"]:
                    panel.tecla(VIEWER)
            else:
                if not b4["doble"]:
                    b4["pendiente"] = time.monotonic() + DOBLE
                b4["doble"] = False

    avisado = False
    while not panel.salir.is_set():
        ruta["actual"] = puerto_por_defecto() if puerto in (None, "auto") else puerto
        try:
            fd = abrir(ruta["actual"])
        except OSError as e:
            mostrar(False)
            if not avisado:
                log(f"C3: no puedo abrir {ruta['actual']} ({e.strerror}); reintento cada segundo", "aviso")
                avisado = True
            time.sleep(1)
            continue
        avisado = False
        log(f"C3 conectado en {ruta['actual']}", "ok")
        mostrar(True)
        buf = b""
        try:
            while not panel.salir.is_set():
                listo, _, _ = select.select([fd], [], [], 0.02)
                ahora = time.monotonic()
                if (b1["desde"] is not None and not b1["calibrado"] and not b1["doble"]
                        and ahora - b1["desde"] >= MANTENER_CALIBRA):
                    b1["calibrado"] = True
                    calibrar(log)
                if b1["pendiente"] is not None and ahora >= b1["pendiente"]:   # no llegó el segundo toque
                    b1["pendiente"] = None
                    panel.combo([SHIFT_UP], OFFSET, dura=0.3)
                    log("C3 botón 1 → SHIFT UP + OFFSET (Primaries ⇄ Log)", "tecla")
                if b4["pendiente"] is not None and ahora >= b4["pendiente"]:
                    b4["pendiente"] = None
                    panel.tecla(OFFSET)
                if not listo:
                    continue
                d = os.read(fd, 256)
                if not d:
                    raise OSError("se cerró")
                buf += d
                while b"\n" in buf:
                    linea, buf = buf.split(b"\n", 1)
                    try:
                        procesar(linea.decode(errors="replace").strip())
                    except (ValueError, OSError) as e:
                        log(f"C3: no pude procesar {linea!r}: {e}", "aviso")
        except OSError:
            log("C3 desconectado; espero a que vuelva…", "aviso")
            mostrar(False)
            time.sleep(1)
        finally:
            os.close(fd)
