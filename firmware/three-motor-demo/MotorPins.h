#ifndef MOTOR_PINS_H
#define MOTOR_PINS_H

#include <Arduino.h>

// Edit these pairs to match the wiring, then re-upload three-motor-demo.ino.
// Each pair is {STEP, DIR}, using Arduino pin numbers (not chip/package pins).
// Motor names in the serial commands and web app use these same assignments.
namespace MotorPins {

struct PinPair {
  uint8_t step;
  uint8_t dir;
};

constexpr PinPair Bottom = {5, 4};
constexpr PinPair Top = {7, 6};
constexpr PinPair Insert = {3, 2};

// All three A4988 boards share these resolution inputs.
constexpr uint8_t MS1 = 10;
constexpr uint8_t MS2 = 9;
constexpr uint8_t MS3 = 8;

// Individual A4988 SLEEP inputs: LOW sleeps, HIGH wakes.
constexpr uint8_t BottomSleep = 12;
constexpr uint8_t TopSleep = 13;
constexpr uint8_t InsertSleep = 11;

// Active LOW limit contacts to GND, with the Uno's internal pull-ups.
// 255 disables an input. A minimum input is required for homing.
constexpr uint8_t InsertLimitMin = A0;
constexpr uint8_t InsertLimitMax = A1;
constexpr uint8_t BottomLimitMin = A2;
constexpr uint8_t BottomLimitMax = A3;
constexpr uint8_t TopLimitMin = A4;
constexpr uint8_t TopLimitMax = A5;
constexpr unsigned long LimitDebounceMs = 5;
constexpr float HomeSpeed = 100.0f;
constexpr float HomeSlowSpeed = 25.0f;
constexpr unsigned long HomeBackoffSteps = 200;
constexpr unsigned long HomeStepLimit = 10000;
constexpr unsigned long HomeTimeoutMs = 120000;
static_assert(HomeSpeed >= 1 && HomeSpeed <= 1000 &&
                  HomeSlowSpeed >= 1 && HomeSlowSpeed < HomeSpeed,
              "Homing speeds must be within 1..1000, with a slower final approach.");
static_assert(HomeStepLimit > HomeBackoffSteps && HomeTimeoutMs > 0,
              "Homing must have finite positive travel and time bounds.");

// Editable idle timeout in milliseconds: 10000UL = 10 seconds.
// Each motor has its own timer, starting when that motor stops moving.
constexpr unsigned long SleepTimeoutMs = 10000UL;
static_assert(SleepTimeoutMs > 0, "Sleep timeout must be greater than zero.");

// All six signals must use different pins. Reserve D0/D1 for serial commands.
constexpr bool validPair(PinPair pins) {
  return pins.step >= 2 && pins.dir >= 2 && pins.step != pins.dir;
}

constexpr bool separatePairs(PinPair first, PinPair second) {
  return first.step != second.step && first.step != second.dir &&
         first.dir != second.step && first.dir != second.dir;
}

static_assert(validPair(Bottom) && validPair(Top) && validPair(Insert),
              "Motor pins: STEP and DIR must differ; D0/D1 are reserved for serial.");
static_assert(separatePairs(Bottom, Top) && separatePairs(Bottom, Insert) &&
                  separatePairs(Top, Insert),
              "Motor pins: all six STEP/DIR pins must be unique.");

constexpr bool separateMicrostepPins(PinPair pins) {
  return pins.step != MS1 && pins.step != MS2 && pins.step != MS3 &&
         pins.dir != MS1 && pins.dir != MS2 && pins.dir != MS3;
}

static_assert(MS1 >= 2 && MS2 >= 2 && MS3 >= 2 &&
                  MS1 != MS2 && MS1 != MS3 && MS2 != MS3,
              "Microstep pins must be distinct; reserve D0/D1 for serial.");
static_assert(separateMicrostepPins(Bottom) && separateMicrostepPins(Top) &&
                  separateMicrostepPins(Insert),
              "Microstep pins must not overlap STEP/DIR pins.");

constexpr bool validSleepPin(uint8_t pin) {
  return pin >= 2 && pin != Bottom.step && pin != Bottom.dir &&
         pin != Top.step && pin != Top.dir &&
         pin != Insert.step && pin != Insert.dir &&
         pin != MS1 && pin != MS2 && pin != MS3;
}

static_assert(validSleepPin(BottomSleep) && validSleepPin(TopSleep) &&
                  validSleepPin(InsertSleep) && BottomSleep != TopSleep &&
                  BottomSleep != InsertSleep && TopSleep != InsertSleep,
              "Sleep pins must be distinct and not overlap motor, microstep, or serial pins.");

constexpr bool validLimitPin(uint8_t pin) {
  return pin == 255 || (pin < 20 && validSleepPin(pin) &&
      pin != BottomSleep && pin != TopSleep && pin != InsertSleep);
}

constexpr bool separateLimits(uint8_t first, uint8_t second) {
  return first == 255 || second == 255 || first != second;
}

constexpr bool limitDistinct(uint8_t pin, uint8_t a, uint8_t b,
                             uint8_t c, uint8_t d, uint8_t e) {
  return separateLimits(pin, a) && separateLimits(pin, b) &&
      separateLimits(pin, c) && separateLimits(pin, d) && separateLimits(pin, e);
}

static_assert(validLimitPin(InsertLimitMin) && validLimitPin(InsertLimitMax) &&
                  validLimitPin(BottomLimitMin) && validLimitPin(BottomLimitMax) &&
                  validLimitPin(TopLimitMin) && validLimitPin(TopLimitMax),
              "Limit pins must be Uno I/O pins D2..D19 or 255, without output conflicts.");
static_assert(limitDistinct(InsertLimitMin, InsertLimitMax, BottomLimitMin, BottomLimitMax,
                            TopLimitMin, TopLimitMax) &&
                  limitDistinct(InsertLimitMax, BottomLimitMin, BottomLimitMax,
                                TopLimitMin, TopLimitMax, 255) &&
                  limitDistinct(BottomLimitMin, BottomLimitMax, TopLimitMin, TopLimitMax, 255, 255) &&
                  limitDistinct(BottomLimitMax, TopLimitMin, TopLimitMax, 255, 255, 255) &&
                  separateLimits(TopLimitMin, TopLimitMax),
              "Enabled limit pins must be distinct.");

#if defined(NUM_DIGITAL_PINS)
constexpr bool withinBoard(PinPair pins) {
  return pins.step < NUM_DIGITAL_PINS && pins.dir < NUM_DIGITAL_PINS;
}

static_assert(withinBoard(Bottom) && withinBoard(Top) && withinBoard(Insert),
              "Motor pins: pin number is outside the selected board's digital pin range.");
static_assert(MS1 < NUM_DIGITAL_PINS && MS2 < NUM_DIGITAL_PINS && MS3 < NUM_DIGITAL_PINS,
              "Microstep pin is outside the selected board's digital pin range.");
static_assert(BottomSleep < NUM_DIGITAL_PINS && TopSleep < NUM_DIGITAL_PINS &&
                  InsertSleep < NUM_DIGITAL_PINS,
              "Sleep pin is outside the selected board's digital pin range.");
#endif

} // namespace MotorPins

#endif
