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
| Agent gateway | 8773, loopback |

- Store settings in `/etc/mg400/mg400.env`. Services read `<PREFIX>BIND`, `<PREFIX>PORT`, and `<PREFIX>TOKEN`. Code and units contain no host-specific addresses or paths.
- `MG400_OPTIONAL_SERVICES` selects optional services. The installer prepares only those. Most services use `/opt/mg400/.venv`. Vision uses `/opt/mg400/.venv-vision`.
- Keep models, calibration, taught stations, tool offsets, stage configuration, thermal regions, furnace state, runs, and recordings under `/var/lib/mg400`.
- Data ownership is `1000:1000`, directory mode `0750`, and file mode `0640` or `0600`. The installer creates data directories.
- Inventory every USB device by device name, VID:PID, stable path, hub/port, and service. Use `/dev/serial/by-id/` and `/dev/v4l/by-path/` identities.
- Put the Toupcam camera on its own USB 3 controller. Use powered hubs by job. Never reset a shared upstream hub.
- Render enabled services' udev templates with their VID/PID, `MODE="0660"`, and `@SERVICE_GROUP@` replaced by the group for GID 1000. Install `/etc/udev/rules.d/70-mg400-<name>.rules` as root with mode `0644`. Reload the rules and trigger USB enumeration.

The hardware freeze continues until the owner ends it. Inventories and software work may proceed. Device moves, wiring, and mounting wait.

## Consequences

Olares becomes the common operating host. USB topology and stable paths become deployment requirements. Software does not replace the e-stop chain or independent over-temperature cutoff.
