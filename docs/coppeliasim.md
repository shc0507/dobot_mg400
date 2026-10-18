# MG400 → CoppeliaSim 单向同步显示

本功能只接收真机 TCP 30004 的关节反馈，再用本机 ZeroMQ Remote API 更新
CoppeliaSim 模型。机器人端没有发送操作，不连接运动端口或 dashboard。
普通运动仍由现有控制程序或示教器执行；同步程序不负责下发动作。

## 使用

运行环境参考：CoppeliaSim 4.10。

1. 启动 CoppeliaSim。
2. File → Open scene 打开项目中的 `coppeliasim/MG400_mirror.ttt`。
3. 保持 **SIMULATION STOPPED**，无需按播放。原有自动演示脚本已在这份副本中禁用。
4. 在项目根目录执行：

```bash
uv run --extra coppeliasim mg400-mirror \
  --host 192.168.1.6 --source-host 192.168.1.10
```

这两个 IP 分别是当前 MG400 和电脑机器人网口的地址。
如果系统路由已能正确选择网口，可以省略 `--source-host`。
默认连接 CoppeliaSim `127.0.0.1:23000`，确保其 ZMQ Remote API server 已启动。
依赖缺失、模型未加载、连接失败会在终端报告。

程序每秒输出一次实际四轴角度（度）、机器人模式、报警标志、累计更新次数，
以及八个模型关节的回读误差（弧度）。默认最多更新 30 次/秒，实际频率取决于反馈和渲染。
按 Ctrl+C 断开显示。窗口保留最后一次姿态，该姿态不再代表实时状态；真机继续由原控制端负责。

短时验证或调整刷新率：

```bash
uv run --extra coppeliasim mg400-mirror \
  --host 192.168.1.6 --source-host 192.168.1.10 --duration 5 --rate 30
```

`--sim-host` / `--sim-port` 可指定仿真接口；`--model` 指定已加载模型路径（默认 `/MG400`）。
`--timeout` 控制反馈帧和 Remote API 的 I/O 超时，默认 2 秒。
反馈或 Remote API 暂时断线、超时后，程序报告 `reconnecting`，并每隔 1 秒尝试重新连接。
断开期间模型保留最后姿态，日志明确标记为非实时；恢复后重新读取反馈，成功更新后才报告 `live`。
重新加载同一模型导致临时脚本或关节绑定失效时，也会重新建立绑定。
非法反馈、模型缺失、模型回读不一致或启动仿真仍会使程序退出。
不会使能、清报警或重发运动。`--duration` 包含等待重连的时间。

## 数据与模型

反馈采用官方四轴 SDK 的 1440 字节布局，核对长度、魔数及四个有效关节值。
读取支持 TCP 分包和合包，丢弃已经积压的旧帧，只显示本次收到的最新完整反馈。
控制器时间字段的含义依固件而异，不作为新鲜度判断；超时使用电脑的单调时钟。

四轴实际角度先从度转换为弧度，再映射到模型：

| 模型关节 | 角度 |
| --- | --- |
| mg400_j1 | q1 |
| mg400_j2_1 | q2 |
| mg400_j2_2 | q2 |
| mg400_j3_1 | q3 − q2 |
| mg400_j3_2 | −q2 |
| mg400_j4_1 | −q3 |
| mg400_j4_2 | q3 |
| mg400_j5 | q4 |

每次更新在 CoppeliaSim 内批量设置并回读八个关节，误差大于 1e-6 rad 即退出。
接入期间会创建一个临时 Lua customization script，断开或正常退出时尝试删除。
每次更新前检查脚本、模型和全部关节的唯一标识，避免场景变化后操作旧句柄。
若进程被强制终止留下该脚本，重新打开已保存场景即可清理；它本身不会自动驱动模型。

- `coppeliasim/MG400_mirror.ttt`：完整场景，可直接打开。
- `coppeliasim/MG400_mirror.ttm`：独立模型，可加载到其他场景（两种方式选一种）。
- `coppeliasim/LICENSE_HarvestX.txt`：原模型许可证。

模型的几何与关节关系来源于
[HarvestX/MG400_ROS2](https://github.com/HarvestX/MG400_ROS2/tree/1a8c7344be44c52030f7216c0f387873ed91fd11)。
原始演示文件保留不变。这里显示的是关节姿态，不包含夹爪、抓取、接触动力学或工作台坐标标定。

## 验证

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

单元测试使用模拟 socket 和模拟 CoppeliaSim 接口，不连接真实设备。
