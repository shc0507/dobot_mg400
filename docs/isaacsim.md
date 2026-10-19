# Isaac Sim：MG400 + PGEA 夹爪

复用本项目已校核的 CAD 网格、连杆尺寸、惯量和夹爪安装关系，直接导出原生 USD。
不需要 ROS、URDF/MJCF 导入扩展或在线资产转换器。

## 文件

| 文件 | 用途 |
| --- | --- |
| `00_OPEN_MG400.usda` | 日常主入口：机械臂、可镜像夹指、地面、灯光和观察相机 |
| `scripts/start_isaac_olares.sh` | Olares 服务启动入口，复用现有 Isaac Sim 安装 |
| `apps/isaacsim/startup.py` | 打开默认场景，检查真机控制导入，记录代码与运行环境 |
| `isaacsim/workspace_scene.usda` | 真机镜像工作场景，引用机器人并停用物理场景 |
| `isaacsim/mg400_with_gripper.usd` | 自带网格与材质的基础机器人资产，默认 prim 为 `/MG400` |
| `isaacsim/scene.usda` | 基础资产检查场景，含地面、灯光、相机和 PhysicsScene |
| `isaacsim/mg400_mirror.usda` | 增加可镜像夹指的 USD 覆盖层，保留关节坐标和行程 |
| `apps/isaacsim/load_model.py` | 开发时检查基础模型导入 |
| `apps/isaacsim/mirror.py` | 在当前 MG400 场景中显示真机四轴反馈，支持断线重连 |
| `apps/isaacsim/mirror_bootstrap.py` | 必要时打开默认场景，再启动只读姿态镜像 |
| `apps/isaacsim/joint_control.py` | 四轴真机面板：读取当前角度、使能、下使能、返回默认姿态、单次到位 |
| `apps/isaacsim/gripper_control.py` | 实体夹爪面板：MG400 I/O 开合与 USB 位置反馈 |
| `apps/isaacsim/olares_gripper.py` | Olares 夹爪面板入口，读取数据目录中的 USB 测量文件 |
| `config/robot.toml` | Isaac 镜像、四轴面板和夹爪面板共用的机器人地址 |
| `scripts/gripper_feedback.py` | Olares 主机读取 USB 夹爪位置，写入共享测量文件 |
| `scripts/isaac_gripper_feedback.mjs` | Mac 读取 USB 夹爪位置，经当前 WebRTC 会话转发到 Isaac |
| `apps/isaacsim/standalone.py` | 基础资产独立检查入口，使用 5.1 兼容 API，6.x 已弃用这些接口 |
| `scripts/build_isaac_model.py` | 从现有模型重建基础 USD，无需启动 Isaac Sim |
| `scripts/build_isaac_mirror.py` | 生成夹爪镜像覆盖层 |
| `scripts/build_isaac_workspace.py` | 重建默认镜像工作场景和主入口 |
| `isaacsim/LICENSE`、`NOTICE.txt`、`MIRROR_NOTICE.txt` | 原始模型来源、许可证和镜像模型说明 |

模型单位为米，Z 轴向上。基础模型的八个旋转关节与三个固定关节连接 11 个刚体。
PGEA 侧出线版、两个估算指尖和各参考坐标系都随法兰移动。
镜像覆盖层为夹爪壳体和两侧夹指提供独立变换，以及关节坐标和行程。
它不添加夹爪质量、驱动或碰撞，工作场景停用 PhysicsScene。
场景中的机器人为 `/World/MG400`，法兰为 `/World/MG400/mg400_end_effector_flange`。
观察相机为 `/World/Camera`。

## 日常使用：查看真机姿态与打开控制面板

Olares 启动脚本通过 `apps/isaacsim/startup.py` 打开 `00_OPEN_MG400.usda`。
它检查四轴面板、夹爪面板和文件反馈模块能否导入，不创建面板或网络连接。
默认启动不开始物理仿真，也不发送真机命令。
在已有服务中，也可用 **File → Open** 打开 `/root/Documents/mg400-isaac/00_OPEN_MG400.usda`。

时间轴保持 **Stop**。
在 **Window → Script Editor** 运行四轴面板，脚本会启动或复用真机姿态镜像：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/apps/isaacsim/joint_control.py")
```

四轴面板启动只读，连接后显示真机实测姿态。
使能、下使能和运动步骤见 [在 Isaac 中控制真机四轴](#在-isaac-中控制真机四轴)。
如果只显示姿态，运行 `apps/isaacsim/mirror_bootstrap.py`，见 [真机姿态实时同步](#真机姿态实时同步)。
USB 接在 Olares 时，先启动宿主机反馈采集，再在 Script Editor 运行夹爪面板：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/apps/isaacsim/olares_gripper.py")
```

夹爪面板启动只读，点击开合按钮才驱动实体夹爪。
采集和文件反馈步骤见 [USB 接在 Olares](#usb-接在-olares)。
将自定义场景另存到数据目录，操作要求见 [代码与运行数据](#代码与运行数据)。

## Olares / WebRTC

Olares 容器内使用 Isaac Sim `6.1.0-rc.26`，安装目录为 `/isaac-sim`。
内置 Python 版本为 `3.12.13`，继续使用该安装提供的 Kit 和运行依赖。
不要用项目的 `uv sync` 或 pip 依赖覆盖 Kit 的 Python 环境。
客户端保持 LarePass VPN 连接：

- Server：`100.64.0.1`
- Signal：`49100/TCP`
- Stream：`47998/UDP`

### Warp 兼容性

当前 Isaac 安装依赖 Warp `1.16.0` 的 APIC 初始化文件修补。
`/isaac-sim/extscache/omni.warp.core-1.16.0+lx64/warp/_src/apic/__init__.py` 必须为 137 字节常规文件。
该路径不能是符号链接，内容使用 [NVIDIA Warp v1.16.0 源文件](https://raw.githubusercontent.com/NVIDIA/warp/v1.16.0/warp/_src/apic/__init__.py)。
在容器内运行以下校验：

```sh
python3 - <<'PY'
import hashlib
from pathlib import Path

warp_file = Path("/isaac-sim/extscache/omni.warp.core-1.16.0+lx64/warp/_src/apic/__init__.py")
assert warp_file.is_file() and not warp_file.is_symlink()
assert warp_file.stat().st_size == 137
assert hashlib.sha256(warp_file.read_bytes()).hexdigest() == (
    "39b67a3543de12fbe919327958818a53222b223688ef8ece33a12bc1e46d1f14"
)
print("校验通过")
PY
```

更换或重建容器映像后，先重新执行此校验。
修补元数据位于 `/root/Documents/isaac-repairs/warp-1.16.0/original-symlink.json`。
保留 `/root/Documents/isaac-repairs` 及其现有路径。

### WebRTC 客户端自动点击 SOP

使用后台点击脚本，不激活窗口、不移动系统鼠标。
它临时使用已获授权的本机调试接口 `127.0.0.1:9229`，每批结束后关闭。

1. 客户端必须已运行、位于后台且未最小化。用完整路径绑定应用：
   `app = await cua.getApp("/Applications/Isaac Sim WebRTC Streaming Client.app")`。
   本地控件优先用 AX 元素操作，远端视频控件按截图定位。
2. 取得完整窗口截图，记录原始像素尺寸 `width × height` 和目标坐标 `(x, y)`。
   窗口及布局未变时可复用上一批结果图。窗口移动、缩放或布局变化后重新截图。
3. 在项目目录运行（Node.js 22+）：
   `node scripts/isaac_client_click.mjs width height x y [x y ...]`。
   脚本已完成移动和点击，不要再调用 `app.click`。
   使用截图原始坐标，不手动扣标题栏、缩放或按远端分辨率换算。
4. 确定的连续操作可合并，每批最多 8 次。批次结束后读取 AX 并截图确认远端结果。
   遇到未知菜单、弹窗或报错时先观察，不要猜坐标或整批盲目重试。
   已展开的菜单可因悬停切换，避免再次点击标题将其收起。
5. 校准只用普通菜单、标签页，不用机械臂动作按钮，也不用拖动代替鼠标移动。

截图失败时，按完整路径重新绑定应用，读取最新 AX。
仍失败时停止操作，不自动改用 `Raise`、重启或前台移动脚本。
只有用户明确接受切换前台和移动系统鼠标时，才可使用前台方案。

### 服务与场景加载

已有服务运行时直接使用当前客户端，不需要启动第二个 Isaac Sim。
需要重新启动服务时，先正常退出已有 Isaac Sim，再在 Olares 容器终端运行：

```sh
ISAACSIM_HOST=100.64.0.1 /root/Documents/mg400-isaac/scripts/start_isaac_olares.sh
```

容器可能预设内部服务地址，VPN 启动时显式覆盖 `ISAACSIM_HOST`。
启动脚本支持以下环境变量。表中默认值仅在对应变量未设置时使用：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `ISAACSIM_ROOT` | `/isaac-sim` | 现有 Isaac Sim 安装目录 |
| `ISAACSIM_HOST` | `100.64.0.1` | 客户端连接的服务地址 |
| `ISAACSIM_SIGNAL_PORT` | `49100` | WebRTC 信令 TCP 端口 |
| `ISAACSIM_STREAM_PORT` | `47998` | WebRTC 视频 UDP 端口 |
| `MG400_DATA_DIR` | `/root/Documents/mg400-data` | 运行数据根目录 |

默认启动 `apps/isaacsim/startup.py`，打开日常场景并检查真机控制模块能否导入。
该入口不创建面板、不连接机器人，也不发送真机命令。

启动记录写入 `$MG400_DATA_DIR/isaacsim/runtime.json`。
记录包含 Git 提交、未提交修改标志、Isaac/Python 版本、场景和数据目录。
`config_sources` 记录机器人配置路径，以及安装、串流和数据目录环境变量。
核对提交与预期版本一致。
`status` 为 `ready` 表示场景打开成功，真机控制模块通过导入检查。
这不代表真机已连接或动作已通过验证。

显式传入 `--exec` 时，启动脚本只运行指定入口，不再运行默认 `startup.py`。
该方式不会自动生成默认启动入口的 `runtime.json`，各入口仍写入自身状态文件。

### 代码与运行数据

代码通过 GitHub 管理，Olares 运行选定提交。
`/root/Documents/mg400-isaac` 是 Git 工作副本。
`apps/`、`config/`、`src/`、`scripts/`、`isaacsim/` 和 `00_OPEN_MG400.usda` 随同一提交更新。
不要只覆盖其中的几个脚本。
不必上传 `.env` 或 API key，也不必安装模型 API 依赖。

Olares 未配置 GitHub 读取凭据时，通过 Git bundle 导入目标版本。
在本地项目根目录生成包含完整 Git 历史的 bundle，并记录目标提交：

```sh
git bundle create /tmp/mg400-deploy.bundle --all
git rev-parse HEAD
```

通过已登录的 Olares 网页会话，将 bundle 传到 `/root/Documents/mg400-data/mg400-deploy.bundle`。
先正常退出已有 Isaac Sim，再在容器中查看工作副本：

```sh
git -C /root/Documents/mg400-isaac status --short
```

如果有未提交修改，先提交或另存改动，再切换版本。
将下列 `COMMIT_SHA` 替换为本地记录的目标提交：

```sh
git -C /root/Documents/mg400-isaac bundle verify /root/Documents/mg400-data/mg400-deploy.bundle
git -C /root/Documents/mg400-isaac fetch /root/Documents/mg400-data/mg400-deploy.bundle 'refs/heads/*:refs/remotes/bundle/*'
git -C /root/Documents/mg400-isaac switch --detach COMMIT_SHA
git -C /root/Documents/mg400-isaac remote set-url origin https://github.com/shc0507/dobot_mg400.git
git -C /root/Documents/mg400-isaac rev-parse HEAD
```

核对提交后，按上面的 VPN 启动命令运行服务，再核对 `runtime.json`。
`origin` 地址为 `https://github.com/shc0507/dobot_mg400.git`。
该私有仓库要求访问凭据，有获授权的 GitHub 只读访问后，可以改从 `origin` 获取提交。

Isaac 入口通过 `MG400_DATA_DIR` 指定运行数据根目录。
未设置时，默认使用项目的 `work/`，Olares 启动脚本设为 `/root/Documents/mg400-data`。
Isaac 状态 JSON 和场景备份写入数据根目录下的 `isaacsim/`。
USB 测量文件存放于数据根目录下的 `work/`。
Olares 代码目录下的 `work` 使用相对链接 `../mg400-data/work`，供既有宿主机采集路径使用。
下文的 `$MG400_DATA_DIR` 均表示实际使用的数据根目录。

自定义场景另存到数据目录，更新代码前保留当前场景与已有备份。
脚本导出的场景备份保留引用和会话修改，仍依赖原源码资产位置。
如果被引用的外部资产含未保存的修改，脚本会拒绝备份，以免重开快照后恢复旧值。
先保存这些资产，再运行会自动备份的入口；脚本不会自动覆盖源资产。
`src/` 修改后必须完整重启 Isaac Sim。
重新运行面板脚本不保证更新 Python 已加载的模块。
Script Editor、自定义启动脚本和 `--exec` 均使用本页的当前入口。

## 真机地址配置

Isaac 镜像、四轴面板和夹爪面板读取项目根目录下的 `config/robot.toml`：

```toml
[robot]
host = "192.168.1.6"
```

修改配置后，重新启动镜像和面板，才会使用新地址。
四轴面板只复用地址与配置一致的镜像。
如果现存镜像地址不同或没有 `host`，四轴面板会拒绝复用。
先停止旧镜像，再重新运行 `apps/isaacsim/mirror.py` 和四轴面板。
此配置只供 Isaac 真机入口使用，CLI 的 `--host` 和独立教学脚本保持各自的地址设置方式。

## 真机姿态实时同步

只显示真机姿态时，在 Isaac Sim **Script Editor** 中运行：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/apps/isaacsim/mirror_bootstrap.py")
```

入口在当前场景启动 `mirror.py`，没有 MG400 时先加载默认场景。
镜像只接收所配置机器人 TCP 30004 的关节反馈，不发送真机命令。
它先备份当前场景，保留物体位置与夹爪开口，再停止物理仿真。
随后在每次画面更新时应用最新的四轴姿态。
八个旋转关节使用下面的同一套映射；连杆变换只写入 USD session layer，源资产保持不变。
夹爪随法兰移动，但开口保持启动时的值，因为此反馈流没有提供夹爪位置。

**MG400 Live Mirror** 面板显示 `LIVE`、更新频率和反馈年龄。
超过 0.5 秒没有新数据时显示 `STALE / RECONNECTING`，模型停在最后有效姿态，
后台自动重连。点击 **Stop synchronization** 停止同步；再次运行脚本可恢复。
更换场景或启动物理仿真也会停止同步，防止两个更新来源同时控制模型。
运行状态每 0.5 秒写入 `$MG400_DATA_DIR/isaacsim/mirror_status.json`，其中包含反馈数、应用帧数、
真机角度、法兰位置、夹爪保持值与场景备份路径。

需要重启串流并自动同步时，先正常退出旧串流进程，再运行：

```sh
ISAACSIM_HOST=100.64.0.1 /root/Documents/mg400-isaac/scripts/start_isaac_olares.sh --exec /root/Documents/mg400-isaac/apps/isaacsim/mirror_bootstrap.py
```

当前场景已有 `/World/MG400` 时，入口保留该场景。
否则打开项目的 `00_OPEN_MG400.usda`。
显式传入 `SCENE_PATH` 时，先备份当前场景，再加载指定文件。
例如，先将自定义场景保存到下面的路径，再在 Script Editor 中指定该文件：

```python
import runpy

runpy.run_path(
    "/root/Documents/mg400-isaac/apps/isaacsim/mirror_bootstrap.py",
    init_globals={"SCENE_PATH": "/root/Documents/mg400-data/isaacsim/my_scene.usda"},
)
```

加载后创建 session layer 中的 `MirrorCamera`，以完整机械臂视角观察同步。
其他相机的可视图标在 session layer 中隐藏，避免近距离挡住观察画面；相机源定义保留。
面板显示的反馈年龄是 Isaac Sim 收到反馈后的经过时间，不包含客户端视频串流延迟。

Olares 直连网口使用 `192.168.1.10/24`。IsaacLab 容器内直接连接机器人，
不要绑定宿主机的源 IP。命名空间中的 `mg400-direct` Linkerd EgressNetwork
将机器人 `30004` 标为 opaque TCP，避免服务端先发送数据的协议检测等待。

## 在 Isaac 中控制真机四轴

使用 Isaac 自带面板，控制链路在 Olares 内完成：

```text
Isaac 面板 J1–J4（度） → TCP 29999 / 30003 → MG400
Isaac 模型与 Actual 显示 ← TCP 30004 ← MG400 实测角度
```

无需 ROS 2 或额外桥接服务。按 [代码与运行数据](#代码与运行数据) 准备工作副本。
在当前 Isaac 的 **Window → Script Editor** 执行：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/apps/isaacsim/joint_control.py")
```

脚本复用现有实时同步；尚未启动同步时，会通过 mirror bootstrap 备份当前场景、
加载 MG400 并启动反馈。出现 **MG400 Joint Control** 面板后：

1. 等待 `LIVE`。`Actual (deg)` 显示实测角度，`Target (deg)` 首次自动填入实测值。
2. 未使能时，点击第一排左侧的 **Enable robot**。
   此操作仅使能，不发送运动，沿用控制器当前负载设置。
3. 返回默认姿态时，点击第二排的 **Return to Home**。
   默认姿态为 J1–J4 全部 0°。
4. 指定其他姿态时，修改所需轴的绝对目标角度。
   点击第三排的 **Move to Target**。
5. 等待 `Arrival verified`。此状态表示新鲜反馈已在目标 ±0.1° 内稳定至少 0.4 秒，
   并通过 dashboard 回读核对。完成后保持使能，可继续下一次动作。
6. 下使能时，点击第一排右侧的 **Disable robot**。

一次只执行一个目标。执行前，程序检查目标和整段路径的关节范围。
指令采用 `SpeedJ=5,AccJ=5` 比例参数，保持全局速度、工具坐标和夹爪输出不变。

输入框本身不发运动命令。模型始终显示真机实际姿态，不把输入目标当作已到位。
动作期间输入和执行按钮禁用，网络操作在后台线程运行。
**Disable robot** 仍可点击，会取消当前动作并发送 `DisableRobot()`。
此版本只控制四轴，不控制真实夹爪，不支持拖动 USD 连杆直接控制真机。

下使能后，面板用新鲜反馈和 dashboard 回读核实静止且处于未使能状态。
核实成功后，目标角度刷新为实测值。点击 **Enable robot** 即可重新使能，无需重载脚本。
若无法核实，下使能报错并保持操作锁定。

运动时关闭面板、更换场景、启动物理仿真或停止反馈同步，会结束控制并请求停止。
异常不会自动重发动作或自动清除报警。
超过 0.25 秒无新鲜反馈、控制器报警、异常模式或 120 秒内未到位都会中止。
命令应答未知时，即使收到停止指令的应答，也不宣称已确认停止。
若显示 `STOP UNCONFIRMED`，使用现场物理急停。

当前关节范围检查复用项目参考模型，并非现场碰撞检查；执行前应确认路径、夹爪和线缆的空间。
建议首次由现场操作者将 J4 目标改为当前值加 1°，检查真实运动与画面一致后再扩大操作。
打开脚本和只读连接测试不会自动执行这一步。

状态每 0.5 秒写入 `$MG400_DATA_DIR/isaacsim/control_status.json`。
其中包含连接状态、实测值、目标值和操作结果，`mirror_status.json` 继续记录模型同步。
停止现有串流进程后也可用同一脚本自动加载：

```sh
ISAACSIM_HOST=100.64.0.1 /root/Documents/mg400-isaac/scripts/start_isaac_olares.sh --exec /root/Documents/mg400-isaac/apps/isaacsim/joint_control.py
```

Olares 容器内不绑定宿主机源 IP。需能连接所配置机器人的 `29999`、`30003`、
`30004` 三个 TCP 端口；已有仅配置 `30004` 的服务网格规则不能代表命令端口也已连通。
本地离线测试使用模拟控制器覆盖协议、限位、重复点击、反馈失效和停止逻辑；
不能据此宣称真机运动或 Isaac 运行时验证已完成。

面板 API 参考：[NVIDIA omni.ui FloatField](https://docs.omniverse.nvidia.com/kit/docs/omni.ui/latest/omni.ui/omni.ui.FloatField.html)；
四轴协议参考：[Dobot 官方 SDK](https://github.com/Dobot-Arm/TCP-IP-4Axis-Python/blob/main/dobot_api.py)。

## 在 Isaac 中控制实体夹爪

在 Script Editor 运行以下脚本，打开 `MG400 Real Gripper` 面板：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/apps/isaacsim/gripper_control.py")
```

面板启动只读状态。`Open gripper` 写 DO2=0、DO1=0，
`Close gripper` 写 DO2=0、DO1=1。它不使能机械臂，不发送关节命令。
面板通过 MG400 的 TCP 29999 控制 I/O。
启用下述 USB 反馈后，仿真夹指跟随实体位置。

当前接线为 MG400 DO1/DO2 → 夹爪 INPUT1/INPUT2，
夹爪 OUTPUT1/OUTPUT2 → MG400 DI1/DI2，输入和输出均为 PNP。
I/O 组 1 为全开（1000），组 2 为全闭（0），位置单位为行程千分数。
两组力度均为 50%。面板显示原始 DI 值。
只有命令后观察到 DI 从 00 变为 10，才报告到位。
通信失败会锁定后续写入，不自动重发命令。
状态写入 `$MG400_DATA_DIR/isaacsim/real_gripper_status.json`。

断电重启后，自动初始化可能未完成。
当前须保留 USB–RS485 连接作为初始化入口：
从站 1、115200、8N1，写 `0x0100=1`，等待 `0x0200=1`。
该初始化会让夹指张开寻找行程端点。
完成初始化后，开合命令经 MG400 I/O 发送，USB 仅用于独立读取。

### 同步实体夹爪位置

USB 接在 Mac 时，保持 LarePass VPN 和 WebRTC 客户端连接。
Isaac 的仿真时间轴必须处于 Stop 状态，夹爪面板保持打开。
在 Mac 项目根目录启动反馈转发：

```sh
node scripts/isaac_gripper_feedback.mjs start
```

脚本独占 `/dev/cu.usbserial-BG02Q5TN`，以 20 Hz 读取初始化状态、
运动状态和实际位置（`0x0200` 至 `0x0202`）。
它不初始化夹爪，不写控制寄存器。
位置经当前 WebRTC 会话送到 Isaac，不新增网络端口。
面板显示 `Following measured USB position.` 后，模型跟随实体读数。

真实位置 1000 对应全开，0 对应每侧夹指内收 5 mm。
代码只更新两个夹指的 SessionLayer 变换，不启动物理仿真。
这项同步用于显示实体开口，不模拟夹持力或接触。
反馈超过 0.75 秒未更新，或初始化、位置数据无效时，模型保持最后位置。
面板同时显示反馈过期或无效提示。有效反馈恢复后，模型继续同步。
若同时运行四轴镜像，首次更新后重启四轴镜像脚本，避免旧实例覆盖夹指位置。

查询或停止 Mac 反馈：

```sh
node scripts/isaac_gripper_feedback.mjs status
node scripts/isaac_gripper_feedback.mjs stop
```

退出客户端或串口读取失败后，反馈进程停止。
重新连接后再执行 `start`，不自动重启或重新初始化。
反馈进程运行时，其他程序不能使用同一串口。
Mac 状态写入 `work/gripper_feedback_status.json`。

转发脚本适配 Streaming Client 2.0.0 内置的 SDK 3.7.0。
启动时使用临时回环调试接口连接现有 SDK，配置结束即关闭接口。
客户端升级后须重新核对接口，脚本会拒绝其他版本。
消息格式参考 [NVIDIA AppStreamer](https://docs.omniverse.nvidia.com/ov-web-sdk/latest/web-streaming-library/classes/AppStreamer.html)
和 [Kit 消息扩展](https://docs.omniverse.nvidia.com/kit/docs/omni.kit.livestream.messaging/latest/Overview.html)。

#### USB 接在 Olares

转换器的 USB 身份为 `0403:6001`，序列号为 `BG02Q5TN`。
主机稳定路径为 `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0`。
`ttyUSB` 编号可能变化，采集脚本按完整 USB 身份选择设备。
不要用扩展坞上的其他串口替代。

Isaac 容器没有映射串口设备。
在 Control Hub 的 Olares 主机终端运行采集脚本。
先进入 `isaaclab-data` 的 `hostPath` 下的 `documents/mg400-isaac` 目录。
该目录对应容器的 `/root/Documents/mg400-isaac`。

```sh
mkdir -p ../mg400-data/work
python3 scripts/gripper_feedback.py --output ../mg400-data/work/gripper_olares_feedback.json
```

脚本每 50 ms 读取三个状态寄存器，并原子替换最新测量文件。
文件权限为 `0644`，容器可以读取。
容器中的对应路径为 `/root/Documents/mg400-data/work/gripper_olares_feedback.json`。
串口被占用、设备缺失或读取失败时，脚本退出。
按 `Ctrl+C` 停止前台采集，或向确认过身份的后台采集进程发送 `SIGTERM`。
主机重启或 USB 重新连接后，手动重新启动采集。

用仓库启动脚本启动 Isaac 后，在 Script Editor 运行 Olares 文件反馈入口：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/apps/isaacsim/olares_gripper.py")
```

该入口把 `$MG400_DATA_DIR/work/gripper_olares_feedback.json` 传给夹爪面板。
文件反馈与 Mac WebRTC 反馈互斥。
同一文件重复读取不会延长测量值的有效期。
该方式沿用 Stop 时间轴和反馈过期保护，不必重启 Isaac 或新增网络端口。

## 四轴映射与当前范围

| 控制器角度 | USD 关节角度                                                |
| ---------- | ----------------------------------------------------------- |
| J1         | `mg400_j1 = J1`                                             |
| J2         | `mg400_j2_1 = J2`、`mg400_j2_2 = J2`、`mg400_j3_2 = -J2`    |
| J3         | `mg400_j3_1 = J3-J2`、`mg400_j4_1 = -J3`、`mg400_j4_2 = J3` |
| J4         | `mg400_j5 = J4`                                             |

USD 旋转关节限位和 drive target 单位为度，Isaac articulation API 的角度为弧度。
脚本按名字匹配关节，不依赖 PhysX 内部排列。应通过该四轴映射同时设置八关节；
单独拖动一个连杆关节不会自动维持平行连杆关系。

基础导入脚本 `load_model.py` 使用运动学姿态赋值，保留 CAD 惯量并用位置驱动保持姿态，
禁用机器人重力和碰撞；没有把 MuJoCo 的 tendon/equality 约束转换成 PhysX 闭环约束。
基础资产的夹爪为固定外形，日常工作场景使用 `mg400_mirror.usda` 显示实测开口。
镜像只显示实测姿态和夹指位置，不验证碰撞、夹持力或真实连杆动力学。
`mg400_tcp` 仍是零工具偏移的法兰坐标系；估算指尖参考为法兰下方 104 mm，
不能将法兰坐标直接当作已标定的实际夹爪 TCP。

## 重建与验证

在本项目根目录依次运行，复用 `uv.lock` 锁定的 USD 构建依赖：

```sh
uv run --group usd python scripts/build_isaac_model.py
uv run --group usd python scripts/build_isaac_mirror.py
uv run --group usd python scripts/build_isaac_workspace.py
```

这些命令安装 `usd` 依赖组，不要求安装或启动 Isaac Sim 运行环境。
构建顺序为基础模型、夹爪镜像覆盖层、工作场景。
构建前，将自定义场景另存到数据目录。
重建会覆盖默认模型、场景和主入口。

运行全部离线检查，包含工作场景、真机控制逻辑和夹爪镜像：

```sh
uv sync --all-extras --group usd
uv run --all-extras --group usd pytest -ra
```

本地测试比较源网格/颜色、惯量张量、关节拓扑、零位坐标、多姿态 CAD 运动学和夹爪偏移。
镜像测试检查夹指方向、开闭净距、原视觉网格去重和反馈失效处理。
`usd` 依赖组提供 `pxr` 和 NumPy。未安装时，USD 模型测试会跳过。
`-ra` 显示跳过原因。
这些检查不能替代服务器端的显示核对或真机验证。

开发时可在 Isaac Script Editor 执行 `apps/isaacsim/load_model.py`，检查基础资产导入。
该脚本先备份当前场景，再打开 `isaacsim/scene.usda` 并启动 PhysX 校验。
结果写入 `$MG400_DATA_DIR/isaacsim/isaac_sim_validation.json`。
只有 `success: true` 表示本项检查通过。
它检查关节赋值和回读的一致性，不测量动力学跟踪精度或真机精度。
独立检查入口 `apps/isaacsim/standalone.py` 必须由 Isaac Sim 自带 Python 启动。
