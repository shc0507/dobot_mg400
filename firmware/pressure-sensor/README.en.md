[中文](README.md)

# Pressure sensor firmware

This sketch reads an Adafruit MPRLS ported pressure sensor (0 to 25 PSI) on an Arduino Uno. The serial link uses 115200 baud and newline-terminated ASCII commands.

The MPRLS measures absolute pressure. At rest, it reads the local atmospheric pressure, about 1013 hPa at sea level. The firmware also sends gauge pressure: the absolute pressure minus a zero that it measures at startup.

25 PSI absolute is only about 10 PSI above the atmosphere. Do not apply more pressure. Keep liquids out of the port.

## Wiring

| Uno | MPRLS breakout |
| --- | --- |
| 5V | VIN |
| GND | GND |
| A5 | SCL |
| A4 | SDA |
| Not connected | 3Vo |
| D3 (optional) | RST |
| D2 (optional) | EOC |

The breakout has its own 3.3 V regulator and I2C level shifter. If you connect RST or EOC, set `MPRLS_RESET_PIN` to `3` or `MPRLS_EOC_PIN` to `2` in the sketch.

## Compile

The sketch needs the Adafruit MPRLS Library and Adafruit BusIO:

```sh
arduino-cli lib install "Adafruit MPRLS Library"
arduino-cli compile --fqbn arduino:avr:uno firmware/pressure-sensor
```

No offline tests exist for this sketch.

## Serial protocol

At startup the board sends `VERSION pressure-sensor 1.0.0`. Then it tries to open the sensor at I2C address `0x18` each 1 s. You can fix the wiring without a reset. When the sensor answers, the board sends `PRESSURE_READY MPRLS 0x18` and measures the zero.

The zero is the mean of 20 readings. Keep the port open to room air during this step.

Commands are case-insensitive. A line can have at most 15 characters.

| Command | Reply |
| --- | --- |
| `TARE` or `T` | `ZERO <hPa>`, or `ERR TARE` if no reading is valid. The old zero stays. |
| `STATUS` | `STATUS ready=<r> zero_hPa=<hPa> reads=<n> errors=<n>` |
| `VERSION` | `VERSION pressure-sensor 1.0.0` |

`ready` is `1` after the sensor answers. `zero_hPa` is `nan` until a zero is measured.

The board sends a sample each 200 ms (5 Hz). The Arduino IDE Serial Plotter can show these lines directly:

```text
abs_hPa:1013.25 gauge_hPa:0.12
```

Other lines:

| Line | Meaning |
| --- | --- |
| `ERR NO_ACK 0x18` | No device answers at `0x18`. Examine VIN, GND, SDA and SCL. |
| `ERR BEGIN 0x18` | The device answers, but the library cannot open it. Examine the RST and EOC settings. |
| `ERR READ 0x<status> [NOT_POWERED] [INTEGRITY] [MATHSAT]` | One reading failed. `MATHSAT` usually shows pressure out of range. |
| `WARN ZERO_OUT_OF_RANGE` | The zero is outside 700 to 1100 hPa. Open the port to room air and send `TARE`. |
| `ERR NOT_READY` | `TARE` came before the sensor answered. |
| `ERR UNKNOWN`, `ERR LINE_TOO_LONG` | The board did not accept the command. |

Unit conversions: 1 PSI = 68.948 hPa, 1 kPa = 10 hPa.
