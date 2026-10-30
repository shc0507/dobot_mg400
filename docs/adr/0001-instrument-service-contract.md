[English](0001-instrument-service-contract.en.md)

# 0001：仪器服务契约 v1

## 状态

已接受：2026-10-30。

## 背景

机械臂、位移台、视觉和仪器服务必须共用发现、控制和故障接口。

## 决定

采用契约版本 1。路径相对于服务配置的基础路径。

| 方法和路径 | 请求或响应 |
| --- | --- |
| `GET api/describe` | `{service, version, contract: 1, demo, zones, lease_seconds, on_lease_expiry, commands: {name: {params: <JSON Schema>, any_client: bool}}}` |
| `GET api/state` | `{service, seq, ts_unix, demo, connected, busy, locked, fault, lease, last_command, ...}` |
| `POST api/command` | 请求体为 `{"action": "...", ...}`。返回 `202` 和 `{command_id, state}`。完成结果写入 `state.last_command`。 |
| `POST api/stop` | 请求体为 `{"wait_s": 0..15}`。返回 `{stop, state}`。`stop` 为 `confirmed`、`pending` 或 `unconfirmed`。任何客户端均可随时停止。重复请求安全。 |
| `POST api/heartbeat` | 获取或续期调用方的租约。其他客户端持有租约时，返回 `409`。 |
| `POST api/release` | 仅当调用方持有租约时释放它。 |
| `GET api/events?since=<seq>` | `{events: [{seq, ts_unix, level, message, fields}], next}`。同一批事件也写入服务日志。 |
| `GET api/ws` | 可选的状态推送，使用机械臂协议 1。 |

- `fault` 为 `null` 或 `{code, message}`。`last_command` 为 `null` 或 `{id, action, client, result, message, finished_at}`。结果为 `ok`、`failed`、`rejected` 或 `stopped`。
- 设备字段名包含单位：`pv_c`、`ramp_c_per_min`、`flow_pct`、`x_mm`、`r_deg`。
- POST 请求必须携带 `Content-Type: application/json` 和 `X-MG400-Client`。客户端 ID 包含 1 至 128 个可打印 ASCII 字符。
- 错误响应使用 `{"error": "..."}`：`400` 请求无效，`401` 令牌错误，`403` 访问规则拒绝，`409` 状态或租约冲突，`415` 非 JSON，`503` 设备连接丢失。
- Bearer 令牌至少包含 24 个可打印 ASCII 字符。无令牌时，仅接受回环地址的 `Host`，并拒绝转发头。保留 Host、Origin 和转发头检查。
- 同时只有 1 个控制客户端。默认租约为 3 s。读取和 `stop` 不要求租约。租约到期时停止运动。炉子遵循 [0005](0005-furnace-lifecycle.md)。在 `on_lease_expiry` 中声明行为。
- 命令结果未知时，设置 `locked` 和 `fault`。人员解除锁定前，拒绝 `stop` 以外的命令。不得自动重试运动。
- `--demo` 使用模拟设备。通过 `state.demo` 和 `describe.demo` 标明模式。客户端拒绝不同的 `contract` 版本。
- 保留机械臂现有协议。增加 `describe`、`stop`、`events` 和 `last_command`。

## 影响

客户端共用控制逻辑，服务保留各设备的安全行为。已确认停止与结果未知保持为不同状态。
