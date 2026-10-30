#include "MotorControl.h"

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

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
char commandBuffer[96];
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

void printHelp() {
  console.println(F("Commands (one per line, case insensitive):"));
  console.println(F("  motor bottom|top|insert  (or 1|2|3)"));
  console.println(F("  dir cw|ccw              Set direction while idle"));
  console.println(F("  steps 200               Move positive STEP pulse count"));
  console.println(F("  rev [1.0]               Move revolutions; default 1"));
  console.println(F("  start                   Continuous constant-speed rotation"));
  console.println(F("  hold                    Constant speed; stops after 1s without keepalive"));
  console.println(F("  keepalive               Renew selected motor's active hold"));
  console.println(F("  stop [all]              Immediately stop selected motor/all"));
  console.println(F("  speed 200               1..1000 STEP pulses/s, while idle"));
  console.println(F("  accel 50                1..10000 STEP pulses/s^2, while idle"));
  console.println(F("  spr 200                 1..65535 full steps/rev, while idle"));
  console.println(F("  microstep [1|2|4|8|16]  Query/set global resolution; all motors idle"));
  console.println(F("  home                    Seek minimum, back off, approach slowly"));
  console.println(F("  soft <min> <max>        Inclusive steps from home; idle and homed only"));
  console.println(F("  awake <ms>              Renew idle holding for 0..60000 ms; 0 cancels"));
  console.println(F("  status                  Show all motors"));
  console.println(F("  help"));
  console.println(F("Select/stop a busy motor before changing its direction or settings."));
}

bool parseMotor(const char *text, MotorControl::Motor &motor) {
  if (!text) return false;
  if (strcmp(text, "bottom") == 0 || strcmp(text, "1") == 0) {
    motor = MotorControl::Motor::Bottom_Motor;
  } else if (strcmp(text, "top") == 0 || strcmp(text, "2") == 0) {
    motor = MotorControl::Motor::Top_Motor;
  } else if (strcmp(text, "insert") == 0 || strcmp(text, "3") == 0) {
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

bool parseLong(const char *text, long &value) {
  if (!text || *text == '\0') return false;
  char *end;
  errno = 0;
  value = strtol(text, &end, 10);
  return end != text && *end == '\0' && errno != ERANGE;
}

bool parsePositiveFloat(const char *text, float &value) {
  if (!text || *text == '\0') return false;
  char *end;
  errno = 0;
  value = static_cast<float>(strtod(text, &end));
  return end != text && *end == '\0' && errno != ERANGE && isfinite(value) && value > 0.0f;
}

void printStatus() {
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
    console.print(status.position);
    console.print(F(" remaining="));
    console.print(status.remainingSteps);
    console.print(F(" speed="));
    console.print(status.speed, 1);
    console.print(F(" accel="));
    console.print(status.acceleration, 1);
    console.print(F(" spr="));
    console.print(status.stepsPerRevolution);
    console.print(F(" homed="));
    console.print(status.homed ? 1 : 0);
    console.print(F(" limits="));
    console.print(status.limitMin ? 1 : 0);
    console.print(status.limitMax ? 1 : 0);
    console.print(F(" soft="));
    console.print(status.soft ? 1 : 0);
    console.print(F(" awake="));
    console.println(status.awake ? 1 : 0);
  }
}

void handleCommand(char *line) {
  for (char *cursor = line; *cursor; ++cursor) {
    *cursor = static_cast<char>(tolower(static_cast<unsigned char>(*cursor)));
  }
  char *command = strtok(line, " \t");
  if (!command) return;
  char *argument = strtok(NULL, " \t");
  char *second = strtok(NULL, " \t");
  if ((second && strcmp(command, "soft") != 0) || strtok(NULL, " \t")) {
    console.println(F("ERROR: Too many arguments; type help."));
    return;
  }

  bool ok = false;
  long integerValue;
  long secondValue;
  float floatValue;
  MotorControl::Motor motor;

  if (strcmp(command, "help") == 0 && !argument) {
    printHelp();
    return;
  } else if (strcmp(command, "status") == 0 && !argument) {
    printStatus();
    return;
  } else if (strcmp(command, "microstep") == 0) {
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
      console.println(F("ERROR: microstep requires 1, 2, 4, 8, or 16 and all motors idle."));
    }
    return;
  } else if (strcmp(command, "home") == 0 && !argument) {
    const MotorControl::HomeError error = MotorControl::home();
    if (error != MotorControl::HomeError::None) {
      console.print(F("ERROR: home failed: "));
      console.println(MotorControl::homeErrorText(error));
      return;
    }
    ok = true;
  } else if (strcmp(command, "soft") == 0) {
    ok = parseLong(argument, integerValue) && parseLong(second, secondValue) &&
         MotorControl::setSoftLimits(integerValue, secondValue);
  } else if (strcmp(command, "awake") == 0) {
    ok = parseLong(argument, integerValue) && integerValue >= 0 && integerValue <= 60000L &&
         MotorControl::keepAwake(static_cast<unsigned long>(integerValue));
  } else if (strcmp(command, "motor") == 0) {
    ok = parseMotor(argument, motor) && MotorControl::selectMotor(motor);
  } else if (strcmp(command, "dir") == 0 && argument) {
    if (strcmp(argument, "cw") == 0) {
      ok = MotorControl::setDirection(MotorControl::Direction::Clockwise);
    } else if (strcmp(argument, "ccw") == 0) {
      ok = MotorControl::setDirection(MotorControl::Direction::Counterclockwise);
    }
  } else if (strcmp(command, "steps") == 0) {
    ok = parsePositiveLong(argument, integerValue) && MotorControl::rotateSteps(integerValue);
  } else if (strcmp(command, "rev") == 0) {
    ok = !argument ? MotorControl::rotateOneRevolution() :
         (parsePositiveFloat(argument, floatValue) && MotorControl::rotateRevolutions(floatValue));
  } else if (strcmp(command, "start") == 0 && !argument) {
    ok = MotorControl::startRotation();
  } else if (strcmp(command, "hold") == 0 && !argument) {
    ok = MotorControl::startHold();
  } else if (strcmp(command, "keepalive") == 0 && !argument) {
    ok = MotorControl::keepHoldAlive();
  } else if (strcmp(command, "stop") == 0) {
    if (!argument) {
      MotorControl::stopRotation();
      ok = true;
    } else if (strcmp(argument, "all") == 0) {
      MotorControl::stopAll();
      console.println(F("OK: All motors stopped."));
      return;
    }
  } else if (strcmp(command, "speed") == 0) {
    ok = parsePositiveFloat(argument, floatValue) && MotorControl::setSpeed(floatValue);
  } else if (strcmp(command, "accel") == 0) {
    ok = parsePositiveFloat(argument, floatValue) && MotorControl::setAcceleration(floatValue);
  } else if (strcmp(command, "spr") == 0) {
    ok = parsePositiveLong(argument, integerValue) && integerValue <= 65535L &&
         MotorControl::setStepsPerRevolution(static_cast<uint16_t>(integerValue));
  }

  if (ok) {
    console.print(F("OK: "));
    console.print(motorName(MotorControl::selectedMotor()));
    console.print(' ');
    console.println(command);
  } else {
    console.println(F("ERROR: Bad command/value, range exceeded, or motor busy. Type help; stop first if busy."));
  }
}

void printHomeErrors() {
  // update() runs during every printed byte. Report deferred failures only from
  // complete reply boundaries, never inside an acknowledgement/status line.
  // Printing one axis can fail a previously scanned axis. Drain before accepting
  // another command: each of the three homes can fail only once without a command.
  bool printed;
  do {
    printed = false;
    for (uint8_t i = 0; i < 3; ++i) {
      const MotorControl::Motor motor = static_cast<MotorControl::Motor>(i);
      const MotorControl::HomeError error = MotorControl::takeHomeError(motor);
      if (error == MotorControl::HomeError::None) continue;
      printed = true;
      console.print(F("ERROR: home failed: "));
      console.print(motorName(motor));
      console.print(' ');
      console.println(MotorControl::homeErrorText(error));
    }
  } while (printed);
}

void readSerialCommands() {
  // Fixed buffer, bounded work, and no parseFloat()/readStringUntil() timeouts.
  for (uint8_t received = 0; received < 16 && Serial.available() > 0; ++received) {
    const char character = static_cast<char>(Serial.read());
    if (character == '\n' || character == '\r') {
      printHomeErrors();
      if (discardLine) {
        console.println(F("ERROR: Invalid or oversized line; discarded."));
      } else if (commandLength > 0) {
        commandBuffer[commandLength] = '\0';
        handleCommand(commandBuffer);
      }
      printHomeErrors();
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
  console.println(F("Three A4988 motors ready. Serial: 115200 baud, Newline or Both NL & CR."));
  console.println(F("Default: bottom, CW, 200 full steps/rev, 200 steps/s, 50 steps/s^2."));
  console.println(F("Global mode: full step. MS1=D10, MS2=D9, MS3=D8."));
  printHelp();
}

void loop() {
  MotorControl::update();
  printHomeErrors();
  readSerialCommands();
  printHomeErrors();
}
