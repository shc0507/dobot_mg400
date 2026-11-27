/*
  pressure-sensor.ino
  Adafruit MPRLS ported pressure sensor (0-25 PSI absolute) on an Arduino Uno.

  Wiring (Uno -> MPRLS breakout):
    5V -> VIN, GND -> GND, A5 -> SCL, A4 -> SDA.
    3Vo stays unconnected. RST (D3) and EOC (D2) are optional, see the pin
    defines below.

  Library: "Adafruit MPRLS Library" with "Adafruit BusIO".

  Serial: 115200 baud, newline-terminated commands. README.md lists the
  protocol. Sample lines use the Serial Plotter "label:value" format, so the
  same output works for the host and for the Arduino IDE plotter.
*/

#include <Wire.h>
#include <Adafruit_MPRLS.h>

#define MPRLS_RESET_PIN -1  // set to 3 if RST is wired to D3
#define MPRLS_EOC_PIN -1    // set to 2 if EOC is wired to D2

const char VERSION_LINE[] PROGMEM = "VERSION pressure-sensor 1.0.0";
const unsigned long SAMPLE_INTERVAL_MS = 200;  // 5 Hz
const unsigned long RETRY_INTERVAL_MS = 1000;
const uint8_t TARE_SAMPLES = 20;
const uint8_t LINE_MAX = 15;

// Plausible absolute pressure at rest: about 3000 m altitude up to a deep low.
const float REST_MIN_HPA = 700.0;
const float REST_MAX_HPA = 1100.0;

Adafruit_MPRLS mpr(MPRLS_RESET_PIN, MPRLS_EOC_PIN);

bool sensorReady = false;
float zeroHpa = NAN;
unsigned long lastSampleMs = 0;
unsigned long lastRetryMs = 0;
unsigned long readCount = 0;
unsigned long errorCount = 0;
char line[LINE_MAX + 1];
uint8_t lineLen = 0;
bool lineOverflow = false;

bool i2cDevicePresent(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

// Opens the sensor. Prints one ERR line on failure.
bool connectSensor() {
  if (!i2cDevicePresent(MPRLS_DEFAULT_ADDR)) {
    Serial.println(F("ERR NO_ACK 0x18"));
    return false;
  }
  if (!mpr.begin()) {
    Serial.println(F("ERR BEGIN 0x18"));
    return false;
  }
  return true;
}

void printZero() {
  if (isnan(zeroHpa)) {
    Serial.println(F("ERR TARE"));
    return;
  }
  Serial.print(F("ZERO "));
  Serial.println(zeroHpa, 2);
  if (zeroHpa < REST_MIN_HPA || zeroHpa > REST_MAX_HPA) {
    Serial.println(F("WARN ZERO_OUT_OF_RANGE"));
  }
}

// Averages TARE_SAMPLES readings as the gauge zero. Keep the port open to
// room air. Leaves the old zero when no reading is valid.
void tare() {
  float sum = 0;
  uint8_t good = 0;
  for (uint8_t i = 0; i < TARE_SAMPLES; i++) {
    float p = mpr.readPressure();
    if (!isnan(p)) {
      sum += p;
      good++;
    }
    delay(20);
  }
  if (good > 0) zeroHpa = sum / good;
  printZero();
}

void printReadError() {
  uint8_t st = mpr.readStatus();
  Serial.print(F("ERR READ 0x"));
  if (st < 0x10) Serial.print('0');
  Serial.print(st, HEX);
  if (!(st & MPRLS_STATUS_POWERED)) Serial.print(F(" NOT_POWERED"));
  if (st & MPRLS_STATUS_FAILED) Serial.print(F(" INTEGRITY"));
  if (st & MPRLS_STATUS_MATHSAT) Serial.print(F(" MATHSAT"));
  Serial.println();
}

void printStatus() {
  Serial.print(F("STATUS ready="));
  Serial.print(sensorReady ? 1 : 0);
  Serial.print(F(" zero_hPa="));
  if (isnan(zeroHpa)) {
    Serial.print(F("nan"));
  } else {
    Serial.print(zeroHpa, 2);
  }
  Serial.print(F(" reads="));
  Serial.print(readCount);
  Serial.print(F(" errors="));
  Serial.println(errorCount);
}

void runCommand() {
  for (uint8_t i = 0; i < lineLen; i++) line[i] = toupper(line[i]);
  if (strcmp_P(line, PSTR("TARE")) == 0 || strcmp_P(line, PSTR("T")) == 0) {
    if (sensorReady) {
      tare();
    } else {
      Serial.println(F("ERR NOT_READY"));
    }
  } else if (strcmp_P(line, PSTR("STATUS")) == 0) {
    printStatus();
  } else if (strcmp_P(line, PSTR("VERSION")) == 0) {
    Serial.println((const __FlashStringHelper *)VERSION_LINE);
  } else {
    Serial.println(F("ERR UNKNOWN"));
  }
}

void handleSerial() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      line[lineLen] = '\0';
      if (lineOverflow) {
        Serial.println(F("ERR LINE_TOO_LONG"));
      } else if (lineLen > 0) {
        runCommand();
      }
      lineLen = 0;
      lineOverflow = false;
    } else if (lineLen < LINE_MAX) {
      line[lineLen++] = c;
    } else {
      lineOverflow = true;
    }
  }
}

void sample() {
  float hpa = mpr.readPressure();
  readCount++;
  if (isnan(hpa)) {
    errorCount++;
    printReadError();
    return;
  }
  Serial.print(F("abs_hPa:"));
  Serial.print(hpa, 2);
  Serial.print(F(" gauge_hPa:"));
  Serial.println(isnan(zeroHpa) ? 0.0 : hpa - zeroHpa, 2);
}

void setup() {
  Serial.begin(115200);
  Wire.begin();
  Serial.println((const __FlashStringHelper *)VERSION_LINE);
  lastRetryMs = millis() - RETRY_INTERVAL_MS;  // try at once
}

void loop() {
  handleSerial();
  unsigned long now = millis();

  // Retry until the sensor answers, so wiring can be fixed without a reset.
  if (!sensorReady) {
    if (now - lastRetryMs < RETRY_INTERVAL_MS) return;
    lastRetryMs = now;
    if (!connectSensor()) return;
    sensorReady = true;
    Serial.println(F("PRESSURE_READY MPRLS 0x18"));
    tare();
    lastSampleMs = millis();
    return;
  }

  if (now - lastSampleMs < SAMPLE_INTERVAL_MS) return;
  lastSampleMs = now;
  sample();
}
