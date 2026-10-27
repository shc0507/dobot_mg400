# Isaac Sim：MG400 与 PGEA 夹爪

[项目首页](../README.md) · [连接](#打开场景与连接) · [操作](#控制机械臂与夹爪) · [停止](#停止与恢复) · [排障](#故障排查)

本页说明如何在 Isaac Sim 中查看真机姿态，并控制机械臂和夹爪。
日常场景显示实测姿态，时间轴保持 `Stop`。
镜像不验证碰撞、夹持力或真实连杆动力学。
法兰坐标也不能直接当作已标定的实际夹爪 TCP。

安装和更新服务见 [Olares 配置与维护](olares.md)。
模型、接口和构建说明见 [开发参考](development.md#isaac-模型与控制接口)。

## 开始前

确认 LarePass VPN 已连接，并使用已运行的 WebRTC 客户端。
服务地址为 `100.64.0.1`，信令端口为 `49100/TCP`，视频端口为 `47998/UDP`。
服务未启动时，按 [启动 Isaac 服务](olares.md#启动-isaac-服务) 操作。
首次使用 Olares USB 夹爪时，先 [安装采集服务](olares.md#usb-接在-olares)。

关闭 DobotStudio 和其他控制程序。
Isaac 与网页面板通过 [面板连接锁](olares.md#面板连接锁) 避免同时连接。
该锁不限制其他主机或 DobotStudio。
机器人地址配置见 [机器人地址与端口](olares.md#机器人地址与端口)。

执行运动前，确认路径、夹爪和线缆的空间。
关节范围和逆解检查不包含现场碰撞检查。
建议首次短按 Q/E，松键后核对真实运动与画面。
确认两者一致后，再扩大操作。
打开脚本和只读连接测试不会自动执行这一步。
若显示 `STOP UNCONFIRMED`，使用现场物理急停。

## 打开场景与连接

默认启动打开 `00_OPEN_MG400.usda`，检查控制模块能否导入。
它不创建面板、不连接机器人、不启动物理仿真，也不发送真机命令。
在已有服务中，也可用 `File → Open` 打开 `/root/Documents/mg400-isaac/00_OPEN_MG400.usda`。

保持时间轴 `Stop`，在 `Window → Script Editor` 运行：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/apps/isaacsim/joint_control.py")
```

入口打开英文界面的 `MG400 Control`，并启动或复用姿态镜像。
尚无镜像时，入口先备份当前场景，必要时加载 MG400，再启动反馈。
已有镜像窗口会隐藏，统一窗口顶部显示同步状态及 `Start sync` / `Stop sync`。
`Coordinates & Motion` 提供 XYZ/R 和夹爪五行滑块及数值输入。
键盘开关和 J1–J4 读数位于下方，操作说明见悬浮提示。
等待 `LIVE` 和 `Actual` 坐标，`Target` 首次自动填入实测坐标。
连接后显示真机姿态，运动仍须手动触发。

夹爪行自动读取 Olares USB 反馈，并请求启动采集服务。
运行中的采集不会重启，打开面板不会初始化夹爪。
`olares_gripper.py` 保留为同一面板的兼容入口。
自定义场景另存到数据目录，要求见 [代码与运行数据](olares.md#代码与运行数据)。

## 控制机械臂与夹爪

### 设置并执行目标

1. 未使能时，点击 `Enable robot`。
   此操作不发送运动，沿用控制器当前负载设置。
2. 指定位置时，修改 X/Y/Z（mm）和 R（度）。
   必要时修改夹爪开口，再点击 `Execute targets`。
3. 等待机械臂到位确认，再查看夹爪结果。
   程序只在机械臂到位后执行用户修改过的夹爪目标。
   完成后机械臂保持使能。
4. 下使能时，点击 `Disable robot`。

若要返回默认姿态，点击 `Home`。
默认姿态为 J1–J4 全部 0°。

坐标使用基座坐标系和法兰原点（`User=0,Tool=0`）。
将鼠标悬停在 `Coordinates & Motion` 标题上，可查看坐标参考说明。
R 表示法兰朝向，不等于 J4 角度。
面板从控制器读取坐标，不用 CAD 模型计算实机目标。
滑块以填入 `Target` 时的实测坐标为中心。
XYZ 调节范围为 ±50 mm，R 为 ±10°。
双击数值框可输入目标值。
数值框可输入范围外的值，滑块范围不是工作空间限位。
执行前仍由控制器逆解和关节范围检查验证目标。

直线运动核对坐标误差不超过 0.1 mm / 0.1°。
关节反馈还须在目标 ±0.1° 内稳定至少 0.4 秒，再用 dashboard 回读核对。
若夹爪未完成，状态保留机械臂已到位的信息，并报告夹爪结果。
输入框本身不发运动命令，模型显示真机实际姿态。
动作期间输入和执行按钮禁用，`Disable robot` 仍可取消动作并下使能。
`Home` 和机械臂键盘点动不写夹爪输出。
不支持拖动 USD 连杆控制真机。

夹爪开口范围为 0–1000，0 为闭合，1000 为全开。
统一面板不设独立夹爪执行或开合按钮。
首次位置命令将夹爪切换为仅 Modbus 控制，保留当前力度和速度。
该操作不初始化夹爪，也不保存到闪存，I/O 开合随后不再生效。
实测位置在目标 ±5 / 1000 内且夹爪报告到位时，才显示到位。
检测到物体接触时，面板单独报告接触状态，并结束本次 Z/X 长按。
松开按键后，才可再次长按。
`Object dropped` 表示物体掉落，不表示到位或夹持成功。
面板继续显示实测位置，并允许发送新目标。

### 初始化夹爪

断电重启后，夹爪可能处于未初始化状态。
Olares USB 面板默认不自动初始化夹爪，空爪和夹持物体时均使用统一入口。
夹持物体时，不要初始化夹爪。
初始化会张开夹指寻找行程端点，开始前清空行程内的障碍。
先关闭键盘控制，保持同步开启和控制未锁定。
夹爪未初始化时，等待新鲜反馈和机械臂空闲，再点击 `Initialize gripper`。
反馈确认已初始化或正在初始化时，初始化按钮禁用。
初始化期间，机械臂运动和夹爪目标操作禁用。
初始化成功后，目标开口重置为实测开口。

初始化共用 15 秒时限，不改变力度、速度、I/O 模式或闪存配置。
失败或超时后不自动重发，通信状态不确定时保留命令锁。
核实夹爪状态并修复故障后，再处理命令锁。
独立 I/O 面板和 Mac 反馈转发不支持此初始化入口。

### 键盘点动

在 `Speed %` 选择 1–100 的整数，默认值为 1。
拖动滑块或双击数值框修改。
调速先关闭键盘控制，动作期间不能调速。
点击 `Keyboard: OFF`，等待检查完成并显示 `Keyboard: ON`。
检查期间设置所选 `SpeedFactor`，并核对新鲜速度比例反馈。
检查期间不发送运动命令，退出键盘控制后不恢复原速度比例。
此全局比例影响所有机械臂键盘运动、`Home` 和目标运动，不改变夹爪速度。
比例相对控制器的速度设置，不表示绝对 mm/s 或 °/s。
键盘控制仅在法兰面板获得焦点时生效。

反馈有效且机械臂与夹爪均空闲时，开启键盘控制后仍可编辑目标。
开始编辑坐标或夹爪目标时，面板先关闭键盘控制。
键盘检查或动作期间，目标输入禁用。

| 按键 | 动作 | 限制 |
| --- | --- | --- |
| W / S | 沿当前机械臂朝向伸出 / 收回，保持高度和法兰朝向 | 连续点动 |
| A / D | J1 左转 / 右转，保持 J2/J3/J4 | 连续点动 |
| R / F | 法兰上升 / 下降 | 连续点动 |
| Q / E | J4 正转 / 反转，保持 XYZ | 连续点动 |
| Z / X | 夹爪开口增加 / 减少 | 每步 25 / 1000 |
| Esc | 下使能机械臂 | 单次 |

W/S、A/D、R/F、Q/E 按下时发送一次 `MoveJog`，长按不追加轨迹。
R/F 使用 `CoordType=0,User=0,Tool=0`。
W/S 使用 `CoordType=0,User=8,Tool=0` 的 X 轴。
User 8 专用于键盘径向点动，不得用于其他标定或程序。
按下 W/S 后，程序先核对 User 0 未改变。
程序按需将 User 8 设为基座原点、旋转角为当前 J1，并读回核对。
长按期间不修改坐标系，退出后保留 User 8 的最后配置。
程序不改变全局 User 选择，也不修改其他坐标系。
配置核对失败，或 J1 相对起点变化超过 0.1° 时，程序停止并锁定控制。
Q/E 在 J1 固定时改变法兰朝向。
松键后发送 `MoveJog()`，再确认停稳。
成功应答后的反馈须持续至少 50 ms，且至少有三帧。
相邻收包间隔不得超过约 16.7 ms。
各关节相对窗口起点的变化不得超过 0.025°。
50 ms 是反馈确认窗口，不是松键到停机的总延迟。

连续点动持续检查反馈、报警和速度比例。
到达关节目标限位的 1.05° 余量边界时，程序请求停止。
起点处于该余量带时，程序先用有限轨迹退回允许范围。
此软件检查不能替代控制器限位或物理急停。
异常停止、使能和断开反馈前的静止确认仍使用 400 ms 窗口。

仅在关节目标限位余量带内，程序使用有限轨迹向内移动。
W/S、R/F 每段最多 50 mm，A/D、Q/E 每段最多 10°。
每段检查实测关节、目标和路径限位。
找到可行目标后，优先缩短到不超过 2 mm 或 1°。
若较短目标未通过检查，保留上一个通过检查的目标。
有限轨迹完成后，重新核对实测关节和空闲状态。
若已退出余量带且按键仍按住，自动恢复同方向连续点动。
切换期间松键、反馈失效或速度变化，均阻止继续运动。
未找到可行目标时，发送前逐次减半行程，最短尝试 0.1 mm。
若目标均被拒绝，本次长按结束，松键后可重试或换方向。
目标拒绝不锁定面板。
发送前查询失败的重试条件见 [操作被拒绝或控制锁定](#操作被拒绝或控制锁定)。
机械臂长按没有总时限或总行程上限。

运动中的通信或机器人状态异常仍会锁定控制。
松键会取消尚未发送的轨迹，并请求停止当前机械臂动作。
停止确认后，可再次按住按键。
Z/X 松键后完成当前夹爪小步，不再追加目标。
Z/X 每次长按最多 5 秒，到期后先松键再按。

按住多个方向键时请求停止。
等待停止确认或夹爪小步完成时，可以松开旧键，再按住新键。
只有一个方向键仍按住且该次按键尚未执行时，程序才接续动作。
期间已松开的按键不会执行，已执行的按键须松开再按。
关闭键盘控制会清除待执行的按键。

切换面板、按 Shift/Ctrl/Alt/Super 或反馈失效时，键盘控制关闭。
收到 WebRTC 客户端断线事件时，键盘控制也会关闭并请求停止。
断线检测没有可保证的时限。
若进程退出或网络失效，停止命令可能无法送达。
连续点动没有有限轨迹终点，断开运动连接也不保证自动停止。
使用连续点动时，须有人在现场并能操作物理急停。

恢复控制前，再次点击键盘按钮。
A/D 会随 J1 改变法兰朝向，不是基座 Y 方向的直线运动。
Z/X 使用同一窗口中的夹爪控制和带 `--commands` 的 USB 采集进程。
发送前若夹爪未初始化或反馈不可用，只提示夹爪错误，机械臂按键保持可用。
夹爪动作期间，机械臂运动按钮禁用。

## 停止与恢复

点击 `Disable robot` 会取消当前动作并下使能。
下使能后，面板用命令应答、新鲜反馈和 dashboard 回读核实未使能状态。
此核验不表示机械臂已静止。
核实成功后，目标坐标刷新为实测值。
点击 `Enable robot` 会先核实静止，再重新使能，无需重载脚本。
若无法核实，下使能报错并保持操作锁定。

使能前若关节未停稳，且新鲜反馈仍为未使能、无报警，面板只提示本次未使能。
停稳后，再次点击 `Enable robot`，无需重开面板。
程序不会自动重试使能。
反馈失效、状态不一致或使能命令发出后的验证失败，仍会锁定控制。

运动时关闭面板、更换场景、启动物理仿真或停止反馈同步，会结束控制并请求停止。
异常不会自动重发动作或自动清除报警。
超过 0.25 秒无新鲜反馈、控制器报警、异常模式或 120 秒内未到位都会中止。
命令应答未知时，即使收到停止指令的应答，也不宣称已确认停止。
若显示 `STOP UNCONFIRMED`，使用现场物理急停。

点击 `Stop sync` 或关闭统一窗口，会结束机械臂和夹爪控制。
脚本等待控制动作和当前夹爪命令结束，再用新鲜反馈核实静止。
确认后停止同步连接，`Stop sync` 保留窗口。
若无法确认停止，脚本保留反馈，记录原因，并保持控制禁用。
此时须核实机器人状态后重载面板，重载本身不能清除未确认的停止状态。

确认停止后，点击 `Start sync` 可恢复反馈。
关闭窗口后，重新运行统一入口可恢复面板和同步。
动作期间不能重载面板，空闲时重复运行会替换控制面板并复用同步连接。
关闭面板后，Olares USB 采集继续运行。
若要停止采集服务，见 [USB 接在 Olares](olares.md#usb-接在-olares)。

## 可选用法

### 只同步真机姿态

只显示真机姿态时，在 Isaac Sim Script Editor 中运行：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/apps/isaacsim/mirror_bootstrap.py")
```

入口在当前场景启动 `mirror.py`，没有 MG400 时先加载默认场景。
镜像只接收所配置机器人 TCP 30004 的关节反馈，不发送真机命令。
它先备份当前场景，保留物体位置与夹爪开口，再停止物理仿真。
随后在每次画面更新时应用最新的四轴姿态。
八个旋转关节使用 [四轴映射](development.md#四轴映射)。
连杆变换只写入 USD session layer，源资产保持不变。
夹爪随法兰移动，但开口保持启动时的值，因为此反馈流没有提供夹爪位置。

`MG400 Live Mirror` 面板显示 `LIVE`、更新频率和反馈年龄。
超过 0.5 秒没有新数据时显示 `STALE / RECONNECTING`，模型停在最后有效姿态，
后台自动重连。点击 `Stop synchronization` 停止同步，再次运行脚本可恢复。
更换场景或启动物理仿真也会停止同步，防止两个更新来源同时控制模型。
运行状态每 0.5 秒写入 `$MG400_DATA_DIR/isaacsim/mirror_status.json`，其中包含反馈数、应用帧数、
真机角度、法兰位置、夹爪保持值与场景备份路径。

当前场景已有 `/World/MG400` 时，入口保留该场景。
否则打开项目的 `00_OPEN_MG400.usda`。
显式传入 `SCENE_PATH` 时，先备份当前场景，再加载指定文件。
例如，先将自定义场景保存到下面的路径，再在 Script Editor 中指定该文件：

```python
import runpy

runpy.run_path(
    "/root/Documents/mg400-isaac/apps/isaacsim/mirror_bootstrap.py",
    init_globals={"SCENE_PATH": "/root/Documents/mg400-data/isaacsim/my_scene.usda"},
)
```

加载后创建 session layer 中的 `MirrorCamera`，以完整机械臂视角观察同步。
其他相机的可视图标在 session layer 中隐藏，避免近距离挡住观察画面。相机源定义保留。
面板显示的反馈年龄是 Isaac Sim 收到反馈后的经过时间，不包含客户端视频串流延迟。

启动串流时直接进入镜像，见 [启动时直接打开面板或镜像](olares.md#启动时直接打开面板或镜像)。

### 使用 I/O 开合夹爪

在 Isaac Script Editor 中使用独立 I/O 入口前，先关闭统一面板：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/apps/isaacsim/gripper_control.py")
```

此入口使用 I/O 开合，不支持连续位置命令。
面板启动只读状态。`Open gripper` 写 DO2=0、DO1=0，
`Close gripper` 写 DO2=0、DO1=1。它不使能机械臂，不发送关节命令。
面板通过 MG400 的 TCP 29999 控制 I/O。
启用下节的 Mac USB 反馈后，仿真夹指跟随实体位置。

当前接线为 MG400 DO1/DO2 → 夹爪 INPUT1/INPUT2，
夹爪 OUTPUT1/OUTPUT2 → MG400 DI1/DI2，输入和输出均为 PNP。
I/O 组 1 为全开（1000），组 2 为全闭（0），位置单位为行程千分数。
两组力度均为 50%。面板显示原始 DI 值。
只有命令后观察到 DI 从 00 变为 10，才报告到位。
通信失败会锁定后续写入，不自动重发命令。
状态写入 `$MG400_DATA_DIR/isaacsim/real_gripper_status.json`。

### USB 接在 Mac

USB 接在 Mac 时，保持 LarePass VPN 和 WebRTC 客户端连接。
Isaac 的仿真时间轴必须处于 Stop 状态，夹爪面板保持打开。
在 Mac 项目根目录启动反馈转发：

```sh
node scripts/isaac_gripper_feedback.mjs start
```

脚本独占 `/dev/cu.usbserial-BG02Q5TN`，读取状态和实际位置。
它不初始化夹爪，不写控制寄存器。
位置经当前 WebRTC 会话送到 Isaac，不新增网络端口。
面板显示 `Following measured USB position.` 后，模型跟随实体读数。

同步用于显示实体开口，不模拟夹持力或接触。
反馈失效时的行为见 [反馈与显示异常](#反馈与显示异常)。
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

文件反馈与 Mac WebRTC 反馈互斥。
此入口仍使用二态 I/O 开合，不支持位置命令。
转发脚本仅支持 Streaming Client 2.0.0 内置的 SDK 3.7.0。
客户端升级后须重新核对接口，详见 [Mac WebRTC 反馈接口](development.md#mac-webrtc-反馈接口)。

## 故障排查

### 操作被拒绝或控制锁定

机械臂控制：

若控制器拒绝逆解，且更新后的反馈确认机械臂仍使能、静止且无报警，
面板保留目标输入。修改坐标后可再次执行。
动作发出前，若只读查询发生网络错误，可重新点击操作。
程序仅在没有未决命令，且新鲜反馈保持静止、无报警、姿态未变时允许重试。
动作发出后若通信失败，仍会锁定控制。
回复格式错误或机器人状态异常也会锁定控制。

夹爪控制：

命令执行期间若反馈短暂中断，面板在原有 5 秒时限内等待，不重发命令。
寄存器确认成功后，若到位或物体接触均未在时限内确认，面板保持锁定。

命令发布前若采集会话变化，取消本次请求，反馈恢复后可重试。
临时命令文件写入失败时，也可在修复文件问题后重试。
命令发布后若采集会话变化或采集进程报告锁定，面板停止等待并锁定控制。
发布结果未知、设备寄存器写入失败或执行结果无法确认时，控制锁定，不重发命令。

夹爪命令超时不会撤回已发送的目标，也不保证夹爪停止运动。
重启采集进程不会清除命令锁。
先核实夹爪状态，修复故障后再处理锁，格式见 [USB 夹爪协议](development.md#usb-夹爪协议)。

### 反馈与显示异常

Olares USB 文件反馈沿用 `Stop` 时间轴和反馈过期保护。
它不要求重启 Isaac，也不新增网络端口。
同一文件重复读取不会延长测量值的有效期。
反馈超过 0.75 秒未更新，或初始化、位置数据无效时，模型保持最后位置。
面板显示反馈过期或无效提示，有效反馈恢复后继续同步。

夹爪场景更新报错时，独立窗口和统一面板均显示 `Scene update paused`。
文件或 WebRTC 反馈继续接收，实测值仍按 0.75 秒有效期检查。
反馈新鲜且现有控制条件满足时，夹爪控制仍可用。
此时显示模型可能与实测位置不一致。

同步开启、时间轴停止且反馈有效时，程序每 1 秒重试场景更新。
重试只更新场景，不发送夹爪命令，也不解除控制锁定。
完整更新成功后，警告消失。

如果找不到 USB 设备、采集服务失败或反馈文件不更新，见 [USB 接在 Olares](olares.md#usb-接在-olares)。
如果连接提示 `Robot controlled by another panel`，见 [面板连接锁](olares.md#面板连接锁)。

### 报警与历史日志

`mode=9, error=True` 只表示故障状态，不含具体报警码。
`GetErrorID()` 只读取当前报警，不能还原最初故障。
`control_status.json` 会反复覆盖，也不能作为历史记录。

查询最初故障时，按以下顺序排查：

1. 在 Olares 中打开 Isaac Sim 容器终端。
2. 通过 SMB（网络文件共享）读取控制器日志。
3. 按日志顺序核对碰撞、暂停和急停记录。

控制器地址见 `config/robot.toml`，当前为 `192.168.1.6`。
SMB 使用 TCP `445`，共享为 `project`，日志目录为 `/logs`。
此路径独立于 dashboard 查询，不必关闭 Isaac 面板。
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

容器缺少 SMB 客户端时，将 `pysmb` 装入临时目录。
不要安装到 Kit 的 Python 环境。
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
- 控制器时钟与 Isaac 日志时钟可能不同。
  保留原始时间，先比较同一控制器内的顺序和间隔。
  跨日志对齐前，核对时区和时钟偏差。

查询当前报警码时，复用面板控制实例和查询锁。
实例为 `builtins._mg400_isaac_joint_control`，下文记为 `panel`。
面板空闲时，在 `async with panel.pose_lock` 内查询。
调用 `await asyncio.to_thread(panel.control._command, "GetErrorID()")`。
不要从外部另开 `29999` 连接与面板轮询竞争。
独立查询 `29999` 前，先关闭空闲控制面板。
面板轮询与外部查询可能互相断开连接。
查询结束后，重新运行统一入口恢复面板和同步。
Isaac 日志位于 `/root/.nvidia-omniverse/logs/Kit/`。
检索 `MG400_KEYBOARD` 可定位应用故障，但其中可能没有具体报警码。

路径参考：[MG400 日志导出说明](https://techshare.co.jp/faq/dobot/mg400/support.html)。
报警码参考：[Dobot 四轴报警表](https://raw.githubusercontent.com/Dobot-Arm/TCP-IP-4Axis-Python/main/files/alarm_controller.json)。

## WebRTC 客户端自动操作

自动操作客户端时，按 [客户端操作流程](agents/isaac-client.md) 执行。
该流程包括状态查询、视频截图、点击、键盘输入和异常处理。
