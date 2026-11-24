[English](README.en.md)

# MG400 控制

Dobot MG400 + 真空吸盘的真机控制项目。
真机可用交互终端或实验室控制台控制。
实验室控制台是唯一的操作网页，也控制 3 电机位移台、摄像头、炉子和显微镜。

## 连接外部智能体

先使用[外部智能体接入文档](docs/agent-connect/README.md)。由人明确提示编程智能体阅读项目知识和通信契约，再修改外部智能体的代码。完成适配后才运行。面向智能体的文件使用英文。

让编程智能体访问本仓库和外部智能体仓库，然后复制以下提示词：

```text
Adapt my external agent to this Lab Console in the external agent's own repository.
Read docs/agent-connect/project-knowledge.md first, then docs/agent-connect/communication-contract.md in the Console repository.
Use the first file for operating context and the second for the required communication behavior.
Include the relevant project rules in my agent's runtime instructions.
Read linked source files only when an implementation detail is unclear. Ask me for missing experimental goals or physical context.
Implement state reads, compatibility checks, pairing, operator control, commands, result tracking and independent Stop as documented.
Keep the Console authoritative. Do not change its code to fit my agent.
Complete adaptation and simulated checks before operation. This task does not authorize equipment commands or live Stop tests.
At runtime, check definitions and read current values. If the interface is unsupported, report it for human-directed adaptation instead of rewriting code during operation.
Report the changed files, supported interfaces, checks and remaining gaps. Never put credentials in source files or model prompts.
```

## 选择入口

按任务选择一篇操作指南：

| 我想做什么 | 入口与说明 |
| --- | --- |
| 在网页控制机械臂、吸盘和位移台，查看摄像头，操作炉子和显微镜 | [实验室控制台](docs/dashboard.md#实验室控制台)，打开 `http://100.64.0.1:8080/` |
| 了解机械臂和吸盘的控制、停止与恢复，配置机器人服务 | [机器人服务](docs/control-panel.md) |
| 了解位移台的控制和停止，配置位移台服务 | [位移台](docs/stage-panel.md) |

真机操作前，先阅读所选指南的前提条件和停止说明。

## 部署、排查与开发

| 任务 | 文档 |
| --- | --- |
| 安装、启动或更新服务，迁移到新主机 | [主机部署](docs/deploy.md) |
| 配置 Olares 网络、获取代码、查看 USB 设备布局，处理摄像头故障 | [Olares 配置与维护](docs/olares.md) |
| 查询机械臂当前报警或历史日志 | [报警与历史日志](docs/hardware.md#报警与历史日志) |
| 修改代码、运行检查、查阅实现细节 | [开发参考](docs/development.md) |
| 查阅真机 TCP 协议与动作完成判定 | [真机控制接口](docs/hardware.md) |

## 许可证

本仓库采用 [GNU Affero 通用公共许可证 v3.0](LICENSE)（AGPL-3.0），与视觉模型和 `ultralytics` 的许可证相同。随仓库提供的第三方文件保留各自的条款：`src/thermal_cam/_vendor/` 采用 Apache-2.0，`src/toupcam_camera/toupcam.py` 来自 ToupTek SDK。
