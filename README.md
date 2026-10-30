[English](README.en.md)

# MG400 控制

Dobot MG400 + PGEA 电动夹爪的真机控制项目。
真机可用交互终端或网页控制。
项目还包含 3 电机位移台的网页面板。

## 选择入口

按任务选择一篇操作指南：

| 我想做什么 | 入口与说明 |
| --- | --- |
| 在终端查看状态并输入命令控制 | [交互终端](docs/terminal-control.md)，运行 `uv run mg400-robot` |
| 用网页或键盘控制机械臂和夹爪，或只读查看 | [网页面板](docs/control-panel.md) |
| 用网页或键盘控制位移台的 3 个电机 | [位移台面板](docs/stage-panel.md) |

真机操作前，先阅读所选指南的前提条件和停止说明。

## 部署、排查与开发

| 任务 | 文档 |
| --- | --- |
| 安装、启动或更新服务，迁移到新主机 | [主机部署](docs/deploy.md) |
| 配置 Olares 网络、获取代码、处理 USB 夹爪和摄像头故障 | [Olares 配置与维护](docs/olares.md) |
| 查询机械臂当前报警或历史日志 | [报警与历史日志](docs/hardware.md#报警与历史日志) |
| 修改代码、运行检查、查阅实现细节 | [开发参考](docs/development.md) |
| 查阅真机 TCP 协议与动作完成判定 | [真机控制接口](docs/hardware.md) |
