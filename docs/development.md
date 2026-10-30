[English](development.en.md)

# 开发参考

本页说明开发环境、代码分工和控制接口。
运行现有功能时，从 [README](../README.md) 选择操作指南。
服务的安装与配置见 [主机部署](deploy.md)，Olares 特有的设置见 [Olares 配置与维护](olares.md)。


按修改内容查阅：

| 任务 | 章节 |
| --- | --- |
| 配置依赖并运行离线检查 | [开发检查](#开发检查) |
| 查找入口与模块 | [代码结构](#代码结构) |
| 编写机器人服务的客户端 | [机器人服务接口](#机器人服务接口) |
| 编写位移台面板的客户端 | [位移台面板接口](#位移台面板接口) |
| 修改关节范围、运动参数或夹爪协议 | [真机控制参考](#真机控制参考) |

## 开发检查

安装 Python 3.12+、uv 和 Node.js 22+。
在项目根目录运行：

```bash
uv sync
uv run pytest
node --test tests/*.mjs
uv run ruff check .
uv run ruff format --check .
```

`uv sync` 默认安装 `dev` 和 `stage` 依赖组。
`stage` 组含位移台面板使用的 Flask、waitress 和 pySerial。
机械臂的网页服务和交互终端只用标准库，不依赖 `stage` 组。

测试不连接机械臂或 Arduino。
拉取请求和推送到 `main` 时，GitHub Actions（`.github/workflows/ci.yml`）在 Python 3.12 上运行以上检查。
`pytest` 运行 Python 测试，`node --test` 运行摄像头预览和网页按键测试。
离线检查不能替代真机验证。

测试共用的 fixture 位于 `tests/conftest.py`。
位移台测试的 `stage_rig`、`stage_ports` 和 `stage_usb` 也在此文件，测试按名称引用。
模拟控制器、连接和网页服务的测试替身（代替真实设备的测试对象）位于 `tests/helpers.py`、`tests/sim_mg400.py` 和 `tests/panel_ws.py`。
位移台的时钟、串口和串口列表替身位于 `tests/stage_helpers.py`。
新增测试时复用这些模块，不得在测试文件中另写副本。

使用 `examples/test.ipynb` 时，给同步命令加 `--group notebook`。
如需保留其他手工安装的包，再加 `--inexact`。

## 离线机器人模拟器

`mg400.sim` 在回环地址模拟控制器和夹爪。
它只接受数字回环地址，默认使用 `127.0.0.2`。
控制器端口为 `29999`、`30003` 和 `30004`，反馈周期为 `8 ms`。
启动后，机械臂处于下使能状态，夹爪已初始化。

在项目根目录运行：

```bash
sim_dir=$(mktemp -d)
uv run python -m mg400.sim --host 127.0.0.2 --gripper-feedback "$sim_dir/gripper.json" &
sim_pid=$!
MG400_CONTROLLER_LOCK_DIR="$sim_dir/locks" uv run python apps/control_panel/server.py \
  --bind 127.0.0.1 --port 18766 --robot-host 127.0.0.2 \
  --gripper-feedback "$sim_dir/gripper.json"
kill "$sim_pid"
wait "$sim_pid"
```

打开 `http://127.0.0.1:18766/`，按以下顺序操作：

1. 点击 `Connect & take control`，等待反馈更新。
2. 点击 `Enable robot`，等待使能完成。
3. 点击 `Use actual`，填写目标坐标，再点击 `Execute targets`。
4. 发送夹爪目标，确认反馈位置更新。
5. 点击 `Disconnect`，等待停止确认。

在终端按 `Ctrl+C` 结束网页服务。
随后两条命令结束本次模拟器进程。
每次启动使用新目录。模拟器拒绝覆盖已有的反馈文件、命令文件或命令锁。

模拟器立即到达关节和笛卡尔目标，不计算真实运动学、路径、碰撞或夹持力。
逆解返回当前关节值。`J1` 点动改变关节反馈，其余点动只改变运动模式。
这些限制只适用于模拟器，生产控制代码和停止确认规则保持不变。
离线演练不能替代硬件验收。

## 代码结构

| 路径 | 用途 |
| --- | --- |
| `config/robot.toml` | 机器人地址，`MG400_ROBOT_HOST` 未设置时使用 |
| `apps/control_panel/` | 独立 Python 网页服务和界面 |
| `apps/layout-studio/` | 离线布局编辑器，使用模拟数据，不发送控制命令 |
| `config/systemd/` | 所有服务的 systemd 定义，配置从 `/etc/mg400/mg400.env` 读取 |
| `deploy/` | 安装器 `install.py`、日常命令 `mg400ctl` 和配置示例 |
| `src/mg400/` | 后端、控制协议和反馈处理 |
| `src/mg400/sim/` | 回环控制器和文件夹爪模拟器，共用测试模型 |
| `src/stage_controller/` | 位移台面板：Flask 服务、串口协议和界面 |
| `firmware/three-motor-demo/` | 位移台的 Arduino 固件和接线说明（英文） |
| `scripts/` | 摄像头预览、控制模式切换和 USB 采集 |
| `examples/` | 单文件教学和 notebook |
| `tests/` | Python 和 JavaScript 离线测试 |
| `docs/` | 操作、部署和开发指南，`docs/agents/` 保存代理操作规程 |
| `work/` | 本地运行数据、备份和临时文件，不纳入 Git |

用浏览器打开 [布局编辑器](../apps/layout-studio/index.html)，可编辑并导出界面布局。

`terminal_control.py` 提供 `mg400-robot` 真机交互终端。
它复用 `web_control.py` 的连接锁、反馈检查和动作执行，不启动 HTTP 服务。

`apps/control_panel/server.py` 是机器人服务，提供网页面板、HTTP 接口和 WebSocket 接口。
`websocket.py` 用标准库实现 WebSocket 帧编解码和连接写线程。
接口说明见 [机器人服务接口](#机器人服务接口)。

网页键盘点动由 `web_control.py` 调度，机械臂长按复用 `keyboard_jog.py`。
`web_keyboard.py` 校验按键输入并执行夹爪小步。

真机面板通过 TCP 协议控制机械臂。
`controller_protocol.py` 解析命令应答，`feedback.py` 读取关节反馈。
`joint_control.py` 执行受限运动，`gripper_feedback.py` 处理夹爪反馈。
TCP 协议参考与完成判定见 [真机控制接口](hardware.md)。
当前没有 ROS 2 后端或视觉感知。

`src/stage_controller/` 是位移台面板，不导入 `mg400`，也不使用机械臂的连接锁。
`__main__.py` 读取 `MG400_STAGE_*` 环境变量和命令行参数，再启动 waitress。
收到 SIGINT、SIGTERM 或 SIGHUP 时，它先停止电机并断开串口，再退出。
`app.py` 提供 HTTP 接口和访问检查。
`controller.py` 管理串口连接、冷却、长按和活动日志。
`protocol.py` 实现 Arduino 串口协议和演示模式。
所有串口命令都在 `MotorController` 的锁内执行，一次移动的命令不与其他请求交错。
修改串口命令时，同时修改固件和 `DemoTransport`。
接口说明见 [位移台面板接口](#位移台面板接口)。

## 机器人服务接口

机器人服务执行 `start_sync` 后连接机械臂，并持有连接锁。
网页面板和自写客户端通过服务读取状态和发送命令。
`mg400-robot` 和 `scripts/mg400_control_mode.py` 直接连接机械臂。
它们与服务共用连接锁，不能与服务同时连接。
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
控制者必须在 3 秒内续期控制租约（控制权的有效期）。
心跳和按键更新都能续期。
网页面板每 0.5 秒发送一次心跳。
服务 3 秒内未收到续期时，请求停止机械臂并锁定控制。

### HTTP 接口

POST 请求必须满足以下条件：

1. 带 `Content-Type: application/json` 和 `X-MG400-Panel: 1`。
2. 真机控制模式带 `X-MG400-Client: <客户端 ID>`。
3. 正文为 1–4096 字节的 JSON 对象，用 `Content-Length` 标明长度。

成功时返回 200 和状态对象。
拒绝时返回 400（请求无效）、401（访问密钥错误）、403（无权）或 409（状态冲突）。
拒绝的正文为 `{"error": "..."}`。

命令正文为 `{"action": "<命令>", ...}`，不得包含其他字段：

| 命令 | 其他字段 | 行为 |
| --- | --- | --- |
| `start_sync` | 无 | 连接机械臂并取得控制权 |
| `stop_sync` | 无 | 停止机械臂，确认后断开 |
| `enable` | 无 | 使能机械臂 |
| `disable` | 无 | 下使能机械臂，任何客户端都可发送 |
| `stop` | 无 | 停止机械臂，任何客户端都可发送 |
| `home` | 无 | J1–J4 移动到 0° |
| `execute` | `targets`：`x`、`y`、`z`、`r`，可选 `grip` | 直线移动到法兰坐标，到位后执行夹爪目标 |
| `grip` | `position`：0–1000 的整数 | 设置夹爪开口 |
| `initialize_gripper` | 无 | 初始化夹爪，会张开夹指 |
| `keyboard` | `enabled`、`seq`，开启时加 `speed`，关闭时加 `reason` | 开启或关闭键盘控制 |
| `clear_alarm` | 无 | 发送 `ClearError()`，报警清除后下使能并确认静止 |

`speed` 是 1–100 的整数。
`clear_alarm` 在控制器报警且反馈新鲜时可用。
`recoverable` 为 `true` 时也可用：报警期间的停止未确认已锁定控制，报警已在面板外清除。
此时服务不发送 `ClearError()`，只下使能并确认静止。
状态对象的 `alarms` 列出当前报警，无报警时为 `null`。
每项含 `id`、`source`（`controller` 或 `J1`–`J4`）、`description` 和 `solution`。
`reason` 取值为 `user`、`focus`、`edit`、`speed` 或 `modifier`。

### WebSocket 接口

按以下顺序通信：

1. 连接后 3 秒内，发送 `{"type":"hello","protocol":1,"client":"<客户端 ID>","token":"<密钥>"}`。
2. 等待服务推送 `{"type":"state","state":{...}}`。
3. 发送请求 `{"type":"<类型>","id":<整数>,"body":{...}}`。
4. 按 `id` 匹配回复。

`protocol` 是消息格式版本，当前为 1。
未设置访问密钥时，`token` 可以为空字符串。
`state` 的字段与 `api/state` 相同。
状态变化时，服务推送状态。
每次回复后，服务也推送一次。
服务至少每秒推送一次。

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

关闭码为 4400、4401、4426 或 1013 时，服务先发送 `{"type":"error","status":<状态码>,"error":"..."}`，再关闭连接。

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
- 客户端的最新连接关闭，或同一客户端 ID 的新连接替换它时，服务立即关闭该客户端的键盘控制。

连接关闭不终止控制租约。
服务或代理关闭连接、客户端 3 秒内重连并续期时，保留控制权。
网络中断时，服务可能检测不到连接关闭，由按键超时和控制租约停止机械臂。

## 位移台面板接口

位移台面板的 HTTP 接口使用 JSON。
服务每 0.2 秒查询一次 Arduino 状态，并在查询时停止超时的长按。

| 接口 | 正文字段 | 用途 |
| --- | --- | --- |
| `GET /api/ports` | 无 | 列出可连接的串口，不含夹爪转换器。设置 `MG400_STAGE_SERIAL` 时只列出该设备 |
| `GET /api/state` | 无 | 读取连接、电机、细分模式和活动日志 |
| `POST /api/connect` | `port` | 连接 `api/ports` 列出的串口，演示模式只接受 `DEMO` |
| `POST /api/disconnect` | 无 | 发送 `stop all`，确认后断开 |
| `POST /api/stop` | 无 | 停止所有电机 |
| `POST /api/microstep` | `microstep`：1、2、4、8 或 16 | 所有电机静止时设置细分模式 |
| `POST /api/motors/<电机>/move` | `direction`、`speed`、`steps` | 固定步数移动 |
| `POST /api/motors/<电机>/hold` | `direction`、`speed`、`hold_id`、`sampled_at` | 开始长按移动 |
| `POST /api/motors/<电机>/hold/keepalive` | `hold_id` | 续期长按 |
| `POST /api/motors/<电机>/hold/stop` | `hold_id` | 结束长按 |

`<电机>` 取 `bottom`、`top` 或 `insert`，`direction` 取 `cw` 或 `ccw`。
`speed` 是 1–1000 的整数，`steps` 是 1–100000 的整数。
`hold_id` 由客户端生成，长度为 16–64 个字符，只含字母、数字、`_` 和 `-`。
每次长按必须使用新的 `hold_id`。
`sampled_at` 取自 `api/state` 的同名字段，必须在服务时钟的最近 2 秒内。
客户端必须在 1 秒内续期长按，网页每 200 ms 续期一次。
`hold/stop` 可以重复发送。
`hold/stop` 先于同一 `hold_id` 的 `hold` 到达时，服务拒绝之后到达的 `hold`。

### 访问检查

设置 `MG400_STAGE_TOKEN` 后，每个 `/api/` 请求都必须带 `Authorization: Bearer <密钥>`。
此时服务接受任何 `Host` 和转发请求头。
`/` 和 `/static/` 不要求访问密钥，页面不含串口、日志和电机位置。
未设置访问密钥时，`Host` 和转发请求头的规则同 [认证与客户端 ID](#认证与客户端-id)。

POST 请求还必须满足以下条件：

1. 带 `Content-Type: application/json`。
2. 带 `X-Control-Token`，值取自页面的 `<meta name="control-token">`，服务每次启动生成新值。
3. 正文不超过 4096 字节。有正文字段的接口，正文必须是 JSON 对象。

成功时返回 200 和状态对象，字段与 `api/state` 相同。
`api/ports` 返回 `{"ports": [{"device": "...", "description": "..."}]}`。
状态对象的 `motors` 列出每个电机的 `mode`（`idle`、`moving` 或 `continuous`）、`position`、`cooldown` 和 `available` 等字段。
拒绝时，正文为 `{"error": "..."}`，状态码如下：

| 状态码 | 含义 |
| --- | --- |
| 400 | 请求无效，或串口不在当前列表中 |
| 401 | 缺少访问密钥或密钥错误 |
| 403 | `Host`、转发请求头或 `X-Control-Token` 不符合要求 |
| 409 | 状态冲突，例如未连接、电机仍在运动或冷却 |
| 413 | 请求正文超过 4096 字节。此时响应正文是 waitress 生成的纯文本 |
| 415 | 不是 JSON 请求 |
| 503 | 串口通信失败，服务已关闭串口连接 |

## 真机控制参考

本节供修改真机运动和夹爪接口时查阅。
部署配置见 [主机部署](deploy.md)。

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

手动 `Execute targets` 的 `MovL` 使用 `SpeedL=1,AccL=1`。
键盘有限轨迹中，W/S、R/F、Q/E 的 `MovL` 使用 `SpeedL=2,AccL=20`。
控制器逆解验证目标关节范围，控制器规划直线路径。
复位使用 `JointMovJ`，参数为 `SpeedJ=5,AccJ=5`。
A/D 的有限轨迹使用 `JointMovJ`，参数为 `SpeedJ=5,AccJ=20`。
两类关节动作检查整段关节范围，不包含现场碰撞检查。
机械臂运动不修改全局速度或工具配置。

A/D 启动和运行中只用实测关节反馈，不查询 `GetPose`。
动作结束后读取坐标，更新面板。
其余六个方向可复用同一控制实例读取的实测坐标。
复用时，测量不得超过 0.25 秒。
测量前后关节反馈还须与当前关节反馈一致。
不满足条件时，重新读取坐标。

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
面板与采集进程必须位于同一主机，并共享同一单调时钟域。
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
重复包、旧序号和已结束会话的包会被忽略，不清除新鲜样本，也不延长其有效期。
同一文件重复读取不会延长测量值的有效期。

夹爪状态寄存器 `0x0201` 的 `3` 表示物体掉落。
状态定义见 [PGEA 用户手册表 2.9](https://www.manualslib.com/manual/4358849/Dh-Robotics-Technology-Pgea-Series.html?page=17)。
