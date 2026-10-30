[中文](0002-repository-layout.md)

# 0002: Repository layout and migrated code

## Status

Accepted: 2026-10-30.

## Context

Independent WuLab applications duplicate shared code. The lab needs a maintained control plane for recipes across devices.

## Decision

This repository owns the migrated WuLab code and keeps its repository name. The robot package is `mg400`. Do not depend on the source repositories.

| Path | Responsibility |
| --- | --- |
| `src/mg400`, `src/stage_controller` | Robot core, client, simulator, and stage control |
| `src/labkit` | Shared access checks, leases, events, service contract, and HTTP client |
| `src/vision`, `src/thermal_cam`, `src/furnace` | Localization, QR reading, thermal sensing, and furnace/gas control |
| `src/toupcam_camera`, `src/cameras`, `src/microscope` | Shared Toupcam support, UVC cameras, and microscope control |
| `src/orchestrator`, `src/gateway`, `src/analysis` | Recipes and run storage, agent access, and later image metrics/optimization |
| `apps/control_panel`, `recipes`, `config`, `deploy` | Robot page, TOML recipes, configuration, and installation |
| `firmware/three-motor-demo`, `firmware/microscope-axes` | Stage and microscope firmware |
| `docs`, `docs/adr`, `docs/agents`, `tests` | Human guides, decisions, agent procedures, and offline tests |

- Use 1 top-level package per area. Add each to the wheel's `packages` list. Add light dependency groups to `default-groups`. Keep `vision-gpu` outside default groups and CI.
- Keep the robot core, robot panel, and `labkit.auth`, `labkit.lease`, `labkit.events`, `labkit.client` on the standard library.
- Move approved source files and their tests from the pinned source commits. Preserve behavior, use package imports, and port host paths and device access to Linux.
- Replace desktop GUI functions with headless interfaces where specified. Exclude Windows binaries, model weights, virtual environments, and hardware scripts from tests.
- Open no device, start no thread, and write no file at import time. Give every service `--demo`.
- Record source repository, commit, and paths with `Source:` trailers. Credit source authors with `Co-authored-by:` trailers. List omitted files and reasons in each migration PR.

The first milestone is a vision-guided chip transfer between tray slots, with QR identity checks before and after the transfer.

## Consequences

The lab maintains the integrated implementations and shared tests. Common camera code has 1 owner, and rehearsal can test recipes without hardware.
