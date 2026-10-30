[中文](furnace.md)

# Furnace and gas control

The furnace page controls temperature, gas flow, and bounded sequences through the same service as automation clients.
Camera and recording controls are unavailable. See [Host deployment](deploy.en.md) and the [furnace lifecycle decision](adr/0005-furnace-lifecycle.en.md).

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

Open `http://127.0.0.1:8769/`. The page shows a demo badge.
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
| `MG400_FURNACE_BASE_PATH` | `/` | Page and API prefix, for example `/heat/` |
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

## Open the page and take control

1. Open the configured page address.
2. Enter the service token in the access field and select **Use token**.
3. Select **Take control**, or send an explicit manual command when no client holds control.
4. Read the connection and fault status before setting outputs.

The page keeps the token only in memory and sends it as a bearer header.
Reloading the page clears the token. The page stores only its client identifier in session storage.
Manual control renews every `1` s. The lease lasts `3` s.
When renewal fails, select **Take control** explicitly after resolving the cause.
An automation holder blocks manual commands, including requests that reuse its client identifier.
The stop buttons remain available. **Release control** releases the lease without immediately stopping a bounded sequence.

The service attempts a read-only furnace attachment at startup and applies the recovery rules below.
If the device is disconnected, correct its connection and select **Connect** explicitly.
The service does not retry the connection automatically. Gas connection is a separate explicit command or part of an explicit gas operation.

## Temperature and status

PV is measured temperature. SV is the active setpoint. A stopped furnace shows no active SV.
The API retains the stored setpoint separately for diagnostics.
The page reports RUN, HOLD, STOP, and connection state. Status updates approximately every `1` s.
HOLD continues temperature regulation while it pauses the controller program. STOP disables heating and allows passive cooling.

Enter a target and ramp rate, then select **Apply and run**.
Targets must be at least `0` °C and within the configured temperature cap. Ramp rates must be positive and within the configured ramp cap.
The driver writes the target to both `SP1` and `SP2` before RUN. It uses current PV to calculate ramp duration.
When controller step `2` begins, the service requests and verifies HOLD.
Do not infer HOLD from a target match alone.

## Sequences and gas

1. Add or remove rows in the sequence table.
2. Enter each target, ramp rate, and hold duration in minutes.
3. Enter optional gas percentages for each step. A blank gas field leaves that channel unchanged.
4. If any step sets gas, enter the end-gas percentages and the temperature below which end gas stops.
5. Select **Start sequence**. It uses the displayed table without a separate save action.

The hold timer starts only after verified HOLD and PV within `1` °C of the target.
The service advances after the hold duration. Ramp and settling time make total duration longer than the sum of holds.
It does not repeatedly send temperature setters during a hold.
After the final hold, heating stops. The end-gas plan continues until PV falls below its threshold. The service then applies idle gas, which defaults to all off.

**Save / update sequence** applies edits during a run.
The active step uses its numerical index. Inserting or removing an earlier row changes which row that index selects.
If a shorter table removes the active index, the service selects the new final step.
Editing fields alone does not change outputs. Save or start the sequence to send those edits.
Status polling preserves the editor. The page highlights the active row and shows remaining hold time.
Changing the active target, ramp, or gas plan reapplies that step and resets its hold timer.
Changing only its hold duration or later steps preserves the current output settings.
Accepted edits are persisted, including future gas plans.

The gas panel shows requested percent, measured percent, and valve readback separately.
**Send flow** applies the entered percent. **100%** explicitly requests full-scale flow.
A missing optional reply means unverified. It does not prove zero flow or a closed valve.
A transport or invalid-reply failure locks control. **Close valve** and **Stop all gas flow** remain available.
Check physical flow and use independent isolation when required.

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
Select **Acknowledge fault** only after checking the device and obtaining verified STOP.
A storage failure locks runtime control and still attempts independent output stops.
If all persistent writes and invalidation fail, the service cannot guarantee a durable fault record.

**Stop heat and gas** and **Stop sequence** cancel remaining steps and attempt both output stops.
A confirmed response requires verified stopped heat and gas. Otherwise the response remains unconfirmed.
Stop is available regardless of lease ownership or expiry failure. Authentication and request-format checks still apply.

Before host maintenance, coordinate shutdown with all clients.
`mg400ctl stop furnace` and `mg400ctl restart furnace` refuse an active sequence or cooling gas plan.
They also refuse unavailable, malformed, disconnected, or locked live status. `all` and mixed target lists receive the same check before any service changes.
Use `--force` only after coordinating the interruption. It permits shutdown and requests heat and gas stop. Verify the stop result.
The status check is advisory: another client can start a sequence after the check and before systemd acts.
See [routine deployment commands](deploy.en.md#routine-commands).

## API and troubleshooting

The service provides the [instrument contract](adr/0001-instrument-service-contract.en.md) and the existing furnace page routes.
Both use the same lifecycle, caps, and gas policy. All API reads require the configured token.
Mutations require JSON and `X-MG400-Client`. Host, Origin, and forwarded-header checks apply to page and API routes.
Use `GET /api/describe` for command schemas and `GET /api/state` for state.
All paths follow the configured base prefix.

If connection fails, check the configured stable device paths and access permissions.
The furnace uses `9600` baud, `8` data bits, no parity, and `1` stop bit. Match its configured slave address.
If control is refused, check the lease holder, gas map, caps, and fault before retrying explicitly.
If a stop is unconfirmed, inspect the device and use physical protection. Do not treat a successful HTTP response as physical confirmation.
Use `mg400ctl logs furnace` to read service events.
