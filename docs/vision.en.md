[中文](vision.md)

# Vision service

The vision service detects chips, the robotic arm, and QR or Micro QR labels in camera frames.
go2rtc owns the cameras. The vision service reads its JPEG endpoint and does not open a camera device.
Model loading and inference start only when the service starts or receives a request.

## Runtime and startup

The live service requires the `vision` and `vision-gpu` dependency groups and verified local model weights.
Use the runtime described in [Vision runtime and GPU use](adr/0006-vision-runtime.en.md).
The service never downloads a model during prediction.
Deployment remains a separate procedure in [Host deployment](deploy.en.md).

For an offline demo from the repository root:

```sh
uv sync
uv run python -m vision --demo
```

Demo mode reads `tests/fixtures/vision/scene.ppm` and `tests/fixtures/vision/detections.json`.
All 3 demo camera numbers use the same image and detections.
The demo uses the real QR reader and a fake localizer. It does not import torch or access cameras.
Demo results do not validate the live model or camera geometry.

Startup loads the model and performs 1 warm-up prediction on a synthetic image.
It does not request a camera frame for warm-up.
GPU memory configuration runs before model loading.
The service then starts a lease poller, which checks expiry even without HTTP requests.
Importing modules or constructing a service starts no threads, loads no model, and reads no frames.
Failed or cancelled startup closes the service instance. Create a new instance before another startup attempt.

## Settings

Command-line options override environment settings. Empty environment values use their defaults.

| Setting | Option | Default and meaning |
| --- | --- | --- |
| `MG400_VISION_BIND` | `--bind` | `127.0.0.1` |
| `MG400_VISION_PORT` | `--port` | `8771` |
| `MG400_VISION_BASE_PATH` | `--base-path` | `/`. Prefix for all routes |
| `MG400_VISION_TOKEN` | None | Empty on loopback. Otherwise at least 24 printable ASCII characters |
| `MG400_VISION_WEIGHTS` | `--weights` | `/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt` |
| `MG400_VISION_DEVICE` | `--device` | `cuda` when available, otherwise `cpu`. Explicit `cuda:N` selects a device |
| `MG400_VISION_GPU_FRACTION` | `--gpu-fraction` | `0.15`. Finite value greater than 0 and at most 1 |
| `MG400_VISION_FRAME_TIMEOUT` | `--frame-timeout` | `2` seconds for each HTTP socket operation and the body-read deadline. Finite and greater than 0 |
| None | `--demo` | Use offline fixtures without the detector runtime |

A configured token requires at least 24 characters even on loopback.
Without a token, requests require a loopback `Host` and must not contain forwarded headers.
With a token, send `Authorization: Bearer <token>`.
The service also checks `Origin` and rejects cross-site requests.
These rules apply to image and result routes as well as contract routes.
Configuration and startup failures exit with status 2.

For CUDA, the service calls `torch.cuda.set_per_process_memory_fraction` before loading the localizer.
The fraction limits PyTorch allocations for this process. Other CUDA allocations can use additional memory.
An explicitly requested but unavailable CUDA device fails startup.
The service does not silently change an explicit CUDA request to CPU.

## Frame sources and coordinates

Camera numbers are integers from 1 to 3.
The live source uses this fixed endpoint, replacing `N` with the camera number:

```text
http://127.0.0.1:1984/api/frame.jpeg?src=cameraN
```

The source bypasses environment proxies and refuses redirects.
It checks elapsed body-read time before and after each read.
A socket read already in progress can continue until its socket timeout before the service checks the body deadline again.
Connection, response-header, and HTTP transfer-framing reads use the socket timeout, without a separate total deadline.
The source accepts frames of at most 8 MiB and 16,000,000 pixels.
The decoder checks JPEG data, applies EXIF orientation, and produces RGB pixels.
Source failures return 503 and set a fault. Requests do not retry automatically.

Results report `coordinate_system` as `oriented_original_image_pixels`.
Detection fields such as `xyxy`, `center_xy`, `polygon`, and `size_px` refer to that image.
`angle_deg` follows the localizer's OpenCV rectangle convention.
QR results contain `corners_px`, `center_px`, and `bounds_px` in full-frame coordinates.
An unreadable label has `decoded=false`. Its text is not a verified identity.

The service currently reports `calibrated=false` and an empty `calibrated_cameras` list.
It does not return `robot_mm` or invent camera-to-robot transforms.
CAL-01 adds calibration loading, image-size validation, and robot millimetres.
Robot motion targets require validated calibration on the applicable plane.

## Routes and commands

Routes are relative to `--base-path` and use the [service contract](adr/0001-instrument-service-contract.en.md).
Only 1 inference runs at a time, including detection and QR reading.
An overlapping request returns 409. The service does not queue extra inference requests.

| Read route | Result |
| --- | --- |
| `GET api/detect?camera=N` | Fresh frame detection as JSON |
| `GET api/labels?camera=N` | Fresh full-frame QR and Micro QR results as JSON |
| `GET api/annotated.jpg?camera=N` | Fresh detection rendered as JPEG |
| `GET api/state` | Model, device, connection, busy state, fault, lease, and latest result summary |
| `GET api/describe` | Contract version and command parameter descriptions |
| `GET api/events?since=0` | Service events, also written to the journal |

Read routes need no lease. They retain all access checks.
Supply `camera` exactly once and no other query parameter on the 3 inference routes.
Responses use `Cache-Control: no-store`.

Commands require `Content-Type: application/json`, `X-MG400-Client`, and a control lease acquired through `POST api/heartbeat`.
The lease lasts 3 seconds. Renew it while a command runs.
Send commands to `POST api/command`:

```json
{"action":"detect","camera":1}
```

```json
{"action":"read_labels","camera":1,"region":[10,20,80,60]}
```

`region` is optional. It contains integer pixel bounds `[x0,y0,x1,y1]`, with exclusive right and bottom edges.
Bounds must increase, be nonnegative, and fit inside the frame.
Returned label coordinates include the crop offset and refer to the full image.

A command returns 202 and `command_id` before completion.
Read `state.last_command` for `result`, `message`, and `finished_at`.
A successful command includes full JSON metadata in `last_command.output`.
Metadata must contain finite JSON values and fit within 256 KiB when encoded as compact ASCII JSON.
Invalid or oversized metadata fails with 503 and is not published.
Failed or stopped commands have `output=null`.
`last_result` retains a small summary of the latest successful inference, including its completion time.
A live service reports `connected=false` until its first successful frame operation.

## Stop and shutdown

Any authenticated client can call `POST api/stop` without holding the lease.
Include `X-MG400-Client` and a JSON body, such as `{"wait_s":0}`.
`wait_s` accepts values from 0 to 15 seconds.
The response is `confirmed` after work finishes or `pending` while it still runs.
Repeated stop requests are safe.

Stop requests cancel the active operation and discard its late results.
During startup, stop cancels model publication and reports `pending` until loading or warm-up finishes.
QR processing checks cancellation between stages.
An active model prediction cannot be forcibly interrupted. It must return before stop becomes confirmed.
Lease expiry cancels the controlling client's command. It does not cancel an independent read request.

SIGINT, SIGTERM, and SIGHUP request cancellation during shutdown.
Shutdown waits up to 15 seconds for active inference or startup and ends lease polling.
The service controls no robot, stage, gripper, or camera device.
