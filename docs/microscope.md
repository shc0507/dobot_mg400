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

## 主机托管服务

在 `/etc/mg400/mg400.env` 的 `MG400_OPTIONAL_SERVICES` 中加入 `microscope`。保留其他选项。
启动服务前，安装锁定版本的 [Toupcam 库](deploy.md#toupcam-库)，并配置下文参数。
安装器在 `/opt/mg400/.venv` 中准备 `microscope` 依赖组，并安装 USB 访问规则。
它还准备默认的私有数据目录，不启动设备，也不创建校准值。
自定义校准和运行根目录须由操作员准备，并保持私有权限。

单元以 `1000:1000` 运行，具有 `dialout` 和 `video` 访问权限。
它设置 `LD_LIBRARY_PATH=/usr/local/lib/mg400`，默认监听 `127.0.0.1:8770`。
它保留 `MG400_FURNACE_TOKEN`，仅供现有炉控元数据客户端使用。
使用代理前缀时，在 `MG400_MICROSCOPE_FURNACE_URL` 中配置炉控原生前缀。
摄像头预览保留视觉令牌。无关服务排除显微镜令牌。

使用前，停止冲突的相机和电机程序。
先用 `mg400ctl start camera` 启动已配置的侧视相机服务，再运行 `mg400ctl start microscope`。
显微镜单元不会自动启动相机服务。
用 `mg400ctl logs microscope` 检查故障，用 `mg400ctl stop microscope` 停止服务。
单元没有 systemd 停止超时，允许清理完成。清理出错后，检查原因再重启。
日常真机使用前，操作员必须完成 `H-21`。

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
| `MG400_TOUPCAM_SERIAL` | 可选相机序列号。它与索引只能设置一项。 |
| `MG400_TOUPCAM_INDEX` | 可选的非负相机索引。两项选择器都为空时，索引为 `0`。 |
| `MG400_MICROSCOPE_FOCUS_LIMITS` | 必填的实测 `MIN,MAX` 整数脉冲，包含零点，且行程不为零。 |
| `MG400_MICROSCOPE_SWITCH_FOCUS` | 必填的实测避让位置，单位为脉冲，必须在限位内。没有归零开关时，必须为 `0`。 |
| `MG400_MICROSCOPE_HOME_SWITCH` | 必填硬件事实：有已配置的调焦归零开关时为 `1`，没有时为 `0`。 |
| `MG400_MICROSCOPE_FURNACE_URL` | 炉控元数据接口，默认为 `http://127.0.0.1:8769/`。Olares 上的炉控服务绑定 VPN 地址，应设置为 `http://100.64.0.1:8769/`。 |
| `MG400_FURNACE_TOKEN` | 读取元数据所用的现有炉控 API 令牌。显微镜不取得炉控控制权。 |
| `MG400_FURNACE_ARCHIVE_DIR` | 默认为空。操作员挂载归档共享目录后，才能配置绝对路径。 |

真实启动必须使用支持调焦参考状态的固件，并显式配置调焦参数。
通用环境示例的物理调焦参数为空，转盘微调范围为 `0`。
当前显微镜使用以下 `H-06` 配置：

```ini
MG400_MICROSCOPE_SWITCH_FOCUS=0
MG400_MICROSCOPE_HOME_SWITCH=0
MG400_MICROSCOPE_FOCUS_LIMITS=0,10000
MG400_MICROSCOPE_NUDGE_STEPS=20
```

不得把演示限位复制到硬件。
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
当前调焦齿轮可能打滑。脉冲计数相对于上次零点。
每次使用前，重新用 50x 物镜对焦样品，再通过手动面板设零。
此焦点是避让零点。调焦正向运动抬高物镜。

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
当前视频流顺序使用 `MG400_MICROSCOPE_SIDE_STREAM=camera3`。
执行 `H-21` 前，把此相机放回转盘上的原位置。
服务与预览通过 go2rtc 采集该 V4L2 设备。
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
显微镜服务要求侧视流的已配置模式，拒绝尺寸不同的帧。未配置相机时，要求 `3840x2160`。
修改采集尺寸时，必须使用匹配的参考图像。`H-21` 在已配置模式下检查物镜切换。
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

## 受监督的校准与实验

这些命令必须由操作员在交互终端运行。输入和输出都必须是 TTY。
不得从代理、服务契约、编排器、浏览器面板或无人值守脚本运行。
工具不会自动停止其他服务。
启动前，操作员必须释放冲突的显微镜和相机占用。
命令从终端环境读取 `MG400_*` 配置。
使用校准目录和运行目录的所有者账户运行。

把 `MG400_MICROSCOPE_SERIAL` 配置为控制板的稳定路径。
校准和实验的主相机使用 `MG400_TOUPCAM_SERIAL` 或 `MG400_TOUPCAM_INDEX`，不得同时配置。
这些命令的运动还必须具备上表所列的 `H-06` 实测限位、避让位置和归零开关配置。
缺少测量值时，工具拒绝启动。不得用演示值代替。

校准和实验运动前，工具取得主相机、所需侧视相机和独占串口。
每个所需相机都必须先提供有效首帧。
侧视采集通过 `MG400_CAMERA_DEVICES`、`MG400_CAMERA_MODES` 和 `MG400_MICROSCOPE_SIDE_STREAM` 选择物理设备及模式。
旧版 `MG400_CAMERA_CAPTURE_SIZES` 规则仍然适用。
受监督适配器通过 `CAP_V4L2` 直接打开稳定设备路径，并要求 `uvcvideo` 驱动。
它在清理前始终保留同一个 MMAP 采集队列。go2rtc 已占用该队列时，工具拒绝启动。
采集开始后，工具不会修改格式、重新打开设备或重连。
此规则排除同一设备上的竞争采集，不排除无关的纯控制 ioctl。

### 零点与运动规则

这些规则适用于校准和实验。
受监督的配置控制台采用下述独立人工测量规则。

实测限位和避让位置采用 `MICRO 4` 坐标。
当前细分为 `m` 时，下限为 `ceil(MIN * m / 4)`，上限为 `floor(MAX * m / 4)`。
向内取整后必须仍有可用行程。避让位置和请求的绝对位置必须能够精确换算。
工具拒绝不能整除或超出限位的坐标。

没有归零开关时，操作员在避让位置设零。`MG400_MICROSCOPE_SWITCH_FOCUS` 必须为 `0`。
配置了开关时，`FOCUS HOME` 建立归零原点。它不会退回 `FOCUS ZERO`。
实测限位和避让位置必须采用同一个归零原点。
每次调焦移动都必须具备当前零点和实测限位。
每次转盘移动还要求调焦轴静止在避让位置，下述有界校准移动除外。

首次运动必须明确确认零点。
实际改变细分或禁用调焦前，工具先用当前有效零点移动到避让位置。
操作后，工具停止两个电机，并再次要求确认：

1. 观察调焦轴和物镜。
2. 检查调焦轴位于避让位置，且物镜已避开窗口。
3. 在 60 秒内输入 `CLEAR` 并按回车。

每次提示都会丢弃此前的输入，必须重新回答。
确认后，工具执行 `FOCUS ZERO` 或 `FOCUS HOME`，再发送换算后的限位。
其他回答、输入结束、超时、复位、断连或图像失败都会触发两个电机的停止和禁用清理。
命令随后以非零状态结束，不会自动重试或恢复零点。
清理错误会报告失败，不能证明硬件已停止。
取消以及 `SIGINT`、`SIGTERM` 或 `SIGHUP` 采用相同的清理规则。

### 校准命令

以下命令显示选项，不打开设备：

```sh
uv run python -m microscope.calibrate --help
uv run python -m microscope.experiments --help
```

校准命令必须遵守上述监督规则：

| 命令 | 操作与默认输出 |
| --- | --- |
| `python -m microscope.calibrate reference` | 采集原始 PNG，保存为 `objective_reference.png`。不归零、不设零，也不移动调焦轴。 |
| `python -m microscope.calibrate objective` | 执行物镜校准，写入 `objective_calibration.csv`。 |
| `python -m microscope.calibrate light` | 执行轻量物镜校准，写入 `light_objective_calibration.csv`。 |

`--reference` 为物镜或轻量物镜校准选择私有校准 PNG，默认为 `objective_reference.png`。
`--output` 选择 `MG400_MICROSCOPE_CALIBRATION_DIR` 内的文件。
校准可以替换现有的自有私有输出文件。若要保留原结果，选择其他输出名称。
参考图像采集必须通过终端和设备占用检查，但不要求运动测量值或零点确认。
它接受 `[focus]` 中的相机尺寸，并在清理时停止和禁用电机。

`--settings FILE` 读取 TOML 文档。
校准时，`[focus]` 提供调焦配置，`[calibrator]` 提供算法选项，`[scan]` 提供扫描选项。
工具拒绝未知段、未知名称和无效值。
默认值保留原 GUI 配置。支持的名称和默认值见 [calibrate.py](../src/microscope/calibrate.py)。
相机尺寸默认为 `2592x1944`。

### 工作焦点处的校准

`MG400_MICROSCOPE_NUDGE_STEPS` 配置实测转盘范围，单位为整步。
未配置或为 `0` 时，保留上述避让行为。`1` 到 `20` 允许工作焦点处的有界移动。
其他值会拒绝启动。此模式仅适用于物镜和轻量物镜校准。

使用 `H-06` 在靠窗口的调焦限位处测得的值，测量时逐个把物镜置于工作位置。
该范围必须保证每个物镜在两个方向都保留物理余量。
不得从校准数据估算此值，也不得采用其他显微镜的测量值。

范围为正时，两条命令都必须提供 `--focus-start N`，单位为实测 `MICRO 4` 坐标，且必须在调焦限位内：

```sh
python -m microscope.calibrate objective --focus-start N
python -m microscope.calibrate light --focus-start N
```

把 `N` 替换为实测调焦起点坐标。
首次输入 `CLEAR` 前，检查调焦轴在避让位置，且物镜在工作位置。
工具随后移动调焦轴到 `N`，并按 `[focus]` 配置执行一次局部自动调焦。

每次转盘移动都必须具备当前零点、实测限位和静止的调焦轴。
工具从首次确认时的转盘位置起累计指令位移，单位为 `1/16` 整步。
允许位移从负的配置范围到正的配置范围，包含边界。
细分改变和后续 `CLEAR` 不会重置此原点。
若移动将超出范围，工具在发送前停止并禁用两个电机，然后结束命令。

改变细分时，工具仍在零点失效前移动调焦轴到避让位置。
提示会先说明调焦返回位置，再要求新的 `CLEAR` 回答。
设零或归零并发送换算限位后，调焦轴按新细分返回先前位置。
返回位置取最近的整数脉冲，且必须在限位内。
仅在此模式下，校准的调焦禁用请求改用 `STOP FOCUS`，保留零点。
实验和完整物镜切换仍只在避让位置转动转盘。

### 受监督的配置控制台

使用此控制台测量 `H-06` 的避让位置、行程限位和转盘范围：

```sh
python -m microscope.calibrate setup
```

先停止显微镜服务。操作员必须能触及急停，并看清调焦轴和所有物镜。
控制台必须使用交互输入、交互输出和独占串口。
它不要求 `H-06` 数值，也不打开相机。
它不接受 `--settings`、`--output` 或 `--focus-start` 选项，也拒绝自定义 `--reference`。
它不写配置文件，也从不发送 `FOCUS LIMIT`。

控制台使用 `MICRO 4`，调焦速度为 `200` 脉冲/秒，转盘速度为 `40` 脉冲/秒。
每次移动完成后，控制台才接受下个移动按键。
直接按单个键，不按回车。`0` 的确认要求在 60 秒内输入 `CLEAR` 后按回车。
在 300 秒内输入新的按键。此前排队的输入不能授权另一次移动。

| 按键 | 动作 |
| --- | --- |
| `a` / `z` | 调焦 `+1` / `-1` 脉冲。 |
| `s` / `x` | 调焦 `+10` / `-10` 脉冲。 |
| `d` / `c` | 调焦 `+100` / `-100` 脉冲。 |
| `f` / `v` | 调焦 `+1000` / `-1000` 脉冲。 |
| `j` / `l` | 转盘 `-1` / `+1` 整步。 |
| `u` / `o` | 转盘 `-10` / `+10` 整步。 |
| `m` | 把当前转盘计数标为显示原点。 |
| `0` | 要求新输入 `CLEAR`，然后发送 `FOCUS ZERO`。 |
| `q` | 停止并禁用两个电机，然后退出。 |

没有动作的按键会停止两个电机，移动期间也有效。
移动期间，`q` 和输入结束会停止并退出。
控制台丢弃移动期间输入的运动按键、`m` 和 `0`。
输入结束、拒绝、超时、复位或断连会停止并禁用两个电机，然后结束控制台。
控制台在运行中和结束时打印已完成动作的调焦计数及转盘位移。
移动中断后，当前位置可能未知。
这些计数不能测量物理位置，也不能证明已避开窗口。
观察每次移动，并另行记录实测值。控制台不会写入这些值。

### 实验命令

使用 `python -m microscope.experiments focus|switch|combined` 并提供所需配置。
每条命令只执行 1 轮。`--output` 选择 `MG400_ORCH_RUNS_DIR` 内的新私有目录。
未指定时，输出位于 `microscope/experiments/` 下的独立命令目录。
配置、汇总行、调焦曲线、切换日志和可用的最终图像都保留在该目录。
失败时保留部分结果和终止失败行。

通过 `--settings FILE` 使用 `[focus]` 和 `[switch]` 表。
`--ids` 提供按转盘正方向相邻排列的不重复物镜 ID。
实验把该顺序视为有边界的直线，不作环形路线。
`--focus-microstep` 默认为 `1`，接受 `1`、`2`、`4`、`8` 或 `16`。
调焦和组合实验必须提供 `--focus-start`，单位为实测 `MICRO 4` 坐标。
调焦参考试验前和每次组合调焦试验前，工具移动到该明确起点。

调焦试验保留 `-2000`、`-1000`、`1000` 和 `2000` 的偏移，单位为当前细分脉冲。
完整扫描半范围必须至少为 `2000`。
切换试验覆盖两个相邻方向，偏移为 `0`、`-50`、`50`、`-100` 和 `100` 整步。
偏移移动使用 `MICRO 1`，ArUco 切换器使用 `MICRO 4`。
这些改变必须经过上述监督确认。
工具不会缩小扫描范围来适应限位。不符合要求的移动会在发送前中止。

### 导入现有校准数据

操作员检查源数据后，在 Olares 运行导入：

```sh
sudo mg400ctl calibration import microscope DIR
```

把 `DIR` 替换为 Objective-Control `3a2dad0` 的数据目录。
导入器选择 `objective_views/`、`side_camera_images/`、`objective_reference.png` 和根目录的 CSV 文件。
它复制目录内容，拒绝符号链接及不安全的选中文件，并列出每个已复制路径。
目标已存在时拒绝复制，不会覆盖。
目标目录归 `1000:1000` 所有，权限为 `0750`。文件归 `1000:1000` 所有，权限为 `0640`。
后续复制失败时，已完成的副本保留。再次尝试前，先检查这些副本。
导入器不打开设备。操作员必须在 Olares 导入，并在迁移议题中报告结果。

## 其他命令行入口

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
