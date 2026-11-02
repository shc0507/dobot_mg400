/*
  Dual A4988 controller for Arduino UNO

  Objective turret A4988:
    DIR  -> D8
    STEP -> D9
    EN   -> D10

  Focus A4988:
    DIR  -> D11
    STEP -> D12
    EN   -> D13

  Shared microstep pins:
    MS3 -> D2
    MS2 -> D3
    MS1 -> D4

  Serial baud: 115200

  Commands:
    OBJ ENABLE
    OBJ DISABLE
    OBJ MOVE <signed_steps>
    OBJ SPEED <steps_per_second>

    FOCUS ENABLE
    FOCUS DISABLE
    FOCUS MOVE <signed_steps>
    FOCUS SPEED <steps_per_second>

    MICRO <1|2|4|8|16>

    STOP OBJ
    STOP FOCUS
    STOP ALL

    STATUS
    HELP

  Notes:
  - A4988 ENABLE is active LOW.
  - STEP is kept LOW when idle.
  - Motion is non-blocking, so STOP commands can be received while moving.
*/

#include <Arduino.h>
#include <string.h>
#include <stdlib.h>
#include <errno.h>
#include <stdint.h>
#include "FocusConfig.h"

// ---------- Pin assignment ----------
const uint8_t MS3_PIN = 2;
const uint8_t MS2_PIN = 3;
const uint8_t MS1_PIN = 4;

const uint8_t OBJ_DIR_PIN  = 8;
const uint8_t OBJ_STEP_PIN = 9;
const uint8_t OBJ_EN_PIN   = 10;

const uint8_t FOCUS_DIR_PIN  = 11;
const uint8_t FOCUS_STEP_PIN = 12;
const uint8_t FOCUS_EN_PIN   = 13;

// ---------- Limits ----------
const unsigned long MIN_SPEED_SPS = 1;
const unsigned long MAX_SPEED_SPS = 10000;
const unsigned int STEP_PULSE_US = 3;

// ---------- Motor state ----------
struct MotorState {
  const char* name;
  uint8_t dirPin;
  uint8_t stepPin;
  uint8_t enPin;

  bool enabled;
  bool moving;
  bool directionPositive;

  unsigned long remainingSteps;
  unsigned long speedSps;
  unsigned long stepIntervalUs;
  unsigned long lastStepUs;

  long pulsePosition;
};

MotorState objective = {
  "OBJ",
  OBJ_DIR_PIN,
  OBJ_STEP_PIN,
  OBJ_EN_PIN,
  false,
  false,
  true,
  0,
  400,
  2500,
  0,
  0
};

MotorState focus = {
  "FOCUS",
  FOCUS_DIR_PIN,
  FOCUS_STEP_PIN,
  FOCUS_EN_PIN,
  false,
  false,
  true,
  0,
  800,
  1250,
  0,
  0
};

int currentMicrostep = 1;
bool focusZeroed = false;
bool focusLimited = false;
long focusMinimum = 0;
long focusMaximum = 0;
bool focusHoming = false;
bool homeSwitchSeen = false;
unsigned long homeStartedMs = 0;
unsigned long homeSwitchMs = 0;
unsigned long homeSavedSpeed = 800;

void clearFocusReference() {
  focusZeroed = false;
  focusLimited = false;
  focusMinimum = focusMaximum = 0;
}

void endFocusHome() {
  if (focusHoming) {
    focus.speedSps = homeSavedSpeed;
    focus.stepIntervalUs = 1000000UL / homeSavedSpeed;
  }
  focusHoming = false;
  homeSwitchSeen = false;
}

// ---------- Serial line buffer ----------
const size_t LINE_BUFFER_SIZE = 96;
char lineBuffer[LINE_BUFFER_SIZE];
size_t lineLength = 0;
bool discardLine = false;


// ============================================================
// Hardware helpers
// ============================================================

void setMotorEnabled(MotorState &m, bool enabled) {
  m.enabled = enabled;

  // A4988 ENABLE is active LOW
  digitalWrite(m.enPin, enabled ? LOW : HIGH);

  if (!enabled) {
    if (&m == &focus) {
      endFocusHome();
      clearFocusReference();
    }
    m.moving = false;
    m.remainingSteps = 0;
    digitalWrite(m.stepPin, LOW);
  }
}

void stopMotor(MotorState &m) {
  if (&m == &focus) endFocusHome();
  m.moving = false;
  m.remainingSteps = 0;
  digitalWrite(m.stepPin, LOW);
}

bool setMotorSpeed(MotorState &m, unsigned long speedSps) {
  if (speedSps < MIN_SPEED_SPS || speedSps > MAX_SPEED_SPS) {
    return false;
  }

  m.speedSps = speedSps;
  m.stepIntervalUs = 1000000UL / speedSps;

  // Keep enough room for the pulse itself.
  if (m.stepIntervalUs <= STEP_PULSE_US + 2) {
    m.stepIntervalUs = STEP_PULSE_US + 2;
  }

  return true;
}

bool startMove(MotorState &m, long signedSteps) {
  if (!m.enabled) {
    Serial.print(F("ERR "));
    Serial.print(m.name);
    Serial.println(F(" DISABLED"));
    return false;
  }

  if (m.moving) {
    Serial.print(F("ERR "));
    Serial.print(m.name);
    Serial.println(F(" BUSY"));
    return false;
  }

  if ((signedSteps > 0 && m.pulsePosition > INT32_MAX - signedSteps) ||
      (signedSteps < 0 && m.pulsePosition < INT32_MIN - signedSteps)) {
    Serial.println(F("ERR MOVE POSITION_RANGE"));
    return false;
  }
  const long target = m.pulsePosition + signedSteps;
  if (&m == &focus && focusLimited &&
      (target < focusMinimum || target > focusMaximum)) {
    Serial.println(F("ERR FOCUS LIMIT"));
    return false;
  }

  if (signedSteps == 0) {
    Serial.print(F("OK "));
    Serial.print(m.name);
    Serial.println(F(" MOVE 0"));
    return true;
  }

  m.directionPositive = (signedSteps > 0);
  digitalWrite(m.dirPin, m.directionPositive ? HIGH : LOW);

  // Avoid overflow from abs(LONG_MIN); practical commands are much smaller.
  unsigned long count = (signedSteps > 0)
                      ? (unsigned long)signedSteps
                      : (unsigned long)(-(signedSteps + 1)) + 1UL;

  m.remainingSteps = count;
  m.lastStepUs = micros();
  m.moving = true;

  Serial.print(F("OK "));
  Serial.print(m.name);
  Serial.print(F(" MOVE "));
  Serial.println(signedSteps);

  return true;
}

// A home attempt needs a new released-to-active switch transition. Pause
// pulses during debounce, and bound both travel and elapsed time.
bool serviceFocusHome() {
  if (!focusHoming) return false;
  const unsigned long now = millis();
  if ((unsigned long)(now - homeStartedMs) >= FOCUS_HOME_TIMEOUT_MS) {
    stopMotor(focus);
    Serial.println(F("ERR FOCUS HOME TIMEOUT"));
    return true;
  }
  if (digitalRead(FOCUS_HOME_PIN) == FOCUS_HOME_ACTIVE_LEVEL) {
    if (!homeSwitchSeen) {
      homeSwitchSeen = true;
      homeSwitchMs = now;
    } else if ((unsigned long)(now - homeSwitchMs) >= 20UL) {
      stopMotor(focus);
      focus.pulsePosition = 0;
      focusZeroed = true;
      Serial.println(F("DONE FOCUS HOME"));
    }
    return true;
  }
  homeSwitchSeen = false;
  return false;
}

void serviceMotor(MotorState &m) {
  if (&m == &focus && serviceFocusHome()) return;
  if (!m.enabled || !m.moving || m.remainingSteps == 0) {
    return;
  }

  unsigned long now = micros();

  if ((unsigned long)(now - m.lastStepUs) >= m.stepIntervalUs) {
    m.lastStepUs = now;

    digitalWrite(m.stepPin, HIGH);
    delayMicroseconds(STEP_PULSE_US);
    digitalWrite(m.stepPin, LOW);

    m.remainingSteps--;

    if (m.directionPositive) {
      m.pulsePosition++;
    } else {
      m.pulsePosition--;
    }

    if (m.remainingSteps == 0) {
      if (&m == &focus && focusHoming) {
        stopMotor(focus);
        Serial.println(F("ERR FOCUS HOME TRAVEL"));
        return;
      }
      m.moving = false;
      Serial.print(F("DONE "));
      Serial.print(m.name);
      Serial.print(F(" POS "));
      Serial.println(m.pulsePosition);
    }
  }
}

bool setMicrostep(int microstep) {
  bool ms1 = LOW;
  bool ms2 = LOW;
  bool ms3 = LOW;

  switch (microstep) {
    case 1:
      ms1 = LOW;  ms2 = LOW;  ms3 = LOW;
      break;
    case 2:
      ms1 = HIGH; ms2 = LOW;  ms3 = LOW;
      break;
    case 4:
      ms1 = LOW;  ms2 = HIGH; ms3 = LOW;
      break;
    case 8:
      ms1 = HIGH; ms2 = HIGH; ms3 = LOW;
      break;
    case 16:
      ms1 = HIGH; ms2 = HIGH; ms3 = HIGH;
      break;
    default:
      return false;
  }

  // Your wiring:
  // D2 = MS3, D3 = MS2, D4 = MS1
  digitalWrite(MS1_PIN, ms1);
  digitalWrite(MS2_PIN, ms2);
  digitalWrite(MS3_PIN, ms3);

  if (currentMicrostep != microstep) clearFocusReference();
  currentMicrostep = microstep;
  return true;
}


// ============================================================
// Serial command processing
// ============================================================

MotorState* motorFromToken(const char* token) {
  if (!token) return nullptr;

  if (strcmp(token, "OBJ") == 0) {
    return &objective;
  }

  if (strcmp(token, "FOCUS") == 0) {
    return &focus;
  }

  return nullptr;
}

void printMotorStatus(const MotorState &m) {
  Serial.print(m.name);
  Serial.print(F(": enabled="));
  Serial.print(m.enabled ? 1 : 0);
  Serial.print(F(" moving="));
  Serial.print(m.moving ? 1 : 0);
  Serial.print(F(" speed="));
  Serial.print(m.speedSps);
  Serial.print(F(" remaining="));
  Serial.print(m.remainingSteps);
  Serial.print(F(" pos="));
  Serial.println(m.pulsePosition);
}

void printStatus() {
  Serial.print(F("MICRO="));
  Serial.println(currentMicrostep);
  printMotorStatus(objective);
  printMotorStatus(focus);
  Serial.print(F("FOCUS_REF: zeroed="));
  Serial.print(focusZeroed ? 1 : 0);
  Serial.print(F(" limited="));
  Serial.print(focusLimited ? 1 : 0);
  Serial.print(F(" min="));
  Serial.print(focusMinimum);
  Serial.print(F(" max="));
  Serial.print(focusMaximum);
  Serial.print(F(" homing="));
  Serial.println(focusHoming ? 1 : 0);
}

void printHelp() {
  Serial.println(F("Commands:"));
  Serial.println(F("  OBJ ENABLE"));
  Serial.println(F("  OBJ DISABLE"));
  Serial.println(F("  OBJ MOVE <signed_steps>"));
  Serial.println(F("  OBJ SPEED <steps_per_second>"));
  Serial.println(F("  FOCUS ENABLE"));
  Serial.println(F("  FOCUS DISABLE"));
  Serial.println(F("  FOCUS MOVE <signed_steps>"));
  Serial.println(F("  FOCUS SPEED <steps_per_second>"));
  Serial.println(F("  MICRO <1|2|4|8|16>"));
  Serial.println(F("  STOP OBJ"));
  Serial.println(F("  STOP FOCUS"));
  Serial.println(F("  STOP ALL"));
  Serial.println(F("  STATUS"));
  Serial.println(F("  HELP"));
  Serial.println(F("  FOCUS ZERO"));
  Serial.println(F("  FOCUS LIMIT <min> <max>"));
  Serial.println(F("  FOCUS HOME"));
  Serial.println(F("  VERSION"));
}

bool parseInteger(const char* text, long &value) {
  if (!text || !*text) return false;
  char* end = nullptr;
  errno = 0;
  const long parsed = strtol(text, &end, 10);
  if (errno == ERANGE || *end || parsed < INT32_MIN || parsed > INT32_MAX) return false;
  value = parsed;
  return true;
}

bool noMoreArguments() {
  if (!strtok(nullptr, " \t")) return true;
  Serial.println(F("ERR EXTRA_ARGUMENTS"));
  return false;
}

void startFocusHome() {
  if (FOCUS_HOME_PIN == 255) {
    Serial.println(F("ERR FOCUS HOME NO_SWITCH"));
    return;
  }
  if (FOCUS_HOME_DIRECTION == 0 || FOCUS_HOME_MAX_STEPS == 0) {
    Serial.println(F("ERR FOCUS HOME NOT_CONFIGURED"));
    return;
  }
  if (digitalRead(FOCUS_HOME_PIN) == FOCUS_HOME_ACTIVE_LEVEL) {
    Serial.println(F("ERR FOCUS HOME SWITCH_ACTIVE"));
    return;
  }
  const long travel = (long)FOCUS_HOME_MAX_STEPS * FOCUS_HOME_DIRECTION;
  if ((travel > 0 && focus.pulsePosition > INT32_MAX - travel) ||
      (travel < 0 && focus.pulsePosition < INT32_MIN - travel)) {
    Serial.println(F("ERR MOVE POSITION_RANGE"));
    return;
  }
  clearFocusReference();
  homeSavedSpeed = focus.speedSps;
  setMotorSpeed(focus, FOCUS_HOME_SPEED_SPS);
  focus.directionPositive = FOCUS_HOME_DIRECTION > 0;
  digitalWrite(focus.dirPin, focus.directionPositive ? HIGH : LOW);
  focus.remainingSteps = FOCUS_HOME_MAX_STEPS;
  focus.lastStepUs = micros();
  focus.moving = focusHoming = true;
  homeSwitchSeen = false;
  homeStartedMs = millis();
  Serial.println(F("OK FOCUS HOME"));
}

void processCommand(char* line) {
  // Convert to uppercase in-place to make commands case-insensitive.
  for (char* p = line; *p; ++p) {
    if (*p >= 'a' && *p <= 'z') {
      *p = *p - 'a' + 'A';
    }
  }

  char* token1 = strtok(line, " \t");
  if (!token1) return;

  if (strcmp(token1, "HELP") == 0) {
    if (!noMoreArguments()) return;
    printHelp();
    return;
  }

  if (strcmp(token1, "STATUS") == 0) {
    if (!noMoreArguments()) return;
    printStatus();
    return;
  }

  if (strcmp(token1, "VERSION") == 0) {
    if (!noMoreArguments()) return;
    Serial.println(F("VERSION microscope-axes 1.0.0"));
    return;
  }

  if (strcmp(token1, "MICRO") == 0) {
    char* token2 = strtok(nullptr, " \t");

    if (!token2) {
      Serial.println(F("ERR MICRO VALUE"));
      return;
    }

    if (!noMoreArguments()) return;
    long micro;
    if (!parseInteger(token2, micro) ||
        !(micro == 1 || micro == 2 || micro == 4 || micro == 8 || micro == 16)) {
      Serial.println(F("ERR MICRO VALID=1,2,4,8,16"));
      return;
    }

    if (objective.moving || focus.moving) {
      Serial.println(F("ERR MICRO WHILE_MOVING"));
      return;
    }

    if (!setMicrostep(micro)) {
      Serial.println(F("ERR MICRO VALID=1,2,4,8,16"));
      return;
    }

    Serial.print(F("OK MICRO "));
    Serial.println(currentMicrostep);
    return;
  }

  if (strcmp(token1, "STOP") == 0) {
    char* token2 = strtok(nullptr, " \t");

    if (!token2) {
      Serial.println(F("ERR STOP TARGET"));
      return;
    }

    if (!noMoreArguments()) return;

    if (strcmp(token2, "OBJ") == 0) {
      stopMotor(objective);
      Serial.println(F("OK STOP OBJ"));
      return;
    }

    if (strcmp(token2, "FOCUS") == 0) {
      stopMotor(focus);
      Serial.println(F("OK STOP FOCUS"));
      return;
    }

    if (strcmp(token2, "ALL") == 0) {
      stopMotor(objective);
      stopMotor(focus);
      Serial.println(F("OK STOP ALL"));
      return;
    }

    Serial.println(F("ERR STOP TARGET"));
    return;
  }

  MotorState* motor = motorFromToken(token1);

  if (!motor) {
    Serial.println(F("ERR UNKNOWN_COMMAND"));
    return;
  }

  char* action = strtok(nullptr, " \t");

  if (!action) {
    Serial.println(F("ERR MISSING_ACTION"));
    return;
  }

  if (strcmp(action, "ENABLE") == 0) {
    if (!noMoreArguments()) return;
    setMotorEnabled(*motor, true);
    Serial.print(F("OK "));
    Serial.print(motor->name);
    Serial.println(F(" ENABLED"));
    return;
  }

  if (strcmp(action, "DISABLE") == 0) {
    if (!noMoreArguments()) return;
    setMotorEnabled(*motor, false);
    Serial.print(F("OK "));
    Serial.print(motor->name);
    Serial.println(F(" DISABLED"));
    return;
  }

  if (strcmp(action, "MOVE") == 0) {
    char* value = strtok(nullptr, " \t");

    if (!value) {
      Serial.println(F("ERR MOVE STEPS"));
      return;
    }

    if (!noMoreArguments()) return;
    long steps;
    if (!parseInteger(value, steps)) {
      Serial.println(F("ERR MOVE STEPS"));
      return;
    }
    startMove(*motor, steps);
    return;
  }

  if (strcmp(action, "SPEED") == 0) {
    char* value = strtok(nullptr, " \t");

    if (!value) {
      Serial.println(F("ERR SPEED VALUE"));
      return;
    }

    if (!noMoreArguments()) return;
    if (motor == &focus && focusHoming) {
      Serial.println(F("ERR FOCUS BUSY"));
      return;
    }
    long speed;
    if (!parseInteger(value, speed) || speed < 1 || !setMotorSpeed(*motor, speed)) {
      Serial.print(F("ERR SPEED RANGE "));
      Serial.print(MIN_SPEED_SPS);
      Serial.print(F("-"));
      Serial.println(MAX_SPEED_SPS);
      return;
    }

    Serial.print(F("OK "));
    Serial.print(motor->name);
    Serial.print(F(" SPEED "));
    Serial.println(motor->speedSps);
    return;
  }

  if (motor == &focus && (strcmp(action, "ZERO") == 0 ||
                         strcmp(action, "LIMIT") == 0 || strcmp(action, "HOME") == 0)) {
    long minimum = 0, maximum = 0;
    if (strcmp(action, "LIMIT") == 0) {
      char* first = strtok(nullptr, " \t");
      char* second = strtok(nullptr, " \t");
      if (!parseInteger(first, minimum) || !parseInteger(second, maximum) || minimum > maximum) {
        Serial.println(F("ERR FOCUS LIMIT RANGE"));
        return;
      }
    }
    if (!noMoreArguments()) return;
    if (!focus.enabled) {
      Serial.println(F("ERR FOCUS DISABLED"));
      return;
    }
    if (focus.moving) {
      Serial.println(F("ERR FOCUS BUSY"));
      return;
    }
    if (strcmp(action, "ZERO") == 0) {
      clearFocusReference();
      focus.pulsePosition = 0;
      focusZeroed = true;
      Serial.println(F("OK FOCUS ZERO"));
    } else if (strcmp(action, "HOME") == 0) {
      startFocusHome();
    } else if (!focusZeroed) {
      Serial.println(F("ERR FOCUS NOT_ZEROED"));
    } else if (focus.pulsePosition < minimum || focus.pulsePosition > maximum) {
      Serial.println(F("ERR FOCUS LIMIT POSITION"));
    } else {
      focusMinimum = minimum;
      focusMaximum = maximum;
      focusLimited = true;
      Serial.print(F("OK FOCUS LIMIT "));
      Serial.print(minimum);
      Serial.print(F(" "));
      Serial.println(maximum);
    }
    return;
  }
  Serial.println(F("ERR UNKNOWN_ACTION"));
}

void serviceSerial() {
  // Bound each pass so a continuous input stream cannot starve motor service.
  size_t budget = LINE_BUFFER_SIZE;
  while (budget-- && Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      lineBuffer[lineLength] = '\0';
      if (!discardLine && lineLength > 0) processCommand(lineBuffer);
      lineLength = 0;
      discardLine = false;
    } else if (discardLine) {
      continue;
    } else if (((uint8_t)c < 32 && c != '\t') || (uint8_t)c > 126) {
      lineLength = 0;
      discardLine = true;
      Serial.println(F("ERR LINE_CHARACTER"));
    } else if (lineLength < LINE_BUFFER_SIZE - 1) {
      lineBuffer[lineLength++] = c;
    } else {
      lineLength = 0;
      discardLine = true;
      Serial.println(F("ERR LINE_TOO_LONG"));
    }
  }
}


// ============================================================
// Arduino setup / loop
// ============================================================

void setup() {
  if (FOCUS_HOME_PIN != 255) pinMode(FOCUS_HOME_PIN, INPUT_PULLUP);
  pinMode(MS1_PIN, OUTPUT);
  pinMode(MS2_PIN, OUTPUT);
  pinMode(MS3_PIN, OUTPUT);

  pinMode(objective.dirPin, OUTPUT);
  pinMode(objective.stepPin, OUTPUT);
  pinMode(objective.enPin, OUTPUT);

  pinMode(focus.dirPin, OUTPUT);
  pinMode(focus.stepPin, OUTPUT);
  pinMode(focus.enPin, OUTPUT);

  digitalWrite(objective.stepPin, LOW);
  digitalWrite(focus.stepPin, LOW);

  digitalWrite(objective.dirPin, LOW);
  digitalWrite(focus.dirPin, LOW);

  // Safe startup: both drivers disabled.
  setMotorEnabled(objective, false);
  setMotorEnabled(focus, false);

  // Default full-step.
  setMicrostep(1);

  setMotorSpeed(objective, 400);
  setMotorSpeed(focus, 800);

  Serial.begin(115200);

  Serial.println(F("DUAL_A4988_READY"));
  printStatus();
}

void loop() {
  serviceSerial();
  serviceMotor(objective);
  serviceMotor(focus);
}
