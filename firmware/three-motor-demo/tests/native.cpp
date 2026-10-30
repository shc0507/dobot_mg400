// Compile only on the host. No serial API, device file, or network is used.
#include "Arduino.h"
#include "../MotorPins.h"
#include "../MotorControl.h"

#include <cassert>
#include <climits>
#include <cstring>
#include <iostream>

namespace {
unsigned long clockMs = 0, clockUs = 0, fractionUs = 0;
uint8_t outputs[20] = {}, inputs[20] = {}, modes[20] = {};
long physical[3] = {};
unsigned long pulses[3] = {};
bool automaticMin[3] = {};
const MotorPins::PinPair pairs[] = {MotorPins::Bottom, MotorPins::Top, MotorPins::Insert};
const uint8_t minimums[] = {MotorPins::BottomLimitMin, MotorPins::TopLimitMin,
                           MotorPins::InsertLimitMin};
}
FakeSerial Serial;
unsigned long millis() { return clockMs; }
unsigned long micros() { return clockUs; }
void delayMicroseconds(unsigned int delay) {
  clockUs += delay;
  fractionUs += delay;
  clockMs += fractionUs / 1000;
  fractionUs %= 1000;
}
void pinMode(uint8_t pin, uint8_t mode) {
  assert(pin < 20);
  modes[pin] = mode;
}
void digitalWrite(uint8_t pin, uint8_t level) {
  assert(pin < 20);
  for (uint8_t i = 0; i < 3; ++i) {
    if (pin == pairs[i].step && level == HIGH && outputs[pin] == LOW) {
      ++pulses[i];
      physical[i] += outputs[pairs[i].dir] == HIGH ? 1 : -1;
    }
  }
  outputs[pin] = level;
}
int digitalRead(uint8_t pin) {
  assert(pin < 20);
  for (uint8_t i = 0; i < 3; ++i) {
    if (automaticMin[i] && pin == minimums[i]) return physical[i] <= 0 ? LOW : HIGH;
  }
  return inputs[pin];
}

#include "../three-motor-demo.ino"

using MotorControl::Direction;
using MotorControl::HomeError;
using MotorControl::Motor;
const Motor bottomMotor = Motor::Bottom_Motor;

MotorControl::Status status(Motor motor = bottomMotor) {
  MotorControl::Status value;
  assert(MotorControl::getStatus(motor, value));
  return value;
}
void advance(unsigned long milliseconds) {
  for (unsigned long i = 0; i < milliseconds * 10; ++i) {
    delayMicroseconds(100);
    MotorControl::update();
  }
}
void reset() {
  clockMs = clockUs = fractionUs = 0;
  std::fill(inputs, inputs + 20, HIGH);
  std::fill(outputs, outputs + 20, LOW);
  std::fill(modes, modes + 20, 0);
  for (uint8_t i = 0; i < 3; ++i) {
    physical[i] = 50;
    pulses[i] = 0;
    automaticMin[i] = false;
  }
  Serial.input.clear();
  Serial.output.clear();
  Serial.onWrite = nullptr;
  commandLength = 0;
  discardLine = false;
  MotorControl::begin();
}
void finish(Motor motor = bottomMotor) {
  for (unsigned long i = 0; i <= MotorPins::HomeTimeoutMs + 100; ++i) {
    if (!MotorControl::isRotating(motor)) return;
    advance(1);
  }
  assert(false && "motion failed to terminate within its bound");
}
void command(const char *line) {
  char buffer[96];
  assert(std::strlen(line) < sizeof(buffer));
  std::strcpy(buffer, line);
  Serial.output.clear();
  handleCommand(buffer);
}
void homeAxis(uint8_t index) {
  const Motor motor = static_cast<Motor>(index);
  assert(MotorControl::selectMotor(motor));
  automaticMin[index] = true;
  assert(MotorControl::home() == HomeError::None);
  finish(motor);
  assert(status(motor).homed && status(motor).position == 0);
  assert(physical[index] == 0);
}
void ordinarySpeedRestored() {
  assert(MotorControl::setDirection(Direction::Clockwise));
  const unsigned long before = pulses[0];
  assert(MotorControl::startRotation());
  advance(100);
  assert(pulses[0] - before >= 90); // Configured1000pps, not temporary homing100pps.
  MotorControl::stopRotation();
}

void limitsAndLegacyHold() {
  reset();
  assert(!status().homed && !status().soft && !status().awake);
  for (uint8_t pin = A0; pin <= A5; ++pin) assert(modes[pin] == INPUT_PULLUP);
  inputs[MotorPins::BottomLimitMax] = LOW;
  assert(!MotorControl::rotateSteps(5) && !MotorControl::startHold());
  assert(MotorControl::setDirection(Direction::Counterclockwise));
  assert(MotorControl::rotateSteps(5));
  finish();
  assert(status().position == -5); // Motion away from a pressed limit remains possible.

  reset();
  assert(MotorControl::rotateSteps(1000));
  advance(100);
  const unsigned long before = pulses[0];
  inputs[MotorPins::BottomLimitMax] = LOW;
  MotorControl::update();
  assert(!status().rotating && pulses[0] == before && !status().limitMax);
  advance(5);
  assert(status().limitMax);
  inputs[MotorPins::BottomLimitMax] = HIGH;
  MotorControl::update();
  advance(4);
  assert(!MotorControl::rotateSteps(1));
  advance(1);
  assert(!status().limitMax && MotorControl::rotateSteps(1));
  finish();

  assert(MotorControl::setDirection(Direction::Counterclockwise));
  assert(MotorControl::startHold());
  advance(100);
  const unsigned long held = pulses[0];
  inputs[MotorPins::BottomLimitMin] = LOW;
  MotorControl::update();
  assert(!status().rotating && pulses[0] == held && !MotorControl::keepHoldAlive());
  reset();
  assert(MotorControl::startHold());
  advance(1000);
  assert(!status().rotating && !MotorControl::keepHoldAlive());

  reset();
  inputs[MotorPins::BottomLimitMax] = LOW;
  MotorControl::update();
  advance(1);
  inputs[MotorPins::BottomLimitMax] = HIGH;
  MotorControl::update();
  assert(!status().limitMax && !MotorControl::rotateSteps(1));
  advance(4);
  assert(!MotorControl::rotateSteps(1));
  advance(1);
  assert(MotorControl::rotateSteps(1));
  finish(); // Even a contact shorter than debounce needs a stable release.

  assert(MotorControl::rotateSteps(5));
  finish();
  assert(!MotorControl::rotateSteps(LONG_MAX));
  assert(MotorControl::setDirection(Direction::Counterclockwise));
  assert(MotorControl::rotateSteps(20));
  finish();
  assert(status().position < 0 && !MotorControl::rotateSteps(LONG_MAX));
}

void homingAndSoftBounds() {
  reset();
  assert(!MotorControl::setSoftLimits(0, 100));
  assert(MotorControl::setSpeed(1000));
  homeAxis(0);
  assert(pulses[0] == 450); // 50 seek +200 backoff +200 slow approach.
  assert(MotorControl::takeHomeError(bottomMotor) == HomeError::None);
  ordinarySpeedRestored();
  assert(MotorControl::home() == HomeError::None);
  assert(!status().homed && !status().soft);
  finish();
  assert(MotorControl::setSoftLimits(5, 10));
  assert(!MotorControl::setSoftLimits(11, 10));
  assert(MotorControl::setDirection(Direction::Clockwise));
  assert(!MotorControl::rotateSteps(11));
  assert(MotorControl::rotateSteps(10));
  finish();
  assert(status().position == 10 && !MotorControl::startHold());
  assert(MotorControl::setDirection(Direction::Counterclockwise));
  assert(MotorControl::startHold());
  finish();
  assert(status().position == 5 && !status().limitMin);
  assert(MotorControl::home() == HomeError::None);
  assert(!status().soft && !status().homed);
  MotorControl::stopAll();
  assert(MotorControl::takeHomeError(bottomMotor) == HomeError::Cancelled);
  ordinarySpeedRestored();

  reset();
  for (uint8_t i = 0; i < 3; ++i) {
    homeAxis(i);
    assert(MotorControl::setSoftLimits(0, 100));
  }
  assert(MotorControl::setMicrostep(1));
  for (uint8_t i = 0; i < 3; ++i) assert(status(static_cast<Motor>(i)).homed);
  const unsigned long before = pulses[0] + pulses[1] + pulses[2];
  assert(MotorControl::setMicrostep(16));
  for (uint8_t i = 0; i < 3; ++i) {
    const auto value = status(static_cast<Motor>(i));
    assert(!value.homed && !value.soft && value.position == 0);
  }
  assert(before == pulses[0] + pulses[1] + pulses[2]);
}

void homingFailures() {
  reset();
  assert(MotorControl::setSpeed(1000));
  assert(MotorControl::home() == HomeError::None);
  finish(); // Unconnected minimum never engages.
  assert(MotorControl::takeHomeError(bottomMotor) == HomeError::StepLimit);
  assert(pulses[0] == MotorPins::HomeStepLimit && !status().homed);
  ordinarySpeedRestored();

  reset();
  assert(MotorControl::home() == HomeError::None);
  clockMs += MotorPins::HomeTimeoutMs;
  clockUs += 2000;
  MotorControl::update();
  assert(MotorControl::takeHomeError(bottomMotor) == HomeError::Timeout);
  assert(!status().rotating && !status().homed && pulses[0] == 0);

  reset();
  inputs[MotorPins::BottomLimitMin] = LOW;
  assert(MotorControl::home() == HomeError::None);
  finish();
  assert(MotorControl::takeHomeError(bottomMotor) == HomeError::NotReleased);
  assert(pulses[0] == MotorPins::HomeBackoffSteps && !status().homed);

  reset();
  physical[0] = 0;
  automaticMin[0] = true;
  assert(MotorControl::home() == HomeError::None);
  advance(200);
  inputs[MotorPins::BottomLimitMax] = LOW;
  MotorControl::update();
  assert(MotorControl::takeHomeError(bottomMotor) == HomeError::Maximum);
  assert(!status().homed);

  reset();
  inputs[MotorPins::BottomLimitMin] = inputs[MotorPins::BottomLimitMax] = LOW;
  assert(MotorControl::home() == HomeError::None);
  advance(3);
  assert(MotorControl::takeHomeError(bottomMotor) == HomeError::BothLimits);
  assert(pulses[0] == 0);
}

void awakeTimers() {
  reset();
  advance(11000);
  assert(!MotorControl::keepAwake(60001));
  assert(MotorControl::keepAwake(1000));
  assert(status().awake && !status(Motor::Top_Motor).awake);
  advance(900);
  assert(MotorControl::keepAwake(1000));
  advance(900);
  assert(status().awake);
  advance(101);
  assert(!status().awake);
  assert(MotorControl::keepAwake(60000));
  assert(MotorControl::keepAwake(0));
  MotorControl::update();
  assert(!status().awake);

  assert(MotorControl::keepAwake(20000));
  assert(MotorControl::startRotation());
  advance(21000);
  MotorControl::stopRotation();
  advance(11000);
  assert(status().awake);
  advance(9001);
  assert(!status().awake);

  reset();
  clockMs = ULONG_MAX - 500;
  assert(MotorControl::keepAwake(1000));
  advance(900);
  assert(status().awake);
  advance(101);
  assert(!status().awake); // Unsigned deadline elapsed across millis() wrap.
}

void protocolCompatibility() {
  reset();
  command("MoToR top");
  assert(Serial.output == "OK: top motor\r\n");
  command("dir ccw");
  assert(Serial.output == "OK: top dir\r\n");
  command("speed 1 2");
  assert(Serial.output == "ERROR: Too many arguments; type help.\r\n");
  command("microstep 2");
  assert(Serial.output == "OK: microstep 2\r\n");
  command("stop all");
  assert(Serial.output == "OK: All motors stopped.\r\n");
  command("status");
  assert(Serial.output.find("microstep=2\r\n") == 0);
  assert(Serial.output.find("* top ccw idle pos=0 remaining=0 speed=200.0 accel=50.0 spr=200 homed=0 limits=00 soft=0 awake=0\r\n") != std::string::npos);
  command("motor bottom");
  command("home");
  assert(Serial.output == "OK: bottom home\r\n");
  command("stop");
  assert(Serial.output == "OK: bottom stop\r\n");
  printHomeErrors();
  assert(Serial.output == "OK: bottom stop\r\nERROR: home failed: bottom cancelled\r\n");
  command("awake 0");
  assert(Serial.output == "OK: bottom awake\r\n");
  command("awake -1");
  assert(Serial.output.find("ERROR:") == 0);
  command("soft 0 100");
  assert(Serial.output.find("ERROR:") == 0);

  command("home");
  Serial.output.clear();
  Serial.input = "stop\nhome\n";
  readSerialCommands();
  assert(Serial.output == "OK: bottom stop\r\nERROR: home failed: bottom cancelled\r\nOK: bottom home\r\n");
  Serial.output.clear();
  clockMs += MotorPins::HomeTimeoutMs;
  clockUs += 2000;
  Serial.input = "home\n";
  loop();
  assert(Serial.output == "ERROR: home failed: bottom timeout\r\nOK: bottom home\r\n");
  Serial.onWrite = []() {
    if (Serial.output.size() == 25) {
      clockMs += MotorPins::HomeTimeoutMs;
      clockUs += 2000;
    }
  };
  command("status");
  Serial.onWrite = nullptr;
  assert(Serial.output.find("ERROR:") == std::string::npos);
  assert(std::count(Serial.output.begin(), Serial.output.end(), '\n') == 4);
  printHomeErrors();
  assert(Serial.output.find("\r\nERROR: home failed: bottom timeout\r\n") != std::string::npos);

  reset();
  command("motor insert");
  assert(MotorControl::home() == HomeError::None);
  MotorControl::stopRotation();
  MotorControl::selectMotor(bottomMotor);
  assert(MotorControl::home() == HomeError::None);
  Serial.output.clear();
  Serial.input = "home\n";
  Serial.onWrite = []() {
    if (Serial.output.size() == 1) {
      clockMs += MotorPins::HomeTimeoutMs;
      clockUs += 2000;
    }
  };
  readSerialCommands();
  Serial.onWrite = nullptr;
  assert(Serial.output == "ERROR: home failed: insert cancelled\r\nERROR: home failed: bottom timeout\r\nOK: bottom home\r\n");
}

int main() {
  if (MotorPins::BottomLimitMin == 255) {
    reset();
    assert(MotorControl::home() == HomeError::NoMinimum);
    assert(!status().homed && !status().rotating && pulses[0] == 0);
    std::cout << "disabled minimum refuses homing\n";
    return 0;
  }
  limitsAndLegacyHold();
  homingAndSoftBounds();
  homingFailures();
  awakeTimers();
  protocolCompatibility();
  std::cout << "native firmware behavior checks passed\n";
}
