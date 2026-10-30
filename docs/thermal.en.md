[中文](thermal.md)

# Thermal Master P1 camera

The thermal package provides a Python controller, acquisition service, and browser dashboard for the Thermal Master P1, USB `3474:45C2`.
The native temperature array is 160 × 120 pixels.
FastAPI and uvicorn serve the dashboard without a JavaScript build step or an external CDN.
The interactive API reference uses Swagger assets from a CDN.

## Linux setup

Use Python 3.12 or later and [uv](https://docs.astral.sh/uv/getting-started/installation/).
From the repository root, install the locked dependencies:

```sh
uv sync --locked
```

The default `thermal` dependency group includes FastAPI, uvicorn, NumPy, PyUSB, and the bundled libusb runtime.
The `dev` group includes httpx for offline API tests.
The bundled runtime does not grant USB access.

An administrator must grant the camera user group access to USB vendor `3474` and product `45c2`.
Use a udev rule for that device with mode `0660` and the intended local group.
Reload the rules and reconnect the camera after changing permissions.
Close other camera applications before connecting.
Only 1 process can own the camera at a time.

The following commands access real USB hardware.
An operator must run them with the dashboard and other camera owners stopped:

```sh
uv run thermal-cam --diagnose
uv run thermal-cam --probe --frames 30 --output captures/probe.npz
```

The diagnostic reports backend and device access without initializing the camera or starting acquisition.
The probe opens the camera, captures the requested frames, reports the result, and closes the camera.
The optional output saves the final frame as a lossless NPZ file.

## Start the dashboard

For synthetic data without a camera:

```sh
uv run thermal-cam --demo
```

Open `http://127.0.0.1:8768`.
Demo mode is visibly marked as simulated.
A failed camera connection never switches to demo data.

For a real camera, the default command starts acquisition automatically:

```sh
uv run thermal-cam
```

Startup reserves the HTTP port before creating the camera application.
If the port is occupied, startup exits without opening the camera.
Keep the terminal open while using the dashboard.
Closing a browser tab does not stop the server.
Use Ctrl+C in the original terminal to stop the server and release the camera.

Other startup options:

```sh
uv run thermal-cam --no-connect
uv run thermal-cam --demo --bind 127.0.0.1 --port 8768
uv run thermal-cam --help
```

With `--no-connect`, the server waits for a Connect request from the dashboard or API.
Use `python -m thermal_cam` as an alternative entrypoint.
Run 1 server process and 1 worker.
Do not use automatic development reload while acquiring camera data.

### Settings and access

| Setting | Default | Purpose |
| --- | --- | --- |
| `MG400_THERMAL_BIND` | `127.0.0.1` | Listen address. `--bind` overrides it. |
| `MG400_THERMAL_PORT` | `8768` | HTTP port. `--port` overrides it. |
| `MG400_THERMAL_TOKEN` | Empty | Bearer token for camera API access. |

A non-loopback bind requires a token.
A configured token must have at least 24 printable ASCII characters, without leading or trailing spaces, including on loopback.
Invalid settings exit with status 2 before opening a listener or camera.
Without a token, requests require a loopback Host and no forwarded headers.
Browser requests must retain the same origin.

When a token is configured, enter it in the dashboard's Access token field.
The page holds it only in memory and sends it in the Authorization header.
It does not save the token in browser storage or URLs.
The static dashboard remains accessible, but camera routes require the token.
The `/docs` and `/openapi.json` routes also require it.

## Use the dashboard

| Control or display | Behavior |
| --- | --- |
| Live view | Colors and min/max/center markers use the same temperature frame. |
| Image rendering | Smooth mode displays 240 × 320 pixels. Native mode displays 120 × 160 pixels. Both rotate the view 90° counterclockwise. |
| Measurements | Maximum, minimum, center, mean, range, and population standard deviation use the full native temperature matrix. |
| Pixel inspection | Point at or select a pixel. Arrow keys move the selected pixel in the displayed direction. |
| Palette and scale | Change colors or automatic/fixed limits without changing measured temperatures. |
| Units | Display Celsius, Fahrenheit, or Kelvin. API and CSV temperatures remain Celsius. |
| Freeze | Keep the displayed frame for inspection and export. Acquisition continues. |
| Exports | Save the displayed frame as CSV, JSON, or PNG. The API also provides NPZ arrays. |
| Connect / Disconnect | Acquire or release the camera without restarting the server. |
| Calibrate shutter / NUC | Request sensor nonuniformity correction. |
| Gain | Only high gain is supported. Low gain is disabled because its P1 temperature conversion is not validated. |

Coordinates use the original sensor orientation: `x` is the column and `y` is the row.
Sensor `(0, 0)` appears at the bottom-left of the rotated view.
The center is the single pixel at `x=80, y=60`, not an average of the 4 central pixels.
A frozen frame or an image retained after disconnection is not a current measurement.
The dashboard displays connection errors and stale data.

## Resolution and temperature meaning

The P1 provides its full native 160 × 120 stream at approximately 25 frames per second.
The [manufacturer's 320 × 240 Super IR image](https://thermalmaster.com/products/thermal-master-p1) derives from that sensor.
The [upstream USB protocol](https://github.com/jvdillon/p3-ir-camera/blob/e3205dca5727682ff2d903585d1dce5a1d19f1f6/P3_PROTOCOL.md#supported-devices) exposes no higher-resolution measurement mode.

The dashboard uses standard image interpolation, not the manufacturer's X³IR processing.
Its canvas ranges from 720 × 960 to 2880 × 3840 pixels according to available space and display density.
These display pixels add no temperature samples.
The native aspect ratio is 4:3, and the rotated view is 3:4.
Normal and full-screen views retain the whole image without stretching or cropping.

PNG exports include the rotated canvas and a footer.
CSV, JSON, and NPZ retain 120 rows × 160 columns of native measurements.
JSON records the display mode and rotation separately.

The upstream protocol represents each thermal pixel as an unsigned 16-bit value in 1/64 kelvin units:

```python
temperature_c = raw.astype("float32") / 64.0 - 273.15
```

The separate 8-bit infrared brightness image is frame data, not a temperature measurement.
Readings are the camera's apparent temperatures.
The application adds no emissivity, reflected-temperature, distance, or atmospheric correction.
Surface emissivity, reflections, camera state, and measurement conditions affect readings.
Compare with a suitable temperature reference before relying on absolute measurements.

Let the connected camera warm for approximately 5 minutes before interpreting measurements.
Then use Calibrate shutter / NUC.
The [upstream troubleshooting guide](https://github.com/jvdillon/p3-ir-camera/blob/e3205dca5727682ff2d903585d1dce5a1d19f1f6/P3_PROTOCOL.md#troubleshooting) recommends warmup and shutter correction for drift.
Shutter correction improves sensor uniformity and operating stability.
It does not establish absolute temperature accuracy or replace a known temperature reference.

## Python integration

The direct adapter owns blocking USB operations.
Use it from 1 worker thread, and close it when finished:

```python
import numpy as np
from thermal_cam.camera import P1Camera

camera = P1Camera()
try:
    device = camera.open()
    ir, raw = camera.read()
    temperatures_c = raw.astype(np.float32) / 64.0 - 273.15
    print(device, temperatures_c[60, 80])
finally:
    camera.close()
```

`read()` returns `(ir, raw)` arrays shaped `(120, 160)`, with `uint8` and `uint16` types respectively.
`open()` returns device information.
The adapter also provides `shutter()`, `set_gain("high")`, and `stats()`.
Low gain requests raise an error.
Do not share an adapter between concurrent readers or use it while the dashboard owns the camera.

The application factory opens no camera on import.
Its lifespan starts and stops acquisition:

```python
import os
import uvicorn
from thermal_cam.app import create_app

app = create_app(auto_connect=True, token=os.environ.get("MG400_THERMAL_TOKEN"))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8768, workers=1)
```

Use `create_app(demo=True)` for synthetic data or `create_app(auto_connect=False)` to wait for a connection request.
Bind and port environment settings belong to the CLI. An embedded server supplies its own settings.
For a mounted ASGI application, the parent must manage the acquisition lifespan.
[FastAPI does not run mounted application lifespan hooks automatically](https://fastapi.tiangolo.com/advanced/events/#sub-applications).

Read a saved frame without opening the camera:

```python
import json
import numpy as np

with np.load("captures/probe.npz", allow_pickle=False) as frame:
    raw = frame["raw"]
    ir = frame["ir"]
    temperatures = frame["temperatures_c"]
    metadata = json.loads(frame["metadata"].item())
    print(metadata["timestamp"], temperatures[60, 80])
```

## HTTP API

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/api/status` | State, source, error, device details, frame count, acquisition rate, and frame age. |
| `GET` | `/api/frame` | 1 coherent frame with sequence, timestamp, dimensions, statistics, Celsius, raw, and brightness matrices. |
| `GET` | `/api/snapshot.npz` | Numerical arrays and metadata from 1 frame. |
| `POST` | `/api/connect` | Request connection and start acquisition. |
| `POST` | `/api/disconnect` | Stop acquisition and release the device. |
| `POST` | `/api/shutter` | Request shutter correction. |
| `POST` | `/api/gain` | Set high gain with `{"gain":"high"}`. Low gain is rejected. |

For a token-protected server, send the configured token in a bearer header:

```sh
curl --fail -H "Authorization: Bearer $MG400_THERMAL_TOKEN" \
  http://127.0.0.1:8768/api/status
```

Without a token, omit that header.
Temperature arrays use `temperatures_c[y][x]`.
Each response is internally consistent, but separate frame and snapshot requests can refer to different frames.
Use the sequence and timestamp to associate measurements.
Unavailable or stale frames return HTTP 503 instead of fabricated readings.
The acquisition worker owns USB reads and commands. HTTP viewers read snapshots.

## Automation contract and regions

The read-only [instrument contract](adr/0001-instrument-service-contract.en.md) uses the base URL `http://127.0.0.1:8768`.
It adds these routes beside the dashboard API:

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/api/describe` | Contract version, demo flag, region definitions, and the maximum frame age. `commands` is empty. |
| `GET` | `/api/state` | Contract fields plus camera status, `frame_sequence`, and `frame_age_s`. |
| `GET` | `/api/events?since=0` | Activity after the given sequence, with a `next` cursor. The same events enter the service journal. |
| `POST` | `/api/heartbeat` | Acquire or renew the informational lease. |
| `POST` | `/api/release` | Release the caller's lease. |
| `POST` | `/api/stop` | Return `stop: "confirmed"`. This no-op leaves acquisition running. |
| `POST` | `/api/command` | Return HTTP 400 because the contract has no commands. |
| `GET` | `/api/roi` | Read a named region or an explicit sensor rectangle. |

The lease lasts 3 seconds. Reads and stop do not require ownership.
Lease expiry and release leave acquisition running.
The lease does not restrict the existing dashboard controls.
Use `/api/disconnect` to stop acquisition and release the camera.
Contract state reports `busy: false`, `locked: false`, and `last_command: null` because this contract controls no output.
Inspect `connected`, `fault`, and frame age before using readings.

These routes retain the token, Host, Origin, and forwarded-header checks described above.
Contract POST requests require `Content-Type: application/json` and exactly 1 `X-MG400-Client` header.
The client ID must contain 1–128 printable ASCII characters.
Send a JSON object of at most 4096 bytes. `/api/stop` accepts `{"wait_s":0}` with a finite value from 0–15.
It returns immediately because there is no output to stop.
Contract errors use `{"error":"..."}`: HTTP 400 for invalid input, 401 for token failure, 403 for access denial, and 415 for non-JSON input.

### Named sensor regions

Store named regions in `/var/lib/mg400/thermal/rois.toml`:

```toml
[regions.crucible]
x0 = 60
y0 = 40
x1 = 100
y1 = 80
```

These example bounds do not identify the crucible in a particular camera view.
Set the bounds from the native sensor image.
Names contain 1–64 ASCII letters, digits, `_`, or `-`.
Each region defines exactly `x0`, `y0`, `x1`, and `y1`, as integers.
The bounds must satisfy `0 <= x0 < x1 <= 160` and `0 <= y0 < y1 <= 120`.
The service rejects empty, reversed, or out-of-range rectangles.
It does not clip bounds or rotate coordinates.

Here, x is the sensor column and y is the sensor row.
The rectangle is `temperatures_c[y0:y1, x0:x1]`: lower bounds are included and upper bounds are excluded.
The rotated display does not change these coordinates.
For example, `x0=159, y0=119, x1=160, y1=120` selects the last sensor pixel.

The service reads this file once during construction, before camera startup.
Restart the service after editing it.
A missing file leaves the named-region map empty. Explicit coordinate queries still work.
An invalid file prevents startup. The service never substitutes example regions.
An embedded application can select another path with `CameraService(roi_path=...)`.

### Region readings

Use exactly 1 selector: `name`, or all of `x0`, `y0`, `x1`, and `y1`.
Do not combine them or repeat query parameters.
For example:

```sh
curl --fail -H "Authorization: Bearer $MG400_THERMAL_TOKEN" \
  'http://127.0.0.1:8768/api/roi?name=crucible'
curl --fail -H "Authorization: Bearer $MG400_THERMAL_TOKEN" \
  'http://127.0.0.1:8768/api/roi?x0=60&y0=40&x1=100&y1=80'
```

Without a token, omit the bearer header.
The response contains `min_c`, `mean_c`, and `max_c` in °C, plus the bounds and `name` (`null` for explicit coordinates).
All statistics, `frame_sequence`, `timestamp`, `source`, `demo`, and `frame_age_s` refer to 1 coherent frame.
Frame age uses a monotonic clock and is measured in seconds.
Frames older than 3 seconds return HTTP 503, with no temperature values.
The same applies when the camera is disconnected, has an error, or has no frame during shutter or gain settling.
Invalid or unknown regions return HTTP 400 before frame access.

These readings have the same apparent-temperature limits as the full frame.
Let the camera warm for approximately 5 minutes, then apply shutter correction before interpreting measurements.
After shutter correction, wait for a new frame. Do not use a retained display image as a current reading.
Shutter correction does not replace comparison with a suitable temperature reference.

## Tests and troubleshooting

Run the offline tests:

```sh
uv run pytest -q tests/test_thermal_*.py
```

Tests use synthetic arrays, mocked USB access, ASGI clients, and fake listeners.
They do not certify a camera or absolute temperature accuracy.

| Symptom | Action |
| --- | --- |
| No camera found | Inspect the USB data cable and power. Identify the device by USB ID `3474:45C2`. |
| Access denied or device busy | Inspect Linux USB group permissions. Close other camera owners before connecting. |
| Repeated timeout or stream error | Disconnect in the dashboard. Reconnect the USB cable, then connect again. Inspect the cable and port. |
| Temperatures stop changing | Inspect errors and frame age. Resume a frozen view if applicable. |
| Implausible temperatures | Inspect the target's pixel coverage and emissivity/reflections. Compare with a known reference. A palette change does not calibrate temperature. |
| Occupied HTTP port | Open the existing dashboard, or stop its original process before restarting. Changing the port does not permit another camera owner. |
| HTTP 401 | Enter the configured token or supply its bearer header. |
| HTTP 400 or 403 | Inspect Host, Origin, and forwarded headers. A tokenless server permits only loopback access. |

## Attribution

The package comes from Chemistry-Integration's thermal controller.
The unchanged vendored driver comes from [jvdillon/p3-ir-camera](https://github.com/jvdillon/p3-ir-camera) at `e3205dca5727682ff2d903585d1dce5a1d19f1f6`.
The protocol comes from community reverse engineering. This application is independent of Thermal Master.
Keep the [Apache license](../src/thermal_cam/_vendor/LICENSE-p3-ir-camera) and [third-party notice](../src/thermal_cam/_vendor/NOTICE.md) with the package.
