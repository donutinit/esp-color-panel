#!/usr/bin/env python3
"""
mcp_falso.py: el "firmware" del Micro Color Panel falso, corriendo en la PC.

En Linux habla por /dev/hidgN con el gadget que arma `sudo tools/mcp_gadget.sh up`; en macOS, con el HID virtual
que crea macos/mcp_hid_mac (ver el README). En los dos:
  * deja listas las respuestas a los GET_REPORT de Resolve, con los bytes de la captura de ra100
    (SystemInfo, BatteryStatus...). Quedan en el kernel aunque este script se cierre.
  * registra lo que manda Resolve: brillo, sesión y los LEDs de las teclas (decodificados).
  * manda perillas (reporte 0x06: 16 x int32 LE, un slot por reporte, múltiplos de 360) y
    teclas (reporte 0x02: bitmap de 64 bits; 0-11 = pulsadores de perilla, 12-51 = las 40 teclas).

Uso:
  python3 tools/mcp_falso.py                  interactivo (teclado)
  python3 tools/mcp_falso.py --mapa           imprime el mapa de teclas y sale
  python3 tools/mcp_falso.py --escuchar       sin teclado: solo registra lo que manda Resolve
  python3 tools/mcp_falso.py --enviar 9 +5    5 pasos (×360) a la perilla 9 y sale (12-14: anillos)
  python3 tools/mcp_falso.py --apretar 9      aprieta y suelta la perilla 9 y sale
  python3 tools/mcp_falso.py --boton 13       aprieta y suelta la tecla del bit 13 (OFFSET) y sale
  python3 tools/mcp_falso.py --simular        la interfaz sin gadget (no manda nada), para probarla
  python3 tools/mcp_falso.py --c3             además lee la ESP32-C3 (3 encoders = anillos, 4 botones);
                                              la asignación está en tools/c3_entrada.py

Interfaz: arriba quedan fijos el estado, la perilla elegida y el mapa de teclas (Tab lo oculta); abajo,
el registro. En verde, las teclas cuyo LED prendió Resolve; en amarillo, el SHIFT puesto; la tecla que
acabas de apretar parpadea.

SIN PROBAR del todo: las perillas ya movieron Resolve; el bit de cada tecla sale de ra100 y de la
captura, y falta confirmarlo tecla por tecla.
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

FUNC = "/sys/kernel/config/usb_gadget/mcp_falso/functions/hid.mcp"
# include/uapi/linux/usb/g_hid.h
GADGET_HID_READ_GET_REPORT_ID = 0x80016741       # _IOR('g', 0x41, __u8)
GADGET_HID_WRITE_GET_REPORT = 0x40486742         # _IOW('g', 0x42, struct usb_hidg_report) (72 B)

UNIDAD = 360                                     # el panel real manda múltiplos de 360
PASOS = [10, 50, 100, 300, 1000]                 # multiplicadores de UNIDAD (×100 = 36,000 = un "toque" de perilla)
PASOS_ANILLO = [5, 10, 25, 50, 100]              # los anillos van aparte: ×25 (= 2 × 4096) se siente bien
# Medido en Resolve 21.2 (Sat, nodo 2): acelera con la velocidad. Un reporte suelto rinde ~11 Sat por millón
# de unidades; reportes seguidos cada 24 ms, ~24. Un giro real de 1 s en la captura suma 0.7–1.3 millones.
TICK = 0.008                                     # el panel real reporta cada ~8 ms
PULSO = 0.12                                     # cuánto dura "apretada" una tecla

# Orden de izquierda a derecha según el manual de Blackmagic (confirmado en Resolve: 3, 5 y 9)
PERILLAS = ["Y LIFT", "Y GAMMA", "Y GAIN", "CONTRAST", "PIVOT", "MID DETAIL",
            "COLOR BOOST", "SHADOWS", "HIGHLIGHTS", "SATURATION", "HUE", "LUM MIX"]
# Controles 12-14: los anillos de las tres bolas (reporte 0x05 = 9 x int32: X1 Y1 anillo1 X2 Y2 anillo2 X3 Y3
# anillo3). En la captura los anillos mandan ±1-2 x 4096 cada 8 ms. Orden izquierda/centro/derecha [INFERIDO].
ANILLOS = {12: 2, 13: 5, 14: 8}                  # control → slot del reporte 0x05
UNIDAD_ANILLO = 4096
# Confirmado en Resolve 21.2: con OFFSET puesto y la paleta HDR abierta, los anillos manejan la Global
# HDR: izquierdo = Temp, centro = Tint, derecho = Exp.
CONTROLES = PERILLAS + ["ANILLO IZQ (Temp)", "ANILLO CEN (Tint)", "ANILLO DER (Exp)"]

# Las 40 teclas: bit del reporte 0x02 → etiqueta del manual. El bit de cada una sale de
# davinci_panel_mapping.json de ra100 (su índice − 8) y del orden en que las apretó en la captura.
TECLAS_PANEL = {
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
# En la captura, con el bit 24 apretado Resolve manda 04 10 y con el 25, 04 20: el foco verde del modificador.
SHIFTS = {24: 0x10, 25: 0x20}

# Teclado en distribución latinoamericana, 4 filas de 10
FILAS = [
    ("fila 1-0 · la fila de arriba del panel", "1234567890", [26, 27, 28, 29, 30, 31, 32, 36, 37, 38]),
    ("fila Q-P · pares PREV/NEXT",             "qwertyuiop", [39, 40, 41, 42, 43, 44, 45, 46, 47, 48]),
    ("fila A-Ñ · bloque izquierdo",            "asdfghjklñ", [12, 13, 14, 15, 16, 17, 18, 19, 20, 21]),
    ("fila Z-  · USER/LOOP, shifts, resets, transporte",
                                               "zxcvbnm,.-", [22, 23, 24, 25, 33, 34, 35, 49, 51, 50]),
]
TECLA_A_BIT = {c: b for _, chars, bits in FILAS for c, b in zip(chars, bits)}
TECLA_A_BIT.update({";": 21, "/": 50})           # las mismas teclas físicas en distribución US

# Etiquetas cortas para la pantalla
CORTOS = ["Y LIFT", "Y GAMMA", "Y GAIN", "CONTRAST", "PIVOT", "MID DET", "C.BOOST", "SHADOWS",
          "HIGHLIGHT", "SAT", "HUE", "LUM MIX", "⊙ TEMP", "⊙ TINT", "⊙ EXP"]
ETIQUETA = dict(TECLAS_PANEL)
ETIQUETA.update({49: "◀ PLAY", 50: "▶ PLAY", 51: "■ STOP"})

SHIFT_UP, SHIFT_DOWN = 24, 25
# Función de cada tecla con (SHIFT UP, SHIFT DOWN), según el manual del Micro Color Panel. None = no tiene.
FUNCIONES_SHIFT = {
    26: ("Split scrn", "Galería"),         # PLAY STILL: pantalla dividida / galería
    27: ("Modo wipe", None),               # WIPE STILL: recorre las opciones de wipe
    29: ("Modo H/Lite", None),             # H/LITE: recorre los modos de highlight
    30: ("Ver clips", "Lightbox"),         # VIEWER
    36: ("N. paralelo", "N. layer"),       # ADD NODE
    37: ("Win lineal", "Track win"),       # ADD WINDOW: lineal / trackea la window en ambos sentidos
    38: ("Keyfrm fijo", None),             # ADD KEYFRM: keyframe estático
    12: ("Grade 2 ant", "Grade ant."),     # AUTO COLOR: aplica el grade de 2 clips antes / del clip anterior
    13: ("Modo Log", None),                # OFFSET
    14: ("Copiar nodo", None),             # COPY: copia la corrección del nodo
    15: ("Pegar still", None),             # PASTE: aplica el grade del still elegido
    18: ("Borra win", "Borra still"),      # DELETE
    19: ("Rst paleta", "⚠ Rst clip"),      # RESET: la paleta abierta / todo el clip (base mem)
    23: ("Ver windows", "Mute audio"),     # LOOP
    33: ("RGB Lift", "Nivel Lift"),        # RESET LIFT: resetea solo el balance RGB (bola) / solo el nivel (master, anillo)
    34: ("RGB Gamma", "Nivel Gamma"),      # RESET GAMMA
    35: ("RGB Gain", "Nivel Gain"),        # RESET GAIN
    39: ("Álbum ant.", "Poner flag"),      # PREV STILL
    40: ("Álbum sig.", None),              # NEXT STILL
    41: ("Marca ant.", "Poner marca"),     # PREV KEYFRM
    42: ("Marca sig.", "Ver marca"),       # NEXT KEYFRM
    43: (None, "1er nodo"),                # PREV NODE
    44: (None, "Últ. nodo"),               # NEXT NODE
    45: (None, "1er frame"),               # PREV FRAME: del clip
    46: (None, "Últ. frame"),              # NEXT FRAME
    47: ("Vers. ant.", "Inicio TL"),       # PREV CLIP: versión de grade / inicio del timeline
    48: ("Vers. sig.", "Fin TL"),          # NEXT CLIP
    49: (None, "Track ◀"),                 # ◀ PLAY: trackea la window hacia atrás
    50: (None, "Track ▶"),                 # ▶ PLAY
    51: (None, "Parar track"),             # STOP
}


def etiqueta(bit, m):
    """Texto de la tecla según el SHIFT puesto (bitmap m): (texto, "normal" | "shift" | "sin")."""
    if bit in SHIFTS or not m:
        return ETIQUETA[bit], "normal"
    up, down = FUNCIONES_SHIFT.get(bit, (None, None))
    arriba, abajo = m >> SHIFT_UP & 1, m >> SHIFT_DOWN & 1
    f = None if arriba and abajo else up if arriba else down
    return (f, "shift") if f else (ETIQUETA[bit], "sin")

# Respuestas a GET_REPORT. El kernel las indexa solo por report ID y las manda tal cual
# (el primer byte es el ID), recortadas a wLength.
GET_RESPUESTAS = {
    0x01: bytes.fromhex("01 15 5b 3b fd 02 02 00"),   # SystemInfo (captura t=7.785): u32 + firmware 2.2.0
    0x08: bytes.fromhex("08 01 63"),                  # BatteryStatus: "cargando, 99 %" (t=7.870)
    0x03: bytes.fromhex("03 64 64"),                  # Brightness: lo último que fijó Resolve
    0x0A: bytes.fromhex("0a 01"),                     # SessionEnabled
    0x02: bytes([0x02]) + bytes(8),                   # inputs: por si alguien los pide por control
    0x05: bytes([0x05]) + bytes(36),
    0x06: bytes([0x06]) + bytes(64),
}

salir = threading.Event()
lock = threading.Lock()
pendiente = [0] * 15                 # unidades por mandar, por control (0-11 perillas, 12-14 anillos)
botones_cola = deque()               # (bitmap, segundos que dura antes del siguiente) por mandar
estado = {"ultimo_set": None, "tomado": False, "bloqueado": False, "enviados": [0] * 15,
          "slot": 9, "paso": 2, "paso_anillo": 2, "desconectado": False, "mantenidos": 0, "previos": {},
          "leds": 0, "indicadores": 0, "flash": {}, "mapa": True, "ruta": "", "c3": ""}
registro = deque(maxlen=500)         # (línea, tipo) para la zona de registro de la interfaz
_salida = threading.Lock()
_interactivo = False


def nombre(bit):
    if bit < len(PERILLAS):
        return f"[{PERILLAS[bit]}] (perilla)"
    return TECLAS_PANEL.get(bit, f"bit {bit}")


def log(msg, tipo="info"):
    """tipo: info, ok, aviso, resolve, tecla (solo cambia el color en la interfaz)."""
    linea = time.strftime("%H:%M:%S ") + msg
    with _salida:
        if _interactivo:
            registro.append((linea, tipo))
            return
    print(linea, flush=True)


def texto_mapa():
    out = ["Perillas (solo flechas):",
           "  ← →        girar                Shift+← →   girar ×10",
           "  ↑ ↓        elegir perilla       Shift+↑ ↓   cambiar el tamaño del paso",
           "             (después de LUM MIX vienen los anillos: con OFFSET (S) y la paleta HDR = Global Temp, Tint, Exp)",
           "  espacio    apretar la perilla elegida",
           "  (mantén la flecha: Resolve acelera con reportes seguidos, como un giro rápido)",
           "",
           "Teclas del panel (teclado latinoamericano; · = sin función con ese Shift):"]
    for titulo, m in (("normal", 0), ("con SHIFT UP (C)", 1 << SHIFT_UP), ("con SHIFT DOWN (V)", 1 << SHIFT_DOWN)):
        out.append(f"  {titulo}")
        for _, chars, bits in FILAS:
            celdas = []
            for c, b in zip(chars, bits):
                txt, modo = etiqueta(b, m)
                celdas.append(f"{c.upper()} {'·' if modo == 'sin' else txt:<12}")
            out.append("   " + " ".join(celdas).rstrip())
    out += ["",
            "  C y V (SHIFT UP/DOWN) se quedan puestas hasta la siguiente tecla, como en el panel",
            "  (apriétalas otra vez para soltarlas). Ej.: C y luego 8 = nodo paralelo.",
            "  Tab: ocultar/mostrar el mapa · Ctrl+C o Ctrl+D: salir"]
    return "\n".join(out)


# ---------------------------------------------------------------- el "cable": gadget de Linux, HID virtual de macOS o nada
# Las tres clases tienen lo mismo: ruta, mandar(reporte), respuesta_get(id, bytes) y eventos() (un hilo que le pasa a
# procesar_set lo que manda Resolve).
def buscar_hidg():
    try:
        mm = open(f"{FUNC}/dev").read().strip()
    except OSError:
        sys.exit("No encuentro el gadget. Primero: sudo tools/mcp_gadget.sh up")
    for c in glob.glob("/sys/class/hidg/hidg*"):
        if open(f"{c}/dev").read().strip() == mm:
            return "/dev/" + os.path.basename(c)
    sys.exit(f"El gadget existe pero no encuentro su /dev/hidgN (dev {mm}).")


class GadgetLinux:
    """Linux: el /dev/hidgN del gadget que arma tools/mcp_gadget.sh (dummy_hcd + configfs + f_hid)."""

    def __init__(self, ruta=None):
        self.ruta = ruta or buscar_hidg()
        try:
            self.fd = os.open(self.ruta, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            sys.exit(f"Sin permiso para {self.ruta}. El 'up' le hace chown a tu usuario; ¿lo corriste con sudo desde tu sesión?")

    def mandar(self, rep):
        os.write(self.fd, rep)          # BlockingIOError: nadie lee el hidraw; OSError: el host no configuró el gadget

    def respuesta_get(self, rid, data, esperar_usuario=False):
        """GADGET_HID_WRITE_GET_REPORT. esperar_usuario=False: el kernel contesta solo, de inmediato."""
        s = struct.pack("<BBH64s4x", rid, 1 if esperar_usuario else 0, len(data), data)
        fcntl.ioctl(self.fd, GADGET_HID_WRITE_GET_REPORT, s)

    def eventos(self):
        """SET_REPORT/OUT que manda Resolve (POLLIN) y GET_REPORT sin respuesta preparada (POLLPRI)."""
        p = select.poll()
        p.register(self.fd, select.POLLIN | select.POLLPRI)
        while not salir.is_set():
            for _, ev in p.poll(200):
                if ev & select.POLLPRI:
                    b = bytearray(1)
                    fcntl.ioctl(self.fd, GADGET_HID_READ_GET_REPORT_ID, b, True)
                    log(f"Resolve → GET_REPORT 0x{b[0]:02x} que no conozco: contesto ceros", "aviso")
                    self.respuesta_get(b[0], bytes([b[0]]), esperar_usuario=True)
                if ev & select.POLLIN:
                    try:
                        d = os.read(self.fd, 256)
                    except BlockingIOError:
                        continue
                    except OSError:                 # ENOMEM: el host deshabilitó la función
                        if not estado["desconectado"]:
                            estado["desconectado"] = True
                            log("El host desconectó el panel (¿replug o down?). Espero…", "aviso")
                        time.sleep(0.3)
                        continue
                    if estado["desconectado"]:
                        estado["desconectado"] = False
                        log("El host volvió a conectar el panel.", "ok")
                    procesar_set(self, d)


HELPER_MAC = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "macos", "mcp_hid_mac")
AYUDA_MAC = ("Revisa el README (sección macOS): compílalo con macos/build.sh, que lo firma con el entitlement de HID\n"
             "virtual; con SIP desactivado y, si macOS lo mata al arrancar, también amfi_get_out_of_my_way=1.")


class HidMac:
    """macOS: el HID virtual (IOHIDUserDevice) lo crea macos/mcp_hid_mac. Hablamos con él por stdin/stdout en
    líneas de texto: le mandamos "IN <hex>" y "GET <hex>"; nos contesta "SET <tipo> <hex>", "GETREQ <tipo> <id>"
    y "ERR …". SIN PROBAR: no había una Mac a la mano."""

    def __init__(self, helper):
        helper = os.path.normpath(helper)
        if not os.access(helper, os.X_OK):
            sys.exit(f"No encuentro {helper}.\n{AYUDA_MAC}")
        self.ruta = f"HID virtual de macOS ({os.path.basename(helper)})"
        self.p = subprocess.Popen([helper], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._lock = threading.Lock()
        primera = self.p.stdout.readline().decode(errors="replace").strip()
        if primera != "READY":
            self.p.kill()
            sys.exit(f"El HID virtual de macOS no arrancó: {primera or f'salió con código {self.p.wait()}'}\n{AYUDA_MAC}")

    def _linea(self, txt):
        with self._lock:                         # BrokenPipeError (OSError) si el ayudante se cerró
            self.p.stdin.write(txt.encode() + b"\n")
            self.p.stdin.flush()

    def mandar(self, rep):
        self._linea("IN " + rep.hex())

    def respuesta_get(self, rid, data, esperar_usuario=False):
        self._linea("GET " + data.hex())        # el primer byte de data es el ID

    def eventos(self):
        for crudo in self.p.stdout:
            p = crudo.decode(errors="replace").split()
            if len(p) == 3 and p[0] == "SET":
                procesar_set(self, bytes.fromhex(p[2]))
            elif len(p) == 3 and p[0] == "GETREQ":
                log(f"Resolve → GET_REPORT (tipo {p[1]}) 0x{int(p[2]):02x} que no conozco: contesto ceros", "aviso")
            elif p:
                log("mcp_hid_mac: " + " ".join(p), "aviso" if p[0] == "ERR" else "info")
        estado["desconectado"] = True
        log("El HID virtual de macOS se cerró.", "aviso")


class Simulado:
    """Sin gadget ni Resolve: la interfaz funciona y los reportes se tiran."""
    ruta = "(simulado: no manda nada)"

    def mandar(self, rep):
        pass

    def respuesta_get(self, rid, data, esperar_usuario=False):
        pass


def preparar(dev):
    for rid, data in GET_RESPUESTAS.items():
        dev.respuesta_get(rid, data)


def procesar_set(dev, d):
    if not d:
        return
    rid = d[0]
    estado["ultimo_set"] = time.monotonic()
    repetido = estado["previos"].get(rid) == d
    estado["previos"][rid] = d
    if rid in (0x03, 0x0A):                       # que un GET posterior devuelva lo último fijado
        dev.respuesta_get(rid, d)
        if not repetido:                          # el keepalive de cada 30 s no se vuelve a imprimir
            log(f"Resolve → {'Brightness' if rid == 3 else 'SessionEnabled'}: {d.hex(' ')}", "resolve")
    elif rid == 0x02 and len(d) >= 9:
        if not repetido:
            b = int.from_bytes(d[1:9], "little")
            estado["leds"] = b
            on = [i for i in range(64) if b >> i & 1]
            txt = ", ".join(f"{nombre(i)} (bit {i})" for i in on) if on else "todas apagadas"
            log(f"Resolve → LEDs: {txt}   [{d[1:9].hex(' ')}]", "resolve")
    elif rid == 0x04:
        if not repetido:
            b = d[1] if len(d) > 1 else 0
            estado["indicadores"] = b
            on = [TECLAS_PANEL[k] for k, m in SHIFTS.items() if b & m]
            otros = b & ~0x30
            txt = ", ".join(on) if on else "apagados"
            log(f"Resolve → indicadores: {txt}{f' + 0x{otros:02x}' if otros else ''}   [{d.hex(' ')}]", "resolve")
    else:
        log(f"Resolve → SET 0x{rid:02x} ¿?: {d.hex(' ')}", "aviso")
    if rid == 0x0A and not estado["tomado"]:
        estado["tomado"] = True
        log("✔ Resolve abrió una sesión con el panel (SessionEnabled).", "ok")


def hilo_emisor(dev):
    bloqueado_desde = None
    proximo_boton = 0.0
    while not salir.is_set():
        time.sleep(TICK)
        with lock:
            if botones_cola and time.monotonic() >= proximo_boton:
                bm, espera = botones_cola[0]
                rep, slot, n = bytes([0x02]) + bm.to_bytes(8, "little"), None, 0
            else:
                slot = next((i for i, v in enumerate(pendiente) if v), None)
                if slot is None:
                    if not botones_cola:
                        bloqueado_desde = None
                    continue
                n = pendiente[slot]
                if slot in ANILLOS:
                    v = [0] * 9
                    v[ANILLOS[slot]] = n
                    rep = bytes([0x05]) + struct.pack("<9i", *v)    # 37 B
                else:
                    v = [0] * 16
                    v[slot] = n
                    rep = bytes([0x06]) + struct.pack("<16i", *v)   # 65 B
        try:
            dev.mandar(rep)
        except BlockingIOError:                   # el reporte anterior sigue sin que el host lo lea
            ahora = time.monotonic()
            bloqueado_desde = bloqueado_desde or ahora
            if ahora - bloqueado_desde > 0.5:
                with lock:
                    pendiente[:] = [0] * len(pendiente)   # no acumules giros para soltarlos de golpe después
                    botones_cola.clear()
                    estado["mantenidos"] = 0
                if not estado["bloqueado"]:
                    estado["bloqueado"] = True
                    log("Nadie está leyendo el panel: Resolve (u otro programa) no tiene abierto su hidraw. Descarto lo pendiente.", "aviso")
            continue
        except OSError as e:                      # ESHUTDOWN: el host no ha configurado el gadget
            with lock:
                pendiente[:] = [0] * len(pendiente)
                botones_cola.clear()
                estado["mantenidos"] = 0
            log(f"No se pudo mandar el reporte ({e.strerror}). ¿Está conectado el gadget?", "aviso")
            time.sleep(0.5)
            continue
        bloqueado_desde = None
        if estado["bloqueado"]:
            estado["bloqueado"] = False
            log("El host volvió a leer el panel.", "ok")
        with lock:
            if slot is None:
                botones_cola.popleft()
                proximo_boton = time.monotonic() + espera
            else:
                pendiente[slot] -= n
                estado["enviados"][slot] += n


def unidades(ctrl, pasos):
    """pasos × 360 en las perillas; en los anillos, lo mismo redondeado a múltiplos de 4096."""
    if ctrl in ANILLOS:
        return round(pasos * UNIDAD / UNIDAD_ANILLO) * UNIDAD_ANILLO or (UNIDAD_ANILLO if pasos > 0 else -UNIDAD_ANILLO)
    return pasos * UNIDAD


def girar(slot, pasos):
    with lock:
        pendiente[slot] += unidades(slot, pasos)


def pulsar(bit):
    """Aprieta y suelta una tecla (bit 0-11 = pulsador de perilla, 12-51 = teclas). Si hay un SHIFT
    puesto, la tecla va junto con él y al soltarla se suelta también el SHIFT."""
    with lock:
        m = estado["mantenidos"]
        botones_cola.append((m | 1 << bit, PULSO))
        if m:
            botones_cola.append((m, 0.05))
            botones_cola.append((0, 0.0))
            estado["mantenidos"] = 0
        else:
            botones_cola.append((0, 0.0))
    return m


def combo(mods, bit, dura=PULSO):
    """Mantiene los mods (SHIFT UP/DOWN), aprieta bit y suelta todo, como con los dedos. Al final vuelve a
    dejar puestos los SHIFT que tuviera el teclado."""
    m = sum(1 << b for b in mods)
    estado["flash"][bit] = time.monotonic() + 0.35
    with lock:
        botones_cola.extend([(m, 0.05), (m | 1 << bit, dura), (m, 0.05), (estado["mantenidos"], 0.0)])


def alternar_shift(bit):
    with lock:
        estado["mantenidos"] ^= 1 << bit
        botones_cola.append((estado["mantenidos"], 0.0))
        return bool(estado["mantenidos"] >> bit & 1)


def tecla(bit):
    estado["flash"][bit] = time.monotonic() + 0.35
    if bit in SHIFTS:
        puesto = alternar_shift(bit)
        log(f"{'▼' if puesto else '▲'} {TECLAS_PANEL[bit]} {'puesto (aprieta otra tecla)' if puesto else 'soltado'}", "tecla")
    else:
        m = pulsar(bit)
        con = " + ".join(TECLAS_PANEL[b] for b in SHIFTS if m >> b & 1)
        if con:
            txt, modo = etiqueta(bit, m)
            res = f" → {txt}" if modo == "shift" else "  (sin función con Shift)"
            log(f"▸ {con} + {nombre(bit)}{res}  (bit {bit})", "tecla")
        else:
            log(f"▸ {nombre(bit)}  (bit {bit})", "tecla")


def apretar(slot):
    pulsar(slot)


def esperar_vacio(timeout=3.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        with lock:
            if not any(pendiente) and not botones_cola:
                return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- teclado
def _mover(d):
    estado["slot"] = (estado["slot"] + d) % len(CONTROLES)


def mult_actual():
    if estado["slot"] in ANILLOS:
        return PASOS_ANILLO[estado["paso_anillo"]]
    return PASOS[estado["paso"]]


def _paso(d):
    k, lista = ("paso_anillo", PASOS_ANILLO) if estado["slot"] in ANILLOS else ("paso", PASOS)
    estado[k] = max(0, min(len(lista) - 1, estado[k] + d))


def _girar(n):
    girar(estado["slot"], n * mult_actual())


SECUENCIAS = {
    "\x1b[1;2C": lambda: _girar(10), "\x1b[1;2D": lambda: _girar(-10),
    "\x1b[1;2A": lambda: _paso(1), "\x1b[1;2B": lambda: _paso(-1),
    "\x1b[C": lambda: _girar(1), "\x1b[D": lambda: _girar(-1),
    "\x1bOC": lambda: _girar(1), "\x1bOD": lambda: _girar(-1),
    "\x1b[A": lambda: _mover(-1), "\x1b[B": lambda: _mover(1),
    "\x1bOA": lambda: _mover(-1), "\x1bOB": lambda: _mover(1),
}
RE_ESC = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|O.)?")


ESPECIALES = {                                   # curses.keyname() de las teclas especiales
    "KEY_LEFT": lambda: _girar(-1), "KEY_RIGHT": lambda: _girar(1),
    "KEY_UP": lambda: _mover(-1), "KEY_DOWN": lambda: _mover(1),
    "KEY_SLEFT": lambda: _girar(-10), "KEY_SRIGHT": lambda: _girar(10),
    "kLFT2": lambda: _girar(-10), "kRIT2": lambda: _girar(10),
    "KEY_SR": lambda: _paso(1), "KEY_SF": lambda: _paso(-1),          # Shift+↑ / Shift+↓ en xterm
    "kUP2": lambda: _paso(1), "kDN2": lambda: _paso(-1),
}


def _put(scr, y, x, txt, attr=0):
    h, w = scr.getmaxyx()
    if 0 <= y < h and 0 <= x < w - 1:
        try:
            scr.addnstr(y, x, txt, w - 1 - x, attr)
        except curses.error:
            pass
    return x + len(txt)


def _colores():
    nombres = {"tecla_letra": 0, "led": 0, "shift": 0, "aviso": curses.A_BOLD, "ok": curses.A_BOLD,
               "resolve": 0, "tenue": curses.A_DIM, "tecla": curses.A_BOLD, "info": 0,
               "shift_txt": curses.A_BOLD | curses.A_UNDERLINE}
    try:
        curses.start_color()
        curses.use_default_colors()
        pares = [("tecla_letra", curses.COLOR_CYAN, -1, curses.A_BOLD),
                 ("led", curses.COLOR_BLACK, curses.COLOR_GREEN, 0),
                 ("shift", curses.COLOR_BLACK, curses.COLOR_YELLOW, 0),
                 ("aviso", curses.COLOR_RED, -1, curses.A_BOLD),
                 ("ok", curses.COLOR_GREEN, -1, curses.A_BOLD),
                 ("resolve", curses.COLOR_MAGENTA, -1, 0),
                 ("shift_txt", curses.COLOR_YELLOW, -1, curses.A_BOLD)]
        for i, (k, fg, bg, extra) in enumerate(pares, start=1):
            curses.init_pair(i, fg, bg)
            nombres[k] = curses.color_pair(i) | extra
    except curses.error:
        pass
    return nombres


def _rejilla(scr, y, celdas, ancho, C):
    """celdas: (letra o None, texto, atributo). Toda la fila si cabe; si no, mitades parejas (5+5, 6+6)."""
    w = scr.getmaxyx()[1]
    caben = max(1, (w - 2) // ancho)
    n = len(celdas)
    por_fila = n if caben >= n else n // 2 if caben >= n // 2 else caben
    for i, (letra, txt, at) in enumerate(celdas):
        fila, col = divmod(i, por_fila)
        x = 1 + col * ancho
        if letra:
            _put(scr, y + fila, x, letra, C["tecla_letra"])
            _put(scr, y + fila, x + 2, txt[:ancho - 3], at)
        else:
            _put(scr, y + fila, x, f" {txt} "[:ancho - 1], at)
    return y + (len(celdas) + por_fila - 1) // por_fila


def dibujar(scr, C):
    scr.erase()
    h, w = scr.getmaxyx()
    ahora = time.monotonic()
    s, m = estado["slot"], estado["mantenidos"]
    # título y conexión
    x = _put(scr, 0, 0, " Micro Color Panel falso ", curses.A_REVERSE | curses.A_BOLD)
    x = _put(scr, 0, x + 1, estado["ruta"], C["tenue"])
    if estado["desconectado"]:
        txt, at = "HOST DESCONECTADO", C["aviso"]
    elif estado["bloqueado"]:
        txt, at = "NADIE LEE EL PANEL (¿Resolve no lo abrió?)", C["aviso"]
    elif estado["ultimo_set"] is None:
        txt, at = "Resolve: sin noticias todavía", C["tenue"]
    else:
        txt = f"Resolve: {'✔ sesión abierta' if estado['tomado'] else 'conectado'} · último mensaje hace {ahora - estado['ultimo_set']:.0f} s"
        at = C["ok"]
    _put(scr, 0, x + 3, txt, at)
    if estado["c3"]:
        _put(scr, 1, 1, estado["c3"], C["tenue"])
    # control elegido
    mult = mult_actual()
    x = _put(scr, 2, 1, "Anillo" if s in ANILLOS else "Perilla", C["tenue"])
    x = _put(scr, 2, x + 1, f" {CONTROLES[s]} ", curses.A_REVERSE | curses.A_BOLD)
    x = _put(scr, 2, x + 2, f"paso ×{mult} = {abs(unidades(s, mult)):,} por toque")
    x = _put(scr, 2, x + 3, f"enviado {estado['enviados'][s]:+,}", C["tenue"])
    if m:
        _put(scr, 2, x + 3, " " + " + ".join(TECLAS_PANEL[b] for b in SHIFTS if m >> b & 1) + " puesto ", C["shift"])
    _put(scr, 3, 1, "←→ girar · ↑↓ elegir · Shift+←→ ×10 · Shift+↑↓ paso · espacio apretar · Tab mapa · Ctrl+C salir",
         C["tenue"])
    y = 4
    if estado["mapa"]:
        sel = lambda i: curses.A_REVERSE | curses.A_BOLD if i == s else 0
        y = _rejilla(scr, y + 1, [(None, CORTOS[i], sel(i)) for i in range(len(PERILLAS))], 11, C)
        x = _put(scr, y, 1, "Anillos", C["tenue"])
        for i in ANILLOS:
            x = _put(scr, y, x + 2, f" {CORTOS[i]} ", sel(i))
        _put(scr, y, x + 3, "con OFFSET (S) y la paleta HDR: Global Temp · Tint · Exp", C["tenue"])
        y += 2
        x = _put(scr, y, 1, "Teclas", curses.A_BOLD)
        puestos = [b for b in SHIFTS if m >> b & 1]
        if puestos:
            x = _put(scr, y, x + 2, " " + " + ".join(TECLAS_PANEL[b] for b in puestos) + " puesto ", C["shift"])
            _put(scr, y, x + 2, "en amarillo, lo que hace cada tecla con Shift · en gris, las que no tienen función", C["tenue"])
        else:
            x = _put(scr, y, x + 2, " LED de Resolve ", C["led"])
            x = _put(scr, y, x + 1, " SHIFT puesto ", C["shift"])
            _put(scr, y, x + 2, "C/V = SHIFT UP/DOWN: se quedan puestas y cambian el texto de las teclas", C["tenue"])
        y += 1
        ancho = max(8, min(16, (w - 2) // 10))
        for _, chars, bits in FILAS:
            x = 1
            for c, b in zip(chars, bits):
                txt, modo = etiqueta(b, m)
                if estado["flash"].get(b, 0) > ahora:
                    at = curses.A_REVERSE | curses.A_BOLD
                elif b in SHIFTS and m >> b & 1:
                    at = C["shift"]
                elif modo == "shift":
                    at = C["shift_txt"]
                elif modo == "sin":
                    at = C["tenue"]
                elif estado["leds"] >> b & 1:
                    at = C["led"]
                else:
                    at = 0
                _put(scr, y, x, c.upper(), C["tecla_letra"])
                _put(scr, y, x + 2, txt[:ancho - 3], at)
                x += ancho
            y += 1
    # registro
    y += 1
    _put(scr, y, 0, "─" * (w - 1), C["tenue"])
    _put(scr, y, 2, " Registro ", C["tenue"])
    y += 1
    filas = h - y
    with _salida:
        lineas = list(registro)[-filas:] if filas > 0 else []
    for i, (txt, tipo) in enumerate(lineas):
        _put(scr, y + i, 1, txt, C.get(tipo, 0))
    scr.noutrefresh()
    curses.doupdate()


def _bucle(scr):
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    scr.keypad(True)
    scr.timeout(50)
    C = _colores()
    while not salir.is_set():
        dibujar(scr, C)
        try:
            k = scr.get_wch()
        except curses.error:
            continue
        if isinstance(k, int):
            accion = ESPECIALES.get(curses.keyname(k).decode(errors="ignore"))
            if accion:
                accion()
            continue
        if k == "\x1b":                         # secuencia que curses no conoce: júntala y que no se cuele como teclas
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
            procesar_entrada(buf)
        else:
            procesar_entrada(k)


def interactivo():
    global _interactivo
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    _interactivo = True
    try:
        curses.wrapper(_bucle)
    finally:
        with _salida:
            _interactivo = False
            ultimas = list(registro)[-12:]
        for linea, _ in ultimas:                 # que el registro no se pierda al salir
            print(linea)


def procesar_entrada(buf):
    i = 0
    while i < len(buf):
        c = buf[i]
        if c == "\x1b":                           # flechas y demás secuencias; las desconocidas se ignoran
            m = RE_ESC.match(buf, i)
            accion = SECUENCIAS.get(m.group(0))
            if accion:
                accion()
            i = m.end()
            continue
        i += 1
        if c == "\x04":                           # Ctrl+D
            salir.set()
            return
        if c == "\t":
            estado["mapa"] = not estado["mapa"]
        elif c == " ":
            if estado["slot"] in ANILLOS:
                log("Los anillos no se aprietan; espacio solo sirve con las perillas.", "aviso")
            else:
                tecla(estado["slot"])
        elif c.lower() in TECLA_A_BIT:
            tecla(TECLA_A_BIT[c.lower()])


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Firmware del Micro Color Panel falso (lado gadget).")
    ap.add_argument("--hidg", help="nodo /dev/hidgN (por defecto lo busca en configfs)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--mapa", action="store_true", help="imprimir el mapa de teclas y salir")
    g.add_argument("--escuchar", action="store_true", help="solo registrar lo que manda Resolve")
    g.add_argument("--enviar", nargs=2, metavar=("PERILLA", "PASOS"), help="p. ej. --enviar 9 +5")
    g.add_argument("--apretar", type=int, metavar="PERILLA")
    g.add_argument("--boton", type=int, metavar="BIT", help="0-11 pulsadores de perilla, 12-51 teclas")
    ap.add_argument("--simular", action="store_true", help="sin gadget: prueba la interfaz, no manda nada")
    ap.add_argument("--c3", nargs="?", const="auto", metavar="PUERTO",
                    help="lee también los encoders y botones de la ESP32-C3 (ver tools/c3_entrada.py); por defecto "
                         "/dev/ttyACM0 en Linux y el primer /dev/cu.usbmodem* en macOS")
    ap.add_argument("--mac-helper", metavar="RUTA",
                    help="usa el HID virtual de macOS con este ayudante (por defecto macos/mcp_hid_mac, solo en macOS)")
    ap.add_argument("--uno-por-reporte", action="store_true",
                    help="con --enviar: un reporte por paso (por defecto, todos juntos en uno)")
    a = ap.parse_args()

    if a.mapa:
        print(texto_mapa())
        return

    global _interactivo
    modo_teclado = not (a.enviar or a.apretar is not None or a.boton is not None or a.escuchar)
    if modo_teclado and not sys.stdin.isatty():
        sys.exit("No hay terminal: usa --escuchar, --enviar, --apretar o --boton.")
    _interactivo = modo_teclado                  # desde aquí el registro va a la zona de abajo

    if a.simular:
        dev = Simulado()
        log("Modo simulado: la interfaz funciona pero no hay gadget ni Resolve.", "aviso")
    else:
        if a.mac_helper or sys.platform == "darwin":
            dev = HidMac(a.mac_helper or HELPER_MAC)
        else:
            dev = GadgetLinux(a.hidg)
        preparar(dev)
        log(f"Panel falso en {dev.ruta}. Respuestas GET listas: " + ", ".join(f"0x{r:02x}" for r in GET_RESPUESTAS))
        threading.Thread(target=dev.eventos, daemon=True).start()
    estado["ruta"] = dev.ruta
    threading.Thread(target=hilo_emisor, args=(dev,), daemon=True).start()
    if a.c3:
        import c3_entrada
        threading.Thread(target=c3_entrada.hilo, args=(a.c3, sys.modules[__name__]), daemon=True).start()

    try:
        if a.enviar:
            slot, pasos = int(a.enviar[0]), int(a.enviar[1])
            if not 0 <= slot < len(CONTROLES):
                sys.exit("PERILLA va de 0 a 14: 0-11 perillas, 12-14 anillos (los pasos son ×360, en anillos redondeados a ×4096)")
            if a.uno_por_reporte:
                for _ in range(abs(pasos)):
                    girar(slot, 1 if pasos > 0 else -1)
                    esperar_vacio()
            else:
                girar(slot, pasos)
            ok = esperar_vacio()
            log(f"{CONTROLES[slot]}: {estado['enviados'][slot]:+,} unidades enviadas" + ("" if ok else " (incompleto: nadie leyó)"))
            sys.exit(0 if ok else 1)
        elif a.apretar is not None or a.boton is not None:
            bit = a.apretar if a.apretar is not None else a.boton
            if not 0 <= bit < 64:
                sys.exit("el bit va de 0 a 63")
            pulsar(bit)
            ok = esperar_vacio()
            log(f"{nombre(bit)} (bit {bit}): apretado y soltado" + ("" if ok else " (incompleto: nadie leyó)"))
            sys.exit(0 if ok else 1)
        elif a.escuchar:
            log("Escuchando (Ctrl+C para salir)…")
            while True:
                time.sleep(1)
        else:
            interactivo()
    except KeyboardInterrupt:
        pass
    finally:
        salir.set()


if __name__ == "__main__":
    main()
