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

enum class Mode : uint8_t { Idle, Finite, Continuous, Homing };
enum class Power : uint8_t { Sleeping, Waking, Awake };
enum class HomePhase : uint8_t { Seek, Backoff, Released, Approach };

struct Limit {
  uint8_t pin;
  bool raw;
  bool active;
  unsigned long changedAt;
  bool inhibited;
};

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
  Limit minimum;
  Limit maximum;
  bool homed;
  bool soft;
  long softMin;
  long softMax;
  unsigned long awakeMs;
  unsigned long awakeSince;
  HomePhase homePhase;
  HomeError homeError;
  unsigned long homeStarted;
  unsigned long homePulses;
  unsigned long backoffPulses;
  unsigned long releasedAt;
  bool idleHold;
};

MotorState motors[] = {
    {&bottom, MotorPins::Bottom, Direction::Clockwise, Mode::Idle, 200.0f, 200, false, 0,
     MotorPins::BottomSleep, Power::Sleeping, 0, 0,
     {MotorPins::BottomLimitMin, false, false, 0}, {MotorPins::BottomLimitMax, false, false, 0}},
    {&top, MotorPins::Top, Direction::Clockwise, Mode::Idle, 200.0f, 200, false, 0,
     MotorPins::TopSleep, Power::Sleeping, 0, 0,
     {MotorPins::TopLimitMin, false, false, 0}, {MotorPins::TopLimitMax, false, false, 0}},
    {&insert, MotorPins::Insert, Direction::Clockwise, Mode::Idle, 200.0f, 200, false, 0,
     MotorPins::InsertSleep, Power::Sleeping, 0, 0,
     {MotorPins::InsertLimitMin, false, false, 0}, {MotorPins::InsertLimitMax, false, false, 0}}};
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

void beginLimit(Limit &limit) {
  if (limit.pin != 255) pinMode(limit.pin, INPUT_PULLUP);
  limit.raw = limit.active = limit.pin != 255 && digitalRead(limit.pin) == LOW;
  limit.inhibited = limit.raw;
  limit.changedAt = millis();
}

void readLimit(Limit &limit) {
  const bool raw = limit.pin != 255 && digitalRead(limit.pin) == LOW;
  const unsigned long now = millis();
  if (raw != limit.raw) {
    limit.raw = raw;
    limit.changedAt = now;
  }
  if (raw) limit.inhibited = true;
  if (now - limit.changedAt >= MotorPins::LimitDebounceMs) {
    limit.active = raw;
    limit.inhibited = raw;
  }
}

void readLimits(MotorState &motor) {
  readLimit(motor.minimum);
  readLimit(motor.maximum);
}

bool pressed(const Limit &limit) {
  // Inhibit immediately on LOW. Require a stable release before moving toward
  // that end again. Contact bounce can stop a move, never extend it into a limit.
  return limit.inhibited;
}

bool blocked(MotorState &motor, bool positive) {
  if (pressed(positive ? motor.maximum : motor.minimum)) return true;
  const long position = motor.stepper->currentPosition();
  return motor.soft && (positive ? position >= motor.softMax : position <= motor.softMin);
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
  if (motor.mode != Mode::Idle) {
    motor.idleSinceMs = millis();
    motor.idleHold = true;
    if (motor.awakeMs) motor.awakeSince = motor.idleSinceMs;
  }
  if (motor.mode == Mode::Homing) {
    motor.homeError = HomeError::Cancelled;
    motor.stepper->setMaxSpeed(motor.speed);
  }
  motor.mode = Mode::Idle;
  motor.held = false;
  digitalWrite(motor.pins.step, LOW);
}

void failHome(MotorState &motor, HomeError error) {
  halt(motor);
  motor.homed = false;
  motor.homeError = error;
}

void homeDirection(MotorState &motor, bool positive, float speed) {
  motor.direction = positive ? Direction::Clockwise : Direction::Counterclockwise;
  motor.stepper->setSpeed(positive ? speed : -speed);
}

void updateHome(MotorState &motor) {
  if (millis() - motor.homeStarted >= MotorPins::HomeTimeoutMs) {
    failHome(motor, HomeError::Timeout);
    return;
  }
  if (pressed(motor.minimum) && pressed(motor.maximum)) {
    failHome(motor, HomeError::BothLimits);
    return;
  }
  if (motor.homePhase == HomePhase::Seek || motor.homePhase == HomePhase::Approach) {
    if (motor.minimum.active) {
      if (motor.homePhase == HomePhase::Seek) {
        motor.homePhase = HomePhase::Backoff;
        motor.backoffPulses = 0;
        homeDirection(motor, true, MotorPins::HomeSpeed);
      } else {
        motor.stepper->setCurrentPosition(0);
        halt(motor);
        motor.homed = true;
        motor.homeError = HomeError::None;
      }
      return;
    }
    if (pressed(motor.minimum)) return;
  } else if (motor.homePhase == HomePhase::Backoff) {
    if (pressed(motor.maximum)) {
      failHome(motor, HomeError::Maximum);
      return;
    }
    if (motor.backoffPulses >= MotorPins::HomeBackoffSteps) {
      motor.homePhase = HomePhase::Released;
      motor.releasedAt = millis();
      return;
    }
  } else {
    if (!pressed(motor.minimum)) {
      motor.homePhase = HomePhase::Approach;
      homeDirection(motor, false, MotorPins::HomeSlowSpeed);
    } else if (millis() - motor.releasedAt >= MotorPins::LimitDebounceMs) {
      failHome(motor, HomeError::NotReleased);
    }
    return;
  }
  if (motor.homePulses >= MotorPins::HomeStepLimit) {
    failHome(motor, HomeError::StepLimit);
    return;
  }
  const long position = motor.stepper->currentPosition();
  if ((motor.direction == Direction::Clockwise && position == LONG_MAX) ||
      (motor.direction == Direction::Counterclockwise && position == LONG_MIN)) {
    failHome(motor, HomeError::PositionRange);
    return;
  }
  motor.stepper->runSpeed();
  if (motor.stepper->currentPosition() != position) {
    ++motor.homePulses;
    if (motor.homePhase == HomePhase::Backoff) ++motor.backoffPulses;
  }
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
    motor.homed = motor.soft = false;
    motor.awakeMs = 0;
    motor.homeError = HomeError::None;
    motor.idleHold = false;
    beginLimit(motor.minimum);
    beginLimit(motor.maximum);
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
    readLimits(motor);
    if (motor.held && millis() - motor.lastHold >= holdTimeoutMs) {
      halt(motor);
    }
    if (motor.mode == Mode::Idle) {
      if (motor.awakeMs && millis() - motor.awakeSince >= motor.awakeMs) motor.awakeMs = 0;
      if (motor.idleHold && millis() - motor.idleSinceMs >= MotorPins::SleepTimeoutMs) {
        motor.idleHold = false;
      }
      if (motor.power != Power::Sleeping &&
          motor.awakeMs == 0 && !motor.idleHold) {
        digitalWrite(motor.sleepPin, LOW);
        motor.power = Power::Sleeping;
      }
      continue;
    }
    if (motor.mode != Mode::Homing && blocked(motor, motor.direction == Direction::Clockwise)) {
      halt(motor);
      continue;
    }
    if (motor.power == Power::Waking) {
      // Do not block serial handling or stepping on the other two motors.
      if (micros() - motor.wakeStartedUs < driverWakeDelayUs) continue;
      motor.power = Power::Awake;
    }
    if (motor.mode == Mode::Homing) {
      updateHome(motor);
    } else if (motor.mode == Mode::Finite) {
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
        if (blocked(motor, motor.direction == Direction::Clockwise)) halt(motor);
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
  if (divisor != microstepDivisor) {
    for (uint8_t i = 0; i < motorCount; ++i) motors[i].homed = motors[i].soft = false;
  }
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
  readLimits(motor);
  if (blocked(motor, delta > 0)) return false;
  const long target = position + delta;
  if (motor.soft && (target < motor.softMin || target > motor.softMax)) return false;
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
  readLimits(motor);
  if (blocked(motor, motor.direction == Direction::Clockwise)) return false;
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

HomeError home() {
  if (!canConfigure()) return HomeError::Busy;
  MotorState &motor = selectedState();
  if (motor.minimum.pin == 255) return HomeError::NoMinimum;
  readLimits(motor);
  motor.homed = motor.soft = false;
  motor.homeError = HomeError::None;
  motor.homePhase = HomePhase::Seek;
  motor.homeStarted = millis();
  motor.homePulses = motor.backoffPulses = 0;
  motor.held = false;
  motor.stepper->setMaxSpeed(MotorPins::HomeSpeed);
  homeDirection(motor, false, MotorPins::HomeSpeed);
  motor.mode = Mode::Homing;
  wakeForMotion(motor);
  return HomeError::None;
}

HomeError takeHomeError(Motor motor) {
  if (!validMotor(motor)) return HomeError::None;
  MotorState &state = motors[static_cast<uint8_t>(motor)];
  const HomeError result = state.homeError;
  state.homeError = HomeError::None;
  return result;
}

const __FlashStringHelper *homeErrorText(HomeError error) {
  switch (error) {
    case HomeError::Busy: return F("motor busy or not initialized");
    case HomeError::NoMinimum: return F("minimum limit not configured");
    case HomeError::Timeout: return F("timeout");
    case HomeError::StepLimit: return F("step limit");
    case HomeError::Maximum: return F("maximum limit reached during backoff");
    case HomeError::BothLimits: return F("both limits active");
    case HomeError::NotReleased: return F("minimum limit did not release");
    case HomeError::PositionRange: return F("position range exceeded");
    case HomeError::Cancelled: return F("cancelled");
    default: return F("unknown");
  }
}

bool setSoftLimits(long minimum, long maximum) {
  if (!canConfigure() || !selectedState().homed || minimum > maximum) return false;
  MotorState &motor = selectedState();
  motor.softMin = minimum;
  motor.softMax = maximum;
  motor.soft = true;
  return true;
}

bool keepAwake(unsigned long milliseconds) {
  if (!initialized || milliseconds > 60000UL) return false;
  MotorState &motor = selectedState();
  motor.awakeMs = milliseconds;
  motor.awakeSince = millis();
  if (milliseconds) wakeForMotion(motor);
  return true;
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
  status.homed = state.homed;
  status.limitMin = state.minimum.active;
  status.limitMax = state.maximum.active;
  status.soft = state.soft;
  status.awake = state.power != Power::Sleeping;
  return true;
}

} // namespace MotorControl
