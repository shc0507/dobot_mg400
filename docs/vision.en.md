[中文](vision.md)

# Vision service

YOLO detects unlabeled chips and the robotic arm in camera frames.
Chip identity comes from its slot and run record. QR and Micro QR readers remain available for labels on the wafer side.
Camera calibration uses ArUco markers on the arm and tray, independently of chip detection.
go2rtc owns the cameras. The vision service reads its JPEG endpoint and does not open a camera device.
Model loading and inference start only when the service starts or receives a request.

## Runtime and startup

The live service requires the `vision` and `vision-gpu` dependency groups and verified local model weights.
Use the runtime described in [Vision runtime and GPU use](adr/0006-vision-runtime.en.md).
The service never downloads a model during prediction.
Before it loads Ultralytics, the service sets `YOLO_OFFLINE=1`. Ultralytics then sends no usage events or error reports and runs no package update checks, whatever its `settings.json` says.
Deployment remains a separate procedure in [Host deployment](deploy.en.md).

For an offline demo from the repository root:

```sh
uv sync
uv run python -m vision --demo
```

Demo mode reads `tests/fixtures/vision/scene.ppm` and `tests/fixtures/vision/detections.json`.
All 4 demo camera numbers use the same image and detections.
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
| `MG400_VISION_CALIBRATION_DIR` | None | `/var/lib/mg400/calibration/cameras/`. Private camera-model directory |
| `MG400_VISION_TAG_HEIGHT_MM` | None | `100`. Nominal arm-tag height above the flange, in millimetres |
| `MG400_VISION_TAG_TIP_MM` | None | `246`. Nominal vertical distance from the tag to the tweezer tip, in millimetres |
| `MG400_VISION_CALIBRATION_RMS_MM` | None | `1`. Finite acceptance limit greater than 0 and at most 1 mm |
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

Live camera numbers are integers from 1 through the configured device count, at most 4.
The service uses the same `MG400_CAMERA_DEVICES`, `MG400_CAMERA_MODES`, and `MG400_CAMERA_CAPTURE_SIZES` settings as the installer and preview.
See [Cameras](olares.en.md#cameras) for allowed modes and defaults. Empty device settings disable live frame commands.
The command schema lists only configured camera numbers. The service rejects unconfigured numbers before requesting a frame.
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
The resulting dimensions must equal the configured capture mode. A mismatch fails without resizing.
`vision.frames.still(camera)` and service reads request 1 frame from this same go2rtc stream.
They do not change capture mode or start a separate device capture.
Source failures return 503 and set a fault. Requests do not retry automatically.

Results report `coordinate_system` as `oriented_original_image_pixels`.
Detection fields such as `xyxy`, `center_xy`, `polygon`, and `size_px` refer to that image.
`angle_deg` follows the localizer's OpenCV rectangle convention.
QR results contain `corners_px`, `center_px`, and `bounds_px` in full-frame coordinates.
An unreadable label has `decoded=false`. Its text is not a verified identity.

For a valid camera model, detections include `robot_mm` with `x` and `y` at the detection center.
The service undistorts the pixel and intersects its ray with the stored horizontal tray plane.
`calibrated_cameras` identifies cameras with valid loaded models. Missing calibration produces `calibrated=false` and no `robot_mm`.
A camera-port or image-size change invalidates the calibration. The service does not reuse another port's file or resize a model.
An image-size mismatch fails the operation without publishing robot coordinates.
Robot motion targets still require a validated calibration on the applicable plane and the normal motion guards.

## Routes and commands

Routes are relative to `--base-path` and use the [service contract](adr/0001-instrument-service-contract.en.md).
Only 1 operation runs at a time, including detection, QR reading, marker reading, calibration, and drift checks.
An overlapping request returns 409. The service does not queue extra operations.

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

Calibration uses 3 leased commands:

| Command | Parameters and result |
| --- | --- |
| `read_markers` | `camera`. Returns `markers` with `id` and `center_px`, plus `image_size`, `camera_device`, `frame_sha256`, and `read_at`. |
| `calibrate_camera` | `camera`, `tag_points`, `tag_pixels`, `tray_points`, `tray_pixels`, and `image_size`. Fits and stores the model through the owned lease. |
| `check_calibration` | `camera`, `flange_mm`, `tool_offset_mm`, and optional `margin_px`. Returns the tip/arm check from the same frame. |

`tag_points` contains 24 uncorrected flange XYZ rows. `tag_pixels` contains their 24 pixel XY rows.
The fitter applies the nominal tag height to these grid points.
`tray_points` contains 2 to 4 rows with transformed fingertip XY and original contact flange Z.
The skill rotates the configured tool XY offset by each recorded R, then adds it to the reference flange XY.
It preserves flange Z for the unknown-distance fit, without applying the tool Z offset.
`tray_pixels` contains the matching pixel XY rows.
`image_size` is `[width, height]`. The request body must fit within 16 KiB.
Use the guarded calibration skill below to collect these arrays. Do not convert historical contact data into robot move commands.

A command returns 202 and `command_id` before completion.
Read `state.last_command` for `result`, `message`, and `finished_at`.
A successful command includes full JSON metadata in `last_command.output`.
Metadata must contain finite JSON values and fit within 256 KiB when encoded as compact ASCII JSON.
Invalid or oversized metadata fails with 503 and is not published.
Failed or stopped commands have `output=null`.
`last_result` retains a small summary of the latest successful inference, including its completion time.
A live service reports `connected=false` until its first successful frame operation.

## Camera-to-robot calibration

Recalibrate after moving a camera, changing its USB port, changing capture dimensions, or changing the arm-tag or tray geometry.
An operator must prepare trusted references and taught stations before calibration.
Review the change and complete the applicable hardware gates before deployment or a real scan.
Software checks and rehearsal do not validate physical clearance or camera geometry.

### Prepare references and safe entry

The arm tag uses `DICT_4X4_50` ID `4` on the J4 axis.
Tray references use distinct IDs from `0`, `1`, `2`, and `7` in the same dictionary.
Configure 2 to 4 measured tray references in a private TOML file.
The orchestrator reads `MG400_CAMERA_CALIBRATION_REFERENCES`, which defaults to `/var/lib/mg400/calibration/cameras/references.toml`.
The file must be trusted, without links, and at most 4 KiB.

This is a template, not measured coordinates. Replace `X`, `Y`, `Z`, and `R` with operator-recorded flange values:

```text
[[references]]
id = 1
flange_mm = [X, Y, Z, R]
```

XYZ values use millimetres. R uses degrees.
The reference contacts from `H-08` remain calibration data, even when a contact lies outside the current automatic motion limits.
The routine never moves to those contact poses.

Teach 2 grid-corner stations with distinct X and Y, the same R, and the same explicit zone.
Configure that zone with owner `robot`.
Stations describe the fingertip. The skill converts them to flange poses through the existing tool definition.
Choose 2 reachable flange heights at least 40 mm apart.
Existing workspace, joint, path, feedback, and control checks remain in force.

Before the scan, reach the first corner at the lower flange height through an approved operator or taught route.
The robot must already be enabled, idle, freshly observed, and owned by the run at that pose.
The calibration skill does not enable the robot, enter the zone from another pose, or recover a stopped run automatically.

The scan requires the vision service to report `connected=true`. A live service initially reports `connected=false`.
Before the scan, use an authorized preview lease to run `read_markers` for the selected camera.
Wait for its successful command receipt, then release the preview lease.
The scan refuses unknown vision health. It does not silently issue a preview request or move to establish health.

### Run the guarded scan

The typed recipe skill takes scalar parameters:

```text
vision.calibrate(camera: int, corner_a: str, corner_b: str,
                 low_flange_z_mm: float, high_flange_z_mm: float)
```

`camera` selects a configured camera. `corner_a` and `corner_b` name the taught stations.
The routine samples a 3-row by 4-column grid at both heights, for 24 tag points.
At each point, a stationarity guard remains active while the service reads 5 sequential ArUco frames.
The routine uses the median marker center. Missing or ambiguous required markers abort the scan.

Each sample must have a different RGB SHA256 from the other samples at that point.
This rejects identical image content. It does not prove sensor freshness.
go2rtc supplies neither a capture timestamp nor a sensor frame ID. `read_at` is the service read time.
Camera path and image size must remain unchanged throughout acquisition.

The skill reads the visible tray markers after the grid, while the robot remains stationary.
It submits the collected points in 1 request and accepts only the exact owned command receipt.
Loss of control, cancellation, or an unknown outcome causes no retry or authority reacquisition.
The run stores the accepted calibration as an artifact.

The routine ends inside the calibration zone. Completion does not prove departure.
Zone occupancy remains until a later explicit departure or supervised recovery establishes clearance.

### Offline calibration rehearsal

The public rehearsal CLI and demo registry use owned synthetic robot and vision services from the same rehearsal pool.
The synthetic camera follows that robot and renders ArUco images for camera `1` at `1920x1080`.
They generate their own synthetic reference data and do not read the host `MG400_CAMERA_CALIBRATION_REFERENCES` path or camera-model files.
Rehearsal opens no camera device or native capture.

Use the synthetic tool offset `[0, 0, 146]` and prepare the grid in flange coordinates:

| Parameter | Synthetic value |
| --- | --- |
| X range | 200 to 260 mm |
| Y range | 0 to 40 mm |
| Lower and upper Z | 50 and 90 mm |
| R | 0 degrees |

Define the corresponding fingertip corner stations with this tool offset and the same explicit `robot`-owned zone.
Earlier recipe steps must enable the synthetic robot and reach the first lower corner before the scan.
The calibration skill does not silently enable the robot or acquire extra authority.
A successful rehearsal validates the software flow, not hardware reachability, measured camera geometry, or physical clearance.

### Fit, acceptance, and limits

The fitter starts with normalized direct linear transform (DLT) and RANSAC at a 2-pixel threshold.
At least 12 of the 18 training tag points must be inliers.
It uses `cv2.calibrateCamera` with `CALIB_USE_INTRINSIC_GUESS` and radial `k1` and `k2` only.
The remaining 6 tag points never participate in fitting.

The joint fit also uses the tray references to recover `D`, the vertical tag-to-tip distance.
Its default nominal value is 246 mm, with a fixed permitted difference of 10 mm.
The nominal tag height above the flange defaults to 100 mm.
The fit reports `distance_mm` and separate `rms_mm.held_out` and `rms_mm.tray` values.

RMS is the root mean square of horizontal position errors.
Held-out tag RMS measures unused tag points. Tray RMS is a fitted-reference residual, not an independent holdout result.
Each error uses that point's own height. Both RMS values must meet the configured limit, at most 1 mm.
The mapping plane uses the mean recovered tray height in model coordinates.
`tray_height_mm` depends on the nominal tag height and is not an independently measured physical robot Z.
Never use it as a robot motion height. The service returns XY only.
A tilted tray remains a limitation: this model uses a horizontal plane and does not model tilt.
Passing RMS checks does not remove that limitation or establish a safe motion path.

### Stored models and invalidation

The service stores bounded JSON under `MG400_VISION_CALIBRATION_DIR`.
The filename is SHA256 of the full `/dev/v4l/by-path/` device path, followed by `.json`.
The document also stores that full path and its image size.
It retains the camera matrix, distortion, rotation, translation, tray height, recovered distance, sample points, split indices, errors, and creation time.
Model points store transformed tray XY with original flange Z under `tray_tip_xy_flange_z_mm`.
The run artifact stores the original reference flange poses, marker IDs, and configured tool offset under `reference_provenance`.
The calibration JSON does not contain this original-reference provenance.

The directory must exist, belong to the service account, and use mode `0700` or `0750`.
Model files must be private regular files with mode `0600` or `0640`, without symbolic or multiple hard links.
New model files use `0600` and an atomic replacement after the final authority check. Each document is at most 64 KiB.
Stop, lease release, or lease expiry rejects later publication.

A new USB-port path selects a different calibration key. Changing image size also prevents coordinate use.
Do not rename an old file to bypass these checks. Recalibrate with the intended camera and capture mode.
The service cannot detect every physical camera movement from its port name alone.
Keep the camera fixed and revalidate after physical changes.

## Calibration drift check

Use `vision.check_calibration(camera)` before each vision-guided move in a transfer recipe.
This typed skill requires the robot and vision services under the run's existing ownership.
It reads the measured robot pose and configured tool offset. It does not move or enable the robot.
The check uses the existing workspace, gripper, feedback, and ownership requirements.

The vision service must already report `connected=true` with no fault.
For a cold live service, complete the authorized `read_markers` preview and release its lease as described above.
The drift check does not acquire a separate preview lease or automatically restore service health.

### Margin and command data

`MG400_ORCH_DRIFT_MARGIN_PX` sets the orchestrator's required pixel inset. It defaults to `0` and accepts finite values from `0` through `4096`.
A larger margin shrinks the acceptable area inside the arm box. It never expands that box.
An inset that leaves no usable box fails the check.
Live runs and owned demos preserve this supplied scalar setting. An absent setting stays absent until the skill applies its default.
Other device, endpoint, token, and path filters remain unchanged.

The skill sends the leased `check_calibration` command with these fields:

| Field | Meaning |
| --- | --- |
| `camera` | Configured camera number. |
| `flange_mm` | Measured `[X,Y,Z,R]`. XYZ uses millimetres and R uses degrees. |
| `tool_offset_mm` | Configured `[dx,dy,dz]` in millimetres. |
| `margin_px` | Optional finite inward inset, from `0` through `4096` pixels. The command default is `0`. |

The service reads 1 current frame, loads 1 model for that camera path, and runs 1 detector prediction on that frame.
It uses the same model and frame for projection and arm-box evidence. It does not reuse cached boxes or rewrite the calibration.
The skill does not load a separate host model.

### Geometry and result

Tip XY uses the tool's XY offset rotated by measured R and added to flange XY.
Projection uses model tip Z `flange_z + nominal_tag_height_mm - distance_mm`.
The configured tool `dz` does not supply projection height.
The projected tip must have positive camera depth and lie inside the calibrated image.
The live camera path and image size must match the stored model.

The detector must supply exactly 1 valid `Robotic Arm` box.
Label comparison ignores case, spaces, and underscores.
The box must contain finite, ordered XYXY coordinates inside the image.
The projected tip must lie inside that box after the inward margin.
Missing, malformed, or ambiguous arm evidence fails the check.

A successful command receipt contains `camera`, `calibrated=true`, and these check fields:

| Field | Meaning |
| --- | --- |
| `passed`, `reason` | Boolean result and failure reason, or `null` when no reason applies. |
| `projected_tip_px` | Projected `[x,y]`, or `null` when unavailable. |
| `arm_box_px` | Detected `[x0,y0,x1,y1]`, or `null` when unavailable. |
| `margin_px` | Applied inward margin. |
| `camera_device`, `image_size` | Camera path and `{width,height}` for this frame and model. |
| `model_created_at` | Creation time of the model used for this check. |

A successful command receipt alone is insufficient. The skill requires a valid positive check result.
It accepts only the exact command, action, and controlling-client receipt.
A guard checks stationary flange XYZ and R plus unchanged robot owner and instance before, during, and after inference.

A negative or malformed check result raises `camera moved, recalibrate`. The run enters `held` before the next step.
An earlier Stop, lease loss, or robot-motion error keeps its original cause.
The Runner stops and drains accepted work and retains occupied zones.
The skill does not retry, reacquire authority, clear a zone, or recalibrate automatically.

Perspective makes this a coarse check. A pass does not detect every camera movement or validate small calibration errors.
It does not replace physical clearance, joint and workspace limits, or the applicable hardware checks.

## Tray geometry and cross-checks

`vision.tray` locates a tray from a supplied RGB image and a validated camera model.
It does not acquire images, issue robot commands, or determine chip identity.
Use [grooved-platform-v2.toml](../config/trays/grooved-platform-v2.toml) for `Grooved Platform V2`.
`load_tray` reads the bounded TOML file. `validate_tray` checks an in-memory configuration.
Call this Python library with supplied data. It adds no HTTP command or orchestrator skill.

### Bounded groove geometry

The tray has 32 named grooves, `groove_00` through `groove_31`.
The origin is the plate corner beside marker ID `1`. The marker center has the offset shown below.
All configured coordinates use millimetres in the tray frame.

| Marker ID | Center XY |
| --- | --- |
| `1` | `(15.15, 15.0)` |
| `0` | `(162.65, 15.0)` |
| `7` | `(15.15, 162.8)` |
| `2` | `(162.65, 162.8)` |

For groove index `k = 0..31`, its centerline is at `x = 5.975 + 5.35 * k`.
Grooves `k = 5..26` extend from `y = 0.0` to `y = 177.8`.
The remaining grooves extend from `y = 30.0` to `y = 147.8`.
Each `centerline_mm` is a bounded segment. Its `reference_mm` is the nominal midpoint, used only to describe geometry.
A chip can occupy any valid position along its groove. Obtain its actual Y from vision.
Do not substitute the midpoint for a chip observation or assume that it is a taught motion target.
These chips have no label area or QR label.

### Locate the tray

Supply a current RGB image and chip centers mapped from that same image through the same camera model.
The Python interfaces accept supplied data:

```text
locate_tray(image, calibration, tray, *, device, tolerance_mm=None)
cross_check_slots(location, *, taught_stations, chips,
                  same_location_slots=(), tolerance_mm=None)
```

`device` must match the calibration's full stable camera path. Image dimensions must also match.
The reader uses `DICT_4X4_50` ArUco markers. It requires at least 3 unique observed corner IDs.
Any duplicate corner ID refuses the frame before fitting, even when other corners are visible.
Keep turret tags and spare prints out of the tray image when they repeat a corner ID.
The arm marker ID `4` is not a tray corner.

The function maps measured centers through the camera model and fits a similarity transform to the configured tray geometry.
It returns the transform, measured marker residuals, and `slots` in `robot_xy_mm` coordinates.
Each slot contains `centerline_xy_mm` and `reference_xy_mm`, plus its transformed `width_mm`.
The output supplies XY geometry only. It does not establish a physical Z or a safe robot path.
The calibration's horizontal-plane assumption also applies here. Tray tilt and chip height above that plane can affect mapped XY.

With only 3 observed corners, `missing_marker` reports sampled cells at the predicted fourth-marker position.
This result is diagnostic. It does not add an observed marker, replace missing evidence, or gate the accepted 3-marker fit.
With all 4 corners observed, `missing_marker` is `null`.
The diagnostic reports `match`, `mismatch`, or `unavailable`, with `used_for_pose=false`.
The supplied tray uses a 24 mm black marker square. Sampling assumes that its edges are parallel to the tray axes.
The comparison tests only rotations in multiples of 90 degrees. It does not test reflected candidates.
Low resolution, occlusion, or tilt can prevent a conclusive comparison.

### Check station and chip evidence

Call `cross_check_slots` with explicit evidence keyed by slot ID.
`taught_stations` maps each slot to a taught robot XY pair.
`chips` supplies one of these observations for each slot:

| Observation | Meaning |
| --- | --- |
| `{"state":"present","center_xy_mm":[x,y]}` | An observed chip center in robot XY millimetres. |
| `{"state":"empty"}` | The caller explicitly establishes that the slot is empty. |
| `{"state":"unknown"}` | The caller has no conclusive chip observation. |

An omitted chip entry is unknown. Missing detection data never establishes an empty slot.
Each groove requires a taught station and conclusive chip evidence for a complete check.
Unknown slot IDs or malformed evidence refuse the request.

The check measures each taught station and observed chip against its bounded groove segment.
The residual includes perpendicular distance and any distance beyond either end.
`tolerance_mm` must be finite, greater than 0, and at most 2 mm. The supplied TOML uses 2 mm.
With `tolerance_mm=None`, `locate_tray` inherits the tray setting and `cross_check_slots` inherits the location setting.
A residual equal to the tolerance passes. A larger residual fails.

`station_chip_distance_mm` reports the full XY distance when both positions exist.
Different valid Y positions along the groove do not indicate tray drift.
Add a slot to `same_location_slots` only when its taught station targets that same actual chip position.
For those slots, the full XY distance must also meet the tolerance. Both positions must exist.

Each slot reports `status` as `ok`, `incomplete`, or `failed`, with `missing` and `failures` details.
The result's `ok` is true only when every slot passes.
An incomplete or failed check raises `TrayCheckError`. Inspect `TrayCheckError.report` for the complete per-slot report.
Do not treat incomplete evidence as permission for motion. Normal robot limits, clearance checks, leases, and zone rules still apply.

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
