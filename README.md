# MG400 Agent

Dobot MG400 + PGEA 电动夹爪的真机控制、离线仿真和 Python agent 项目。
真机可用网页或 Isaac Sim 面板控制。
MuJoCo 用于离线关节控制和轨迹回放。

## 选择入口

按任务选择一篇操作指南：

| 我想做什么 | 入口与说明 | 连接真机 | 大模型服务 |
| --- | --- | --- | --- |
| 用网页控制或查看机械臂和夹爪 | [网页面板](docs/control-panel.md)，不依赖 Isaac Sim | 连接 | 不需要 |
| 查看真机姿态，用面板或键盘控制 | [Isaac Sim](docs/isaacsim.md)，在 Olares 运行 | 连接 | 不需要 |
| 离线控制四轴或回放轨迹 | [MuJoCo](docs/mujoco.md) | 不连接 | 仅自然语言控制需要 |

自然语言真机入口仅提供 [J4 +2° 后回位的受限试验](docs/agent.md#自然语言-j4-受限试验)。
它不支持通用真机聊天或抓放。

不要将仿真坐标直接用于真机。
Isaac 姿态镜像不验证碰撞或夹持力，MuJoCo 也未验证真实抓放。
真机操作前，先阅读所选指南的前提条件和停止说明。

## 部署、排查与开发

| 任务 | 文档 |
| --- | --- |
| 配置 Olares、启动服务、更新代码、接入 USB 设备 | [Olares 部署与维护](docs/olares.md) |
| 查询机械臂当前报警或历史日志 | [报警与历史日志](docs/isaacsim.md#报警与历史日志) |
| 修改代码、运行检查、重建模型、查阅实现细节 | [开发参考](docs/development.md) |
| 查阅真机 TCP 协议与动作完成判定 | [真机控制接口](docs/hardware.md) |
