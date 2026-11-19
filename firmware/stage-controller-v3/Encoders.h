#ifndef ENCODERS_H
#define ENCODERS_H
#include <Arduino.h>
namespace Encoders {
enum class Health : uint8_t { Ok, Offline, Magnet, Untrusted };
struct Reading {
  long counts;
  unsigned long sampledAt;
  float velocity;
  Health health;
  uint8_t channel;
  uint16_t raw;
  uint8_t magnetStatus; // AS5600 STATUS bits; 255 means no successful status read.
};
void begin();
void update();
Reading get(uint8_t axis);
bool refresh(uint8_t axis); // Synchronous fresh read; never restores lost trust.
// Commit zero for an already-fresh, validated count sample without another I2C read.
// Preserves the raw-angle baseline/trust; rejects a changed sample or unusable feedback.
bool zeroFromSample(uint8_t axis, long sampledCounts);
bool resetPosition(uint8_t axis); // Fresh healthy/trusted encoder required; no motion.
// Explicit recovery; resets counts only when continuity was lost.
bool recover(uint8_t axis, bool allowRebase = true);
const __FlashStringHelper *healthName(Health health);
}
#endif
