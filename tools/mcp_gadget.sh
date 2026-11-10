#!/usr/bin/env bash
# mcp_gadget.sh: Micro Color Panel falso (1edb:da0f) como USB virtual en esta misma PC.
#
# dummy_hcd crea un controlador USB de mentira; configfs + usb_f_hid le cuelgan un dispositivo HID.
# Para Resolve es un panel conectado: tiene padre USB con idVendor/idProduct, que es lo que
# HIDLinuxManager busca en udev (un uhid no lo tendría). El "firmware" es tools/mcp_falso.py.
#
# Uso:
#   sudo tools/mcp_gadget.sh up       crea el panel y lo "conecta"
#   sudo tools/mcp_gadget.sh replug   desconecta y reconecta el cable virtual (sin cerrar Resolve)
#   sudo tools/mcp_gadget.sh down     lo borra
#   tools/mcp_gadget.sh status        cómo está (no necesita sudo)
#
# Probado con DaVinci Resolve Studio 21.2 en Fedora 44. Descriptores de dispositivo y features: captura de ra100.
set -euo pipefail

G=/sys/kernel/config/usb_gadget/mcp_falso
F=$G/functions/hid.mcp
REGLA=/etc/udev/rules.d/70-mcp-falso.rules
LINEA_REGLA='KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1edb", ATTRS{idProduct}=="da0f", MODE="0660", TAG+="uaccess"'

# Descriptor de reporte escrito a mano: el real (297 B) no es público. Declara los mismos IDs y
# tamaños que se ven en la captura. Resolve no lee el descriptor (no hay HIDIOCGRDESC en
# libDaVinciPanels.so); solo lo necesita el kernel para crear el hidraw.
DESC='
06 00 FF  09 01  A1 01             Usage Page 0xFF00, Usage 1, Collection (Application)
15 00  26 FF 00  75 08             0..255, 8 bits
85 02  09 01  95 08  81 02         Input   0x02 botones (bitmap 64 bits)    8 B
85 05  09 02  95 24  81 02         Input   0x05 trackballs (9 x int32)     36 B
85 06  09 03  95 40  81 02         Input   0x06 perillas (16 x int32)      64 B
85 02  09 04  95 08  91 02         Output  0x02 LEDs de teclas              8 B
85 04  09 05  95 06  91 02         Output  0x04 indicadores                 6 B
85 01  09 06  95 07  B1 02         Feature 0x01 SystemInfo                  7 B
85 03  09 07  95 02  B1 02         Feature 0x03 Brightness                  2 B
85 08  09 08  95 02  B1 02         Feature 0x08 BatteryStatus               2 B
85 0A  09 09  95 01  B1 02         Feature 0x0A SessionEnabled              1 B
C0                                 End Collection
'

die() { echo "Error: $*" >&2; exit 1; }
como_root() { [[ $EUID -eq 0 ]] || die "córrelo con sudo: sudo $0 $1"; }

desc_bytes() {   # de cada línea toma los pares hex hasta el primer token que no lo sea (el comentario)
    awk '{ for (i = 1; i <= NF && $i ~ /^[0-9A-Fa-f][0-9A-Fa-f]$/; i++) printf "%s", $i }' <<<"$DESC" | xxd -r -p
}

udc() { ls /sys/class/udc 2>/dev/null | grep -m1 '^dummy_udc' || true; }

hidg_node() {
    local mm c
    mm=$(cat "$F/dev" 2>/dev/null) || return 1
    for c in /sys/class/hidg/hidg*; do
        [[ -e $c/dev && $(cat "$c/dev") == "$mm" ]] && { echo "/dev/${c##*/}"; return 0; }
    done
    return 1
}

hidraw_panel() {   # imprime los hidraw con HID_ID del panel; marca el que cuelga de dummy_hcd
    local d
    for d in /sys/class/hidraw/hidraw*; do
        [[ -e $d/device/uevent ]] || continue
        grep -q '^HID_ID=0003:00001EDB:0000DA0F$' "$d/device/uevent" || continue
        if [[ $(readlink -f "$d") == *dummy_hcd* ]]; then echo "/dev/${d##*/} (falso)"; else echo "/dev/${d##*/} (¿real?)"; fi
    done
}

instalar_regla() {
    if ! grep -qxF "$LINEA_REGLA" "$REGLA" 2>/dev/null; then
        printf '# Micro Color Panel (real o falso): el usuario de la sesión puede abrir su hidraw.\n# Va antes de 73-seat-late.rules para que uaccess surta efecto. Creada por tools/mcp_gadget.sh.\n%s\n' \
            "$LINEA_REGLA" > "$REGLA"
        udevadm control --reload
        echo "Regla udev instalada: $REGLA"
    fi
}

up() {
    como_root up
    local usuario=${SUDO_USER:-}
    [[ -n $usuario && $usuario != root ]] || die "córrelo con sudo desde tu usuario (falta SUDO_USER)"
    [[ ! -d $G ]] || die "el gadget ya existe; bórralo con: sudo $0 down"
    if pgrep -f '^/opt/resolve/bin/resolve' >/dev/null; then   # su comm es "GUI Thread": pgrep -x no sirve
        echo "Aviso: Resolve está abierto. Va a abrir el panel en cuanto aparezca; si mcp_falso.py no ha corrido"
        echo "       nunca, la Feature 1 (SystemInfo) se contesta con ceros tras 2.5 s. Mejor cierra Resolve."
    fi

    instalar_regla
    modprobe -a libcomposite usb_f_hid dummy_hcd
    for _ in $(seq 50); do [[ -n $(udc) ]] && break; sleep 0.1; done
    local u; u=$(udc); [[ -n $u ]] || die "no apareció dummy_udc en /sys/class/udc"

    mkdir "$G"
    echo 0x1edb > "$G/idVendor"
    echo 0xda0f > "$G/idProduct"
    echo 0x0100 > "$G/bcdDevice"
    echo 0x0200 > "$G/bcdUSB"
    echo 0xEF   > "$G/bDeviceClass"      # compuesto con IAD, como el real (aquí solo hay HID; Linux no lo revisa)
    echo 0x02   > "$G/bDeviceSubClass"
    echo 0x01   > "$G/bDeviceProtocol"
    echo 64     > "$G/bMaxPacketSize0"
    mkdir "$G/strings/0x409"
    echo "Blackmagic Design"                  > "$G/strings/0x409/manufacturer"   # adivinado: la captura no trae strings
    echo "DaVinci Resolve Micro Color Panel"  > "$G/strings/0x409/product"        # adivinado
    echo "MCPFALSO0000001"                    > "$G/strings/0x409/serialnumber"

    mkdir "$F"
    echo 0  > "$F/protocol"
    echo 0  > "$F/subclass"
    echo 65 > "$F/report_length"      # el 0x06 mide 65 B con el ID: cabe en un paquete a High Speed
    echo 1  > "$F/no_out_endpoint"    # con EP OUT, f_hid hace STALL a los SET_REPORT de features
    desc_bytes > "$F/report_desc"

    mkdir "$G/configs/c.1"
    echo 100  > "$G/configs/c.1/MaxPower"
    echo 0x80 > "$G/configs/c.1/bmAttributes"
    ln -s "$F" "$G/configs/c.1/"

    echo "$u" > "$G/UDC"

    local n=""
    for _ in $(seq 50); do n=$(hidg_node || true); [[ -n $n && -e $n ]] && break; sleep 0.1; done
    [[ -n $n ]] || die "no apareció /dev/hidgN"
    chown "$usuario" "$n"
    echo "Lado panel:  $n (dueño $usuario)"

    local h=""
    for _ in $(seq 50); do h=$(hidraw_panel | grep falso || true); [[ -n $h ]] && break; sleep 0.1; done
    [[ -n $h ]] || die "el host no creó el hidraw; revisa: sudo dmesg | tail -30"
    udevadm settle
    echo "Lado Resolve: $h"
    getfacl -p "${h%% *}" 2>/dev/null | grep -E "^user:$usuario" || echo "  Ojo: sin ACL para $usuario (¿no hay sesión activa en el seat?)"
    lsusb -d 1edb:da0f || true
    echo
    echo "Siguiente: python3 tools/mcp_falso.py   (y luego abre Resolve)"
}

down() {
    como_root down
    if [[ -d $G ]]; then
        echo "" > "$G/UDC" 2>/dev/null || true
        rm -f "$G/configs/c.1/hid.mcp"
        rmdir "$G/configs/c.1" "$F" "$G/strings/0x409" "$G" 2>/dev/null || true
    fi
    [[ -d $G ]] && die "no pude borrar $G (¿algo lo tiene abierto?)"
    modprobe -r dummy_hcd 2>/dev/null || true
    echo "Gadget borrado."
    echo "La regla $REGLA se queda (también sirve para el ESP32). Para quitarla:"
    echo "  sudo rm $REGLA && sudo udevadm control --reload"
}

replug() {
    como_root replug
    [[ -d $G ]] || die "no hay gadget; usa: sudo $0 up"
    local usuario=${SUDO_USER:-} u; u=$(udc)
    [[ -n $u ]] || die "no hay dummy_udc"
    # soft_connect disconnect/connect deja a dummy_hcd sin poder enumerar (error -71): mejor desligar y religar
    echo "" > "$G/UDC" 2>/dev/null || true
    sleep 1
    echo "$u" > "$G/UDC"
    local n=""
    for _ in $(seq 50); do n=$(hidg_node || true); [[ -n $n && -e $n ]] && break; sleep 0.1; done
    [[ -n $n && -n $usuario ]] && chown "$usuario" "$n"
    local h=""
    for _ in $(seq 50); do h=$(hidraw_panel | grep falso || true); [[ -n $h ]] && break; sleep 0.1; done
    echo "Reconectado en $u: ${n:-sin hidg} · ${h:-sin hidraw}"
}

status() {
    if [[ -d $G ]]; then
        echo "Gadget: $G (UDC: $(cat "$G/UDC" 2>/dev/null || echo ninguno))"
        local n; n=$(hidg_node || true)
        [[ -n $n ]] && ls -l "$n"
    else
        echo "Gadget: no existe"
    fi
    hidraw_panel | while read -r h tag; do ls -l "$h"; getfacl -p "$h" 2>/dev/null | grep '^user:' || true; done
    lsusb -d 1edb:da0f || true
    [[ -f $REGLA ]] && echo "Regla: $REGLA" || echo "Regla: no instalada"
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    replug) replug ;;
    status) status ;;
    desc) desc_bytes | xxd ;;   # para revisar el descriptor sin root
    *) echo "uso: sudo $0 up|replug|down   ·   $0 status|desc" >&2; exit 2 ;;
esac
