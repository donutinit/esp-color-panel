# esp-color-panel

A fake DaVinci Resolve Micro Color Panel. Resolve thinks the Blackmagic panel (USB `1edb:da0f`) is plugged in and takes orders from a Python script that drives it from the keyboard or from real knobs wired to an ESP32-C3.

On Linux the panel is a virtual USB gadget (`dummy_hcd` + `f_hid`); on macOS it is a virtual HID device (`IOHIDUserDevice`) that needs SIP and AMFI turned off. In both, a Python "firmware" speaks the panel's HID protocol, as reverse engineered by ra100. The protocol notes are in [docs/protocol.md](docs/protocol.md).

It started because I wanted one thing: a physical knob for the exposure of the HDR wheels, without trackballs. Along the way the whole panel ended up on the keyboard.

## Thanks, ra100

All of this rests on the work of [ra100](https://github.com/ra100), who reverse engineered the Micro Color Panel and published [micro-color-panel-controller](https://github.com/ra100/micro-color-panel-controller): a Wireshark capture of the USB traffic between a real panel and Resolve, plus the map of every control. That project goes the other way (using the real panel outside Resolve), and still its capture has everything it takes to fake the panel. The USB descriptors, the sequence Resolve uses to open the panel, the format of each report and the bit of each key all come from there. Without that capture this repo wouldn't exist.

The order of the knobs and what each key does with Shift come from Blackmagic Design's Micro Color Panel manual.

## How it works

```
 keyboard or ESP32-C3 (3 encoders + 4 buttons)
        │
        ▼
 tools/fake_panel.py     the "firmware": builds the panel's HID reports
        │
        ├── Linux   /dev/hidgN → f_hid + dummy_hcd → a USB 1edb:da0f → hidraw → Resolve
        │
        └── macOS   macos/mcp_hid_mac → IOHIDUserDevice 1edb:da0f → Resolve
```

On Linux, `dummy_hcd` creates a virtual USB controller and `configfs` hangs an HID device with the panel's VID and PID off it. To Resolve it's a plugged-in panel. Resolve finds the panel by walking up the USB parents of the hidraw, and that's why a full virtual USB is needed here; a plain HID (`uhid`) has no such parent and Resolve ignores it. The details are in [docs/protocol.md](docs/protocol.md).

On macOS, the helper `macos/mcp_hid_mac` creates the device with `IOHIDUserDevice`, and `fake_panel.py` talks to it over its stdin and stdout.

`fake_panel.py` has ready the answers Resolve asks for when it opens the panel (firmware version, battery, brightness), logs what Resolve sends it (key LEDs, Shift indicators) and turns each key or knob into the report the real panel would send.

## Before you start

The knobs change your project for real, and Resolve accelerates with the speed of the turn: a fast turn back doesn't undo a slow turn forward. Do your testing in a test project.

This project has no relation to Blackmagic Design. "DaVinci Resolve" and "Micro Color Panel" are trademarks of Blackmagic Design; they are named here only to say what this is compatible with.

## What you need

On Linux (tested on Fedora 44, kernel 7.2, with DaVinci Resolve Studio 21.2):

- The `dummy_hcd`, `libcomposite` and `usb_f_hid` modules. Fedora ships them; on other distros check with `modinfo dummy_hcd`, because some don't build `dummy_hcd` and you have to build it yourself.
- `sudo`, only to create and remove the virtual USB.
- Python 3.8 or newer, no extra packages.
- DaVinci Resolve Studio. We don't know if the free version accepts the panel.

On macOS (tested on macOS 26.3, Apple Silicon, with DaVinci Resolve Studio 21.0.2):

- SIP disabled and the boot-arg `amfi_get_out_of_my_way=1`. Together they turn off code signature validation for the whole system; the macOS section below explains why it's needed and how to undo it.
- The Xcode command line tools (`xcode-select --install`).
- Python 3 from Homebrew or python.org.
- `pyobjc-framework-Quartz`, only if you'll use the HDR click of the ESP32-C3. Homebrew's Python refuses a plain `pip install`, so: `python3 -m pip install --user --break-system-packages pyobjc-framework-Quartz`.

For the physical knobs (optional):

- An ESP32-C3 SuperMini, three EC11 encoders with push switch and four buttons.
- [PlatformIO](https://platformio.org/) to build and flash the firmware. `pipx install platformio` installs it.

## Linux, step by step

1. Create the panel and "plug it in":

   ```sh
   sudo tools/mcp_gadget.sh up
   ```

   It installs a udev rule so your user can open the panel's hidraw, loads the modules, builds the gadget and gives your user the `/dev/hidgN`. At the end it tells you which hidraw the system created.

2. Start the firmware:

   ```sh
   python3 tools/fake_panel.py
   ```

   If Resolve was already open, better close it and open it after this step. Resolve asks for the firmware version as soon as it sees the panel, and the answer has to be ready.

3. Open Resolve and go to the Color page. At the top of the `fake_panel.py` interface you should see "✔ session open". If Resolve doesn't take the panel by itself, pick it in Preferences → Control Panels.

To unplug and replug the virtual cable without closing Resolve there's `sudo tools/mcp_gadget.sh replug`. `sudo tools/mcp_gadget.sh down` removes everything and `tools/mcp_gadget.sh status` shows how it stands.

If you want to test without Resolve, `python3 tools/fake_resolve.py` plays Resolve: it opens the hidraw, replays the startup from the capture and shows you what the panel sends. And `python3 tools/fake_panel.py --simulate` shows you the interface without a gadget.

## macOS, step by step

Tested on 2026-11-09 on macOS 26.3 (Apple Silicon) with DaVinci Resolve Studio 21.0.2: macOS sees the panel and Resolve opens a session with it. Not confirmed yet: that the knobs and keys do anything in Resolve, and the HDR click. If you try those, open an issue and tell us what happened.

macOS does have a way to create a virtual HID (`IOHIDUserDevice`), but it requires the `com.apple.developer.hid.virtual.device` entitlement, and Apple doesn't grant it without an approved request. `build.sh` signs the helper ad hoc with that entitlement, and macOS won't run it like that while its protections are on. Disabling SIP is not enough: AMFI still kills the helper at launch ("Killed: 9"), and the kernel log says "Code has restricted entitlements, but the validation of its code signature failed". It also takes the boot-arg `amfi_get_out_of_my_way=1`.

That boot-arg turns off code signature validation for the whole system, not just for this helper: macOS will run any binary, whether its signature is valid or not. Do it only on a machine where you're fine with that, and undo it when you're done (at the end of this section).

1. Disable SIP: boot into recoveryOS (on Apple Silicon, shut down, hold the power button until the startup options appear and choose Options), open Terminal and run `csrutil disable`. Restart into macOS.
2. Check whether you already have boot-args:

   ```sh
   nvram boot-args
   ```

   If it prints an error, you have none, and this is enough:

   ```sh
   sudo nvram boot-args="amfi_get_out_of_my_way=1"
   ```

   If it prints something, write it down and keep it, adding the new one at the end: `sudo nvram boot-args="<what you had> amfi_get_out_of_my_way=1"`. Setting boot-args replaces the whole value.

3. Reboot.
4. Build and sign the helper:

   ```sh
   macos/build.sh
   ```

5. Start the firmware. On macOS it launches `macos/mcp_hid_mac` by itself:

   ```sh
   python3 tools/fake_panel.py
   ```

6. Open Resolve and go to the Color page. At the top of the interface you should see "✔ session open". If Resolve doesn't take the panel by itself, pick it in Preferences → Control Panels.

To check that it works without touching your project, start it with `python3 tools/fake_panel.py --listen` instead: it only logs what Resolve sends and sends nothing. That's how it was tested: the helper prints READY, macOS lists the device as `1edb:da0f` "DaVinci Resolve Micro Color Panel", and Resolve opens a session (SessionEnabled) and sends the brightness and the key LEDs.

If the helper still dies with "Killed: 9", the boot-arg didn't take: check `nvram boot-args`, `csrutil status`, and that you rebooted after setting them.

To undo it when you're done, put the boot-args back with `sudo nvram -d boot-args` if you had none, or `sudo nvram boot-args="<what you had>"`. Then boot into recoveryOS, run `csrutil enable` and restart.

## The panel on the keyboard

The knobs are on the arrow keys: ↑ and ↓ pick the knob, ← and → turn it, Shift with the arrows turns ×10 or changes the step size, and space presses the selected knob. After LUM MIX come the three trackball rings.

The 40 keys are in four rows, as on a Latin American keyboard (on a US one, `;` and `/` act as `ñ` and `-`):

```
1 PLAY STILL   2 WIPE STILL   3 GRAB STILL   4 H/LITE       5 VIEWER       6 CURSOR       7 SELECT       8 ADD NODE     9 ADD WINDOW   0 ADD KEYFRM
Q PREV STILL   W NEXT STILL   E PREV KEYFRM  R NEXT KEYFRM  T PREV NODE    Y NEXT NODE    U PREV FRAME   I NEXT FRAME   O PREV CLIP    P NEXT CLIP
A AUTO COLOR   S OFFSET       D COPY         F PASTE        G UNDO         H REDO         J DELETE       K RESET        L BYPASS       Ñ DISABLE
Z USER         X LOOP         C SHIFT UP     V SHIFT DOWN   B RESET LIFT   N RESET GAMMA  M RESET GAIN   , ◀ PLAY       . ■ STOP       - ▶ PLAY
```

C and V (SHIFT UP and SHIFT DOWN) stay on until the next key, as on the panel, and while they're on each key shows what it does with Shift. Keys whose LED Resolve turns on show in green. `python3 tools/fake_panel.py --map` prints the three layers.

For HDR exposure: open the HDR palette, press S (OFFSET) and use the third ring. The first is Temp and the second Tint, always on the Global wheel.

## Real knobs with an ESP32-C3

The C3 has no native USB, so it can't pass itself off as the panel. What it does is read the encoders and buttons and send them as text over its USB serial; `fake_panel.py --c3` turns them into panel actions. Each switch goes to its own GPIO and to GND, with the internal pull-up, so there's no need for a matrix or diodes.

| GPIO | Connection | GPIO | Connection |
|---|---|---|---|
| 7 / 8 | Encoder 1, A / B | 6 | Encoder 1 push switch |
| 1 / 0 | Encoder 2, A / B | 5 | Encoder 2 push switch |
| 3 / 2 | Encoder 3, A / B | 4 | Encoder 3 push switch |
| 10 / 20 | Button 1 / Button 2 | 21 / 9 | Button 3 / Button 4 |

The middle pin of each encoder (C) goes to GND, and so does the other leg of each button and each push switch. Everything runs at 3.3 V. Three things about the SuperMini: GPIO8 has the blue LED (it blinks when you turn encoder 1), GPIO9 is the BOOT button (don't hold button 4 down while plugging it in, or it goes into flashing mode) and GPIO2 is a strapping pin. If your encoders turn the wrong way, flip the sign of their input in `firmware/c3/src/main.cpp`.

Flash the firmware and test the wiring:

```sh
pio run -d firmware/c3 -t upload
python3 tools/c3_test.py
```

`c3_test.py` prints a line for every click and every button. A normal click counts 4 quarter steps. Close it before flashing again, because both use the same port.

The C3 shows up as `/dev/ttyACM0` on Linux and as the first `/dev/cu.usbmodem*` on macOS; `c3_test.py` and `--c3` use that by default and take another port as an argument. When `c3_test.py` starts, the C3 should answer "HELLO esp-color-panel 1". If it answers "HOLA c3_prueba 1", it has an older test firmware whose lines `fake_panel.py` ignores; flash it again with the command above.

Then start the firmware like this instead (step 2 on Linux, step 5 on macOS):

```sh
python3 tools/fake_panel.py --c3
```

The mapping is meant for the HDR Global wheel:

| Control | What it does |
|---|---|
| Encoders 1, 2 and 3 | The left, center and right rings. With OFFSET and the HDR palette: Temp, Tint and Exp |
| Push switches 1 and 2 | RESET LIFT and RESET GAMMA (they reset Temp and Tint) |
| Push switch 3 | SHIFT DOWN + RESET GAIN (resets only the right ring) |
| Button 1, single tap | SHIFT UP + OFFSET: toggles between Primaries and Log |
| Button 1, double tap | Clicks the HDR palette button |
| Button 1, held 1.5 s | Saves where the HDR button is (with the pointer over it) |
| Button 2 | DISABLE |
| Button 3 | UNDO |
| Button 4, single tap | OFFSET |
| Button 4, double tap | VIEWER (enlarges the viewer) |

The panel has no key to open the HDR palette and Resolve has no keyboard shortcut for it either, so the double tap clicks on the screen: it moves the cursor to the button, clicks and puts the cursor back where it was. The first time, put the pointer over Resolve's HDR button and hold button 1 down for 1.5 s; the position is saved in `~/.config/esp-color-panel/c3.json`. If you resize the Resolve window, you have to save it again.

On Linux the click works on Hyprland: it moves the cursor through its IPC and clicks with a `uinput` virtual mouse, which your user needs access to (`input` group). With another compositor everything else works, but the click doesn't. On macOS it uses Quartz (`pyobjc-framework-Quartz`, see What you need), and your terminal app needs Accessibility permission (System Settings → Privacy & Security → Accessibility). Restart the terminal after granting it.

The whole mapping is at the top of `tools/c3_input.py`.

## What's tested

On Linux, with Resolve Studio 21.2:

- Resolve accepts the virtual panel and reads firmware version 2.2.0.
- Contrast, Mid Detail and Saturation move with their knobs.
- With OFFSET and the HDR palette, the rings move Temp, Tint and Exp of the Global wheel.
- The ESP32-C3 reads the three encoders, the three push switches and the four buttons correctly.

On macOS 26.3 (Apple Silicon), with Resolve Studio 21.0.2:

- With SIP disabled and `amfi_get_out_of_my_way=1`, the helper starts (READY) and macOS sees the panel as `1edb:da0f`.
- Resolve opens a session with the panel (SessionEnabled) and sends it the brightness and the key LEDs.
- The ESP32-C3 works over `/dev/cu.usbmodem*`.

Still to confirm:

- The bit of each key. It comes from ra100 and the capture, but hasn't been checked one by one.
- Which of bits 24 and 25 is SHIFT UP. Test: C and then 8 should give a parallel node; if it gives a layer, they're swapped.
- The bit order of the LEDs Resolve sends.
- That button 1 switches to Log and that the HDR click reaches Resolve.
- On macOS, everything past the session: what the knobs and keys do in Resolve, and the HDR click.

## The files

| File | What it does | System |
|---|---|---|
| `tools/mcp_gadget.sh` | Creates, replugs and removes the virtual USB (with sudo) | Linux |
| `tools/fake_panel.py` | The firmware: interface, keyboard and panel reports | Linux, macOS |
| `tools/fake_resolve.py` | Plays Resolve against the panel, to test without Resolve | Linux |
| `macos/mcp_hid_mac.c` | The macOS virtual HID (needs SIP and AMFI off); `build.sh` builds and signs it | macOS |
| `tools/c3_input.py` | Turns what the ESP32-C3 sends into panel actions | Linux, macOS |
| `tools/c3_test.py` | Prints what you do with each encoder and button | Linux, macOS |
| `firmware/c3/` | Firmware for the ESP32-C3 (PlatformIO, Arduino) | |
| `tools/decode_capture.py` | Summarizes the reports in ra100's capture (with tshark) | Linux, macOS |
| `docs/protocol.md` | Everything known about the protocol | |

## License

MIT. See [LICENSE](LICENSE).
