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
Closing the process stops simulated outputs and removes its temporary state.

## Configuration

The host service reads `/etc/mg400/mg400.env`. The installer always installs the furnace service but does not start it. After you set the serial paths, run `mg400ctl start furnace`.
It installs the `furnace` dependency group into `.venv` and grants the service only the `dialout` supplementary group.
The installer creates `/var/lib/mg400/furnace` with owner `1000:1000` and mode `0750`.

| Setting | Default | Rule |
| --- | --- | --- |
| `MG400_FURNACE_SERIAL` | Empty | Furnace device under `/dev/serial/by-id/`, required before startup |
| `MG400_MFC_SERIAL` | Empty | Separate gas-controller device under `/dev/serial/by-id/`, required for gas access |
| `MG400_FURNACE_SLAVE` | `1` | Integer address from `1` to `247` |
| `MG400_FURNACE_GAS_MAP` | `Gas 1` to `Gas 3` | JSON map from channel to gas label |
| `MG400_FURNACE_IDLE_GAS` | All off | JSON map from channel to percent for orphan expiry, cooling completion, or uncertain recovery |

The gas map uses channel keys `0`, `1`, and `2`, which address MFC units `7`, `8`, and `9`.
Percent means percent of that controller's full-scale flow. Do not interpret it as a generic volumetric flow rate.
These values are fixed in code: the temperature cap `1000` °C, the ramp cap `100` °C/min, and the orphan grace `15` minutes.
By default, the service listens on `127.0.0.1:8769` with the base path `/`. `--bind`, `--port`, and `--base-path` override these defaults. The `mg400ctl` shutdown check reads `127.0.0.1:8769`.
The microscope and the orchestrator use the fixed URL `http://127.0.0.1:8769/`. If you change the listen address, port, or base path, these 2 clients cannot reach the furnace service.
Idle gas defaults to all off. Any nonzero idle plan requires the operator's process decision.

In the settings file, enclose the gas map and idle gas JSON objects in single quotes.
The installer preserves the inner double quotes exactly. It rejects escapes, apostrophes, control characters, duplicate keys, and nonfinite numbers.
All other settings retain the usual quote and backslash restrictions.
These empty objects show syntax only:

```ini
MG400_FURNACE_GAS_MAP='{}'
MG400_FURNACE_IDLE_GAS='{}'
```

The service checks no token, `Host`, or `Origin`. The VPN is the access boundary. See [Olares networking](olares.en.md#network).

## Take control in the console

1. In the Lab Console, open the Furnace + microscope workspace.
2. On the furnace card, select **Take control**.
3. Read the connection and fault status before setting outputs.

The lease lasts `3` s. The console renews it while it is open.
When renewal fails, select **Take control** explicitly after resolving the cause.
When another client holds the lease, the service refuses **Take control**. A contract lease also blocks the manual-lease routes, including requests that reuse its client identifier.
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
The ramp duration (the difference between target and current PV, divided by the ramp rate) must be at most `3200` min. If it is longer, the service refuses the command before it writes to the controller. The furnace does not lock. A sequence checks each step in the same way: the first step from the current PV, and each later step from the previous target.
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
If the controller enters STOP during a sequence, for example from its front panel, the service locks the furnace until a fault acknowledgement.

The `set_sequence` command applies edits during a run. During end-gas cooling, the service refuses `set_sequence` and `start_sequence`.
The console disables **Save sequence** and **Start sequence…** while a sequence runs. It also disables **Start sequence…** during cooling.
The active step uses its numerical index. Inserting or removing an earlier row changes which row that index selects.
If a shorter table removes the active index, the service selects the new final step.
Editing fields alone does not change outputs. Save or start the sequence to send those edits.
The console shows the active step and the remaining hold time.
Changing the active target or ramp reapplies that step and resets its hold timer. Changing only its gas plan applies the new gas at once and keeps the hold timer.
Changing only its hold duration or later steps preserves the current output settings.
Accepted edits are persisted, including future gas plans.

The gas area shows the measured percent of each gas. Without readback, it shows unverified.
**Send flow** applies the entered percent.
**Stop all gas** sends `POST /api/flow/gas/stop` for each channel. It stops all gas channels and does not require control.
**Close** on a gas row stops that channel and does not require control. It is sent 1 time. The result is confirmed, not confirmed, or unknown.
`set_gas_valve` with `open: true` sends full-scale flow (`100` %). `open: false` is the same as a stop of that channel.
A missing optional reply means unverified. It does not prove zero flow or a closed valve.
A transport or invalid-reply failure locks control. **Stop all gas**, `POST /api/flow/gas/stop`, and `POST /api/flow/stop-all` remain available.
Check physical flow and use independent isolation when required.

## Microscope recording

While you hold the microscope control lease, start recording on the microscope card of the same workspace. Or use **Record next furnace sequence** to arm recording of the next furnace sequence.
The microscope reads furnace state from `http://127.0.0.1:8769/`.
It does not acquire a furnace lease or change furnace outputs.
Arming requires fresh, healthy, idle furnace state and applies to one new sequence identity.
Missing furnace replies leave recording metadata blank and add an event. Capture continues until an explicit stop or another valid completion condition.

Recordings stay under `/var/lib/mg400/recordings/`. Finalized recordings include the timestamps CSV and a summary targeting 3 minutes. Shorter clips are not lengthened.
Cancelled or failed recordings retain local partial files without a promised summary.
See the [recording instructions](microscope.en.md#recording-and-furnace-sequences) for command receipts, cancellation, and chip-linked manifests.

## Client loss, recovery, and shutdown

| Condition | Behavior |
| --- | --- |
| A sequence loses its client | Continue its bounded steps and end-gas cooling plan |
| An open-ended setpoint loses its client | Start the configured orphan grace, then stop heat and apply idle gas |
| No client holds control | Stop is available. Other commands require control |
| Startup reads matching RUN or HOLD | Resume valid saved intent only when the target matches within `2` °C and live settings satisfy the caps |
| Startup reads unmatched RUN or HOLD | Attempt heat stop and idle gas, then lock until acknowledgement |
| Saved state has a fault or an interrupted command | Lock until acknowledgement, also when the controller shows STOP |
| Startup reads STOP | Remain idle, including when saved state contains a cooling plan |
| SIGINT, SIGTERM, or SIGHUP | Attempt heat and gas stop independently, close transports, and exit |

Matching state requires the same furnace serial path, slave address, gas serial path, and gas map as when the state was saved. If you change these settings during heating, the next startup stops the outputs and locks.
The service writes `state.json` atomically at phase changes and every `60` s during a hold.
It preserves wall-clock hold and orphan times across restart.
State files and the persistent `.state.json.lock` use mode `0600`.
Do not remove a state or lock file to clear a fault.
An interrupted command or uncertain outcome requires inspection and acknowledgement.
When the furnace is locked, the card shows **Acknowledge fault…**. It requires furnace control and a confirmation in the page.
Select and confirm it only after you check the device and make sure that the controller shows STOP.
`POST /api/furnace/acknowledge` takes `{}` and accepts only the current lease holder, with a contract or a manual lease. When no client holds control, it first acquires manual control for the caller. The runner never calls this route.
The service refuses acknowledgement with HTTP 409 until startup recovery completes. It also refuses with HTTP 409 unless the furnace is connected and reads STOP.
A storage failure locks runtime control and still attempts independent output stops.
If all persistent writes and invalidation fail, the service cannot guarantee a durable fault record.

**Stop heat and gas** in the console, `POST /api/stop`, `POST /api/furnace/stop`, and `POST /api/sequence/stop` cancel remaining steps and attempt both output stops.
A confirmed response requires verified stopped heat and gas. Otherwise the response remains unconfirmed.
Without a gas controller, or without gas readback, a stop remains unconfirmed.
Stop is available regardless of lease ownership or expiry failure. The JSON, `X-MG400-Client`, and body-format checks still apply.

Before host maintenance, coordinate shutdown with all clients.
`mg400ctl stop furnace` and `mg400ctl restart furnace` refuse an active sequence or cooling gas plan.
They also refuse unavailable, malformed, disconnected, or locked live status. `all` and mixed target lists receive the same check before any service changes.
Use `--force` only after coordinating the interruption. It permits shutdown and requests heat and gas stop. Verify the stop result.
The status check is advisory: another client can start a sequence after the check and before systemd acts.
See [routine deployment commands](deploy.en.md#routine-commands).

## API and troubleshooting

The service provides the [instrument contract](adr/0001-instrument-service-contract.en.md), stop routes, and operator routes.
All routes use the same lifecycle, caps, and gas policy.
Mutations require JSON and `X-MG400-Client`.
All paths follow the configured base prefix.

| Route | Use |
| --- | --- |
| `GET /api/describe` | Command schemas and caps |
| `GET /api/state` | Furnace, sequence, and gas state |
| `GET /api/events` | Service events after sequence number `since` |
| `POST /api/heartbeat`, `POST /api/renew`, `POST /api/release` | Acquire, renew, or release the contract lease |
| `POST /api/command` | Submit `connect`, `connect_flow`, `apply_setpoint`, `set_sequence`, `start_sequence`, `set_gas_flow`, `set_gas_valve`, or `inspect_gas_flow` |
| `POST /api/stop`, `POST /api/furnace/stop`, `POST /api/sequence/stop` | Stop heat and gas, and cancel the sequence |
| `POST /api/flow/gas/stop` | Stop 1 gas channel with `channel_index` |
| `POST /api/flow/stop-all` | Stop all gas |
| `POST /api/manual/heartbeat`, `POST /api/manual/renew`, `POST /api/manual/release` | Acquire, renew, or release the manual lease with an empty object |
| `POST /api/furnace/acknowledge` | Acknowledge a fault. See above |

`POST /api/stop` accepts an optional `wait_s` from `0` to `15`. The furnace service repeats the stop until the stop is confirmed, the service locks, or `wait_s` ends. It returns `stop` and `state`.
`POST /api/furnace/stop` and `POST /api/sequence/stop` accept only `{}` and return `ok`, `stop`, and `status`.
The gas stop routes return `ok` and `status`. `ok` means only that the service is not locked. It does not prove zero flow.

If connection fails, check the configured stable device paths and access permissions.
The furnace uses `9600` baud, `8` data bits, no parity, and `1` stop bit. Match its configured slave address. The driver supports the AI-808 and AI-518P controllers.
The gas line uses `9600` baud, `8` data bits, no parity, and `1` stop bit. The MFC addresses are `7`, `8`, and `9`. A Modbus relay at address `4` on the same line controls the gas solenoids.
If control is refused, check the lease holder, caps, and fault before retrying explicitly.
If a stop is unconfirmed, inspect the device and use physical protection. Do not treat a successful HTTP response as physical confirmation.
After a configuration error, the service exits with status `2` and systemd does not restart it.
Use `mg400ctl logs furnace` to read errors and service events.
