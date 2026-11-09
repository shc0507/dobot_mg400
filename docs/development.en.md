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

`uv sync` installs the `dev` and `stage` dependency groups by default.
The `stage` group contains Flask, waitress, and pySerial for the stage service.
The robot service and interactive terminal use only the standard library and do not require the `stage` group.

Tests do not connect to a robot or Arduino.
For pull requests and pushes to `main`, GitHub Actions (`.github/workflows/ci.yml`) runs these checks on Python 3.12.
`pytest` runs Python tests. `node --test` runs the Lab Console JavaScript tests.
Offline checks cannot replace physical robot verification.

Shared test fixtures are in `tests/conftest.py`.
The stage test fixtures `stage_rig`, `stage_ports`, and `stage_usb` are also in this file. Tests use them by name.
Test doubles are objects that replace real devices in tests.
Controller, connection, and web service doubles are in `tests/helpers.py`, `tests/sim_mg400.py`, and `tests/panel_ws.py`.
Stage clock, serial port, and serial port list doubles are in `tests/stage_helpers.py`.
Reuse these modules in new tests. Do not duplicate them in test files.

For `examples/test.ipynb`, add `--group notebook` to the synchronization command.
To keep other packages that you installed manually, also add `--inexact`.

## Offline robot simulator

`mg400.sim` simulates the controller on a loopback address.
It accepts only numeric loopback addresses. The default is `127.0.0.2`.
The controller ports are `29999`, `30003`, and `30004`. The feedback interval is `8 ms`.
At startup, the robot is disabled.

Run these commands in the project root:

```bash
sim_dir=$(mktemp -d)
uv run python -m mg400.sim --host 127.0.0.2 &
sim_pid=$!
MG400_CONTROLLER_LOCK_DIR="$sim_dir/locks" uv run python apps/control_panel/server.py \
  --bind 127.0.0.1 --port 18766 --robot-host 127.0.0.2
kill "$sim_pid"
wait "$sim_pid"
rm -r "$sim_dir"
```

In another terminal, use the [Python client](#python-client) to connect to `http://127.0.0.1:18766/`. Send these commands in sequence:

1. Run `start_sync`. Wait for feedback to update.
2. Run `enable`. Wait for the robot to become enabled.
3. Send the target coordinates with `execute`.
4. Run `stop_sync`. Wait for stop confirmation, then run `disable`.

Press `Ctrl+C` in the terminal to stop the robot service.
The next commands terminate this simulator process and remove the temporary lock directory.

The simulator reaches joint and Cartesian targets immediately. It does not calculate real kinematics, paths, or collisions.
Inverse kinematics returns the current joint values. `J1` jogging changes joint feedback. Other jogging changes only the motion mode.
These limitations apply only to the simulator. Production control code and stop confirmation rules remain unchanged.
Offline rehearsal cannot replace hardware acceptance.

## Code structure

| Path | Purpose |
| --- | --- |
| `config/robot.toml` | Robot address, used when `MG400_ROBOT_HOST` is not set |
| `apps/control_panel/` | Standalone Python robot service, without a web page |
| `apps/layout-studio/` | Offline layout editor that uses simulated data and sends no control commands |
| `config/systemd/` | systemd definitions for all services, with configuration from `/etc/mg400/mg400.env` |
| `deploy/` | The `install.py` installer, the `mg400ctl` command for routine operations, and configuration examples |
| `src/mg400/` | Backend, control protocol, and feedback processing |
| `src/mg400/sim/` | Loopback controller simulator, with a shared test model |
| `src/stage_controller/` | Stage service: Flask interface and serial protocol |
| `firmware/three-motor-demo/` | Stage Arduino firmware and wiring instructions in English |
| `scripts/` | Camera preview, control mode switching, and the interactive terminal entry point |
| `examples/` | Single-file tutorials and notebooks |
| `tests/` | Python and JavaScript offline tests |
| `docs/` | Operating, deployment, and development guides. `docs/agents/` contains agent procedures |
| `work/` | Local runtime data, backups, and temporary files, excluded from Git |

Open the [layout editor](../apps/layout-studio/index.html) in a browser to edit and export interface layouts.

`terminal_control.py` provides the `mg400-robot` interactive terminal for the physical robot.
It reuses the connection lock, feedback checks, and action execution from `web_control.py`. It does not start an HTTP service.

`apps/control_panel/server.py` is the robot service. It provides the HTTP interface and WebSocket interface, without a web page.
The Motion workspace of the Lab Console controls the robot and suction cup through this service.
`websocket.py` uses the standard library for WebSocket frame encoding and decoding, and for connection writer threads.
See [Robot service interface](#robot-service-interface) for interface details.

`web_control.py` dispatches WebSocket key jogging. Robot press-and-hold control reuses `keyboard_jog.py`.
`web_keyboard.py` validates key input. It maps Z to suck and X to release.

The robot service controls the robot through the TCP protocol.
`controller_protocol.py` parses command replies. `feedback.py` reads joint feedback.
`joint_control.py` executes motion with limits and the suction outputs.
For TCP protocol references and completion criteria, see [Physical robot control interface](hardware.en.md).
There is currently no ROS 2 backend or visual perception.

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

After `start_sync`, the robot service connects to the robot and holds the connection lock.
The Lab Console and custom clients read state and send commands through the service.
`mg400-robot` and `scripts/mg400_control_mode.py` connect directly to the robot.
They share the connection lock with the service and cannot connect at the same time as the service.
The service uses only the Python standard library.

| Interface | Purpose |
| --- | --- |
| `GET {prefix}api/state` | Read the state once |
| `GET {prefix}api/describe` | Read the contract version, command parameters, and lease behavior |
| `POST {prefix}api/stop` | Request a stop and read its confirmation status |
| `GET {prefix}api/events?since=<seq>` | Read journal events after the specified sequence |
| `POST {prefix}api/command` | Send one command |
| `POST {prefix}api/heartbeat` | Renew the control lease |
| `GET {prefix}api/ws` | WebSocket connection: state updates, commands, heartbeats, and keys |

`{prefix}` is the value of `--base-path`, which defaults to `/`.
In this section, angle brackets show placeholders. For example, `<client-id>` is the client ID.
Keys can only be sent through WebSocket.

### Access and client ID

The service listens on `127.0.0.1` and does not check tokens, `Host`, or `Origin`. The VPN is the access boundary.

Physical robot control mode identifies clients by a client ID of 1–128 printable ASCII characters.
Only one client ID has control at a time.
The controlling client must renew the control lease within 3 seconds. The lease defines how long control remains valid.
Heartbeats, key updates, and commands from the controlling client renew the lease.
If the service receives no renewal within 3 seconds, it requests a robot stop and locks control.

### HTTP interface

POST requests must meet these conditions:

1. Include `Content-Type: application/json` and `X-MG400-Panel: 1`.
2. In physical robot control mode, include `X-MG400-Client: <client-id>`.
3. Send a JSON object of 1–4096 bytes, with its length specified by `Content-Length`.

Successful `api/command` and `api/heartbeat` requests return 200 and the state object.
Rejection returns 400 (invalid request), 403 (refused), or 409 (state conflict).
The rejection body is `{"error": "..."}`.

The command body is `{"action": "<action>", ...}`. It must contain no fields beyond those listed:

| Command | Additional fields | Behavior |
| --- | --- | --- |
| `start_sync` | None | Connect to the robot and take control |
| `stop_sync` | None | Stop the robot and release control. Keep feedback until disable and rest are confirmed |
| `enable` | None | Enable the robot |
| `disable` | None | Disable the robot. Any client can send this command |
| `stop` | None | Stop the robot. Any client can send this command |
| `home` | None | Move J1–J4 to 0° |
| `execute` | `targets` (with `x`, `y`, `z`, `r`), optional `speed` | Move linearly to the flange coordinates |
| `vacuum` | `operation`: `suck`, `release` or `off` | Control the vacuum suction cup. See [Vacuum suction cup](#vacuum-suction-cup) |
| `keyboard` | `enabled`, `seq`, plus `speed` to enable or `reason` to disable | Enable or disable keyboard control |
| `clear_alarm` | None | Send `ClearError()`, then disable the robot and confirm that it is stationary after the alarm clears |

For `keyboard`, `speed` is an integer from 1–100.
For `execute`, optional `speed` is an integer from 1 to the host's `MG400_ROBOT_MAX_SPEED_L` cap.
The service rejects invalid values or values above the cap with 400 before queuing movement.
Booleans, strings, fractional values, and non-finite values are invalid.

The movement uses `SpeedL=<speed>,AccL=<min(speed, acceleration cap)>`.
The acceleration cap is `MG400_ROBOT_MAX_ACC_L`.
Both host settings default to 1 and accept integers from 1–100. Empty settings also use 1.
Invalid host settings prevent service startup. Restart the service after changing a cap.

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

`api/state`, WebSocket state updates, and successful replies return the same state object. In physical robot control mode, it has these fields:

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

Read-only mode returns only `mode`, `robot_host`, `enabled` (`null`), `syncing`, `actual` (4 `null` values), `joints`, `robot_mode`, `robot_error`, `feedback_fresh`, and `message`.

### Contract discovery, stop, and events

Physical robot control mode provides `api/describe`, `api/stop`, and `api/events`.
`api/stop` checks the POST headers and client ID described above.

`api/describe` returns `service`, `version`, `contract`, `demo`, `zones`, `lease_seconds`, `on_lease_expiry`, and `commands`.
`contract` is 1, and `demo` is `false`.
Each command's `params` describes its arguments as JSON Schema, excluding `action`.
For `stop` and `disable`, `any_client` is `true`.
For `keyboard`, `ui_only` is `true`. Only interactive interfaces use this command.
`start_sync` takes control. The existing heartbeat route only renews it.

The `api/stop` body is `{"wait_s": 0}`.
`wait_s` is optional and defaults to 0. It must be a finite number from 0–15 seconds.
The route uses the existing stop worker and releases the state lock while waiting.
Any client can stop without holding the control lease.
Repeated requests follow the existing stop deduplication rules.
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
Events record existing state messages, lock and unlock, and unconfirmed stops and their clearance.
They also record feedback mode and alarm flag changes, alarms, and keyboard close reasons. They add no journal lines.

### Python client

`mg400.client.RobotClient(url, client_id)` uses the service base URL and an explicit client ID.
Use a unique ID for each program. Do not share an ID between active programs.
Construction opens no connection. `connect()` checks WebSocket protocol 1 and service contract 1, then starts the client threads.
Use `http://`, `https://`, `ws://`, or `wss://`, including the service base path when configured.

Start the [offline simulator and robot service](#offline-robot-simulator) before this example.
The example sends movement commands to that simulator.

```python
from mg400.client import RobotClient

with RobotClient("http://127.0.0.1:18766/", "recipe-example") as robot:
    robot.run("start_sync")
    robot.run("enable")
    result = robot.run("execute", timeout=30, targets={"x": 260, "y": 12, "z": 42, "r": 31})
    print(result["last_command"])
```

- `state` returns a copy of the latest state. It is `None` before connection. `connected` reports whether the WebSocket is available.
- `command(action, **fields)` sends 1 request and returns the protocol reply. Check `status`. Acceptance does not mean completion.
- `wait(predicate, timeout)` returns a state that satisfies the predicate. A timeout raises `TimeoutError`.
- `run(action, timeout=30, **fields)` waits for `busy == False` and a new `last_command` for its action and client.
- `vacuum(operation, timeout=30)` sends 1 `vacuum` command through `run()`. `operation` is `suck`, `release` or `off`. Other values raise `ValueError`. See [Vacuum suction cup](#vacuum-suction-cup).
- `vacuum_state` returns the `vacuum` field of the latest state, or `None` before connection.
- `keys(seq, held)` sends 1 held-key set through the WebSocket and returns the protocol reply. It does not retry.
- `RobotClientError` reports a refusal or uncertain outcome. It retains `status` and `close_code` when available.

`run()` accepts `ok`. For `stop`, `disable`, and `stop_sync`, it also accepts `stopped` when the stop remains confirmed.
`run("start_sync")` can succeed while the service is locked, so the new holder can send `clear_alarm` to recover. The service still refuses motion.
A rejection, failed operation, superseding stop, or service restart cannot count as successful motion.
Use 1 active program per client ID. Ordinary commands from the same client are serialized during `run()`.
A different thread can still call `command("stop")`, `command("disable")`, `run("stop")`, or `run("disable")`.

While this client holds control, it sends heartbeats every 0.5 seconds.
After a dropped socket, it attempts transport reconnection for up to 2.5 seconds, within the 3-second lease.
It never replays a command. A lost reply or timeout can leave delivery or completion unknown.
Inspect the service state before another action. Do not automatically retry motion.
Hello, protocol, and replacement failures end the connection attempts.

When the socket is unavailable, `command("stop")` and `command("disable")` use HTTP with the same client ID.
The fallback checks contract 1 before sending a command.
Its replies preserve `locked` and `stop_unconfirmed`. A reply still means acceptance, not verified completion.
Other commands have no HTTP fallback. HTTP requests follow no redirects and have no automatic retries.

Context exit runs `stop_sync` while this client holds control, then closes the connection and stops its threads.
If the arm is enabled, the service keeps feedback and blocks motion until an explicit `disable` confirms mode 4 and rest.
The client does not disable the arm automatically. After a confirmed stop, `disable` can complete the disconnect without a service restart.
The optional `keep_control=True` skips `stop_sync`. It does not keep heartbeats running after exit.
Calling `close()` directly also ends the threads without renewing or releasing control.
The service then applies its normal lease-expiry stop and lock.
The optional `timeout=5` sets the connection and reply timeout in seconds.

### WebSocket interface

Communicate in this sequence:

1. Within 3 seconds of connection, send `{"type":"hello","protocol":1,"client":"<client-id>"}`.
2. Wait for the service to send `{"type":"state","state":{...}}`.
3. Send a request: `{"type":"<type>","id":<integer>,"body":{...}}`.
4. Match the reply by `id`.

`protocol` is the message format version, currently 1. The hello must contain no other fields.
The fields in `state` are the same as in `api/state`.
The service sends state updates when the state changes.
It also sends an update after each reply.
It sends at least one update per second.

| Request type | `body` |
| --- | --- |
| `command` | Same as the `api/command` body |
| `heartbeat` | `{}` |
| `keys` | `{"seq":<seq>,"held":[<key>]}` |

`id` is an integer from 0 to 2^53, selected by the client.
The service replies in request order, with one reply per request.
A successful reply is `{"type":"reply","id":<id>,"status":200,"state":{...}}`.
A rejected reply is `{"type":"reply","id":<id>,"status":<status>,"error":"..."}`.
Status codes have the same meanings as in the HTTP interface. 500 means an internal service error.
For an invalid `body`, the service replies with an error and keeps the connection open.
A successful `command` reply means that the service accepted the command. It does not mean that the action is complete.
When the action ends, `busy` becomes `false` in the state, and `message` gives the result.
The service writes one log entry per `command`. It does not log `keys` or `heartbeat`.

The `seq` in `keys` shares an increasing sequence number with the keyboard enable/disable command.
The service ignores messages whose value is not greater than the previous value.
`held` lists the keys that are currently held: W, A, S, D, Q, E, R, F, Z, and X.
While a key is held, the client must resend the message within 0.4 seconds. The Lab Console resends it every 150 ms.
If the service receives no update within 0.4 seconds, it stops jogging and disables keyboard control.

For close codes 4400, 4426, or 1013, the service sends `{"type":"error","status":<status>,"error":"..."}` before it closes the connection.

| Close code | Meaning |
| --- | --- |
| 4400 | Invalid hello or request format |
| 4426 | `protocol` does not match the service. Update the client |
| 4409 | A new connection with the same client ID replaced this connection |
| 1013 | Connection capacity is full |
| 1001 | Service shutdown. The service sends the final state before closure |
| 1011 | The service failed while it handled the connection |
| 1002, 1003, 1007, 1009 | Invalid frame format, binary message, text that is not UTF-8, or an oversized message |

Connection rules:

- Messages must be UTF-8 text of no more than 4096 bytes.
- The service sends a ping every 5 seconds. It closes the connection if it receives no frames for 15 seconds.
- Up to 8 client IDs can have connections after hello at the same time.
- Up to 8 connections can wait for hello at the same time. When a new connection exceeds this limit, the service closes the connection that waited longest.
- If `GET api/ws` is not a WebSocket upgrade request, the service returns HTTP 426.
- When the same client ID sends hello again, the service closes the old connection.
- The service immediately disables a client's keyboard control when its latest connection closes.
  The same applies when a new connection with the same client ID replaces it.

Connection closure does not end the control lease.
If the service or proxy closes the connection, the client keeps control if it reconnects and renews within 3 seconds.
During a network interruption, the service might not detect connection closure. The key timeout and control lease stop the robot.

## Stage service API

The stage service HTTP interface uses JSON. All routes are under `{prefix}contract/api/`.
`{prefix}` is the stage base path, fixed at `/`.
The service queries Arduino state every 0.2 seconds and stops expired press-and-hold motion during each query.

### Stage automation contract

The base URL for `labkit.client.ServiceClient` is `http://127.0.0.1:8767/contract`, including `/contract`.
The interface follows the [instrument service contract](adr/0001-instrument-service-contract.en.md).

| Interface | Purpose |
| --- | --- |
| `GET /contract/api/describe` | Read command parameters, limits, demo status, and lease rules |
| `GET /contract/api/state` | Read motor state plus `service`, `seq`, `ts_unix`, `busy`, `locked`, `fault`, `lease`, and `last_command` |
| `POST /contract/api/heartbeat` | Acquire or renew the control lease. Return 409 if another client holds it |
| `POST /contract/api/renew` | Renew only an existing, unexpired lease held by the caller. Otherwise return 409 |
| `POST /contract/api/release` | Release the caller's control lease without sending a stop command |
| `POST /contract/api/command` | Send `action` and its parameters. Acceptance returns 202, `command_id`, and `state` |
| `POST /contract/api/stop` | Any client can request a stop without a lease. `wait_s` is 0–15 seconds, default 0 |
| `GET /contract/api/events?since=0` | Read activity events and the `next` cursor. Record each activity once and also write it to the service journal |

| `action` | Parameters | Completion |
| --- | --- | --- |
| `move` | `motor`, `direction`, `speed`, `steps` | Update `last_command` after fresh feedback confirms that the target motor is idle |
| `hold` | `motor`, `direction`, `speed`, `hold_id`, `sampled_at` | Arduino acknowledges the hold start. The motor continues moving |
| `keepalive` | `motor`, `hold_id` | Arduino acknowledges hold renewal |
| `release_hold` | `motor`, `hold_id` | End the matching hold |
| `stop_all` | None | Arduino acknowledges the stop request. No lease is required. When fault-locked, use the dedicated stop interface |
| `set_microstep` | `microstep` | Arduino confirms the microstep mode |

The values for `motor` are `bottom`, `top`, and `insert`. The values for `direction` are `cw` and `ccw`.
`speed` is an integer from 1–1000. `steps` is an integer from 1–100000. `microstep` is 1, 2, 4, 8, or 16.
The client generates `hold_id`: 16–64 characters, with only letters, digits, `_`, and `-`.
Each press-and-hold operation must use a new `hold_id`.
`sampled_at` comes from the field with the same name in `api/state`. It must be within the service clock's last 2 seconds.
`release_hold` can be sent repeatedly. If it arrives before `hold` with the same `hold_id`, the service rejects the later `hold`.
The service rejects missing or extra parameters.

The control lease lasts 3 seconds by default. Holds still require a separate renewal within 1 second.
Hold renewal does not renew the control lease.
The labkit Flask adapter advertises `renew_only_heartbeat: true` in discovery.
For continuous ownership, acquire explicitly with `ServiceClient.heartbeat()`, then use `ServiceClient.renew()`.
The optional `heartbeat_thread(renew_only=True)` helper uses this strict renewal and stops on its first error.
If a renewal arrives after lease expiry or release, it returns 409 without taking control again.
An older service without `api/renew` returns 404. The client never falls back to acquisition.
The existing `heartbeat()` and default `heartbeat_thread()` retain their acquire-or-renew behavior.
After a renewal failure, stop motion and reconcile the service state before explicitly taking control again.
On lease expiry, the service requests that all motors stop.
Releasing the lease does not stop motion. Wait for completion or request a stop before returning control.

The dedicated stop interface returns `stop` and `state`. The `stop` value is `confirmed`, `pending`, or `unconfirmed`.
It returns `confirmed` only after all motors report idle.
While waiting for feedback, the service permits other stop requests but blocks new motion.
If the stop wait expires or a serial outcome is unknown, `locked` is true and `fault` is non-null.
Later idle feedback does not clear the fault. Check the physical state, then run `mg400ctl restart stage` and connect again. The fault clears only after all motors report idle.
Reconnection returns 409 while stop confirmation is in progress. Feedback from a new connection cannot confirm that the old device stopped.
`last_command` distinguishes `ok`, `failed`, `rejected`, and `stopped`. The service never retries motion automatically.

### Operator routes

The Lab Console also uses these 4 routes, with the same prefix as the contract:

| Interface | Body | Purpose |
| --- | --- | --- |
| `GET /contract/api/ports` | None | List available serial ports. When `MG400_STAGE_SERIAL` is set, list only that device |
| `POST /contract/api/connect` | `{"port": "..."}` | Connect to a serial port listed by `api/ports`. Demo mode accepts only `DEMO`. The caller must hold the stage lease |
| `GET /contract/api/motors/<motor>/reference` | None | Read the camera image and the marker's pixel offset |
| `POST /contract/api/motors/<motor>/teach_reference` | `{"confirm": true}` | Save the current marker position as the reference of that axis. The caller must hold the stage lease |

`api/ports` returns `{"ports": [{"device": "...", "description": "..."}]}`.
On success, `api/connect` and `teach_reference` return the state object with `lease`.

### Access checks

The service checks no token, `Host`, or `Origin`. See [Access and client ID](#access-and-client-id).
POST requests must include `Content-Type: application/json` and `X-MG400-Client`. The body must be a JSON object.
Client IDs contain 1–128 printable ASCII characters.
The body limit is 4096 bytes.

On rejection, the operator routes return the body `{"error": "..."}`, with these status codes:

| Status code | Meaning |
| --- | --- |
| 400 | Invalid request, or the serial port is not in the current list |
| 409 | State conflict, such as no lease, an existing connection, or a motor still moving |
| 413 | The request body exceeds 4096 bytes |
| 415 | The request is not JSON |
| 503 | Serial communication failed. The service closed the serial connection |

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
- After the motion stop command, stop and disable send `DOExecute(1,0)` once if a blow is in progress or feedback shows DO1 high. They do not wait for the blow to end and send no DO2 command. If that command fails, the stop result contains `blow_error`; the stop itself does not fail because of it.
- The `vacuum` field of the state object contains `suck`, `blow` and `held`. `suck` and `blow` are the measured levels of DO2 and DO1, or `null` without feedback. `held` is `null` while `GRIP_DI` is not set.
- The `payload_kg` field of the state object is the payload that the controller reports, in kg.
- The constants are in `src/mg400/suction.py`: `PRESSURE_DO`, `VACUUM_DO`, `BLOW_SECONDS` and `GRIP_DI`.
- The offline simulator answers `DOExecute` and reports the DO bits in its feedback frames.
- For the wiring and the feedback fields, see [Vacuum suction cup](hardware.en.md#vacuum-suction-cup).
