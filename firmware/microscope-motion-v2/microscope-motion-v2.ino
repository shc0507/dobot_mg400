/* Microscope motion controller v2: timer pulses, ramps, and idle power control.
   Uno R3: OBJ DIR/STEP/EN/SLEEP = D8/D9/D10/D6.
   FOCUS DIR/STEP/EN/SLEEP = D11/D12/D13/D7. Shared MS1/MS2/MS3 = D4/D3/D2.
   USB serial: 115200 baud, newline-terminated ASCII. See README.en.md.
*/
#include <Arduino.h>
#include <string.h>
#include <stdlib.h>
#include <errno.h>
#include <stdint.h>
#include "MotionEngine.h"

const uint8_t MS3_PIN = 2, MS2_PIN = 3, MS1_PIN = 4;
int currentMicrostep = 1;
bool focusZeroed = false, focusLimited = false;
int32_t focusMinimum = 0, focusMaximum = 0;
bool homeSwitchSeen = false;
uint32_t homeStartedMs = 0, homeSwitchMs = 0;
uint16_t homeSavedSpeed = 800;

void clearFocusReference() {
  focusZeroed = false;
  focusLimited = false;
  focusMinimum = focusMaximum = 0;
}

void endFocusHome() {
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    if (focusHoming) focus.speedSps = homeSavedSpeed;
    focusHoming = false;
  }
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
  if (enabled) {
    bool wake;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { wake = m.sleeping; }
    if (wake) {
      digitalWrite(m.sleepPin, HIGH);
      delayMicroseconds(WAKE_DELAY_US);
    }
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
      m.sleeping = false;
      m.enabled = true;
      m.lastActivityMs = millis();
      digitalWrite(m.enPin, LOW);
    }
  } else {
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
      stopMotorState(m);
      m.enabled = false;
      digitalWrite(m.enPin, HIGH);
    }
    if (&m == &focus) { endFocusHome(); clearFocusReference(); }
  }
}

void servicePower() {
  const uint32_t now = millis();
  for (uint8_t index = 0; index < 2; ++index) {
    MotorState &m = index == 0 ? objective : focus;
    bool disableFocus = false, sleep = false;
    ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
      const uint32_t idle = now - m.lastActivityMs;
      if (!m.moving && !m.donePending && !m.sleeping) {
        disableFocus = &m == &focus && m.enabled && idle >= FOCUS_IDLE_DISABLE_MS;
        sleep = idle >= IDLE_SLEEP_MS;
        if (disableFocus || sleep) {
          // Keep the last motion time: releasing focus must not postpone sleep.
          m.enabled = false;
          digitalWrite(m.enPin, HIGH);
          m.pulseHigh = false;
          digitalWrite(m.stepPin, LOW);
        }
        if (sleep) {
          m.sleeping = true;
          digitalWrite(m.sleepPin, LOW);
        }
      }
    }
    if (&m == &focus && (disableFocus || sleep)) { endFocusHome(); clearFocusReference(); }
  }
}

void stopMotor(MotorState &m) {
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { stopMotorState(m); }
  if (&m == &focus) endFocusHome();
}

bool setMotorSpeed(MotorState &m, unsigned long speedSps) {
  if (speedSps < MIN_SPEED_SPS || speedSps > MAX_SPEED_SPS) return false;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { m.speedSps = speedSps; }
  return true;
}

bool startMove(MotorState &m, long signedSteps) {
  uint8_t error = 0;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    if (!m.enabled || m.sleeping) error = 1;
    else if (m.moving || m.donePending) error = 2;
    else if ((signedSteps > 0 && m.pulsePosition > INT32_MAX - signedSteps) ||
             (signedSteps < 0 && m.pulsePosition < INT32_MIN - signedSteps)) error = 3;
    else {
      const int32_t target = m.pulsePosition + signedSteps;
      if (&m == &focus && focusLimited && (target < focusMinimum || target > focusMaximum)) error = 4;
      else if (signedSteps != 0) beginMotorState(m, signedSteps);
    }
  }
  if (error == 1 || error == 2) {
    Serial.print(F("ERR ")); Serial.print(m.name);
    Serial.println(error == 1 ? F(" DISABLED") : F(" BUSY"));
  } else if (error == 3) Serial.println(F("ERR MOVE POSITION_RANGE"));
  else if (error == 4) Serial.println(F("ERR FOCUS LIMIT"));
  else {
    Serial.print(F("OK ")); Serial.print(m.name); Serial.print(F(" MOVE "));
    Serial.println(signedSteps);
  }
  return error == 0;
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
      ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { focus.pulsePosition = 0; }
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
  bool done = false;
  int32_t position = 0;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    if (m.donePending) {
      done = true;
      position = m.completedPosition;
      m.donePending = false;
    }
  }
  if (!done) return;
  if (&m == &focus && focusHoming) {
    stopMotor(focus);
    Serial.println(F("ERR FOCUS HOME TRAVEL"));
    return;
  }
  Serial.print(F("DONE ")); Serial.print(m.name); Serial.print(F(" POS "));
  Serial.println(position);
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

void printMotorStatus(const AxisSnapshot &m) {
  Serial.print(m.name); Serial.print(F(": enabled=")); Serial.print(m.enabled ? 1 : 0);
  Serial.print(F(" moving=")); Serial.print(m.moving ? 1 : 0);
  Serial.print(F(" speed=")); Serial.print(m.speed);
  Serial.print(F(" remaining=")); Serial.print(m.remaining);
  Serial.print(F(" pos=")); Serial.print(m.position);
  Serial.print(F(" sleeping=")); Serial.print(m.sleeping ? 1 : 0);
  Serial.print(F(" acceleration=")); Serial.println(ACCELERATION_SPS2);
}

void printStatus() {
  // Copy the complete group before UART output can block. No serial write
  // occurs with interrupts masked, and all rows describe the same instant.
  AxisSnapshot obj(objective), foc(focus);
  bool zeroed, limited, homing;
  int32_t minimum, maximum;
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    obj = AxisSnapshot(objective); foc = AxisSnapshot(focus);
    zeroed = focusZeroed; limited = focusLimited;
    minimum = focusMinimum; maximum = focusMaximum;
    homing = focusHoming && foc.moving;
  }
  Serial.print(F("MICRO=")); Serial.println(currentMicrostep);
  printMotorStatus(obj); printMotorStatus(foc);
  Serial.print(F("FOCUS_REF: zeroed=")); Serial.print(zeroed ? 1 : 0);
  Serial.print(F(" limited=")); Serial.print(limited ? 1 : 0);
  Serial.print(F(" min=")); Serial.print(minimum);
  Serial.print(F(" max=")); Serial.print(maximum);
  Serial.print(F(" homing=")); Serial.println(homing ? 1 : 0);
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
  ATOMIC_BLOCK(ATOMIC_RESTORESTATE) {
    beginMotorState(focus, travel);
    focusHoming = true;
  }
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
    Serial.println(F("VERSION microscope-axes 2.0.0"));
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
      ATOMIC_BLOCK(ATOMIC_RESTORESTATE) { focus.pulsePosition = 0; }
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
  pinMode(MS1_PIN, OUTPUT); pinMode(MS2_PIN, OUTPUT); pinMode(MS3_PIN, OUTPUT);
  for (uint8_t index = 0; index < 2; ++index) {
    MotorState &m = index == 0 ? objective : focus;
    // Set inactive latches before changing pin direction.
    digitalWrite(m.enPin, HIGH); digitalWrite(m.sleepPin, LOW);
    digitalWrite(m.stepPin, LOW); digitalWrite(m.dirPin, LOW);
    pinMode(m.enPin, OUTPUT); pinMode(m.sleepPin, OUTPUT);
    pinMode(m.stepPin, OUTPUT); pinMode(m.dirPin, OUTPUT);
    m.enabled = false; m.sleeping = true; m.lastActivityMs = millis();
  }
  setMicrostep(1);
  configureMotionTimer();
  Serial.begin(115200);
  Serial.println(F("DUAL_A4988_READY"));
  printStatus();
}

void loop() {
  serviceMotor(objective);
  serviceMotor(focus);
  servicePower();
  serviceSerial();
}
