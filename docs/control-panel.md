# 网页面板

[返回项目首页](../README.md)

网页面板可直接控制 MG400 和 PGEA 夹爪，不依赖 Isaac Sim。
它使用 Python 3.11+ 标准库，提供真机控制和只读反馈。
所有模式均不支持键盘点动，也不更新 Isaac 场景。

## 使用前

服务默认使用真机控制模式。
启动服务和打开网页不会连接机械臂或发送动作。
使用前，关闭 Isaac 控制面板和其他控制程序。
多个程序查询控制器时，连接可能互相中断。

Isaac 面板和网页面板用文件锁限制真机连接，同一机械臂只允许一个面板连接。
网页的只读模式也使用此锁。
部署在容器和宿主机时，按 [面板连接锁](olares.md#面板连接锁) 配置共享目录。
此锁只协调本项目的面板，不能阻止 DobotStudio 或其他主机连接。

## 打开面板

### 使用 Olares 已部署的服务

保持 LarePass VPN 连接。
VPN 访问控制规则必须允许 TCP `8766`。
打开 [Olares 网页面板](http://100.64.0.1:8766/)。

若出现认证框，输入访问密钥，再点击 `Connect`。
访问密钥保存在宿主机 `/opt/mg400-web-control/.env`。
本机备份为 `work/.web-panel-token`，该文件不纳入 Git。
服务未启动时，参见 [服务配置](#服务配置)。

### 在本地启动

在能访问机械臂的主机进入项目目录，运行：

```sh
python3 apps/control_panel/server.py
```

在该主机打开 [本地网页面板](http://127.0.0.1:8766/)。
省略 `--mode` 时，服务使用 `robot` 模式。
机械臂地址默认取自 `config/robot.toml`，可用 `--robot-host` 指定。

## 控制机械臂

按以下顺序操作：

1. 点击 `Connect & take control`，等待实测坐标。
2. 点击 `Enable robot`，使能机械臂。
3. 使能完成后，点击 `Use actual`。
4. 修改 `Target` 中的 X/Y/Z（mm）和 R（度）。
5. 点击 `Execute targets`，等待到位确认。

`Use actual` 将实测坐标复制到目标。
坐标使用基座坐标系和法兰原点（`User=0,Tool=0`）。
滑块范围是实测值附近的调节区间，不是真机限位。
执行前，程序用控制器逆解和现有关节范围检查验证目标。

`Home` 将 J1–J4 移动到 0°。
直线运动使用 `SpeedL=1,AccL=1`，回零使用 `SpeedJ=5,AccJ=5`。

## 控制夹爪

夹爪使用同主机的 USB 采集程序。
采集程序必须带 `--commands`，且必须与网页服务共享单调时钟。
先按 [USB 接在 Olares](olares.md#usb-接在-olares) 安装并启动采集服务。
再启动网页服务，用 `--gripper-feedback` 指定采集文件。
以下示例使用 Isaac 容器内的数据路径：

```sh
python3 apps/control_panel/server.py --mode robot \
  --gripper-feedback /root/Documents/mg400-data/work/gripper_olares_feedback.json
```

`--gripper-feedback` 必须是网页进程可读的路径。
在 Olares 宿主机运行时，按 [USB 配置](olares.md#usb-接在-olares) 的 `hostPath` 映射改写路径。
网页服务不会自动启动采集程序或初始化夹爪。
没有夹爪反馈时，机械臂仍可单独控制。

夹持物体时不要初始化。
初始化会张开夹指寻找行程端点。
按以下顺序操作：

1. 未初始化时，点击 `Initialize gripper`。
2. 修改 `Gripper` 开口，0 表示闭合，1000 表示全开。
3. 点击 `Execute targets`。

机械臂到位确认后，服务才执行夹爪目标。
`Stop arm` 和下使能不会撤回已发送的夹爪命令。

## 停止与恢复

正常结束时，点击 `Disable robot`，再点击 `Disconnect`。
`Stop arm` 请求受控停止，不是物理急停。
若停止或命令结果无法确认，使用现场物理急停。

同一时刻只有一个页面持有控制权。
页面关闭或心跳超时后，服务请求停止机械臂并锁定控制。
进程退出或网络故障时，停止命令可能无法送达。
机械臂可能继续到已发送轨迹的终点。

已确认停止后，可下使能并重新使能。
未知命令结果不会因重新连接页面而解除锁定。
若断开时无法确认停止，面板停止常规坐标查询。
此时保留反馈、连接锁和安全警告，另一个面板不能接管。
不要删除锁文件来解除占用。
文件删除后，两个进程可能各自持锁。

## 只读反馈

只读模式使用以下命令：

```sh
python3 apps/control_panel/server.py --mode read-only --robot-host 192.168.1.6
```

点击 `Start sync` 才连接 TCP `30004`，`Stop sync` 停止读取。
此模式只显示实测关节、控制器模式和报警标志。
它不提供坐标或夹爪反馈。

## 服务配置

Olares 部署目录为 `/opt/mg400-web-control`。
`mg400-web-panel.service` 使用宿主机 Python 3.12。
在 Olares 宿主机启动或检查服务：

```sh
sudo systemctl start mg400-web-panel.service
sudo systemctl status mg400-web-panel.service --no-pager
```

服务默认监听 `127.0.0.1:8766`，可用 `--bind` 和 `--port` 修改。
监听非回环地址时，必须设置至少 24 个字符的 `MG400_PANEL_TOKEN` 环境变量。
网页使用同一密钥访问 API。

反向代理保留路径前缀时，可加 `--base-path /mg400-panel/`。
网页和 API 均使用此前缀。
