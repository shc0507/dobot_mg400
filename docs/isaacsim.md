# Isaac Sim：MG400 + PGEA 夹爪

复用本项目已校核的 CAD 网格、连杆尺寸、惯量和夹爪安装关系，直接导出原生 USD。
不需要 ROS、URDF/MJCF 导入扩展或在线资产转换器。

## 文件

| 文件                                                 | 用途                                                                    |
| ---------------------------------------------------- | ----------------------------------------------------------------------- |
| `00_OPEN_MG400.usda`                                 | 日常主入口：直接打开即可看到机械臂、物理夹爪、灯光、支撑台和测试方块    |
| `isaacsim/workspace_scene.usda`                      | 保存初始姿态、测试物体、物理参数、动作预设和观察相机的场景层            |
| `examples/isaac_sim_actions_mg400.py`                | 当前场景的仿真动作面板；加载时不运动，不重载或搭建场景                  |
| `scripts/build_isaac_workspace.py`                   | 开发时重建静态主场景；日常打开和控制时不需要运行                        |
| `isaacsim/mg400_with_gripper.usd`                    | 自带网格与材质的机器人资产，默认 prim `/MG400`                          |
| `isaacsim/scene.usda`                                | 引用机器人的场景，含地面外形、灯光、相机及 PhysicsScene                 |
| `isaacsim/mg400_grasp.usda`                          | 在原资产上增加两侧物理滑块、夹指碰撞和夹爪质量的 USD 覆盖层             |
| `isaacsim/grasp_scene.usda`                          | 引用物理夹爪资产，并启用地面碰撞的场景                                  |
| `examples/isaac_sim_grasp_mg400.py`                  | Isaac Sim 6.1 的开合、接触夹持、提起与释放测试，可在 GUI 或独立进程运行 |
| `examples/isaac_sim_grasp_bootstrap.py`              | Kit `--exec` 自动加载入口，无需操作远程 Script Editor                   |
| `examples/isaac_sim_load_mg400.py`                   | 在运行中的 Isaac Sim 6.1 内加载并检查模型                               |
| `examples/isaac_sim_mirror_mg400.py`                 | 在当前场景中实时显示真机四轴反馈，支持断线重连                          |
| `examples/isaac_sim_mirror_bootstrap.py`             | Kit `--exec` 加载已保存的场景并启动真机姿态同步                         |
| `examples/isaac_sim_control_mg400.py`                | Isaac 内四轴真机控制面板：读取当前角度、使能、单次到位、停止            |
| `examples/isaac_sim_gripper_mg400.py`                | 实体夹爪面板：MG400 I/O 开合与 USB 位置反馈                            |
| `scripts/gripper_feedback.py`                       | Olares 主机读取 USB 夹爪位置，写入供 Isaac 读取的测量文件              |
| `scripts/isaac_gripper_feedback.mjs`                | Mac 读取 USB 夹爪位置，经当前 WebRTC 会话转发到 Isaac                  |
| `examples/isaac_sim_mg400.py`                        | 使用 Isaac Sim 5.1 兼容 API 的独立运行入口                              |
| `scripts/build_isaac_model.py`                       | 从现有模型重建 USD，无需启动 Isaac Sim                                  |
| `scripts/build_isaac_grasp.py`                       | 生成物理夹爪覆盖层，保留基础机器人资产                                  |
| `isaacsim/LICENSE`、`NOTICE.txt`、`GRASP_NOTICE.txt` | 原始模型来源、许可证和物理夹爪假设                                      |

模型单位为米，Z 轴向上。基础模型的八个旋转关节与三个固定关节连接 11 个刚体。
PGEA 侧出线版、两个估算指尖和各参考坐标系都随法兰移动。
物理夹爪覆盖层增加固定壳体与两个运动夹指，共 14 个刚体、8 个旋转自由度和
2 个直线自由度；两个直线自由度由同一个开合目标同步控制。
场景中的机器人为 `/World/MG400`，法兰为
`/World/MG400/mg400_end_effector_flange`。

## 日常使用：场景保存在 USD，Python 只控制动作

在 Isaac Sim 用 **File → Open** 打开
`/root/Documents/mg400-isaac/00_OPEN_MG400.usda`。
模型、灯光、地面、相机、支撑台、橙色方块和物理参数都已经保存在 USD 中。
机械臂各刚体的初始变换和关节驱动目标也一并保存，打开时不需要 Python 帮忙摆姿态。

需要控制时，在 **Window → Script Editor** 运行：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/examples/isaac_sim_actions_mg400.py")
```

脚本只显示 **MG400 Actions — current scene** 面板。点击按钮后才开始物理仿真，
通过驱动目标完成夹爪开合、移动到抓取位、抬起或完整的抓取／抬起／松开演示。
每次动作结束自动暂停；**Pause** 按钮可以中途暂停。
它不重新打开 USD、不创建方块或相机、不重置机器人位置，也不连接真机。

**Pause** 保留当前状态；Isaac 时间轴的 **Stop** 会恢复场景保存的初始状态，
之后再次点击动作按钮即可重新建立物理控制接口。
更换场景后，旧动作会取消；新场景需要包含同样的 MG400 结构和动作预设才能继续控制。
重复运行面板脚本会替换旧面板，不会同时运行两个动作控制器。

动作预设保存在 `/World/MG400` 的 `mg400:readyJointsDeg`、
`mg400:pickupJointsDeg`、`mg400:liftJointsDeg` 属性中，均为四轴角度，单位为度。
方块与支撑台分别为 `/World/GraspObject` 和 `/World/GraspSupport`；
在界面中编辑、保存 USD 后，下一次打开仍使用保存的场景。
如果移动了方块，需要相应调整抓取预设，演示不会自动识别新的物体位置。

`isaac_sim_grasp_mg400.py` 是完整的自动验收脚本，它会重建自己的测试场景、
生成报告并验证接触力等条件。日常学习和控制使用 `isaac_sim_actions_mg400.py`。

如需由项目源数据重新生成默认场景，在项目目录运行：

```sh
uv run --no-project --with usd-core==26.8 --with numpy python scripts/build_isaac_workspace.py
```

此命令会重建主入口和工作场景层；先保存另一个副本，再重建，以保留自己的场景编辑。

## Olares / WebRTC

Olares 容器内使用 Isaac Sim `6.1.0-rc.26`，安装目录为 `/isaac-sim`。
客户端保持 LarePass VPN 连接：

- Server：`100.64.0.1`
- Signal：`49100/TCP`
- Stream：`47998/UDP`

### WebRTC 客户端自动点击 SOP

使用后台点击脚本，不激活窗口、不移动系统鼠标。
它临时使用已获授权的本机调试接口 `127.0.0.1:9229`，每批结束后关闭。

1. 客户端必须已运行、位于后台且未最小化。用完整路径绑定应用：
   `app = await cua.getApp("/Applications/Isaac Sim WebRTC Streaming Client.app")`。
   本地控件优先用 AX 元素操作，远端视频控件按截图定位。
2. 取得完整窗口截图，记录原始像素尺寸 `width × height` 和目标坐标 `(x, y)`。
   窗口及布局未变时可复用上一批结果图。窗口移动、缩放或布局变化后重新截图。
3. 在项目目录运行（Node.js 22+）：
   `node scripts/isaac_client_click.mjs width height x y [x y ...]`。
   脚本已完成移动和点击，不要再调用 `app.click`。
   使用截图原始坐标，不手动扣标题栏、缩放或按远端分辨率换算。
4. 确定的连续操作可合并，每批最多 8 次。批次结束后读取 AX 并截图确认远端结果。
   遇到未知菜单、弹窗或报错时先观察，不要猜坐标或整批盲目重试。
   已展开的菜单可因悬停切换，避免再次点击标题将其收起。
5. 校准只用普通菜单、标签页，不用机械臂动作按钮，也不用拖动代替鼠标移动。

截图失败时，按完整路径重新绑定应用，读取最新 AX。
仍失败时停止操作，不自动改用 `Raise`、重启或前台移动脚本。
只有用户明确接受切换前台和移动系统鼠标时，才可使用前台方案。

### 服务与场景加载

已有服务运行时直接使用当前客户端，不需要启动第二个 Isaac Sim。
需要重新启动服务时，在 Olares 容器终端运行现有脚本：

```sh
/root/Documents/start-isaac-vpn.sh
```

将以下文件和目录复制到 `/root/Documents/mg400-isaac/`，保留相对目录结构：

- `00_OPEN_MG400.usda`：日常场景入口。
- `isaacsim/`：场景层、机器人资产及许可证。
- `examples/`：仿真动作、真机面板与验收脚本。
- `src/`：面板和脚本共用的 Python 模块。
- `scripts/`：包含 Olares 主机使用的 USB 反馈采集脚本。

相对引用允许场景整体移动。不必上传 `.env` 或 API key，也不必安装模型 API 依赖。
日常使用按上文打开 `00_OPEN_MG400.usda` 和仿真动作面板。
检查基础模型导入时，在 **Window → Script Editor** 执行下列脚本：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/examples/isaac_sim_load_mg400.py")
```

脚本会先备份原场景到 `isaacsim/previous_scene_<时间>.usda`，再打开新场景；
设置观察相机、初始化 PhysX，将控制器四轴设为 `[20, 15, 35, 25]` 度，
检查八个具名关节的回读与固定底座，最后暂停并保留画面。
结果写入 `isaacsim/isaac_sim_validation.json`。
只有 `success: true` 才代表运行时校验成功；异常也会写入这个文件。

该校验检查直接赋值关节后的数值一致性，不测量动力学跟踪精度或真机精度。
场景时间范围为 0–600 帧，帧率为 60 fps。

如果只查看外形，可用 **File → Open** 打开
`/root/Documents/mg400-isaac/isaacsim/scene.usda`，选择 `/World/Camera` 相机。
要加入自己的场景，可将 `mg400_with_gripper.usd` 作为 USD reference 引入。

## 完整自动验收：夹爪开合与物理抓取

在现有 WebRTC 客户端的 **Window → Script Editor** 中运行：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/examples/isaac_sim_grasp_mg400.py")
```

脚本先备份当前场景到 `isaacsim/before_grasp_<时间>.usda`，然后打开
`grasp_scene.usda`，创建支撑台和一个 **5 g、8 × 8 × 20 mm** 的自由刚体。
测试依次执行空爪闭合、重新打开、下降接近、夹住物体、向上提起 80 mm、
保持 2 秒、松开并等待物体落回支撑台。测试结束暂停场景，结果写入
`isaacsim/grasp_validation.json`；成功后另外保存 `isaacsim/grasp_test_scene.usda`。
有可用视口时，还会保存 `isaacsim/grasp_held.png` 与 `isaacsim/grasp_released.png`。
GUI 测试通过后会显示 **MG400 Gripper** 面板；点击 **Open** 或 **Close** 可再次开合，
面板显示实测夹指净距，每次动作结束会自动暂停。重新加载场景后需重新运行脚本建立控制面板。

只有报告中的 `success: true` 表示整套运行时检查通过。检查包括空爪行程、两侧接触力、
物体实际升高量、保持期间的滑落量、释放后自由下落与固定底座稳定性。
物理步频设为 120 Hz，脚本检查实际步长。
释放检查包含最后 0.5 秒的速度与位置。
物体全程启用重力，由接触与摩擦承载；创建后不再改写物体位置，也不添加物体附着关节。
机器人仅在初始时赋值一次姿态，此后的机械臂和夹爪运动通过关节驱动目标完成。

独立测试必须由服务器上的 Isaac Sim Python 启动，它会创建一个无窗口实例：

```sh
/isaac-sim/python.sh /root/Documents/mg400-isaac/examples/isaac_sim_grasp_mg400.py
```

要在当前 WebRTC 画面里观察测试，使用上面的 Script Editor 入口。
也可在旧串流进程正常退出后，在 Olares 终端启动自动加载入口：

```sh
/root/Documents/start-isaac-vpn.sh --exec /root/Documents/mg400-isaac/examples/isaac_sim_grasp_bootstrap.py
```

此入口由 Kit 自身执行，用 `runpy` 加载测试，避免额外创建 `SimulationApp`。

手动点击 Stop 或更换场景会使控制面板的物理句柄失效，需重新运行测试脚本。
面板自己的动作只使用 Play / Pause。
面板开合各推进约 2 秒的物理仿真。

两个滑动关节为 `/World/MG400/joints/pgea_left_slide` 和
`/World/MG400/joints/pgea_right_slide`，USD 与运行时的直线坐标单位均为米：

| 坐标或参数                 | 定义                                                         |
| -------------------------- | ------------------------------------------------------------ |
| `q_left = q_right = 0`     | 保留当前 CAD 开口，估算夹指净距 11.9 mm                      |
| `q_left = q_right = 0.005` | 两侧各向内移动 5 mm，空爪净距 1.9 mm                         |
| 实际夹指净距               | `0.0119 - q_left - q_right` 米                               |
| 夹爪质量                   | 共 0.15 kg，暂分配壳体 0.13 kg、左右夹指各 0.01 kg           |
| 夹指驱动                   | 每侧最大力 2 N，力模式位置驱动，刚度 1000 N/m、阻尼 10 N·s/m |
| 接触模型                   | 两个 10 × 9 × 15 mm 长方体夹指，静/动摩擦系数 1，恢复系数 0  |
| 接触距离                   | `contactOffset = 0.2 mm`、`restOffset = 0`                   |

总行程 10 mm、夹爪总质量 0.15 kg，以及 PGEA-2-10 每侧 0.8–2 N 的夹持力范围来自
[DH-Robotics 官方规格](https://en.dh-robotics.com/product/pgea)。现有 CAD 同时适用于
PGEA-2-10/15-10；这里选择较小的 2 N 力上限，未据此确认实物型号。
夹指是按照片估算的外形，因此其 1.9–11.9 mm 净距不等同于厂商标准滑块开口。
质量分配、长方体惯量、摩擦、接触距离和驱动增益均为仿真假设，未经过实测标定。

壳体与滑块启用重力，仅夹指、支撑台、地面和测试物体参与碰撞；机械臂及夹爪内部
CAD 网格未添加碰撞。机械臂仍按四轴映射给八个旋转关节发送驱动目标，
没有真实的闭环连杆约束，机械臂自身重力也仍关闭。因此该测试可验证夹爪的
PhysX 接触、摩擦承载和释放行为，不能验证整机碰撞、真实电机负载或闭环机构动力学。

## 真机姿态实时同步

在当前已加载 MG400 的 Isaac Sim **Script Editor** 中运行：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/examples/isaac_sim_mirror_mg400.py")
```

脚本只接收 `192.168.1.6:30004` 的关节反馈，不发送真机命令。它先备份当前场景，
保留物体位置与夹爪开口，再停止物理仿真，在每次画面更新时应用最新的四轴姿态。
八个旋转关节使用下面的同一套映射；连杆变换只写入 USD session layer，源资产保持不变。
夹爪随法兰移动，但开口保持启动时的值，因为此反馈流没有提供夹爪位置。

**MG400 Live Mirror** 面板显示 `LIVE`、更新频率和反馈年龄。
超过 0.5 秒没有新数据时显示 `STALE / RECONNECTING`，模型停在最后有效姿态，
后台自动重连。点击 **Stop synchronization** 停止同步；再次运行脚本可恢复。
更换场景或启动物理仿真也会停止同步，防止两个更新来源同时控制模型。
运行状态每 0.5 秒写入 `isaacsim/mirror_status.json`，其中包含反馈数、应用帧数、
真机角度、法兰位置、夹爪保持值与场景备份路径。

需要重启串流并自动同步时，先正常退出旧串流进程，再运行：

```sh
/root/Documents/start-isaac-vpn.sh --exec /root/Documents/mg400-isaac/examples/isaac_sim_mirror_bootstrap.py
```

入口优先打开已有的 `grasp_test_scene.usda`，否则打开 `grasp_scene.usda`。
加载后创建 session layer 中的 `MirrorCamera`，以完整机械臂视角观察同步。
其他相机的可视图标在 session layer 中隐藏，避免近距离挡住观察画面；相机源定义保留。
面板显示的反馈年龄是 Isaac Sim 收到反馈后的经过时间，不包含客户端视频串流延迟。

Olares 直连网口使用 `192.168.1.10/24`。IsaacLab 容器内直接连接机器人，
不要绑定宿主机的源 IP。命名空间中的 `mg400-direct` Linkerd EgressNetwork
将机器人 `30004` 标为 opaque TCP，避免服务端先发送数据的协议检测等待。

## 在 Isaac 中控制真机四轴

使用 Isaac 自带面板，控制链路在 Olares 内完成：

```text
Isaac 面板 J1–J4（度） → TCP 29999 / 30003 → MG400
Isaac 模型与 Actual 显示 ← TCP 30004 ← MG400 实测角度
```

无需 ROS 2 或额外桥接服务。将 `examples/` 和 `src/` 复制到现有
`/root/Documents/mg400-isaac/`，保留已有 `isaacsim/` 资产。在当前 Isaac 的
**Window → Script Editor** 执行：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/examples/isaac_sim_control_mg400.py")
```

脚本复用现有实时同步；尚未启动同步时，会通过 mirror bootstrap 备份当前场景、
加载 MG400 并启动反馈。出现 **MG400 Joint Control** 面板后：

1. 等待 `LIVE`，`Actual (deg)` 显示实测角度，`Target (deg)` 首次自动填入实测值。
2. 未使能时点击 **Enable robot**，仅使能，不发送运动；使能沿用控制器当前负载设置。
3. 点击 **Use current angles** 重新取得起点，修改所需轴的绝对目标角度。
4. 点击 **Move real robot to target**。一次只执行一个目标，每轴相对实测起点最多 5°，
   指令采用 `SpeedJ=5,AccJ=5` 比例参数，保持全局速度、工具坐标和夹爪输出不变。
5. `Arrival verified` 表示新鲜反馈已在目标 ±0.1° 内稳定至少 0.4 秒，
   并通过 dashboard 回读核对。完成后保持使能，可继续下一次动作。

输入框本身不发运动命令；模型始终显示真机实际姿态，不把输入目标当作已到位。
动作期间输入和执行按钮禁用，网络操作在后台线程运行，**STOP robot** 仍可点击。
此版本只控制四轴，不控制真实夹爪，不支持拖动 USD 连杆直接控制真机。

**STOP robot** 请求 `ResetRobot()` 并锁定本次控制会话。成功时显示静止已核实，
否则显示 `STOP UNCONFIRMED`；这类情况下需使用现场物理急停。
停止后重新运行脚本才能恢复控制。运动时关闭面板、更换场景、启动物理仿真，
或停止反馈同步，也会结束控制并请求停止；异常不会自动重发动作或自动清除报警。
超过 0.25 秒无新鲜反馈、控制器报警、异常模式或 20 秒内未到位都会中止。
命令应答未知时，即使收到停止指令的应答，也不宣称已确认停止。

当前关节范围检查复用项目参考模型，并非现场碰撞检查；执行前应确认路径、夹爪和线缆的空间。
建议首次由现场操作者将 J4 目标改为当前值加 1°，检查真实运动与画面一致后再扩大操作。
打开脚本和只读连接测试不会自动执行这一步。

状态每 0.5 秒写入 `isaacsim/control_status.json`，包含连接状态、实测值、目标值和操作结果；
`mirror_status.json` 继续记录模型同步。停止现有串流进程后也可用同一脚本自动加载：

```sh
/root/Documents/start-isaac-vpn.sh --exec /root/Documents/mg400-isaac/examples/isaac_sim_control_mg400.py
```

Olares 容器内不绑定宿主机源 IP。需能连接机器人 `192.168.1.6` 的 `29999`、`30003`、
`30004` 三个 TCP 端口；已有仅配置 `30004` 的服务网格规则不能代表命令端口也已连通。
本地离线测试使用模拟控制器覆盖协议、限位、重复点击、反馈失效和停止逻辑；
不能据此宣称真机运动或 Isaac 运行时验证已完成。

面板 API 参考：[NVIDIA omni.ui FloatField](https://docs.omniverse.nvidia.com/kit/docs/omni.ui/latest/omni.ui/omni.ui.FloatField.html)；
四轴协议参考：[Dobot 官方 SDK](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python/blob/main/dobot_api.py)。

## 在 Isaac 中控制实体夹爪

在 Script Editor 运行以下脚本，打开 `MG400 Real Gripper` 面板：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/examples/isaac_sim_gripper_mg400.py")
```

Olares 项目根目录的 `START_REAL_GRIPPER.py` 是同一入口。
面板启动只读状态。`Open gripper` 写 DO2=0、DO1=0，
`Close gripper` 写 DO2=0、DO1=1。它不使能机械臂，不发送关节命令。
面板通过 MG400 的 TCP 29999 控制 I/O。
启用下述 USB 反馈后，仿真夹指跟随实体位置。

当前接线为 MG400 DO1/DO2 → 夹爪 INPUT1/INPUT2，
夹爪 OUTPUT1/OUTPUT2 → MG400 DI1/DI2，输入和输出均为 PNP。
I/O 组 1 为全开（1000），组 2 为全闭（0），位置单位为行程千分数。
两组力度均为 50%。面板显示原始 DI 值。
只有命令后观察到 DI 从 00 变为 10，才报告到位。
通信失败会锁定后续写入，不自动重发命令。
状态写入 `isaacsim/real_gripper_status.json`。

断电重启后，自动初始化可能未完成。
当前须保留 USB–RS485 连接作为初始化入口：
从站 1、115200、8N1，写 `0x0100=1`，等待 `0x0200=1`。
该初始化会让夹指张开寻找行程端点。
完成初始化后，开合命令经 MG400 I/O 发送，USB 仅用于独立读取。

### 同步实体夹爪位置

USB 接在 Mac 时，保持 LarePass VPN 和 WebRTC 客户端连接。
Isaac 的仿真时间轴必须处于 Stop 状态，夹爪面板保持打开。
在 Mac 项目根目录启动反馈转发：

```sh
node scripts/isaac_gripper_feedback.mjs start
```

脚本独占 `/dev/cu.usbserial-BG02Q5TN`，以 20 Hz 读取初始化状态、
运动状态和实际位置（`0x0200` 至 `0x0202`）。
它不初始化夹爪，不写控制寄存器。
位置经当前 WebRTC 会话送到 Isaac，不新增网络端口。
面板显示 `Following measured USB position.` 后，模型跟随实体读数。

真实位置 1000 对应全开，0 对应每侧夹指内收 5 mm。
代码只更新两个夹指的 SessionLayer 变换，不启动物理仿真。
这项同步用于显示实体开口，不模拟夹持力或接触。
反馈超过 0.75 秒未更新，或初始化、位置数据无效时，模型保持最后位置。
面板同时显示反馈过期或无效提示。有效反馈恢复后，模型继续同步。
若同时运行四轴镜像，首次更新后重启四轴镜像脚本，避免旧实例覆盖夹指位置。

查询或停止 Mac 反馈：

```sh
node scripts/isaac_gripper_feedback.mjs status
node scripts/isaac_gripper_feedback.mjs stop
```

退出客户端或串口读取失败后，反馈进程停止。
重新连接后再执行 `start`，不自动重启或重新初始化。
反馈进程运行时，其他程序不能使用同一串口。
Mac 状态写入 `work/gripper_feedback_status.json`。

转发脚本适配 Streaming Client 2.0.0 内置的 SDK 3.7.0。
启动时使用临时回环调试接口连接现有 SDK，配置结束即关闭接口。
客户端升级后须重新核对接口，脚本会拒绝其他版本。
消息格式参考 [NVIDIA AppStreamer](https://docs.omniverse.nvidia.com/ov-web-sdk/latest/web-streaming-library/classes/AppStreamer.html)
和 [Kit 消息扩展](https://docs.omniverse.nvidia.com/kit/docs/omni.kit.livestream.messaging/latest/Overview.html)。

#### USB 接在 Olares

转换器的 USB 身份为 `0403:6001`，序列号为 `BG02Q5TN`。
主机稳定路径为 `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0`。
`ttyUSB` 编号可能变化，采集脚本按完整 USB 身份选择设备。
不要用扩展坞上的其他串口替代。

Isaac 容器没有映射串口设备。
在 Control Hub 的 Olares 主机终端运行采集脚本。
先进入 `isaaclab-data` 的 `hostPath` 下的 `documents/mg400-isaac` 目录。
该目录对应容器的 `/root/Documents/mg400-isaac`。

```sh
python3 scripts/gripper_feedback.py --output work/gripper_olares_feedback.json
```

脚本每 50 ms 读取三个状态寄存器，并原子替换最新测量文件。
文件权限为 `0644`，容器可以读取。
串口被占用、设备缺失或读取失败时，脚本退出。
按 `Ctrl+C` 停止前台采集，或向确认过身份的后台采集进程发送 `SIGTERM`。
主机重启或 USB 重新连接后，手动重新启动采集。

在 Isaac Script Editor 中选择文件反馈：

```python
import runpy

root = "/root/Documents/mg400-isaac"
runpy.run_path(
    root + "/examples/isaac_sim_gripper_mg400.py",
    init_globals={
        "FEEDBACK_PATH": root + "/work/gripper_olares_feedback.json",
    },
)
```

文件反馈与 Mac WebRTC 反馈互斥。
同一文件重复读取不会延长测量值的有效期。
该方式沿用 Stop 时间轴和反馈过期保护，不必重启 Isaac 或新增网络端口。

## 四轴映射与当前范围

| 控制器角度 | USD 关节角度                                                |
| ---------- | ----------------------------------------------------------- |
| J1         | `mg400_j1 = J1`                                             |
| J2         | `mg400_j2_1 = J2`、`mg400_j2_2 = J2`、`mg400_j3_2 = -J2`    |
| J3         | `mg400_j3_1 = J3-J2`、`mg400_j4_1 = -J3`、`mg400_j4_2 = J3` |
| J4         | `mg400_j5 = J4`                                             |

USD 旋转关节限位和 drive target 单位为度，Isaac articulation API 的角度为弧度。
脚本按名字匹配关节，不依赖 PhysX 内部排列。应通过该四轴映射同时设置八关节；
单独拖动一个连杆关节不会自动维持平行连杆关系。

基础导入脚本 `isaac_sim_load_mg400.py` 使用运动学姿态赋值，保留 CAD 惯量并用位置驱动保持姿态，
禁用机器人重力和碰撞；没有把 MuJoCo 的 tendon/equality 约束转换成 PhysX 闭环约束。
基础资产的夹爪仍为固定外形；需要开合及接触时使用上述物理夹爪覆盖层与抓取脚本。
`mg400_tcp` 仍是零工具偏移的法兰坐标系；估算指尖参考为法兰下方 104 mm，
不能将法兰坐标直接当作已标定的实际夹爪 TCP。

## 重建与验证

在本项目根目录运行。构建依赖与现有运行环境隔离：

```sh
uv run --no-project --with usd-core==26.8 --with numpy python scripts/build_isaac_model.py
uv run --no-project --with usd-core==26.8 --with numpy python scripts/build_isaac_grasp.py
uv run --no-project --with usd-core==26.8 --with numpy python scripts/build_isaac_workspace.py
```

运行全部离线检查，包含工作场景、动作面板和夹爪镜像：

```sh
uv sync --all-extras --group usd
uv run --all-extras --group usd pytest -ra
```

本地测试比较源网格/颜色、惯量张量、关节拓扑、零位坐标、多姿态 CAD 运动学和夹爪偏移。
物理夹爪测试还检查两侧滑动方向、开闭净距、原视觉网格去重、质量分配、驱动与接触参数。
`usd` 依赖组提供 `pxr` 和 NumPy。未安装时，USD 模型测试会跳过。
`-ra` 显示跳过原因，Lua 检查仍要求本机安装 Lua 解释器。
这些检查不能替代服务器端的渲染与 PhysX 验证。

独立运行入口供装有 Isaac Sim 的 Linux / Windows 环境使用。以下是 5.1 的兼容 API，
6.x 已弃用这些接口，Olares 当前版本优先用上面的 GUI 加载脚本：

```sh
/path/to/isaac-sim/python.sh examples/isaac_sim_mg400.py --headless --steps 240
/path/to/isaac-sim/python.sh examples/isaac_sim_mg400.py --demo
```

Windows 使用 `python.bat`。`--joints J1 J2 J3 J4` 接受度数；`--demo` 在零位和目标间往返。
独立入口必须由 Isaac Sim 自带 Python 启动，本项目的 Mac 虚拟环境只能生成/检查 USD。

参考：[SimulationManager](https://docs.isaacsim.omniverse.nvidia.com/latest/py/source/extensions/isaacsim.core.simulation_manager/docs/index.html)、
[Physics tensor API](https://docs.omniverse.nvidia.com/kit/docs/omni_physics/109.0/extensions/runtime/source/omni.physics.tensors/docs/api/python.html)。
