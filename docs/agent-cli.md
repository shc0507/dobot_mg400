[English](agent-cli.en.md)

# 智能体命令行

Olares 上的智能体用 `labctl` 设备命令组操作实验室设备。每次调用是 1 个进程，在 stdout 输出 1 行、1 个 JSON 对象。诊断信息输出到 stderr。

- 已安装：`sudo labctl <group> ...`。分派器以 UID 1000 运行调用，不分配终端。
- 代码检出：`uv run labctl <group> ...`，访问同一组回环服务。
- 演示：先启动 `uv run python -m orchestrator serve --demo --port 18772 --base-path /lab/`，再在每次调用中加 `--url http://127.0.0.1:18772/lab/`。

命令行不增加限制和检查。各服务保留测试台保护：停止、每台设备 1 个控制者、高温炉上限、机器人速度上限和报警阻止、显微镜对焦限位和物镜转换间隙位、电动台软限位。

## 会话和控制权

- `--session NAME` 选择智能体会话。默认值为 `main`。控制台页面为 `agent-NAME`，Overview **Control** 卡片上的持有者为 `agent-NAME-<12 hex>`。
- 每个智能体使用 1 个会话。同一会话的调用共享控制权。
- `labctl <device> take` 通过 Lab Console 获取控制权。控制台在两次调用之间持有租约，每 1 秒续租。
- 会话 10 分钟内没有调用时，控制台释放该会话的设备。释放机器人前，先将机械臂下使能。
- `labctl <device> release` 释放 1 台设备。`labctl all release` 释放该会话的所有设备。
- 所有者用 Overview 上的 **Release all** 或 `sudo labctl all release --session NAME` 收回控制权。

## 命令

| 命令 | 作用 |
| --- | --- |
| `labctl all state` | 所有设备的状态、控制持有者和当前运行 |
| `labctl all session` | 本会话的设备和剩余空闲时间（`idle_left_s`） |
| `labctl all events [--since ID] [--device D]` | 已保存的事件，最新在前。`ID` 是事件的 `id` |
| `labctl all history [--device D] [--from T] [--to T]` | 已保存的设备状态记录，最新在前。`T` 是 Unix 时间 |
| `labctl <device> state` | 1 台设备的完整状态、持有者和 `mine` |
| `labctl <device> describe` | 服务列出的命令及其 JSON Schema |
| `labctl <device> take`, `labctl <device> release` | 获取或释放控制权 |
| `labctl <device> call ACTION [NAME=VALUE ...]` | 发送 1 条服务列出的命令 |
| `labctl <device> wait [COMMAND_ID] [--timeout S]` | 等待用 `--no-wait` 发送的命令。对机器人，等到其空闲 |
| `labctl camera still VIEW [--out PATH]` | 从摄像头预览保存 1 张 JPEG，例如 `camera1` |
| `labctl microscope frame microscope\|side [--png] [--out PATH]` | 保存 1 帧显微镜或侧面摄像头图像 |

设备：`robot`、`stage`、`furnace`、`microscope`、`vision`、`thermal`。

在 `call` 中，`VALUE` 能解析为 JSON 时按 JSON 处理，否则按字符串处理。`NAME=@FILE` 从文件读取 JSON。`NAME=@-` 从 stdin 读取 JSON。参数由服务检查。

快捷命令：

| 快捷命令 | 发送 |
| --- | --- |
| `robot enable`, `robot disable`, `robot home`, `robot clear-alarm` | `enable`、`disable`、`home`、`clear_alarm` |
| `robot move X Y Z R [--speed N] [--relative]` | 带法兰 `targets` 的 `execute`。`--relative` 把数值加到当前 `actual` 位姿上 |
| `robot vacuum suck\|release\|off` | `vacuum` |
| `stage move MOTOR STEPS [--speed N]` | `move`。STEPS 的正负选择 `cw` 或 `ccw`。默认速度为 50 |
| `stage home MOTOR`, `stage zero MOTOR`, `stage goto MOTOR NAME`, `stage pose NAME` | `home`、带 `confirm` 的 `zero`、`goto`、`pose` |
| `stage connect [PORT]` | 连接电动台串口。不给 PORT 时列出串口 |
| `furnace setpoint C --ramp C_PER_MIN` | `apply_setpoint` |
| `furnace sequence FILE [--start]` | 从 JSON 文件发送 `set_sequence` 或 `start_sequence` |
| `furnace gas CHANNEL PERCENT`, `furnace valve CHANNEL open\|close` | `set_gas_flow`、`set_gas_valve` |
| `furnace acknowledge` | 确认高温炉报警 |
| `microscope objective ID`, `microscope nudge STEPS`, `microscope focus STEPS` | `select_objective`、`objective_nudge`、`focus_move` |
| `microscope autofocus full\|local`, `microscope zero`, `microscope home` | `autofocus`、`set_focus_zero`、`home_focus` |
| `microscope record start [--mode time_lapse\|live] [--name RUN_NAME]` | 带新 `recording_id` 的 `start_recording` |
| `microscope record stop ID` | `stop_recording` |
| `vision detect\|markers\|labels CAMERA` | `detect`、`read_markers`、`read_labels`。结果在 `state.last_command.output` 中 |

每条命令等待其结果。默认 `--timeout` 为 300 秒。`--no-wait` 在控制台接受命令后返回。

## 组合运行

| 命令 | 作用 |
| --- | --- |
| `labctl do RECIPE [NAME=VALUE ...] [--timeout S]` | 用本会话持有的设备运行 1 个目录配方 |
| `labctl chip locate SLOT` | 对 1 个槽位运行 `slot-state`。输出 `state`、`center_xy_mm` 和 `angle_deg` |

- 先获取配方用到的每台设备。否则调用失败，`hint` 列出要运行的 `take` 命令。
- 运行借用本会话的设备。租约仍由控制台持有，因此机械臂保持上使能，显微镜保留对焦零点。运行期间，本会话不能向这些设备发命令。停止仍然有效。
- 运行期间，命令行持有编排器租约。失败步骤后保持的运行会被终止。运行结束后，设备归还本会话。
- 运行期间命令行退出时，编排器租约过期。运行保持，运动停止。在 Overview 上终止该运行。
- 默认 `--timeout` 为 1800 秒。超时后，命令行终止运行。
- 输出包含 `run_id`、`status`（`completed` 或 `aborted`）、`steps`（含 `skill`、`status`、`result` 和 `error`）以及 `devices`。
- 单步目录配方：`slot-state`、`station-transfer`、`crucible-load`、`crucible-unload`、`microscope-record`、`check-calibration`。目录中还有 `chip-pick` 和 `tray-transfer`。

## 停止

停止不需要控制权和会话。只发送 1 次，不重试。

| 命令 | 作用 |
| --- | --- |
| `labctl all stop` | STOP ALL MOTION：控制台停止（同时保持运行）以及机器人、电动台和显微镜的停止 |
| `labctl <device> stop` | 停止 1 台设备 |
| `labctl furnace stop` | 关闭加热和气体 |
| `labctl furnace gas-off [CHANNEL]` | 停止 1 路气体，或全部 3 路 |

使用默认 `--url` 时，每次停止还直接发到回环上的服务，因此控制台停机时也有效。使用其他 `--url` 时，停止只经过该控制台。

没有同步客户端的机器人服务无法确认停止。它报告 `unconfirmed` 并锁定。用 `labctl robot take` 和 `labctl robot disable`，或控制台中的 **Recover** 恢复。

## 输出和退出码

命令结果：

```json
{"ok": true, "device": "stage", "action": "move", "outcome": "ok", "command_id": "42", "message": "...", "last_command": {}, "state": {}}
```

拒绝：

```json
{"ok": false, "device": "robot", "action": "execute", "outcome": "refused", "error": {"code": "not_controlled", "status": 409, "message": "Take control of this device first."}, "hint": "labctl robot take"}
```

- `outcome`：`ok`、`accepted`、`failed`、`rejected`、`stopped`、`refused`、`unknown`、`interrupted`。
- `error.code`：`usage`、`invalid`、`not_controlled`、`held_by_other`、`locked`、`busy`、`refused`、`failed`、`rejected`、`stopped`、`unknown`、`timeout`、`unreachable`、`interrupted`。
- `hint`：适用时给出下一条要运行的命令。
- 停止输出 `results`，每台设备为 `confirmed`、`pending`、`unconfirmed`、`unreachable` 或 `not-configured`。

| 退出码 | 含义 | 下一步 |
| --- | --- | --- |
| 0 | `ok` 或 `accepted` | 继续 |
| 1 | 被拒绝、失败、被驳回或已停止 | 读取 `error` 和 `hint` |
| 2 | 用法错误或参数无效 | 修正调用 |
| 3 | 结果未知：超时、发送后连接中断或停止未确认 | 先读取状态。未检查前不要重复运动 |
| 130 | 被中断。命令行已向正在等待的设备发送 1 次停止 | 读取状态 |

## 限制

- 没有保持运行的操作员提示：`do` 终止该运行。
- 不支持键盘点动和电动台按住运动。它们需要每 100 毫秒和 500 毫秒刷新。
- 命令行退出前已开始的运动按命令继续。
- 未给 `--out` 时，图像文件保存到 `/tmp/labctl/`。已安装的命令行以 UID 1000 运行，不能写入 `/home/olares`。
