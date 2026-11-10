// mcp_hid_mac.c: el Micro Color Panel falso en macOS, como HID virtual (IOHIDUserDevice) con el VID/PID del panel.
//
// Es la contraparte del gadget USB de Linux (tools/mcp_gadget.sh): solo crea el dispositivo y pasa bytes.
// La lógica (perillas, teclas, interfaz) sigue en tools/mcp_falso.py, que lo lanza y le habla por stdin/stdout
// en líneas de texto:
//
//   mcp_falso.py → aquí    IN <hex>             reporte de entrada para Resolve (el primer byte es el ID)
//                          GET <hex>            respuesta para los GET_REPORT de ese ID (primer byte = ID)
//   aquí → mcp_falso.py    READY                el dispositivo ya existe
//                          SET <tipo> <hex>     Resolve mandó un SET_REPORT (tipo 1 = output, 2 = feature)
//                          GETREQ <tipo> <id>   Resolve pidió un GET_REPORT sin respuesta preparada
//                          ERR <texto>
//
// Crear un IOHIDUserDevice pide el entitlement com.apple.developer.hid.virtual.device, que Apple no da a
// cualquiera. Firmado ad hoc solo corre con SIP desactivado (y a veces con amfi_get_out_of_my_way=1): ver el README.
// Compilar y firmar: macos/build.sh
//
// SIN PROBAR: se escribió sin una Mac a la mano. No se sabe si Resolve en macOS acepta un HID virtual como panel.
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/hid/IOHIDKeys.h>
#include <IOKit/hidsystem/IOHIDUserDevice.h>
#include <dispatch/dispatch.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// El mismo descriptor que tools/mcp_gadget.sh (escrito a mano: el real no es público). Si cambias uno, cambia el otro.
static const uint8_t DESCRIPTOR[] = {
    0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01,         // Usage Page 0xFF00, Usage 1, Collection (Application)
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08,         // 0..255, 8 bits
    0x85, 0x02, 0x09, 0x01, 0x95, 0x08, 0x81, 0x02,   // Input   0x02 botones (bitmap 64 bits)
    0x85, 0x05, 0x09, 0x02, 0x95, 0x24, 0x81, 0x02,   // Input   0x05 trackballs y anillos (9 x int32)
    0x85, 0x06, 0x09, 0x03, 0x95, 0x40, 0x81, 0x02,   // Input   0x06 perillas (16 x int32)
    0x85, 0x02, 0x09, 0x04, 0x95, 0x08, 0x91, 0x02,   // Output  0x02 LEDs de las teclas
    0x85, 0x04, 0x09, 0x05, 0x95, 0x06, 0x91, 0x02,   // Output  0x04 indicadores
    0x85, 0x01, 0x09, 0x06, 0x95, 0x07, 0xB1, 0x02,   // Feature 0x01 SystemInfo
    0x85, 0x03, 0x09, 0x07, 0x95, 0x02, 0xB1, 0x02,   // Feature 0x03 Brightness
    0x85, 0x08, 0x09, 0x08, 0x95, 0x02, 0xB1, 0x02,   // Feature 0x08 BatteryStatus
    0x85, 0x0A, 0x09, 0x09, 0x95, 0x01, 0xB1, 0x02,   // Feature 0x0A SessionEnabled
    0xC0,                                             // End Collection
};

#define MAX_REPORTE 72
static uint8_t respuestas[256][MAX_REPORTE];
static CFIndex largo[256];
static pthread_mutex_t candado = PTHREAD_MUTEX_INITIALIZER;

static void guardar_respuesta(const uint8_t *d, size_t n) {
    if (n == 0 || n > MAX_REPORTE) return;
    pthread_mutex_lock(&candado);
    memcpy(respuestas[d[0]], d, n);
    largo[d[0]] = (CFIndex)n;
    pthread_mutex_unlock(&candado);
}

static size_t hex_a_bytes(const char *s, uint8_t *out, size_t max) {
    size_t n = 0;
    int alto = -1;
    for (; *s && n < max; s++) {
        int v;
        if (*s >= '0' && *s <= '9') v = *s - '0';
        else if (*s >= 'a' && *s <= 'f') v = *s - 'a' + 10;
        else if (*s >= 'A' && *s <= 'F') v = *s - 'A' + 10;
        else continue;                                 // espacios y fin de línea
        if (alto < 0) alto = v;
        else { out[n++] = (uint8_t)(alto << 4 | v); alto = -1; }
    }
    return n;
}

static void imprimir_hex(const char *prefijo, const uint8_t *d, CFIndex n) {
    char linea[32 + 2 * 256];
    int k = snprintf(linea, sizeof linea, "%s", prefijo);
    for (CFIndex i = 0; i < n && k < (int)sizeof linea - 3; i++) k += snprintf(linea + k, sizeof linea - k, "%02x", d[i]);
    printf("%s\n", linea);                             // un solo printf: las líneas no se mezclan entre hilos
}

static void poner_numero(CFMutableDictionaryRef d, const char *clave, int64_t v) {
    CFStringRef k = CFStringCreateWithCString(NULL, clave, kCFStringEncodingUTF8);
    CFNumberRef n = CFNumberCreate(NULL, kCFNumberSInt64Type, &v);
    CFDictionarySetValue(d, k, n);
    CFRelease(n);
    CFRelease(k);
}

static void poner_texto(CFMutableDictionaryRef d, const char *clave, const char *valor) {
    CFStringRef k = CFStringCreateWithCString(NULL, clave, kCFStringEncodingUTF8);
    CFStringRef v = CFStringCreateWithCString(NULL, valor, kCFStringEncodingUTF8);
    CFDictionarySetValue(d, k, v);
    CFRelease(v);
    CFRelease(k);
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);

    // Respuestas a GET_REPORT con los bytes de la captura de ra100: Resolve pide SystemInfo apenas abre el panel,
    // así que tienen que estar antes de que mcp_falso.py mande las suyas.
    static const uint8_t f01[] = {0x01, 0x15, 0x5b, 0x3b, 0xfd, 0x02, 0x02, 0x00};   // SystemInfo, firmware 2.2.0
    static const uint8_t f08[] = {0x08, 0x01, 0x63};                                 // BatteryStatus: 99 %
    static const uint8_t f03[] = {0x03, 0x64, 0x64};                                 // Brightness
    static const uint8_t f0a[] = {0x0a, 0x01};                                       // SessionEnabled
    guardar_respuesta(f01, sizeof f01);
    guardar_respuesta(f08, sizeof f08);
    guardar_respuesta(f03, sizeof f03);
    guardar_respuesta(f0a, sizeof f0a);

    CFMutableDictionaryRef p = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks,
                                                         &kCFTypeDictionaryValueCallBacks);
    CFDataRef desc = CFDataCreate(NULL, DESCRIPTOR, sizeof DESCRIPTOR);
    CFDictionarySetValue(p, CFSTR(kIOHIDReportDescriptorKey), desc);
    CFRelease(desc);
    poner_numero(p, kIOHIDVendorIDKey, 0x1edb);
    poner_numero(p, kIOHIDProductIDKey, 0xda0f);
    poner_numero(p, kIOHIDVersionNumberKey, 0x0100);
    poner_numero(p, kIOHIDLocationIDKey, 0x1edbda0f);
    poner_numero(p, kIOHIDPrimaryUsagePageKey, 0xff00);
    poner_numero(p, kIOHIDPrimaryUsageKey, 0x01);
    poner_numero(p, kIOHIDMaxInputReportSizeKey, 65);
    poner_numero(p, kIOHIDMaxOutputReportSizeKey, 9);
    poner_numero(p, kIOHIDMaxFeatureReportSizeKey, 8);
    poner_texto(p, kIOHIDTransportKey, "USB");
    poner_texto(p, kIOHIDManufacturerKey, "Blackmagic Design");
    poner_texto(p, kIOHIDProductKey, "DaVinci Resolve Micro Color Panel");
    poner_texto(p, kIOHIDSerialNumberKey, "MCPFALSO0000001");

    IOHIDUserDeviceRef dev = IOHIDUserDeviceCreateWithProperties(kCFAllocatorDefault, p, 0);
    CFRelease(p);
    if (!dev) {
        printf("ERR macOS no dejó crear el HID virtual (IOHIDUserDeviceCreateWithProperties devolvió NULL): "
               "¿falta el entitlement com.apple.developer.hid.virtual.device o SIP está activo?\n");
        return 1;
    }

    dispatch_queue_t cola = dispatch_queue_create("mcp-falso.hid", DISPATCH_QUEUE_SERIAL);
    IOHIDUserDeviceSetDispatchQueue(dev, cola);

    IOHIDUserDeviceRegisterGetReportBlock(dev, ^IOReturn(IOHIDReportType tipo, uint32_t id, uint8_t *reporte,
                                                         CFIndex *n) {
        pthread_mutex_lock(&candado);
        CFIndex k = largo[id & 0xff];
        if (k) {
            if (k > *n) k = *n;
            memcpy(reporte, respuestas[id & 0xff], (size_t)k);
        }
        pthread_mutex_unlock(&candado);
        if (!k) {                                      // como en Linux: contesta solo el ID y avisa
            printf("GETREQ %d %u\n", (int)tipo, id);
            reporte[0] = (uint8_t)id;
            k = 1;
        }
        *n = k;
        return kIOReturnSuccess;
    });

    IOHIDUserDeviceRegisterSetReportBlock(dev, ^IOReturn(IOHIDReportType tipo, uint32_t id, const uint8_t *reporte,
                                                         CFIndex n) {
        char prefijo[16];
        snprintf(prefijo, sizeof prefijo, "SET %d ", (int)tipo);
        if (n > 0 && reporte[0] == (uint8_t)id) {
            imprimir_hex(prefijo, reporte, n);         // ya trae el ID al principio, como en USB
        } else {                                       // por si macOS lo quitó: mcp_falso.py lo espera
            uint8_t con_id[1 + 256];
            CFIndex m = n > 256 ? 256 : n;
            con_id[0] = (uint8_t)id;
            memcpy(con_id + 1, reporte, (size_t)m);
            imprimir_hex(prefijo, con_id, m + 1);
        }
        return kIOReturnSuccess;
    });

    IOHIDUserDeviceActivate(dev);
    printf("READY\n");

    char linea[1024];
    uint8_t buf[MAX_REPORTE];
    while (fgets(linea, sizeof linea, stdin)) {
        if (!strncmp(linea, "IN ", 3)) {
            size_t n = hex_a_bytes(linea + 3, buf, sizeof buf);
            if (!n) continue;
            IOReturn r = IOHIDUserDeviceHandleReportWithTimeStamp(dev, mach_absolute_time(), buf, (CFIndex)n);
            if (r != kIOReturnSuccess) printf("ERR HandleReport 0x%08x\n", r);
        } else if (!strncmp(linea, "GET ", 4)) {
            size_t n = hex_a_bytes(linea + 4, buf, sizeof buf);
            guardar_respuesta(buf, n);
        }
    }

    IOHIDUserDeviceCancel(dev);                        // stdin cerrado: mcp_falso.py salió
    CFRelease(dev);
    return 0;
}
