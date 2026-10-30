[中文](0005-furnace-lifecycle.md)

# 0005: Furnace client loss, restart, and shutdown

## Status

Accepted: 2026-10-30.

## Context

Furnace holds and gas schedules can outlast a client connection. Lease expiry and process shutdown require different behavior.

## Decision

Preserve ramp, HOLD, and hold-timer behavior. Apply configured temperature and ramp caps to every command.

| Condition | Required behavior |
| --- | --- |
| Sequence step starts | Apply its optional `gas` channel-to-percent table. Record readback. Missing readback means unverified, not failure. |
| Any step sets gas | Require sequence fields `end_gas` and `end_gas_until_c`. |
| Sequence loses its controlling client | Continue to its planned STOP/COOLING end. Apply `end_gas` until PV falls below `end_gas_until_c`. |
| Open-ended `apply_setpoint` loses its client | Start `MG400_FURNACE_ORPHAN_GRACE_MIN`, default 15 min. On expiry, stop heating and apply `MG400_FURNACE_IDLE_GAS`, default all off. |
| No client holds the lease | Accept stop. Other commands wait until a client acquires the lease. |
| Startup reads RUN or HOLD with matching persisted state | Resume only when the controller target matches within 2 °C. Record an event. |
| Startup reads RUN or HOLD without matching state | Stop heating, apply idle gas, and lock until a person acknowledges. |
| Startup reads STOP | Remain idle. |
| Service receives SIGTERM | Stop heating and gas. |

Persist `/var/lib/mg400/furnace/state.json` atomically at each phase change and every 60 s during a hold. Store steps, index, phase, hold start as wall-clock time, elapsed time, gas plan, and orphan time.

`mg400ctl` refuses furnace stop or restart during a sequence unless the caller supplies `--force`. Until the operator supplies `MG400_FURNACE_GAS_MAP`, refuse gas commands. The operator supplies the gas map and process defaults.

## Consequences

A lost client does not interrupt a bounded thermal sequence. Shutdown still disables outputs. Restart uncertainty requires acknowledgement, and software does not replace independent thermal protection.
