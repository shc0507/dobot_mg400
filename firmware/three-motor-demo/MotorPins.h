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
