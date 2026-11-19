#ifndef TEST_WIRE_H
#define TEST_WIRE_H
#include <Arduino.h>
#define WIRE_HAS_TIMEOUT
class FakeWire {
public:
  void begin() {}
  void setClock(uint32_t) {}
  void setWireTimeout(uint32_t, bool) {}
  void beginTransmission(uint8_t address);
  void write(uint8_t value);
  uint8_t endTransmission(bool stop=true);
  uint8_t requestFrom(uint8_t address, uint8_t length);
  int read();
};
extern FakeWire Wire;
#endif
