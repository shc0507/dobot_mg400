[中文](agent-cli.md)

# Agent CLI

Agents on Olares operate the lab devices with `labctl` device groups. Each call is 1 process and prints 1 JSON object on 1 line to stdout. Diagnostics go to stderr.

- Installed: `sudo labctl <group> ...`. The dispatcher runs the call as UID 1000 without a terminal.
- Checkout: `uv run labctl <group> ...` against the same loopback services.
- Demo: start `uv run python -m orchestrator serve --demo --port 18772 --base-path /lab/`, then add `--url http://127.0.0.1:18772/lab/` to each call.

The CLI adds no limits and no checks. The services keep the test-stage protections: stops, 1 controller per device, the furnace caps, the robot speed cap and alarm block, the microscope focus limits and turret clearance, and the stage soft limits.

## Sessions and control

- `--session NAME` selects the agent session. The default is `main`. The console page is `agent-NAME`, and the holder on the Overview **Control** card is `agent-NAME-<12 hex>`.
- Use 1 session for each agent. Calls with the same session share control.
- `labctl <device> take` takes control through the Lab Console. The console holds the lease between calls and renews it every 1 second.
- The console releases the session's devices after 10 minutes without a call from the session. A release of the robot disables the arm first.
- `labctl <device> release` releases 1 device. `labctl all release` releases every device of the session.
- The owner takes control back with **Release all** on the Overview, or with `sudo labctl all release --session NAME`.

## Commands

| Command | Effect |
| --- | --- |
| `labctl all state` | State of every device, the control holders and the current run |
| `labctl all session` | This session's devices and the idle time left (`idle_left_s`) |
| `labctl all events [--since ID] [--device D]` | Stored events, newest first. `ID` is the `id` of an event |
| `labctl all history [--device D] [--from T] [--to T]` | Stored device state rows, newest first. `T` is a Unix time |
| `labctl <device> state` | Full state of 1 device, its holder and `mine` |
| `labctl <device> describe` | Described commands and their JSON Schemas |
| `labctl <device> take`, `labctl <device> release` | Take or release control |
| `labctl <device> call ACTION [NAME=VALUE ...]` | Send 1 described command |
| `labctl <device> wait [COMMAND_ID] [--timeout S]` | Wait for a command sent with `--no-wait`. For the robot, wait until it is idle |
| `labctl camera still VIEW [--out PATH]` | Save 1 JPEG from the camera preview, for example `camera1` |
| `labctl microscope frame microscope\|side [--png] [--out PATH]` | Save 1 microscope or side camera frame |

Devices: `robot`, `stage`, `furnace`, `microscope`, `vision`, `thermal`.

In `call`, `VALUE` is JSON when it parses, else a string. `NAME=@FILE` reads JSON from a file. `NAME=@-` reads JSON from stdin. The service checks the parameters.

Shortcuts:

| Shortcut | Sends |
| --- | --- |
| `robot enable`, `robot disable`, `robot home`, `robot clear-alarm` | `enable`, `disable`, `home`, `clear_alarm` |
| `robot move X Y Z R [--speed N] [--relative]` | `execute` with flange `targets`. `--relative` adds the values to the current `actual` pose |
| `robot vacuum suck\|release\|off` | `vacuum` |
| `stage move MOTOR STEPS [--speed N]` | `move`. The sign of STEPS selects `cw` or `ccw`. The default speed is 50 |
| `stage home MOTOR`, `stage zero MOTOR`, `stage goto MOTOR NAME`, `stage pose NAME` | `home`, `zero` with `confirm`, `goto`, `pose` |
| `stage connect [PORT]` | Connect the stage port. Without PORT, list the ports |
| `furnace setpoint C --ramp C_PER_MIN` | `apply_setpoint` |
| `furnace sequence FILE [--start]` | `set_sequence`, or `start_sequence`, from a JSON file |
| `furnace gas CHANNEL PERCENT`, `furnace valve CHANNEL open\|close` | `set_gas_flow`, `set_gas_valve` |
| `furnace acknowledge` | Acknowledge the furnace alarm |
| `microscope objective ID`, `microscope nudge STEPS`, `microscope focus STEPS` | `select_objective`, `objective_nudge`, `focus_move` |
| `microscope autofocus full\|local`, `microscope zero`, `microscope home` | `autofocus`, `set_focus_zero`, `home_focus` |
| `microscope record start [--mode time_lapse\|live] [--name RUN_NAME]` | `start_recording` with a new `recording_id` |
| `microscope record stop ID` | `stop_recording` |
| `vision detect\|markers\|labels CAMERA` | `detect`, `read_markers`, `read_labels`. The result is in `state.last_command.output` |

Each command waits for its result. The default `--timeout` is 300 seconds. `--no-wait` returns after the console accepts the command.

## Composite runs

| Command | Effect |
| --- | --- |
| `labctl do RECIPE [NAME=VALUE ...] [--timeout S]` | Run 1 catalog recipe with the devices that the session holds |
| `labctl chip locate SLOT` | Run `slot-state` for 1 slot. Print `state`, `center_xy_mm` and `angle_deg` |

- Take every device of the recipe first. Otherwise the call fails, and `hint` names the `take` commands.
- The run borrows the session's devices. The console keeps the leases, so the arm stays enabled and the microscope keeps focus zero. During the run, the session cannot command these devices. Stops still work.
- The CLI holds the orchestrator lease during the run. A run that holds after a failed step is aborted. When the run ends, the devices return to the session.
- If the CLI stops during the run, the orchestrator lease expires. The run holds and motion stops. Abort the run on the Overview.
- The default `--timeout` is 1800 seconds. At the timeout, the CLI aborts the run.
- The output has `run_id`, `status` (`completed` or `aborted`), `steps` with `skill`, `status`, `result` and `error`, and `devices`.
- One-step catalog recipes: `slot-state`, `station-transfer`, `crucible-load`, `crucible-unload`, `microscope-record`, `check-calibration`. `chip-pick` and `tray-transfer` are also in the catalog.

## Stops

Stops need no control and no session. They are sent once and never retried.

| Command | Effect |
| --- | --- |
| `labctl all stop` | STOP ALL MOTION: the console stop (it also holds a run) and a stop of the robot, the stage and the microscope |
| `labctl <device> stop` | Stop 1 device |
| `labctl furnace stop` | Heat and gas off |
| `labctl furnace gas-off [CHANNEL]` | Stop 1 gas channel, or all 3 |

With the default `--url`, each stop also goes straight to the service on loopback, so it works when the console is down. With another `--url`, stops go only through that console.

A robot service with no syncing client cannot confirm a stop. It reports `unconfirmed` and locks. Recover it with `labctl robot take` and `labctl robot disable`, or with **Recover** in the console.

## Output and exit codes

A command result:

```json
{"ok": true, "device": "stage", "action": "move", "outcome": "ok", "command_id": "42", "message": "...", "last_command": {}, "state": {}}
```

A refusal:

```json
{"ok": false, "device": "robot", "action": "execute", "outcome": "refused", "error": {"code": "not_controlled", "status": 409, "message": "Take control of this device first."}, "hint": "labctl robot take"}
```

- `outcome`: `ok`, `accepted`, `failed`, `rejected`, `stopped`, `refused`, `unknown`, `interrupted`.
- `error.code`: `usage`, `invalid`, `not_controlled`, `held_by_other`, `locked`, `busy`, `refused`, `failed`, `rejected`, `stopped`, `unknown`, `timeout`, `unreachable`, `interrupted`.
- `hint`: the next command to run, when one applies.
- A stop prints `results` with `confirmed`, `pending`, `unconfirmed`, `unreachable` or `not-configured` for each device.

| Exit | Meaning | Next step |
| --- | --- | --- |
| 0 | `ok` or `accepted` | Continue |
| 1 | Refused, failed, rejected or stopped | Read `error` and `hint` |
| 2 | Usage error or invalid parameters | Correct the call |
| 3 | Outcome unknown: a timeout, a lost connection after the send, or an unconfirmed stop | Read the state first. Do not repeat a motion without a check |
| 130 | Interrupted. The CLI sent 1 stop for the device that it waited on | Read the state |

## Limitations

- No operator prompt for a held run: `do` aborts it.
- No keyboard jog and no stage hold. They need refreshes every 100 ms and 500 ms.
- A motion that started before the CLI stopped continues as commanded.
- Image files go to `/tmp/labctl/` unless `--out` is given. The installed CLI runs as UID 1000 and cannot write into `/home/olares`.
