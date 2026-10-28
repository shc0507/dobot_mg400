#include "MotorControl.h"
#include "MotorPins.h"

#include <AccelStepper.h>
#include <limits.h>
#include <math.h>

namespace MotorControl {
namespace {

// Edit MotorPins.h to change wiring. Both the driver and direct GPIO writes
// use the same pairs. GPIO setup is deferred to begin().
AccelStepper bottom(AccelStepper::DRIVER, MotorPins::Bottom.step, MotorPins::Bottom.dir,
                    0, 0, false);
AccelStepper top(AccelStepper::DRIVER, MotorPins::Top.step, MotorPins::Top.dir,
                 0, 0, false);
AccelStepper insert(AccelStepper::DRIVER, MotorPins::Insert.step, MotorPins::Insert.dir,
                    0, 0, false);

enum class Mode : uint8_t { Idle, Finite, Continuous };
enum class Power : uint8_t { Sleeping, Waking, Awake };

struct MotorState {
  AccelStepper *stepper;
  MotorPins::PinPair pins;
  Direction direction;
  Mode mode;
  float speed;
  uint16_t stepsPerRevolution;
  bool held;
  unsigned long lastHold;
  uint8_t sleepPin;
  Power power;
  unsigned long idleSinceMs;
  unsigned long wakeStartedUs;
};

MotorState motors[] = {
    {&bottom, MotorPins::Bottom, Direction::Clockwise, Mode::Idle, 200.0f, 200, false, 0,
     MotorPins::BottomSleep, Power::Sleeping, 0, 0},
    {&top, MotorPins::Top, Direction::Clockwise, Mode::Idle, 200.0f, 200, false, 0,
     MotorPins::TopSleep, Power::Sleeping, 0, 0},
    {&insert, MotorPins::Insert, Direction::Clockwise, Mode::Idle, 200.0f, 200, false, 0,
     MotorPins::InsertSleep, Power::Sleeping, 0, 0}};
const unsigned long holdTimeoutMs = 1000;
// A4988 requires at least 1 ms; 2 ms includes margin for micros() resolution.
const unsigned long driverWakeDelayUs = 2000;
const uint8_t motorCount = sizeof(motors) / sizeof(motors[0]);
Motor selected = Motor::Bottom_Motor;
bool initialized = false;
uint8_t microstepDivisor = 1;

bool validMotor(Motor motor) {
  return static_cast<uint8_t>(motor) < motorCount;
}

MotorState &selectedState() {
  return motors[static_cast<uint8_t>(selected)];
}

bool canConfigure() {
  return initialized && selectedState().mode == Mode::Idle;
}

void wakeForMotion(MotorState &motor) {
  if (motor.power != Power::Sleeping) return;
  digitalWrite(motor.sleepPin, HIGH);
  motor.wakeStartedUs = micros();
  motor.power = Power::Waking;
}

void halt(MotorState &motor) {
  // setCurrentPosition also clears speed and target, preventing an old move
  // from resuming when switching between continuous and finite rotation.
  motor.stepper->setCurrentPosition(motor.stepper->currentPosition());
  // Repeated stops/status/configuration must not extend an idle timer.
  if (motor.mode != Mode::Idle) motor.idleSinceMs = millis();
  motor.mode = Mode::Idle;
  motor.held = false;
  digitalWrite(motor.pins.step, LOW);
}

} // namespace

void begin() {
  initialized = false;
  // Put every driver to sleep before initializing STEP/DIR or resolution.
  for (uint8_t i = 0; i < motorCount; ++i) {
    MotorState &motor = motors[i];
    digitalWrite(motor.sleepPin, LOW);
    pinMode(motor.sleepPin, OUTPUT);
    motor.power = Power::Sleeping;
    motor.idleSinceMs = millis();
    motor.wakeStartedUs = 0;
  }
  selected = Motor::Bottom_Motor;
  microstepDivisor = 1;
  digitalWrite(MotorPins::MS1, LOW);
  digitalWrite(MotorPins::MS2, LOW);
  digitalWrite(MotorPins::MS3, LOW);
  pinMode(MotorPins::MS1, OUTPUT);
  pinMode(MotorPins::MS2, OUTPUT);
  pinMode(MotorPins::MS3, OUTPUT);
  for (uint8_t i = 0; i < motorCount; ++i) {
    MotorState &motor = motors[i];
    motor.direction = Direction::Clockwise;
    motor.mode = Mode::Idle;
    motor.held = false;
    motor.speed = 200.0f;
    motor.stepsPerRevolution = 200;
    digitalWrite(motor.pins.step, LOW);
    digitalWrite(motor.pins.dir, HIGH);
    motor.stepper->enableOutputs();
    motor.stepper->setCurrentPosition(0);
    motor.stepper->setMaxSpeed(motor.speed);
    motor.stepper->setAcceleration(50.0f);
    motor.stepper->setMinPulseWidth(3); // A4988 requires at least 1 us HIGH.
  }
  initialized = true;
}

void update() {
  if (!initialized) return;
  for (uint8_t i = 0; i < motorCount; ++i) {
    MotorState &motor = motors[i];
    if (motor.held && millis() - motor.lastHold >= holdTimeoutMs) {
      halt(motor);
    }
    if (motor.mode == Mode::Idle) {
      if (motor.power != Power::Sleeping &&
          millis() - motor.idleSinceMs >= MotorPins::SleepTimeoutMs) {
        digitalWrite(motor.sleepPin, LOW);
        motor.power = Power::Sleeping;
      }
      continue;
    }
    if (motor.power == Power::Waking) {
      // Do not block serial handling or stepping on the other two motors.
      if (micros() - motor.wakeStartedUs < driverWakeDelayUs) continue;
      motor.power = Power::Awake;
    }
    if (motor.mode == Mode::Finite) {
      motor.stepper->run();
      if (!motor.stepper->isRunning()) halt(motor);
    } else if (motor.mode == Mode::Continuous) {
      const long position = motor.stepper->currentPosition();
      // Avoid wrapping AccelStepper's signed position during a very long run.
      if ((motor.direction == Direction::Clockwise && position == LONG_MAX) ||
          (motor.direction == Direction::Counterclockwise && position == LONG_MIN)) {
        halt(motor);
      } else {
        motor.stepper->runSpeed();
      }
    }
  }
}

bool selectMotor(Motor motor) {
  if (!validMotor(motor)) return false;
  selected = motor;
  return true;
}

Motor selectedMotor() {
  return selected;
}

bool setMicrostep(uint8_t divisor) {
  if (!initialized) return false;
  uint8_t levels;
  switch (divisor) {
    case 1: levels = 0b000; break;  // MS1 LOW,  MS2 LOW,  MS3 LOW.
    case 2: levels = 0b001; break;  // MS1 HIGH, MS2 LOW,  MS3 LOW.
    case 4: levels = 0b010; break;  // MS1 LOW,  MS2 HIGH, MS3 LOW.
    case 8: levels = 0b011; break;  // MS1 HIGH, MS2 HIGH, MS3 LOW.
    case 16: levels = 0b111; break; // MS1 HIGH, MS2 HIGH, MS3 HIGH.
    default: return false;
  }
  for (uint8_t i = 0; i < motorCount; ++i) {
    if (motors[i].mode != Mode::Idle) return false;
  }
  // STEP is inactive while idle. Set all MS inputs before the next pulse.
  digitalWrite(MotorPins::MS1, (levels & 0b001) ? HIGH : LOW);
  digitalWrite(MotorPins::MS2, (levels & 0b010) ? HIGH : LOW);
  digitalWrite(MotorPins::MS3, (levels & 0b100) ? HIGH : LOW);
  delayMicroseconds(1); // A4988 MS input setup time is at least 200 ns.
  microstepDivisor = divisor;
  return true;
}

uint8_t microstep() {
  return microstepDivisor;
}

bool setDirection(Direction direction) {
  if (!canConfigure() ||
      (direction != Direction::Clockwise && direction != Direction::Counterclockwise)) {
    return false;
  }
  MotorState &motor = selectedState();
  motor.direction = direction;
  digitalWrite(motor.pins.dir, direction == Direction::Clockwise ? HIGH : LOW);
  return true;
}

bool setSpeed(float stepsPerSecond) {
  if (!canConfigure() || !isfinite(stepsPerSecond) ||
      stepsPerSecond < 1.0f || stepsPerSecond > 1000.0f) return false;
  selectedState().speed = stepsPerSecond;
  selectedState().stepper->setMaxSpeed(stepsPerSecond);
  return true;
}

bool setAcceleration(float stepsPerSecondSquared) {
  if (!canConfigure() || !isfinite(stepsPerSecondSquared) ||
      stepsPerSecondSquared < 1.0f || stepsPerSecondSquared > 10000.0f) return false;
  selectedState().stepper->setAcceleration(stepsPerSecondSquared);
  return true;
}

bool setStepsPerRevolution(uint16_t steps) {
  if (!canConfigure() || steps == 0) return false;
  selectedState().stepsPerRevolution = steps;
  return true;
}

bool rotateSteps(long steps) {
  if (!canConfigure() || steps <= 0) return false;
  MotorState &motor = selectedState();
  const long delta = motor.direction == Direction::Clockwise ? steps : -steps;
  const long position = motor.stepper->currentPosition();
  if ((delta > 0 && position > LONG_MAX - delta) ||
      (delta < 0 && position < LONG_MIN - delta)) return false;
  motor.stepper->move(delta);
  motor.mode = Mode::Finite;
  wakeForMotion(motor);
  return true;
}

bool rotateRevolutions(float revolutions) {
  if (!canConfigure() || !isfinite(revolutions) || revolutions <= 0.0f) return false;
  const double steps = floor(static_cast<double>(revolutions) *
                             selectedState().stepsPerRevolution * microstepDivisor + 0.5);
  // Strict upper bound also prevents overflow on AVR, where double is float.
  if (!isfinite(steps) || steps < 1.0 || steps >= static_cast<double>(LONG_MAX)) return false;
  return rotateSteps(static_cast<long>(steps));
}

bool rotateOneRevolution() {
  return rotateRevolutions(1.0f);
}

bool startRotation() {
  if (!canConfigure()) return false;
  MotorState &motor = selectedState();
  motor.stepper->setSpeed(motor.direction == Direction::Clockwise ? motor.speed : -motor.speed);
  motor.mode = Mode::Continuous;
  motor.held = false;
  wakeForMotion(motor);
  return true;
}

bool startHold() {
  if (!startRotation()) return false;
  selectedState().held = true;
  selectedState().lastHold = millis();
  return true;
}

bool keepHoldAlive() {
  if (!initialized) return false;
  MotorState &motor = selectedState();
  if (!motor.held || motor.mode != Mode::Continuous) return false;
  // A late renewal cannot restart an expired hold, even before update() runs.
  if (millis() - motor.lastHold >= holdTimeoutMs) {
    halt(motor);
    return false;
  }
  motor.lastHold = millis();
  return true;
}

void stopRotation() {
  if (initialized) halt(selectedState());
}

void stopAll() {
  if (!initialized) return;
  for (uint8_t i = 0; i < motorCount; ++i) halt(motors[i]);
}

bool isRotating(Motor motor) {
  return initialized && validMotor(motor) &&
         motors[static_cast<uint8_t>(motor)].mode != Mode::Idle;
}

bool getStatus(Motor motor, Status &status) {
  if (!initialized || !validMotor(motor)) return false;
  MotorState &state = motors[static_cast<uint8_t>(motor)];
  status.direction = state.direction;
  status.rotating = state.mode != Mode::Idle;
  status.continuous = state.mode == Mode::Continuous;
  status.position = state.stepper->currentPosition();
  status.remainingSteps = state.mode == Mode::Finite ? state.stepper->distanceToGo() : 0;
  status.speed = state.speed;
  status.acceleration = state.stepper->acceleration();
  status.stepsPerRevolution = state.stepsPerRevolution;
  return true;
}

} // namespace MotorControl
