[中文](microscope.md)

# Microscope service and algorithms

The `microscope` package provides autofocus, objective switching, image calibration, and side-camera capture on Linux.
It runs without a desktop or Tk.
The contract service enforces focus-reference, travel-limit, and turret-clearance checks.
The browser panel uses the service for daily controls. Direct algorithm calls do not acquire the service lease or provide its interlocks.
Before real motion, an operator must establish safe focus travel and clearance from the furnace window.

## Linux environment

Use Python 3.12 or later.
Install the locked dependencies from the repository root:

```sh
uv sync --locked
```

The default `microscope` group contains NumPy, opencv-python-headless, pyserial, Flask, and waitress.
Package imports do not open cameras or serial ports.
The Toupcam adapter is `toupcam_camera.MicroscopeCamera`.
Real Toupcam capture requires the matching Linux vendor library and device permissions.
See the [deployment guide](deploy.en.md) for host configuration.

## Contract service

Display service options without opening devices, or start the simulator:

```sh
uv run python -m microscope --help
uv run python -m microscope --demo --bind 127.0.0.1 --port 8770
```

Demo mode uses simulated motors and generated microscope and ArUco frames.
It uses synthetic focus limits and clearance. These values do not establish safe settings for hardware.
The service opens its devices only when it starts. Invalid configuration exits with status `2`.
On `SIGINT`, `SIGTERM`, or `SIGHUP`, it cancels work, stops and disables the motors, closes devices, and exits.
Cleanup failures produce a nonzero exit status.

| Setting | Configuration |
| --- | --- |
| `MG400_MICROSCOPE_BIND` | Defaults to `127.0.0.1`. |
| `MG400_MICROSCOPE_PORT` | Defaults to `8770`. |
| `MG400_MICROSCOPE_BASE_PATH` | Defaults to `/`. Route paths below are relative to this prefix. |
| `MG400_MICROSCOPE_TOKEN` | At least 24 printable ASCII characters. Required for a non-loopback bind. |
| `MG400_MICROSCOPE_SERIAL` | The controller's stable `/dev/serial/by-id/` path. |
| `MG400_MICROSCOPE_SIDE_STREAM` | The stream owned by go2rtc for the side camera. |
| `MG400_MICROSCOPE_FOCUS_LIMITS` | Required measured `MIN,MAX` integer pulses, including zero, with nonzero travel. |
| `MG400_MICROSCOPE_SWITCH_FOCUS` | Required measured clearance position, in pulses within the limits. Without a home switch, it must be `0`. |
| `MG400_MICROSCOPE_HOME_SWITCH` | Required hardware fact: `1` for a configured focus home switch, or `0` when none exists. |
| `MG400_MICROSCOPE_FURNACE_URL` | Furnace metadata endpoint. Defaults to `http://127.0.0.1:8769/`. |
| `MG400_FURNACE_TOKEN` | Existing furnace API token for metadata reads. The microscope does not acquire furnace control. |
| `MG400_FURNACE_ARCHIVE_DIR` | Empty by default. Set an absolute directory only after the operator mounts the archive share. |

Real startup requires reference-aware firmware and explicit focus settings.
Use the measurements from `H-06`. Do not copy demo limits to hardware.
All requests retain the shared Host, Origin, and forwarded-header checks.
Protected data and action routes also require the configured token.
Without a token, the service accepts only loopback Host values without forwarded headers.
POST requests require `Content-Type: application/json` and `X-MG400-Client`.
The control lease lasts 3 seconds and must be renewed during work.

| Route | Use |
| --- | --- |
| `GET /api/describe` | Contract version, supported commands, objective IDs, and lease behavior. |
| `GET /api/state` | Focus position and zero state, limits, objective ID, camera state, busy state, faults, and command result. |
| `GET /api/events` | Service event history. |
| `POST /api/heartbeat`, `POST /api/renew`, `POST /api/release` | Acquire, renew, or release ordinary control. |
| `POST /api/command` | Submit an action and its parameters. A `202` response returns a command ID, not a completion proof. |
| `POST /api/stop` | Stop from any authenticated client without a controlling lease. Read its `confirmed`, `pending`, or `unconfirmed` result. |

The command actions are `select_objective` with `id`, `autofocus` with `mode` (`full` or `local`), `focus_move` with relative `steps`, `home_focus`, and `snapshot` with an optional `path`.
Read the command ID in `last_command` and its result before the next action.
Every turret turn follows a completed clearance move and a fresh stationary-position check.
Focus moves and autofocus stay within the configured limits and require a known zero.
A full autofocus scan uses a window centered inside the measured range.
A local scan uses a smaller window near the current position.
The service first moves the scan center inside the limits when clearance is at a range endpoint.
Snapshot creates a new PNG under the run root and refuses an existing destination.
Its returned `captured_at` preserves the source clock. Real and demo main-camera frames use host wall time. Side-camera frames use monotonic receive time.
These values do not measure sensor exposure time.

## Focus zero and control handoff

With a configured home switch, `home_focus` establishes zero and installs the configured firmware limits.
Without a switch, an operator must place focus at the verified clearance position before setting zero.
That position is `0`. The service does not move to an unknown clearance position automatically.

For the operator flow, send an empty object to `POST /api/manual/heartbeat` to acquire manual control.
Use `POST /api/manual/renew` to retain it.
Only after physical confirmation, send `{"confirm": true}` to `POST /api/manual/set_focus_zero` and wait for the command result.
Manual zero is unavailable through ordinary contract commands and must not be called by automation.
An automation lease blocks manual acquisition, even if a request reuses the same client ID.
The service checks stationary axes, sets `MICRO 4`, establishes zero, installs limits, and verifies the firmware result.

After zero succeeds, release manual control through `POST /api/manual/release` before another client acquires ordinary control.
A healthy idle release can retain the verified reference only until the former lease's remaining deadline.
The fresh check requires stationary axes, enabled focus, and matching zero, limits, and firmware session.
The next client must acquire control before that deadline. Release does not grant an additional lease period.
If the check fails or the deadline expires, the service stops and disables both motors and invalidates zero.

Explicit stop, lease expiry, firmware reset, and focus-reference changes invalidate the service's zero.
Unknown command or stop outcomes latch a fault and lock ordinary commands.
Stop remains available, but successful stop does not clear that fault.
After resolving the cause, reconnect through a controlled service restart and establish zero again.
Never retry unknown motion automatically or re-establish zero from a cached position.

## Browser panel

Open `/` under the configured base path. For the demo command above, open `http://127.0.0.1:8770/`.
The fixed page and assets contain no service state or access token.
If the service requires a token, enter it and select **Connect**.
The page keeps the token in memory and sends it in the `Authorization` header. It does not put tokens in URLs or browser storage.

Select **Acquire control** before an ordinary action. The page renews its existing lease while connected.
An existing lease held by another client blocks acquisition. The page does not take over automation control.
Objective buttons use IDs from loaded calibration references. Missing or invalid references leave these buttons unavailable.
Install valid reference images, then restart the service to reload cached calibration metadata.
Full and local autofocus, bounded focus jog, and snapshot use the contract commands above.
Read the command result before another action. **Stop** remains available without a controlling lease.

Without a home switch, select **Set focus zero…** only after placing focus at the verified clearance position.
Accept the dialog only after you check the physical position. The page releases its idle ordinary lease, acquires manual control, and submits the zero command.
After successful completion, it releases manual control and immediately acquires ordinary control within the remaining handoff deadline.
Any failed request stops this sequence. Reconnect explicitly after access or lease failure.
With a configured home switch, **Home focus…** asks for confirmation before homing.
Closing the page stops local requests and renewals. Lease expiry then stops and disables both motors and invalidates zero.

| Preview route | Result |
| --- | --- |
| `GET /api/panel-info` | Calibrated objective IDs, calibration availability, and home-switch configuration. |
| `GET /api/camera/microscope/mjpeg`, `GET /api/camera/side/mjpeg` | Authenticated MJPEG views from the existing camera owners. |
| `GET /api/camera/microscope/frame.jpg`, `GET /api/camera/side/frame.jpg` | One JPEG, or `204` when no newer frame is available. |

Preview requests accept an optional nonnegative `after_id` cursor.
Each JPEG carries `X-Frame-Id` and `X-Captured-At` headers. Main-camera timestamps use host wall time, while side-camera timestamps use monotonic receive time.
Do not compare timestamps across cameras or treat them as sensor exposure times.
The browser uses authenticated fetch requests and discards stale preview results.
At most 4 MJPEG responses can remain open. Extra streams return `503`. Close another preview before reconnecting.
This limit leaves workers available for control requests, but does not guarantee a stop response time.
Preview reads do not acquire or renew control, and shutdown waits for accepted reads before closing camera owners.

## Recording and furnace sequences

Use the microscope panel to record from the camera that this service already owns.
Recording commands require live ordinary control and an idle service. They do not acquire furnace control or start a furnace sequence.
Keep the page connected with control during capture and finalization.

| Action | Parameters and result |
| --- | --- |
| `start_recording` | Supply a new `recording_id` containing exactly 32 lowercase hexadecimal characters. Optional fields are `run_name`, `mode`, `capture_interval_seconds`, and `output_fps`. |
| `stop_recording` | Supply the active `recording_id` owned by the same client. Completion includes capture shutdown, summary compilation, and any configured archive copy. |
| `record_with_sequence` | Use `enabled: true`, a new `recording_id`, and optional recording fields to arm once. Use `enabled: false` with that ID to cancel. |
| `set_stabilization` | Supply the boolean `enabled` to change camera stabilization. |

The panel offers **Start recording**, **Finish recording**, and separate sequence arm and cancellation controls.
An optional run name contains at most 80 printable characters. It does not select a file path.
The `time_lapse` defaults are a 30-second capture interval and 1 output frame per second.
The `live` default is 30 output frames per second, with no capture interval.
Explicit `capture_interval_seconds` values range from 0.1 to 3600. Explicit `output_fps` values range from 0.1 to 120.

Read `recording`, `recording_sequence`, and `last_result` with the matching `last_command` receipt.
Check the recording ID, command ID, action, client, and expected result before claiming success.
The panel observes finalization for at most 600 seconds. Other command observations use 120 seconds.
These are observation limits, not encoder completion guarantees. An unknown result causes no automatic retry.
Global Stop remains available while ordinary recording controls are unavailable during motion or finalization.

Sequence recording requires fresh, healthy, idle furnace state when armed.
It waits for a new sequence `started_at`, then records only that sequence.
The same sequence must finish in `complete` or `cooling` before normal finalization.
Arming applies once. The watcher cannot acquire or renew a control lease.

The service polls furnace state at 1 Hz and reads cached values for frame metadata.
The columns in `video.timestamps.csv` remain unchanged.
Demo mode uses generated frames without a furnace connection. Its metadata stays blank unless an embedding supplies fake readings.

Unavailable furnace readings leave metadata fields blank and add a service event while capture continues.
A missing reply does not mean sequence completion. A changed sequence identity or an unhealthy sequence refuses normal completion.

The service reserves recording IDs exclusively under `/var/lib/mg400/recordings/`.
It creates private output files and refuses existing or unsafe destinations.
Finalized recordings retain video segments, `video.timestamps.csv`, and a summary targeting 3 minutes. Shorter clips are not lengthened.
Cancelled or failed recordings retain their local partial files without a promised summary.
Finalization requires recorded frames and valid media. It requires FFmpeg and ffprobe on the host.
If you set `MG400_FURNACE_ARCHIVE_DIR`, finalization verifies the archive copy and retains the local files.
Archive failure leaves local files available and does not report successful finalization.

Stop, release, lease expiry, and shutdown revoke recording authority immediately. The service sends the motor stop commands without waiting for the encoder.
The optional `wait_s` observes motor and recording cleanup within one budget of `0..15` seconds.
`pending` means accepted cleanup remains. `unconfirmed` reports a cleanup failure.
The service remains busy until accepted recording work ends. Check the stop result before new work.
Compiler and archive callbacks check cancellation before publishing success. Revoked work cannot resume or automatically retry.

The orchestrator skill `microscope.record(chip_id, seconds, run_name='', mode='time_lapse')` uses the same owned command flow.
Use a finite positive duration and a step deadline that also permits finalization.
It stores a bounded JSON intent before dispatch and a completed manifest only after an exact finalization receipt.
The manifest links the chip, run, step, recording ID, and media basenames. Media and per-frame CSV data stay in the recording directory.
The chip passport can download the manifest. It does not follow its filenames or serve the video through the run store.
Cancellation or an unknown outcome leaves the intent without a false completed artifact.

## Python interfaces

Pass camera and motor objects explicitly to the algorithm classes.
Use simulated objects for software tests.

| Interface | Purpose |
| --- | --- |
| `microscope.autofocus.AutoFocuser` | Continuous coarse focus scan and adaptive fine focus scan. |
| `microscope.aruco_locator.Locator` | Measure marker displacement from supplied reference images. |
| `microscope.aruco_switch.ArucoObjectiveSwitcher` | Switch or align an objective using side-camera markers. |
| `microscope.objective_calibration.ObjectiveCalibrator` | Calibrate objective motion against an image displacement map. |
| `microscope.light_calibration.LightObjectiveCalibrator` | Align a known reference view with a short scan and autofocus. |
| `microscope.side_capture.capture_objective_series` | Capture fresh side-camera images after objective moves. |
| `microscope.motors.MotorController` | Manage explicit serial connections and command replies. |
| `cameras.uvc.USBCamera` | Read side-camera frames from the local go2rtc owner. |
| `cameras.usb.enumerate_usb_cameras` | List Linux camera metadata and stable device paths without opening capture. |

Image displacement, commanded motor travel, and firmware position are separate values.
A marker reference does not prove mechanical seating or clearance.
The caller must handle cancellation, close camera connections, and stop or disable motors when work ends.

## Calibration and run files

| Setting | Default | Contents |
| --- | --- | --- |
| `MG400_MICROSCOPE_CALIBRATION_DIR` | `/var/lib/mg400/calibration/microscope` | Calibration CSV files, reference images, and side-camera calibration captures. |
| `MG400_ORCH_RUNS_DIR` | `/var/lib/mg400/runs` | Autofocus curves, objective-switch logs, and image analysis outputs. |

Use `microscope.paths.calibration_path()` for calibration paths and `microscope.paths.run_path()` for run paths.
Relative paths resolve under the corresponding root.
Absolute output paths must remain inside that root.
The roots must be absolute and must not contain parent traversal.
Resolving a path creates no files.

Keep `objective_views/`, `current_objective.txt`, `objective_reference.png`, and `side_camera_images/` under the calibration root.
The locator reads the filenames in `microscope.aruco_locator.REFERENCES` from `side_camera_images/`.
It requires matching image dimensions and the expected marker IDs.
Recalibrate after changing the camera position or markers.
Historical CSV files and images are not included with the package.

Output helpers create directories with mode `0750` and files with mode `0640`.
The process umask can make these modes more restrictive.
Existing output directories and files must belong to the current user and retain private permissions.
Writers reject parent traversal, symbolic links, and files with multiple hard links.
Do not substitute repository paths or shared public directories for the configured roots.

## Side-camera stream

The microscope side camera is the AC410 on fixed hub port `12.1.4`. It watches the objective turret.
For the service and preview, go2rtc owns its V4L2 capture.
`USBCamera` reads JPEG frames from `http://127.0.0.1:1984/api/frame.jpeg`.
It rejects redirects and ignores environment proxy settings.
Keep the cameras on their fixed ports and never reset the shared USB hub.

| Setting | Configuration |
| --- | --- |
| `MG400_CAMERA_DEVICES` | Stable device paths in the order used by the camera preview installer. |
| `MG400_MICROSCOPE_SIDE_STREAM` | The configured go2rtc stream for the side camera. There is no default stream. |
| `MG400_CAMERA_MODES` | Optional comma-separated `WIDTHxHEIGHT@FPS` values, with 1 value for each configured device. |
| `MG400_CAMERA_CAPTURE_SIZES` | Legacy comma-separated `WIDTHxHEIGHT` values, with 1 value for each configured device. |

Map the side-camera device path to its stream before enabling capture.
Configure only one mode or size list. The installer rejects configurations with both lists.
Explicit values control each stream. When both lists are empty, the selected side stream uses `3840x2160@30`.
Other camera streams retain `640x480@30`. Large streams start on demand.
`USBCamera` expects `3840x2160` by default and rejects frames with a different size.
Changing capture dimensions also requires compatible reference images and an explicit expected resolution in the caller.
The supervised `H-21` checks must cover USB 2 bandwidth and microscope operation before lab use.

`USBCamera.get_frame_packet()` returns a frame ID, a monotonic receive time, and an image.
The receive time is not a sensor exposure timestamp.
Use `after_id` when a calculation requires a newer frame.
Capture errors stop acquisition and remain visible to the caller.

## Motor compatibility

Set `MG400_MICROSCOPE_SERIAL` to the board's `/dev/serial/by-id/` path.
`MotorController.connect()` explicitly opens the port at `115200` baud and asks `VERSION`.
The `VERSION microscope-axes 1.x.y` reply enables waits for focus completion replies.
Modern focus moves wait for `DONE FOCUS POS` after command acceptance.
Only the exact `ERR UNKNOWN_COMMAND` reply selects the legacy computed wait.
An unknown version or failed handshake refuses the connection.

`focus_move_async()` returns a move ticket so autofocus can sample images during motion.
`wait_focus_done()` waits for completion using that ticket.
`focus_move()` combines these operations.
`objective_move()` waits for command acceptance, while the calling algorithm retains its objective travel estimate.
Stop requests can interrupt a completion wait.
Unknown transport or completion outcomes latch a fault and require an explicit reconnect.
See the [firmware guide](../firmware/microscope-axes/README.en.md) for commands, wiring, and firmware limits.

## Supervised calibration and experiments

These commands require a person at an interactive terminal. Both input and output must be TTYs.
Do not run them from an agent, service contract, orchestrator, browser panel, or unattended script.
They do not stop other services automatically.
An operator must release conflicting microscope and camera owners before starting them.
The commands read `MG400_*` settings from the terminal environment.
Run them as the account that owns the calibration and run directories.

Set `MG400_MICROSCOPE_SERIAL` to the stable controller path.
Calibration and experiments select the main camera with `MG400_TOUPCAM_SERIAL` or `MG400_TOUPCAM_INDEX`, never both.
Their motion also requires the measured `H-06` limits, clearance, and home-switch configuration listed above.
Missing measurements refuse startup. Do not substitute demonstration values.

Calibration and experiments acquire the main camera, any required side camera, and exclusive serial access before motion.
Each required camera must supply a valid first frame.
Side capture uses `MG400_CAMERA_DEVICES`, `MG400_CAMERA_MODES`, and `MG400_MICROSCOPE_SIDE_STREAM` to select the physical device and mode.
The legacy `MG400_CAMERA_CAPTURE_SIZES` rules still apply.
The supervised adapter opens the stable device directly through `CAP_V4L2` and requires the `uvcvideo` driver.
It retains the same MMAP capture queue until cleanup. An occupied go2rtc capture queue refuses startup.
It never changes the format, reopens the device, or reconnects after capture starts.
This ownership excludes competing capture on that device. It does not exclude unrelated control-only ioctls.

### Reference and motion rules

These rules apply to calibration and experiments.
The supervised setup console has separate manual measurement rules below.

The measured limits and clearance use `MICRO 4` coordinates.
At active microstep `m`, the lower limit is `ceil(MIN * m / 4)` and the upper limit is `floor(MAX * m / 4)`.
This inward rounding must leave usable travel. Clearance and requested absolute positions must convert exactly.
The tools refuse fractional or out-of-range coordinates.

Without a home switch, the operator establishes zero at clearance. `MG400_MICROSCOPE_SWITCH_FOCUS` must be `0`.
With a configured switch, `FOCUS HOME` establishes the home origin. It never falls back to `FOCUS ZERO`.
The measured limits and clearance must use that same home origin.
Every focus move requires a current reference and measured limits.
Every turret move also requires stationary focus at clearance, except the bounded calibration moves described below.

The first motion requires explicit reference confirmation.
An actual microstep change or focus disable first moves focus to clearance with the current valid reference.
After that event, the tool stops both motors and requests another confirmation:

1. Look at the focus and objectives.
2. Check that focus is at clearance and the objectives are clear of the window.
3. Type `CLEAR` and press Enter within 60 seconds.

Each prompt discards earlier input and requires a new answer.
After confirmation, the tool uses `FOCUS ZERO` or `FOCUS HOME` and sends the converted limits.
Any other answer, end of input, timeout, reset, disconnect, or failed frame starts Stop/Disable cleanup for both motors.
The command then ends with a nonzero status. It cannot retry or restore a reference automatically.
Cleanup errors report failure. They do not prove that hardware stopped.
Cancellation and `SIGINT`, `SIGTERM`, or `SIGHUP` use the same cleanup rule.

### Calibration commands

Display options without opening devices:

```sh
uv run python -m microscope.calibrate --help
uv run python -m microscope.experiments --help
```

Run calibration commands only under the supervision rules above:

| Command | Operation and default output |
| --- | --- |
| `python -m microscope.calibrate reference` | Capture a raw PNG as `objective_reference.png`. It does not home, set zero, or move focus. |
| `python -m microscope.calibrate objective` | Run objective calibration and write `objective_calibration.csv`. |
| `python -m microscope.calibrate light` | Run light-objective calibration and write `light_objective_calibration.csv`. |

`--reference` selects the private calibration PNG for objective or light calibration. Its default is `objective_reference.png`.
`--output` selects a file beneath `MG400_MICROSCOPE_CALIBRATION_DIR`.
Calibration can replace an existing owned private output file. Select another output name to retain the previous result.
Reference capture requires terminal and device ownership checks, but does not require motion measurements or a reference confirmation.
It accepts camera dimensions in `[focus]` and still stops and disables motors during cleanup.

`--settings FILE` reads a TOML document.
For calibration, `[focus]` supplies focus settings, `[calibrator]` supplies algorithm options, and `[scan]` supplies scan options.
The tools refuse unknown sections, names, or invalid values.
Defaults preserve the original GUI settings. The accepted names and defaults are in [calibrate.py](../src/microscope/calibrate.py).
Camera dimensions default to `2592x1944`.

### Calibration at working focus

`MG400_MICROSCOPE_NUDGE_STEPS` sets the measured turret window in full steps.
Unset or `0` keeps the clearance behavior above. Values from `1` through `20` enable bounded moves at working focus.
Other values refuse startup. This mode applies only to objective and light calibration.

Use the value measured in `H-06` at the window-side focus limit, with every objective in its working position.
The window must preserve the physical margin in both directions for every objective.
Do not estimate this value from calibration data or use another microscope's measurement.

With a positive window, both commands require `--focus-start N` in measured `MICRO 4` coordinates, inside the focus limits:

```sh
python -m microscope.calibrate objective --focus-start N
python -m microscope.calibrate light --focus-start N
```

Replace `N` with the measured starting focus coordinate.
Before the first `CLEAR`, check focus at clearance and the objective in its working position.
The tool then moves focus to `N` and runs local autofocus once with the `[focus]` settings.

Each turret move requires a current reference, measured limits, and stationary focus.
The tool counts commanded turret displacement from the position at the first confirmation, in `1/16` full steps.
The allowed displacement is inclusive, from minus the configured window to plus that window.
Microstep changes and later `CLEAR` answers do not reset this origin.
A move beyond the window stops and disables both motors and ends the command before dispatch.

A microstep change still moves focus to clearance before the reference becomes invalid.
The prompt states the focus return before requesting a new `CLEAR` answer.
After zero or home and converted limits, focus returns to its earlier position at the new microstep.
This return rounds to the nearest pulse and must stay inside the limits.
In this mode only, a calibration request to disable focus uses `STOP FOCUS` and preserves the reference.
Experiments and full objective switches still turn the turret only at clearance.

### Supervised setup console

Use this console for the `H-06` clearance, travel-limit, and turret-window measurements:

```sh
python -m microscope.calibrate setup
```

Stop the microscope service first. Keep a person at the emergency stop who can see the focus and every objective.
The console requires interactive input and output plus exclusive serial access.
It needs no `H-06` values and opens no camera.
It accepts no `--settings`, `--output`, or `--focus-start` option. It also refuses a custom `--reference`.
It writes no settings file and never sends `FOCUS LIMIT`.

The console uses `MICRO 4`, with focus speed `200` pulses/second and turret speed `40` pulses/second.
The console completes each move before it accepts the next move key.
Press a single key without Enter. The `0` confirmation requires `CLEAR` followed by Enter.
Enter a fresh key within 60 seconds. Earlier queued input does not authorize another move.

| Key | Action |
| --- | --- |
| `a` / `z` | Focus `+1` / `-1` pulse. |
| `s` / `x` | Focus `+10` / `-10` pulses. |
| `d` / `c` | Focus `+100` / `-100` pulses. |
| `f` / `v` | Focus `+1000` / `-1000` pulses. |
| `j` / `l` | Turret `-1` / `+1` full step. |
| `u` / `o` | Turret `-10` / `+10` full steps. |
| `m` | Mark the current turret count as the display origin. |
| `0` | Request a fresh typed `CLEAR`, then send `FOCUS ZERO`. |
| `q` | Stop and disable both motors, then quit. |

Any key without an action stops both motors, including during a move.
During a move, `q` and end of input stop and quit.
The console discards motion keys, `m`, and `0` pressed during a move.
End of input, refusal, timeout, reset, or disconnect stops and disables both motors and ends the console.
The console prints the focus count and turret displacement from completed actions, during use and at the end.
An interrupted move can leave the current position unknown.
These counts do not measure physical position or prove clearance.
Observe each move and record the measured values separately. The console does not install them.

### Experiment commands

Use `python -m microscope.experiments focus|switch|combined` with the required settings.
Each command runs 1 pass. `--output` selects a new private directory beneath `MG400_ORCH_RUNS_DIR`.
Without it, outputs use `microscope/experiments/` with a unique command directory.
Settings, summary rows, focus curves, switch logs, and available final images stay in that directory.
Failures retain partial results and a terminal failure row.

Use `[focus]` and `[switch]` tables with `--settings FILE`.
`--ids` supplies distinct objective IDs in adjacent positive turret order.
The experiment treats that order as a bounded line, not a circular route.
`--focus-microstep` defaults to `1` and accepts `1`, `2`, `4`, `8`, or `16`.
Focus and combined trials require `--focus-start`, expressed in measured `MICRO 4` coordinates.
The tool moves to that explicit start before the focus reference trial and before each combined focus trial.

Focus trials retain offsets of `-2000`, `-1000`, `1000`, and `2000` active-mode pulses.
Their full scan half-range must be at least `2000`.
Switch trials test both adjacent directions with offsets of `0`, `-50`, `50`, `-100`, and `100` full steps.
The offset moves use `MICRO 1`. The ArUco switcher uses `MICRO 4`.
These changes require the supervised confirmations above.
The tools do not shrink scans to fit limits. An incompatible move aborts before dispatch.

### Import existing calibration data

The operator runs the import on Olares after reviewing the source data:

```sh
sudo mg400ctl calibration import microscope DIR
```

Replace `DIR` with the data directory from Objective-Control `3a2dad0`.
The importer selects `objective_views/`, `side_camera_images/`, `objective_reference.png`, and root-level CSV files.
It copies directory contents, refuses symbolic links and unsafe selected files, and lists each copied path.
It refuses existing destinations instead of overwriting them.
Destination directories use owner `1000:1000` and mode `0750`. Files use owner `1000:1000` and mode `0640`.
Completed copies remain if a later copy fails. Inspect them before another attempt.
The importer opens no devices. The operator must perform the Olares import and report its result on the migration issue.

## Other command-line entrypoints

Display the supported options without connecting devices:

```sh
uv run python -m microscope.autofocus --help
uv run python -m microscope.aruco_locator --help
```

Running autofocus without `--help` connects the serial controller and opens the Toupcam camera.
Its required `--half-range` is the allowed travel on each side of the start position, in pulses.
An operator must determine this value from the real hardware limits.
`--port` overrides the serial setting, and `--output` selects a file under the run root.

The locator processes a saved `--image` or a `--folder` of PNG images without motor access.
It requires the calibration reference images.
Its `--output` directory defaults to `microscope/scan_position_results` under the run root.
The other algorithms expose Python APIs and have no standalone command-line entrypoint.

## Offline checks

Run the microscope tests with simulated serial transport, generated images, and temporary data directories:

```sh
uv run pytest -q tests/test_microscope_*.py
```

These tests do not connect lab motors, open native cameras, or send commands to lab hardware.
Passing them does not establish physical travel limits, safe clearance, or real camera bandwidth.
