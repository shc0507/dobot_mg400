[中文](stage-panel.md)

# Stage panel

[Return to the project home page](../README.en.md)

The stage panel is a web service that controls the three-motor stage. It runs separately from the robot web panel.
The stage's three stepper motors connect to one Arduino. Each motor uses an A4988 driver.
The panel sends commands to the Arduino through a USB serial port at 115200 baud.
The service uses Flask, waitress, and pySerial.

The motor directions and keys are:

| Motor | Action | Page label | Serial direction | Key |
| --- | --- | --- | --- | --- |
| `bottom` | Raise | `Up` | `ccw` | W |
| `bottom` | Lower | `Down` | `cw` | S |
| `top` | Right | `Right` | `ccw` | D |
| `top` | Left | `Left` | `cw` | A |
| `insert` | Insert | `Insert In` | `ccw` | J |
| `insert` | Withdraw | `Insert Out` | `cw` | K |

Hold buttons add `Hold` before the label, for example, `Hold Up`.
Labels identify only the serial direction. The actual rotation depends on the motor wiring and viewing direction.

## Before use

A person who can disconnect motor power must be present during use.
`Stop all motors` is a software stop, not an emergency stop.

The Arduino connects to a USB port on the Olares host and must run the `firmware/three-motor-demo` firmware.
See the [firmware guide](../firmware/three-motor-demo/README.en.md) for upload instructions, wiring, and serial commands.
The panel uses a file lock for exclusive access to the serial port.
A program that does not use the lock can still open the same port. The panel cannot detect that program.

For an Olares deployment, upload the firmware from a computer with the Arduino IDE:

1. Click `Stop all motors` and `Disconnect` in the panel.
2. Disconnect the Arduino from the Olares host. Connect it to that computer.
3. Upload the firmware as the firmware guide specifies.
4. Reconnect the Arduino to a USB port on the Olares host.
5. Run `ls -l /dev/serial/by-id/` on the host. Confirm that the Arduino path matches `MG400_STAGE_SERIAL`.
6. Click `Connect` in the panel.

When you run the panel locally, click `Disconnect` in the panel before uploading firmware.

The stage uses open-loop control: it sends pulses without reading the actual position. It has no encoders.
Updated firmware supports homing switches and soft limits. Old firmware keeps manual control but cannot use homing or named moves.
The page count is a signed STEP pulse count. Reset and homing reset its reference. It is not measured travel.
Before moving, confirm that sufficient travel remains in that direction.

Most Arduino boards reset when the serial port opens. The panel opens the serial port on every connection.
During reset, the drivers may briefly wake, and the motors may move to the A4988 Home microstep position.
After reset, the counts return to zero. The microstep mode, which sets the fraction of a full step per pulse, returns to full step.
After the firmware starts, all three drivers enter sleep mode.

When a motor stops, its driver keeps the coils energized.
After the motor stops for a period, the driver enters sleep mode and the motor no longer holds its position.
The firmware's `SleepTimeoutMs` setting controls this period. The default is 10 seconds.
External forces can turn a sleeping motor. The count does not record these rotations.
When the driver wakes, the motor may move to the A4988 Home microstep position.

The panel does not distinguish between operators.
Every page with the panel open can send commands.
All pages share the connection, motion state, activity log, and cooldown times.
Speed and step inputs belong only to the current page. Refreshing the page restores their defaults.

## Open the panel

### Use the service deployed on Olares

Keep LarePass VPN connected.
The VPN access-control rules must allow TCP `8767`.
Open the [Olares stage panel](http://100.64.0.1:8767/).

When the page shows the `Panel access token` input, enter the access token. Click the adjacent `Connect` button.
The access token is the `MG400_STAGE_TOKEN` value in `/etc/mg400/mg400.env` on the host.
The page stores the token in the current tab. You do not need to enter it again after a refresh.
If the service rejects the token, the page deletes the saved token and shows the input again.
While the input is visible, the page pauses status reads.
If the service is not running, see [Service configuration](#service-configuration).

### Start locally

On the computer connected to the Arduino, enter the project directory and run:

```sh
uv sync
uv run stage-panel
```

Open the [local stage panel](http://127.0.0.1:8767/) on that computer.
`uv run python -m stage_controller` is equivalent to `uv run stage-panel`.
Local operation also reads the environment variables and arguments in [Configuration](#configuration).
On Linux, the service user must have read and write access to the serial device, for example, through the `dialout` group.
Log in again after joining the group.

Without an Arduino, use demo mode to view the interface:

```sh
uv run stage-panel --demo
```

Select `DEMO` in the serial-port list. Click `Connect`.
Demo mode does not open a serial port. Its simulated motion times exclude acceleration and deceleration.

## Controls

### Automation control

While an automation client holds the control lease, the page shows `Stage is controlled by automation`.
Fixed-step, hold, keyboard motion, microstep, Home, named position, and pose controls are unavailable.
`Stop all motors` and hold release remain available.
The lease lasts 3 seconds by default. The client must renew it.
On expiry, the service requests that all motors stop.
Releasing the lease only returns control ownership. It does not stop motion.
Before releasing control, the client must wait for motion to finish or request a stop.

If the stop cannot be confirmed, the page shows a fault and blocks new motion.
First use the physical emergency stop and check the motors. Then manually disconnect and reconnect the Arduino.
Wait until the stop request finishes before reconnecting.
Reconnection clears the fault only after all motors report idle.
Idle feedback alone does not automatically clear the fault.
See [Stage automation contract](development.en.md#stage-automation-contract) for the interface.

### Connection

Use this sequence:

1. Select the Arduino in the serial-port list under `Device connection`.
2. Click `Connect`. Wait for the status to show `Arduino connected`.
3. When finished, click `Disconnect`.

The serial-port list shows ports on the host that runs the service.
For an Olares deployment, the list comes from the Olares host, not the computer with the browser.
After connecting the Arduino, click the refresh button to update the list.
The list excludes the gripper's USB converter, which has serial number `BG02Q5TN`.
When `MG400_STAGE_SERIAL` is set, the list shows only that device. See [Configuration](#configuration).
Otherwise, the list may contain other serial ports.
Run `ls -l /dev/serial/by-id/` on the host. Find the Arduino by its device name.
Do not connect to an unidentified serial port.
During connection, the service sends a `status` command to that port.

The service opens only serial ports that currently appear in the list.
During connection, the service opens the serial port and waits about 2 seconds for the Arduino to reset.
Controls unlock only after the Arduino returns the status of all three motors.
`Disconnect` sends `stop all`. It closes the serial port after receiving confirmation.

### Microstep mode

`Stepping mode` applies to all three motors. Select full step, 1/2, 1/4, 1/8, or 1/16 step.
While all motors are stationary, select the mode. Click `Apply to all`.
The page separately shows the current mode reported by the Arduino.

Speed and steps both count STEP pulses in the current mode.
The panel does not convert speed or steps when you change the mode.
For example, in 1/16-step mode, 50 pulses equal 3.125 full steps.
Counts from different modes cannot be converted to the same distance.
Changing the microstep mode clears the home reference and soft limits for all 3 axes.
Home each axis again before a named move or automated motion.

### Homing and named moves

Before routine hardware use, complete the supervised switch and calibration checks in H-07 and H-18.
Homing finds the configured reference switch. Soft limits restrict travel from that reference.
The service reads measured calibration from `/var/lib/mg400/stage.toml`. See [Stage calibration](#stage-calibration).

Each motor shows its reference state:

| Badge | Meaning |
| --- | --- |
| `Homing unavailable` | The motor is disconnected, firmware lacks support, or calibration is unavailable. Manual controls remain available when connected. |
| `Home required` | The service has not confirmed homing and the configured soft limits for this connection and microstep mode. |
| `Homing…` | Homing or soft-limit confirmation is in progress. |
| `Homed` | The service confirmed homing and soft limits. |

Clear the travel path before clicking `Home` for an axis.
Wait for `Homed` before selecting an Insert named position, such as `load` or `furnace`.
Named positions use millimetres from home. The millimetres shown come from pulse counts and calibration, not measured travel.
The host refuses automated motion without a confirmed reference with `position unknown, home first`.

Home all 3 axes before selecting a button under `Named poses`.
A pose moves each axis to its configured position, 1 axis at a time in the configured order.
New motion controls remain unavailable while homing or a named move is active. `Stop all motors` remains available.
The page does not queue or automatically retry these actions.
Reset, reconnection, and a microstep change require fresh homing and soft-limit confirmation.

State reports `feedback_age_s` from the last complete board reply. `feedback_fresh` requires that reply to be at most 2 s old.
Each motor's `automation_ready` requires fresh feedback and a confirmed reference and soft range for the current microstep.
Its `at_positions` lists named targets only while idle at the exact configured pulse count. Do not assume `load` is zero.
These fields do not use the HTTP response timestamp as board feedback.
Releasing a lease cancels the remaining sequence, even if the same client acquires control again. Renewal preserves the current sequence.

The page routes are:

| POST route | JSON body |
| --- | --- |
| `/api/motors/<motor>/home` | `{}` |
| `/api/motors/<motor>/awake` | `{"seconds": 30}` |
| `/api/motors/<motor>/goto` | `{"name": "load"}` |
| `/api/poses/<name>` | `{}` |

These routes use the same page authentication and automation ownership rules as manual motion.
An accepted request returns a snapshot. Poll state to check completion.
An awake request extends driver energization for 0–60 seconds. It sends no STEP pulses and does not establish a reference.
An extension of 0 cancels the extension. The normal 10-second idle hold still applies.
The page does not renew awake requests. The hold watchdog remains 1 second.

The contract adds `home` with `motor`, `awake` with `motor` and `seconds`, `goto` with `motor` and `name`, and `pose` with `name`.
A positive contract `awake` requires a confirmed reference. An owning client can cancel the extension with 0 without homing.
The manual awake route requires updated firmware but does not require homing.

### Fixed-step movement

Each motor has its own `Speed` and `Steps per click`:

| Input | Range | Default |
| --- | --- | --- |
| `Speed` | 1–1000 pulses/second | 50 |
| `Steps per click` | 1–100000 pulses | 50 |

When you click a direction button, the motor moves the specified number of steps at the specified speed.
Fixed-step movements accelerate and decelerate. The default acceleration is 50 pulses/second².
For short distances, the motor may not reach the specified speed.

After a fixed-step movement ends, that motor has a 1-second cooldown before it can move again.
The service also enforces the cooldown. Multiple pages or rapid repeated clicks cannot bypass it.
The panel does not queue movements or retry them automatically.
This cooldown does not affect other motors.

Changing speed or steps affects only the next movement.
`Reset defaults` restores the current page's speed and steps to 50.
It does not change the microstep mode or stop motors that are moving.

### Hold-to-move

While you press a `Hold` button, the motor runs continuously at the specified speed. It stops when you release the button.
Hold-to-move has no acceleration or deceleration and does not use `Steps per click`.
You can also focus the button and hold the space bar or Enter key.
You cannot change the speed during a hold.

The hold stops when you release the button, move the pointer outside it, press Esc, switch pages, or close the page.
After stopping, that motor has a 1-second cooldown.

The page renews the hold about every 200 ms to tell the service and Arduino that the button is still pressed.
If the Arduino receives no renewal for 1 second, it stops that motor.
The service also stops an unrenewed hold after 1 second.
If the browser, VPN, service, or USB connection fails, the held motor stops within about 1 second.

### Keyboard controls

`Keyboard controls` starts at `Off` whenever the page loads.
Click the switch to select `On`. Then hold a key or its corresponding button on the page.
The table at the start of this page lists the keys.

Keyboard controls use each motor's `Speed` and hold rules. They do not use `Steps per click`.
Multiple motors can run under hold control at the same time.
Letter keys have no effect while you edit an input or hold Ctrl, Alt, or Meta.
If you keep a key pressed through a cooldown or stop, the motor does not restart. Release the key, then press it again.
When you switch keyboard controls off, holds started from that area stop.

### Stop

`Stop all motors` stops pulse output to all motors. It is available during motion and cooldown.
After stopping, the coils remain energized until the drivers sleep.
Esc stops only hold-to-move motion.

If the page cannot reach the service, it shows `Cannot reach the stage panel service. Motion status is unknown.` and ends the hold.
When the page closes or the network connection fails, the service keeps running. Fixed-step movements continue to their endpoints.

When the service stops, it first sends `stop all` and disconnects the serial port, then exits.
If the process is killed or crashes, the service sends no stop command.
In that case, the Arduino stops held motors after 1 second. Fixed-step movements continue to their endpoints.

If USB disconnects, the service cannot guarantee a stop. A fixed-step movement that already started may finish.
When the service loses the Arduino connection, it attempts to send `stop all`, then closes the connection.
The page then shows `Connection lost; motor stop could not be confirmed.`.
Go to the equipment and confirm that the motors stopped before reconnecting.
The panel does not reconnect automatically or retry movements.

## Service configuration

[Host deployment](deploy.en.md) installs the `mg400-stage-panel.service` service. Its `mg400ctl` name is `stage`.
The service runs as a user and group with numeric ID `1000`. It also joins `dialout` for serial-port read and write access.
The service starts manually. It is not configured to start at boot.
Starting the service does not open a serial port. The port opens only after you click `Connect` on the page.
The service writes no files to disk. It sends output to the systemd journal.

### Configuration

The service reads configuration from environment variables. Command-line arguments override their corresponding environment variables:

| Environment variable | Argument | Default | Purpose |
| --- | --- | --- | --- |
| `MG400_STAGE_BIND` | `--bind` | `127.0.0.1` | Listen address |
| `MG400_STAGE_PORT` | `--port` | `8767` | Listen port, 1–65535 |
| `MG400_STAGE_TOKEN` | None | None | Access token |
| `MG400_STAGE_SERIAL` | `--serial` | None | Arduino serial-port path |
| `MG400_STAGE_CONFIG` | `--config` | `/var/lib/mg400/stage.toml` | Measured stage calibration and named targets |

An empty environment variable has the same effect as an unset variable.

You must set `MG400_STAGE_TOKEN` when listening on a non-loopback address.
The access token must contain at least 24 characters. Use only printable ASCII characters, with no space at either end.
Otherwise, the service refuses to start.
With an access token set, the service accepts any host name, including when it listens on a loopback address.
Without an access token, the service accepts only `127.0.0.1`, `localhost`, or `[::1]` addresses.
It also rejects requests with `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host`, or `X-Real-IP` headers in that case.
See [Stage panel API](development.en.md#stage-panel-api) for the interface.

When `MG400_STAGE_SERIAL` is set, the serial-port list shows only that device, and `Connect` accepts only that device.
Use a path under `/dev/serial/by-id/` where possible. This path stays the same after disconnecting and reconnecting the device.
If the device is absent, the list is empty.
If the path points to the gripper converter, the service refuses to start.
Without this setting, the list shows all serial ports except the gripper converter.
Demo mode ignores this setting.

### Stage calibration

Use the measurements and travel limits from H-07. Do not infer calibration from the pulse count or copy demo geometry to hardware.
The file contains a `motors` table and an optional `poses` table. It has no production defaults.
Use a regular file owned by UID 1000 or the local service user, with mode `0600` or `0640`.
The reader refuses symbolic links in the file path. Keep the host data directory at mode `0750`.
Each of `motors.bottom`, `motors.top`, and `motors.insert` contains:

| Field | Value |
| --- | --- |
| `steps_per_mm` | Positive, finite pulse count per millimetre at the calibration microstep mode. |
| `microstep` | Calibration mode: 1, 2, 4, 8, or 16. |
| `min_mm`, `max_mm` | Finite, ordered travel bounds from home. |
| `positions` | Optional table of names and positions in millimetres. Insert requires `load` and `furnace`. |

Each `poses.<name>` table contains `order` and `positions`.
`order` lists `bottom`, `top`, and `insert` exactly 1 time, in the required movement order.
`positions` maps each axis to a position in millimetres within its bounds.
The `furnace` pose identifies the inserted position from which crucible unloading starts.

The host computes pulses per millimetre as `steps_per_mm * current_microstep / microstep`.
It rounds boundaries inward and targets to the nearest pulse. It checks the rounded target against the pulse boundaries again.
After homing, the host sends the soft limits and waits for confirmation before enabling named or automated motion.
An absent or invalid file disables these controls and shows the configuration error. Manual controls and Stop remain available.

Without an explicit configuration override, demo mode uses separate synthetic geometry for offline practice.
An explicit `--config` or `MG400_STAGE_CONFIG` also applies in demo mode.
Synthetic geometry is not real calibration. Check the hardware configuration before use.

### Configuration file

The service reads configuration from `/etc/mg400/mg400.env`.
The installer generates `MG400_STAGE_TOKEN`. Adjust the other settings for this host.

After connecting the Arduino, find its serial-port path:

```sh
ls -lL /dev/serial/by-id/
```

Each entry is a USB serial device. Its name usually contains the manufacturer, model, and serial number, and stays the same after reconnection.
The entry containing `BG02Q5TN` is the gripper converter, not the Arduino.
`-L` shows the target device's permissions and group, not its device name.
Confirm that the Arduino device's group is `dialout`.
Set `MG400_STAGE_SERIAL` to `/dev/serial/by-id/` followed by the Arduino's name.

View the access token with `sudo mg400ctl token stage`.
Restart the service after changing the configuration file.
After changing the access token, enter the new token on the page.

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
After a restart, the panel is disconnected from the Arduino. Reconnect on the page.
After a host reboot, start the service manually.

After updating the code, run the installer as [Host deployment](deploy.en.md#installation-and-updates) specifies.
Then run `mg400ctl restart stage` and reload any open panel pages.
The service generates a new command token, a random value sent with page commands, on every startup.
The service rejects commands from old pages.

## Troubleshooting

The following errors appear on the panel page. Errors that prevent service startup appear in the service journal.

| Symptom | Action |
| --- | --- |
| The serial-port list shows `No serial ports found`, or the Arduino is missing | Run `ls -l /dev/serial/by-id/` on the host. If the Arduino is absent, inspect its USB data cable and power, then click the refresh button. If `MG400_STAGE_SERIAL` is set, confirm that it matches the Arduino path. |
| `That port is no longer available. Refresh the port list.` | Click the refresh button. Select the serial port again. |
| `Cannot open <串口>: ...`, containing `Permission denied` | The service user cannot read or write the serial port. Confirm that the device group is `dialout`. For local operation, add the user to `dialout` and log in again. |
| `<串口> is in use by another program.` | Another program has the serial port open. Close that program, for example, the Arduino IDE serial monitor or another `stage-panel` process. |
| `Arduino did not reply in time. Check the port and uploaded sketch.` | Confirm that the selected port is the Arduino and that `firmware/three-motor-demo` is uploaded. The panel disconnects after this error. After fixing the problem, click `Connect` again. |
| `Hold to move was rejected. Upload the updated three-motor-demo sketch.`, or `Stepping mode` shows `Stepping mode unavailable on this firmware` | The Arduino runs old firmware. Upload this project's `firmware/three-motor-demo`. |
| The page shows the `Panel access token` input or `Enter the panel access token` | Enter the `MG400_STAGE_TOKEN` value from `/etc/mg400/mg400.env`. |
| The browser shows only JSON containing `Open the panel at 127.0.0.1 or localhost`, with no panel | The service has no access token, and the request uses another host name or a proxy. Open the `127.0.0.1` address on the service host, or set `MG400_STAGE_TOKEN` to at least 24 characters. |
| `Reload this page before sending another command.` | The service restarted. Reload the page. |
| `Cannot reach the stage panel service. Motion status is unknown.` | Inspect the VPN connection. Then run `mg400ctl status` on the host. |
| The service journal shows `Failed to load environment files` | Run the installer as [Host deployment](deploy.en.md#installation-and-updates) specifies to create `/etc/mg400/mg400.env`. Start the service again. |
| The service journal shows `Set MG400_STAGE_TOKEN to at least 24 characters, or leave it unset and bind to localhost` | If the existing `MG400_STAGE_TOKEN` has fewer than 24 characters, change that line to `MG400_STAGE_TOKEN=`. Run the installer to generate an access token, then start the service. |
| The service journal shows `MG400_STAGE_SERIAL points to the gripper USB converter (BG02Q5TN)` | Find the Arduino path as [Configuration file](#configuration-file) specifies. Update `MG400_STAGE_SERIAL`, then start the service. |
| The service journal shows `MG400_STAGE_PORT must be a whole number between 1 and 65535` | Set `MG400_STAGE_PORT` to an integer from 1–65535, then start the service. |
