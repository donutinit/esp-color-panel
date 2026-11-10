# The Micro Color Panel protocol

This is what is known about the USB protocol of the DaVinci Resolve Micro Color Panel and how Resolve uses it. Almost all of it comes from the capture published by [ra100](https://github.com/ra100/micro-color-panel-controller) (`data/davinci_interaction_capture.pcapng`; the panel is address 65). The rest was measured with this fake panel against DaVinci Resolve Studio 21.2 on Linux.

The tags: **[capture]** can be read in ra100's capture, **[tested]** was checked in Resolve, **[inferred]** was deduced and still needs confirming.

## How Resolve finds the panel (Linux)

Resolve looks for the panel in `libDaVinciPanels.so` (class `HIDLinuxManager`):

1. It enumerates the `hidraw` nodes with libudev.
2. For each one it walks up the parents (`udev_device_get_parent`) until it finds the `idVendor` and `idProduct` attributes, and compares them with `1edb` and `da0f`.
3. It opens the node and confirms it with `HIDIOCGRAWINFO`. On stderr it prints `Employing USB device, vendor ID: 7899, product ID: 55823`.

Resolve never reads the report descriptor. It needs a USB parent with a VID and PID, which is why a `uhid` device doesn't work: it hangs off `/devices/virtual/misc/uhid` and has no USB parent. With `dummy_hcd` the gadget has a real (virtual) USB controller and Resolve accepts it. **[tested]**

On macOS the mechanism is different and we haven't looked inside it: we assume Resolve uses IOHIDManager and matches on VID and PID. **[inferred]** It does accept a virtual HID (IOHIDUserDevice) with that VID and PID: see the macOS section below. **[tested]**

## USB descriptors

Device descriptor, as in the capture: `12 01 00 02 EF 02 01 40 DB 1E 0F DA 00 01 01 02 03 01`. It's a composite device (class EF/02/01) with VID 0x1edb, PID 0xda0f and bcdDevice 0x0100. **[capture]**

The configuration has three interfaces: a vendor one (FF), a DFU one (FE/01/01) and the HID one, with a 64-byte interrupt IN endpoint and a 64-byte interrupt OUT endpoint. The real HID report descriptor is 297 bytes and isn't in the capture. The gadget uses a handwritten one (87 bytes) that declares the same IDs and sizes: it's in `tools/mcp_gadget.sh` and in `macos/mcp_hid_mac.c`. Since Resolve doesn't read it, it's enough for the kernel to accept it.

## Startup

This is what Resolve sends when it opens the panel (times in seconds from the capture): **[capture]**

| t | What | Bytes |
|---|---|---|
| 7.774 | SET_REPORT Feature 0x03 (Brightness) | `03 00 00` |
| 7.775 | OUT 0x04 (indicators) | `04 00 00 00 00 00 00` |
| 7.777 | OUT 0x02 (LEDs) | `02` and 8 zeros |
| 7.785 | GET_REPORT Feature 0x01 (SystemInfo) | reply `01 15 5b 3b fd 02 02 00` |
| 7.785 | SET_REPORT Feature 0x0A (SessionEnabled) | `0a 01` |
| 7.870 | GET_REPORT Feature 0x08 (BatteryStatus) | reply `08 01 63` |
| 9.925 | SET_REPORT Feature 0x03 | `03 64 64` |

After that it repeats `0a 01` and `03 64 64` every 30 seconds.

SystemInfo is a u32 (`15 5b 3b fd`, maybe an identifier) followed by the firmware version. Resolve reads it as `Firmware version: 131584` = 0x020200 = 2.2.0. If the GET arrives before there's a reply, Resolve logs firmware 0 and counts the panel anyway. **[tested]** BatteryStatus `08 01 63` reads as "charging, 99 %". **[inferred]**

## Input reports (panel → Resolve)

| ID | Length | Contents |
|---|---|---|
| 0x02 | 9 B | 64-bit bitmap, little endian: bits 0 to 11 = knob presses, 12 to 51 = the 40 keys |
| 0x05 | 37 B | 9 × int32 LE: X1 Y1 ring1 X2 Y2 ring2 X3 Y3 ring3 |
| 0x06 | 65 B | 16 × int32 LE: slot *i* = knob *i* |

The knobs send deltas in multiples of 360. In the capture there's never more than one nonzero slot per 0x06 report, and a fast turn sends 1080 to 1800 per report, with peaks of ±112 680. The rings send ±1 to ±2 × 4096 every 8 ms, and the balls bigger chunks every 48 ms. **[capture]**

Knob order, left to right per the manual: 0 Y LIFT, 1 Y GAMMA, 2 Y GAIN, 3 CONTRAST, 4 PIVOT, 5 MID DETAIL, 6 COLOR BOOST, 7 SHADOWS, 8 HIGHLIGHTS, 9 SATURATION, 10 HUE, 11 LUM MIX. Contrast (3), Mid Detail (5) and Saturation (9) were checked in Resolve. **[tested]**

### Scale and acceleration

Resolve accelerates with speed: it counts units per unit of time, not just the size of each report. Measured with the scripting API on a node's Saturation (`GetCDL()["Saturation"] × 50`): **[tested]**

| Report pattern | ΔSat | Sat per million units |
|---|---|---|
| 1 report of +36 000 | +0.40 | 11.1 |
| 1 report of +360 000 | +8.80 | 24.4 |
| 10 × +36 000, one every 24 ms | +8.40 | 23.3 |
| 100 × +3 600, one every 24 ms | +2.17 | 6.0 |

That's why a fast turn back doesn't undo a slow turn forward. A one-second turn in the capture adds up to between 0.7 and 1.3 million units, with one report every 24 ms (median). `fake_panel.py` uses ×100 (36 000 units) per step on the knobs and ×25 (8192 = 2 × 4096) on the rings.

### The rings and the HDR Global wheel

With the HDR palette open and OFFSET on, the rings drive the Global wheel: the left one (slot 2) is Temp, the center one (slot 5) Tint and the right one (slot 8) Exp. That matches the manual, which says that in Offset mode the left ring is temperature and the right one the "master exposure". The balls aren't needed. **[tested]**

## Output reports and features (Resolve → panel)

| ID | Type | Contents |
|---|---|---|
| 0x02 | Output, 9 B | key LEDs; we assume the same bit order as input 0x02 **[inferred]** |
| 0x04 | Output, 7 B | indicators: byte 1 = 0x10 with SHIFT UP on, 0x20 with SHIFT DOWN **[capture]** |
| 0x01 | Feature, 8 B | SystemInfo (see above) |
| 0x03 | Feature, 3 B | Brightness |
| 0x08 | Feature, 3 B | BatteryStatus |
| 0x0A | Feature, 2 B | SessionEnabled |

## The keys

Each key's bit in report 0x02 is its index in ra100's `davinci_panel_mapping.json` minus 8. The capture supports 24 and 25 being the Shifts (Resolve answers with `04 10` and `04 20`), but which one is SHIFT UP and which SHIFT DOWN is still to be confirmed. A test that works: SHIFT UP + ADD NODE gives a parallel node and SHIFT DOWN + ADD NODE, a layer.

| Bit | Key | Bit | Key | Bit | Key | Bit | Key |
|---|---|---|---|---|---|---|---|
| 12 | AUTO COLOR | 22 | USER | 32 | SELECT | 42 | NEXT KEYFRM |
| 13 | OFFSET | 23 | LOOP | 33 | RESET LIFT | 43 | PREV NODE |
| 14 | COPY | 24 | SHIFT UP | 34 | RESET GAMMA | 44 | NEXT NODE |
| 15 | PASTE | 25 | SHIFT DOWN | 35 | RESET GAIN | 45 | PREV FRAME |
| 16 | UNDO | 26 | PLAY STILL | 36 | ADD NODE | 46 | NEXT FRAME |
| 17 | REDO | 27 | WIPE STILL | 37 | ADD WINDOW | 47 | PREV CLIP |
| 18 | DELETE | 28 | GRAB STILL | 38 | ADD KEYFRM | 48 | NEXT CLIP |
| 19 | RESET | 29 | H/LITE | 39 | PREV STILL | 49 | PLAY REVERSE |
| 20 | BYPASS | 30 | VIEWER | 40 | NEXT STILL | 50 | PLAY |
| 21 | DISABLE | 31 | CURSOR | 41 | PREV KEYFRM | 51 | STOP |

Some combinations from the manual that the ESP32-C3 bridge uses:

- SHIFT UP + OFFSET toggles the balls' Log mode.
- RESET LIFT, RESET GAMMA and RESET GAIN reset what the left, center and right ball and ring move. With SHIFT DOWN they reset only the level, that is, the ring.

What each key does with SHIFT UP and SHIFT DOWN is in `SHIFT_FUNCTIONS` in `tools/fake_panel.py`, taken from the manual's key-by-key description.

## The Linux gadget (f_hid)

Details of `drivers/usb/gadget/function/f_hid.c` (kernel 7.2) that matter here:

- `report_length` is both the IN endpoint's wMaxPacketSize and the cap on each `write()`. With 65, report 0x06 fits in one packet at High Speed, which is `dummy_hcd`'s default speed.
- Feature SET_REPORTs only arrive with `no_out_endpoint=1`; with an OUT endpoint, f_hid answers them with STALL. This way Resolve's OUTs (0x02 and 0x04) also arrive as SET_REPORT and are read with `read()` on `/dev/hidgN`.
- GET_REPORT is indexed by report ID only and returns the bytes as they are (the first one is the ID). Replies are registered with the `GADGET_HID_WRITE_GET_REPORT` ioctl (0x40486742, 72-byte struct) and stay in the kernel even after the program exits. An ID with no registered reply wakes userspace with POLLPRI (`GADGET_HID_READ_GET_REPORT_ID`, 0x80016741) and, if nobody answers within 2.5 s, the kernel sends zeros.
- With `O_NONBLOCK`, `write()` returns EAGAIN when nobody is reading the hidraw. `fake_panel.py` uses that to warn that Resolve hasn't opened the panel. If the host disabled the function, `read()` returns ENOMEM; if it hasn't configured it, `write()` returns ESHUTDOWN.
- To "unplug the cable" without closing Resolve you have to unbind and rebind the UDC. With `soft_connect`, `dummy_hcd` no longer enumerates (error -71).

## macOS (IOHIDUserDevice)

`macos/mcp_hid_mac` creates the panel with `IOHIDUserDeviceCreateWithProperties`, with the same properties (VendorID, ProductID, Transport "USB", names and descriptor) and the same GET_REPORT replies. Input reports go out with `IOHIDUserDeviceHandleReportWithTimeStamp`; SET_REPORTs come in through a block registered with `IOHIDUserDeviceRegisterSetReportBlock`.

Creating an IOHIDUserDevice needs the `com.apple.developer.hid.virtual.device` entitlement, and Apple doesn't grant it without an approved request. With an ad hoc signature it takes SIP disabled and the boot-arg `amfi_get_out_of_my_way=1`; the boot-arg is required, not optional. With SIP off but without it, AMFI kills the helper on launch (`Killed: 9`; the kernel log says `Code has restricted entitlements, but the validation of its code signature failed`). Without the entitlement, `IOHIDUserDeviceCreateWithProperties` returns NULL (as a normal user). **[tested]**

Tested on 2026-11-09 on macOS 26.3 (Apple Silicon) with DaVinci Resolve Studio 21.0.2, with SIP off and that boot-arg: the helper prints `READY`, `ioreg` shows 1edb:da0f "DaVinci Resolve Micro Color Panel" by "Blackmagic Design", and Resolve opens a session (SessionEnabled, Brightness, LEDs, indicators). So Resolve on macOS does accept a virtual HID as the panel. **[tested]** What the knobs and keys do in Resolve on macOS is not confirmed yet.

## Decoding the capture again

```sh
tshark -r davinci_interaction_capture.pcapng \
  -Y 'usb.device_address==65 && usb.transfer_type==1 && usb.endpoint_address==0x81 && usb.data_len>0' \
  -T fields -e frame.number -e frame.time_relative -e usbhid.data \
  | python3 tools/decode_capture.py
```
