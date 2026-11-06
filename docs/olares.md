[English](olares.en.md)

# Olares 配置与维护

[项目首页](../README.md) · [网络](#网络) · [获取代码](#获取代码) · [迁移](#从旧布局迁移) · [USB 夹爪](#usb-夹爪转换器) · [摄像头](#摄像头)

本页只说明 Olares 特有的设置和故障排查。
安装、更新、配置和日常命令见 [主机部署](deploy.md)。
本页命令以 root 身份在 Olares 宿主机终端运行（Control Hub → Terminal → Olares）。

## 网络

客户端通过 LarePass VPN 访问 Olares。Olares 的 VPN 地址为 `100.64.0.1`。
VPN 是访问边界。服务不检查令牌、Host 标头或来源。
所有服务监听 `127.0.0.1`。[单端口代理](#单端口代理)是 VPN 上唯一的入口。
Olares 直连机械臂的网口地址为 `192.168.1.10/24`，机械臂地址为 `192.168.1.6`。

| 服务 | 回环端口 |
| --- | --- |
| 机器人 | `8766` |
| 位移台 | `8767` |
| 热像仪 | `8768` |
| 炉控 | `8769` |
| 显微镜 | `8770` |
| 视觉 | `8771` |
| 实验室控制台和配方服务 | `8772` |
| MCP 网关 | `8773` |
| 实验室控制台控制套接字 | `8774` |
| 摄像头视频流 | `1985` |

安装前，把 `thermal`、`vision` 等可选服务加入 `MG400_OPTIONAL_SERVICES`。
服务列表和视觉运行环境见[主机部署](deploy.md#服务清单)。
所有者安装并启动视觉服务后，运行 `mg400ctl gpu`，以 UID `1000` 读取 GPU 内存。

可选的 [MCP 网关](agent-gateway.md) 只监听回环地址，没有代理路由。
远程 MCP 客户端须使用到回环接口的隧道，并携带 `MG400_GATEWAY_TOKEN` Bearer 凭据。

## 单端口代理

服务 `proxy` 用 1 个 VPN 端口提供实验室控制台和摄像头视频流。
通过 `--caddy PATH` 提供 Caddy 2 二进制文件。
安装器写入 `/etc/mg400/Caddyfile`，并安装 `/usr/local/lib/mg400/caddy`。
安装器不会下载 Caddy，也不会启动代理。

在 `/etc/mg400/mg400.env` 中设置以下值：

```ini
MG400_PROXY_BIND=100.64.0.1
MG400_PROXY_PORT=8080
```

代理地址必须是 1 个数字形式的回环或 VPN 地址，不得使用通配地址。
代理端口必须在 `1024` 到 `65535` 之间，且不同于所有上游端口。

代理只有以下路由：

| VPN 路径 | 回环上游 |
| --- | --- |
| `/lab/api/console/socket` | `127.0.0.1:8774`，控制台控制套接字（WebSocket） |
| `/lab/` | `127.0.0.1:8772`，固定基础路径为 `/lab/` |
| `/camera/<view>.mp4`、`/camera/<view>.mjpeg`、`/camera/<view>.jpg`、`/camera/<view>.full.jpg`、`/camera/fps.json` | `127.0.0.1:1985`，仅移除 `/camera` |

`<view>` 为 `camera1` 到 `camera4`。`/camera/<view>.mp4` 的 `<view>` 还可以是 `microscope`。
代理把根路径 `/` 重定向到实验室控制台 `/lab/`。参见[实验室控制台](dashboard.md#实验室控制台)。
摄像头路由只转发视频流和单帧图像，供控制台的 Cameras 工作区使用。
代理不转发机器人、位移台、热像仪、炉控、显微镜和视觉服务。
控制台、命令行工具和配方运行器在 `127.0.0.1` 直接调用它们。

配置完成后，安装提供的二进制文件并检查生成的配置：

```sh
/usr/bin/python3 /opt/mg400/deploy/install.py --dry-run --caddy /path/to/caddy
/usr/bin/python3 /opt/mg400/deploy/install.py --caddy /path/to/caddy
/usr/local/lib/mg400/caddy validate --config /etc/mg400/Caddyfile --adapter caddyfile
```

明确运行 `mg400ctl start proxy` 启动代理。
在 VPN 规则中允许 TCP `8080`，或已配置的代理端口。
打开 `http://100.64.0.1:8080/`。代理把它重定向到实验室控制台。
后续更新可复用已安装的二进制文件，不必再次传入 `--caddy`。

代理保留浏览器 `Host` 的端口，并用该值设置 `X-Forwarded-Host`。
它替换请求中的转发地址标头，并移除 `Forwarded` 和 `X-Real-IP`。
Caddy 原生反向代理转发 WebSocket 的 `Upgrade` 和 `Connection`，上游使用 HTTP/1.1。
上游响应头和读取超时均为 `60` s。
代理不新增请求重试，并禁用管理 API、配置持久化和请求日志。
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
| `/opt/mg400-web-control` | 网页面板代码 | `/opt/mg400`，`/etc/mg400/mg400.env` |
| `$DOCS/mg400-isaac` | 夹爪采集代码 | `/opt/mg400` |
| `$DOCS/mg400-terminal`、`$DOCS/mg400-tools` | 交互终端代码、启动脚本、模式切换工具 | `mg400ctl terminal`、`mg400ctl mode` |
| `$DOCS/mg400-data` | 连接锁、夹爪反馈、模式切换备份 | `/var/lib/mg400` |
| `/opt/mg400-camera-preview` | go2rtc、采集配置和预览程序 | `/usr/local/lib/mg400`、`/etc/mg400` |

迁移只做一次。迁移前，关闭所有面板页面和交互终端，下使能机械臂。

1. 停止旧服务，备份旧的服务定义，并停用旧的终端启动脚本。
   旧脚本使用旧的锁目录，与新服务不互斥：

   ```sh
   DOCS=/olares/userdata/Cache/pvc-appcache-qumusphusis-aah2phqnttncsxoh/isaaclab/documents
   systemctl stop mg400-web-panel mg400-stage-panel mg400-camera-preview mg400-gripper-collector.path mg400-gripper-collector
   mkdir -p /root/mg400-old-units
   cp -a /etc/systemd/system/mg400-* /root/mg400-old-units/
   mv "$DOCS/mg400-tools/start-terminal.sh" "$DOCS/mg400-tools/start-terminal.sh.disabled"
   ```

2. 按 [获取代码](#获取代码) 把 `/opt/mg400` 切换到目标提交。
3. 把摄像头设备写入配置文件：

   ```sh
   grep -o '/dev/v4l/by-path/[^&]*' /opt/mg400-camera-preview/go2rtc.yaml | paste -sd, | sed 's/^/MG400_CAMERA_DEVICES=/' >> /etc/mg400/mg400.env
   ```

4. 运行安装器，复用已有的 go2rtc：

   ```sh
   /usr/bin/python3 /opt/mg400/deploy/install.py --go2rtc /opt/mg400-camera-preview/go2rtc
   ```

5. 复制模式切换备份：

   ```sh
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

6. 按 [单端口代理](#单端口代理) 设置代理地址。
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

8. 启用[实验室控制台](dashboard.md#实验室控制台)后，核对以下各项：
   - Cameras 工作区显示所有已配置的画面。
   - Motion 工作区显示机械臂状态。
   - 在 Motion 工作区取得机械臂控制权后，`mg400ctl status` 显示夹爪采集为 `active`。
   - Motion 工作区的位移台卡片能连接 Arduino。

旧目录保留到核对完成。
要恢复旧布局，先运行 `mg400ctl stop all`，再把 `/root/mg400-old-units` 中的文件复制回 `/etc/systemd/system/` 并运行 `systemctl daemon-reload`。
然后把 `start-terminal.sh.disabled` 改回原名，恢复旧的终端启动脚本。

## USB 设备布局

所有实验室 USB 设备通过 1 个自供电集线器连接到 1 个主机端口。
控制器为 `0000:80:14.0`，USB 2 端口为 `12`，USB 3 端口为 `3`。
Toupcam 摄像头保持连接在集线器的 USB 3 侧。不得复位共用集线器。
复位会同时断开电机、炉子、气体、夹爪和摄像头的连接。

每台摄像头保持连接在表中的集线器端口。
4 台 AC410 的序列号均为 `HU12321178`，因此稳定名称使用端口路径。
`12.1.4` 上的是显微镜侧视摄像头，拍摄物镜转盘。go2rtc 独占设备，显微镜读取共享视频流。
`12.1.2` 上的摄像头从上方拍摄位移台。`12.1.3` 上的摄像头沿插入导轨拍摄，后方可见托盘。
`12.2.1` 上的摄像头拍摄托盘。将其路径配置为 `camera4` 后，控制台即可显示。

| 设备 | VID:PID | 稳定标识 | 集线器端口 | 服务 |
| --- | --- | --- | --- | --- |
| 位移台 Arduino Uno R3 | `2341:0043` | `/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_1453230313235111D1E0-if00` | `12.2.3` | `stage`（`MG400_STAGE_SERIAL`） |
| 转盘与调焦 Arduino Uno R3 | `2341:0043` | `/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_035353433353512070D1-if00` | `12.2.2` | `microscope` |
| 炉子 FT231X | `0403:6015` | `/dev/serial/by-id/usb-FTDI_FT231X_USB_UART_DU0DT9G8-if00-port0` | `12.3.2` | `furnace`（`MG400_FURNACE_SERIAL`） |
| 气体控制器 FT232R | `0403:6001` | `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_ABBCE4JW-if00-port0` | `12.3.1` | `furnace`（`MG400_MFC_SERIAL`） |
| 夹爪转换器 FT232R | `0403:6001` | `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0` | `12.3.3` | `collector` |
| Thermal Master P1 | `3474:45c2` | libusb，序列号 `P1016021DG040100208` | `12.1.1` | `thermal` |
| AC410 位移台俯视摄像头 | `f326:1306` | `/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.2:1.0-video-index0` | `12.1.2` | `camera` |
| AC410 插入导轨摄像头 | `f326:1306` | `/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.3:1.0-video-index0` | `12.1.3` | `camera` |
| AC410 显微镜侧视摄像头 | `f326:1306` | `/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.4:1.0-video-index0` | `12.1.4` | `camera`，通过 go2rtc 共享 |
| AC410 托盘摄像头 | `f326:1306` | `/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.2.1:1.0-video-index0` | `12.2.1` | `camera` |
| ToupTek USB 3 摄像头 | `0547:1375`（USB 3），`0547:1376`（USB 2） | Toupcam SDK，无序列号 | `3.2.4` | `microscope`，与炉子录像共享 |

控制器必须使用完整串口路径。气体控制器与夹爪转换器的 VID:PID 相同，但序列号不同。
热像仪和 Toupcam 使用 `/dev/bus/usb` 下的 USB 设备节点，没有串口或视频设备节点。
总线和设备编号可能变化。让 libusb 或 SDK 查找摄像头。

### USB 访问规则

安装程序只为已注册且启用的服务选择 `config/udev/70-mg400-<name>.rules.in`。
它从主机用户组数据库获取 GID `1000` 的组名，替换 `@SERVICE_GROUP@`。
每条规则匹配 VID:PID，并以 `MODE="0660"` 授予该组访问权限。
串口规则还匹配表中的设备序列号。摄像头规则保留现有端口路径名称。
热像仪规则按前缀匹配表中的 P1 序列号。P1 在序列号后附加不可打印字节，而 udev 比较完整的值。
模板不创建设备别名，也不执行复位程序。

安装程序以 root 身份写入 `/etc/udev/rules.d/70-mg400-<name>.rules`，权限为 `0644`。
随后执行 `udevadm control --reload-rules` 和 `udevadm trigger --subsystem-match=usb`。
USB trigger 不会复位集线器或启动服务。
TTY 和视频权限在下次 udev 事件时生效。服务仍可通过现有的 `dialout` 和 `video` 组访问设备。
安装前，用安装程序的 dry run 检查所选文件和命令。

默认服务选择 `stage`、`collector`、`camera`、`furnace` 和 `microscope` 模板。
`furnace` 模板含 2 条串口控制器规则。
启用 `thermal` 后会选择 P1 模板。
显微镜独占 Toupcam，也提供炉子录像。炉子规则不另行授予该摄像头的访问权限。
模板不选择服务配置中的设备路径。保持这些路径与表格一致。

## USB 夹爪转换器

转换器的 USB 身份为 `0403:6001`，序列号为 `BG02Q5TN`。
主机稳定路径为 `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0`。
`ttyUSB` 编号可能变化，采集程序按完整 USB 身份选择设备。
不要用扩展坞上的其他串口替代。
位移台的 Arduino 也接在 Olares USB 上，在 `/dev/serial/by-id/` 中是另一项。
夹爪采集不使用 Arduino，位移台服务不列出夹爪转换器。

采集程序每 50 ms 读取三个状态寄存器，并原子替换反馈文件 `/var/lib/mg400/work/gripper_feedback.json`。
采集服务启动时只读状态，收到机器人服务的请求后才执行初始化或位置命令。
关闭控制台后，采集继续运行。
串口被占用、设备缺失或读取失败时，采集程序退出。
服务在异常退出后等待 2 秒再启动，不限制重试次数。
USB 恢复连接后，采集自动恢复，不必重新打开控制台。
每次启动使用新会话，不重放旧命令，也不清除命令锁定。
若断线发生在命令执行期间，控制可能锁定，须先核实夹爪状态。
机器人服务与采集程序必须位于同一主机，并共享同一单调时钟域。
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

在 `MG400_CAMERA_DEVICES` 中配置 1 到 4 台 AC410，使用不同的 USB 接口路径。
视频流按设备顺序命名为 `camera1` 到 `camera4`。配置为空时关闭摄像头。
在 `MG400_CAMERA_MODES` 中为每台设备指定 1 个 MJPEG 模式，用逗号分隔。只接受以下模式：

```text
1920x1080@60, 1920x1080@30, 4000x3000@15, 3840x2160@30,
2592x1944@30, 2560x1440@30, 2048x1536@30, 1600x1200@30,
1280x720@30, 1024x768@30, 1024x576@30, 800x600@30,
640x480@30, 640x360@30, 320x240@30
```

列表为空时，普通流使用 `1280x720@30`，`MG400_MICROSCOPE_SIDE_STREAM` 使用 `3840x2160@30`。
显式列表覆盖这些默认值，包括侧视流。
以下 4 台摄像头的配置按上表接口顺序排列。
侧视流 `camera3` 使用 `640x360@30`，与 `3840x2160` 侧视参考图像的宽高比相同。其他摄像头使用 `1280x720@30`。
原版 `uvcvideo` 驱动让 AC410 在大于 `640x480` 的模式下占用最大的同步传输时隙，所以共享 USB 2 链路只能容纳 1 路这样的流。
安装 [USB 带宽补丁](#usb-带宽补丁) 后，3 路 `1280x720@30` 和 `640x360@30` 的侧视流可以同时以 30 fps 运行。
提高模式前，先用 `mg400ctl camera-check` 和内核日志检查带宽：

```ini
MG400_CAMERA_DEVICES=/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.2:1.0-video-index0,/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.3:1.0-video-index0,/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.4:1.0-video-index0,/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.2.1:1.0-video-index0
MG400_CAMERA_MODES=1280x720@30,1280x720@30,640x360@30,1280x720@30
MG400_MICROSCOPE_SIDE_STREAM=camera3
```

只预加载宽度不超过 1280 且高度不超过 720 的流。
更大的流在视频流、显微镜或单帧读取请求时启动。
显微镜服务只在 `select_objective` 运行期间读取侧视流。
预览转发服务不转发大于 `1280x720` 的侧视流。
`camera3` 以 `3840x2160@30` 运行时，共享 USB 2 链路截断 `camera2` 的图像帧。
按需启动的流可能发送截断的第 1 帧。显微镜读取线程丢弃不完整的帧。
大尺寸流的最后 1 个观看者离开后，预览服务在读取响应正文的间隙释放上游读取连接。
套接字操作仍使用 2 秒超时。这不是总关闭时限。其他读取者可使该流继续运行。
视频流上限为 30 fps，包括采集模式为 `1920x1080@60` 时。
观看者可以用 `?fps=N` 请求更低的帧率，N 为 1 到 30，例如 `/camera/camera2.mjpeg?fps=5`。其他查询值返回 404。
每个预加载的流另有 1 路 H.264 预览 `/camera/<view>.mp4`，格式为分段 MP4，帧率与摄像头相同。宽度大于 640 的流码率上限为 4 Mbit/s，其他流为 2 Mbit/s。该路由不接受查询参数。
go2rtc 从 `http://127.0.0.1:8770/api/camera/microscope/mjpeg` 读取 `1296x968` 的显微镜预览，在 `/camera/microscope.mp4` 提供码率上限为 3 Mbit/s 的 H.264 预览。
go2rtc 只在有 H.264 观看者时读取显微镜预览。显微镜服务没有画面时，`/camera/microscope.mp4` 返回 502，控制台改用中转的 MJPEG。
`/camera/fps.json` 返回每路 MJPEG 流在最近 2 s 内发送给每个观看者的帧率。没有观看者的流为 `null`。控制台在摄像头状态中读取这些值。
go2rtc 在第 1 个观看者打开 H.264 预览时启动 ffmpeg 编码，在最后 1 个观看者离开后停止。
ffmpeg 通过只监听 `127.0.0.1:8554` 的 RTSP 读写该流。主机未安装 ffmpeg 时，H.264 预览返回 502。
单帧请求从同一 go2rtc 流读取配置尺寸的图像，不切换模式，也不另行打开设备采集。

4 台 AC410 与热像仪共用 480 Mbit/s 的 USB 2 链路。多路大尺寸流可能超过其周期传输预算。
同时使用真实设备前，必须按 H-21 在有人监督下检查带宽。
托盘上约 30 mm 的 Micro QR 标签含 17 个模块，横向至少需要 70 像素。
图像宽度为 1280 像素时，556 mm 的托盘视野约有 2.3 像素/mm。
保持摄像头连接表中的 USB 接口。修改设备或模式后，重新运行安装器并重启摄像头。

启动摄像头服务：

```sh
mg400ctl start camera
```

在实验室控制台的 Cameras 工作区查看画面。
启用 `proxy` 服务时，视频流链接为 `http://<MG400_PROXY_BIND>:<MG400_PROXY_PORT>/camera/<view>.mjpeg`，`mg400ctl camera-url` 会列出这些链接。
关闭终端不会停止预览服务。
停止预览服务时，摄像头采集服务也停止。
启动失败时，运行 `mg400ctl logs camera`。

服务端只保留最新待处理帧，网络变慢时丢弃积压帧。
服务端发送超时为 1.5 秒。H.264 预览的发送超时为 5 秒。

### USB 带宽补丁

`deploy/uvcvideo/` 包含 `uvcvideo` 驱动的补丁。补丁添加模块参数 `mjpeg_bpp`，单位为 1/1000 字节每像素，`0` 表示关闭。
对 MJPEG 模式，驱动按宽度、高度、帧率和 `mjpeg_bpp` 估算每个微帧的传输量。估算值小于摄像头请求值时，驱动使用估算值。
`mjpeg_bpp=200` 时，AC410 在 `1280x720@30` 下使用 alt 5（每微帧 800 字节）。原版驱动使用 alt 11（每微帧 3072 字节）。

安装前，确认主机已安装 `dkms` 和当前内核的 `linux-headers`，并且 Secure Boot 已关闭。补丁模块没有签名。

1. 构建并安装模块：

   ```bash
   sudo /usr/bin/python3 /opt/mg400/deploy/uvcvideo/install.py
   ```

   脚本下载当前内核系列的上游驱动源码，例如 `v6.14`，应用补丁，再通过 DKMS 构建和安装模块。
   然后脚本写入 `/etc/modprobe.d/mg400-uvcvideo.conf`，内容为 `options uvcvideo mjpeg_bpp=200`。
2. 重启主机，或重新加载驱动：

   ```bash
   sudo mg400ctl stop camera thermal
   sudo rmmod uvcvideo
   sudo modprobe uvcvideo
   sudo mg400ctl start camera thermal
   ```

3. 确认 `cat /sys/module/uvcvideo/parameters/mjpeg_bpp` 输出 `200`。
   在 `/sys/kernel/debug/usb/devices` 中，`1280x720@30` 的摄像头使用 `Alt= 5`。

限制：

- DKMS 为同一内核系列的每个新内核重建模块。启动其他内核系列时，主机使用原版驱动，并忽略 `mjpeg_bpp`。此时再次运行脚本。
- 构建失败时，脚本删除它的 DKMS 包，原版驱动保持不变。
- 3 路 `1920x1080@30` 加侧视流接近链路上限。不要使用该配置。

### 摄像头故障

画面异常时，先运行 `mg400ctl camera-check`。
它按 USB 接口路径检查已配置的摄像头，再检查 2 个服务和应预加载的 go2rtc 流。
对更大的流，它报告按需启动，不要求或启动连续预加载。
全部正常时，退出码为 0。有问题时，退出码为 1，并显示下一步操作。
有摄像头缺失时，它不再检查服务。

所有流同时断流时，先用 `lsusb -t` 和 `ls -l /dev/video*` 检查设备是否仍被识别。
服务显示 `active` 只说明进程存活。取图返回 `200` 时，还须确认图片非空且能解码。
若集线器和摄像头都未被识别，重启预览服务不能代替 USB 连接检查。

若摄像头已被识别，但视频流帧率很低，在 Olares 主机运行
`curl -s http://127.0.0.1:1984/api/preload`。
确认输出包含所有宽度不超过 1280 且高度不超过 720 的已配置流。
若应预加载的流缺失，运行 `mg400ctl restart camera`，等待 2 秒后再次查询。
采集服务启动时若设备缺失，预加载可能未建立。
控制台的视频流会自动重连。已配置的 `camera<N>.jpg` 单帧接口在流按需启动时可能较慢。

提高分辨率后，须按 H-21 同时检查所需画面，并验证连续多张 JPEG 能完整解码。
浏览器显示分辨率和帧率不能排除花屏或部分图像损坏。
若出现这类问题，恢复 `640 × 480` 配置，再检查 USB 采集链路。

所有实验室设备共用外接集线器。不得通过复位共用上游集线器恢复摄像头。
检查故障摄像头的端口和日志。改变设备连接前，遵守有人监督的硬件操作流程。
USB 重新连接不代表允许发送机械臂、位移台、夹爪、炉子或气体指令。
