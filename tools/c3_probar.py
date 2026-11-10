#!/usr/bin/env python3
"""c3_probar.py: imprime lo que haces con cada encoder y botón de la ESP32-C3 (firmware/c3).

Uso:  python3 tools/c3_probar.py [PUERTO]      Ctrl+C para salir
      (por defecto /dev/ttyACM0 en Linux y el primer /dev/cu.usbmodem* en macOS)
"""
import os, sys, termios, time

from c3_entrada import puerto_por_defecto

PUERTO = sys.argv[1] if len(sys.argv) > 1 else puerto_por_defecto()
NOMBRE = {"P1": "Pulsador encoder 1", "P2": "Pulsador encoder 2", "P3": "Pulsador encoder 3",
          "B1": "Botón 1", "B2": "Botón 2", "B3": "Botón 3", "B4": "Botón 4"}
total = {1: 0, 2: 0, 3: 0}


def abrir():
    fd = os.open(PUERTO, os.O_RDWR | os.O_NOCTTY)
    a = termios.tcgetattr(fd)
    a[0] = a[1] = a[3] = 0                                  # crudo: sin eco ni traducciones
    a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    a[4] = a[5] = termios.B115200
    a[6][termios.VMIN], a[6][termios.VTIME] = 1, 0
    termios.tcsetattr(fd, termios.TCSANOW, a)
    os.write(fd, b"?\n")                                     # pide el estado de los pines
    return fd


def mostrar(linea):
    t = time.strftime("%H:%M:%S")
    p = linea.split()
    if not p:
        return
    if p[0] in ("E1", "E2", "E3") and len(p) == 3:
        n, clics, cuartos = int(p[0][1]), int(p[1]), int(p[2][1:])
        if clics:
            total[n] += clics
            flecha = "▶ +" if clics > 0 else "◀ "
            print(f"{t}  Encoder {n}  {flecha}{clics} clic   (cuartos {cuartos:+d}, total {total[n]:+d})")
        else:
            print(f"{t}  Encoder {n}  · se movió {cuartos:+d} cuartos sin llegar al reposo")
    elif p[0] in NOMBRE and len(p) == 2:
        print(f"{t}  {NOMBRE[p[0]]}  {'APRETADO' if p[1] == '1' else 'soltado'}")
    elif p[0] == "PINES":
        print(f"{t}  Pines en reposo (1 = suelto, 0 = cerrado a GND):")
        for campo in p[1:]:
            nombre, resto = campo.split(":")
            gpio, nivel = resto.split("=")
            aviso = "" if nivel == "1" or nombre[0] == "E" else "   ← ¿apretado o en corto a GND?"
            print(f"            {nombre:4} GPIO{gpio:>2} = {nivel}{aviso}")
        print("          Gira y aprieta lo que quieras:")
    else:
        print(f"{t}  {linea}")


def main():
    print(f"Escuchando {PUERTO}  (Ctrl+C para salir)")
    while True:
        try:
            fd = abrir()
        except OSError as e:
            print(f"  no puedo abrir {PUERTO}: {e.strerror}; reintento…")
            time.sleep(1)
            continue
        buf = b""
        try:
            while True:
                d = os.read(fd, 256)
                if not d:
                    raise OSError("se cerró")
                buf += d
                while b"\n" in buf:
                    linea, buf = buf.split(b"\n", 1)
                    mostrar(linea.decode(errors="replace").strip())
        except OSError:
            print("  se desconectó; espero a que vuelva…")
            os.close(fd)
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
