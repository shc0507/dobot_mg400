#pragma once

// Timer1 owns pulse timing. Serial output stays in the foreground.
#include <Arduino.h>
#include <stdint.h>
#include "FocusConfig.h"
#ifdef __AVR__
#include <avr/interrupt.h>
#include <util/atomic.h>
#else
#define ATOMIC_RESTORESTATE 0
#define ATOMIC_BLOCK(mode) for (bool atomic_once = true; atomic_once; atomic_once = false)
#endif

const uint32_t TIMER_HZ = 20000UL;
const uint16_t TIMER_TICK_US = 50;
const uint16_t ACCELERATION_SPS2 = 2000;
const uint16_t START_SPEED_SPS = 100;
const uint32_t FOCUS_IDLE_DISABLE_MS = 30000UL;
const uint32_t IDLE_SLEEP_MS = 120000UL;
const uint16_t WAKE_DELAY_US = 1000;
const uint32_t MIN_SPEED_SPS = 1;
const uint32_t MAX_SPEED_SPS = 10000;

struct MotorState {
  const char* name;
  uint8_t dirPin, stepPin, enPin, sleepPin;
  volatile bool enabled = false, moving = false, directionPositive = true;
  volatile bool sleeping = true, pulseHigh = false, donePending = false;
  volatile uint32_t remainingSteps = 0;
  volatile uint16_t speedSps = 400, velocitySps = 0;
  volatile uint32_t phase = 0, lastActivityMs = 0;
  volatile int32_t pulsePosition = 0, completedPosition = 0;
  MotorState(const char* label, uint8_t dir, uint8_t step, uint8_t enable,
             uint8_t sleep, uint16_t speed)
      : name(label), dirPin(dir), stepPin(step), enPin(enable), sleepPin(sleep),
        speedSps(speed) {}
};

struct AxisSnapshot {
  const char* name;
  bool enabled, moving, sleeping;
  uint16_t speed;
  uint32_t remaining;
  int32_t position;
  explicit AxisSnapshot(const MotorState &m)
      : name(m.name), enabled(m.enabled), moving(m.moving), sleeping(m.sleeping),
        speed(m.speedSps), remaining(m.remainingSteps), position(m.pulsePosition) {}
};

MotorState objective("OBJ", 8, 9, 10, 6, 400);
MotorState focus("FOCUS", 11, 12, 13, 7, 800);
volatile bool focusHoming = false;
volatile uint8_t accelerationTick = 0;

// Call with interrupts masked. A raw STOP removes pulses, not holding current.
void stopMotorState(MotorState &m) {
  m.moving = false;
  m.remainingSteps = 0;
  m.phase = 0;
  m.velocitySps = 0;
  m.donePending = false;
  m.pulseHigh = false;
  m.lastActivityMs = millis();
  digitalWrite(m.stepPin, LOW);
}

void beginMotorState(MotorState &m, int32_t distance) {
  m.directionPositive = distance > 0;
  digitalWrite(m.dirPin, m.directionPositive ? HIGH : LOW);
  m.remainingSteps = distance > 0 ? uint32_t(distance) : uint32_t(-(distance + 1)) + 1UL;
  m.phase = 0;
  m.velocitySps = m.speedSps < START_SPEED_SPS ? m.speedSps : START_SPEED_SPS;
  m.donePending = false;
  m.lastActivityMs = millis();
  m.moving = true;
}

void rampMotor(MotorState &m) {
  if (!m.moving) return;
  uint16_t target = m.speedSps;
  const uint32_t velocity = m.velocitySps;
  const uint16_t floorSpeed = target < START_SPEED_SPS ? target : START_SPEED_SPS;
  // Avoid division in the interrupt and bound the multiplication to 32 bits.
  const uint32_t remaining = m.remainingSteps;
  if (remaining <= 25000UL && (remaining - 1UL) * (2UL * ACCELERATION_SPS2) <
      velocity * velocity && target > floorSpeed) target = floorSpeed;
  const uint16_t increment = ACCELERATION_SPS2 / 1000;
  if (m.velocitySps < target) {
    const uint16_t increased = m.velocitySps + increment;
    m.velocitySps = increased < target ? increased : target;
  } else if (m.velocitySps > target) {
    m.velocitySps = m.velocitySps - target > increment ? m.velocitySps - increment : target;
  }
}

void setStep(MotorState &m, bool high) {
#ifdef __AVR__
  const uint8_t mask = m.stepPin == 9 ? _BV(PB1) : _BV(PB4);
  if (high) PORTB |= mask;
  else PORTB &= ~mask;
#else
  digitalWrite(m.stepPin, high ? HIGH : LOW);
#endif
}

void pulseMotor(MotorState &m, bool paused) {
  // Pulse width is one timer tick, even when the foreground is busy printing.
  if (m.pulseHigh) {
    setStep(m, false);
    m.pulseHigh = false;
  }
  if (!m.enabled || m.sleeping || !m.moving || paused) return;
  m.phase += m.velocitySps;
  if (m.phase < TIMER_HZ) return;
  m.phase -= TIMER_HZ;
  setStep(m, true);
  m.pulseHigh = true;
  m.pulsePosition += m.directionPositive ? 1 : -1;
  --m.remainingSteps;
  if (!m.remainingSteps) {
    m.moving = false;
    m.completedPosition = m.pulsePosition;
    m.donePending = true;
    m.lastActivityMs = millis();
  }
}

void motionTimerTick() {
  bool paused = false;
  if (FOCUS_HOME_PIN != 255 && focusHoming)
    paused = digitalRead(FOCUS_HOME_PIN) == FOCUS_HOME_ACTIVE_LEVEL;
  if (++accelerationTick >= TIMER_HZ / 1000) {
    accelerationTick = 0;
    rampMotor(objective);
    if (!paused) rampMotor(focus);
  }
  pulseMotor(objective, false);
  pulseMotor(focus, paused);
}

#ifdef __AVR__
ISR(TIMER1_COMPA_vect) { motionTimerTick(); }
#endif

void configureMotionTimer() {
#ifdef __AVR__
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    TCCR1A = 0;
    TCCR1B = 0;
    TCNT1 = 0;
    OCR1A = F_CPU / 8UL / TIMER_HZ - 1;
    TIFR1 = _BV(OCF1A);
    TIMSK1 = _BV(OCIE1A);
    TCCR1B = _BV(WGM12) | _BV(CS11);
  }
#endif
}
