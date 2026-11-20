[中文](development.md)

# Development reference

This page describes the development environment, code responsibilities, and control interfaces.
To use existing functions, select an operating guide from the [README](../README.en.md).
For service installation and configuration, see [Host deployment](deploy.en.md).
For Olares-specific settings, see [Olares configuration and maintenance](olares.en.md).


Find the section for your change:

| Task | Section |
| --- | --- |
| Install dependencies and run offline checks | [Development checks](#development-checks) |
| Find entry points and modules | [Code structure](#code-structure) |
| Write a robot service client | [Robot service interface](#robot-service-interface) |
| Write a stage service client | [Stage service API](#stage-service-api) |
| Change joint limits, motion parameters, or the suction outputs | [Physical robot control reference](#physical-robot-control-reference) |

## Development checks

Install Python 3.12+, uv, and Node.js 22+.
Run these commands in the project root:

```bash
uv sync
uv run pytest
node --test tests/*.mjs
uv run ruff check .
uv run ruff format --check .
```

By default, `uv sync` installs the dependency groups in `default-groups` in `pyproject.toml`: `dev`, `stage`, `vision`, `thermal`, `furnace`, `camera`, `orchestrator`, and `microscope`.
`vision-gpu` and `notebook` are not default groups.
The `stage` group contains Flask, waitress, pySerial, NumPy, and OpenCV for the stage service.
The robot service and interactive terminal use only the standard library and do not require the `stage` group.

Tests do not connect to a robot or Arduino.
For pull requests and pushes to `main`, GitHub Actions (`.github/workflows/ci.yml`) runs these checks on Python 3.12.
A pull request runs only the pytest files that `scripts/select_tests.py` selects from the changed files. A push to `main` runs all tests.
`pytest` runs Python tests. `node --test` runs the Lab Console JavaScript tests.
Offline checks cannot replace physical robot verification.

Shared test fixtures are in `tests/conftest.py`.
The stage test fixtures `stage_rig`, `stage_ports`, and `stage_usb` are also in this file. Tests use them by name.
Test doubles are objects that replace real devices in tests.
Controller, connection, and web service doubles are in `tests/helpers.py`, `tests/sim_mg400.py`, and `tests/panel_ws.py`.
Shared checks for the service contract tests are in `tests/contract_helpers.py`.
Stage clock, serial port, and serial port list doubles are in `tests/stage_helpers.py`.
Reuse these modules in new tests. Do not duplicate them in test files.

For `examples/test.ipynb`, add `--group notebook` to the synchronization command.
To keep other packages that you installed manually, also add `--inexact`.

## Offline robot simulator

`mg400.sim` simulates the controller on a loopback address.
It accepts only numeric loopback addresses. The default is `127.0.0.2`.
The controller ports are `29999`, `30003`, and `30004`. The feedback interval is `8 ms`.
At startup, the robot is disabled.

The connection lock directory is fixed at `/var/lib/mg400/locks`. If the host does not have this directory, first run `sudo install -d -m 0700 -o "$USER" /var/lib/mg400/locks`.
Each robot address has its own lock file, so the simulator lock does not affect the real robot.

Run these commands in the project root:

```bash
uv run python -m mg400.sim --host 127.0.0.2 &
sim_pid=$!
uv run python apps/control_panel/server.py \
  --bind 127.0.0.1 --port 18766 --robot-host 127.0.0.2
kill "$sim_pid"
wait "$sim_pid"
```

In another terminal, connect with `labkit.client.ServiceClient("http://127.0.0.1:18766/", "<client-id>")`. Send these requests in sequence:

1. Call `heartbeat()` to take the lease. Wait until `connected` in the state is `true`.
2. Call `command("enable")`. Wait for the robot to become enabled.
3. Send the target coordinates with `command("execute", targets=...)`.
4. Call `command("disable")`. Wait for stop confirmation, then call `release()`.

Each `command()` returns a `command_id`. The command is finished when `last_command.id` in `state()` equals it and `busy` is `false`.
The lease expires every 3 seconds. Call `renew()` while you wait.

Press `Ctrl+C` in the terminal to stop the robot service.
The next commands terminate this simulator process and remove the temporary lock directory.

The simulator reaches joint and Cartesian targets immediately. It does not calculate real kinematics, paths, or collisions.
Inverse kinematics returns the current joint values. `J1` jogging changes joint feedback. Other jogging changes only the motion mode.
These limitations apply only to the simulator. Production control code and stop confirmation rules remain unchanged.
Offline rehearsal cannot replace hardware acceptance.

## Code structure

### Layers

The repository has 4 layers. Each layer calls only the layer below it.

| Layer | Parts | Purpose |
| --- | --- | --- |
| Backend | Device services (`apps/control_panel/`, `src/stage_controller/`, `src/thermal_cam/`, `src/furnace/`, `src/microscope/`, `src/vision/`) and `firmware/` | 1 process for each device. It listens only on `127.0.0.1`. Users and agents do not call the backend directly |
| Orchestrator | `src/orchestrator/` | The device cache `devices.py` runs 1 reader thread for each device. The thread reads `api/state` every 250 ms, the events every 1 s, and `describe` when the instance changes. The console and `labctl` read device state only from this cache. Commands, leases, and recipe runs also go through the orchestrator |
| Toolkit | `labctl` (`deploy/labctl`, device groups in `src/orchestrator/agent_cli.py`) | Agents use the device groups to operate the devices. Administrators use the service commands to start and stop the services, read the logs, make backups, and check the hardware |
| Frontend | Lab Console (`src/orchestrator/web/`) | Browser page. It reaches the orchestrator through the proxy |

Exception: a `labctl` stop goes to the orchestrator and also directly to the device service.

### Directories

| Path | Purpose |
| --- | --- |
| `config/robot.toml` | Robot address, used when `MG400_ROBOT_HOST` is not set |
| `apps/control_panel/` | Standalone Python robot service, without a web page |
| `apps/layout-studio/` | Offline layout editor that uses simulated data and sends no control commands |
| `config/systemd/` | systemd definitions for all services, with configuration from `/etc/mg400/mg400.env` |
| `deploy/` | The `install.py` installer, the `labctl` command for routine operations, and configuration examples |
| `src/mg400/` | Backend, control protocol, and feedback processing |
| `src/mg400/sim/` | Loopback controller simulator, with a shared test model |
| `src/stage_controller/` | Stage service: Flask interface and serial protocol |
| `src/labkit/` | Shared service contract, leases, events, access checks, and HTTP client. See [ADR 0001](adr/0001-instrument-service-contract.en.md) |
| `src/cameras/` | AC410 camera modes and UVC reading |
| `src/vision/` | [Vision service](vision.en.md): localization, QR reading, trays, and camera calibration |
| `src/thermal_cam/` | [Thermal camera service](thermal.en.md) |
| `src/furnace/` | [Furnace and gas service](furnace.en.md) |
| `src/microscope/`, `src/toupcam_camera/` | [Microscope service](microscope.en.md) and the Toupcam camera |
| `src/orchestrator/` | [Recipe service](orchestrator.en.md) and `labctl`. `web/` is the [Lab Console](dashboard.en.md) |
| `recipes/` | TOML recipes |
| `config/trays/`, `config/proxy/`, `config/udev/` | Tray geometry, proxy template, and USB access rule templates |
| `firmware/three-motor-demo/` | Stage Arduino firmware and wiring instructions in English |
| `firmware/microscope-axes/` | Microscope turret and focus firmware. See the [firmware guide](../firmware/microscope-axes/README.en.md) |
| `scripts/` | Camera preview, control mode switching, console screenshots (`console_shots.py`), test selection (`select_tests.py`), document pair checks (`check_doc_pairs.py`), agent signals (`agent_signal.py`), detector evaluation (`vision_eval.py`), and the console font (`console_font.py`) |
| `examples/` | Single-file tutorials and notebooks |
| `tests/` | Python and JavaScript offline tests |
| `docs/` | Operating, deployment, and development guides. `docs/agents/` contains agent procedures |
| `work/` | Local runtime data, backups, and temporary files, excluded from Git |

Open the [layout editor](../apps/layout-studio/index.html) in a browser to edit and export interface layouts.

`apps/control_panel/server.py` is the robot service. It provides the HTTP interface, without a web page.
The Motion workspace of the Lab Console controls the robot and suction cup through this service.
`websocket.py` uses the standard library for WebSocket frame encoding and decoding. The console control socket uses it.
See [Robot service interface](#robot-service-interface) for interface details.

`web_control.py` dispatches key jogging. Robot press-and-hold control reuses `keyboard_jog.py`.
`web_keyboard.py` validates key input. It maps Z to suck and X to release.

The robot service controls the robot through the TCP protocol.
`controller_protocol.py` parses command replies. `feedback.py` reads joint feedback.
`joint_control.py` executes motion with limits and the suction outputs.
For TCP protocol references and completion criteria, see [Physical robot control interface](hardware.en.md).
There is currently no ROS 2 backend.

`src/stage_controller/` is the stage service. It does not import `mg400` or use the robot connection lock.
`__main__.py` reads `MG400_STAGE_*` environment variables and command-line arguments, then starts waitress.
On SIGINT, SIGTERM, or SIGHUP, it stops the motors and disconnects the serial port before it exits.
`app.py` provides the HTTP interface and access checks.
`controller.py` manages the serial connection, cooldown, press-and-hold control, and activity log.
`protocol.py` implements the Arduino serial protocol and demo mode.
All serial commands execute inside the `MotorController` lock. Commands for one move do not interleave with other requests.
When you change serial commands, update both the firmware and `DemoTransport`.
See [Stage service API](#stage-service-api) for interface details.

## Robot service interface

After it takes the control lease, the robot service connects to the robot and holds the connection lock.
The Lab Console and custom clients read state and send commands through the service.
`labctl mode` connects directly to the robot.
It shares the connection lock with the service and cannot connect at the same time as the service.
The service uses only the Python standard library.

| Interface | Purpose |
| --- | --- |
| `GET {prefix}api/state` | Read the state once |
| `GET {prefix}api/describe` | Read the contract version, command parameters, and lease behavior |
| `POST {prefix}api/stop` | Request a stop and read its confirmation status |
| `GET {prefix}api/events?since=<seq>` | Read journal events after the specified sequence |
| `POST {prefix}api/command` | Send one command |
| `POST {prefix}api/heartbeat` | Take the control lease and connect to the robot |
| `POST {prefix}api/renew`, `POST {prefix}api/release` | Renew or release the lease. See [Contract lease and commands](#contract-lease-and-commands) |

`{prefix}` is the value of `--base-path`, which defaults to `/`.
In this section, angle brackets show placeholders. For example, `<client-id>` is the client ID.
Send keys with the command `keys`.

### Access and client ID

The service listens on `127.0.0.1` and does not check tokens, `Host`, or `Origin`. The VPN is the access boundary.

Physical robot control mode identifies clients by a client ID of 1–128 printable ASCII characters.
Only one client ID has control at a time.
The controlling client must renew the control lease within 3 seconds. The lease defines how long control remains valid.
Renewals, key updates, and commands from the controlling client renew the lease.
If the service receives no renewal within 3 seconds, it requests a robot stop and locks control.

### HTTP interface

POST requests follow the [instrument service contract](adr/0001-instrument-service-contract.en.md). They must meet these conditions:

1. Include `Content-Type: application/json`. Other content types return 415.
2. In physical robot control mode, include `X-MG400-Client: <client-id>`.
3. Send a JSON object of 1–4096 bytes, with its length specified by `Content-Length`. Lease requests send `{}`.

A successful `api/command` returns 202, `command_id`, and `state`.
A successful `api/heartbeat`, `api/renew`, or `api/release` returns 200 and `{"lease": {...}}`.
Rejection returns 400 (invalid request) or 409 (state conflict, including a refusal from the robot core).
The rejection body is `{"error": "..."}`.

The command body is `{"action": "<action>", ...}`. It must contain no fields beyond those listed:

| Command | Additional fields | Behavior |
| --- | --- | --- |
| `enable` | None | Enable the robot |
| `disable` | None | Disable the robot. Any client can send this command |
| `stop` | None | Stop the robot. Any client can send this command |
| `home` | None | Move J1–J4 to 0° |
| `execute` | `targets` (with `x`, `y`, `z`, `r`), optional `speed` | Move linearly to the flange coordinates |
| `vacuum` | `operation`: `suck`, `release` or `off` | Control the vacuum suction cup. See [Vacuum suction cup](#vacuum-suction-cup) |
| `keyboard` | `enabled`, `seq`, plus `speed` to enable or `reason` to disable | Enable or disable keyboard control |
| `clear_alarm` | None | Send `ClearError()`, then disable the robot and confirm that it is stationary after the alarm clears |
| `keys` | `seq`, `held` | Send 1 held-key set. `command_id` is `null`. See [Keys](#keys) |

For `keyboard`, `speed` is an integer from 1–100.
For `execute`, optional `speed` is an integer from 1 to the speed cap `20`.
The service rejects invalid values or values above the cap with 400 before queuing movement.
Booleans, strings, fractional values, and non-finite values are invalid.

The movement uses `SpeedL=<speed>,AccL=<min(speed, acceleration cap)>`.
The acceleration cap is `20`.
Both caps are fixed in `apps/control_panel/server.py`.

Without `speed`, the wire command remains `SpeedL=1,AccL=1`, even with higher caps.
These settings do not change `home`, `SpeedFactor`, or keyboard jogging.
An operator must validate speeds above 1 on hardware before unattended transfers use them.
`clear_alarm` is available when the controller has an alarm and feedback is fresh.
It is also available when `recoverable` is `true`: a stop or disable is unconfirmed, the controller reports no alarm, and feedback is fresh.
In this case, the service does not send `ClearError()`. It only disables the robot, confirms that it is stationary, and clears the warning.
The state object's `alarms` field lists current alarms. It is `null` when there are no alarms.
Each item contains `id`, `source` (`controller` or `J1`–`J4`), `description`, and `solution`.
The values for `reason` are `user`, `focus`, `edit`, `speed`, and `modifier`.

### State object

`api/state` and successful command replies return the same state object. In physical robot control mode, it has these fields:

| Field | Meaning |
| --- | --- |
| `mode` | Service mode, `robot` |
| `instance`, `version` | Service instance ID and increasing version number. The instance ID changes after a restart |
| `robot_host` | Robot address |
| `syncing`, `feedback_fresh` | Whether feedback is connected, and whether it is fresh |
| `robot_mode`, `robot_error` | Controller mode and alarm flag |
| `enabled` | `true` in mode 5 or 7 |
| `joints` | Measured joints J1–J4 in degrees |
| `actual` | Flange pose X, Y, Z, R. 4 `null` values during motion |
| `speed_factor` | Global speed percentage from controller feedback |
| `busy`, `locked`, `stop_unconfirmed`, `recoverable` | Operation in progress, control locked, stop unconfirmed, recovery available |
| `alarms` | Current alarm list, described above |
| `message`, `last_command` | Service message and latest command result |
| `vacuum`, `payload_kg` | Measured suction state and payload. See [Vacuum suction cup](#vacuum-suction-cup) |
| `controller_id` | Client ID that holds control, or `null` without a holder |
| `keyboard_enabled`, `keyboard_arming` | Keyboard jog is on, or is turning on |
| `keyboard_active_key`, `keyboard_speed_percent`, `keyboard_disarm_reason` | Active key, keyboard speed, and close reason |
| `service`, `seq`, `ts_unix`, `demo` | Contract fields: `robot`, the version number, Unix time, and `false` |
| `connected` | Feedback is connected and fresh |
| `fault` | `null`, or `{code, message}`. `code` is `stop_unconfirmed` or `robot_alarm` |
| `lease` | `{holder, remaining_s}`. `holder` is the same as `controller_id` |

Read-only mode returns only `mode`, `robot_host`, `enabled` (`null`), `syncing`, `actual` (4 `null` values), `joints`, `robot_mode`, `robot_error`, `feedback_fresh`, and `message`.

### Contract lease and commands

- `api/heartbeat` takes the lease, and the service connects to the robot. When another client holds the lease, it returns 409.
- `api/renew` renews only a lease that the caller holds. After the lease expires or is released, it returns 409 and does not take the lease again.
- `api/release` stops the robot and releases the lease. If the robot is enabled, the service keeps feedback and the control lock until an explicit `disable` confirms mode 4 and rest. If the robot is already disabled and idle, a confirmed mode 4 is sufficient.
- When the lease expires, the service does the same stop and disconnect as `api/release`. If the robot is enabled, the service stops and locks, and keeps feedback until a disable.
- While the service is locked, a new holder can still take the lease and send `clear_alarm` to recover. The service still refuses motion.
- `api/command` returns 202, `command_id`, and `state` when it accepts a command. A refusal from the robot core returns 409.
- The commands `start_sync` and `stop_sync` do not exist. Use the lease routes.

A command is finished when `state.last_command.id` equals its `command_id`.
A motion command succeeds only when `busy` is `false` and neither `locked` nor `stop_unconfirmed` is `true` (except for `stop` and `disable`).
The result is unknown after a service restart (`instance` changes) or when a later stop replaces the command.
A client must not send a command again. After a lost reply, read the state first.

### Contract discovery, stop, and events

Physical robot control mode provides `api/describe`, `api/stop`, and `api/events`.
`api/stop` checks the request headers and client ID described above.

`api/describe` returns `service`, `version`, `contract`, `demo`, `zones`, `lease_seconds`, `on_lease_expiry`, and `commands`.
`contract` is 1, and `demo` is `false`.
Each command's `params` describes its arguments as JSON Schema, excluding `action`.
For `stop` and `disable`, `any_client` is `true`.
For `keyboard` and `keys`, `ui_only` is `true`. Only interactive interfaces use these commands.

The `api/stop` body is `{"wait_s": 0}`.
`wait_s` is optional and defaults to 0. It must be a finite number from 0–15 seconds.
The route runs the stop in the stop worker and releases the state lock while waiting.
Any client can stop without holding the control lease.
While the same type of stop runs, a repeated request starts no new stop.
A successful response is 200 with `{"stop": "confirmed", "state": {...}}`.

| `stop` | Meaning |
| --- | --- |
| `confirmed` | Robot stop is confirmed |
| `pending` | The wait ended while a stop worker was still active |
| `unconfirmed` | Robot stop is unconfirmed |

A later successful stop cannot clear an earlier unconfirmed stop.
The response preserves the lock state. It does not retry motion automatically.

The state's `last_command` starts as `null`.
After a command completes or is rejected, it contains `id`, `action`, `client`, `result`, `message`, and `finished_at`.
`finished_at` is a Unix timestamp in seconds.
`result` is `ok`, `failed`, `rejected`, or `stopped`.
An asynchronous command updates its result after its worker completes.
A rejection from another client does not hide the final result of an active command.
An older canceled worker cannot overwrite a newer completed stop.

`api/events` returns `{"events": [...], "next": 0}`.
Each event contains `seq`, `ts_unix`, `level`, `message`, and `fields`.
`since` defaults to 0 and must be a nonnegative integer.
The response contains only events with sequences greater than `since`. Use `next` for the next query.
The service retains the latest 1000 events. Sequence numbering restarts with the service.
Events record state messages, lock and unlock, and unconfirmed stops and their clearance.
They also record feedback mode and alarm flag changes, alarms, and keyboard close reasons. The `message` of each event is the related service log line.

### Keys

The `keys` command body is `{"action":"keys","seq":<seq>,"held":[<key>]}`.
`seq` shares one increasing sequence with the keyboard on and off commands. The service ignores a message that is not greater than the last value.
`held` lists the keys that are held now: W, A, S, D, Q, E, R, F, Z, or X.
While a key is held, the client must send it again within 0.4 seconds. The lab console sends it again every 150 ms.
If the service receives no update within 0.4 seconds, it stops the jog and disables keyboard control.
The service log does not record `keys` requests.

## Stage service API

The JSON API is under `/api/` by default. Use `http://127.0.0.1:8767/` for `labkit.client.ServiceClient`. The `/contract/api/` compatibility aliases share the same handlers and lease. The service follows the [instrument service contract](adr/0001-instrument-service-contract.en.md).

The Arduino broadcasts encoder and switch state. The service drains telemetry every 0.05 s. Public coordinates and relative distances use motor shaft rotations. Speeds use rotations/s. `ccw` increases the coordinate. `cw` decreases it. The firmware owns the control loop.

### Stage automation contract

| Interface | Purpose |
| --- | --- |
| `GET /api/describe` | Read command schemas, limits, capabilities and lease rules |
| `GET /api/state` | Read motors, connection, lease, command result and cancellation epoch |
| `POST /api/heartbeat` | Acquire or renew control; another holder returns 409 |
| `POST /api/renew` | Renew only the caller's existing, unexpired lease |
| `POST /api/release` | Release ownership without sending stop |
| `POST /api/command` | Send `action` and parameters; accepted requests return 202, `command_id` and `state` |
| `POST /api/stop` | Request stop without a lease; `wait_s` is 0–15 s, default 0 |
| `GET /api/events?since=0` | Read activity and the `next` cursor |

| `action` | Parameters | Meaning |
| --- | --- | --- |
| `connect` | `port` | Connect a port listed in state. Demo accepts only `DEMO`. Available during a service fault |
| `move` | `motor`, `direction`, `speed`, `rotations` | Relative encoder move |
| `move_guarded` | `motor`, `direction`, `speed`, `rotations`, `stop_epoch` | Relative move with cancellation check |
| `move_to_position` | `motor`, `position`, `speed` | Absolute encoder target after calibration |
| `move_to_position_guarded` | `motor`, `position`, `speed`, `stop_epoch` | Absolute target with cancellation check |
| `calibrate` | `motor`, `speed` | Seek lower, seek upper, then move to center |
| `reset_position` | `motor` | Set position to zero and clear both bounds |
| `set_limit` | `motor` | Record positive upper or negative lower bound; reject zero |
| `clear_upper_limit` | `motor` | Remove upper bound |
| `clear_lower_limit` | `motor` | Remove lower bound |
| `clear_fault` | `motor` | Reset feedback fault without motion |
| `hold` | `motor`, `direction`, `speed`, `hold_id`, `sampled_at` | Start continuous movement |
| `keepalive` | `motor`, `hold_id` | Renew matching hold |
| `release_hold` | `motor`, `hold_id` | End matching hold |
| `stop_all` | None | Stop request available to any client |
| `set_microstep` | `microstep` | Confirm fixed 1/8 mode |
| `set_microstep_guarded` | `microstep`, `stop_epoch` | Confirm fixed mode with cancellation check |

`motor` is `bottom`, `top` or `insert`. `direction` is `cw` or `ccw`. `rotations` accepts 0.001–10000. `speed` accepts 0.001–5, further limited by the motor's `max_speed`. `position` is nonnegative and must lie within the calibrated bounds. `microstep` accepts only 8. Missing or extra parameters are rejected.

The service permits 1 moving axis at a time and waits for the stage cooldown before another move. Relative moves and holds require fresh healthy encoder feedback. Absolute moves also require completed calibration and both bounds. Changing zero or bounds invalidates calibration. Ordinary switch contact does not zero the coordinate.

State includes `service`, `seq`, `ts_unix`, `sampled_at`, `busy`, `locked`, `fault`, `lease`, `last_command`, `stop_epoch` and `coordinate_unit`. The coordinate unit is `rotations`. Device capabilities include `closed_loop`, `telemetry_streaming`, `home_switches`, `auto_calibration` and `absolute_positioning`.

Each motor supplies `position_rotations`, `remaining_rotations`, `measured_speed`, `max_speed`, `encoder`, `fault`, `telemetry_age_ms`, `limit_telemetry_age_ms`, `telemetry_fresh`, `switch_mask`, `calibration`, `lower_limit_rotations`, `upper_limit_rotations`, `lower_limit_set`, `upper_limit_set`, `motion_ready`, `positioning_ready` and `position_tolerance_rotations`. Unset bound aliases are null. Freshness requires encoder and supported switch frames no older than 1 s. Raw `position`, `remaining`, `lower_limit` and `upper_limit` also use rotations on encoder firmware.

Calibration phases are `none`, `lower`, `upper`, `center`, `done`, `cancelled` and `failed`. Active phases count as busy, even between internal movements. `warning`, `direction_warning_count` and `recalibration_recommended` report advisory direction pauses. They do not start calibration.

Command acceptance does not mean motion completion. `last_command.result` can be `running`, `ok`, `failed`, `rejected` or `stopped`. A move completes only after fresh idle feedback confirms the measured target within encoder tolerance. Calibration additionally requires `done`. Stops cancel pending motion commands. The service does not retry motion automatically.

For guarded commands, read `stop_epoch` after acquiring control and keep that value throughout the sequence. The service checks it under the motion lock. Stops, ownership changes, disconnection, link loss and unguarded commands invalidate it. Renewal preserves it. A mismatch returns 409. Do not read a new epoch to resume a cancelled sequence.

Generate a new `hold_id` for each hold: 16–64 letters, digits, underscores or hyphens. `sampled_at` must come from state within the last 2 s of the service clock. `release_hold` is repeatable and prevents a later start with the same ID. Holds require keepalive within 1 s, independently of the 3 s control lease.

Discovery advertises `renew_only_heartbeat: true`. Acquire with `ServiceClient.heartbeat()`, then renew with `ServiceClient.renew()` or `heartbeat_thread(renew_only=True)`. Renewal after release or expiry returns 409 without reacquiring. An older service without renewal returns 404 without fallback. Lease expiry requests stop. Release alone does not stop motion.

The dedicated stop route returns `stop` and `state`. Outcomes are `confirmed`, `pending` or `unconfirmed`. Confirmation requires every motor idle with no active calibration or direction pause. Stop waiting permits other stop requests but blocks new motion. An unknown serial outcome or expired stop wait locks the service. Later idle feedback alone does not clear this fault. Check the physical stage, restart with `labctl restart stage`, then reconnect. Reconnection returns 409 while stop confirmation is in progress.

### Stage connection and compatibility

When disconnected, state `ports` lists available serial ports as `{"device", "description"}`. The list is empty while connected. `MG400_STAGE_SERIAL` restricts the list to that device. Use the `connect` command through `POST /api/command`. There is no separate operator connect route.

Legacy `steps`, millimetre targets, named pulse poses and camera calibration do not create encoder motion. Existing geometry files remain inactive. Use the commands above and rotation-based Sample Transfer profiles.

### Access checks

The service checks no token, `Host` or `Origin`. See [Access and client ID](#access-and-client-id). POST requests require `Content-Type: application/json`, `X-MG400-Client` and a JSON object. Client IDs use 1–128 printable ASCII characters. The request-body limit is 4096 bytes.

Operator errors use `{"error": "..."}`:

| Status | Meaning |
| --- | --- |
| 400 | Invalid parameters or unlisted port |
| 409 | Lease, firmware capability, calibration or motor-state conflict |
| 413 | Body exceeds 4096 bytes |
| 415 | Request is not JSON |
| 503 | Serial failure; connection closed |

## Physical robot control reference

Use this section when you change physical robot motion or suction interfaces.
For deployment configuration, see [Host deployment](deploy.en.md).

### Controller joint limits

J1–J4 use the controller's `QActual` / `GetAngle()` angles in degrees.
J3 does not subtract J2 or change sign or zero point.
The model's elbow joint uses `J3−J2`. It cannot replace controller J3 in range checks.

[Appendix A of the MG400 User Guide V1.7](https://download.dobot.cc/DobotStudio-pro/20231116/Dobot%20MG400%20User%20Guide%20V1.7_20231116_en.pdf)
gives these motion ranges:

| Joint | Manufacturer's specified range (degrees) |
| --- | --- |
| J1 | −160 … 160 |
| J2 | −25 … 85 |
| J3 | −25 … 105 |
| J4 | −360 … 360 |

Joint targets have a 1.05° inward margin.
The current position and path are checked against the original ranges.
When the current position is within the original range, it can move from the margin area toward an allowed target.
J4 uses the manufacturer's ±360° range. Its allowed target range is ±358.95°.
Angles use raw controller readings without adding or subtracting 360°.
While the robot is disabled, the rest check and the angle agreement checks use J1–J3 only. A disabled J4 has no holding torque, and the tool's gas tube turns it.

The conditional J2 lower limit and `J3−J2` check retain the
[HarvestX reference model limits](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_common/include/mg400_common/kinematics.hpp).
When J3 > 0°, the original J2 lower limit is −25°.
When J3 ≤ 0°, the lower limit tightens to −19°.
After the target margin is applied, the corresponding lower limits are −23.95° and −17.95°.

`J3−J2` is the model elbow joint's rotation from zero. Its original range is ±60°.
After the target margin is applied, the difference range is ±58.95°.
For example, J2=20° and J3=90° are each within their limits. Their difference is 70°, so this combination is rejected.
These checks do not mean that this controller's limit configuration has been read.
Successful forward kinematics means only that coordinate calculation succeeded. It does not prove that the target is within the motion range.
A disabled robot with no alarm also does not prove that its current pose permits motion to start.

### Motion commands and coordinate cache

Without `speed`, `execute` uses `MovL` with `SpeedL=1,AccL=1`.
Controller inverse kinematics validates the target joint limits. The controller plans the straight path.
Reset uses `JointMovJ` with `SpeedJ=5,AccJ=5`.
`execute` and reset do not change global speed or tool configuration.

When keyboard jog turns on, the service sends `SpeedFactor(<speed>)`. This sets the global speed.
While a key is held, the service sends the continuous jog command `MoveJog`:

| Key | Command |
| --- | --- |
| W/S | `MoveJog(X+,CoordType=0,User=8,Tool=0)`, `MoveJog(X-,CoordType=0,User=8,Tool=0)`: reach out and in along the J1 direction |
| A/D | `MoveJog(J1+)`, `MoveJog(J1-)` |
| Q/E | `MoveJog(J4+)`, `MoveJog(J4-)` |
| R/F | `MoveJog(Z+,CoordType=0,User=0,Tool=0)`, `MoveJog(Z-,CoordType=0,User=0,Tool=0)` |

Before W/S starts, the service checks that User 0 is unchanged. Then it aligns user frame 8 with the current J1 through `SetUser(8,{0,0,0,J1})`.
When a continuous jog approaches a joint limit, the service stops it early. The early stop band is 0.08° for each 1% of speed.

When a joint is in the 1.05° margin band, the service uses finite trajectories to move inward:
W/S, R/F, and Q/E use `MovL` with `SpeedL=2,AccL=20`.
A/D uses `JointMovJ` with `SpeedJ=5,AccJ=20`.
Joint motion checks the joint ranges along the entire segment. It does not include collision checks for the physical setup.

A/D uses only measured joint feedback at startup and during motion. It does not query `GetPose`.
After the action ends, it reads coordinates and updates the state.
The other six directions can reuse measured coordinates read by the same control instance.
For reuse, the measurement must be no more than 0.25 seconds old.
Joint feedback before and after the measurement must also match the current joint feedback.
If these conditions are not met, the control instance reads the coordinates again.

Four-axis protocol reference: [Official Dobot SDK](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python/blob/main/dobot_api.py).

### Vacuum suction cup

The `operation` of the `vacuum` command is one of these values:

| `operation` | Controller commands |
| --- | --- |
| `suck` | `DOExecute(1,0)`, `DOExecute(2,1)` |
| `release` | `DOExecute(2,0)`, `DOExecute(1,1)`, blow for 0.3 seconds, `DOExecute(1,0)` |
| `off` | `DOExecute(2,0)`, `DOExecute(1,0)` |

- The robot must be idle, in controller mode 4 or 5. The command does not enable or move the robot.
- The service sends each command once. Then it waits up to 1 second for the measured DO bit in the 30004 feedback to change.
- After the motion stop command, stop and disable send `DOExecute(1,0)` once if a blow is in progress or feedback shows DO1 high. They do not wait for the blow to end and send no DO2 command. If that command fails, the result of `JointControl.stop()` contains `blow_error`; the stop itself does not fail because of it. The robot service does not report `blow_error`, and disable does not record the error.
- The `vacuum` field of the state object contains `suck`, `blow` and `held`. `suck` and `blow` are the measured levels of DO2 and DO1, or `null` without feedback. `held` is the measured level of DI8 (`GRIP_DI`), or `null` when `GRIP_DI` is `None` or without feedback.
- The `payload_kg` field of the state object is the payload that the controller reports, in kg.
- The constants are in `src/mg400/suction.py`: `PRESSURE_DO`, `VACUUM_DO`, `BLOW_SECONDS` and `GRIP_DI`.
- The offline simulator answers `DOExecute` and reports the DO bits in its feedback frames. While DO2 is high, the simulator also sets DI8.
- For the wiring and the feedback fields, see [Vacuum suction cup](hardware.en.md#vacuum-suction-cup).
