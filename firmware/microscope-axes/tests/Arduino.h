#pragma once
// Offline GPIO, UART, and clock only. No operating-system device access.
#include <cstdint>
#include <sstream>
#include <string>
constexpr uint8_t LOW = 0, HIGH = 1, OUTPUT = 1, INPUT_PULLUP = 2;
#define F(text) text
unsigned long micros();
unsigned long millis();
void delayMicroseconds(unsigned int);
void pinMode(uint8_t, uint8_t);
void digitalWrite(uint8_t, uint8_t);
int digitalRead(uint8_t);
struct FakeSerial {
  std::string input, output;
  void begin(unsigned long) {}
  int available() { return static_cast<int>(input.size()); }
  int read() { char c = input.front(); input.erase(0, 1); return static_cast<uint8_t>(c); }
  template <typename T> void print(T value) {
    std::ostringstream stream;
    stream << value;
    output += stream.str();
  }
  template <typename T> void println(T value) { print(value); output += "\r\n"; }
};
extern FakeSerial Serial;
