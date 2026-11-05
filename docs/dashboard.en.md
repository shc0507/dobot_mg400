# Lab Console and chip passport

[中文](dashboard.md)

The `orch` service serves the Lab Console and the chip passport pages. See [deployment](deploy.en.md) and [recipe service configuration](orchestrator.en.md#http-service).
The installed unit serves the console at `http://127.0.0.1:8772/lab/`.
The proxy uses `/lab/` and redirects `/` to `/lab/`. On Olares, connect the VPN and open `http://100.64.0.1:8080/`.

## Lab Console

The console is the only operator web page.

- The header shows the console name, the deployed commit, the workspace tabs and the **Chip passport** link. It also shows the language switch, the devices that this page controls, and **STOP ALL MOTION**.
- The workspaces are **Overview**, **Motion**, **Cameras** and **Furnace + microscope**.
- The language switch changes between English and 中文. The browser keeps the selected language.
- The status strip shows 1 chip for each service: a dot and a text. A status older than 3 seconds shows as stale.
- A service that is not configured shows Not configured on every workspace.
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

- **Run** shows the current run and its steps. See [run control and recovery](#run-control-and-recovery).
- **Events** merges the `api/events` of each service, newest first. You can filter for warnings or for 1 service.
- **Safety** gives Stop all motion, Heat off and Gas off, and shows the result for each target. Heat off and Gas off need a confirmation in the page.
- **Agent proposals** shows the number of waiting proposals. See [agent proposals](#agent-proposals).

### Motion workspace

The Motion workspace has 3 columns: the robot arm, the cameras and the stage. At phone width they stack. **Robot and stage activity** below them lists the events of both devices and the outcome of each command from this page.

Robot arm:

- **Take control**, **Enable**, **Disable**, **Clear alarm**, **Home…** and **Stop arm**. **Disable** and **Stop arm** need no control.
- **Home…** needs a confirmation. It moves J1 to J4 to 0°.
- The card lists each controller alarm with its ID, description and solution. Below the alarms, the card shows the message of the robot service.
- After an unconfirmed stop, the card shows a warning and **Clear alarm** becomes **Recover**. Check the robot, then select **Recover**. A restart is not necessary.
- The pose tiles show the actual X, Y, Z and R. They show `—` while the arm moves. The joint tiles show J1 to J4 in degrees.
- The jog pad moves ±X, ±Y, Z up and down, or R ± by 1 step of 0.5, 2 or 10 mm (degrees for R). Each press sends 1 `execute` move from a fresh read of the actual pose.
- **Move to a pose**: **Use actual** copies the actual pose. **Execute** moves to the entered pose. The speed is from 1 to the `execute` speed cap that the robot service describes.
- The console sends jog and pose targets with no bounds check and no tool file. The robot service applies its speed cap and refuses motion while the controller reports an alarm. Jog and pose use the `ws/motion/jog` and `ws/motion/move` routes.
- **Gripper**: **Open** (1000), **Close** (0) and **Initialize…**. Initialization opens the jaws and needs a confirmation. **Move** sends the entered **Position** from 0 to 1000. The gripper outcome shows beside the title, and the gripper message shows below the buttons.
- **Keyboard**: when this page holds control and the robot is enabled, turn on the switch to start keyboard jog at the selected speed. The speed is from 1 to the `keyboard` speed cap that the robot service describes. While the switch is on and the page has focus, hold W/S (reach out and in), A/D (base J1), R/F (Z up and down), Q/E (flange J4) or Z/X (gripper open and close). The page sends the held keys through `ws/motion/keys` on each change and every 150 ms while a key is held. The arm stops 0.4 s after the last update. A window focus loss, a hidden tab, leaving the Motion tab, the switch off or a loss of control releases all keys and turns the keyboard off. When the robot service turns the keyboard off, the page shows the reason below the switch. Stop and **STOP ALL MOTION** work at all times.

Stage:

- **Take control**, **Stepping** and **Stop stage**. **Stop stage** needs no control.
- If the Arduino is not connected, pick 1 of the listed serial ports, then select **Connect**. **Refresh ports** reads the port list again.
- A change of the stepping mode clears the zero and soft limits of all 3 axes and needs a confirmation.
- Each axis shows its position in pulses and in mm, and its reference state. **Step −** and **Step +** send 1 `move` of the entered steps and pulses/s. A step move needs no reference. Soft limits apply when the axis has a confirmed reference.
- **Hold −** and **Hold +** move the axis at the entered pulses/s while you press the button. Release the button to stop the axis. 1 axis moves at a time. When the axis has a confirmed reference, the axis stops at its soft limits.
- The console sends the stage commands of a hold. It sends 1 `hold`, then 1 `keepalive` every 0.2 s while the page refreshes the hold. The page refreshes the hold through `ws/motion/stage/hold` every 150 ms. When no refresh arrives for 0.5 s, the console releases the hold. If the console stops, the firmware stops the axis 1 s after the last `keepalive`.
- **Keyboard**: when this page holds stage control, turn on the switch. While the page has focus, hold I/K (Bottom − and +), J/L (Top − and +) or U/O (Insert − and +). Each key moves its axis like the Hold button. The page releases the hold and turns the keyboard off on a window focus loss or a hidden tab. It does the same when you leave the Motion tab, turn off the switch or lose control.
- After a refused or interrupted hold, release the button or the key, then press again. **Stop stage** and **STOP ALL MOTION** stop a held axis at all times.
- **Home…** (boards with switches) and **Set zero…** need a confirmation. **Set zero…** sets the zero and the soft limits.
- For an axis with a camera reference, **Check reference** shows the camera frame with the marker and the marker offset in pixels.
- **Teach reference…** needs a confirmation. It saves the current marker position as the camera reference of the axis in the stage config.
- **Named positions** sends `goto` for each named position of an axis.
- **Named poses** sends 1 `pose` for the selected pose. The pose moves 1 axis at a time in the configured order. It is available when all 3 axes are homed or zeroed.

Control socket: the page sends arm keys, stage holds and hold releases over 1 WebSocket, `/lab/api/console/socket`. Chrome opens at most 6 HTTP connections to the console, and the camera streams use 3 of them. Requests on the socket do not wait for a free connection. When the socket is closed, the page sends these requests over HTTP. Stop and **STOP ALL MOTION** go over the socket and over HTTP.

Cameras: camera2 is large, camera4 and camera3 are small. **Swap view** puts a small view in the large tile. Without the Cameras workspace a tile shows OFFLINE.

### Cameras workspace

- The grid shows 6 views: camera1 to camera4, the thermal camera and the microscope (Toupcam). Each view shows its name and role, LIVE, STALE or OFFLINE with the frame age, its mode with the measured frame rate, and **Snapshot** and **Expand**. **Focus · 1 + strip** shows 1 large view and a strip of the others.
- The measured frame rate updates about every 1 s, for example `640×480 · 29.8 fps`. An H.264 view counts the frames that the browser shows. An MJPEG view shows the frames per second that the camera preview or the microscope relay sent to each viewer in the last 2 s. With several viewers of the same view, it shows their mean. The thermal view counts the new frames that it gets. Without a new measurement, the view shows no frame rate.
- camera1 to camera4 and the microscope load directly from the proxy. A view plays H.264 video from `/camera/<view>.mp4` at the camera frame rate and stays near the newest frame. Each view of camera1 to camera4 uses at most 2 Mbit/s, and the microscope view at most 3 Mbit/s. This keeps room on the VPN link for control traffic.
- If the browser or the host cannot play H.264, camera1 to camera4 load MJPEG from `/camera/<view>.mjpeg`. The camera preview sends each viewer only the newest frame. Grid and strip views get at most 5 frames a second, and a large view at most 10.
- **Snapshot** on camera1 to camera4 downloads 1 frame from `/camera/<view>.jpg`.
- The console relays the thermal frame, the microscope MJPEG fallback stream and the microscope **Snapshot**. If the microscope view cannot play H.264, it loads the relayed MJPEG. The microscope **Snapshot** downloads the Toupcam frame as a lossless PNG at full size, `2592x1944`. It needs no control.
- camera3 gives snapshots only at its stream size. A `3840x2160` capture truncates `camera2` frames on the shared USB 2 link and interrupts the side reader.
- The microscope relay sends only the newest complete frame. Grid and strip views get at most 10 frames a second, and a large view at most 30. It opens at most 2 microscope streams.
- The streams stop when you leave the workspace, hide the browser tab or close the page.
- You can switch each overlay on or off: the chip detection boxes on camera 4, and the thermal minimum, maximum and center markers with the colour bar.
- When the thermal camera is selected, the side panel gives the units, the palette, an auto or fixed colour scale, and Freeze view. These settings change only the display. You can export CSV, Raw + JSON and PNG.
- On the thermal view, the pointer reads the temperature of 1 sensor pixel. The text below the view shows the pixel and its temperature. Click to pin the pixel. Click again to unpin it.
- When the thermal service has no camera connection, the side panel shows **Connect camera**. **Calibrate shutter** needs a confirmation. These 2 commands are `any_client` commands and need no control.
- **Draw region** drags the `crucible` region on the thermal view and shows its bounds. **Save region** is available only when the thermal service lists `set_region` in `api/describe`, and it needs a confirmation in the page. Otherwise, put the bounds in `rois.toml`.
- Set the camera devices on the host as described in [Cameras](olares.en.md#cameras).

### Furnace + microscope workspace

Every device command goes through the console relay. Use **Take control** for the furnace or the microscope first. **Stop heat and gas**, **Stop all gas**, **Close** and the microscope **Stop** need no control.

- The furnace shows PV, SV, the ramp, the mode and the maximum temperature of the thermal `crucible` region.
- The chart shows PV and SV for the last 60 minutes. The console process records 1 point every 5 s while a page is open. After a restart the chart starts empty.
- **Apply and run…** and **Start sequence…** heat the furnace and need a confirmation in the page. The target and the ramp cannot exceed the caps that the service describes.
- The sequence editor adds and removes steps and sets the target, ramp, hold and 3 gas channels of each step, and the end gas and end temperature. When a step sets gas, the end temperature is necessary. The page loads the furnace sequence on its first read.
- Without `MG400_FURNACE_GAS_MAP`, the gas channels are named Gas 1 to Gas 3. **Stop all gas** uses the gas-off route of the Overview.
- Each gas row shows the actual flow and the valve state. A valve state that the flow controller did not confirm shows not confirmed.
- **Close** stops 1 gas channel through `ws/process/gas-stop`. It sets the flow to 0 and closes the valve, and needs no control. The page shows the result: confirmed, not confirmed or unknown.
- When a fault locks the furnace, **Acknowledge fault…** shows. It needs furnace control and a confirmation in the page. Before you acknowledge, make sure that the controller shows STOP.
- The microscope shows the Toupcam view and the camera3 side view. It uses `window.LabCameras.tile` from the Cameras workspace when it exists. Otherwise it shows OFFLINE.
- The objective buttons, nudges, jog and autofocus are available only when the focus zero is known. The service raises the focus to clearance before it switches the objective. The focus bar shows the position on the measured limits.
- The **−10**, **−1**, **+1** and **+10** buttons below the objective buttons turn the turret in full steps at the current focus. They do not raise the focus to clearance first. The text beside them shows the turret offset and its limit. A button is unavailable when the offset is unknown, or when the nudge would move the offset outside the limit.
- **Set focus zero…** needs microscope control and a confirmation in the page. The console sends it 1 time. The focus gears can slip. Re-zero at the 50x focus before each use. Agents, recipes and the runner cannot set the focus zero.
- **Save snapshot** saves 1 PNG. Leave **Snapshot path** empty for the service default, or enter a relative path without `.` or `..` parts.
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
The header has **STOP ALL MOTION**. The **Safety** card of the Overview has all 3 buttons:

| Button | Effect |
| --- | --- |
| **STOP ALL MOTION** | Sends independent stop requests to the robot, stage, and microscope in parallel. It also holds the active run and cancels recovery. Holding a run can stop its acquired heat, gas, and water outputs. |
| **Heat off…** | Requires confirmation inside the page. It cancels the furnace sequence and stops heat and all furnace-controlled gas and water. It also holds the active run and cancels recovery. |
| **Gas off…** | Requires confirmation inside the page. It independently requests gas-only stops for channels `0`, `1`, and `2`, without holding the run. An active sequence or recipe can apply gas again. Furnace fault handling can also stop other outputs. |

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

Open `/chip` under the configured service prefix.
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
