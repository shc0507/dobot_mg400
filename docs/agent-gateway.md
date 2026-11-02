[English](agent-gateway.en.md)

# 智能体只读网关

网关通过 Streamable HTTP 提供 7 个 MCP 工具，地址为 `http://127.0.0.1:8773/mcp`。
它读取服务描述、当前状态、配方、运行记录和芯片记录。
它不能提议或启动运行、获取控制权、续租、停止设备或发送命令。
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

调用者身份来自服务器配置，工具参数与客户端请求头不能改变它。
共享 Bearer 令牌的客户端共用此审计身份。它不能区分个人。
使用现有的 `MG400_ORCH_<SERVICE>_URL` 与 `MG400_ORCH_<SERVICE>_TOKEN` 别名配置服务读取。
服务 ID 为 `robot`、`stage`、`thermal`、`vision`、`furnace` 和 `microscope`。
URL 留空时不列出该服务。URL 必须包含服务基础路径，例如位移台的 `/contract`。
网关只发送 GET 请求，拒绝重定向，并忽略环境代理配置。
安装器保留明确配置的别名，并从对应服务令牌复制空别名。它不会为别名单独生成凭据。

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

读取数量默认值为 100，允许范围为 1 到 100。偏移量允许范围为 0 到 1000000。
`get_run` 分别限制每个关联表。`get_chip` 对每个所选运行的每个关联表最多读取 100 个匹配行。
`truncated` 字段标明不完整的表。有表被截断时，结果不代表完整芯片历史。
请求上限为 64 KiB，响应上限为 1 MiB。超过此限制时，减少所选记录数量。
未知工具或额外参数会失败。工具参数不接受任意 URL、SQL、文件路径或设备操作。

## 审计与失败

公共 API `RunStore.audit_call` 在每次工具读取前记录调用者、工具、参数和时间。
审计记录使用私密运行数据库，不要求已有运行。网关不会创建虚构配方运行。
未知工具调用与无效工具参数也会写入审计。无效 MCP 消息可能在工具分派前被拒绝。
审计写入失败时，工具不读取数据。服务或记录不可用时，工具返回错误。
网关不返回下游令牌或私密连接错误。
关闭时只关闭 HTTP 资源和数据库，不向设备发送停止或释放租约请求。

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
进程退出时会删除演示审计。网关仍要求 Bearer 令牌。
