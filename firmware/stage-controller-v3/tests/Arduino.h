#ifndef TEST_ARDUINO_H
#define TEST_ARDUINO_H
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <sstream>
#include <string>
using boolean = bool;
using byte = uint8_t;
using std::max;
using std::min;
constexpr uint8_t LOW=0, HIGH=1, OUTPUT=1, INPUT_PULLUP=2;
constexpr uint8_t A0=14, A1=15, A2=16, A4=18;
#define NUM_DIGITAL_PINS 20
#define PIN_WIRE_SDA 18
#define PIN_WIRE_SCL 19
struct __FlashStringHelper {};
#define F(text) reinterpret_cast<const __FlashStringHelper *>(text)
template <typename T> T constrain(T value, T low, T high) { return max(low, min(value, high)); }
unsigned long millis();
unsigned long micros();
void delayMicroseconds(unsigned int delay);
inline void yield() {}
void pinMode(uint8_t pin, uint8_t mode);
void digitalWrite(uint8_t pin, uint8_t level);
int digitalRead(uint8_t pin);
int analogRead(uint8_t pin);
class Print {
public:
  virtual ~Print() {}
  virtual size_t write(uint8_t value) = 0;
  size_t write(const uint8_t *data, size_t length) {
    for (size_t i=0; i<length; ++i) write(data[i]);
    return length;
  }
  void print(const char *value) { while (*value) write(static_cast<uint8_t>(*value++)); }
  void print(const __FlashStringHelper *value) { print(reinterpret_cast<const char *>(value)); }
  void print(uint8_t value) { print(static_cast<unsigned int>(value)); }
  template <typename T> void print(T value) {
    std::ostringstream stream; stream << value; print(stream.str().c_str());
  }
  void print(float value, int precision) {
    std::ostringstream stream; stream << std::fixed << std::setprecision(precision) << value;
    print(stream.str().c_str());
  }
  template <typename T> void println(T value) { print(value); println(); }
  void println(float value, int precision) { print(value, precision); println(); }
  void println() { print("\r\n"); }
};
class FakeSerial : public Print {
public:
  std::string input, output;
  int (*roomProvider)() = nullptr;
  void (*byteObserver)(uint8_t) = nullptr;
  void begin(unsigned long) {}
  int available() { return static_cast<int>(input.size()); }
  int availableForWrite() { return roomProvider ? roomProvider() : 64; }
  int read() { char c=input.front(); input.erase(0,1); return c; }
  size_t write(uint8_t value) override {
    output += static_cast<char>(value);
    if (byteObserver) byteObserver(value);
    return 1;
  }
};
extern FakeSerial Serial;
#endif
