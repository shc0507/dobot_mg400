#include "Encoders.h"
#include "EncoderConfig.h"
#include "MotorPins.h"
#include <Wire.h>
#include <limits.h>

// A stuck I2C bus must not hang stepping, stop, or the hold watchdog.
// Call setWireTimeout unconditionally below: AVR 1.8.8 supports it without
// defining WIRE_HAS_TIMEOUT. A core without this method must fail compilation.

namespace Encoders {
namespace {
struct Axis {
  uint8_t channel;
  bool invert;
  bool baseline;
  bool trusted;
  bool online;
  bool magnet;
  uint16_t raw;
  long counts;
  unsigned long sampledAt;
  float velocity;
  uint8_t magnetStatus;
};
Axis axes[] = {
  {EncoderConfig::BottomChannel, EncoderConfig::BottomInvert},
  {EncoderConfig::TopChannel, EncoderConfig::TopInvert},
  {EncoderConfig::InsertChannel, EncoderConfig::InsertInvert}
};
uint8_t nextAxis = 0;
unsigned long lastSample = 0;

#if defined(PIN_WIRE_SDA) && defined(PIN_WIRE_SCL)
constexpr bool freeOfI2c(uint8_t p) { return p != PIN_WIRE_SDA && p != PIN_WIRE_SCL; }
static_assert(freeOfI2c(MotorPins::Bottom.step) && freeOfI2c(MotorPins::Bottom.dir) &&
  freeOfI2c(MotorPins::Top.step) && freeOfI2c(MotorPins::Top.dir) &&
  freeOfI2c(MotorPins::Insert.step) && freeOfI2c(MotorPins::Insert.dir) &&
  freeOfI2c(MotorPins::MS1) && freeOfI2c(MotorPins::MS2) && freeOfI2c(MotorPins::MS3) &&
  freeOfI2c(MotorPins::BottomSleep) && freeOfI2c(MotorPins::TopSleep) &&
  freeOfI2c(MotorPins::InsertSleep), "Motor pins overlap SDA/SCL.");
#endif

bool readRegisters(uint8_t reg, uint8_t *data, uint8_t length) {
  Wire.beginTransmission(0x36);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(static_cast<uint8_t>(0x36), length) != length) return false;
  for (uint8_t i = 0; i < length; ++i) {
    const int value = Wire.read();
    if (value < 0) return false;
    data[i] = static_cast<uint8_t>(value);
  }
  return true;
}

void sample(Axis &axis) {
  Wire.beginTransmission(EncoderConfig::MuxAddress);
  Wire.write(static_cast<uint8_t>(1U << axis.channel));
  bool ok = Wire.endTransmission() == 0;
  uint8_t status, angle[2];
  axis.magnetStatus = 255;
  // Match the reference firmware: address STATUS and RAW ANGLE independently.
  // Keep the two angle bytes together; never rely on a cross-register burst
  // starting at STATUS to provide an angle. No configuration/OTP writes.
  if (ok) ok = readRegisters(0x0B, &status, 1);
  if (ok) {
    axis.magnetStatus = status;
    ok = readRegisters(0x0C, angle, 2);
  }
  const unsigned long now = millis();
  if (!ok) {
    axis.online = false;
    axis.trusted = false;
    axis.baseline = false;
    axis.velocity = 0;
    return;
  }
  const uint16_t raw = (static_cast<uint16_t>(angle[0] & 0x0F) << 8) | angle[1];
  axis.online = true;
  axis.magnet = (status & 0x38) == 0x20; // Detected, neither too weak nor too strong.
  if (!axis.magnet) {
    axis.trusted = false;
    axis.baseline = false;
    axis.velocity = 0;
  } else if (axis.baseline) {
    const unsigned long elapsed = now - axis.sampledAt;
    if (elapsed > EncoderConfig::MaxGapMs) {
      axis.trusted = false;
      axis.velocity = 0;
    } else {
      long delta = static_cast<long>(raw) - axis.raw;
      if (delta > 2048) delta -= 4096;
      else if (delta < -2048) delta += 4096;
      // Exactly half a turn has ambiguous direction and cannot be unwrapped.
      if (delta == 2048 || delta == -2048) axis.trusted = false;
      if (axis.invert) delta = -delta;
      if ((delta > 0 && axis.counts > EncoderConfig::CountLimit - delta) ||
          (delta < 0 && axis.counts < -EncoderConfig::CountLimit - delta)) {
        axis.trusted = false;
      } else if (axis.trusted) {
        axis.counts += delta;
        if (elapsed) {
          const float instantaneous = delta * (1000.0f / 4096.0f) / elapsed;
          axis.velocity += 0.25f * (instantaneous - axis.velocity);
        }
      }
    }
  }
  axis.raw = raw;
  axis.sampledAt = now;
  axis.baseline = axis.magnet;
}
}

void begin() {
  Wire.begin();
  Wire.setClock(EncoderConfig::I2cClockHz);
  Wire.setWireTimeout(EncoderConfig::I2cTimeoutUs, true);
  for (uint8_t i = 0; i < 3; ++i) {
    Axis &axis = axes[i];
    axis.baseline = false;
    axis.trusted = true;
    axis.online = false;
    axis.magnet = false;
    axis.counts = 0;
    axis.velocity = 0;
    axis.raw = 0;
    axis.magnetStatus = 255;
    axis.sampledAt = millis();
    sample(axis);
  }
  nextAxis = 0;
  lastSample = millis();
}

void update() {
  if (millis() - lastSample < EncoderConfig::SampleIntervalMs) return;
  lastSample = millis();
  sample(axes[nextAxis]);
  nextAxis = (nextAxis + 1) % 3;
}

Reading get(uint8_t index) {
  Axis &axis = axes[index];
  Health health = Health::Ok;
  if (!axis.online) health = Health::Offline;
  else if (!axis.magnet) health = Health::Magnet;
  else if (!axis.trusted || millis() - axis.sampledAt > EncoderConfig::MaxGapMs)
    health = Health::Untrusted;
  return {axis.counts, axis.sampledAt, axis.velocity, health,
          axis.channel, axis.raw, axis.magnetStatus};
}

bool refresh(uint8_t index) {
  if (index >= 3) return false;
  sample(axes[index]);
  return get(index).health == Health::Ok;
}

bool zeroFromSample(uint8_t index, long sampledCounts) {
  if (index >= 3 || get(index).health != Health::Ok || axes[index].counts != sampledCounts)
    return false;
  axes[index].counts = 0;
  axes[index].velocity = 0;
  return true;
}

bool resetPosition(uint8_t index) {
  return refresh(index) && zeroFromSample(index, axes[index].counts);
}

bool recover(uint8_t index, bool allowRebase) {
  Axis &axis = axes[index];
  // Take a new hardware sample before accepting a clear; never trust cached health.
  sample(axis);
  if (!axis.online || !axis.magnet) return false;
  if (!axis.trusted && !allowRebase) return false;
  if (!axis.trusted) axis.counts = 0;
  axis.trusted = true;
  axis.baseline = true;
  axis.velocity = 0;
  return true;
}

const __FlashStringHelper *healthName(Health health) {
  switch (health) {
    case Health::Ok: return F("ok");
    case Health::Offline: return F("offline");
    case Health::Magnet: return F("magnet");
    default: return F("untrusted");
  }
}
}
