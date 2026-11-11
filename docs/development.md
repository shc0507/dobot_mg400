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
| 编写位移台服务的客户端 | [位移台服务接口](#位移台服务接口) |
| 修改关节范围、运动参数或吸盘输出 | [真机控制参考](#真机控制参考) |

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

`uv sync` 默认安装 `pyproject.toml` 中 `default-groups` 列出的依赖组：`dev`、`stage`、`vision`、`thermal`、`furnace`、`camera`、`orchestrator` 和 `microscope`。
`vision-gpu` 和 `notebook` 不在默认组中。
`stage` 组含位移台服务使用的 Flask、waitress、pySerial、NumPy 和 OpenCV。
机器人服务和交互终端只用标准库，不依赖 `stage` 组。

测试不连接机械臂或 Arduino。
拉取请求和推送到 `main` 时，GitHub Actions（`.github/workflows/ci.yml`）在 Python 3.12 上运行以上检查。
拉取请求只运行 `scripts/select_tests.py` 按改动文件选出的 pytest 文件。推送到 `main` 时运行全部测试。
`pytest` 运行 Python 测试，`node --test` 运行实验室控制台的 JavaScript 测试。
离线检查不能替代真机验证。

测试共用的 fixture 位于 `tests/conftest.py`。
位移台测试的 `stage_rig`、`stage_ports` 和 `stage_usb` 也在此文件，测试按名称引用。
模拟控制器、连接和网页服务的测试替身（代替真实设备的测试对象）位于 `tests/helpers.py`、`tests/sim_mg400.py` 和 `tests/panel_ws.py`。
服务契约测试的共用检查位于 `tests/contract_helpers.py`。
位移台的时钟、串口和串口列表替身位于 `tests/stage_helpers.py`。
新增测试时复用这些模块，不得在测试文件中另写副本。

使用 `examples/test.ipynb` 时，给同步命令加 `--group notebook`。
如需保留其他手工安装的包，再加 `--inexact`。

## 离线机器人模拟器

`mg400.sim` 在回环地址模拟控制器。
它只接受数字回环地址，默认使用 `127.0.0.2`。
控制器端口为 `29999`、`30003` 和 `30004`，反馈周期为 `8 ms`。
启动后，机械臂处于下使能状态。

连接锁目录固定为 `/var/lib/mg400/locks`。主机上没有该目录时，先运行 `sudo install -d -m 0700 -o "$USER" /var/lib/mg400/locks`。
锁文件按机械臂地址区分，因此模拟器的锁不影响真机。

在项目根目录运行：

```bash
uv run python -m mg400.sim --host 127.0.0.2 &
sim_pid=$!
uv run python apps/control_panel/server.py \
  --bind 127.0.0.1 --port 18766 --robot-host 127.0.0.2
kill "$sim_pid"
wait "$sim_pid"
```

在另一个终端用 [Python 客户端](#python-客户端) 连接 `http://127.0.0.1:18766/`，按以下顺序发送命令：

1. 运行 `start_sync`，等待反馈更新。
2. 运行 `enable`，等待使能完成。
3. 用 `execute` 发送目标坐标。
4. 运行 `stop_sync`，等待停止确认，再运行 `disable`。

在终端按 `Ctrl+C` 结束机器人服务。
随后的命令结束本次模拟器进程，并删除临时锁目录。

模拟器立即到达关节和笛卡尔目标，不计算真实运动学、路径或碰撞。
逆解返回当前关节值。`J1` 点动改变关节反馈，其余点动只改变运动模式。
这些限制只适用于模拟器，生产控制代码和停止确认规则保持不变。
离线演练不能替代硬件验收。

## 代码结构

### 分层

仓库分为 4 层。每层只调用下一层。

| 层 | 组成 | 用途 |
| --- | --- | --- |
| 后端 | 设备服务（`apps/control_panel/`、`src/stage_controller/`、`src/thermal_cam/`、`src/furnace/`、`src/microscope/`、`src/vision/`）和 `firmware/` | 每台设备 1 个进程，只监听 `127.0.0.1`。用户和代理不直接调用后端 |
| 编排器 | `src/orchestrator/` | 设备缓存 `devices.py` 为每台设备运行 1 个读取线程：每 250 ms 读 1 次 `api/state`，每 1 s 读 1 次事件，实例变化时读 `describe`。控制台和 `labctl` 只从这个缓存读设备状态。命令、租约和配方运行也经过编排器 |
| 工具 | `labctl`（`src/orchestrator/agent_cli.py`）和 `mg400ctl`（`deploy/mg400ctl`） | 代理用 `labctl` 操作设备。管理员用 `mg400ctl` 启停服务、查看日志、备份和检查硬件 |
| 前端 | 实验室控制台（`src/orchestrator/web/`） | 浏览器页面，经代理访问编排器 |

例外：`labctl` 的停止命令同时发给编排器和设备服务。

### 目录

| 路径 | 用途 |
| --- | --- |
| `config/robot.toml` | 机器人地址，`MG400_ROBOT_HOST` 未设置时使用 |
| `apps/control_panel/` | 独立 Python 机器人服务，不提供网页 |
| `apps/layout-studio/` | 离线布局编辑器，使用模拟数据，不发送控制命令 |
| `config/systemd/` | 所有服务的 systemd 定义，配置从 `/etc/mg400/mg400.env` 读取 |
| `deploy/` | 安装器 `install.py`、日常命令 `mg400ctl` 和配置示例 |
| `src/mg400/` | 后端、控制协议和反馈处理 |
| `src/mg400/sim/` | 回环控制器模拟器，共用测试模型 |
| `src/stage_controller/` | 位移台服务：Flask 接口和串口协议 |
| `src/labkit/` | 共用的服务契约、租约、事件、访问检查和 HTTP 客户端，见 [ADR 0001](adr/0001-instrument-service-contract.md) |
| `src/cameras/` | AC410 摄像头模式和 UVC 读取 |
| `src/vision/` | [视觉服务](vision.md)：定位、QR 读取、托盘和相机校准 |
| `src/thermal_cam/` | [热像仪服务](thermal.md) |
| `src/furnace/` | [炉温与气体服务](furnace.md) |
| `src/microscope/`、`src/toupcam_camera/` | [显微镜服务](microscope.md)和 Toupcam 摄像头 |
| `src/orchestrator/` | [配方服务](orchestrator.md)和 `labctl`。`web/` 是[实验室控制台](dashboard.md) |
| `recipes/` | TOML 配方 |
| `config/trays/`、`config/proxy/`、`config/udev/` | 托盘几何、代理模板和 USB 访问规则模板 |
| `firmware/three-motor-demo/` | 位移台的 Arduino 固件和接线说明（英文） |
| `firmware/microscope-axes/` | 显微镜转盘与调焦的固件，见[固件说明](../firmware/microscope-axes/README.md) |
| `scripts/` | 摄像头预览、控制模式切换、交互终端入口、控制台截图（`console_shots.py`）、测试选择（`select_tests.py`）、文档对检查（`check_doc_pairs.py`）、代理信号（`agent_signal.py`）、检测模型评估（`vision_eval.py`）和控制台字体（`console_font.py`） |
| `examples/` | 单文件教学和 notebook |
| `tests/` | Python 和 JavaScript 离线测试 |
| `docs/` | 操作、部署和开发指南，`docs/agents/` 保存代理操作规程 |
| `work/` | 本地运行数据、备份和临时文件，不纳入 Git |

用浏览器打开 [布局编辑器](../apps/layout-studio/index.html)，可编辑并导出界面布局。

`terminal_control.py` 提供 `mg400-robot` 真机交互终端。
它复用 `web_control.py` 的连接锁、反馈检查和动作执行，不启动 HTTP 服务。

`apps/control_panel/server.py` 是机器人服务，提供 HTTP 接口和 WebSocket 接口，不提供网页。
实验室控制台的 Motion 工作区通过该服务控制机械臂和吸盘。
`websocket.py` 用标准库实现 WebSocket 帧编解码和连接写线程。
接口说明见 [机器人服务接口](#机器人服务接口)。

WebSocket 按键点动由 `web_control.py` 调度，机械臂长按复用 `keyboard_jog.py`。
`web_keyboard.py` 校验按键输入，并把 Z 和 X 映射为吸取和放下。

机器人服务通过 TCP 协议控制机械臂。
`controller_protocol.py` 解析命令应答，`feedback.py` 读取关节反馈。
`joint_control.py` 执行受限运动和吸盘输出。
TCP 协议参考与完成判定见 [真机控制接口](hardware.md)。
当前没有 ROS 2 后端。

`src/stage_controller/` 是位移台服务，不导入 `mg400`，也不使用机械臂的连接锁。
`__main__.py` 读取 `MG400_STAGE_*` 环境变量和命令行参数，再启动 waitress。
收到 SIGINT、SIGTERM 或 SIGHUP 时，它先停止电机并断开串口，再退出。
`app.py` 提供 HTTP 接口和访问检查。
`controller.py` 管理串口连接、冷却、长按和活动日志。
`protocol.py` 实现 Arduino 串口协议和演示模式。
所有串口命令都在 `MotorController` 的锁内执行，一次移动的命令不与其他请求交错。
修改串口命令时，同时修改固件和 `DemoTransport`。
接口说明见 [位移台服务接口](#位移台服务接口)。

## 机器人服务接口

机器人服务执行 `start_sync` 后连接机械臂，并持有连接锁。
实验室控制台和自写客户端通过服务读取状态和发送命令。
`mg400-robot` 和 `scripts/mg400_control_mode.py` 直接连接机械臂。
它们与服务共用连接锁，不能与服务同时连接。
服务只使用 Python 标准库。

| 接口 | 用途 |
| --- | --- |
| `GET {prefix}api/state` | 读取一次状态 |
| `GET {prefix}api/describe` | 读取契约版本、命令参数和租约行为 |
| `POST {prefix}api/stop` | 请求停止并读取确认结果 |
| `GET {prefix}api/events?since=<seq>` | 读取指定序号之后的日志事件 |
| `POST {prefix}api/command` | 发送一个命令 |
| `POST {prefix}api/heartbeat` | 续期控制租约。契约请求也取得租约 |
| `POST {prefix}api/renew`、`POST {prefix}api/release` | 契约请求：续期或释放租约，见 [契约租约与命令](#契约租约与命令) |
| `GET {prefix}api/ws` | WebSocket 连接：状态推送、命令、心跳和按键 |

`{prefix}` 是 `--base-path` 的值，默认为 `/`。
本节用尖括号表示占位符，例如 `<client-id>` 表示客户端 ID。
按键经 WebSocket 或契约命令 `keys` 发送。

### 访问与客户端 ID

服务监听 `127.0.0.1`，不检查令牌、`Host` 或 `Origin`。VPN 是访问边界。

真机控制模式用客户端 ID 区分客户端，长度为 1–128 个可打印 ASCII 字符。
同一时刻只有一个客户端 ID 持有控制权。
控制者必须在 3 秒内续期控制租约（控制权的有效期）。
心跳、按键更新和控制者的命令都能续期。
服务 3 秒内未收到续期时，请求停止机械臂并锁定控制。

### HTTP 接口

带 `X-MG400-Panel` 的 POST 请求必须满足以下条件：

1. 带 `Content-Type: application/json` 和 `X-MG400-Panel: 1`。
2. 真机控制模式带 `X-MG400-Client: <client-id>`。
3. 正文为 1–4096 字节的 JSON 对象，用 `Content-Length` 标明长度。

`api/command` 和 `api/heartbeat` 成功时返回 200 和状态对象。
拒绝时返回 400（请求无效）、403（拒绝）或 409（状态冲突）。
拒绝的正文为 `{"error": "..."}`。

命令正文为 `{"action": "<action>", ...}`，不得包含其他字段：

| 命令 | 其他字段 | 行为 |
| --- | --- | --- |
| `start_sync` | 无 | 连接机械臂并取得控制权 |
| `stop_sync` | 无 | 停止机械臂并释放控制权，保留反馈直到下使能和停稳获确认。机械臂已下使能且空闲时，确认模式 4 即可 |
| `enable` | 无 | 使能机械臂 |
| `disable` | 无 | 下使能机械臂，任何客户端都可发送 |
| `stop` | 无 | 停止机械臂，任何客户端都可发送 |
| `home` | 无 | J1–J4 移动到 0° |
| `execute` | `targets`（含 `x`、`y`、`z`、`r`），可选 `speed` | 直线移动到法兰坐标 |
| `vacuum` | `operation`：`suck`、`release` 或 `off` | 控制真空吸盘，见 [真空吸盘](#真空吸盘) |
| `keyboard` | `enabled`、`seq`，开启时加 `speed`，关闭时加 `reason` | 开启或关闭键盘控制 |
| `clear_alarm` | 无 | 发送 `ClearError()`，报警清除后下使能并确认静止 |

对于 `keyboard`，`speed` 是 1–100 的整数。
对于 `execute`，可选 `speed` 是整数，范围从 1 到速度上限 `20`。
值无效或超出上限时，服务返回 400，不会将运动排队。
布尔值、字符串、小数和非有限值均无效。

运动使用 `SpeedL=<speed>,AccL=<min(speed, acceleration cap)>`。
加速度上限为 `20`。
两个上限固定在 `apps/control_panel/server.py` 中。

未提供 `speed` 时，即使主机上限提高，协议命令仍为 `SpeedL=1,AccL=1`。
这些配置不改变 `home`、`SpeedFactor` 或键盘点动。
无人值守搬运使用高于 1 的速度前，操作员必须先在硬件上验证。
`clear_alarm` 在控制器报警且反馈新鲜时可用。
`recoverable` 为 `true` 时也可用：停止或下使能未确认，控制器无报警，且反馈新鲜。
此时服务不发送 `ClearError()`，只下使能、确认静止并清除警告。
状态对象的 `alarms` 列出当前报警，无报警时为 `null`。
每项含 `id`、`source`（`controller` 或 `J1`–`J4`）、`description` 和 `solution`。
`reason` 取值为 `user`、`focus`、`edit`、`speed` 或 `modifier`。

### 状态对象

`api/state`、WebSocket 状态推送和成功回复返回同一个状态对象。真机控制模式的字段如下：

| 字段 | 含义 |
| --- | --- |
| `mode` | 服务模式，为 `robot` |
| `instance`、`version` | 服务实例 ID 和递增版本号，重启后实例 ID 改变 |
| `robot_host` | 机械臂地址 |
| `syncing`、`feedback_fresh` | 是否连接反馈，反馈是否新鲜 |
| `robot_mode`、`robot_error` | 控制器模式和报警标志 |
| `enabled` | 模式为 5 或 7 时为 `true` |
| `joints` | 实测关节 J1–J4，单位为度 |
| `actual` | 法兰位姿 X、Y、Z、R。运动中为 4 个 `null` |
| `speed_factor` | 控制器反馈的全局速度百分比 |
| `busy`、`locked`、`stop_unconfirmed`、`recoverable` | 执行中、控制锁定、停止未确认、可恢复 |
| `alarms` | 当前报警列表，见上文 |
| `message`、`last_command` | 服务消息和最近命令结果 |
| `vacuum`、`payload_kg` | 吸盘实测状态和负载，见 [真空吸盘](#真空吸盘) |
| `controller_id` | 持有控制权的客户端 ID，无持有者时为 `null` |
| `keyboard_enabled`、`keyboard_arming` | 键盘点动已开启，正在开启 |
| `keyboard_active_key`、`keyboard_speed_percent`、`keyboard_disarm_reason` | 正在执行的按键、键盘速度和关闭原因 |
| `service`、`seq`、`ts_unix`、`demo` | 契约字段：`robot`、版本号、Unix 时间和 `false` |
| `connected` | 反馈已连接且新鲜 |
| `fault` | `null`，或 `{code, message}`。`code` 为 `stop_unconfirmed` 或 `robot_alarm` |
| `lease` | `{holder, remaining_s}`。`holder` 与 `controller_id` 相同 |

只读模式只返回 `mode`、`robot_host`、`enabled`（`null`）、`syncing`、`actual`（4 个 `null`）、`joints`、`robot_mode`、`robot_error`、`feedback_fresh` 和 `message`。

### 契约租约与命令

不带 `X-MG400-Panel` 的 POST 请求遵循 [仪器服务契约](adr/0001-instrument-service-contract.md)，例如 `labkit.client.ServiceClient` 的请求：

- 请求带 `Content-Type: application/json` 和 `X-MG400-Client`。其他内容类型返回 415。
- `api/heartbeat` 取得或续期租约。取得租约时，服务执行 `start_sync`。其他客户端持有租约时，返回 409。
- `api/renew` 只续期调用者持有的租约。`api/release` 执行 `stop_sync`。
- 租约过期时，服务执行与 `stop_sync` 相同的停止和断开。机械臂已使能时，服务停止并锁定，保留反馈直到下使能。
- `api/command` 接受后返回 202、`command_id` 和 `state`。机器人核心的拒绝返回 409。
- `keys` 命令带 `seq` 和 `held`，与 WebSocket 按键更新相同。它的 `command_id` 为 `null`。

### 契约发现、停止与事件

真机控制模式提供 `api/describe`、`api/stop` 和 `api/events`。
`api/stop` 检查上述 POST 请求头和客户端 ID。

`api/describe` 返回 `service`、`version`、`contract`、`demo`、`zones`、`lease_seconds`、`on_lease_expiry` 和 `commands`。
`contract` 为 1，`demo` 为 `false`。
每个命令的 `params` 用 JSON Schema 描述参数，不含 `action`。
`stop` 和 `disable` 的 `any_client` 为 `true`。
`keyboard` 和 `keys` 的 `ui_only` 为 `true`，只供交互界面使用。
带 `X-MG400-Panel` 时，`start_sync` 取得控制权，`api/heartbeat` 只续期持有者的租约，不取得控制权。

`api/stop` 的正文为 `{"wait_s": 0}`。
`wait_s` 可省略，默认为 0，范围为 0–15 秒，必须是有限数值。
接口在停止工作线程中执行停止，等待时释放状态锁。
任何客户端都能停止，无须持有控制租约。
同类停止正在执行时，重复请求不启动新的停止。
成功响应为 200 和 `{"stop": "confirmed", "state": {...}}`。

| `stop` | 含义 |
| --- | --- |
| `confirmed` | 机械臂停止已确认 |
| `pending` | 等待期限已到，停止工作线程仍在执行 |
| `unconfirmed` | 机械臂停止未确认 |

先前的停止未确认时，后续停止成功也不能清除此状态。
响应保留锁定状态，不自动重试运动。

状态的 `last_command` 初始为 `null`。
命令完成或被拒绝后，该字段包含 `id`、`action`、`client`、`result`、`message` 和 `finished_at`。
`finished_at` 是 Unix 秒数。
`result` 为 `ok`、`failed`、`rejected` 或 `stopped`。
异步命令在工作线程完成后更新结果。
其他客户端的拒绝请求不会隐藏正在执行的命令的最终结果。
已取消的旧工作线程不能覆盖较新的已完成停止。

`api/events` 返回 `{"events": [...], "next": 0}`。
每个事件含 `seq`、`ts_unix`、`level`、`message` 和 `fields`。
`since` 默认为 0，必须是非负整数。
响应只包含序号大于 `since` 的事件，下一次查询使用 `next`。
服务保留最近 1000 个事件，重启后序号重新开始。
事件记录状态消息、锁定和解锁、停止未确认及其清除。
事件还记录反馈模式和报警标志的变化、报警和键盘关闭原因。每个事件的 `message` 即对应的服务日志行。

### Python 客户端

`mg400.client.RobotClient(url, client_id)` 使用服务基础 URL 和明确的客户端 ID。
每个程序使用独立 ID。运行中的程序不得共用 ID。
构造对象不会连接服务。`connect()` 检查 WebSocket 协议 1 和服务契约 1，然后启动客户端线程。
支持 `http://`、`https://`、`ws://` 和 `wss://`。如果服务配置了基础路径，URL 必须包含该路径。

运行示例前，启动[离线模拟器和机器人服务](#离线机器人模拟器)。
示例向该模拟器发送运动命令。

```python
from mg400.client import RobotClient

with RobotClient("http://127.0.0.1:18766/", "recipe-example") as robot:
    robot.run("start_sync")
    robot.run("enable")
    result = robot.run("execute", timeout=30, targets={"x": 260, "y": 12, "z": 42, "r": 31})
    print(result["last_command"])
```

- `state` 返回最新状态的副本。连接前为 `None`。`connected` 表示 WebSocket 是否可用。
- `command(action, **fields)` 发送 1 次请求并返回协议回复。检查 `status`。接受请求不表示操作完成。
- `wait(predicate, timeout)` 返回满足条件的状态。超时抛出 `TimeoutError`。
- `run(action, timeout=30, **fields)` 等待 `busy == False`，并检查属于本次动作和客户端的新 `last_command`。
- `vacuum(operation, timeout=30)` 用 `run()` 发送 1 次 `vacuum` 命令。`operation` 取 `suck`、`release` 或 `off`，其他值抛出 `ValueError`。见[真空吸盘](#真空吸盘)。
- `vacuum_state` 返回最新状态的 `vacuum` 字段。连接前为 `None`。
- `keys(seq, held)` 经 WebSocket 发送 1 次按键集合并返回协议回复，不重试。
- `RobotClientError` 表示请求被拒绝或结果未知。如果服务提供错误码，异常保留 `status` 和 `close_code`。

`run()` 接受 `ok`。对于 `stop`、`disable` 和 `stop_sync`，停止仍获确认时，也接受 `stopped`。
服务锁定时，`run("start_sync")` 仍可成功，新持有者因此可以发送 `clear_alarm` 恢复。服务继续拒绝运动。
`start_sync` 还等待 `feedback_fresh` 为真。
其他动作完成时，若 `stop_unconfirmed` 为真，`run()` 抛出 `RobotClientError`。若 `locked` 为真，`run()` 也抛出该异常，`stop`、`disable` 和 `stop_sync` 除外。
请求被拒绝、操作失败、停止取代运动或服务重启，都不能算作运动成功。
每个客户端 ID 只能有 1 个运行中的程序。`run()` 期间，同一客户端的普通命令串行执行。
其他线程仍可调用 `command("stop")`、`command("disable")`、`run("stop")` 或 `run("disable")`。

持有控制权时，客户端每 0.5 秒发送心跳。
套接字断开后，客户端最多尝试重连传输连接 2.5 秒，短于 3 秒租约。
客户端不会重发命令。回复丢失或超时后，命令是否送达或完成可能未知。
再次操作前，检查服务状态。不得自动重试运动。
hello 失败、协议错误或连接被替换后，客户端停止尝试连接。

套接字不可用时，`command("stop")` 和 `command("disable")` 通过 HTTP 发送，保留客户端 ID。
改用 HTTP 时，先检查契约 1。
回复保留 `locked` 和 `stop_unconfirmed`。回复仍只表示接受请求，不表示停止已获确认。
其他命令不会改用 HTTP。HTTP 请求不跟随重定向，也不会自动重试。

退出上下文时，如果客户端持有控制权，先运行 `stop_sync`，再关闭连接并停止线程。
机械臂仍使能时，服务保留反馈并阻止运动，直到显式 `disable` 确认模式 4 和停稳。
客户端不会自动下使能。停止获确认后，发送 `disable` 即可完成断开，无须重启服务。
可选参数 `keep_control=True` 跳过 `stop_sync`。退出后仍会停止心跳。
直接调用 `close()` 也会停止线程，不续期或释放控制权。
服务随后按租约超时规则停止机械臂并锁定控制。
可选参数 `timeout=5` 配置连接和回复超时，单位为秒。

### WebSocket 接口

按以下顺序通信：

1. 连接后 3 秒内，发送 `{"type":"hello","protocol":1,"client":"<client-id>"}`。
2. 等待服务推送 `{"type":"state","state":{...}}`。
3. 发送请求 `{"type":"<type>","id":<integer>,"body":{...}}`。
4. 按 `id` 匹配回复。

`protocol` 是消息格式版本，当前为 1。hello 不得含其他字段。
`state` 的字段与 `api/state` 相同。
状态变化时，服务推送状态。
每次回复后，服务也推送一次。
服务至少每秒推送一次。

| 请求类型 | `body` |
| --- | --- |
| `command` | 与 `api/command` 的正文相同 |
| `heartbeat` | `{}` |
| `keys` | `{"seq":<seq>,"held":[<key>]}` |

`id` 是 0 到 2^53 的整数，由客户端选择。
服务按请求顺序逐个回复，每个请求一个回复。
成功回复为 `{"type":"reply","id":<id>,"status":200,"state":{...}}`。
拒绝回复为 `{"type":"reply","id":<id>,"status":<status>,"error":"..."}`。
状态码含义同 HTTP 接口，500 表示服务内部错误。
`body` 无效时，服务只回复错误，连接保持打开。
`command` 的成功回复表示命令已受理，不表示动作已完成。
动作结束后，状态中的 `busy` 变为 `false`，`message` 给出结果。
服务日志为每个 `command` 记录一行，`keys` 和 `heartbeat` 不记录。

`keys` 的 `seq` 与键盘开关命令共用一个递增序号，服务忽略不大于上次值的消息。
`held` 列出当前按住的键，取值为 W、A、S、D、Q、E、R、F、Z、X。
按住按键时，客户端必须在 0.4 秒内重发。实验室控制台每 150 ms 重发一次。
服务 0.4 秒内未收到更新时，停止点动并关闭键盘控制。

关闭码为 4400、4426 或 1013 时，服务先发送 `{"type":"error","status":<status>,"error":"..."}`，再关闭连接。

| 关闭码 | 含义 |
| --- | --- |
| 4400 | hello 或请求格式无效 |
| 4426 | `protocol` 与服务不一致，客户端必须更新 |
| 4409 | 同一客户端 ID 的新连接替换了此连接 |
| 1013 | 连接数已满 |
| 1001 | 服务停止，关闭前推送最后状态 |
| 1011 | 服务处理连接时出错 |
| 1002、1003、1007、1009 | 帧格式错误、二进制消息、文本不是 UTF-8 或消息过长 |

连接规则如下：

- 消息只能是不超过 4096 字节的 UTF-8 文本。
- 服务每 5 秒发送 ping，15 秒未收到任何帧时关闭连接。
- 最多 8 个客户端 ID 同时保持 hello 后的连接。
- 最多 8 个连接同时等待 hello。超出时，服务关闭等待最久的连接。
- `GET api/ws` 不是 WebSocket 升级请求时，服务返回 HTTP 426。
- 同一客户端 ID 再次发送 hello 时，服务关闭旧连接。
- 客户端的最新连接关闭，或同一客户端 ID 的新连接替换它时，服务立即关闭该客户端的键盘控制。

连接关闭不终止控制租约。
服务或代理关闭连接、客户端 3 秒内重连并续期时，保留控制权。
网络中断时，服务可能检测不到连接关闭，由按键超时和控制租约停止机械臂。

## 位移台服务接口

位移台服务的 HTTP 接口使用 JSON，全部位于 `{prefix}contract/api/`。
`{prefix}` 是 `--base-path` 的值，默认为 `/`。
服务每 0.2 秒查询一次 Arduino 状态，并在查询时停止超时的长按。

### 位移台自动化契约

`labkit.client.ServiceClient` 的基地址为 `http://127.0.0.1:8767/contract`，包含 `/contract`。
接口遵循 [仪器服务契约](adr/0001-instrument-service-contract.md)。

| 接口 | 用途 |
| --- | --- |
| `GET /contract/api/describe` | 读取命令参数、限制、演示标志和租约规则 |
| `GET /contract/api/state` | 读取电机状态及 `service`、`seq`、`ts_unix`、`busy`、`locked`、`fault`、`lease`、`last_command` |
| `POST /contract/api/heartbeat` | 取得或续期控制租约。其他客户端占用时返回 409 |
| `POST /contract/api/renew` | 仅续期调用者持有且未过期的租约，否则返回 409 |
| `POST /contract/api/release` | 释放调用者的控制租约，不发送停止命令 |
| `POST /contract/api/command` | 正文携带 `action` 和对应参数。接受后返回 202、`command_id` 和 `state` |
| `POST /contract/api/stop` | 任意客户端可请求停止，无须租约。`wait_s` 为 0–15 秒，默认 0 |
| `GET /contract/api/events?since=0` | 读取活动事件和 `next` 游标。每条活动只记录一次，同时写入服务日志 |

| `action` | 参数 | 完成判定 |
| --- | --- | --- |
| `move` | `motor`、`direction`、`speed`、`steps` | 新鲜反馈确认目标电机空闲后，更新 `last_command` |
| `hold` | `motor`、`direction`、`speed`、`hold_id`、`sampled_at` | Arduino 确认开始长按。电机继续运动 |
| `keepalive` | `motor`、`hold_id` | Arduino 确认续期长按 |
| `release_hold` | `motor`、`hold_id` | 结束对应长按 |
| `stop_all` | 无 | Arduino 确认停止请求。无须持有租约。故障锁定时使用专用停止接口 |
| `set_microstep` | `microstep` | Arduino 确认细分模式 |

`motor` 取 `bottom`、`top` 或 `insert`，`direction` 取 `cw` 或 `ccw`。
`speed` 是 1–1000 的整数，`steps` 是 1–100000 的整数，`microstep` 取 1、2、4、8 或 16。
`hold_id` 由客户端生成，长度为 16–64 个字符，只含字母、数字、`_` 和 `-`。
每次长按必须使用新的 `hold_id`。
`sampled_at` 取自 `api/state` 的同名字段，必须在服务时钟的最近 2 秒内。
`release_hold` 可以重复发送。它先于同一 `hold_id` 的 `hold` 到达时，服务拒绝之后到达的 `hold`。
契约还提供 `home`（`motor`）、`zero`（`motor`、`confirm: true`）、`awake`（`motor`、`seconds` 0–60）、`goto`（`motor`、`name`）和 `pose`（`name`），见 [位移台接口](stage-panel.md#接口)。
服务拒绝缺少或多余的参数。

控制租约默认有效 3 秒，长按仍须在 1 秒内单独续期。
长按续期不续期控制租约。
labkit Flask 适配器在发现响应中声明 `renew_only_heartbeat: true`。
要持续持有控制权，先用 `ServiceClient.heartbeat()` 明确取得租约，再用 `ServiceClient.renew()` 续期。
可选的 `heartbeat_thread(renew_only=True)` 辅助线程使用严格续期，首次出错后停止。
续期请求在租约过期或释放后到达时，返回 409，不会重新取得控制权。
旧服务没有 `api/renew` 时，返回 404。客户端不会改为取得租约。
`heartbeat()` 和默认 `heartbeat_thread()` 取得或续期租约。
续期失败后，先停止运动并核对服务状态，再明确取得控制权。
租约过期时，服务请求停止所有电机。
释放租约不停止运动。先等待完成或请求停止，再交还控制权。

专用停止接口返回 `stop` 和 `state`，`stop` 为 `confirmed`、`pending` 或 `unconfirmed`。
仅确认所有电机空闲后才返回 `confirmed`。
等待反馈时，服务允许其他停止请求，但禁止新运动。
停止等待到期或串口结果未知时，`locked` 为真且 `fault` 非空。
后续空闲反馈不清除故障。核对现场状态后，运行 `mg400ctl restart stage`，再重新连接。确认所有电机空闲后才清除。
停止确认尚未结束时，重新连接请求返回 409，防止用新连接的反馈确认旧设备已停止。
`last_command` 区分 `ok`、`failed`、`rejected` 和 `stopped`。服务不自动重试运动。

### 操作接口

实验室控制台还使用以下 4 个接口，前缀与契约相同：

| 接口 | 正文 | 用途 |
| --- | --- | --- |
| `GET /contract/api/ports` | 无 | 列出可连接的串口。设置 `MG400_STAGE_SERIAL` 时只列出该设备 |
| `POST /contract/api/connect` | `{"port": "..."}` | 连接 `api/ports` 列出的串口，演示模式只接受 `DEMO`。调用者必须持有位移台租约 |
| `GET /contract/api/motors/<motor>/reference` | 无 | 读取相机图像和标记的像素偏移 |
| `POST /contract/api/motors/<motor>/teach_reference` | `{"confirm": true}` | 把当前标记位置保存为该轴的参考。调用者必须持有位移台租约 |

`api/ports` 返回 `{"ports": [{"device": "...", "description": "..."}]}`。
`api/connect` 和 `teach_reference` 成功时返回状态对象和 `lease`。

### 访问检查

服务不检查令牌、`Host` 或 `Origin`，见 [访问与客户端 ID](#访问与客户端-id)。
POST 必须带 `Content-Type: application/json` 和 `X-MG400-Client`，正文必须是 JSON 对象。
客户端 ID 为 1–128 个可打印 ASCII 字符。
正文大小上限为 4096 字节。

操作接口拒绝时，正文为 `{"error": "..."}`，状态码如下：

| 状态码 | 含义 |
| --- | --- |
| 400 | 请求无效，或串口不在当前列表中 |
| 409 | 状态冲突，例如未持有租约、已连接或电机仍在运动 |
| 413 | 请求正文超过 4096 字节 |
| 415 | 不是 JSON 请求 |
| 503 | 串口通信失败，服务已关闭串口连接 |

## 真机控制参考

本节供修改真机运动和吸盘接口时查阅。
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

未提供 `speed` 时，`execute` 的 `MovL` 使用 `SpeedL=1,AccL=1`。
控制器逆解验证目标关节范围，控制器规划直线路径。
复位使用 `JointMovJ`，参数为 `SpeedJ=5,AccJ=5`。
`execute` 和复位不修改全局速度或工具配置。

键盘点动开启时，服务发送 `SpeedFactor(<speed>)`，设置全局速度。
按住按键时，服务发送连续点动命令 `MoveJog`：

| 按键 | 命令 |
| --- | --- |
| W/S | `MoveJog(X+,CoordType=0,User=8,Tool=0)`、`MoveJog(X-,CoordType=0,User=8,Tool=0)`，沿 J1 方向伸出和收回 |
| A/D | `MoveJog(J1+)`、`MoveJog(J1-)` |
| Q/E | `MoveJog(J4+)`、`MoveJog(J4-)` |
| R/F | `MoveJog(Z+,CoordType=0,User=0,Tool=0)`、`MoveJog(Z-,CoordType=0,User=0,Tool=0)` |

W/S 开始前，服务确认 User 0 未改变，再用 `SetUser(8,{0,0,0,J1})` 把用户坐标系 8 对齐当前 J1。
连续点动接近关节界限时，服务提前停止。提前量为每 1% 速度 0.08°。

关节处于 1.05° 余量区时，服务改用有限轨迹向内移动：
W/S、R/F、Q/E 使用 `MovL`，参数为 `SpeedL=2,AccL=20`。
A/D 使用 `JointMovJ`，参数为 `SpeedJ=5,AccJ=20`。
关节动作检查整段关节范围，不包含现场碰撞检查。

A/D 启动和运行中只用实测关节反馈，不查询 `GetPose`。
动作结束后读取坐标，更新状态。
其余六个方向可复用同一控制实例读取的实测坐标。
复用时，测量不得超过 0.25 秒。
测量前后关节反馈还须与当前关节反馈一致。
不满足条件时，重新读取坐标。

四轴协议参考：[Dobot 官方 SDK](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python/blob/main/dobot_api.py)。

### 真空吸盘

`vacuum` 命令的 `operation` 取以下值之一：

| `operation` | 控制器命令 |
| --- | --- |
| `suck` | `DOExecute(1,0)`，`DOExecute(2,1)` |
| `release` | `DOExecute(2,0)`，`DOExecute(1,1)`，吹气 0.3 秒，`DOExecute(1,0)` |
| `off` | `DOExecute(2,0)`，`DOExecute(1,0)` |

- 机械臂必须空闲，控制器模式为 4 或 5。命令不使能、也不移动机械臂。
- 服务每条命令只发送一次，然后等待 30004 反馈中的 DO 实测位改变，时限 1 秒。
- 停止和下使能在运动停止命令后发送 1 次 `DOExecute(1,0)`，条件是吹气正在进行或反馈显示 DO1 为高电平。它们不等待吹气结束，也不发送 DO2 命令。该命令失败时，`JointControl.stop()` 的结果包含 `blow_error`，停止本身不因此失败。机器人服务不报告 `blow_error`，下使能不记录该错误。
- 状态对象的 `vacuum` 含 `suck`、`blow` 和 `held`。`suck` 和 `blow` 是 DO2 和 DO1 的实测电平，无反馈时为 `null`。未设置 `GRIP_DI` 时，`held` 为 `null`。
- 状态对象的 `payload_kg` 是控制器反馈的负载，单位 kg。
- 常量在 `src/mg400/suction.py` 中：`PRESSURE_DO`、`VACUUM_DO`、`BLOW_SECONDS` 和 `GRIP_DI`。
- 离线模拟器响应 `DOExecute`，并在反馈帧中报告 DO 位。
- 接线和反馈字段见 [真空吸盘](hardware.md#真空吸盘)。
