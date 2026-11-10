[中文](stage-panel.md)

# Stage

[Return to the project home page](../README.en.md)

The stage service controls the three-motor stage. Its command is `stage-panel`, and its systemd service is `mg400-stage-panel.service`.
The service has no web page. The operator uses the `Stage` card in the Motion workspace of the Lab Console.
The stage's three stepper motors connect to one Arduino. Each motor uses an A4988 driver.
The service sends commands to the Arduino through a USB serial port at 115200 baud.
The service uses Flask, waitress, and pySerial.

The motor directions and buttons are:

| Motor | Axis on the card | Action | Button | Serial direction |
| --- | --- | --- | --- | --- |
| `bottom` | `Bottom` | Raise | `Step −` | `ccw` |
| `bottom` | `Bottom` | Lower | `Step +` | `cw` |
| `top` | `Top` | Right | `Step −` | `ccw` |
| `top` | `Top` | Left | `Step +` | `cw` |
| `insert` | `Insert` | Insert | `Step −` | `ccw` |
| `insert` | `Insert` | Withdraw | `Step +` | `cw` |

`cw` increases the pulse count. `ccw` decreases the pulse count.
Actions identify only the serial direction. The actual rotation depends on the motor wiring and viewing direction.

## Before use

A person who can disconnect motor power must be present during use.
`Stop stage` is a software stop, not an emergency stop.

The Arduino connects to a USB port on the Olares host and must run the `firmware/three-motor-demo` firmware.
See the [firmware guide](../firmware/three-motor-demo/README.en.md) for upload instructions, wiring, and serial commands.
The service uses a file lock for exclusive access to the serial port.
A program that does not use the lock can still open the same port. The service cannot detect that program.

For an Olares deployment, upload the firmware from a computer with the Arduino IDE:

1. Click `Stop stage` in the console.
2. Run `mg400ctl stop stage` on the host. The service sends `stop all`, then closes the serial port.
3. Disconnect the Arduino from the Olares host. Connect it to that computer.
4. Upload the firmware as the firmware guide specifies.
5. Reconnect the Arduino to a USB port on the Olares host.
6. Run `ls -l /dev/serial/by-id/` on the host. Confirm that the Arduino path matches `MG400_STAGE_SERIAL`.
7. Run `mg400ctl start stage`. Then connect the Arduino as [Connection](#connection) specifies.

When you run the service locally, stop the `stage-panel` process before uploading firmware.

The stage uses open-loop control: it sends pulses without reading the actual position. It has no encoders.
The stage has no limit switches. Updated firmware supports confirmed manual zero and soft limits.
A board with configured switches can still use homing.
The count on the card is a signed STEP pulse count. Reset, homing, and manual zero reset its reference. It is not measured travel.
Before moving, confirm that sufficient travel remains in that direction.

Most Arduino boards reset when the serial port opens. The service opens the serial port on every connection.
During reset, the drivers may briefly wake, and the motors may move to the A4988 Home microstep position.
After reset, the counts return to zero. The microstep mode, which sets the fraction of a full step per pulse, returns to full step.
After the firmware starts, all three drivers enter sleep mode.

When a motor stops, its driver keeps the coils energized.
After the motor stops for a period, the driver enters sleep mode and the motor no longer holds its position.
The firmware's `SleepTimeoutMs` setting controls this period. The default is 10 seconds.
External forces can turn a sleeping motor. The count does not record these rotations.
When the driver wakes, the motor may move to the A4988 Home microstep position.

## Open the console

### Use the service deployed on Olares

Keep LarePass VPN connected.
Open the [Lab Console](http://100.64.0.1:8080/). Go to the `Motion` workspace.
The stage service listens only on a loopback address. The console process calls it on the host through `127.0.0.1`.
The console connects to `http://127.0.0.1:8767/contract/`. See [Recipe service](orchestrator.en.md).
If the card shows `Not running` or `Not reachable`, see [Service configuration](#service-configuration).

### Start locally

On the computer connected to the Arduino, enter the project directory and run:

```sh
uv sync
uv run stage-panel
```

`uv run python -m stage_controller` is equivalent to `uv run stage-panel`.
Local operation also reads the environment variables and arguments in [Configuration](#configuration).
On Linux, the service user must have read and write access to the serial device, for example, through the `dialout` group.
Log in again after joining the group.
The console always connects to `http://127.0.0.1:8767/contract/`.

Without an Arduino, use the console demo mode to view the stage card:

```sh
uv run python -m orchestrator serve --demo --recipes-dir ./recipes --port 8790 --base-path /lab/
```

Open the [local console](http://127.0.0.1:8790/lab/) on that computer.
`uv run stage-panel --demo` starts a demo service. Its serial-port list contains only `DEMO`.
Demo mode does not open a serial port. Its simulated motion times exclude acceleration and deceleration.

## Controls

### Control ownership

Click `Take control` to get the stage control lease.
Without control, connection, stepping, step moves, homing, zero, reference teaching, and named positions are unavailable.
`Stop stage` and `STOP ALL MOTION` do not need control.
While another client holds the lease, the control button shows `In use by another client`, or `In use by another page` when another console page holds it.
The lease lasts 3 seconds by default. The client must renew it.
On expiry, the service requests that all motors stop.
Releasing the lease only returns control ownership. It does not stop motion.
Before you release control, wait for motion to finish or click `Stop stage`.

If the stop cannot be confirmed, the card shows a fault and the service blocks new motion.
First use the physical emergency stop and check the motors.
After the stop request finishes, run `mg400ctl restart stage`. Then connect again.
See [Stage automation contract](development.en.md#stage-automation-contract) for the interface.

### Connection

While the Arduino is not connected, the card shows `Arduino not connected`, the `Serial port` list, and the `Connect` button.
Use this sequence:

1. Click `Take control`.
2. Select the Arduino in the `Serial port` list.
3. Click `Connect`. Wait for the status to show `Connected`.

The serial-port list shows ports on the host that runs the service.
For an Olares deployment, the list comes from the Olares host, not the computer with the browser.
After you connect the Arduino, click `Refresh ports` to update the list.
When `MG400_STAGE_SERIAL` is set, the list shows only that device. See [Configuration](#configuration).
Otherwise, the list may contain other serial ports.
Run `ls -l /dev/serial/by-id/` on the host. Find the Arduino by its device name.
Do not connect to an unidentified serial port.

The service opens only serial ports that currently appear in the list.
During connection, the service opens the serial port and waits about 2 seconds for the Arduino to reset.
The service sends `status` and the read-only `features` query to that port.
Old firmware can refuse `features`.
Controls unlock only after the Arduino returns the status of all three motors.
The console has no disconnect button. When the service stops, it sends `stop all` and closes the serial port after confirmation.

### Microstep mode

`Stepping` applies to all three motors. Select full step, 1/2, 1/4, 1/8, or 1/16 step.
While all motors are stationary, click a mode. Then confirm on the card.
The highlighted mode is the current mode reported by the Arduino.

Speed and steps both count STEP pulses in the current mode.
The service does not convert speed or steps when you change the mode.
For example, in 1/16-step mode, 50 pulses equal 3.125 full steps.
Counts from different modes cannot be converted to the same distance.
Changing the microstep mode clears the zero and soft limits for all 3 axes.
After that, confirm each axis reference again.

### Reference state and homing

Before routine hardware use, measure `steps_per_mm`, the travel bounds and the camera reference on the hardware, under supervision.
Soft limits restrict travel from the confirmed reference.
The service reads measured calibration from `/var/lib/mg400/stage/stage.toml`. See [Stage calibration](#stage-calibration).

Each axis shows its reference state:

| Badge | Meaning |
| --- | --- |
| `Reference unavailable` | The motor is disconnected, firmware lacks support, or calibration is unavailable. |
| `Zero required` | No switches. The service has not confirmed zero and soft limits for this connection and microstep mode. |
| `Home required` | Switches. The service has not confirmed homing and soft limits for this connection and microstep mode. |
| `Zero confirmed` | The service confirmed zero and soft limits. |
| `Homed` | The service confirmed homing and soft limits. |

Step moves and press-and-hold motion need no reference. Soft limits apply only to an axis with a confirmed reference.
Named positions (`goto`) and named poses (`pose`) require `Zero confirmed` or `Homed`, because they need an absolute position.
Without a confirmed reference, the service refuses them with `position unknown, home first`.
Reset, reconnection, and a microstep change require fresh reference and soft-limit confirmation.

An axis with configured switches shows `Home…`:

1. Clear the travel path of the axis.
2. Click `Home…`. Confirm on the card. The axis moves to its reference switch.
3. Wait for `Homed`.

### Manual zero and camera reference

Without switches, each axis shows `Set zero…`.
An axis with a configured camera reference also shows `Check reference` and `Teach reference…`.
The demo transport also supports simulated switches for homing practice.

1. Put the axis at its measured safe zero position.
2. Wait until the axis is idle and the cooldown ends.
3. If a reference is configured, click `Check reference`. The card shows the camera image and the `Marker offset` in pixels.
4. Click `Set zero…`. Check the physical position, then confirm on the card.
5. Wait for `Zero confirmed` before you move the axis.

Zeroing sends no motion. It resets the pulse count, clears the firmware soft range, and then installs the configured soft limits.
Both zero and soft limits must be confirmed by fresh board replies. The configured range must contain zero.
Reset, reconnection, or an actual microstep change invalidates this confirmation.
The activity log identifies the client that set zero.

Each axis can have this optional entry in its calibration table:

```toml
reference = {stream = "camera3", marker = 12, pixel = [99.5, 99.5], tolerance_px = 2}
```

These values illustrate the format. Measure the real pixel and tolerance on the hardware.
`stream` is a go2rtc stream name with letters, digits, underscores, or hyphens. It is not a URL.
`marker` selects 1 ArUco marker from `DICT_4X4_1000`, with an ID from 0 to 999.
`pixel` is the marker centre in image coordinates, with x first and y second.
`tolerance_px` is a positive maximum Euclidean offset in pixels.
Coordinates and tolerance must be finite and within 8192 pixels.

The service reads 1 JPEG from `http://127.0.0.1:1984/api/frame.jpeg` through go2rtc, which owns the camera.
Each socket operation has a 2-second timeout. Between single-buffer reads, the service checks a 2-second body age budget and a 4 MiB limit.
HTTP headers and a pending socket receive can extend request duration. Zeroing requires a frame age of at most 2 seconds.
Redirects and proxies are disabled.
The service refuses a missing, duplicated, or out-of-tolerance marker before sending zero.
If the stage state or control lease changes during capture, it refuses the operation.
Without a reference, zero still requires explicit confirmation. Demo references require an offline frame source.

Reference teaching stores the detected marker centre as the axis reference, including when it differs from the old centre:

1. Put the axis at its safe physical zero. Wait until it is idle.
2. Click `Teach reference…`. Confirm on the card.
3. Teaching invalidates the previous zero confirmation. Click `Set zero…` afterwards.

Teaching requires control and the same conditions as zero.
Teaching requires a configuration file.
It replaces the file atomically, retains its owner and permissions, and refuses concurrent calibration changes or links.
The saved file uses the supported calibration schema. Formatting and comments are not retained.

### Named positions

`Named positions` shows one button for each configured position, for example, `Insert → load`.
The button is highlighted while the axis is idle at the exact target pulse count.
Named positions use millimetres from zero. The millimetres shown come from pulse counts and calibration, not measured travel.
During motion, the move buttons of that axis are unavailable. `Stop stage` remains available.
The console does not queue or automatically retry movements.

### Step moves

Each axis has its own `Steps` and `Pulses/s` inputs:

| Input | Range | Default |
| --- | --- | --- |
| `Steps` | 1–100000 pulses | 50 |
| `Pulses/s` | 1–1000 pulses/second | 50 |

When you click `Step −` or `Step +`, the motor moves the specified number of steps at the specified speed.
If the target is outside the soft limits, the service refuses the move with `Stage target is outside the configured soft limits.`.
Step moves accelerate and decelerate. The default acceleration is 50 pulses/second².
For short distances, the motor may not reach the specified speed.

After a step move ends, that motor has a 1-second cooldown before it can move again. The state shows `Cooling down`.
The service also enforces the cooldown. Multiple pages or rapid repeated clicks cannot bypass it.
This cooldown does not affect other motors.
The inputs belong only to the current page. Refreshing the page restores their defaults.

### Press-and-hold

Hold `Hold −` or `Hold +` to move an axis at its `Pulses/s` value. Only 1 axis moves at a time.
The `Keys` switch maps the keyboard: I/K move Bottom, J/L move Top, U/O move Insert (the first key is minus).
The motor stops when you release the button or key, when the page loses focus or is hidden, and when the page loses control.

### Stop

`Stop stage` stops pulse output to all motors. It is available during motion and cooldown.
`STOP ALL MOTION` in the console also stops the stage.
After stopping, the coils remain energized until the drivers sleep.

When the service stops, it first sends `stop all` and disconnects the serial port, then exits.
If the process is killed or crashes, the service sends no stop command. Step moves that already started continue to their endpoints.

If USB disconnects, the service cannot guarantee a stop. A step move that already started may finish.
When the service loses the Arduino connection, it attempts to send `stop all`, then closes the connection.
The card status then shows `Connection lost; motor stop could not be confirmed.`.
Go to the equipment and confirm that the motors stopped before reconnecting.
The service does not reconnect automatically or retry movements.

## API

All routes are under `<base>/contract/api/`. `<base>` is `/`.
POST requests must have a JSON body and `X-MG400-Client`.

| Route | Request | Purpose |
| --- | --- | --- |
| `GET contract/api/describe` | None | Read command parameters, limits, the demo flag, and lease rules |
| `GET contract/api/state` | None | Read the connection, motors, microstep mode, reference metadata, and lease |
| `GET contract/api/events?since=0` | None | Read activity events |
| `POST contract/api/heartbeat` | `{}` | Get or renew the control lease |
| `POST contract/api/renew` | `{}` | Renew the lease that the caller holds |
| `POST contract/api/release` | `{}` | Release the lease without stopping motion |
| `POST contract/api/command` | `action` and its parameters | Send a contract command |
| `POST contract/api/stop` | `{"wait_s": 0}` | Stop all motors. No lease is necessary. `wait_s` is optional, 0–15 s. The reply `stop` is `confirmed`, `pending` or `unconfirmed` |
| `GET contract/api/ports` | None | List the serial ports that the service can connect |
| `POST contract/api/connect` | `{"port": "..."}` | Connect a listed serial port. Requires the lease |
| `GET contract/api/motors/<motor>/reference` | None | Read the camera image and marker offset |
| `POST contract/api/motors/<motor>/teach_reference` | `{"confirm": true}` | Store the current marker position as the reference. Requires the lease |

`<motor>` is `bottom`, `top`, or `insert`.
The contract commands are `move`, `hold`, `keepalive`, `release_hold`, `stop_all`, `set_microstep`, `home`, `zero`, `awake`, `goto`, and `pose`.
`zero` requires `motor` and `confirm: true`.
`pose` moves each axis to its configured position, 1 axis at a time in the configured order.
`awake` extends driver energization for 0–60 seconds. It sends no STEP pulses and does not establish a reference. 0 cancels the extension.
In demo mode, `connect` accepts only `DEMO`.
While a board is connected, `connect` returns `Disconnect the current board before choosing another port.`.
Images come only from the reference route. State replies contain reference metadata without the JPEG.

State reports `feedback_age_s` from the last complete board reply. `feedback_fresh` requires that reply to be at most 2 s old.
Each motor's `automation_ready` requires fresh feedback and a confirmed reference and soft range for the current microstep.
Its `at_positions` lists named targets only while idle at the exact configured pulse count. Do not assume `load` is zero.
Releasing a lease cancels the remaining sequence, even if the same client acquires control again. Renewal preserves the current sequence.
See [Stage automation contract](development.en.md#stage-automation-contract) for parameters and completion rules.

## Service configuration

[Host deployment](deploy.en.md) installs the `mg400-stage-panel.service` service. Its `mg400ctl` name is `stage`.
The service runs as a user and group with numeric ID `1000`. It also joins `dialout` for serial-port read and write access.
The service starts manually. It is not configured to start at boot.
Starting the service does not open a serial port. The port opens only after you click `Connect` in the console.
Reference teaching updates the stage calibration file. The service sends output to the systemd journal.

### Configuration

The service listens on `127.0.0.1:8767` with the base path `/`. The contract is at `contract/` below it.
The environment variables below and the arguments `--bind`, `--port`, and `--base-path` change these defaults.
The service checks no token, `Host`, or `Origin`. The VPN is the access boundary.

| Environment variable | Argument | Default | Purpose |
| --- | --- | --- | --- |
| `MG400_STAGE_SERIAL` | `--serial` | None | Arduino serial-port path |
| `MG400_STAGE_CONFIG` | `--config` | `/var/lib/mg400/stage/stage.toml` | Measured stage calibration and named targets |
| `MG400_STAGE_BIND` | `--bind` | `127.0.0.1` | Listen address |
| `MG400_STAGE_PORT` | `--port` | `8767` | Listen port, 1–65535 |
| `MG400_STAGE_BASE_PATH` | `--base-path` | `/` | Base path |

An empty environment variable has the same effect as an unset variable.

When `MG400_STAGE_SERIAL` is set, the serial-port list shows only that device, and `connect` accepts only that device.
Use a path under `/dev/serial/by-id/` where possible. This path stays the same after disconnecting and reconnecting the device.
If the device is absent, the list is empty.
Without this setting, the list shows all serial ports.
Demo mode ignores this setting.

### Stage calibration

Use measured values and travel limits. Do not infer calibration from the pulse count or copy demo geometry to hardware.
The file contains a `motors` table and an optional `poses` table. It has no production defaults.
Use a regular file of at most 1 MiB. Reference teaching refuses a symbolic link anywhere in the path and a file with more than 1 hard link.
The installer creates `/var/lib/mg400/stage` with owner `1000:1000` and mode `0700` so the service can teach references.
The parent `/var/lib/mg400` remains root-owned with mode `0750`.
If `/var/lib/mg400/stage.toml` exists, installation prints a move instruction without moving or overwriting the file.
With the stage service stopped, check both paths and move the old file to `/var/lib/mg400/stage/stage.toml` manually.
Retain owner `1000:1000` and file mode `0600` or `0640`. Update any old `MG400_STAGE_CONFIG` override before restarting.
Each of `motors.bottom`, `motors.top`, and `motors.insert` contains:

| Field | Value |
| --- | --- |
| `steps_per_mm` | Positive, finite pulse count per millimetre at the calibration microstep mode. |
| `microstep` | Calibration mode: 1, 2, 4, 8, or 16. |
| `min_mm`, `max_mm` | Finite, ordered travel bounds from zero. |
| `positions` | Optional table of names and positions in millimetres. Insert requires `load` and `furnace`. |
| `reference` | Optional camera reference with `stream`, `marker`, `pixel`, and `tolerance_px`. |

Each `poses.<name>` table contains `order` and `positions`.
`order` lists `bottom`, `top`, and `insert` exactly 1 time, in the required movement order.
`positions` maps each axis to a position in millimetres within its bounds.
The `furnace` pose identifies the inserted position from which crucible unloading starts.
The `Named poses` block on the card shows 1 button for each pose. The buttons need a reference on all 3 axes.
Position and pose names match `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`. The reader refuses unknown keys. Every bound and position must fit in a signed 32-bit pulse count in all 5 microstep modes.

The host computes pulses per millimetre as `steps_per_mm * current_microstep / microstep`.
It rounds boundaries inward and targets to the nearest pulse. It checks the rounded target against the pulse boundaries again.
After homing or manual zero, the host sends the soft limits and waits for confirmation before it enables motion.
An absent or invalid file disables homing, set zero, reference teaching, named positions and poses. Step and hold moves still work, without soft limits.

Without an explicit configuration override, demo mode uses separate synthetic geometry for offline practice.
An explicit `--config` or `MG400_STAGE_CONFIG` also applies in demo mode.
Synthetic geometry is not real calibration. Check the hardware configuration before use.

### Configuration file

The service reads configuration from `/etc/mg400/mg400.env`.
Adjust the settings for this host.

After connecting the Arduino, find its serial-port path:

```sh
ls -lL /dev/serial/by-id/
```

Each entry is a USB serial device. Its name usually contains the manufacturer, model, and serial number, and stays the same after reconnection.
`-L` shows the target device's permissions and group, not its device name.
Confirm that the Arduino device's group is `dialout`.
Set `MG400_STAGE_SERIAL` to `/dev/serial/by-id/` followed by the Arduino's name.

Restart the service after changing the configuration file.

### Start and stop

```sh
mg400ctl start stage
mg400ctl status
mg400ctl logs stage
mg400ctl stop stage
```

After startup, the journal shows the listen address, for example, `Stage Controller: http://127.0.0.1:8767`.
When `MG400_STAGE_SERIAL` is set, the journal also shows `Serial port:` and that path.
When the service stops, the journal records the stop result, for example, `All motors stopped · disconnected`.
After an abnormal service exit, systemd waits 3 seconds before restarting the service.
If the service refuses to start because of a configuration error, systemd does not restart it. The status shows `failed`.
After a restart, the service is disconnected from the Arduino. Connect again in the console.
After a host reboot, start the service manually.

After updating the code, run the installer as [Host deployment](deploy.en.md#installation-and-updates) specifies. Then run `mg400ctl restart stage`.

## Troubleshooting

The following errors appear on the stage card or in console notices. Errors that prevent service startup appear in the service journal.

| Symptom | Action |
| --- | --- |
| The serial-port list shows `No serial port found`, or the Arduino is missing | Run `ls -l /dev/serial/by-id/` on the host. If the Arduino is absent, inspect its USB data cable and power, then click `Refresh ports`. If `MG400_STAGE_SERIAL` is set, confirm that it matches the Arduino path. |
| `That port is no longer available. Refresh the port list.` | Click `Refresh ports`. Select the serial port again. |
| `Cannot open <port>: ...`, containing `Permission denied` | The service user cannot read or write the serial port. Confirm that the device group is `dialout`. For local operation, add the user to `dialout` and log in again. |
| `<port> is in use by another program.` | Another program has the serial port open. Close that program, for example, the Arduino IDE serial monitor or another `stage-panel` process. |
| `Arduino did not reply in time. Check the port and uploaded sketch.` | Confirm that the selected port is the Arduino and that `firmware/three-motor-demo` is uploaded. The service disconnects after this error. After fixing the problem, click `Connect` again. |
| `In use by another client` | Another client holds the lease. Wait until it releases control, then click `Take control`. |
| `position unknown, home first` | A named position or pose needs a confirmed reference. Set zero as [Manual zero and camera reference](#manual-zero-and-camera-reference) specifies, or home the axis. |
| `Stage target is outside the configured soft limits.` | Decrease the steps, or change the direction. |
| The card shows `Not running` or `Not reachable` | Run `mg400ctl status` on the host. Confirm that `stage` is running. |
| The service journal shows `Failed to load environment files` | Run the installer as [Host deployment](deploy.en.md#installation-and-updates) specifies to create `/etc/mg400/mg400.env`. Start the service again. |
