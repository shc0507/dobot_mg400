[English](stage-panel.en.md)

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
上传、接线和串口命令见 [固件说明](../firmware/three-motor-demo/README.md)。
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

位移台是开环控制（只发脉冲，不读取实际位置），没有编码器。
新版固件支持回零开关和软限位。旧版固件保留手动控制，但不能回零或移动到命名目标。
页面计数是有符号 STEP 脉冲数。复位和回零会重设参考位置，计数不是实测行程。
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

### 自动化控制

自动化客户端持有控制租约时，页面显示 `Stage is controlled by automation`。
固定步数、长按、键盘移动、细分、Home、命名位置和位姿控件不可用。
`Stop all motors` 和结束长按仍可用。
租约默认有效 3 秒，客户端必须续期。
租约过期时，服务请求停止所有电机。
释放租约只交还控制权，不会停止运动。
客户端交还控制权前，先等待运动完成或请求停止。

停止无法确认时，页面显示故障并禁止新运动。
先使用现场物理急停并核对电机状态，再手动断开和连接 Arduino。
停止请求结束后再重新连接。
重新连接读到所有电机空闲后才清除故障。
仅收到空闲反馈不会自动清除故障。
接口说明见 [位移台自动化契约](development.md#位移台自动化契约)。

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
切换细分模式会清除全部 3 轴的原点参考和软限位。
移动到命名目标或自动移动前，再次回零各轴。

### 回零与命名移动

硬件日常使用前，完成 H-07 和 H-18 的监督开关与校准检查。
回零是寻找配置的原点开关，以建立参考位置。软限位限制相对于原点的行程。
服务从 `/var/lib/mg400/stage.toml` 读取实测校准，见[位移台校准](#位移台校准)。

各电机显示参考状态：

| 标记 | 含义 |
| --- | --- |
| `Homing unavailable` | 电机未连接、固件不支持，或校准不可用。连接后仍可手动控制。 |
| `Home required` | 服务尚未确认当前连接和细分模式下的回零与配置软限位。 |
| `Homing…` | 正在回零或确认软限位。 |
| `Homed` | 服务已确认回零和软限位。 |

点击某轴的 `Home` 前，清空行程路径。
等待 `Homed`，再选择 Insert 命名位置，如 `load` 或 `furnace`。
命名位置以原点为基准，单位为毫米。显示的毫米值来自脉冲计数和校准，不是实测行程。
缺少已确认的参考时，主机拒绝自动移动，返回 `position unknown, home first`。

选择 `Named poses` 下的按钮前，回零全部 3 轴。
位姿按配置顺序逐轴移动，每次只移动 1 轴。
回零或命名移动期间，新移动控件不可用，`Stop all motors` 仍可用。
页面不排队，也不自动重试这些操作。
复位、重新连接和切换细分后，必须重新回零并确认软限位。

状态中的 `feedback_age_s` 从最近一次完整开发板回复计时。只有回复不超过 2 s，`feedback_fresh` 才为真。
各电机的 `automation_ready` 要求反馈新鲜，并已确认当前细分模式下的参考和软限位。
只有电机空闲且脉冲计数精确匹配配置目标时，`at_positions` 才列出该命名位置。不要假定 `load` 为零。
这些字段不用 HTTP 响应时间代替开发板反馈时间。
释放租约会取消序列的剩余步骤，即使同一客户端再次取得控制权。续期保留当前序列。

页面路由如下：

| POST 路由 | JSON 请求体 |
| --- | --- |
| `/api/motors/<motor>/home` | `{}` |
| `/api/motors/<motor>/awake` | `{"seconds": 30}` |
| `/api/motors/<motor>/goto` | `{"name": "load"}` |
| `/api/poses/<name>` | `{}` |

这些路由与手动移动使用相同的页面认证和自动化控制权规则。
请求被接受后返回状态快照。轮询状态以确认完成。
保持唤醒请求延长驱动器通电 0–60 秒，不发送 STEP 脉冲，也不建立参考。
延长时间为 0 时，取消延长，常规 10 秒空闲保持仍然生效。
页面不续期保持唤醒请求。长按看门狗仍为 1 秒。

契约新增 `home`（`motor`）、`awake`（`motor` 和 `seconds`）、`goto`（`motor` 和 `name`）、`pose`（`name`）。
正数契约 `awake` 必须有已确认的参考。持有控制权的客户端可以用 0 取消延长，无须回零。
手动保持唤醒路由要求新版固件，但不要求回零。

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

服务名为 `mg400-stage-panel.service`，由 [主机部署](deploy.md) 安装，`mg400ctl` 名称为 `stage`。
服务以数字 ID `1000` 的用户和组运行，并加入 `dialout` 组，用于读写串口。
服务手动启动，未设置开机自启。
服务启动不打开串口，在页面点击 `Connect` 后才打开。
服务不在磁盘写入文件，输出写入 systemd 日志。

### 配置项

服务从环境变量读取配置，命令行参数覆盖对应的环境变量：

| 环境变量 | 参数 | 默认值 | 用途 |
| --- | --- | --- | --- |
| `MG400_STAGE_BIND` | `--bind` | `127.0.0.1` | 监听地址 |
| `MG400_STAGE_PORT` | `--port` | `8767` | 监听端口，1–65535 |
| `MG400_STAGE_TOKEN` | 无 | 无 | 访问密钥 |
| `MG400_STAGE_SERIAL` | `--serial` | 无 | Arduino 的串口路径 |
| `MG400_STAGE_CONFIG` | `--config` | `/var/lib/mg400/stage.toml` | 位移台实测校准和命名目标 |

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

### 位移台校准

使用 H-07 的测量值和行程限位。不要根据脉冲计数推测校准，也不要把演示几何值用于硬件。
文件包含 `motors` 表和可选的 `poses` 表，没有生产默认值。
使用 UID 1000 或本地服务用户拥有的普通文件，权限为 `0600` 或 `0640`。
读取器拒绝文件路径中的符号链接。宿主机数据目录权限保持为 `0750`。
`motors.bottom`、`motors.top` 和 `motors.insert` 各包含：

| 字段 | 值 |
| --- | --- |
| `steps_per_mm` | 校准细分模式下，每毫米的脉冲数，必须为有限正数。 |
| `microstep` | 校准模式：1、2、4、8 或 16。 |
| `min_mm`、`max_mm` | 相对于原点的行程边界，必须有限且按大小排列。 |
| `positions` | 可选的名称与毫米位置表。Insert 必须包含 `load` 和 `furnace`。 |

各 `poses.<name>` 表包含 `order` 和 `positions`。
`order` 按移动顺序列出 `bottom`、`top` 和 `insert`，各出现 1 次。
`positions` 给出各轴限位范围内的毫米位置。
`furnace` 位姿表示坩埚卸载开始时的插入位置。

主机按 `steps_per_mm * current_microstep / microstep` 计算每毫米的脉冲数。
边界向内取整，目标取最近的脉冲。取整后再次检查目标是否位于脉冲边界内。
回零后，主机发送软限位并等待确认，再启用命名移动或自动移动。
文件缺失或无效时，这些控件不可用，并显示配置错误。手动控件和 Stop 仍可用。

未显式指定配置时，演示模式使用独立的合成几何值，供离线练习。
显式指定的 `--config` 或 `MG400_STAGE_CONFIG` 也适用于演示模式。
合成几何值不是实测校准。使用前，验证硬件配置。

### 配置文件

服务从 `/etc/mg400/mg400.env` 读取配置。
安装器生成 `MG400_STAGE_TOKEN`，其余各项按本主机修改。

接上 Arduino 后，查找它的串口路径：

```sh
ls -lL /dev/serial/by-id/
```

每一项是一个 USB 串口设备，名称通常含厂商、型号和序列号，插拔后不变。
名称含 `BG02Q5TN` 的一项是夹爪转换器，不是 Arduino。
`-L` 显示链接所指设备的权限和属组，不显示设备名。
确认 Arduino 一项的属组为 `dialout`。
将 `MG400_STAGE_SERIAL` 设为 `/dev/serial/by-id/` 加 Arduino 的名称。

查看访问密钥：`sudo mg400ctl token stage`。
修改配置文件后，重启服务。
修改访问密钥后，在页面输入新密钥。

### 启动与停止

```sh
mg400ctl start stage
mg400ctl status
mg400ctl logs stage
mg400ctl stop stage
```

启动后，日志显示监听地址，例如 `Stage Controller: http://127.0.0.1:8767`。
设置 `MG400_STAGE_SERIAL` 时，日志还显示 `Serial port:` 和该路径。
停止服务时，日志记录停止结果，例如 `All motors stopped · disconnected`。
服务异常退出后，systemd 等待 3 秒再启动服务。
服务因配置错误拒绝启动时，systemd 不重启服务，状态显示 `failed`。
重启后，面板未连接 Arduino，在页面重新连接。
主机重启后，手动启动服务。

更新代码后，按 [主机部署](deploy.md#安装与更新) 运行安装器，再运行 `mg400ctl restart stage`，并重新加载已打开的面板页面。
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
| `Cannot reach the stage panel service. Motion status is unknown.` | 检查 VPN 连接，再在宿主机运行 `mg400ctl status`。 |
| 服务日志显示 `Failed to load environment files` | 按 [主机部署](deploy.md#安装与更新) 运行安装器，创建 `/etc/mg400/mg400.env`，再启动服务。 |
| 服务日志显示 `Set MG400_STAGE_TOKEN to at least 24 characters, or leave it unset and bind to localhost` | 已有的 `MG400_STAGE_TOKEN` 少于 24 个字符时，把该行改为 `MG400_STAGE_TOKEN=`，再运行安装器生成访问密钥，然后启动服务。 |
| 服务日志显示 `MG400_STAGE_SERIAL points to the gripper USB converter (BG02Q5TN)` | 按 [配置文件](#配置文件) 查找 Arduino 的路径，改写 `MG400_STAGE_SERIAL`，再启动服务。 |
| 服务日志显示 `MG400_STAGE_PORT must be a whole number between 1 and 65535` | 把 `MG400_STAGE_PORT` 改为 1–65535 的整数，再启动服务。 |
