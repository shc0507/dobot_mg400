#pragma once

// H-06 must establish the sign away from the window and a safe travel budget.
// The installed board has no focus switch. These defaults never home.
#ifndef FOCUS_HOME_PIN
#define FOCUS_HOME_PIN 255
#endif
#ifndef FOCUS_HOME_DIRECTION
#define FOCUS_HOME_DIRECTION 0
#endif
#ifndef FOCUS_HOME_MAX_STEPS
#define FOCUS_HOME_MAX_STEPS 0UL
#endif
#ifndef FOCUS_HOME_SPEED_SPS
#define FOCUS_HOME_SPEED_SPS 100UL
#endif
#ifndef FOCUS_HOME_TIMEOUT_MS
#define FOCUS_HOME_TIMEOUT_MS 30000UL
#endif
#ifndef FOCUS_HOME_ACTIVE_LEVEL
#define FOCUS_HOME_ACTIVE_LEVEL LOW
#endif

static_assert(FOCUS_HOME_PIN == 255 ||
              (FOCUS_HOME_PIN >= 5 && FOCUS_HOME_PIN <= 19 &&
               !(FOCUS_HOME_PIN >= 8 && FOCUS_HOME_PIN <= 13)),
              "Focus switch pin overlaps UART, driver pins, or board range");
static_assert(FOCUS_HOME_DIRECTION == -1 || FOCUS_HOME_DIRECTION == 0 ||
              FOCUS_HOME_DIRECTION == 1,
              "Focus home direction must be -1, 0 (unknown), or 1");
static_assert(FOCUS_HOME_MAX_STEPS <= 2147483647UL,
              "Focus home travel exceeds signed position range");
static_assert(FOCUS_HOME_SPEED_SPS >= 1 && FOCUS_HOME_SPEED_SPS <= 10000,
              "Focus home speed outside firmware range");
static_assert(FOCUS_HOME_TIMEOUT_MS >= 20 && FOCUS_HOME_TIMEOUT_MS <= 60000,
              "Focus home timeout must be 20..60000 milliseconds");
static_assert(FOCUS_HOME_ACTIVE_LEVEL == LOW || FOCUS_HOME_ACTIVE_LEVEL == HIGH,
              "Focus switch active level must be LOW or HIGH");
