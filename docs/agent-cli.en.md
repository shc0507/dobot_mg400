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
- The console releases the session's devices after 10 minutes without a call from the session. A release of the robot disables the arm first. A release of the stage stops the stage first.
- `labctl <device> release` releases 1 device. `labctl all release` releases every device of the session.
- The owner takes control back with **Release all** on the Overview, or with `sudo labctl all release --session NAME`. Both skip devices lent to a run. Stop or abort the run first.

## Commands

| Command | Effect |
| --- | --- |
| `labctl all state` | State of every device, the control holders and the current run |
| `labctl all session` | This session's devices and the idle time left (`idle_left_s`) |
| `labctl all events [--since ID] [--device D]` | Stored events, newest first. `ID` is the `id` of an event. With `--since`, the reply holds the oldest 500 events after `ID`. To read all new events, repeat with the largest `id` until the reply is empty. `fields` holds the event details, for example `action`, `params`, `client` and `result` |
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
| `stage move MOTOR ROTATIONS [--speed N]` | Relative `move`. Positive ROTATIONS use `ccw`; negative values use `cw`. Default speed is 0.25 rotations/s |
| `stage goto MOTOR POSITION [--speed N]` | `move_to_position` at an absolute rotation coordinate. Default speed is 0.25 rotations/s |
| `stage calibrate MOTOR [--speed N]` | `calibrate`. Default speed is 0.25 rotations/s |
| `stage reset-position MOTOR`, `stage set-limit MOTOR` | `reset_position`, `set_limit` |
| `stage clear-upper-limit MOTOR`, `stage clear-lower-limit MOTOR`, `stage clear-fault MOTOR` | `clear_upper_limit`, `clear_lower_limit`, `clear_fault` |
| `stage connect [PORT]` | Connect the stage port. Without PORT, list the ports |
| `furnace setpoint C --ramp C_PER_MIN` | `apply_setpoint` |
| `furnace sequence FILE [--start]` | `set_sequence`, or `start_sequence`, from a JSON file |
| `furnace gas CHANNEL PERCENT`, `furnace valve CHANNEL open\|close` | `set_gas_flow`, `set_gas_valve`. `CHANNEL` is 0, 1 or 2 for Gas 1 to 3 |
| `furnace acknowledge` | Acknowledge the furnace alarm |
| `microscope objective ID`, `microscope nudge STEPS`, `microscope focus STEPS` | `select_objective`, `objective_nudge`, `focus_move` |
| `microscope autofocus full\|local`, `microscope zero`, `microscope home` | `autofocus`, `set_focus_zero`, `home_focus` |
| `microscope record start [--mode time_lapse\|live] [--name RUN_NAME]` | `start_recording` with a new `recording_id` |
| `microscope record stop ID` | `stop_recording` |
| `vision detect\|markers\|labels CAMERA` | `detect`, `read_markers`, `read_labels`. The result is in `state.last_command.output` |

Run `microscope zero` only after an operator puts the 50x objective in focus at the clearance position. Do not use it to recover from a failed command: zero at another height moves the focus limits.

Stage coordinates use measured shaft rotations. Absolute `goto` requires completed calibration, valid bounds and fresh encoder feedback. Use Sample Transfer for saved positions. The old stage `home`, `zero` and `pose` shortcuts, and named `goto`, are unsupported.

Each command waits for its result. The default `--timeout` is 300 seconds. `--no-wait` returns after the console accepts the command.

## Composite runs

| Command | Effect |
| --- | --- |
| `labctl do RECIPE [NAME=VALUE ...] [--timeout S]` | Run 1 catalog recipe with the devices that the session holds |
| `labctl chip locate SLOT` | Run `slot-state` for 1 slot. Print `state`, `center_xy_mm` and `angle_deg` |

- Take every device of the recipe first. Otherwise the call fails, and `hint` names the `take` commands.
- The run borrows the session's devices. The console keeps the leases, so the arm stays enabled and the microscope keeps focus zero. During the run, the session cannot command these devices. Stops still work.
- The CLI holds the orchestrator lease during the run. A run that holds after a failed step is aborted. When the run ends, the devices return to the session.
- Every run ends with a stop of each device that it used. A microscope stop disables the motors and clears the focus zero. After a run that used the microscope, set the focus zero again before a focus move.
- Run 1 `do` at a time in a session. During a run, `do` and `chip locate` of the same session fail with `busy`. The run continues.
- If the CLI stops during the run, the orchestrator lease expires. The run holds and motion stops. Abort it with `POST /lab/api/runs/<run_id>/abort`. See the HTTP routes in [Recipe orchestrator](orchestrator.en.md).
- The default `--timeout` is 1800 seconds. At the timeout, the CLI aborts the run.
- `microscope-record` needs time to finalize after the capture. Set `seconds` to at most 3000, and set `--timeout` to at least `seconds` plus 600.
- The output has `run_id`, `status` (`completed` or `aborted`), `steps` with `skill`, `status`, `result` and `error`, and `devices`.
- One-step catalog recipes: `slot-state`, `station-transfer`, `crucible-load`, `crucible-unload`, `microscope-record`, `check-calibration`. `chip-pick` and `tray-transfer` are also in the catalog.

Encoder stages cannot run the legacy `crucible-load` or `crucible-unload` recipes. These catalog entries require a new coordinate design before encoder use. Sample Transfer buttons move the stage only and do not replace the robot transfer workflow.

## Stops

Stops need no control and no session. They are sent once and never retried.

| Command | Effect |
| --- | --- |
| `labctl all stop` | STOP ALL MOTION: the console stop (it also holds a run) and a stop of the robot, the stage and the microscope |
| `labctl <device> stop` | Stop 1 device |
| `labctl furnace stop` | Heat and gas off |
| `labctl furnace gas-off [CHANNEL]` | Stop 1 gas channel, or all 3 |

With the default `--url`, each stop also goes straight to the service on loopback, so it works when the console is down. With another `--url`, stops go only through that console.

`sudo labctl` stops also run when `/etc/mg400/mg400.env` is missing or invalid.

The furnace cannot read back the flow of a write-only flow controller. A gas stop of such a channel sends the zero and closes the valve, but its result stays `unconfirmed`. Check the channel with `labctl furnace state`.

A robot service with no syncing client cannot confirm a stop. It sends `ResetRobot()` and reports `unconfirmed`, but it does not lock. A later `labctl robot take` reads the robot state again.

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
- If the reply to a command is lost, the CLI reads the device state for up to 3 s. If the service recorded the command for this session, the CLI reports that result. A robot command stays `unknown`.

| Exit | Meaning | Next step |
| --- | --- | --- |
| 0 | `ok` or `accepted` | Continue |
| 1 | Refused, failed, rejected or stopped | Read `error` and `hint` |
| 2 | Usage error or invalid parameters | Correct the call |
| 3 | Outcome unknown: a timeout, a lost connection after the send, or an unconfirmed stop | Read the state first. Do not repeat a motion without a check |
| 130 | Interrupted. The CLI sent 1 stop for the device that it waited on | Read the state |

## Limitations

- No operator prompt for a held run: `do` aborts it.
- No keyboard jog and no stage hold. They need a refresh within 0.4 s (keyboard jog) and 1 s (stage hold).
- A motion that started before the CLI stopped continues as commanded.
- Image files go to `/tmp/labctl-<UID>/` unless `--out` is given. The installed CLI runs as UID 1000 and cannot write into `/home/olares`.
