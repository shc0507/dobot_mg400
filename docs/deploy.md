[English](deploy.en.md)

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
| `/var/lib/mg400` | 运行数据的上级目录 | `root:1000`，`0750` |
| `/var/lib/mg400/locks` | 机械臂连接锁 | `1000:1000`，`0700` |
| `/var/lib/mg400/work` | 夹爪反馈、夹爪命令文件和模式切换备份 | `1000:1000`，`0750` |
| `/var/lib/mg400/models` | 按资源清单安装的模型权重，文件权限为 `0640` 或 `0600` | `1000:1000`，`0750` |
| `/var/lib/mg400/camera` | 摄像头预览密钥 `preview.key` | `1000:1000`，`0700` |
| `/var/lib/mg400/furnace` | 炉控状态和持久锁，文件为 `0600` | `1000:1000`，`0750` |
| `/usr/local/lib/mg400/go2rtc` | 摄像头采集程序 go2rtc 1.9.14 | `root:root`，`0755` |
| `/usr/local/bin/mg400ctl` | 日常命令，链接到 `/opt/mg400/deploy/mg400ctl` | 无 |
| `/usr/local/bin/labctl` | 同一命令的另一个链接，参数和行为相同 | 无 |

所有服务以数字 ID `1000` 的用户和组运行。
服务定义位于 `config/systemd/`，安装到 `/etc/systemd/system/`：

| 服务 | `mg400ctl` 名称 | 用途 | 附加组 |
| --- | --- | --- | --- |
| `mg400-web-panel.service` | `web` | [网页面板](control-panel.md)，端口 `8766` | 无 |
| `mg400-stage-panel.service` | `stage` | [位移台面板](stage-panel.md)，端口 `8767` | `dialout` |
| `mg400-camera-preview.service` | `camera` | 摄像头预览网页，端口 `1985` | 无 |
| `mg400-camera-capture.service` | `camera` | go2rtc 采集，只监听 `127.0.0.1:1984` | `video` |
| `mg400-gripper-collector.service` | `collector` | 夹爪 USB 采集 | `dialout` |
| `mg400-furnace.service` | `furnace` | 可选的[炉温与气体网页](furnace.md)，端口 `8769` | `dialout` |
| `mg400-gripper-collector.path` | 无 | 面板请求时启动夹爪采集 | 无 |

服务手动启动，不开机自启。
主机重启后，只有夹爪采集的请求监听自动恢复。

## 前提条件

- 主机运行 Linux 和 systemd。
- `/usr/bin/python3` 为 Python 3.12 或更高版本。
- 主机已安装 Git、`setpriv` 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
- 主机能访问 PyPI。`uv sync` 从 PyPI 下载依赖。
- 主机有一个网口直连机械臂，地址为 `192.168.1.10/24`。
- 该网口能连接机械臂的 TCP `29999`、`30003` 和 `30004`。
- 使用摄像头时，从 [go2rtc 发布页](https://github.com/AlexxIT/go2rtc/releases/tag/v1.9.14) 下载 1.9.14 版的 Linux 程序。

## 安装与更新

以 root 身份运行本节命令。
更新前，先停止服务：`mg400ctl stop all`。

1. 获取代码。首次安装时运行：

   ```sh
   git clone https://github.com/shc0507/dobot_mg400.git /opt/mg400
   ```

   更新时，切换到目标提交：

   ```sh
   git -C /opt/mg400 fetch origin
   git -C /opt/mg400 switch --detach origin/main
   ```

   主机不能访问 GitHub 时，用 Git bundle 获取代码，见 [Olares 无法访问 GitHub 时](olares.md#无法访问-github-时)。

2. 运行安装器：

   ```sh
   /usr/bin/python3 /opt/mg400/deploy/install.py
   ```

   使用摄像头时，首次安装加 `--go2rtc <go2rtc 程序路径>`。
   加 `--dry-run` 时，安装器只列出步骤，不修改主机。

3. 首次安装后，按 [配置文件](#配置文件) 修改配置。
4. 启动或重启服务，见 [日常命令](#日常命令)。

安装器按以下顺序工作：

1. 检查 Python 版本、uv、配置文件和独立运行的夹爪采集进程。
   同时检查服务清单、服务选择、所选单元和 udev 模板。
   如指定资源，还检查资源清单、目标路径和本地文件。检查不通过时，安装器不修改主机。
2. 创建配置文件，或补全缺少的配置项。空的 `*_TOKEN` 项填入新生成的访问密钥。已有的值不变。
3. 创建 `/var/lib/mg400` 下的目录和摄像头预览密钥。
   安装器拒绝其中的符号链接，也不覆盖已有的密钥。
4. 按服务清单运行 `uv sync`，只安装已启用服务的依赖组。
   默认在 `/opt/mg400/.venv` 安装位移台和工具依赖。
5. 安装显式指定的外部资源，先校验大小和 SHA-256，再原子替换目标。
6. 配置了摄像头时，安装 go2rtc 并生成 `go2rtc.yaml`。未配置时，删除旧的 `go2rtc.yaml`。
7. 如有所选服务的 udev 模板，渲染并安装规则，再重新加载规则并触发 USB 设备的 udev 事件。
8. 安装所选服务定义，重新加载 systemd，启用夹爪采集的请求监听。
9. 链接 `mg400ctl` 和 `labctl`。

安装器不启动、不停止，也不重启服务。
更新代码前必须停止服务。运行中的服务可能读取新旧混合的文件。
更新后，启动服务，再重新加载已打开的面板页面。
不要在 `/opt/mg400` 修改文件。修改的文件可能让 `git switch` 中止更新。

## 服务清单

`deploy/services.toml` 保存每个已实现服务的 `[[service]]` 条目。
安装器、`mg400ctl` 和 `labctl` 共用此清单。
新增服务时，把条目与对应单元文件一起加入仓库。

| 字段 | 含义 |
| --- | --- |
| `name` | 命令使用的服务名称 |
| `units` | systemd 单元列表。启动第 1 项，其依赖关系启动其余单元 |
| `optional` | 可选服务为 `true`，必需服务为 `false` |
| `venv` | Python 环境，限 `.venv` 或 `.venv-vision` |
| `groups` | 此服务使用的 uv 依赖组 |
| `token` | 共享配置中的密钥名称，空字符串表示无此项 |
| `port` | 主 TCP 端口，0 表示无 TCP 端口 |
| `assets` | 此服务拥有的资源清单名称 |

`web`、`stage`、`camera` 和 `collector` 始终为必需服务。
`MG400_OPTIONAL_SERVICES` 只接受清单中可选服务的名称，用逗号分隔。
默认值为空。未知、重复、空项或必需服务名称会使安装器拒绝安装。
可选服务 `furnace` 使用 `furnace` 依赖组。
真机使用前，配置串口路径和操作员提供的气体映射。
设置 `MG400_OPTIONAL_SERVICES=furnace` 后运行安装器，再明确运行 `mg400ctl start furnace`。
安装器不会启动炉控服务。

安装器按 `venv` 合并已启用服务的 `groups`，每个环境只同步 1 次。
同步显式使用 `--no-default-groups`，不安装项目的默认依赖组。
必需服务使用 `stage` 和 `tools`，环境为 `/opt/mg400/.venv`。
选择 `.venv-vision` 的服务使用 `/opt/mg400/.venv-vision`。
安装器显式设置 `UV_PROJECT_ENVIRONMENT`，不采用调用者指定的其他环境。

如服务有 udev 规则，模板路径为 `config/udev/70-mg400-<name>.rules.in`。
模板必须含 VID、PID、`MODE="0660"` 和 `@SERVICE_GROUP@`。
安装器把占位符替换为 GID 1000 的组名。
目标为 `/etc/udev/rules.d/70-mg400-<name>.rules`，所有者为 `root:root`，权限为 `0644`。
只有已启用服务的模板会安装。试运行只列出规则安装、重新加载和 USB 设备 udev 事件的触发步骤。

删除可选服务名称前，先停止该服务。
删除名称不会停止进程，也不会删除已有的文件或规则。
`start`、`restart` 和 `status` 只使用已启用服务。
`stop` 和 `logs` 仍可访问清单中已停用的服务，`stop all` 包含全部已登记服务。

## 外部资源

`config/assets.toml` 为每项外部资源保存 1 个表。
字段为 `url`、`sha256`、`bytes`、`install`、`mode`、`service`、`source` 和 `license`。
`install` 是 `/var/lib/mg400/` 下的绝对目标路径。`mode` 只能是 `0640` 或 `0600`。
资源文件和目标目录归 `1000:1000` 所有，目标目录权限为 `0750`。
安装器拒绝源文件、目标文件或路径中的符号链接。

首项 `vision-weights` 对应本仓库 `vision-model-v1` 发布中的 `chip-arm-yolo26s-seg-v1.pt`。
大小为 23,393,117 字节，SHA-256 为 `0b684c470191540b0cfc8802b0d13149b47d7be76b901ad1dd59882297407a07`。
目标为 `/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt`，权限为 `0640`。
清单按 Roboflow 声明记录许可证 `AGPL-3.0`。
权重发布必须等待仓库许可证决定。发布中没有文件时，下载会失败。

安装器默认不下载或安装资源。资源必须属于已启用服务，并列在该服务的 `assets` 中。
`vision-weights` 只有在 `vision` 服务条目加入清单并启用后才能安装。
此前，`--fetch-assets` 跳过该资源，显式 `--asset NAME=PATH` 会拒绝安装。
显式下载已启用服务的资源：

```sh
/usr/bin/python3 /opt/mg400/deploy/install.py --fetch-assets
```

下载只使用 HTTPS，包括重定向。
安装器把内容写入目标目录的临时文件，再校验字节数和 SHA-256。
校验通过并设置所有者和权限后，安装器原子替换目标文件。
下载、校验或替换前的权限设置失败时，原文件不变。

离线安装时，按 [Olares 无法访问 GitHub 时](olares.md#无法访问-github-时) 的 Drive 传文件流程，把权重复制到主机。
本地文件路径不必与目标路径相同。启用 `vision` 后，以 `/tmp/chip-arm-yolo26s-seg-v1.pt` 为例：

```sh
/usr/bin/python3 /opt/mg400/deploy/install.py --asset vision-weights=/tmp/chip-arm-yolo26s-seg-v1.pt
```

`--asset NAME=PATH` 可重复使用，每项资源只能指定 1 次。
与 `--fetch-assets` 同时使用时，同名资源采用本地文件。
本地文件与下载文件执行相同的大小和 SHA-256 校验，不得修改清单来绕过校验。
加 `--dry-run` 只显示计划步骤，不下载、不写入，也不修改权限。
试运行仍校验指定的本地文件，不能确认远程资源是否可用。

运行 `mg400ctl assets` 可检查清单中的每项资源：

- `present`：内容、大小、所有者和权限符合清单。
- `missing`：目标文件不存在。
- `mismatch`：内容或文件属性不匹配、路径不安全，或文件无法读取。

全部为 `present` 时退出码为 0，否则为 1。
此命令不下载资源、不修改文件，也不启动服务。

## 配置文件

`/etc/mg400/mg400.env` 保存本主机的全部配置。
服务启动时读取该文件。修改后，重启对应服务。
值为空的项使用默认值，`MG400_CONTROLLER_LOCK_DIR` 除外。
除下述炉控 JSON 配置外，值不得含内部引号或反斜杠。
安装器和 `mg400ctl` 拒绝 systemd 可能读成其他值的行。
只有 `MG400_FURNACE_GAS_MAP` 和 `MG400_FURNACE_IDLE_GAS` 接受单引号包围的 JSON 对象。
内部双引号原样保留。转义、撇号、控制字符、重复键和非有限数字均被拒绝。
语法及工艺限制见[炉控配置](furnace.md#配置)。
每次运行时，安装器把该文件恢复为 `root:root` 和 `0600`。

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `MG400_OPTIONAL_SERVICES` | 空 | 服务清单中的可选服务名称，用逗号分隔 |
| `MG400_ROBOT_HOST` | `config/robot.toml` 中的地址 | 机械臂地址 |
| `MG400_CONTROLLER_LOCK_DIR` | 必填，示例为 `/var/lib/mg400/locks` | [面板连接锁](#面板连接锁) 的目录，必须为绝对路径 |
| `MG400_PANEL_BIND` | `127.0.0.1` | 网页面板的监听地址 |
| `MG400_PANEL_PORT` | `8766` | 网页面板的端口 |
| `MG400_PANEL_TOKEN` | 安装器生成 | 网页面板的访问密钥 |
| `MG400_CAMERA_BIND` | `127.0.0.1` | 摄像头预览的监听地址 |
| `MG400_CAMERA_DEVICES` | 空 | 3 个摄像头设备，用逗号分隔。为空时不配置摄像头 |
| `MG400_STAGE_BIND` | `127.0.0.1` | 位移台面板的监听地址 |
| `MG400_STAGE_PORT` | `8767` | 位移台面板的端口 |
| `MG400_STAGE_TOKEN` | 安装器生成 | 位移台面板的访问密钥 |
| `MG400_STAGE_SERIAL` | 空 | Arduino 的串口路径，见 [位移台配置项](stage-panel.md#配置项) |
| `MG400_FURNACE_BIND` | `127.0.0.1` | 炉控监听地址 |
| `MG400_FURNACE_PORT` | `8769` | 炉控端口 |
| `MG400_FURNACE_TOKEN` | 安装器生成 | 炉控访问密钥 |
| `MG400_FURNACE_SERIAL` | 空 | 炉控串口设备 |
| `MG400_MFC_SERIAL` | 空 | 独立气控串口设备 |

面板监听回环地址以外的地址时，访问密钥必须至少有 24 个字符。
摄像头设备使用 `/dev/v4l/by-path/` 下的路径。该路径按 USB 接口区分 3 台同型号摄像头。
摄像头预览固定显示 3 路画面。
修改 `MG400_CAMERA_DEVICES` 后，重新运行安装器，生成新的 `go2rtc.yaml`，再重启摄像头。

## 日常命令

以 root 身份运行 `mg400ctl`，例如 `sudo mg400ctl status`。
`labctl` 使用相同的参数和行为：

| 命令 | 作用 |
| --- | --- |
| `mg400ctl backup FILE.tar` | 私密备份主机配置和数据，排除进程锁 |
| `mg400ctl assets` | 检查外部资源的内容、所有者和权限 |
| `mg400ctl status` | 显示所有服务的状态 |
| `mg400ctl start web stage camera` | 启动服务。名称为 `web`、`stage`、`camera`、`collector`、`furnace` 或 `all` |
| `mg400ctl stop all` | 停止服务 |
| `mg400ctl restart web` | 重启服务 |
| `mg400ctl logs web -n 100` | 显示服务日志。加 `-f` 持续显示 |
| `mg400ctl terminal` | 打开 [交互终端](terminal-control.md) |
| `mg400ctl mode` | 读取控制器模式，见 [切换 TCP/IP 模式](hardware.md#从服务器切换-tcpip-模式) |
| `mg400ctl camera-url` | 显示含访问密钥的摄像头预览链接 |
| `mg400ctl camera-check` | 依次检查摄像头设备、服务和采集流，见 [摄像头故障](olares.md#摄像头故障) |
| `mg400ctl token web` | 显示网页面板的访问密钥。`token stage` 显示位移台面板的密钥 |

炉控 `stop` 和 `restart` 在修改任何所选服务前，先认证并读取实时状态。
检查覆盖混合列表及 `all`，也覆盖 `stop all` 选中的已停用炉控服务。
活动序列或冷却气体计划必须使用 `--force`。状态不可用、格式错误、断开或锁定时也必须使用 `--force`。
使用 `mg400ctl stop furnace --force` 或 `mg400ctl restart furnace --force` 前，必须与所有客户端协调。
强制关闭请求服务停止加热和气体。必须核对停止结果。检查仅作提示性保护：状态读取与 systemd 操作之间仍可能启动序列。
检查使用配置的 IP 监听地址、端口和基础路径。它接受 `localhost`，将 `0.0.0.0` 映射为 `127.0.0.1`，将 `::` 映射为 `::1`。
密钥只在 bearer 标头中发送，不使用代理或重定向。
套接字操作在 `2` s 后超时。每次收到数据块后检查 `2` s 的响应体截止时间。
标头和传输分块格式读取使用套接字超时，因此这不是整个请求的总时限。
超过 `262144` 字节的响应会被拒绝。

`terminal` 和 `mode` 用 `runuser --pty` 以 UID 1000 在独立的伪终端中运行。
它们读取配置文件，与面板共用连接锁，但不接收访问密钥。
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

## 备份

`mg400ctl backup FILE.tar` 归档 `/etc/mg400/mg400.env` 和 `/var/lib/mg400`，保留数字属主和文件权限。
它只排除存放进程连接锁的 `/var/lib/mg400/locks`。
`/var/lib/mg400/work` 下的持久夹爪命令锁仍会备份，包括 `gripper_feedback.json.command-lock.json`。
它也保留 `/var/lib/mg400/furnace` 下的炉控状态和 `.state.json.lock`。

备份前，按下方迁移步骤停止服务、夹爪请求监听器和其他数据写入程序。
备份命令不会代为停止它们。
输入缺失、符号链接、特殊文件、目录循环或读取时检测到变化，都会导致备份失败。
修复原因后再试。不要为通过备份而删除安全锁。

选择已存在、由 root 拥有的目标目录，组成员和其他用户不得有写权限。
目标必须在备份源路径之外，路径中不得有符号链接或 `..` 部分。
命令拒绝覆盖任何已有路径，包括断开的符号链接。
命令先写入私密临时归档，完整写入后才生成目标文件。

归档属主为 `root:root`，权限为 `0600`。
其中包含访问令牌和摄像头预览密钥。
存储或传输时保留这些权限，不要提交到 Git 或公开发布。

## 迁移到新主机

以下路径存在时，备份会包含它们：

| 路径 | 内容 |
| --- | --- |
| `/var/lib/mg400/models/` | 已安装的模型权重 |
| `/var/lib/mg400/calibration/cameras/` | 按相机路径保存的相机到机械臂单应矩阵 |
| `/var/lib/mg400/calibration/microscope/` | 物镜转盘、自动对焦和参考视图数据 |
| `/var/lib/mg400/stations.toml` | 示教机械臂工位 |
| `/var/lib/mg400/tool.toml` | 夹爪工具偏移 |
| `/var/lib/mg400/stage.toml` | 位移台每毫米步数、软限位、命名位置和位姿 |
| `/var/lib/mg400/thermal/rois.toml` | 命名热像区域 |
| `/var/lib/mg400/furnace/state.json` | 炉程序状态 |
| `/var/lib/mg400/runs/` | 运行文件和 SQLite 索引 |
| `/var/lib/mg400/recordings/` | 归档前的炉视频 |
| `/var/lib/mg400/camera/preview.key` | 摄像头预览密钥 |
| `/var/lib/mg400/work/` | 夹爪反馈、命令文件、持久命令锁和模式切换备份 |

在新主机上手动恢复：

1. 先让旧主机上的实验安全结束。停止服务和夹爪采集请求监听器：

   ```sh
   mg400ctl stop all
   systemctl disable --now mg400-gripper-collector.path
   ```

   继续前，停止其他写入主机数据的进程。
2. 在旧主机创建备份：

   ```sh
   mg400ctl backup /root/mg400-host.tar
   ```

3. 安全地把归档传到新主机的 `/root/mg400-host.tar`。保留属主 `root:root` 和权限 `0600`。
4. 按[前提条件](#前提条件)准备新主机，再按[安装与更新](#安装与更新)安装。
   恢复期间保持硬件断开。恢复数据前，停止新安装的服务和请求监听器：

   ```sh
   mg400ctl stop all
   systemctl disable --now mg400-gripper-collector.path
   ```

5. 只恢复本命令生成的可信归档。先检查归档成员：

   ```sh
   tar --list --verbose --file /root/mg400-host.tar
   ```

   成员必须使用 `etc/mg400/mg400.env` 和 `var/lib/mg400` 的相对路径，不得含 `..`、链接、特殊文件或进程锁子目录。
   确认新主机的恢复路径中没有符号链接。
   恢复数字属主和权限：

   ```sh
   tar --extract --file /root/mg400-host.tar --directory / --numeric-owner --same-owner --same-permissions
   ```

6. 按新主机的监听地址和摄像头设备路径修改 `/etc/mg400/mg400.env`。保留属主 `root:root` 和权限 `0600`。
   保留访问令牌后，用户不必重新输入。恢复 `/var/lib/mg400/camera/preview.key` 后，预览链接不变。
   上述数据路径的属主为 `1000:1000`，目录权限为 `0750`，文件权限为 `0640` 或 `0600`。
   保留摄像头目录原有的更严格权限 `0700`，以及归档中其他已有数据的属主和权限。
7. 如果存在 `/var/lib/mg400/work/gripper_feedback.json.command-lock.json`，说明夹爪命令结果未确认。
   检查夹爪状态，并保留此文件，属主为 `1000:1000`，权限为 `0600`。
   采集服务读到恢复的文件后保持锁定。不要删除它来恢复命令执行。
   启动服务前，按仪器实际状态核对待执行命令和已保存的运行／炉状态。恢复操作不得重放命令。
8. 修改摄像头设备后，再次运行安装器。
   运动前，核对恢复的工位、工具偏移、位移台配置和校准映射是否符合硬件。
9. 连接机械臂网络接口和 USB 设备，再按正常操作流程启动所选服务。
