[中文](README.md)

# Three A4988 motor demo

Open `three-motor-demo.ino` in Arduino IDE. Keep `MotorControl.h`,
`MotorControl.cpp`, and `MotorPins.h` in the same sketch directory; Arduino compiles the `.cpp`
automatically. Install **AccelStepper by Mike McCauley** through Library Manager,
select your board and port, then upload.

The target is Arduino Uno R3 with FQBN `arduino:avr:uno` and core `arduino:avr`.
Only an operator uploads firmware during supervised hardware validation.
Before upload, install the STG-02 host update, which accepts the appended status fields below.
Older hosts that require `spr` at the end of each motor line cannot parse this firmware.

## Offline build and checks

These commands run from the repository root. They compile without a board or serial port:

```sh
arduino-cli core install arduino:avr
arduino-cli lib install AccelStepper
arduino-cli compile --fqbn arduino:avr:uno firmware/three-motor-demo
python firmware/three-motor-demo/tests/run.py --library /path/to/AccelStepper
```

The native checks require Python and `g++`. They link the installed AccelStepper source with fake GPIO, clock, and serial interfaces.
They also reject conflicting pins and check that a disabled minimum input refuses homing.
Use a workspace-local Arduino CLI configuration for its data, downloads, and user directories.

## Web control panel

The stage panel (`stage-panel`) is the web GUI for this sketch. It has independent
speed and step-count settings, direction buttons, and serial port selection. It also
has a one-second cooldown after each move completes, Stop all, reset defaults, and
light/dark themes. This sketch works with the GUI, including separate direction
hold buttons for continuous motion until release. Upload this sketch to enable
held moves and their one-second timeout, plus a global stepping-mode control for
all three motors. To install, start, and connect to the panel, see the
[stage panel guide](../../docs/stage-panel.en.md). Close Serial Monitor before
connecting from the GUI.

If the host has more than one serial device, set `MG400_STAGE_SERIAL` to the
`/dev/serial/by-id/` path of the Arduino. The GUI then shows only that port.

The GUI replaces CCW/CW with these labels:

| Motor | CCW (`ccw`) label | CW (`cw`) label |
| --- | --- | --- |
| Bottom | Up | Down |
| Top | Right | Left |
| Insert | Insert In | Insert Out |

Hold buttons add **Hold** to each label (for example, **Hold Up** / **Hold Down**),
and movement status uses the same direction names. These are display labels only;
serial directions remain `ccw` / `cw`, and motor behavior is unchanged.

The GUI also has an optional **Keyboard controls** section, switched **Off** by
default. Hold **W/S** for bottom Up/Down, **A/D** for top Left/Right, or **J/K** for
Insert In/Insert Out; the displayed keys can also be pressed and held. Each uses
its motor channel's selected speed and the same hold/release commands and timeout.
Turning the section off releases its active holds. See the [stage panel guide](../../docs/stage-panel.en.md)
for keyboard behavior and stopping controls.

## Wiring

Default assignments (customizable in `MotorPins.h`):

| Motor | STEP | DIR | SLEEP | LimitMin | LimitMax |
| --- | --- | --- | --- | --- | --- |
| Bottom_Motor | D5 | D4 | D12 | A2 | A3 |
| Top_Motor | D7 | D6 | D13 | A4 | A5 |
| Insert_Motor | D3 | D2 | D11 | A0 | A1 |

Each driver has its own SLEEP output. Connect it only to the SLP input of that
motor's driver.

Connect each limit contact between its input and GND. Inputs use `INPUT_PULLUP` and become active at LOW.
The minimum is the CCW end, and the maximum is the CW end. Check the physical directions during supervised validation.
`InsertLimitMin`, `InsertLimitMax`, `BottomLimitMin`, `BottomLimitMax`, `TopLimitMin`, and `TopLimitMax` set these pins.
The value `255` disables an input. Homing refuses a motor without a minimum input.
An absent maximum input provides no maximum contact protection.
Compilation checks all enabled inputs against each other and the outputs, within Uno pins D2..D19.
The default wiring uses all 18 nonserial I/O pins. Keep D0/D1 free.

Shared resolution inputs (connect each Arduino pin to that input on **all three** boards):

| A4988 input | Arduino pin |
| --- | --- |
| MS1 | D10 |
| MS2 | D9 |
| MS3 | D8 |

### Match motor names to your wiring

Edit only the pin assignments in [`MotorPins.h`](MotorPins.h). Each pair is
**`{STEP, DIR}`**, in that order. Use Arduino pin numbers, not physical chip pin
numbers. For example, if the bottom motor's driver is wired to STEP D5 / DIR D4
and the top motor's driver is wired to STEP D3 / DIR D2, swap their pairs:

```cpp
constexpr PinPair Bottom = {5, 4};
constexpr PinPair Top = {3, 2};
constexpr PinPair Insert = {7, 6};
```

`BottomSleep`, `TopSleep`, and `InsertSleep` set the SLEEP output of each motor.
Set each one to the Arduino pin that connects to the SLP input of that motor's driver.
If you swap two pairs, make sure that each SLEEP pin still goes to the driver of
the same motor.

You can also choose other output-capable pins on your board, or change STEP and
DIR independently to match the wires. All twelve STEP/DIR/MS/SLEEP pins must be distinct.
D0/D1 are reserved for serial communication. Compilation rejects overlapping assignments,
D0/D1, and out-of-range numbers on boards that define `NUM_DIGITAL_PINS`. Check
your board's pinout for any additional pin restrictions.

Stop all motors and disconnect the web app before uploading the revised sketch.
Reconnect after upload: the **Bottom**, **Top**, and **Insert** controls and serial
commands now use the new pairs. The assignments remain in the firmware across
restarts. The Python app needs no pin configuration or restart. Uploading resets
commanded positions to zero.

STEP and DIR must be **separate signals**, not electrically shorted together or
shared between drivers. The mapping is used for driver pulses, direction changes,
startup, and stopping, so there are no other pin numbers to update in the code.

### Driver connections

MS1/MS2/MS3 are driven by the shared D10/D9/D8 outputs listed above. Startup sets
all three outputs LOW for full stepping.

The firmware drives the SLEEP outputs. LOW puts a driver to sleep. HIGH wakes it.
On the Pololu carrier, SLP has a pull-up. While the Arduino resets, the firmware
does not control SLP, so the drivers can be awake until startup sets SLEEP LOW.
On many boards, for example the Uno, D13 (`TopSleep`) also controls the on-board
LED. After a reset from the serial port or the reset button, the bootloader flashes
this LED. Thus the Top driver can sleep and wake several times each time the panel
connects. Changing `TopSleep` requires a distinct output pin and corresponding wiring changes.
The default analog pins now serve the limit contacts. Do not reuse one without changing and validating the affected wiring.

Do not leave RST floating. The Pololu carrier has no pull resistor on RST. Connect
RST to the adjacent SLP pin on the carrier, or connect RST to the logic supply (VDD).
If RST connects to SLP, RST follows the SLEEP output. The driver operates only while
RST is HIGH.

EN must be LOW for the driver outputs to operate; it can be tied to GND.
The API does not control EN or RST. Arduino and drivers
need a common ground and appropriate logic/motor supplies. See the
[carrier wiring reference](https://www.pololu.com/product/1182).

### Driver sleep

Each driver has its own sleep timer:

1. At startup, all three drivers sleep.
2. If the driver sleeps, an accepted move, `start`, or `hold` sets the SLEEP output
   of that motor HIGH. The firmware then waits 2 ms before the first STEP pulse.
   This wait does not stop serial commands or the other motors.
3. When the motor stops, its driver stays awake and holds the motor.
4. After `SleepTimeoutMs` without motion, the firmware sets SLEEP LOW. The default
   value is `10000UL` (10 s).

A new move before the timeout keeps the driver awake. Motor selection, `status`,
`microstep`, ordinary settings, and `stop` on an idle motor do not wake a driver. They
also do not restart its sleep timer.

`awake <ms>` is the explicit exception. It wakes the selected driver and extends holding by up to 60000 ms after motion ends.
Renewal while idle starts that interval again. `awake 0` cancels the extension, while the normal idle timer still applies.
The status field `awake=1` means SLEEP is HIGH, including the wake delay.

To change the timeout, edit `SleepTimeoutMs` in [`MotorPins.h`](MotorPins.h). Then
upload the sketch again. The value must be greater than zero.

A sleeping driver does not hold the motor: the motor has no holding torque. An
external force can turn the shaft, and the position count does not record this
movement. The [A4988 datasheet](https://www.allegromicro.com/-/media/files/datasheets/a4988-datasheet.ashx)
states that a HIGH on SLEEP allows startup, at which time the driver moves the
motor to the Home microstep position. Thus the motor can also move when its
driver wakes.

## Limits, homing, and soft range

A LOW contact inhibits pulses toward that limit immediately, without a deceleration ramp.
Status changes after 5 ms of stable input. Motion toward that end remains blocked until release is stable for 5 ms.
A short contact bounce can stop a move. Motion away from that limit remains possible if the opposite limit and soft range permit it.
These checks apply to finite moves, `start`, and `hold`. Contacts do not detect a broken wire.

`home` applies to the selected idle motor. It clears that motor's reference and soft range, then runs these phases:

1. Seek the minimum contact in CCW at 100 STEP pulses/s.
2. Back off in CW by 200 STEP pulses. The minimum must release.
3. Approach again in CCW at 25 STEP pulses/s. On stable contact, stop and set position 0 with `homed=1`.

An already active minimum starts with backoff. The firmware restores the configured ordinary speed after every homing exit.
The direction remains that of the last homing phase.
`HomeSpeed`, `HomeSlowSpeed`, `HomeStepLimit`, and `HomeTimeoutMs` in `MotorPins.h` set the speeds and bounds.
The defaults limit the complete operation to 10000 pulses or 120000 ms. The first reached bound fails the operation.
`HomeBackoffSteps` is 200. Homing also fails on conflicting limits, a blocked backoff, an unreleased minimum, or position overflow.

Acceptance replies immediately with `OK: bottom home` for the bottom motor. Check `status` for completion.
Failures produce `ERROR: home failed: <reason>` on a complete serial line, including failures after the acknowledgement.
`stop` and `stop all` cancel homing. A failed or cancelled home leaves `homed=0` and does not retry.
Other motors and serial handling continue during homing.

`soft <min> <max>` sets an inclusive range in STEP pulses from home. The selected motor must be idle and homed, and the minimum cannot exceed the maximum.
Finite targets outside the range fail with `ERROR`. Continuous and held motion stop at the edge without a ramp.
If a new range excludes the current position, only motion toward the range is possible, and a finite target must lie inside it.
Reset and power-up clear every reference and range. A real microstep change does the same for all motors without resetting their counters.
A successful home establishes a switch reference, not encoder feedback. External movement while asleep can still invalidate the physical position.

## Global microstepping

Send `microstep 1`, `microstep 2`, `microstep 4`, `microstep 8`, or `microstep 16`.
This command applies to **all three motors**, regardless of the selected motor,
and succeeds only while every motor is idle. It acknowledges with, for example,
`OK: microstep 16`. Send `microstep` to query the current mode; `status` also begins
with a global line such as `microstep=16` before its three motor lines.

| Command | MS1 / D10 | MS2 / D9 | MS3 / D8 | Resolution |
| --- | --- | --- | --- | --- |
| `microstep 1` | LOW | LOW | LOW | Full step |
| `microstep 2` | HIGH | LOW | LOW | Half step |
| `microstep 4` | LOW | HIGH | LOW | Quarter step |
| `microstep 8` | HIGH | HIGH | LOW | Eighth step |
| `microstep 16` | HIGH | HIGH | HIGH | Sixteenth step |

Mode changes generate no STEP pulses and preserve the signed pulse counters.
An actual mode change sets `homed=0` and `soft=0` for all motors. Repeating the current mode leaves those flags unchanged.
The mode lasts until the board resets; there is no EEPROM write. `steps`, speed,
acceleration, position, and remaining counts use STEP pulses at the active mode.
At sixteenth step, `steps 50` requests 3.125 full steps. `spr 200` still describes
a 200-full-step/revolution motor: `rev` generates 3,200 pulses at sixteenth step.
The GUI keeps its numeric speed and step settings when changing resolution.

The [A4988 datasheet](https://www.allegromicro.com/-/media/files/datasheets/a4988-datasheet.ashx)
specifies that the new mode takes effect on the next STEP rising edge. To preserve
absolute position when switching resolutions, switch at a translator position
shared by both modes. The pulse counters have no encoder or translator-state
feedback and cannot verify that alignment. Firmware changes the MS outputs only
while idle and provides a 1 µs setup interval before subsequent STEP pulses.

## Serial test

Status preserves the existing field order and appends the reference, contacts, range, and driver state:

```text
microstep=1
* bottom cw idle pos=0 remaining=0 speed=200.0 accel=50.0 spr=200 homed=0 limits=00 soft=0 awake=0
  top cw idle pos=0 remaining=0 speed=200.0 accel=50.0 spr=200 homed=0 limits=00 soft=0 awake=0
  insert cw idle pos=0 remaining=0 speed=200.0 accel=50.0 spr=200 homed=0 limits=00 soft=0 awake=0
```

`limits=<min><max>` uses 1 for an active contact and 0 otherwise. `soft=1` means the range is enabled.
All existing commands retain their acknowledgement text.

Set Serial Monitor to **115200 baud** and **Newline** or **Both NL & CR**. Send
one command per line. Commands are case insensitive; whitespace is accepted.
No motor starts automatically at boot.

```text
motor bottom
dir cw
steps 200
```

After it finishes (use `status` to see `idle`), reverse it:

```text
dir ccw
rev
```

Try the other motors independently:

```text
motor top
dir cw
rev 0.5
motor insert
dir ccw
steps 100
status
```

Selecting a motor does not stop another motor, so this also tests concurrent
movement. For continuous rotation:

```text
motor bottom
stop
speed 100
start
status
stop
stop all
```

| Command | Action |
| --- | --- |
| `motor bottom`, `motor top`, `motor insert` | Select motor (also `motor 1`, `motor 2`, `motor 3`) |
| `dir cw`, `dir ccw` | Set selected motor's direction while idle |
| `steps 200` | Move a positive number of STEP pulses in that direction |
| `rev`, `rev 2`, `rev 0.5` | Move one, two, or half a revolution |
| `start` | Rotate continuously at constant speed until stopped |
| `hold` | Rotate at constant speed; stop after 1 second without a `keepalive` |
| `keepalive` | Renew the selected motor's active hold; never restart an expired hold |
| `stop`, `stop all` | Cancel selected motor's movement, or all movement immediately |
| `speed 200` | Set selected motor's speed limit (1..1000 STEP pulses/s) while idle |
| `accel 50` | Set finite-move acceleration (1..10000 STEP pulses/s squared) while idle |
| `spr 200` | Set full steps/revolution (1..65535) while idle |
| `microstep`, `microstep 16` | Query/set the global resolution (1, 2, 4, 8, 16); all motors must be idle to change |
| `home` | Start bounded homing of the selected idle motor |
| `soft 0 10000` | Set the selected homed motor's inclusive range |
| `awake 60000`, `awake 0` | Renew or cancel the selected driver's holding extension |
| `status` | Show all motors; `*` marks the selected motor |
| `help` | Print command list |

Counts must be positive; use `dir ccw` to reverse. Revolutions round to the
nearest STEP pulse at the active resolution; a request rounding to zero is rejected. Invalid, oversized,
or extra-argument commands are rejected. A new movement or configuration is
rejected while the selected motor is moving. Use `stop` first. The `awake` renewal remains available during motion. Positions and
remaining counts are signed (CW positive). Position is the signed number of commanded
STEP pulses since startup or the last successful home, with no encoder feedback. Pulse counts across
different resolutions cannot be treated as a single physical distance.

The default **200 full steps/revolution** assumes a 1.8-degree motor. Use `spr 400`
for a 0.9-degree motor; this setting is per motor and lasts until restart.
Physical CW/CCW depends on your wiring and viewing direction. Finite moves use
acceleration/deceleration; `start`, `hold`, and `stop` change pulse generation immediately.
After a stop, the driver keeps the coils energized until its sleep timeout ends
(see [Driver sleep](#driver-sleep)).

The GUI uses `hold` and renews it about every 200 ms while a button is pressed.
Releasing sends `motor <name>` followed by `stop`. The hold timeout is per motor
and runs on the Arduino, so it still ends held motion after a USB disconnect or
server failure. The standalone Serial Monitor `start` command remains continuous
until explicitly stopped.

## API use from another sketch

```cpp
#include "MotorControl.h"

void setup() {
  MotorControl::begin();
  MotorControl::selectMotor(MotorControl::Motor::Top_Motor);
  MotorControl::setDirection(MotorControl::Direction::Counterclockwise);
  MotorControl::setStepsPerRevolution(200);
  MotorControl::setSpeed(200);
  MotorControl::setAcceleration(50);
  MotorControl::rotateOneRevolution(); // Or rotateSteps(200), rotateRevolutions(0.5).
}

void loop() {
  MotorControl::update(); // Services all motors; call as often as possible.
}
```

`update()` also starts stepping after the wake delay and puts idle drivers to
sleep. Call it even while all motors are idle.

Movement functions return immediately and return `false` if rejected.
`isRotating(motor)` and `getStatus(motor, status)` let your main loop track
completion. `startRotation()`, `stopRotation()`, and `stopAll()` provide continuous
rotation and cancellation. See the header for signatures and the
[AccelStepper reference](https://www.airspayce.com/mikem/arduino/AccelStepper/classAccelStepper.html)
for the underlying motion library.
