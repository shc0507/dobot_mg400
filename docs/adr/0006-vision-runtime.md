[English](0006-vision-runtime.en.md)

# 0006：视觉运行环境与 GPU 使用

## 状态

已接受：2026-10-30。

## 背景

Olares 配备 32 GB 的 RTX 5090。其他应用可能共用 GPU 显存。检测器必须使用兼容且独立的运行环境。

## 决定

视觉在 Olares 上以独立进程运行，使用 `/opt/mg400/.venv-vision`。以后可以改为独立的 Olares 应用。

- `vision-gpu` 组包含 `ultralytics>=8.4,<9`、`torch>=2.7` 和 `torchvision>=0.22`。torch 与 torchvision 使用显式索引 `pytorch-cu128`，地址为 `https://download.pytorch.org/whl/cu128`。
- 使用为 CUDA 12.8 构建的 PyTorch 2.7 或更高版本。仅安装提供 `cv2` 的 `opencv-python-headless`。用 `opencv-python; sys_platform == 'never'` 覆盖 ultralytics 的 `opencv-python` 依赖。
- 如果 uv 拒绝该覆盖规则，停止并报告失败。后备方案是独立的 uv 视觉项目。`vision-gpu` 不加入默认依赖组。默认 CI 不安装 torch。
- 通过 `MG400_OPTIONAL_SERVICES` 启用安装。使用 `UV_PROJECT_ENVIRONMENT=/opt/mg400/.venv-vision uv sync --locked --no-default-groups --group vision --group vision-gpu`。安装经过校验的 `vision-weights`，并创建 `/var/lib/mg400/vision`。
- `mg400-vision.service` 使用 `/opt/mg400/.venv-vision/bin/python -m vision` 启动。默认绑定回环地址，端口为 8771。
- `MG400_VISION_DEVICE` 在可用时默认为 `cuda`，否则为 `cpu`。加载模型前，通过 `torch.cuda.set_per_process_memory_fraction` 应用 `MG400_VISION_GPU_FRACTION`，默认值为 0.15。
- 启动时执行 1 次预热推理，同时只执行 1 次推理。演示模式使用测试检测结果，不要求 torch。go2rtc 继续管理摄像头。
- 配置 `MG400_VISION_BIND`、`MG400_VISION_PORT`、`MG400_VISION_TOKEN` 和 `MG400_VISION_WEIGHTS`。`mg400ctl gpu` 以 UID 1000 执行 `nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv`。

## 影响

检测器使用兼容的 GPU 软件包，普通服务环境和 CI 不增加这些依赖。模型加载前限制显存分配。
