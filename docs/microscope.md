[English](microscope.en.md)

# 显微镜算法

`microscope` 包在 Linux 上提供自动调焦、物镜切换、图像校准和侧视相机采集。
它不依赖桌面或 Tk。
这些 Python 模块尚未提供显微镜网页服务、面板或自动避让联锁。
真实运动前，操作员必须确认安全调焦行程及炉顶窗口的避让位置。

## Linux 环境

使用 Python 3.12 或更高版本。
在仓库根目录安装锁定版本的依赖：

```sh
uv sync --locked
```

默认的 `microscope` 依赖组包含 NumPy、opencv-python-headless 和 pyserial。
导入包不会打开相机或串口。
Toupcam 适配器为 `toupcam_camera.MicroscopeCamera`。
真实 Toupcam 采集必须具备匹配的 Linux 厂商库和设备权限。
主机配置见[部署指南](deploy.md)。

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

显微镜侧视相机为固定集线器端口 `12.1.4` 上的 AC410，拍摄物镜转盘。
相机预览的 go2rtc 进程独占其 V4L2 设备。
`USBCamera` 从 `http://127.0.0.1:1984/api/frame.jpeg` 读取 JPEG 图像。
它拒绝重定向，忽略环境代理配置。
相机必须保留在固定端口，不得复位共用 USB 集线器。

| 配置项 | 配置内容 |
| --- | --- |
| `MG400_CAMERA_DEVICES` | 按相机预览安装程序顺序排列的稳定设备路径。 |
| `MG400_MICROSCOPE_SIDE_STREAM` | 侧视相机对应的流：`camera1`、`camera2` 或 `camera3`。默认不选择任何流。 |
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
