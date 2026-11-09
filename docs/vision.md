[English](vision.en.md)

# 视觉服务

YOLO 从摄像头图像中检测无标签芯片和机械臂。
芯片身份由槽位和运行记录确定。QR 和 Micro QR 读取器保留用于晶圆侧标签。
相机校准使用机械臂和托盘上的 ArUco 标记，与芯片检测分开。
go2rtc 管理摄像头。视觉服务读取其 JPEG 接口，不打开摄像头设备。
只有启动服务或收到请求时，服务才加载模型或执行推理。

## 运行环境与启动

真实模式使用 `vision`、`vision-gpu` 依赖组和经过校验的本地模型权重。
运行环境见 [视觉运行环境与 GPU 使用](adr/0006-vision-runtime.md)。
服务不会在预测期间下载模型。
加载 Ultralytics 前，服务设置 `YOLO_OFFLINE=1`。此后，无论其 `settings.json` 如何设置，Ultralytics 都不发送使用统计或错误报告，也不检查软件包更新。
部署另见 [主机部署](deploy.md)。

在仓库根目录启动离线演示：

```sh
uv sync
uv run python -m vision --demo
```

演示模式读取 `tests/fixtures/vision/scene.ppm` 和 `tests/fixtures/vision/detections.json`。
4 个演示摄像头编号使用相同的图像和检测结果。
演示使用真实 QR 读取器和模拟定位器，不导入 torch，也不访问摄像头。
演示结果不能验证真实模型或摄像头几何关系。
演示摄像头没有稳定端口路径。`calibrate_camera`、`check_calibration` 和 `observe_slots` 返回 400，`read_markers` 返回 `camera_device=null`。
演示服务启动后即报告 `connected=true`。

启动时，服务加载模型，并用合成图像执行 1 次预热预测。
预热不请求摄像头图像。
GPU 内存配置在模型加载前执行。
随后服务启动租约轮询线程，即使没有 HTTP 请求也检查过期状态。
导入模块或创建服务对象不会启动线程、加载模型或读取图像。
启动失败或取消后，该服务对象关闭。再次启动前必须创建新的服务对象。

## 配置

命令行选项覆盖环境变量。环境变量为空时使用默认值。

| 配置项 | 选项 | 默认值与含义 |
| --- | --- | --- |
| `MG400_VISION_BIND` | `--bind` | `127.0.0.1` |
| `MG400_VISION_PORT` | `--port` | `8771`，范围为 `1` 到 `65535` |
| `MG400_VISION_BASE_PATH` | `--base-path` | `/`，所有路由的前缀 |
| `MG400_VISION_WEIGHTS` | `--weights` | `/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt` |
| `MG400_VISION_DEVICE` | `--device` | CUDA 可用时为 `cuda`，否则为 `cpu`。显式 `cuda:N` 选择设备 |
| `MG400_VISION_GPU_FRACTION` | `--gpu-fraction` | `0.15`，必须有限、大于 0 且不大于 1 |
| `MG400_VISION_FRAME_TIMEOUT` | `--frame-timeout` | 每次 HTTP 套接字操作和响应正文读取的时限均为 `2` 秒。必须有限且大于 0 |
| 无 | `--demo` | 使用离线数据，不加载检测运行库 |
| `MG400_VISION_CALIBRATION_DIR` | 无 | `/var/lib/mg400/calibration/cameras`，相机模型目录 |
| `MG400_VISION_TAG_HEIGHT_MM` | 无 | `100`，机械臂标记高于法兰的名义高度，单位为 mm。必须大于 0 且不大于 1000 |
| `MG400_VISION_CALIBRATION_RMS_MM` | 无 | `1`，校准验收阈值，单位为 mm RMS。必须大于 0 且不大于 1 |
| `MG400_VISION_CACHE_DIR` | 无 | `/var/lib/mg400/vision/yolo`，用作 Ultralytics 的 `YOLO_CONFIG_DIR` |

标记到指尖的名义距离等于名义标记高度加 `tool_dz_mm`。
服务不检查令牌、`Host` 或 `Origin`。VPN 是访问边界。
配置或启动失败时，退出码为 2。

使用 CUDA 时，服务先调用 `torch.cuda.set_per_process_memory_fraction`，再加载定位器。
该比例限制本进程的 PyTorch 分配量。其他 CUDA 分配仍可能使用额外内存。
显式请求的 CUDA 设备不可用时，启动失败。
服务不会把显式 CUDA 请求自动改为 CPU。

## 图像来源与坐标

真实模式的摄像头编号为 1 到已配置设备数量的整数，最多为 4。
服务与安装器、预览使用相同的 `MG400_CAMERA_DEVICES` 和 `MG400_CAMERA_MODES` 配置。
允许的模式和默认值见[摄像头](olares.md#摄像头)。设备配置为空时，关闭真实图像命令。
命令参数说明只列出已配置的摄像头编号。服务在请求图像前拒绝未配置的编号。
真实模式使用以下固定地址，把 `N` 替换为摄像头编号：

```text
http://127.0.0.1:1984/api/frame.jpeg?src=cameraN
```

图像来源不使用环境代理，也不跟随重定向。
每次读取前后，服务都检查响应正文的累计读取时间。
已开始的套接字读取可持续到该次操作超时，随后服务才再次检查正文读取时限。
连接、响应头和 HTTP 传输格式读取使用套接字超时，不设单独的总时限。
每张图像最多 8 MiB、16,000,000 像素。
解码器检查 JPEG 数据，应用 EXIF 方向，再输出 RGB 像素。
输出尺寸必须与配置的采集模式一致。不一致时失败，不调整图像尺寸。
`vision.frames.still(camera)` 和服务读取都从同一 go2rtc 流请求 1 帧。
它们不改变采集模式，也不另行打开设备采集。
读取失败返回 503。请求不自动重试。

结果的 `coordinate_system` 为 `oriented_original_image_pixels`。
检测字段 `xyxy`、`center_xy`、`polygon` 和 `size_px` 使用该图像的坐标。
`angle_deg` 遵循定位器的 OpenCV 矩形角度约定。
QR 结果的 `corners_px`、`center_px` 和 `bounds_px` 使用完整图像坐标。
无法解码的标签标为 `decoded=false`，其文本不是经过验证的身份。

相机模型有效时，检测结果和 QR 标签在其中心提供 `robot_mm`，包含 `x` 和 `y`。
服务先消除像素畸变，再求射线与已存水平托盘平面的交点。
`calibrated_cameras` 列出已加载有效模型的相机。缺少校准时，返回 `calibrated=false`，不提供 `robot_mm`。
相机端口或图像尺寸改变会使校准失效。服务不会复用其他端口的文件，也不会缩放模型。
图像尺寸不匹配时，操作失败，不发布机器人坐标。
机器人运动目标仍必须具备适用平面上的有效校准，并通过常规运动检查。

## 路由与命令

路由相对于 `--base-path`，遵循 [服务契约](adr/0001-instrument-service-contract.md)。
同一时刻只运行 1 个操作，包括检测、QR 读取、标记读取、校准、漂移检查和槽位观测。
重叠请求返回 409，服务不排队执行额外操作。

| 读取路由 | 结果 |
| --- | --- |
| `GET api/detect?camera=N` | 新图像的检测 JSON |
| `GET api/labels?camera=N` | 新完整图像的 QR 和 Micro QR 结果 JSON |
| `GET api/annotated.jpg?camera=N` | 新检测结果的标注 JPEG |
| `GET api/state` | 模型、设备、连接、忙状态、故障、租约和最近结果摘要 |
| `GET api/describe` | 契约版本和命令参数说明 |
| `GET api/events?since=0` | 服务事件，同时写入日志 |

读取路由不要求租约，也不检查请求头。
3 个推理路由必须且只能包含 1 个 `camera` 查询参数，不接受其他查询参数。
响应使用 `Cache-Control: no-store`。
每个请求正文都不得超过 16 KiB。

命令必须带 `Content-Type: application/json` 和 `X-MG400-Client`。
先通过 `POST api/heartbeat` 获取控制租约，再发送命令。
租约有效期为 3 秒。命令运行期间，客户端必须通过 `POST api/renew` 续期。
用 `POST api/release` 释放租约。持有者释放租约时，服务取消其正在运行的命令。
向 `POST api/command` 发送命令：

```json
{"action":"detect","camera":1}
```

```json
{"action":"read_labels","camera":1,"region":[10,20,80,60]}
```

`region` 可省略。它是整数像素边界 `[x0,y0,x1,y1]`，不含右边界和下边界。
边界必须递增、非负，并位于图像内。
返回的标签坐标包含裁剪偏移，仍使用完整图像坐标。

校准使用 3 个要求租约的命令：

| 命令 | 参数与结果 |
| --- | --- |
| `read_markers` | `camera`。返回含 `id`、`center_px` 和 `corners_px` 的 `markers`，以及 `camera`、`calibrated=false`、`coordinate_system`、`image_size`、`camera_device`、`frame_sha256` 和 `read_at`。没有稳定端口路径时，`camera_device` 为 `null`。 |
| `calibrate_camera` | `camera`、`tag_points`、`tag_pixels`、`tray_points`、`tray_pixels`、`image_size`、`scan_r_deg` 和 `tool_dz_mm`。在持有租约时拟合并保存模型。返回 `camera`、`calibrated=true` 和已保存的模型 `calibration`。 |
| `check_calibration` | `camera`、`flange_mm` 和可选 `limit_px`。返回同一图像的标记 4 检查。 |

`tag_points` 包含 24 行未经修正的法兰 XYZ，`tag_pixels` 包含对应的 24 行像素 XY。
拟合程序为这些网格点加上名义标记高度。
`scan_r_deg` 是全部 24 个网格点的法兰 R，单位为度。
`tool_dz_mm` 是工具文件中的实测工具 Z 偏移，单位为毫米。名义标记高度加 `tool_dz_mm` 必须大于 0 且不超过 1000 mm。
`tray_points` 包含 2 到 4 行修正后的指尖 XY 和原始接触法兰 Z。
技能按各参考点记录的 R 旋转配置工具的 XY 偏移，再加到参考法兰 XY。
它保留法兰 Z，用于拟合未知距离，不应用工具 Z 偏移。
`tray_pixels` 包含对应的像素 XY。
`image_size` 为 `[width, height]`。
使用下述受保护的校准技能采集这些数组。不得把历史接触数据转换为机器人移动命令。

命令完成前返回 202 和 `command_id`。
读取 `state.last_command` 中的 `result`、`message` 和 `finished_at`。
成功命令在 `last_command.output` 中提供完整 JSON 结果。
结果必须是有限的 JSON 值，编码为紧凑 ASCII JSON 后不超过 256 KiB。
无效或过大的结果返回 503，且不发布。
失败或停止的命令为 `output=null`。
`last_result` 保存最近一次成功操作的 `camera`、`action`、`count` 和 `finished_at`。
真实模式在首次成功处理图像前报告 `connected=false`。
任何失败的操作都设置 `fault`，下次成功的操作清除它。只有返回 503 的失败才把 `connected` 设为 `false`。
`fault` 不为空时，校准技能和漂移检查拒绝执行。此时先完成 1 次成功读取，例如 `GET api/detect`。

## 相机到机器人校准

移动相机、更换 USB 端口、改变采集尺寸或调整机械臂标记、托盘几何关系后，重新校准。
校准前，操作员必须准备可信参考数据和示教站位。
部署或实机扫描前，先审核变更并完成适用的硬件检查。
软件检查和演练不能验证物理避让空间或真实相机几何关系。

### 准备参考数据与安全入场

机械臂标记位于 J4 轴线上，使用 `DICT_4X4_50` 的 ID `4`。
托盘参考标记使用同一字典，从 `0`、`1`、`2`、`7` 中选取不重复 ID。
在私有 TOML 文件中配置 2 到 4 个实测托盘参考点。
编排器读取 `/var/lib/mg400/calibration/cameras/references.toml`。设置 `MG400_CAMERA_CALIBRATION_REFERENCES` 时，改读该路径。
文件必须可信、没有链接，且不超过 4 KiB。

以下是模板，不是实测坐标。用操作员记录的法兰数值替换 `X`、`Y`、`Z` 和 `R`：

```text
[[references]]
id = 1
flange_mm = [X, Y, Z, R]
```

XYZ 的单位为毫米，R 的单位为度。
`references.toml` 中的参考接触位置始终属于校准数据，即使某个位置超出当前自动运动限位。
例程绝不会移动到这些接触姿态。

示教 2 个网格角点站位，X 和 Y 均不同，R 相同，并归属同一个明确区域。
把该区域的控制者配置为 `robot`。
站位描述指尖位置。技能通过现有工具定义把它们转换为法兰姿态。
选择相差至少 40 mm 的 2 个可达法兰高度。
现有工作空间、关节、路径、反馈和控制检查仍然生效。

扫描前，通过批准的人工或示教路线到达低法兰高度的第一个角点。
机器人必须已使能、空闲，具备新鲜反馈，且由当前运行控制，并位于该姿态。
校准技能不会使能机器人，不会从其他姿态入场，也不会自动恢复已停止的运行。

扫描要求视觉服务报告 `connected=true`。真实服务启动后先报告 `connected=false`。
所选相机的一次成功帧操作（例如 `GET api/detect?camera=N`）会把状态设为 `connected=true`。
视觉状态不明时，扫描会拒绝执行。它不会暗中发起预览请求，也不会靠移动来确认状态。

### 执行受保护的扫描

类型化配方技能接受标量参数：

```text
vision.calibrate(camera: int, corner_a: str, corner_b: str,
                 low_flange_z_mm: float, high_flange_z_mm: float)
```

`camera` 选择已配置的相机，`corner_a` 和 `corner_b` 指定示教站位名称。
例程在两个高度分别采集 3 行、4 列网格，共 24 个标记点。
每个点都保持静止监测，服务按顺序读取 5 帧 ArUco 图像。
静止监测要求法兰 XYZ 与预期姿态相差不超过 0.1 mm，R 相差不超过 0.1 度。
例程使用标记中心的中位数。所需标记缺失或不唯一时，中止扫描。

同一点的各次采样必须具有不同的 RGB SHA256。每点的第 1 帧也不得与上一点的最后 1 帧相同。
这会拒绝完全相同的图像内容，但不能证明传感器图像新鲜。
go2rtc 不提供采集时间戳或传感器帧 ID。`read_at` 是服务读取时间。
整个采集期间，相机路径和图像尺寸必须保持不变。

完成网格后，技能在机器人静止时读取可见托盘标记。
它只提交 1 次采集点，并且只接受当前控制者对应的准确命令回执。
失去控制、取消或结果不明时，不重试，也不重新获取控制权。
运行把已接受的校准保存为产物。

例程在校准区域内结束，完成不代表离场。
区域占用保留，直到后续显式离场或受监督恢复确认区域已清空。

### 离线校准演练

公开演练 CLI `labctl rehearse`（见[编排器](orchestrator.md)）和演示注册表使用同一演练池拥有的合成机器人与视觉服务。
合成相机跟随该机器人，为相机 `1` 生成 `1920x1080` 的 ArUco 图像。
它们自行生成合成参考数据，不读取主机的参考文件或相机模型文件。
演练不打开相机设备，也不启动原生采集。

使用合成工具偏移 `[0, 0, 146]`，按法兰坐标准备网格：

| 参数 | 合成值 |
| --- | --- |
| X 范围 | 200 到 260 mm |
| Y 范围 | 0 到 40 mm |
| 低位与高位 Z | 50 和 90 mm |
| R | 0 度 |

使用该工具偏移定义对应的指尖角点站位，并归属同一个明确由 `robot` 控制的区域。
扫描前，前序配方步骤必须使能合成机器人，并到达低位的第一个角点。
校准技能不会暗中使能机器人，也不会获取额外控制权。
演练成功只验证软件流程，不能证明硬件可达性、实测相机几何关系或物理避让空间。

### 拟合、验收与限制

拟合先采用归一化直接线性变换（DLT）和 RANSAC，阈值为 2 像素。
18 个训练标记点中，至少 12 个必须为内点。
程序使用 `cv2.calibrateCamera`、`CALIB_USE_INTRINSIC_GUESS`，仅拟合径向 `k1` 和 `k2`。
其余 6 个标记点始终不参与拟合。

联合拟合还使用托盘参考点，恢复标记到指尖的垂直距离 `D`。
其名义值等于名义标记高度加 `tool_dz_mm`，固定允许偏差为 10 mm。
更换工具后，先写入实测偏移，再校准。
标记高于法兰的名义高度默认为 100 mm。
拟合报告 `distance_mm`，并分别报告 `rms_mm.held_out` 和 `rms_mm.tray`。

RMS 为水平位置误差的均方根值。
保留点 RMS 使用未参与拟合的标记点。托盘 RMS 是参与拟合的参考点残差，不是独立验证结果。
每个误差都使用该点自身的高度。两个 RMS 都必须满足配置阈值，且不得超过 1 mm。
映射平面采用模型坐标下恢复的托盘平均高度。
`tray_height_mm` 取决于名义标记高度，不是独立实测的物理机器人 Z。
绝不能把它作为机器人运动高度。服务只返回 XY。
倾斜托盘仍有限制：此模型使用水平平面，不对倾斜建模。
通过 RMS 检查不会消除该限制，也不能确定安全运动路径。

### 模型保存与失效

服务在 `/var/lib/mg400/calibration/cameras/` 下保存有大小限制的 JSON。
文件名为完整 `/dev/v4l/by-path/` 设备路径的 SHA256，加上 `.json`。
文档也保存该完整路径和图像尺寸。
它保留相机矩阵、畸变、旋转、平移、托盘高度、恢复距离、扫描 R、采样点、划分索引、误差和创建时间。
模型点以 `tray_tip_xy_flange_z_mm` 保存修正后的托盘 XY 和原始法兰 Z。
运行产物在 `reference_provenance` 中保存原始参考法兰姿态、标记 ID 和配置工具偏移。
校准 JSON 不包含这些原始参考来源信息。

新模型使用 `schema` 2，并用 `scan_r_deg` 保存扫描 R。
坐标映射也接受没有 `scan_r_deg` 的 `schema` 1 模型。漂移检查拒绝 `schema` 1 模型。
使用漂移检查前，重新校准相机。

安装器创建该目录，属主为 `1000:1000`，权限为 `0700`，见[主机部署](deploy.md)。
目录必须归服务账户所有，权限为 `0700` 或 `0750`。
模型必须是权限为 `0600` 或 `0640` 的私有普通文件，不得带符号链接或多个硬链接。
新模型文件使用 `0600`，在最终权限检查后原子替换。每个文档最多 64 KiB。
停止、释放租约或租约过期都会拒绝后续发布。

新 USB 端口路径会选取不同的校准键，图像尺寸改变也会阻止坐标使用。
不得重命名旧文件来绕过检查。使用预期相机及采集模式重新校准。
服务仅凭端口名不能发现所有物理相机移动。
保持相机固定，发生物理变化后重新验证。

## 校准漂移检查

视觉引导技能 `vision.slot_state`、`vision.find_chip`、`vision.pick_chip` 和 `vision.tray_transfer` 在观察托盘前自行执行此检查。单独检查时，调用 `vision.check_calibration(camera)`。
此类型化技能要求机器人和视觉服务保持当前运行的控制权。
它读取实测机器人姿态，不移动或使能机器人。
检查保留现有工作空间、吸盘、反馈和控制权要求。

视觉服务必须已经报告 `connected=true`，且没有故障。
真实服务冷启动时，先按上文完成 1 次成功帧操作。
漂移检查不会另行获取预览租约，也不会自动恢复服务健康状态。

检查时，机器人必须静止在校准网格内的姿态：

- 法兰 X 和 Y 位于 2 个网格角点之间。
- 法兰 Z 位于 2 个扫描高度之间。
- R 与扫描 R 相差不超过 0.1 度。相差 360 度整数倍的 R 视为相同。

服务拒绝其他姿态。保留点只验证网格内的模型。
机械臂标记不完全位于 J4 轴线上时，会随 R 移动。

### 限值与命令数据

像素限值默认为 `3`。设置 `MG400_ORCH_DRIFT_LIMIT_PX` 可降低技能使用的限值，取值必须大于 `0` 且不超过 `3`。技能把该值作为 `limit_px` 发送。

技能通过租约发送 `check_calibration` 命令，字段如下：

| 字段 | 含义 |
| --- | --- |
| `camera` | 已配置的相机编号。 |
| `flange_mm` | 实测 `[X,Y,Z,R]`。XYZ 单位为毫米，R 单位为度。 |
| `limit_px` | 可选像素限值，大于 `0` 且不超过 `3`。命令默认值为 `3`。 |

服务读取 1 张当前图像，加载该相机路径的 1 个模型，并在该图像中检测 1 次 `DICT_4X4_50` 标记。
预期中心和检测中心使用同一模型和图像。服务不运行芯片检测器，不复用缓存结果，也不重写校准。
技能不另行读取主机模型。

### 几何与结果

标记 4 的预期中心取法兰 X 和 Y，Z 取法兰 Z 加 `nominal_tag_height_mm`。校准拟合使用同一标记中心。
服务用存储模型的相机矩阵、畸变、旋转和平移投影该中心。
投影中心在相机坐标系中的深度必须为正，且像素必须位于校准图像内。
真实相机路径和图像尺寸必须匹配存储模型。
缺少模型、图像尺寸改变或使用 `schema` 1 模型时，命令失败，消息以 `camera moved, recalibrate:` 开头。

图像中必须恰好有 1 个标记 4。缺少标记 4 时，检查失败。图像中有重复的标记 ID 时，命令失败。
检测中心到预期中心的距离不超过限值时，检查才通过。

成功命令回执包含 `camera`、`calibrated=true`，以及下列检查字段：

| 字段 | 含义 |
| --- | --- |
| `passed`、`reason` | 布尔结果及失败原因，没有适用原因时为 `null`。 |
| `detected_center_px` | 检测到的标记 4 中心 `[x,y]`，不可用时为 `null`。 |
| `expected_center_px` | 投影得到的标记 4 中心 `[x,y]`，不可用时为 `null`。 |
| `distance_px` | 2 个中心之间的像素距离，不可用时为 `null`。 |
| `limit_px` | 应用的像素限值。 |
| `camera_device`、`image_size` | 此图像和模型的相机路径及 `{width,height}`。 |
| `model_created_at` | 此次检查所用模型的创建时间。 |

`reason` 取以下值之一：`expected_behind_camera`、`expected_outside_image`、`marker_missing`、`marker_beyond_limit` 或 `marker_detection_malformed`。

只有成功命令回执还不够。技能必须取得有效的通过结果。
技能用回执中的 2 个中心重新计算距离。该距离必须与 `distance_px` 一致，且不超过限值。
它只接受命令、动作和控制客户端完全匹配的回执。
读取标记前、读取中和读取后，守卫检查法兰 XYZ 与 R 静止（变化不超过 0.1 mm 和 0.1 度），且机器人控制者和实例不变。

检查结果未通过或无效时，抛出 `camera moved, recalibrate`。运行在下一步之前进入 `held` 状态。
命令失败时，抛出 `Vision calibration failed:` 和服务消息。运行同样进入 `held` 状态。
较早发生的停止、租约丢失或机器人移动错误保留原始原因。
Runner 停止并等待已接受的工作结束，同时保留占用区域。
技能不重试，不重新取得权限，不清空区域，也不自动重新校准。

1 个标记只提供 1 个图像点。某些相机移动使其他图像点的位移大于标记点。
例如，标记靠近图像中心时，相机绕镜头光轴旋转。
通过检查不能发现所有相机移动。它不替代物理避让、关节与工作空间限位，或适用的硬件检查。

## 托盘几何与交叉检查

`vision.tray` 使用传入的 RGB 图像和有效相机模型定位托盘。
它不采集图像，不发送机器人命令，也不确定芯片身份。
`Grooved Platform V2` 使用 [grooved-platform-v2.toml](../config/trays/grooved-platform-v2.toml)。
`load_tray` 读取有大小上限的 TOML 文件。`validate_tray` 检查内存中的配置。
此库接收传入数据。持有租约的 `observe_slots` 命令把它与当前帧和检测器证据结合。

### 受保护的槽位证据

通过现有 `POST api/command` 路由发送 `observe_slots`，保留常规客户端和租约检查。
命令使用以下字段：

| 字段 | 要求 |
| --- | --- |
| `camera` | 已配置的相机编号。该相机必须有稳定端口路径和已安装的校准模型。 |
| `tray` | 通过校验的托盘模型文档，不是主机文件路径。 |
| `targets` | 请求的槽位 ID，以及各槽位准确的示教机器人 XY 对。值为 `null` 时，该槽位没有示教工位，只检查芯片。 |
| `axis` | 按显式吸取约定选择 `long` 或 `short`。 |

完整 JSON 命令不得超过 16 KiB。托盘文件有效，不代表完整命令符合大小限制。
每次观测使用 1 个当前帧和绑定该相机的校准模型。
服务定位托盘，并把该帧中的检测器证据映射到机器人 XY。
空槽位结果必须有成功推理，以及有效的托盘、目标和校准证据。
数据缺失或芯片归属含糊，不能证明槽位为空。
请求的整条槽道必须可见。机械臂遮挡或芯片掩模截断时，无法得到确证。
请求槽道的芯片证据不得与另一条已配置槽道重叠。
掩模边长之差不超过长边的 1% 时，轴方向含糊。

输出包含 `camera`、`camera_device`、`calibrated`、`image_size`、`model_created_at`、`frame_sha256`、`read_at` 和 `axis`。
每个槽位报告 `state`、`center_xy_mm`、`angle_deg` 和 `reason`。位置和角度可以为空。
角度描述机器人 XY 中选定的芯片轴。它不是原始图像角度，也不是法兰 R 命令。
停止、租约丢失、元数据无效或发布权限撤销时，不能发布成功结果。

`vision.find_chip(slot)` 和 `vision.slot_state(slot)` 技能要求新鲜、已持有控制权的静止机器人状态，并执行校准漂移检查。
组合技能 `vision.tray_transfer` 还要求实测吸取约定和准确的示教槽位目标。
吸盘技能 `vision.pick_chip(slot)` 不要求槽位工位，见[吸盘取芯片](orchestrator.md#吸盘取芯片)。
它把观测结果保留在受保护步骤内，并检查各次观测的相机、图像尺寸和校准身份。
配置和监督流程见[视觉引导的托盘转移](orchestrator.md#视觉引导的托盘转移)。

### 有界槽道几何

托盘有 32 条命名槽道，从 `groove_00` 到 `groove_31`。
原点位于 ID `1` 标记旁的板角。标记中心具有下表所列的偏移。
全部配置坐标采用托盘坐标系，单位为毫米。

| 标记 ID | 中心 XY |
| --- | --- |
| `1` | `(15.15, 15.0)` |
| `0` | `(162.65, 15.0)` |
| `7` | `(15.15, 162.8)` |
| `2` | `(162.65, 162.8)` |

槽道索引为 `k = 0..31`，中心线位于 `x = 5.975 + 5.35 * k`。
槽道 `k = 5..26` 从 `y = 0.0` 延伸到 `y = 177.8`。
其余槽道从 `y = 30.0` 延伸到 `y = 147.8`。
每个 `centerline_mm` 都是有界线段。`reference_mm` 为名义中点，仅描述几何关系。
芯片可以位于槽道内任意有效位置。实际 Y 必须来自视觉观察。
不得用中点代替芯片观察，也不得假定中点是示教运动目标。
这些芯片没有标签区域或 QR 标签。

### 定位托盘

提供当前 RGB 图像，并通过同一相机模型映射该图像中的芯片中心。
Python 接口接受传入的数据：

```text
locate_tray(image, calibration, tray, *, device, tolerance_mm=None)
cross_check_slots(location, *, taught_stations, chips,
                  same_location_slots=(), tolerance_mm=None)
```

`device` 必须匹配校准中的完整稳定相机路径。图像尺寸也必须匹配。
读取器使用 `DICT_4X4_50` ArUco 标记，要求至少 3 个不重复的可见角点 ID。
帧中任一 `DICT_4X4_50` ID 重复时，在拟合前拒绝该帧。非角点 ID 重复时也拒绝。
不要让任何 ID 的重复标记（例如转盘标记和备用打印件）进入托盘图像。
机械臂标记 ID `4` 不属于托盘角点。

函数先通过相机模型映射实测中心，再按配置的托盘几何拟合相似变换。
任一角点残差超过 `tolerance_mm` 时，定位失败。
它返回变换、实测标记残差，以及采用 `robot_xy_mm` 坐标的 `slots`。
每个槽位包含 `centerline_xy_mm`、`reference_xy_mm`，以及变换后的 `width_mm`。
输出只提供 XY 几何，不确定物理 Z 或安全机器人路径。
这里仍采用校准的水平面假设。托盘倾斜和芯片高于该平面都可能影响映射后的 XY。

只观察到 3 个角点时，`missing_marker` 报告预测的第 4 个标记位置处的单元采样结果。
此结果仅供诊断，不增加实测标记，不替代缺失证据，也不决定 3 标记拟合是否通过。
全部 4 个角点可见时，`missing_marker` 为 `null`。
诊断报告 `match`、`mismatch` 或 `unavailable`，且 `used_for_pose=false`。
所提供托盘采用 24 mm 的黑色标记方块。采样假定其边缘平行于托盘坐标轴。
比较仅测试 90 度整数倍旋转，不测试镜像候选图案。
分辨率低、遮挡或倾斜可能使比较无法得出明确结果。

### 检查工位与芯片证据

调用 `cross_check_slots` 时，按槽位 ID 显式提供证据。
`taught_stations` 把每个槽位映射到示教机器人 XY 坐标对。值为 `None` 时，只检查芯片。
`chips` 为每个槽位提供下列一种观察：

| 观察 | 含义 |
| --- | --- |
| `{"state":"present","center_xy_mm":[x,y]}` | 观察到的芯片中心，采用机器人 XY 坐标，单位为毫米。 |
| `{"state":"empty"}` | 调用者明确确认槽位为空。 |
| `{"state":"unknown"}` | 调用者没有明确的芯片观察。 |

省略的芯片条目表示未知。缺少检测数据绝不代表空槽位。
每条槽道都必须有 `taught_stations` 条目（XY 坐标对或 `None`）和明确芯片证据，检查才完整。
未知槽位 ID 或无效证据会拒绝请求。

检查分别计算示教工位和观察到的芯片到有界槽道线段的距离。
残差包含垂直距离和超出任一端点的距离。
`tolerance_mm` 必须为有限值，大于 0 且不超过 2 mm。所提供 TOML 采用 2 mm。
`tolerance_mm=None` 时，`locate_tray` 继承托盘配置，`cross_check_slots` 继承定位结果中的配置。
残差等于容差时通过，大于容差时失败。

两个位置都存在时，`station_chip_distance_mm` 报告完整 XY 距离。
槽道内不同的有效 Y 位置不表示托盘漂移。
仅当示教工位指向同一芯片实际位置时，才把该槽位加入 `same_location_slots`。
这些槽位的完整 XY 距离也必须满足容差，且两个位置都必须存在。

每个槽位的 `status` 为 `ok`、`incomplete` 或 `failed`，并提供 `missing` 和 `failures` 详情。
只有全部槽位通过时，结果的 `ok` 才为真。
检查不完整或失败时，抛出 `TrayCheckError`。通过 `TrayCheckError.report` 查看完整的逐槽位报告。
不得把不完整证据视为运动许可。常规机器人限位、避让检查、租约和区域规则仍然适用。

## 停止与退出

任何客户端都可调用 `POST api/stop`，不必持有租约。
请求必须带 `X-MG400-Client` 和 JSON 正文，例如 `{"wait_s":0}`。
`wait_s` 取 0 到 15 秒。
工作完成后返回 `confirmed`，仍在运行时返回 `pending`。
停止请求可重复发送。

停止请求取消当前操作，并丢弃延迟完成的结果。
启动期间，停止会取消模型发布，并在加载或预热结束前返回 `pending`。
QR 处理在各阶段之间检查取消状态。
已开始的模型预测不能强制中断。只有它返回后，停止才能得到确认。
租约过期或持有者释放租约会取消控制客户端的命令，不会取消独立的读取请求。

SIGINT、SIGTERM 和 SIGHUP 会在退出时请求取消。
退出最多等待当前推理或启动 15 秒，并结束租约轮询。
服务不控制机器人、位移台、吸盘或摄像头设备。

## 检测模型评估

`scripts/vision_eval.py` 用带标签的数据集评估芯片检测。它不训练模型，也不读取摄像头。
数据集和权重不得放入 Git。
H-22 评估集位于 Olares Files 的 `Home/Datasets/h22-tray-eval-v1/`。它的托盘比例为 1.554 px/mm。

数据集目录包含 `images/` 和 `labels/`。标签文件与图像同名，格式为 YOLO 多边形。
类别 `0` 是芯片。其他类别只用于给帧分组。

在 Linux 上，从仓库根目录运行：

```bash
uv run --group vision-gpu python scripts/vision_eval.py ~/datasets/h22-tray-eval-v1 --weights chip-arm-yolo26s-seg-v1.pt --px-per-mm 1.554 --device cuda
```

锁定的 `torch` 没有 macOS 版本。在 macOS 上，在另一个环境中安装锁定版本的 `ultralytics`。运行脚本前，设置 `PYTHONPATH=src`。

评估规则：

- 脚本把每帧交给 `Localizer.predict`，与视觉服务相同。该函数先把宽于 4:3 的图像裁成居中的 4:3 区域（1280x720 裁成 960x720），再拉伸到 640x640。检测坐标仍按完整原图像素报告。
- 脚本只读取 `images/` 中的 `.jpg`、`.jpeg` 和 `.png` 文件。
- `--weights` 默认读取 `MG400_VISION_WEIGHTS`。`--device` 默认为 `cpu`。使用 GPU 时，设置 `--device cuda`。
- 脚本使用 `Localizer.predict` 的默认置信度阈值 `0.39`，与服务相同。如果要评估其他阈值，设置 `--confidence`。
- 检测框与标签框的 IoU（交并比）不小于 0.5 时，二者匹配。脚本按置信度从高到低匹配。每个标签最多匹配 1 个检测。
- 未匹配的检测计为误检。未匹配的标签计为漏检。
- 精确率等于匹配数除以检测数。召回率等于匹配数除以标签数。
- 中心误差是匹配框的中心距离。`--px-per-mm` 把像素换算为毫米。

输出是 Markdown 报告。报告列出各组帧的计数、芯片精确率和召回率、中心误差，以及有误检或漏检的帧。
