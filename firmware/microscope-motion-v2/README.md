[English](README.en.md)

# 显微镜运动控制器 v2

此 Arduino Uno R3 固件控制转盘和调焦 A4988 驱动器。Timer1 产生脉冲，逐渐改变速度，并在空闲时自动降低功耗。原固件保留在 `../microscope-axes/`。

## 接线

| 信号 | 转盘 | 调焦 |
| --- | --- | --- |
| DIR | D8 | D11 |
| STEP | D9 | D12 |
| ENABLE | D10 | D13 |
| SLEEP | D6 | D7 |

共用细分引脚保持 MS1 = D4、MS2 = D3、MS3 = D2。逻辑电路必须共地。ENABLE 和 SLEEP 均为低电平有效。连接 Arduino 输出前，先拆除 SLEEP 与 5V 的直接连接。运行时 RESET 必须为高电平。修改 RESET/SLEEP 连接前，检查实际驱动板接线。

启动时，两个电机均禁用并休眠。固件先设置非活动输出电平，再启用输出引脚。可选调焦原点开关不得使用 D6 或 D7。`FocusConfig.h` 默认不配置回零。

## 运动与功耗

- Timer1 以 20000 Hz 调度两个电机。STEP 高电平持续 50 微秒。Timer0 仍供 Arduino 时间函数使用。
- 速度范围为每秒 1 至 10000 个脉冲。加速度和减速度为每平方秒 2000 个脉冲。每次运动的起止速度不超过每秒 100 个脉冲。
- `MOVE` 使用当前细分下的有符号整数脉冲数。电机必须已启用且空闲。完成回复报告脉冲计数，不报告实测位移。
- 调焦电机无活动达 30 秒后禁用。每个轴无活动达 120 秒后休眠。运动中的轴不得休眠。
- 已接受的非零运动、运动完成、`ENABLE`、`DISABLE` 和 `STOP` 更新时间。`STATUS`、`VERSION`、`HELP`、`SPEED` 和 `MICRO` 不延长此时间。
- `ENABLE` 唤醒对应轴，至少等待 1 毫秒后启用输出，不发送运动脉冲。`MOVE` 不会隐式启用已禁用的电机。
- `DISABLE` 停止脉冲并取消保持电流。`STOP` 停止脉冲并保留当前启用状态。这两个命令均不会自动恢复已停止的运动。
- 调焦禁用或休眠后，脉冲零点和限位失效。自动调焦前，控制台必须重新获取视觉参考。休眠也可能改变驱动器的电气步进状态。

操作结束后，控制台可以提前禁用调焦电机。同一次自动调焦的测量和运动期间，必须保持调焦电机启用。释放电机后，保存的脉冲计数不能证明物理位置。

此固件独占 Timer1，不得添加同样占用 Timer1 的库。串口输出和命令解析在定时器中断之外执行。这些修改不增加编码器反馈。

## 串口接口

使用 115200 波特率和以换行结束的 ASCII 命令。现有命令仍可使用：

```text
VERSION
STATUS
OBJ ENABLE
OBJ SPEED 400
OBJ MOVE 5
OBJ DISABLE
FOCUS ENABLE
FOCUS SPEED 800
FOCUS MOVE -5
STOP ALL
MICRO 4
```

读取每条确认回复。收到 `DONE` 后，才能发送该轴的下一次运动。原始 `MOVE` 结束时不会自动禁用电机。主机仍可用 `FOCUS ZERO`、`FOCUS LIMIT` 和可选的 `FOCUS HOME` 管理参考。固件不测量转盘避让高度。

版本和状态格式如下：

```text
VERSION microscope-axes 2.0.0
MICRO=1
OBJ: enabled=0 moving=0 speed=400 remaining=0 pos=0 sleeping=1 acceleration=2000
FOCUS: enabled=0 moving=0 speed=800 remaining=0 pos=0 sleeping=1 acceleration=2000
FOCUS_REF: zeroed=0 limited=0 min=0 max=0 homing=0
```

更新后的主机接受固件版本 1 和 2。版本 1 不报告休眠和加速度。版本 2 自动调焦必须使用静止位置，不得按恒定速度估算图像位置。

## 编译与手动上传

替换前，保存已安装固件及其配置。在 Arduino IDE 中打开 `microscope-motion-v2.ino`，选择 Arduino Uno 和正确端口。接线完成后手动上传。其他程序打开串口前，先停止显微镜服务。

此命令只编译，不打开设备：

```sh
arduino-cli core install arduino:avr
arduino-cli compile --fqbn arduino:avr:uno firmware/microscope-motion-v2
```

上传后，检查 `VERSION` 和 `STATUS`。主机发送启用命令前，两个轴均必须报告禁用且休眠。操作系统前，先通过控制台建立相机校准。下述软件检查不验证物理避让、电流限值或丢步表现。

## 离线检查

原生测试程序用模拟 GPIO 和串口调用同一定时器处理函数。它可以暂停前台处理，同时继续产生定时脉冲。它不打开 Arduino 端口。

```sh
uv run pytest -q tests/test_microscope_motion_v2.py tests/test_microscope_motors.py
```

原生测试必须安装 `g++`。Python 模拟器通过 `MicroscopeSimulator(firmware_version=2)` 选择此固件；默认仍为版本 1，以支持旧测试。
