[中文](0009-service-web-framework.md)

# 0009: Web frameworks for services

## Status

Accepted: 2026-10-30.

## Context

The stage panel already uses Flask and waitress. The migrated thermal service already uses FastAPI and uvicorn.

## Decision

Use Flask and waitress for new services. Keep FastAPI and uvicorn in the migrated thermal service. Keep the robot core and panel on the standard library.

- New Flask services expose `create_app(service, *, token, base_path)`. Attach the shared `labkit` contract blueprint and any page routes to the same service object.
- Only `labkit.flask` imports Flask. Other shared contract modules use the standard library.
- The service owns device access under 1 lock. Its `start()` method starts polling. Module imports open no device and start no thread.
- Service entrypoints read `MG400_<SERVICE>_*`, support `--bind`, `--port`, and `--demo`, and enforce the shared access rules. Configuration errors exit with status 2.
- Framework choice does not change the [service contract](0001-instrument-service-contract.en.md), control leases, stop behavior, or device-specific shutdown rules.

## Consequences

New services reuse the stage deployment pattern. The thermal migration retains its existing framework while providing the same external contract.
