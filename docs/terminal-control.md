# 交互终端

[返回项目首页](../README.md)

交互终端直接控制 MG400 和 PGEA 夹爪。
界面持续显示反馈，在底部输入命令。
它不依赖 HTTP 服务。
终端不能检测松键，不支持键盘点动。

## 启动

在能连接机械臂的主机运行终端。
程序使用 Python 3.11+ 标准库中的 `curses`。
支持 macOS、Linux 本地终端和带 PTY 的 SSH 会话。
PTY 是 SSH 分配的交互终端，可用 `ssh -t` 请求。

使用前，关闭其他机械臂控制程序。
终端与网页面板共用机械臂连接锁。
多个进程必须配置相同的机械臂地址。
它们还必须共用 `MG400_CONTROLLER_LOCK_DIR`。
目录、权限和 Olares 路径见 [面板连接锁](olares.md#面板连接锁)。
此锁不能阻止 DobotStudio 或其他主机连接。

进入项目目录，运行：

```sh
uv run mg400-robot
```

也可以直接运行：

```sh
python3 scripts/mg400_robot.py
```

机械臂地址默认取自 `config/robot.toml`。
用 `--robot-host` 覆盖地址：

```sh
uv run mg400-robot --robot-host 192.168.1.6
```

启动程序不会连接、使能或移动机械臂。
启动程序也不会初始化夹爪。
输入 `connect` 后，等待界面显示实测状态。
确认运动范围无人或障碍物后，输入 `enable`。

## 输入命令

输入命令后按 Enter 执行。
用上、下方向键调用历史命令。
用左、右方向键移动输入光标。
输入 `help` 查看命令说明，输入 `status` 查看当前状态。

| 命令 | 行为 |
| --- | --- |
| `connect` | 连接机械臂并取得连接锁 |
| `disconnect` | 请求受控停止，确认后断开连接 |
| `enable` | 使能机械臂 |
| `disable` | 下使能机械臂 |
| `move X Y Z R` | 移动到绝对坐标 |
| `home` | 将 J1–J4 移动到 0° |
| `grip N` | 设置夹爪开口，范围为 0–1000 |
| `grip-init` | 初始化夹爪，会张开夹指 |
| `stop` | 请求机械臂受控停止 |
| `status` | 显示当前状态 |
| `help` | 显示命令说明 |
| `quit` | 请求受控停止，确认断开后退出 |
| `quit --force` | 强制退出，不能确认机械臂停止 |

`move` 使用基座坐标系和法兰原点（`User=0,Tool=0`）。
X/Y/Z 单位为 mm，R 单位为度。
执行前，程序用控制器逆解和现有关节范围检查验证目标。
直线运动使用 `SpeedL=1,AccL=1`。
`home` 使用 `SpeedJ=5,AccJ=5`。
`home` 会移动机械臂，不是坐标校准命令。

## 控制夹爪

夹爪使用现有 USB 采集服务。
先按 [USB 接在 Olares](olares.md#usb-接在-olares) 启动采集服务。
采集程序必须带 `--commands`。
终端与采集程序必须在同一主机。
两者必须共用单调时钟（计算时间间隔的时钟）。
夹爪命令不支持跨主机传递。

用 `--gripper-feedback` 指定反馈文件。
以下示例使用 Olares 容器内的路径：

```sh
python3 scripts/mg400_robot.py \
  --gripper-feedback /root/Documents/mg400-data/work/gripper_olares_feedback.json
```

该路径必须能由终端进程读取。
在 Olares 宿主机运行时，按 [USB 配置](olares.md#usb-接在-olares) 改写路径。
终端不会自动启动采集服务或初始化夹爪。
未指定夹爪反馈文件时，仍可单独控制机械臂。

夹爪命令必须先用 `connect` 连接机械臂。
机械臂必须空闲，控制器模式必须为 4 或 5。
这两个模式分别表示未使能空闲和已使能空闲。

夹持物体时不要输入 `grip-init`。
初始化会张开夹指寻找行程端点。
按以下顺序操作：

1. 确认夹爪未夹持物体。
2. 若夹爪未初始化，输入 `grip-init`。
3. 输入 `grip N`，将 N 替换为目标开口。

0 表示闭合，1000 表示全开。
`grip` 只移动夹爪，不移动机械臂。
`stop` 和下使能不会撤回已发送的夹爪命令。

## 停止与退出

输入 `stop` 或按 Ctrl+C，请求机械臂受控停止。
Ctrl+C 后终端保持打开。
停止确认后，输入 `disable`，再输入 `enable` 恢复运动。
受控停止不能代替现场物理急停。
若停止或命令结果无法确认，使用现场物理急停。

输入 `quit` 或按 Ctrl+D，请求受控停止并断开。
确认断开且没有执行中的任务后，终端才退出。
若无法确认停止，终端保持打开并保留连接锁。
退出不会自动下使能。
正常结束时，先输入 `disable`，再输入 `quit`。

仅在现场物理停止后，使用 `quit --force` 强制退出。
强制退出返回退出码 1，并释放进程锁。
它不能确认机械臂已停止。

进程退出或网络故障时，停止命令可能无法送达。
机械臂可能继续到已发送轨迹的终点。
不要删除锁文件来解除占用。
锁的处理方式见 [面板连接锁](olares.md#面板连接锁)。

## Olares 启动

在 Control Hub 的宿主机终端运行：

```sh
cd /olares/userdata/Cache/pvc-appcache-qumusphusis-aah2phqnttncsxoh/isaaclab/documents
sh mg400-tools/start-terminal.sh
```

代码位于同目录下的 `mg400-terminal`。
启动脚本使用用户和组 `1000:1000`。
它配置共享连接锁和夹爪反馈路径。
启动后显示 `DISCONNECTED`，输入 `connect` 才连接机械臂。
