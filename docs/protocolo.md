# El protocolo del Micro Color Panel

Esto es lo que se sabe del protocolo USB del DaVinci Resolve Micro Color Panel y de cómo lo usa Resolve. Casi todo sale de la captura que publicó [ra100](https://github.com/ra100/micro-color-panel-controller) (`data/davinci_interaction_capture.pcapng`, el panel es la dirección 65). Lo demás se midió con este panel falso contra DaVinci Resolve Studio 21.2 en Linux.

Las etiquetas: **[captura]** se lee en la captura de ra100, **[probado]** se comprobó en Resolve, **[inferido]** se dedujo y falta confirmarlo.

## Cómo encuentra Resolve el panel (Linux)

Resolve busca el panel en `libDaVinciPanels.so` (clase `HIDLinuxManager`):

1. Enumera los nodos `hidraw` con libudev.
2. Por cada uno sube por los padres (`udev_device_get_parent`) hasta encontrar los atributos `idVendor` e `idProduct`, y los compara con `1edb` y `da0f`.
3. Abre el nodo y lo confirma con `HIDIOCGRAWINFO`. En stderr imprime `Employing USB device, vendor ID: 7899, product ID: 55823`.

Resolve nunca lee el descriptor de reporte. Necesita un padre USB con VID y PID, y por eso un dispositivo `uhid` no sirve: cuelga de `/devices/virtual/misc/uhid` y no tiene padre USB. Con `dummy_hcd` el gadget tiene un controlador USB de verdad (virtual) y Resolve lo acepta. **[probado]**

En macOS el mecanismo es otro y no lo hemos visto: suponemos que Resolve usa IOHIDManager y que filtra por VID y PID. **[inferido]**

## Descriptores USB

Descriptor de dispositivo, tal cual en la captura: `12 01 00 02 EF 02 01 40 DB 1E 0F DA 00 01 01 02 03 01`. Es un dispositivo compuesto (clase EF/02/01) con VID 0x1edb, PID 0xda0f y bcdDevice 0x0100. **[captura]**

La configuración trae tres interfaces: una vendor (FF), una de DFU (FE/01/01) y la HID, con un endpoint IN y uno OUT de interrupción de 64 bytes. El descriptor de reporte HID real mide 297 bytes y no aparece en la captura. El gadget usa uno escrito a mano (87 bytes) que declara los mismos IDs y tamaños: está en `tools/mcp_gadget.sh` y en `macos/mcp_hid_mac.c`. Como Resolve no lo lee, alcanza con que el kernel lo acepte.

## Arranque

Esto manda Resolve al abrir el panel (tiempos en segundos de la captura): **[captura]**

| t | Qué | Bytes |
|---|---|---|
| 7.774 | SET_REPORT Feature 0x03 (Brightness) | `03 00 00` |
| 7.775 | OUT 0x04 (indicadores) | `04 00 00 00 00 00 00` |
| 7.777 | OUT 0x02 (LEDs) | `02` y 8 ceros |
| 7.785 | GET_REPORT Feature 0x01 (SystemInfo) | responde `01 15 5b 3b fd 02 02 00` |
| 7.785 | SET_REPORT Feature 0x0A (SessionEnabled) | `0a 01` |
| 7.870 | GET_REPORT Feature 0x08 (BatteryStatus) | responde `08 01 63` |
| 9.925 | SET_REPORT Feature 0x03 | `03 64 64` |

Después, cada 30 segundos repite `0a 01` y `03 64 64`.

SystemInfo es un u32 (`15 5b 3b fd`, quizá un identificador) seguido de la versión de firmware. Resolve la lee como `Firmware version: 131584` = 0x020200 = 2.2.0. Si el GET llega antes de que haya respuesta, Resolve anota firmware 0 y de todos modos cuenta el panel. **[probado]** BatteryStatus `08 01 63` se lee como "cargando, 99 %". **[inferido]**

## Reportes de entrada (panel → Resolve)

| ID | Largo | Contenido |
|---|---|---|
| 0x02 | 9 B | bitmap de 64 bits, little endian: bits 0 a 11 = pulsadores de las perillas, 12 a 51 = las 40 teclas |
| 0x05 | 37 B | 9 × int32 LE: X1 Y1 anillo1 X2 Y2 anillo2 X3 Y3 anillo3 |
| 0x06 | 65 B | 16 × int32 LE: slot *i* = perilla *i* |

Las perillas mandan deltas en múltiplos de 360. En la captura nunca hay más de un slot distinto de cero por reporte 0x06, y un giro rápido manda de 1080 a 1800 por reporte, con picos de ±112 680. Los anillos mandan de ±1 a ±2 × 4096 cada 8 ms y las bolas, bloques más grandes cada 48 ms. **[captura]**

Orden de las perillas, de izquierda a derecha según el manual: 0 Y LIFT, 1 Y GAMMA, 2 Y GAIN, 3 CONTRAST, 4 PIVOT, 5 MID DETAIL, 6 COLOR BOOST, 7 SHADOWS, 8 HIGHLIGHTS, 9 SATURATION, 10 HUE, 11 LUM MIX. Contrast (3), Mid Detail (5) y Saturation (9) se comprobaron en Resolve. **[probado]**

### Escala y aceleración

Resolve acelera según la velocidad: cuenta unidades por unidad de tiempo, no solo el tamaño de cada reporte. Medido con la API de scripting sobre la Saturation de un nodo (`GetCDL()["Saturation"] × 50`): **[probado]**

| Patrón de reportes | ΔSat | Sat por millón de unidades |
|---|---|---|
| 1 reporte de +36 000 | +0.40 | 11.1 |
| 1 reporte de +360 000 | +8.80 | 24.4 |
| 10 × +36 000, uno cada 24 ms | +8.40 | 23.3 |
| 100 × +3 600, uno cada 24 ms | +2.17 | 6.0 |

Por eso un regreso rápido no deshace un avance lento. Un giro de un segundo en la captura suma entre 0.7 y 1.3 millones de unidades, con un reporte cada 24 ms (mediana). `mcp_falso.py` usa ×100 (36 000 unidades) por toque en las perillas y ×25 (8192 = 2 × 4096) en los anillos.

### Los anillos y la Global de HDR

Con la paleta HDR abierta y OFFSET puesto, los anillos manejan la rueda Global: el izquierdo (slot 2) es Temp, el central (slot 5) Tint y el derecho (slot 8) Exp. Coincide con el manual, que dice que en modo Offset el anillo izquierdo es la temperatura y el derecho la "master exposure". No hacen falta las bolas. **[probado]**

## Reportes de salida y features (Resolve → panel)

| ID | Tipo | Contenido |
|---|---|---|
| 0x02 | Output, 9 B | LEDs de las teclas; suponemos el mismo orden de bits que el 0x02 de entrada **[inferido]** |
| 0x04 | Output, 7 B | indicadores: byte 1 = 0x10 con SHIFT UP puesto, 0x20 con SHIFT DOWN **[captura]** |
| 0x01 | Feature, 8 B | SystemInfo (ver arriba) |
| 0x03 | Feature, 3 B | Brightness |
| 0x08 | Feature, 3 B | BatteryStatus |
| 0x0A | Feature, 2 B | SessionEnabled |

## Las teclas

El bit de cada tecla en el reporte 0x02 es el índice de `davinci_panel_mapping.json` de ra100 menos 8. Que 24 y 25 son los Shift lo apoya la captura (Resolve responde con `04 10` y `04 20`), pero falta confirmar cuál es SHIFT UP y cuál SHIFT DOWN. Una prueba que sirve: SHIFT UP + ADD NODE da un nodo paralelo y SHIFT DOWN + ADD NODE, un layer.

| Bit | Tecla | Bit | Tecla | Bit | Tecla | Bit | Tecla |
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

Algunas combinaciones del manual que usa el puente de la ESP32-C3:

- SHIFT UP + OFFSET alterna el modo Log de las bolas.
- RESET LIFT, RESET GAMMA y RESET GAIN resetean lo que mueven la bola y el anillo izquierdo, central y derecho. Con SHIFT DOWN resetean solo el nivel, es decir, el anillo.

Lo que hace cada tecla con SHIFT UP y SHIFT DOWN está en `FUNCIONES_SHIFT` de `tools/mcp_falso.py`, sacado de la descripción tecla por tecla del manual.

## El gadget de Linux (f_hid)

Detalles de `drivers/usb/gadget/function/f_hid.c` (kernel 7.2) que importan aquí:

- `report_length` es a la vez el wMaxPacketSize del endpoint IN y el tope de cada `write()`. Con 65 el reporte 0x06 cabe en un paquete a High Speed, que es la velocidad por defecto de `dummy_hcd`.
- Los SET_REPORT de features solo llegan con `no_out_endpoint=1`; con endpoint OUT, f_hid les responde STALL. Así los OUT de Resolve (0x02 y 0x04) también llegan como SET_REPORT y se leen con `read()` en `/dev/hidgN`.
- GET_REPORT se indexa solo por report ID y responde los bytes tal cual (el primero es el ID). Las respuestas se registran con el ioctl `GADGET_HID_WRITE_GET_REPORT` (0x40486742, struct de 72 bytes) y se quedan en el kernel aunque el programa se cierre. Un ID sin respuesta registrada despierta a userspace con POLLPRI (`GADGET_HID_READ_GET_REPORT_ID`, 0x80016741) y, si nadie contesta en 2.5 s, el kernel manda ceros.
- Con `O_NONBLOCK`, `write()` da EAGAIN cuando nadie lee el hidraw. `mcp_falso.py` lo usa para avisar que Resolve no abrió el panel. Si el host deshabilitó la función, `read()` da ENOMEM; si no la ha configurado, `write()` da ESHUTDOWN.
- Para "desconectar el cable" sin cerrar Resolve hay que desligar y religar el UDC. Con `soft_connect` `dummy_hcd` ya no enumera (error -71).

## macOS (IOHIDUserDevice)

`macos/mcp_hid_mac` crea el panel con `IOHIDUserDeviceCreateWithProperties`, con las mismas propiedades (VendorID, ProductID, Transport "USB", nombres y descriptor) y las mismas respuestas a GET_REPORT. Los reportes de entrada salen con `IOHIDUserDeviceHandleReportWithTimeStamp`; los SET_REPORT llegan por un block registrado con `IOHIDUserDeviceRegisterSetReportBlock`.

Crear un IOHIDUserDevice pide el entitlement `com.apple.developer.hid.virtual.device`, y Apple no lo da sin una solicitud aprobada. Con una firma ad hoc, macOS solo lo respeta con SIP desactivado. Nada de esto se probó en una Mac: el ayudante se compiló y se corrió en Linux contra un IOKit simulado, y eso solo comprueba nuestra lógica, no la de macOS ni la de Resolve.

## Para decodificar la captura otra vez

```sh
tshark -r davinci_interaction_capture.pcapng \
  -Y 'usb.device_address==65 && usb.transfer_type==1 && usb.endpoint_address==0x81 && usb.data_len>0' \
  -T fields -e frame.number -e frame.time_relative -e usbhid.data \
  | python3 tools/decodificar_captura.py
```
