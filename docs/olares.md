# Olares 配置与维护

[项目首页](../README.md) · [网络](#网络) · [获取代码](#获取代码) · [迁移](#从旧布局迁移) · [USB 夹爪](#usb-夹爪转换器) · [摄像头](#摄像头)

本页只说明 Olares 特有的设置和故障排查。
安装、更新、配置和日常命令见 [主机部署](deploy.md)。
本页命令以 root 身份在 Olares 宿主机终端运行（Control Hub → Terminal → Olares）。

## 网络

客户端通过 LarePass VPN 访问 Olares。Olares 的 VPN 地址为 `100.64.0.1`。
面板和摄像头预览必须监听该地址。在 `/etc/mg400/mg400.env` 中设置：

```ini
MG400_PANEL_BIND=100.64.0.1
MG400_CAMERA_BIND=100.64.0.1
MG400_STAGE_BIND=100.64.0.1
```

Olares 的 VPN 访问控制规则必须允许 TCP `8766`、`8767` 和 `1985`。
Olares 直连机械臂的网口地址为 `192.168.1.10/24`，机械臂地址为 `192.168.1.6`。

## 获取代码

Olares 没有 GitHub 读取凭据，通过 Git bundle 获取代码。

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

### 摄像头故障

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
