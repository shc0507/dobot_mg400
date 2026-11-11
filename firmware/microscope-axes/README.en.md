[中文](README.md)

# Microscope turret and focus firmware

This sketch controls 2 A4988 drivers on an Arduino Uno R3. The serial link uses 115200 baud and newline-terminated ASCII commands.

## Wiring and safe use

| Function | Uno pins |
| --- | --- |
| Turret DIR / STEP / EN | D8 / D9 / D10 |
| Focus DIR / STEP / EN | D11 / D12 / D13 |
| Shared MS1 / MS2 / MS3 | D4 / D3 / D2 |

The enable pins are active low. Both drivers start disabled, with full steps. STEP stays low when idle.

Before reprogramming a board, an operator must save its existing firmware. Agents compile only. H-06 establishes the focus direction, clearance position and travel limits. H-21 validates the firmware before automated use.

Manual focus moves remain available without a reference for supervised setup. The firmware does not know the furnace geometry or turret clearance. The host must require a known focus zero and limits before automated motion. The host must permit turret rotation only at the verified clearance position, except the bounded calibration moves below.

Objective and light calibration can turn at working focus within the measured turret window `NUDGE_WINDOW`.
This exception requires a known focus reference, measured limits, stationary focus, and the supervised confirmations in the [microscope guide](../../docs/microscope.en.md).
Unset or `0` preserves the clearance rule. The allowed positive window is `1` through `20` full steps.
The service `objective_nudge` also turns the turret at working focus within this window.
Experiments and full objective switches still require clearance.

For supervised `H-06` measurements, use `sudo labctl calibrate setup` after stopping the microscope service.
The console opens no cameras, writes no settings file, and never sends `FOCUS LIMIT`.
Its counted positions do not measure physical travel. Follow the operator instructions and key controls in the microscope guide.

## Compile and offline tests

Compile without a connected board:

```sh
arduino-cli core install arduino:avr
arduino-cli compile --fqbn arduino:avr:uno firmware/microscope-axes
```

The sketch needs no external Arduino library. The protocol tests use `g++`, fake GPIO, a virtual clock and fake serial input:

```sh
uv run pytest tests/test_microscope_protocol.py
```

`microscope.sim.MicroscopeSimulator` has no device access or background thread. `command(line)` returns immediate reply lines. `feed(data)` accepts serial byte fragments. `advance(seconds)` advances virtual time and returns completion lines. `reset()` models power-up. Optional switch settings exist for offline homing tests.

## Serial protocol

Commands are case-insensitive. A line can contain at most 95 ASCII characters, excluding CR and LF. Tabs separate words. Invalid characters or an overlong line discard the entire line through LF. Numeric arguments must be complete decimal integers. Extra arguments are refused.

| Command | Reply |
| --- | --- |
| `OBJ ENABLE`, `FOCUS ENABLE` | `OK OBJ ENABLED`, `OK FOCUS ENABLED` |
| `OBJ DISABLE`, `FOCUS DISABLE` | `OK OBJ DISABLED`, `OK FOCUS DISABLED` |
| `OBJ SPEED <n>`, `FOCUS SPEED <n>` | `OK OBJ SPEED <n>`, `OK FOCUS SPEED <n>` |
| `OBJ MOVE <n>`, `FOCUS MOVE <n>` | `OK OBJ MOVE <n>`, `OK FOCUS MOVE <n>` followed by `DONE OBJ POS <n>` or `DONE FOCUS POS <n>` when complete |
| `MICRO <1|2|4|8|16>` | `OK MICRO <n>` |
| `STOP OBJ`, `STOP FOCUS`, `STOP ALL` | `OK STOP OBJ`, `OK STOP FOCUS`, `OK STOP ALL` |
| `FOCUS ZERO` | `OK FOCUS ZERO` |
| `FOCUS LIMIT <min> <max>` | `OK FOCUS LIMIT <min> <max>` |
| `FOCUS HOME` | `OK FOCUS HOME` followed by `DONE FOCUS HOME`, or an `ERR` line |
| `VERSION` | `VERSION microscope-axes 1.0.0` |
| `STATUS` | State lines shown below |
| `HELP` | Command list |

Speeds are 1..10000 pulses per second. Move arguments and positions use signed 32-bit pulse counts. Each pulse follows the selected microstep mode. A target outside the position range is refused before motion. A move requires an enabled, idle motor. A zero-length move returns only its `OK` line, as in the original protocol.

Both motors can move concurrently. `MICRO` is global and is refused while either motor moves. Stop cancels the remaining pulses immediately. A cancelled move does not emit a completion line and does not restart. Disable also stops that motor. Errors start with `ERR`.

`STATUS` preserves the original 3 lines and adds the focus reference:

```text
MICRO=1
OBJ: enabled=0 moving=0 speed=400 remaining=0 pos=0
FOCUS: enabled=0 moving=0 speed=800 remaining=0 pos=0
FOCUS_REF: zeroed=0 limited=0 min=0 max=0 homing=0
```

After power-up or reset, the board prints `DUAL_A4988_READY` and then the `STATUS` lines. The host discards these lines after the reset delay.

## Focus reference and limits

With the focus enabled and idle, an operator can establish a verified position with `FOCUS ZERO`. It sets the pulse position to 0 and clears the previous limits. Then use `FOCUS LIMIT <min> <max>` with the measured safe range. The current position must be inside that range. Limits require a known zero. Each later focus move must end inside the inclusive range.

Power-up, reset, focus disable and an actual microstep change invalidate the focus zero and limits. Repeating the current microstep mode preserves them. Ordinary stop preserves the counted position and reference. These counts do not measure physical position. A person must re-establish the reference if the mechanism slips or moves without counted pulses.

## Optional focus homing

The installed board has no focus switch. `FocusConfig.h` uses these safe defaults:

| Setting | Default | Meaning |
| --- | --- | --- |
| `FOCUS_HOME_PIN` | `255` | No switch |
| `FOCUS_HOME_DIRECTION` | `0` | Direction away from the furnace window is unknown |
| `FOCUS_HOME_MAX_STEPS` | `0` | No approved travel budget |
| `FOCUS_HOME_SPEED_SPS` | `100` | Homing pulse rate |
| `FOCUS_HOME_TIMEOUT_MS` | `30000` | Maximum homing time |
| `FOCUS_HOME_ACTIVE_LEVEL` | `LOW` | Active switch level, with `INPUT_PULLUP` |

`FOCUS HOME` refuses motion without a switch, a verified direction and a nonzero travel budget. An operator must configure those values after hardware validation. The switch pin must not overlap the UART or driver pins. Direction must be `-1` or `1` for homing. The maximum configurable timeout is 60000 milliseconds.

Homing requires the focus enabled and idle, with the switch initially released. It clears the old reference and limits. It moves only in the configured direction. Pulses pause when the switch becomes active. The switch must remain active for 20 milliseconds before the firmware establishes zero. Success leaves limits unset and restores the previous speed.

Travel exhaustion or timeout stops motion with `ERR FOCUS HOME TRAVEL` or `ERR FOCUS HOME TIMEOUT`. Stop and disable cancel homing. A failed or cancelled attempt leaves the reference unknown. No attempt retries. Focus speed changes, zeroing and limit changes are refused during homing.
