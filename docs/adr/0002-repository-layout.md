[English](0002-repository-layout.en.md)

# 0002：仓库布局与迁入代码

## 状态

已接受：2026-10-30。

## 背景

独立的 WuLab 应用重复维护共用代码。实验室必须统一维护跨设备配方的控制平台。

## 决定

本仓库维护迁入的 WuLab 代码，并保留仓库名称。机械臂软件包为 `mg400`。不得依赖源仓库。

| 路径 | 职责 |
| --- | --- |
| `src/mg400`, `src/stage_controller` | 机械臂核心、客户端、模拟器和位移台控制 |
| `src/labkit` | 共用访问检查、租约、事件、服务契约和 HTTP 客户端 |
| `src/vision`, `src/thermal_cam`, `src/furnace` | 定位、QR 读取、热成像和炉子及气体控制 |
| `src/toupcam_camera`, `src/cameras`, `src/microscope` | 共用 Toupcam 支持、UVC 摄像头和显微镜控制 |
| `src/orchestrator`, `src/gateway`, `src/analysis` | 配方与运行记录、智能体访问，以及后续图像指标与优化 |
| `apps/control_panel`, `recipes`, `config`, `deploy` | 机械臂页面、TOML 配方、配置和安装 |
| `firmware/three-motor-demo`, `firmware/microscope-axes` | 位移台和显微镜固件 |
| `docs`, `docs/adr`, `docs/agents`, `tests` | 人员指南、决定、智能体流程和离线测试 |

- 每个领域使用 1 个顶层软件包。将它加入 wheel 的 `packages` 列表。轻量依赖组加入 `default-groups`。`vision-gpu` 不加入默认组或 CI。
- 机械臂核心、机械臂面板及 `labkit.auth`、`labkit.lease`、`labkit.events`、`labkit.client` 只用标准库。
- 从固定的源提交迁入已批准文件及其测试。保留行为，使用软件包导入，并将主机路径与设备访问迁至 Linux。
- 按规定用无界面接口替代桌面 GUI 功能。Windows 二进制文件、模型权重、虚拟环境和硬件脚本不得进入测试。
- 导入模块时不得打开设备、启动线程或写文件。每个服务均提供 `--demo`。
- 在 `Source:` 提交尾注中记录源仓库、提交和路径。用 `Co-authored-by:` 尾注署名源作者。每个迁移 PR 列出未迁入文件及原因。

首个里程碑是视觉引导的芯片托盘槽位间转移。转移前后均用 QR 确认身份。

## 影响

实验室维护集成后的实现和共用测试。共用摄像头代码由 1 处维护，配方可在无硬件环境中演练。
