[中文](microscope.md)

# Microscope service and algorithms

The `microscope` package provides autofocus, objective switching, image calibration, and side-camera capture on Linux.
It runs without a desktop or Tk.
The contract service enforces focus-reference, travel-limit, and turret-clearance checks.
The browser panel is not yet available. Direct algorithm calls do not acquire the service lease or provide its interlocks.
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

Real startup requires reference-aware firmware and explicit focus settings.
Use the measurements from `H-06`; do not copy demo limits to hardware.
All requests retain the shared token, Host, Origin, and forwarded-header checks.
Without a token, only loopback Host values without forwarded headers are accepted.
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
Its returned `captured_at` is the frame's monotonic receive time, not sensor exposure time.

## Focus zero and control handoff

With a configured home switch, `home_focus` establishes zero and installs the configured firmware limits.
Without a switch, an operator must place focus at the verified clearance position before setting zero.
That position is `0`; the service does not move to an unknown clearance position automatically.

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
Camera preview's go2rtc process owns its V4L2 device.
`USBCamera` reads JPEG frames from `http://127.0.0.1:1984/api/frame.jpeg`.
It rejects redirects and ignores environment proxy settings.
Keep the cameras on their fixed ports and never reset the shared USB hub.

| Setting | Configuration |
| --- | --- |
| `MG400_CAMERA_DEVICES` | Stable device paths in the order used by the camera preview installer. |
| `MG400_MICROSCOPE_SIDE_STREAM` | The configured go2rtc stream for the side camera. There is no default stream. |
| `MG400_CAMERA_CAPTURE_SIZES` | Optional comma-separated `WIDTHxHEIGHT` values, with 1 value for each configured device. |

Map the side-camera device path to its stream before enabling capture.
When the size list is empty, the selected side stream uses `3840x2160`.
Other camera streams retain `640x480`.
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

## Command-line entrypoints

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
