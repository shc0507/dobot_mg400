[English](thermal.en.md)

# Thermal Master P1 热像仪

热像仪包为 Thermal Master P1 提供 Python 控制器、采集服务和网页面板，USB ID 为 `3474:45C2`。
原生温度数组为 160 × 120 像素。
FastAPI 和 uvicorn 提供网页面板，不必构建 JavaScript，也不依赖外部 CDN。
交互式 API 参考会从 CDN 加载 Swagger 资源。

## Linux 环境准备

使用 Python 3.12 或更高版本，以及 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
在仓库根目录安装锁定版本的依赖：

```sh
uv sync --locked
```

默认的 `thermal` 依赖组包含 FastAPI、uvicorn、NumPy、PyUSB 和随包提供的 libusb 运行库。
`dev` 组包含 httpx，用于离线 API 测试。
安装运行库不会授予 USB 访问权限。

管理员必须允许相机用户组访问 USB 厂商 `3474`、产品 `45c2` 的设备。
用该设备专用的 udev 规则，设置模式 `0660` 和预定的本地用户组。
修改权限后，重新加载规则并重新连接相机。
连接前，关闭其他相机应用。
同一时间只能有 1 个进程占用相机。

以下命令会访问真实 USB 硬件。
操作员必须先停止面板和其他占用相机的程序，再执行命令：

```sh
uv run thermal-cam --diagnose
uv run thermal-cam --probe --frames 30 --output captures/probe.npz
```

诊断命令报告后端和设备访问情况，不初始化相机或启动采集。
采集检查会打开相机，采集指定帧数，报告结果，然后关闭相机。
可选输出把最后一帧保存为无损 NPZ 文件。

## 启动面板

没有相机时，使用合成数据：

```sh
uv run thermal-cam --demo
```

打开 `http://127.0.0.1:8768`。
演示模式会明确标记数据为模拟值。
相机连接失败时，程序不会切换到演示数据。

使用真实相机时，默认命令会自动启动采集：

```sh
uv run thermal-cam
```

启动时先占用 HTTP 端口，再创建相机应用。
如果端口已占用，程序会退出，不打开相机。
使用面板期间，保持终端打开。
关闭浏览器标签页不会停止服务器。
在原终端按 Ctrl+C，停止服务器并释放相机。

其他启动选项：

```sh
uv run thermal-cam --no-connect
uv run thermal-cam --demo --bind 127.0.0.1 --port 8768
uv run thermal-cam --help
```

使用 `--no-connect` 时，服务器等待面板或 API 的 Connect 请求。
也可以通过 `python -m thermal_cam` 启动。
只运行 1 个服务器进程和 1 个工作进程。
采集相机数据时，不要启用开发模式的自动重载。

### 配置与访问

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `MG400_THERMAL_BIND` | `127.0.0.1` | 监听地址。`--bind` 覆盖此值。 |
| `MG400_THERMAL_PORT` | `8768` | HTTP 端口。`--port` 覆盖此值。 |
| `MG400_THERMAL_TOKEN` | 空 | 相机 API 的 Bearer 令牌。 |

监听非回环地址时，必须配置令牌。
令牌必须至少有 24 个可打印 ASCII 字符，首尾不能有空格。回环地址也遵守此规则。
配置无效时，程序以状态码 2 退出，不打开监听端口或相机。
未配置令牌时，请求的 Host 必须是回环地址，且不能带转发头。
浏览器请求必须保持同源。

配置令牌后，在面板的 Access token 字段中输入令牌。
页面只在内存中保留它，并通过 Authorization 请求头发送。
页面不会把令牌存入浏览器存储或 URL。
静态面板仍可访问，但相机路由要求令牌。
`/docs` 和 `/openapi.json` 路由也要求令牌。

## 使用面板

| 控件或显示 | 行为 |
| --- | --- |
| 实时画面 | 颜色及最低、最高、中心标记使用同一温度帧。 |
| 图像呈现 | 平滑模式显示 240 × 320 像素，原生模式显示 120 × 160 像素。两者都逆时针旋转 90°。 |
| 测量值 | 最高、最低、中心、平均、范围和总体标准差使用完整的原生温度矩阵。 |
| 像素检查 | 指向或选择像素。方向键按画面方向移动所选像素。 |
| 调色板与色标 | 修改颜色或自动／固定范围，不改变测量温度。 |
| 单位 | 显示摄氏、华氏或开尔文。API 和 CSV 温度始终使用摄氏度。 |
| Freeze | 保留显示帧供检查和导出，采集继续。 |
| 导出 | 把显示帧保存为 CSV、JSON 或 PNG。API 还提供 NPZ 数组。 |
| Connect / Disconnect | 获取或释放相机，不重启服务器。 |
| Calibrate shutter / NUC | 请求传感器非均匀性校正。 |
| Gain | 只支持高增益。低增益的 P1 温度换算尚未验证，因此禁用。 |

坐标采用原始传感器方向：`x` 是列，`y` 是行。
传感器的 `(0, 0)` 在旋转画面的左下角。
中心值来自 `x=80, y=60` 的单个像素，不是中央 4 个像素的平均值。
冻结帧或断开连接后保留的图像不是当前测量值。
面板会显示连接错误和过期数据。

## 分辨率与温度含义

P1 以约每秒 25 帧提供完整的原生 160 × 120 数据流。
[厂商的 320 × 240 Super IR 图像](https://thermalmaster.com/products/thermal-master-p1)来自该传感器。
[上游 USB 协议](https://github.com/jvdillon/p3-ir-camera/blob/e3205dca5727682ff2d903585d1dce5a1d19f1f6/P3_PROTOCOL.md#supported-devices)没有更高分辨率的测量模式。

面板使用标准图像插值，不使用厂商的 X³IR 处理。
画布按可用空间和显示密度，在 720 × 960 到 2880 × 3840 像素之间变化。
这些显示像素不会增加温度采样点。
原生宽高比为 4:3，旋转画面为 3:4。
普通和全屏视图都保留完整图像，不拉伸或裁剪。

PNG 导出包含旋转后的画布和页脚。
CSV、JSON 和 NPZ 保留 120 行 × 160 列的原生测量数据。
JSON 单独记录显示模式和旋转。

上游协议用无符号 16 位数表示每个热像素，单位是 1/64 开尔文：

```python
temperature_c = raw.astype("float32") / 64.0 - 273.15
```

单独的 8 位红外亮度图是帧数据，不是温度测量值。
读数是相机报告的表观温度。
应用不增加发射率、反射温度、距离或大气校正。
表面发射率、反射、相机状态和测量条件都会影响读数。
依赖绝对测量值前，先与合适的温度参考比较。

解释测量值前，让已连接的相机预热约 5 分钟。
然后使用 Calibrate shutter / NUC。
[上游故障排查指南](https://github.com/jvdillon/p3-ir-camera/blob/e3205dca5727682ff2d903585d1dce5a1d19f1f6/P3_PROTOCOL.md#troubleshooting)建议通过预热和快门校正减轻漂移。
快门校正改善传感器均匀性和运行稳定性。
它不能确立绝对温度精度，也不能代替已知温度参考。

## Python 集成

直接适配器的 USB 操作会阻塞。
在 1 个工作线程中使用它，用完后关闭：

```python
import numpy as np
from thermal_cam.camera import P1Camera

camera = P1Camera()
try:
    device = camera.open()
    ir, raw = camera.read()
    temperatures_c = raw.astype(np.float32) / 64.0 - 273.15
    print(device, temperatures_c[60, 80])
finally:
    camera.close()
```

`read()` 返回 `(ir, raw)` 数组，形状均为 `(120, 160)`，类型依次为 `uint8` 和 `uint16`。
`open()` 返回设备信息。
适配器还提供 `shutter()`、`set_gain("high")` 和 `stats()`。
请求低增益会报错。
不要让并发读取方共用适配器，也不要在面板占用相机时使用它。

导入应用工厂不会打开相机。
应用生命周期负责启动和停止采集：

```python
import os
import uvicorn
from thermal_cam.app import create_app

app = create_app(auto_connect=True, token=os.environ.get("MG400_THERMAL_TOKEN"))

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8768, workers=1)
```

使用 `create_app(demo=True)` 生成合成数据，或用 `create_app(auto_connect=False)` 等待连接请求。
监听地址和端口环境变量由命令行读取。嵌入式服务器自行提供配置。
挂载 ASGI 应用时，父应用必须管理采集生命周期。
[FastAPI 不会自动运行已挂载应用的生命周期钩子](https://fastapi.tiangolo.com/advanced/events/#sub-applications)。

读取已保存的帧，不打开相机：

```python
import json
import numpy as np

with np.load("captures/probe.npz", allow_pickle=False) as frame:
    raw = frame["raw"]
    ir = frame["ir"]
    temperatures = frame["temperatures_c"]
    metadata = json.loads(frame["metadata"].item())
    print(metadata["timestamp"], temperatures[60, 80])
```

## HTTP API

| 方法 | 路径 | 结果 |
| --- | --- | --- |
| `GET` | `/api/status` | 状态、来源、错误、设备信息、帧数、采集速率和帧龄。 |
| `GET` | `/api/frame` | 1 个内部一致的帧，含序号、时间戳、尺寸、统计、摄氏温度、原始值和亮度矩阵。 |
| `GET` | `/api/snapshot.npz` | 1 个帧的数值数组和元数据。 |
| `POST` | `/api/connect` | 请求连接并启动采集。 |
| `POST` | `/api/disconnect` | 停止采集并释放设备。 |
| `POST` | `/api/shutter` | 请求快门校正。 |
| `POST` | `/api/gain` | 用 `{"gain":"high"}` 设置高增益。低增益请求会遭拒。 |

访问受令牌保护的服务器时，通过 Bearer 请求头发送已配置的令牌：

```sh
curl --fail -H "Authorization: Bearer $MG400_THERMAL_TOKEN" \
  http://127.0.0.1:8768/api/status
```

未配置令牌时，省略该请求头。
温度数组使用 `temperatures_c[y][x]`。
每个响应内部一致，但分别请求的帧和快照可能来自不同帧。
用序号和时间戳关联测量值。
帧不可用或过期时，返回 HTTP 503，不编造读数。
采集工作线程负责 USB 读取和命令，HTTP 查看者读取快照。

## 自动化契约与测温区域

只读[仪器契约](adr/0001-instrument-service-contract.md)的基地址为 `http://127.0.0.1:8768`。
以下路由与面板 API 并存：

| 方法 | 路径 | 结果 |
| --- | --- | --- |
| `GET` | `/api/describe` | 契约版本、演示标志、区域定义和最大帧龄。`commands` 为空。 |
| `GET` | `/api/state` | 契约字段，以及相机状态、`frame_sequence` 和 `frame_age_s`。 |
| `GET` | `/api/events?since=0` | 指定序号之后的活动，以及 `next` 游标。同一事件也写入服务日志。 |
| `POST` | `/api/heartbeat` | 获取或续期仅作信息记录的租约。 |
| `POST` | `/api/release` | 释放调用者的租约。 |
| `POST` | `/api/stop` | 返回 `stop: "confirmed"`。此操作不改变采集状态，采集继续。 |
| `POST` | `/api/command` | 契约没有命令，返回 HTTP 400。 |
| `GET` | `/api/roi` | 读取具名区域或指定的传感器矩形。 |

租约持续 3 秒。读取和停止请求不要求持有租约。
租约到期或释放时，采集继续。
租约不限制现有面板控件。
用 `/api/disconnect` 停止采集并释放相机。
契约不控制输出，所以状态中的 `busy: false`、`locked: false` 和 `last_command: null` 固定不变。
使用读数前，检查 `connected`、`fault` 和帧龄。

这些路由保留上文的令牌、Host、Origin 和转发头检查。
契约 POST 请求必须包含 `Content-Type: application/json`，并且只能有 1 个 `X-MG400-Client` 请求头。
客户端 ID 必须包含 1–128 个可打印 ASCII 字符。
发送不超过 4096 字节的 JSON 对象。`/api/stop` 接受 `{"wait_s":0}`，其值必须是 0–15 的有限数值。
因为没有输出可停止，此请求立即返回。
契约错误使用 `{"error":"..."}`：输入无效为 HTTP 400，令牌错误为 401，访问遭拒为 403，非 JSON 输入为 415。

### 具名传感器区域

将具名区域保存在 `/var/lib/mg400/thermal/rois.toml`：

```toml
[regions.crucible]
x0 = 60
y0 = 40
x1 = 100
y1 = 80
```

这些示例边界不代表任何具体视图中的坩埚位置。
根据原生传感器图像设置边界。
名称包含 1–64 个 ASCII 字母、数字、`_` 或 `-`。
每个区域必须恰好定义 `x0`、`y0`、`x1` 和 `y1`，且各值为整数。
边界必须满足 `0 <= x0 < x1 <= 160` 和 `0 <= y0 < y1 <= 120`。
服务拒绝空矩形、反向矩形和越界矩形。
服务不会截断边界或旋转坐标。

这里 x 是传感器列，y 是传感器行。
矩形为 `temperatures_c[y0:y1, x0:x1]`：包含下界，不包含上界。
旋转后的显示画面不改变这些坐标。
例如，`x0=159, y0=119, x1=160, y1=120` 选择传感器的最后 1 个像素。

服务构造时读取此文件 1 次，此时尚未启动相机。
编辑文件后，重启服务。
文件不存在时，具名区域映射为空，仍可按坐标查询。
文件无效时，服务拒绝启动。服务不会改用示例区域。
嵌入式应用可以用 `CameraService(roi_path=...)` 选择其他路径。

### 区域读数

只能使用 1 种选择方式：`name`，或完整的 `x0`、`y0`、`x1` 和 `y1`。
不得混用两者，也不得重复查询参数。
例如：

```sh
curl --fail -H "Authorization: Bearer $MG400_THERMAL_TOKEN" \
  'http://127.0.0.1:8768/api/roi?name=crucible'
curl --fail -H "Authorization: Bearer $MG400_THERMAL_TOKEN" \
  'http://127.0.0.1:8768/api/roi?x0=60&y0=40&x1=100&y1=80'
```

未配置令牌时，省略 Bearer 请求头。
响应包含以 °C 表示的 `min_c`、`mean_c` 和 `max_c`，以及边界和 `name`（按坐标查询时为 `null`）。
所有统计值、`frame_sequence`、`timestamp`、`source`、`demo` 和 `frame_age_s` 来自同 1 个内部一致的帧。
帧龄使用单调时钟，以秒为单位。
帧龄超过 3 秒时，返回 HTTP 503，不包含温度值。
相机断开、发生错误，或快门与增益稳定期间没有帧时，同样返回此错误。
区域无效或名称未知时，在读取帧之前返回 HTTP 400。

这些读数与完整帧具有相同的表观温度限制。
相机预热约 5 分钟后，先校正快门，再解读测量值。
快门校正后，等待新帧。不得将保留的显示图像用作当前读数。
快门校正不能替代与合适温度参考的比较。

## 检查与故障排查

运行离线测试：

```sh
uv run pytest -q tests/test_thermal_*.py
```

测试使用合成数组、模拟 USB 访问、ASGI 客户端和假监听器。
它们不能认证相机或绝对温度精度。

| 现象 | 处理 |
| --- | --- |
| 找不到相机 | 检查 USB 数据线和供电。确认设备 USB ID 为 `3474:45C2`。 |
| 访问遭拒或设备忙 | 检查 Linux USB 用户组权限。连接前，关闭其他占用相机的程序。 |
| 反复超时或数据流错误 | 在面板中断开连接。重新连接 USB 线，再连接相机。检查线缆和端口。 |
| 温度停止变化 | 检查错误和帧龄。如果画面已冻结，恢复实时显示。 |
| 温度不合理 | 检查目标覆盖的像素及发射率／反射。与已知参考比较。修改调色板不会校准温度。 |
| HTTP 端口已占用 | 打开现有面板，或先停止原进程再重启。修改端口不会允许其他程序同时占用相机。 |
| HTTP 401 | 输入已配置的令牌，或提供对应的 Bearer 请求头。 |
| HTTP 400 或 403 | 检查 Host、Origin 和转发头。无令牌服务器只允许回环访问。 |

## 来源说明

本包来自 Chemistry-Integration 的热像仪控制器。
未修改的第三方驱动来自 [jvdillon/p3-ir-camera](https://github.com/jvdillon/p3-ir-camera)，提交为 `e3205dca5727682ff2d903585d1dce5a1d19f1f6`。
协议来自社区逆向研究，本应用独立于 Thermal Master。
分发本包时，保留 [Apache 许可证](../src/thermal_cam/_vendor/LICENSE-p3-ir-camera)和[第三方声明](../src/thermal_cam/_vendor/NOTICE.md)。
