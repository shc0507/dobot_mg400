[English](deploy.en.md)

# 主机部署

[项目首页](../README.md) · [安装](#安装与更新) · [配置](#配置文件) · [日常命令](#日常命令) · [连接锁](#面板连接锁) · [迁移](#迁移到新主机)

本页说明如何在 Linux 主机上安装、更新和运行本项目的服务。
部署不依赖特定平台。Olares 的网络、代码获取和 USB 排障见 [Olares 配置与维护](olares.md)。

## 部署内容

| 路径 | 内容 | 属主与权限 |
| --- | --- | --- |
| `/opt/mg400` | Git 工作副本和 Python 环境 `.venv`、可选的 `.venv-vision` | `root:1000`，`0750` |
| `/etc/mg400/mg400.env` | 本主机的硬件配置 | `root:root`，`0600` |
| `/etc/mg400/go2rtc.yaml` | 摄像头采集配置，由安装器生成 | `root:1000`，`0640` |
| `/var/lib/mg400` | 运行数据的上级目录 | `root:1000`，`0750` |
| `/var/lib/mg400/locks` | 机械臂连接锁 | `1000:1000`，`0700` |
| `/var/lib/mg400/work` | 模式切换备份 | `1000:1000`，`0750` |
| `/var/lib/mg400/robot` | `tool.toml` 中的实测工具偏移与 `stations.toml` 中的示教工位，文件为 `0600` 或 `0640` | `1000:1000`，`0750` |
| `/var/lib/mg400/vision` | 启用视觉服务时创建的数据目录 | `1000:1000`，`0750` |
| `/var/lib/mg400/models` | 按资源清单安装的模型权重，文件权限为 `0640` 或 `0600` | `1000:1000`，`0750` |
| `/var/lib/mg400/stage` | `stage.toml` 中可写的位移台校准和相机参考 | `1000:1000`，`0700` |
| `/var/lib/mg400/calibration` | 仪器校准数据的父目录 | `1000:1000`，`0750` |
| `/var/lib/mg400/calibration/cameras` | 相机到机械臂的校准 JSON 和操作者示教参考，文件为 `0600` 或 `0640` | `1000:1000`，`0700` |
| `/var/lib/mg400/calibration/microscope` | 选中显微镜服务所用的已导入校准 | `1000:1000`，`0750` |
| `/var/lib/mg400/recordings` | 校验归档副本前保留的本地显微镜录像 | `1000:1000`，`0750` |
| `/var/lib/mg400/thermal` | `rois.toml` 中的命名热像区域，文件权限 `0640` 或 `0600` | `1000:1000`, `0750` |
| `/var/lib/mg400/furnace` | 炉控状态和持久锁，文件为 `0600` | `1000:1000`，`0750` |
| `/var/lib/mg400/runs` | 编排器记录和显微镜运行输出，文件为 `0600` 或 `0640` | `1000:1000`，`0750` |
| `/var/lib/mg400/zones` | 区域配置和保留占用，文件为 `0640` 或 `0600` | `1000:1000`，`0750` |
| `/usr/local/lib/mg400/go2rtc` | 摄像头采集程序 go2rtc 1.9.14 | `root:root`，`0755` |
| `/usr/local/lib/mg400/libtoupcam.so` | 可选的 Toupcam 59.30594.20260120 Linux x64 库 | `root:root`，`0755` |
| `/usr/local/bin/mg400ctl` | 日常命令，链接到 `/opt/mg400/deploy/mg400ctl` | 无 |
| `/usr/local/bin/labctl` | 同一命令的另一个链接，参数和行为相同 | 无 |

所有服务以数字 ID `1000` 的用户和组运行。
服务定义位于 `config/systemd/`，安装到 `/etc/systemd/system/`：

| 服务 | `mg400ctl` 名称 | 用途 | 附加组 |
| --- | --- | --- | --- |
| `mg400-web-panel.service` | `web` | [机器人服务](development.md#机器人服务接口)，端口 `8766` | 无 |
| `mg400-stage-panel.service` | `stage` | [位移台服务](stage-panel.md)，端口 `8767` | `dialout` |
| `mg400-camera-preview.service` | `camera` | 摄像头视频流，端口 `1985` | 无 |
| `mg400-camera-capture.service` | `camera` | go2rtc 采集和 H.264 编码，只监听 `127.0.0.1:1984` 和 `127.0.0.1:8554` | `video` |
| `mg400-thermal.service` | `thermal` | 可选的[热像仪服务](thermal.md)，端口 `8768` | 无 |
| `mg400-furnace.service` | `furnace` | [炉温与气体服务](furnace.md)，端口 `8769` | `dialout` |
| `mg400-microscope.service` | `microscope` | [显微镜服务](microscope.md)，回环端口 `8770` | `dialout video` |
| `mg400-vision.service` | `vision` | 可选的[视觉服务](vision.md)，回环端口 `8771` | 无 |
| `mg400-orchestrator.service` | `orch` | [配方服务](orchestrator.md#http-服务)，端口 `8772`，控制台控制套接字端口 `8774` | 无 |
| `mg400-gateway.service` | `gateway` | 可选的[MCP 网关](agent-gateway.md)，回环端口 `8773` | 无 |
| `mg400-proxy.service` | `proxy` | [单端口代理](olares.md#单端口代理)，端口 `8080` | 无 |

服务手动启动，不开机自启。

## 前提条件

- 主机运行 Linux 和 systemd。
- `/usr/bin/python3` 为 Python 3.12 或更高版本。
- 主机已安装 Git、`setpriv` 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
- 主机能访问 PyPI。`uv sync` 从 PyPI 下载依赖。
- 主机有一个网口直连机械臂，地址为 `192.168.1.10/24`。
- 该网口能连接机械臂的 TCP `29999`、`30003` 和 `30004`。
- 使用摄像头时，从 [go2rtc 发布页](https://github.com/AlexxIT/go2rtc/releases/tag/v1.9.14) 下载 1.9.14 版的 Linux 程序。
- 使用显微镜录像或摄像头 H.264 预览时，主机已安装 `ffmpeg` 和 `ffprobe`。在 Debian 或 Ubuntu 上，`ffmpeg` 软件包提供这两个程序：`apt-get install ffmpeg`。

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
   /usr/bin/python3 /opt/mg400/deploy/install.py --caddy /path/to/caddy
   ```

   首次安装时，`--caddy` 指定 Caddy 2 程序。之后安装器复用已安装的程序。
   使用摄像头时，首次安装加 `--go2rtc <go2rtc 程序路径>`。
   使用 Toupcam 时，加 `--toupcam PATH`，指定下文说明的已校验库文件。
   加 `--dry-run` 时，安装器只列出步骤，不修改主机。

3. 首次安装后，按 [配置文件](#配置文件) 修改配置。
4. 启动或重启服务，见 [日常命令](#日常命令)。

安装器按以下顺序工作：

1. 检查 Python 版本、uv 和配置文件。
   同时检查服务清单、服务选择、所选单元和 udev 模板。
   如指定 `--toupcam`，检查源文件、目标路径和固定的 SHA-256。
   如指定资源或启用 `vision`，还检查资源清单、目标路径和本地文件。
   启用 `microscope` 时，在 systemd 服务的默认 `PATH` 中查找 `ffmpeg` 和 `ffprobe`。配置了摄像头时，查找 `ffmpeg`。缺少时，只给出提示，继续安装。
   检查不通过时，安装器不修改主机。
2. 创建配置文件，或补全缺少的配置项。非空值保持不变。
   删除旧的服务 `*_TOKEN` 项（`MG400_GATEWAY_TOKEN` 除外），以及示例中没有的空项。
3. 创建 `/var/lib/mg400` 下的目录。安装器拒绝其中的符号链接。
4. 按服务清单运行 `uv sync`，只安装已启用服务的依赖组。
   默认在 `/opt/mg400/.venv` 安装位移台和工具依赖。
5. 安装指定的外部资源。启用 `vision` 时，同时安装必需的权重。先校验大小和 SHA-256，再原子替换目标。
6. 配置了摄像头时，安装 go2rtc 并生成 `go2rtc.yaml`。未配置时，删除旧的 `go2rtc.yaml`。
   如指定 `--toupcam`，安装第 1 步已校验的库文件内容。
7. 如有所选服务的 udev 模板，渲染并安装规则，再重新加载规则并触发 USB 设备的 udev 事件。
8. 安装所选服务定义，重新加载 systemd。
9. 链接 `mg400ctl` 和 `labctl`。

安装器不启动、不停止，也不重启服务。
更新代码前必须停止服务。运行中的服务可能读取新旧混合的文件。
更新后，启动服务，再重新加载已打开的实验室控制台。
不要在 `/opt/mg400` 修改文件。修改的文件可能让 `git switch` 中止更新。

## Toupcam 库

从 [Toupcam 59.30594.20260120 SDK 压缩包](https://www.touptekphotonics.com/downloads/software/toupcamsdk.20260120.zip)提取 `linux/x64/libtoupcam.so`。
压缩包和库文件都放在 Git 仓库外。把提取出的库文件路径传给安装器：

```sh
/usr/bin/python3 /opt/mg400/deploy/install.py --toupcam /path/to/linux/x64/libtoupcam.so
```

要求的 SHA-256 为 `5ba51b66bb9e2901e769fa1d5d5ae88c359269ea7fb20358335462c531a3e997`。
校验和不匹配、路径含符号链接或文件不是普通文件时，安装器拒绝安装，不修改主机。
安装器把已校验的内容原子写入 `/usr/local/lib/mg400/libtoupcam.so`，属主为 `root:root`，权限为 `0755`。
安装器不加载库，也不打开摄像头。加 `--dry-run` 可校验并列出步骤，不修改文件。
未指定 `--toupcam` 时，已有库文件保持不变。

没有此库时，安装器打印警告，安装不含摄像头的显微镜服务。
已安装的库必须哈希匹配，且属主和权限安全。
修改主机前，安装器拒绝不可信的目标目录。
现有目标目录须归 root 所有，允许 UID `1000` 遍历，且不得允许服务或其他用户写入。
显微镜服务独占此摄像头，并负责炉控录像。其单元设置 `Environment=LD_LIBRARY_PATH=/usr/local/lib/mg400`。
已有的 `config/udev/70-mg400-microscope.rules.in` 模板以 `0660` 权限，向服务组开放 C3CMOS05100KPA 的 USB3 `0547:1375` 和 USB2 `0547:1376` 设备。
安装器为显微镜服务渲染此模板。
不要安装厂商的 `99-toupcam.rules`：它以 `0666` 权限，向所有用户开放厂商 `0547` 和 `04b4` 的设备。

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
| `port` | 主 TCP 端口，0 表示无 TCP 端口 |
| `assets` | 此服务拥有的资源清单名称 |

`web`、`stage`、`camera`、`furnace` 和 `microscope` 始终为必需服务。
`MG400_OPTIONAL_SERVICES` 接受清单中的服务名称，用逗号分隔。
默认值为空。未知、重复或空项会使安装器拒绝安装。必需服务名称不起作用。
可选服务 `thermal` 使用 `/opt/mg400/.venv` 中的 `thermal` 依赖组。
把 `thermal` 加入 `MG400_OPTIONAL_SERVICES`，保留其他选项，然后运行安装程序。
安装程序安装热像仪 USB 访问规则，并以 `1000:1000`、`0750` 创建 `/var/lib/mg400/thermal`。
它不创建区域坐标，也不启动摄像头。命名区域的配置见[热像仪说明](thermal.md)。
由所有者执行 `mg400ctl start thermal` 启动服务。服务启动时自动打开摄像头。

用 `mg400ctl logs thermal` 查看日志，用 `mg400ctl stop thermal` 停止服务。
先关闭其他摄像头程序。P1 摄像头只能由 1 个进程独占。

服务 `furnace` 使用 `furnace` 依赖组。
真机使用前配置串口路径，再明确运行 `mg400ctl start furnace`。
安装器不会启动炉控服务。

服务 `microscope` 在 `/opt/mg400/.venv` 中使用 `microscope` 依赖组。
配置[显微镜](microscope.md#主机托管服务)。
安装器准备默认的校准、录像和运行目录，不替换已导入的数据。
所有者先启动 `camera`，再启动 `microscope`。安装器不启动这两项服务。

可选服务 `vision` 在独立的 `/opt/mg400/.venv-vision` 环境中使用 `vision` 和 `vision-gpu` 依赖组。
把 `vision` 加入 `MG400_OPTIONAL_SERVICES`，保留其他选择。运行安装器。
安装器准备 `/var/lib/mg400/vision`，属主为 `1000:1000`，权限为 `0750`，并安装已校验的 `vision-weights` 资源。
未选中 `vision` 时，不准备该环境、目录、资源或单元。

首次安装、运行 `mg400ctl start vision` 和在 Olares 检查 `mg400ctl gpu` 均由所有者执行。
运行配置见[视觉服务](vision.md)，访问规则见 [Olares 网络](olares.md#网络)。

服务 `orch` 在 `/opt/mg400/.venv` 中使用 `orchestrator` 依赖组。
此组含 Flask、waitress，以及自有演示服务所需的 `stage`、`thermal` 和 `furnace` 组。
安装器准备 `runs` 和 `zones` 目录。它不会生成区域配置，也不会启动服务。
同一服务还提供[实验室控制台](dashboard.md#实验室控制台)。它是唯一的操作网页，含 Overview、Motion、Cameras 和 Furnace + microscope 工作区，以及独立的运动、加热和气体停止操作。
控制台没有配方运行控制。启动、终止和恢复运行使用[配方服务接口](orchestrator.md#http-服务)。

可选服务 `gateway` 使用 `gateway` 依赖组。这部分工作已推迟，见[智能体网关](agent-gateway.md)。
选中 `gateway` 时，安装器也会准备 `runs` 目录，用于审计记录。安装器不会启动网关。
每个 MCP 请求都必须携带 `MG400_GATEWAY_TOKEN`。安装器不生成它，须在配置文件中手动设置。

服务 `proxy` 不增加 Python 依赖组。首次安装时通过 `--caddy PATH` 提供 Caddy 2，并配置[单端口入口](olares.md#单端口代理)。
安装器在改文件前检查代理配置，不会启动代理。

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
systemd 报告某单元未安装且未运行时，`stop all` 跳过该单元并打印单元名称。

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
发布中没有文件时，下载会失败。

资源必须属于已启用服务，并列在该服务的 `assets` 中。
启用 `vision` 时，安装必须包含 `vision-weights`，即使未指定 `--fetch-assets`。
用 `--asset vision-weights=PATH` 提供通过校验的本地文件，或让安装器下载清单中的资源。
停用 `vision` 时，`--fetch-assets` 跳过其权重，显式 `--asset vision-weights=PATH` 拒绝安装。
其他资源必须显式指定。下载已启用服务的资源：

```sh
/usr/bin/python3 /opt/mg400/deploy/install.py --fetch-assets
```

已安装文件的大小、SHA-256、所有者和权限都与清单一致时，安装器保留该文件，不再下载。
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
值为空的项使用默认值。
除下述炉控 JSON 配置外，值不得含内部引号或反斜杠。
安装器和 `mg400ctl` 拒绝 systemd 可能读成其他值的行。
只有 `MG400_FURNACE_GAS_MAP` 和 `MG400_FURNACE_IDLE_GAS` 接受单引号包围的 JSON 对象。
内部双引号原样保留。转义、撇号、控制字符、重复键和非有限数字均被拒绝。
语法及工艺限制见[炉控配置](furnace.md#配置)。
每次运行时，安装器把该文件恢复为 `root:root` 和 `0600`。

该文件只含硬件事实。监听地址、端口、基础路径和服务 URL 是代码和单元中的固定默认值。
所有服务监听 `127.0.0.1`。VPN 上只开放代理端口 `8080`。VPN 是访问边界，服务不检查令牌。
机械臂连接锁目录为 `/var/lib/mg400/locks`。实验室控制台使用基础路径 `/lab/`。

| 配置项 | 默认值 | 用途 |
| --- | --- | --- |
| `MG400_PROXY_BIND` | `127.0.0.1` | 代理监听地址，使用数字形式 |
| `MG400_PROXY_PORT` | `8080` | 代理端口，范围为 `1024` 到 `65535` |
| `MG400_OPTIONAL_SERVICES` | 空 | 服务清单中的可选服务名称，用逗号分隔 |
| `MG400_ROBOT_HOST` | `config/robot.toml` 中的地址 | 机械臂地址 |
| `MG400_ROBOT_MAX_SPEED_L`、`MG400_ROBOT_MAX_ACC_L` | `1` | 机械臂速度上限，整数百分比，范围为 `1` 到 `100` |
| `MG400_ORCH_TOOL_FILE` | `/var/lib/mg400/robot/tool.toml` | 实测工具偏移 |
| `MG400_CAMERA_DEVICES` | 空 | 1 到 4 个不同的摄像头设备，用逗号分隔。为空时关闭摄像头 |
| `MG400_CAMERA_MODES` | 空 | 每台设备对应 1 个允许的 `WIDTHxHEIGHT@FPS` 模式，顺序与设备相同 |
| `MG400_STAGE_SERIAL` | 空 | Arduino 的串口路径，见 [位移台配置项](stage-panel.md#配置项) |
| `MG400_VISION_WEIGHTS` | `/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt` | 已校验的本地模型权重 |
| `MG400_VISION_DEVICE` | 空 | CUDA 可用时选 `cuda`，否则选 `cpu` |
| `MG400_FURNACE_SERIAL` | 空 | 炉控串口设备 |
| `MG400_MFC_SERIAL` | 空 | 独立气控串口设备 |

按[炉控配置](furnace.md#配置)配置炉温上限和气体映射。
按[显微镜指南](microscope.md#契约服务)配置设备选择、调焦限位和相机视频流。

摄像头使用 `/dev/v4l/by-path/` 下不同的设备路径，按 USB 接口区分同型号摄像头。
摄像头服务只提供已配置的视频流，按设备顺序命名为 `camera1` 到 `camera4`。
模式列表为空时，普通摄像头使用 `1280x720@30`。配置的 `MG400_MICROSCOPE_SIDE_STREAM` 使用 `3840x2160@30`。
显式模式列表覆盖这些默认值。数量、路径或模式无效时，安装器在修改主机前退出。
只预加载宽度不超过 1280 且高度不超过 720 的流。更大的流按需启动。
多台摄像头使用大于 `640x480` 的模式时，先安装 [USB 带宽补丁](olares.md#usb-带宽补丁)。
修改设备或模式后，重新运行安装器，生成新的 `go2rtc.yaml`，再运行 `mg400ctl restart camera vision microscope orch`。
允许的模式和托盘摄像头配置见[摄像头](olares.md#摄像头)。

## 日常命令

以 root 身份运行 `mg400ctl`，例如 `sudo mg400ctl status`。
`labctl` 使用相同的参数和行为：

| 命令 | 作用 |
| --- | --- |
| `mg400ctl backup FILE.tar` | 私密备份主机配置和数据，排除进程锁 |
| `mg400ctl assets` | 检查外部资源的内容、所有者和权限 |
| `mg400ctl gpu` | 以 UID `1000` 读取 GPU 名称、已用内存和总内存 |
| `mg400ctl status` | 显示所有服务的状态 |
| `mg400ctl start web stage camera` | 启动服务。名称为 `web`、`stage`、`camera`、`thermal`、`furnace`、`microscope`、`vision`、`orch`、`gateway`、`proxy` 或 `all` |
| `mg400ctl stop all` | 停止服务 |
| `mg400ctl restart web` | 重启服务 |
| `mg400ctl logs web -n 100` | 显示服务日志。加 `-f` 持续显示 |
| `mg400ctl terminal` | 打开 [交互终端](terminal-control.md) |
| `mg400ctl mode` | 读取控制器模式，见 [切换 TCP/IP 模式](hardware.md#从服务器切换-tcpip-模式) |
| `mg400ctl camera-url` | 为每台摄像头显示 1 个 `/camera/cameraN.mjpeg` 链接。启用 `proxy` 时，链接使用代理地址 |
| `mg400ctl camera-check` | 依次检查摄像头设备、服务和采集流，见 [摄像头故障](olares.md#摄像头故障) |

`gpu` 以 UID `1000` 运行 `nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv`。
此诊断只读状态，不启动服务、不加载模型，也不修改 GPU 配置。
该命令不接收主机配置。
GPU 输出不能单独证明视觉服务选择了 CUDA。请检查 `mg400ctl logs vision` 和服务状态。

炉控 `stop` 和 `restart` 在修改任何所选服务前，先读取实时状态。
检查覆盖混合列表及 `all`，也覆盖 `stop all` 选中的已停用炉控服务。
仅当 systemd 报告全部炉控单元未安装且未运行时，`stop` 才跳过此检查。systemd 未给出明确结果时，仍执行检查。
活动序列或冷却气体计划必须使用 `--force`。状态不可用、格式错误、断开或锁定时也必须使用 `--force`。
使用 `mg400ctl stop furnace --force` 或 `mg400ctl restart furnace --force` 前，必须与所有客户端协调。
强制关闭请求服务停止加热和气体。必须核对停止结果。检查仅作提示性保护：状态读取与 systemd 操作之间仍可能启动序列。
检查使用配置的 IP 监听地址、端口和基础路径。它接受 `localhost`，将 `0.0.0.0` 映射为 `127.0.0.1`，将 `::` 映射为 `::1`。
检查不使用代理或重定向。
套接字操作在 `2` s 后超时。每次收到数据块后检查 `2` s 的响应体截止时间。
标头和传输分块格式读取使用套接字超时，因此这不是整个请求的总时限。
超过 `262144` 字节的响应会被拒绝。

停止 `orch` 时，服务请求暂停，等待已接受的操作结束，记录最终状态，再释放服务租约。
单元使用 `TimeoutStopSec=infinity`，systemd 会等待清理完成，不另设停止期限。
重启服务不会恢复已保存的运行，也不会清除保留区域。请使用[恢复流程](orchestrator.md#http-服务)。

`terminal` 和 `mode` 用 `runuser --pty` 以 UID 1000 在独立的伪终端中运行。
它们读取配置文件，与机器人服务共用连接锁。
停止或重启机器人服务时，服务最长用约 60 秒确认机械臂停止。

## 面板连接锁

机器人服务、交互终端和模式切换在连接机械臂前取得文件锁。
机器人服务的只读模式也使用此锁。
另一个程序持锁时，新连接返回 `Robot controlled by another panel`。
正常断开后，机器人服务等待反馈线程和命令任务结束，再释放锁。
停止状态无法确认时，机器人服务停止常规坐标查询，保留反馈和锁，并显示警告。
检查机械臂，再点击 Recover。重新加载实验室控制台不会清除警告。

所有程序必须使用相同的机械臂地址和锁目录 `/var/lib/mg400/locks`。
单元和 `mg400ctl` 设置此目录。它必须支持操作系统文件锁。
所有程序以 UID 1000 运行。锁文件权限为 `0600`。
以 root 运行时，程序拒绝使用属主不是 root 的锁目录。
已有锁文件的属主不同时，原地修改属主，保留原文件。

不要删除锁文件来解除占用。进程退出时，操作系统释放该进程持有的锁。
进程退出不能确认机械臂已停止。
此锁不限制 DobotStudio 和其他主机，使用前须关闭其他控制程序。

## 备份

`mg400ctl backup FILE.tar` 归档 `/etc/mg400/mg400.env` 和 `/var/lib/mg400`，保留数字属主和文件权限。
它只排除存放进程连接锁的 `/var/lib/mg400/locks`。
它也保留 `/var/lib/mg400/furnace` 下的炉控状态和 `.state.json.lock`。

备份前，按下方迁移步骤停止服务和其他数据写入程序。
备份命令不会代为停止它们。
输入缺失、符号链接、特殊文件、目录循环或读取时检测到变化，都会导致备份失败。
修复原因后再试。不要为通过备份而删除安全锁。

选择已存在、由 root 拥有的目标目录，组成员和其他用户不得有写权限。
目标必须在备份源路径之外，路径中不得有符号链接或 `..` 部分。
命令拒绝覆盖任何已有路径，包括断开的符号链接。
命令先写入私密临时归档，完整写入后才生成目标文件。

归档属主为 `root:root`，权限为 `0600`。
其中包含主机配置和数据。
存储或传输时保留这些权限，不要提交到 Git 或公开发布。

## 迁移到新主机

以下路径存在时，备份会包含它们：

| 路径 | 内容 |
| --- | --- |
| `/var/lib/mg400/models/` | 已安装的模型权重 |
| `/var/lib/mg400/calibration/cameras/` | 按相机路径保存的相机到机械臂单应矩阵 |
| `/var/lib/mg400/calibration/microscope/` | 物镜转盘、自动对焦和参考视图数据 |
| `/var/lib/mg400/robot/stations.toml` | 示教机械臂工位 |
| `/var/lib/mg400/robot/tool.toml` | 吸盘工具偏移 |
| `/var/lib/mg400/stage/stage.toml` | 位移台每毫米步数、软限位、命名位置和位姿 |
| `/var/lib/mg400/thermal/rois.toml` | 命名热像区域 |
| `/var/lib/mg400/furnace/state.json` | 炉程序状态 |
| `/var/lib/mg400/runs/` | 运行文件和 SQLite 索引 |
| `/var/lib/mg400/recordings/` | 归档前的炉视频 |
| `/var/lib/mg400/work/` | 模式切换备份 |

安装器创建 `/var/lib/mg400/robot`，属主为 `1000:1000`，模式为 `0750`，用于工具和工位数据。
若 `/var/lib/mg400/tool.toml` 或 `/var/lib/mg400/stations.toml` 存在，安装器只打印移动提示，不修改该文件。
停止使用机器人数据的服务和 CLI 进程后，再手动将各旧文件移入 `/var/lib/mg400/robot`。
不得覆盖已有目标。保留 `1000:1000` 属主以及 `0600` 或 `0640` 文件模式。
安装器保留已有的 `MG400_ORCH_TOOL_FILE` 值。重新启动使用这些数据的进程前，更新旧的覆盖配置。

安装器创建 `/var/lib/mg400/stage`，所有者为 `1000:1000`，权限为 `0700`，用于写入位移台校准。
父目录 `/var/lib/mg400` 仍由 root 拥有，权限为 `0750`。
若 `/var/lib/mg400/stage.toml` 存在，安装器只打印移动提示，不修改文件。
停止位移台服务后，手动将旧文件移到 `/var/lib/mg400/stage/stage.toml`，不得覆盖已有目标。
保留所有者 `1000:1000` 和文件权限 `0600` 或 `0640`。重启前更新旧的 `MG400_STAGE_CONFIG` 覆盖配置。

在新主机上手动恢复：

1. 先让旧主机上的实验安全结束。停止服务：

   ```sh
   mg400ctl stop all
   ```

   继续前，停止其他写入主机数据的进程。
2. 在旧主机创建备份：

   ```sh
   mg400ctl backup /root/mg400-host.tar
   ```

3. 安全地把归档传到新主机的 `/root/mg400-host.tar`。保留属主 `root:root` 和权限 `0600`。
4. 按[前提条件](#前提条件)准备新主机，再按[安装与更新](#安装与更新)安装。
   恢复期间保持硬件断开。恢复数据前，停止新安装的服务：

   ```sh
   mg400ctl stop all
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

6. 按新主机的摄像头设备路径修改 `/etc/mg400/mg400.env`。保留属主 `root:root` 和权限 `0600`。
   上述数据路径的属主为 `1000:1000`，文件权限为 `0640` 或 `0600`。
   目录权限保留 `0750`，但摄像头、位移台和 `calibration/cameras` 目录必须为 `0700`。保留归档中其他已有数据的属主和权限。
7. 启动服务前，按仪器实际状态核对待执行命令和已保存的运行／炉状态。恢复操作不得重放命令。
8. 修改摄像头设备后，再次运行安装器。
   运动前，核对恢复的工位、工具偏移、位移台配置和校准映射是否符合硬件。
9. 连接机械臂网络接口和 USB 设备，再按正常操作流程启动所选服务。
