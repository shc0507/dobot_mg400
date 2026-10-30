[English](vision.en.md)

# 视觉服务

视觉服务从摄像头图像中检测芯片、机械臂、QR 和 Micro QR 标签。
go2rtc 管理摄像头。视觉服务读取其 JPEG 接口，不打开摄像头设备。
只有启动服务或收到请求时，服务才加载模型或执行推理。

## 运行环境与启动

真实模式使用 `vision`、`vision-gpu` 依赖组和经过校验的本地模型权重。
运行环境见 [视觉运行环境与 GPU 使用](adr/0006-vision-runtime.md)。
服务不会在预测期间下载模型。
部署另见 [主机部署](deploy.md)。

在仓库根目录启动离线演示：

```sh
uv sync
uv run python -m vision --demo
```

演示模式读取 `tests/fixtures/vision/scene.ppm` 和 `tests/fixtures/vision/detections.json`。
3 个演示摄像头编号使用相同的图像和检测结果。
演示使用真实 QR 读取器和模拟定位器，不导入 torch，也不访问摄像头。
演示结果不能验证真实模型或摄像头几何关系。

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
| `MG400_VISION_PORT` | `--port` | `8771` |
| `MG400_VISION_BASE_PATH` | `--base-path` | `/`，所有路由的前缀 |
| `MG400_VISION_TOKEN` | 无 | 回环地址可为空，否则至少 24 个可打印 ASCII 字符 |
| `MG400_VISION_WEIGHTS` | `--weights` | `/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt` |
| `MG400_VISION_DEVICE` | `--device` | CUDA 可用时为 `cuda`，否则为 `cpu`。显式 `cuda:N` 选择设备 |
| `MG400_VISION_GPU_FRACTION` | `--gpu-fraction` | `0.15`，必须有限、大于 0 且不大于 1 |
| `MG400_VISION_FRAME_TIMEOUT` | `--frame-timeout` | 每次 HTTP 套接字操作和响应正文读取的时限均为 `2` 秒。必须有限且大于 0 |
| 无 | `--demo` | 使用离线数据，不加载检测运行库 |

配置了密钥时，即使绑定回环地址，也必须至少有 24 个字符。
未配置密钥时，请求的 `Host` 必须是回环地址，且不得含转发请求头。
配置了密钥时，发送 `Authorization: Bearer <token>`。
服务还检查 `Origin` 并拒绝跨站请求。
图像和结果路由与契约路由使用相同的访问检查。
配置或启动失败时，退出码为 2。

使用 CUDA 时，服务先调用 `torch.cuda.set_per_process_memory_fraction`，再加载定位器。
该比例限制本进程的 PyTorch 分配量。其他 CUDA 分配仍可能使用额外内存。
显式请求的 CUDA 设备不可用时，启动失败。
服务不会把显式 CUDA 请求自动改为 CPU。

## 图像来源与坐标

摄像头编号为 1 到 3 的整数。
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
读取失败返回 503 并记录故障。请求不自动重试。

结果的 `coordinate_system` 为 `oriented_original_image_pixels`。
检测字段 `xyxy`、`center_xy`、`polygon` 和 `size_px` 使用该图像的坐标。
`angle_deg` 遵循定位器的 OpenCV 矩形角度约定。
QR 结果的 `corners_px`、`center_px` 和 `bounds_px` 使用完整图像坐标。
无法解码的标签标为 `decoded=false`，其文本不是经过验证的身份。

当前服务报告 `calibrated=false`，`calibrated_cameras` 列表为空。
服务不返回 `robot_mm`，也不生成虚构的相机到机器人变换。
CAL-01 添加标定加载、图像尺寸检查和机器人毫米坐标。
机器人运动目标必须使用适用平面上经过验证的标定。

## 路由与命令

路由相对于 `--base-path`，遵循 [服务契约](adr/0001-instrument-service-contract.md)。
检测和 QR 读取共用执行通道，同一时刻只运行 1 次推理。
重叠请求返回 409，服务不排队执行额外推理请求。

| 读取路由 | 结果 |
| --- | --- |
| `GET api/detect?camera=N` | 新图像的检测 JSON |
| `GET api/labels?camera=N` | 新完整图像的 QR 和 Micro QR 结果 JSON |
| `GET api/annotated.jpg?camera=N` | 新检测结果的标注 JPEG |
| `GET api/state` | 模型、设备、连接、忙状态、故障、租约和最近结果摘要 |
| `GET api/describe` | 契约版本和命令参数说明 |
| `GET api/events?since=0` | 服务事件，同时写入日志 |

读取路由不要求租约，但保留全部访问检查。
3 个推理路由必须且只能包含 1 个 `camera` 查询参数，不接受其他查询参数。
响应使用 `Cache-Control: no-store`。

命令必须带 `Content-Type: application/json` 和 `X-MG400-Client`。
先通过 `POST api/heartbeat` 获取控制租约，再发送命令。
租约有效期为 3 秒。命令运行时继续续期。
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

命令完成前返回 202 和 `command_id`。
读取 `state.last_command` 中的 `result`、`message` 和 `finished_at`。
成功命令在 `last_command.output` 中提供完整 JSON 结果。
结果必须是有限的 JSON 值，编码为紧凑 ASCII JSON 后不超过 256 KiB。
无效或过大的结果返回 503，且不发布。
失败或停止的命令为 `output=null`。
`last_result` 保留最近成功推理的摘要及其完成时间。
真实模式在首次成功处理图像前报告 `connected=false`。

## 停止与退出

任何通过认证的客户端都可调用 `POST api/stop`，不必持有租约。
请求必须带 `X-MG400-Client` 和 JSON 正文，例如 `{"wait_s":0}`。
`wait_s` 取 0 到 15 秒。
工作完成后返回 `confirmed`，仍在运行时返回 `pending`。
停止请求可重复发送。

停止请求取消当前操作，并丢弃延迟完成的结果。
启动期间，停止会取消模型发布，并在加载或预热结束前返回 `pending`。
QR 处理在各阶段之间检查取消状态。
已开始的模型预测不能强制中断。只有它返回后，停止才能得到确认。
租约过期会取消控制客户端的命令，不会取消独立的读取请求。

SIGINT、SIGTERM 和 SIGHUP 会在退出时请求取消。
退出最多等待当前推理或启动 15 秒，并结束租约轮询。
服务不控制机器人、位移台、夹爪或摄像头设备。
