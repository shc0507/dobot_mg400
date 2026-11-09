[中文](0003-one-host-olares.md)

# 0003: One host, Olares

## Status

Accepted: 2026-10-30.

## Context

Lab devices currently have separate application environments. Central services must retain stable device identities and controlled access.

## Decision

Connect every lab device to Olares and run its services there. Each device has 1 service owner. Use these service ports:

| Service | Port |
| --- | --- |
| Robot panel | 8766 |
| Stage panel | 8767 |
| Camera preview / go2rtc | 1985 / 1984. go2rtc is loopback-only |
| Thermal / furnace / microscope | 8768 / 8769 / 8770 |
| Vision | 8771, loopback |
| Orchestrator and dashboard | 8772 |

- Store settings in `/etc/mg400/mg400.env`. The file holds only hardware facts. Bind addresses and ports are fixed defaults in code. Code and units contain no host-specific addresses or paths.
- `MG400_OPTIONAL_SERVICES` selects optional services. The installer prepares only those. Most services use `/opt/mg400/.venv`. Vision uses `/opt/mg400/.venv-vision`.
- Keep models, calibration, taught stations, tool offsets, stage configuration, thermal regions, furnace state, runs, and recordings under `/var/lib/mg400`.
- Data ownership is `1000:1000`, directory mode `0750`, and file mode `0640` or `0600`. The private stage calibration directory `/var/lib/mg400/stage` uses mode `0700`. The installer creates data directories.
- Inventory every USB device by device name, VID:PID, stable path, hub/port, and service. Use `/dev/serial/by-id/` and `/dev/v4l/by-path/` identities.
- Connect every USB device through 1 self-powered hub on 1 host port. Put the Toupcam camera on the hub's USB 3 side. Never reset the shared hub.
- Keep each camera on its fixed hub port. The 3 AC410 cameras report the same serial number, so stable names must distinguish their ports.
- Render enabled services' udev templates with their VID/PID, `MODE="0660"`, and `@SERVICE_GROUP@` replaced by the group for GID 1000. Install `/etc/udev/rules.d/70-mg400-<name>.rules` as root with mode `0644`. Reload the rules and trigger USB enumeration.

Device moves are complete. Wiring and mounting wait until the owner ends the hardware freeze. Inventories, measurements, reviews, decisions, and software work may proceed. Software uses demo and rehearsal modes.

## Consequences

Olares becomes the common operating host. USB topology and stable paths become deployment requirements. Software does not replace the e-stop chain or independent over-temperature cutoff.
