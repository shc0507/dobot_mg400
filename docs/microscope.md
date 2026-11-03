[English](microscope.en.md)

# 显微镜服务与算法

`microscope` 包在 Linux 上提供自动调焦、物镜切换、图像校准和侧视相机采集。
它不依赖桌面或 Tk。
契约服务检查调焦零点、行程限位和物镜转盘避让位置。
浏览器面板通过服务提供日常控制。直接调用算法不会取得服务租约，也不提供服务联锁。
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
| `MG400_MICROSCOPE_FURNACE_URL` | 炉控元数据接口，默认为 `http://127.0.0.1:8769/`。 |
| `MG400_FURNACE_TOKEN` | 读取元数据所用的现有炉控 API 令牌。显微镜不取得炉控控制权。 |
| `MG400_FURNACE_ARCHIVE_DIR` | 默认为空。操作员挂载归档共享目录后，才能配置绝对路径。 |

真实启动必须使用支持调焦参考状态的固件，并显式配置调焦参数。
使用 `H-06` 的实测值，不得把演示限位复制到硬件。
所有请求保留共用的 Host、Origin 和转发头检查。
受保护的数据和动作路由还要求配置的令牌。
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
返回的 `captured_at` 保留源时钟。真实和演示主相机帧都使用主机墙上时间。侧视相机帧使用单调接收时间。
这些值不代表传感器曝光时间。

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

## 浏览器面板

打开配置的基本路径下的 `/`。使用上方演示命令时，打开 `http://127.0.0.1:8770/`。
固定页面和资源不包含服务状态或访问令牌。
配置了令牌时，输入令牌并选择 **Connect**。
页面在内存中保留令牌，通过 `Authorization` 请求头发送。令牌不进入 URL 或浏览器存储。

普通操作前，选择 **Acquire control**。连接期间，页面续期已有租约。
其他客户端持有租约时，页面无法取得控制权，也不会接管自动化控制。
物镜按钮使用已加载校准参考中的 ID。参考缺失或无效时，这些按钮不可用。
安装有效参考图像后，重启服务以重新加载缓存的校准元数据。
完整和局部自动调焦、限位内点动及快照使用上方契约命令。
下一动作前，读取命令结果。**Stop** 无须持有控制租约。

没有归零开关时，先把调焦轴放到已验证的避让位置，再选择 **Set focus zero…**。
在对话框中确认物理位置。页面释放空闲的普通租约，取得手动控制权，再提交确认后的零点命令。
命令成功后，页面释放手动控制权，并在交接剩余期限内立即取得普通控制权。
任何请求失败都会停止该流程。访问或租约失败后，显式重新连接。
有已配置的归零开关时，**Home focus…** 在归零前要求确认。
关闭页面会停止本地请求和续租。租约到期后，服务停止并禁用两轴，零点随之失效。

| 预览路由 | 结果 |
| --- | --- |
| `GET /api/panel-info` | 已校准的物镜 ID、校准可用状态及归零开关配置。 |
| `GET /api/camera/microscope/mjpeg`, `GET /api/camera/side/mjpeg` | 从已有相机所有者读取的已认证 MJPEG 视图。 |
| `GET /api/camera/microscope/frame.jpg`, `GET /api/camera/side/frame.jpg` | 单张 JPEG，没有更新的帧时返回 `204`。 |

预览请求接受可选的非负 `after_id` 游标。
每张 JPEG 带 `X-Frame-Id` 和 `X-Captured-At` 响应头。主相机时间戳使用主机墙上时间，侧视相机使用单调接收时间。
不得跨相机比较时间戳，也不得将其作为传感器曝光时间。
浏览器通过已认证的 fetch 请求读取预览，并丢弃过期结果。
最多同时保留 4 个 MJPEG 响应。额外的流返回 `503`，关闭其他预览后再连接。
该限制为控制请求保留工作线程，但不保证停止响应时间。
预览读取不会取得或续期控制权。关闭相机所有者前，服务等待已接受的读取完成。

## 录像与炉控序列

使用显微镜面板录制本服务已独占的相机。
录像命令要求有效的普通控制权和空闲服务，不取得炉控控制权，也不启动炉控序列。
采集和收尾期间，保持网页连接并持有控制权。

| 动作 | 参数与结果 |
| --- | --- |
| `start_recording` | 提供新的 `recording_id`，恰好包含 32 个小写十六进制字符。可选字段为 `run_name`、`mode`、`capture_interval_seconds` 和 `output_fps`。 |
| `stop_recording` | 提供同一客户端正在录制的 `recording_id`。完成条件包括采集关闭、摘要视频编译和已配置的归档复制。 |
| `record_with_sequence` | 用 `enabled: true`、新的 `recording_id` 和可选录像字段预备单次录制。用 `enabled: false` 和该 ID 取消。 |
| `set_stabilization` | 用布尔值 `enabled` 改变相机防抖状态。 |

面板提供 **Start recording**、**Finish recording** 以及独立的序列录制预备和取消控件。
可选运行名称最多包含 80 个可打印字符，不能用来选择文件路径。
`time_lapse` 默认每隔 30 秒采集一帧，输出帧率为每秒 1 帧。
`live` 默认输出帧率为每秒 30 帧，没有采集间隔。
显式 `capture_interval_seconds` 范围为 0.1 至 3600，显式 `output_fps` 范围为 0.1 至 120。

读取 `recording`、`recording_sequence`、`last_result` 和匹配的 `last_command` 回执。
报告成功前，检查录像 ID、命令 ID、动作、客户端和预期结果。
面板最多观察收尾过程 600 秒，其他命令的观察限时为 120 秒。
这些是观察限时，不保证编码器在限时内完成。结果未知时，不自动重试。
移动或收尾期间，普通录像控件不可用，但全局 Stop 仍可用。

预备序列录制时，炉控状态必须新鲜、健康且空闲。
服务等待新的序列 `started_at`，随后只录制该序列。
同一序列必须进入 `complete` 或 `cooling`，才能正常收尾。
每次预备只生效一次。监视器不能取得或续期控制租约。

服务以 1 Hz 轮询炉控状态，逐帧元数据读取缓存值。
`video.timestamps.csv` 的列保持不变。
演示模式使用生成帧，不连接炉控。除非嵌入程序提供模拟读数，否则元数据留空。

炉控读数不可用时，元数据字段留空，并添加服务事件，采集继续。
缺少回复不代表序列结束。序列标识改变或序列不健康时，不正常完成录像。

服务在 `/var/lib/mg400/recordings/` 内独占预留录像 ID。
输出文件创建时即为私有，并拒绝已存在或不安全的目标。
完成收尾的录像保留视频分段、`video.timestamps.csv` 和目标时长为 3 分钟的摘要视频。较短片段不会延长。
取消或失败的录像保留已有本地文件，不保证生成摘要视频。
收尾要求已有采集帧和有效媒体，主机必须装有 FFmpeg 和 ffprobe。
配置 `MG400_FURNACE_ARCHIVE_DIR` 后，收尾会验证归档副本，并保留本地文件。
归档失败时，本地文件仍可用，服务不报告收尾成功。

Stop、释放控制权、租约到期和关闭都会立即撤销录像权限。电机停止指令不等待编码器。
可选参数 `wait_s` 在同一个 `0..15` 秒限时内观察电机和录像的清理结果。
`pending` 表示已接受的清理尚未结束，`unconfirmed` 表示清理失败。
已接受的录像工作结束前，服务保持忙碌。开始新工作前，检查停止结果。
编译器和归档回调在报告成功前检查取消状态。权限撤销后，工作不能恢复或自动重试。

编排技能 `microscope.record(chip_id, seconds, run_name='', mode='time_lapse')` 使用同一受控命令流程。
使用有限的正数时长，并为步骤设置足够完成收尾的截止时间。
技能在派发前保存有大小限制的 JSON 意图记录，只在收到精确的收尾回执后保存完成清单。
清单关联芯片、运行、步骤、录像 ID 和媒体文件名。媒体和逐帧 CSV 数据保留在录像目录中。
芯片档案可下载清单，但不跟随其中的文件名，也不通过运行存储提供视频。
取消或结果未知时，意图记录保留，不生成虚假的完成附件。

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
| `MG400_MICROSCOPE_SIDE_STREAM` | 侧视相机对应的已配置 go2rtc 流。默认不选择任何流。 |
| `MG400_CAMERA_MODES` | 可选的逗号分隔 `WIDTHxHEIGHT@FPS` 列表，每个已配置设备对应 1 个值。 |
| `MG400_CAMERA_CAPTURE_SIZES` | 旧版逗号分隔 `WIDTHxHEIGHT` 列表，每个已配置设备对应 1 个值。 |

启用采集前，确认侧视相机设备路径对应的流。
模式列表和尺寸列表只能配置其一，同时配置时安装程序拒绝执行。
显式配置的值决定各个流。两个列表都为空时，选定的侧视流使用 `3840x2160@30`。
其他相机流保留 `640x480@30`。大尺寸流按需启动。
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
