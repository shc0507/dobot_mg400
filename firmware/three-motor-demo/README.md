[English](README.en.md)

# 三个 A4988 电机演示

在 Arduino IDE 中打开 `three-motor-demo.ino`。
把 `MotorControl.h`、`MotorControl.cpp` 和 `MotorPins.h` 放在同一草图目录。
Arduino 会自动编译 `.cpp`。
使用库管理器。
安装 **AccelStepper by Mike McCauley**。
选择开发板和端口，然后上传。

目标开发板为 Arduino Uno R3，FQBN 是 `arduino:avr:uno`，核心是 `arduino:avr`。
只有操作员可以在有人监督的硬件验证中上传固件。
上传前，安装 STG-02 主机更新，使主机支持下文追加的状态字段。
如果旧主机要求每条电机状态以 `spr` 结尾，它就无法解析此固件。

## 离线编译和检查

在仓库根目录运行以下命令。编译不使用开发板或串口：

```sh
arduino-cli core install arduino:avr
arduino-cli lib install AccelStepper
arduino-cli compile --fqbn arduino:avr:uno firmware/three-motor-demo
python firmware/three-motor-demo/tests/run.py --library /path/to/AccelStepper
```

本机检查依赖 Python 和 `g++`。
检查把已安装的 AccelStepper 源码与假的 GPIO、时钟和串口接口链接。
检查还会拒绝引脚冲突，并确认禁用最小限位输入时拒绝回零。
Arduino CLI 的数据、下载和用户目录使用工作区内的配置。

## 网页控制面板

位移台面板（`stage-panel`）是此草图的网页界面。
面板提供独立的速度和步数设置、方向按钮，并支持选择串口。
每次运动完成后有一秒冷却时间。
面板还提供 Stop all、恢复默认值和浅色／深色主题。

此草图支持网页界面，包括持续运动直到松开的独立方向长按按钮。
上传此草图后，可以使用长按运动及其一秒超时。
还可以用全局步进模式控件设置全部三个电机。

面板的安装、启动和连接方法见[位移台面板说明](../../docs/stage-panel.md)。
通过网页界面连接前，关闭串口监视器。

如果主机有多个串口设备，把 `MG400_STAGE_SERIAL` 设为 Arduino 的 `/dev/serial/by-id/` 路径。
网页界面随后只显示该端口。

网页界面用以下标签代替 CCW/CW：

| 电机 | CCW（`ccw`）标签 | CW（`cw`）标签 |
| --- | --- | --- |
| Bottom | Up | Down |
| Top | Right | Left |
| Insert | Insert In | Insert Out |

长按按钮在每个标签前加 **Hold**，例如 **Hold Up** / **Hold Down**。
运动状态使用相同的方向名称。
这些只是显示标签，串口方向仍为 `ccw` / `cw`，电机行为不变。

网页界面还提供可选的 **Keyboard controls** 区域，默认是 **Off**。
按住 **W/S** 控制 Bottom 向上／向下。
按住 **A/D** 控制 Top 向左／向右。
按住 **J/K** 控制 Insert 向内／向外。
也可以按住页面上显示的按键。

每个按键使用对应电机通道选择的速度，以及相同的长按／松开命令和超时。
关闭此区域会释放它当前的长按。
键盘行为和停止控件见[位移台面板说明](../../docs/stage-panel.md)。

## 接线

默认分配如下，可在 `MotorPins.h` 中修改：

| 电机 | STEP | DIR | SLEEP | LimitMin | LimitMax |
| --- | --- | --- | --- | --- | --- |
| Bottom_Motor | D5 | D4 | D12 | A2 | A3 |
| Top_Motor | D7 | D6 | D13 | A4 | A5 |
| Insert_Motor | D3 | D2 | D11 | A0 | A1 |

每个驱动器有独立的 SLEEP 输出。
每个 SLEEP 输出只能连接对应电机驱动器的 SLP 输入。

每个限位触点接在对应输入和 GND 之间。
输入使用 `INPUT_PULLUP`，LOW 表示触发。
最小限位位于 CCW 端，最大限位位于 CW 端。
有人监督验证时，检查实际运动方向。
`InsertLimitMin`、`InsertLimitMax`、`BottomLimitMin`、`BottomLimitMax`、`TopLimitMin` 和 `TopLimitMax` 设置这些引脚。
`255` 表示禁用输入。没有最小限位输入的电机拒绝回零。
没有最大限位输入时，不提供最大限位触点保护。
编译会检查启用的输入之间及其与输出之间是否冲突，并限制在 Uno 的 D2..D19 范围。
默认接线使用全部 18 个非串口 I/O 引脚。D0/D1 必须空闲。

分辨率输入共用。把每个 Arduino 引脚连接到**全部三个**驱动板上的对应输入：

| A4988 输入 | Arduino 引脚 |
| --- | --- |
| MS1 | D10 |
| MS2 | D9 |
| MS3 | D8 |

### 按接线分配电机名称

只修改 [`MotorPins.h`](MotorPins.h) 中的引脚分配。
每对引脚按 **`{STEP, DIR}`** 的顺序排列。
使用 Arduino 引脚编号，不要使用芯片的物理引脚编号。
例如，Bottom 驱动器接 STEP D5 / DIR D4。
Top 驱动器接 STEP D3 / DIR D2。
如果按此接线，交换对应引脚对：

```cpp
constexpr PinPair Bottom = {5, 4};
constexpr PinPair Top = {3, 2};
constexpr PinPair Insert = {7, 6};
```

`BottomSleep`、`TopSleep` 和 `InsertSleep` 设置各电机的 SLEEP 输出。
每项设为连接对应驱动器 SLP 输入的 Arduino 引脚。
交换两个引脚对时，确认每个 SLEEP 引脚仍接同一电机的驱动器。

也可以选择开发板上其他支持输出的引脚，或分别修改 STEP 和 DIR 来匹配接线。
全部十二个 STEP/DIR/MS/SLEEP 引脚必须互不相同。
D0/D1 留给串口通信。
编译时会拒绝重复分配和 D0/D1。
对于定义了 `NUM_DIGITAL_PINS` 的开发板，编译时还会拒绝超出范围的编号。
检查开发板引脚图中的其他引脚限制。

上传修改后的草图前，停止全部电机。
断开网页应用连接。
上传后重新连接。
**Bottom**、**Top** 和 **Insert** 控件及串口命令就会使用新的引脚对。
固件会在重启后保留这些分配。
Python 应用不必配置引脚或重启。
上传会把指令位置清零。

STEP 和 DIR 必须是**独立信号**，不得短接或在驱动器之间共用。
驱动脉冲、方向切换、启动和停止都使用这份映射。
代码中没有其他引脚编号要修改。

### 驱动器连接

MS1/MS2/MS3 由上表中的共用 D10/D9/D8 输出驱动。
启动时，全部三个输出设为 LOW，使用整步模式。

固件控制 SLEEP 输出。
LOW 让驱动器睡眠，HIGH 唤醒驱动器。
Pololu 载板的 SLP 有上拉电阻。
Arduino 复位期间，固件不控制 SLP。
因此，在启动代码把 SLEEP 设为 LOW 之前，驱动器可能保持唤醒。

在 Uno 等许多开发板上，D13（`TopSleep`）还控制板载 LED。
串口或复位按钮触发复位后，引导程序会闪烁此 LED。
因此，面板每次连接时，Top 驱动器可能多次睡眠和唤醒。
修改 `TopSleep` 时，必须选择独立输出引脚并修改对应接线。
默认模拟引脚现在用于限位触点。
修改并验证受影响的接线后，才能改用其中的引脚。

不要让 RST 悬空。
Pololu 载板的 RST 没有上拉或下拉电阻。
把 RST 接到载板上相邻的 SLP 引脚，或接到逻辑电源 VDD。
如果 RST 接到 SLP，RST 会跟随 SLEEP 输出。
驱动器只在 RST 为 HIGH 时工作。

EN 必须为 LOW，驱动器输出才能工作。EN 可以接 GND。
API 不控制 EN 或 RST。
Arduino 和驱动器必须共地，并使用合适的逻辑电源和电机电源。
参见[载板接线参考](https://www.pololu.com/product/1182)。

### 驱动器睡眠

每个驱动器有独立的睡眠计时器：

1. 启动时，全部三个驱动器睡眠。
2. 如果驱动器正在睡眠，接受的运动、`start` 或 `hold` 命令会把对应电机的 SLEEP 输出设为 HIGH。
   固件随后等待 2 ms，再发送第一个 STEP 脉冲。
   等待期间，串口命令和其他电机仍能运行。
3. 电机停止后，驱动器保持唤醒并锁住电机。
4. 如果持续 `SleepTimeoutMs` 没有运动，固件把 SLEEP 设为 LOW。
   默认值为 `10000UL`（10 s）。

超时前开始新运动，会让驱动器保持唤醒。
选择电机、`status`、`microstep`、其他设置，以及对空闲电机发送 `stop`，都不会唤醒驱动器。
这些操作也不会重置睡眠计时器。

`awake <ms>` 是明确的例外。
它唤醒所选驱动器，并在运动结束后延长保持时间，最长 60000 ms。
空闲时续期会重新开始该时间段。
`awake 0` 取消延长，但普通空闲计时器仍然有效。
状态字段 `awake=1` 表示 SLEEP 为 HIGH，包括唤醒延迟期间。

要修改超时，编辑 [`MotorPins.h`](MotorPins.h) 中的 `SleepTimeoutMs`。
然后重新上传草图。
该值必须大于零。

睡眠的驱动器不会锁住电机，电机没有保持扭矩。
外力可以转动电机轴，位置计数不会记录这种运动。
[A4988 数据手册](https://www.allegromicro.com/-/media/files/datasheets/a4988-datasheet.ashx)说明，SLEEP 为 HIGH 时允许启动。
此时驱动器会让电机移到 Home 微步位置。
因此，驱动器唤醒时，电机也可能运动。

## 限位、回零和软件范围

触点变为 LOW 时，固件立即禁止朝该限位发送脉冲，不使用减速斜坡。
输入稳定 5 ms 后，状态才改变。
释放稳定 5 ms 后，才能再次朝该端运动。
短暂的触点抖动也可能停止运动。
如果另一端限位和软件范围允许，仍可以向离开该限位的方向运动。
这些检查适用于定步运动、`start` 和 `hold`。触点不能检测断线。

`home` 作用于所选的空闲电机。
它清除该电机的参考位置和软件范围，再执行以下阶段：

1. 沿 CCW 以每秒 100 个 STEP 脉冲寻找最小限位。
2. 沿 CW 退回 200 个 STEP 脉冲。最小限位必须释放。
3. 沿 CCW 以每秒 25 个 STEP 脉冲再次接近。触点稳定后停止，位置设为 0，并设置 `homed=1`。

如果最小限位已经触发，直接开始退回。
每次退出回零时，固件都恢复配置的普通速度。
方向保持为最后一个回零阶段的方向。
`MotorPins.h` 中的 `HomeSpeed`、`HomeSlowSpeed`、`HomeStepLimit` 和 `HomeTimeoutMs` 设置速度和上限。
默认整个操作最多发送 10000 个脉冲或持续 120000 ms。
先达到任一上限，操作就失败。`HomeBackoffSteps` 为 200。
限位冲突、退回受阻、最小限位未释放或位置溢出也会使回零失败。

Bottom 电机接受回零后立即回复 `OK: bottom home`。
使用 `status` 检查完成状态。
失败时，固件在完整串口行中输出 `ERROR: home failed: <reason>`，包括确认回复之后发生的失败。
`stop` 和 `stop all` 取消回零。
回零失败或取消后保持 `homed=0`，不会重试。
回零期间，其他电机和串口处理继续运行。

`soft <min> <max>` 设置从回零点起算的 STEP 脉冲范围，包含两端。
所选电机必须空闲且已回零，最小值不能大于最大值。
定步目标超出范围时返回 `ERROR`。
持续运动和长按运动在边界停止，不使用减速斜坡。
如果新范围不包含当前位置，只能朝范围内运动，且定步目标必须在范围内。
复位和上电会清除全部参考位置和范围。
实际改变微步模式也会清除全部电机的参考位置和范围，但不重置计数器。
成功回零建立开关参考位置，不提供编码器反馈。
睡眠时的外力运动仍可能让物理位置失准。

## 全局微步设置

发送 `microstep 1`、`microstep 2`、`microstep 4`、`microstep 8` 或 `microstep 16`。
无论当前选择哪个电机，此命令都作用于**全部三个电机**。
只有全部电机空闲时，命令才会成功。
确认回复例如 `OK: microstep 16`。
发送 `microstep` 查询当前模式。
`status` 也会先显示全局信息，例如 `microstep=16`，再显示三个电机的信息。

| 命令 | MS1 / D10 | MS2 / D9 | MS3 / D8 | 分辨率 |
| --- | --- | --- | --- | --- |
| `microstep 1` | LOW | LOW | LOW | 整步 |
| `microstep 2` | HIGH | LOW | LOW | 半步 |
| `microstep 4` | LOW | HIGH | LOW | 四分之一步 |
| `microstep 8` | HIGH | HIGH | LOW | 八分之一步 |
| `microstep 16` | HIGH | HIGH | HIGH | 十六分之一步 |

切换模式不产生 STEP 脉冲，并保留有符号脉冲计数。
实际改变模式会为全部电机设置 `homed=0` 和 `soft=0`。
重复当前模式不会改变这些标志。
模式保持到开发板复位，不写入 EEPROM。

`steps`、速度、加速度、位置和剩余计数都按当前模式的 STEP 脉冲计算。
在十六分之一步模式下，`steps 50` 请求 3.125 个整步。
`spr 200` 仍表示每圈 200 个整步的电机。
在十六分之一步模式下，`rev` 产生 3,200 个脉冲。
切换分辨率时，网页界面保留速度和步数的数值设置。

[A4988 数据手册](https://www.allegromicro.com/-/media/files/datasheets/a4988-datasheet.ashx)规定，新模式在下一个 STEP 上升沿生效。
要保留绝对位置，必须在两种模式共用的译码器位置切换分辨率。
脉冲计数器没有编码器或译码器状态反馈，无法确认这种对齐。
固件只在空闲时修改 MS 输出，并在后续 STEP 脉冲前提供 1 µs 建立时间。

## 串口测试

状态保留原有字段顺序，并追加参考位置、触点、范围和驱动器状态：

```text
microstep=1
* bottom cw idle pos=0 remaining=0 speed=200.0 accel=50.0 spr=200 homed=0 limits=00 soft=0 awake=0
  top cw idle pos=0 remaining=0 speed=200.0 accel=50.0 spr=200 homed=0 limits=00 soft=0 awake=0
  insert cw idle pos=0 remaining=0 speed=200.0 accel=50.0 spr=200 homed=0 limits=00 soft=0 awake=0
```

`limits=<min><max>` 中，1 表示触点触发，0 表示未触发。
`soft=1` 表示已启用范围。
全部原有命令保留确认回复原文。

把串口监视器设为 **115200 波特**。
行尾选择 **Newline** 或 **Both NL & CR**。
每行发送一个命令。
命令不区分大小写，并接受空白字符。
启动时不会自动启动任何电机。

```text
motor bottom
dir cw
steps 200
```

运动完成后，反向运动。使用 `status` 查看 `idle` 状态：

```text
dir ccw
rev
```

分别试用其他电机：

```text
motor top
dir cw
rev 0.5
motor insert
dir ccw
steps 100
status
```

选择一个电机不会停止另一个电机，因此这也会测试并发运动。
持续旋转：

```text
motor bottom
stop
speed 100
start
status
stop
stop all
```

| 命令 | 动作 |
| --- | --- |
| `motor bottom`, `motor top`, `motor insert` | 选择电机，也可以用 `motor 1`、`motor 2`、`motor 3` |
| `dir cw`, `dir ccw` | 空闲时设置所选电机的方向 |
| `steps 200` | 按该方向发送正数个 STEP 脉冲 |
| `rev`, `rev 2`, `rev 0.5` | 旋转一圈、两圈或半圈 |
| `start` | 以恒定速度持续旋转，直到停止 |
| `hold` | 以恒定速度旋转，1 秒未收到 `keepalive` 后停止 |
| `keepalive` | 续期所选电机当前的长按，不会重新启动已过期的长按 |
| `stop`, `stop all` | 立即取消所选电机的运动，或全部运动 |
| `speed 200` | 空闲时设置所选电机的速度上限（1..1000 STEP 脉冲/秒） |
| `accel 50` | 空闲时设置定步运动的加速度（1..10000 STEP 脉冲/秒平方） |
| `spr 200` | 空闲时设置每圈整步数（1..65535） |
| `microstep`, `microstep 16` | 查询或设置全局分辨率（1、2、4、8、16）。修改时全部电机必须空闲 |
| `home` | 开始所选空闲电机的有界回零 |
| `soft 0 10000` | 设置所选已回零电机的范围，包含两端 |
| `awake 60000`、`awake 0` | 续期或取消所选驱动器的保持延长 |
| `status` | 显示全部电机，`*` 标记所选电机 |
| `help` | 打印命令列表 |

计数必须为正数，反向运动用 `dir ccw`。
圈数按当前分辨率四舍五入到最接近的 STEP 脉冲数。
如果请求四舍五入后为零，固件会拒绝请求。
无效、过大或参数过多的命令都会被拒绝。
所选电机运动时，固件会拒绝新的运动或配置。先用 `stop` 停止它。
运动期间仍可以用 `awake` 续期。

位置和剩余计数有正负号，CW 为正。
位置表示启动或最近一次成功回零以来指令 STEP 脉冲的有符号计数，没有编码器反馈。
不同分辨率下的脉冲计数不能当作同一种物理距离。

默认的**每圈 200 个整步**适用于 1.8 度电机。
对于 0.9 度电机，使用 `spr 400`。
此设置按电机分别保存，重启后失效。
实际 CW/CCW 方向取决于接线和观察方向。

定步运动会加速和减速。
`start`、`hold` 和 `stop` 立即改变脉冲生成。
停止后，驱动器会继续给线圈供电，直到睡眠超时结束。
参见[驱动器睡眠](#驱动器睡眠)。

按住按钮时，网页界面使用 `hold`，约每 200 ms 续期一次。
松开时，发送 `motor <name>`，然后发送 `stop`。
每个电机有独立的长按超时，计时在 Arduino 上运行。
因此，即使 USB 断开或服务器故障，超时仍会结束长按运动。
单独从串口监视器发送的 `start` 命令会持续运行，直到明确停止。

## 在其他草图中使用 API

```cpp
#include "MotorControl.h"

void setup() {
  MotorControl::begin();
  MotorControl::selectMotor(MotorControl::Motor::Top_Motor);
  MotorControl::setDirection(MotorControl::Direction::Counterclockwise);
  MotorControl::setStepsPerRevolution(200);
  MotorControl::setSpeed(200);
  MotorControl::setAcceleration(50);
  MotorControl::rotateOneRevolution(); // Or rotateSteps(200), rotateRevolutions(0.5).
}

void loop() {
  MotorControl::update(); // Services all motors; call as often as possible.
}
```

`update()` 还会在唤醒延迟后开始步进，并让空闲驱动器睡眠。
即使全部电机空闲，也要调用它。

运动函数立即返回。如果请求遭拒，返回 `false`。
主循环可以通过 `isRotating(motor)` 和 `getStatus(motor, status)` 跟踪完成情况。
`startRotation()`、`stopRotation()` 和 `stopAll()` 提供持续旋转和取消功能。
函数签名见头文件。
底层运动库见 [AccelStepper 参考](https://www.airspayce.com/mikem/arduino/AccelStepper/classAccelStepper.html)。
