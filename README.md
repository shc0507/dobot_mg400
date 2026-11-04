[English](README.en.md)

# MG400 控制

Dobot MG400 + PGEA 电动夹爪的真机控制项目。
真机可用交互终端或实验室控制台控制。
实验室控制台是唯一的操作网页，也控制 3 电机位移台、摄像头、炉子和显微镜。

## 选择入口

按任务选择一篇操作指南：

| 我想做什么 | 入口与说明 |
| --- | --- |
| 在终端查看状态并输入命令控制 | [交互终端](docs/terminal-control.md)，运行 `uv run mg400-robot` |
| 在网页控制机械臂、夹爪和位移台，查看摄像头，操作炉子和显微镜 | [实验室控制台](docs/dashboard.md#实验室控制台)，打开 `http://100.64.0.1:8080/` |
| 了解机械臂和夹爪的控制、停止与恢复，配置机器人服务 | [机器人服务](docs/control-panel.md) |
| 了解位移台的控制和停止，配置位移台服务 | [位移台](docs/stage-panel.md) |

真机操作前，先阅读所选指南的前提条件和停止说明。

## 部署、排查与开发

| 任务 | 文档 |
| --- | --- |
| 安装、启动或更新服务，迁移到新主机 | [主机部署](docs/deploy.md) |
| 配置 Olares 网络、获取代码、处理 USB 夹爪和摄像头故障 | [Olares 配置与维护](docs/olares.md) |
| 查询机械臂当前报警或历史日志 | [报警与历史日志](docs/hardware.md#报警与历史日志) |
| 修改代码、运行检查、查阅实现细节 | [开发参考](docs/development.md) |
| 查阅真机 TCP 协议与动作完成判定 | [真机控制接口](docs/hardware.md) |

## 许可证

本仓库采用 [GNU Affero 通用公共许可证 v3.0](LICENSE)（AGPL-3.0），与视觉模型和 `ultralytics` 的许可证相同。随仓库提供的第三方文件保留各自的条款：`src/thermal_cam/_vendor/` 采用 Apache-2.0，`src/toupcam_camera/toupcam.py` 来自 ToupTek SDK。
