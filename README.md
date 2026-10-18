# MG400 Agent

Dobot MG400 + PGEA 电动夹爪的仿真、真机控制和 Python agent 项目。
主要工作流是 Olares 上的 Isaac Sim 场景与真机面板。
MuJoCo 用于离线关节控制和轨迹回放。

`mg400-agent` 默认使用内存模拟器，无需机械臂或 API key。
它支持「读取场景 → 抓取 → 放置 → 反馈」，不含物理、运动学或碰撞检测。
agent 抓放流程目前仅支持内存模拟，通用真机运动适配器尚未实现。
示例坐标不能直接用于真机。

## Isaac Sim 与真机入口

保持 LarePass VPN 和现有 WebRTC Streaming Client 连接。
在 Olares 的 Isaac Sim 6.1 中打开 `/root/Documents/mg400-isaac/00_OPEN_MG400.usda`。
场景已保存机器人、物理夹爪、灯光、支撑台、测试方块和动作预设。
在 **Window → Script Editor** 运行仿真动作面板：

```python
import runpy

runpy.run_path("/root/Documents/mg400-isaac/apps/isaacsim/actions.py")
```

面板加载时不运动，点击动作按钮后才开始物理仿真。
它不重载场景、不重置机器人，也不连接真机。
每次动作结束自动暂停，**Pause** 保留当前状态。
Isaac 时间轴的 **Stop** 会恢复场景保存的初始状态。

真机功能使用独立入口：

| 入口 | 用途 |
| --- | --- |
| `apps/isaacsim/mirror.py` | 只接收真机四轴反馈，更新场景姿态 |
| `apps/isaacsim/joint_control.py` | 四轴面板，通过 Olares 直连 MG400 |
| `apps/isaacsim/gripper_control.py` | 实体夹爪面板，通过 MG400 I/O 开合 |

四轴面板启动只读，必须手动使能和执行，每次每轴最多变化 5°。
夹爪面板启动只读，点击开合按钮会驱动实体夹爪，但不会使能或移动机械臂。
三个真机入口共用 `config/robot.toml` 中 `[robot]` 表的 `host`。
修改地址后，重新启动镜像和面板，详见 [真机地址配置](docs/isaacsim.md#真机地址配置)。
上传清单、运行步骤和停止条件见 [Isaac Sim 使用说明](docs/isaacsim.md)。
自然语言真机入口仅提供 J4 +2° 后回位，见 [受限真机试验说明](docs/hardware.md#自然语言-j4-受限试验)。

Isaac 夹持由 PhysX 接触与摩擦承载，夹指尺寸、摩擦及质量分配仍是估算。
机械臂使用同步关节驱动近似连杆关系，尚不包含整机碰撞或真实闭环连杆动力学。
模型层次、基础导入和自动验收入口见 [Isaac Sim 文件说明](docs/isaacsim.md#文件)。

## 内存模拟快速开始

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

## MuJoCo 辅助工作流

MuJoCo 支持自然语言四轴控制、连续对话和离线轨迹回放，所有入口均不连接机械臂。
先运行无需 API key 的演示，确认 J1 转到 20°，再回到四轴零目标：

```bash
uv run --extra mujoco mg400-agent --backend mujoco --viewer demo
```

按下方 [模型 API 配置](#模型-api-配置) 设置 `.env`，再启动连续对话：

```bash
uv run --env-file .env --extra llm --extra mujoco mg400-agent --backend mujoco --viewer chat
```

输入“把第一轴转到20度”或“回到初始姿态”，输入 `quit`、`exit`、`退出` 结束。
也可按 Ctrl+C 或关闭窗口。`stop` 或错误会冻结会话，必须重新启动才能继续运动。
夹爪只能随法兰显示，目前不能开合、抓放或验证碰撞。
执行器参数和参考限位未经真机标定。

离线轨迹回放无需模型 API：

```bash
uv run --extra mujoco mg400-mujoco
uv run --extra mujoco mg400-mujoco --viewer
uv run --extra mujoco mg400-mujoco --dynamics --viewer
```

单次任务、工具参数、限速、到位标准和自定义轨迹见 [MuJoCo 使用说明](docs/mujoco.md)。

## 模型 API 配置

自然语言入口支持 OpenAI Responses API 和兼容服务的 Chat Completions。
服务和模型必须支持结构化函数工具调用，仅能回答 “Hello” 不足以证明可以执行关节动作。
模型按一个动作一步执行，API 接收任务文本、工具定义和所选后端状态。
API key 按服务要求配置，普通 `demo` 不调用模型。

如果没有 `.env`，复制 `.env.example`。已有文件时直接编辑，不要覆盖。
Qwen 服务使用以下配置，允许保留空 key：

```dotenv
OPENAI_API_KEY=
OPENAI_MODEL=Olares/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL
OPENAI_API_MODE=chat_completions
OPENAI_BASE_URL=https://366ada1d.chrisjuan.olares.com/v1
```

另一台 Qwen 服务地址为 `https://366ada1d.qumusphusis.olares.com/v1`。
切换服务时，更改 `OPENAI_BASE_URL`，地址必须包含协议和 `/v1`。
这两台服务使用 OpenAI 兼容的 Chat Completions 接口。
显式配置 `OPENAI_BASE_URL` 的 `chat_completions` 服务允许 key 留空。
若服务器要求认证，请填写本地 key，程序不会在认证失败后自动尝试无认证请求。

使用 OpenAI 官方服务时，设置 `OPENAI_API_MODE=responses`（默认）。
不设置 `OPENAI_BASE_URL`，填写 `OPENAI_API_KEY` 和 `OPENAI_MODEL`。
程序不会自动读取 `.env`，下列命令通过 uv 加载，也可直接设置环境变量：

```bash
uv sync --extra llm
uv run --env-file .env --extra llm mg400-agent ask "把红色积木 red_block 放到 box_a"
```

`ask` 和 `chat` 可以追加 `--model 模型ID` 覆盖 `OPENAI_MODEL`。
`.env` 已加入 `.gitignore`。API key 只需保存在本地环境或 `.env`，不要粘贴到聊天或输出日志中。

## 代码结构

```text
config/demo.toml              模拟场景、命名目标、单位、工作空间范围
config/robot.toml             Isaac 真机入口共用的机器人地址
00_OPEN_MG400.usda            Isaac 日常场景入口
isaacsim/                    USD 模型、物理夹爪覆盖层与工作场景
apps/isaacsim/               Isaac 仿真、真机面板与验收入口
examples/                    标准库单文件教学、受限 J4 试验与 notebook
scripts/                     模型构建、串流客户端操作与夹爪反馈采集
src/mg400_agent/
  cli.py                     state / demo / ask / chat 命令入口
  agent.py                   可选的大模型工具调用循环
  skills.py                  pick / place、动作顺序、状态与参数检查
  mujoco_skills.py            MuJoCo 关节工具、范围检查与动力学到位确认
  mujoco_session.py           持续对话、单次任务与 viewer 生命周期
  config.py                  四轴 TCP 位姿与 TOML 配置校验
  backends.py                Arm / Gripper 接口与内存模拟实现
  kinematics.py              四轴到模型八关节的映射
  controller_protocol.py     真机 TCP 命令应答解析
  feedback.py                真机关节反馈读取
  joint_control.py           受限真机四轴控制
  gripper_feedback.py        夹爪位置反馈读取
  hardware.py                MG400 真机适配入口（待实现）
docs/hardware.md             当前真机入口、通用适配要求与可选 ROS 2 路线
tests/                       动作失败、状态变化、agent 循环与 CLI 测试
```

内存抓放数据流：用户指令 → agent → 白名单技能 → Arm / Gripper → 执行反馈。
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

现有真机四轴和夹爪面板通过直接 TCP 接入，通用 `Arm` / `Gripper` 适配器仍待实现。
后续可复用直接 TCP 协议，也可接入 ROS 2，当前没有 ROS 2 后端。
先核对固件、夹爪型号、I/O 与 Modbus 配置，再实现 `hardware.connect_mg400()`。
接口要求「确认到位 / 确认夹持」，不能把命令已接收当作动作成功。
详见 [真机接入说明](docs/hardware.md)。

## 开发检查

```bash
uv sync --all-extras --group usd
uv run --all-extras --group usd pytest -ra
uv run ruff check .
uv run ruff format --check .
```

使用 `examples/test.ipynb` 时，给同步命令加 `--group notebook`。
如需保留其他手工安装的包，再加 `--inexact`。
测试不消耗模型额度，不连接机械臂，也不启动 Isaac Sim。
`--all-extras` 安装 MuJoCo 等可选依赖，`--group usd` 安装 USD 检查依赖。
只运行 `uv run pytest` 时，缺少依赖的模型测试会跳过，不能据此判断全部检查已通过。
`-ra` 显示跳过原因。
自然语言入口依据 [OpenAI 官方函数调用文档](https://developers.openai.com/api/docs/guides/function-calling) 实现。
