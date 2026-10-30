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
| Write a stage panel client | [Stage panel API](#stage-panel-api) |
| Change joint limits, motion parameters, or the gripper protocol | [Physical robot control reference](#physical-robot-control-reference) |

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
The `stage` group contains Flask, waitress, and pySerial for the stage panel.
The robot web service and interactive terminal use only the standard library and do not require the `stage` group.

Tests do not connect to a robot or Arduino.
For pull requests and pushes to `main`, GitHub Actions (`.github/workflows/ci.yml`) runs these checks on Python 3.12.
`pytest` runs Python tests. `node --test` runs camera preview and web keyboard tests.
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

`mg400.sim` simulates the controller and gripper on a loopback address.
It accepts only numeric loopback addresses. The default is `127.0.0.2`.
The controller ports are `29999`, `30003`, and `30004`. The feedback interval is `8 ms`.
At startup, the robot is disabled and the gripper is initialized.

Run these commands in the project root:

```bash
sim_dir=$(mktemp -d)
uv run python -m mg400.sim --host 127.0.0.2 --gripper-feedback "$sim_dir/gripper.json" &
sim_pid=$!
MG400_CONTROLLER_LOCK_DIR="$sim_dir/locks" uv run python apps/control_panel/server.py \
  --bind 127.0.0.1 --port 18766 --robot-host 127.0.0.2 \
  --gripper-feedback "$sim_dir/gripper.json"
kill "$sim_pid"
wait "$sim_pid"
```

Open `http://127.0.0.1:18766/` and do these steps in sequence:

1. Click `Connect & take control`. Wait for feedback to update.
2. Click `Enable robot`. Wait for the robot to become enabled.
3. Click `Use actual`, enter the target coordinates, then click `Execute targets`.
4. Send a gripper target. Make sure that the feedback position updates.
5. Click `Disconnect`. Wait for stop confirmation.

Press `Ctrl+C` in the terminal to stop the web service.
The next two commands terminate this simulator process.
Use a new directory for each startup. The simulator refuses to overwrite existing feedback files, command files, or command locks.

The simulator reaches joint and Cartesian targets immediately. It does not calculate real kinematics, paths, collisions, or gripping force.
Inverse kinematics returns the current joint values. `J1` jogging changes joint feedback. Other jogging changes only the motion mode.
These limitations apply only to the simulator. Production control code and stop confirmation rules remain unchanged.
Offline rehearsal cannot replace hardware acceptance.

## Code structure

| Path | Purpose |
| --- | --- |
| `config/robot.toml` | Robot address, used when `MG400_ROBOT_HOST` is not set |
| `apps/control_panel/` | Standalone Python web service and interface |
| `apps/layout-studio/` | Offline layout editor that uses simulated data and sends no control commands |
| `config/systemd/` | systemd definitions for all services, with configuration from `/etc/mg400/mg400.env` |
| `deploy/` | The `install.py` installer, the `mg400ctl` command for routine operations, and configuration examples |
| `src/mg400/` | Backend, control protocol, and feedback processing |
| `src/mg400/sim/` | Loopback controller and file-based gripper simulator, with a shared test model |
| `src/stage_controller/` | Stage panel: Flask service, serial protocol, and interface |
| `firmware/three-motor-demo/` | Stage Arduino firmware and wiring instructions in English |
| `scripts/` | Camera preview, control mode switching, and USB collection |
| `examples/` | Single-file tutorials and notebooks |
| `tests/` | Python and JavaScript offline tests |
| `docs/` | Operating, deployment, and development guides. `docs/agents/` contains agent procedures |
| `work/` | Local runtime data, backups, and temporary files, excluded from Git |

Open the [layout editor](../apps/layout-studio/index.html) in a browser to edit and export interface layouts.

`terminal_control.py` provides the `mg400-robot` interactive terminal for the physical robot.
It reuses the connection lock, feedback checks, and action execution from `web_control.py`. It does not start an HTTP service.

`apps/control_panel/server.py` is the robot service. It provides the web panel, HTTP interface, and WebSocket interface.
`websocket.py` uses the standard library for WebSocket frame encoding and decoding, and for connection writer threads.
See [Robot service interface](#robot-service-interface) for interface details.

`web_control.py` dispatches web keyboard jogging. Robot press-and-hold control reuses `keyboard_jog.py`.
`web_keyboard.py` validates key input and executes small gripper steps.

The physical robot panel controls the robot through the TCP protocol.
`controller_protocol.py` parses command replies. `feedback.py` reads joint feedback.
`joint_control.py` executes motion with limits. `gripper_feedback.py` processes gripper feedback.
For TCP protocol references and completion criteria, see [Physical robot control interface](hardware.en.md).
There is currently no ROS 2 backend or visual perception.

`src/stage_controller/` is the stage panel. It does not import `mg400` or use the robot connection lock.
`__main__.py` reads `MG400_STAGE_*` environment variables and command-line arguments, then starts waitress.
On SIGINT, SIGTERM, or SIGHUP, it stops the motors and disconnects the serial port before it exits.
`app.py` provides the HTTP interface and access checks.
`controller.py` manages the serial connection, cooldown, press-and-hold control, and activity log.
`protocol.py` implements the Arduino serial protocol and demo mode.
All serial commands execute inside the `MotorController` lock. Commands for one move do not interleave with other requests.
When you change serial commands, update both the firmware and `DemoTransport`.
See [Stage panel API](#stage-panel-api) for interface details.

## Robot service interface

After `start_sync`, the robot service connects to the robot and holds the connection lock.
The web panel and custom clients read state and send commands through the service.
`mg400-robot` and `scripts/mg400_control_mode.py` connect directly to the robot.
They share the connection lock with the service and cannot connect at the same time as the service.
The service uses only the Python standard library.

| Interface | Purpose |
| --- | --- |
| `GET {前缀}api/state` | Read the state once |
| `GET {前缀}api/describe` | Read the contract version, command parameters, and lease behavior |
| `POST {前缀}api/stop` | Request a stop and read its confirmation status |
| `GET {前缀}api/events?since=<seq>` | Read journal events after the specified sequence |
| `POST {前缀}api/command` | Send one command |
| `POST {前缀}api/heartbeat` | Renew the control lease |
| `GET {前缀}api/ws` | WebSocket connection: state updates, commands, heartbeats, and keys |

`{前缀}` means the prefix: the value of `--base-path`, which defaults to `/`.
Keys can only be sent through WebSocket.

### Authentication and client ID

When `MG400_PANEL_TOKEN` is set, every request must include the access token.
HTTP requests send the token through `Authorization: Bearer <密钥>`, where the placeholder means the token.
WebSocket sends the token in the hello message.

Without an access token, the service accepts requests only when `Host` is `127.0.0.1`, `localhost`, or `[::1]`.
This restriction prevents DNS rebinding access to the panel, where a malicious webpage resolves its domain name to the local machine.
The service also rejects requests with `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`, or `X-Real-IP` headers in this mode.
You must set an access token for access through a reverse proxy.

Physical robot control mode identifies pages and programs by a client ID of 1–128 printable ASCII characters.
Only one client ID has control at a time.
The controlling client must renew the control lease within 3 seconds. The lease defines how long control remains valid.
Heartbeats and key updates both renew the lease.
The web panel sends a heartbeat every 0.5 seconds.
If the service receives no renewal within 3 seconds, it requests a robot stop and locks control.

### HTTP interface

POST requests must meet these conditions:

1. Include `Content-Type: application/json` and `X-MG400-Panel: 1`.
2. In physical robot control mode, include `X-MG400-Client: <客户端 ID>`. The placeholder means the client ID.
3. Send a JSON object of 1–4096 bytes, with its length specified by `Content-Length`.

Successful `api/command` and `api/heartbeat` requests return 200 and the state object.
Rejection returns 400 (invalid request), 401 (incorrect access token), 403 (unauthorized), or 409 (state conflict).
The rejection body is `{"error": "..."}`.

The command body is `{"action": "<命令>", ...}`, where the placeholder means the command. It must contain no fields beyond those listed:

| Command | Additional fields | Behavior |
| --- | --- | --- |
| `start_sync` | None | Connect to the robot and take control |
| `stop_sync` | None | Stop the robot and disconnect after confirmation |
| `enable` | None | Enable the robot |
| `disable` | None | Disable the robot. Any client can send this command |
| `stop` | None | Stop the robot. Any client can send this command |
| `home` | None | Move J1–J4 to 0° |
| `execute` | `targets`: `x`, `y`, `z`, `r`, and optional `grip`, plus optional `speed` | Move linearly to the flange coordinates, then execute the gripper target after arrival |
| `grip` | `position`: an integer from 0–1000 | Set the gripper opening |
| `initialize_gripper` | None | Initialize the gripper. This opens the fingers |
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
These settings do not change `home`, global `SpeedFactor`, or keyboard jogging.
An operator must validate speeds above 1 on hardware before unattended transfers use them.
`clear_alarm` is available when the controller has an alarm and feedback is fresh.
It is also available when `recoverable` is `true`: an unconfirmed stop during an alarm locked control, and the alarm was cleared outside the panel.
In this case, the service does not send `ClearError()`. It only disables the robot and confirms that it is stationary.
The state object's `alarms` field lists current alarms. It is `null` when there are no alarms.
Each item contains `id`, `source` (`controller` or `J1`–`J4`), `description`, and `solution`.
The values for `reason` are `user`, `focus`, `edit`, `speed`, and `modifier`.

### Contract discovery, stop, and events

Physical robot control mode provides `api/describe`, `api/stop`, and `api/events`.
These routes check the access token, `Host`, and `Origin`.
Without an access token, they still reject forwarded headers.
`api/stop` also checks the POST headers and client ID described above.

`api/describe` returns `service`, `version`, `contract`, `demo`, `zones`, `lease_seconds`, `on_lease_expiry`, and `commands`.
`contract` is 1, and `demo` is `false`.
Each command's `params` describes its arguments as JSON Schema, excluding `action`.
For `stop` and `disable`, `any_client` is `true`.
For `keyboard`, `ui_only` is `true`. The web panel uses this command.
`start_sync` takes control. The existing heartbeat route only renews it.

The `api/stop` body is `{"wait_s": 0}`.
`wait_s` is optional and defaults to 0. It must be a finite number from 0–15 seconds.
The route uses the existing stop worker and releases the state lock while waiting.
Any client can stop without holding the control lease.
Repeated requests follow the existing stop deduplication rules.
A successful response is 200 with `{"stop": "confirmed", "state": {...}}`.

| `stop` | Meaning |
| --- | --- |
| `confirmed` | Robot stop is confirmed, and the gripper has no active command or lock |
| `pending` | The wait ended while a stop worker or gripper command was still active |
| `unconfirmed` | Robot stop is unconfirmed, or the gripper is locked |

Stopping the robot does not interrupt an active gripper command.
A later successful stop cannot clear an earlier unconfirmed stop.
The response preserves the lock state. It does not retry motion automatically.

The state's `last_command` starts as `null`.
After a command completes or is rejected, it contains `id`, `action`, `client`, `result`, `message`, and `finished_at`.
`finished_at` is a Unix timestamp in seconds.
`result` is `ok`, `failed`, `rejected`, or `stopped`.
An asynchronous command updates its result after its worker completes.
Stop results also wait for active gripper commands to finish. A gripper lock produces `failed`.
A rejection from another client does not hide the final result of an active command.
An older canceled worker cannot overwrite a newer completed stop.

`api/events` returns `{"events": [...], "next": 0}`.
Each event contains `seq`, `ts_unix`, `level`, `message`, and `fields`.
`since` defaults to 0 and must be a nonnegative integer.
The response contains only events with sequences greater than `since`. Use `next` for the next query.
The service retains the latest 1000 events. Sequence numbering restarts with the service.
Events correspond to existing state messages, locks, alarms, keyboard close reasons, and gripper lock messages. They add no journal lines.

### Python client

`mg400.client.RobotClient(url, token, client_id)` uses the service base URL and an explicit client ID.
Use a unique ID for each program. Do not share an ID between active programs.
Construction opens no connection. `connect()` checks WebSocket protocol 1 and service contract 1, then starts the client threads.
Use `http://`, `https://`, `ws://`, or `wss://`, including the service base path when configured.
The token follows the robot service's access rules. An empty token works only with the permitted loopback hosts.

Start the [offline simulator and panel](#offline-robot-simulator) before this example.
The example sends movement commands to that simulator.

```python
from mg400.client import RobotClient

with RobotClient("http://127.0.0.1:8766/", "", "recipe-example") as robot:
    robot.run("start_sync")
    robot.run("enable")
    result = robot.run("execute", timeout=30, targets={"x": 260, "y": 12, "z": 42, "r": 31})
    print(result["last_command"])
```

- `state` returns a copy of the latest state. It is `None` before connection. `connected` reports whether the WebSocket is available.
- `command(action, **fields)` sends 1 request and returns the protocol reply. Check `status`. Acceptance does not mean completion.
- `wait(predicate, timeout)` returns a state that satisfies the predicate. A timeout raises `TimeoutError`.
- `run(action, timeout=30, **fields)` waits for `busy == False` and a new `last_command` for its action and client.
- `RobotClientError` reports a refusal or uncertain outcome. It retains `status` and `close_code` when available.

`run()` accepts `ok`. For `stop`, `disable`, and `stop_sync`, it also accepts `stopped` when the stop remains confirmed.
A rejection, failed operation, superseding stop, or service restart cannot count as successful motion.
Use 1 active program per client ID. Ordinary commands from the same client are serialized during `run()`.
A different thread can still call `command("stop")`, `command("disable")`, `run("stop")`, or `run("disable")`.

While this client holds control, it sends heartbeats every 0.5 seconds.
After a dropped socket, it attempts transport reconnection for up to 2.5 seconds, within the 3-second lease.
It never replays a command. A lost reply or timeout can leave delivery or completion unknown.
Inspect the service state before another action. Do not automatically retry motion.
Authentication, protocol, and replacement failures end the connection attempts.

When the socket is unavailable, `command("stop")` and `command("disable")` use HTTP with the same token and client ID.
The fallback checks contract 1 before sending a command.
Its replies preserve `locked` and `stop_unconfirmed`. A reply still means acceptance, not verified completion.
Other commands have no HTTP fallback. HTTP requests follow no redirects and have no automatic retries.

Context exit runs `stop_sync` while this client holds control, then closes the connection and stops its threads.
The optional `keep_control=True` skips `stop_sync`. It does not keep heartbeats running after exit.
Calling `close()` directly also ends the threads without renewing or releasing control.
The service then applies its normal lease-expiry stop and lock.
The optional `timeout=5` sets the connection and reply timeout in seconds.

### WebSocket interface

Communicate in this sequence:

1. Within 3 seconds of connection, send `{"type":"hello","protocol":1,"client":"<客户端 ID>","token":"<密钥>"}`. The placeholders mean client ID and token.
2. Wait for the service to send `{"type":"state","state":{...}}`.
3. Send a request: `{"type":"<类型>","id":<整数>,"body":{...}}`. The placeholders mean type and integer.
4. Match the reply by `id`.

`protocol` is the message format version, currently 1.
Without an access token, `token` can be an empty string.
The fields in `state` are the same as in `api/state`.
The service sends state updates when the state changes.
It also sends an update after each reply.
It sends at least one update per second.

| Request type | `body` |
| --- | --- |
| `command` | Same as the `api/command` body |
| `heartbeat` | `{}` |
| `keys` | `{"seq":<序号>,"held":[<按键>]}`. The placeholders mean sequence number and key |

`id` is an integer from 0 to 2^53, selected by the client.
The service replies in request order, with one reply per request.
A successful reply is `{"type":"reply","id":<id>,"status":200,"state":{...}}`.
A rejected reply is `{"type":"reply","id":<id>,"status":<状态码>,"error":"..."}`, where the placeholder means status code.
Status codes have the same meanings as in the HTTP interface. 500 means an internal service error.
For an invalid `body`, the service replies with an error and keeps the connection open.
A successful `command` reply means that the service accepted the command. It does not mean that the action is complete.
When the action ends, `busy` becomes `false` in the state, and `message` gives the result.
The service writes one log entry per `command`. It does not log `keys` or `heartbeat`.

The `seq` in `keys` shares an increasing sequence number with the keyboard enable/disable command.
The service ignores messages whose value is not greater than the previous value.
`held` lists the keys that are currently held: W, A, S, D, Q, E, R, F, Z, and X.
The client must resend the message at intervals of 100 ms while a key is held.
If the service receives no update within 0.4 seconds, it stops jogging and disables keyboard control.

For close codes 4400, 4401, 4426, or 1013, the service sends `{"type":"error","status":<状态码>,"error":"..."}` before it closes the connection.

| Close code | Meaning |
| --- | --- |
| 4400 | Invalid hello or request format |
| 4401 | Incorrect access token |
| 4426 | `protocol` does not match the service. Update the client |
| 4409 | A new connection with the same client ID replaced this connection |
| 1013 | Authenticated connection capacity is full |
| 1001 | Service shutdown. The service sends the final state before closure |
| 1002, 1003, 1007, 1009 | Invalid frame format, binary message, text that is not UTF-8, or an oversized message |

Connection rules:

- Messages must be UTF-8 text of no more than 4096 bytes.
- For browser connections, `Origin` must match the service address. Programs can omit `Origin`.
- The service sends a ping every 5 seconds. It closes the connection if it receives no frames for 15 seconds.
- Up to 8 client IDs can have authenticated connections at the same time.
- When the same client ID sends hello again, the service closes the old connection.
- The service immediately disables a client's keyboard control when its latest connection closes.
  The same applies when a new connection with the same client ID replaces it.

Connection closure does not end the control lease.
If the service or proxy closes the connection, the client keeps control if it reconnects and renews within 3 seconds.
During a network interruption, the service might not detect connection closure. The key timeout and control lease stop the robot.

## Stage panel API

The stage panel HTTP interface uses JSON.
The service queries Arduino state every 0.2 seconds and stops expired press-and-hold motion during each query.

| Interface | Body fields | Purpose |
| --- | --- | --- |
| `GET /api/ports` | None | List available serial ports, excluding the gripper converter. When `MG400_STAGE_SERIAL` is set, list only that device |
| `GET /api/state` | None | Read the connection, motors, microstep mode, and activity log |
| `POST /api/connect` | `port` | Connect to a serial port listed by `api/ports`. Demo mode accepts only `DEMO` |
| `POST /api/disconnect` | None | Send `stop all` and disconnect after confirmation |
| `POST /api/stop` | None | Stop all motors |
| `POST /api/microstep` | `microstep`: 1, 2, 4, 8, or 16 | Set microstep mode when all motors are stationary |
| `POST /api/motors/<电机>/move` | `direction`, `speed`, `steps` | Move a fixed number of steps |
| `POST /api/motors/<电机>/hold` | `direction`, `speed`, `hold_id`, `sampled_at` | Start press-and-hold motion |
| `POST /api/motors/<电机>/hold/keepalive` | `hold_id` | Renew press-and-hold motion |
| `POST /api/motors/<电机>/hold/stop` | `hold_id` | End press-and-hold motion |

`<电机>` means the motor: `bottom`, `top`, or `insert`. The values for `direction` are `cw` and `ccw`.
`speed` is an integer from 1–1000. `steps` is an integer from 1–100000.
The client generates `hold_id`: 16–64 characters, with only letters, digits, `_`, and `-`.
Each press-and-hold operation must use a new `hold_id`.
`sampled_at` comes from the field with the same name in `api/state`. It must be within the service clock's last 2 seconds.
The client must renew press-and-hold motion within 1 second. The webpage renews it every 200 ms.
`hold/stop` can be sent repeatedly.
If `hold/stop` arrives before `hold` with the same `hold_id`, the service rejects the later `hold`.

### Stage automation contract

Automation uses `/contract/api/` beside the existing panel routes.
Connect the Arduino through the panel before using automation.
The base URL for `labkit.client.ServiceClient` is `http://127.0.0.1:8767/contract`, including `/contract`.
The interface follows the [instrument service contract](adr/0001-instrument-service-contract.en.md).

| Interface | Purpose |
| --- | --- |
| `GET /contract/api/describe` | Read command parameters, limits, demo status, and lease rules |
| `GET /contract/api/state` | Read motor state plus `service`, `seq`, `ts_unix`, `busy`, `locked`, `fault`, `lease`, and `last_command` |
| `POST /contract/api/heartbeat` | Acquire or renew the control lease. Return 409 if another client holds it |
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

Parameter ranges match the panel interface. Missing or extra parameters are rejected.
The control lease lasts 3 seconds by default. Holds still require a separate renewal within 1 second.
Hold renewal does not renew the control lease.
On lease expiry, the service requests that all motors stop.
Releasing the lease does not stop motion. Wait for completion or request a stop before returning control.
While a lease is active, panel motion, hold renewal, and microstep routes return 409 with `Stage is controlled by automation`.
Panel stop and hold release remain available.

The dedicated stop interface returns `stop` and `state`. The `stop` value is `confirmed`, `pending`, or `unconfirmed`.
It returns `confirmed` only after all motors report idle.
While waiting for feedback, the service permits other stop requests but blocks new motion.
If the stop wait expires or a serial outcome is unknown, `locked` is true and `fault` is non-null.
Later idle feedback does not clear the fault. Check the physical state, then manually disconnect and reconnect.
Reconnection returns 409 while stop confirmation is in progress. Feedback from a new connection cannot confirm that the old device stopped.
Reconnection clears the fault only after all motors report idle.
`last_command` distinguishes `ok`, `failed`, `rejected`, and `stopped`. The service never retries motion automatically.

Contract routes use the same `MG400_STAGE_TOKEN` and check `Host`, `Origin`, and forwarding headers.
POST requests must include `Content-Type: application/json` and `X-MG400-Client`. The body must be a JSON object.
Client IDs contain 1–128 printable ASCII characters.
Contract routes do not use `X-Control-Token`. A client ID cannot bypass that check on legacy panel routes.
The body limit remains 4096 bytes.

### Access checks

The following checks apply to the existing `/api/` panel routes.

When `MG400_STAGE_TOKEN` is set, every `/api/` request must include `Authorization: Bearer <密钥>`.
The service then accepts any `Host` and forwarding headers.
`/` and `/static/` do not require an access token. The page contains no serial ports, logs, or motor positions.
Without an access token, the `Host` and forwarding header rules are the same as in [Authentication and client ID](#authentication-and-client-id).

POST requests must also meet these conditions:

1. Include `Content-Type: application/json`.
2. Include `X-Control-Token` from the page's `<meta name="control-token">`. The service generates a new value at each startup.
3. The body must not exceed 4096 bytes. For interfaces with body fields, it must be a JSON object.

Success returns 200 and a state object with the same fields as `api/state`.
`api/ports` returns `{"ports": [{"device": "...", "description": "..."}]}`.
The state object's `motors` field lists each motor's `mode` (`idle`, `moving`, or `continuous`), `position`, `cooldown`, `available`, and other fields.
On rejection, the body is `{"error": "..."}`, with these status codes:

| Status code | Meaning |
| --- | --- |
| 400 | Invalid request, or the serial port is not in the current list |
| 401 | Missing or incorrect access token |
| 403 | `Host`, forwarding headers, or `X-Control-Token` do not meet the requirements |
| 409 | State conflict, such as no connection, a motor still moving, or cooldown |
| 413 | The request body exceeds 4096 bytes. The response body is plain text generated by waitress |
| 415 | The request is not JSON |
| 503 | Serial communication failed. The service closed the serial connection |

## Physical robot control reference

Use this section when you change physical robot motion or gripper interfaces.
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

For manual `Execute targets`, `MovL` uses `SpeedL=1,AccL=1`.
For finite keyboard trajectories, W/S, R/F, and Q/E use `MovL` with `SpeedL=2,AccL=20`.
Controller inverse kinematics validates the target joint limits. The controller plans the straight path.
Reset uses `JointMovJ` with `SpeedJ=5,AccJ=5`.
The A/D finite trajectory uses `JointMovJ` with `SpeedJ=5,AccJ=20`.
Both types of joint motion check the joint ranges along the entire segment. They do not include collision checks for the physical setup.
Robot motion does not change global speed or tool configuration.

A/D uses only measured joint feedback at startup and during motion. It does not query `GetPose`.
After the action ends, it reads coordinates and updates the panel.
The other six directions can reuse measured coordinates read by the same control instance.
For reuse, the measurement must be no more than 0.25 seconds old.
Joint feedback before and after the measurement must also match the current joint feedback.
If these conditions are not met, the control instance reads the coordinates again.

Four-axis protocol reference: [Official Dobot SDK](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python/blob/main/dobot_api.py).

### USB gripper protocol

Gripper position commands do not themselves enable or move the robot.
Initialization reuses the USB–RS485 collection connection. It does not open another serial port.
The connection uses slave 1, 115200, and 8N1. Initialization writes `0x0100=1` once.
The program waits for `0x0200=1`, an in-position state, and a valid measured position.

Position commands are written to the `.command.json` file beside the feedback file.
The collector executes only requests that match the current session and are no more than 0.75 seconds old. Each request executes once.
Position commands require the collector to report `control.protocol=2` or `3`.
Initialization requires `control.protocol=3`. The request uses `operation=initialize` and contains no position target.
Feedback for completed initialization has `operation=initialize`, `target=null`, and `ack=true`.

The command file's `deadline_monotonic` specifies a deadline on the monotonic clock.
The monotonic clock measures elapsed time and does not change with system date adjustments.
The panel and collector must run on the same host and share the same monotonic clock domain.
Position commands do not support transfer between hosts or time namespaces with clock offsets.
File preparation, collector receipt, register operations, and in-position confirmation share one command deadline.
The time limit is 5 seconds for position commands and 15 seconds for initialization.
The panel starts timing before it prepares the command file. The collector must not restart the timer.

The collector checks the deadline before each serial request, including mode and target writes.
If a request has already been sent, the collector still receives and validates the complete reply with the original 1-second reply timeout.
If the deadline has then expired, the collector stops this command, retains the command lock, and sends no further command requests.
A timeout does not withdraw a target already sent or guarantee that gripper motion stops.

Before transmission, the command is recorded in `.command-lock.json` beside the feedback file.
The collector deletes this record after successful write acknowledgment and target register readback before the deadline.
If the record still exists at restart, the collector stays locked and only reads feedback.

An actual position of 1000 means fully open. At 0, each finger has moved inward by 5 mm.
Duplicate packets, old sequence numbers, and packets from ended sessions are ignored. They do not clear fresh samples or extend their validity.
Repeated reads of the same file do not extend measurement validity.

A value of `3` in the gripper status register `0x0201` means that the object fell from the gripper.

Robot state includes `gripper.outcome` and `gripper.outcome_at` beside the existing gripper fields.
`outcome` is `null`, `arrived`, `contact`, or `dropped`.
`outcome_at` is the Unix timestamp, in seconds, when the adapter observed that outcome. It is `null` when no outcome is available.

A new gripper operation clears both fields before publication. The robot state also hides the previous outcome while that operation is active.
Initialization does not produce a grasp outcome.

`arrived` retains the measured target-arrival result. `contact` retains the confirmed contact result.
After `contact`, a fresh initialized sample with state `3` changes the observed outcome to `dropped`.
The sample must have a newer sequence in the same collector session as the contact result.
A collector restart, stale sample, or repeated sample cannot create that transition.

The existing collector poll observes drops between operations and after a motion job.
Each new drop produces 1 warning event with `outcome` and `outcome_at` fields. Repeated polls do not change its timestamp or repeat the event.

The outcome and timestamp remain historical metadata when feedback becomes unavailable. They do not prove a current grasp.
`WebGripper.last_outcome` retains the last command's `arrived` or `contact` result, even after the observed outcome becomes `dropped`.
This keeps keyboard stepping stopped after contact. Drop observation sends no command and does not clear a lock.
For status definitions, see [PGEA User Manual, Table 2.9](https://www.manualslib.com/manual/4358849/Dh-Robotics-Technology-Pgea-Series.html?page=17).
