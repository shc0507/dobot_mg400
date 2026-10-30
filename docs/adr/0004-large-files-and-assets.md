[English](0004-large-files-and-assets.en.md)

# 0004：大文件与资源

## 状态

已接受：2026-10-30。

## 背景

模型权重和本地库必须可用，二进制资源和大型数据不得存入 Git。

## 决定

模型权重发布为 GitHub Release 资源。每项安装资源在 `config/assets.toml` 中声明 `url`、`sha256`、`bytes`、`install`、`mode`、`service`、`source` 和 `license`。`install` 字段使用 `/var/lib/mg400/` 下的目标绝对路径。

- 安装器下载到目标目录的临时文件。校验字节数和 SHA-256 后，原子替换目标文件，并设置所有者和权限。拒绝符号链接。
- `--fetch-assets` 下载资源。`--asset NAME=PATH` 安装本地文件并执行相同校验。试运行列出计划操作。`mg400ctl assets` 报告资源存在、缺失或不匹配。
- 仅安装已启用服务的资源。模型权重存于 `/var/lib/mg400/models/`。校准与运行数据存于 `/var/lib/mg400`。
- 超过 1,000,000 字节的文件不得进入 Git，生成的 `uv.lock` 除外。图片不得超过 300,000 字节。
- 拒绝这些扩展名：`.dll`、`.so`、`.dylib`、`.exe`、`.bin`、`.pt`、`.pth`、`.onnx`、`.engine`、`.npz`、`.h5`、`.zip`、`.stp`。

首个 `vision-weights` 资源是本仓库 `vision-model-v1` 发布中的 `chip-arm-yolo26s-seg-v1.pt`。大小为 23,393,117 字节。SHA-256 为 `0b684c470191540b0cfc8802b0d13149b47d7be76b901ad1dd59882297407a07`。安装路径为 `/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt`。Roboflow 声明的许可证为 `AGPL-3.0`。权重发布必须等待仓库许可证决定。

## 影响

Git 保留小型源文件和可复现的资源元数据。经过校验的本地文件支持离线安装。资源可用性与许可证均为明确的安装要求。
