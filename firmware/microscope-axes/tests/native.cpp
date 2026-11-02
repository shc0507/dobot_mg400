#include <Arduino.h>
#include <cassert>
#include <iostream>
#include <string>
FakeSerial Serial;
unsigned long clockUs = 0;
bool switchActive = false;
unsigned long stepEdges[20] = {};
unsigned long micros() { return clockUs; }
unsigned long millis() { return clockUs / 1000; }
void delayMicroseconds(unsigned int) {}
void pinMode(uint8_t pin, uint8_t) { assert(pin < 20); }
void digitalWrite(uint8_t pin, uint8_t level) {
  assert(pin < 20);
  if ((pin == 9 || pin == 12) && level == HIGH) ++stepEdges[pin];
}
int digitalRead(uint8_t pin) { assert(pin < 20); return switchActive ? LOW : HIGH; }
#include "../microscope-axes.ino"

int main() {
  setup();
  std::cout << Serial.output;
  Serial.output.clear();
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.rfind("@advance ", 0) == 0) {
      const auto stop = clockUs + std::stoul(line.substr(9));
      while (clockUs < stop) {
        clockUs += std::min(100UL, stop - clockUs);
        loop();
      }
    } else if (line.rfind("@switch ", 0) == 0) {
      switchActive = line.substr(8) == "1";
    } else if (line.rfind("@position ", 0) == 0) {
      focus.pulsePosition = std::stol(line.substr(10));
    } else if (line == "@edges") {
      std::cout << "EDGES OBJ=" << stepEdges[9] << " FOCUS=" << stepEdges[12] << '\n';
    } else if (line.rfind("@hex ", 0) == 0) {
      const std::string hex = line.substr(5);
      for (size_t i = 0; i < hex.size(); i += 2) {
        Serial.input += static_cast<char>(std::stoul(hex.substr(i, 2), nullptr, 16));
      }
      while (Serial.available()) loop();
    } else {
      Serial.input += line + "\n";
      while (Serial.available()) loop();
    }
    std::cout << Serial.output;
    Serial.output.clear();
  }
}
