[English](microscope.en.md)

# 显微镜服务与算法

`microscope` 包在 Linux 上提供自动调焦、物镜切换、图像校准和侧视相机采集。
它不依赖桌面或 Tk。
契约服务检查调焦零点、行程限位和物镜转盘避让位置。
浏览器面板尚未提供。直接调用算法不会取得服务租约，也不提供服务联锁。
真实运动前，操作员必须确认安全调焦行程及炉顶窗口的避让位置。

## Linux 环境

使用 Python 3.12 或更高版本。
在仓库根目录安装锁定版本的依赖：

```sh
uv sync --locked
```

默认的 `microscope` 依赖组包含 NumPy、opencv-python-headless、pyserial、Flask 和 waitress。
导入包不会打开相机或串口。
Toupcam 适配器为 `toupcam_camera.MicroscopeCamera`。
真实 Toupcam 采集必须具备匹配的 Linux 厂商库和设备权限。
主机配置见[部署指南](deploy.md)。

## 契约服务

显示服务选项时不会打开设备。以下命令也可启动模拟器：

```sh
uv run python -m microscope --help
uv run python -m microscope --demo --bind 127.0.0.1 --port 8770
```

演示模式使用模拟电机、生成的显微图像和 ArUco 图像。
其调焦限位和避让位置是合成值，不能作为硬件安全配置。
服务启动时才打开设备。配置无效时，以状态码 `2` 退出。
收到 `SIGINT`、`SIGTERM` 或 `SIGHUP` 后，服务取消工作，停止并禁用电机，关闭设备后退出。
清理失败时，退出状态码不为零。

| 配置项 | 配置内容 |
| --- | --- |
| `MG400_MICROSCOPE_BIND` | 默认为 `127.0.0.1`。 |
| `MG400_MICROSCOPE_PORT` | 默认为 `8770`。 |
| `MG400_MICROSCOPE_BASE_PATH` | 默认为 `/`。下列路由路径相对于此前缀。 |
| `MG400_MICROSCOPE_TOKEN` | 至少 24 个可打印 ASCII 字符。绑定非回环地址时必须配置。 |
| `MG400_MICROSCOPE_SERIAL` | 控制板的稳定 `/dev/serial/by-id/` 路径。 |
| `MG400_MICROSCOPE_SIDE_STREAM` | go2rtc 独占的侧视相机流。 |
| `MG400_MICROSCOPE_FOCUS_LIMITS` | 必填的实测 `MIN,MAX` 整数脉冲，包含零点，且行程不为零。 |
| `MG400_MICROSCOPE_SWITCH_FOCUS` | 必填的实测避让位置，单位为脉冲，必须在限位内。没有归零开关时，必须为 `0`。 |
| `MG400_MICROSCOPE_HOME_SWITCH` | 必填硬件事实：有已配置的调焦归零开关时为 `1`，没有时为 `0`。 |

真实启动必须使用支持调焦参考状态的固件，并显式配置调焦参数。
使用 `H-06` 的实测值，不得把演示限位复制到硬件。
所有请求保留共用的令牌、Host、Origin 和转发头检查。
没有令牌时，只接受没有转发头的回环 Host 值。
POST 请求必须带 `Content-Type: application/json` 和 `X-MG400-Client`。
控制租约为 3 秒，工作期间必须续租。

| 路由 | 用途 |
| --- | --- |
| `GET /api/describe` | 契约版本、支持的命令、物镜 ID 和租约行为。 |
| `GET /api/state` | 调焦位置和零点状态、限位、物镜 ID、相机状态、忙碌状态、故障及命令结果。 |
| `GET /api/events` | 服务事件历史。 |
| `POST /api/heartbeat`, `POST /api/renew`, `POST /api/release` | 取得、续期或释放普通控制权。 |
| `POST /api/command` | 提交动作和参数。`202` 响应返回命令 ID，不代表命令完成。 |
| `POST /api/stop` | 任何已认证客户端都能停止，无须持有控制租约。读取其 `confirmed`、`pending` 或 `unconfirmed` 结果。 |

命令动作包括带 `id` 的 `select_objective`、带 `mode`（`full` 或 `local`）的 `autofocus`、带相对 `steps` 的 `focus_move`、`home_focus`，以及带可选 `path` 的 `snapshot`。
执行下一动作前，读取 `last_command` 中对应命令 ID 的结果。
每次转动物镜转盘前，服务完成避让移动，并重新检查位置静止。
调焦移动和自动调焦必须具有已知零点，且不得越过配置限位。
完整自动调焦扫描使用以实测行程内部为中心的窗口。
局部扫描使用靠近当前位置的较小窗口。
避让位置位于行程端点时，服务先把扫描中心移到限位内。
快照在运行根目录内新建 PNG，拒绝覆盖现有目标。
返回的 `captured_at` 是帧的单调接收时间，不是传感器曝光时间。

## 调焦零点与控制交接

有已配置的归零开关时，`home_focus` 建立零点并写入配置的固件限位。
没有开关时，操作员必须先把调焦轴放到已验证的避让位置，再设置零点。
该位置为 `0`，服务不会自动移动到未知的避让位置。

操作员先向 `POST /api/manual/heartbeat` 发送空对象，取得手动控制权。
通过 `POST /api/manual/renew` 保留控制权。
完成物理确认后，才向 `POST /api/manual/set_focus_zero` 发送 `{"confirm": true}`，并等待命令结果。
普通契约命令不能设置手动零点，自动化程序不得调用此操作。
自动化持有租约时，手动请求无法取得控制权，即使复用同一客户端 ID。
服务检查两轴静止，设置 `MICRO 4`，建立零点，写入限位，并验证固件结果。

零点建立成功后，通过 `POST /api/manual/release` 释放手动控制权，再由另一客户端取得普通控制权。
健康且空闲时，释放操作可保留已验证的参考状态，最长到原租约的剩余截止时间。
重新检查必须确认两轴静止、调焦轴已启用，且零点、限位和固件会话一致。
下一客户端必须在截止时间前取得控制权。释放不会增加租约时间。
检查失败或超时后，服务停止并禁用两轴，零点随之失效。

显式停止、租约到期、固件复位或调焦参考状态变化都会使服务零点失效。
命令或停止结果不确定时，服务锁存故障，锁定普通命令。
停止仍可用，但成功停止不会清除该故障。
排除原因后，通过受控的服务重启重新连接，并重新建立零点。
不得自动重试结果未知的运动，也不得用缓存位置恢复零点。

## Python 接口

向算法类显式传入相机和电机对象。
软件测试使用模拟对象。

| 接口 | 用途 |
| --- | --- |
| `microscope.autofocus.AutoFocuser` | 连续粗调焦扫描及自适应精调焦扫描。 |
| `microscope.aruco_locator.Locator` | 根据提供的参考图像测量标记位移。 |
| `microscope.aruco_switch.ArucoObjectiveSwitcher` | 根据侧视相机标记切换或对准物镜。 |
| `microscope.objective_calibration.ObjectiveCalibrator` | 根据图像位移图谱校准物镜运动。 |
| `microscope.light_calibration.LightObjectiveCalibrator` | 通过短行程扫描和自动调焦对准已知参考视图。 |
| `microscope.side_capture.capture_objective_series` | 物镜移动后，采集新的侧视图像。 |
| `microscope.motors.MotorController` | 管理显式串口连接和命令回复。 |
| `cameras.uvc.USBCamera` | 从本机 go2rtc 相机所有者读取侧视图像。 |
| `cameras.usb.enumerate_usb_cameras` | 列出 Linux 相机元数据和稳定设备路径，不打开采集。 |

图像位移、电机指令行程和固件位置是不同的值。
标记参考不能证明机械就位或避让空间安全。
调用方必须处理取消，并在工作结束时关闭相机连接、停止或禁用电机。

## 校准与运行文件

| 配置项 | 默认值 | 内容 |
| --- | --- | --- |
| `MG400_MICROSCOPE_CALIBRATION_DIR` | `/var/lib/mg400/calibration/microscope` | 校准 CSV、参考图像和侧视相机校准采集。 |
| `MG400_ORCH_RUNS_DIR` | `/var/lib/mg400/runs` | 自动调焦曲线、物镜切换日志和图像分析输出。 |

校准路径使用 `microscope.paths.calibration_path()`，运行路径使用 `microscope.paths.run_path()`。
相对路径按对应根目录解析。
绝对输出路径必须位于对应根目录内。
根目录必须为绝对路径，不得包含父目录跳转。
解析路径不会创建文件。

把 `objective_views/`、`current_objective.txt`、`objective_reference.png` 和 `side_camera_images/` 放在校准根目录内。
定位器从 `side_camera_images/` 读取 `microscope.aruco_locator.REFERENCES` 列出的文件。
图像尺寸和预期标记 ID 必须匹配。
改变相机位置或标记后，重新校准。
包内不附带历史 CSV 文件或图像。

输出辅助函数以 `0750` 创建目录，以 `0640` 创建文件。
进程 umask 可以进一步收紧权限。
现有输出目录和文件必须归当前用户所有，并保留私有权限。
写入函数拒绝父目录跳转、符号链接和带多个硬链接的文件。
不得用仓库路径或公共共享目录代替配置的根目录。

## 侧视相机流

显微镜侧视相机为固定集线器端口 `12.1.4` 上的 AC410。
相机预览的 go2rtc 进程独占其 V4L2 设备。
`USBCamera` 从 `http://127.0.0.1:1984/api/frame.jpeg` 读取 JPEG 图像。
它拒绝重定向，忽略环境代理配置。
相机必须保留在固定端口，不得复位共用 USB 集线器。

| 配置项 | 配置内容 |
| --- | --- |
| `MG400_CAMERA_DEVICES` | 按相机预览安装程序顺序排列的稳定设备路径。 |
| `MG400_MICROSCOPE_SIDE_STREAM` | 侧视相机对应的已配置 go2rtc 流。默认不选择任何流。 |
| `MG400_CAMERA_CAPTURE_SIZES` | 可选的逗号分隔 `WIDTHxHEIGHT` 列表，每个已配置设备对应 1 个值。 |

启用采集前，确认侧视相机设备路径对应的流。
尺寸列表为空时，选定的侧视流使用 `3840x2160`。
其他相机流保留 `640x480`。
`USBCamera` 默认要求 `3840x2160`，拒绝尺寸不同的帧。
修改采集尺寸时，必须使用匹配的参考图像，并由调用方显式指定预期分辨率。
实验室使用前，必须通过受监督的 `H-21` 检查，验证 USB 2 带宽和显微镜操作。

`USBCamera.get_frame_packet()` 返回帧 ID、单调接收时间和图像。
接收时间不是传感器曝光时间。
计算要求更新的帧时，使用 `after_id`。
采集错误会停止采集，并继续向调用方报告。

## 电机兼容性

把 `MG400_MICROSCOPE_SERIAL` 配置为控制板的 `/dev/serial/by-id/` 路径。
`MotorController.connect()` 显式打开串口，波特率为 `115200`，并查询 `VERSION`。
`VERSION microscope-axes 1.x.y` 回复会启用调焦完成确认。
新固件接受调焦命令后，程序等待 `DONE FOCUS POS`。
只有精确的 `ERR UNKNOWN_COMMAND` 回复会选用旧固件的估算等待。
版本未知或握手失败时，程序拒绝连接。

`focus_move_async()` 返回运动凭据，使自动调焦能在运动期间采样。
`wait_focus_done()` 根据该凭据等待完成。
`focus_move()` 合并这两步操作。
`objective_move()` 等待命令接受，调用算法仍按估算时间等待物镜行程。
停止请求可以中断完成等待。
传输或完成状态不确定时，程序锁存故障，必须显式重连。
命令、接线和固件限制见[固件指南](../firmware/microscope-axes/README.md)。

## 命令行入口

以下命令显示支持的选项，不连接设备：

```sh
uv run python -m microscope.autofocus --help
uv run python -m microscope.aruco_locator --help
```

不带 `--help` 运行自动调焦会连接串口控制器并打开 Toupcam 相机。
必填的 `--half-range` 表示起点两侧各自允许的行程，单位为脉冲。
操作员必须根据真实硬件限位确定此值。
`--port` 覆盖串口配置，`--output` 选择运行根目录内的文件。

定位器处理已保存的 `--image` 或 `--folder` 内的 PNG 图像，不访问电机。
它必须具备校准参考图像。
其 `--output` 目录默认为运行根目录下的 `microscope/scan_position_results`。
其他算法提供 Python API，没有独立命令行入口。

## 离线检查

以下显微镜测试使用模拟串口、生成的图像和临时数据目录：

```sh
uv run pytest -q tests/test_microscope_*.py
```

这些测试不连接实验室电机，不打开原生相机，也不向实验室硬件发送命令。
测试通过不能确定物理行程限位、安全避让位置或真实相机带宽。
