#include <Arduino.h>

static const int PIN_LED_CLK    = 14;
static const int PIN_LED_LATCH  = 13;
static const int PIN_LED_OE     = 10;  // active low
static const int PIN_LED_DOUT   = 15;
static const int PIN_DATA_CLK   = 18;
static const int PIN_DATA_LATCH = 17;
static const int PIN_DATA_DIN   = 16;

static const int NUM_PORTS = 12;
static const int NUM_LEDS = NUM_PORTS * 2;

static const size_t NUM_LED_BYTES = (NUM_LEDS + 7) / 8;

uint8_t ledData[NUM_LED_BYTES] = {0};
uint8_t portData[NUM_PORTS] = {0};



static const uint8_t ledIdxMapping[] = {
  // Y,  G
     1,  0,     // Port 0
     3,  2,
     5,  4,
     7,  6,
     9,  8, 
    11, 15,     // Port 5 (green swapped with yellow of port 7)
    13, 12,
    10, 14,     // Port 7 (yellow swapped with green of port 5)
    17, 16,
    19, 23,     // Port 9 (green swapped with yellow of port 11)
    21, 20,
    18, 22,     // Port 11 (yellow swapped with green of port 9)
};


void updateLeds() {
    for (int i = 0; i < NUM_LED_BYTES; i++) {
        shiftOut(PIN_LED_DOUT, PIN_LED_CLK, MSBFIRST, ledData[i]);
    }
    digitalWrite(PIN_LED_LATCH, LOW);
    digitalWrite(PIN_LED_LATCH, HIGH);
}

// led: 0 = yellow, 1 = green
void setLed(int port, int led, bool state) {
    int ledIdx = port * 2 + led;
    ledData[ledIdxMapping[ledIdx] / 8] = 1 << (ledIdxMapping[ledIdx] % 8);

    updateLeds();
}


void setup() {
    pinMode(PIN_LED_CLK, OUTPUT);
    pinMode(PIN_LED_LATCH, OUTPUT);
    digitalWrite(PIN_LED_OE, HIGH);
    pinMode(PIN_LED_OE, OUTPUT);
    pinMode(PIN_LED_DOUT, OUTPUT);
    pinMode(PIN_DATA_CLK, OUTPUT);
    digitalWrite(PIN_DATA_CLK, HIGH);
    pinMode(PIN_DATA_LATCH, OUTPUT);

    analogWrite(PIN_LED_OE, 127);

    Serial.begin(115200);
}


uint32_t ledIdx = 0;

void loop() {
    // memset(ledData, 0, sizeof(ledData));
    // ledData[ledIdxMapping[ledIdx] / 8] = 1 << (ledIdxMapping[ledIdx] % 8);
    ledIdx++;
    if (ledIdx >= NUM_LEDS) {
        ledIdx = 0;
    }

    // for (int i = 0; i < NUM_LED_BYTES; i++) {
    //     shiftOut(PIN_LED_DOUT, PIN_LED_CLK, MSBFIRST, ledData[i]);
    // }
    // digitalWrite(PIN_LED_LATCH, HIGH);
    // digitalWrite(PIN_LED_LATCH, LOW);
    
    memset(ledData, 0, sizeof(ledData));
    updateLeds();

    digitalWrite(PIN_DATA_LATCH, LOW);
    digitalWrite(PIN_DATA_LATCH, HIGH);
    for (int i = 0; i < NUM_PORTS; i++) {
        // portData[i] = shiftIn(PIN_DATA_DIN, PIN_DATA_CLK, MSBFIRST);
        // Serial.print(portData[i], 2);
        // Serial.print(" ");

        portData[i] = 0;
        for (int bit = 0; bit < 8; bit++) {
            portData[i] |= digitalRead(PIN_DATA_DIN) << bit;
            digitalWrite(PIN_DATA_CLK, LOW);
            digitalWrite(PIN_DATA_CLK, HIGH);
        }

        if (portData[i] != 0xFF) {
            setLed(NUM_PORTS - i - 1, ledIdx % 2, !(ledIdx % 2));
            setLed(NUM_PORTS - i - 1, ledIdx % 2, (ledIdx % 2));
        }

        char buf[64];
        int len = snprintf(buf, sizeof(buf), "%02X ", portData[i]);
        Serial.write(buf, len);
    }
    Serial.println();


    delay(100);
}
