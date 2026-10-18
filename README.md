# MG400 Agent

Dobot MG400 + 电动夹爪的 Python agent scaffold。默认使用内存模拟器，
可以跑通「读取场景 → 抓取 → 放置 → 反馈」流程，不需要机械臂或 API key。

**抓放流程目前仅支持内存模拟；通用真机运动适配器尚未实现。**

真机另有独立的自然语言 J4 +2° 后回位试验，见 [受限真机试验说明](docs/hardware.md#自然语言-j4-受限试验)。
MuJoCo 后端支持自然语言控制四轴关节、回到初始姿态，并在窗口中显示运动。
另有只读真机关节反馈 → CoppeliaSim 同步显示，见下方说明；它不使能或移动机械臂。
内存模拟器不含物理、运动学或碰撞检测，示例坐标不能直接用于真机。

## 快速开始

需要 Python 3.11+。以下命令在项目根目录执行：

```bash
uv sync
uv run mg400-agent state
uv run mg400-agent demo
uv run pytest
```

`demo` 将 `red_block` 从初始位置抓起，放到 `box_a`。输出每一步结果和最终状态：
`holding` 为 `null`，`occupied_locations` 包含 `box_a`，物体位置更新为目标位置。
每次命令创建一个新场景；状态仅在这次进程内保存。

没有 uv 时：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
mg400-agent demo
```

## Isaac Sim + Olares 串流

MG400 + PGEA 夹爪的原生 USD 模型包含八个连杆关节、固定底座、
原 CAD 材质颜色及夹爪安装关系。打开 `isaacsim/scene.usda` 可加载带灯光和相机的场景。
`isaacsim/mg400_with_gripper.usd` 可作为机器人资产引用到其他场景。

Olares 上的 Isaac Sim 6.1 使用现有 WebRTC Streaming Client。在 Script Editor 中执行
`examples/isaac_sim_load_mg400.py`，会备份当前场景、加载模型并检查四轴到八关节的映射。
物理夹爪入口为 `examples/isaac_sim_grasp_mg400.py`：加载 `isaacsim/grasp_scene.usda`，
通过两个直线关节开合，测试夹住 5 g 物体、提起、保持及释放，结果写入
`isaacsim/grasp_validation.json`。覆盖层 `isaacsim/mg400_grasp.usda` 保留基础资产。
全模型包含八个旋转自由度和两个夹爪直线自由度。
真机四轴面板入口为 `examples/isaac_sim_control_mg400.py`：在 Isaac 内输入 J1–J4
绝对角度，点击执行后通过 Olares 直连 MG400；场景继续显示真机反馈。
打开面板只读，需手动使能和执行。每次每轴最多变化 5°。
上传步骤、运行命令和模型范围见 [Isaac Sim 使用说明](docs/isaacsim.md)。
夹持由 PhysX 接触与摩擦承载；夹指尺寸、摩擦及质量分配仍是估算。
机械臂使用同步关节驱动近似连杆关系，尚不包含整机碰撞或真实闭环连杆动力学。

## MuJoCo 自然语言关节控制

不接机械臂即可让 agent 控制窗口中的 MG400。先运行无 API 的关节演示，
确认窗口能显示 J1 转到 20°、再回到四轴零目标的运动：

```bash
uv run --extra mujoco mg400-agent --backend mujoco --viewer demo
```

需要 Python 3.11+ 和支持函数工具调用的模型 API。如果还没有 `.env`，复制
`.env.example`。已有文件时直接编辑，避免覆盖。Qwen 服务使用以下配置，
当前服务可保留空 key 直接运行：

```dotenv
OPENAI_API_KEY=
OPENAI_MODEL=Olares/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL
OPENAI_API_MODE=chat_completions
OPENAI_BASE_URL=https://366ada1d.chrisjuan.olares.com/v1
```

如服务要求认证，再将 key 填入本地 `.env`，无需发到聊天中。然后启动连续对话：

```bash
uv run --env-file .env --extra llm --extra mujoco mg400-agent --backend mujoco --viewer chat
```

在终端逐条输入，例如：

```text
把第一轴转到20度
第二轴转到15度，第三轴转到30度
第一轴再增加10度
读取当前关节角
回到初始姿态
退出
```

同一窗口和关节状态会持续保留，agent 会保留对话上下文。等待输入或 API 时暂停物理步进，
窗口仍可交互。输入 `quit`、`exit`、`退出`、按 Ctrl+C 或关闭窗口结束。
初始姿态是四轴目标 `[0, 0, 0, 0]`，回位通过执行器运动完成。

也可单次执行，或不带 `--viewer` 无窗口运行；`state` 和 `demo` 不需要 API key：

```bash
uv run --env-file .env --extra llm --extra mujoco mg400-agent --backend mujoco --viewer ask "把第一轴转到20度，然后回到初始姿态"
uv run --extra mujoco mg400-agent --backend mujoco state
```

单次 `ask` / `demo` 完成后关闭窗口。关节路径限速为 15°/s，执行前检查参考限位，
执行后确认实际到位。`stop` 或错误会冻结当前会话，需重新启动才能继续运动。
夹爪随法兰一起显示，目前不能开合、抓放或验证碰撞。详见 [MuJoCo 使用说明](docs/mujoco.md)。

## MuJoCo 离线轨迹回放

不接机械臂即可显示 MG400 模型、回放四轴轨迹，并读取 CAD 模型的法兰位姿。
模型按实物照片同轴安装 PGEA 侧出线版 CAD，保留原有法兰；黑色加长指尖为估算外形。
可选动力学模式通过四轴执行器、重力和理想连杆约束跟踪轨迹，并检查参考关节范围。
尚未接入 `pick/place` 或夹爪接触；执行器参数与限位参考均未经真机标定。

```bash
uv run --extra mujoco mg400-mujoco
uv run --extra mujoco mg400-mujoco --viewer
uv run --extra mujoco mg400-mujoco --dynamics --viewer
```

默认播放内置的 8 秒演示轨迹。无窗口模式快速运行并输出 JSON；viewer 按实际时间播放，
完成后关闭。动力学模式还会等待末端关节目标稳定，并报告实际跟踪误差。
Mac 入口会自动调用 `mjpython`，并处理 uv Python 的动态库搜索路径。
自定义轨迹、坐标系说明和已知限制见 [MuJoCo 使用说明](docs/mujoco.md)。

## CoppeliaSim 真机同步显示

读取真机 TCP 30004 的关节反馈。
将四轴角度映射到 MG400 运动学模型的八个关节（含平行连杆）。
这条通路不依赖抓放任务或 API key。

1. 在 CoppeliaSim 4.10 中打开 `coppeliasim/MG400_mirror.ttt`，保持仿真停止。
2. 在本项目目录运行：

```bash
uv run --extra coppeliasim mg400-mirror --host 192.168.1.6 --source-host 192.168.1.10
```

真机由你现有的控制程序或示教器运动，窗口中的 MG400 会跟随实际关节反馈。
终端按 Ctrl+C 结束显示；不会停止或改变真机。无需点击仿真播放按钮。
IP 是当前连接配置，换设备时按实际地址修改。

详见 [同步显示说明](docs/coppeliasim.md)，包括端口、模型文件、刷新率和断线处理。

## 接入自然语言

支持 OpenAI Responses API 和兼容服务的 Chat Completions 函数工具调用，模型按一个动作一步执行。
该入口需要可用模型，API key 按服务要求配置；发送任务文本、工具定义和模拟状态。
默认 `OPENAI_API_MODE=responses`；使用上述 Qwen 服务时设为 `chat_completions`，
同时配置 `OPENAI_BASE_URL`。使用 OpenAI 官方服务时不需要设置 `OPENAI_BASE_URL`。

```bash
uv sync --extra llm
# 仅在没有 .env 时复制 .env.example，已有配置请直接编辑。
# 在 .env 中填写 OPENAI_API_KEY 和 OPENAI_MODEL
uv run --env-file .env --extra llm mg400-agent ask "把红色积木 red_block 放到 box_a"
```

也可以通过环境变量和 `ask --model` 传入；程序本身不会自动读取 `.env`。
`.env` 已加入 `.gitignore`。普通 `demo` 是固定程序演示，不会假装理解自然语言。

## 代码结构

```text
config/demo.toml              模拟场景、命名目标、单位、工作空间范围
src/mg400_agent/
  cli.py                     state / demo / ask / chat 命令入口
  agent.py                   可选的大模型工具调用循环
  skills.py                  pick / place、动作顺序、状态与参数检查
  mujoco_skills.py            MuJoCo 关节工具、范围检查与动力学到位确认
  mujoco_session.py           持续对话、单次任务与 viewer 生命周期
  config.py                  四轴 TCP 位姿与 TOML 配置校验
  backends.py                Arm / Gripper 接口与内存模拟实现
  hardware.py                MG400 真机适配入口（待实现）
docs/hardware.md             HarvestX ROS 2 / 官方 SDK 映射与真机接入步骤
tests/                       动作失败、状态变化、agent 循环与 CLI 测试
```

数据流：用户指令 → agent → 白名单技能 → Arm / Gripper → 执行反馈。
MuJoCo 关节后端通过独立的关节工具执行动力学运动，工具及限制见 [MuJoCo 使用说明](docs/mujoco.md)。
当前无 ROS、视觉感知或 Web 服务。需要复杂避障时，再在执行层接运动规划器。

## 工具与执行约定

以下抓放工具用于默认内存模拟后端：

| 工具              | 参数          | 作用                                           |
| ----------------- | ------------- | ---------------------------------------------- |
| `get_scene`       | 无            | 已知物体和命名放置点，当前为配置场景           |
| `get_robot_state` | 无            | 位姿、持物、停止、故障状态                     |
| `pick`            | `object_id`   | 抬升到移动高度、接近物体、下降、确认抓取、抬升 |
| `place`           | `location_id` | 接近目标、下降、确认释放、抬升                 |
| `stop`            | 无            | 停止本次会话，保持夹爪状态                     |

- 模型只能选已知目标，不能发送任意坐标、脚本或底层 TCP 命令。
- 所有动作串行、阻塞到完成；不提供并发控制或后台任务队列。
- 任一失败会锁定本次会话，后续动作被拒绝；没有自动重试或自动恢复。
- `state_valid=false` 表示动作状态需要核对，缓存的持物和物体位置不能视作新观测。
- 大模型返回的文字仅作说明，判断执行结果应读取工具结果及最终状态。
- `action_timeout_s` 是传给适配器的执行期限；适配器必须实现超时，
  当前模拟器即时返回，执行器不会强行中断一个阻塞的硬件驱动。
- 工作空间仅检查数值范围，不能证明 MG400 可达或整条路径无碰撞。
  真实系统的限速、限位、急停与故障监测须在设备/控制器层实现。

## 修改示例场景

编辑 `config/demo.toml`。所有坐标采用同一基坐标系：`[x_mm, y_mm, z_mm, r_deg]`。
新增物体或目标后会自动出现在工具参数的枚举中。

```bash
uv run mg400-agent --config config/demo.toml demo --object red_block --destination box_a
```

当前场景假设静止物体、单次持物、单物体放置点。`travel_z_mm` 是模拟的移动高度，
不是经过验证的安全高度；真实夹爪 TCP、标定、障碍物和夹持反馈还未接入。

## 真机开发的下一步

ROS 2 接入优先参考
[HarvestX/MG400_ROS2](https://github.com/HarvestX/MG400_ROS2) 的 `humble` 分支。
具体版本、接口映射及取消动作的限制见 [真机接入说明](docs/hardware.md)。
计划在现有 `Arm` / `Gripper` 接口后接 ROS 2 客户端；目前尚未实现 ROS 2 后端。

先记录 MG400 固件版本、电动夹爪型号及通讯方式（I/O、Modbus 等），
再实现 `hardware.connect_mg400()` 返回的 `Arm` 和 `Gripper`。
接口要求「确认到位 / 确认夹持」，不能把命令已接收当作动作成功。
详见 [真机接入说明](docs/hardware.md)。

## 开发检查

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

测试只用模拟后端和模拟 API 响应，不消耗模型额度，不连接机械臂。
自然语言入口依据 [OpenAI 官方函数调用文档](https://developers.openai.com/api/docs/guides/function-calling) 实现。
