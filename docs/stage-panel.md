[English](stage-panel.en.md)

# 位移台

[返回项目首页](../README.md)

位移台服务控制三电机位移台，命令名为 `stage-panel`，systemd 服务名为 `mg400-stage-panel.service`。
服务没有网页。操作员在 Lab Console 运动工作区（Motion）的位移台卡片 `Stage · 3 axes` 上操作。
位移台的三个步进电机接在一块 Arduino 上，各由一块 A4988 驱动器驱动。
服务通过 USB 串口向 Arduino 发送命令，波特率为 115200。
服务使用 Flask、waitress 和 pySerial。

三个电机的方向和按钮如下：

| 电机 | 卡片中的轴 | 动作 | 按钮 | 串口方向 |
| --- | --- | --- | --- | --- |
| `bottom` | `Bottom` | 升 | `Step −` | `ccw` |
| `bottom` | `Bottom` | 降 | `Step +` | `cw` |
| `top` | `Top` | 右 | `Step −` | `ccw` |
| `top` | `Top` | 左 | `Step +` | `cw` |
| `insert` | `Insert` | 插入 | `Step −` | `ccw` |
| `insert` | `Insert` | 退出 | `Step +` | `cw` |

`cw` 增加脉冲计数，`ccw` 减少脉冲计数。
动作只对应串口方向，实际转向取决于电机接线和观察方向。

## 使用前

使用时，现场必须有人能切断电机电源。
`Stop stage` 是软件停止，不是急停。

Arduino 接在 Olares 宿主机的 USB 上，必须运行 `firmware/three-motor-demo` 固件。
上传、接线和串口命令见 [固件说明](../firmware/three-motor-demo/README.md)。
服务用文件锁独占串口。
不加锁的程序仍能同时打开该串口，服务不能发现它。

部署在 Olares 时，在装有 Arduino IDE 的电脑上传固件：

1. 在控制台点击 `Stop stage`。
2. 在宿主机运行 `mg400ctl stop stage`。服务发送 `stop all`，再关闭串口。
3. 从 Olares 宿主机拔下 Arduino，接到该电脑。
4. 按固件说明上传固件。
5. 把 Arduino 接回 Olares 宿主机的 USB。
6. 在宿主机运行 `ls -l /dev/serial/by-id/`，确认 Arduino 的路径与 `MG400_STAGE_SERIAL` 相同。
7. 运行 `mg400ctl start stage`，再按 [连接](#连接) 连接 Arduino。

在本地运行服务时，上传前停止 `stage-panel` 进程。

位移台是开环控制（只发脉冲，不读取实际位置），没有编码器。
位移台没有限位开关。新版固件支持确认后手动设零和软限位。
配置了开关的开发板仍可回零。
卡片显示的计数是有符号 STEP 脉冲数。复位、回零和手动设零会重设参考位置，计数不是实测行程。
移动前，确认该方向有足够行程。

多数 Arduino 在串口打开时复位，服务每次连接都打开串口。
复位期间，驱动器可能短时唤醒，电机可能转到 A4988 的 Home 细分位置。
复位后，计数归零，细分模式（一个脉冲走几分之一整步）恢复整步。
固件启动后，三个驱动器进入睡眠。

电机停止后，驱动器保持线圈通电。
电机停止一段时间后，驱动器进入睡眠，电机不再保持位置。
这段时间由固件的 `SleepTimeoutMs` 设置，默认 10 秒。
驱动器睡眠时，外力能转动电机，计数不记录这些转动。
驱动器唤醒时，电机可能转到 A4988 的 Home 细分位置。

## 打开控制台

### 使用 Olares 已部署的服务

保持 LarePass VPN 连接。
打开 [Lab Console](http://100.64.0.1:8080/)，进入 `Motion` 工作区。
位移台服务只监听回环地址。控制台进程在宿主机上通过 `127.0.0.1` 调用它。
控制台连接 `http://127.0.0.1:8767/contract/`，见 [配方服务](orchestrator.md)。
卡片显示 `Not running` 或 `Not reachable` 时，参见 [服务配置](#服务配置)。

### 在本地启动

在接有 Arduino 的电脑进入项目目录，运行：

```sh
uv sync
uv run stage-panel
```

`uv run python -m stage_controller` 与 `uv run stage-panel` 等效。
本地运行也读取 [配置项](#配置项) 中的环境变量和参数。
在 Linux 上，运行服务的用户必须能读写串口设备，例如加入 `dialout` 组。
加入组后，重新登录。
启动控制台时，把 `MG400_ORCH_STAGE_URL` 设为 `http://127.0.0.1:8767/contract/`。

没有 Arduino 时，用控制台的演示模式查看位移台卡片：

```sh
uv run python -m orchestrator serve --demo --recipes-dir ./recipes --port 8790 --base-path /lab/
```

在该电脑打开 [本地控制台](http://127.0.0.1:8790/lab/)。
`uv run stage-panel --demo` 启动演示服务。它的串口列表只有 `DEMO`。
演示模式不打开串口，模拟的运动时间不含加减速。

## 控制

### 控制权

点击 `Take control` 取得位移台的控制租约。
没有控制权时，连接、细分、步进移动、回零、设零、参考示教和命名位置不可用。
`Stop stage` 和 `STOP ALL MOTION` 不需要控制权。
其他客户端持有租约时，卡片显示 `Another client controls it`。
租约默认有效 3 秒，客户端必须续期。
租约过期时，服务请求停止所有电机。
释放租约只交还控制权，不会停止运动。
交还控制权前，先等待运动完成或点击 `Stop stage`。

停止无法确认时，卡片显示故障，服务禁止新运动。
先使用现场物理急停并核对电机状态。
停止请求结束后，运行 `mg400ctl restart stage`，再重新连接。
接口说明见 [位移台自动化契约](development.md#位移台自动化契约)。

### 连接

Arduino 未连接时，卡片显示 `Arduino not connected`、`Serial port` 列表和 `Connect` 按钮。
按以下顺序操作：

1. 点击 `Take control`。
2. 在 `Serial port` 列表选择 Arduino。
3. 点击 `Connect`，等待状态显示 `Connected`。

串口列表显示运行服务的主机的串口。
部署在 Olares 时，列表来自 Olares 宿主机，不是浏览器所在的电脑。
插入 Arduino 后，重新加载控制台页面以更新列表。
列表不显示夹爪的 USB 转换器（序列号 `BG02Q5TN`）。
设置 `MG400_STAGE_SERIAL` 时，列表只显示该设备，见 [配置项](#配置项)。
未设置时，列表可能含有其他串口。
在宿主机运行 `ls -l /dev/serial/by-id/`，按设备名找到 Arduino。
不要连接身份不明的串口。

服务只打开列表中当前存在的串口。
连接时，服务打开串口，等待约 2 秒让 Arduino 复位。
服务向该串口发送 `status` 和只读查询 `features`。
旧版固件可拒绝 `features`。
Arduino 返回三个电机的状态后，控制才解锁。
控制台没有断开按钮。停止服务时，服务发送 `stop all`，收到确认后关闭串口。

### 细分模式

`Stepping` 对三个电机同时生效，可选整步、1/2、1/4、1/8 或 1/16 步。
所有电机静止时，点击一个模式，再在卡片中确认。
高亮的模式是 Arduino 报告的当前模式。

速度和步数都按当前模式的 STEP 脉冲计数。
切换模式时，服务不换算速度和步数。
例如 1/16 步时，50 个脉冲等于 3.125 个整步。
不同模式下的计数不能换算成同一距离。
切换细分模式会清除全部 3 轴的零点和软限位。
之后，再次确认各轴参考。

### 参考状态与回零

硬件日常使用前，完成 H-07 和 H-18 的监督参考与校准检查。
软限位限制相对于已确认参考的行程。
服务从 `/var/lib/mg400/stage/stage.toml` 读取实测校准，见[位移台校准](#位移台校准)。

各轴显示参考状态：

| 标记 | 含义 |
| --- | --- |
| `Reference unavailable` | 电机未连接、固件不支持，或校准不可用。 |
| `Zero required` | 没有开关。服务尚未确认当前连接和细分模式下的零点与软限位。 |
| `Home required` | 有开关。服务尚未确认当前连接和细分模式下的回零与软限位。 |
| `Zero confirmed` | 服务已确认零点和软限位。 |
| `Homed` | 服务已确认回零和软限位。 |

步进移动和长按运动不需要参考。只有已确认参考的轴才应用软限位。
命名位置和毫米目标需要绝对位置，因此要求 `Zero confirmed` 或 `Homed`。
缺少已确认的参考时，服务拒绝这些移动，返回 `position unknown, home first`。
复位、重新连接和切换细分后，必须重新确认参考和软限位。

配置了开关的轴显示 `Home…`：

1. 清空该轴的行程路径。
2. 点击 `Home…`，在卡片中确认。该轴移动到参考开关。
3. 等待 `Homed`。

### 设零与相机参考

没有开关时，各轴显示 `Set zero…`。
配置了相机参考的轴还显示 `Check reference` 和 `Teach reference…`。
演示传输也支持模拟开关，用于练习回零。

1. 将该轴放在实测的安全零点。
2. 等待该轴空闲，冷却结束。
3. 若已配置参考，点击 `Check reference`。卡片显示相机图像和 `Marker offset` 像素偏移。
4. 点击 `Set zero…`，检查物理位置，再在卡片中确认。
5. 等待 `Zero confirmed`，再移动。

设零不发送运动。它重设脉冲计数，清除固件软限位，再写入配置的软限位。
设零和软限位都必须得到新鲜的开发板回复确认。配置范围必须包含零。
复位、重新连接或实际切换细分都会使确认失效。
活动日志记录设零的客户端。

各轴校准表可添加以下可选条目：

```toml
reference = {stream = "camera3", marker = 12, pixel = [99.5, 99.5], tolerance_px = 2}
```

这些值仅说明格式。在 H-07 中测量实际像素位置和容差。
`stream` 是 go2rtc 流名称，仅含字母、数字、下划线或连字符，不是 URL。
`marker` 从 `DICT_4X4_1000` 中选择 1 个 ArUco 标记，ID 为 0 到 999。
`pixel` 是图像中的标记中心，先 x 后 y。
`tolerance_px` 是正数，表示允许的最大欧氏距离，单位为像素。
坐标和容差必须有限，且不超过 8192 像素。

服务通过 go2rtc 从 `http://127.0.0.1:1984/api/frame.jpeg` 读取 1 张 JPEG。相机由 go2rtc 占用。
各套接字操作的超时为 2 秒。服务在单缓冲区读取之间检查 2 秒正文时限和 4 MiB 大小上限。
等待 HTTP 头部或套接字接收可能延长请求时间。设零时帧龄不得超过 2 秒。
禁用重定向和代理。
标记缺失、重复或超出容差时，服务拒绝发送设零命令。
取图期间位移台状态或控制租约改变时，服务拒绝操作。
没有参考时，设零仍须明确确认。演示参考必须使用离线帧源。

示教参考把当前检测到的标记中心保存为该轴的参考，即使它与原中心不同：

1. 将该轴放在安全物理零点，等待空闲。
2. 点击 `Teach reference…`，在卡片中确认。
3. 示教使原零点确认失效。之后点击 `Set zero…`。

示教要求持有控制权，条件与设零相同。
示教要求配置文件。
服务原子替换文件，保留所有者和权限，拒绝并发校准变更或链接。
保存的文件采用支持的校准结构，不保留格式和注释。

### 命名位置

`Named positions` 为每个配置的位置显示一个按钮，例如 `Insert → load`。
该轴空闲且脉冲计数精确匹配目标时，按钮高亮。
命名位置以零点为基准，单位为毫米。显示的毫米值来自脉冲计数和校准，不是实测行程。
运动期间，该轴的移动按钮不可用，`Stop stage` 仍可用。
控制台不排队，也不自动重试移动。

### 步进移动

每个轴有独立的 `Steps` 和 `Pulses/s` 输入：

| 输入 | 范围 | 默认值 |
| --- | --- | --- |
| `Steps` | 1–100000 脉冲 | 50 |
| `Pulses/s` | 1–1000 脉冲/秒 | 50 |

点击 `Step −` 或 `Step +` 时，电机按设定速度移动设定的步数。
目标超出软限位时，服务拒绝移动，返回 `Stage target is outside the configured soft limits.`。
步进移动有加减速，默认加速度为 50 脉冲/秒²。
距离短时，电机可能达不到设定速度。

步进移动结束后，该电机冷却 1 秒，之后才能再次移动，状态显示 `Cooling down`。
服务也检查冷却，多个页面或快速连击都不能绕过。
其他电机不受此冷却影响。
输入只属于当前页面，刷新后恢复默认值。

### 停止

`Stop stage` 停止所有电机的脉冲输出，移动和冷却期间都可用。
控制台的 `STOP ALL MOTION` 也停止位移台。
停止后，线圈保持通电，直到驱动器睡眠。

停止服务时，服务先发送 `stop all` 并断开串口，再退出。
进程被强制结束或崩溃时，服务不发送停止命令，已开始的步进移动继续到终点。

USB 断开时，服务不能保证停止，已开始的步进移动可能走完。
服务失去 Arduino 连接时，尝试发送 `stop all`，再关闭连接。
此时卡片状态显示 `Connection lost; motor stop could not be confirmed.`。
到现场确认电机已停止，再重新连接。
服务不自动重连，也不重试移动。

## 接口

所有路由位于 `<base>/contract/api/` 下。`<base>` 为 `/`。
POST 必须带 JSON 正文和 `X-MG400-Client`。

| 路由 | 请求 | 用途 |
| --- | --- | --- |
| `GET contract/api/describe` | 无 | 读取命令参数、限制、演示标志和租约规则 |
| `GET contract/api/state` | 无 | 读取连接、电机、细分模式、参考元数据和租约 |
| `GET contract/api/events?since=0` | 无 | 读取活动事件 |
| `POST contract/api/heartbeat` | `{}` | 取得或续期控制租约 |
| `POST contract/api/renew` | `{}` | 续期调用者持有的租约 |
| `POST contract/api/release` | `{}` | 释放租约，不停止运动 |
| `POST contract/api/command` | `action` 和参数 | 发送契约命令 |
| `POST contract/api/stop` | `{"wait_s": 0}` | 停止所有电机，不需要租约 |
| `GET contract/api/ports` | 无 | 列出可连接的串口 |
| `POST contract/api/connect` | `{"port": "..."}` | 连接列出的串口，要求租约 |
| `GET contract/api/motors/<motor>/reference` | 无 | 读取相机图像和标记偏移 |
| `POST contract/api/motors/<motor>/teach_reference` | `{"confirm": true}` | 保存当前标记位置为参考，要求租约 |

`<motor>` 取 `bottom`、`top` 或 `insert`。
契约命令为 `move`、`hold`、`keepalive`、`release_hold`、`stop_all`、`set_microstep`、`home`、`zero`、`awake`、`goto` 和 `pose`。
`zero` 要求 `motor` 和 `confirm: true`。
`pose` 按配置顺序逐轴移动，每次只移动 1 轴。
`awake` 延长驱动器通电 0–60 秒，不发送 STEP 脉冲，也不建立参考。0 取消延长。
演示模式的 `connect` 只接受 `DEMO`。
已连接时，`connect` 返回 `Disconnect the current board before choosing another port.`。
图像仅通过参考路由返回。状态回复仅含参考元数据，不含 JPEG。

状态中的 `feedback_age_s` 从最近一次完整开发板回复计时。只有回复不超过 2 s，`feedback_fresh` 才为真。
各电机的 `automation_ready` 要求反馈新鲜，并已确认当前细分模式下的参考和软限位。
只有电机空闲且脉冲计数精确匹配配置目标时，`at_positions` 才列出该命名位置。不要假定 `load` 为零。
释放租约会取消序列的剩余步骤，即使同一客户端再次取得控制权。续期保留当前序列。
参数和完成判定见 [位移台自动化契约](development.md#位移台自动化契约)。

## 服务配置

服务名为 `mg400-stage-panel.service`，由 [主机部署](deploy.md) 安装，`mg400ctl` 名称为 `stage`。
服务以数字 ID `1000` 的用户和组运行，并加入 `dialout` 组，用于读写串口。
服务手动启动，未设置开机自启。
服务启动不打开串口，在控制台点击 `Connect` 后才打开。
参考示教会更新位移台校准文件。服务将输出发送到 systemd 日志。

### 配置项

服务监听 `127.0.0.1:8767`，基础路径为 `/`，契约位于其下的 `contract/`。
参数 `--bind`、`--port` 和 `--base-path` 覆盖这些固定默认值。
服务不检查令牌、`Host` 或 `Origin`。VPN 是访问边界。

| 环境变量 | 参数 | 默认值 | 用途 |
| --- | --- | --- | --- |
| `MG400_STAGE_SERIAL` | `--serial` | 无 | Arduino 的串口路径 |
| `MG400_STAGE_CONFIG` | `--config` | `/var/lib/mg400/stage/stage.toml` | 位移台实测校准和命名目标 |

值为空的环境变量等同于未设置。

设置 `MG400_STAGE_SERIAL` 时，串口列表只显示该设备，`connect` 也只接受该设备。
建议使用 `/dev/serial/by-id/` 下的路径，重新插拔后路径不变。
该设备不存在时，列表为空。
路径指向夹爪转换器时，服务拒绝启动。
未设置时，列表显示除夹爪转换器外的所有串口。
演示模式忽略此项。

### 位移台校准

使用 H-07 的测量值和行程限位。不要根据脉冲计数推测校准，也不要把演示几何值用于硬件。
文件包含 `motors` 表和可选的 `poses` 表，没有生产默认值。
使用普通文件。读取器拒绝文件路径中的符号链接。
安装器创建 `/var/lib/mg400/stage`，所有者为 `1000:1000`，权限为 `0700`，供服务写入示教参考。
父目录 `/var/lib/mg400` 仍由 root 拥有，权限为 `0750`。
若 `/var/lib/mg400/stage.toml` 存在，安装器只打印移动提示，不移动或覆盖文件。
停止位移台服务后，检查两个路径，手动将旧文件移到 `/var/lib/mg400/stage/stage.toml`。
保留所有者 `1000:1000` 和文件权限 `0600` 或 `0640`。重启前更新旧的 `MG400_STAGE_CONFIG` 覆盖配置。
`motors.bottom`、`motors.top` 和 `motors.insert` 各包含：

| 字段 | 值 |
| --- | --- |
| `steps_per_mm` | 校准细分模式下，每毫米的脉冲数，必须为有限正数。 |
| `microstep` | 校准模式：1、2、4、8 或 16。 |
| `min_mm`、`max_mm` | 相对于零点的行程边界，必须有限且按大小排列。 |
| `positions` | 可选的名称与毫米位置表。Insert 必须包含 `load` 和 `furnace`。 |
| `reference` | 可选相机参考，含 `stream`、`marker`、`pixel` 和 `tolerance_px`。 |

各 `poses.<name>` 表包含 `order` 和 `positions`。
`order` 按移动顺序列出 `bottom`、`top` 和 `insert`，各出现 1 次。
`positions` 给出各轴限位范围内的毫米位置。
`furnace` 位姿表示坩埚卸载开始时的插入位置。
控制台不显示位姿按钮。位姿通过契约命令 `pose` 使用。

主机按 `steps_per_mm * current_microstep / microstep` 计算每毫米的脉冲数。
边界向内取整，目标取最近的脉冲。取整后再次检查目标是否位于脉冲边界内。
回零或手动设零后，主机发送软限位并等待确认，再启用移动。
文件缺失或无效时，参考和移动控件不可用，`Stop stage` 仍可用。

未显式指定配置时，演示模式使用独立的合成几何值，供离线练习。
显式指定的 `--config` 或 `MG400_STAGE_CONFIG` 也适用于演示模式。
合成几何值不是实测校准。使用前，验证硬件配置。

### 配置文件

服务从 `/etc/mg400/mg400.env` 读取配置。
按本主机修改各项。

接上 Arduino 后，查找它的串口路径：

```sh
ls -lL /dev/serial/by-id/
```

每一项是一个 USB 串口设备，名称通常含厂商、型号和序列号，插拔后不变。
名称含 `BG02Q5TN` 的一项是夹爪转换器，不是 Arduino。
`-L` 显示链接所指设备的权限和属组，不显示设备名。
确认 Arduino 一项的属组为 `dialout`。
将 `MG400_STAGE_SERIAL` 设为 `/dev/serial/by-id/` 加 Arduino 的名称。

修改配置文件后，重启服务。

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
重启后，服务未连接 Arduino，在控制台重新连接。
主机重启后，手动启动服务。

更新代码后，按 [主机部署](deploy.md#安装与更新) 运行安装器，再运行 `mg400ctl restart stage`。

## 故障排查

以下报错显示在位移台卡片或控制台通知中，服务启动失败的报错显示在服务日志中。

| 现象 | 处理 |
| --- | --- |
| 串口列表显示 `No serial port found`，或找不到 Arduino | 在宿主机运行 `ls -l /dev/serial/by-id/`。没有 Arduino 时，检查 USB 数据线和 Arduino 供电，再重新加载控制台页面。设置了 `MG400_STAGE_SERIAL` 时，确认它与 Arduino 的路径相同。 |
| `That port is no longer available. Refresh the port list.` | 重新加载控制台页面，再选择串口。 |
| `Cannot open <串口>: ...`，其中含 `Permission denied` | 服务用户不能读写该串口。确认设备属组为 `dialout`。本地运行时，把用户加入 `dialout` 组并重新登录。 |
| `<串口> is in use by another program.` | 另一个程序占用该串口。关闭该程序，例如 Arduino IDE 的串口监视器或另一个 `stage-panel` 进程。 |
| `Arduino did not reply in time. Check the port and uploaded sketch.` | 确认所选串口是 Arduino，并已上传 `firmware/three-motor-demo`。出错后服务断开连接，修复后再点击 `Connect`。 |
| `Another client controls it` | 另一个客户端持有租约。等它释放控制权，再点击 `Take control`。 |
| `position unknown, home first` | 命名位置或毫米目标需要已确认的参考。按 [设零与相机参考](#设零与相机参考) 设零，或回零。 |
| `Stage target is outside the configured soft limits.` | 减少步数，或改变方向。 |
| 卡片显示 `Not running` 或 `Not reachable` | 在宿主机运行 `mg400ctl status`，确认 `stage` 已启动。 |
| 服务日志显示 `Failed to load environment files` | 按 [主机部署](deploy.md#安装与更新) 运行安装器，创建 `/etc/mg400/mg400.env`，再启动服务。 |
| 服务日志显示 `MG400_STAGE_SERIAL points to the gripper USB converter (BG02Q5TN)` | 按 [配置文件](#配置文件) 查找 Arduino 的路径，改写 `MG400_STAGE_SERIAL`，再启动服务。 |
