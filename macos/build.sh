#!/bin/sh
# Builds macos/mcp_hid_mac and signs it ad hoc with the virtual HID entitlement.
# Needs the Xcode command line tools (xcode-select --install).
set -eu
cd "$(dirname "$0")"

[ "$(uname)" = Darwin ] || { echo "This is for macOS. On Linux use tools/mcp_gadget.sh." >&2; exit 1; }

clang -O2 -Wall -Wextra -fblocks -mmacosx-version-min=10.15 \
    -framework IOKit -framework CoreFoundation \
    -o mcp_hid_mac mcp_hid_mac.c
codesign --force --sign - --entitlements mcp_hid_mac.entitlements mcp_hid_mac

echo "Done: macos/mcp_hid_mac"
codesign -d --entitlements - mcp_hid_mac 2>/dev/null | grep -q hid.virtual.device \
    && echo "Signed with com.apple.developer.hid.virtual.device." \
    || echo "Warning: the entitlement isn't in the signature."
if command -v csrutil >/dev/null && csrutil status | grep -q enabled; then
    echo "Warning: SIP is enabled; macOS won't let it create the virtual HID. See the README, macOS section."
fi
if ! nvram boot-args 2>/dev/null | grep -q amfi_get_out_of_my_way=1; then
    echo "Warning: the boot-arg amfi_get_out_of_my_way=1 isn't set; even with SIP off, AMFI kills the helper" \
         "(\"Killed: 9\"). See the README, macOS section."
fi
