# Lab Console and chip passport

[中文](dashboard.md)

The `orch` service serves the Lab Console and the chip passport pages. See [deployment](deploy.en.md) and [recipe service configuration](orchestrator.en.md#http-service).
The installed unit serves the console at `http://127.0.0.1:8772/lab/`.
The proxy uses `/lab/` and redirects `/` to `/lab/`. On Olares, connect the VPN and open `http://100.64.0.1:8080/`.

## Lab Console

The console is the only operator web page.

- Row 1 of the header shows the console name, the workspace tabs, the **Chip passport** link, the devices that this page controls and the language switch. Hover over the console name to see the deployed commit. The controlled devices show only while this page holds 1 or more devices. Row 2 is the status strip. **STOP ALL MOTION** is the tall button at the right of the header and spans the 2 rows.
- The workspaces are **Overview**, **Motion**, **Cameras**, **Furnace**, **Sample Transfer**, **Twin** and **Position Finder (legacy)**. The legacy workspace is last and starts disabled. Sample Transfer uses live encoder rotations and saved named positions.
- The language switch changes between English and 中文. The browser keeps the selected language.
- Select **Dark mode** or **Light mode** beside the language switch to change the dashboard appearance. Light mode is the default. The browser saves the choice for this Console address.
- The status strip shows 1 chip for each service: a dot and a text. The **Runs** chip shows only while a run exists. A state that needs attention (for example stale, fault or unavailable) has a colored background. A status older than 3 seconds shows as stale.
- When the console does not answer for 3 seconds, the strip shows only **Console offline** and the age of the last state. Check the VPN and the `orch` service.
- The stop result band below the header shows the last stop of the header or the **Safety** card, from any page or client. See [stop controls](#stop-controls).
- In a window wider than 720 px, the header, the status strip, the stop result band and the notice stay at the top of the window when you scroll.
- A service that is not configured shows Not configured on every workspace.
- Chinese text uses the existing bundled font or a system fallback. Prioritize English labels. Do not rebuild fonts only for translated text.
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
- Agent pages: `labctl` uses the page identity `agent-<session>`. The holder on the **Control** card is `agent-<session>-<12 hex>`. The console renews an agent page's leases every 1 second. It releases them after 10 minutes without a request from that page. Any request from the page counts. While a `labctl do` run borrows a device, the page cannot command or release it, and the idle timeout does not apply.
- `GET /lab/api/console/state/<device>` returns the full state of 1 device and its console holder. It needs no control.

### Command relay

The workspaces send manual commands through the console. The console forwards each command to the service.

- Only the commands in the service's `api/describe` are permitted. Lease operations cannot be forwarded.
- A command needs control by this page, except a command that the service marks `any_client`.
- Each command is sent 1 time. If the outcome is unknown, the page shows unknown and does not retry. Check the device before you continue.

### Overview workspace

- **Getting started**, above **Control**, explains device status, camera views, control ownership, workspaces and stop controls.
- **Control** follows **Getting started**. It has 1 row for each device with **Take control** or **Release**, and **Release all**. See [Control](#control).
- **Run** shows the current run and its steps. The card shows only while a run exists. See [run control and recovery](#run-control-and-recovery).
- **Events** merges the `api/events` of each service, newest first. You can filter for warnings or for 1 service. Only a warning or an error shows its level. A successful vision inference is not an event.
- The orchestrator keeps the events and the device state changes in `/var/lib/mg400/runs/runs.db` for 30 days. They stay after a service restart. A device whose values change gets at most 1 state row each 10 s. A phase or status change gets a row at once. Clocks, counters, data ages, raw sensor counts, lease countdowns and small read noise in positions, speeds and joint angles are not changes. A device without changes gets 1 row each 5 min, so a gap in the rows means that the orchestrator did not read the device. `api/console/history` and `labctl all history` read the state rows.
- **Safety** gives **Stop heat and gas** and **Stop all gas**. **STOP ALL MOTION** is in the header. Each button acts on 1 press. The stop result band shows the results.

### Motion workspace

The Motion workspace places stage controls beside **Stage position**, which shows encoder rotations: Top is X, Bottom is Y, and Insert has its own travel diagram. Motion reads broadcast positions every 250 ms. Fresh values appear without camera sampling or Position Finder. Stale or untrusted readings show **—**. Plot markers require valid lower and upper limits. The X/Y diagram labels the minimum and maximum coordinates at each corner. Unavailable bounds show **—**.

**Stage position** retains the X/Y and Insert travel diagrams for a new position source. Without a source, readings show **—** and the diagrams show no position marker. Motion does not start Position Finder, sample camera frames or use its calibration. Motor pulse counts do not supply measured coordinates.

With stage control, fresh encoder feedback and calibrated limits, double-click the X/Y map or Insert rail to select a target. A flashing marker shows the target; the current-position marker stays visible. Select **Confirm move** within 5 s, or select **Cancel**. An unconfirmed target expires without movement. X/Y moves Bottom first, then Top, and leaves Insert unchanged. An Insert target moves only Insert. These moves use rotations and do not change saved profiles. STOP cancels movement and clears the target.

Views:

- Each station shows 1 live view. Both offer **Insert rail** (camera2), **Tray** (camera4), **Microscope side** (camera3), **Stage** (camera1), **Microscope** and **Thermal**. The arm starts with **Insert rail**. The stage starts with **Microscope**. Camera selectors wrap above the image.
- On wide screens, the stage and position panels have equal heights, and the camera fills the remaining space. On narrow screens, the panels stack.
- The robot header spans the panel. A larger camera and keyboard controls occupy the left column, with position and suction on the right. Below 720 px, the camera, keyboard controls and readout stack.
- **Snapshot** and **Expand** are inside the frame. Without the Cameras workspace a view shows OFFLINE.

Robot arm:

- The header row has the state, **Enable** or **Disable**, **Home…**, **Take control** and **Stop arm**. **Disable** shows while the arm is enabled, moves or jogs. **Disable** and **Stop arm** need no control. When the robot service has no fresh feedback, the state is Not connected.
- When another page or client holds the arm, the control button says so and is unavailable. A demo arm shows DEMO.
- **Home…** needs a confirmation. It moves J1 to J4 to 0°.
- During an alarm, a coral note at the top of the station lists each controller alarm with its ID, description and solution. **Clear alarm** is in the note. Below the folds, the station shows the message of the robot service.
- After an unconfirmed stop, the note shows Stop not confirmed and **Clear alarm** becomes **Recover**. Check the robot, then select **Recover**. A restart is not necessary.
- **Keyboard jog** is the main arm control. When this page holds control and the robot is enabled, turn on the switch to start keyboard jog at the selected speed. The speed is from 1 to the `keyboard` speed cap that the robot service describes. The key map shows each key and its motion: W/S (reach out and in), A/D (base J1), R/F (Z up and down), Q/E (flange J4) and Z/X (suction: suck and drop). A held key lights up. Z and X run 1 suction action per press, also while the key stays held.
- While the switch is on and the page has focus, hold a key to move. The page sends the held keys through `ws/motion/keys` on each change and every 150 ms while a key is held. The arm stops 0.4 s after the last update. A window focus loss, a hidden tab, leaving the Motion tab, the switch off or a loss of control releases all keys and turns the keyboard off. When the robot service turns the keyboard off, the page shows the reason below the switch. Stop and **STOP ALL MOTION** work at all times.
- The readout shows actual X and Y on a Cartesian plot, with Z and R on 1 row. It also shows J1 to J4 in degrees. While the arm moves, the robot service sends no pose, and the readout shows the last known pose in grey. Without fresh feedback, the readout also shows the last values in grey. Below it is **Suction**: the measured state (Sucking, Blowing or Off) and **Suck** and **Drop**. **Drop** turns suction off and blows a short pulse. The buttons need control and an idle arm. When the robot service reports `held`, the block also shows Held or Not held.
- **Jog buttons** (folded) moves ±X, ±Y, Z up and down, or R ± by 1 step of 0.5, 2 or 10 mm (degrees for R). Each press sends 1 `execute` move from a fresh read of the actual pose. While the keyboard is on, the Z and R buttons show the key that moves the same way: R and F for Z, Q and E for R.
- **Move to a pose** (folded): **Use actual** copies the actual pose. **Execute** moves to the entered pose. The speed is from 1 to the `execute` speed cap that the robot service describes.
- The console sends jog and pose targets with no bounds check and no tool file. The robot service applies its speed cap and refuses motion while the controller reports an alarm. Jog and pose use the `ws/motion/jog` and `ws/motion/move` routes.

Stage:

- The header has the state, **Take control** and **Stop stage**. Stop needs no control. A demo stage shows DEMO.
- If the Arduino is disconnected, select a listed serial port and **Connect**. The list updates automatically from service state. Each port shows its description and device name.
- Positions use measured shaft rotations. Speeds use rotations/s. All 3 axes use fixed 1/8 stepping. Each motor reports its maximum speed. The default speed is 0.25 rotations/s.
- Hold a direction button to move. Release it to stop. Bottom (Y) uses Up/Down, Top (X) uses Left/Right, and Insert uses In/Out. The controller maps `ccw` to Up, Right and In. These directions increase the coordinate. It maps `cw` to Down, Left and Out.
- **Keys** retains I/K for Bottom Up/Down, J/L for Top Right/Left, and U/O for Insert In/Out. A hold moves 1 axis at a time. Loss of focus or control, a hidden tab, or leaving Motion releases the hold.
- The page refreshes `ws/motion/stage/hold` every 150 ms. The console sends `keepalive` every 0.2 s. Without page refreshes for 0.5 s, it releases the hold. Firmware stops the axis 1 s after its last keepalive.
- **Moves, limits and calibration** contains each axis's relative rotations, absolute target and setup controls. A relative move accepts fractions from 0.001 to 10000 rotations. It requires fresh encoder feedback, but no completed calibration.
- **Auto calibrate** moves to the lower switch, sets the lower coordinate to 0, finds the upper switch, then moves to the center. The page shows each phase. New moves remain unavailable during calibration.
- **Move to target** uses the entered rotation coordinate. It requires completed calibration, both limits and fresh feedback. The firmware controls the move.
- **Reset position** sets the measured position to 0 and clears both limits. **Set limit here** records a positive position as the upper limit or a negative position as the lower limit. It rejects 0. These actions invalidate calibration.
- **Clear upper** and **Clear lower** remove only the selected limit. Each visible axis row has **Clear fault**, which resets feedback fault state without moving. These controls require stage control and remain available while an idle encoder reports a fault.
- Live status shows feedback age, encoder state, measured speed, switch contact and faults. Direction-pause counts and calibration recommendations are advisory. They do not start calibration. Unknown or stale measurements show **—**.
- Configured soft limits and switch interlocks remain active. Switch contact does not reset the coordinate. Older pulse firmware can connect and stop, but encoder motion controls remain unavailable. **Stop stage**, each axis's **Stop**, and **STOP ALL MOTION** always remain available.

Control socket: the page sends arm keys, stage holds and hold releases over 1 WebSocket, `/lab/api/console/socket`. Chrome opens at most 6 HTTP connections to the console, and the 2 camera views use 2 of them. Requests on the socket do not wait for a free connection. When the socket is closed, the page sends these requests over HTTP. Stop and **STOP ALL MOTION** go over the socket and over HTTP.

### Cameras workspace

- **All views** shows 6 views as a wall that fits the window: camera1 to camera4, the thermal camera and the microscope (Toupcam). Inside its frame, each view shows its role, a green dot while live or STALE, OFFLINE or FROZEN with the frame age, its mode with the measured frame rate, and **Snapshot** and **Expand**. The tooltip on the role shows the camera ID. The microscope shows its decoded size while it plays. Camera 4 adds the detection count to the mode line.
- **Large view** shows 1 view large and a rail of the other 5. **Expand** or a click on a rail view puts that view in the large place. **All views** goes back to the wall.
- The measured frame rate updates about every 1 s, for example `640×480 · 29.8 fps`. An H.264 view counts the frames that the browser shows. An MJPEG view shows the frames per second that the camera preview or the microscope relay sent to each viewer in the last 2 s. With several viewers of the same view, it shows their mean. The thermal view counts the new frames that it gets. Without a new measurement, the view shows no frame rate.
- camera1 to camera4 and the microscope load directly from the proxy. A view plays H.264 video from `/camera/<view>.mp4` at the camera frame rate. Views wider than 640 use at most 4 Mbit/s, or 3 Mbit/s with NVENC. Smaller views use at most 2 Mbit/s, and the microscope view at most 3 Mbit/s. With NVENC, a camera above `1280x720` has only an H.265 preview. See [Cameras](olares.en.md#cameras).
- If the browser supports HEVC, a view first requests `/camera/<view>.mp4?codec=h265`. If H.265 does not start to play because the browser cannot decode it or the host returns 404, the page uses H.264. If the host does not answer, for example while go2rtc restarts, the view tries H.265 again after the retry delay and does not change to H.264.
- If the browser or the host cannot play H.264, camera1 to camera4 load MJPEG from `/camera/<view>.mjpeg`. The camera preview sends each viewer only the newest frame. Wall and rail views get at most 3 frames a second, and the large view at most 6.
- **Snapshot** on camera1 to camera4 downloads a `4000x3000` JPEG from `/camera/<view>.full.jpg`. A camera with a mode above `1280x720` uses its own mode. This mode change temporarily pauses other camera streams and restores them afterward. Only 1 still runs at a time.
- The console relays the thermal frame. If the microscope view cannot play H.264, it uses `/camera/microscope.mjpeg`. Its **Snapshot** downloads a lossless `2592x1944` PNG from `/camera/microscope.png`. It needs no control.
- While a microscope job runs, **Snapshot** on camera3 returns `409`, because an objective switch reads the side stream.
- The microscope proxy sends only the newest complete frame. Wall and rail views get at most 10 frames a second, and the large view at most 30.
- Browser previews stop when you leave the workspace, hide the browser tab or close the page. Preloaded camera capture continues.
- You can switch each overlay on or off: the chip detection boxes on camera 4, and the thermal minimum, maximum and center markers with the colour bar.
- The thermal view shows its maximum and mean temperatures. The colour bar and the markers show the minimum and the center. **Settings** on the thermal view opens the thermal settings beside it: the units, the palette, an auto or fixed colour scale, and Freeze view. These settings change only the display. The settings also have the crucible region, the shutter calibration and the export of CSV, Raw + JSON and PNG. Esc, **Close** or a press outside the settings closes them.
- On the thermal view, the pointer reads the temperature of 1 sensor pixel. The text at the bottom of the view shows the pixel and its temperature. Click to pin the pixel. Click again to unpin it.
- When the thermal service has no camera connection, the thermal settings show **Connect camera**. **Calibrate shutter…** needs a confirmation. These 2 commands are `any_client` commands and need no control.
- **Draw region** closes the settings. Drag the `crucible` region on the thermal view. A bar on the view shows the bounds, **Save region** and **Cancel**. **Save region** is available only when the thermal service lists `set_region` in `api/describe`, and it needs a confirmation in the page. Otherwise, put the bounds in `rois.toml`.
- Set the camera devices on the host as described in [Cameras](olares.en.md#cameras).

### Position Finder (legacy) workspace

**Position Finder (legacy)** is the last workspace tab. It starts disabled. Use its activation toggle to open **Monitor** and **Calibration**. Turning it off stops sampling and preview requests and releases temporary image data. Saved calibration profiles remain on disk. Enabling it does not connect Motion or Sample Transfer to its measurements.

When enabled, Position Finder estimates stage positions from camera images. The monitor image, detected markers, and axis readings come from the same frame.
The camera dropdown lists the configured shared optical cameras from `camera1` through `camera4`. It selects the overhead stage camera `camera1` by default.

New calibration profiles for real cameras default to `3840x2160`. Monitoring uses the saved profile's image dimensions. Endpoint capture uses the draft dimensions.
Captures and monitoring read the continuous shared stream of the selected camera.
The profile dimensions must match the configured stream dimensions. Other dimensions are refused.
Native captures preserve the camera's actual pixels without image resizing.

Create a calibration profile in the console:

1. Open **Calibration**. Select the camera. Click **New profile**.
2. Enter the profile name and travel.
3. Move the stage manually in the Motion workspace.
4. When the stage is stationary, return to Position Finder (legacy). Capture an endpoint.
5. Capture 2 endpoints for each of the 3 axes, for 6 endpoints in total.
6. Click **Save calibration**. The console automatically selects the saved profile for monitoring.

The list in **Calibration** shows only saved profiles for the selected camera. Click **Load** on a profile row. This loads the editable draft and selects the profile for monitoring. Click **Delete** to delete the saved profile.
If monitoring uses that profile, the console clears that selection. Deletion preserves the editable draft.

Upload a calibration profile:

1. Select the camera used to capture the endpoints.
2. Click **Upload profile**.
3. Select a JSON file no larger than 1 MiB.

The file's `format` must be `furnace-twin.calibration-profile`, and its `schema_version` must be `1`.

The console checks the structure, numeric values, and marker data. It recalculates the axis fits. You can also upload valid partial profiles.
The console saves a snapshot with a new UUID (unique identifier) and binds it to the selected camera.
Uploads never overwrite existing profiles. The console automatically selects the new profile for monitoring.

In **Monitor**, select a saved profile from the profile dropdown. Click **Start sampling**. Click **Pause sampling** to stop sampling.
Monitor uses the selected saved profile. Draft edits affect monitoring only after you save them.

**Target rate** sets the sampling frequency limit. When capture is slower, the console skips missed sample times, so the actual frame rate decreases.
During sampling or automatic tuning, Position Finder sends no separate preview requests.
Matching native captures read the preloaded stream without an overlay or a go2rtc restart. They do not check whether the microscope is busy.
Captures that require a mode change temporarily pause the other camera streams and restore them afterward.
When a microscope side camera is configured, a microscope job blocks mode changes.

**Save calibration** needs no local file picker. Position Finder sends no motion commands.
You can save a partial profile and capture the remaining endpoints later.
The console saves host profiles in `/var/lib/mg400/calibration/twin/profiles/`.
The console records the selected profile in `/var/lib/mg400/calibration/twin/selection.json`.
Demo mode uses temporary profiles and a fixed `640x480` synthetic scene. It reads no host profiles or real cameras.

### Furnace workspace

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
- Each objective button shows the objective name that the service describes, for example 10x. Without a name, it shows the objective ID.
- The objective buttons, nudges, jog and autofocus are available only when the focus zero is known. The service raises the focus to clearance before it switches the objective. The focus bar shows the position on the measured limits.
- The objective buttons and focus controls are below the preview. Focus controls include the focus bar, **−**, **+**, **Autofocus** (local), **Full autofocus** and **Set focus zero**. The jog pulses appear between **−** and **+**.
- **Lens calibration** is beside the objective buttons. Use it to choose a saved complete profile. The 5x, 10x, 20x and 50x buttons use that profile. The selection persists after a service restart.
- **Turret**, **Recording** and **Sequence** are closed groups. Open a group to use its controls.
- The **−10**, **−1**, **+1** and **+10** buttons in **Turret** turn the turret in full steps at the current focus. They do not raise the focus to clearance first. The text beside them shows the turret offset and its limit. A button is unavailable when the offset is unknown, or when the nudge would move the offset outside the limit.
- **Set focus zero** needs microscope control and no confirmation. The console sends it 1 time. The focus gears can slip. Re-zero at the 50x focus before each use.
- **Save snapshot** in **Recording** saves 1 PNG. Leave **Snapshot path** empty for the service default, or enter a relative path without `.` or `..` parts.
- **Start recording** and **Record next furnace sequence** send the recording commands with **Run name**, **Mode**, **Interval s** and **Output fps**. **Live** uses no interval. A failed recording shows its error.
- **Turn stabilization on** and **Turn stabilization off** switch the stabilization.

#### Turret calibration

Use **Turret calibration** to record each objective after manual positioning. Only the current objective marker must be in focus and fully visible. Objective markers have approximately 8 mm sides: ID 0 = 5x, ID 1 = 10x, ID 2 = 20x, ID 3 = 50x.

1. Take microscope control. We recommend that you first move the focus manually to the clearance position shown in the interface. Click **New profile** and enter a profile name.
2. Click **Start calibration**. It releases only the turret motor. It does not move the focus axis.
3. Seat the current objective in its detent manually. Check Camera3, then click **Record**. Click **Next** to record all 4 objectives. **Retake** replaces the current objective image.
4. Check the captured images. After recording all 4 objectives, click **Finish calibration**. Then click **Save & use** to save and select the profile. To end early, click **End session**. Then use **Save profile** to save the incomplete profile.

**Live** shows the live image. **Captured** shows the current objective capture. Calibration polling and preview stop when you leave the workspace. A failed capture or upload shows an error and preserves the previous captures.

The saved profile list offers **Select**, **Download** and **Delete**. Both **Select** and **Lens calibration** select a complete profile. Use **Load draft** for an incomplete profile. Click **Start calibration** again to continue capture. The camera and focus position must match the draft. **Upload JSON** accepts profile files of at most 2 MiB.

Alignment uses recorded image coordinates. Keep Camera3 fixed. After moving or rotating Camera3, or changing its focus or zoom, record all 4 objectives again.

A complete profile contains captures for all 4 objectives. Recorded focus is informational and does not affect profile completeness or selection. Unknown focus stays unknown instead of becoming `0`. The interface shows clearance and known recorded positions in `MICRO 4` pulses. The current focus zero must be known before automatic movement. The focus axis must reach the configured clearance position before the turret turns.

The interface shows when focus is not zeroed or the profile cannot support automatic switching. It shows an error when the selected profile is invalid.

### Sample Transfer workspace

**Operate** shows Extract/Insert controls, **Move Out Stage**, **Move to Microscope**, saved named positions and live encoder coordinates. **Calibration** contains manual adjustment, position recording and profile management. Both views contain stop controls. Coordinates use motor rotations: X is Top, Y is Bottom, and Insert is the insertion axis. Sample Transfer does not read Position Finder (legacy) images or profiles.

Take stage control in Motion or Sample Transfer. In Motion, run **Auto calibrate** for each axis used by the action. The firmware finds the lower and upper switches, sets the lower position to zero, then moves to the midpoint. Calibration must finish before an absolute move. After a board restart or zero reset, calibrate again.

**Extract Sample** moves only Insert to coordinate `0`. **Insert Sample** moves only Insert to its current firmware upper limit. These actions require a calibrated Insert axis and fresh feedback, but no saved profile. Coordinate `0` must be within the current bounds. X/Y do not move.

In **Calibration**, record the X/Y positions for **Move Out Stage** and **Move to Microscope**, then select **Save profile**. Each record contains only X and Y encoder rotations. In Operate, each button moves Bottom first, then Top, to its target in the selected saved profile. Insert does not move and does not need calibration for these records. A missing or out-of-range target disables its action.

In **Calibration**, adjust the motors to the required position. Each manual button moves 0.1 rotations. Enter a position name, then select **Record position**. The server records fresh, idle encoder coordinates for all axes. **Record again** replaces that draft record. Save the profile before using its positions in Operate.

In **Operate**, **Move to position** uses the selected saved profile. It moves Bottom, then Top, then Insert. Only 1 axis moves at a time. Each axis must finish within its reported tolerance before the next starts. An axis already within tolerance is skipped. The firmware controls each absolute movement. Stale feedback, faults, lost control or Stop cancel the remaining sequence. Chip recognition and robot transfer are not implemented in this workspace.

New profiles default to `Stage Coordinate Calibration YYYY-MM-DD`, using the current local date in the browser. **Save profile** saves a new profile and selects it when it contains named positions or X/Y targets. Saving edits to the selected profile also selects an empty result, so removing all saved targets disables their actions. Extract/Insert remain independent of profiles. **Load draft** resumes editing without selecting another profile. The list supports **Select**, **Delete**, **Upload JSON** and **Download**. Delete individual named positions or X/Y targets from a draft, then save it. Draft edits do not change the saved targets until saved. Selecting a profile does not move the stage.

Transfer files use `sample-transfer.calibration-profile`, schema version `5`, with `unit: rotations`. Named `position_rotations` records remain in `positions`; X/Y targets are in `stage_positions`, keyed by `out_stage` and `microscope`. Versions `3` and `4` retain their named positions and load with empty X/Y targets. Version `4` also preserves its previous `transfer_endpoints` as named full-coordinate positions with unique legacy names. Those records do not become X/Y targets automatically. The server validates uploads before saving them. Uploads are limited to 128 KiB. New captures allow 64 named positions; migration can preserve up to 66. Names can contain up to 120 characters. Host profiles persist under `/var/lib/mg400/calibration/sample-transfer/`.

Schema versions `1` and `2` retain their millimetre coordinates and source metadata. Version `1` endpoints load as **Fully out** and **Fully in** named positions. Unknown X or Y values stay unknown. These legacy profiles remain available for review, export and deletion. They cannot record encoder positions or command movements. Create a new rotation profile instead. Loading does not rewrite the original files. Existing `motion-scale.json` files remain on disk but are not used.

**STOP** and **STOP ALL MOTION** need no control lease. Leaving the workspace or hiding the page requests a stop for motion started by that page.

### Twin workspace

The Twin workspace shows a 3D bench model with live robot, microscope turret and furnace state. It polls 4 times per second while open. It sends no commands and takes no control. Encoder stage readings use rotations, but stage geometry remains uncalibrated.

- The page loads three.js r128 from `cdn.jsdelivr.net`. The browser must have internet access for this workspace.
- The model is the `twin-model` asset at `/var/lib/mg400/twin/twin.glb`. To install it, run `install.py --fetch-assets` (see [External assets](deploy.en.md#external-assets)). To build a new model, run `build.py -- --console <path>` in the twin authoring folder, outside the repository. The model shows the bench as in the first bench video. Joint value 0 is that state.
- Robot J1 to J4 come from the robot joints. When the robot feedback is stale, the robot is see-through. During an alarm, the robot is red.
- Encoder stage coordinates have no calibrated mapping to the model. Stage geometry remains see-through. Do not apply the old pulse-to-millimetre mapping to rotations. A future mapping must define each axis's millimetres per rotation, direction and model-zero offset.
- While the stage moves, the stage and the insert axis are blue.
- The turret turns 72 degrees for each objective from objective 1. The turn direction is not checked. The focus does not move in the model.
- Above 50 °C the furnace glows. The glow is full at 1000 °C. A tag above the furnace shows PV.

### Run offline

For a local dashboard preview on Windows or Linux, run this command from the repository root:

```sh
uv run --no-default-groups --group vision --group stage python scripts/console_preview.py
```

Open `http://127.0.0.1:8790/lab/#motion`. The preview uses the real dashboard assets and requires no VPN or Olares connection. Open `#position-finder` and use the activation toggle to inspect the legacy monitor and calibration with synthetic camera images.

Open `http://127.0.0.1:8790/lab/#process` to preview turret calibration. Camera3 uses a synthetic `1280 × 720` image. **Start calibration**, objective capture and profile operations use a local simulator. They require no physical microscope control. Profiles persist under `work/console-preview/calibration/microscope/turret_profiles/`.

Open `http://127.0.0.1:8790/lab/#sample-transfer` to inspect both views, saved profiles, manual adjustment and STOP. Calibrate the simulated stage in Motion before recording or moving to positions. Transfer profiles persist under `work/console-preview/calibration/sample-transfer/`.

Legacy mock profiles use `3840 × 2880` images. Existing `640 × 480` profiles remain unchanged. To reset the local profile store, stop the preview and rename `work/console-preview/calibration/twin/` to an unused backup name. The next legacy activation creates `Mock stage` again.

Other device controls remain unavailable. Local calibration profiles persist under `work/console-preview/` and do not change Olares profiles. First legacy activation creates a `Mock stage` profile if its directory is absent. Synthetic movement cannot validate physical accuracy or real device commands.

Refresh the browser after CSS or JavaScript changes. Restart the preview after Python or HTML template changes. Use `Ctrl+C` in its terminal to stop it. Use Olares to verify real camera resolution, capture timing, and device commands.

For the full device demo on Linux:

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
It fetches these files without credentials and uses temporary browser blob URLs for previews.
Other file types are available only as downloads. The browser never receives the stored filesystem path.
