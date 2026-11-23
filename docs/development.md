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
机器人服务只用标准库，不依赖 `stage` 组。

测试不连接机械臂或 Arduino。
拉取请求和推送到 `main` 时，GitHub Actions（`.github/workflows/ci.yml`）在 Python 3.12 上运行以上检查。
拉取请求只运行 `scripts/select_tests.py` 按改动文件选出的 pytest 文件。推送到 `main` 时运行全部测试。
`pytest` 运行 Python 测试，`node --test` 运行实验室控制台的 JavaScript 测试。
离线检查不能替代真机验证。

测试共用的 fixture 位于 `tests/conftest.py`。
位移台测试的 `stage_rig`、`stage_ports` 和 `stage_usb` 也在此文件，测试按名称引用。
模拟控制器、连接和网页服务的测试替身（代替真实设备的测试对象）位于 `tests/helpers.py`、`tests/sim_mg400.py` 和 `tests/panel_http.py`。
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

在另一个终端用 `labkit.client.ServiceClient("http://127.0.0.1:18766/", "<client-id>")` 连接，按以下顺序发送请求：

1. 调用 `heartbeat()` 取得租约，等待状态的 `connected` 为 `true`。
2. 调用 `command("enable")`，等待使能完成。
3. 用 `command("execute", targets=...)` 发送目标坐标。
4. 先调用 `command("disable")`，等待停止确认，再调用 `release()`。

每个 `command()` 返回 `command_id`。`state()` 的 `last_command.id` 等于它，且 `busy` 为 `false` 时，命令完成。
租约每 3 秒到期，等待期间调用 `renew()`。

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
| 工具 | `labctl`（`deploy/labctl`，设备命令组在 `src/orchestrator/agent_cli.py`） | 代理用设备命令组操作设备。管理员用服务命令启停服务、查看日志、备份和检查硬件 |
| 前端 | 实验室控制台（`src/orchestrator/web/`） | 浏览器页面，经代理访问编排器 |

例外：`labctl` 的停止命令同时发给编排器和设备服务。

### 目录

| 路径 | 用途 |
| --- | --- |
| `config/robot.toml` | 机器人地址，`MG400_ROBOT_HOST` 未设置时使用 |
| `apps/control_panel/` | 独立 Python 机器人服务，不提供网页 |
| `apps/layout-studio/` | 离线布局编辑器，使用模拟数据，不发送控制命令 |
| `config/systemd/` | 所有服务的 systemd 定义，配置从 `/etc/mg400/mg400.env` 读取 |
| `deploy/` | 安装器 `install.py`、日常命令 `labctl` 和配置示例 |
| `src/mg400/` | 后端、控制协议和反馈处理 |
| `src/mg400/sim/` | 回环控制器模拟器，共用测试模型 |
| `src/stage_controller/` | 位移台服务：Flask 接口和串口协议 |
| `src/labkit/` | 共用的服务契约、租约、事件、访问检查和 HTTP 客户端，见 [ADR 0001](adr/0001-instrument-service-contract.md) |
| `src/cameras/` | AC410 摄像头模式和 go2rtc 图像读取 |
| `src/vision/` | [视觉服务](vision.md)：定位、QR 读取、托盘和相机校准 |
| `src/thermal_cam/` | [热像仪服务](thermal.md) |
| `src/furnace/` | [炉温与气体服务](furnace.md) |
| `src/microscope/`、`src/toupcam_camera/` | [显微镜服务](microscope.md)和 Toupcam 摄像头 |
| `src/orchestrator/` | [配方服务](orchestrator.md)和 `labctl`。`web/` 是[实验室控制台](dashboard.md) |
| `recipes/` | TOML 配方 |
| `config/trays/`、`config/proxy/`、`config/udev/` | 托盘几何、代理模板和 USB 访问规则模板 |
| `firmware/three-motor-demo/` | 位移台的 Arduino 固件和接线说明（英文） |
| `firmware/microscope-axes/` | 显微镜转盘与调焦的固件，见[固件说明](../firmware/microscope-axes/README.md) |
| `scripts/` | 摄像头预览、控制模式切换、控制台截图（`console_shots.py`）、测试选择（`select_tests.py`）、文档对检查（`check_doc_pairs.py`）、代理信号（`agent_signal.py`）、检测模型评估（`vision_eval.py`）和控制台字体（`console_font.py`） |
| `examples/` | 单文件教学和 notebook |
| `tests/` | Python 和 JavaScript 离线测试 |
| `docs/` | 操作、部署和开发指南，`docs/agents/` 保存代理操作规程 |
| `work/` | 本地运行数据、备份和临时文件，不纳入 Git |

用浏览器打开 [布局编辑器](../apps/layout-studio/index.html)，可编辑并导出界面布局。

`apps/control_panel/server.py` 是机器人服务，提供 HTTP 接口，不提供网页。
实验室控制台的 Motion 工作区通过该服务控制机械臂和吸盘。
`websocket.py` 用标准库实现 WebSocket 帧编解码，供控制台控制套接字使用。
接口说明见 [机器人服务接口](#机器人服务接口)。

按键点动由 `web_control.py` 调度，机械臂长按复用 `keyboard_jog.py`。
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

机器人服务取得控制租约后连接机械臂，并持有连接锁。
实验室控制台和自写客户端通过服务读取状态和发送命令。
`labctl mode` 直接连接机械臂。
它与服务共用连接锁，不能与服务同时连接。
服务只使用 Python 标准库。

| 接口 | 用途 |
| --- | --- |
| `GET {prefix}api/state` | 读取一次状态 |
| `GET {prefix}api/describe` | 读取契约版本、命令参数和租约行为 |
| `POST {prefix}api/stop` | 请求停止并读取确认结果 |
| `GET {prefix}api/events?since=<seq>` | 读取指定序号之后的日志事件 |
| `POST {prefix}api/command` | 发送一个命令 |
| `POST {prefix}api/heartbeat` | 取得控制租约，连接机械臂 |
| `POST {prefix}api/renew`、`POST {prefix}api/release` | 续期或释放租约，见 [契约租约与命令](#契约租约与命令) |

`{prefix}` 是 `--base-path` 的值，默认为 `/`。
本节用尖括号表示占位符，例如 `<client-id>` 表示客户端 ID。
按键经命令 `keys` 发送。

### 访问与客户端 ID

服务监听 `127.0.0.1`，不检查令牌、`Host` 或 `Origin`。VPN 是访问边界。

真机控制模式用客户端 ID 区分客户端，长度为 1–128 个可打印 ASCII 字符。
同一时刻只有一个客户端 ID 持有控制权。
控制者必须在 3 秒内续期控制租约（控制权的有效期）。
续期、按键更新和控制者的命令都能续期。
服务 3 秒内未收到续期时，请求停止机械臂并锁定控制。

### HTTP 接口

POST 请求遵循 [仪器服务契约](adr/0001-instrument-service-contract.md)，必须满足以下条件：

1. 带 `Content-Type: application/json`。其他内容类型返回 415。
2. 真机控制模式带 `X-MG400-Client: <client-id>`。
3. 正文为 1–4096 字节的 JSON 对象，用 `Content-Length` 标明长度。租约请求的正文为 `{}`。

`api/command` 成功时返回 202、`command_id` 和 `state`。
`api/heartbeat`、`api/renew` 和 `api/release` 成功时返回 200 和 `{"lease": {...}}`。
拒绝时返回 400（请求无效）或 409（状态冲突，含机器人核心的拒绝）。
拒绝的正文为 `{"error": "..."}`。

命令正文为 `{"action": "<action>", ...}`，不得包含其他字段：

| 命令 | 其他字段 | 行为 |
| --- | --- | --- |
| `enable` | 无 | 使能机械臂，并设定负载 |
| `disable` | 无 | 下使能机械臂，任何客户端都可发送 |
| `stop` | 无 | 停止机械臂，任何客户端都可发送 |
| `home` | 无 | J1–J4 移动到 0° |
| `execute` | `targets`（含 `x`、`y`、`z`、`r`），可选 `speed` | 直线移动到法兰坐标 |
| `vacuum` | `operation`：`suck`、`release` 或 `off` | 控制真空吸盘，见 [真空吸盘](#真空吸盘) |
| `keyboard` | `enabled`、`seq`，开启时加 `speed`，关闭时加 `reason` | 开启或关闭键盘控制 |
| `clear_alarm` | 无 | 发送 `ClearError()`；报警清除后发送 `ResetRobot()`，下使能并确认静止 |
| `keys` | `seq`、`held` | 发送一次按住的键，`command_id` 为 `null`，见 [按键](#按键) |

对于 `keyboard`，`speed` 是 1–100 的整数。
对于 `execute`，可选 `speed` 是整数，范围从 1 到速度上限 `20`。
值无效或超出上限时，服务返回 400，不会将运动排队。
布尔值、字符串、小数和非有限值均无效。

运动使用 `SpeedL=<speed>,AccL=<min(speed, acceleration cap)>`。
加速度上限为 `20`。
两个上限固定在 `apps/control_panel/server.py` 中。

未提供 `speed` 时，协议命令仍为 `SpeedL=1,AccL=1`。
这些上限不改变 `home`、`SpeedFactor` 或键盘点动。
无人值守搬运使用高于 1 的速度前，操作员必须先在硬件上验证。
`clear_alarm` 在控制器报警且反馈新鲜时可用。
`recoverable` 为 `true` 时也可用：停止或下使能未确认，控制器无报警，且反馈新鲜。
此时服务不发送 `ClearError()`，只发送 `ResetRobot()`、下使能、确认静止并清除警告。
状态对象的 `alarms` 列出当前报警，无报警时为 `null`。
每项含 `id`、`source`（`controller` 或 `J1`–`J4`）、`description` 和 `solution`。
`reason` 取值为 `user`、`focus`、`edit`、`speed` 或 `modifier`。

### 状态对象

`api/state` 和命令的成功回复返回同一个状态对象。真机控制模式的字段如下：

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

- `api/heartbeat` 取得租约，服务连接机械臂。其他客户端持有租约时，返回 409。
- `api/renew` 只续期调用者持有的租约。租约过期或已释放时返回 409，不重新取得租约。
- `api/release` 停止机械臂并释放租约。机械臂已使能时，服务保留反馈和控制锁，直到显式 `disable` 确认模式 4 和停稳。机械臂已下使能且空闲时，确认模式 4 即可。
- 租约过期时，服务执行与 `api/release` 相同的停止和断开。机械臂已使能时，服务停止并锁定，保留反馈直到下使能。
- 服务锁定时，新持有者仍可取得租约，并可发送 `clear_alarm` 恢复。服务继续拒绝运动。
- `api/command` 接受后返回 202、`command_id` 和 `state`。机器人核心的拒绝返回 409。
- 命令 `start_sync` 和 `stop_sync` 不存在，请使用租约路由。

`state.last_command.id` 等于 `command_id` 时，命令结束。
`busy` 为 `false` 且 `locked` 与 `stop_unconfirmed` 均为 `false`（`stop`、`disable` 除外）时，运动命令才算成功。
服务重启（`instance` 改变）或后续停止取代命令时，结果未知。
客户端不得重发命令，回复丢失后须先读取状态。

### 契约发现、停止与事件

真机控制模式提供 `api/describe`、`api/stop` 和 `api/events`。
`api/stop` 检查上述请求头和客户端 ID。

`api/describe` 返回 `service`、`version`、`contract`、`demo`、`zones`、`lease_seconds`、`on_lease_expiry` 和 `commands`。
`contract` 为 1，`demo` 为 `false`。
每个命令的 `params` 用 JSON Schema 描述参数，不含 `action`。
`stop` 和 `disable` 的 `any_client` 为 `true`。
`keyboard` 和 `keys` 的 `ui_only` 为 `true`，只供交互界面使用。

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

### 按键

`keys` 命令的正文为 `{"action":"keys","seq":<seq>,"held":[<key>]}`。
`seq` 与键盘开关命令共用一个递增序号，服务忽略不大于上次值的消息。
`held` 列出当前按住的键，取值为 W、A、S、D、Q、E、R、F、Z、X。
按住按键时，客户端必须在 0.4 秒内重发。实验室控制台每 150 ms 重发一次。
服务 0.4 秒内未收到更新时，停止点动并关闭键盘控制。
服务日志不记录 `keys` 请求。

## 位移台服务接口

JSON API 默认位于 `/api/`。`labkit.client.ServiceClient` 使用 `http://127.0.0.1:8767/`。兼容别名 `/contract/api/` 共用同一组处理器和租约。服务遵循[仪器服务契约](adr/0001-instrument-service-contract.md)。

Arduino 广播编码器和开关状态。服务每 0.05 s 读取遥测。公开坐标和相对距离使用电机轴圈数，速度使用圈/秒。`ccw` 增加坐标，`cw` 减少坐标。固件执行控制环。

### 位移台自动化契约

| 接口 | 用途 |
| --- | --- |
| `GET /api/describe` | 读取命令格式、限制、能力和租约规则 |
| `GET /api/state` | 读取电机、连接、租约、命令结果和取消序号 |
| `POST /api/heartbeat` | 获取或续期控制权，有其他持有者时返回 409 |
| `POST /api/renew` | 只续期调用者持有且未过期的租约 |
| `POST /api/release` | 释放控制权，不发送停止 |
| `POST /api/command` | 发送 `action` 和参数，接受后返回 202、`command_id` 和 `state` |
| `POST /api/stop` | 不要求租约的停止请求，`wait_s` 为 0–15 s，默认 0 |
| `GET /api/events?since=0` | 读取活动和 `next` 游标 |

| `action` | 参数 | 含义 |
| --- | --- | --- |
| `connect` | `port` | 连接状态中列出的串口。演示只接受 `DEMO`。服务故障时仍可用 |
| `move` | `motor`、`direction`、`speed`、`rotations` | 编码器相对移动 |
| `move_guarded` | `motor`、`direction`、`speed`、`rotations`、`stop_epoch` | 检查取消状态后相对移动 |
| `move_to_position` | `motor`、`position`、`speed` | 标定后的编码器绝对目标 |
| `move_to_position_guarded` | `motor`、`position`、`speed`、`stop_epoch` | 检查取消状态后移到绝对目标 |
| `calibrate` | `motor`、`speed` | 寻找下限、上限，再移到中心 |
| `reset_position` | `motor` | 位置归零并清除全部限位 |
| `set_limit` | `motor` | 记录正上限或负下限，拒绝零值 |
| `clear_upper_limit` | `motor` | 清除上限 |
| `clear_lower_limit` | `motor` | 清除下限 |
| `clear_fault` | `motor` | 清除反馈故障，不移动 |
| `hold` | `motor`、`direction`、`speed`、`hold_id`、`sampled_at` | 开始连续移动 |
| `keepalive` | `motor`、`hold_id` | 续期对应的按住移动 |
| `release_hold` | `motor`、`hold_id` | 结束对应的按住移动 |
| `stop_all` | 无 | 所有客户端均可请求停止 |
| `set_microstep` | `microstep` | 确认固定 1/8 模式 |
| `set_microstep_guarded` | `microstep`、`stop_epoch` | 检查取消状态后确认固定模式 |

`motor` 为 `bottom`、`top` 或 `insert`。`direction` 为 `cw` 或 `ccw`。`rotations` 接受 0.001–10000。`speed` 接受 0.001–5，并受电机 `max_speed` 限制。`position` 必须非负，且在标定限位内。`microstep` 只接受 8。缺少或多余的参数均被拒绝。

服务每次只允许 1 个轴移动，并等待位移台冷却结束后再移动。相对移动和按住移动要求编码器反馈正常且未过期。绝对移动还要求完成标定并设置全部限位。修改零点或限位会使标定失效。正常触发开关不会使坐标归零。

状态含 `service`、`seq`、`ts_unix`、`sampled_at`、`busy`、`locked`、`fault`、`lease`、`last_command`、`stop_epoch` 和 `coordinate_unit`。坐标单位为 `rotations`。设备能力含 `closed_loop`、`telemetry_streaming`、`home_switches`、`auto_calibration` 和 `absolute_positioning`。

每个电机提供 `position_rotations`、`remaining_rotations`、`measured_speed`、`max_speed`、`encoder`、`fault`、`telemetry_age_ms`、`limit_telemetry_age_ms`、`telemetry_fresh`、`switch_mask`、`calibration`、`lower_limit_rotations`、`upper_limit_rotations`、`lower_limit_set`、`upper_limit_set`、`motion_ready`、`positioning_ready` 和 `position_tolerance_rotations`。未设限位的别名为 null。编码器和受支持的开关帧不超过 1 s 时，反馈才未过期。编码器固件中的原始 `position`、`remaining`、`lower_limit` 和 `upper_limit` 也使用圈数。

标定阶段为 `none`、`lower`、`upper`、`center`、`done`、`cancelled` 和 `failed`。活动阶段计为忙碌，包括内部移动之间的间隔。`warning`、`direction_warning_count` 和 `recalibration_recommended` 报告方向暂停建议，不启动标定。

命令获接受不表示移动完成。`last_command.result` 可以为 `running`、`ok`、`failed`、`rejected` 或 `stopped`。只有未过期的空闲反馈确认实测目标在编码器容差内，移动才算完成。标定还要求 `done`。停止会取消待完成的移动命令。服务不自动重试移动。

受保护命令使用获取控制权后读取的 `stop_epoch`，整个序列保留该值。服务在运动锁内检查它。停止、控制权变化、断开、连接丢失和不受保护的命令会使其失效。续期保留该值。不匹配时返回 409。不得读取新序号来恢复已取消的序列。

每次按住移动生成新的 `hold_id`，长度为 16–64，只含字母、数字、下划线或连字符。`sampled_at` 必须来自服务时钟最近 2 s 内的状态。`release_hold` 可重复，并阻止使用相同 ID 后续启动。按住移动要求 1 s 内保活，与 3 s 控制租约分别续期。

发现接口报告 `renew_only_heartbeat: true`。使用 `ServiceClient.heartbeat()` 获取控制权，再用 `ServiceClient.renew()` 或 `heartbeat_thread(renew_only=True)` 续期。释放或过期后续期返回 409，不重新获取。无续期接口的旧服务返回 404，不回退。租约过期请求停止。单独释放不会停止移动。

专用停止路由返回 `stop` 和 `state`。结果为 `confirmed`、`pending` 或 `unconfirmed`。确认要求全部电机空闲，无活动标定或方向暂停。等待停止期间允许其他停止请求，但阻止新移动。串口结果未知或停止等待过期会锁定服务。后续空闲反馈不会单独清除该故障。确认实际位移台状态后，运行 `labctl restart stage` 并重新连接。停止确认进行中，重连返回 409。

### 位移台连接与兼容性

未连接时，状态的 `ports` 以 `{"device", "description"}` 列出可用串口。连接后列表为空。`MG400_STAGE_SERIAL` 将列表限制为该设备。通过 `POST /api/command` 发送 `connect` 命令，不提供独立的操作员连接路由。

旧 `steps`、毫米目标、命名脉冲位姿和相机校准不生成编码器移动。旧几何文件保持未启用。使用上述命令和基于圈数的 Sample Transfer 配置。

### 访问检查

服务不检查令牌、`Host` 或 `Origin`，见[访问与客户端 ID](#访问与客户端-id)。POST 请求必须含 `Content-Type: application/json`、`X-MG400-Client` 和 JSON 对象。客户端 ID 使用 1–128 个可打印 ASCII 字符。请求正文上限为 4096 字节。

操作接口错误使用 `{"error": "..."}`：

| 状态 | 含义 |
| --- | --- |
| 400 | 参数无效或串口不在列表中 |
| 409 | 租约、固件能力、标定或电机状态冲突 |
| 413 | 正文超过 4096 字节 |
| 415 | 请求不是 JSON |
| 503 | 串口失败，连接关闭 |

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
机器人下使能时，静止检查和角度一致性检查只用 J1–J3。下使能的 J4 没有保持力矩，工具的气管会转动它。

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
服务只检查起点和目标。控制器检查路径；路径超出范围时，控制器报警，例如 23 或 72。
复位使用 `JointMovJ`，参数为 `SpeedJ=5,AccJ=5`。
`execute` 和复位不修改全局速度或工具配置。

键盘点动开启时，服务发送 `SpeedFactor(<speed>)`，设置全局速度。
键盘点动关闭后，控制器保留该值。`execute` 和复位的实际速度 = 命令速度 × 全局 `SpeedFactor` × 控制器回放设置。验证速度前，先读取状态中的 `speed_factor`。
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
- 状态对象的 `vacuum` 含 `suck`、`blow` 和 `held`。`suck` 和 `blow` 是 DO2 和 DO1 的实测电平，无反馈时为 `null`。`held` 是 DI8（`GRIP_DI`）的实测电平；`GRIP_DI` 为 `None` 或无反馈时为 `null`。
- 状态对象的 `payload_kg` 是控制器反馈的负载，单位 kg。服务每次使能时用 `EnableRobot` 命令发送 `PAYLOAD_KG` 和 `PAYLOAD_CENTER_MM`。两个常量在 `src/mg400/joint_control.py` 中。`PAYLOAD_KG` 是末端工具、腕部相机和相机支架的总质量。`PAYLOAD_CENTER_MM` 是 J4 为 0° 时该质量的重心相对 J4 轴的位置。
- 常量在 `src/mg400/suction.py` 中：`PRESSURE_DO`、`VACUUM_DO`、`BLOW_SECONDS` 和 `GRIP_DI`。
- 离线模拟器响应 `DOExecute`，并在反馈帧中报告 DO 位。DO2 为高电平时，模拟器同时置高 DI8。
- 接线和反馈字段见 [真空吸盘](hardware.md#真空吸盘)。
