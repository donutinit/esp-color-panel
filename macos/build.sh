#!/bin/sh
# Compila macos/mcp_hid_mac y lo firma ad hoc con el entitlement de HID virtual.
# Necesita las herramientas de línea de comandos de Xcode (xcode-select --install).
set -eu
cd "$(dirname "$0")"

[ "$(uname)" = Darwin ] || { echo "Esto es para macOS. En Linux usa tools/mcp_gadget.sh." >&2; exit 1; }

clang -O2 -Wall -Wextra -fblocks -mmacosx-version-min=10.15 \
    -framework IOKit -framework CoreFoundation \
    -o mcp_hid_mac mcp_hid_mac.c
codesign --force --sign - --entitlements mcp_hid_mac.entitlements mcp_hid_mac

echo "Listo: macos/mcp_hid_mac"
codesign -d --entitlements - mcp_hid_mac 2>/dev/null | grep -q hid.virtual.device \
    && echo "Firmado con com.apple.developer.hid.virtual.device." \
    || echo "Ojo: no veo el entitlement en la firma."
if command -v csrutil >/dev/null && csrutil status | grep -q enabled; then
    echo "Ojo: SIP está activo; macOS no va a dejar crear el HID virtual. Ver el README, sección macOS."
fi
