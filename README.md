# MG400 控制

Dobot MG400 + PGEA 电动夹爪的真机控制项目。
真机可用交互终端或网页控制。

## 选择入口

按任务选择一篇操作指南：

| 我想做什么 | 入口与说明 |
| --- | --- |
| 在终端查看状态并输入命令控制 | [交互终端](docs/terminal-control.md)，运行 `uv run mg400-robot` |
| 用网页或键盘控制机械臂和夹爪，或只读查看 | [网页面板](docs/control-panel.md) |

真机操作前，先阅读所选指南的前提条件和停止说明。

## 部署、排查与开发

| 任务 | 文档 |
| --- | --- |
| 配置 Olares、启动服务、更新代码、接入 USB 设备 | [Olares 部署与维护](docs/olares.md) |
| 查询机械臂当前报警或历史日志 | [报警与历史日志](docs/hardware.md#报警与历史日志) |
| 修改代码、运行检查、查阅实现细节 | [开发参考](docs/development.md) |
| 查阅真机 TCP 协议与动作完成判定 | [真机控制接口](docs/hardware.md) |
