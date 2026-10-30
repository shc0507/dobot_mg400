#ifndef NATIVE_ARDUINO_H
#define NATIVE_ARDUINO_H

// Host-only GPIO/clock/UART seam. The test links the actual AccelStepper source.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <iomanip>
#include <sstream>
#include <string>

using boolean = bool;
using byte = uint8_t;
using std::max;
using std::min;
constexpr uint8_t LOW = 0, HIGH = 1, OUTPUT = 1, INPUT_PULLUP = 2;
constexpr uint8_t A0 = 14, A1 = 15, A2 = 16, A3 = 17, A4 = 18, A5 = 19;
#define NUM_DIGITAL_PINS 20
struct __FlashStringHelper {};
#define F(text) reinterpret_cast<const __FlashStringHelper *>(text)
template <typename T> T constrain(T value, T low, T high) {
  return max(low, min(value, high));
}
unsigned long millis();
unsigned long micros();
void delayMicroseconds(unsigned int delay);
inline void yield() {}
void pinMode(uint8_t pin, uint8_t mode);
void digitalWrite(uint8_t pin, uint8_t level);
int digitalRead(uint8_t pin);

class Print {
public:
  virtual ~Print() {}
  virtual size_t write(uint8_t value) = 0;
  size_t write(const uint8_t *data, size_t length) {
    for (size_t i = 0; i < length; ++i) write(data[i]);
    return length;
  }
  void print(const char *value) {
    while (*value) write(static_cast<uint8_t>(*value++));
  }
  void print(const __FlashStringHelper *value) { print(reinterpret_cast<const char *>(value)); }
  void print(uint8_t value) { print(static_cast<unsigned int>(value)); }
  template <typename T> void print(T value) {
    std::ostringstream text;
    text << value;
    print(text.str().c_str());
  }
  void print(float value, int precision) {
    std::ostringstream text;
    text << std::fixed << std::setprecision(precision) << value;
    print(text.str().c_str());
  }
  template <typename T> void println(T value) { print(value); println(); }
  void println() { print("\r\n"); }
};

class FakeSerial : public Print {
public:
  std::string input, output;
  void (*onWrite)() = nullptr;
  void begin(unsigned long) {}
  int available() { return static_cast<int>(input.size()); }
  int availableForWrite() { return 64; }
  int read() {
    const char value = input.front();
    input.erase(0, 1);
    return value;
  }
  size_t write(uint8_t value) override {
    output += static_cast<char>(value);
    if (onWrite) onWrite();
    return 1;
  }
};
extern FakeSerial Serial;
#endif
