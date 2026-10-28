# 真机控制接口

[返回首页，选择操作入口](../README.md)

本页说明真机控制的 TCP 协议和反馈判定。
现有真机功能直接通过 TCP 接入，不提供通用抓放接口。
日常操作请使用对应指南：

- [网页面板](control-panel.md)：独立网页真机控制。
- [交互终端](terminal-control.md)：终端命令控制机械臂和夹爪。
- [自然语言 J4 受限试验](agent.md#自然语言-j4-受限试验)：仅接受预设的转出和回位动作。

网页面板和交互终端共用[机器人地址配置](olares.md#机器人地址与端口)。
J4 命令行试验通过 `--host` 和 `--source-host` 指定网络地址。

## 直接 TCP 接口

- [MG400 / M1Pro 四轴 Python SDK](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python)
- [SDK 源码](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python/blob/main/dobot_api.py)
- [四轴 TCP/IP 协议仓库](https://github.com/Dobot-Arm/TCP-IP-Protocol-4AXis)
- [四轴协议，2024-04-19 英文版](https://github.com/Dobot-Arm/TCP-IP-Protocol-4AXis/blob/master/TCP_IP%20Remote%20Control%20Interface%20Guide%20%284axis%29_20240419_en.pdf)

上述 Python README 标注控制器 V1.5.6.0+。
故障排查建议尝试 V1.6.0.0+。
最终以本机固件对应的官方手册为准。
控制前，进入 TCP/IP 二次开发模式。
可在 DobotStudio Pro 中选择，也可使用下文脚本。
不要混用六轴机器人 SDK。

### 从服务器切换 TCP/IP 模式

[`scripts/mg400_control_mode.py`](../scripts/mg400_control_mode.py) 默认只读。
它通过 SMB（网络文件共享）读取模式，通过 HTTP 读取运行状态。
`configured_mode` 为保存的配置，不能单独证明模式已生效。
`tp` 表示在线模式，`tcp` 表示 TCP/IP 模式。

脚本要求 Python 3.11+ 和 `pysmb`。
服务器须能访问机械臂的 TCP `445`、`22000` 和 `29999`。
在能访问机械臂的主机运行，例如 Olares 宿主机，使用 `UID:GID 1000:1000`。
首次使用时，将 `pysmb` 装入独立目录：

```sh
python3 -m pip install --target "$HOME/mg400-mode-deps" pysmb
```

每次运行前，设置依赖目录和共享锁目录，再在项目根目录运行。
锁目录与网页服务一致，见[面板连接锁](olares.md#面板连接锁)：

```sh
export PYTHONPATH="$HOME/mg400-mode-deps${PYTHONPATH:+:$PYTHONPATH}"
export MG400_CONTROLLER_LOCK_DIR=/olares/userdata/Cache/pvc-appcache-qumusphusis-aah2phqnttncsxoh/isaaclab/documents/mg400-data/controller-locks
python3 scripts/mg400_control_mode.py
```

切换前，下使能机械臂并停止运行中的程序。
关闭其他控制客户端，释放[面板连接锁](olares.md#面板连接锁)。
切换时，在命令末尾加 `--switch-to-tcp`。
脚本默认读取 `config/robot.toml`，可用 `--host` 指定地址。
切换仅接受原模式 `tp` 或 `tcp`，并核对下使能、程序停止及无报警状态。
脚本先备份配置，再写入 `project` 共享内的 `/settings/function/remoteControl.json`。
随后向 `22000` 端口的 `/settings/function/remoteControl` POST 同一配置。
目标字段为 `{"mode":"tcp","name":""}`，其他字段保持原值。
此顺序来自 [DobotLink 源码](https://github.com/Dobot-Arm/DobotLink/blob/80c6ad78010721648ad671cc47320604d303f1d9/Plugins/IndustrialRobotPlugin/Device.cpp#L3239)。

成功要求 HTTP 返回 `status: true`、配置读回一致且 `RobotMode()` 成功返回 `4`。
脚本不使能、不清除报警，也不发送运动命令。
备份默认存入 `work/control-mode/`。
可用 `--backup-dir` 修改备份目录。
写入后发生错误时，控制器状态可能已改变。
脚本会报告备份路径，不会自动重发切换请求或回滚。
先核对状态和报错，再决定是否重新执行。
该流程依赖官方客户端使用的接口，固件升级后须重新核对兼容性。

### 官方 SDK 接口

| 操作 | 官方四轴接口 / 说明 |
| --- | --- |
| 建立控制连接 | `DobotApiDashboard(ip, 29999)` |
| 建立运动连接 | `DobotApiMove(ip, 30003)` |
| 反馈连接 | `DobotApi(ip, 30004)`，需正确拆分二进制帧 |
| 读取位姿 | `dashboard.GetPose()`。XYZ 毫米，R 角度 |
| 直线运动 | `motion.MovL(x, y, z, r, *params)` |
| 等待队列完成 | `motion.Sync()`。还需核对新鲜位姿与控制器状态 |
| 受控停止映射参考 | `dashboard.ResetRobot()`，手册描述为停止并清空规划队列 |
| 电动夹爪 | 取决于具体夹爪的 I/O 或通讯协议 |

协议支持 `GetPose(User=0,Tool=0)`，但上述 Python 包装方法 `GetPose()` 不接收参数。
必须明确配置/验证当前 User、Tool 和 TCP。
不能假定返回数据与场景使用同一坐标系。
官方 `MovL` 可选参数是字符串形式，例如 `"User=0"`，不是任意 Python 关键字参数。

## 完成判定与失败处理

TCP 响应 `ErrorID,{values},Command(...);` 中的成功码只表示命令成功接收。
排队运动的应答不代表已经到位。
发送运动命令后，解析成功应答并等待队列完成。
然后检查新鲜位姿和状态，处理报警和超时。
官方示例传输代码不能直接当作可靠完成保证。

收到超时或断线后，动作可能已经部分执行。
此时停止会话，重新核对物理状态，不要自动重发运动或清除报警。
`SpeedFactor` 是相对控制器设置的百分比，不是绝对 mm/s。
软件停止命令不等价于独立硬件急停。
真实系统的限速、限位、急停与故障监测须在设备/控制器层实现。

电动夹爪开爪后必须确认完成，未确认则按失败处理。
闭爪后，读取夹持到物体的反馈。
单纯“闭爪完成”不能证明已抓住物体。
无夹持检测时，需增加外部检测或明确保留未验证状态。

## 报警与历史日志

`mode=9, error=True` 只表示故障状态，不含具体报警码。
`GetErrorID()` 只读取当前报警，不能还原最初故障。
`control_status.json` 会反复覆盖，也不能作为历史记录。

查询最初故障时，按以下顺序排查：

1. 在能访问控制器的主机打开终端，例如 Olares 宿主机。
2. 通过 SMB（网络文件共享）读取控制器日志。
3. 按日志顺序核对碰撞、暂停和急停记录。

控制器地址见 `config/robot.toml`，当前为 `192.168.1.6`。
SMB 使用 TCP `445`，共享为 `project`，日志目录为 `/logs`。
此路径独立于 dashboard 查询，不必关闭网页面板。
只读排查期间，不清除报警、不使能、不发送运动命令。

| 控制器共享内的路径 | 用途 |
| --- | --- |
| `/logs/channel_0` | 控制器事件、报警状态和命令返回值 |
| `/logs/algs/algs_info` | 运动算法事件，含带时间戳的碰撞检测记录 |
| `/logs/gcontrol/log`、`/logs/codesys/log` | 补充核对控制程序记录 |
| 同目录下带 `.1`、`.2` 等后缀的文件 | 轮转日志，即分卷保存的旧日志 |

先读取前两个文件。
如果时间范围不足，按目录清单选择旧日志。
文件修改时间只用于筛选，事件时间以日志内容为准。

缺少 SMB 客户端时，将 `pysmb` 装入临时目录。
下列命令匿名连接共享，列出文件并保存两个日志副本：

```sh
export MG400_LOG_DEPS=$(mktemp -d /tmp/mg400-smb.XXXXXX)
export MG400_LOG_DIR=$(mktemp -d /tmp/mg400-logs.XXXXXX)
python3 -m pip install --target "$MG400_LOG_DEPS" pysmb
PYTHONPATH="$MG400_LOG_DEPS" python3 - <<'PY'
import os
from pathlib import Path
from smb.SMBConnection import SMBConnection

robot_ip = "192.168.1.6"  # 与 config/robot.toml 保持一致
output = Path(os.environ["MG400_LOG_DIR"])
connection = SMBConnection(
    "", "", "mg400logreader", "MG400",
    use_ntlm_v2=True, is_direct_tcp=True,
)
try:
    assert connection.connect(robot_ip, 445, timeout=8)
    for directory in ("/logs", "/logs/algs"):
        for entry in connection.listPath("project", directory, timeout=15):
            if not entry.isDirectory:
                print(directory, entry.filename, entry.file_size, entry.last_write_time)
    for source in ("/logs/channel_0", "/logs/algs/algs_info"):
        destination = output / source.strip("/").replace("/", "__")
        with destination.open("wb") as stream:
            connection.retrieveFile("project", source, stream, timeout=20)
        print(destination)
finally:
    connection.close()
PY
rg -n -C 2 'collision!|collisionStatus|碰撞检测|alarm ID:' "$MG400_LOG_DIR"
rg -n -A 14 'func = geterrorid' "$MG400_LOG_DIR"
```

根据目录清单修改 `source` 列表，可读取旧日志或补充日志。
匿名认证失败时，可用空密码和用户名 `Guest` 重试。
查询结束后，仅清理本次创建的临时目录。
需保留的证据存入数据目录的 `work/`，不写入操作文档。

判断时保留以下区别：

- `collision!`、`collisionStatus` 和“碰撞检测 Pause”用于核对碰撞检测及暂停。
  出现这些记录，不等于读到了数值报警码。
- 报警 `85` 表示硬件急停按钮按下或软件急停触发。
  后出现的 `85` 不能替代最初事件。
- 控制器时钟与面板日志时钟可能不同。
  保留原始时间，先比较同一控制器内的顺序和间隔。
  跨日志对齐前，核对时区和时钟偏差。

查询当前报警码时，先断开网页面板和交互终端，再独立连接 `29999` 调用 `GetErrorID()`。
面板轮询与外部查询可能互相断开连接。

路径参考：[MG400 日志导出说明](https://techshare.co.jp/faq/dobot/mg400/support.html)。
报警码参考：[Dobot 四轴报警表](https://raw.githubusercontent.com/Dobot-Arm/TCP-IP-4Axis-Python/main/files/alarm_controller.json)。

## 真机验证

按以下顺序验证：

1. 核对固件、机器人 IP、夹爪型号、供电和 I/O 或 Modbus 通讯参数。只读测试位姿与报警。
2. 设定工具 TCP 和负载。用示教器记录真实起点、抓取点、放置点与路径。
3. 在受控工作区低速逐步验证单次动作，再验证固定抓放。
4. 接入 agent 前，补充路径可达性、碰撞检查和独立于模型的设备保护。

## 可选 ROS 2 路线

如果采用 ROS 2，可参考 [HarvestX/MG400_ROS2](https://github.com/HarvestX/MG400_ROS2)。
本文接口说明基于 `humble` 分支提交
[`1a8c7344be44c52030f7216c0f387873ed91fd11`](https://github.com/HarvestX/MG400_ROS2/tree/1a8c7344be44c52030f7216c0f387873ed91fd11)。

上游 [README](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/README.md)
列出的环境是 Ubuntu 22 + ROS 2 Humble、MG400 固件 1.5.6.0。
仓库提供机器人模型、RViz 显示、驱动节点、运动 action 和控制 service。
`display.launch.py` 可无硬件显示模型，但不等于物理仿真或完整避障规划。

本项目当前没有 ROS 2 后端。
以下名称基于 `main.launch.py` 的默认 `mg400` 命名空间。
修改 namespace 时，同步调整接口名称。

| 操作 | HarvestX 对应接口 / 接入要求 |
| --- | --- |
| 直线运动 | `/mg400/mov_l`，类型 `mg400_msgs/action/MovL`。等待最终 action 状态并检查 `result` |
| 读取位姿 | 结合 `/mg400/joint_states`、TF、连接和报警状态取得新鲜位姿，核对法兰与夹爪 TCP 的关系 |
| 受控停止 | 评估 `/mg400/reset_robot` service 并核实停止反馈。不能只发送 action cancel |
| 电动夹爪输出 | `/mg400/tool_do_execute`，仅在夹爪确实通过工具端数字 I/O 控制时使用 |
| 电动夹爪输入 | `/mg400/tool_di`，按夹爪手册解释输入信号。读取成功不等于夹持成功 |

接入要求与限制如下：

1. 单位转换：本项目使用 mm / deg。`MovL` 的 `PoseStamped` 使用米和四元数，
   驱动取 yaw 弧度后再转换成设备协议单位。适配器需转换长度、角度并指定正确 TF frame。
   依据：[运动命令实现](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_interface/src/commander/motion_commander.cpp)。
2. 取消与停止：此版本 `MovL::handle_cancel` 只返回接受，停止仍为 TODO，
   超时 / abort 分支也未主动发送停止命令，不能据此报告机械臂已停止。
   其执行路径还存在发送异常后直接返回、未结束 action 的分支。
   客户端必须有自己的期限和故障处理。
   依据：[MovL 实现](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_plugin/src/motion_api/mov_l.cpp)。
3. 到位精度须复核：此版本 `MovL` 根据位姿误差和 ENABLE 状态判断完成，
   位置阈值为每轴 5 mm、角度阈值为 0.0174 rad。抓取任务需采用自己的验收精度。
   同一源码中有固定 10 秒执行期限，不能假定客户端期限会自动传到服务端。
4. 夹爪适配：仓库有工具端 I/O 服务，但未提供适用于任意电动夹爪的抓取确认。
   若夹爪使用 Modbus 或其他通讯，需实现相应协议。不能直接套用数字输出。
   依据：[ToolDOExecute](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_plugin/src/dashboard_api/tool_do_execute.cpp)、
   [ToolDI](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_plugin/src/dashboard_api/tool_di.cpp)。

实现 ROS 2 适配器前，先确定部署环境。
本项目要求 Python 3.11+。
`rclpy` 必须与所安装 ROS 发行版的 Python 环境兼容。
不能假定当前 macOS 的 venv 可直接使用。
先在上游支持的 Ubuntu / Humble 环境验证驱动。
再确定 agent 同进程运行还是通过独立桥接进程通信。
