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
| `sample.record` | `chip_id`, `note` | None |

The example in `recipes/example-rehearsal.toml` enables the simulated robot, moves it, records a sample, and stops it. It contains no gas or heating command. Custom skills receive a revocable context with service clients, run records, and the zone manager. Until ORC-04, the zone manager performs no checks and provides no cross-device interlock. Zone entry does not validate conditions or exclude other services.

## Service settings

Each run uses a unique client identity. The runner checks all required service contracts before acquiring the first lease. It renews existing leases without an acquisition fallback. A lost lease holds the run and cannot be resumed through automatic reacquisition.

| Setting | Meaning |
| --- | --- |
| `MG400_ORCH_RUNS_DIR` | Record directory, default `/var/lib/mg400/runs` |
| `MG400_ORCH_TIMEOUT_S` | Positive finite client timeout in seconds, default `2` |
| `MG400_ORCH_ROBOT_URL`, `MG400_ORCH_ROBOT_TOKEN` | Robot endpoint and token |
| `MG400_ORCH_STAGE_URL`, `MG400_ORCH_STAGE_TOKEN` | Stage contract endpoint and token |
| `MG400_ORCH_THERMAL_URL`, `MG400_ORCH_THERMAL_TOKEN` | Thermal endpoint and token |
| `MG400_ORCH_VISION_URL`, `MG400_ORCH_VISION_TOKEN` | Vision endpoint and token |
| `MG400_ORCH_FURNACE_URL`, `MG400_ORCH_FURNACE_TOKEN` | Furnace endpoint and token |
| `MG400_ORCH_MICROSCOPE_URL`, `MG400_ORCH_MICROSCOPE_TOKEN` | Microscope endpoint and token |

Configure only the services in use. Use the stage contract base path, which ends with `/contract`. Tokens must follow the [service contract](adr/0001-instrument-service-contract.en.md), including the minimum length of 24 characters. Service adapters connect directly and do not use proxy settings.

## Held runs and abort

On failure or timeout, the runner revokes the failed step and requests a stop from every acquired service. With `on_fail = "hold"`, it retains the leases and waits for a person. It never retries the failed step.

At the prompt, enter `abort` to terminate the run. To continue after manual completion, enter `manually-completed` and supply a nonempty resolution note. The old worker, helper calls, and in-flight service operations must finish. After the manual declaration, the runner requests a fresh stop and requires confirmation.

Ownership must match. If a service reports an instance ID, that ID must also match. Services must be healthy and idle, with `busy = false`, `locked = false`, and `fault = null`. Lease loss or audit persistence failure prevents resumption. The runner records the manual completion and continues with the next step. A refused resolution leaves the run held.

EOF, Ctrl-C, SIGTERM, and SIGHUP request abort. The CLI waits for active work and stop operations to finish before closing the store or demo services. Repeated interrupts do not bypass this wait.

The CLI prints the in-memory safety report even if database writes fail. Check `stops`, `unconfirmed_services`, `persistence_error`, and `release_errors`. An `aborted` status does not prove that every device stopped. If a service cannot confirm its stop, keep the work area clear and follow the device recovery procedure.

## Run records

The database is `runs.db` inside the record directory. Run files use a separate `RUN_ID` directory. Samples always have a `chip_id`. The store accepts only its required owner, safe file types, and private permissions. It refuses symbolic links in the record path.

`labctl runs` reads history without acquiring a service. An optional run ID shows steps, samples, artifacts, and events. This command never resumes or restarts a stored run. A held row from an earlier process requires operator recovery before a new run.

## Rehearsal and installed commands

Rehearsal creates and owns demo services on numeric loopback addresses. It checks `demo = true` and closes its services after the run terminates. It removes production device settings, tokens, proxy settings, and Python environment overrides. Robot rehearsal requires the repository checkout with its control panel. Demo factories currently cover the robot, stage, and thermal camera. Unsupported required services fail before the run starts.

On an installed host, `labctl` and `mg400ctl` use the same dispatcher. Recipe commands are exactly `run`, `rehearse`, and `runs`. Administrative commands retain their root requirement.

When root invokes a recipe command, the dispatcher uses `runuser --pty` to select UID/GID 1000 before starting Python. It uses the absolute installed interpreter with `-I`. Only the approved orchestrator settings reach the child. Tokens and endpoints pass only to `run`. `runs` receives only the record path. Root rehearsal does not read the host settings file.

A direct installed invocation requires real and effective UID/GID 1000. It uses only the caller's approved explicit environment settings and never reads the root settings file. Local `uv run labctl` uses the developer's identity. Use `--runs-dir` for a local writable directory.
