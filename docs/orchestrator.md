[English](orchestrator.en.md)

# 配方编排器

编排器跨仪器服务执行配方。配方是带版本的 TOML 文件，包含参数和有序操作步骤。操作单元是注册表中的具名操作。每次运行记录步骤、芯片身份、样本、产物和安全结果。

## 命令

在仓库内使用项目环境：

```sh
uv run labctl rehearse recipes/example-rehearsal.toml --runs-dir ./rehearsal-runs
uv run labctl runs --runs-dir ./rehearsal-runs
uv run labctl runs RUN_ID --runs-dir ./rehearsal-runs
```

`labctl run RECIPE` 使用配置的仪器服务。真机运行前，必须完成操作员检查。操作员必须守在急停旁。代理只能使用演练。

`--runs-dir PATH` 指定记录目录。`--param NAME=JSON` 覆盖配方声明的参数。字符串值必须使用 JSON 字符串引号：

```sh
uv run labctl rehearse recipes/example-rehearsal.toml --runs-dir ./rehearsal-runs --param 'chip_id="demo-chip-2"' --time-scale 0.1
```

`--time-scale` 必须为正数，只用于演练。默认值为 `0.1`。它缩短模拟等待，步骤超时仍按实际时间计算。

## 配方

CLI 先校验完整配方，再创建记录、启动演示服务或获取控制租约。未知操作单元、缺少参数、类型错误、超时无效或未知字段都会拒绝。

```toml
name = "Record a sample"
version = 1

[params.chip_id]
type = "string"
default = "demo-chip"

[[steps]]
skill = "sample.record"
params = { chip_id = "${chip_id}", note = "Example record" }
timeout_s = 1
on_fail = "hold"
```

参数类型为 `string`、`integer`、`number` 或 `boolean`。没有默认值的参数必须提供覆盖值。`${chip_id}` 替换完整参数值，并保留类型。每步必须包含 `skill`、`params` 和正有限数 `timeout_s`。`on_fail` 默认为 `hold`，也接受 `abort`。

| 操作单元 | 参数 | 服务 |
| --- | --- | --- |
| `wait` | `seconds` | 无 |
| `fail` | `message` | 无 |
| `robot.enable` | 无 | 机器人 |
| `robot.move` | `x_mm`、`y_mm`、`z_mm`、`r_deg` | 机器人 |
| `robot.stop` | 无 | 机器人 |
| `sample.record` | `chip_id`、`note` | 无 |

`recipes/example-rehearsal.toml` 示例使能模拟机器人，移动后记录样本，再停止机器人。它不含供气或加热命令。自定义操作单元接收可撤销的上下文，其中包含服务客户端、运行记录和区域管理器。ORC-04 完成前，区域管理器不检查，也不提供跨设备互锁。进入区域时，不校验条件，也不排除其他服务。

## 服务配置

每次运行使用唯一客户端身份。运行器先检查所需服务的契约，再获取首个控制租约。续期只延长现有租约，不会重新获取。租约丢失后，运行保持暂停，不能通过自动获取控制权恢复。

| 配置项 | 含义 |
| --- | --- |
| `MG400_ORCH_RUNS_DIR` | 记录目录，默认为 `/var/lib/mg400/runs` |
| `MG400_ORCH_TIMEOUT_S` | 客户端超时秒数，必须为正有限数，默认为 `2` |
| `MG400_ORCH_ROBOT_URL`、`MG400_ORCH_ROBOT_TOKEN` | 机器人端点和令牌 |
| `MG400_ORCH_STAGE_URL`、`MG400_ORCH_STAGE_TOKEN` | 位移台契约端点和令牌 |
| `MG400_ORCH_THERMAL_URL`、`MG400_ORCH_THERMAL_TOKEN` | 热像端点和令牌 |
| `MG400_ORCH_VISION_URL`、`MG400_ORCH_VISION_TOKEN` | 视觉端点和令牌 |
| `MG400_ORCH_FURNACE_URL`、`MG400_ORCH_FURNACE_TOKEN` | 炉子端点和令牌 |
| `MG400_ORCH_MICROSCOPE_URL`、`MG400_ORCH_MICROSCOPE_TOKEN` | 显微镜端点和令牌 |

只配置使用中的服务。位移台必须使用以 `/contract` 结尾的契约基路径。令牌必须符合[服务契约](adr/0001-instrument-service-contract.md)，包括至少 24 个字符的要求。服务适配器直接连接，不使用代理配置。

## 暂停与终止

失败或超时后，运行器撤销失败步骤，并请求所有已获取控制权的服务停止。使用 `on_fail = "hold"` 时，它保留租约并等待人工处理。它不会重试失败步骤。

在提示处输入 `abort`，终止运行。人工完成操作后，输入 `manually-completed` 并填写非空处理说明。旧工作线程、辅助调用和正在执行的服务操作都必须结束。人工声明后，运行器重新请求停止，并要求得到确认。

控制权必须保持一致。服务提供实例 ID 时，该 ID 也必须保持一致。服务必须正常且空闲，满足 `busy = false`、`locked = false` 和 `fault = null`。租约丢失或审计记录写入失败时，不得恢复。运行器记录人工完成结果，再执行下一步。处理说明遭拒时，运行保持暂停。

EOF、Ctrl-C、SIGTERM 和 SIGHUP 都会请求终止。CLI 等待活动操作和停止操作结束，再关闭记录库或演示服务。重复中断不会绕过等待。

数据库写入失败时，CLI 仍输出内存中的安全报告。检查 `stops`、`unconfirmed_services`、`persistence_error` 和 `release_errors`。`aborted` 状态不能证明所有设备已停止。服务无法确认停止时，保持工作区域无人，并按设备恢复流程处理。

## 运行记录

数据库位于记录目录中的 `runs.db`。每次运行的文件放在独立的 `RUN_ID` 目录。样本记录必须有 `chip_id`。记录库只接受规定的属主、安全文件类型和私有权限。记录路径中不得有符号链接。

`labctl runs` 只读取历史，不获取服务控制权。指定运行 ID 后，可查看步骤、样本、产物和事件。该命令不会恢复或重启已有运行。旧进程留下暂停记录时，操作员必须先恢复设备，再开始新运行。

## 演练与安装后的命令

演练在数字环回地址创建并持有演示服务。它检查 `demo = true`，并在运行终止后关闭这些服务。它清除生产设备配置、令牌、代理配置和 Python 环境覆盖项。机器人演练要求仓库中有控制面板。现有演示工厂支持机器人、位移台和热像仪。所需服务不受支持时，运行开始前就会报错。

安装后，`labctl` 和 `mg400ctl` 使用同一分派器。配方命令必须精确匹配 `run`、`rehearse` 或 `runs`。管理命令仍要求 root。

root 调用配方命令时，分派器先用 `runuser --pty` 切换至 UID/GID 1000，再启动 Python。它使用安装后的解释器绝对路径，并传入 `-I`。子进程只接收允许的编排器配置。只有 `run` 接收令牌和端点。`runs` 只接收记录路径。root 演练不会读取主机配置文件。

直接调用已安装命令时，实际和有效 UID/GID 都必须为 1000。它只使用调用者明确提供且允许的环境配置，不读取 root 配置文件。本地 `uv run labctl` 使用开发者身份。用 `--runs-dir` 指定本地可写目录。
