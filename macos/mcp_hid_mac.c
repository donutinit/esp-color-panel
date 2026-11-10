// mcp_hid_mac.c: the fake Micro Color Panel on macOS, as a virtual HID (IOHIDUserDevice) with the panel's VID/PID.
//
// The counterpart of the Linux USB gadget (tools/mcp_gadget.sh): it only creates the device and passes bytes.
// The logic (knobs, keys, UI) stays in tools/fake_panel.py, which launches it and talks to it over stdin/stdout
// in text lines:
//
//   fake_panel.py → here   IN <hex>             input report for Resolve (the first byte is the ID)
//                          GET <hex>            reply to GET_REPORTs for that ID (first byte = ID)
//   here → fake_panel.py   READY                the device exists
//                          SET <type> <hex>     Resolve sent a SET_REPORT (type 1 = output, 2 = feature)
//                          GETREQ <type> <id>   Resolve asked for a GET_REPORT with no reply ready
//                          ERR <text>
//
// Creating an IOHIDUserDevice needs the entitlement com.apple.developer.hid.virtual.device, which Apple doesn't
// hand out. Ad hoc signed, it only runs with SIP disabled and the boot-arg amfi_get_out_of_my_way=1: see the README.
// Build and sign: macos/build.sh
//
// Tested 2026-11-09 on macOS 26.3 (Apple Silicon) with DaVinci Resolve Studio 21.0.2: Resolve takes it as the panel and
// opens a session. What the knobs and keys do in Resolve on macOS is not confirmed yet.
#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/hid/IOHIDKeys.h>
#include <IOKit/hidsystem/IOHIDUserDevice.h>
#include <dispatch/dispatch.h>
#include <mach/mach_time.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// The same descriptor as tools/mcp_gadget.sh (handwritten: the real one isn't public). If you change one, change the other.
static const uint8_t DESCRIPTOR[] = {
    0x06, 0x00, 0xFF, 0x09, 0x01, 0xA1, 0x01,         // Usage Page 0xFF00, Usage 1, Collection (Application)
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08,         // 0..255, 8 bits
    0x85, 0x02, 0x09, 0x01, 0x95, 0x08, 0x81, 0x02,   // Input   0x02 buttons (64-bit bitmap)
    0x85, 0x05, 0x09, 0x02, 0x95, 0x24, 0x81, 0x02,   // Input   0x05 trackballs and rings (9 x int32)
    0x85, 0x06, 0x09, 0x03, 0x95, 0x40, 0x81, 0x02,   // Input   0x06 knobs (16 x int32)
    0x85, 0x02, 0x09, 0x04, 0x95, 0x08, 0x91, 0x02,   // Output  0x02 key LEDs
    0x85, 0x04, 0x09, 0x05, 0x95, 0x06, 0x91, 0x02,   // Output  0x04 indicators
    0x85, 0x01, 0x09, 0x06, 0x95, 0x07, 0xB1, 0x02,   // Feature 0x01 SystemInfo
    0x85, 0x03, 0x09, 0x07, 0x95, 0x02, 0xB1, 0x02,   // Feature 0x03 Brightness
    0x85, 0x08, 0x09, 0x08, 0x95, 0x02, 0xB1, 0x02,   // Feature 0x08 BatteryStatus
    0x85, 0x0A, 0x09, 0x09, 0x95, 0x01, 0xB1, 0x02,   // Feature 0x0A SessionEnabled
    0xC0,                                             // End Collection
};

#define MAX_REPORT 72
static uint8_t replies[256][MAX_REPORT];
static CFIndex reply_len[256];
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;

static void store_reply(const uint8_t *d, size_t n) {
    if (n == 0 || n > MAX_REPORT) return;
    pthread_mutex_lock(&lock);
    memcpy(replies[d[0]], d, n);
    reply_len[d[0]] = (CFIndex)n;
    pthread_mutex_unlock(&lock);
}

static size_t hex_to_bytes(const char *s, uint8_t *out, size_t max) {
    size_t n = 0;
    int hi = -1;
    for (; *s && n < max; s++) {
        int v;
        if (*s >= '0' && *s <= '9') v = *s - '0';
        else if (*s >= 'a' && *s <= 'f') v = *s - 'a' + 10;
        else if (*s >= 'A' && *s <= 'F') v = *s - 'A' + 10;
        else continue;                                 // spaces and newline
        if (hi < 0) hi = v;
        else { out[n++] = (uint8_t)(hi << 4 | v); hi = -1; }
    }
    return n;
}

static void print_hex(const char *prefix, const uint8_t *d, CFIndex n) {
    char line[32 + 2 * 256];
    int k = snprintf(line, sizeof line, "%s", prefix);
    for (CFIndex i = 0; i < n && k < (int)sizeof line - 3; i++) k += snprintf(line + k, sizeof line - k, "%02x", d[i]);
    printf("%s\n", line);                              // a single printf: lines from different threads don't mix
}

static void set_number(CFMutableDictionaryRef d, const char *key, int64_t v) {
    CFStringRef k = CFStringCreateWithCString(NULL, key, kCFStringEncodingUTF8);
    CFNumberRef n = CFNumberCreate(NULL, kCFNumberSInt64Type, &v);
    CFDictionarySetValue(d, k, n);
    CFRelease(n);
    CFRelease(k);
}

static void set_string(CFMutableDictionaryRef d, const char *key, const char *value) {
    CFStringRef k = CFStringCreateWithCString(NULL, key, kCFStringEncodingUTF8);
    CFStringRef v = CFStringCreateWithCString(NULL, value, kCFStringEncodingUTF8);
    CFDictionarySetValue(d, k, v);
    CFRelease(v);
    CFRelease(k);
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);

    // GET_REPORT replies with the bytes from ra100's capture: Resolve asks for SystemInfo as soon as it opens the panel,
    // so they have to be in place before fake_panel.py sends its own.
    static const uint8_t f01[] = {0x01, 0x15, 0x5b, 0x3b, 0xfd, 0x02, 0x02, 0x00};   // SystemInfo, firmware 2.2.0
    static const uint8_t f08[] = {0x08, 0x01, 0x63};                                 // BatteryStatus: 99 %
    static const uint8_t f03[] = {0x03, 0x64, 0x64};                                 // Brightness
    static const uint8_t f0a[] = {0x0a, 0x01};                                       // SessionEnabled
    store_reply(f01, sizeof f01);
    store_reply(f08, sizeof f08);
    store_reply(f03, sizeof f03);
    store_reply(f0a, sizeof f0a);

    CFMutableDictionaryRef p = CFDictionaryCreateMutable(NULL, 0, &kCFTypeDictionaryKeyCallBacks,
                                                         &kCFTypeDictionaryValueCallBacks);
    CFDataRef desc = CFDataCreate(NULL, DESCRIPTOR, sizeof DESCRIPTOR);
    CFDictionarySetValue(p, CFSTR(kIOHIDReportDescriptorKey), desc);
    CFRelease(desc);
    set_number(p, kIOHIDVendorIDKey, 0x1edb);
    set_number(p, kIOHIDProductIDKey, 0xda0f);
    set_number(p, kIOHIDVersionNumberKey, 0x0100);
    set_number(p, kIOHIDLocationIDKey, 0x1edbda0f);
    set_number(p, kIOHIDPrimaryUsagePageKey, 0xff00);
    set_number(p, kIOHIDPrimaryUsageKey, 0x01);
    set_number(p, kIOHIDMaxInputReportSizeKey, 65);
    set_number(p, kIOHIDMaxOutputReportSizeKey, 9);
    set_number(p, kIOHIDMaxFeatureReportSizeKey, 8);
    set_string(p, kIOHIDTransportKey, "USB");
    set_string(p, kIOHIDManufacturerKey, "Blackmagic Design");
    set_string(p, kIOHIDProductKey, "DaVinci Resolve Micro Color Panel");
    set_string(p, kIOHIDSerialNumberKey, "ESPCOLORPANEL01");

    IOHIDUserDeviceRef dev = IOHIDUserDeviceCreateWithProperties(kCFAllocatorDefault, p, 0);
    CFRelease(p);
    if (!dev) {
        printf("ERR macOS wouldn't create the virtual HID (IOHIDUserDeviceCreateWithProperties returned NULL): "
               "is the com.apple.developer.hid.virtual.device entitlement missing, or is SIP enabled?\n");
        return 1;
    }

    dispatch_queue_t queue = dispatch_queue_create("esp-color-panel.hid", DISPATCH_QUEUE_SERIAL);
    IOHIDUserDeviceSetDispatchQueue(dev, queue);

    IOHIDUserDeviceRegisterGetReportBlock(dev, ^IOReturn(IOHIDReportType type, uint32_t id, uint8_t *report,
                                                         CFIndex *n) {
        pthread_mutex_lock(&lock);
        CFIndex k = reply_len[id & 0xff];
        if (k) {
            if (k > *n) k = *n;
            memcpy(report, replies[id & 0xff], (size_t)k);
        }
        pthread_mutex_unlock(&lock);
        if (!k) {                                      // as on Linux: reply with just the ID and report it
            printf("GETREQ %d %u\n", (int)type, id);
            report[0] = (uint8_t)id;
            k = 1;
        }
        *n = k;
        return kIOReturnSuccess;
    });

    IOHIDUserDeviceRegisterSetReportBlock(dev, ^IOReturn(IOHIDReportType type, uint32_t id, const uint8_t *report,
                                                         CFIndex n) {
        char prefix[16];
        snprintf(prefix, sizeof prefix, "SET %d ", (int)type);
        if (n > 0 && report[0] == (uint8_t)id) {
            print_hex(prefix, report, n);              // already starts with the ID, as on USB
        } else {                                       // in case macOS stripped it: fake_panel.py expects it
            uint8_t with_id[1 + 256];
            CFIndex m = n > 256 ? 256 : n;
            with_id[0] = (uint8_t)id;
            memcpy(with_id + 1, report, (size_t)m);
            print_hex(prefix, with_id, m + 1);
        }
        return kIOReturnSuccess;
    });

    IOHIDUserDeviceActivate(dev);
    printf("READY\n");

    char line[1024];
    uint8_t buf[MAX_REPORT];
    while (fgets(line, sizeof line, stdin)) {
        if (!strncmp(line, "IN ", 3)) {
            size_t n = hex_to_bytes(line + 3, buf, sizeof buf);
            if (!n) continue;
            IOReturn r = IOHIDUserDeviceHandleReportWithTimeStamp(dev, mach_absolute_time(), buf, (CFIndex)n);
            if (r != kIOReturnSuccess) printf("ERR HandleReport 0x%08x\n", r);
        } else if (!strncmp(line, "GET ", 4)) {
            size_t n = hex_to_bytes(line + 4, buf, sizeof buf);
            store_reply(buf, n);
        }
    }

    IOHIDUserDeviceCancel(dev);                        // stdin closed: fake_panel.py exited
    CFRelease(dev);
    return 0;
}
