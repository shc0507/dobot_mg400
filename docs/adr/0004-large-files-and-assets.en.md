[中文](0004-large-files-and-assets.md)

# 0004: Large files and assets

## Status

Accepted: 2026-10-30.

## Context

Model weights and native libraries must remain available without placing binary assets or large data in Git.

## Decision

Publish model weights as GitHub Release assets. Describe each installed asset in `config/assets.toml` with `url`, `sha256`, `bytes`, `install`, `mode`, `service`, `source`, and `license`. The `install` field is an absolute destination path under `/var/lib/mg400/`.

- The installer downloads to a temporary file in the target directory. It checks byte count and SHA-256, replaces the destination atomically, and sets ownership and mode. Refuse symbolic links.
- `--fetch-assets` downloads assets. `--asset NAME=PATH` installs local files with the same checks. Dry runs list planned actions. `mg400ctl assets` reports present, missing, or mismatched assets.
- Install only assets for enabled services. Store model weights under `/var/lib/mg400/models/` and calibration/run data under `/var/lib/mg400`.
- Keep files larger than 1,000,000 bytes out of Git, except generated `uv.lock`. Images must not exceed 300,000 bytes.
- Reject these file extensions: `.dll`, `.so`, `.dylib`, `.exe`, `.bin`, `.pt`, `.pth`, `.onnx`, `.engine`, `.npz`, `.h5`, `.zip`, `.stp`.

The initial `vision-weights` asset is `chip-arm-yolo26s-seg-v1.pt` in this repository's `vision-model-v1` release. Its size is 23,393,117 bytes. Its SHA-256 is `0b684c470191540b0cfc8802b0d13149b47d7be76b901ad1dd59882297407a07`. Install it at `/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt`. Its declared license is `AGPL-3.0` per Roboflow. Publishing the weights waits for the repository license decision.

## Consequences

Git retains small source files and reproducible asset metadata. Offline installation remains possible through verified local files. Asset availability and licensing remain explicit installation requirements.
