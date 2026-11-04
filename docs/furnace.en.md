[中文](furnace.md)

# Furnace and gas control

The furnace service controls temperature, gas flow, and bounded sequences for operators and automation clients. The service has no web page.
Operators use the Furnace + microscope workspace of the Lab Console. The same workspace shows microscope previews and recording. See the [microscope guide](microscope.en.md). The microscope remains the camera's only owner.
See [Host deployment](deploy.en.md) and the [furnace lifecycle decision](adr/0005-furnace-lifecycle.en.md).

## Before hardware use

Do not start real heating before supervised hardware validation and operator approval.
Confirm the serial devices, gas identities, full-scale capacities, temperature caps, and process limits with the operator.
Software does not replace independent thermal protection, physical gas isolation, or an emergency shutoff.
Use the demo before a supervised hardware session. Do not run another program against the same furnace or gas controller.
Validate a new sequence at conservative temperatures. Keep the physical emergency stop accessible.

## Demo

Run this command from the repository:

```sh
uv run python -m furnace --demo --bind 127.0.0.1 --port 8769
```

`GET /api/describe` and `GET /api/state` report `demo: true`.
Demo mode uses simulated serial transports and an isolated temporary state directory.
It ignores production serial paths and gas settings. It does not model process safety or physical furnace accuracy.
The access token and bind rules still apply. Closing the process stops simulated outputs and removes its temporary state.

## Configuration

The host service reads `/etc/mg400/mg400.env`. The installer installs it only when `MG400_OPTIONAL_SERVICES` includes `furnace`.
It installs the `furnace` dependency group into `.venv` and grants the service only the `dialout` supplementary group.
The installer creates `/var/lib/mg400/furnace` with owner `1000:1000` and mode `0750`.

| Setting | Default | Rule |
| --- | --- | --- |
| `MG400_FURNACE_BIND` | `127.0.0.1` | Listen address |
| `MG400_FURNACE_PORT` | `8769` | Port from `1` to `65535` |
| `MG400_FURNACE_BASE_PATH` | `/` | API prefix, for example `/heat/` |
| `MG400_FURNACE_TOKEN` | Installer-generated | At least `24` printable ASCII characters |
| `MG400_FURNACE_SERIAL` | Empty | Furnace device under `/dev/serial/by-id/`, required before startup |
| `MG400_MFC_SERIAL` | Empty | Separate gas-controller device under `/dev/serial/by-id/`, required for gas access |
| `MG400_FURNACE_SLAVE` | `1` | Integer address from `1` to `247` |
| `MG400_FURNACE_MAX_C` | `1000` | Finite cap greater than `0`, at most `1000` °C |
| `MG400_FURNACE_MAX_RAMP` | `100` | Finite cap greater than `0`, at most `100` °C/min |
| `MG400_FURNACE_GAS_MAP` | Empty | Operator-supplied JSON map from channel to gas label |
| `MG400_FURNACE_IDLE_GAS` | All off | JSON map from channel to percent for orphan expiry, cooling completion, or uncertain recovery |
| `MG400_FURNACE_ORPHAN_GRACE_MIN` | `15` | Finite grace period at least `0`, in minutes |

The gas map uses channel keys `0`, `1`, and `2`, which address MFC units `7`, `8`, and `9`.
Percent means percent of that controller's full-scale flow. Do not interpret it as a generic volumetric flow rate.
Until the operator supplies the gas map, gas commands are refused. Stop remains available.
Idle gas defaults to all off. Any nonzero idle plan requires the operator's process decision.

In the settings file, enclose the gas map and idle gas JSON objects in single quotes.
The installer preserves the inner double quotes exactly. It rejects escapes, apostrophes, control characters, duplicate keys, and nonfinite numbers.
All other settings retain the usual quote and backslash restrictions.
These empty objects show syntax only. They do not identify gases or authorize output:

```ini
MG400_FURNACE_GAS_MAP='{}'
MG400_FURNACE_IDLE_GAS='{}'
```

Keep the configuration file private. Do not put the access token in a URL or a shared screenshot.
For VPN access, use the bind address and TCP rule in [Olares networking](olares.en.md#network).
A non-loopback bind requires a valid token. Without a token, only loopback hosts without forwarded headers are accepted.

## Take control in the console

1. In the Lab Console, open the Furnace + microscope workspace.
2. On the furnace card, select **Take control**.
3. Read the connection and fault status before setting outputs.

The lease lasts `3` s. The console renews it while it is open.
When renewal fails, select **Take control** explicitly after resolving the cause.
An automation holder blocks console and manual commands, including requests that reuse its client identifier.
The stop buttons do not require control. **Release** releases the lease without immediately stopping a bounded sequence.

The service attempts a read-only furnace attachment at startup and applies the recovery rules below.
When the furnace is not connected, the card shows **Connect furnace**. Correct the connection, then select it explicitly.
The service does not retry the connection automatically. Gas connection needs **Connect gas flow** or an explicit gas operation.

## Temperature and status

PV is measured temperature. SV is the active setpoint. A stopped furnace shows no active SV.
The API retains the stored setpoint separately for diagnostics.
The furnace card shows PV, SV, mode, the thermal crucible reading, and a temperature chart of the last `60` minutes. The chart records only while the console is open.
HOLD continues temperature regulation while it pauses the controller program. STOP disables heating and allows passive cooling.

Enter a target and ramp rate, select **Apply and run…**, then confirm in the page.
Targets must be at least `0` °C and within the configured temperature cap. Ramp rates must be positive and within the configured ramp cap.
The driver writes the target to both `SP1` and `SP2` before RUN. It uses current PV to calculate ramp duration.
When controller step `2` begins, the service requests and verifies HOLD.
Do not infer HOLD from a target match alone.

## Sequences and gas

1. Use **+ Add step** to add a row. Use the button at the end of a row to remove it.
2. Enter each target, ramp rate, and hold duration in minutes.
3. Enter optional gas percentages for each step. A blank gas field leaves that channel unchanged.
4. If any step sets gas, enter the end-gas percentages and the temperature below which end gas stops.
5. Select **Start sequence…**, then confirm in the page. It uses the displayed table without a separate save action.

The hold timer starts only after verified HOLD and PV within `1` °C of the target.
The service advances after the hold duration. Ramp and settling time make total duration longer than the sum of holds.
It does not repeatedly send temperature setters during a hold.
After the final hold, heating stops. The end-gas plan continues until PV falls below its threshold. The service then applies idle gas, which defaults to all off.

The `set_sequence` command applies edits during a run. The console disables **Save sequence** while a sequence runs.
The active step uses its numerical index. Inserting or removing an earlier row changes which row that index selects.
If a shorter table removes the active index, the service selects the new final step.
Editing fields alone does not change outputs. Save or start the sequence to send those edits.
The console shows the active step and the remaining hold time.
Changing the active target, ramp, or gas plan reapplies that step and resets its hold timer.
Changing only its hold duration or later steps preserves the current output settings.
Accepted edits are persisted, including future gas plans.

The gas area shows the measured percent of each gas. Without readback, it shows unverified.
**Send flow** applies the entered percent. The service refuses it until the gas map names real gases.
**Stop all gas** stops all gas channels and does not require control.
A missing optional reply means unverified. It does not prove zero flow or a closed valve.
A transport or invalid-reply failure locks control. **Stop all gas**, `POST /api/flow/gas/stop`, and `POST /api/flow/stop-all` remain available.
Check physical flow and use independent isolation when required.

## Microscope recording

While you hold the microscope control lease, start recording on the microscope card of the same workspace. Or use **Record next furnace sequence** to arm recording of the next furnace sequence.
The microscope reads furnace state through `MG400_MICROSCOPE_FURNACE_URL` and the existing `MG400_FURNACE_TOKEN`.
It does not acquire a furnace lease or change furnace outputs.
Arming requires fresh, healthy, idle furnace state and applies to one new sequence identity.
Missing furnace replies leave recording metadata blank and add an event. Capture continues until an explicit stop or another valid completion condition.

Recordings stay under `/var/lib/mg400/recordings/`. Finalized recordings include the timestamps CSV and a summary targeting 3 minutes. Shorter clips are not lengthened.
Cancelled or failed recordings retain local partial files without a promised summary.
Set `MG400_FURNACE_ARCHIVE_DIR` only after the operator mounts the archive share.
The service retains local files after a verified archive copy. An archive failure does not remove local data or count as successful finalization.
See the [recording instructions](microscope.en.md) for command receipts, cancellation, and chip-linked manifests.

## Client loss, recovery, and shutdown

| Condition | Behavior |
| --- | --- |
| A sequence loses its client | Continue its bounded steps and end-gas cooling plan |
| An open-ended setpoint loses its client | Start the configured orphan grace, then stop heat and apply idle gas |
| No client holds control | Stop is available. Other commands require control |
| Startup reads matching RUN or HOLD | Resume valid saved intent only when the target matches within `2` °C and live settings satisfy the caps |
| Startup reads unmatched RUN or HOLD | Attempt heat stop and idle gas, then lock until acknowledgement |
| Startup reads STOP | Remain idle, including when saved state contains a cooling plan |
| SIGINT, SIGTERM, or SIGHUP | Attempt heat and gas stop independently, close transports, and exit |

The service writes `state.json` atomically at phase changes and every `60` s during a hold.
It preserves wall-clock hold and orphan times across restart.
State files and the persistent `.state.json.lock` use mode `0600`.
Do not remove a state or lock file to clear a fault.
An interrupted command or uncertain outcome requires inspection and acknowledgement.
When the furnace is locked, the card shows **Acknowledge fault…**. It requires furnace control and a confirmation in the page.
Select and confirm it only after you check the device and make sure that the controller shows STOP.
`POST /api/furnace/acknowledge` takes `{}` and accepts only the current lease holder, with a contract or a manual lease. When no client holds control, it first acquires manual control for the caller. The runner never calls this route.
A storage failure locks runtime control and still attempts independent output stops.
If all persistent writes and invalidation fail, the service cannot guarantee a durable fault record.

**Stop heat and gas** in the console, `POST /api/stop`, `POST /api/furnace/stop`, and `POST /api/sequence/stop` cancel remaining steps and attempt both output stops.
A confirmed response requires verified stopped heat and gas. Otherwise the response remains unconfirmed.
Stop is available regardless of lease ownership or expiry failure. Authentication and request-format checks still apply.

Before host maintenance, coordinate shutdown with all clients.
`mg400ctl stop furnace` and `mg400ctl restart furnace` refuse an active sequence or cooling gas plan.
They also refuse unavailable, malformed, disconnected, or locked live status. `all` and mixed target lists receive the same check before any service changes.
Use `--force` only after coordinating the interruption. It permits shutdown and requests heat and gas stop. Verify the stop result.
The status check is advisory: another client can start a sequence after the check and before systemd acts.
See [routine deployment commands](deploy.en.md#routine-commands).

## API and troubleshooting

The service provides the [instrument contract](adr/0001-instrument-service-contract.en.md), stop routes, and operator routes.
All routes use the same lifecycle, caps, and gas policy. All requests require the configured token.
Mutations require JSON and `X-MG400-Client`. Host, Origin, and forwarded-header checks apply to all routes.
All paths follow the configured base prefix.

| Route | Use |
| --- | --- |
| `GET /api/describe` | Command schemas and caps |
| `GET /api/state` | Furnace, sequence, and gas state |
| `GET /api/events` | Service events |
| `POST /api/heartbeat`, `POST /api/renew`, `POST /api/release` | Acquire, renew, or release the contract lease |
| `POST /api/command` | Submit `connect`, `connect_flow`, `apply_setpoint`, `set_sequence`, `start_sequence`, `set_gas_flow`, `set_gas_valve`, or `inspect_gas_flow` |
| `POST /api/stop`, `POST /api/furnace/stop`, `POST /api/sequence/stop` | Stop heat and gas, and cancel the sequence |
| `POST /api/flow/gas/stop` | Stop 1 gas channel with `channel_index` |
| `POST /api/flow/stop-all` | Stop all gas |
| `POST /api/manual/heartbeat`, `POST /api/manual/renew`, `POST /api/manual/release` | Acquire, renew, or release the manual lease with an empty object |
| `POST /api/furnace/acknowledge` | Acknowledge a fault. See above |

If connection fails, check the configured stable device paths and access permissions.
The furnace uses `9600` baud, `8` data bits, no parity, and `1` stop bit. Match its configured slave address.
If control is refused, check the lease holder, gas map, caps, and fault before retrying explicitly.
If a stop is unconfirmed, inspect the device and use physical protection. Do not treat a successful HTTP response as physical confirmation.
Use `mg400ctl logs furnace` to read service events.
