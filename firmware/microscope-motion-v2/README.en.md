[中文](README.md)

# Microscope motion controller v2

This Arduino Uno R3 sketch drives the turret and focus A4988 modules. It uses Timer1 for pulses, gradual speed changes, and automatic idle power control. The original sketch remains in `../microscope-axes/`.

## Wiring

| Signal | Turret | Focus |
| --- | --- | --- |
| DIR | D8 | D11 |
| STEP | D9 | D12 |
| ENABLE | D10 | D13 |
| SLEEP | D6 | D7 |

Shared microstep pins remain MS1 = D4, MS2 = D3, MS3 = D2. Keep a common logic ground. ENABLE and SLEEP are active low. Remove a direct connection between SLEEP and 5V before attaching an Arduino output. RESET must be high during operation. Check the actual carrier wiring before changing any RESET/SLEEP link.

Both motors start disabled and asleep. The firmware sets inactive output latches before it enables the pins. The optional focus home switch cannot use D6 or D7. `FocusConfig.h` leaves homing unconfigured.

## Motion and power

- Timer1 schedules both motors at 20000 Hz. STEP stays high for 50 microseconds. Timer0 remains available for Arduino time functions.
- Speed is 1 through 10000 pulses per second. Acceleration and deceleration are 2000 pulses per second squared. Each move starts and finishes at no more than 100 pulses per second.
- `MOVE` uses signed integer pulse counts at the current microstep setting. It requires an enabled, idle motor. Completion reports counted pulses, not measured travel.
- Focus disables after 30 seconds without motor activity. Each axis sleeps after 120 seconds without motor activity. A moving axis cannot enter sleep.
- Accepted nonzero movement, movement completion, `ENABLE`, `DISABLE`, and `STOP` update the activity time. `STATUS`, `VERSION`, `HELP`, `SPEED`, and `MICRO` do not extend it.
- `ENABLE` wakes that axis and waits at least 1 millisecond before enabling the outputs. It sends no movement pulses. `MOVE` does not enable a disabled motor implicitly.
- `DISABLE` stops pulses and removes holding current. `STOP` stops pulses and retains the current enable state. Neither command automatically resumes a stopped move.
- Disabling or sleeping focus invalidates its counted zero and limits. The Console must obtain a new visual reference before automatic focus motion. Sleep can also change the driver's electrical step state.

The Console can disable focus earlier when an operation finishes. It must keep focus enabled across the measurements and movements of one autofocus operation. A saved pulse count cannot establish physical position after power release.

Timer1 is reserved for this sketch. Do not add a library that also owns Timer1. Serial output and command parsing remain outside the timer interrupt. These changes do not add encoder feedback.

## Serial interface

Use 115200 baud and newline-terminated ASCII. Existing commands remain available:

```text
VERSION
STATUS
OBJ ENABLE
OBJ SPEED 400
OBJ MOVE 5
OBJ DISABLE
FOCUS ENABLE
FOCUS SPEED 800
FOCUS MOVE -5
STOP ALL
MICRO 4
```

Read each acknowledgement. Wait for `DONE` before another move on that axis. The motor does not disable automatically at the end of a raw `MOVE`. `FOCUS ZERO`, `FOCUS LIMIT`, and optional `FOCUS HOME` remain available for host reference management. The firmware does not measure turret clearance.

Version and status use this format:

```text
VERSION microscope-axes 2.0.0
MICRO=1
OBJ: enabled=0 moving=0 speed=400 remaining=0 pos=0 sleeping=1 acceleration=2000
FOCUS: enabled=0 moving=0 speed=800 remaining=0 pos=0 sleeping=1 acceleration=2000
FOCUS_REF: zeroed=0 limited=0 min=0 max=0 homing=0
```

The updated host accepts firmware versions 1 and 2. Version 1 does not report sleep or acceleration. Version 2 autofocus must use settled positions instead of estimating image positions from constant speed.

## Build and manual upload

Save the currently installed firmware and its settings before replacement. Open `microscope-motion-v2.ino` in Arduino IDE, select Arduino Uno, and select the correct port. Upload manually after wiring is complete. Stop the microscope service before another program opens its serial port.

The command-line compile does not open a board:

```sh
arduino-cli core install arduino:avr
arduino-cli compile --fqbn arduino:avr:uno firmware/microscope-motion-v2
```

After upload, check `VERSION` and `STATUS`. Both axes must report disabled and sleeping before the host sends an enable command. Establish the camera calibration through the Console before operating the system. The software checks below do not establish physical clearance, current limits, or missed-step performance.

## Offline checks

The native harness calls the same timer handler with fake GPIO and serial input. It can withhold foreground processing while timer pulses continue. It never opens an Arduino port.

```sh
uv run pytest -q tests/test_microscope_motion_v2.py tests/test_microscope_motors.py
```

The native tests require `g++`. The Python simulator selects this firmware with `MicroscopeSimulator(firmware_version=2)`; its default remains version 1 for legacy tests.
