// ESP32-C3 SuperMini with 3 EC11 encoders (with push switch) and 4 buttons, for the fake Micro Color Panel.
// Each event is a text line over USB:
//   E1 +1 q+4    encoder 1 moved one click forward (q = quarter steps, to tell which kind of encoder it is)
//   E1 0 q+2     it moved without completing a click
//   P1 1 / P1 0  encoder 1 push switch pressed / released
//   B1 1 / B1 0  button 1 pressed / released
//   PINS ...     level of each pin (on '?')
// Everything goes from pin to GND with the internal pull-up: 1 = open, 0 = closed.
#include <Arduino.h>
#include "esp_timer.h"
#include "soc/gpio_reg.h"

struct Enc { uint8_t a, b; int8_t sign; volatile int32_t clicks, quarters; int8_t acc; uint8_t st; };
static Enc enc[3] = {{7, 8, +1}, {1, 0, -1}, {3, 2, -1}};   // {A, B, sign}: -1 = reversed direction

struct Sw { const char *name; uint8_t pin; volatile bool down; uint8_t count; };
static Sw sw[7] = {{"P1", 6}, {"P2", 5}, {"P3", 4}, {"B1", 10}, {"B2", 20}, {"B3", 21}, {"B4", 9}};

// index = (previous state << 2) | new state; state = (A << 1) | B
static const int8_t TABLE[16] = {0, -1, +1, 0, +1, 0, 0, -1, -1, 0, 0, +1, 0, +1, -1, 0};

static void scan(void *) {   // every 500 µs
    uint32_t in = REG_READ(GPIO_IN_REG);
    for (auto &e : enc) {
        uint8_t s = (((in >> e.a) & 1) << 1) | ((in >> e.b) & 1);
        int8_t d = TABLE[(e.st << 2) | s] * e.sign;
        e.st = s;
        if (d) { e.acc += d; e.quarters += d; }
        if (s == 3) {   // EC11 detent: both contacts open
            if (e.acc >= 2) e.clicks++;
            else if (e.acc <= -2) e.clicks--;
            e.acc = 0;
        }
    }
    for (auto &w : sw) {   // debounce: 5 ms stable
        bool down = !((in >> w.pin) & 1);
        if (down == w.down) w.count = 0;
        else if (++w.count >= 10) { w.down = down; w.count = 0; }
    }
}

static void pins() {
    uint32_t in = REG_READ(GPIO_IN_REG);
    Serial.print("PINS");
    for (int i = 0; i < 3; i++)
        Serial.printf(" E%dA:%d=%d E%dB:%d=%d", i + 1, enc[i].a, (int)((in >> enc[i].a) & 1),
                      i + 1, enc[i].b, (int)((in >> enc[i].b) & 1));
    for (auto &w : sw) Serial.printf(" %s:%d=%d", w.name, w.pin, (int)((in >> w.pin) & 1));
    Serial.println();
}

void setup() {
    Serial.begin(115200);
    Serial.setTxTimeoutMs(0);   // don't block if nobody reads the port
    for (auto &e : enc) { pinMode(e.a, INPUT_PULLUP); pinMode(e.b, INPUT_PULLUP); }
    for (auto &w : sw) pinMode(w.pin, INPUT_PULLUP);
    delay(5);
    uint32_t in = REG_READ(GPIO_IN_REG);
    for (auto &e : enc) e.st = (((in >> e.a) & 1) << 1) | ((in >> e.b) & 1);
    for (auto &w : sw) w.down = !((in >> w.pin) & 1);
    const esp_timer_create_args_t args = {.callback = scan, .name = "scan"};
    esp_timer_handle_t t;
    esp_timer_create(&args, &t);
    esp_timer_start_periodic(t, 500);
}

void loop() {
    static int32_t prev_clicks[3], prev_quarters[3];
    static uint32_t t_move[3];
    static bool prev_down[7];
    static bool first = true;
    if (first) { for (int j = 0; j < 7; j++) prev_down[j] = sw[j].down; first = false; }

    for (int i = 0; i < 3; i++) {
        int32_t c = enc[i].clicks, q = enc[i].quarters;
        if (c != prev_clicks[i]) {
            Serial.printf("E%d %+d q%+d\n", i + 1, (int)(c - prev_clicks[i]), (int)(q - prev_quarters[i]));
            prev_clicks[i] = c; prev_quarters[i] = q; t_move[i] = 0;
        } else if (q != prev_quarters[i]) {   // it moved but didn't reach a detent
            if (!t_move[i]) t_move[i] = millis();
            else if (millis() - t_move[i] > 400) {
                Serial.printf("E%d 0 q%+d\n", i + 1, (int)(q - prev_quarters[i]));
                prev_quarters[i] = q; t_move[i] = 0;
            }
        }
    }
    for (int j = 0; j < 7; j++) {
        bool d = sw[j].down;
        if (d != prev_down[j]) { Serial.printf("%s %d\n", sw[j].name, d ? 1 : 0); prev_down[j] = d; }
    }
    while (Serial.available()) if (Serial.read() == '?') { Serial.println("HELLO esp-color-panel 1"); pins(); }
    delay(1);
}
