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
| Camera preview / go2rtc | 1985 / 1984 |
| Thermal / furnace / microscope | 8768 / 8769 / 8770 |
| Vision | 8771 |
| Orchestrator and Lab Console | 8772, console control socket 8774 |
| Single-port proxy | 8080 |

All services listen only on `127.0.0.1`. The proxy is the only entry point on the VPN.

- Store settings in `/etc/mg400/mg400.env`. The file holds hardware facts and the proxy address (`MG400_PROXY_BIND`, `MG400_PROXY_PORT`). The other services listen on fixed loopback ports. `src/labkit/ports.py` holds the only port table. No setting changes these ports. Code and units contain no host-specific addresses or paths.
- `MG400_OPTIONAL_SERVICES` selects optional services. The installer prepares only those. Most services use `/opt/mg400/.venv`. Vision uses `/opt/mg400/.venv-vision`.
- Keep models, calibration, taught stations, tool offsets, stage configuration, thermal regions, furnace state, runs, and recordings under `/var/lib/mg400`.
- `/var/lib/mg400` is `0:1000` with mode `0750`. Its data directories are `1000:1000` with mode `0750`, and files use mode `0640` or `0600`. The private directories `locks`, `camera`, `stage`, and `calibration/cameras` use mode `0700`. The installer creates data directories.
- Inventory every USB device by device name, VID:PID, stable path, hub/port, and service. Use `/dev/serial/by-id/` and `/dev/v4l/by-path/` identities.
- Connect every USB device through 1 self-powered hub on 1 host port. Put the Toupcam camera on the hub's USB 3 side. Never reset the shared hub.
- Keep each camera on its fixed hub port. The 4 AC410 cameras report the same serial number, so stable names must distinguish their ports.
- Render enabled services' udev templates with their VID/PID, `MODE="0660"`, and `@SERVICE_GROUP@` replaced by the group for GID 1000. Install `/etc/udev/rules.d/70-mg400-<name>.rules` as root with mode `0644`. Reload the rules and trigger USB enumeration.

## Consequences

Olares becomes the common operating host. USB topology and stable paths become deployment requirements. Software does not replace the e-stop chain or independent over-temperature cutoff.
