# mcp-falso

Un DaVinci Resolve Micro Color Panel de mentira. Resolve cree que tiene conectado el panel de Blackmagic (USB `1edb:da0f`) y obedece a un script de Python que lo maneja desde el teclado o desde perillas reales conectadas a una ESP32-C3.

Nació porque yo quería una sola cosa: una perilla física para la exposición de las HDR wheels, sin trackballs. En el camino salió el panel entero en el teclado.

> **English summary.** A fake Blackmagic DaVinci Resolve Micro Color Panel. On Linux it is a virtual USB gadget (`dummy_hcd` + `f_hid`) driven by a Python "firmware" that speaks the panel's HID protocol, as reverse engineered by [ra100](https://github.com/ra100/micro-color-panel-controller). Resolve Studio 21.2 accepts it: the knobs, the OFFSET key and the trackball rings work (the rings also drive the HDR Global wheel), and the other keys are mapped but not all verified. An ESP32-C3 with three rotary encoders can feed it real knobs. There is an experimental, untested macOS port based on `IOHIDUserDevice` that needs SIP disabled. Docs are in Spanish; the protocol notes are in [docs/protocolo.md](docs/protocolo.md).

## Gracias, ra100

Todo esto se apoya en el trabajo de [ra100](https://github.com/ra100), que le hizo ingeniería inversa al Micro Color Panel y publicó [micro-color-panel-controller](https://github.com/ra100/micro-color-panel-controller): una captura con Wireshark del tráfico USB entre un panel real y Resolve, más el mapa de cada control. Su proyecto va en la dirección contraria (usar el panel de verdad fuera de Resolve), y aun así su captura trae lo que hacía falta para fingir el panel. De ahí salen los descriptores USB, la secuencia con la que Resolve abre el panel, el formato de cada reporte y el bit de cada tecla. Sin esa captura este repo no existiría.

El orden de las perillas y lo que hace cada tecla con Shift salen del manual del Micro Color Panel de Blackmagic Design.

## Cómo funciona

```
 teclado o ESP32-C3 (3 encoders + 4 botones)
        │
        ▼
 tools/mcp_falso.py      el "firmware": arma los reportes HID del panel
        │
        ├── Linux   /dev/hidgN → f_hid + dummy_hcd → un USB 1edb:da0f → hidraw → Resolve
        │
        └── macOS   macos/mcp_hid_mac → IOHIDUserDevice 1edb:da0f → Resolve   (sin probar)
```

En Linux, `dummy_hcd` crea un controlador USB virtual y `configfs` le cuelga un dispositivo HID con el VID y el PID del panel. Para Resolve es un panel enchufado. Resolve busca el panel subiendo por los padres USB del hidraw, y por eso aquí hace falta un USB virtual completo; un HID simple (`uhid`) no tiene ese padre y Resolve lo ignora. Los detalles están en [docs/protocolo.md](docs/protocolo.md).

`mcp_falso.py` deja listas las respuestas que Resolve pide al abrir el panel (versión de firmware, batería, brillo), registra lo que Resolve le manda (LEDs de las teclas, indicadores de Shift) y convierte cada tecla o perilla en el reporte que mandaría el panel real.

## Antes de empezar

Las perillas cambian tu proyecto de verdad, y Resolve acelera según la velocidad del giro: un regreso rápido no deshace un avance lento. Haz las pruebas en un proyecto de prueba.

Este proyecto no tiene relación con Blackmagic Design. "DaVinci Resolve" y "Micro Color Panel" son marcas de Blackmagic Design; aquí solo se nombran para decir con qué es compatible.

## Lo que necesitas

En Linux (probado en Fedora 44, kernel 7.2, con DaVinci Resolve Studio 21.2):

- Los módulos `dummy_hcd`, `libcomposite` y `usb_f_hid`. Fedora los trae; en otras distros revísalo con `modinfo dummy_hcd`, porque algunas no compilan `dummy_hcd` y hay que compilarlo aparte.
- `sudo`, solo para crear y borrar el USB virtual.
- Python 3.8 o más nuevo, sin paquetes extra.
- DaVinci Resolve Studio. No sabemos si la versión gratuita acepta el panel.

En macOS (sin probar):

- SIP desactivado. Abajo está el porqué.
- Las herramientas de línea de comandos de Xcode (`xcode-select --install`).
- Python 3 de Homebrew o de python.org.
- `pip3 install pyobjc-framework-Quartz`, solo si vas a usar el clic en HDR de la ESP32-C3.

Para las perillas físicas (opcional):

- Una ESP32-C3 SuperMini, tres encoders EC11 con pulsador y cuatro botones.
- [PlatformIO](https://platformio.org/) para compilar y flashear el firmware.

## Linux, paso a paso

1. Crea el panel y "conéctalo":

   ```sh
   sudo tools/mcp_gadget.sh up
   ```

   Instala una regla de udev para que tu usuario pueda abrir el hidraw del panel, carga los módulos, arma el gadget y le da a tu usuario el `/dev/hidgN`. Al final te dice qué hidraw creó el sistema.

2. Arranca el firmware:

   ```sh
   python3 tools/mcp_falso.py
   ```

   Si Resolve ya estaba abierto, mejor ciérralo y ábrelo después de este paso. Resolve pide la versión de firmware apenas ve el panel, y la respuesta tiene que estar lista.

3. Abre Resolve y ve a la página Color. Arriba, en la interfaz de `mcp_falso.py`, debe aparecer "✔ sesión abierta". Si Resolve no toma el panel solo, elígelo en Preferencias → Control Panels.

Para desconectar y volver a conectar el cable virtual sin cerrar Resolve existe `sudo tools/mcp_gadget.sh replug`. `sudo tools/mcp_gadget.sh down` lo borra todo y `tools/mcp_gadget.sh status` muestra cómo está.

Si quieres probar sin Resolve, `python3 tools/mcp_leer.py` hace de Resolve: abre el hidraw, repite el arranque de la captura y te muestra lo que manda el panel. Y con `python3 tools/mcp_falso.py --simular` ves la interfaz sin gadget.

## macOS, paso a paso (sin probar)

Esta parte se escribió sin una Mac a la mano. El ayudante en C se compiló y se corrió en Linux contra un IOKit simulado, y eso solo comprueba nuestra lógica. Falta ver si macOS lo deja correr y si Resolve acepta un HID virtual como panel. Si lo pruebas, abre un issue y cuéntanos qué pasó.

macOS sí tiene una forma de crear un HID virtual (`IOHIDUserDevice`), pero exige el entitlement `com.apple.developer.hid.virtual.device`, y Apple no lo da sin una solicitud aprobada. Con una firma ad hoc macOS solo lo respeta si desactivas SIP.

1. Desactiva SIP: arranca en modo de recuperación, abre la Terminal y corre `csrutil disable`. Reinicia. Cuando termines de usar el panel puedes volver a activarlo con `csrutil enable`.
2. Compila y firma el ayudante:

   ```sh
   macos/build.sh
   ```

3. Arranca el firmware. En macOS lanza solo a `macos/mcp_hid_mac`:

   ```sh
   python3 tools/mcp_falso.py
   ```

4. Abre Resolve y elige el panel en Preferencias → Control Panels si no lo toma solo.

Si macOS mata al ayudante al arrancar ("Killed: 9") o dice que no pudo crear el HID virtual, puede faltar además el boot-arg `amfi_get_out_of_my_way=1`. Tampoco lo hemos probado.

## El panel en el teclado

Las perillas van en las flechas: ↑ y ↓ eligen la perilla, ← y → la giran, Shift con las flechas gira ×10 o cambia el tamaño del paso, y espacio aprieta la perilla elegida. Después de LUM MIX vienen los tres anillos de las bolas.

Las 40 teclas van en cuatro filas, como en un teclado latinoamericano (en uno US, `;` y `/` hacen de `ñ` y `-`):

```
1 PLAY STILL   2 WIPE STILL   3 GRAB STILL   4 H/LITE       5 VIEWER       6 CURSOR       7 SELECT       8 ADD NODE     9 ADD WINDOW   0 ADD KEYFRM
Q PREV STILL   W NEXT STILL   E PREV KEYFRM  R NEXT KEYFRM  T PREV NODE    Y NEXT NODE    U PREV FRAME   I NEXT FRAME   O PREV CLIP    P NEXT CLIP
A AUTO COLOR   S OFFSET       D COPY         F PASTE        G UNDO         H REDO         J DELETE       K RESET        L BYPASS       Ñ DISABLE
Z USER         X LOOP         C SHIFT UP     V SHIFT DOWN   B RESET LIFT   N RESET GAMMA  M RESET GAIN   , ◀ PLAY       . ■ STOP       - ▶ PLAY
```

C y V (SHIFT UP y SHIFT DOWN) se quedan puestas hasta la siguiente tecla, como en el panel, y mientras están puestas cada tecla muestra lo que hace con Shift. Las teclas cuyo LED prende Resolve salen en verde. `python3 tools/mcp_falso.py --mapa` imprime las tres capas.

Para la exposición de HDR: abre la paleta HDR, aprieta S (OFFSET) y usa el tercer anillo. El primero es Temp y el segundo Tint, siempre sobre la rueda Global.

## Perillas de verdad con una ESP32-C3

La C3 no tiene USB nativo, así que no puede hacerse pasar por el panel. Lo que hace es leer los encoders y los botones y mandarlos como texto por su USB serie; `mcp_falso.py --c3` los convierte en acciones del panel. Cada interruptor va a su propio GPIO y a GND, con el pull-up interno, así que no hace falta matriz ni diodos.

| GPIO | Conexión | GPIO | Conexión |
|---|---|---|---|
| 7 / 8 | Encoder 1, A / B | 6 | Pulsador del encoder 1 |
| 1 / 0 | Encoder 2, A / B | 5 | Pulsador del encoder 2 |
| 3 / 2 | Encoder 3, A / B | 4 | Pulsador del encoder 3 |
| 10 / 20 | Botón 1 / Botón 2 | 21 / 9 | Botón 3 / Botón 4 |

La pata central de cada encoder (C) va a GND, igual que la otra pata de cada botón y de cada pulsador. Todo funciona a 3.3 V. Hay tres detalles de la SuperMini: el GPIO8 tiene el LED azul (parpadea al girar el encoder 1), el GPIO9 es el botón BOOT (no tengas el botón 4 apretado al conectarla o entra en modo de flasheo) y el GPIO2 es un pin de arranque. Si tus encoders giran al revés, cambia el signo de su entrada en `firmware/c3/src/main.cpp`.

Flashea el firmware y prueba el cableado:

```sh
pio run -d firmware/c3 -t upload
python3 tools/c3_probar.py
```

`c3_probar.py` imprime una línea por cada clic y cada botón. Un clic normal cuenta 4 cuartos de paso. Ciérralo antes de volver a flashear, porque los dos usan el mismo puerto.

Después, en vez del paso 2 de arriba:

```sh
python3 tools/mcp_falso.py --c3
```

La asignación está pensada para la rueda Global de HDR:

| Control | Qué hace |
|---|---|
| Encoders 1, 2 y 3 | Los anillos izquierdo, central y derecho. Con OFFSET y la paleta HDR: Temp, Tint y Exp |
| Pulsadores 1 y 2 | RESET LIFT y RESET GAMMA (resetean Temp y Tint) |
| Pulsador 3 | SHIFT DOWN + RESET GAIN (resetea solo el anillo derecho) |
| Botón 1, un toque | SHIFT UP + OFFSET: alterna entre Primaries y Log |
| Botón 1, doble toque | Clic en el botón de la paleta HDR |
| Botón 1, mantenido 1.5 s | Guarda dónde está el botón HDR (con el puntero encima) |
| Botón 2 | DISABLE |
| Botón 3 | UNDO |
| Botón 4, un toque | OFFSET |
| Botón 4, doble toque | VIEWER (el visor grande) |

El panel no tiene tecla para abrir la paleta HDR y Resolve tampoco tiene un atajo de teclado para eso, así que el doble toque hace el clic en la pantalla: lleva el cursor al botón, aprieta y lo regresa a donde estaba. La primera vez, pon el puntero sobre el botón HDR de Resolve y mantén el botón 1 apretado 1.5 s; la posición queda en `~/.config/mcp-falso/c3.json`. Si cambias el tamaño de la ventana de Resolve, hay que guardarla otra vez.

En Linux el clic funciona en Hyprland: mueve el cursor por su IPC y aprieta con un mouse virtual de `uinput`, al que tu usuario necesita acceso (grupo `input`). Con otro compositor todo lo demás funciona, pero el clic no. En macOS usa Quartz, y la terminal necesita permiso de Accesibilidad (Ajustes del Sistema → Privacidad y seguridad → Accesibilidad).

La asignación entera está al principio de `tools/c3_entrada.py`.

## Qué está probado

Probado en Resolve Studio 21.2 sobre Linux:

- Resolve acepta el panel virtual y lee la versión de firmware 2.2.0.
- Contrast, Mid Detail y Saturation se mueven con sus perillas.
- Con OFFSET y la paleta HDR, los anillos mueven Temp, Tint y Exp de la rueda Global.
- La ESP32-C3 lee bien los tres encoders, los tres pulsadores y los cuatro botones.

Falta confirmar:

- El bit de cada tecla. Sale de ra100 y de la captura, pero no se ha revisado una por una.
- Cuál de los bits 24 y 25 es SHIFT UP. Prueba: C y luego 8 debe dar un nodo paralelo; si da un layer, están al revés.
- El orden de bits de los LEDs que manda Resolve.
- Que el botón 1 cambie a Log y que el clic en HDR llegue a Resolve.
- Todo lo de macOS.

## Los archivos

| Archivo | Qué hace | Sistema |
|---|---|---|
| `tools/mcp_gadget.sh` | Crea, reconecta y borra el USB virtual (con sudo) | Linux |
| `tools/mcp_falso.py` | El firmware: interfaz, teclado y reportes del panel | Linux, macOS |
| `tools/mcp_leer.py` | Hace de Resolve contra el panel, para probar sin Resolve | Linux |
| `macos/mcp_hid_mac.c` | El HID virtual de macOS; `build.sh` lo compila y lo firma | macOS |
| `tools/c3_entrada.py` | Convierte lo que manda la ESP32-C3 en acciones del panel | Linux, macOS |
| `tools/c3_probar.py` | Imprime lo que haces con cada encoder y botón | Linux, macOS |
| `firmware/c3/` | Firmware de la ESP32-C3 (PlatformIO, Arduino) | |
| `tools/decodificar_captura.py` | Resume los reportes de la captura de ra100 (con tshark) | Linux, macOS |
| `docs/protocolo.md` | Todo lo que se sabe del protocolo | |

## Licencia

MIT. Ver [LICENSE](LICENSE).
