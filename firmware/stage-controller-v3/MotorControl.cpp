#include "MotorControl.h"
#include "MotorPins.h"
#include "EncoderConfig.h"
#include <AccelStepper.h>
#include <limits.h>
#include <math.h>

namespace MotorControl {
namespace {
AccelStepper bottom(AccelStepper::DRIVER, MotorPins::Bottom.step, MotorPins::Bottom.dir, 0, 0, false);
AccelStepper top(AccelStepper::DRIVER, MotorPins::Top.step, MotorPins::Top.dir, 0, 0, false);
AccelStepper insert(AccelStepper::DRIVER, MotorPins::Insert.step, MotorPins::Insert.dir, 0, 0, false);
enum class Mode : uint8_t { Idle, Finite, Continuous };
enum class Power : uint8_t { Sleeping, Waking, Awake };
struct MotorState {
  AccelStepper *stepper;
  MotorPins::PinPair pins;
  uint8_t sleepPin;
  Direction direction;
  Mode mode;
  Power power;
  Fault fault;
  float speed;
  float acceleration;
  uint16_t stepsPerRevolution;
  bool held;
  bool settling;
  bool absoluteMove; // Restore the user's configured direction after an absolute finite move.
  unsigned long lastHold;
  unsigned long idleSinceMs;
  unsigned long wakeStartedUs;
  unsigned long startedMs;
  unsigned long durationLimitMs;
  unsigned long lastControlMs;
  unsigned long lastProgressMs;
  unsigned long settleSinceMs;
  long originCounts;
  long targetCounts;
  long progressCounts;
  long progressPulses;
  float phaseRemainder;
  float outputSpeed;
  float referenceSpeed; // Nonnegative held-motion trajectory speed, reset for each press.
  // Internal CW-positive coordinates place upper at minimum, lower at maximum.
  // Public CCW-positive coordinates are projected separately in getStatus().
  long minimumCounts;
  long maximumCounts;
  long samplePulses; // Pulse position at acquisition of the most recent encoder sample.
  uint8_t bounds;
  Calibration calibration;
  Direction savedDirection;
  unsigned long phaseStartedMs;
  long directionOriginCounts; // Retry reference; originCounts remains the fixed per-hold hard anchor.
  unsigned long lastWarningMs;
  unsigned long previousWarningMs;
  uint16_t directionWarnings;
  uint8_t directionPauseSamples; // Zero outside a warning pause; otherwise stable fresh samples.
};
struct SwitchState {
  unsigned long candidateSince;
  uint16_t raw;
  uint8_t candidate;
  uint8_t stable;
};
SwitchState switches[3];
unsigned long lastSwitchSample;
constexpr uint8_t switchPins[] = {MotorPins::BottomSwitch, MotorPins::TopSwitch, MotorPins::InsertSwitch};
constexpr unsigned long switchDebounceMs = 20;
MotorState motors[] = {
  {&bottom, MotorPins::Bottom, MotorPins::BottomSleep},
  {&top, MotorPins::Top, MotorPins::TopSleep},
  {&insert, MotorPins::Insert, MotorPins::InsertSleep}
};
Motor selected = Motor::Bottom_Motor;
bool initialized = false;
// All three stages always use eighth stepping; no command can change this.
constexpr uint8_t microstepDivisor = 8;
constexpr float countsPerRotation = 4096.0f;
constexpr float maximumPulseRate = 1000.0f;
constexpr unsigned long holdTimeoutMs = 1000;
constexpr unsigned long driverWakeDelayUs = 2000;

float minimum(float a, float b) { return a < b ? a : b; }
float maximum(float a, float b) { return a > b ? a : b; }
float clamp(float x, float low, float high) { return maximum(low, minimum(high, x)); }
float pulsesPerRotation(const MotorState &m) { return m.stepsPerRevolution * float(microstepDivisor); }
float speedLimit(const MotorState &m) {
  // The main loop sends at most about 650 pulses/s on one axis. A held move needs
  // 1.25 times its speed to catch up, so 480 pulses/s is the highest that tracks.
  return minimum(EncoderConfig::MaxShaftSpeed, 480.0f / pulsesPerRotation(m));
}
float toleranceCounts(const MotorState &m) {
  return ceilf(0.5f * countsPerRotation / pulsesPerRotation(m) + EncoderConfig::EncoderNoiseCounts);
}
MotorState &selectedState() { return motors[static_cast<uint8_t>(selected)]; }
bool calibrating(const MotorState &m) {
  return m.calibration >= Calibration::Lower && m.calibration <= Calibration::Center;
}
bool seeking(const MotorState &m) {
  return m.calibration == Calibration::Lower || m.calibration == Calibration::Upper;
}
float motionSpeed(const MotorState &m) {
  return calibrating(m) ? minimum(m.speed, EncoderConfig::CalibrationMaxSpeed) : m.speed;
}
bool canConfigure() { return initialized && selectedState().mode == Mode::Idle && !calibrating(selectedState()); }
uint8_t decodeSwitch(uint16_t raw) { return raw < 100 ? 2 : raw < 700 ? 1 : 0; }
uint8_t switchMask(uint8_t index) {
  // A released upper contact rises through the lower band. While the upper contact is
  // the debounced state, a lower reading is that transition: the lower switch is at the
  // other end of travel. A real lower contact becomes stable after the debounce time.
  const SwitchState &s = switches[index];
  return s.stable == 2 && s.candidate == 1 ? 2 : s.candidate | s.stable;
}
uint8_t directionBit(float direction) { return direction > 0 ? 1 : 2; }
float intendedDirection(const MotorState &m) {
  return m.outputSpeed != 0 ? m.outputSpeed : (m.direction == Direction::Clockwise ? 1.0f : -1.0f);
}
float boundedSpeed(const MotorState &m, long counts, float direction, float requestedSpeed) {
  const long distance = direction > 0 ? m.maximumCounts - counts : counts - m.minimumCounts;
  const float room = maximum(0, float(distance) - EncoderConfig::EncoderNoiseCounts) / countsPerRotation;
  const float stoppingDistance = requestedSpeed * requestedSpeed / (2.0f * m.acceleration);
  return room >= stoppingDistance ? requestedSpeed : sqrtf(2.0f * m.acceleration * room);
}
bool targetAllowed(const MotorState &m, long current, long target) {
  // An axis externally pushed outside a bound may recover in smaller inward moves.
  if (target < m.minimumCounts) return current < m.minimumCounts && target > current;
  if (target > m.maximumCounts) return current > m.maximumCounts && target < current;
  return true;
}
void halt(MotorState &m) {
  m.stepper->setCurrentPosition(0); // Internal pulse count is local to each motion.
  if (m.mode != Mode::Idle) m.idleSinceMs = millis();
  m.mode = Mode::Idle;
  m.held = false;
  m.settling = false;
  m.outputSpeed = 0;
  m.referenceSpeed = 0;
  m.directionPauseSamples = 0;
  digitalWrite(m.pins.step, LOW);
  if (m.absoluteMove) {
    m.absoluteMove = false;
    m.direction = m.savedDirection;
    digitalWrite(m.pins.dir, m.direction == Direction::Clockwise ? HIGH : LOW);
  }
}
void endCalibration(MotorState &m, Calibration result) {
  if (!calibrating(m)) return;
  m.calibration = result;
  if (result == Calibration::Done) m.directionWarnings = 0;
  m.direction = m.savedDirection;
  digitalWrite(m.pins.dir, m.direction == Direction::Clockwise ? HIGH : LOW);
}
void fail(MotorState &m, Fault fault) {
  halt(m); endCalibration(m, Calibration::Failed); m.fault = fault;
}
bool captureSwitch(uint8_t index, uint8_t side) {
  // Only serviceCalibration calls this helper; ordinary contacts never capture or zero.
  MotorState &m = motors[index];
  if (!Encoders::refresh(index)) { fail(m, Fault::Encoder); return false; }
  const long position = Encoders::get(index).counts;
  if (side == 2) {
    if (position > m.maximumCounts) { fail(m, Fault::Tracking); return false; }
    m.minimumCounts = position;
  } else {
    // The calibration lower/CW contact establishes zero. Rebase a saved upper
    // endpoint to preserve its physical location; an unset sentinel stays unset.
    // Both count values are within +/-1e9, so subtraction fits signed 32-bit long.
    const long shiftedUpper = m.bounds & 2 ? m.minimumCounts - position : -EncoderConfig::CountLimit;
    if (shiftedUpper < -EncoderConfig::CountLimit || shiftedUpper > 0) {
      fail(m, Fault::Tracking); // Validate everything before changing position or bounds.
      return false;
    }
    halt(m); // Never clear a fault or resume a canceled move when establishing zero.
    if (!Encoders::zeroFromSample(index, position)) { fail(m, Fault::Encoder); return false; }
    m.originCounts = m.targetCounts = m.progressCounts = 0;
    m.progressPulses = m.samplePulses = 0;
    m.phaseRemainder = 0;
    m.minimumCounts = shiftedUpper;
    m.maximumCounts = 0;
  }
  m.bounds |= side; // Switch side determines the bound, regardless of position sign.
  return true;
}
void sampleSwitch(uint8_t index) {
  SwitchState &s = switches[index];
  s.raw = analogRead(switchPins[index]);
  const uint8_t candidate = decodeSwitch(s.raw);
  const unsigned long now = millis();
  if (candidate != s.candidate) {
    s.candidate = candidate;
    s.candidateSince = now;
  }
  if (s.stable == s.candidate || now - s.candidateSince < switchDebounceMs) return;
  s.stable = s.candidate;
  // Ordinary switch contacts only interlock motion. Calibration owns all automatic capture/zero.
}
void updateSwitches() {
  const unsigned long now = millis();
  if (now - lastSwitchSample < 1) return;
  lastSwitchSample = now;
  for (uint8_t i = 0; i < 3; ++i) sampleSwitch(i);
}
void wake(MotorState &m) {
  if (m.power != Power::Sleeping) return;
  digitalWrite(m.sleepPin, HIGH);
  m.wakeStartedUs = micros();
  m.power = Power::Waking;
  // SLEEP high returns the A4988 translator to its Home microstep. The rotor then moves
  // up to 2 full steps to a Home equilibrium. Friction can hold it until the first pulses
  // move the field, so allow that reverse movement in the guard of this motion.
  const long snap = static_cast<long>(2.0f * countsPerRotation / m.stepsPerRevolution) + 1;
  m.directionOriginCounts = m.originCounts += m.direction == Direction::Clockwise ? -snap : snap;
}
bool prepareMotion(MotorState &m, bool refresh = true) {
  if (!initialized || m.mode != Mode::Idle || m.fault != Fault::None) return false;
  const uint8_t index = static_cast<uint8_t>(&m - motors);
  sampleSwitch(index); // A command cannot bypass a newly asserted physical input.
  if (m.fault != Fault::None || (switchMask(index) &
      directionBit(m.direction == Direction::Clockwise ? 1 : -1))) return false;
  if (refresh && !Encoders::refresh(index)) return false;
  const Encoders::Reading r = Encoders::get(index);
  if (r.health != Encoders::Health::Ok ||
      m.speed < 0.001f || m.speed > speedLimit(m)) return false;
  const unsigned long now = millis();
  m.originCounts = m.targetCounts = m.progressCounts = r.counts;
  m.directionOriginCounts = r.counts;
  m.directionPauseSamples = 0;
  m.startedMs = m.lastProgressMs = now;
  m.lastControlMs = r.sampledAt;
  m.progressPulses = 0;
  m.samplePulses = 0;
  m.phaseRemainder = 0;
  m.outputSpeed = 0;
  m.referenceSpeed = 0;
  m.settling = false;
  m.held = false;
  m.stepper->setCurrentPosition(0);
  return true;
}
void launchFinite(MotorState &m, long target, float nominalSeconds) {
  m.targetCounts = target;
  m.durationLimitMs = static_cast<unsigned long>((nominalSeconds * 1.5f +
                        2.0f * motionSpeed(m) / m.acceleration + 5.0f) * 1000.0f);
  m.mode = Mode::Finite;
  wake(m);
}
void beginSearch(MotorState &m) {
  m.direction = m.calibration == Calibration::Lower ? Direction::Clockwise : Direction::Counterclockwise;
  m.phaseStartedMs = millis();
  if (prepareMotion(m)) { m.mode = Mode::Continuous; wake(m); }
  else if (!(switchMask(static_cast<uint8_t>(&m - motors)) & directionBit(intendedDirection(m))))
    fail(m, Fault::Encoder);
}
void serviceCalibration(uint8_t index) {
  MotorState &m = motors[index];
  if (!calibrating(m)) return;
  if (Encoders::get(index).health != Encoders::Health::Ok) { fail(m, Fault::Encoder); return; }
  if (!seeking(m)) {
    if (m.mode == Mode::Idle) {
      if (labs(m.targetCounts - Encoders::get(index).counts) > toleranceCounts(m)) fail(m, Fault::Tracking);
      else endCalibration(m, Calibration::Done);
    }
    return;
  }
  if (millis() - m.phaseStartedMs >= EncoderConfig::CalibrationSearchTimeoutMs) {
    fail(m, Fault::Timeout); return;
  }
  const uint8_t wanted = m.calibration == Calibration::Lower ? 1 : 2;
  if (m.mode == Mode::Idle && !(switchMask(index) & directionBit(intendedDirection(m)))) {
    // A contact reading that did not confirm halted the seek: continue it. The phase
    // timeout still runs from the start of the phase.
    const unsigned long started = m.phaseStartedMs;
    beginSearch(m);
    m.phaseStartedMs = started;
    return;
  }
  if (switches[index].stable != wanted || switches[index].candidate != wanted) return;
  halt(m);
  if (!captureSwitch(index, wanted)) return;
  if (wanted == 1) {
    m.calibration = Calibration::Upper;
    beginSearch(m);
  } else {
    if (m.minimumCounts >= 0) { fail(m, Fault::Tracking); return; }
    m.calibration = Calibration::Center;
    m.direction = Direction::Clockwise;
    if (!prepareMotion(m)) { fail(m, Fault::Encoder); return; }
    const long target = (m.minimumCounts - 1) / 2; // Public half-count ties round upward.
    const float seconds = labs(target - m.originCounts) / countsPerRotation / motionSpeed(m);
    if (seconds > 86400.0f) { fail(m, Fault::Timeout); return; }
    launchFinite(m, target, seconds);
  }
}

bool recordDirectionWarning(MotorState &m, unsigned long now) {
  const bool frequent = m.directionWarnings >= 2 && now - m.previousWarningMs <= EncoderConfig::DirectionWarningWindowMs;
  if (m.directionWarnings < UINT16_MAX) ++m.directionWarnings;
  m.previousWarningMs = m.lastWarningMs;
  m.lastWarningMs = now;
  return frequent;
}
bool holdDirectionGuard(MotorState &m, const Encoders::Reading &r, float direction, float threshold) {
  const unsigned long now = millis();
  const long displacement = r.counts - m.directionOriginCounts;
  const bool significant = direction * displacement <= -2 * threshold ||
    direction * (r.counts - m.originCounts) <= -2 * threshold;
  if (m.directionPauseSamples) {
    if (significant || now - m.settleSinceMs >= EncoderConfig::DirectionPauseTimeoutMs) {
      fail(m, Fault::Direction); return true;
    }
    if (labs(r.counts - m.progressCounts) > 2 * EncoderConfig::EncoderNoiseCounts) {
      m.progressCounts = r.counts;
      m.lastProgressMs = now;
      m.directionPauseSamples = 1;
    } else if (m.directionPauseSamples < 3) ++m.directionPauseSamples;
    if (m.directionPauseSamples >= 3 && now - m.lastProgressMs >= EncoderConfig::DirectionPauseStableMs) {
      // Resume only this held trajectory. Never zero encoder position, shift bounds,
      // renew its lease, or move the original hard reverse-excursion anchor.
      m.targetCounts = m.directionOriginCounts = m.progressCounts = r.counts;
      m.phaseRemainder = 0;
      m.progressPulses = m.stepper->currentPosition();
      m.samplePulses = m.progressPulses;
      m.lastControlMs = r.sampledAt;
      m.lastProgressMs = now;
      m.directionPauseSamples = 0;
    }
    return true;
  }
  if (!significant && direction * displacement >= -threshold) return false;
  const bool frequent = recordDirectionWarning(m, now);
  if (significant || frequent) fail(m, Fault::Direction);
  else {
    m.directionPauseSamples = 1;
    m.settleSinceMs = m.lastProgressMs = now;
    m.progressCounts = r.counts;
    m.outputSpeed = m.referenceSpeed = 0;
    m.stepper->setSpeed(0);
  }
  return true;
}
void feedback(MotorState &m, const Encoders::Reading &r) {
  if (r.sampledAt == m.lastControlMs) return;
  const unsigned long elapsedMs = r.sampledAt - m.lastControlMs;
  m.lastControlMs = r.sampledAt;
  const float dt = elapsedMs * 0.001f;
  const float ppr = pulsesPerRotation(m);
  const float direction = m.direction == Direction::Clockwise ? 1.0f : -1.0f;
  const float tolerance = toleranceCounts(m);
  const long pulses = m.stepper->currentPosition();
  m.samplePulses = pulses;
  const long displacement = r.counts - m.originCounts;
  const float directionThreshold = maximum(32.0f, 2.0f * countsPerRotation / ppr);
  if (m.held) {
    if (holdDirectionGuard(m, r, direction, directionThreshold)) return;
  // Other movement modes retain their strict original reverse-direction guard.
  } else if (direction * displacement < -directionThreshold) {
    fail(m, Fault::Direction);
    return;
  }
  // Count actual pulse attempts before declaring a stall: 0.001 rev/s can
  // legitimately take seconds between steps. Progress is only encoder travel in the
  // direction of the pulses sent since the anchor. A rotor blocked by an obstruction
  // bounces back up to 2 full steps each electrical cycle; that bounce is not progress.
  const long pulseTravel = pulses - m.progressPulses;
  const long countTravel = r.counts - m.progressCounts;
  if (pulseTravel == 0) {
    m.progressCounts = r.counts; // No pulse since the anchor: follow the shaft.
  } else if ((pulseTravel > 0 ? countTravel : -countTravel) >=
      maximum(2.0f * EncoderConfig::EncoderNoiseCounts + 1.0f, tolerance * 0.5f)) {
    m.progressCounts = r.counts;
    m.progressPulses = pulses;
    m.lastProgressMs = millis();
  } else if (labs(pulseTravel) >= maximum(8.0f, ppr * 0.05f) &&
             millis() - m.lastProgressMs >= 1500) {
    fail(m, Fault::Stall);
    return;
  }

  float desired;
  if (m.mode == Mode::Continuous) {
    const long room = direction > 0 ? m.maximumCounts - r.counts : r.counts - m.minimumCounts;
    if (room < countsPerRotation / ppr + EncoderConfig::EncoderNoiseCounts) {
      halt(m); return;
    }
    const float approachSpeed = boundedSpeed(m, r.counts, direction, motionSpeed(m));
    float feedforwardSpeed = approachSpeed;
    float travel = approachSpeed * dt; // Serial-only start retains its immediate constant speed.
    if (m.held || seeking(m)) {
      // A boundary may cut speed immediately; normal held startup ramps from zero.
      const float previous = minimum(m.referenceSpeed, approachSpeed);
      const float rampTime = minimum(dt, (approachSpeed - previous) / m.acceleration);
      m.referenceSpeed = minimum(approachSpeed, previous + m.acceleration * rampTime);
      // Integrate the ramp and any remaining constant-speed part of this interval.
      travel = 0.5f * (previous + m.referenceSpeed) * rampTime +
               m.referenceSpeed * (dt - rampTime);
      feedforwardSpeed = m.referenceSpeed;
    }
    // Slow the reference trajectory as well as the pulses near a bound.
    m.phaseRemainder += direction * travel * countsPerRotation;
    const long advance = static_cast<long>(m.phaseRemainder);
    if ((advance > 0 && m.targetCounts > EncoderConfig::CountLimit - advance) ||
        (advance < 0 && m.targetCounts < -EncoderConfig::CountLimit - advance)) {
      fail(m, Fault::Tracking);
      return;
    }
    m.targetCounts += advance;
    m.phaseRemainder -= advance;
    if (direction > 0 && m.targetCounts >= m.maximumCounts) {
      m.targetCounts = m.maximumCounts; m.phaseRemainder = 0;
    } else if (direction < 0 && m.targetCounts <= m.minimumCounts) {
      m.targetCounts = m.minimumCounts; m.phaseRemainder = 0;
    }
    const float error = (m.targetCounts - r.counts + m.phaseRemainder) / countsPerRotation;
    if (fabsf(error) > EncoderConfig::MaximumFollowingError) {
      fail(m, Fault::Tracking);
      return;
    }
    desired = direction * feedforwardSpeed + EncoderConfig::PositionGain * error;
    // Held motion corrects rate but never reverses the chosen direction. Held and seek
    // catch-up after an obstruction stays near the requested speed.
    const float cap = m.held || seeking(m) ? 1.25f * approachSpeed :
        minimum(EncoderConfig::MaxShaftSpeed, maximumPulseRate / ppr);
    desired = direction * clamp(direction * desired, 0, cap);
    if (m.held || seeking(m)) {
      const float maxChange = m.acceleration * dt;
      m.outputSpeed += clamp(desired - m.outputSpeed, -maxChange, maxChange);
    } else {
      m.outputSpeed = desired;
    }
  } else {
    if (millis() - m.startedMs > m.durationLimitMs) {
      fail(m, Fault::Timeout);
      return;
    }
    const long errorCounts = m.targetCounts - r.counts;
    if (labs(errorCounts) <= tolerance) {
      m.outputSpeed = 0;
      m.stepper->setSpeed(0);
      if (!m.settling) {
        m.settling = true;
        m.settleSinceMs = millis();
      } else if (millis() - m.settleSinceMs >= EncoderConfig::SettleMs) {
        halt(m);
      }
      return;
    }
    m.settling = false;
    if (!EncoderConfig::AllowReverseCorrection && direction * errorCounts < 0) {
      fail(m, Fault::Tracking);
      return;
    }
    const float distance = labs(errorCounts) / countsPerRotation;
    const float brakingSpeed = sqrtf(2.0f * m.acceleration * distance);
    desired = minimum(motionSpeed(m), minimum(brakingSpeed, EncoderConfig::PositionGain * distance));
    if (errorCounts < 0) desired = -desired;
    const float maxChange = m.acceleration * dt;
    m.outputSpeed += clamp(desired - m.outputSpeed, -maxChange, maxChange);
  }
  // Decelerate toward the boundary in the actual pulse direction, including
  // finite-move overshoot correction. Integer differences avoid large-position ULP loss.
  if (m.outputSpeed != 0) {
    const float pulseDirection = m.outputSpeed > 0 ? 1.0f : -1.0f;
    const float boundarySpeed = boundedSpeed(m, r.counts, pulseDirection, fabsf(m.outputSpeed));
    if (boundarySpeed == 0 && m.mode == Mode::Finite &&
        pulseDirection * (m.targetCounts - r.counts) > tolerance) {
      fail(m, Fault::Tracking); // Correcting this error would require an outward pulse.
      return;
    }
    m.outputSpeed = pulseDirection * minimum(fabsf(m.outputSpeed), boundarySpeed);
  }
  m.stepper->setSpeed(clamp(m.outputSpeed * ppr, -maximumPulseRate, maximumPulseRate));
}

bool pulseWithinBounds(MotorState &m, const Encoders::Reading &r) {
  if (m.outputSpeed == 0) return true;
  const float direction = m.outputSpeed > 0 ? 1.0f : -1.0f;
  const long room = direction > 0 ? m.maximumCounts - r.counts : r.counts - m.minimumCounts;
  const long sentSinceSample = m.stepper->currentPosition() - m.samplePulses;
  const float onePulse = countsPerRotation / pulsesPerRotation(m);
  const float projectedRoom = float(room) - direction * sentSinceSample * onePulse;
  if (projectedRoom >= onePulse + EncoderConfig::EncoderNoiseCounts) return true;
  m.stepper->setSpeed(0);
  // Wait for the next encoder sample when pulses are still in flight, rather
  // than accumulating an entire sampling interval of additional outward pulses.
  if (sentSinceSample != 0) return false;
  if (m.mode == Mode::Finite &&
      direction * (m.targetCounts - r.counts) < -toleranceCounts(m)) {
    m.outputSpeed = 0; // A subsequent feedback update may correct inward.
  } else if (m.mode == Mode::Finite &&
             labs(m.targetCounts - r.counts) > toleranceCounts(m)) {
    fail(m, Fault::Tracking); // A safe stop outside completion tolerance is not success.
  } else {
    halt(m); // No complete outward pulse fits inside the remaining safe room.
  }
  return false;
}
}

void begin() {
  initialized = false;
  selected = Motor::Bottom_Motor;
  for (uint8_t i = 0; i < 3; ++i) {
    MotorState &m = motors[i];
    digitalWrite(m.sleepPin, LOW);
    pinMode(m.sleepPin, OUTPUT);
    m.power = Power::Sleeping;
    m.idleSinceMs = millis();
    m.direction = Direction::Clockwise;
    m.mode = Mode::Idle;
    m.fault = Fault::None;
    m.calibration = Calibration::None;
    m.speed = 0.25f;
    m.acceleration = 0.25f;
    m.stepsPerRevolution = 200;
    m.held = m.settling = m.absoluteMove = false;
    m.referenceSpeed = 0;
    m.directionPauseSamples = 0;
    m.directionWarnings = 0;
    m.lastWarningMs = m.previousWarningMs = 0;
    m.minimumCounts = -EncoderConfig::CountLimit;
    m.maximumCounts = EncoderConfig::CountLimit;
    m.bounds = 0;
    m.samplePulses = 0;
    digitalWrite(m.pins.step, LOW);
    digitalWrite(m.pins.dir, HIGH);
    m.stepper->enableOutputs();
    m.stepper->setCurrentPosition(0);
    m.stepper->setMaxSpeed(maximumPulseRate);
    m.stepper->setMinPulseWidth(3);
  }
  digitalWrite(MotorPins::MS1, HIGH);
  digitalWrite(MotorPins::MS2, HIGH);
  digitalWrite(MotorPins::MS3, LOW);
  pinMode(MotorPins::MS1, OUTPUT);
  pinMode(MotorPins::MS2, OUTPUT);
  pinMode(MotorPins::MS3, OUTPUT);
  Encoders::begin();
  for (uint8_t i = 0; i < 3; ++i) {
    pinMode(switchPins[i], INPUT_PULLUP);
    SwitchState &s = switches[i];
    s.raw = analogRead(switchPins[i]);
    s.candidate = decodeSwitch(s.raw);
    s.stable = 0;
    s.candidateSince = millis();
  }
  lastSwitchSample = millis();
  initialized = true;
}

void update() {
  if (!initialized) return;
  updateSwitches();
  Encoders::update();
  for (uint8_t i = 0; i < 3; ++i) {
    MotorState &m = motors[i];
    const unsigned long now = millis();
    if (m.directionWarnings && now - m.lastWarningMs > EncoderConfig::DirectionWarningWindowMs) {
      // Keep expired timestamps old across millis rollover without erasing lifetime history.
      m.lastWarningMs = m.previousWarningMs = now - EncoderConfig::DirectionWarningWindowMs - 1;
    }
    serviceCalibration(i);
    if (m.held && millis() - m.lastHold >= holdTimeoutMs) halt(m);
    if (m.mode == Mode::Idle) {
      if (m.power != Power::Sleeping && millis() - m.idleSinceMs >= MotorPins::SleepTimeoutMs) {
        digitalWrite(m.sleepPin, LOW);
        m.power = Power::Sleeping;
      }
      continue;
    }
    const Encoders::Reading r = Encoders::get(i);
    if (r.health != Encoders::Health::Ok) { fail(m, Fault::Encoder); continue; }
    if (switchMask(i) & directionBit(intendedDirection(m))) {
      halt(m); // Candidate contact stops immediately; bounce never resumes a canceled move.
      continue;
    }
    if (m.power == Power::Waking) {
      if (micros() - m.wakeStartedUs < driverWakeDelayUs) continue;
      m.power = Power::Awake;
    }
    feedback(m, r);
    if (m.mode != Mode::Idle) {
      if (m.outputSpeed != 0 && (switchMask(i) & directionBit(m.outputSpeed))) {
        halt(m); // Also guard reverse correction pulses after feedback changes direction.
        continue;
      }
      const long pulses = m.stepper->currentPosition();
      if (pulses >= LONG_MAX - 1 || pulses <= LONG_MIN + 1) fail(m, Fault::Tracking);
      else if (pulseWithinBounds(m, r)) m.stepper->runSpeed();
    }
  }
}

bool selectMotor(Motor motor) {
  if (static_cast<uint8_t>(motor) >= 3) return false;
  selected = motor;
  return true;
}
Motor selectedMotor() { return selected; }
bool setMicrostep(uint8_t divisor) {
  if (!initialized || divisor != microstepDivisor) return false;
  for (uint8_t i = 0; i < 3; ++i) {
    if (motors[i].mode != Mode::Idle || calibrating(motors[i])) return false;
  }
  // Retain an idempotent serial acknowledgement for existing clients.
  // Pin levels were established before any driver wakes in begin().
  return true;
}
uint8_t microstep() { return microstepDivisor; }
bool setDirection(Direction direction) {
  if (!canConfigure() || (direction != Direction::Clockwise && direction != Direction::Counterclockwise))
    return false;
  selectedState().direction = direction;
  digitalWrite(selectedState().pins.dir, direction == Direction::Clockwise ? HIGH : LOW);
  return true;
}
bool setSpeed(float speed) {
  if (!canConfigure() || !isfinite(speed) || speed < 0.001f ||
      speed > speedLimit(selectedState()) + 0.00000051f)
    return false;
  // Status rounds to six decimals; accept that displayed ceiling then clamp.
  selectedState().speed = minimum(speed, speedLimit(selectedState()));
  return true;
}
bool setAcceleration(float acceleration) {
  if (!canConfigure() || !isfinite(acceleration) || acceleration < 0.001f || acceleration > 50.0f)
    return false;
  selectedState().acceleration = acceleration;
  return true;
}
bool setStepsPerRevolution(uint16_t steps) {
  if (!canConfigure() || !steps || steps * float(microstepDivisor) > 480000.0f) return false;
  selectedState().stepsPerRevolution = steps;
  selectedState().speed = minimum(selectedState().speed, speedLimit(selectedState()));
  return true;
}
bool rotateRevolutions(float rotations) {
  if (!canConfigure() || !isfinite(rotations) || rotations < 0.001f || rotations > 10000.0f)
    return false;
  MotorState &m = selectedState();
  const float nominalSeconds = rotations / m.speed;
  if (nominalSeconds > 86400.0f) return false;
  const long magnitude = static_cast<long>(floorf(rotations * countsPerRotation + 0.5f));
  const long delta = m.direction == Direction::Clockwise ? magnitude : -magnitude;
  if (!prepareMotion(m)) return false;
  const long position = m.originCounts;
  if ((delta > 0 && position > EncoderConfig::CountLimit - delta) ||
      (delta < 0 && position < -EncoderConfig::CountLimit - delta) ||
      !targetAllowed(m, position, position + delta)) return false;
  launchFinite(m, position + delta, nominalSeconds);
  return true;
}
bool rotateSteps(long steps) { return steps > 0 && rotateRevolutions(steps / pulsesPerRotation(selectedState())); }
bool rotateOneRevolution() { return rotateRevolutions(1.0f); }
bool goToPosition(float rotations) {
  if (!canConfigure() || !isfinite(rotations)) return false;
  MotorState &m = selectedState();
  if (m.calibration != Calibration::Done || m.bounds != 3 || m.fault != Fault::None ||
      rotations < -m.maximumCounts / countsPerRotation ||
      rotations > -m.minimumCounts / countsPerRotation) return false;
  const uint8_t index = static_cast<uint8_t>(selected);
  if (!Encoders::refresh(index)) return false;
  const long target = -static_cast<long>(floorf(rotations * countsPerRotation + 0.5f));
  if (target < m.minimumCounts || target > m.maximumCounts) return false;
  const long delta = target - Encoders::get(index).counts;
  // An endpoint already reached needs no outward pulse, even with its switch held.
  if (labs(delta) <= toleranceCounts(m)) return true;
  const float seconds = labs(delta) / countsPerRotation / m.speed;
  if (seconds > 86400.0f) return false;
  m.savedDirection = m.direction;
  m.direction = delta > 0 ? Direction::Clockwise : Direction::Counterclockwise;
  // Reuse the same fresh sample used to choose direction and the absolute target.
  if (!prepareMotion(m, false)) { m.direction = m.savedDirection; return false; }
  m.absoluteMove = true;
  launchFinite(m, target, seconds);
  return true;
}
bool startRotation() {
  if (!canConfigure()) return false;
  MotorState &m = selectedState();
  if (!prepareMotion(m)) return false;
  if ((m.direction == Direction::Clockwise && m.originCounts >= m.maximumCounts) ||
      (m.direction == Direction::Counterclockwise && m.originCounts <= m.minimumCounts)) return false;
  m.mode = Mode::Continuous;
  wake(m);
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
  MotorState &m = selectedState();
  if (!m.held || m.mode != Mode::Continuous) return false;
  if (millis() - m.lastHold >= holdTimeoutMs) { halt(m); return false; }
  m.lastHold = millis();
  return true;
}
bool calibrate() {
  if (!canConfigure() || selectedState().fault != Fault::None) return false;
  const uint8_t index = static_cast<uint8_t>(selected);
  sampleSwitch(index);
  if (switchMask(index) == 3 || selectedState().fault != Fault::None || !Encoders::refresh(index))
    return false;
  MotorState &m = selectedState();
  m.minimumCounts = -EncoderConfig::CountLimit;
  m.maximumCounts = EncoderConfig::CountLimit;
  m.bounds = 0;
  m.savedDirection = m.direction;
  m.calibration = Calibration::Lower;
  beginSearch(m);
  return calibrating(m);
}
void stopRotation() {
  if (initialized) { halt(selectedState()); endCalibration(selectedState(), Calibration::Cancelled); }
}
void stopAll() {
  if (initialized) for (uint8_t i = 0; i < 3; ++i) {
    halt(motors[i]); endCalibration(motors[i], Calibration::Cancelled);
  }
}
bool clearFault() {
  if (!canConfigure() || !Encoders::recover(static_cast<uint8_t>(selected), selectedState().bounds == 0))
    return false;
  selectedState().fault = Fault::None;
  return true;
}
bool resetPosition() {
  if (!canConfigure()) return false;
  const uint8_t index = static_cast<uint8_t>(selected);
  sampleSwitch(index);
  if (!Encoders::resetPosition(index)) return false;
  MotorState &m = selectedState();
  m.minimumCounts = -EncoderConfig::CountLimit;
  m.maximumCounts = EncoderConfig::CountLimit;
  m.bounds = 0;
  m.calibration = Calibration::None;
  return true;
}
bool setLimit() {
  if (!canConfigure() || !Encoders::refresh(static_cast<uint8_t>(selected))) return false;
  const long position = Encoders::get(static_cast<uint8_t>(selected)).counts;
  MotorState &m = selectedState();
  if (position > 0 && position >= m.minimumCounts) {
    m.maximumCounts = position; m.bounds |= 1;
  } else if (position < 0 && position <= m.maximumCounts) {
    m.minimumCounts = position; m.bounds |= 2;
  } else return false;
  m.calibration = Calibration::None;
  return true;
}
bool clearLowerLimit() {
  if (!canConfigure()) return false;
  const uint8_t index = static_cast<uint8_t>(selected);
  sampleSwitch(index);
  selectedState().maximumCounts = EncoderConfig::CountLimit;
  selectedState().bounds &= ~1;
  selectedState().calibration = Calibration::None;
  return true;
}
bool clearUpperLimit() {
  if (!canConfigure()) return false;
  const uint8_t index = static_cast<uint8_t>(selected);
  sampleSwitch(index);
  selectedState().minimumCounts = -EncoderConfig::CountLimit;
  selectedState().bounds &= ~2;
  selectedState().calibration = Calibration::None;
  return true;
}
bool isRotating(Motor motor) {
  return initialized && static_cast<uint8_t>(motor) < 3 &&
    (motors[static_cast<uint8_t>(motor)].mode != Mode::Idle || calibrating(motors[static_cast<uint8_t>(motor)]));
}
bool getStatus(Motor motor, Status &s) {
  if (!initialized || static_cast<uint8_t>(motor) >= 3) return false;
  MotorState &m = motors[static_cast<uint8_t>(motor)];
  const Encoders::Reading r = Encoders::get(static_cast<uint8_t>(motor));
  s.direction = m.direction;
  s.rotating = m.mode != Mode::Idle || calibrating(m);
  s.continuous = m.mode == Mode::Continuous || seeking(m);
  // Public coordinates increase toward the upper/CCW end. The proven feedback
  // controller and encoder polarity remain CW-positive internally.
  s.position = -r.counts / countsPerRotation;
  s.remaining = m.mode == Mode::Finite ? (r.counts - m.targetCounts) / countsPerRotation : 0;
  s.speed = m.speed;
  s.acceleration = m.acceleration;
  s.stepsPerRevolution = m.stepsPerRevolution;
  s.encoder = r.health;
  s.fault = m.fault;
  s.tolerance = toleranceCounts(m) / countsPerRotation;
  s.maxSpeed = speedLimit(m);
  s.measuredSpeed = r.velocity == 0 ? 0 : -r.velocity;
  s.positionCounts = -r.counts;
  s.remainingCounts = m.mode == Mode::Finite ? r.counts - m.targetCounts : 0;
  s.lowerCounts = -m.maximumCounts;
  s.upperCounts = -m.minimumCounts;
  s.bounds = m.bounds;
  s.switchMask = switchMask(static_cast<uint8_t>(motor));
  s.switchRaw = switches[static_cast<uint8_t>(motor)].raw;
  s.calibration = m.calibration;
  s.directionPaused = m.directionPauseSamples != 0;
  s.directionWarnings = m.directionWarnings;
  return true;
}
const __FlashStringHelper *faultName(Fault fault) {
  switch (fault) {
    case Fault::None: return F("none");
    case Fault::Encoder: return F("encoder");
    case Fault::Stall: return F("stall");
    case Fault::Direction: return F("direction");
    case Fault::Tracking: return F("tracking");
    default: return F("timeout");
  }
}
const __FlashStringHelper *calibrationName(Calibration value) {
  switch (value) {
    case Calibration::Lower: return F("lower");
    case Calibration::Upper: return F("upper");
    case Calibration::Center: return F("center");
    case Calibration::Done: return F("done");
    case Calibration::Cancelled: return F("cancelled");
    case Calibration::Failed: return F("failed");
    default: return F("none");
  }
}
const __FlashStringHelper *warningName(bool paused) {
  return paused ? F("direction_pause") : F("none");
}
}
