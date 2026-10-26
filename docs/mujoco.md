# MuJoCo 离线仿真

[返回首页，选择其他入口](../README.md)

本文说明如何运行自然语言关节控制和轨迹回放。
所有 MuJoCo 入口均不连接机械臂。
`ask` / `chat` 必须配置模型 API。
`state` 和轨迹回放不必配置 API key。

## 用途与限制

MuJoCo 提供运动学回放和关节动力学模式。
动力学模式包含重力、四轴执行器和理想连杆约束。
`mg400-agent` 默认使用 MuJoCo，支持四轴关节控制和持续对话。

此版本不提供末端坐标运动、逆运动学（IK）、夹爪开合或 `pick/place`。
场景不含桌面、积木或接触模型，也不提供抓取或释放反馈。
夹爪只有固定视觉外形，随法兰移动，不能验证避障、抓取或接触力。
场景禁用碰撞接触，关节限位检查不等于整条路径无碰撞。

## 运行环境

夹爪使用已打包的 PGEA 侧出线版外形，无需重新生成模型。
运行环境参考为 Python 3.12、MuJoCo 3.15.0 和 macOS。
依赖版本由 `uv.lock` 固定，模型随 Python 包提供。
Mac 的 `--viewer` 会自动使用官方启动器。
窗口启动实现见[运行环境与窗口](development.md#运行环境与窗口)。

## 自然语言关节控制

先按[模型 API 配置](agent.md#模型-api-配置)填写项目根目录的 `.env`。
以下命令通过 uv 加载 `.env`：

```bash
# 连续对话：同一窗口、同一物理状态，保留对话上下文。
uv run --env-file .env --extra llm --extra mujoco mg400-agent --backend mujoco --viewer chat

# 单次任务：执行后输出结果并关闭窗口。
uv run --env-file .env --extra llm --extra mujoco mg400-agent --backend mujoco --viewer ask "把第一轴转到20度，然后回到初始姿态"

# 读取初始模型状态：无需 API key。
uv run --extra mujoco mg400-agent --backend mujoco state
```

连续对话可依次输入“第一轴转到20度”“第一轴再增加10度”“读取当前关节角”“回到初始姿态”。
关节编号是控制器的 J1、J2、J3、J4，角度单位为度。
模型仅在会话创建时初始化到四轴零位，回位命令的目标为 `[0, 0, 0, 0]`。
受重力和伺服误差影响，返回的实际角度可能不恰好等于零。

等待终端输入或模型响应时，物理步进暂停，窗口继续刷新。
收到动作后，从保留的状态继续，所有动作串行执行。
各轴按关节角线性插值运动，速度不超过 15°/s。
沿途检查参考关节范围。
工具参数和到位标准见[自然语言关节执行](development.md#自然语言关节执行)。

## 轨迹回放

回放时必须用 `--trajectory` 指定轨迹文件。
文件格式见[自定义轨迹](#自定义轨迹)。
在项目根目录选择回放模式：

```bash
# 无窗口快速回放，输出完成状态和最终法兰位姿。
uv run --extra mujoco mg400-mujoco --trajectory path/to/trajectory.json

# 打开窗口，按实际时间播放指定轨迹。
uv run --extra mujoco mg400-mujoco --trajectory path/to/trajectory.json --viewer

# 四轴执行器跟踪同一条轨迹；省略 --viewer 可无窗口快速验证。
uv run --extra mujoco mg400-mujoco --trajectory path/to/trajectory.json --dynamics --viewer
```

默认采用运动学回放，直接设置模型关节角。
`--dynamics` 使用四轴执行器跟踪轨迹，并检查输入文件和插值路径的关节范围。
无窗口模式不等待真实时间，窗口模式完成后自动关闭。

## 自定义轨迹

将轨迹保存为 JSON，格式如下：

```json
[
  {"time_s": 0, "joints_deg": [0, 0, 0, 0]},
  {"time_s": 2, "joints_deg": [30, 20, 40, -30]},
  {"time_s": 4, "joints_deg": [0, 0, 0, 0]}
]
```

然后指定轨迹文件：

```bash
uv run --extra mujoco mg400-mujoco --trajectory path/to/trajectory.json --viewer
```

每帧的四个角度按控制器 J1、J2、J3、J4 排列，单位为度。
时间从 0 开始且严格递增，至少两帧，所有值必须是有限数字。
程序在启动 viewer 前检查整个文件。

默认以 30 Hz 在相邻关键帧之间做关节角线性插值。
`--rate 60` 可改变采样频率，最后一个关键帧始终只输出一次。
动力学模式下，`--rate` 只控制 viewer 刷新率。

角度按提供的数值插值，不自动选择跨 ±180° 的最短路径。
这是关节空间插值，不是直线 TCP 运动。
运动学回放不验证关节限位，动力学模式采用[参考关节范围](development.md#动力学执行与关节范围)。
两种轨迹回放模式都没有速度/加速度规划或碰撞检测。
记录真机角度后，可以转换成此格式离线复现。目前没有新增真机录制命令。

## 停止与故障处理

在 `chat` 中输入 `quit`、`exit`、`退出`，按 Ctrl+C 或关闭窗口可结束会话。
单次 `ask` 完成后关闭窗口。
调用 `stop` 会冻结当前会话，拒绝后续运动，但仍允许读取状态。

无效参数或越界目标会返回 `recoverable=true`，模型可纠正请求。
模型 API 或响应格式错误不冻结执行器，`chat` 可继续接受下一轮输入。
动作执行失败或执行结果未知会冻结当前会话。
程序不会自动重试动作，冻结后须退出并重新启动。

轨迹回放中，提前关闭窗口或按 Ctrl+C 会取消回放，退出码为 130。
成功退出码为 0，轨迹输入或运行错误为 1。
缺少 `--trajectory` 等命令用法错误的退出码为 2。
动力学模式发现关节越界、跟踪误差、连杆误差或数值异常时，会报错退出。
到位超时也按失败处理，不报告任务成功。
具体阈值和输出字段见[动力学完成标准与输出](development.md#动力学完成标准与输出)。

模型坐标系、夹爪安装和重建方法见[MuJoCo 模型与执行参考](development.md#mujoco-模型与执行参考)。
完整离线检查命令见[开发检查](development.md#开发检查)。
