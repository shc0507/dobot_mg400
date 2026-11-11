[English](README.en.md)

# 显微镜转盘与调焦固件

此固件在 Arduino Uno R3 上控制 2 个 A4988 驱动器。串口波特率为 115200，ASCII 命令以换行符结束。

## 接线与安全使用

| 功能 | Uno 引脚 |
| --- | --- |
| 转盘 DIR / STEP / EN | D8 / D9 / D10 |
| 调焦 DIR / STEP / EN | D11 / D12 / D13 |
| 共用 MS1 / MS2 / MS3 | D4 / D3 / D2 |

使能引脚低电平有效。启动时，两个驱动器均禁用，采用整步模式。空闲时 STEP 保持低电平。

重新烧录前，操作员必须保存板上的现有固件。代理只编译。H-06 确定调焦方向、避让位置和行程范围。自动运行前，H-21 验证固件。

人工监督配置时，调焦电机可在未建立参考位置时手动移动。固件不知道炉体几何或转盘避让位置。自动移动前，主机必须确认调焦零点和限位。主机只允许转盘在已验证的避让位置转动，下述有界校准移动除外。

物镜和轻量物镜校准可以在实测转盘范围 `NUDGE_WINDOW` 内，在工作焦点处转动。
此例外必须具备已知调焦零点、实测限位、静止的调焦轴，以及[显微镜指南](../../docs/microscope.md)中的监督确认。
未配置或为 `0` 时，保留避让规则。允许的正范围为 `1` 到 `20` 整步。
服务的 `objective_nudge` 也可在此范围内于工作焦点处转动转盘。
实验和完整物镜切换仍必须避让。

人工监督测量 `H-06` 时，先停止显微镜服务，再使用 `sudo mg400ctl calibrate setup`。
控制台不打开相机，不写配置文件，也从不发送 `FOCUS LIMIT`。
其计数位置不能测量物理行程。遵守显微镜指南中的操作说明和按键规则。

## 编译与离线测试

无需连接板卡即可编译：

```sh
arduino-cli core install arduino:avr
arduino-cli compile --fqbn arduino:avr:uno firmware/microscope-axes
```

固件不依赖外部 Arduino 库。协议测试使用 `g++`、模拟 GPIO、虚拟时钟和模拟串口输入：

```sh
uv run pytest tests/test_microscope_protocol.py
```

`microscope.sim.MicroscopeSimulator` 不访问设备，也不启动后台线程。`command(line)` 返回即时回复行。`feed(data)` 接收串口字节片段。`advance(seconds)` 推进虚拟时间并返回完成回复。`reset()` 模拟上电。可选的开关配置用于离线回零测试。

## 串口协议

命令不区分大小写。每行最多包含 95 个 ASCII 字符，不计 CR 和 LF。制表符可分隔单词。出现无效字符或行过长时，固件丢弃整行，直到 LF。数值参数必须是完整的十进制整数。固件拒绝多余参数。

| 命令 | 回复 |
| --- | --- |
| `OBJ ENABLE`, `FOCUS ENABLE` | `OK OBJ ENABLED`, `OK FOCUS ENABLED` |
| `OBJ DISABLE`, `FOCUS DISABLE` | `OK OBJ DISABLED`, `OK FOCUS DISABLED` |
| `OBJ SPEED <n>`, `FOCUS SPEED <n>` | `OK OBJ SPEED <n>`, `OK FOCUS SPEED <n>` |
| `OBJ MOVE <n>`, `FOCUS MOVE <n>` | `OK OBJ MOVE <n>`, `OK FOCUS MOVE <n>`，完成后返回 `DONE OBJ POS <n>` 或 `DONE FOCUS POS <n>` |
| `MICRO <1|2|4|8|16>` | `OK MICRO <n>` |
| `STOP OBJ`, `STOP FOCUS`, `STOP ALL` | `OK STOP OBJ`, `OK STOP FOCUS`, `OK STOP ALL` |
| `FOCUS ZERO` | `OK FOCUS ZERO` |
| `FOCUS LIMIT <min> <max>` | `OK FOCUS LIMIT <min> <max>` |
| `FOCUS HOME` | `OK FOCUS HOME`，随后返回 `DONE FOCUS HOME` 或 `ERR` 行 |
| `VERSION` | `VERSION microscope-axes 1.0.0` |
| `STATUS` | 下方的状态行 |
| `HELP` | 命令列表 |

速度范围为每秒 1..10000 个脉冲。移动参数和位置使用有符号 32 位脉冲计数。每个脉冲对应当前细分模式。目标位置超出计数范围时，固件在移动前拒绝命令。移动要求电机已使能且空闲。与原协议一致，零距离移动只返回 `OK` 行。

两个电机可同时移动。`MICRO` 为全局配置，任一电机移动时均拒绝更改。停止命令立即取消剩余脉冲。取消的移动不返回完成行，也不会重新启动。禁用电机也会停止该电机。错误回复以 `ERR` 开头。

`STATUS` 保留原有 3 行，并增加调焦参考状态：

```text
MICRO=1
OBJ: enabled=0 moving=0 speed=400 remaining=0 pos=0
FOCUS: enabled=0 moving=0 speed=800 remaining=0 pos=0
FOCUS_REF: zeroed=0 limited=0 min=0 max=0 homing=0
```

上电或复位后，控制板先输出 `DUAL_A4988_READY`，再输出 `STATUS` 各行。主机在复位延时后丢弃这些行。

## 调焦参考位置与限位

调焦电机已使能且空闲时，操作员可用 `FOCUS ZERO` 确立已验证的位置。它将脉冲位置设为 0，并清除旧限位。随后用 `FOCUS LIMIT <min> <max>` 配置实测安全范围。当前位置必须处于该范围内。配置限位前，零点必须已知。后续调焦移动的终点必须处于范围内，包括边界。

上电、复位、禁用调焦电机或实际改变细分模式，均使调焦零点和限位失效。重复当前细分模式会保留它们。普通停止保留脉冲位置和参考状态。计数不测量物理位置。如果机构打滑或在无计数脉冲时移动，操作员必须重新建立参考位置。

## 可选的调焦回零

现有板卡未接调焦限位开关。`FocusConfig.h` 采用以下安全默认值：

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `FOCUS_HOME_PIN` | `255` | 未接开关 |
| `FOCUS_HOME_DIRECTION` | `0` | 远离炉顶窗口的方向未知 |
| `FOCUS_HOME_MAX_STEPS` | `0` | 未批准行程预算 |
| `FOCUS_HOME_SPEED_SPS` | `100` | 回零脉冲速率 |
| `FOCUS_HOME_TIMEOUT_MS` | `30000` | 最长回零时间 |
| `FOCUS_HOME_ACTIVE_LEVEL` | `LOW` | 开关有效电平，采用 `INPUT_PULLUP` |

缺少开关、已验证方向或非零行程预算时，`FOCUS HOME` 拒绝移动。硬件验证后，操作员必须配置这些值。开关引脚不得与 UART 或驱动器引脚重叠。回零方向必须为 `-1` 或 `1`。可配置的最长超时为 60000 毫秒。

回零要求调焦电机已使能且空闲，开关起初未触发。回零清除旧参考状态和限位，只沿配置方向移动。开关触发后，脉冲暂停。开关必须持续触发 20 毫秒，固件才确立零点。成功后仍无软限位，并恢复之前的速度。

行程耗尽或超时会停止移动，并返回 `ERR FOCUS HOME TRAVEL` 或 `ERR FOCUS HOME TIMEOUT`。停止或禁用命令会取消回零。失败或取消后，参考位置仍未知。回零不会重试。回零期间，固件拒绝更改调焦速度、设零和配置限位。
