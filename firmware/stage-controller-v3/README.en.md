[中文](README.md)

# Stage Controller v3

Open `stage-controller-v3.ino` in Arduino IDE. Select Arduino Uno. The sketch needs no extra library. The sketch and its folder must have the same name. Older firmware remains in the other folders under `firmware/`.

This firmware controls 3 A4988 drivers with AS5600 encoder feedback through a TCA9548A multiplexer. The Arduino owns the closed loop. The Console sends rotation targets and reads measured shaft rotations. It does not convert camera measurements or millimetres into pulses.

## Wiring and units

| Axis | STEP | DIR | SLEEP | Encoder channel | Encoder inversion | Switch input |
| --- | --- | --- | --- | --- | --- | --- |
| `bottom` | 5 | 4 | 12 | 0 | false | A0 |
| `top` | 7 | 6 | 13 | 2 | true | A1 |
| `insert` | 3 | 2 | 11 | 1 | true | A2 |

- Shared microstep pins: MS1=D10 HIGH, MS2=D9 HIGH, MS3=D8 LOW. Stepping is fixed at 1/8.
- Default full steps per revolution: 200. Nominal pulses per revolution: 1600. Encoder counts per revolution: 4096.
- I2C uses A4/SDA and A5/SCL at 400 kHz. Multiplexer address: `0x70`. Encoder address: `0x36`.
- Each analog input connects 2 normally open switches. The upper switch connects directly to ground. The lower switch connects through 10 kΩ. ADC below 100 means upper; 100–699 means lower; 700 or greater means released.
- The direct connection dominates if both switches close. An open wire can appear released. This circuit cannot independently identify both closed contacts.
- When the upper contact opens, the input rises through the lower band. While the debounced state is upper, a lower-band reading does not stop `cw` motion. A lower contact stops `cw` motion when it is the debounced state (20 ms).
- Public coordinates increase for `ccw`: Bottom Up, Top Right, Insert In. They decrease for `cw`: Bottom Down, Top Left, Insert Out. Do not change encoder inversion to change labels.

## Calibration and motion

1. Select an axis and explicitly start calibration. The motor moves toward the lower switch in `cw`.
2. Lower contact establishes zero. The motor moves toward the upper switch in `ccw` and records the positive travel span.
3. The motor moves to the midpoint. Absolute positioning becomes available after calibration completes and feedback settles.

Calibration clears both bounds before it moves. The seeks ignore limits from `setlimit` and travel to the switches. After a stop or a failure, the bounds stay cleared or partial. A switch reading that does not confirm within 20 ms stops a seek for that time only; then the seek continues. Each seek phase fails with `timeout` after 120 s.

Normal switch contact stops outward travel. It does not reset the position or change recorded limits. Movement away from the switch remains possible. Calibration is never automatic after connection or a warning.

Relative moves and holds use measured rotations. Absolute moves require completed calibration, valid bounds and fresh healthy encoder feedback. Resetting the position or changing a bound invalidates completed calibration. Stop cancels calibration and movement without restarting them.

The default speed and acceleration are 0.25 rotations/s and 0.25 rotations/s². The speed limit is 960 pulses/s: 0.6 rotations/s at 200 full steps per revolution. The main loop sends STEP pulses. When a loop pass is late, the next pulse keeps the original pulse timing, so short delays do not lower the rate. A pulse that is more than 1 interval late restarts the timing and sends no burst. The home switches use ADC prescaler 16, about 13 µs per read. Held motion needs up to 1.25 times its speed to correct a following error. A higher limit makes long holds stop with a `tracking` fault. A finite move has no rate correction and can run a few percent slower than the requested speed. Drivers sleep after 10 s idle. A wake can move a shaft up to 2 full steps. The reverse-direction check allows this movement in the first move after a wake. A move stops with a `stall` fault after at least 1.5 s and 0.05 rotations of pulses without encoder movement in the pulse direction. Back-and-forth movement at an obstruction does not count. Held motion and seeks catch up after an obstruction at no more than 1.25 times the requested speed. A failed encoder read does not stop a move or lose tracking. The firmware uses the last good sample. If the gap between good samples exceeds 60 ms, the move stops with an `encoder` fault and tracking is lost. Encoder feedback continues while a shaft moves manually. No active position hold runs after a move completes.

Positions and bounds are in RAM. Arduino reset or power loss invalidates them. Recalibrate before using absolute targets. A single-turn AS5600 cannot recover rotations made while power was off.

## Serial interface

Use 115200 baud and newline-terminated commands. Opening the port can reset the Uno. Wait 2 s, then request `status`. The maximum command length is 63 characters. Wait for the `OK` or `ERROR` reply to each line before you send the next line: the receive buffer holds 63 bytes, and a long reply such as `status` does not read input. Only 1 process may own the serial port.

```text
motor insert
speed 0.25
calibrate
status
goto 0.5
stop all
```

Wait for calibration completion before sending `goto`. The target must be within the measured span. Other commands include `dir`, `rev`, `hold`, `keepalive`, `stop`, `reset`, `setlimit`, `clearupper`, `clearlower` and `clear`. A hold requires keepalive within 1 s.

Capability lines identify supported behavior. The Console requires the encoder, coordinate, calibration and positioning capabilities before enabling the corresponding controls. Do not send the new motion commands to legacy pulse firmware.

Unsolicited `TEL` and `LIM` lines report each axis at about 10 Hz. `TEL` includes motion, encoder health and remaining travel. Counted `LIM` supplies an atomic position, bounds, switch state, calibration phase and warning state. Counts convert to rotations by division by 4096. Do not combine old bounds with a newer position across a reset.

## Build and local checks

```sh
arduino-cli compile --fqbn arduino:avr:uno firmware/stage-controller-v3
python firmware/stage-controller-v3/tests/run.py
```

The native check compiles the actual sketch against simulated motors, encoders and switches. It requires a C++ compiler with a 32-bit `long` and 32-bit `float` math, as on the AVR. For example, give g++ `-m32 -msse2 -mfpmath=sse` through `--compiler-arg`. CI runs it on every run. It does not flash hardware. The Uno has little free flash and RAM; check both sizes after changes. Native checks cannot establish electrical behavior, mechanical clearance or accuracy under load.

Before flashing, stop the stage service and close every serial monitor. Flash only with the intended board and port selected. See the [Stage guide](../../docs/stage-panel.en.md) for connection and operation.
