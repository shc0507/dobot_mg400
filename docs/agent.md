# 自然语言控制

[返回项目首页](../README.md)

本页说明模型 API 配置和真机 J4 受限试验。
离线四轴控制与连续对话见 [MuJoCo 使用说明](mujoco.md)。
运行环境要求 Python 3.11+。

## 模型 API 配置

自然语言入口支持 OpenAI Responses API 和兼容服务的 Chat Completions。
服务和模型必须支持结构化函数工具调用。
仅能回答“Hello”不足以证明可以执行关节动作。
模型按一个动作一步执行。
大模型返回的文字仅作说明。
判断执行结果时，必须读取工具结果及最终状态。
API 接收任务文本、工具定义和所选后端状态。
API key 按服务要求配置。

如果没有 `.env`，复制 `.env.example`。
已有文件时直接编辑，不要覆盖。
Qwen 服务使用以下配置，允许保留空 key：

```dotenv
OPENAI_API_KEY=
OPENAI_MODEL=Olares/unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_XL
OPENAI_API_MODE=chat_completions
OPENAI_BASE_URL=https://366ada1d.chrisjuan.olares.com/v1
```

另一台 Qwen 服务地址为 `https://366ada1d.qumusphusis.olares.com/v1`。
切换服务时，更改 `OPENAI_BASE_URL`。
地址必须包含协议和 `/v1`。
这两台服务使用 OpenAI 兼容的 Chat Completions 接口。
显式配置 `OPENAI_BASE_URL` 的 `chat_completions` 服务允许 key 留空。
若服务器要求认证，请填写本地 key。
程序不会在认证失败后自动尝试无认证请求。

使用 OpenAI 官方服务时，设置 `OPENAI_API_MODE=responses`（默认）。
不设置 `OPENAI_BASE_URL`，填写 `OPENAI_API_KEY` 和 `OPENAI_MODEL`。
程序不会自动读取 `.env`。
下列命令通过 uv 加载配置，也可直接设置环境变量：

```bash
uv sync --extra llm --extra mujoco
uv run --env-file .env --extra llm --extra mujoco mg400-agent --backend mujoco ask "把第一轴转到20度，然后回到初始姿态"
```

`ask` 和 `chat` 可以追加 `--model 模型ID`，覆盖 `OPENAI_MODEL`。
`.env` 已加入 `.gitignore`。
API key 只需保存在本地环境或 `.env`。
不要粘贴到聊天或输出日志中。

自然语言入口依据 [OpenAI 官方函数调用文档](https://developers.openai.com/api/docs/guides/function-calling) 实现。

## 自然语言 J4 受限试验

`examples/agent_j4_trial.py` 把固定中文任务交给 `.env` 配置的模型。
模型顺序调用 `get_robot_state`、`rotate_j4_out`、`return_to_start`。
执行器只接受空参数的预设工具。
程序以启动时读取的实际 J4 角度为起点。
J4 从起点增加 2°，到位后返回起点。每个动作最多执行一次。
此入口不支持通用真机聊天或抓放，也不使用仿真的四轴零位作为真机起点。
模型 API 地址、认证和 `.env` 用法见 [模型 API 配置](#模型-api-配置)。

先确认夹爪、线缆周围有空隙，且操作者在急停旁。
以下命令会实际使能和运动：

```bash
uv run --env-file .env --extra llm python examples/agent_j4_trial.py \
  --host 192.168.1.6 --source-host 192.168.1.10 --execute-j4-trial
```

这两个 IP 分别是 MG400 和运行试验的电脑机器人网口地址。
如果系统路由能正确选择网口，可以省略 `--source-host`。
在 Olares 容器内运行时，不要绑定宿主机的源 IP。

试验使用每条指令的 `SpeedJ=5,AccJ=5` 比例参数。
它不改全局速度、负载、工具坐标或夹爪输出。
被动反馈超过 0.25 秒未刷新、报警或离开限定关节区间会中止动作。
到位要求新鲜反馈在目标 ±0.1° 内稳定至少 0.4 秒，并由 dashboard 再核对。

发生故障不重发运动，也不自动回位。
必要时请求 `ResetRobot()` 并核实静止。
如果无法确认停止，终端会明确要求操作者使用物理急停。
正常返回起点后，若试验开始时未使能，会恢复未使能状态。
恢复未使能状态后的最终检查中，若 J4 反馈超出起点 ±0.1°，程序按失败中止。

同步显示使用 [Isaac 真机姿态同步](isaacsim.md#只同步真机姿态)。
镜像只接收真机实际关节反馈，不发送真机命令。
与四轴控制面板同时使用时，按 [面板停止流程](isaacsim.md#停止与恢复) 结束控制。
