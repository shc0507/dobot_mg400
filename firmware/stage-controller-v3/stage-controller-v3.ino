#include "MotorControl.h"
#include "EncoderConfig.h"

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

// A single pending line bounds RAM and prevents stale telemetry queues. UART
// bytes are drained in small batches; response traffic never shares a line.
class TelemetryLine : public Print {
public:
  char data[192]; // Worst TEL 187 bytes; worst LIM 176, including CRLF.
  uint8_t length = 0;
  uint8_t sent = 0;
  bool overflow = false;
  size_t write(uint8_t value) override {
    if (length == sizeof(data)) { overflow = true; return 0; }
    data[length++] = static_cast<char>(value);
    if ((length & 15) == 0) MotorControl::update();
    return 1;
  }
  using Print::write;
  void reset() { length = sent = 0; overflow = false; }
};
TelemetryLine telemetryLine;
unsigned long telemetrySequence = 0;
unsigned long lastTelemetryMs = 0;
uint8_t telemetryAxis = 0;
bool telemetryLimits = false;
constexpr unsigned long telemetryIntervalMs = 33; // One axis/tick: ~99 ms per axis.

void resetTelemetry() {
  telemetryLine.reset();
  telemetrySequence = 0;
  telemetryAxis = 0;
  telemetryLimits = false;
  lastTelemetryMs = millis();
}

void drainTelemetry() {
  for (uint8_t budget = 0; budget < 16 && telemetryLine.sent < telemetryLine.length;
       ++budget) {
    if (Serial.availableForWrite() <= 0) return;
    Serial.write(static_cast<uint8_t>(telemetryLine.data[telemetryLine.sent++]));
    MotorControl::update();
  }
  if (telemetryLine.sent == telemetryLine.length) telemetryLine.reset();
}

void finishTelemetry() {
  // Called before executing a complete command. At 115200 baud this adds at
  // most one line of wire time (~18 ms), while motor/watchdog updates continue.
  while (telemetryLine.length) {
    MotorControl::update();
    drainTelemetry();
  }
}

// Keep stepping even when the serial transmit buffer fills (help/status can
// exceed its size). On this demo's Arduino UART, writes are serviced per byte.
class MotorServicedPrint : public Print {
public:
  size_t write(uint8_t value) override {
    MotorControl::update();
    while (Serial.availableForWrite() == 0) MotorControl::update();
    return Serial.write(value);
  }
  using Print::write;
};

MotorServicedPrint console;
char commandBuffer[64]; // Up to 63 characters plus the terminator; discard oversized lines atomically.
uint8_t commandLength = 0;
bool discardLine = false;

const __FlashStringHelper *motorName(MotorControl::Motor motor) {
  switch (motor) {
    case MotorControl::Motor::Bottom_Motor: return F("bottom");
    case MotorControl::Motor::Top_Motor: return F("top");
    case MotorControl::Motor::Insert_Motor: return F("insert");
  }
  return F("unknown");
}

void serviceTelemetry() {
  drainTelemetry();
  const unsigned long now = millis();
  if (telemetryLine.length || Serial.available() > 0 ||
      (!telemetryLimits && now - lastTelemetryMs < telemetryIntervalMs) ||
      Serial.availableForWrite() <= 0) return;
  if (!telemetryLimits) lastTelemetryMs = now;
  const MotorControl::Motor motor = static_cast<MotorControl::Motor>(telemetryAxis);
  const Encoders::Reading reading = Encoders::get(telemetryAxis);
  MotorControl::Status status;
  if (!MotorControl::getStatus(motor, status)) return;
  if (telemetryLimits) {
    telemetryLine.print(F("LIM "));
    telemetryLine.print(++telemetrySequence);
    telemetryLine.print(' '); telemetryLine.print(now);
    telemetryLine.print(' '); telemetryLine.print(motorName(motor));
    telemetryLine.print(F(" lower=")); telemetryLine.print(status.lowerCounts);
    telemetryLine.print(F(" upper=")); telemetryLine.print(status.upperCounts);
    telemetryLine.print(F(" bounds=")); telemetryLine.print(status.bounds);
    telemetryLine.print(F(" switch=")); telemetryLine.print(status.switchMask);
    telemetryLine.print(F(" raw=")); telemetryLine.print(status.switchRaw);
    telemetryLine.print(F(" counts=")); telemetryLine.print(status.positionCounts);
    telemetryLine.print(F(" calibration=")); telemetryLine.print(MotorControl::calibrationName(status.calibration));
    telemetryLine.print(F(" warning=")); telemetryLine.print(MotorControl::warningName(status.directionPaused));
    telemetryLine.print(F(" warnings=")); telemetryLine.println(status.directionWarnings);
    telemetryLimits = false;
    telemetryAxis = (telemetryAxis + 1) % 3;
    if (telemetryLine.overflow) telemetryLine.reset();
    else drainTelemetry();
    return;
  }
  telemetryLimits = true;
  telemetryLine.print(F("TEL "));
  telemetryLine.print(++telemetrySequence);
  telemetryLine.print(' '); telemetryLine.print(now);
  telemetryLine.print(' '); telemetryLine.print(motorName(motor));
  telemetryLine.print(status.direction == MotorControl::Direction::Clockwise ? F(" cw ") : F(" ccw "));
  telemetryLine.print(!status.rotating ? F("idle") : (status.continuous ? F("continuous") : F("moving")));
  telemetryLine.print(F(" counts=")); telemetryLine.print(status.positionCounts);
  telemetryLine.print(F(" remaining=")); telemetryLine.print(status.remainingCounts);
  telemetryLine.print(F(" speed=")); telemetryLine.print(status.measuredSpeed, 6);
  telemetryLine.print(F(" encoder=")); telemetryLine.print(Encoders::healthName(status.encoder));
  telemetryLine.print(F(" fault=")); telemetryLine.print(MotorControl::faultName(status.fault));
  telemetryLine.print(F(" raw=")); telemetryLine.print(reading.raw);
  telemetryLine.print(F(" channel=")); telemetryLine.print(reading.channel);
  telemetryLine.print(F(" magnet=")); telemetryLine.print(reading.magnetStatus);
  telemetryLine.print(F(" age=")); telemetryLine.println(now - reading.sampledAt);
  // A format expansion can never leak a partial/invalid line onto the stream.
  if (telemetryLine.overflow) telemetryLine.reset();
  else drainTelemetry();
}

void printHelp() {
  console.println(F("motor dir rev steps speed accel spr microstep hold keepalive start stop clear calibrate goto reset setlimit clearupper clearlower status enc help"));
  console.println(F("Settings: idle only. Syntax/units: firmware README."));
}

bool parseMotor(const char *text, MotorControl::Motor &motor) {
  if (!text) return false;
  if (strcmp_P(text, PSTR("bottom")) == 0 || strcmp_P(text, PSTR("1")) == 0) {
    motor = MotorControl::Motor::Bottom_Motor;
  } else if (strcmp_P(text, PSTR("top")) == 0 || strcmp_P(text, PSTR("2")) == 0) {
    motor = MotorControl::Motor::Top_Motor;
  } else if (strcmp_P(text, PSTR("insert")) == 0 || strcmp_P(text, PSTR("3")) == 0) {
    motor = MotorControl::Motor::Insert_Motor;
  } else {
    return false;
  }
  return true;
}

bool parsePositiveLong(const char *text, long &value) {
  if (!text || *text == '\0') return false;
  char *end;
  errno = 0;
  value = strtol(text, &end, 10);
  return end != text && *end == '\0' && errno != ERANGE && value > 0;
}

bool parseFiniteFloat(const char *text, float &value) {
  if (!text || *text == '\0') return false;
  char *end;
  errno = 0;
  value = static_cast<float>(strtod(text, &end));
  return end != text && *end == '\0' && errno != ERANGE && isfinite(value);
}
bool parsePositiveFloat(const char *text, float &value) {
  return parseFiniteFloat(text, value) && value > 0.0f;
}

void printStatus() {
  console.println(F("warnings=held_direction_v1"));
  console.println(F("positioning=calibrated_absolute_v1"));
  console.println(F("calibration=switch_midpoint_v1"));
  console.println(F("coordinates=ccw_positive_v1"));
  console.println(F("homing=calibration_only_v1"));
  console.println(F("switches=analog_pair_v2"));
  console.println(F("limits=directional_v2"));
  console.println(F("telemetry=positions_v1"));
  console.println(F("motion=closed_loop_v1"));
  console.print(F("microstep="));
  console.println(MotorControl::microstep());
  for (uint8_t i = 0; i < 3; ++i) {
    const MotorControl::Motor motor = static_cast<MotorControl::Motor>(i);
    MotorControl::Status status;
    if (!MotorControl::getStatus(motor, status)) continue;
    console.print(motor == MotorControl::selectedMotor() ? F("* ") : F("  "));
    console.print(motorName(motor));
    console.print(status.direction == MotorControl::Direction::Clockwise ? F(" cw ") : F(" ccw "));
    console.print(!status.rotating ? F("idle") : (status.continuous ? F("continuous") : F("moving")));
    console.print(F(" pos="));
    console.print(status.position, 6);
    console.print(F(" remaining="));
    console.print(status.remaining, 6);
    console.print(F(" speed="));
    console.print(status.speed, 6);
    console.print(F(" accel="));
    console.print(status.acceleration, 6);
    console.print(F(" spr="));
    console.print(status.stepsPerRevolution);
    console.print(F(" encoder="));
    console.print(Encoders::healthName(status.encoder));
    console.print(F(" fault="));
    console.print(MotorControl::faultName(status.fault));
    console.print(F(" tolerance="));
    console.print(status.tolerance, 6);
    console.print(F(" max_speed="));
    console.print(status.maxSpeed, 6);
    console.print(F(" measured_speed="));
    console.print(status.measuredSpeed, 6);
    console.print(F(" lower=")); console.print(status.lowerCounts);
    console.print(F(" upper=")); console.print(status.upperCounts);
    console.print(F(" bounds=")); console.print(status.bounds);
    console.print(F(" switch=")); console.print(status.switchMask);
    console.print(F(" switch_raw=")); console.print(status.switchRaw);
    console.print(F(" calibration=")); console.print(MotorControl::calibrationName(status.calibration));
    console.print(F(" warning=")); console.print(MotorControl::warningName(status.directionPaused));
    console.print(F(" warnings=")); console.println(status.directionWarnings);
  }
}

void printEncoders() {
  // Observations only: no fault clear, position reset, or motor movement.
  console.print(F("ENC mux_address="));
  console.println(EncoderConfig::MuxAddress);
  for (uint8_t i = 0; i < 3; ++i) {
    const Encoders::Reading r = Encoders::get(i);
    console.print(F("ENC "));
    console.print(motorName(static_cast<MotorControl::Motor>(i)));
    console.print(F(" channel=")); console.print(r.channel);
    console.print(F(" raw=")); console.print(r.raw);
    console.print(F(" angle_deg=")); console.print(r.raw * (360.0f / 4096.0f), 2);
    console.print(F(" magnet="));
    if (r.magnetStatus == 255) console.print(F("unknown"));
    else if (!(r.magnetStatus & 0x20)) console.print(F("missing"));
    else if (r.magnetStatus & 0x10) console.print(F("weak"));
    else if (r.magnetStatus & 0x08) console.print(F("strong"));
    else console.print(F("ok"));
    console.print(F(" health=")); console.print(Encoders::healthName(r.health));
    console.print(F(" counts=")); console.print(-r.counts);
    console.print(F(" age_ms=")); console.println(millis() - r.sampledAt);
  }
}

void handleCommand(char *line) {
  finishTelemetry();
  for (char *cursor = line; *cursor; ++cursor) {
    *cursor = static_cast<char>(tolower(static_cast<unsigned char>(*cursor)));
  }
  char *command = strtok(line, " \t");
  if (!command) return;
  char *argument = strtok(NULL, " \t");
  if (strtok(NULL, " \t")) {
    console.println(F("ERROR: Too many arguments."));
    return;
  }

  bool ok = false;
  long integerValue;
  float floatValue;
  MotorControl::Motor motor;

  if (strcmp_P(command, PSTR("help")) == 0 && !argument) {
    printHelp();
    return;
  } else if (strcmp_P(command, PSTR("status")) == 0 && !argument) {
    printStatus();
    return;
  } else if (strcmp_P(command, PSTR("enc")) == 0 && !argument) {
    printEncoders();
    return;
  } else if (strcmp_P(command, PSTR("microstep")) == 0) {
    if (!argument) {
      console.print(F("microstep="));
      console.println(MotorControl::microstep());
      return;
    }
    if (parsePositiveLong(argument, integerValue) && integerValue <= 16L &&
        MotorControl::setMicrostep(static_cast<uint8_t>(integerValue))) {
      console.print(F("OK: microstep "));
      console.println(MotorControl::microstep());
    } else {
      console.println(F("ERROR: All stages use fixed eighth stepping; idle only."));
    }
    return;
  } else if (strcmp_P(command, PSTR("motor")) == 0) {
    ok = parseMotor(argument, motor) && MotorControl::selectMotor(motor);
  } else if (strcmp_P(command, PSTR("dir")) == 0 && argument) {
    if (strcmp_P(argument, PSTR("cw")) == 0) {
      ok = MotorControl::setDirection(MotorControl::Direction::Clockwise);
    } else if (strcmp_P(argument, PSTR("ccw")) == 0) {
      ok = MotorControl::setDirection(MotorControl::Direction::Counterclockwise);
    }
  } else if (strcmp_P(command, PSTR("steps")) == 0) {
    ok = parsePositiveLong(argument, integerValue) && MotorControl::rotateSteps(integerValue);
  } else if (strcmp_P(command, PSTR("rev")) == 0) {
    ok = !argument ? MotorControl::rotateOneRevolution() :
         (parsePositiveFloat(argument, floatValue) && MotorControl::rotateRevolutions(floatValue));
  } else if (strcmp_P(command, PSTR("goto")) == 0) {
    ok = parseFiniteFloat(argument, floatValue) && !strstr(argument, "0x") &&
         MotorControl::goToPosition(floatValue);
  } else if (strcmp_P(command, PSTR("start")) == 0 && !argument) {
    ok = MotorControl::startRotation();
  } else if (strcmp_P(command, PSTR("calibrate")) == 0 && !argument) {
    ok = MotorControl::calibrate();
  } else if (strcmp_P(command, PSTR("hold")) == 0 && !argument) {
    ok = MotorControl::startHold();
  } else if (strcmp_P(command, PSTR("keepalive")) == 0 && !argument) {
    ok = MotorControl::keepHoldAlive();
  } else if (strcmp_P(command, PSTR("clear")) == 0 && !argument) {
    ok = MotorControl::clearFault();
  } else if (strcmp_P(command, PSTR("reset")) == 0 && !argument) {
    ok = MotorControl::resetPosition();
  } else if (strcmp_P(command, PSTR("setlimit")) == 0 && !argument) {
    ok = MotorControl::setLimit();
  } else if (strcmp_P(command, PSTR("clearupper")) == 0 && !argument) {
    ok = MotorControl::clearUpperLimit();
  } else if (strcmp_P(command, PSTR("clearlower")) == 0 && !argument) {
    ok = MotorControl::clearLowerLimit();
  } else if (strcmp_P(command, PSTR("stop")) == 0) {
    if (!argument) {
      MotorControl::stopRotation();
      ok = true;
    } else if (strcmp_P(argument, PSTR("all")) == 0) {
      MotorControl::stopAll();
      console.println(F("OK: All motors stopped."));
      return;
    }
  } else if (strcmp_P(command, PSTR("speed")) == 0) {
    ok = parsePositiveFloat(argument, floatValue) && MotorControl::setSpeed(floatValue);
  } else if (strcmp_P(command, PSTR("accel")) == 0) {
    ok = parsePositiveFloat(argument, floatValue) && MotorControl::setAcceleration(floatValue);
  } else if (strcmp_P(command, PSTR("spr")) == 0) {
    ok = parsePositiveLong(argument, integerValue) && integerValue <= 65535L &&
         MotorControl::setStepsPerRevolution(static_cast<uint16_t>(integerValue));
  }

  if (ok) {
    console.print(F("OK: "));
    console.print(motorName(MotorControl::selectedMotor()));
    console.print(' ');
    console.println(command);
  } else {
    console.println(F("ERROR: Check value/limits/switch, idle, encoder/fault. Goto needs calibration. See status/help."));
  }
}

void readSerialCommands() {
  // Fixed buffer, bounded work, and no parseFloat()/readStringUntil() timeouts.
  for (uint8_t received = 0; received < 16 && Serial.available() > 0; ++received) {
    const char character = static_cast<char>(Serial.read());
    if (character == '\n' || character == '\r') {
      if (discardLine) {
        finishTelemetry();
        console.println(F("ERROR: Bad/long line; discarded."));
      } else if (commandLength > 0) {
        commandBuffer[commandLength] = '\0';
        handleCommand(commandBuffer);
      }
      commandLength = 0;
      discardLine = false;
    } else if (!discardLine) {
      // Discard the entire line on non-text input rather than executing a
      // truncated command (e.g. a command containing an embedded NUL byte).
      if (static_cast<unsigned char>(character) < 32 && character != '\t') {
        discardLine = true;
      } else if (commandLength < sizeof(commandBuffer) - 1) {
        commandBuffer[commandLength++] = character;
      } else {
        discardLine = true;
      }
    }
    MotorControl::update();
  }
}

void setup() {
  Serial.begin(115200);
  MotorControl::begin();
  console.println(F("Stage closed-loop firmware ready. Serial: 115200 baud, Newline or Both NL & CR."));
  printHelp();
  resetTelemetry();
}

void loop() {
  MotorControl::update();
  readSerialCommands();
  serviceTelemetry();
}
