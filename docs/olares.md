[English](olares.en.md)

# Olares 配置与维护

[项目首页](../README.md) · [网络](#网络) · [获取代码](#获取代码) · [迁移](#从旧布局迁移) · [USB 夹爪](#usb-夹爪转换器) · [摄像头](#摄像头)

本页只说明 Olares 特有的设置和故障排查。
安装、更新、配置和日常命令见 [主机部署](deploy.md)。
本页命令以 root 身份在 Olares 宿主机终端运行（Control Hub → Terminal → Olares）。

## 网络

客户端通过 LarePass VPN 访问 Olares。Olares 的 VPN 地址为 `100.64.0.1`。
使用各服务的独立端口时，面板和摄像头预览必须监听该地址。在 `/etc/mg400/mg400.env` 中设置：

```ini
MG400_PANEL_BIND=100.64.0.1
MG400_CAMERA_BIND=100.64.0.1
MG400_STAGE_BIND=100.64.0.1
```

Olares 的 VPN 访问控制规则必须允许 TCP `8766`、`8767` 和 `1985`。
Olares 直连机械臂的网口地址为 `192.168.1.10/24`，机械臂地址为 `192.168.1.6`。

启用可选炉控服务时，设置 `MG400_FURNACE_BIND=100.64.0.1`，并在 VPN 规则中允许 TCP `8769`。
保持 `MG400_FURNACE_TOKEN` 私密。在[炉控网页](furnace.md)中输入密钥，不得放入 URL。

可选配方服务必须接受 VPN 客户端时，设置 `MG400_ORCH_BIND=100.64.0.1`，并在 VPN 规则中允许 TCP `8772`。
通过 bearer 标头发送 `MG400_ORCH_TOKEN`，不得放入 URL。下游令牌别名必须保存在主机上，不得公开。
默认监听地址仍为 `127.0.0.1`。访问与恢复规则见[配方服务](orchestrator.md#http-服务)。

可选的 [MCP 网关](agent-gateway.md) 必须保持 `MG400_GATEWAY_BIND=127.0.0.1`，端口为 `8773`。不得通过 VPN 直接开放此端口。
远程 MCP 客户端必须使用到回环接口的已认证隧道，并携带 `MG400_GATEWAY_TOKEN` Bearer 凭据。

## 可选的单端口代理

可选服务 `proxy` 用 1 个 VPN 端口转发仪器服务。
通过 `--caddy PATH` 提供 Caddy 2 二进制文件。
安装器写入 `/etc/mg400/Caddyfile`，并安装 `/usr/local/lib/mg400/caddy`。
安装器不会下载 Caddy，也不会启动代理。

把 `proxy` 加入 `MG400_OPTIONAL_SERVICES`，保留其他已选服务。
如果没有其他可选服务，在 `/etc/mg400/mg400.env` 中设置：

```ini
MG400_OPTIONAL_SERVICES=proxy
MG400_PROXY_BIND=100.64.0.1
MG400_PROXY_PORT=8080
MG400_PANEL_BIND=127.0.0.1
MG400_PANEL_BASE_PATH=/robot/
MG400_STAGE_BIND=127.0.0.1
MG400_STAGE_BASE_PATH=/stage/
MG400_CAMERA_BIND=127.0.0.1
```

代理地址必须是 1 个数字形式的回环或 VPN 地址，不得使用通配地址。
代理端口必须在 `1024` 到 `65535` 之间，且不同于所有上游端口。
各上游端口也必须互不相同。
写文件前，安装器检查已启用服务的回环地址、原生前缀和令牌。

如果启用了 `furnace` 或 `orch`，还必须设置对应项：

```ini
MG400_FURNACE_BIND=127.0.0.1
MG400_FURNACE_BASE_PATH=/furnace/
MG400_ORCH_BIND=127.0.0.1
MG400_ORCH_BASE_PATH=/lab/
```

路由保留各服务的原生前缀：

| VPN 路径 | 回环上游 | 必需的原生前缀 |
| --- | --- | --- |
| `/robot/` | `127.0.0.1:8766` | `/robot/` |
| `/stage/` | `127.0.0.1:8767` | `/stage/` |
| `/camera/<key>/` | `127.0.0.1:1985` | 仅移除 `/camera`，保留 `/<key>/` |
| `/thermal/` | `127.0.0.1:8768` | `/thermal/` |
| `/furnace/` | `127.0.0.1:8769` | `/furnace/` |
| `/microscope/` | `127.0.0.1:8770` | `/microscope/` |
| `/lab/` | `127.0.0.1:8772` | `/lab/` |

摄像头端口固定为 `1985`。其他上游端口采用已提供的 `MG400_*_PORT` 配置。
代理不会安装或启用热成像和显微镜服务。
只有上游服务存在且使用必需的原生前缀运行，路由才能工作。
单独启动的服务必须监听 `127.0.0.1`。即使连接来自本机，也必须配置服务令牌。
热成像启动环境必须包含 `MG400_THERMAL_TOKEN` 和 `MG400_THERMAL_BASE_PATH=/thermal/`。
`thermal-cam --base-path /thermal/` 覆盖该前缀配置。

单独安装的显微镜服务使用 `MG400_MICROSCOPE_TOKEN`，并配置其原生前缀选项为 `/microscope/`。
共享环境示例不新增这两个服务的直接访问令牌。
分别配置它们的启动环境。不得把令牌放入 Caddy 配置或 URL。
MCP 网关仍只监听回环地址，不提供代理路由。

更改服务 URL 前，停止正在运行的操作。更新下游客户端 URL，加入新的原生前缀。
例如，使用 `MG400_ORCH_ROBOT_URL=http://127.0.0.1:8766/robot/` 和 `MG400_ORCH_STAGE_URL=http://127.0.0.1:8767/stage/contract/`。
位移台契约位于 `/stage/contract/api/`，与原有页面 API 分开。
未配置基本路径时，位移台和热成像服务仍默认使用 `/`。

配置完成后，安装提供的二进制文件并检查生成的配置：

```sh
/usr/bin/python3 /opt/mg400/deploy/install.py --dry-run --caddy /path/to/caddy
/usr/bin/python3 /opt/mg400/deploy/install.py --caddy /path/to/caddy
/usr/local/lib/mg400/caddy validate --config /etc/mg400/Caddyfile --adapter caddyfile
```

通过 `mg400ctl` 重启配置已改变的上游服务。
然后明确运行 `mg400ctl start proxy` 启动代理。
在 VPN 规则中允许 TCP `8080`，或已配置的代理端口。
打开 `http://100.64.0.1:8080/robot/`，或表中的其他路由。
访问摄像头时，必须保留原有预览 URL 中的完整密钥。
后续更新可复用已安装的二进制文件，不必再次传入 `--caddy`。

代理原样传递浏览器的 bearer 凭据，不会代填上游令牌。
各服务保留令牌、Host、Origin、租约和命令检查。
代理保留浏览器 `Host` 的端口，并用该值设置 `X-Forwarded-Host`。
它替换请求中的转发地址标头，并移除 `Forwarded` 和 `X-Real-IP`。

Caddy 原生反向代理转发 WebSocket 的 `Upgrade` 和 `Connection`，上游使用 HTTP/1.1。
上游响应头和读取超时均为 `60` s，大于机械臂的 `5` s ping 间隔。
代理不新增请求重试，并禁用管理 API 和配置持久化。
摄像头 URL 包含密钥，因此配置排除了请求访问日志和请求错误日志。
此 HTTP 入口使用 VPN 连接，不启用公共 HTTPS。

## 获取代码

仓库公开。Olares 通过 HTTPS 从 GitHub 读取代码，不需要凭据。
按 [主机部署](deploy.md#安装与更新) 获取代码并运行安装器。
`/opt/mg400` 没有 `origin` 时，先添加：

```sh
git -C /opt/mg400 remote add origin https://github.com/shc0507/dobot_mg400.git
```

Olares 没有 GitHub 写入凭据，不能推送。在本地修改代码，推送到 GitHub。

### 无法访问 GitHub 时

如果 Olares 不能访问 GitHub，通过 Git bundle 获取代码：

1. 在 Olares 宿主机读取已部署的提交：

   ```sh
   git -C /opt/mg400 rev-parse HEAD
   ```

2. 在本地项目根目录生成 bundle，只包含该提交之后的历史，并记录目标提交。
   将 `HOST_SHA` 替换为第 1 步的提交：

   ```sh
   git bundle create /tmp/mg400-deploy.bundle main ^HOST_SHA
   git rev-parse main
   ```

   `/opt/mg400` 不存在时，用 `--all` 代替 `main ^HOST_SHA`，生成包含完整历史的 bundle。

3. 在 Olares 网页的 Files 中，把 bundle 上传到 Drive 的 `Home/Downloads`。
   该目录在宿主机的路径为：

   ```sh
   BUNDLE=/olares/rootfs/userspace/pvc-userspace-qumusphusis-8vsdwupyazk45sjv/Home/Downloads/mg400-deploy.bundle
   ```

4. `/opt/mg400` 不存在时，先运行 `git init -q /opt/mg400`。
   将下列 `COMMIT_SHA` 替换为第 2 步记录的目标提交：

   ```sh
   git -C /opt/mg400 bundle verify "$BUNDLE"
   git -C /opt/mg400 fetch "$BUNDLE" 'refs/heads/*:refs/remotes/bundle/*'
   git -C /opt/mg400 switch --detach COMMIT_SHA
   git -C /opt/mg400 rev-parse HEAD
   ```

   如果 `bundle verify` 报告缺少提交，用 `--all` 重新生成 bundle。

然后按 [主机部署](deploy.md#安装与更新) 运行安装器。

## 从旧布局迁移

旧布局把代码和数据放在多个目录，部分位于 IsaacLab 应用的存储中。
下文的 `$DOCS` 为 `/olares/userdata/Cache/pvc-appcache-qumusphusis-aah2phqnttncsxoh/isaaclab/documents`。

| 旧位置 | 内容 | 新位置 |
| --- | --- | --- |
| `/opt/mg400-web-control` | 网页面板代码，`.env` 含 `MG400_PANEL_TOKEN` | `/opt/mg400`，`/etc/mg400/mg400.env` |
| `$DOCS/mg400-isaac` | 夹爪采集代码 | `/opt/mg400` |
| `$DOCS/mg400-terminal`、`$DOCS/mg400-tools` | 交互终端代码、启动脚本、模式切换工具 | `mg400ctl terminal`、`mg400ctl mode` |
| `$DOCS/mg400-data` | 连接锁、夹爪反馈、模式切换备份 | `/var/lib/mg400` |
| `/opt/mg400-camera-preview` | go2rtc、采集配置、预览程序和密钥 | `/usr/local/lib/mg400`、`/etc/mg400`、`/var/lib/mg400/camera` |

迁移只做一次。迁移前，关闭所有面板页面和交互终端，下使能机械臂。

1. 停止旧服务，备份旧的服务定义，并停用旧的终端启动脚本。
   旧脚本使用旧的锁目录，与新面板不互斥：

   ```sh
   DOCS=/olares/userdata/Cache/pvc-appcache-qumusphusis-aah2phqnttncsxoh/isaaclab/documents
   systemctl stop mg400-web-panel mg400-stage-panel mg400-camera-preview mg400-gripper-collector.path mg400-gripper-collector
   mkdir -p /root/mg400-old-units
   cp -a /etc/systemd/system/mg400-* /root/mg400-old-units/
   mv "$DOCS/mg400-tools/start-terminal.sh" "$DOCS/mg400-tools/start-terminal.sh.disabled"
   ```

2. 按 [获取代码](#获取代码) 把 `/opt/mg400` 切换到目标提交。
3. 把旧的网页面板访问密钥和摄像头设备写入配置文件。用户不必换密钥：

   ```sh
   grep '^MG400_PANEL_TOKEN=' /opt/mg400-web-control/.env >> /etc/mg400/mg400.env
   grep -o '/dev/v4l/by-path/[^&]*' /opt/mg400-camera-preview/go2rtc.yaml | paste -sd, | sed 's/^/MG400_CAMERA_DEVICES=/' >> /etc/mg400/mg400.env
   ```

4. 运行安装器，复用已有的 go2rtc：

   ```sh
   /usr/bin/python3 /opt/mg400/deploy/install.py --go2rtc /opt/mg400-camera-preview/go2rtc
   ```

5. 复制摄像头预览密钥和模式切换备份。保留原密钥时，预览链接不变：

   ```sh
   install -o 1000 -g 1000 -m 0600 /opt/mg400-camera-preview/preview.key /var/lib/mg400/camera/preview.key
   test -d "$DOCS/mg400-data/work/control-mode" && cp -a "$DOCS/mg400-data/work/control-mode" /var/lib/mg400/work/ && chown -R 1000:1000 /var/lib/mg400/work/control-mode
   ```

   检查夹爪命令锁。该文件存在时，有夹爪命令结果未确认：

   ```sh
   ls -l "$DOCS/mg400-data/work/gripper_olares_feedback.json.command-lock.json"
   ```

   文件存在时，先核实夹爪状态，再按新的反馈文件名复制该锁。
   新采集服务读到该锁后保持锁定，与旧服务一致：

   ```sh
   install -o 1000 -g 1000 -m 0600 "$DOCS/mg400-data/work/gripper_olares_feedback.json.command-lock.json" /var/lib/mg400/work/gripper_feedback.json.command-lock.json
   ```

6. 按 [网络](#网络) 设置监听地址。
7. 删除旧服务的附加配置和开机自启链接，再启动服务。
   第 1 步已备份这些文件：

   ```sh
   rm -rf /etc/systemd/system/mg400-web-panel.service.d
   rm -f /etc/systemd/system/*.wants/mg400-*.service
   systemctl daemon-reload
   systemctl is-enabled mg400-web-panel mg400-stage-panel mg400-camera-preview
   mg400ctl start web stage camera
   mg400ctl status
   ```

   `is-enabled` 对三项都显示 `static`，表示服务不会开机自启。

8. 核对以下各项：
   - `mg400ctl camera-url` 的链接能打开三路画面。
   - 网页面板接受原访问密钥。
   - 网页面板点击 `Connect & take control` 后，`mg400ctl status` 显示夹爪采集为 `active`。
   - 位移台面板能连接 Arduino。

旧目录保留到核对完成。
要恢复旧布局，先运行 `mg400ctl stop all`，再把 `/root/mg400-old-units` 中的文件复制回 `/etc/systemd/system/` 并运行 `systemctl daemon-reload`。
然后把 `start-terminal.sh.disabled` 改回原名，恢复旧的终端启动脚本。

## USB 夹爪转换器

转换器的 USB 身份为 `0403:6001`，序列号为 `BG02Q5TN`。
主机稳定路径为 `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0`。
`ttyUSB` 编号可能变化，采集程序按完整 USB 身份选择设备。
不要用扩展坞上的其他串口替代。
位移台的 Arduino 也接在 Olares USB 上，在 `/dev/serial/by-id/` 中是另一项。
夹爪采集不使用 Arduino，位移台面板不列出夹爪转换器。

采集程序每 50 ms 读取三个状态寄存器，并原子替换反馈文件 `/var/lib/mg400/work/gripper_feedback.json`。
采集服务启动时只读状态，收到面板请求后才执行初始化或位置命令。
关闭面板后，采集继续运行。
串口被占用、设备缺失或读取失败时，采集程序退出。
服务在异常退出后等待 2 秒再启动，不限制重试次数。
USB 恢复连接后，采集自动恢复，不必重新打开面板。
每次启动使用新会话，不重放旧命令，也不清除命令锁定。
若断线发生在命令执行期间，控制可能锁定，须先核实夹爪状态。
面板与采集程序必须位于同一主机，并共享同一单调时钟域。
命令文件和锁的格式见 [USB 夹爪协议](development.md#usb-夹爪协议)。
查看日志：`mg400ctl logs collector`。

### 恢复 USB 连接

如果日志报告找不到 `BG02Q5TN`，先在主机检查 USB：

```sh
ls -l /dev/serial/by-id/
dmesg --ctime | grep -E 'BG02Q5TN|ftdi_sio|ttyUSB|device descriptor|accepting address'
```

`device descriptor read` 或 `device not accepting address` 表示 USB 枚举失败。
先按日志中的端口路径核对原转换器，恢复该 USB 连接。
更换集线器连接线不会改变转换器序列号，不要据此改选其他串口。
若内核提供该端口的 `disable` 文件，可以单独重新枚举该端口。
先停止采集监听和采集服务。
将 `disable` 写为 `1`，再写回 `0`，随后恢复监听。
不要重置共同上游集线器，否则其他设备也会断开。
确认 `BG02Q5TN` 重新出现后，再启动采集并检查反馈文件是否持续更新。

若服务或监听处于 `failed` 状态，修复 USB 连接后执行：

```sh
systemctl reset-failed mg400-gripper-collector.service mg400-gripper-collector.path
systemctl restart mg400-gripper-collector.path
mg400ctl start collector
```

## 摄像头

三台 AC410 接在 Olares USB 上，按 USB 接口路径绑定。
预览分辨率为 `640 × 480`，每路使用一条连续 MJPEG 视频流，最高每秒 30 帧。
保持摄像头连接原 USB 接口。
更换接口后，修改 `MG400_CAMERA_DEVICES`，再重新运行安装器并重启摄像头。

启动预览，再显示含访问密钥的链接：

```sh
mg400ctl start camera
mg400ctl camera-url
```

连接 LarePass VPN 后，在浏览器打开该链接。
链接包含访问密钥，服务重启后保持不变。
关闭终端不会停止预览服务。
停止预览时，摄像头采集服务也停止。
启动失败时，运行 `mg400ctl logs camera`。

画面下方显示实际解码尺寸和更新帧率。实际帧率受摄像头、网络和浏览器性能影响。
服务端和浏览器只保留最新待处理帧，网络变慢时丢弃积压帧。
服务端发送超时为 1.5 秒。
连续 1.5 秒未收到完整帧时，网页重连并暂留上一帧。
断流或显示帧自接收起满 1.5 秒时，网页标注“画面已暂停 · 非实时”。
上一帧自接收起最多保留 5 秒，重连不会延长该时限。
切换到其他标签页时暂停预览并清图，返回预览页后恢复。

### 识别叠加

在摄像头卡片中启用 `识别叠加`，即可显示检测框、轮廓、类别和置信度。
打开页面时，该开关默认关闭。
芯片使用绿色 `#18b85a`，机械臂使用橙色 `#ed8a24`。
颜色按类别名称选择，不依赖模型的数字类别 ID。

卡片可见且画面实时更新时，每路最多每秒请求 1 次检测。
页面中的摄像头轮流请求。请求变慢时，更新率可能降低。
卡片滚出视口、浏览器切换标签页、关闭开关或画面暂停时，均取消并清除该路叠加。
这些操作保留原有视频暂停规则。
没有新结果时，叠加在 1.5 秒后过期。浏览器检测请求在 2 秒后超时。

检测使用单独的图像，可能落后于视频。
叠加使用图像像素坐标，并计入图像周围的空白边距。
结果的图像尺寸与当前画面不同时，页面隐藏结果。
不要把叠加结果用作经过标定的机器人运动目标。

带访问密钥的路由 `/<key>/camera<N>/detections.json` 请求固定地址 `http://127.0.0.1:8771/api/detect?camera=N`。
视觉服务必须使用端口 `8771` 和基本路径 `/`，此代理才能访问。
预览服务读取 `MG400_VISION_TOKEN`，并作为 Bearer 请求头发送。浏览器不会收到该密钥。
预览单元保留 `/etc/mg400/mg400.env` 中的此变量。
代理使用 2 秒套接字超时，不使用环境代理，也不跟随重定向。
无效 JSON 或超过 256 KiB 的检测响应会被拒绝。

视觉出错时，路由返回 `{"available":false}`，卡片显示 `识别不可用`。
视频独立于视觉服务继续运行。
预览不会启动视觉服务。运行环境和启动条件见 [视觉服务](vision.md)。

### 摄像头故障

画面异常时，先运行 `mg400ctl camera-check`。
它按配置的 USB 接口路径检查三个摄像头，再检查两个服务和 go2rtc 预加载的流。
全部正常时，退出码为 0。有问题时，退出码为 1，并显示下一步操作。
有摄像头缺失时，它不再检查服务。

三路同时断流时，先用 `lsusb -t` 和 `ls -l /dev/video*` 检查设备是否仍被识别。
服务显示 `active` 只说明进程存活。取图返回 `200` 时，还须确认图片非空且能解码。
若集线器和摄像头都未被识别，重启预览服务不能代替 USB 连接检查。

若摄像头已被识别，但预览帧率很低，在 Olares 主机运行
`curl -s http://127.0.0.1:1984/api/preload`。
确认输出包含 `camera1`、`camera2`、`camera3`。
若有缺失，运行 `mg400ctl restart camera`，等待 2 秒后再次查询。
采集服务启动时若设备缺失，预加载可能未建立。
网页的视频流会自动重连。保留的 `camera1.jpg`、`camera2.jpg`、`camera3.jpg` 单帧接口在没有持续采集时可能较慢。

提高分辨率后，须同时检查三路画面，并验证连续多张 JPEG 能完整解码。
浏览器显示分辨率和帧率不能排除花屏或部分图像损坏。
若出现这类问题，恢复 `640 × 480` 配置，再检查 USB 采集链路。

摄像头与夹爪 USB 转换器共用外接集线器。
恢复共同上游 USB 端口前，停止 `mg400-gripper-collector.path` 和 `mg400-gripper-collector.service`。
Arduino 接在同一集线器时，也先在位移台面板点击 `Stop all motors` 和 `Disconnect`。
操作后恢复监听和采集服务。不要把 USB 重新连接视为允许机械臂或夹爪运动。
