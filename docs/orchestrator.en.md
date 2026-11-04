[中文](orchestrator.md)

# Recipe orchestrator

The orchestrator runs a recipe across instrument services. A recipe is a versioned TOML file with parameters and ordered skill steps. A skill is a named operation in the registry. Each run records its steps, chip identities, samples, artifacts, and safety outcomes.

## Commands

From the repository, use the project environment:

```sh
uv run labctl rehearse recipes/example-rehearsal.toml --runs-dir ./rehearsal-runs
uv run labctl runs --runs-dir ./rehearsal-runs
uv run labctl runs RUN_ID --runs-dir ./rehearsal-runs
```

`labctl run RECIPE` uses configured instrument services. Before a hardware run, complete the required operator checks and keep an operator at the emergency stop. Agents must use rehearsal only.

`--runs-dir PATH` selects the record directory. `--param NAME=JSON` overrides a declared recipe parameter. Quote string values as JSON strings:

```sh
uv run labctl rehearse recipes/example-rehearsal.toml --runs-dir ./rehearsal-runs --param 'chip_id="demo-chip-2"' --time-scale 0.1
```

`--time-scale` is positive and applies only to rehearsal. Its default is `0.1`. It scales simulated waits, while step timeouts remain wall-clock limits.

## HTTP service

The optional `orch` service runs `python -m orchestrator serve` with Flask and waitress.
Install it through the [service registry](deploy.en.md#service-registry). It uses no device groups and starts no recipe automatically.
The existing `run`, `rehearse`, `runs`, and `station` commands remain available.

The service listens on `127.0.0.1:8772`. The installed unit sets the base path `/lab/`, which the proxy serves.
It reads the recipe catalog from `/opt/mg400/recipes`.
The flags `--bind`, `--port`, `--base-path`, `--recipes-dir`, and `--runs-dir` override these defaults.

POST requests require `Content-Type: application/json` and `X-MG400-Client` with a stable caller identity.
Acquire and renew the frontend lease through `/api/heartbeat`. All custom mutation routes below require that lease.
The shared `/api/stop` route remains available to any client. It holds the active run or cancels recovery.
Frontend lease release or expiry also requests a hold or cancels recovery.

The following paths are relative to the configured base path:

| Method and path | Request or result |
| --- | --- |
| `GET /api/recipes` | List recipe identifiers, names, and parameter declarations |
| `POST /api/runs` | Start with a required `recipe` identifier and an optional `params` object |
| `GET /api/runs` | List recorded runs |
| `GET /api/runs/<id>` | Read a run and its recorded results |
| `POST /api/runs/<id>/resume` | Supply `manually_completed: true`, a nonempty `note`, and `physically_clear: true` when this run retains zone claims |
| `POST /api/runs/<id>/abort` | Abort the current run with an empty object |
| `POST /api/runs/<id>/recovery` | Begin recovery of retained claims from an ended run with an empty object |
| `POST /api/runs/<id>/recovery/confirm` | Confirm `physically_clear: true` and a nonempty `note` |
| `POST /api/runs/<id>/recovery/abort` | Cancel recovery with an empty object |

Mutations return `202` and a `command_id` after admission. Poll `/api/state` and `/api/runs/<id>` for the asynchronous outcome.
Admission does not prove completion. Only 1 run or recovery session can be active.
The complete recipe and its required zone services are validated before device leases are acquired.

Live resumption uses the manual-completion and drain checks described below. It advances to the next step without replaying the failed step.
Recovery of an ended run is separate. Its first request creates fresh client identities and holds the required leases during manual checks.
It does not clear claims. Confirm only after checking physical clearance and completing the required device recovery.
Confirmation performs fresh stop or verified-rest checks, validates ownership and health, and records the clearance before removing the compared claims.
Cancel, lease loss, or shutdown stops and drains recovery work and releases leases while retaining claims.
Recovery never restarts the stored recipe.

On `SIGTERM`, the service requests a hold and waits for accepted work and stop operations to finish.
It records the run state and releases leases before closing. Retained zone claims remain after shutdown.
The systemd unit waits for this cleanup. A restart does not resume saved runs or clear their zones.

For an isolated local service, use:

```sh
uv run python -m orchestrator serve --demo --recipes-dir ./recipes
```

Demo startup removes downstream endpoints and credentials, device paths, and proxies from the process environment before threads start.
It owns temporary run records, occupancy, device simulations, gripper feedback, and controller locks.
Ambient record and occupancy paths are ignored. `--runs-dir` is refused with `--demo`.
The recipe catalog remains read-only. Explicit station, tool, workspace, pre-grip, and zone-configuration settings remain available as read-only inputs.
Demo initialization does not establish physical clearance. Temporary data is removed when the service closes.

## Recipes

The CLI validates the complete recipe before it creates records, starts demos, or acquires a service. It rejects unknown skills, missing parameters, wrong types, invalid timeouts, and unknown fields.

```toml
name = "Record a sample"
version = 1

[params.chip_id]
type = "string"
default = "demo-chip"

[[steps]]
skill = "sample.record"
params = { chip_id = "${chip_id}", note = "Example record" }
timeout_s = 1
on_fail = "hold"
```

A parameter type is `string`, `integer`, `number`, or `boolean`. A parameter without a default requires an override. `${chip_id}` substitutes the complete parameter value and preserves its type. Each step requires `skill`, `params`, and a positive finite `timeout_s`. `on_fail` defaults to `hold` and also accepts `abort`.

| Skill | Parameters | Service |
| --- | --- | --- |
| `wait` | `seconds` | None |
| `fail` | `message` | None |
| `robot.enable` | None | Robot |
| `robot.move` | `x_mm`, `y_mm`, `z_mm`, `r_deg` | Robot |
| `robot.stop` | None | Robot |
| `robot.pick` | `station`, `grip` | Robot |
| `robot.place` | `station`, `release` | Robot |
| `robot.transfer` | `from`, `to`, `grip`, `release` | Robot |
| `vision.find_chip` | `slot` | Robot, vision |
| `vision.slot_state` | `slot` | Robot, vision |
| `vision.tray_transfer` | `from_slot`, `to_slot`, optional `speed` | Robot, vision |
| `crucible.load` | `from_slot`, optional limits below | Robot, stage, furnace |
| `crucible.unload` | `to_slot`, optional limits below | Robot, stage, furnace, thermal |
| `sample.record` | `chip_id`, `note` | None |

The example in `recipes/example-rehearsal.toml` enables the simulated robot, moves it, records a sample, and stops it. It contains no gas or heating command. Custom skills receive a revocable context with service clients, run records, and the zone manager. Named zone entry requires explicit configuration and checks the configured entry conditions.

## Service settings

Each run uses a unique client identity. The runner checks all required service contracts before acquiring the first lease. It renews existing leases without an acquisition fallback. A lost lease holds the run and cannot be resumed through automatic reacquisition.

These values are fixed defaults:

| Item | Fixed default |
| --- | --- |
| Record directory | `/var/lib/mg400/runs` |
| Client timeout | `2` s |
| Zone configuration | `/var/lib/mg400/zones/config.toml` |
| Durable occupancy | `/var/lib/mg400/zones/occupancy.json` |
| Robot | `http://127.0.0.1:8766/` |
| Stage contract | `http://127.0.0.1:8767/contract/` |
| Thermal | `http://127.0.0.1:8768/` |
| Furnace | `http://127.0.0.1:8769/` |
| Microscope | `http://127.0.0.1:8770/` |
| Vision | `http://127.0.0.1:8771/` |

The services do not check tokens. Service adapters connect directly and do not use proxy settings. See the [service contract](adr/0001-instrument-service-contract.en.md).

## Zones and entry conditions

A zone is a shared physical space with an owner device and entry conditions.
The configuration names permitted owners. The occupancy record names the current holder.
A second holder waits for release and fails after its timeout.
Zone waits cannot exceed the remaining step deadline. A custom zone context can request a shorter timeout.
Missing configuration or an unknown zone prevents entry. There is no fallback that skips zone checks.

The operator must prepare the private host directory before using the installed orchestrator:

```sh
sudo install -d -m 0750 -o 1000 -g 1000 /var/lib/mg400/zones
```

Save the configuration as `/var/lib/mg400/zones/config.toml`, owned by `1000:1000`, with mode `0640`.
For local paths, the parent directory must belong to the caller and use mode `0700` or `0750`.
Config and occupancy reads accept only regular files with mode `0600` or `0640` and the required owner.
The store refuses unsafe parents and symbolic links in all path components. It atomically writes occupancy in the existing owned directory.
The installer does not prepare this directory. Agents must not execute the host preparation command.
All hardware runs controlling the same equipment must use the same configuration and occupancy paths.

Storage contention waits at most 0.25 seconds, within the remaining step deadline.
Write, file-sync, and replacement failures before publication preserve the old claims.
A directory-sync failure after replacement reports a failure, but the new state may already be visible.
A released claim can appear only after verified departure or reconciled physical-clearance recovery. Do not assume rollback after an error.

The zone configuration is:

```toml
[zones.tray]
owner = "robot"
conditions = []

[zones.crucible-load]
owner = "robot"
conditions = ["stage_at_load"]

[zones.furnace-tube]
owner = "stage"
conditions = ["robot_clear"]
```

Custom zone names also require an explicit owner and conditions. Owners use the service names in the settings table above.
Names contain 1 to 64 letters, digits, underscores, or hyphens and start with a letter or digit.
Unknown fields, unknown conditions, and repeated conditions are refused.
The reserved `crucible-load` zone requires the robot owner and `stage_at_load`.
The reserved `furnace-tube` zone requires the stage owner and `robot_clear`.
The robot cannot hold it. Do not send the robot beneath the microscope.
These ownership checks do not prove that arbitrary `robot.move` commands avoid the microscope.
Operator-verified routes and workspace limits remain required.

| Condition | Required state |
| --- | --- |
| `stage_at_load` | Fresh, connected, healthy stage state under this run's control. All 3 axes must be homed, ready for automation, and idle. Insert must be awake and report `load` in `at_positions`. |
| `robot_clear` | No durable claim for `crucible-load`. The run also holds the robot control lease. |

The runner resolves services needed by conditions before acquiring leases.
Condition checks never depend on a service left under manual control. Clients acquired only for conditions are not available to skills for writes.
The checks use reported named positions. They do not infer a load position from coordinates.
Awake time is not renewed automatically. A lost condition holds the run and requests stops.

A recipe step can declare `zone = "tray"`. The runner checks that zone before the step and again after it.
Generic step completion retains the durable claim. A successful step alone does not prove that the device left the zone.
The station skills release claims only at their explicit measured exits.
Exceptions, abort, and process exit retain claims. Neither idle feedback nor lease expiry clears them.

The run snapshot reports claims in `zones` and failures in `zone_error`.
Before clearing a retained claim, physically check that the zone is clear.
Live held recovery requires manual completion and a nonempty note.
Only when the current run holds retained claims does it also require the separate declaration `physically-clear`.
Recovery records that declaration. Another run's claims do not trigger this declaration and cannot be cleared through the current run's recovery.

Every resumption requires old work to drain, fresh stop confirmation or verified robot rest, matching ownership, and healthy service states.
A transient guard failure with no claim held by the current run can resume through normal manual completion after these checks.
This clears only the transient error. It does not clear another run's claims or replay the failed step.
Recovery never reacquires a lost lease.

Claims from an earlier process also require audited recovery with already-owned clients. There is no installed recovery command.
Do not delete or replace the occupancy file to clear a claim.

### Recover claims from an exited process

The public Python API is `orchestrator.zones.Zones.recover`.
An operator must first inspect the equipment and confirm physical clearance. The earlier process must have exited.
Use the same zone configuration and occupancy paths. Prepare these arguments in the recovery application:

| Argument | Requirement |
| --- | --- |
| `recipe`, `registry` | A validated recipe and its skill registry, passed to `prepare_zones`. |
| `zone_settings` | A mapping with `MG400_ORCH_ZONES_FILE` and `MG400_ORCH_ZONE_STATE_FILE`. |
| `recovery_services` | Service-name mapping of public adapters that already own every service recorded in the retained claims. Maintain their leases during recovery. |
| `recovery_id` | A unique identity for this recovery manager. |
| `old_holder` | The retained holder identifier from the claims in `manager.snapshot()`. |
| `record_event` | A callback accepting event kind and data. Persist the audit event. Return `False` or raise if persistence fails. |
| `operator_note` | A nonempty description of the physical checks and manual resolution. |

```python
from orchestrator.zones import Zones, prepare_zones

plan = prepare_zones(recipe, registry, settings=zone_settings)
manager = Zones(plan, recovery_services, holder=recovery_id, audit=record_event)
manager.recover(
    old_holder,
    recovery_services,
    physically_clear=True,
    note=operator_note,
)
```

This call reads discovery and verifies existing ownership before clearing the reconciled claims.
After the drain or exited-process check, it requires fresh stop confirmation or verified robot rest and healthy idle states.
Robot rest uses the same explicit manual `disable` proof as live recovery. Without that proof, it requests Stop.
The robot requires its instance ID. Generic contract services may omit it.
When an instance ID is present, it must be a nonempty string and remain unchanged during recovery.
A concurrently changed claim prevents clearing it.
It refuses recovery if the previous process is still alive, a required service is absent, or a stop remains unconfirmed.
The call never acquires a lease. A persistence error prevents a successful recovery result.
For a live held run, use the runner's recovery prompt instead.

## Held runs and abort

On failure or timeout, the runner revokes the failed step and requests a stop from every acquired service. With `on_fail = "hold"`, it retains the leases and waits for a person. It never retries the failed step.

At the prompt, enter `abort` to terminate the run. To continue after manual completion, enter `manually-completed` and supply a nonempty resolution note. The old worker, helper calls, and in-flight service operations must finish. The runner then requires fresh stop confirmation or verified robot rest after an explicit manual `disable`. Robot rest requires the latest command to confirm successful disable, current disabled and idle feedback, and no keyboard or gripper activity. Without this proof, and during ordinary stop, hold, abort, or cleanup, the runner sends Stop.

Every service must be healthy, retain ownership, and report `busy = false` and `locked = false`. Generic services also require `fault = null`, and any reported instance ID must match. The robot requires fresh syncing feedback, `robot_error = false`, `stop_unconfirmed = false`, and a valid unchanged instance ID. The runner never disables the robot automatically. Lease loss or audit persistence failure prevents resumption. The runner records manual completion and continues with the next step, while a refused resolution keeps the run held.

EOF, Ctrl-C, SIGTERM, and SIGHUP request abort. The CLI waits for active work and stop operations to finish before closing the store or demo services. Repeated interrupts do not bypass this wait.

The CLI prints the in-memory safety report even if database writes fail. Check `stops`, `unconfirmed_services`, `persistence_error`, and `release_errors`. An `aborted` status does not prove that every device stopped. If a service cannot confirm its stop, keep the work area clear and follow the device recovery procedure.

## Run records

The database is `runs.db` inside the record directory. Run files use a separate `RUN_ID` directory. Samples always have a `chip_id`. The store accepts only its required owner, safe file types, and private permissions. It refuses symbolic links in the record path.

`labctl runs` reads history without acquiring a service. An optional run ID shows steps, samples, artifacts, and events. This command never resumes or restarts a stored run. A held row from an earlier process requires operator recovery before a new run.

## Teaching stations and tool offsets

A station stores a measured fingertip pose, its approach clearance, and an optional route through other stations. Keep the controller at `User=0,Tool=0`. The tool offset applies in software only.

The operator must supply the measured offset from H-08 in `/var/lib/mg400/robot/tool.toml`. Its only field is `offset_mm = [dx, dy, dz]`. Replace these names with the measured finite numbers in millimetres. The offset has no default. Missing or invalid measurements prevent teaching.

The conversion rotates `(dx, dy)` by the flange angle R, then adds the result to flange XY. Fingertip Z equals flange Z minus dz. The inverse conversion subtracts the rotated XY offset and adds dz. Both conversions preserve R, including its sign and complete turns.

The tool file is `MG400_ORCH_TOOL_FILE`, default `/var/lib/mg400/robot/tool.toml`. The station file is `/var/lib/mg400/robot/stations.toml`.

Station teaching and the station, tray, and crucible transfers below are postponed work. Their code needs measured inputs that the host configuration does not provide, so these commands and steps refuse on the host.

An operator first positions the robot in the Motion workspace of the Lab Console. The console, or another controller, must retain control while teaching. Teaching requires fresh measured feedback, an idle robot, no lock, and no unconfirmed stop. The teaching command reads discovery and state only. It never acquires or releases a lease, changes controller tools, or sends motion.

```sh
labctl station save tray-pick --approach 20 --zone tray
labctl station list
labctl station show tray-pick
labctl station rm tray-pick
```

`--approach` is a vertical clearance above the fingertip Z coordinate and must be at least 10 mm. The command example uses 20 mm, which the operator must check against the measured workspace. `--zone` defaults to an empty name. `--stations-file PATH` overrides the station path for any station command. For teaching, `--tool-file PATH` overrides the tool path.

The file uses one `[stations.NAME]` table per station. Fields are `x_mm`, `y_mm`, `z_mm`, `r_deg`, `approach_mm`, `zone`, and `saved_at`. `saved_at` is a Unix timestamp. Optional `via` lists station names in travel order.

Names use 1 to 64 letters, digits, underscores, or hyphens. Start a name with a letter or digit. Every reference must exist, with no duplicates or cycles. Saving an existing station preserves its route. Removing a referenced station fails.

Every edit checks the complete station graph before publication. Concurrent edits serialize, and the file replacement is atomic. The store refuses symbolic links and special files.

The installer creates `/var/lib/mg400/robot` with owner `1000:1000` and mode `0750`, including when the optional orchestrator service is disabled.
The parent `/var/lib/mg400` remains owned by `root:1000` with mode `0750`.
Legacy files at `/var/lib/mg400/tool.toml` or `/var/lib/mg400/stations.toml` remain unchanged. The installer prints a manual move notice.
Follow the [robot data migration instructions](deploy.en.md#migration-to-a-new-host) before using those files with the new defaults.
An existing `MG400_ORCH_TOOL_FILE` override is preserved. Update an old override after moving its file.

A write or file-sync failure preserves the old file. A directory-sync failure after replacement reports an error but can leave the new file visible. Inspect the station file before repeating an edit after that error.

## Station transfers

`robot.pick` and `robot.place` use a station name. `robot.transfer` combines them with public parameters `from` and `to`. These names receive the same complete type and unknown-field checks as other recipe parameters.

```toml
[[steps]]
skill = "robot.transfer"
params = { from = "tray-pick", to = "tray-place", grip = 400, release = 900, speed = 1 }
timeout_s = 60
on_fail = "hold"
```

The numbers in this fragment are examples, not measured hardware settings. A pick needs a measured pre-grip opening. It has no default. `grip`, `release`, and the pre-grip opening are integers from 0 to 1000. The pre-grip opening must exceed `grip`. For a transfer, `release` must also exceed `grip`. Before travel to the destination, it must exceed the measured contact width.

The robot must be enabled and idle, with fresh feedback and an initialized, available gripper. Every state check verifies this run's ownership and the same robot instance. A lock, robot error, unconfirmed stop, or active keyboard control prevents transfer. The skills never initialize the gripper or recover control automatically.

Before starting, position the robot at a taught station or its approach. The measured flange pose must match exactly one station after tool conversion, within 0.1 mm or 0.1 degrees per axis. An unknown or ambiguous start refuses motion. The skill checks the starting station again before each route leg.

Before the first motion, the skill expands all `via` routes and checks every waypoint and tool conversion. Converted flange poses must remain finite. A station's approach height is its Z plus `approach_mm`.

Each leg rises only at its starting station, to that station's approach. It follows the declared `via` order at each via station's taught pose. It then reaches the destination's approach and descends to its taught pose. Each move is a straight line. If the robot is already at the destination, it skips that station's incoming vias. A leading via that is the starting station is already visited and is also skipped.

A pick opens to the pre-grip opening before descending. It closes to `grip`, requires `contact`, and rises to that station's approach while checking for `dropped`. A place requires an existing contact. It descends, opens to `release`, requires `arrived`, and rises to that station's approach. An empty close that reports `arrived` cannot satisfy a pick.

`robot.pick`, `robot.place`, and `robot.transfer` accept `speed`, an integer with default 1. The permitted range is 1 through the robot service's advertised `MG400_ROBOT_MAX_SPEED_L` cap, which is at most 100. Invalid values or an invalid advertised cap refuse the step before motion. All moves use `execute`, and the robot service still enforces its speed and acceleration caps. The controller remains at `User=0,Tool=0`.

Each move waits for completion and the next fresh measured pose. The measured flange coordinates must match the requested waypoint within 0.1 mm or 0.1 degrees on each axis. Missing pose samples cause reads only, within the step deadline. A mismatch, drop, unexpected outcome, or timeout holds the run and requests stops. No move or grasp is retried.

The skill enters the starting station's zone before its initial rise. It enters each later named station zone before moving toward it. During a transfer, it retains the source zone until measured arrival toward the next station. Standalone pick and place release their final zone only after measured arrival at the raised endpoint. Failures retain durable claims for physical checks and audited recovery.

The operator must verify that every endpoint approach is reachable, clears its working zone, and connects to an unobstructed route. At C, do not assume that the minimum 10 mm rise is reachable. Over the tray, the approach must keep flange Z below the camera 4 limit of 154 mm. Re-teach an unsuitable endpoint or route before use. The minimum approach does not prove physical clearance. Software waypoint checks do not validate collisions or joint reachability.

## Vision-guided tray transfer

`recipes/tray-transfer.toml` requires the string parameters `from_slot` and `to_slot`.
Its guarded `vision.tray_transfer` step checks calibration, observes both slots, transfers the chip, and checks both slots again.
It requires a chip at the source and an empty target before motion.
After placement, it requires a chip at the target and an empty source.
Missing or ambiguous evidence, an empty grasp, a drop, or failed calibration holds the run.

The step needs measured inputs with no hardware defaults: a tray-model TOML file of at most 16 KiB, the calibrated tray camera from 1 to 4, grip and release targets from 0 to 1000, the chip axis `long` or `short`, and a flange R offset and window narrower than 180 degrees.
Symbolic links, nonregular files, multiple hard links, and concurrent changes of the tray file refuse the step.
The station-transfer inputs also remain required.
Teach an actual pick or place target for each exact slot ID.
Use a safe contact depth and an operator-validated approach of at least 10 mm.
Travel references and calibration contacts do not replace these targets. A groove midpoint is not a taught target.

The service maps the observed chip axis into robot XY before the skill applies the measured R offset.
Equivalent orientations differ by multiples of 180 degrees. The permitted interval must select exactly 1.
The result must also satisfy the ordinary workspace and robot limits. A raw image angle is not a flange R target.

Before entry, the robot must satisfy the [calibration drift check](vision.en.md#calibration-drift-check) at the stored scan R inside its calibrated grid.
It must also match an unambiguous taught station or its approach, with a verified route to the source.
The skill does not position the robot for the drift check or initialize its gripper.

The source endpoint uses observed XY and the permitted R, with the taught depth, approach, zone, and vias.
The destination remains the exact taught slot target. Stored stations do not change.
The skill checks every waypoint and conversion before motion. Speed defaults to 1 and remains bounded by the robot service cap.
Observation results stay inside the guarded step, with stable camera, image dimensions, and calibration identity.
See [guarded slot evidence](vision.en.md#guarded-slot-evidence) for the observation contract.

For hardware use, an operator must validate the settings, clearance, reach, and grasp convention before the supervised procedure.
After an enabled robot receives `stop_sync` or its client context exits, feedback and the motion lock remain until explicit disable.
Prepare subsequent runs through that explicit operator workflow. The recipe does not recover or retry automatically.
Hardware acceptance requires 3 consecutive supervised transfers with vision confirmation and results recorded on the task issue.

Run the offline acceptance cases without opening device or network ports:

```sh
uv run pytest -q tests/test_recipe_tray_transfer.py
```

These cases use explicit synthetic inputs and an in-process ready robot.
Ordinary owned rehearsal starts disabled. The production tray recipe does not enable the robot or initialize its gripper.
After the operator completes measured configuration and hardware readiness, run the recipe through the configured services:

```sh
uv run labctl run recipes/tray-transfer.toml --param 'from_slot="YOUR_SOURCE_SLOT"' --param 'to_slot="YOUR_TARGET_SLOT"'
```

Replace both placeholders with exact taught slot IDs. Keep the operator at the emergency stop throughout the hardware run.

## Crucible load and unload

`crucible.load` moves a chip from a taught tray station to `crucible-load`, then inserts the crucible.
`crucible.unload` withdraws the crucible, waits for a cold named thermal region, then transfers the chip to a taught tray station.
The tray station must differ from `crucible-load`. That station must use zone `crucible-load`.
Do not declare the entire composite step with `zone = "crucible-load"`. Its robot phase releases that zone before Insert can move.

Supply these values as typed recipe parameters. The default value `-1` means unset.
It is never a safety limit. Missing, nonfinite, or invalid values refuse the step before any command.

| Parameter | Requirement |
| --- | --- |
| `load_max_c` | Nonnegative maximum furnace PV for load |
| `unload_max_c` | Nonnegative maximum crucible-region temperature for unload |
| `grip` | Integer from 0 to 1000 |
| `release` | Integer from 0 to 1000, greater than `grip` and the measured contact width |
| `awake_s` | Greater than 0 and at most 60 seconds |
| `max_age_s` | Positive maximum measured-data age |

The required pre-grip opening, measured tool offset, and taught routes follow the station-transfer rules.
Both routes and every tool conversion are checked before Insert or the robot receives a command.
Both skills accept `speed` with default 1 and the same advertised service cap as station transfers.
Robot moves use the taught route heights and retain measured arrival, `contact`, `arrived`, and `dropped` checks.
The skill never homes, initializes, enables, unlocks, or retries a device.

Every stage axis must have a fresh home reference and soft limits at its current microstep.
The read-only `automation.at_poses` field compares each configured pose with measured positions:

```json
{"furnace":{"matched":false,"axes":{"bottom":true,"top":true,"insert":false}}}
```

The field is empty during motion, an active automation sequence, stale feedback, or an uncertain reference or stop.
`matched` is true only when all configured axis targets match. It never assumes that a named position equals 0.
Load requires matching Bottom and Top furnace targets plus Insert at its named `load` position.
Unload requires the complete measured `furnace` pose. It does not command that pose to repair a failed precondition.
Bottom and Top must remain idle at the verified alignment throughout the transfer.

The furnace must report fresh measured PV, `STOP/COOLING`, no running sequence, and no fault or persistence error.
Load requires PV strictly below `load_max_c`. After robot departure reaches the taught clearance, Insert moves to its named `furnace` position.
The run retains known occupancy of `furnace-tube` while Insert remains inside. A successful load does not prove departure from that zone.

Unload reserves `furnace-tube`, moves only Insert to `load`, and verifies fresh named arrival before releasing the zone.
It waits for the existing stage cooldown with reads before dispatch. It never retries a rejected motion command.
It then requests its explicit awake interval and reads only the named thermal region `crucible`.
The reading's maximum must be strictly below `unload_max_c`. There is no whole-frame fallback.
The skill pins the integer pixel bounds from `api/describe`. Each reading must match that rectangle within the native 160 by 120 sensor.
Malformed, stale, future, or nonfinite readings refuse the transfer. The thermal service's age bound can only tighten `max_age_s`.
While the region remains hot, the skill reads again within the step deadline. It holds and requests stops when the deadline expires.

Each awake phase sends only 1 explicit request. The remaining wall-clock step budget must fit within its interval after millisecond truncation.
Rehearsal time scaling does not extend that budget. Lease renewal never extends awake time.
Owned service state, measured freshness, and reported instance identities are checked again during waits and before each actuation.
The runner also monitors furnace and thermal conditions during blocking robot commands. A failed check revokes the step and requests independent stops.
No new phase starts before old monitor checks drain.

A later unload in the same run can reuse its known retained tube claim.
A standalone unload can reserve an absent claim after all measured preconditions pass.
Foreign, unknown, or terminal retained claims require the existing explicit physical-clearance recovery. A new run never adopts them automatically.
Software checks do not establish physical clearance. The operator must verify the taught routes, stage calibration, temperature limits, and approach heights.

## Rehearsal and installed commands

Rehearsal creates and owns demo services on numeric loopback addresses. It checks `demo = true` and closes its services after the run terminates. It removes production device settings, proxy settings, and Python environment overrides. Robot rehearsal requires the repository checkout with its robot service `apps/control_panel/`. Demo factories cover the robot, stage, thermal camera, and furnace. Unsupported required services fail before the run starts.

Transfer rehearsal retains the explicit station inputs and the zone configuration path. The documented configuration path defaults still apply. Files must contain valid geometry and named zone definitions. Rehearsal does not invent an offset, workspace, or zone configuration. It resolves condition services before creating demos. Its occupancy file belongs to the temporary rehearsal directory.

Its gripper feedback belongs to its temporary SIM-01 collector. Only that demo starts initialized. It sends no initialization command and never selects a production collector path.

The default simulated gripper reaches its target and reports `arrived`. A pick therefore holds with an empty grasp. Offline tests supply explicit contact and drop scenarios to exercise the complete transfer and failure paths. Rehearsal does not establish physical clearance or prove a real grasp.

Crucible rehearsal preserves the explicit safety inputs above. It can read named-region data for its owned thermal demo.
Without that data, it defines no thermal regions. It never selects a real camera, production furnace, or device path.
The demo stage starts without a home reference. Offline tests explicitly home their owned simulator and supply contact feedback.
The furnace demo owns its serial simulators and temporary state. Crucible skills send no heat or gas command.

On an installed host, `labctl` and `mg400ctl` use the same dispatcher. Recipe commands are exactly `run`, `rehearse`, and `runs`. Station commands are exactly `station save`, `station list`, `station show`, and `station rm`. Administrative commands retain their root requirement.

When root invokes a recipe or station command, the dispatcher uses `runuser --pty` to select UID/GID 1000 before starting Python. It uses the absolute installed interpreter with `-I`. The child receives every nonempty `MG400_*` setting from the host settings file and the fixed service URLs.

Root rehearsal does not read the host settings file. It receives the `MG400_*` settings from the caller's environment and no fixed service URL.

A direct installed invocation requires real and effective UID/GID 1000. It uses the caller's environment settings and never reads the root settings file. Local `uv run labctl` uses the developer's identity. Use `--runs-dir` for a local writable directory.
