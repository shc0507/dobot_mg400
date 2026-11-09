# Lab Console and chip passport

[中文](dashboard.md)

The `orch` service serves the Lab Console and the chip passport pages. See [deployment](deploy.en.md) and [recipe service configuration](orchestrator.en.md#http-service).
The installed unit serves the console at `http://127.0.0.1:8772/lab/`.
The proxy uses `/lab/` and redirects `/` to `/lab/`. On Olares, connect the VPN and open `http://100.64.0.1:8080/`.

## Lab Console

The console is the only operator web page.

- Row 1 of the header shows the console name, the workspace tabs, the **Chip passport** link, the devices that this page controls and the language switch. Hover over the console name to see the deployed commit. The controlled devices show only while this page holds 1 or more devices. Row 2 is the status strip. **STOP ALL MOTION** is the tall button at the right of the header and spans the 2 rows.
- The workspaces are **Overview**, **Motion**, **Cameras** and **Furnace + microscope**.
- The language switch changes between English and 中文. The browser keeps the selected language.
- The status strip shows 1 chip for each service: a dot and a text. The **Runs** chip shows only while a run exists. A state that needs attention (for example stale, fault or unavailable) has a colored background. A status older than 3 seconds shows as stale.
- When the console does not answer for 3 seconds, the strip shows only **Console offline** and the age of the last state. Check the VPN and the `orch` service.
- The stop result band below the header shows the last stop of the header or the **Safety** card, from any page or client. See [stop controls](#stop-controls).
- In a window wider than 720 px, the header, the status strip, the stop result band and the notice stay at the top of the window when you scroll.
- A service that is not configured shows Not configured on every workspace.
- Chinese text uses a font that the console serves itself, so it also shows offline.
- A confirmation in the page takes the keyboard focus. Esc or **Cancel** closes it and sends nothing.
- The console checks no token, `Host`, or `Origin`. The VPN is the access boundary. See the [single-port proxy](olares.en.md#single-port-proxy) for remote access.

**STOP ALL MOTION** sends independent stop requests to the robot, the stage and the microscope. A stop needs no control and does not wait for a lease. See [stop controls](#stop-controls).

### Control

- In the **Control** card, select **Take control** for 1 device. The console requests the lease from that service with a new identity.
- The service refuses the request when another client holds the lease. The console never takes a lease from another holder.
- The page sends a heartbeat every 500 ms. Each heartbeat renews the lease 1 time.
- The console releases the lease when no heartbeat arrives for 3 seconds, or when the page closes. If a renewal fails, the control ends and the console does not request it again.
- Before the console releases the robot, it disables the arm. This applies to **Release**, **Release all**, a page close and a missing heartbeat. The disable also stops motion. The robot service then stays unlocked.
- **Release all** releases only the console's own leases.

### Command relay

The workspaces send manual commands through the console. The console forwards each command to the service.

- Only the commands in the service's `api/describe` are permitted. Lease operations cannot be forwarded.
- A command needs control by this page, except a command that the service marks `any_client`.
- Each command is sent 1 time. If the outcome is unknown, the page shows unknown and does not retry. Check the device before you continue.

### Overview workspace

- **Run** shows the current run and its steps. The card shows only while a run exists. See [run control and recovery](#run-control-and-recovery).
- **Events** merges the `api/events` of each service, newest first. You can filter for warnings or for 1 service. Only a warning or an error shows its level. A successful vision inference is not an event.
- **Safety** gives **Stop heat and gas** and **Stop all gas**. **STOP ALL MOTION** is in the header. Each button acts on 1 press. The stop result band shows the results.
- **Agent proposals** shows the number of waiting proposals. The card shows only while 1 or more proposals wait. See [agent proposals](#agent-proposals).

### Motion workspace

The Motion workspace has 2 stations side by side: the robot arm under its camera view, and the stage under the microscope view. Below 1100 px the stations stack. **Robot and stage activity** below them lists the events of both devices and the outcome of each command from this page.

Views:

- Each station shows 1 live view. The tabs inside the frame pick the view: **Insert rail** (camera2), **Tray** (camera4) or **Microscope side** (camera3) for the arm, and **Microscope**, **Microscope side** or **Insert rail** for the stage.
- **Snapshot** and **Expand** are inside the frame. Without the Cameras workspace a view shows OFFLINE.

Robot arm:

- The header row has the state, **Enable** or **Disable**, **Home…**, **Take control** and **Stop arm**. **Disable** shows while the arm is enabled, moves or jogs. **Disable** and **Stop arm** need no control. When the robot service has no fresh feedback, the state is Not connected.
- When another page or client holds the arm, the control button says so and is unavailable. A demo arm shows DEMO.
- **Home…** needs a confirmation. It moves J1 to J4 to 0°.
- During an alarm, a coral note at the top of the station lists each controller alarm with its ID, description and solution. **Clear alarm** is in the note. Below the folds, the station shows the message of the robot service.
- After an unconfirmed stop, the note shows Stop not confirmed and **Clear alarm** becomes **Recover**. Check the robot, then select **Recover**. A restart is not necessary.
- **Keyboard jog** is the main arm control. When this page holds control and the robot is enabled, turn on the switch to start keyboard jog at the selected speed. The speed is from 1 to the `keyboard` speed cap that the robot service describes. The key map shows each key and its motion: W/S (reach out and in), A/D (base J1), R/F (Z up and down), Q/E (flange J4) and Z/X (suction: suck and drop). A held key lights up. Z and X run 1 suction action per press, also while the key stays held.
- While the switch is on and the page has focus, hold a key to move. The page sends the held keys through `ws/motion/keys` on each change and every 150 ms while a key is held. The arm stops 0.4 s after the last update. A window focus loss, a hidden tab, leaving the Motion tab, the switch off or a loss of control releases all keys and turns the keyboard off. When the robot service turns the keyboard off, the page shows the reason below the switch. Stop and **STOP ALL MOTION** work at all times.
- The readout shows the actual X, Y, Z and R, and J1 to J4 in degrees. While the arm moves, the robot service sends no pose, and the readout shows the last known pose in grey. Without fresh feedback, the readout also shows the last values in grey. Below it is **Suction**: the measured state (Sucking, Blowing or Off) and **Suck** and **Drop**. **Drop** turns suction off and blows a short pulse. The buttons need control and an idle arm. When the robot service reports `held`, the block also shows Held or Not held.
- **Jog buttons** (folded) moves ±X, ±Y, Z up and down, or R ± by 1 step of 0.5, 2 or 10 mm (degrees for R). Each press sends 1 `execute` move from a fresh read of the actual pose. While the keyboard is on, the Z and R buttons show the key that moves the same way: R and F for Z, Q and E for R.
- **Move to a pose** (folded): **Use actual** copies the actual pose. **Execute** moves to the entered pose. The speed is from 1 to the `execute` speed cap that the robot service describes.
- The console sends jog and pose targets with no bounds check and no tool file. The robot service applies its speed cap and refuses motion while the controller reports an alarm. Jog and pose use the `ws/motion/jog` and `ws/motion/move` routes.

Stage:

- The header row has the state, **Take control** and **Stop stage**. **Stop stage** needs no control. When another page or client holds the stage, the control button says so. A demo stage shows DEMO.
- If the Arduino is not connected, pick 1 of the listed serial ports, then select **Connect**. Each port shows its description and device name. The full device path is in the tooltip. **Refresh ports** reads the port list again.
- Each axis row shows the position, **Hold −**, **Hold +**, the hold speed in pulses/s and the keys of the axis. With a confirmed reference the position is in mm. Without one it is the pulse count. While the Arduino is not connected, the position shows —. An axis shows its state only when it is not idle.
- **Hold −** and **Hold +** move the axis at the entered pulses/s while you press the button. Release the button to stop the axis. 1 axis moves at a time. When the axis has a confirmed reference, the axis stops at its soft limits.
- The console sends the stage commands of a hold. It sends 1 `hold`, then 1 `keepalive` every 0.2 s while the page refreshes the hold. The page refreshes the hold through `ws/motion/stage/hold` every 150 ms. When no refresh arrives for 0.5 s, the console releases the hold. If the console stops, the firmware stops the axis 1 s after the last `keepalive`.
- **Stepping** below the axes sets the stepping mode of all 3 axes: Full, 1/2, 1/4, 1/8 or 1/16. A change clears the zero and soft limits of all 3 axes and needs a confirmation.
- **Keys**: when this page holds stage control, turn on the switch. While the page has focus, hold I/K (Bottom − and +), J/L (Top − and +) or U/O (Insert − and +). Each key moves its axis like the Hold button. The page releases the hold and turns the keyboard off on a window focus loss or a hidden tab. It does the same when you leave the Motion tab, turn off the switch or lose control.
- After a refused or interrupted hold, release the button or the key, then press again. **Stop stage** and **STOP ALL MOTION** stop a held axis at all times.
- **Named positions** sends `goto` for each named position of an axis. **Named poses** sends 1 `pose` for the selected pose. The pose moves 1 axis at a time in the configured order. It is available when all 3 axes are homed or zeroed. Both show only when the stage config has them.
- **Step moves and zero** (folded) has 1 block for each axis: the reference state, **Step −** and **Step +**, **Home…**, **Set zero…**, **Check reference** and **Teach reference…**.
- **Step −** and **Step +** send 1 `move` of the entered steps and pulses/s. A step move needs no reference. Soft limits apply when the axis has a confirmed reference.
- **Set zero…** shows only when the stage service can zero the axis. It needs the [stage calibration](stage-panel.en.md#stage-calibration) file `/var/lib/mg400/stage/stage.toml` and zero support in the firmware. Without them, the fold says so, and hold and step still work. Without stage control, the fold says that control is necessary. **Home…** (boards with switches) and **Set zero…** need a confirmation. **Set zero…** sets the zero and the soft limits.
- For an axis with a camera reference, **Check reference** shows the camera frame with the marker and the marker offset in pixels. **Teach reference…** needs a confirmation. It saves the current marker position as the camera reference of the axis in the stage config.

Control socket: the page sends arm keys, stage holds and hold releases over 1 WebSocket, `/lab/api/console/socket`. Chrome opens at most 6 HTTP connections to the console, and the 2 camera views use 2 of them. Requests on the socket do not wait for a free connection. When the socket is closed, the page sends these requests over HTTP. Stop and **STOP ALL MOTION** go over the socket and over HTTP.

### Cameras workspace

- **All views** shows 6 views as a wall that fits the window: camera1 to camera4, the thermal camera and the microscope (Toupcam). Inside its frame, each view shows its role, a green dot while live or STALE, OFFLINE or FROZEN with the frame age, its mode with the measured frame rate, and **Snapshot** and **Expand**. The tooltip on the role shows the camera ID. The microscope shows its decoded size while it plays. Camera 4 adds the detection count to the mode line.
- **Large view** shows 1 view large and a rail of the other 5. **Expand** or a click on a rail view puts that view in the large place. **All views** goes back to the wall.
- The measured frame rate updates about every 1 s, for example `640×480 · 29.8 fps`. An H.264 view counts the frames that the browser shows. An MJPEG view shows the frames per second that the camera preview or the microscope relay sent to each viewer in the last 2 s. With several viewers of the same view, it shows their mean. The thermal view counts the new frames that it gets. Without a new measurement, the view shows no frame rate.
- camera1 to camera4 and the microscope load directly from the proxy. A view plays H.264 video from `/camera/<view>.mp4` at the camera frame rate and stays near the newest frame. A view of camera1 to camera4 wider than 640 uses at most 4 Mbit/s, a smaller one at most 2 Mbit/s, and the microscope view at most 3 Mbit/s. This keeps room on the VPN link for control traffic.
- If the browser supports HEVC, a view first requests `/camera/<view>.mp4?codec=h265`. If H.265 does not start to play, the page uses H.264.
- If the browser or the host cannot play H.264, camera1 to camera4 load MJPEG from `/camera/<view>.mjpeg`. The camera preview sends each viewer only the newest frame. Wall and rail views get at most 3 frames a second, and the large view at most 6.
- **Snapshot** on camera1 to camera4 downloads a `4000x3000` JPEG from `/camera/<view>.full.jpg`. The 4 cameras share 1 USB 2 link, so go2rtc captures only that camera during the still. The previews and readers of the other cameras stop for approximately 3 seconds and then recover automatically. Only 1 still runs at a time. Other requests wait.
- The console relays the thermal frame, the microscope MJPEG fallback stream and the microscope **Snapshot**. If the microscope view cannot play H.264, it loads the relayed MJPEG. The microscope **Snapshot** downloads the Toupcam frame as a lossless PNG at full size, `2592x1944`. It needs no control.
- While a microscope job runs, **Snapshot** on camera3 returns `409`, because an objective switch reads the side stream.
- The microscope relay sends only the newest complete frame. Wall and rail views get at most 10 frames a second, and the large view at most 30. It opens at most 2 microscope streams.
- The streams stop when you leave the workspace, hide the browser tab or close the page.
- You can switch each overlay on or off: the chip detection boxes on camera 4, and the thermal minimum, maximum and center markers with the colour bar.
- The thermal view shows its maximum and mean temperatures. The colour bar and the markers show the minimum and the center. **Settings** on the thermal view opens the thermal settings beside it: the units, the palette, an auto or fixed colour scale, and Freeze view. These settings change only the display. The settings also have the crucible region, the shutter calibration and the export of CSV, Raw + JSON and PNG. Esc, **Close** or a press outside the settings closes them.
- On the thermal view, the pointer reads the temperature of 1 sensor pixel. The text at the bottom of the view shows the pixel and its temperature. Click to pin the pixel. Click again to unpin it.
- When the thermal service has no camera connection, the thermal settings show **Connect camera**. **Calibrate shutter…** needs a confirmation. These 2 commands are `any_client` commands and need no control.
- **Draw region** closes the settings. Drag the `crucible` region on the thermal view. A bar on the view shows the bounds, **Save region** and **Cancel**. **Save region** is available only when the thermal service lists `set_region` in `api/describe`, and it needs a confirmation in the page. Otherwise, put the bounds in `rois.toml`.
- Set the camera devices on the host as described in [Cameras](olares.en.md#cameras).

### Furnace + microscope workspace

Every device command goes through the console relay. Use **Take control** for the furnace or the microscope first. **Stop heat and gas**, **Stop all gas**, **Close** and the microscope **Stop** need no control.

- The furnace shows PV, SV, the mode and the maximum temperature of the thermal `crucible` region. When the data is older than 3 seconds, the page shows its age and dims the readings and the chart.
- The chart shows PV and SV for the last 60 minutes. The console process records 1 point every 5 s while a page is open. After a restart the chart starts empty.
- **Apply and run…** and **Start sequence…** heat the furnace and need a confirmation in the page. The target and the ramp cannot exceed the caps that the service describes. The labels of the target and ramp fields show the caps. The controller ramp shows next to the mode only while the furnace runs, ramps or holds.
- The sequence editor adds and removes steps and sets the target, ramp, hold and 3 gas channels of each step, and the end gas and end temperature. When a step sets gas, the end temperature is necessary. The page loads the furnace sequence on its first read.
- Without `MG400_FURNACE_GAS_MAP`, the gas channels are named Gas 1 to Gas 3. **Stop heat and gas** and **Stop all gas** use the routes of the Overview **Safety** card. The stop result band shows the result.
- Each gas row shows the actual flow and the valve state. A valve state that the flow controller did not confirm shows not confirmed.
- **Close** stops 1 gas channel through `ws/process/gas-stop`. It sets the flow to 0 and closes the valve, and needs no control. The page shows the result: confirmed, not confirmed or unknown.
- When a fault locks the furnace, **Acknowledge fault…** shows. It needs furnace control and a confirmation in the page. Before you acknowledge, make sure that the controller shows STOP.
- The microscope card shows 1 large preview. The **Microscope** and **Side camera** switch in the preview selects the Toupcam view or the camera3 side view. Only the shown view streams. The preview uses `window.LabCameras.tile` from the Cameras workspace when it exists. Otherwise it shows OFFLINE.
- The objective buttons, nudges, jog and autofocus are available only when the focus zero is known. The service raises the focus to clearance before it switches the objective. The focus bar shows the position on the measured limits.
- The objective buttons and the focus controls are below the preview: the focus bar, **−** and **+** with the jog pulses between them, **Autofocus** (local), **Full autofocus** and **Set focus zero…**.
- **Turret**, **Recording** and **Sequence** are closed groups. Open a group to use its controls.
- The **−10**, **−1**, **+1** and **+10** buttons in **Turret** turn the turret in full steps at the current focus. They do not raise the focus to clearance first. The text beside them shows the turret offset and its limit. A button is unavailable when the offset is unknown, or when the nudge would move the offset outside the limit.
- **Set focus zero…** needs microscope control and a confirmation in the page. The console sends it 1 time. The focus gears can slip. Re-zero at the 50x focus before each use. Agents, recipes and the runner cannot set the focus zero.
- **Save snapshot** in **Recording** saves 1 PNG. Leave **Snapshot path** empty for the service default, or enter a relative path without `.` or `..` parts.
- **Start recording** and **Record next furnace sequence** send the recording commands with **Run name**, **Mode**, **Interval s** and **Output fps**. **Live** uses no interval. A failed recording shows its error.
- **Turn stabilization on** and **Turn stabilization off** switch the stabilization.

### Run offline

```sh
uv run python -m orchestrator serve --demo --recipes-dir ./recipes --port 8790 --base-path /lab/
```

Open `http://127.0.0.1:8790/lab/`. In demo mode, the first **Take control** starts the owned demo service of that device. The microscope has no owned demo.

## Stop controls

Keep the physical emergency stop reachable. A web request or a displayed confirmation is not a physical safety interlock.
These stop controls do not require **Take control** or a control lease.
The header has **STOP ALL MOTION**. The **Safety** card of the Overview has **Stop heat and gas** and **Stop all gas**:

| Button | Effect |
| --- | --- |
| **STOP ALL MOTION** | Sends independent stop requests to the robot, stage, and microscope in parallel. It also holds the active run and cancels recovery. Holding a run can stop its acquired heat, gas, and water outputs. |
| **Stop heat and gas** | Cancels the furnace sequence and stops heat and all furnace-controlled gas and water. It also holds the active run and cancels recovery. |
| **Stop all gas** | Independently requests gas-only stops for channels `0`, `1`, and `2`, without holding the run. An active sequence or recipe can apply gas again. Furnace fault handling can also stop other outputs. |

The stop result band shows the button and the time, the phase and the result of each target. The time is when this page sent the stop or first saw it. A new stop replaces the band. **Dismiss** hides it.

- Motion and heat stops go through Stopping, Holding run, Final stop and Done. Gas stops go through Stopping and Done.
- Before Done, a confirmed motion or heat target shows Confirmed (provisional).
- When the request does not get to the console, the band shows Not sent and the error.

Read every target result. A target can be pending, confirmed, unconfirmed, unavailable, or failed.
For motion and heat stops, initial confirmations are provisional while the run hold and accepted work drain.
New control work remains blocked until that work drains and the service completes its final stop attempts.
An operation that finishes can still contain unconfirmed or failed targets.
Do not infer that all motion stopped from one successful target or an accepted request.
If a result is unknown, check the device and follow its recovery procedure before continuing.

## Run control and recovery

The console has no controls yet to hold, abort, or recover a run. Use the run routes of the [recipe service](orchestrator.en.md#http-service).

## Agent proposals

The console has no controls yet to approve agent proposals. The Overview shows only the number of waiting proposals. See the [gateway policy](agent-gateway.en.md#approval-policy) for supported steps and limits.

## Chip passport

Open `/chip` under the configured service prefix, or select **Chip passport** in the console header.
The page uses the console colours and its language switch. It keeps the language choice of the console. **Lab Console** in the header goes back to the console.
Search with the chip's exact QR text.
Search preserves case, spaces, slashes, and punctuation. It does not trim or normalize the identifier.
For a direct link, use `/chip?id=` followed by the URL-encoded identifier.
The ordinary `/chip/<id>` route also opens the passport shell.

The page does not acquire a control lease or contact instruments.
The passport shows saved records only. An empty result does not prove that no physical work occurred.

Run summaries include runs associated with the exact chip identifier.
Within each run, steps, samples, events, and artifacts include only records explicitly associated with that chip.
Other chips' records are not included through a shared run.
The page loads 25 run summaries at a time. Each opened run loads 100 records per category at a time.
Use **Load more** to continue each list. The first page is not the complete history.

The passport shows recorded transfers, step parameters and results, samples, and events.
Its furnace PV chart uses explicit finite `pv_c` or `furnace.pv_c` values with recorded timestamps.
It does not infer temperatures from missing fields or create thermal readings that were not saved.
Missing chart data remains unavailable while other recorded data remains visible.
The server redacts private fields, known credentials, and configured private endpoint URLs in displayed records.
Invalid JSON payloads and payloads larger than 256 KiB show an unavailable marker.

Artifact requests use an opaque record ID and the exact chip identifier. They do not accept a filesystem path.
Only checked private regular files can be read. Symlinks, hard links, unsafe permissions, and changed paths or files cause refusal.
The read limit is 16 MiB per artifact. A refused read does not delete or rewrite the saved record.
The page previews PNG, JPEG, GIF, and WebP files identified by their bytes.
It fetches these files with authorization and uses temporary browser blob URLs for previews.
Other file types are available only as downloads. The browser never receives the stored filesystem path.
