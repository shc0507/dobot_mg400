[中文](stage-panel.md)

# Stage

[Return to the project home page](../README.en.md)

The `stage-panel` service controls 3 Arduino motors through USB serial at 115200 baud. The Arduino runs the encoder control loop. The Console sends targets in shaft rotations and reads measured positions. Use **Motion** in the Lab Console. The stage service has no separate web page.

Use [Stage Controller v3](../firmware/stage-controller-v3/README.en.md). It connects AS5600 encoders through a TCA9548A multiplexer and reads upper/lower switches. Legacy pulse firmware can connect and stop. It cannot use the encoder motion controls.

| Motor | Console axis | Positive direction | Negative direction |
| --- | --- | --- | --- |
| `bottom` | Bottom (Y) | Up, `ccw` | Down, `cw` |
| `top` | Top (X) | Right, `ccw` | Left, `cw` |
| `insert` | Insert | In, `ccw` | Out, `cw` |

## Connection

Keep LarePass VPN connected for [Olares](http://100.64.0.1:8080/). Open **Motion**, take stage control, select the Arduino port, then select **Connect**. The port list updates automatically from service state.

The service opens no serial port at startup. Opening the port can reset the Uno. Connection waits about 2 s, then reads state and capabilities. Only 1 process can own the port. Close Arduino Serial Monitor before connecting. The service does not reconnect or retry motion automatically.

Before uploading firmware, stop motion and run `labctl stop stage`. Follow the firmware guide. After reconnecting the Arduino, check `/dev/serial/by-id/`, start the service, then connect in the Console.

On a computer connected to the Arduino:

```sh
uv sync
uv run stage-panel
```

The equivalent module command is `uv run python -m stage_controller`. On Linux, the service user needs serial read/write permission, usually through `dialout`. The Console uses `http://127.0.0.1:8767/`.

For hardware-free operation, use `uv run stage-panel --demo` and connect to `DEMO`. The local Console preview procedure is in [Dashboard](dashboard.en.md#run-offline). Demo mode does not open serial hardware. It cannot verify physical accuracy or clearance.

## Controls

Take stage control before connection or motion. Only 1 controller can hold the lease. The default lease lasts 3 s. Expiry requests a stop. Releasing the lease does not stop a finite move. Stop or wait for completion before release.

- Positions use rotations and speeds use rotations/s. The firmware fixes microstepping at 1/8. Defaults are 200 full steps/revolution and 0.25 rotations/s. Use the reported motor speed limit, normally 0.5 rotations/s with these defaults.
- Hold a direction button to move. Release it to stop. **Keys** uses I/K for Bottom Up/Down, J/L for Top Right/Left, and U/O for Insert In/Out.
- Each axis row has **Clear fault** beside its movement controls. It requires stage control and does not move the motor. The X/Y diagram shows the minimum and maximum rotation bounds at its corners; unavailable bounds show **—**.
- **Moves, limits and calibration** contains relative moves, absolute targets and setup controls. Relative moves accept 0.001–10000 rotations. A relative move requires fresh healthy encoder feedback, but no completed calibration.
- **Move to target** uses an absolute coordinate. It requires completed calibration, valid bounds and fresh feedback. Only 1 stage axis moves at a time.
- **Stop stage**, each axis's **Stop**, and **STOP ALL MOTION** need no control. Stop cancels calibration and finite motion.
- Feedback shows encoder health, measured speed, frame age, switch state and faults. Stale or untrusted positions show **—**. Entered speeds and targets remain unchanged during updates.

The browser refreshes a hold every 150 ms. The Console renews it every 0.2 s. Without a browser refresh for 0.5 s, the Console releases it. Firmware stops a hold 1 s after the last keepalive. Focus loss, a hidden page or leaving Motion releases the hold.

A completed move has no active position hold. The driver sleeps after 10 s idle. Powered encoders continue to measure manual shaft movement. The AS5600 cannot recover complete rotations made while power was off.

## Stage calibration

Use **Auto calibrate** separately for each axis:

1. The motor seeks the lower switch in `cw`.
2. It sets the lower coordinate to 0, then seeks the upper switch in `ccw`.
3. It records the upper coordinate and moves to the midpoint.
4. After calibration completes and feedback settles, absolute targets become available.

Calibration phases are `none`, `lower`, `upper`, `center`, `done`, `cancelled` and `failed`. New motion is unavailable during active phases. Calibration starts only on request.

**Reset position** sets the measured position to 0 and clears both bounds. **Set limit here** records positive coordinates as upper bounds and negative coordinates as lower bounds. It rejects 0. **Clear upper** and **Clear lower** remove only the selected bound. Each change invalidates completed calibration.

Normal switch contact stops travel toward that switch. It permits travel away from it and does not zero the coordinate. Configured soft limits also restrict travel. Direction pauses and warning counts do not start calibration. **Clear fault** clears feedback faults without moving. Lost tracking with saved bounds requires clearing both bounds before fault recovery.

Position, bounds and calibration state reside in Arduino RAM. Reset or power loss invalidates them. Recalibrate before using saved absolute targets. Sample Transfer stores named coordinates on disk, but a saved profile does not restore the Arduino reference.

Old `steps_per_mm`, millimetre geometry, named pulse poses and camera references are inactive. The service does not use them to create encoder targets. A missing legacy file does not prevent encoder control.

The legacy `crucible.load` and `crucible.unload` orchestration skills and the `stage_at_load` zone check require migration to encoder coordinates. They reject the new stage state before motion. Use Sample Transfer for the rotation-based workflows.

## Service configuration

The systemd unit is `mg400-stage-panel.service`. Its `labctl` name is `stage`. It runs as user/group `1000` with `dialout` membership. The default listener is `127.0.0.1:8767`, with canonical routes under `/api/`. The `/contract/api/` aliases use the same handlers and lease.

### Configuration

| Environment | Argument | Default | Purpose |
| --- | --- | --- | --- |
| `MG400_STAGE_SERIAL` | `--serial` | Unset | Offer only this Arduino port |
| `MG400_STAGE_CONFIG` | `--config` | `/var/lib/mg400/stage/stage.toml` | Legacy geometry retained for compatibility; inactive for encoder motion |

The arguments `--bind`, `--port` and `--base-path` override the local listener defaults. Olares clients use the fixed stage endpoint.

Arguments override environment values. Empty environment values count as unset. Olares reads `/etc/mg400/mg400.env`. Prefer a stable `/dev/serial/by-id/` path for the Arduino. A configured port that is absent produces an empty port list. Demo mode ignores the configured serial path.

### Start and stop

```sh
ls -lL /dev/serial/by-id/
labctl start stage
labctl status
labctl logs stage
labctl stop stage
```

After changing configuration or installing updated code, run `labctl restart stage`, then connect in the Console. The unit is not enabled at boot. An abnormal exit restarts it after 3 s. Exit status 2 indicates a startup configuration error and does not restart it.

## Interface

Use [Stage automation contract](development.en.md#stage-automation-contract) for command schemas, state fields, leases and completion rules. Send mutations through the Console relay or a leased contract client. Stop uses the dedicated contract stop route and requires no lease.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| No serial port | Check USB power/data wiring and `/dev/serial/by-id/`. Verify `MG400_STAGE_SERIAL` and wait for the port list to update. |
| Permission denied | Check `dialout` access. Log in again after changing local group membership. |
| Port in use | Close other stage services and serial monitors that own the same Arduino. |
| Connected, motion unavailable | Check encoder firmware capabilities, fixed 1/8 mode, frame age, encoder health, switches and cooldown. |
| Encoder offline or magnet fault | Check encoder power, the multiplexer channel, I²C wiring and magnet placement. Use **Clear fault** after correcting the cause. |
| Absolute target unavailable | Complete **Auto calibrate**, check both bounds and wait for fresh idle feedback. |
| Direction warnings | Check couplings, encoder direction and mechanical resistance. Follow the calibration recommendation when shown. |
| Stop unconfirmed or connection lost | Confirm the physical state. Restart the stage service and reconnect. Later idle feedback alone does not clear the service stop fault. |

A software stop requires a working control path. A disconnected USB cable or host failure can prevent stop confirmation. Finite firmware moves can continue until their target or a firmware limit. The service does not retry them.
