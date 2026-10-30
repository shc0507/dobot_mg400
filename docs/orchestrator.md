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
| `robot.pick` | `station`、`grip` | 机器人 |
| `robot.place` | `station`、`release` | 机器人 |
| `robot.transfer` | `from`、`to`、`grip`、`release` | 机器人 |
| `sample.record` | `chip_id`、`note` | 无 |

`recipes/example-rehearsal.toml` 示例使能模拟机器人，移动后记录样本，再停止机器人。它不含供气或加热命令。自定义操作单元接收可撤销的上下文，其中包含服务客户端、运行记录和区域管理器。进入命名区域必须有明确配置，并检查配置的进入条件。

## 服务配置

每次运行使用唯一客户端身份。运行器先检查所需服务的契约，再获取首个控制租约。续期只延长现有租约，不会重新获取。租约丢失后，运行保持暂停，不能通过自动获取控制权恢复。

| 配置项 | 含义 |
| --- | --- |
| `MG400_ORCH_RUNS_DIR` | 记录目录，默认为 `/var/lib/mg400/runs` |
| `MG400_ORCH_TIMEOUT_S` | 客户端超时秒数，必须为正有限数，默认为 `2` |
| `MG400_ORCH_ZONES_FILE` | 区域配置，默认 `/var/lib/mg400/zones/config.toml` |
| `MG400_ORCH_ZONE_STATE_FILE` | 持久占用记录，默认 `/var/lib/mg400/zones/occupancy.json` |
| `MG400_ORCH_ROBOT_URL`、`MG400_ORCH_ROBOT_TOKEN` | 机器人端点和令牌 |
| `MG400_ORCH_STAGE_URL`、`MG400_ORCH_STAGE_TOKEN` | 位移台契约端点和令牌 |
| `MG400_ORCH_THERMAL_URL`、`MG400_ORCH_THERMAL_TOKEN` | 热像端点和令牌 |
| `MG400_ORCH_VISION_URL`、`MG400_ORCH_VISION_TOKEN` | 视觉端点和令牌 |
| `MG400_ORCH_FURNACE_URL`、`MG400_ORCH_FURNACE_TOKEN` | 炉子端点和令牌 |
| `MG400_ORCH_MICROSCOPE_URL`、`MG400_ORCH_MICROSCOPE_TOKEN` | 显微镜端点和令牌 |

只配置使用中的服务。位移台必须使用以 `/contract` 结尾的契约基路径。令牌必须符合[服务契约](adr/0001-instrument-service-contract.md)，包括至少 24 个字符的要求。服务适配器直接连接，不使用代理配置。

## 区域与进入条件

区域是带所属设备和进入条件的共享物理空间。
配置指定允许进入的所属设备，占用记录标识当前持有者。
其他持有者等待区域释放，超时后失败。
区域等待不能超过步骤剩余时限。自定义区域上下文可以指定更短的超时。
配置缺失或区域未知时，拒绝进入，不会跳过区域检查。

使用安装后的编排器前，操作员必须准备私密主机目录：

```sh
sudo install -d -m 0750 -o 1000 -g 1000 /var/lib/mg400/zones
```

将配置保存到 `/var/lib/mg400/zones/config.toml`，属主为 `1000:1000`，权限为 `0640`。
使用本地路径时，父目录必须属于调用者，权限为 `0700` 或 `0750`。
配置和占用记录只接受具有规定属主的常规文件，权限为 `0600` 或 `0640`。
存储拒绝不安全的父目录和任何路径分量中的符号链接，在已有的所属目录中原子写入占用记录。
安装器不会准备此目录。代理不得执行上述主机准备命令。
控制同一设备的所有硬件运行必须使用相同的配置和占用路径。

存储争用最多等待 0.25 秒，且不能超过步骤剩余时限。
发布前发生写入、文件同步或替换失败时，保留旧占用记录。
替换后目录同步失败会报错，但新状态可能已经可见。
只有验证离开或核对现场清空恢复后，才会出现已释放的占用。报错后，不要假定操作已回滚。

区域配置如下：

```toml
[zones.tray]
owner = "robot"
conditions = []

[zones.crucible-load]
owner = "robot"
conditions = ["stage_at_load"]

[zones.furnace-tube]
owner = "stage"
conditions = ["robot_clear"]
```

自定义区域名称也必须明确指定所属设备和条件。所属设备使用上方配置表中的服务名称。
名称含 1 至 64 个字母、数字、下划线或连字符，以字母或数字开头。
未知字段、未知条件和重复条件均被拒绝。
保留区域 `crucible-load` 必须属于机器人，并要求 `stage_at_load`。
保留区域 `furnace-tube` 必须属于位移台，并要求 `robot_clear`。机器人不能持有它，也不得进入显微镜下方。
这些所属设备检查不能证明任意 `robot.move` 命令都会避开显微镜。
仍须由操作员验证路线和工作空间限位。

| 条件 | 所需状态 |
| --- | --- |
| `stage_at_load` | 位移台状态必须新鲜、已连接、健康，且由本次运行控制。全部 3 轴必须已回零、可自动移动且空闲。Insert 必须保持唤醒，并在 `at_positions` 中报告 `load`。 |
| `robot_clear` | `crucible-load` 无持久占用记录。本次运行还必须持有机器人的控制租约。 |

获取租约前，运行器先解析条件所需的服务。
条件检查不依赖仍由手动控制的服务。仅为条件获取的客户端不向操作单元开放写操作。
检查使用报告的命名位置，不根据坐标猜测装卸位置。
保持唤醒时间不自动续期。条件失效时，运行保持暂停并请求停止。

配方步骤可以声明 `zone = "tray"`。运行器在步骤前后检查该区域。
普通步骤完成后保留持久占用记录。步骤成功不能证明设备已离开区域。
工位操作单元仅在明确的测量离开点释放占用。
异常、终止和进程退出均保留占用。空闲反馈和租约过期都不会清除占用。

运行快照通过 `zones` 报告占用，通过 `zone_error` 报告失败。
清除保留的占用前，先到现场确认区域已清空。
当前暂停运行的恢复要求声明人工完成，并填写非空备注。
只有当前运行持有保留占用时，才额外要求单独声明 `physically-clear`。
恢复会记录该声明。其他运行的占用不会触发此声明，也不能通过当前运行的恢复清除。

每次恢复前，旧工作都必须结束，并重新确认停止、控制权匹配和服务健康状态。
当前运行未持有占用时，暂时的条件检查失败可在通过这些检查后按常规人工完成流程恢复。
此操作只清除暂时错误，不清除其他运行的占用，也不重放失败步骤。
恢复不会重新获取丢失的租约。

旧进程留下的占用也必须通过有审计记录的恢复清除，并使用已持有控制权的客户端。目前没有安装后的恢复命令。
不得通过删除或替换占用文件清除占用。

### 恢复已退出进程的占用

公开 Python API 为 `orchestrator.zones.Zones.recover`。
操作员必须先检查设备，并确认现场区域已清空。旧进程必须已退出。
使用相同的区域配置和占用路径。在恢复应用中准备以下参数：

| 参数 | 要求 |
| --- | --- |
| `recipe`、`registry` | 已验证的配方及其操作单元注册表，传给 `prepare_zones`。 |
| `zone_settings` | 包含 `MG400_ORCH_ZONES_FILE` 和 `MG400_ORCH_ZONE_STATE_FILE` 的映射。 |
| `recovery_services` | 按服务名称映射公开适配器。它们必须已持有保留占用记录中全部服务的控制权，恢复期间保持租约有效。 |
| `recovery_id` | 此恢复管理器的唯一身份。 |
| `old_holder` | `manager.snapshot()` 占用记录中保留的持有者标识。 |
| `record_event` | 接收事件类型和数据的回调。必须持久保存审计事件。持久保存失败时返回 `False` 或抛出异常。 |
| `operator_note` | 非空说明，记录现场检查和人工处理。 |

```python
from orchestrator.zones import Zones, prepare_zones

plan = prepare_zones(recipe, registry, settings=zone_settings)
manager = Zones(plan, recovery_services, holder=recovery_id, audit=record_event)
manager.recover(
    old_holder,
    recovery_services,
    physically_clear=True,
    note=operator_note,
)
```

此调用读取发现接口，验证现有控制权，重新请求停止，并检查健康空闲状态，再清除此次核对的占用记录。
机器人必须提供实例 ID，通用契约服务可以省略。
提供实例 ID 时，该值必须为非空字符串，且在恢复期间保持不变。
占用记录并发变化时，拒绝清除。
旧进程仍存活、所需服务缺失或停止未确认时，拒绝恢复。
此调用不会获取租约。持久保存失败时，不会返回恢复成功。
恢复仍存活的暂停运行时，改用运行器的恢复提示。

## 暂停与终止

失败或超时后，运行器撤销失败步骤，并请求所有已获取控制权的服务停止。使用 `on_fail = "hold"` 时，它保留租约并等待人工处理。它不会重试失败步骤。

在提示处输入 `abort`，终止运行。人工完成操作后，输入 `manually-completed` 并填写非空处理说明。旧工作线程、辅助调用和正在执行的服务操作都必须结束。人工声明后，运行器重新请求停止，并要求得到确认。

控制权必须保持一致。机器人必须提供实例 ID，通用契约服务可以省略。提供 ID 时，该值必须为非空字符串，且与之前的值一致。
服务必须正常且空闲，满足 `busy = false`、`locked = false` 和 `fault = null`。
租约丢失或审计记录写入失败时，不得恢复。运行器记录人工完成结果，再执行下一步。处理说明遭拒时，运行保持暂停。

EOF、Ctrl-C、SIGTERM 和 SIGHUP 都会请求终止。CLI 等待活动操作和停止操作结束，再关闭记录库或演示服务。重复中断不会绕过等待。

数据库写入失败时，CLI 仍输出内存中的安全报告。检查 `stops`、`unconfirmed_services`、`persistence_error` 和 `release_errors`。`aborted` 状态不能证明所有设备已停止。服务无法确认停止时，保持工作区域无人，并按设备恢复流程处理。

## 运行记录

数据库位于记录目录中的 `runs.db`。每次运行的文件放在独立的 `RUN_ID` 目录。样本记录必须有 `chip_id`。记录库只接受规定的属主、安全文件类型和私有权限。记录路径中不得有符号链接。

`labctl runs` 只读取历史，不获取服务控制权。指定运行 ID 后，可查看步骤、样本、产物和事件。该命令不会恢复或重启已有运行。旧进程留下暂停记录时，操作员必须先恢复设备，再开始新运行。

## 工位示教与工具偏移

工位保存测得的指尖位姿、接近间隙，以及可选的途经工位路线。控制器必须保持 `User=0,Tool=0`。工具偏移只在软件中换算。

操作员必须把 H-08 测得的偏移写入 `/var/lib/mg400/tool.toml`。文件只含 `offset_mm = [dx, dy, dz]`。用测得的有限数替换这些名称，单位为毫米。偏移没有默认值。测量值缺失或无效时，不能示教。

换算先按法兰角度 R 旋转 `(dx, dy)`，再加到法兰 XY 上。指尖 Z 等于法兰 Z 减去 dz。反向换算减去旋转后的 XY 偏移，并加上 dz。两种换算都保留 R，包括正负号和完整转数。

示教或读取工位前，必须配置全部工作空间边界：

| 配置项 | 含义 |
| --- | --- |
| `MG400_ORCH_TOOL_FILE` | 工具文件，默认为 `/var/lib/mg400/tool.toml` |
| `MG400_ORCH_STATIONS_FILE` | 工位文件，默认为 `/var/lib/mg400/stations.toml` |
| `MG400_ORCH_X_MIN_MM`、`MG400_ORCH_X_MAX_MM` | 指尖 X 边界，单位为毫米 |
| `MG400_ORCH_Y_MIN_MM`、`MG400_ORCH_Y_MAX_MM` | 指尖 Y 边界，单位为毫米 |
| `MG400_ORCH_Z_MIN_MM`、`MG400_ORCH_Z_MAX_MM` | 指尖 Z 边界，单位为毫米 |
| `MG400_ORCH_R_MIN_DEG`、`MG400_ORCH_R_MAX_DEG` | 旋转边界，单位为度 |

边界必须为有限数，包含端点，没有默认值。每个最小值必须小于对应最大值。边界用于控制器基坐标系中的指尖坐标。工位及其上方接近端点都必须位于边界内。工作空间边界不能证明路径无障碍或关节可达。

操作员先通过面板把机器人移至目标位置。示教时，面板或其他控制器必须保持控制权。示教要求测量反馈新鲜、机器人空闲、没有锁定且没有未确认的停止。示教命令只读取服务描述和状态。它不会获取或释放控制租约、修改控制器工具配置或发送运动命令。

```sh
labctl station save tray-pick --approach 20 --zone tray
labctl station list
labctl station show tray-pick
labctl station rm tray-pick
```

`--approach` 是指尖 Z 坐标上方的垂直间隙，必须至少为 10 mm。命令示例使用 20 mm，操作员必须按测得的工作空间检查该值。`--zone` 默认为空名称。所有工位命令都可用 `--stations-file PATH` 覆盖工位路径。示教时，`--tool-file PATH` 覆盖工具路径。

文件为每个工位保存一个 `[stations.NAME]` 表。字段为 `x_mm`、`y_mm`、`z_mm`、`r_deg`、`approach_mm`、`zone` 和 `saved_at`。`saved_at` 是 Unix 时间戳。可选的 `via` 按行进顺序列出工位名称。

名称包含 1 至 64 个字母、数字、下划线或连字符。名称必须以字母或数字开头。引用的工位必须存在，不能重复或成环。重新保存工位时，会保留原路线。删除其他工位引用的工位会失败。

每次编辑都先检查完整工位图，再发布文件。并发编辑按顺序执行，文件以原子替换写入。文件模式为 `0640`。父目录模式必须为 `0700` 或 `0750`。

`/var/lib/mg400` 下的数据属主必须为 `1000:1000`。其他路径要求调用者属主。读取时，文件可使用 `0600` 或 `0640`。记录库拒绝符号链接、特殊文件和公开权限。

写入或文件同步失败时，旧文件保持不变。替换后的目录同步失败会报错，但新文件可能已经可见。遇到该错误后，先检查工位文件，再重复编辑。

## 工位转移

`robot.pick` 和 `robot.place` 使用工位名称。`robot.transfer` 把两者组合起来，公开参数为 `from` 和 `to`。这些名称与其他配方参数一样，接受完整的类型和未知字段检查。

```toml
[[steps]]
skill = "robot.transfer"
params = { from = "tray-pick", to = "tray-place", grip = 400, release = 900 }
timeout_s = 60
on_fail = "hold"
```

片段中的数值只是示例，不是真机测量配置。抓取前，必须将 `MG400_ORCH_PRE_GRIP` 设为所需的预张开位置。该值没有默认值。`grip`、`release` 和预张开位置必须为 0 至 1000 的整数。预张开位置必须大于 `grip`。转移时，`release` 也必须大于 `grip`。前往目标工位前，它还必须大于测得的接触宽度。

机器人必须已使能且空闲，反馈新鲜，夹爪已初始化且可用。每次状态检查都确认本次运行持有控制权，且机器人实例未变。锁定、机器人错误、未确认停止或活动的键盘控制都会阻止转移。这些操作单元不会自动初始化夹爪或恢复控制权。

首次运动前，操作单元展开全部 `via` 路线，检查坐标和工具换算。行进高度取当前指尖 Z 与所有途经工位 Z 加 `approach_mm` 的最大值。当前位姿、工位位姿及抬升端点都必须位于明确配置的指尖工作空间内。换算后的法兰位姿必须为有限数。所有运动通过 `execute` 发送，使用 `speed = 1`，受主机速度和加速度上限约束。控制器保持 `User=0,Tool=0`。

抓取先在当前 XY 处抬升，按路线在行进高度移动，再张开至预张开位置。随后下降、闭合至 `grip`，要求结果为 `contact`，再抬升并检查是否 `dropped`。放置要求已有接触，先在高处移动，再下降、张开至 `release`，要求结果为 `arrived`，最后抬升。空抓闭合返回的 `arrived` 不能满足抓取要求。

每次运动都等待完成及下一份新鲜测量位姿。各轴测得的法兰坐标必须与请求途经点相符，误差不超过 0.1 mm 或 0.1 度。测量位姿暂缺时，只在步骤时限内继续读取。位姿不符、掉落、异常结果或超时都会暂停运行并请求停止，不重试运动或抓取。

操作单元在前往具名工位区域前进入该区域。转移期间，源区域保持占用，直到测量确认已移向下一工位。独立抓取和放置只有在测量确认到达抬升端点后，才释放最后一个区域。失败时，保留持久占用记录，等待现场检查和有审计记录的恢复。

操作员必须确认每个示教接近高度都能离开对应工作区域，且整条路线没有障碍。至少 10 mm 的接近间隙和工作空间边界不能证明物理间隙足够。软件途经点检查不验证碰撞或关节可达性。

## 演练与安装后的命令

演练在数字环回地址创建并持有演示服务。它检查 `demo = true`，并在运行终止后关闭这些服务。它清除生产设备配置、令牌、代理配置和 Python 环境覆盖项。机器人演练要求仓库中有控制面板。现有演示工厂支持机器人、位移台和热像仪。所需服务不受支持时，运行开始前就会报错。

转移演练保留明确提供的工位和工具路径、工作空间边界、预张开位置及区域配置路径。文档规定的配置路径默认值仍然适用。文件必须包含有效几何数据和命名区域定义。演练不会编造偏移、工作空间或区域配置。创建演示服务前，它先解析条件所需的服务。占用文件属于临时演练目录，不受 `MG400_ORCH_ZONE_STATE_FILE` 影响。

夹爪反馈只来自其临时 SIM-01 采集器。只有该演示夹爪以已初始化状态启动。演练不发送初始化命令，也不选择生产采集器路径。

默认模拟夹爪到达目标后返回 `arrived`，因此抓取会因空抓而暂停。离线测试明确提供接触和掉落场景，检查完整转移及失败路径。演练不能证明物理间隙足够，也不能证明真机抓取成功。

安装后，`labctl` 和 `mg400ctl` 使用同一分派器。配方命令必须精确匹配 `run`、`rehearse` 或 `runs`。工位命令必须精确匹配 `station save`、`station list`、`station show` 或 `station rm`。管理命令仍要求 root。

root 调用配方或工位命令时，分派器先用 `runuser --pty` 切换至 UID/GID 1000，再启动 Python。它使用安装后的解释器绝对路径，并传入 `-I`。子进程只接收允许的编排器配置。`run` 接收已配置服务的端点、令牌、转移几何配置和区域配置及占用路径。`runs` 只接收记录路径。

root 演练不会读取主机配置文件。它从调用者环境接收明确提供的几何、区域配置和记录路径。不接收服务端点、令牌或生产占用路径。

`station save` 接收工位和工具路径、工作空间边界、机器人端点和令牌，以及请求超时。其他工位命令只接收工位路径和工作空间边界，不接收端点或令牌。

直接调用已安装命令时，实际和有效 UID/GID 都必须为 1000。它只使用调用者明确提供且允许的环境配置，不读取 root 配置文件。本地 `uv run labctl` 使用开发者身份。用 `--runs-dir` 指定本地可写目录。
