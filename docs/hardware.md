# MG400 与电动夹爪适配

通用抓放真机入口 `src/mg400_agent/hardware.py` 仍会抛出 `NotImplementedError`。
现有真机功能通过直接 TCP 接入，使用独立入口：

- [Isaac 四轴面板](isaacsim.md#在-isaac-中控制真机四轴)：单次受限关节运动与实际姿态同步。
- [Isaac 夹爪面板](isaacsim.md#在-isaac-中控制实体夹爪)：MG400 I/O 开合与 USB 位置反馈。
- 下述自然语言 J4 试验：仅接受预设的转出和回位动作。

这些入口尚未接入通用 `Arm` / `Gripper` 抓放接口。
通用适配可继续使用直接 TCP，也可采用后文的 ROS 2 路线。
Isaac 真机入口共用 [机器人地址配置](isaacsim.md#真机地址配置)。
下述命令行试验仍通过 `--host` 和 `--source-host` 指定网络地址。

## 自然语言 J4 受限试验

`examples/agent_j4_trial.py` 把固定中文任务交给 `.env` 配置的模型，由模型顺序调用
`get_robot_state`、`rotate_j4_out`、`return_to_start`。执行器只接受空参数的预设工具：
程序以启动时读取的实际 J4 角度为起点。
J4 从起点增加 2°，到位后返回起点。每个动作最多执行一次。
此入口不支持通用真机聊天或抓放，也不使用仿真的四轴零位作为真机起点。
模型 API 地址、认证和 `.env` 用法见 [模型 API 配置](../README.md#模型-api-配置)。

确认夹爪、线缆周围有空隙且操作者在急停旁后，运行以下命令会实际使能和运动：

```bash
uv run --env-file .env --extra llm python examples/agent_j4_trial.py \
  --host 192.168.1.6 --source-host 192.168.1.10 --execute-j4-trial
```

试验使用每条指令的 `SpeedJ=5,AccJ=5` 比例参数；不改全局速度、负载、工具坐标或夹爪输出。
被动反馈超过 0.25 秒未刷新、报警或离开限定关节区间会中止动作。
到位要求新鲜反馈在目标 ±0.1° 内稳定至少 0.4 秒，并由 dashboard 再核对。
发生故障不重发运动，也不自动回位；必要时请求 `ResetRobot()` 并核实静止。
如果无法确认停止，终端会明确要求操作者使用物理急停。
正常返回起点后，若试验开始时未使能，会恢复未使能状态。
恢复未使能状态后的最终检查中，若 J4 反馈超出起点 ±0.1°，程序按失败中止。

同步显示可独立运行 `mg400-mirror`（见 [CoppeliaSim 说明](coppeliasim.md)）；
显示来自真机实际反馈，CoppeliaSim 应保持仿真停止。当前这份显示模型尚不含夹爪。

## 可选 ROS 2 路线：HarvestX/MG400_ROS2

如果采用 ROS 2，可参考 [HarvestX/MG400_ROS2](https://github.com/HarvestX/MG400_ROS2)。
本文接口说明基于 `humble` 分支提交
[`1a8c7344be44c52030f7216c0f387873ed91fd11`](https://github.com/HarvestX/MG400_ROS2/tree/1a8c7344be44c52030f7216c0f387873ed91fd11)。
这是 HarvestX 的 ROS 2 项目。
下方 Dobot 官方 SDK 用于核对现有直接 TCP 接口和后续通用适配。

其 [README](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/README.md)
列出的环境是 Ubuntu 22 + ROS 2 Humble、MG400 固件 1.5.6.0。
仓库提供机器人模型、RViz 显示、驱动节点、运动 action 和控制 service。
`display.launch.py` 可无硬件显示模型，但不等于物理仿真或完整避障规划。

ROS 2 路线结构：现有 agent / skills → ROS 2 适配器 → HarvestX 驱动 → MG400。
本项目当前没有 ROS 2 后端。
以下名称基于 `main.launch.py` 的默认 `mg400` 命名空间；修改 namespace 时应同步调整。

| 本项目边界 | HarvestX 对应接口 / 接入要求 |
| --- | --- |
| `Arm.move_linear` | `/mg400/mov_l`，类型 `mg400_msgs/action/MovL`；等待最终 action 状态并检查 `result` |
| `Arm.get_pose` | 结合 `/mg400/joint_states`、TF、连接和报警状态取得新鲜位姿，核对法兰与夹爪 TCP 的关系 |
| `Arm.stop` | 评估 `/mg400/reset_robot` service 并核实停止反馈；不能只发送 action cancel |
| 电动夹爪输出 | `/mg400/tool_do_execute`，仅在夹爪确实通过工具端数字 I/O 控制时使用 |
| 电动夹爪输入 | `/mg400/tool_di`，按夹爪手册解释输入信号；读取成功不等于夹持成功 |

接入要求与限制如下：

1. **单位转换**：本项目使用 mm / deg；`MovL` 的 `PoseStamped` 使用米和四元数，
   驱动取 yaw 弧度后再转换成设备协议单位。适配器需转换长度、角度并指定正确 TF frame。
   依据：[运动命令实现](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_interface/src/commander/motion_commander.cpp)。
2. **取消不是停止**：此版本 `MovL::handle_cancel` 只返回接受，停止仍为 TODO，
   超时 / abort 分支也未主动发送停止命令，不能据此报告机械臂已停止。
   其执行路径还存在发送异常后直接返回、未结束 action 的分支；
   客户端必须有自己的期限和故障处理。
   依据：[MovL 实现](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_plugin/src/motion_api/mov_l.cpp)。
3. **到位精度需要复核**：此版本 `MovL` 根据位姿误差和 ENABLE 状态判断完成，
   位置阈值为每轴 5 mm、角度阈值为 0.0174 rad；抓取任务需要自己的验收精度。
   同一源码中有固定 10 秒执行期限，不能假定本项目的 `action_timeout_s` 会自动传到服务端。
4. **夹爪仍需适配**：仓库有工具端 I/O 服务，但未提供适用于任意电动夹爪的抓取确认。
   若夹爪使用 Modbus 或其他通讯，应实现相应 `Gripper`，不能直接套用数字输出。
   依据：[ToolDOExecute](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_plugin/src/dashboard_api/tool_do_execute.cpp)、
   [ToolDI](https://github.com/HarvestX/MG400_ROS2/blob/1a8c7344be44c52030f7216c0f387873ed91fd11/mg400_plugin/src/dashboard_api/tool_di.cpp)。

实现 ROS 2 适配器前，先确定部署环境：当前 scaffold 要求 Python 3.11+，
`rclpy` 必须与所安装 ROS 发行版的 Python 环境兼容，不能假定当前 macOS 的 venv 可直接使用。
驱动应先在上游支持的 Ubuntu / Humble 环境验证，再确定 agent 同进程运行还是通过独立桥接进程通信。

## Dobot 官方资料（直接 TCP 接入）

- [MG400 / M1Pro 四轴 Python SDK](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python)
- [SDK 源码](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python/blob/main/dobot_api.py)
- [四轴 TCP/IP 协议仓库](https://github.com/Dobot-Arm/TCP-IP-Protocol-4AXis)
- [四轴协议，2024-04-19 英文版](https://github.com/Dobot-Arm/TCP-IP-Protocol-4AXis/blob/master/TCP_IP%20Remote%20Control%20Interface%20Guide%20%284axis%29_20240419_en.pdf)

上述 Python README 标注控制器 V1.5.6.0+，故障排查建议尝试
V1.6.0.0+；最终应以本机固件对应的官方手册为准。需要在 DobotStudio Pro 中
选择 TCP/IP 二次开发模式。不要混用六轴机器人 SDK。

## 官方 SDK 接口映射

| 本项目接口 | 官方四轴接口 / 说明 |
| --- | --- |
| 建立控制连接 | `DobotApiDashboard(ip, 29999)` |
| 建立运动连接 | `DobotApiMove(ip, 30003)` |
| 反馈连接 | `DobotApi(ip, 30004)`，需正确拆分二进制帧 |
| 读取位姿 | `dashboard.GetPose()`；XYZ 毫米，R 角度 |
| 直线运动 | `motion.MovL(x, y, z, r, *params)` |
| 等待队列完成 | `motion.Sync()`；还应核对新鲜位姿与控制器状态 |
| 受控停止映射参考 | `dashboard.ResetRobot()`，手册描述为停止并清空规划队列 |
| 电动夹爪 | 取决于具体夹爪的 I/O 或通讯协议，单独实现 `Gripper` |

协议支持 `GetPose(User=0,Tool=0)`，但上述 Python 包装方法 `GetPose()` 不接收参数。
必须明确配置/验证当前 User、Tool 和 TCP，不能假定返回数据与场景使用同一坐标系。
官方 `MovL` 可选参数是字符串形式，例如 `"User=0"`，不是任意 Python 关键字参数。

## 完成语义与失败处理

TCP 响应 `ErrorID,{values},Command(...);` 中的成功码只能表示命令成功接收；
排队运动的应答不代表已经到位。实现 `move_linear` 时应解析成功应答、等待队列完成、
检查新鲜位姿/状态，并处理报警和超时。官方示例传输代码不能直接当作可靠完成保证。

收到超时或断线后，动作可能已经部分执行；停止会话并重新核对物理状态，
不要自动重发运动或清除报警。`SpeedFactor` 是相对控制器设置的百分比，
不是绝对 mm/s。软件停止命令不等价于独立硬件急停。

电动夹爪需要实现两个阻塞接口：

- `release(timeout_s)`：开爪并确认完成；未确认则抛出异常。
- `grasp(timeout_s) -> bool`：闭爪并读取夹持到物体的反馈。单纯“闭爪完成”
  不能证明已抓住物体；无夹持检测时，需增加外部检测或明确保留未验证状态。

## 建议接入顺序

1. 核对固件、机器人 IP、夹爪型号、供电和通讯参数；只读测试位姿与报警。
2. 设定工具 TCP 和负载，用示教器记录真实起点、抓取点、放置点与路径。
3. 实现有期限的移动、夹爪反馈和停止；加入对应协议测试。
4. 在受控工作区低速逐步验证单次动作，再验证固定抓放。
5. 接入 agent 前，补充路径可达性、碰撞检查和独立于模型的设备保护。

当前 `config/demo.toml` 是虚构场景；不要将其作为已标定的真机配置。
