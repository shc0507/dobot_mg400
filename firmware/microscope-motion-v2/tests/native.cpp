#include <Arduino.h>
#include <cassert>
#include <iostream>
#include <vector>
#include <string>
FakeSerial Serial;
uint64_t clockUs = 0, nextTickUs = 50;
bool switchActive = false;
bool uartDelay = false;
uint8_t pinLevels[20] = {};
std::vector<uint64_t> edges[20];
void advanceTime(uint64_t delta, bool foreground);
unsigned long micros() { return uint32_t(clockUs); }
unsigned long millis() { return uint32_t(clockUs / 1000); }
void delayMicroseconds(unsigned int time) { advanceTime(time, false); }
void onSerialPrint() { if (uartDelay) advanceTime(200, false); }
void pinMode(uint8_t pin, uint8_t) { assert(pin < 20); }
void digitalWrite(uint8_t pin, uint8_t level) {
  assert(pin < 20);
  if ((pin == 9 || pin == 12) && level == HIGH && pinLevels[pin] == LOW)
    edges[pin].push_back(clockUs);
  pinLevels[pin] = level;
}
int digitalRead(uint8_t pin) { assert(pin < 20); return switchActive ? LOW : HIGH; }
#include "../microscope-motion-v2.ino"
void advanceTime(uint64_t delta, bool foreground) {
  const auto stop = clockUs + delta;
  while (nextTickUs <= stop) {
    clockUs = nextTickUs;
    nextTickUs += TIMER_TICK_US;
    motionTimerTick();
    if (foreground) loop();
  }
  clockUs = stop;
  if (foreground) loop();
}
int main() {
  setup();
  std::cout << Serial.output;
  Serial.output.clear();
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.rfind("@advance ", 0) == 0) advanceTime(std::stoull(line.substr(9)), true);
    else if (line.rfind("@blocked ", 0) == 0) { advanceTime(std::stoull(line.substr(9)), false); loop(); }
    else if (line.rfind("@switch ", 0) == 0) switchActive = line.substr(8) == "1";
    else if (line.rfind("@uart ", 0) == 0) uartDelay = line.substr(6) == "1";
    else if (line.rfind("@position ", 0) == 0) focus.pulsePosition = std::stol(line.substr(10));
    else if (line == "@edges")
      std::cout << "EDGES OBJ=" << edges[9].size() << " FOCUS=" << edges[12].size() << '\n';
    else if (line == "@pins")
      std::cout << "PINS OBJ_EN=" << int(pinLevels[10]) << " OBJ_SLEEP=" << int(pinLevels[6])
                << " FOCUS_EN=" << int(pinLevels[13]) << " FOCUS_SLEEP=" << int(pinLevels[7]) << '\n';
    else if (line == "@time") std::cout << "TIME " << clockUs << '\n';
    else if (line.rfind("@intervals ", 0) == 0) {
      const auto &times = edges[line.substr(11) == "OBJ" ? 9 : 12];
      std::cout << "INTERVALS";
      for (size_t i = 1; i < times.size(); ++i) std::cout << ' ' << times[i] - times[i-1];
      std::cout << '\n';
    } else if (line.rfind("@hex ", 0) == 0) {
      const auto hex = line.substr(5);
      for (size_t i=0; i<hex.size(); i+=2)
        Serial.input += static_cast<char>(std::stoul(hex.substr(i, 2), nullptr, 16));
      while (Serial.available()) loop();
    } else {
      Serial.input += line + "\n";
      while (Serial.available()) loop();
    }
    std::cout << Serial.output;
    Serial.output.clear();
  }
}
