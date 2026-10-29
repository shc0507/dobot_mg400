# 主机部署

[项目首页](../README.md) · [安装](#安装与更新) · [配置](#配置文件) · [日常命令](#日常命令) · [连接锁](#面板连接锁) · [迁移](#迁移到新主机)

本页说明如何在 Linux 主机上安装、更新和运行本项目的服务。
部署不依赖特定平台。Olares 的网络、代码获取和 USB 排障见 [Olares 配置与维护](olares.md)。

## 部署内容

| 路径 | 内容 | 属主与权限 |
| --- | --- | --- |
| `/opt/mg400` | Git 工作副本和 Python 环境 `.venv` | `root:1000`，`0750` |
| `/etc/mg400/mg400.env` | 本主机的配置和访问密钥 | `root:root`，`0600` |
| `/etc/mg400/go2rtc.yaml` | 摄像头采集配置，由安装器生成 | `root:1000`，`0640` |
| `/var/lib/mg400/locks` | 机械臂连接锁 | `1000:1000`，`0700` |
| `/var/lib/mg400/work` | 夹爪反馈、夹爪命令文件和模式切换备份 | `1000:1000`，`0750` |
| `/var/lib/mg400/camera` | 摄像头预览密钥 `preview.key` | `1000:1000`，`0700` |
| `/usr/local/lib/mg400/go2rtc` | 摄像头采集程序 go2rtc 1.9.14 | `root:root`，`0755` |
| `/usr/local/bin/mg400ctl` | 日常命令，链接到 `/opt/mg400/deploy/mg400ctl` | 无 |

所有服务以数字 ID `1000` 的用户和组运行。
服务定义位于 `config/systemd/`，安装到 `/etc/systemd/system/`：

| 服务 | `mg400ctl` 名称 | 用途 | 附加组 |
| --- | --- | --- | --- |
| `mg400-web-panel.service` | `web` | [网页面板](control-panel.md)，端口 `8766` | 无 |
| `mg400-stage-panel.service` | `stage` | [位移台面板](stage-panel.md)，端口 `8767` | `dialout` |
| `mg400-camera-preview.service` | `camera` | 摄像头预览网页，端口 `1985` | 无 |
| `mg400-camera-capture.service` | `camera` | go2rtc 采集，只监听 `127.0.0.1:1984` | `video` |
| `mg400-gripper-collector.service` | `collector` | 夹爪 USB 采集 | `dialout` |
| `mg400-gripper-collector.path` | 无 | 面板请求时启动夹爪采集 | 无 |

服务手动启动，不开机自启。
主机重启后，只有夹爪采集的请求监听自动恢复。

## 前提条件

- 主机运行 Linux 和 systemd。
- `/usr/bin/python3` 为 Python 3.11 或更高版本。
- 主机已安装 Git、`setpriv` 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
- 主机能访问 PyPI。`uv sync` 从 PyPI 下载依赖。
- 主机有一个网口直连机械臂，地址为 `192.168.1.10/24`。
- 该网口能连接机械臂的 TCP `29999`、`30003` 和 `30004`。
- 使用摄像头时，从 [go2rtc 发布页](https://github.com/AlexxIT/go2rtc/releases/tag/v1.9.14) 下载 1.9.14 版的 Linux 程序。

## 安装与更新

以 root 身份运行本节命令。

1. 获取代码。首次安装时运行：

   ```sh
   git clone https://github.com/shc0507/dobot_mg400.git /opt/mg400
   ```

   更新时，切换到目标提交：

   ```sh
   git -C /opt/mg400 fetch origin
   git -C /opt/mg400 switch --detach origin/main
   ```

   主机不能访问 GitHub 时，用 Git bundle 获取代码，见 [Olares 获取代码](olares.md#获取代码)。

2. 运行安装器：

   ```sh
   /usr/bin/python3 /opt/mg400/deploy/install.py
   ```

   使用摄像头时，首次安装加 `--go2rtc <go2rtc 程序路径>`。
   加 `--dry-run` 时，安装器只列出步骤，不修改主机。

3. 首次安装后，按 [配置文件](#配置文件) 修改配置。
4. 启动或重启服务，见 [日常命令](#日常命令)。

安装器按以下顺序工作：

1. 检查 Python 版本、uv 和独立运行的夹爪采集进程。检查不通过时，安装器不修改主机。
2. 创建配置文件，或补全缺少的配置项。空的 `*_TOKEN` 项填入新生成的访问密钥。已有的值不变。
3. 创建 `/var/lib/mg400` 下的目录和摄像头预览密钥。
4. 运行 `uv sync`，在 `/opt/mg400/.venv` 安装位移台和工具依赖。
5. 配置了摄像头时，安装 go2rtc 并生成 `go2rtc.yaml`。
6. 安装服务定义，重新加载 systemd，启用夹爪采集的请求监听。
7. 链接 `mg400ctl`。

安装器不启动、不停止，也不重启服务。
正在运行的服务继续使用旧代码，直到重启。
更新后，重启需要更新的服务，再重新加载已打开的面板页面。
不要在 `/opt/mg400` 修改文件。修改的文件可能让 `git switch` 中止更新。

## 配置文件

`/etc/mg400/mg400.env` 保存本主机的全部配置。
服务启动时读取该文件。修改后，重启对应服务。
值为空的项使用默认值。

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `MG400_ROBOT_HOST` | `config/robot.toml` 中的地址 | 机械臂地址 |
| `MG400_CONTROLLER_LOCK_DIR` | `/var/lib/mg400/locks` | [面板连接锁](#面板连接锁) 的目录 |
| `MG400_PANEL_BIND` | `127.0.0.1` | 网页面板的监听地址 |
| `MG400_PANEL_PORT` | `8766` | 网页面板的端口 |
| `MG400_PANEL_TOKEN` | 安装器生成 | 网页面板的访问密钥 |
| `MG400_CAMERA_BIND` | `127.0.0.1` | 摄像头预览的监听地址 |
| `MG400_CAMERA_DEVICES` | 空 | 三个摄像头设备，用逗号分隔。为空时不配置摄像头 |
| `MG400_STAGE_BIND` | `127.0.0.1` | 位移台面板的监听地址 |
| `MG400_STAGE_PORT` | `8767` | 位移台面板的端口 |
| `MG400_STAGE_TOKEN` | 安装器生成 | 位移台面板的访问密钥 |
| `MG400_STAGE_SERIAL` | 空 | Arduino 的串口路径，见 [位移台配置项](stage-panel.md#配置项) |

面板监听回环地址以外的地址时，访问密钥必须至少有 24 个字符。
摄像头设备使用 `/dev/v4l/by-path/` 下的路径。该路径按 USB 接口区分三台同型号摄像头。
摄像头预览固定显示三路画面。
修改 `MG400_CAMERA_DEVICES` 后，重新运行安装器，生成新的 `go2rtc.yaml`，再重启摄像头。

## 日常命令

以 root 身份运行 `mg400ctl`，例如 `sudo mg400ctl status`：

| 命令 | 作用 |
| --- | --- |
| `mg400ctl status` | 显示所有服务的状态 |
| `mg400ctl start web stage camera` | 启动服务。名称为 `web`、`stage`、`camera`、`collector` 或 `all` |
| `mg400ctl stop all` | 停止服务 |
| `mg400ctl restart web` | 重启服务 |
| `mg400ctl logs web -n 100` | 显示服务日志。加 `-f` 持续显示 |
| `mg400ctl terminal` | 打开 [交互终端](terminal-control.md) |
| `mg400ctl mode` | 读取控制器模式，见 [切换 TCP/IP 模式](hardware.md#从服务器切换-tcpip-模式) |
| `mg400ctl camera-url` | 显示含访问密钥的摄像头预览链接 |
| `mg400ctl token web` | 显示网页面板的访问密钥。`token stage` 显示位移台面板的密钥 |

`terminal` 和 `mode` 以 UID 1000 运行，并读取配置文件，与面板共用连接锁。
停止或重启网页面板时，面板最长用约 60 秒确认机械臂停止。
夹爪采集不必手动启动。网页面板或交互终端连接机械臂时，请求启动采集。

## 面板连接锁

网页面板、交互终端和模式切换在连接机械臂前取得文件锁。
网页的只读模式也使用此锁。
另一个程序持锁时，新连接返回 `Robot controlled by another panel`。
正常断开后，面板等待反馈线程和命令任务结束，再释放锁。
停止状态无法确认时，面板停止常规坐标查询，保留反馈和锁。
重新加载面板页面不能清除未确认的停止状态。

所有程序必须使用相同的机械臂地址和 `MG400_CONTROLLER_LOCK_DIR`。
锁目录必须使用绝对路径，并支持操作系统文件锁。
所有程序以 UID 1000 运行。锁文件权限为 `0600`。
以 root 运行时，程序拒绝使用属主不是 root 的锁目录。
已有锁文件的属主不同时，原地修改属主，保留原文件。

不要删除锁文件来解除占用。进程退出时，操作系统释放该进程持有的锁。
进程退出不能确认机械臂已停止。
此锁不限制 DobotStudio 和其他主机，使用前须关闭其他控制程序。

## 迁移到新主机

1. 在旧主机运行 `mg400ctl stop all`。
2. 在新主机按 [前提条件](#前提条件) 准备主机，再按 [安装与更新](#安装与更新) 安装。
3. 把旧主机的 `/etc/mg400/mg400.env` 复制到新主机，再按新主机修改监听地址和摄像头设备。
   保留访问密钥时，用户不必重新输入密钥。
4. 要保留摄像头预览链接，把旧主机的 `/var/lib/mg400/camera/preview.key` 复制到新主机的同一路径，属主为 `1000:1000`。
5. 修改摄像头设备后，重新运行安装器。
6. 连接机械臂网口和 USB 设备，再启动服务。
