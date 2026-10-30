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
| `sample.record` | `chip_id`, `note` | None |

The example in `recipes/example-rehearsal.toml` enables the simulated robot, moves it, records a sample, and stops it. It contains no gas or heating command. Custom skills receive a revocable context with service clients, run records, and the zone manager. Named zone entry requires explicit configuration and checks the configured entry conditions.

## Service settings

Each run uses a unique client identity. The runner checks all required service contracts before acquiring the first lease. It renews existing leases without an acquisition fallback. A lost lease holds the run and cannot be resumed through automatic reacquisition.

| Setting | Meaning |
| --- | --- |
| `MG400_ORCH_RUNS_DIR` | Record directory, default `/var/lib/mg400/runs` |
| `MG400_ORCH_TIMEOUT_S` | Positive finite client timeout in seconds, default `2` |
| `MG400_ORCH_ZONES_FILE` | Zone configuration, default `/var/lib/mg400/zones/config.toml` |
| `MG400_ORCH_ZONE_STATE_FILE` | Durable occupancy, default `/var/lib/mg400/zones/occupancy.json` |
| `MG400_ORCH_ROBOT_URL`, `MG400_ORCH_ROBOT_TOKEN` | Robot endpoint and token |
| `MG400_ORCH_STAGE_URL`, `MG400_ORCH_STAGE_TOKEN` | Stage contract endpoint and token |
| `MG400_ORCH_THERMAL_URL`, `MG400_ORCH_THERMAL_TOKEN` | Thermal endpoint and token |
| `MG400_ORCH_VISION_URL`, `MG400_ORCH_VISION_TOKEN` | Vision endpoint and token |
| `MG400_ORCH_FURNACE_URL`, `MG400_ORCH_FURNACE_TOKEN` | Furnace endpoint and token |
| `MG400_ORCH_MICROSCOPE_URL`, `MG400_ORCH_MICROSCOPE_TOKEN` | Microscope endpoint and token |

Configure only the services in use. Use the stage contract base path, which ends with `/contract`. Tokens must follow the [service contract](adr/0001-instrument-service-contract.en.md), including the minimum length of 24 characters. Service adapters connect directly and do not use proxy settings.

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

The operator must supply the measured offset from H-08 in `/var/lib/mg400/tool.toml`. Its only field is `offset_mm = [dx, dy, dz]`. Replace these names with the measured finite numbers in millimetres. The offset has no default. Missing or invalid measurements prevent teaching.

The conversion rotates `(dx, dy)` by the flange angle R, then adds the result to flange XY. Fingertip Z equals flange Z minus dz. The inverse conversion subtracts the rotated XY offset and adds dz. Both conversions preserve R, including its sign and complete turns.

Configure every workspace bound before teaching or reading stations:

| Setting | Meaning |
| --- | --- |
| `MG400_ORCH_TOOL_FILE` | Tool file, default `/var/lib/mg400/tool.toml` |
| `MG400_ORCH_STATIONS_FILE` | Station file, default `/var/lib/mg400/stations.toml` |
| `MG400_ORCH_X_MIN_MM`, `MG400_ORCH_X_MAX_MM` | Fingertip X bounds in millimetres |
| `MG400_ORCH_Y_MIN_MM`, `MG400_ORCH_Y_MAX_MM` | Fingertip Y bounds in millimetres |
| `MG400_ORCH_Z_MIN_MM`, `MG400_ORCH_Z_MAX_MM` | Fingertip Z bounds in millimetres |
| `MG400_ORCH_R_MIN_DEG`, `MG400_ORCH_R_MAX_DEG` | Rotation bounds in degrees |

Bounds are finite, inclusive, and have no default. Each minimum must be less than its maximum. They apply to fingertip coordinates in the controller base frame. The station and its raised approach endpoint must fit these bounds. Workspace bounds do not establish obstacle clearance or joint reachability.

An operator first positions the robot through its panel. The panel, or another controller, must retain control while teaching. Teaching requires fresh measured feedback, an idle robot, no lock, and no unconfirmed stop. The teaching command reads discovery and state only. It never acquires or releases a lease, changes controller tools, or sends motion.

```sh
labctl station save tray-pick --approach 20 --zone tray
labctl station list
labctl station show tray-pick
labctl station rm tray-pick
```

`--approach` is a vertical clearance above the fingertip Z coordinate and must be at least 10 mm. The command example uses 20 mm, which the operator must check against the measured workspace. `--zone` defaults to an empty name. `--stations-file PATH` overrides the station path for any station command. For teaching, `--tool-file PATH` overrides the tool path.

The file uses one `[stations.NAME]` table per station. Fields are `x_mm`, `y_mm`, `z_mm`, `r_deg`, `approach_mm`, `zone`, and `saved_at`. `saved_at` is a Unix timestamp. Optional `via` lists station names in travel order.

Names use 1 to 64 letters, digits, underscores, or hyphens. Start a name with a letter or digit. Every reference must exist, with no duplicates or cycles. Saving an existing station preserves its route. Removing a referenced station fails.

Every edit checks the complete station graph before publication. Concurrent edits serialize, and the file replacement is atomic. The file mode is `0640`. The parent directory must have mode `0700` or `0750`.

Data under `/var/lib/mg400` requires owner `1000:1000`. Other paths require the caller's owner. Files may use `0600` or `0640` when read. The store refuses symbolic links, special files, and public permissions.

A write or file-sync failure preserves the old file. A directory-sync failure after replacement reports an error but can leave the new file visible. Inspect the station file before repeating an edit after that error.

## Station transfers

`robot.pick` and `robot.place` use a station name. `robot.transfer` combines them with public parameters `from` and `to`. These names receive the same complete type and unknown-field checks as other recipe parameters.

```toml
[[steps]]
skill = "robot.transfer"
params = { from = "tray-pick", to = "tray-place", grip = 400, release = 900 }
timeout_s = 60
on_fail = "hold"
```

The numbers in this fragment are examples, not measured hardware settings. Set `MG400_ORCH_PRE_GRIP` to the required opening before a pick. It has no default. `grip`, `release`, and the pre-grip opening are integers from 0 to 1000. The pre-grip opening must exceed `grip`. For a transfer, `release` must also exceed `grip`. Before travel to the destination, it must exceed the measured contact width.

The robot must be enabled and idle, with fresh feedback and an initialized, available gripper. Every state check verifies this run's ownership and the same robot instance. A lock, robot error, unconfirmed stop, or active keyboard control prevents transfer. The skills never initialize the gripper or recover control automatically.

Before the first motion, the skill expands all `via` routes and checks their coordinates and tool conversions. Travel height is the greatest of the current fingertip Z and every routed station's Z plus its `approach_mm`. The current pose, station poses, and raised endpoints must fit the explicit fingertip workspace. Converted flange poses must remain finite. All moves use `execute` with `speed = 1`, within the host speed and acceleration caps. The controller remains at `User=0,Tool=0`.

A pick rises at the current XY, follows the route at travel height, and opens to the pre-grip opening. It then descends, closes to `grip`, requires `contact`, and rises while checking for `dropped`. A place requires an existing contact, travels at height, descends, opens to `release`, requires `arrived`, and rises. An empty close that reports `arrived` cannot satisfy a pick.

Each move waits for completion and the next fresh measured pose. The measured flange coordinates must match the requested waypoint within 0.1 mm or 0.1 degrees on each axis. Missing pose samples cause reads only, within the step deadline. A mismatch, drop, unexpected outcome, or timeout holds the run and requests stops. No move or grasp is retried.

The skill enters each named station zone before moving toward it. During a transfer, it retains the source zone until measured arrival toward the next station. Standalone pick and place release their final zone only after measured arrival at the raised endpoint. Failures retain durable claims for physical checks and audited recovery.

The operator must verify that each taught approach height clears its working zone and that every route is unobstructed. The minimum 10 mm approach and workspace bounds do not prove physical clearance. Software waypoint checks do not validate collisions or joint reachability.

## Rehearsal and installed commands

Rehearsal creates and owns demo services on numeric loopback addresses. It checks `demo = true` and closes its services after the run terminates. It removes production device settings, tokens, proxy settings, and Python environment overrides. Robot rehearsal requires the repository checkout with its control panel. Demo factories currently cover the robot, stage, and thermal camera. Unsupported required services fail before the run starts.

Transfer rehearsal retains explicit station and tool paths, workspace bounds, the pre-grip opening, and the zone configuration path. The documented configuration path defaults still apply. Files must contain valid geometry and named zone definitions. Rehearsal does not invent an offset, workspace, or zone configuration. It resolves condition services before creating demos. Its occupancy file belongs to the temporary rehearsal directory, regardless of `MG400_ORCH_ZONE_STATE_FILE`.

Its gripper feedback belongs to its temporary SIM-01 collector. Only that demo starts initialized. It sends no initialization command and never selects a production collector path.

The default simulated gripper reaches its target and reports `arrived`. A pick therefore holds with an empty grasp. Offline tests supply explicit contact and drop scenarios to exercise the complete transfer and failure paths. Rehearsal does not establish physical clearance or prove a real grasp.

On an installed host, `labctl` and `mg400ctl` use the same dispatcher. Recipe commands are exactly `run`, `rehearse`, and `runs`. Station commands are exactly `station save`, `station list`, `station show`, and `station rm`. Administrative commands retain their root requirement.

When root invokes a recipe or station command, the dispatcher uses `runuser --pty` to select UID/GID 1000 before starting Python. It uses the absolute installed interpreter with `-I`. Only the approved orchestrator settings reach the child. `run` receives its configured service endpoints, tokens, transfer geometry settings, and both zone paths. `runs` receives only the record path.

Root rehearsal does not read the host settings file. It receives explicit geometry, zone configuration, and record-path settings from the caller's environment. It receives no service endpoint, token, or production occupancy path.

`station save` receives the station and tool paths, workspace bounds, robot endpoint and token, and request timeout. The other station commands receive only the station path and workspace bounds. They receive no endpoint or token.

A direct installed invocation requires real and effective UID/GID 1000. It uses only the caller's approved explicit environment settings and never reads the root settings file. Local `uv run labctl` uses the developer's identity. Use `--runs-dir` for a local writable directory.
