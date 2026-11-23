[English](README.en.md)

# 压力传感器固件

此固件在 Arduino Uno 上读取 Adafruit MPRLS 带接口压力传感器（0 到 25 PSI）。串口波特率为 115200，ASCII 命令以换行符结束。

MPRLS 测量绝对压力。静置时读数为当地大气压，海平面约 1013 hPa。固件同时发送表压：绝对压力减去启动时测得的零点。

25 PSI 绝对压力只比大气压高约 10 PSI。不要施加更高的压力。不要让液体进入接口。

## 接线

| Uno | MPRLS 模块 |
| --- | --- |
| 5V | VIN |
| GND | GND |
| A5 | SCL |
| A4 | SDA |
| 不接 | 3Vo |
| D3（可选） | RST |
| D2（可选） | EOC |

模块自带 3.3 V 稳压器和 I2C 电平转换。如果接了 RST 或 EOC，在固件中把 `MPRLS_RESET_PIN` 设为 `3`，或把 `MPRLS_EOC_PIN` 设为 `2`。

## 编译

固件需要 Adafruit MPRLS Library 和 Adafruit BusIO：

```sh
arduino-cli lib install "Adafruit MPRLS Library"
arduino-cli compile --fqbn arduino:avr:uno firmware/pressure-sensor
```

此固件没有离线测试。

## 串口协议

启动时，板子发送 `VERSION pressure-sensor 1.0.0`。随后每 1 s 尝试打开 I2C 地址 `0x18` 上的传感器。修好接线后无需复位。传感器应答后，板子发送 `PRESSURE_READY MPRLS 0x18`，然后测量零点。

零点是 20 次读数的平均值。测量期间，接口必须通向室内空气。

命令不区分大小写。一行最多 15 个字符。

| 命令 | 回复 |
| --- | --- |
| `TARE` 或 `T` | `ZERO <hPa>`；没有有效读数时回复 `ERR TARE`，保留原零点。 |
| `STATUS` | `STATUS ready=<r> zero_hPa=<hPa> reads=<n> errors=<n>` |
| `VERSION` | `VERSION pressure-sensor 1.0.0` |

传感器应答后 `ready` 为 `1`。测得零点前 `zero_hPa` 为 `nan`。

板子每 200 ms（5 Hz）发送一次采样。Arduino IDE 的串口绘图器可直接显示这些行：

```text
abs_hPa:1013.25 gauge_hPa:0.12
```

其他行：

| 行 | 含义 |
| --- | --- |
| `ERR NO_ACK 0x18` | `0x18` 上没有设备应答。检查 VIN、GND、SDA 和 SCL。 |
| `ERR BEGIN 0x18` | 设备应答，但库无法打开它。检查 RST 和 EOC 设置。 |
| `ERR READ 0x<status> [NOT_POWERED] [INTEGRITY] [MATHSAT]` | 一次读数失败。`MATHSAT` 通常表示压力超出量程。 |
| `WARN ZERO_OUT_OF_RANGE` | 零点不在 700 到 1100 hPa 之间。让接口通向室内空气，然后发送 `TARE`。 |
| `ERR NOT_READY` | 传感器应答前收到了 `TARE`。 |
| `ERR UNKNOWN`、`ERR LINE_TOO_LONG` | 板子不接受该命令。 |

单位换算：1 PSI = 68.948 hPa，1 kPa = 10 hPa。
