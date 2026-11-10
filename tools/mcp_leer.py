#!/usr/bin/env python3
"""
mcp_leer.py: hace de "Resolve" contra el Micro Color Panel (falso o real) por hidraw.

Sirve para probar el ciclo completo sin abrir Resolve:
  1. Busca el hidraw con HID_ID 0003:00001EDB:0000DA0F (prefiere el que cuelga de dummy_hcd).
  2. HIDIOCGRAWINFO: lo mismo que revisa Resolve en HIDLinuxManager::OnDeviceAdded.
  3. Repite el arranque de la captura de ra100: SET F3, OUT 04, OUT 02, GET F1, SET F0A, GET F8, SET F3.
  4. Decodifica lo que mande el panel (0x02 botones, 0x05 bolas, 0x06 perillas).

Uso:
  python3 tools/mcp_leer.py                  arranque + lectura hasta Ctrl+C
  python3 tools/mcp_leer.py --segundos 5     arranque + lectura 5 s
  python3 tools/mcp_leer.py --sin-arranque   solo lectura

Mejor con Resolve cerrado: si los dos piden features a la vez, se cruzan las respuestas.
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
PERILLAS = ["Y Lift", "Y Gamma", "Y Gain", "Contrast", "Pivot", "Mid Detail",
            "Color Boost", "Shadows", "Highlights", "Saturation", "Hue", "Lum Mix"]
F1_CAPTURA = bytes.fromhex("01 15 5b 3b fd 02 02 00")
F8_CAPTURA = bytes.fromhex("08 01 63")


def hidiocgfeature(n):
    return (3 << 30) | (n << 16) | (ord("H") << 8) | 0x07


def hidiocsfeature(n):
    return (3 << 30) | (n << 16) | (ord("H") << 8) | 0x06


def buscar():
    hallados = []
    for d in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            if "HID_ID=0003:00001EDB:0000DA0F" in open(f"{d}/device/uevent").read():
                hallados.append(("dummy_hcd" in os.path.realpath(d), "/dev/" + os.path.basename(d)))
        except OSError:
            pass
    if not hallados:
        sys.exit("No hay ningún hidraw 1edb:da0f. ¿Corriste sudo tools/mcp_gadget.sh up?")
    hallados.sort(reverse=True)
    falso, ruta = hallados[0]
    return ruta, falso


def get_feature(fd, rid, n):
    buf = bytearray([rid] + [0] * (n - 1))
    t = time.monotonic()
    r = fcntl.ioctl(fd, hidiocgfeature(n), buf, True)
    return bytes(buf[:r if r > 0 else n]), time.monotonic() - t


def set_feature(fd, data):
    fcntl.ioctl(fd, hidiocsfeature(len(data)), bytearray(data), True)


def paso(desc, fn):
    try:
        r = fn()
        print(f"  ok   {desc}" + (f"  {r}" if r else ""))
        return True
    except OSError as e:
        print(f"  FALLA {desc}: {e}")
        return False


def arranque(fd):
    print("Arranque como en la captura (t=7.77 s):")
    paso("SET Feature 3  03 00 00", lambda: set_feature(fd, b"\x03\x00\x00"))
    paso("OUT 0x04       04 00 00 00 00 00 00", lambda: os.write(fd, bytes.fromhex("04 00 00 00 00 00 00")) and None)
    paso("OUT 0x02       02 + 8 ceros", lambda: os.write(fd, b"\x02" + bytes(8)) and None)

    def f1():
        d, dt = get_feature(fd, 0x01, 8)
        nota = "igual a la captura" if d == F1_CAPTURA else f"DISTINTO de la captura ({F1_CAPTURA.hex(' ')})"
        lento = f" · tardó {dt:.2f} s (¿mcp_falso.py nunca corrió?)" if dt > 1 else ""
        return f"{d.hex(' ')}  ({nota}{lento})"
    paso("GET Feature 1  (SystemInfo)", f1)
    paso("SET Feature 0x0A  0a 01", lambda: set_feature(fd, b"\x0a\x01"))

    def f8():
        d, _ = get_feature(fd, 0x08, 3)
        return f"{d.hex(' ')}  ({'igual a la captura' if d == F8_CAPTURA else 'distinto: ' + F8_CAPTURA.hex(' ')})"
    paso("GET Feature 8  (BatteryStatus)", f8)
    paso("SET Feature 3  03 64 64", lambda: set_feature(fd, b"\x03\x64\x64"))


def leer(fd, segundos):
    print("\nLeyendo reportes del panel (Ctrl+C para salir)…")
    totales = [0] * 16
    fin = time.monotonic() + segundos if segundos else None
    previo = 0
    while fin is None or time.monotonic() < fin:
        r, _, _ = select.select([fd], [], [], 0.2)
        if not r:
            continue
        d = os.read(fd, 128)
        t = time.strftime("%H:%M:%S")
        if d[0] == 0x06:
            v = struct.unpack("<16i", d[1:65])
            nz = [(i, x) for i, x in enumerate(v) if x]
            for i, x in nz:
                totales[i] += x
                nombre = PERILLAS[i] if i < 12 else "¿?"
                extra = "" if x % 360 == 0 else "  (¡no es múltiplo de 360!)"
                print(f"{t} perilla {i:2d} {nombre:<11} {x:+8d} = {x // 360:+d} pasos · total {totales[i] // 360:+d}{extra}")
            if len(nz) != 1:
                print(f"{t} 0x06 con {len(nz)} slots distintos de cero")
            if len(d) != 65:
                print(f"{t} ¡el 0x06 midió {len(d)} B, se esperaban 65!")
        elif d[0] == 0x02:
            b = int.from_bytes(d[1:9], "little")
            on = [i for i in range(64) if (b >> i) & 1 and not (previo >> i) & 1]
            off = [i for i in range(64) if not (b >> i) & 1 and (previo >> i) & 1]
            print(f"{t} botones: apretado {on or '-'} · soltado {off or '-'}")
            previo = b
        elif d[0] == 0x05:
            print(f"{t} bolas {struct.unpack('<9i', d[1:37])}")
        else:
            print(f"{t} reporte desconocido {d.hex(' ')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sin-arranque", action="store_true")
    ap.add_argument("--segundos", type=float, default=0)
    a = ap.parse_args()

    ruta, falso = buscar()
    print(f"{ruta} ({'falso, en dummy_hcd' if falso else 'parece un panel real'})")
    try:
        fd = os.open(ruta, os.O_RDWR)
    except PermissionError:
        sys.exit(f"Sin permiso para {ruta}: falta la regla udev con uaccess (la instala mcp_gadget.sh up).")
    info = bytearray(8)
    fcntl.ioctl(fd, HIDIOCGRAWINFO, info, True)
    bus, vid, pid = struct.unpack("<IHH", info)
    ok = bus == 3 and vid == 0x1EDB and pid == 0xDA0F
    print(f"HIDIOCGRAWINFO: bus {bus} vid {vid:04x} pid {pid:04x} ({'lo que espera Resolve' if ok else 'NO es lo que espera Resolve'})")
    if not a.sin_arranque:
        arranque(fd)
    try:
        leer(fd, a.segundos)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
