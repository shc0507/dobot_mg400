# 开发参考

本页说明开发环境、代码分工、模型构建和控制接口。
运行现有功能时，从 [README](../README.md) 选择操作指南。
服务器与 USB 服务配置见 [Olares 部署与维护](olares.md)。


按修改内容查阅：

| 任务 | 章节 |
| --- | --- |
| 配置依赖并运行离线检查 | [开发检查](#开发检查) |
| 查找入口与模块 | [代码结构](#代码结构) |
| 编写机器人服务的客户端 | [机器人服务接口](#机器人服务接口) |
| 修改 USD 模型、真机映射或夹爪协议 | [Isaac 模型与控制接口](#isaac-模型与控制接口) |
| 修改动力学、模型坐标系或重建 MuJoCo 资产 | [MuJoCo 模型与执行参考](#mujoco-模型与执行参考) |

## 开发检查

安装 Python 3.11+、uv 和 Node.js 22+。
在项目根目录运行：

```bash
uv sync --all-extras --group usd
uv run --all-extras --group usd pytest -ra
node --test tests/*.mjs
uv run ruff check .
uv run ruff format --check .
```

测试不消耗模型额度，不连接机械臂，也不启动 Isaac Sim。
`pytest` 运行 Python 测试，`node --test` 运行摄像头预览、客户端和网页按键测试。
`--all-extras` 安装 MuJoCo 等可选依赖。
`--group usd` 安装 USD 检查依赖，包含 `pxr` 和 NumPy。
只运行 `uv run pytest` 时，缺少依赖的模型测试会跳过。
不能据此判断全部检查已通过，`-ra` 显示跳过原因。
离线检查不能替代服务器显示核对或真机验证。

使用 `examples/test.ipynb` 时，给同步命令加 `--group notebook`。
如需保留其他手工安装的包，再加 `--inexact`。
不要用项目依赖覆盖 Isaac Sim 的 Kit Python 环境。

## 代码结构

| 路径 | 用途 |
| --- | --- |
| `config/robot.toml` | 真机面板与姿态镜像的机器人地址 |
| `apps/control_panel/` | 独立 Python 网页服务和界面 |
| `apps/isaacsim/` | Isaac 姿态镜像、统一控制面板和模型检查入口 |
| `apps/layout-studio/` | 离线布局编辑器，使用模拟数据，不发送控制命令 |
| `config/camera_preview/` | 摄像头预览配置和 systemd 服务 |
| `isaacsim/`、`00_OPEN_MG400.usda` | USD 模型、工作场景和日常场景入口 |
| `src/mg400_agent/` | agent、后端、控制协议和反馈处理 |
| `scripts/` | 模型构建、Olares 启动、客户端操作和 USB 采集 |
| `examples/` | 单文件教学、受限 J4 试验和 notebook |
| `tests/` | Python 和 JavaScript 离线测试 |
| `downloads/` | 已纳入 Git 的 CAD 来源文件，夹爪构建脚本读取其中的 ZIP |
| `docs/` | 操作、部署和开发指南，`docs/agents/` 保存代理操作规程 |
| `work/` | 本地运行数据、备份和临时文件，不纳入 Git |

用浏览器打开 [布局编辑器](../apps/layout-studio/index.html)，可编辑并导出界面布局。
`MG400_DATA_DIR` 可指定运行数据目录，配置见 [代码与运行数据](olares.md#代码与运行数据)。
`downloads/` 是模型来源目录，不是下载缓存。

`cli.py` 提供 MuJoCo 的 `state`、`ask` 和 `chat` 命令。
`terminal_control.py` 提供 `mg400-robot` 真机交互终端。
它复用 `web_control.py` 的连接锁、反馈检查和动作执行，不启动 HTTP 服务。

`apps/control_panel/server.py` 是机器人服务，提供网页面板、HTTP 接口和 WebSocket 接口。
`websocket.py` 用标准库实现 WebSocket 帧编解码和连接写线程。
接口说明见 [机器人服务接口](#机器人服务接口)。

网页键盘点动由 `web_control.py` 调度，机械臂长按复用 `keyboard_jog.py`。
`web_keyboard.py` 校验按键输入并执行夹爪小步。
网页点动不使用 Isaac 的 `keyboard_control.py`。

`agent.py` 实现模型工具调用，API 配置见 [自然语言控制](agent.md#模型-api-配置)。
MuJoCo 通过关节工具执行动力学运动。
`mujoco_skills.py` 检查范围和到位状态。
`mujoco_session.py` 管理任务、连续对话和 viewer 生命周期。
`kinematics.py` 将控制器四轴映射到模型八关节。

真机面板通过 TCP 协议控制机械臂。
`controller_protocol.py` 解析命令应答，`feedback.py` 读取关节反馈。
`joint_control.py` 执行受限运动，`gripper_feedback.py` 处理夹爪反馈。
`runtime_paths.py` 管理 `MG400_DATA_DIR` 下的运行文件和备份。
TCP 协议参考与完成判定见 [真机控制接口](hardware.md)。
当前没有 ROS 2 后端或视觉感知。

## 机器人服务接口

机器人服务持有机械臂连接，同一机械臂只允许一个服务连接。
网页面板、脚本和程序都通过它读取状态和发送命令。
服务只使用 Python 标准库。

| 接口 | 用途 |
| --- | --- |
| `GET {前缀}api/state` | 读取一次状态 |
| `POST {前缀}api/command` | 发送一个命令 |
| `POST {前缀}api/heartbeat` | 续期控制租约 |
| `GET {前缀}api/ws` | WebSocket 连接：状态推送、命令、心跳和按键 |

`{前缀}` 是 `--base-path` 的值，默认为 `/`。
按键只能经 WebSocket 发送。

### 认证与客户端 ID

设置 `MG400_PANEL_TOKEN` 后，每个请求都必须带访问密钥。
HTTP 请求用 `Authorization: Bearer <密钥>` 发送密钥。
WebSocket 在 hello 消息中发送密钥。

未设置访问密钥时，服务只接受 `Host` 为 `127.0.0.1`、`localhost` 或 `[::1]` 的请求。
此限制阻止 DNS 重绑定（恶意网页把自己的域名解析到本机）访问面板。
此时服务也拒绝带 `Forwarded`、`X-Forwarded-For`、`X-Forwarded-Host` 或 `X-Real-IP` 的请求。
经反向代理访问时，必须设置访问密钥。

真机控制模式用客户端 ID 区分页面和程序，长度为 1–128 个可打印 ASCII 字符。
同一时刻只有一个客户端 ID 持有控制权。
控制者必须在 3 秒内续期控制租约，心跳和按键更新都可续期。
网页面板每 0.5 秒发送一次心跳。
服务 3 秒内未收到续期时，请求停止机械臂并锁定控制。

### HTTP 接口

POST 请求必须满足以下条件：

1. 带 `Content-Type: application/json` 和 `X-MG400-Panel: 1`。
2. 真机控制模式带 `X-MG400-Client: <客户端 ID>`。
3. 正文为 1–4096 字节的 JSON 对象，用 `Content-Length` 标明长度。

成功时返回 200 和状态对象。
拒绝时返回 400（请求无效）、403（无权）或 409（状态冲突），正文为 `{"error": "..."}`。

### WebSocket 接口

按以下顺序通信：

1. 连接后 3 秒内，发送 `{"type":"hello","protocol":1,"client":"<客户端 ID>","token":"<密钥>"}`。
2. 等待服务推送 `{"type":"state","state":{...}}`。
3. 发送请求 `{"type":"<类型>","id":<整数>,"body":{...}}`。
4. 按 `id` 匹配回复。

`protocol` 是消息格式版本，当前为 1。
未设置访问密钥时，`token` 可以为空字符串。
`state` 的字段与 `api/state` 相同。
状态变化时，服务推送状态，至少每秒一次，每次回复后也推送一次。

| 请求类型 | `body` |
| --- | --- |
| `command` | 与 `api/command` 的正文相同 |
| `heartbeat` | `{}` |
| `keys` | `{"seq":<序号>,"held":[<按键>]}` |

`id` 是 0 到 2^53 的整数，由客户端选择。
服务按请求顺序逐个回复，每个请求一个回复。
成功回复为 `{"type":"reply","id":<id>,"status":200,"state":{...}}`。
拒绝回复为 `{"type":"reply","id":<id>,"status":<状态码>,"error":"..."}`。
状态码含义同 HTTP 接口，500 表示服务内部错误。
`body` 无效时，服务只回复错误，连接保持打开。
`command` 的成功回复表示命令已受理，不表示动作已完成。
动作结束后，状态中的 `busy` 变为 `false`，`message` 给出结果。
服务日志为每个 `command` 记录一行，`keys` 和 `heartbeat` 不记录。

`keys` 的 `seq` 与键盘开关命令共用一个递增序号，服务忽略不大于上次值的消息。
`held` 列出当前按住的键，取值为 W、A、S、D、Q、E、R、F、Z、X。
按住按键时，客户端必须每 100 ms 重发一次。
服务 0.4 秒内未收到更新时，停止点动并关闭键盘控制。

服务主动关闭连接前，先发送 `{"type":"error","status":<状态码>,"error":"..."}`。

| 关闭码 | 含义 |
| --- | --- |
| 4400 | hello 或请求格式无效 |
| 4401 | 访问密钥错误 |
| 4426 | `protocol` 与服务不一致，客户端必须更新 |
| 4409 | 同一客户端 ID 的新连接替换了此连接 |
| 1013 | 已认证连接数已满 |
| 1001 | 服务停止，关闭前推送最后状态 |
| 1002、1003、1007、1009 | 帧格式错误、二进制消息、文本不是 UTF-8 或消息过长 |

连接规则如下：

- 消息只能是不超过 4096 字节的 UTF-8 文本。
- 浏览器连接时，`Origin` 必须与服务地址一致，程序可以省略 `Origin`。
- 服务每 5 秒发送 ping，15 秒未收到任何帧时关闭连接。
- 最多 8 个客户端 ID 同时保持已认证连接。
- 同一客户端 ID 再次发送 hello 时，服务关闭旧连接。
- 客户端的最新连接关闭或被替换时，服务立即关闭该客户端的键盘控制。

连接关闭不终止控制租约。
服务或代理关闭连接、客户端 3 秒内重连并续期时，保留控制权。
网络中断时，服务可能检测不到连接关闭，由按键超时和控制租约停止机械臂。

## Isaac 模型与控制接口

本节供模型构建和控制接口开发时查阅。
运行操作见 [Isaac Sim](isaacsim.md)，部署配置见 [Olares](olares.md)。
模型复用已校核的 CAD 网格、连杆尺寸、惯量和夹爪安装关系，直接导出原生 USD。
不要求 ROS、URDF/MJCF 导入扩展或在线资产转换器。

### 资产与入口

| 文件 | 用途 |
| --- | --- |
| `00_OPEN_MG400.usda` | 日常主入口：机械臂、可镜像夹指、地面、灯光和观察相机 |
| `scripts/start_isaac_olares.sh` | Olares 服务启动入口，复用现有 Isaac Sim 安装 |
| `apps/isaacsim/startup.py` | 打开默认场景，检查真机控制导入，记录代码与运行环境 |
| `isaacsim/workspace_scene.usda` | 真机镜像工作场景，直接引用基础机器人资产并停用物理场景 |
| `isaacsim/mg400_with_gripper.usd` | 含可动夹指、网格与材质的基础机器人资产，默认 prim 为 `/MG400` |
| `isaacsim/scene.usda` | 基础资产检查场景，含地面、灯光、相机和 PhysicsScene |
| `apps/isaacsim/load_model.py` | 开发时检查基础模型导入 |
| `apps/isaacsim/mirror.py` | 在当前 MG400 场景中显示真机四轴反馈，支持断线重连 |
| `apps/isaacsim/mirror_bootstrap.py` | 必要时打开默认场景，再启动只读姿态镜像 |
| `apps/isaacsim/joint_control.py` | 统一面板入口：实时同步、法兰坐标、键盘点动、使能、下使能、复位和夹爪连续开口 |
| `apps/isaacsim/gripper_control.py` | 统一面板复用的夹爪模块，也保留独立 I/O 开合入口 |
| `apps/isaacsim/olares_gripper.py` | 兼容旧入口，打开同一个统一控制面板 |
| `config/robot.toml` | Isaac 镜像、法兰面板和夹爪面板共用的机器人地址 |
| `scripts/gripper_feedback.py` | Olares 主机读取 USB 夹爪位置，写入共享测量文件 |
| `scripts/install_gripper_collector.py` | 在 Olares 主机安装夹爪采集的自动启动服务 |
| `scripts/isaac_gripper_feedback.mjs` | Mac 读取 USB 夹爪位置，经当前 WebRTC 会话转发到 Isaac |
| `apps/isaacsim/standalone.py` | 基础资产独立检查入口，使用 5.1 兼容 API，6.x 已弃用这些接口 |
| `scripts/build_isaac_model.py` | 从现有模型重建含可动夹指的基础 USD，无需启动 Isaac Sim |
| `scripts/build_isaac_workspace.py` | 重建默认镜像工作场景和主入口 |
| `isaacsim/LICENSE`、`NOTICE.txt` | 原始模型来源、许可证和模型限制 |

模型单位为米，Z 轴向上。机械臂的八个旋转关节与三个固定关节连接 11 个刚体。
PGEA 侧出线版、两个估算指尖和各参考坐标系都随法兰移动。
基础模型为夹爪壳体和两侧夹指提供独立变换，以及关节坐标和行程。
这些夹爪关节不参与物理解算。
夹爪不添加刚体、质量、驱动或碰撞，工作场景停用 PhysicsScene。
场景中的机器人为 `/World/MG400`，法兰为 `/World/MG400/mg400_end_effector_flange`。
观察相机为 `/World/Camera`。

### 四轴映射

| 控制器角度 | USD 关节角度                                                |
| ---------- | ----------------------------------------------------------- |
| J1         | `mg400_j1 = J1`                                             |
| J2         | `mg400_j2_1 = J2`、`mg400_j2_2 = J2`、`mg400_j3_2 = -J2`    |
| J3         | `mg400_j3_1 = J3-J2`、`mg400_j4_1 = -J3`、`mg400_j4_2 = J3` |
| J4         | `mg400_j5 = J4`                                             |

USD 旋转关节限位和 drive target 单位为度，Isaac articulation API 的角度为弧度。
脚本按名字匹配关节，不依赖 PhysX 内部排列。必须通过该四轴映射同时设置八关节。
单独拖动一个连杆关节不会自动维持平行连杆关系。

基础导入脚本 `load_model.py` 使用运动学姿态赋值，保留 CAD 惯量并用位置驱动保持姿态，
禁用机器人重力和碰撞。没有把 MuJoCo 的 tendon/equality 约束转换成 PhysX 闭环约束。
基础资产内含可独立移动的夹指，日常工作场景直接引用它来显示实测开口。
镜像只显示实测姿态和夹指位置，不验证碰撞、夹持力或真实连杆动力学。
`mg400_tcp` 仍是零工具偏移的法兰坐标系。估算指尖参考为法兰下方 104 mm，
不能将法兰坐标直接当作已标定的实际夹爪 TCP。

### 控制器关节范围

J1–J4 使用控制器 `QActual` / `GetAngle()` 的角度，单位为度。
J3 不减去 J2，也不改变符号或零点。
模型肘关节使用 `J3−J2`，不能用它替代控制器 J3 做范围检查。

[MG400 用户手册 V1.7 附录 A](https://download.dobot.cc/DobotStudio-pro/20231116/Dobot%20MG400%20User%20Guide%20V1.7_20231116_en.pdf)
给出的运动范围如下：

| 关节 | 厂家规格范围（度） |
| --- | --- |
| J1 | −160 … 160 |
| J2 | −25 … 85 |
| J3 | −25 … 105 |
| J4 | −360 … 360 |

关节目标范围向内保留 1.05° 余量。
当前位置和路径按原始范围检查。
当前位置在原始范围内时，可以从余量区移向允许的目标。
J4 使用厂家 ±360° 范围，目标允许范围为 ±358.95°。
角度使用控制器原始读数，不加减 360°。

条件 J2 下限和 `J3−J2` 检查保留
[HarvestX 参考模型的限制](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_common/include/mg400_common/kinematics.hpp)。
当 J3 > 0° 时，J2 原始下限为 −25°。
当 J3 ≤ 0° 时，下限收紧到 −19°。
目标扣除余量后，对应下限为 −23.95° 和 −17.95°。

`J3−J2` 是模型肘关节相对零位的转角，原始范围为 ±60°。
目标扣除余量后，差值范围为 ±58.95°。
例如 J2=20°、J3=90° 各自未越界，但差值为 70°，因此拒绝该组合。
这些检查不代表已读取本机控制器的限位配置。
正解成功只表示坐标计算成功，不证明目标符合运动范围。
未使能且无报警，也不证明当前姿态允许启动运动。

### 运动命令与坐标缓存

使用 Isaac 自带面板，控制链路在 Olares 内完成：

```text
Isaac 面板 XYZ（mm）/ R（度） → TCP 29999 / 30003 → MG400
Isaac 模型与 Actual 显示 ← TCP 30004 ← MG400 实测角度
```

控制链路无需 ROS 2 或额外桥接服务。

键盘按下和松开事件交给 `src/mg400_agent/keyboard_control.py`。
该模块保存当前按键，串行启动机械臂长按或夹爪小步。
按键变化或动作结束时，模块重新检查当前输入。
机械臂确认停止或夹爪当前小步完成后，才启动下一次动作。
`apps/isaacsim/joint_control.py` 检查面板权限并更新显示。
机械臂有限轨迹和停止确认由 `src/mg400_agent/keyboard_jog.py` 执行。

手动 `Execute targets` 的 `MovL` 使用 `SpeedL=1,AccL=1`。
键盘 W/S、R/F、Q/E 的 `MovL` 使用 `SpeedL=2,AccL=20`。
控制器逆解验证目标关节范围，控制器规划直线路径。
复位使用 `JointMovJ`，参数为 `SpeedJ=5,AccJ=5`。
键盘 A/D 的 J1 点动使用 `JointMovJ`，参数为 `SpeedJ=5,AccJ=20`。
两类关节动作检查整段关节范围，不包含现场碰撞检查。
机械臂运动不修改全局速度或工具配置。

A/D 启动和运行中只用实测关节反馈，不查询 `GetPose`。
动作结束后读取坐标，更新面板。
其余六个方向可复用同一控制实例读取的实测坐标。
复用时，测量不得超过 0.25 秒。
测量前后关节反馈还须与当前关节反馈一致。
不满足条件时，重新读取坐标。

动作期间输入和执行按钮禁用，网络操作在后台线程运行。
`Disable robot` 会取消当前动作并发送 `DisableRobot()`。
状态每 0.5 秒写入 `$MG400_DATA_DIR/isaacsim/control_status.json`。
其中包含连接状态、实测值、目标值和操作结果。
`mirror_status.json` 继续记录模型同步。

面板 API 参考：[NVIDIA omni.ui FloatField](https://docs.omniverse.nvidia.com/kit/docs/omni.ui/latest/omni.ui/omni.ui.FloatField.html)。
四轴协议参考：[Dobot 官方 SDK](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python/blob/main/dobot_api.py)。

### USB 夹爪协议

夹爪位置命令本身不会使能或移动机械臂。
初始化复用 USB–RS485 采集连接，不另开串口。
从站 1、115200、8N1，写一次 `0x0100=1`。
程序等待 `0x0200=1`、状态到位和有效实测位置。

位置命令写入反馈文件旁的 `.command.json` 文件。
采集进程只执行匹配当前会话且不超过 0.75 秒的请求，每个请求只执行一次。
位置命令要求采集进程报告 `control.protocol=2` 或 `3`。
初始化要求 `control.protocol=3`，请求使用 `operation=initialize` 且不含位置目标。
初始化完成的反馈为 `operation=initialize`、`target=null` 和 `ack=true`。

命令文件的 `deadline_monotonic` 表示单调时钟上的截止时间。
单调时钟用于计时，不随系统日期调整。
面板与采集进程必须位于同一 Olares 主机，并共享同一单调时钟域。
位置命令不支持跨主机传递，也不支持带时钟偏移的时间命名空间。
文件准备、采集接收、寄存器操作和到位确认共用命令时限。
位置命令为 5 秒，初始化为 15 秒。
面板在准备命令文件前开始计时，采集进程不得重新计时。

采集进程在每次串口请求前检查截止时间，包括模式写入和目标写入。
若请求已发送，采集进程仍按原有 1 秒应答时限接收并校验完整应答。
若此时已超时，采集进程停止本次命令，保留命令锁定，不再发送后续命令请求。
超时不会撤回已发送的目标，也不保证夹爪停止运动。

发送前，命令记录在反馈文件旁的 `.command-lock.json` 文件中。
写应答和目标寄存器回读在截止时间前成功后，采集进程删除该记录。
重启时若记录仍存在，采集进程保持锁定，只读取反馈。

真实位置 1000 对应全开，0 对应每侧夹指内收 5 mm。
代码只更新两个夹指的 SessionLayer 变换，不启动物理仿真。
重复包、旧序号和已结束会话的包会被忽略，不清除新鲜样本，也不延长其有效期。
同一文件重复读取不会延长测量值的有效期。
统一入口把 `$MG400_DATA_DIR/work/gripper_olares_feedback.json` 传给夹爪行。

夹爪状态寄存器 `0x0201` 的 `3` 表示物体掉落。
状态定义见 [PGEA 用户手册表 2.9](https://www.manualslib.com/manual/4358849/Dh-Robotics-Technology-Pgea-Series.html?page=17)。

### Mac WebRTC 反馈接口

Mac 脚本以 20 Hz 读取初始化状态、运动状态和实际位置（`0x0200` 至 `0x0202`）。

转发脚本适配 Streaming Client 2.0.0 内置的 SDK 3.7.0。
启动时使用临时回环调试接口连接现有 SDK，配置结束即关闭接口。
客户端升级后须重新核对接口，脚本会拒绝其他版本。
消息格式参考 [NVIDIA AppStreamer](https://docs.omniverse.nvidia.com/ov-web-sdk/latest/web-streaming-library/classes/AppStreamer.html)
和 [Kit 消息扩展](https://docs.omniverse.nvidia.com/kit/docs/omni.kit.livestream.messaging/latest/Overview.html)。

### 重建与验证

构建前，将自定义场景另存到数据目录。
重建会覆盖默认模型、场景和主入口。
在本项目根目录依次运行，复用 `uv.lock` 锁定的 USD 构建依赖：

```sh
uv run --group usd python scripts/build_isaac_model.py
uv run --group usd python scripts/build_isaac_workspace.py
```

这些命令安装 `usd` 依赖组，不要求安装或启动 Isaac Sim 运行环境。
先构建含可动夹指的基础模型，再构建工作场景。

全部离线检查命令见 [开发检查](#开发检查)。

开发时可在 Isaac Script Editor 执行 `apps/isaacsim/load_model.py`，检查基础资产导入。
该脚本先备份当前场景，再打开 `isaacsim/scene.usda` 并启动 PhysX 校验。
结果写入 `$MG400_DATA_DIR/isaacsim/isaac_sim_validation.json`。
只有 `success: true` 表示本项检查通过。
它检查关节赋值和回读的一致性，不测量动力学跟踪精度或真机精度。
独立检查入口 `apps/isaacsim/standalone.py` 必须由 Isaac Sim 自带 Python 启动。

## MuJoCo 模型与执行参考

日常操作见 [MuJoCo 离线仿真](mujoco.md)。本节供修改执行器、坐标系或模型资产时查阅。

### 运行环境与窗口

模型包含在 Python 包中，安装后无需依赖参考仓库。

Mac 的 passive viewer 必须使用官方 `mjpython` 启动器。
本项目的 `--viewer` 自动重启到该启动器。
子进程的 `DYLD_FALLBACK_LIBRARY_PATH` 会加入当前 Python 的 `LIBDIR`。
这可修复 uv 虚拟环境中找不到 `libpython` 的问题，不修改 shell 配置或系统目录。
参考：[官方 viewer 文档](https://mujoco.readthedocs.io/en/stable/python.html#passive-viewer)、
[上游 uv 问题记录](https://github.com/google-deepmind/mujoco/issues/1923)。

### 自然语言关节执行

模型只选择工具，不直接写 `qpos` 或控制物理步进。
关节编号采用控制器的 J1、J2、J3、J4，执行层把它们映射到八个连杆关节。

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `get_robot_state` | 无 | 读取实际关节角、目标和执行状态 |
| `move_joint` | `joint`：J1…J4。`angle_deg`。`relative` | 单轴绝对目标，或相对当前实际角度增加指定角度。其余轴保持原目标 |
| `move_joints` | `joints_deg`：四个角度 | 设置四轴绝对目标 |
| `home` | 无 | 运动回 `[0, 0, 0, 0]` 目标 |
| `stop` | 无 | 冻结当前会话，拒绝后续运动 |

模型仅在会话创建时初始化到四轴零位。
后续移动和回位均更新执行器目标并调用 `mj_step()`，不重置物理状态。
终点使用下述 0.25° / 0.5°/s 到位标准。

### 动力学执行与关节范围

`--dynamics` 使用单独的 `dynamics.xml`。
初始化到轨迹首帧后，只更新四轴执行器目标。
程序调用 `mj_step()` 推进物理，不再按目标改写关节位置或速度。
输入文件和插值路径会在创建模型及打开窗口前完成范围检查。

物理和控制周期固定为 2 ms（500 Hz）。
此模式下，`--rate` 只控制 viewer 刷新率。
第三轴通过 fixed tendon 的线性组合 `j2_1+j3_1` 控制。
四个等式约束维持平行连杆关系。
模型开启重力，采用 `implicitfast` 积分。
执行器增益、力矩上限、阻尼和附加惯量是仿真调试参数。
数值及来源见模型 `NOTICE.txt`，它们不代表实际 MG400 伺服响应。

J4 采用 MG400 用户手册的 ±360° 范围。
其他范围来自固定版本 HarvestX `kinematics.hpp`，尚未与本机控制器校准。
指令在范围内向内保留 1.05° 余量。
运动起点和路径按原始范围检查，允许从余量区移向有效目标。
每个物理步进后，程序检查实际四轴角度是否仍在原始参考范围内。

| 角度 | 原始参考范围（度） | 指令允许范围（度） |
| --- | --- | --- |
| J1 | −160 … 160 | −158.95 … 158.95 |
| J4 | −360 … 360 | −358.95 … 358.95 |
| J2，J3 > 0 | −25 … 85 | −23.95 … 83.95 |
| J2，J3 ≤ 0 | −19 … 85 | −17.95 … 83.95 |
| J3 | −25 … 105 | −23.95 … 103.95 |
| J3 − J2 | −60 … 60 | −58.95 … 58.95 |

路径检查包括跨越 J3=0 的位置。只检查关键帧会漏掉这一条件边界。
角度不会加减 360°，例如 J4=280° 保持为 280°。
MJCF 中的范围是软约束，Python 状态检查负责在测量值越界时退出。

### 动力学完成标准与输出

轨迹结束后，四轴实际误差均需不超过 0.25°，最大轴速度不超过 0.5°/s。
此状态须连续保持 0.1 个仿真秒，最多再等待 2 个仿真秒。
以下情况明确报错退出，不报告任务成功：

- 指令或实际关节状态超出参考范围。
- 任一轴跟踪误差超过 5°（通常需要放慢轨迹）。
- 连杆关系的最大误差超过 0.1°。
- MuJoCo 数值警告、非有限状态，或最终到位超时。

这些数值是当前仿真验收标准。
`joints_deg` 来自实际 `qpos`，`target_joints_deg` 是目标。
`tracking_error_deg` 和 `linkage_error_deg` 分别报告两者差距和连杆关系偏差。
`peak_*` 给出整次运行的最大误差。
`simulation_time_s` 包含最终稳定时间。
轨迹时长若不是 2 ms 的整数倍，会向上取整到下一个物理步，误差小于 2 ms。

### PGEA-2-10 夹爪安装

两份 MuJoCo 场景采用用户提供的实物照片所示布局。
夹爪位于原法兰正下方，侧面出线，前端装有黑色加长夹指。
模型使用原法兰 `mg400_end_effector_flange` / `flange_0.stl`。
装配中没有蓝色 L 形支架或侧向偏置。

本体使用 PGEA-2-10/15-10 O-S CAD。
配套图纸给出的 O-S 长度为 89 mm，侧面出线，符合照片所示布局。
包装铭牌标注 94 mm，与 O-B 底部出线版一致。
模型按实物照片选择几何外形，包装与实际夹爪的版本对应关系尚未确认。
STL 使用米制，分别保留本体、银色前壳、左右标准滑块和侧面线缆。

当前安装定义如下（均相对于原法兰，+Z 朝上）：

- 后盖中心为 `[0, 0, 0]` mm，与原法兰底面同轴贴合。未单独增加转接片厚度。
- 夹爪长轴朝 -Z，绕 Z 转 90°，让侧出线朝法兰 -X（J4=0 时朝机器人后方）。
  照片不能确定精确的末端旋转零位，这个绕 Z 方向仍为装配假设。
- `pgea_jaw_center`：原 CAD 标准滑块前端中点，位于 `[0, 0, -89]` mm。
- 两个黑色指尖是按照片补充的简化长方体，各 10 × 9 × 15 mm，保持 CAD 当前的开口。
  不是厂家指尖 CAD，长度和截面均为估算值。
- `pgea_tip`：估算指尖末端中点，位于 `[0, 0, -104]` mm。
- `mg400_tcp` 与原输出 `tool_offset_mm=[0,0,0]` 仍保留法兰零工具参考定义，
  没有把估算的夹指长度作为标定后的工具 TCP。

夹爪仍是固定视觉装配，用于外形与运动跟随检查。未计入新增负载惯量，
未添加开合控制或接触几何。拿到实测指尖长度及安装尺寸后，再修正几何和工具 TCP。

先转换夹爪 CAD（仅重建需要 CADQuery，运行时不需要）：

```bash
uv run --no-project --with cadquery==2.8.0 python scripts/build_gripper_mesh.py
```

随后使用下述模型重建命令，或直接运行已打包的模型查看。

### 模型和坐标系

资产来自 HarvestX/MG400_ROS2，固定提交为
`1a8c7344be44c52030f7216c0f387873ed91fd11`。
DAE 网格应用原有场景变换后转换为 STL，保留材质颜色。
MJCF 保留原 URDF 的关节树、轴、局部平移及惯量。
许可证、来源和修改记录随模型打包在
`src/mg400_agent/assets/mg400/LICENSE` 和 `NOTICE.txt`。

MuJoCo 内部使用米、弧度。CLI 输入关节角为度，输出位置为毫米。

| 输出 / site | 含义 |
| --- | --- |
| `mg400_base` | 固定底座，和当前 world 重合 |
| `mg400_controller_origin` | 原 URDF 的 origin，位于底座的 `[-5, 0, 224]` mm，轴平行。未经真机标定 |
| `mg400_flange` | 原 URDF 的末端法兰坐标系 |
| `mg400_tcp` | 当前与法兰重合，尚未配置夹爪长度 |
| `pgea_jaw_center` | 标准滑块前端中点，法兰下方 89 mm |
| `pgea_tip` | 估算指尖末端中点，法兰下方 104 mm，不是标定后的真机 TCP |
| `flange_in_base_mm` | 法兰在底座坐标系中的位置 |
| `flange_in_urdf_origin_mm` | 法兰在上述 URDF origin 中的位置 |
| `flange_yaw_deg` | 实际法兰 yaw。运动学模式等于 J1+J4 折返到 ±180°，动力学模式包含约束残差 |

运动学模式沿用现有镜像映射，按名称设置八个模型关节，然后调用 `mj_forward()`。
此模式不调用 `mj_step()`，`scene.xml` 禁用了重力和接触，没有执行器或动力学约束。
动力学模式使用上文的 `dynamics.xml`，仅在初始化时设置一次耦合姿态。
两份模型的关节存储顺序都与显示名称顺序不同，不能直接把八个数顺序赋给 `data.qpos`。

参考仓库的简化解析 FK 与 CAD URDF 并不完全相同。在四轴全零时：

| 定义 | 法兰位置（mm） |
| --- | --- |
| CAD，底座坐标系 | `[278.448743, -0.000124, 353.251164]` |
| CAD，URDF origin 坐标系 | `[283.448743, -0.000124, 129.251164]` |
| 参考简化解析 FK，origin 坐标系 | `[284.5, 0, 122]` |

后两者相差约 7.327 mm，差异随姿态变化，不能用固定平移消除。
这些坐标不代表已经标定的真机 TCP。

### MuJoCo 检查与重建

只检查 MuJoCo 与基础功能时，运行：

```bash
uv run --extra mujoco pytest
```

未安装 MuJoCo 时，依赖真实模型的测试会跳过，其余项目测试仍可运行。
完整离线检查命令见 [开发检查](#开发检查)。

重建模型时，本地参考 Git 仓库必须保留上述提交，支持 sparse checkout。
构建依赖和运行依赖分开：

```bash
uv run --no-project --with pycollada==0.9.3 --with xacro==2.1.1 \
  python scripts/build_mujoco_model.py
```

脚本重建固定来源的两份模型、网格、许可证和来源说明，保留自定义轨迹。
夹爪网格由 `build_gripper_mesh.py` 单独生成。指定其他输出目录时，模型重建脚本会复制
已转换的五个夹爪 STL，并保持安装结构。

`MuJoCoArm` 的末端坐标运动和 IK 尚未实现，也尚未接入抓放任务层。
仿真物体位置与持物状态尚未接入任务层。
抓放场景的不可达、碰撞、夹空、掉落和超时回归测试仍待补充。
坐标、参数和任务验收精度尚未用真机与实际夹爪标定。
