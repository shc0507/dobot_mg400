# Olares 配置与维护

[项目首页](../README.md) · [连接配置](#连接环境) · [代码更新](#代码与运行数据) · [USB 夹爪](#usb-接在-olares) · [摄像头](#usb-摄像头预览)

本页供安装、更新和排障时查阅。
日常操作见 [交互终端](terminal-control.md) 或 [网页控制面板](control-panel.md)。
本页命令在本地项目或 Olares 宿主机运行，各节会标明位置。

## 连接环境

客户端保持 LarePass VPN 连接，Olares 服务地址为 `100.64.0.1`。

### 机器人地址与端口

网页面板和交互终端默认读取项目根目录下的 `config/robot.toml`：

```toml
[robot]
host = "192.168.1.6"
```

修改配置后，重新启动服务或终端，才会使用新地址。
网页服务可用 `--robot-host` 指定地址。
CLI 的 `--host` 和独立教学脚本保持各自的地址设置方式。

Olares 直连网口使用 `192.168.1.10/24`。
运行控制程序的主机须能连接机器人的 `29999`、`30003`、`30004` 三个 TCP 端口。

## 面板连接锁

网页面板和交互终端在真机连接前取得文件锁。
网页的 `read-only` 模式也使用此锁。
另一个程序持锁时，新连接返回 `Robot controlled by another panel`。
正常断开后，面板等待反馈线程和命令任务结束，再释放锁。
停止状态无法确认时，面板停止常规坐标查询，保留反馈和锁。
面板重载不能清除未确认的停止状态。

所有进程必须配置相同的机械臂地址。
`MG400_CONTROLLER_LOCK_DIR` 必须使用绝对路径，指向同一共享目录。
所有进程都必须有该目录的读写权限，并使用相同的数字用户 ID（UID）。
共享目录和锁文件的属主必须为 `1000:1000`。
目录权限为 `0700`，锁文件权限为 `0600`。
该目录必须支持操作系统文件锁，不能放在各自独立的容器临时目录中。
Olares 宿主机使用以下目录：

```text
/olares/userdata/Cache/pvc-appcache-qumusphusis-aah2phqnttncsxoh/isaaclab/documents/mg400-data/controller-locks
```

应用配置前，先停止机械臂并正常关闭原面板。
在网页服务的 `EnvironmentFile`（`/opt/mg400-web-control/.env`）中设置 `MG400_CONTROLLER_LOCK_DIR`。
交互终端的 Olares 启动脚本已设置同一目录。

网页服务使用 `mg400-panel` 用户和组，数字 ID 均为 `1000`。
将以下配置写入宿主机的 `/etc/systemd/system/mg400-web-panel.service.d/50-controller-ownership.conf`：

```ini
[Service]
User=1000
Group=1000
```

网页服务用户必须能进入代码目录并读取代码和配置。
`/opt/mg400-web-control` 使用属主 `root:1000` 和权限 `0750`。
服务用户必须能读取反馈文件，并写入运行数据的 `work/` 目录。
`.env` 由 systemd 读取，可以仅允许 root 读取。

运行 `systemctl daemon-reload`。
然后启动网页服务。
如果已有锁文件属主不同，原地修改属主，保留原文件。
不要删除锁文件来解除占用。进程退出时，操作系统释放该进程持有的锁。
进程退出不确认机械臂已停止。
DobotStudio 和其他主机不受此锁限制，仍须关闭其他控制程序。

## 代码与运行数据

下文的 `$DOCS` 表示以下宿主机目录。运行本页命令前，先在宿主机终端设置：

```sh
DOCS=/olares/userdata/Cache/pvc-appcache-qumusphusis-aah2phqnttncsxoh/isaaclab/documents
```

| 内容 | 宿主机路径 |
| --- | --- |
| 网页服务代码 | `/opt/mg400-web-control` |
| 交互终端代码 | `$DOCS/mg400-terminal` |
| 夹爪采集代码（Git 工作副本） | `$DOCS/mg400-isaac` |
| 运行数据 | `$DOCS/mg400-data` |

`$DOCS` 位于 IsaacLab 应用的存储中。
卸载该应用前，先迁移以上目录，再更新服务配置和本页路径。

代码通过 GitHub 管理，Olares 运行选定提交。
`apps/`、`config/`、`src/` 和 `scripts/` 随同一提交更新，不要只覆盖其中的几个脚本。
更新后，重启对应服务。

Olares 未配置 GitHub 读取凭据时，通过 Git bundle 导入目标版本。
在本地项目根目录生成包含完整 Git 历史的 bundle，并记录目标提交：

```sh
git bundle create /tmp/mg400-deploy.bundle --all
git rev-parse HEAD
```

通过已登录的 Olares 网页会话，将 bundle 传到 `$DOCS/mg400-data/mg400-deploy.bundle`。
在宿主机查看工作副本：

```sh
git -C "$DOCS/mg400-isaac" status --short
```

如果有未提交修改，先提交或另存改动，再切换版本。
将下列 `COMMIT_SHA` 替换为本地记录的目标提交：

```sh
git -C "$DOCS/mg400-isaac" bundle verify "$DOCS/mg400-data/mg400-deploy.bundle"
git -C "$DOCS/mg400-isaac" fetch "$DOCS/mg400-data/mg400-deploy.bundle" 'refs/heads/*:refs/remotes/bundle/*'
git -C "$DOCS/mg400-isaac" switch --detach COMMIT_SHA
git -C "$DOCS/mg400-isaac" rev-parse HEAD
```

`origin` 地址为 `https://github.com/shc0507/dobot_mg400.git`。
该私有仓库要求访问凭据，有获授权的 GitHub 只读访问后，可以改从 `origin` 获取提交。

USB 测量文件存放于 `$DOCS/mg400-data/work/`。
代码目录下的 `work` 使用相对链接 `../mg400-data/work`，供采集服务使用。

## USB 接在 Olares

转换器的 USB 身份为 `0403:6001`，序列号为 `BG02Q5TN`。
主机稳定路径为 `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0`。
`ttyUSB` 编号可能变化，采集脚本按完整 USB 身份选择设备。
不要用扩展坞上的其他串口替代。

### 安装与启动采集

在 Control Hub 的 Olares 主机终端安装采集服务。
先进入 `$DOCS/mg400-isaac` 目录。
安装前，停止已确认身份的独立采集进程。
以 root 身份运行一次：

```sh
python3 scripts/install_gripper_collector.py
```

默认数据目录为仓库旁的 `mg400-data`，可用 `--data-dir` 指定。
安装器启用 `mg400-gripper-collector.path`，监听共享文件 `work/gripper_collector.start`。
更新该文件时，主机启动 `mg400-gripper-collector.service`。
服务使用 `--commands`，运行中的采集不会重启。
采集服务启动只读状态，收到面板请求后才执行初始化或位置命令。
关闭面板后，采集继续运行。

网页面板和交互终端不会触发采集启动。
使用前，在 Olares 主机终端运行：

```sh
systemctl start mg400-gripper-collector.service
```

网页操作见 [网页控制面板](control-panel.md)。
省略 `--commands` 时，采集进程只读反馈，夹爪目标输入不可用。
旧版采集仍可提供测量值，但面板禁止位置命令。
更新代码后，在主机重启采集服务，再重新打开面板。
面板与采集进程必须位于同一 Olares 主机，并共享同一单调时钟域。
位置命令不支持跨主机传递，也不支持带时钟偏移的时间命名空间。
命令文件和锁的格式见 [USB 夹爪协议](development.md#usb-夹爪协议)。

脚本每 50 ms 读取三个状态寄存器，并原子替换最新测量文件。
文件权限为 `0644`，路径为 `$DOCS/mg400-data/work/gripper_olares_feedback.json`。
串口被占用、设备缺失或读取失败时，脚本退出。
服务在异常退出后等待 2 秒，再尝试启动，不限制重试次数。
USB 恢复连接后，采集自动恢复，不必重新打开面板。
每次启动使用新会话，不重放旧命令，也不清除命令锁定。
若断线发生在命令执行期间，控制可能锁定，须先核实夹爪状态。
手动停止服务后，服务不会自行重启。
主机重启后，监听服务自动恢复，采集服务须手动启动。
若已安装旧版服务，重新运行安装器，再重启采集服务以应用新配置。
查看日志或手动停止采集：

```sh
journalctl -u mg400-gripper-collector.service -n 50 --no-pager
systemctl stop mg400-gripper-collector.service
```

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
systemctl start mg400-gripper-collector.service
```

## USB 摄像头预览

摄像头预览运行在 Olares 主机上。
服务文件存放于 `/opt/mg400-camera-preview`，重启主机后保留。
服务手动启动，未设置开机自启。

在 Control Hub → Terminal → Olares 中运行：

```sh
systemctl start mg400-camera-preview
cat /opt/mg400-camera-preview/preview.url
```

连接 LarePass VPN 后，在浏览器打开输出的完整链接。
链接包含访问密钥，服务重启后保持不变。
密钥存放于 `preview.key`，仅 root 可以读取。
关闭终端不会停止预览服务。

查询状态或停止预览：

```sh
systemctl status mg400-camera-preview mg400-camera-capture --no-pager
systemctl stop mg400-camera-preview
```

停止预览时，摄像头采集服务也会停止。
若启动失败，运行 `journalctl -u mg400-camera-preview -u mg400-camera-capture -n 30 --no-pager`。

网页监听 VPN 地址 `100.64.0.1:1985`。
Olares 的 VPN ACL（端口访问规则）必须允许端口 `1985`。
go2rtc `1.9.14` 仅监听主机回环地址 `127.0.0.1:1984`。
三台 AC410 按 USB 接口路径绑定，预览分辨率为 `640 × 480`。
每路使用一条连续 MJPEG 视频流，最高每秒 30 帧。
画面下方显示实际解码尺寸和更新帧率。实际帧率受摄像头、网络和浏览器性能影响。
服务端和浏览器只保留最新待处理帧，网络变慢时丢弃积压帧。
服务端发送超时为 1.5 秒。
连续 1.5 秒未收到完整帧时，网页重连并暂留上一帧。
断流或显示帧自接收起满 1.5 秒时，网页标注“画面已暂停 · 非实时”。
上一帧自接收起最多保留 5 秒，重连不会延长该时限。
切换到其他标签页时暂停预览并清图，返回预览页后恢复。
保持摄像头连接原 USB 接口。
更换接口后，更新 `/opt/mg400-camera-preview/go2rtc.yaml` 中的设备路径。

### 摄像头故障

三路同时断流时，先用 `lsusb -t` 和 `ls -l /dev/video*` 检查设备是否仍被识别。
服务显示 `active` 只说明进程存活。取图返回 `200` 时，还须确认图片非空且能解码。
若集线器和摄像头都未被识别，重启预览服务不能代替 USB 连接检查。

若摄像头已被识别，但预览帧率很低，在 Olares 主机运行
`curl -s http://127.0.0.1:1984/api/preload`。
确认输出包含 `camera1`、`camera2`、`camera3`。
若有缺失，运行 `systemctl restart mg400-camera-preview`，等待 2 秒后再次查询。
采集服务启动时若设备缺失，预加载可能未建立。
网页的视频流会自动重连。保留的 `camera1.jpg`、`camera2.jpg`、`camera3.jpg` 单帧接口在没有持续采集时可能较慢。

提高分辨率后，须同时检查三路画面，并验证连续多张 JPEG 能完整解码。
浏览器显示分辨率和帧率不能排除花屏或部分图像损坏。
若出现这类问题，恢复 `640 × 480` 配置，再检查 USB 采集链路。

摄像头与夹爪 USB 转换器共用外接集线器。
恢复共同上游 USB 端口前，停止 `mg400-gripper-collector.path` 和 `mg400-gripper-collector.service`。
操作后恢复监听和采集服务。不要把 USB 重新连接视为允许机械臂或夹爪运动。

源码位于 `scripts/camera_preview.py`，部署时使用文件名 `preview.py`。
将 `scripts/camera_preview.js` 同时部署为同目录的 `preview.js`。
采集配置和服务定义位于 `config/camera_preview/`。
服务定义安装于主机的 `/etc/systemd/system/`。
