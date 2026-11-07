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
`stage` 组含位移台服务使用的 Flask、waitress 和 pySerial。
机器人服务和交互终端只用标准库，不依赖 `stage` 组。

测试不连接机械臂或 Arduino。
拉取请求和推送到 `main` 时，GitHub Actions（`.github/workflows/ci.yml`）在 Python 3.12 上运行以上检查。
`pytest` 运行 Python 测试，`node --test` 运行实验室控制台的 JavaScript 测试。
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

在另一个终端用 [Python 客户端](#python-客户端) 连接 `http://127.0.0.1:18766/`，按以下顺序发送命令：

1. 运行 `start_sync`，等待反馈更新。
2. 运行 `enable`，等待使能完成。
3. 用 `execute` 发送目标坐标。
4. 用 `grip` 发送夹爪目标，确认反馈位置更新。
5. 运行 `stop_sync`，等待停止确认，再运行 `disable`。

在终端按 `Ctrl+C` 结束机器人服务。
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
| `apps/control_panel/` | 独立 Python 机器人服务，不提供网页 |
| `apps/layout-studio/` | 离线布局编辑器，使用模拟数据，不发送控制命令 |
| `config/systemd/` | 所有服务的 systemd 定义，配置从 `/etc/mg400/mg400.env` 读取 |
| `deploy/` | 安装器 `install.py`、日常命令 `mg400ctl` 和配置示例 |
| `src/mg400/` | 后端、控制协议和反馈处理 |
| `src/mg400/sim/` | 回环控制器和文件夹爪模拟器，共用测试模型 |
| `src/stage_controller/` | 位移台服务：Flask 接口和串口协议 |
| `firmware/three-motor-demo/` | 位移台的 Arduino 固件和接线说明（英文） |
| `scripts/` | 摄像头预览、控制模式切换和 USB 采集 |
| `examples/` | 单文件教学和 notebook |
| `tests/` | Python 和 JavaScript 离线测试 |
| `docs/` | 操作、部署和开发指南，`docs/agents/` 保存代理操作规程 |
| `work/` | 本地运行数据、备份和临时文件，不纳入 Git |

用浏览器打开 [布局编辑器](../apps/layout-studio/index.html)，可编辑并导出界面布局。

`terminal_control.py` 提供 `mg400-robot` 真机交互终端。
它复用 `web_control.py` 的连接锁、反馈检查和动作执行，不启动 HTTP 服务。

`apps/control_panel/server.py` 是机器人服务，提供 HTTP 接口和 WebSocket 接口，不提供网页。
实验室控制台的 Motion 工作区通过该服务控制机械臂和夹爪。
`websocket.py` 用标准库实现 WebSocket 帧编解码和连接写线程。
接口说明见 [机器人服务接口](#机器人服务接口)。

WebSocket 按键点动由 `web_control.py` 调度，机械臂长按复用 `keyboard_jog.py`。
`web_keyboard.py` 校验按键输入并执行夹爪小步。

机器人服务通过 TCP 协议控制机械臂。
`controller_protocol.py` 解析命令应答，`feedback.py` 读取关节反馈。
`joint_control.py` 执行受限运动，`gripper_feedback.py` 处理夹爪反馈。
TCP 协议参考与完成判定见 [真机控制接口](hardware.md)。
当前没有 ROS 2 后端或视觉感知。

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
| `GET {前缀}api/state` | 读取一次状态 |
| `GET {前缀}api/describe` | 读取契约版本、命令参数和租约行为 |
| `POST {前缀}api/stop` | 请求停止并读取确认结果 |
| `GET {前缀}api/events?since=<seq>` | 读取指定序号之后的日志事件 |
| `POST {前缀}api/command` | 发送一个命令 |
| `POST {前缀}api/heartbeat` | 续期控制租约 |
| `GET {前缀}api/ws` | WebSocket 连接：状态推送、命令、心跳和按键 |

`{前缀}` 是 `--base-path` 的值，默认为 `/`。
按键只能经 WebSocket 发送。

### 访问与客户端 ID

服务监听 `127.0.0.1`，不检查令牌、`Host` 或 `Origin`。VPN 是访问边界。

真机控制模式用客户端 ID 区分客户端，长度为 1–128 个可打印 ASCII 字符。
同一时刻只有一个客户端 ID 持有控制权。
控制者必须在 3 秒内续期控制租约（控制权的有效期）。
心跳和按键更新都能续期。
服务 3 秒内未收到续期时，请求停止机械臂并锁定控制。

### HTTP 接口

POST 请求必须满足以下条件：

1. 带 `Content-Type: application/json` 和 `X-MG400-Panel: 1`。
2. 真机控制模式带 `X-MG400-Client: <客户端 ID>`。
3. 正文为 1–4096 字节的 JSON 对象，用 `Content-Length` 标明长度。

`api/command` 和 `api/heartbeat` 成功时返回 200 和状态对象。
拒绝时返回 400（请求无效）、403（拒绝）或 409（状态冲突）。
拒绝的正文为 `{"error": "..."}`。

命令正文为 `{"action": "<命令>", ...}`，不得包含其他字段：

| 命令 | 其他字段 | 行为 |
| --- | --- | --- |
| `start_sync` | 无 | 连接机械臂并取得控制权 |
| `stop_sync` | 无 | 停止机械臂并释放控制权，保留反馈直到下使能和停稳获确认 |
| `enable` | 无 | 使能机械臂 |
| `disable` | 无 | 下使能机械臂，任何客户端都可发送 |
| `stop` | 无 | 停止机械臂，任何客户端都可发送 |
| `home` | 无 | J1–J4 移动到 0° |
| `execute` | `targets`：`x`、`y`、`z`、`r`，可选 `grip`，另有可选 `speed` | 直线移动到法兰坐标，到位后执行夹爪目标 |
| `grip` | `position`：0–1000 的整数 | 设置夹爪开口 |
| `initialize_gripper` | 无 | 初始化夹爪，会张开夹指 |
| `vacuum` | `operation`：`suck`、`release` 或 `off` | 控制真空吸盘，见 [真空吸盘](#真空吸盘) |
| `keyboard` | `enabled`、`seq`，开启时加 `speed`，关闭时加 `reason` | 开启或关闭键盘控制 |
| `clear_alarm` | 无 | 发送 `ClearError()`，报警清除后下使能并确认静止 |

对于 `keyboard`，`speed` 是 1–100 的整数。
对于 `execute`，可选 `speed` 是整数，范围从 1 到主机的 `MG400_ROBOT_MAX_SPEED_L` 上限。
值无效或超出上限时，服务返回 400，不会将运动排队。
布尔值、字符串、小数和非有限值均无效。

运动使用 `SpeedL=<speed>,AccL=<min(speed, acceleration cap)>`。
加速度上限为 `MG400_ROBOT_MAX_ACC_L`。
两个主机配置均默认为 1，接受 1–100 的整数。空值也使用 1。
主机配置无效时，服务拒绝启动。修改上限后，重启服务。

未提供 `speed` 时，即使主机上限提高，协议命令仍为 `SpeedL=1,AccL=1`。
这些配置不改变 `home`、全局 `SpeedFactor` 或键盘点动。
无人值守搬运使用高于 1 的速度前，操作员必须先在硬件上验证。
`clear_alarm` 在控制器报警且反馈新鲜时可用。
`recoverable` 为 `true` 时也可用：停止或下使能未确认，控制器无报警，且反馈新鲜。
此时服务不发送 `ClearError()`，只下使能、确认静止并清除警告。
状态对象的 `alarms` 列出当前报警，无报警时为 `null`。
每项含 `id`、`source`（`controller` 或 `J1`–`J4`）、`description` 和 `solution`。
`reason` 取值为 `user`、`focus`、`edit`、`speed` 或 `modifier`。

### 契约发现、停止与事件

真机控制模式提供 `api/describe`、`api/stop` 和 `api/events`。
`api/stop` 检查上述 POST 请求头和客户端 ID。

`api/describe` 返回 `service`、`version`、`contract`、`demo`、`zones`、`lease_seconds`、`on_lease_expiry` 和 `commands`。
`contract` 为 1，`demo` 为 `false`。
每个命令的 `params` 用 JSON Schema 描述参数，不含 `action`。
`stop` 和 `disable` 的 `any_client` 为 `true`。
`keyboard` 的 `ui_only` 为 `true`，只供交互界面使用。
`start_sync` 取得控制权，原心跳接口只续期。

`api/stop` 的正文为 `{"wait_s": 0}`。
`wait_s` 可省略，默认为 0，范围为 0–15 秒，必须是有限数值。
接口沿用停止工作线程，等待时释放状态锁。
任何客户端都能停止，无须持有控制租约。
重复请求不改变既有的停止去重规则。
成功响应为 200 和 `{"stop": "confirmed", "state": {...}}`。

| `stop` | 含义 |
| --- | --- |
| `confirmed` | 机械臂停止已确认，夹爪无活动命令或锁定 |
| `pending` | 等待期限已到，停止工作线程或夹爪命令仍在执行 |
| `unconfirmed` | 机械臂停止未确认，或夹爪已锁定 |

停止机械臂不会中断正在执行的夹爪命令。
先前的停止未确认时，后续停止成功也不能清除此状态。
响应保留锁定状态，不自动重试运动。

状态的 `last_command` 初始为 `null`。
命令完成或被拒绝后，该字段包含 `id`、`action`、`client`、`result`、`message` 和 `finished_at`。
`finished_at` 是 Unix 秒数。
`result` 为 `ok`、`failed`、`rejected` 或 `stopped`。
异步命令在工作线程完成后更新结果。
停止结果也等待活动夹爪命令结束，夹爪锁定时记为 `failed`。
其他客户端的拒绝请求不会隐藏正在执行的命令的最终结果。
已取消的旧工作线程不能覆盖较新的已完成停止。

`api/events` 返回 `{"events": [...], "next": 0}`。
每个事件含 `seq`、`ts_unix`、`level`、`message` 和 `fields`。
`since` 默认为 0，必须是非负整数。
响应只包含序号大于 `since` 的事件，下一次查询使用 `next`。
服务保留最近 1000 个事件，重启后序号重新开始。
事件对应既有的状态消息、锁定、报警、键盘关闭原因和夹爪锁定日志，不增加日志行。

### Python 客户端

`mg400.client.RobotClient(url, client_id)` 使用服务基础 URL 和明确的客户端 ID。
每个程序使用独立 ID。运行中的程序不得共用 ID。
构造对象不会连接服务。`connect()` 检查 WebSocket 协议 1 和服务契约 1，然后启动客户端线程。
支持 `http://`、`https://`、`ws://` 和 `wss://`。如果服务配置了基础路径，URL 必须包含该路径。

运行示例前，启动[离线模拟器和机器人服务](#离线机器人模拟器)。
示例向该模拟器发送运动命令。

```python
from mg400.client import RobotClient

with RobotClient("http://127.0.0.1:8766/", "recipe-example") as robot:
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
- `RobotClientError` 表示请求被拒绝或结果未知。如果服务提供错误码，异常保留 `status` 和 `close_code`。

`run()` 接受 `ok`。对于 `stop`、`disable` 和 `stop_sync`，停止仍获确认时，也接受 `stopped`。
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
服务随后按原有租约超时规则停止机械臂并锁定控制。
可选参数 `timeout=5` 配置连接和回复超时，单位为秒。

### WebSocket 接口

按以下顺序通信：

1. 连接后 3 秒内，发送 `{"type":"hello","protocol":1,"client":"<客户端 ID>"}`。
2. 等待服务推送 `{"type":"state","state":{...}}`。
3. 发送请求 `{"type":"<类型>","id":<整数>,"body":{...}}`。
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

关闭码为 4400、4426 或 1013 时，服务先发送 `{"type":"error","status":<状态码>,"error":"..."}`，再关闭连接。

| 关闭码 | 含义 |
| --- | --- |
| 4400 | hello 或请求格式无效 |
| 4426 | `protocol` 与服务不一致，客户端必须更新 |
| 4409 | 同一客户端 ID 的新连接替换了此连接 |
| 1013 | 连接数已满 |
| 1001 | 服务停止，关闭前推送最后状态 |
| 1002、1003、1007、1009 | 帧格式错误、二进制消息、文本不是 UTF-8 或消息过长 |

连接规则如下：

- 消息只能是不超过 4096 字节的 UTF-8 文本。
- 服务每 5 秒发送 ping，15 秒未收到任何帧时关闭连接。
- 最多 8 个客户端 ID 同时保持 hello 后的连接。
- 同一客户端 ID 再次发送 hello 时，服务关闭旧连接。
- 客户端的最新连接关闭，或同一客户端 ID 的新连接替换它时，服务立即关闭该客户端的键盘控制。

连接关闭不终止控制租约。
服务或代理关闭连接、客户端 3 秒内重连并续期时，保留控制权。
网络中断时，服务可能检测不到连接关闭，由按键超时和控制租约停止机械臂。

## 位移台服务接口

位移台服务的 HTTP 接口使用 JSON，全部位于 `{前缀}contract/api/`。
`{前缀}` 是位移台基础路径，固定为 `/`。
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
服务拒绝缺少或多余的参数。

控制租约默认有效 3 秒，长按仍须在 1 秒内单独续期。
长按续期不续期控制租约。
labkit Flask 适配器在发现响应中声明 `renew_only_heartbeat: true`。
要持续持有控制权，先用 `ServiceClient.heartbeat()` 明确取得租约，再用 `ServiceClient.renew()` 续期。
可选的 `heartbeat_thread(renew_only=True)` 辅助线程使用严格续期，首次出错后停止。
续期请求在租约过期或释放后到达时，返回 409，不会重新取得控制权。
旧服务没有 `api/renew` 时，返回 404。客户端不会改为取得租约。
原有 `heartbeat()` 和默认 `heartbeat_thread()` 保留取得或续期租约的行为。
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
| `GET /contract/api/ports` | 无 | 列出可连接的串口，不含夹爪转换器。设置 `MG400_STAGE_SERIAL` 时只列出该设备 |
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

未提供 `speed` 时，`execute` 的 `MovL` 使用 `SpeedL=1,AccL=1`。
键盘有限轨迹中，W/S、R/F、Q/E 的 `MovL` 使用 `SpeedL=2,AccL=20`。
控制器逆解验证目标关节范围，控制器规划直线路径。
复位使用 `JointMovJ`，参数为 `SpeedJ=5,AccJ=5`。
A/D 的有限轨迹使用 `JointMovJ`，参数为 `SpeedJ=5,AccJ=20`。
两类关节动作检查整段关节范围，不包含现场碰撞检查。
机械臂运动不修改全局速度或工具配置。

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
- 吹气期间停止时，DO1 可能保持高电平。恢复后发送 `off`。
- 状态对象的 `vacuum` 含 `suck`、`blow` 和 `held`。`suck` 和 `blow` 是 DO2 和 DO1 的实测电平，无反馈时为 `null`。未设置 `GRIP_DI` 时，`held` 为 `null`。
- 状态对象的 `payload_kg` 是控制器反馈的负载，单位 kg。
- 常量在 `src/mg400/suction.py` 中：`PRESSURE_DO`、`VACUUM_DO`、`BLOW_SECONDS` 和 `GRIP_DI`。
- 离线模拟器响应 `DOExecute`，并在反馈帧中报告 DO 位。
- 接线和反馈字段见 [真空吸盘](hardware.md#真空吸盘)。

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
机器人服务与采集进程必须位于同一主机，并共享同一单调时钟域。
位置命令不支持跨主机传递，也不支持带时钟偏移的时间命名空间。
文件准备、采集接收、寄存器操作和到位确认共用命令时限。
位置命令为 5 秒，初始化为 15 秒。
机器人服务在准备命令文件前开始计时，采集进程不得重新计时。

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

机器人状态保留原有夹爪字段，并增加 `gripper.outcome` 和 `gripper.outcome_at`。
`outcome` 为 `null`、`arrived`、`contact` 或 `dropped`。
`outcome_at` 是适配器观察到该结果时的 Unix 时间戳，单位为秒。无结果时为 `null`。

新的夹爪操作在发布前清空两个字段。操作进行期间，机器人状态也隐藏旧结果。
初始化不产生抓取结果。

`arrived` 保留测量到位结果，`contact` 保留已确认的接触结果。
在 `contact` 之后，已初始化的新鲜样本报告状态 `3` 时，观察结果变为 `dropped`。
样本必须来自接触结果所在的采集会话，且序号更大。
采集进程重启、过期样本或重复样本都不能触发该转换。

原有采集轮询在操作间隙和运动任务结束后观察掉落。
每次新掉落产生 1 条警告事件，包含 `outcome` 和 `outcome_at` 字段。重复轮询不改变时间戳，也不重复记录事件。

反馈不可用时，结果和时间戳仍作为历史信息保留，不证明当前仍持有物体。
即使观察结果变为 `dropped`，`WebGripper.last_outcome` 仍保留上次命令的 `arrived` 或 `contact` 结果。
这让键盘步进在接触后继续保持停止。掉落观察不发送命令，也不清除锁定。
状态定义见 [PGEA 用户手册表 2.9](https://www.manualslib.com/manual/4358849/Dh-Robotics-Technology-Pgea-Series.html?page=17)。
