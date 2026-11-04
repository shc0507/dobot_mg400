# Lab dashboard

[中文](dashboard.md)

The existing optional `orch` service serves the Lab Console and the dashboard. See [deployment](deploy.en.md) and [recipe service configuration](orchestrator.en.md#http-service).
With the default configuration, the console is at `http://127.0.0.1:8772/` and the dashboard is at `http://127.0.0.1:8772/dashboard/`.
If you configure `MG400_ORCH_BASE_PATH`, use that prefix. The optional proxy uses `/lab/` and redirects `/` to `/lab/`.

## Lab Console

The console is the single entry for the operator. The old service pages stay as fallbacks.

- The header shows the console name, the deployed commit, the workspace tabs, the language switch, the devices that this page controls, and **STOP ALL MOTION**.
- The workspaces are **Overview**, **Motion**, **Cameras** and **Furnace + microscope**. A workspace that is not built yet links to the old pages.
- The language switch changes between English and 中文. The browser keeps the selected language.
- The status strip shows 1 chip for each service: a dot and a text. A status older than 3 seconds shows as stale.
- If `MG400_ORCH_TOKEN` is set, enter it in **Console access token**, then select **Connect**. The page keeps this token only in memory.

**STOP ALL MOTION** uses the same stop fan-out as the dashboard: the robot, the stage and the microscope. A stop needs no control and does not wait for a lease.

### Control

- In the **Control** card, select **Take control** for 1 device. The console requests the lease from that service with a new identity.
- The service refuses the request when another client holds the lease. The console never takes a lease from another holder.
- The page sends a heartbeat every 500 ms. Each heartbeat renews the lease 1 time.
- The console releases the lease when no heartbeat arrives for 3 seconds, or when the page closes. If a renewal fails, the control ends and the console does not request it again.
- **Release all** releases only the console's own leases.

### Command relay

The workspaces send manual commands through the console. The console forwards each command with the server-side token. The browser never receives a service token.

- Only the commands in the service's `api/describe` are permitted. Lease operations cannot be forwarded.
- A command needs control by this page, except a command that the service marks `any_client`.
- Each command is sent 1 time. If the outcome is unknown, the page shows unknown and does not retry. Check the device before you continue.

### Overview workspace

- **Readiness before a run**: the tool offset file, the camera calibration, the stage zero and soft limits, the microscope focus zero, the gas map and the over-temperature cutoff. Each row shows ready, to do or blocked, and links to the workspace that fixes it.
- The over-temperature cutoff has no machine-readable state. After H-11, the owner sets `MG400_DASH_CUTOFF_CONFIRMED=1` on the host. While it is not set, the row is blocked.
- The gas map row is ready only when each gas channel has a real gas name.
- **Run** shows the current run and its steps. Hold and Abort are in the dashboard.
- **Events** merges the `api/events` of each service, newest first. You can filter for warnings or for 1 service.
- **Safety** gives Stop all motion, Heat off and Gas off, and shows the result for each target. Heat off and Gas off need a confirmation in the page.

### Motion workspace

The Motion workspace has 3 columns: the robot arm, the cameras and the stage. At phone width they stack. **Robot and stage activity** below them lists the events of both devices and the outcome of each command from this page.

Robot arm:

- **Take control**, **Enable**, **Disable** and **Clear alarm**. **Disable** needs no control.
- The pose tiles show the actual X, Y, Z and R. They show `—` while the arm moves.
- The jog pad moves ±X, ±Y, Z up and down, or R ± by 1 step of 0.5, 2 or 10 mm (degrees for R). Each press sends 1 `execute` move from a fresh read of the actual pose. Keyboard jog stays in the robot panel.
- **Move to a pose**: **Use actual** copies the actual pose. **Execute** moves to the entered pose. The speed is from 1 to the `execute` speed cap that the robot service describes.
- Before it sends a jog or a pose, the console checks the fingertip target against `MG400_ORCH_X_MIN_MM` to `MG400_ORCH_R_MAX_DEG` and the tool offset in `MG400_ORCH_TOOL_FILE`. If a bound or the tool file is missing, the console refuses the move. Demo mode without bounds uses a synthetic box. The generic `command/robot` route refuses `execute` with 409; use the `ws/motion/jog` and `ws/motion/move` routes.
- **Gripper**: **Open** (1000), **Close** (0) and **Initialize…**. Initialization opens the jaws and needs a confirmation. The gripper outcome shows beside the title.

Stage:

- **Take control**, **Stepping** and **Stop stage**. **Stop stage** needs no control.
- A change of the stepping mode clears the zero and soft limits of all 3 axes and needs a confirmation.
- Each axis shows its position in pulses and in mm, and its reference state. **Step −** and **Step +** send 1 `move` of the entered steps and pulses/s. The stage accepts a move only after the reference is confirmed.
- **Home…** (boards with switches) and **Set zero…** need a confirmation. **Check reference** opens the stage panel for an axis with a camera reference.
- **Named positions** sends `goto` for each named position of an axis.
- Hold-to-move stays in the stage panel. The contract `hold` needs the service clock time of a fresh state and a renewal every 200 ms.

Cameras: camera2 is large, camera4 and camera3 are small. **Swap view** puts a small view in the large tile. Without the Cameras workspace a tile shows OFFLINE.

### Cameras workspace

- The grid shows 6 views: camera1 to camera4, the thermal camera and the microscope (Toupcam). Each view shows its name and role, LIVE, STALE or OFFLINE with the frame age, its mode, and **Snapshot** and **Expand**. **Focus · 1 + strip** shows 1 large view and a strip of the others.
- The console service relays all views. The browser gets no camera key and no service token. An `<img>` stream uses a short-lived single-use ticket: it is valid for 20 seconds, for 1 use and for 1 view.
- The relay opens at most 6 streams at a time, at most 2 per view and at most 2 for the microscope. The relay stops when you leave the workspace, hide the browser tab or close the page.
- You can switch each overlay on or off: the chip detection boxes on camera 4, and the thermal minimum, maximum and center markers with the colour bar.
- When the thermal camera is selected, the side panel gives the units, the palette, an auto or fixed colour scale, and Freeze view. These settings change only the display. You can export CSV, Raw + JSON and PNG.
- **Draw region** drags the `crucible` region on the thermal view and shows its bounds. **Save region** is available only when the thermal service lists `set_region` in `api/describe`, and it needs a confirmation in the page. Otherwise, put the bounds in `rois.toml`. **Calibrate shutter** also waits until the service lists `shutter`, and it needs a confirmation.
- Set the source of camera1 to camera4 on the host. The other views use the existing `MG400_ORCH_<SERVICE>_URL` and `_TOKEN`:

```sh
MG400_ORCH_CAMERA_URL=http://127.0.0.1:1985
MG400_CAMERA_KEY_FILE=/var/lib/mg400/camera/preview.key
```

### Furnace + microscope workspace

Every device command goes through the console relay. Use **Take control** for the furnace or the microscope first. **Stop heat and gas**, **Stop all gas** and the microscope **Stop** need no control.

- The furnace shows PV, SV, the mode and the maximum temperature of the thermal `crucible` region.
- The chart shows PV and SV for the last 60 minutes. The console process records 1 point every 5 s while a page is open. After a restart the chart starts empty.
- **Apply and run…** and **Start sequence…** heat the furnace and need a confirmation in the page. The target and the ramp cannot exceed the caps that the service describes.
- The sequence editor adds and removes steps and sets the target, ramp, hold and 3 gas channels of each step, and the end gas and end temperature. When a step sets gas, the end temperature is necessary. The page loads the furnace sequence on its first read.
- Until each gas channel has a real gas name, **Send flow** is not available and the service refuses gas commands. **Stop all gas** uses the gas-off route of the Overview.
- While `MG400_DASH_CUTOFF_CONFIRMED` is not set, a banner says: do not heat before H-11 and H-20.
- The microscope shows the Toupcam view and the camera3 side view. It uses `window.LabCameras.tile` from the Cameras workspace when it exists. Otherwise it shows OFFLINE.
- The objective buttons, jog and autofocus are available only when the focus zero is known. The service raises the focus to clearance before it switches the objective. The focus bar shows the position on the measured limits.
- The service accepts a focus zero only from the microscope panel. **Set focus zero…** opens the microscope panel after a confirmation. Re-zero at the 50x focus before each use.
- **Save snapshot**, **Start recording** and **Record next furnace sequence** send the microscope snapshot and recording commands.

### Run offline

```sh
MG400_ORCH_BASE_PATH=/lab uv run python -m orchestrator serve --demo --recipes-dir ./recipes --port 8790
```

Open `http://127.0.0.1:8790/lab/`. In demo mode, the first **Take control** starts the owned demo service of that device. The microscope has no owned demo.

## Dashboard

The dashboard shows service status, the current run, its steps, and links to camera pages. It stays at `/dashboard/` as a fallback.

## Access and status

Enter `MG400_ORCH_TOKEN` in **Access token**, then select **Connect**.
The page keeps this token only in memory. Reloading the page or selecting **Forget token** clears it.
Do not put this token in page URLs. Downstream service tokens stay in the orchestrator process.

The page shell and its fixed styles and script do not require a bearer token. Host and Origin checks still apply.
Status and control APIs require the orchestrator token. Mutation requests also require a JSON object and a valid client identity.
Use the configured access path. See the [single-port proxy](olares.en.md#optional-single-port-proxy) for remote access.

Each service card shows connected, busy, locked, fault, lease holder, and demo status.
The cards cover the robot, stage, thermal camera, vision service, furnace, and microscope.
Missing values remain unknown. Unavailable or stale data does not prove that a device is healthy or stopped.
Server status older than 3 seconds is stale. The browser also marks its display stale when polling fails.
The service permits only 1 background state read per instrument at a time.

In demo mode, dashboard reads never start an instrument demo.
A card becomes available only after a requested run or recovery creates that owned demo.
Production endpoints and public camera links are not used in demo mode.

## Camera links

Configure camera page URLs explicitly in the service environment:

```sh
MG400_DASH_CAMERA_URL=
MG400_DASH_THERMAL_URL=
MG400_DASH_MICROSCOPE_URL=
```

The defaults are blank. A blank setting leaves its camera tile unavailable.
Use an HTTP(S) page URL that dashboard users may receive. The browser opens that page directly.
Do not include URL credentials, instrument API bearer tokens, query parameters, fragments, spaces, control characters, or backslashes.
The dashboard does not derive these URLs from private instrument endpoints or forward instrument credentials.
A camera page can require a secret path. Only a deliberately configured link is returned through the authenticated dashboard API.
The dashboard does not discover secret paths.
The camera page keeps its own access requirements.

## Stop controls

Keep the physical emergency stop reachable. A web request or a displayed confirmation is not a physical safety interlock.
These stop controls require authentication but do not require **Take control** or a control lease.
They remain separate from run controls:

| Button | Effect |
| --- | --- |
| **Stop all motion** | Sends independent stop requests to the robot, stage, and microscope in parallel. It also holds the active run and cancels recovery. Holding a run can stop its acquired heat, gas, and water outputs. |
| **Heat off** | Requires confirmation inside the page. It cancels the furnace sequence and stops heat and all furnace-controlled gas and water. It also holds the active run and cancels recovery. |
| **Gas off** | Requires confirmation inside the page. It independently requests gas-only stops for channels `0`, `1`, and `2`, without holding the run. An active sequence or recipe can apply gas again. Furnace fault handling can also stop other outputs. |

Read every target result. A target can be pending, confirmed, unconfirmed, unavailable, or failed.
For motion and heat stops, initial confirmations are provisional while the run hold and accepted work drain.
New control work remains blocked until that work drains and the service completes its final stop attempts.
An operation that finishes can still contain unconfirmed or failed targets.
Do not infer that all motion stopped from one successful target or an accepted request.
If a result is unknown, check the device and follow its recovery procedure before continuing.

## Run control and recovery

The **Current run** panel shows the run and its recorded steps.
Select **Take control** before **Hold run**, **Resume run**, or **Abort run**.
The page renews its existing lease only. After renewal fails, it requires another explicit **Take control** action.
Releasing or losing control holds an active run and cancels recovery.

**Hold run** stops progress and starts the stop and drain procedure.
**Abort run** ends the run after cleanup. It does not clear retained zone claims.
Neither action proves that an unknown device outcome is safe.

Before resuming a held run, read the recorded step status:

| Step status | Required action |
| --- | --- |
| Pending, never started | Do not perform this step manually. Acknowledge that it will execute after resume checks pass. |
| Started | Verify its physical outcome. Complete the step manually if necessary, then record the resolution. Resume skips this old step and does not replay it. |
| Unknown | Do not resume. Resolve the missing state first. |

Enter a nonempty **Resolution note** and select the acknowledgement for the displayed step.
If this run retains zones, verify physical clearance and select the separate clearance declaration.
Idle status alone does not establish physical clearance.
The service must still finish accepted work, confirm stops, and reconcile ownership and device health.
A refusal keeps the run held.

Use the [orchestrator recovery procedure](orchestrator.en.md#http-service) for terminal runs and retained zones.
The dashboard does not automatically resume a saved run, replay a step, reacquire an old run's leases, or clear unknown occupancy.

## Agent proposals

The **Agent proposals** section shows the requester, reason, expiry, full recipe definition, and effective parameters.
Treat this text as data. A requester cannot approve its own proposal.
Use the operator credential. The gateway credential has no dashboard decision authority.

1. Review every step and parameter.
2. Select **Take control**.
3. Confirm that you are in the lab and can reach a stop control throughout the run.
4. Select **Approve and run**, or select **Reject** without acquiring control.
5. Read the decision and current run status.

Approval starts through the same admission and lease checks as a manual run.
Keep this page connected and keep control. Lease expiry or release retains the existing run hold behavior.
A stale page cannot approve. Approval requires the displayed definition to match the current catalog and effective host limits.
The page sends each decision once and does not retry an uncertain result.
If dispatch fails, that approval stays consumed. Check the run before creating another proposal.
Proposal records disappear from the page after restart. Their audit remains saved, and no pending authority resumes automatically.
See the [gateway policy](agent-gateway.en.md#approval-policy) for supported steps and limits.

## Chip passport

Select **Chip passport** on the dashboard, or open `/chip` under the configured service prefix.
Enter the orchestrator token, then search with the chip's exact QR text.
Search preserves case, spaces, slashes, and punctuation. It does not trim or normalize the identifier.
For a direct link, use `/chip?id=` followed by the URL-encoded identifier.
The ordinary `/chip/<id>` route also opens the passport shell.
Do not include the access token in either URL.

The fixed passport shell and assets keep the dashboard's Host and Origin checks.
All history, search-data, and artifact requests require the bearer token.
The page keeps the token only in memory. It does not acquire a control lease or contact instruments.
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
