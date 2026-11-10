#!/usr/bin/env bash
# mcp_gadget.sh: fake Micro Color Panel (1edb:da0f) as a virtual USB device on this same PC.
#
# dummy_hcd creates a fake USB controller; configfs + usb_f_hid hang an HID device off it.
# To Resolve it's a plugged-in panel: it has a USB parent with idVendor/idProduct, which is what
# HIDLinuxManager looks for in udev (a uhid device wouldn't). The "firmware" is tools/fake_panel.py.
#
# Usage:
#   sudo tools/mcp_gadget.sh up       create the panel and "plug it in"
#   sudo tools/mcp_gadget.sh replug   unplug and replug the virtual cable (without closing Resolve)
#   sudo tools/mcp_gadget.sh down     delete it
#   tools/mcp_gadget.sh status        show its state (no sudo needed)
#
# Tested with DaVinci Resolve Studio 21.2 on Fedora 44. Device descriptors and features: ra100 capture.
set -euo pipefail

G=/sys/kernel/config/usb_gadget/esp_color_panel
F=$G/functions/hid.mcp
RULE=/etc/udev/rules.d/70-esp-color-panel.rules
OLD_RULE=/etc/udev/rules.d/70-mcp-falso.rules   # name before the rename
RULE_LINE='KERNEL=="hidraw*", SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1edb", ATTRS{idProduct}=="da0f", MODE="0660", TAG+="uaccess"'

# Hand-written report descriptor: the real one (297 B) isn't public. It declares the same IDs and
# sizes seen in the capture. Resolve doesn't read the descriptor (no HIDIOCGRDESC in
# libDaVinciPanels.so); only the kernel needs it to create the hidraw.
DESC='
06 00 FF  09 01  A1 01             Usage Page 0xFF00, Usage 1, Collection (Application)
15 00  26 FF 00  75 08             0..255, 8 bits
85 02  09 01  95 08  81 02         Input   0x02 buttons (64-bit bitmap)     8 B
85 05  09 02  95 24  81 02         Input   0x05 trackballs (9 x int32)     36 B
85 06  09 03  95 40  81 02         Input   0x06 knobs (16 x int32)         64 B
85 02  09 04  95 08  91 02         Output  0x02 key LEDs                    8 B
85 04  09 05  95 06  91 02         Output  0x04 indicators                  6 B
85 01  09 06  95 07  B1 02         Feature 0x01 SystemInfo                  7 B
85 03  09 07  95 02  B1 02         Feature 0x03 Brightness                  2 B
85 08  09 08  95 02  B1 02         Feature 0x08 BatteryStatus               2 B
85 0A  09 09  95 01  B1 02         Feature 0x0A SessionEnabled              1 B
C0                                 End Collection
'

die() { echo "Error: $*" >&2; exit 1; }
as_root() { [[ $EUID -eq 0 ]] || die "run it with sudo: sudo $0 $1"; }

desc_bytes() {   # from each line take the hex pairs up to the first token that isn't one (the comment)
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

hidraw_panel() {   # print the hidraws with the panel's HID_ID; tag the one under dummy_hcd
    local d
    for d in /sys/class/hidraw/hidraw*; do
        [[ -e $d/device/uevent ]] || continue
        grep -q '^HID_ID=0003:00001EDB:0000DA0F$' "$d/device/uevent" || continue
        if [[ $(readlink -f "$d") == *dummy_hcd* ]]; then echo "/dev/${d##*/} (fake)"; else echo "/dev/${d##*/} (real?)"; fi
    done
}

install_rule() {
    if ! grep -qxF "$RULE_LINE" "$RULE" 2>/dev/null; then
        printf '# Micro Color Panel (real or fake): the session user can open its hidraw.\n# Goes before 73-seat-late.rules so uaccess takes effect. Created by tools/mcp_gadget.sh.\n%s\n' \
            "$RULE_LINE" > "$RULE"
        udevadm control --reload
        echo "udev rule installed: $RULE"
    fi
}

up() {
    as_root up
    local user=${SUDO_USER:-}
    [[ -n $user && $user != root ]] || die "run it with sudo from your own user (SUDO_USER is missing)"
    [[ ! -d $G ]] || die "the gadget already exists; delete it with: sudo $0 down"
    if pgrep -f '^/opt/resolve/bin/resolve' >/dev/null; then   # its comm is "GUI Thread": pgrep -x won't work
        echo "Warning: Resolve is open. It will open the panel as soon as it appears; if fake_panel.py has never"
        echo "         run, Feature 1 (SystemInfo) gets answered with zeros after 2.5 s. Better close Resolve."
    fi

    rm -f "$OLD_RULE"
    install_rule
    modprobe -a libcomposite usb_f_hid dummy_hcd
    for _ in $(seq 50); do [[ -n $(udc) ]] && break; sleep 0.1; done
    local u; u=$(udc); [[ -n $u ]] || die "dummy_udc didn't show up in /sys/class/udc"

    mkdir "$G"
    echo 0x1edb > "$G/idVendor"
    echo 0xda0f > "$G/idProduct"
    echo 0x0100 > "$G/bcdDevice"
    echo 0x0200 > "$G/bcdUSB"
    echo 0xEF   > "$G/bDeviceClass"      # composite with IAD, like the real one (only HID here; Linux doesn't check)
    echo 0x02   > "$G/bDeviceSubClass"
    echo 0x01   > "$G/bDeviceProtocol"
    echo 64     > "$G/bMaxPacketSize0"
    mkdir "$G/strings/0x409"
    echo "Blackmagic Design"                  > "$G/strings/0x409/manufacturer"   # guessed: the capture has no strings
    echo "DaVinci Resolve Micro Color Panel"  > "$G/strings/0x409/product"        # guessed
    echo "ESPCOLORPANEL01"                    > "$G/strings/0x409/serialnumber"

    mkdir "$F"
    echo 0  > "$F/protocol"
    echo 0  > "$F/subclass"
    echo 65 > "$F/report_length"      # 0x06 is 65 B with the ID: fits in one High Speed packet
    echo 1  > "$F/no_out_endpoint"    # with an OUT EP, f_hid STALLs feature SET_REPORTs
    desc_bytes > "$F/report_desc"

    mkdir "$G/configs/c.1"
    echo 100  > "$G/configs/c.1/MaxPower"
    echo 0x80 > "$G/configs/c.1/bmAttributes"
    ln -s "$F" "$G/configs/c.1/"

    echo "$u" > "$G/UDC"

    local n=""
    for _ in $(seq 50); do n=$(hidg_node || true); [[ -n $n && -e $n ]] && break; sleep 0.1; done
    [[ -n $n ]] || die "/dev/hidgN didn't show up"
    chown "$user" "$n"
    echo "Panel side:   $n (owner $user)"

    local h=""
    for _ in $(seq 50); do h=$(hidraw_panel | grep fake || true); [[ -n $h ]] && break; sleep 0.1; done
    [[ -n $h ]] || die "the host didn't create the hidraw; check: sudo dmesg | tail -30"
    udevadm settle
    echo "Resolve side: $h"
    getfacl -p "${h%% *}" 2>/dev/null | grep -E "^user:$user" || echo "  Note: no ACL for $user (no active session on the seat?)"
    lsusb -d 1edb:da0f || true
    echo
    echo "Next: python3 tools/fake_panel.py   (then open Resolve)"
}

down() {
    as_root down
    if [[ -d $G ]]; then
        echo "" > "$G/UDC" 2>/dev/null || true
        rm -f "$G/configs/c.1/hid.mcp"
        rmdir "$G/configs/c.1" "$F" "$G/strings/0x409" "$G" 2>/dev/null || true
    fi
    [[ -d $G ]] && die "couldn't delete $G (is something holding it open?)"
    modprobe -r dummy_hcd 2>/dev/null || true
    echo "Gadget deleted."
    echo "The rule $RULE stays (the ESP32 uses it too). To remove it:"
    echo "  sudo rm -f $RULE $OLD_RULE && sudo udevadm control --reload"
}

replug() {
    as_root replug
    [[ -d $G ]] || die "no gadget; use: sudo $0 up"
    local user=${SUDO_USER:-} u; u=$(udc)
    [[ -n $u ]] || die "no dummy_udc"
    # soft_connect disconnect/connect leaves dummy_hcd unable to enumerate (error -71): unbind and rebind instead
    echo "" > "$G/UDC" 2>/dev/null || true
    sleep 1
    echo "$u" > "$G/UDC"
    local n=""
    for _ in $(seq 50); do n=$(hidg_node || true); [[ -n $n && -e $n ]] && break; sleep 0.1; done
    [[ -n $n && -n $user ]] && chown "$user" "$n"
    local h=""
    for _ in $(seq 50); do h=$(hidraw_panel | grep fake || true); [[ -n $h ]] && break; sleep 0.1; done
    echo "Reconnected on $u: ${n:-no hidg} · ${h:-no hidraw}"
}

status() {
    if [[ -d $G ]]; then
        echo "Gadget: $G (UDC: $(cat "$G/UDC" 2>/dev/null || echo none))"
        local n; n=$(hidg_node || true)
        [[ -n $n ]] && ls -l "$n"
    else
        echo "Gadget: not present"
    fi
    hidraw_panel | while read -r h tag; do ls -l "$h"; getfacl -p "$h" 2>/dev/null | grep '^user:' || true; done
    lsusb -d 1edb:da0f || true
    [[ -f $RULE ]] && echo "Rule: $RULE" || echo "Rule: not installed"
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    replug) replug ;;
    status) status ;;
    desc) desc_bytes | xxd ;;   # to check the descriptor without root
    *) echo "usage: sudo $0 up|replug|down   ·   $0 status|desc" >&2; exit 2 ;;
esac
