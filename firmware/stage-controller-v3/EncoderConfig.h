#ifndef ENCODER_CONFIG_H
#define ENCODER_CONFIG_H

#include <Arduino.h>

namespace EncoderConfig {
constexpr uint8_t TopChannel = 2;
constexpr uint8_t InsertChannel = 1;
constexpr uint8_t BottomChannel = 0;
// Internal positive encoder counts MUST match DIR HIGH (the existing CW command).
// Public CCW-positive reporting is converted separately; do not invert these flags for it.
// Change the relevant flag if commissioning reports a direction fault.
constexpr bool TopInvert = true;
constexpr bool InsertInvert = true;
constexpr bool BottomInvert = false;
// The TCA9548A address is selected by A0..A2; change this if they are not LOW.
constexpr uint8_t MuxAddress = 0x70;
constexpr uint32_t I2cClockHz = 400000UL;
constexpr uint32_t I2cTimeoutUs = 1000UL;
constexpr uint32_t SampleIntervalMs = 4; // One axis per tick; 12 ms per axis.
constexpr uint32_t MaxGapMs = 60; // At <=5 shaft rev/s this is < half a turn.
constexpr long CountsPerRotation = 4096;
// Keep every subtraction of two count positions inside a signed 32-bit long.
constexpr long CountLimit = 1000000000L;
constexpr float MaxShaftSpeed = 5.0f;
constexpr bool AllowReverseCorrection = true;
constexpr uint32_t SettleMs = 150;
constexpr float PositionGain = 6.0f;
constexpr float MaximumFollowingError = 0.25f; // Shaft revolutions, held motion.
constexpr float EncoderNoiseCounts = 2.0f;
constexpr unsigned long DirectionPauseStableMs = 100;
constexpr unsigned long DirectionPauseTimeoutMs = 500;
constexpr unsigned long DirectionWarningWindowMs = 10000;
constexpr float CalibrationMaxSpeed = 0.25f; // Shaft rev/s; user's configured speed may be lower.
constexpr unsigned long CalibrationSearchTimeoutMs = 120000UL; // Per physical-switch search.
static_assert(TopChannel < 8 && InsertChannel < 8 && BottomChannel < 8,
              "TCA9548A channels must be 0..7.");
static_assert(TopChannel != InsertChannel && TopChannel != BottomChannel &&
              InsertChannel != BottomChannel, "Encoders require separate mux channels.");
static_assert(SampleIntervalMs * 3 < MaxGapMs, "Encoder sample interval is too long.");
static_assert(MuxAddress >= 0x70 && MuxAddress <= 0x77, "Invalid TCA9548A address.");
}
#endif
