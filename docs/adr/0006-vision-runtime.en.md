[中文](0006-vision-runtime.md)

# 0006: Vision runtime and GPU use

## Status

Accepted: 2026-10-30.

## Context

Olares has an RTX 5090 with 32 GB. Other applications may share its GPU memory. The detector needs a compatible, isolated runtime.

## Decision

Run vision as its own process on Olares in `/opt/mg400/.venv-vision`. A separate Olares app remains a later option.

- The `vision-gpu` group contains `ultralytics>=8.4,<9`, `torch>=2.7`, and `torchvision>=0.22`. Resolve torch and torchvision from explicit index `pytorch-cu128` at `https://download.pytorch.org/whl/cu128`.
- Use PyTorch 2.7 or later built for CUDA 12.8. Install only `opencv-python-headless`, which supplies `cv2`. Override ultralytics' `opencv-python` dependency with `opencv-python; sys_platform == 'never'`.
- If uv rejects that override, stop and report the failure. The fallback is a separate uv project for vision. Keep `vision-gpu` outside default dependency groups. Default CI installs no torch.
- Enable installation through `MG400_OPTIONAL_SERVICES`. Use `UV_PROJECT_ENVIRONMENT=/opt/mg400/.venv-vision uv sync --locked --no-default-groups --group vision --group vision-gpu`. Install the verified `vision-weights` asset and create `/var/lib/mg400/vision`.
- Start `mg400-vision.service` with `/opt/mg400/.venv-vision/bin/python -m vision`. Default binding is loopback on port 8771.
- `MG400_VISION_DEVICE` defaults to `cuda` when available, otherwise `cpu`. Apply `MG400_VISION_GPU_FRACTION`, default 0.15, through `torch.cuda.set_per_process_memory_fraction` before loading the model.
- Perform 1 warm-up inference at startup and only 1 inference at a time. Demo mode uses fixture detections without torch. go2rtc remains the camera owner.
- Configure `MG400_VISION_BIND`, `MG400_VISION_PORT`, `MG400_VISION_TOKEN`, and `MG400_VISION_WEIGHTS`. `mg400ctl gpu` runs `nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv` as UID 1000.

## Consequences

The detector has compatible GPU packages without adding them to ordinary service environments or CI. Vision caps memory allocation before loading the model.
