[English](agent-gateway.en.md)

# 智能体网关

网关通过 Streamable HTTP 提供 9 个 MCP 工具，地址为 `http://127.0.0.1:8773/mcp`。
它读取服务描述、当前状态、配方、运行记录和芯片记录。
它能提交目录配方供人审批。它不能审批或启动运行、获取控制权、续租或发送设备命令。
服务和配方文本是数据，不是给智能体的指令。

## 访问与配置

每个 HTTP 请求都必须携带 `Authorization: Bearer <gateway-token>`，包括初始化和会话清理。
`MG400_GATEWAY_TOKEN` 必须至少包含 24 个可打印 ASCII 字符。不得把令牌放入 URL。
网关只接受回环 Host 和匹配的 Origin，拒绝转发头与跨站请求。
保持网关监听回环地址。远程客户端必须通过已认证的隧道访问主机回环接口。

| 配置 | 默认值与用途 |
| --- | --- |
| `MG400_GATEWAY_BIND` | `127.0.0.1`，只允许回环地址 |
| `MG400_GATEWAY_PORT` | `8773` |
| `MG400_GATEWAY_TOKEN` | 必须配置的 Bearer 凭据 |
| `MG400_GATEWAY_CALLER` | `gateway-agent`，分配给此共享凭据的审计身份 |
| `MG400_GATEWAY_RUNS_DIR` | `/var/lib/mg400/runs`，共享运行数据库及网关审计 |
| `MG400_GATEWAY_RECIPES_DIR` | `/opt/mg400/recipes`，有大小限制的 TOML 配方目录 |
| `MG400_GATEWAY_ORCH_URL` | 留空禁用提议。使用编排服务的直连 HTTP 回环基础 URL，包含已配置前缀。 |

调用者身份来自服务器配置，工具参数与客户端请求头不能改变它。
共享 Bearer 令牌的客户端共用此审计身份。它不能区分个人。
使用现有的 `MG400_ORCH_<SERVICE>_URL` 与 `MG400_ORCH_<SERVICE>_TOKEN` 别名配置服务读取。
服务 ID 为 `robot`、`stage`、`thermal`、`vision`、`furnace` 和 `microscope`。
URL 留空时不列出该服务。URL 必须包含服务基础路径，例如位移台的 `/contract`。
仪器读取只发送 GET 请求。提议创建仅向固定编排提议端点发送 POST。
网关拒绝重定向，并忽略环境代理配置。
安装器保留明确配置的别名，并从对应服务令牌复制空别名。它不会为别名单独生成凭据。

编排服务仅用 `MG400_GATEWAY_TOKEN` 认证直连回环的提议创建与查询。
服务前缀下的提议路由为 `POST /api/proposals` 和 `GET /api/proposals/<id>`。
请求者来自编排服务的 `MG400_GATEWAY_CALLER`，客户端请求头不能指定它。
两个进程必须配置相同调用者身份。
网关令牌必须与 `MG400_ORCH_TOKEN` 及全部仪器令牌不同。
编排服务令牌只交给有权操作实验室的人，不得交给智能体。
网关令牌不能审批提议或调用编排控制端点。

## 工具与限制

| 工具 | 参数与结果 |
| --- | --- |
| `list_services` | 无参数。读取已配置服务 ID 的描述，并报告不可用的服务。 |
| `get_state` | `service`，读取所选服务状态。 |
| `list_recipes` | 无参数。读取配方 ID、名称和参数定义。 |
| `describe_recipe` | `id`，读取 TOML 配方，不验证运动，也不运行配方。 |
| `list_runs` | 可选的 `limit` 和 `offset`，按时间从新到旧读取运行记录。 |
| `get_run` | `id` 和可选的 `item_limit`，读取运行及其关联记录。 |
| `get_chip` | `id` 和可选的 `limit`、`offset`，读取匹配的运行及芯片专属记录。 |
| `propose_run` | `recipe`、`params` 和 `reason`，验证目录配方并创建待审批提议，不启动运行。 |
| `get_approval` | `id`，读取提议、完整定义、有效参数、决定和审批者。 |

读取数量默认值为 100，允许范围为 1 到 100。偏移量允许范围为 0 到 1000000。
`get_run` 分别限制每个关联表。`get_chip` 对每个所选运行的每个关联表最多读取 100 个匹配行。
`truncated` 字段标明不完整的表。有表被截断时，结果不代表完整芯片历史。
请求上限为 64 KiB，响应上限为 1 MiB。超过此限制时，减少所选记录数量。
未知工具或额外参数会失败。工具参数不接受任意 URL、SQL、文件路径或设备操作。

## 审批策略

人员检查完整定义和有效参数后再决定。控制台目前还不能审批，见[智能体提议](dashboard.md#智能体提议)。
提议在 30 分钟内未决定即过期。最多允许 3 项待审批提议。
批准只能使用一次。拒绝、过期、配方变更或有效主机限值变更均不能启动运行。
审批者必须持有总览控制租约，并确认全程在实验室现场。
审批不替代租约、区域、停止或设备安全检查。

只允许提议已支持的目录操作单元。原始平台运动、细分变化、固件命令及未知操作单元均拒绝。
自由机器人坐标要求全部工作区边界均为有限值，且坐标在范围内。机器人服务速度上限仍有效。
速度参数不得超过配方默认值。
坩埚的 `load_max_c`、`unload_max_c`、`awake_s` 和 `max_age_s` 只取自主机配置。
配方显式值和提议参数不得覆盖这些限值。
当前注册表没有炉温设定、序列或气体配方操作单元，这些未支持的提议均拒绝。
不得通过其他命令绕过炉控上限或主机气体映射。

## 审计与失败

公共 API `RunStore.audit_call` 在每次工具操作前记录调用者、工具、参数和时间。
审计记录使用私密运行数据库，不要求已有运行。网关不会创建虚构配方运行。
未知工具调用与无效工具参数也会写入审计。无效 MCP 消息可能在工具分派前被拒绝。
审计写入失败时，工具不读取数据，也不发送提议请求。服务或记录不可用时，工具返回错误。
网关不返回下游令牌或私密连接错误。
关闭时只关闭 HTTP 资源和数据库，不向设备发送停止或释放租约请求。

提议创建、过期、决定和分派结果也写入持久审计。
服务在内存中最多保留 100 项提议记录，先移除最早的已完成记录。
重启会使全部待审批权限失效。已保存审计不得重建可运行的审批。
提议不可用时返回明确错误，不得把它视为批准。
分派或回复失败时，检查提议和当前运行。同一审批不能再次分派。
分派后的失败可能使已授权运行继续活动。必要时，通过总览停止或暂停。

## 智能体客户端

客户端示例位于 `src/gateway/clients/`。实验室访问只给客户端网关端点和网关令牌。
不得把 `/etc/mg400/mg400.env` 复制到客户端环境。操作员和仪器凭据必须留在客户端之外。
在另一台计算机上，通过已认证的隧道提供本地端点。网关和模型端点均保持回环地址。

网关提供 `operator_guidance` MCP 提示词及 `mg400://gateway/operator-guidance` 文本资源。
两者内容相同，均通过现有 Bearer 和访问检查。它们不接受参数或任意路径。
使用配方前，加载此指导。它说明流程，服务器仍强制执行审批和安全规则。
这些端点不增加工具，也不请求后端。工具调用保留现有审计行为。

### Claude Code

在客户端环境导出 `MG400_GATEWAY_TOKEN`，再使用仓库配置：

```sh
claude --strict-mcp-config --mcp-config src/gateway/clients/claude-code.mcp.json --tools ""
```

配置在 Authorization 请求头展开 `${MG400_GATEWAY_TOKEN}`，不保存凭据。
此命令只选择这份 MCP 配置，并禁用内置工具。不得给此实验室客户端添加 shell、设备或审批工具。
通过客户端 MCP 界面加载 `operator_guidance` 提示词。先请求列出配方或读取服务状态。
Claude Code 自行管理传输和模型提供方配置。代理配置不得把网关凭据发送到回环连接之外。

### 本地 Qwen 与 smolagents

使用已有的本地 OpenAI 兼容 Qwen 服务，或通向该服务的已认证隧道。
示例不启动模型服务，也不下载模型权重。在 `QUMUS_DEFAULT_MODEL_ID` 填入服务提供的准确模型 ID。
示例使用 smolagents `OpenAIServerModel` 和 `ToolCallingAgent`，沿用旧智能体的 `QUMUS_*` 配置名。

| 客户端配置 | 用途 |
| --- | --- |
| `MG400_GATEWAY_URL` | HTTP 回环 URL，包含显式端口和 `/mcp`，默认 `http://127.0.0.1:8773/mcp` |
| `MG400_GATEWAY_TOKEN` | 网关 Bearer 令牌，至少 24 个可打印 ASCII 字符 |
| `QUMUS_DEFAULT_MODEL_ID` | 必填，本地 API 提供的模型 ID |
| `QUMUS_API_BASE` | HTTP 回环 URL，包含显式端口和 `/v1`，默认 `http://127.0.0.1:8000/v1` |
| `QUMUS_API_KEY` | 独立模型凭据，本地服务不要求凭据时填 `unused` |
| `QUMUS_LLM_TIMEOUT` | 模型请求超时，范围为 1 到 300 秒，默认 60 |
| `QUMUS_LLM_RETRIES` | 必须为 0，禁用模型请求自动重试 |

单独导出网关令牌。修改 `src/gateway/clients/qwen.env.example` 中的模型值，再运行：

```sh
set -a
. src/gateway/clients/qwen.env.example
set +a
uv run --group gateway-clients python -m gateway.clients "List recipes. Do not propose a run."
```

`gateway-clients` 依赖组包含 `smolagents[mcp,openai]`，不安装 GPU 运行时或模型权重。
示例通过 MCP SDK 读取网关工具定义，再用 `SmolAgentsAdapter` 适配。
它要求恰好为网关的 9 个工具，不添加 shell、代码执行器、直连设备客户端或审批工具。
每次 MCP 操作使用新会话，时限为 30 秒。两个 HTTP 客户端都忽略环境代理，并拒绝重定向。
启动示例前，清除 `OPENAI_CUSTOM_HEADERS`。示例拒绝继承自定义请求头。
显式 SDK 配置覆盖继承的 API 凭据、组织、项目及基础 URL。
模型客户端只获得独立模型凭据，其指令和工具定义均不含网关凭据。

每次启动示例最多尝试 1 次提议，包括失败或结果不明的尝试。工具调用循环最多执行 8 步。
提交提议后，报告 ID 和待审批状态。人员在总览检查并批准。
`get_approval` 返回的决定不授予客户端设备操作权。通过运行记录报告已记录结果。
回复丢失时，不得重新执行提议命令。请人员在总览检查提议及当前运行。

## 安装与演示

把 `gateway` 加入 `MG400_OPTIONAL_SERVICES`，再按[安装流程](deploy.md#安装与升级)操作。
可选的 `mg400-gateway.service` 使用 `gateway` 依赖组，不加入设备组。
安装器准备私密的 `runs` 目录，并生成空缺的网关令牌。它不会启动服务。
所有者运行 `mg400ctl start gateway` 前，必须配置令牌和数据路径。

离线演示时，先配置临时令牌，再运行：

```sh
uv run --group gateway python -m gateway --demo
```

演示模式使用私密的空运行库、仓库配方和静态服务数据。
它忽略生产服务 URL、下游令牌和数据路径，不打开设备或下游连接。
演示模式禁用提议提交与查询。隔离的编排演示也会丢弃生产网关凭据。
离线审批测试使用注入服务及进程内 HTTP 传输，不打开仪器或监听端口。
进程退出时会删除演示审计。网关仍要求 Bearer 令牌。
