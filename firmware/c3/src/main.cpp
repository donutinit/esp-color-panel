// ESP32-C3 SuperMini con 3 encoders EC11 (con pulsador) y 4 botones, para el Micro Color Panel falso.
// Cada evento es una línea de texto por el USB:
//   E1 +1 q+4    encoder 1 avanzó un clic (q = cuartos de paso, para saber qué tipo de encoder es)
//   E1 0 q+2     se movió sin completar un clic
//   P1 1 / P1 0  pulsador del encoder 1 apretado / soltado
//   B1 1 / B1 0  botón 1 apretado / soltado
//   PINES ...    nivel de cada pin (al recibir '?')
// Todo va de pin a GND con el pull-up interno: 1 = suelto, 0 = cerrado.
#include <Arduino.h>
#include "esp_timer.h"
#include "soc/gpio_reg.h"

struct Enc { uint8_t a, b; int8_t signo; volatile int32_t clics, cuartos; int8_t acc; uint8_t st; };
static Enc enc[3] = {{7, 8, +1}, {1, 0, -1}, {3, 2, -1}};   // {A, B, signo}: -1 = sentido invertido

struct Sw { const char *nom; uint8_t pin; volatile bool abajo; uint8_t cuenta; };
static Sw sw[7] = {{"P1", 6}, {"P2", 5}, {"P3", 4}, {"B1", 10}, {"B2", 20}, {"B3", 21}, {"B4", 9}};

// índice = (estado anterior << 2) | estado nuevo; estado = (A << 1) | B
static const int8_t TABLA[16] = {0, -1, +1, 0, +1, 0, 0, -1, -1, 0, 0, +1, 0, +1, -1, 0};

static void escanear(void *) {   // cada 500 µs
    uint32_t in = REG_READ(GPIO_IN_REG);
    for (auto &e : enc) {
        uint8_t s = (((in >> e.a) & 1) << 1) | ((in >> e.b) & 1);
        int8_t d = TABLA[(e.st << 2) | s] * e.signo;
        e.st = s;
        if (d) { e.acc += d; e.cuartos += d; }
        if (s == 3) {   // reposo del EC11: los dos contactos abiertos
            if (e.acc >= 2) e.clics++;
            else if (e.acc <= -2) e.clics--;
            e.acc = 0;
        }
    }
    for (auto &w : sw) {   // antirrebote: 5 ms estable
        bool abajo = !((in >> w.pin) & 1);
        if (abajo == w.abajo) w.cuenta = 0;
        else if (++w.cuenta >= 10) { w.abajo = abajo; w.cuenta = 0; }
    }
}

static void pines() {
    uint32_t in = REG_READ(GPIO_IN_REG);
    Serial.print("PINES");
    for (int i = 0; i < 3; i++)
        Serial.printf(" E%dA:%d=%d E%dB:%d=%d", i + 1, enc[i].a, (int)((in >> enc[i].a) & 1),
                      i + 1, enc[i].b, (int)((in >> enc[i].b) & 1));
    for (auto &w : sw) Serial.printf(" %s:%d=%d", w.nom, w.pin, (int)((in >> w.pin) & 1));
    Serial.println();
}

void setup() {
    Serial.begin(115200);
    Serial.setTxTimeoutMs(0);   // si nadie lee el puerto, no se traba
    for (auto &e : enc) { pinMode(e.a, INPUT_PULLUP); pinMode(e.b, INPUT_PULLUP); }
    for (auto &w : sw) pinMode(w.pin, INPUT_PULLUP);
    delay(5);
    uint32_t in = REG_READ(GPIO_IN_REG);
    for (auto &e : enc) e.st = (((in >> e.a) & 1) << 1) | ((in >> e.b) & 1);
    for (auto &w : sw) w.abajo = !((in >> w.pin) & 1);
    const esp_timer_create_args_t args = {.callback = escanear, .name = "escaneo"};
    esp_timer_handle_t t;
    esp_timer_create(&args, &t);
    esp_timer_start_periodic(t, 500);
}

void loop() {
    static int32_t clics_ant[3], cuartos_ant[3];
    static uint32_t t_mov[3];
    static bool abajo_ant[7];
    static bool primera = true;
    if (primera) { for (int j = 0; j < 7; j++) abajo_ant[j] = sw[j].abajo; primera = false; }

    for (int i = 0; i < 3; i++) {
        int32_t c = enc[i].clics, q = enc[i].cuartos;
        if (c != clics_ant[i]) {
            Serial.printf("E%d %+d q%+d\n", i + 1, (int)(c - clics_ant[i]), (int)(q - cuartos_ant[i]));
            clics_ant[i] = c; cuartos_ant[i] = q; t_mov[i] = 0;
        } else if (q != cuartos_ant[i]) {   // se movió pero no llegó al reposo
            if (!t_mov[i]) t_mov[i] = millis();
            else if (millis() - t_mov[i] > 400) {
                Serial.printf("E%d 0 q%+d\n", i + 1, (int)(q - cuartos_ant[i]));
                cuartos_ant[i] = q; t_mov[i] = 0;
            }
        }
    }
    for (int j = 0; j < 7; j++) {
        bool a = sw[j].abajo;
        if (a != abajo_ant[j]) { Serial.printf("%s %d\n", sw[j].nom, a ? 1 : 0); abajo_ant[j] = a; }
    }
    while (Serial.available()) if (Serial.read() == '?') { Serial.println("HOLA mcp-c3 1"); pines(); }
    delay(1);
}
