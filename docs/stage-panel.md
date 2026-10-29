# 位移台面板

[返回项目首页](../README.md)

位移台面板是控制三电机位移台的网页服务，与机械臂的网页面板分开运行。
位移台的三个步进电机接在一块 Arduino 上，各由一块 A4988 驱动器驱动。
面板通过 USB 串口向 Arduino 发送命令，波特率为 115200。
服务使用 Flask、waitress 和 pySerial。

三个电机的方向和按键如下：

| 电机 | 动作 | 页面标签 | 串口方向 | 按键 |
| --- | --- | --- | --- | --- |
| `bottom` | 升 | `Up` | `ccw` | W |
| `bottom` | 降 | `Down` | `cw` | S |
| `top` | 右 | `Right` | `ccw` | D |
| `top` | 左 | `Left` | `cw` | A |
| `insert` | 插入 | `Insert In` | `ccw` | J |
| `insert` | 退出 | `Insert Out` | `cw` | K |

长按按钮在标签前加 `Hold`，例如 `Hold Up`。
标签只对应串口方向，实际转向取决于电机接线和观察方向。

## 使用前

使用时，现场必须有人能切断电机电源。
`Stop all motors` 是软件停止，不是急停。

Arduino 接在 Olares 宿主机的 USB 上，必须运行 `firmware/three-motor-demo` 固件。
上传、接线和串口命令见 [固件说明](../firmware/three-motor-demo/README.md)（英文）。
面板用文件锁独占串口。
不加锁的程序仍能同时打开该串口，面板不能发现它。

部署在 Olares 时，在装有 Arduino IDE 的电脑上传固件：

1. 在面板点击 `Stop all motors` 和 `Disconnect`。
2. 从 Olares 宿主机拔下 Arduino，接到该电脑。
3. 按固件说明上传固件。
4. 把 Arduino 接回 Olares 宿主机的 USB。
5. 在宿主机运行 `ls -l /dev/serial/by-id/`，确认 Arduino 的路径与 `MG400_STAGE_SERIAL` 相同。
6. 在面板点击 `Connect`。

在本地运行面板时，上传前在面板点击 `Disconnect`。

位移台是开环控制（只发脉冲，不读取实际位置）。
它没有编码器、回零或限位开关，面板也不限制行程。
页面计数是 Arduino 复位以来发出的有符号 STEP 脉冲数，不是实测行程。
移动前，确认该方向有足够行程。

多数 Arduino 在串口打开时复位，面板每次连接都打开串口。
复位期间，驱动器可能短时唤醒，电机可能转到 A4988 的 Home 细分位置。
复位后，计数归零，细分模式（一个脉冲走几分之一整步）恢复整步。
固件启动后，三个驱动器进入睡眠。

电机停止后，驱动器保持线圈通电。
电机停止一段时间后，驱动器进入睡眠，电机不再保持位置。
这段时间由固件的 `SleepTimeoutMs` 设置，默认 10 秒。
驱动器睡眠时，外力能转动电机，计数不记录这些转动。
驱动器唤醒时，电机可能转到 A4988 的 Home 细分位置。

面板不区分控制者。
打开面板的每个页面都能发送命令。
连接、运动状态、活动日志和冷却时间在所有页面共享。
速度和步数输入只属于当前页面，刷新后恢复默认值。

## 打开面板

### 使用 Olares 已部署的服务

保持 LarePass VPN 连接。
VPN 访问控制规则必须允许 TCP `8767`。
打开 [Olares 位移台面板](http://100.64.0.1:8767/)。

页面显示 `Panel access token` 输入框时，输入访问密钥，再点击旁边的 `Connect`。
访问密钥是宿主机 `/etc/mg400/mg400.env` 中 `MG400_STAGE_TOKEN` 的值。
页面在当前标签页保存密钥，刷新后不必重新输入。
服务拒绝密钥时，页面删除保存的密钥，再次显示输入框。
输入框显示期间，页面暂停读取状态。
服务未启动时，参见 [服务配置](#服务配置)。

### 在本地启动

在接有 Arduino 的电脑进入项目目录，运行：

```sh
uv sync
uv run stage-panel
```

在该电脑打开 [本地位移台面板](http://127.0.0.1:8767/)。
`uv run python -m stage_controller` 与 `uv run stage-panel` 等效。
本地运行也读取 [配置项](#配置项) 中的环境变量和参数。
在 Linux 上，运行服务的用户必须能读写串口设备，例如加入 `dialout` 组。
加入组后，重新登录。

没有 Arduino 时，用演示模式查看界面：

```sh
uv run stage-panel --demo
```

在串口列表选择 `DEMO`，再点击 `Connect`。
演示模式不打开串口，模拟的运动时间不含加减速。

## 控制

### 连接

按以下顺序操作：

1. 在 `Device connection` 的串口列表选择 Arduino。
2. 点击 `Connect`，等待状态显示 `Arduino connected`。
3. 结束时，点击 `Disconnect`。

串口列表显示运行服务的主机的串口。
部署在 Olares 时，列表来自 Olares 宿主机，不是浏览器所在的电脑。
插入 Arduino 后，点击刷新按钮更新列表。
列表不显示夹爪的 USB 转换器（序列号 `BG02Q5TN`）。
设置 `MG400_STAGE_SERIAL` 时，列表只显示该设备，见 [配置项](#配置项)。
未设置时，列表可能含有其他串口。
在宿主机运行 `ls -l /dev/serial/by-id/`，按设备名找到 Arduino。
不要连接身份不明的串口。
连接时，服务向该串口发送 `status` 命令。

服务只打开列表中当前存在的串口。
连接时，服务打开串口，等待约 2 秒让 Arduino 复位。
Arduino 返回三个电机的状态后，控制才解锁。
`Disconnect` 发送 `stop all`，收到确认后关闭串口。

### 细分模式

`Stepping mode` 对三个电机同时生效，可选整步、1/2、1/4、1/8 或 1/16 步。
所有电机静止时，选择模式，再点击 `Apply to all`。
页面另行显示 Arduino 报告的当前模式。

速度和步数都按当前模式的 STEP 脉冲计数。
切换模式时，面板不换算速度和步数。
例如 1/16 步时，50 个脉冲等于 3.125 个整步。
不同模式下的计数不能换算成同一距离。

### 固定步数移动

每个电机有独立的 `Speed` 和 `Steps per click`：

| 输入 | 范围 | 默认值 |
| --- | --- | --- |
| `Speed` | 1–1000 脉冲/秒 | 50 |
| `Steps per click` | 1–100000 脉冲 | 50 |

点击方向按钮时，电机按设定速度移动设定的步数。
固定步数移动有加减速，默认加速度为 50 脉冲/秒²。
距离短时，电机可能达不到设定速度。

固定步数移动结束后，该电机冷却 1 秒，之后才能再次移动。
服务也检查冷却，多个页面或快速连击都不能绕过。
面板不排队，也不自动重试移动。
其他电机不受此冷却影响。

修改速度或步数只影响下一次移动。
`Reset defaults` 把当前页面的速度和步数恢复为 50。
它不改变细分模式，也不停止正在运动的电机。

### 长按移动

按住 `Hold` 按钮时，电机以设定速度连续运动，松开后停止。
长按移动没有加减速，不使用 `Steps per click`。
也可以聚焦按钮，再按住空格键或回车键。
长按期间，速度不能修改。

松开按钮、指针移出按钮、按 Esc、切换页面或关闭页面时，长按停止。
停止后，该电机冷却 1 秒。

页面约每 200 ms 续期一次长按（告诉服务和 Arduino 按钮仍按住）。
Arduino 1 秒内未收到续期时，停止该电机。
服务也在 1 秒后停止未续期的长按。
浏览器、VPN、服务或 USB 中断时，长按的电机约 1 秒内停止。

### 键盘控制

每次加载页面时，`Keyboard controls` 为 `Off`。
点击开关切换为 `On`，再按住按键或页面上的对应按钮。
按键见本页开头的表格。

键盘控制使用各电机的 `Speed` 和长按规则，不使用 `Steps per click`。
多个电机可以同时长按。
编辑输入框或按住 Ctrl、Alt、Meta 时，字母键无效。
冷却或停止期间一直按住按键，电机不会重新启动，松开后再按。
关闭开关时，从该区域开始的长按停止。

### 停止

`Stop all motors` 停止所有电机的脉冲输出，移动和冷却期间都可用。
停止后，线圈保持通电，直到驱动器睡眠。
Esc 只停止长按移动。

页面连不上服务时，显示 `Cannot reach the stage panel service. Motion status is unknown.`，并结束长按。
关闭页面或网络中断时，服务继续运行，固定步数移动继续到终点。

停止服务时，服务先发送 `stop all` 并断开串口，再退出。
进程被强制结束或崩溃时，服务不发送停止命令。
此时长按的电机由 Arduino 在 1 秒后停止，固定步数移动继续到终点。

USB 断开时，服务不能保证停止，已开始的固定步数移动可能走完。
服务失去 Arduino 连接时，尝试发送 `stop all`，再关闭连接。
此时页面显示 `Connection lost; motor stop could not be confirmed.`。
到现场确认电机已停止，再重新连接。
面板不自动重连，也不重试移动。

## 服务配置

服务定义为 `config/stage_panel/mg400-stage-panel.service`，服务名为 `mg400-stage-panel.service`。
服务以数字 ID `1000` 的用户和组运行，并加入 `dialout` 组，用于读写串口。
服务手动启动，未设置开机自启。
服务启动不打开串口，在页面点击 `Connect` 后才打开。
服务不在磁盘写入文件，输出写入 systemd 日志。

机械臂的网页服务仍按 [Olares 部署与维护](olares.md) 和 [网页控制面板](control-panel.md#服务配置) 部署。

### 配置项

服务从环境变量读取配置，命令行参数覆盖对应的环境变量：

| 环境变量 | 参数 | 默认值 | 用途 |
| --- | --- | --- | --- |
| `MG400_STAGE_BIND` | `--bind` | `127.0.0.1` | 监听地址 |
| `MG400_STAGE_PORT` | `--port` | `8767` | 监听端口，1–65535 |
| `MG400_STAGE_TOKEN` | 无 | 无 | 访问密钥 |
| `MG400_STAGE_SERIAL` | `--serial` | 无 | Arduino 的串口路径 |

值为空的环境变量等同于未设置。

监听非回环地址时，必须设置 `MG400_STAGE_TOKEN`。
访问密钥至少 24 个字符，只能包含可打印 ASCII 字符，不得以空格开头或结尾。
不满足时，服务拒绝启动。
设置访问密钥后，服务接受任何主机名，监听回环地址时也一样。
未设置访问密钥时，服务只接受 `127.0.0.1`、`localhost` 或 `[::1]` 地址。
此时服务也拒绝带 `Forwarded`、`X-Forwarded-For`、`X-Forwarded-Host` 或 `X-Real-IP` 的请求。
接口说明见 [位移台面板接口](development.md#位移台面板接口)。

设置 `MG400_STAGE_SERIAL` 时，串口列表只显示该设备，`Connect` 也只接受该设备。
建议使用 `/dev/serial/by-id/` 下的路径，重新插拔后路径不变。
该设备不存在时，列表为空。
路径指向夹爪转换器时，服务拒绝启动。
未设置时，列表显示除夹爪转换器外的所有串口。
演示模式忽略此项。

### 安装与更新

安装前，确认以下条件：

- 宿主机已安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
- 宿主机的 `/usr/bin/python3` 为 Python 3.11 或更高版本。
- 宿主机能访问 PyPI。`uv sync` 从 PyPI 下载依赖和构建工具。
- `$DOCS/mg400-isaac` 已切换到目标提交，见 [代码与运行数据](olares.md#代码与运行数据)。

本节命令以 root 身份在 Olares 宿主机终端运行。
更新时，先停止服务：

```sh
systemctl stop mg400-stage-panel.service
```

然后运行以下命令，安装代码、依赖和服务定义：

```sh
DOCS=/olares/userdata/Cache/pvc-appcache-qumusphusis-aah2phqnttncsxoh/isaaclab/documents
install -d -o root -g 1000 -m 0750 /opt/mg400
test -d /opt/mg400/.git || git init -q /opt/mg400
git -C /opt/mg400 fetch -q --depth 1 "$DOCS/mg400-isaac" HEAD
git -C /opt/mg400 switch -q --detach FETCH_HEAD
cd /opt/mg400
UV_LINK_MODE=copy uv sync --locked --no-default-groups --group stage --python /usr/bin/python3
install -m 0644 config/stage_panel/mg400-stage-panel.service /etc/systemd/system/
systemctl daemon-reload
```

`/opt/mg400` 是 Git 工作副本，内容为 `$DOCS/mg400-isaac` 当前的提交，不含未提交的修改。
`--depth 1` 只取该提交，不取历史。
`/opt/mg400` 的属主为 `root:1000`，权限为 `0750`。
服务用户能读取代码，不能修改代码。
不要在 `/opt/mg400` 修改文件。修改的文件可能让 `git switch` 中止更新。
运行 `git -C /opt/mg400 rev-parse HEAD`，查看已安装的提交。

`--python /usr/bin/python3` 用宿主机 Python 创建 `.venv`。
不指定时，uv 可能使用 root 主目录中的 Python，服务用户不能读取。
`UV_LINK_MODE=copy` 把依赖复制到 `.venv`，不链接 root 的 uv 缓存。
`--no-default-groups --group stage` 只安装运行依赖，不安装测试工具。

### 配置文件

服务从 `/etc/mg400/mg400.env` 读取配置。
该文件只允许 root 读取，systemd 读取后启动服务。
文件可能含有其他服务的配置，修改时保留其他行。

首次安装时，创建配置文件并生成访问密钥：

```sh
mkdir -p /etc/mg400
touch /etc/mg400/mg400.env
chmod 0600 /etc/mg400/mg400.env
grep -q '^MG400_STAGE_TOKEN=' /etc/mg400/mg400.env || printf 'MG400_STAGE_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> /etc/mg400/mg400.env
```

文件中没有 `MG400_STAGE_TOKEN` 时，最后一行生成 43 个字符的访问密钥。

接上 Arduino 后，查找它的串口路径：

```sh
ls -lL /dev/serial/by-id/
```

每一项是一个 USB 串口设备，名称通常含厂商、型号和序列号，插拔后不变。
名称含 `BG02Q5TN` 的一项是夹爪转换器，不是 Arduino。
`-L` 显示链接所指设备的权限和属组，不显示设备名。
确认 Arduino 一项的属组为 `dialout`。

最后用文本编辑器加入 `MG400_STAGE_BIND`、`MG400_STAGE_PORT` 和 `MG400_STAGE_SERIAL`。
`MG400_STAGE_SERIAL` 的值是 `/dev/serial/by-id/` 加 Arduino 的名称。
完成后，Olares 的位移台配置如下：

```ini
MG400_STAGE_BIND=100.64.0.1
MG400_STAGE_PORT=8767
MG400_STAGE_TOKEN=<至少 24 个字符>
MG400_STAGE_SERIAL=/dev/serial/by-id/<Arduino>
```

文件中只保留一行 `MG400_STAGE_TOKEN`，值为生成的访问密钥。
监听 `100.64.0.1` 时，Olares 的 VPN 访问控制规则必须允许 TCP `8767`。

查看访问密钥：

```sh
sed -n 's/^MG400_STAGE_TOKEN=//p' /etc/mg400/mg400.env
```

修改配置文件后，重启服务。
修改访问密钥后，在页面输入新密钥。

### 启动与停止

```sh
systemctl start mg400-stage-panel.service
systemctl status mg400-stage-panel.service --no-pager
journalctl -u mg400-stage-panel.service -n 50 --no-pager
systemctl stop mg400-stage-panel.service
```

启动后，日志显示监听地址，例如 `Stage Controller: http://100.64.0.1:8767`。
设置 `MG400_STAGE_SERIAL` 时，日志还显示 `Serial port:` 和该路径。
停止服务时，日志记录停止结果，例如 `All motors stopped · disconnected`。
服务异常退出后，systemd 等待 3 秒再启动服务。
服务因配置错误拒绝启动时，systemd 不重启服务，状态显示 `failed`。
重启后，面板未连接 Arduino，在页面重新连接。
主机重启后，手动启动服务。

位移台面板与其他服务使用同一提交。
更新代码时，按以下顺序操作：

1. 按 [代码与运行数据](olares.md#代码与运行数据)，把 `$DOCS/mg400-isaac` 切换到新提交。
2. 停止服务，运行 [安装与更新](#安装与更新) 的命令。
3. 启动服务。
4. 重新加载已打开的面板页面。

服务每次启动生成新的命令令牌（页面发送命令时附带的随机值）。
服务拒绝旧页面发送的命令。

## 故障排查

以下报错显示在面板页面上，服务启动失败的报错显示在服务日志中。

| 现象 | 处理 |
| --- | --- |
| 串口列表显示 `No serial ports found`，或找不到 Arduino | 在宿主机运行 `ls -l /dev/serial/by-id/`。没有 Arduino 时，检查 USB 数据线和 Arduino 供电，再点击刷新按钮。设置了 `MG400_STAGE_SERIAL` 时，确认它与 Arduino 的路径相同。 |
| `That port is no longer available. Refresh the port list.` | 点击刷新按钮，再选择串口。 |
| `Cannot open <串口>: ...`，其中含 `Permission denied` | 服务用户不能读写该串口。确认设备属组为 `dialout`。本地运行时，把用户加入 `dialout` 组并重新登录。 |
| `<串口> is in use by another program.` | 另一个程序占用该串口。关闭该程序，例如 Arduino IDE 的串口监视器或另一个 `stage-panel` 进程。 |
| `Arduino did not reply in time. Check the port and uploaded sketch.` | 确认所选串口是 Arduino，并已上传 `firmware/three-motor-demo`。出错后面板断开连接，修复后再点击 `Connect`。 |
| `Hold to move was rejected. Upload the updated three-motor-demo sketch.`，或 `Stepping mode` 区域显示 `Stepping mode unavailable on this firmware` | Arduino 运行旧固件。上传本项目的 `firmware/three-motor-demo`。 |
| 页面显示 `Panel access token` 输入框，或提示 `Enter the panel access token` | 输入 `/etc/mg400/mg400.env` 中的 `MG400_STAGE_TOKEN`。 |
| 浏览器不显示面板，只显示含 `Open the panel at 127.0.0.1 or localhost` 的 JSON | 服务未设置访问密钥，请求来自其他主机名或代理。在服务主机打开 `127.0.0.1` 地址，或设置至少 24 个字符的 `MG400_STAGE_TOKEN`。 |
| `Reload this page before sending another command.` | 服务已重启。重新加载页面。 |
| `Cannot reach the stage panel service. Motion status is unknown.` | 检查 VPN 连接，再在宿主机运行 `systemctl status mg400-stage-panel.service --no-pager`。 |
| 服务日志显示 `Failed to load environment files` | 按 [配置文件](#配置文件) 创建 `/etc/mg400/mg400.env`，再启动服务。 |
| 服务日志显示 `Set MG400_STAGE_TOKEN to at least 24 characters, or leave it unset and bind to localhost` | 已有的 `MG400_STAGE_TOKEN` 少于 24 个字符时，先删除该行。按 [配置文件](#配置文件) 生成访问密钥，再启动服务。 |
| 服务日志显示 `MG400_STAGE_SERIAL points to the gripper USB converter (BG02Q5TN)` | 按 [配置文件](#配置文件) 查找 Arduino 的路径，改写 `MG400_STAGE_SERIAL`，再启动服务。 |
| 服务日志显示 `MG400_STAGE_PORT must be a whole number between 1 and 65535` | 把 `MG400_STAGE_PORT` 改为 1–65535 的整数，再启动服务。 |
