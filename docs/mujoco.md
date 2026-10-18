# MuJoCo 离线工作流

MuJoCo 支持运动学回放，以及带重力、四轴执行器和理想连杆约束的关节动力学模式。
`mg400-agent --backend mujoco` 支持自然语言关节控制和持续对话。
所有 MuJoCo 入口均不连接机械臂；`ask` / `chat` 需要模型 API，`state`、固定演示和
轨迹回放不需要 API key。内存抓放模拟和 CoppeliaSim 真机反馈镜像仍使用各自原有入口。

## 自然语言控制与连续窗口

先验证不依赖 API 的关节演示：

```bash
uv run --extra mujoco mg400-agent --backend mujoco --viewer demo
```

窗口中的 J1 通过执行器转到 20°，然后运动回四轴零目标。夹爪使用已打包的
PGEA 侧出线版外形，随法兰一起移动，无需重新生成模型。

项目根目录的 `.env` 可从 `.env.example` 新建；已有文件时不要覆盖。
以下 Qwen 服务使用此配置，允许保留空 key：

```dotenv
OPENAI_API_KEY=
OPENAI_MODEL=Olares/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL
OPENAI_API_MODE=chat_completions
OPENAI_BASE_URL=https://366ada1d.chrisjuan.olares.com/v1
```

另一台 Qwen 服务的配置地址为 `https://366ada1d.qumusphusis.olares.com/v1`。
切换服务时，更改 `OPENAI_BASE_URL`。按对应服务的要求在本地配置 key。
地址必须包含协议和 `/v1`。
这两台服务使用 OpenAI 兼容的 Chat Completions 接口。
使用 OpenAI 官方 Responses 接口时，设置 `OPENAI_API_MODE=responses`（默认），不设置 `OPENAI_BASE_URL`。

服务和模型必须支持结构化函数工具调用，仅能回答 “Hello” 不足以证明可以执行关节动作。
显式配置 `OPENAI_BASE_URL` 的 `chat_completions` 服务允许 key 留空；
若服务器要求认证，请填写本地 key，程序不会在认证失败后自动尝试无认证请求。
程序不会自动加载 `.env`，以下命令通过 uv 加载，也可直接设置环境变量。

```bash
# 连续对话：同一窗口、同一物理状态，保留对话上下文。
uv run --env-file .env --extra llm --extra mujoco mg400-agent --backend mujoco --viewer chat

# 单次任务：执行后输出结果并关闭窗口。
uv run --env-file .env --extra llm --extra mujoco mg400-agent --backend mujoco --viewer ask "把第一轴转到20度，然后回到初始姿态"

# 读取初始模型状态：无需 API key，也无需 demo.toml 配置。
uv run --extra mujoco mg400-agent --backend mujoco state
```

`ask` 和 `chat` 可以追加 `--model 模型ID` 覆盖 `OPENAI_MODEL`。
省略 `--viewer` 时无窗口运行；`demo` 固定执行动作，不调用模型。
API key 只需保存在本地环境或 `.env`，不要粘贴到聊天或输出日志中。

连续对话可依次输入“第一轴转到20度”“第一轴再增加10度”“读取当前关节角”
“回到初始姿态”。关节编号是控制器的 J1、J2、J3、J4，角度单位为度，
由执行层映射到模型的八个连杆关节。模型只选择工具，不直接写 `qpos` 或控制物理步进。

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `get_robot_state` | 无 | 读取实际关节角、目标和执行状态 |
| `move_joint` | `joint`：J1…J4；`angle_deg`；`relative` | 单轴绝对目标，或相对当前实际角度增加指定角度；其余轴保持原目标 |
| `move_joints` | `joints_deg`：四个角度 | 设置四轴绝对目标 |
| `home` | 无 | 运动回 `[0, 0, 0, 0]` 目标 |
| `stop` | 无 | 冻结当前会话，拒绝后续运动 |

模型仅在会话创建时初始化到四轴零位；后续移动和回位均更新执行器目标并调用
`mj_step()`，不重置物理状态。线性关节插值限制各轴速度不超过 15°/s，
沿途检查下述参考范围，终点使用同样的 0.25° / 0.5°/s 到位标准。
受重力和伺服误差影响，返回的实际角度可能不恰好等于零。

等待终端输入或模型响应时暂停物理步进，但继续刷新 viewer；收到动作后从保留的状态继续。
`chat` 在输入 `quit`、`exit`、`退出`、按 Ctrl+C 或关闭窗口时结束；
单次 `ask` / `demo` 完成后关闭窗口。所有动作串行执行。
错误（包括无效动作参数）或 `stop` 会冻结当前会话；执行器仍允许读取状态，
不会自动恢复或重试运动，需要退出后重新启动。

此版本只提供关节控制，不提供末端坐标、IK、夹爪开合或 `pick/place`。
场景禁用碰撞接触，关节限位检查不等于整条路径无碰撞。
自动化测试使用模拟 API 响应验证工具调用流程。

## 轨迹回放启动

在项目根目录运行：

```bash
# 无窗口快速回放，输出完成状态和最终法兰位姿。
uv run --extra mujoco mg400-mujoco

# 打开窗口，按实际时间播放内置的 8 秒轨迹。
uv run --extra mujoco mg400-mujoco --viewer

# 四轴执行器跟踪同一条轨迹；省略 --viewer 可无窗口快速验证。
uv run --extra mujoco mg400-mujoco --dynamics --viewer
```

运行环境参考：Python 3.12、MuJoCo 3.15.0、macOS。
依赖版本记录在 `uv.lock`，其中 MuJoCo 锁定为上述版本。
模型和演示数据包含在 Python 包中，安装后无需依赖参考仓库或 CoppeliaSim。

viewer 完成后自动关闭。提前关闭窗口或按 Ctrl+C 会取消回放，退出码为 130；
成功为 0，输入或运行错误为 1。无窗口模式不等待真实时间。

Mac 的 passive viewer 需要官方 `mjpython` 启动器。本项目的 `--viewer` 自动重启到
该启动器，并将当前 Python 的 `LIBDIR` 加入子进程的 `DYLD_FALLBACK_LIBRARY_PATH`，
修复 uv 虚拟环境中找不到 `libpython` 的问题；不会修改 shell 配置或系统目录。
参考：[官方 viewer 文档](https://mujoco.readthedocs.io/en/stable/python.html#passive-viewer)、
[上游 uv 问题记录](https://github.com/google-deepmind/mujoco/issues/1923)。

## 自定义轨迹

复制并修改 `src/mg400_agent/assets/mg400/demo_trajectory.json`，或创建自己的 JSON：

```json
[
  {"time_s": 0, "joints_deg": [0, 0, 0, 0]},
  {"time_s": 2, "joints_deg": [30, 20, 40, -30]},
  {"time_s": 4, "joints_deg": [0, 0, 0, 0]}
]
```

```bash
uv run --extra mujoco mg400-mujoco --trajectory path/to/trajectory.json --viewer
```

每帧的四个角度按控制器 J1、J2、J3、J4 排列，单位为度。时间从 0 开始且严格递增，
至少两帧，所有值必须是有限数字。程序在启动 viewer 前检查整个文件。
默认以 30 Hz 在相邻关键帧之间进行关节角线性插值；`--rate 60` 可改变采样频率。
最后一个关键帧始终只输出一次。角度按提供的数值插值，不自动选择跨 ±180° 的最短路径。
这是关节空间插值，不是直线 TCP 运动。运动学回放不验证关节限位；
动力学模式会应用下述参考范围。两种模式都没有速度/加速度规划或碰撞检测。

记录真机角度后，可以转换成此格式离线复现；目前没有新增真机录制命令。

## 动力学模式

`--dynamics` 使用单独的 `dynamics.xml`，初始化到轨迹首帧后，只更新四轴执行器目标，
调用 `mj_step()` 推进物理，不再按目标改写关节位置或速度。输入文件和插值路径会在创建
模型及打开窗口前完成范围检查。

物理和控制周期固定为 2 ms（500 Hz），`--rate` 在此模式下只控制 viewer 刷新率。
第三轴通过 fixed tendon 的线性组合 `j2_1+j3_1` 控制，四个等式约束维持平行连杆关系。
开启重力，采用 `implicitfast` 积分；执行器增益、力矩上限、阻尼和附加惯量是仿真
调试参数，数值及来源见模型 `NOTICE.txt`。它们不代表实际 MG400 伺服响应。

### 参考关节范围

范围来自固定版本 HarvestX `kinematics.hpp`，尚未与本机控制器校准。指令在范围内
向内保留 1.05° 余量；每个物理步进后检查实际四轴角度是否仍在原始参考范围内。

| 角度 | 原始参考范围（度） | 指令允许范围（度） |
| --- | --- | --- |
| J1、J4 | −160 … 160 | −158.95 … 158.95 |
| J2，J3 > 0 | −25 … 85 | −23.95 … 83.95 |
| J2，J3 ≤ 0 | −19 … 85 | −17.95 … 83.95 |
| J3 | −25 … 105 | −23.95 … 103.95 |
| J3 − J2 | −60 … 60 | −58.95 … 58.95 |

路径检查包括跨越 J3=0 的位置；只检查关键帧会漏掉这一条件边界。
不会将额外整圈归一化，例如 J4=280° 会被拒绝。
MJCF 中的范围是软约束，Python 状态检查负责在测量值越界时退出。

### 完成与失败

轨迹结束后，四轴实际误差均需不超过 0.25°，最大轴速度不超过 0.5°/s，连续保持
0.1 个仿真秒；最多再等待 2 个仿真秒。以下情况明确报错退出，不报告任务成功：

- 指令或实际关节状态超出参考范围。
- 任一轴跟踪误差超过 5°（通常需要放慢轨迹）。
- 连杆关系的最大误差超过 0.1°。
- MuJoCo 数值警告、非有限状态，或最终到位超时。

这些数值是当前仿真验收标准。`joints_deg` 来自实际 `qpos`，
`target_joints_deg` 是目标，`tracking_error_deg` 和 `linkage_error_deg` 分别报告两者
差距和连杆关系偏差；`peak_*` 给出整次运行的最大误差。
`simulation_time_s` 包含最终稳定时间。轨迹时长若不是 2 ms 的整数倍，会向上取整
到下一个物理步，误差小于 2 ms。

模型继续禁用碰撞接触；夹爪目前只有固定视觉外形，不能验证避障、抓取或接触力。

## PGEA-2-10 夹爪安装

两份 MuJoCo 场景采用用户提供的实物照片所示布局。
夹爪位于原法兰正下方，侧面出线，前端装有黑色加长夹指。
模型使用原法兰 `mg400_end_effector_flange` / `flange_0.stl`。
装配中没有蓝色 L 形支架或侧向偏置。

本体使用 PGEA-2-10/15-10 O-S CAD。
配套图纸给出的 O-S 长度为 89 mm，侧面出线，符合照片所示布局。
包装铭牌标注 94 mm，与 O-B 底部出线版一致。
模型按实物照片选择几何外形，包装与实际夹爪的版本对应关系尚未确认。
STL 使用米制，分别保留本体、银色前壳、左右标准滑块和侧面线缆。

当前安装定义如下（均相对于原法兰，+Z 朝上）：

- 后盖中心为 `[0, 0, 0]` mm，与原法兰底面同轴贴合；未单独增加转接片厚度。
- 夹爪长轴朝 -Z，绕 Z 转 90°，让侧出线朝法兰 -X（J4=0 时朝机器人后方）。
  照片不能确定精确的末端旋转零位，这个绕 Z 方向仍为装配假设。
- `pgea_jaw_center`：原 CAD 标准滑块前端中点，位于 `[0, 0, -89]` mm。
- 两个黑色指尖是按照片补充的简化长方体，各 10 × 9 × 15 mm，保持 CAD 当前的开口；
  不是厂家指尖 CAD，长度和截面均为估算值。
- `pgea_tip`：估算指尖末端中点，位于 `[0, 0, -104]` mm。
- `mg400_tcp` 与原输出 `tool_offset_mm=[0,0,0]` 仍保留法兰零工具参考定义，
  没有把估算的夹指长度作为标定后的工具 TCP。

夹爪仍是固定视觉装配，用于外形与运动跟随检查；未计入新增负载惯量，
未添加开合控制或接触几何。拿到实测指尖长度及安装尺寸后，再修正几何和工具 TCP。

先转换夹爪 CAD（仅重建需要 CADQuery，运行时不需要）：

```bash
uv run --no-project --with cadquery==2.8.0 python scripts/build_gripper_mesh.py
```

随后使用本文末尾的模型重建命令，或直接运行已打包的模型查看。

## 模型和坐标系

资产来自 HarvestX/MG400_ROS2，固定提交为
`1a8c7344be44c52030f7216c0f387873ed91fd11`。
DAE 网格应用原有场景变换后转换为 STL，保留材质颜色；MJCF 保留原 URDF
的关节树、轴、局部平移及惯量。许可证、来源和修改记录随模型打包在
`src/mg400_agent/assets/mg400/LICENSE` 和 `NOTICE.txt`。

MuJoCo 内部使用米、弧度；CLI 输入关节角为度，输出位置为毫米。

| 输出 / site | 含义 |
| --- | --- |
| `mg400_base` | 固定底座，和当前 world 重合 |
| `mg400_controller_origin` | 原 URDF 的 origin，位于底座的 `[-5, 0, 224]` mm，轴平行；未经真机标定 |
| `mg400_flange` | 原 URDF 的末端法兰坐标系 |
| `mg400_tcp` | 当前与法兰重合，尚未配置夹爪长度 |
| `pgea_jaw_center` | 标准滑块前端中点，法兰下方 89 mm |
| `pgea_tip` | 估算指尖末端中点，法兰下方 104 mm，不是标定后的真机 TCP |
| `flange_in_base_mm` | 法兰在底座坐标系中的位置 |
| `flange_in_urdf_origin_mm` | 法兰在上述 URDF origin 中的位置 |
| `flange_yaw_deg` | 实际法兰 yaw；运动学模式等于 J1+J4 折返到 ±180°，动力学模式包含约束残差 |

运动学模式沿用现有镜像映射，按名称设置八个模型关节，然后调用 `mj_forward()`。
此模式不调用 `mj_step()`，`scene.xml` 禁用了重力和接触，没有执行器或动力学约束。
动力学模式使用上文的 `dynamics.xml`，仅在初始化时设置一次耦合姿态。
两份模型的关节存储顺序都与显示名称顺序不同，不能直接把八个数顺序赋给 `data.qpos`。

参考仓库的简化解析 FK 与 CAD URDF 并不完全相同。在四轴全零时：

| 定义 | 法兰位置（mm） |
| --- | --- |
| CAD，底座坐标系 | `[278.448743, -0.000124, 353.251164]` |
| CAD，URDF origin 坐标系 | `[283.448743, -0.000124, 129.251164]` |
| 参考简化解析 FK，origin 坐标系 | `[284.5, 0, 122]` |

后两者相差约 7.327 mm，差异随姿态变化，不能用固定平移消除。
这些坐标不代表已经标定的真机 TCP。

## 检查与重建

```bash
uv run --extra mujoco pytest
uv run ruff check .
uv run ruff format --check .
```

测试检查 CAD FK、水平法兰和 yaw、八关节映射、位置单位和坐标系。
测试还检查重复回放无漂移、轨迹输入、插值端点，以及关闭窗口时及时取消。
动力学测试检查关节扰动恢复、关闭执行器后的重力下落、条件限位和异常退出。
未安装 MuJoCo 时，依赖真实模型的测试会跳过，其余项目测试仍可运行。

重建模型需要本地参考 Git 仓库中保留上述提交；支持 sparse checkout。
构建依赖和运行依赖分开：

```bash
uv run --no-project --with pycollada==0.9.3 --with xacro==2.1.1 \
  python scripts/build_mujoco_model.py
```

脚本重建固定来源的两份模型、网格、许可证和来源说明，保留自定义轨迹。
夹爪网格由 `build_gripper_mesh.py` 单独生成；指定其他输出目录时，模型重建脚本会复制
已转换的五个夹爪 STL，并保持安装结构。

## 当前限制

`mg400-agent --backend mujoco` 当前支持四轴关节控制。
`MuJoCoArm` 的末端坐标运动和 IK 尚未实现，也尚未接入抓放任务层。
抓放任务仍只有内存模拟，没有 MuJoCo 抓放演示。
场景不含桌面、积木、夹爪开合和接触模型，也不提供抓取或释放反馈。
仿真物体位置与持物状态尚未接入任务层。

抓放场景的不可达、碰撞、夹空、掉落和超时回归测试仍待补充。
坐标、参数和任务验收精度尚未用真机与实际夹爪标定。
