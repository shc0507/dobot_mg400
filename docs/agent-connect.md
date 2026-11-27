# 智能体连接

智能体连接发布设备缓存数据，并在明确授予操作权后提供目录命令。控制接口使用版本 6、阶段 8。8082 正式 Console 启用真机命令，包括受支持的加热炉命令。只读数据流、普通命令和紧急 Stop 使用独立路径。

接入其他智能体时，使用[外部智能体接入文档](agent-connect/README.md)。由人使用[根 README](../README.md#连接外部智能体) 的提示词启动适配。先读项目知识，再读通信契约。修改并检查外部连接器后才运行。运行时发现只检查兼容性并提供当前值，不触发自主代码修改。本页说明现有门户和 Console 的操作。

## 本地连接页

在 LarePass 中启用 VPN。在仓库根目录运行：

```powershell
uv run python scripts/agent_portal.py
```

命令在 `http://127.0.0.1:8792/` 打开 Agent Portal。Live equipment 来源连接 `http://100.64.0.1:8082`。如果 VPN 或数据源不可用，页面显示错误。页面不会换成模拟数据。

Agent 页接受问题和指令。打开 Model APIs 后，按以下步骤配置：

1. 在 API route 中选择 Direct provider 或 Token Station。
2. 输入配置名称、模型 ID 和 API 密钥。
3. 使用 Direct provider 时，选择提供方。
4. Base URL 留空时使用默认地址，也可填入自定义地址。
5. 选择 Save profile。

配置将 `route` 保存为 `direct` 或 `token_station`。没有 `route` 的配置使用 `direct`。Direct provider 使用所选提供方的原生 API：

| 提供方 | API 与输出 |
| --- | --- |
| OpenAI | Responses，使用结构化输出。 |
| Anthropic | Messages，使用结构化输出。 |
| Gemini | GenerateContent，使用 JSON schema。 |

Token Station 隐藏提供方选项，使用 Chat Completions 协议。保留模型 ID 的提供方前缀，例如 `openai/gpt-6.1-sol`。默认基础地址为 `https://physics.bytefuture.ai/v1`。默认请求地址为 `https://physics.bytefuture.ai/v1/chat/completions`。请求使用 `POST`、`Authorization: Bearer <api_key>`、`model`、`messages` 和 `stream: true`。

使用 Token Station 时，智能体收集完整的服务器发送事件（SSE）响应。核验决策 JSON 后，才提交命令。部分输出不会触发命令。若数据流截断或报告错误，智能体不提交命令。

选择 Activate，启用已保存的配置。保存配置不会自动启用。Deactivate 停用模型并保留配置。Delete 删除配置。删除当前配置后，没有启用的模型。真机和模拟来源共用 1 个当前配置。

Windows DPAPI 为当前 Windows 用户加密全部保存内容。加密内容包括 API 密钥和配置资料。默认文件为 `%LOCALAPPDATA%/AI Chemistry Console/agent-portal/model-profiles.json`，位于 Git 仓库外。重启 Portal 后，恢复已保存的配置及当前选择。如果 Windows 凭据保护不可用，不能保存配置，也不使用明文存储。默认配置模式不自动读取 `OPENAI_API_KEY`。

读写配置时，Portal 使用有等待时限的进程锁。每次读取或修改前，重新加载最新文件。新的模型请求使用当前配置。正在运行的请求继续使用原配置。若读取失败，清除缓存中的配置凭据。

启动器将真机配对保存到 `%LOCALAPPDATA%/AI Chemistry Console/agent-portal/pairing.json`，使用 Windows DPAPI 加密。不保存会话令牌或操作权。若智能体原先已连接，启动时最多尝试 5 次观察者连接，每次间隔 2 秒。离线重连失败后，开启 LarePass，再选择 **Connect**。选择 **Disconnect** 可取消恢复，并在重启后保持断开。仅 1 个 Portal 进程可持有保存的配对。重连后须重新授予操作权，才能执行命令。

真机历史记录加密保存在 `%LOCALAPPDATA%/AI Chemistry Console/agent-portal/history.sqlite3`。保留 60 条消息、120 条活动、100 条用量记录和 100 条请求引用。模拟历史仍保存在内存中。恢复的历史只提供过去的上下文，不代表当前读数或待执行指令。**Check previous command results** 读取原请求 ID 的结果，不重发命令、不恢复任务，也不申请操作权。

修改配置或重启 Portal 不会自动探测 API，也不会发送设备命令。提交请求后，指令和近期对话发送到当前 API 地址。请求还包含已审阅的项目知识、已核验的 Console 状态和当前命令目录。不发送摄像头视频或 Console 凭据。

对于少量明确的位移台或机器人请求，模型只接收该设备的状态和动作。操作规则、单位、选项、样品及运行状态仍完整提供。含糊请求和比较请求保留完整上下文。返回的动作仍用原始完整目录核验。

**API usage** 显示近期每次请求的提供方、模型、路由、输入/输出/总 token 数、耗时和结果。仅当提供方明确返回美元费用时，才显示费用。缺少用量或费用时，保持未知。**Cancel agent task** 中断本地 API 连接，禁止迟到的响应触发命令。但不能保证提供方停止计算或计费。

运行时，本地智能体加载已审阅的项目知识。使用状态前，先检查当前只读契约。对受支持的动作，核对已审阅的参数结构、修订版本、默认值和设备。若不匹配，必须由人指示编码智能体更新代码，普通命令才能恢复。本地智能体不会自主修改代码。

调用模型前，Portal 保存 `choices.stage_context`。它将保存的位移台参考与模型状态快照中的 `devices.stage.values.stage_reference` 比较。对位移台运动和传送配置操作，Portal 将保存的 `preconditions` 原样放入命令封装。模型不提供参考或配置绑定值。

通过 `POST /commands` 提交这些动作前，Portal 读取新选项并比较原值。如果上下文缺失、不可用、格式错误或发生变化，Portal 拒绝这些动作，不发送命令。Portal 不会用新值替换原值。执行前，服务器再次检查这些值。

此绑定只覆盖位移台校准参考和当前传送配置。草稿内容和动作参数指定的其他配置不在此绑定范围内。它不要求传感器读数保持不变，也不增加设备进程标识检查。位移台校准、故障清除、其他设备动作和 Stop 保留原有路径。

连接并明确授予操作权后，清晰指令立即执行。缺少目标或单位时，智能体会提问。每条指令可执行 1 个目录动作，包括已有的组合动作。位移台坐标使用圈数。机器人 X/Y/Z 使用毫米，R 使用度。智能体显示 Console 返回的结果。结果未知时，保留请求 ID，不自动重发运动命令。**Cancel agent task** 取消待处理的解释，并请求取消自己的命令。Portal 各页签均保留 Stop 控件。

实时活动列表每 1 秒更新。它显示状态读取、指令解释、参数核验、命令提交和 Console 结果。每条已提交命令均显示完整 JSON 请求正文和请求 ID。指令解释提供简短的动作说明，不展示模型的私有推理。真机活动使用加密的本地历史记录。模拟活动保存在内存中，与真机活动分开。

Digital Twin 页显示实时 Console 状态。Manual testing 页包含命令表单、目标预览和可选模拟。切换页签不会断开智能体。只有明确提交的指令或手动命令才会请求设备操作。打开页面、设置模型和授予控制权均不会启动设备。

本地服务器把 Olares 只读 WebSocket 转发为服务器发送事件（SSE）。它订阅 `devices`、`samples` 和 `operations`。Updates per second 可选 1、2 或 4 Hz，默认 2 Hz。读取区的 Disconnect 只断开数据连接。

Digital Twin 显示从 Console 数据流收到的每台设备。它显示位移台位置图、机器人坐标及加热炉温度和气体读数。它还显示热像仪读数、显微镜状态和视觉结果。缺失读数仍为未知。设备详情和 Full state JSON 保留收到的全部字段。设备卡片下方保留 Active samples、Operations 和最近 100 条事件。

位移台位置图使用每个数据帧中的校准下限和上限，单位为转数。每次收到新数据帧，页面更新显示的限位。未设置的原生限位显示为不可用。Portal 不会用协议的数值范围替代它们。若需重新校准，使用[位移台面板](stage-panel.md)中的现有控件。

浏览器核对不可变数据约定及其摘要后才解码。定义不受支持或摘要不匹配时，停止解码。原始 JSON 仍可查看。

在 Manual testing 中，Send command 按设备分组显示动作。表单显示参数、受支持的目标预览和待发送 JSON。目标预览不会移动设备。只有 Send command 提交手动请求。可视化页面使用现有机器接口，不向数据流添加摄像头视频。

使用 `--no-browser` 可不打开标签页。按 Ctrl+C 停止服务器。下列进程只监听回环地址：

| 进程 | 端口 | 用途 |
| --- | --- | --- |
| Portal | 8792 | 显示实时数据并转发明确提交的命令。 |
| 模拟 Console | 8790、8793 | 提供模拟 HTTP 和 WebSocket 接口。 |
| 独立模拟器与 Stop 网关 | 8794 | 模拟 Console 停止后，仍提供模拟位移台 Stop 和状态。 |

启动前关闭其他 Console 预览，两者都使用端口 8790。外观按钮选择深色或浅色模式，并在本地保存选择。

## 智能体连接与控制

操作员归属只限于 8082 测试 Console。它不会锁定 8080 或其他设备直连接口。Mark 协调测试，确保智能体控制期间无人通过这些接口操作设备。原有设备租约继续生效。

公共只读数据流不要求邀请或操作员授权。Console 管理信任实验室网络内的访问。不要求所有者登录、密码、所有者代码或 SSH 隧道。人工控制继续使用设备租约。智能体持有操作权时，先选择 Take control，再发送普通人工命令。

### Console 策略

通过 LarePass 打开 `http://100.64.0.1:8082/lab/#agent-connect`。控制约定位于 `/lab/api/agent/control/contract`。它声明 `pairing: {approval: "invitation", operator_grant: "explicit"}`。状态报告 `management_available`。

默认打开 **Connections** 子页，用于邀请、智能体记录和操作权管理。**Testing** 子页包含模拟控件及数据与服务详情。Device Stop 位于两个子页外，始终可用。切换子页不会启动设备。

库构造默认使用 `allow_physical=False`。正式注册通过 `allow_physical=True` 明确启用真机。邀请、授权、命令参数和页面控件都不能改变服务器的这个选择。

| 来源 | 模式与命令策略 |
| --- | --- |
| 8082 正式 Console | `equipment_mode: "physical"`、`commands_enabled: true`、`physical_commands_enabled: true`、`furnace_control_enabled: true`、`stop_enabled: true`。 |
| 本地模拟 | `equipment_mode: "simulated"`、`commands_enabled: true`、`physical_commands_enabled: false`、`furnace_control_enabled: false`、`stop_enabled: true`。 |
| 未启用真机或模拟的库 | `equipment_mode: "disabled"`、`commands_enabled: false`、`physical_commands_enabled: false`、`furnace_control_enabled: false`、`stop_enabled: false`。 |

选择动作前，读取命令目录。受支持的动作调用已审查的原生处理器，涵盖位移台、机器人、显微镜、热像仪、视觉和加热炉。服务不支持的能力显示不可用及其原因。接口不提供任意设备命令、URL、配方或组合动作转发。

命令保留原生限制和设备租约。这些限制包括机器人速度与报警检查、已配置的位移台限位、显微镜焦点与转盘避让限制，以及加热炉温度和升温速率上限。

### 从 PC 连接并发送命令

在 Portal 中使用 Agent connection and control。控制连接和只读数据流向同一 8082 Console 发送独立请求。

1. 在 Console 中选择 Create invitation。单次邀请在 300 秒后过期。选择 Copy。
2. 在 Portal 中输入 Agent name 和邀请。选择 Use invitation。
3. 选择 Connect。智能体以观察者身份连接。
4. 释放现有设备控制。等待运行、任务和 Sample Transfer 结束。
5. 在 Console 中为已连接智能体选择 Grant control。
6. 在 Portal 中选择 Action。填写参数。选择 1 次 Send command。

邀请包含 8 个易读字符，显示为 `XXXX-XXXX`。HTTP Console 页面支持 Copy。有效邀请立即授予连接权限，不要求另行批准。

同一 Console 同时只允许 1 名操作员。Grant control 不获取设备租约，也不发送命令。用户选择首条真机命令及其参数。选择动作、连接、重连和操作员授权都不会自动提交命令。

Commands 区标明 Physical commands 或 Simulated commands。选择器只列出可用动作。展开不可用动作可查看原因。

表单显示单位、限制和已存选项。加热炉参数初始为空。按约定填写温度 °C、升温速率 °C/min 和气体流量百分比。表单按约定检查序列 JSON。Advanced JSON and parameter schema 显示完整约定。可选的 Preconditions (JSON) 初始为 `{}`。

Move one stage axis 使用编码器绝对位置，单位为转数，原点是所选轴的零位。Portal 显示该轴当前位置和校准后的最小、最大位置。校准数据缺失或过期时，Send command 不可用。校准数据刷新不会替换已输入的目标。

`/commands/choices` 响应中的 `constraints.stage` 来自现有设备缓存。这些运行时限位不改变命令目录约定或摘要。运动前，服务器重新检查所选轴限位、故障、租约和 Stop 版本。X/Y 移动不依赖 Insert 的位置。这适用于单轴移动、组合坐标、微动、步进和已存位置。组合 X/Y 移动先执行 Bottom，再执行 Top，Insert 不变。命名位置按 Bottom、Top、Insert 顺序移动，不额外执行抽出动作。每次只移动 1 个轴。

`choices.stage_context` 包含 `version: 1`、`available`、`reason` 和 `preconditions`。可用时，`preconditions` 包含 `stage_reference`、`profile_id` 和 `profile_digest`。该上下文与返回选项使用同一份位移台缓存和当前配置快照。不可用时，`preconditions` 为 `null`，`reason` 说明原因。此上下文不改变命令目录摘要。

状态字段 `devices.stage.values.stage_reference` 与该帧使用同一份原生缓存快照。不可用时，该字段为 `null`。为受保护的位移台动作决策前，Portal 将它与选项中的参考比较。

`stage_reference` 必须包含 `instance`、`reference_revision`、`reference_source`、`stop_epoch` 和 `limits_digest`。设备重连保留服务进程的 `instance`，服务重启则更改它。服务重启后，`reference_revision` 可能重复。旧版真机上下文将缺失的身份或修订值保留为 `null`，但只有修订而没有身份时，上下文无效。缺少身份时，进程连续性未经验证。本地模型拒绝省略 `instance` 字段的上下文。

只读约定 `1.2.0` 使用新摘要，并要求包含可为 null 的身份字段。位移台上下文封装保留 `version: 1`。旧版智能体必须阅读通信指南，适配支持的约定和摘要，并保留完整上下文。服务提供真实身份时，不带身份的参考不能匹配该服务。

拒绝或失败的命令在控制区显示原因。展开 Recent commands and results 可查看完整记录。

每次明确选择 Send command 都创建新的 `request_id`。如果响应丢失，Portal 查询原 ID，不重发命令。结果未确定时，禁止再次提交。切换来源会清空命令视图，并忽略旧来源迟到的响应。

### 交接与连接丢失

Take control 把操作权交给人。Release control 清空操作员。Revoke access 断开智能体并禁止重连。撤销后，智能体从 Agent connections 列表消失。内部保留连接权限记录，用于拒绝旧凭据及追踪命令或 Stop 的发送者。已批准的智能体断线后仍保留在列表中。这些归属变更要求设备空闲。

每次变更都会更新 `control_epoch`。命令工作线程取消过期任务，并只释放该智能体的设备租约。清理时不会获取其他持有者的租约。

选择 Connect 后，本地 Portal 进程每 2 秒发送 1 次心跳。最小化或关闭浏览器不会结束智能体连接。保持 Portal 进程运行、PC 唤醒和 LarePass 连接。明确选择 Disconnect 会停止本地智能体会话。撤销权限、会话过期或重启 Console 也会结束会话。重新打开页面只显示当前会话，不发送设备命令。

本地智能体连接后，即使浏览器关闭，也每秒采集 1 个原始状态快照。内存只保留最新快照。此接收检查不解码数据，也不验证数据约定，状态标为未验证。浏览器可视化保留独立的已验证数据流。`GET /api/agent-control/latest?source=live` 返回本地接收状态和最后的原始快照。Disconnect 停止采集。网络错误会把保留快照标为最后收到的数据。

连续 10 秒未收到心跳后，会话和授权过期。Console 为失联操作员记录警告。重连后仍为观察者，必须再次明确授权。

连接监控使用 `policy: "warn_only"` 和 `shutdown_enabled: false`。它不添加自动关炉动作。清理会释放运动控制。加热炉遵循原有运行与恢复规则。

警告包含发送者，并保留到人工选择 Dismiss warning。此操作向 `POST /lab/api/agent/control/acknowledge-warning` 发送 `{}`。关闭警告不会恢复操作权，也不会删除事件记录。

客户端连接权限和 `last_connected_at` 保存在 Console 运行目录的 `agent-connect/authority.db`。该时间戳以 Unix 秒记录最近的成功连接或重连。未记录连接时间时，值为 `null`，历史记录也按此处理。配对、心跳、状态读取、释放操作权和遭拒的连接均不更新它。邀请、会话和授权不持久化。重启 Console 会使会话和授权失效。

PC 启动器在重启后恢复加密配对。会话令牌和操作权仍只保存在内存中。配对被撤销或无效时，须使用新邀请。旧版待批准客户端也须使用新邀请。

Start test、Run next step 和 Stop test 提供独立的内存交接测试。测试包含 3 个手动步骤，在 Olares 上也只改变内存。Stop test 只停止该测试，不停止设备。

### 命令接口与结果

命令路由以 `/lab/api/agent/control` 为基础路径：

| 方法 | 路径 | 权限与结果 |
| --- | --- | --- |
| GET | `/commands/catalog` | 公开。读取定义、可用状态和摘要。 |
| GET | `/commands/choices` | 已认证。读取已存配置和设备选项。 |
| GET | `/commands/history` | 已认证。读取最近 50 条摘要及总数。 |
| GET | `/commands/request/<request_id>` | 已认证。读取完整记录。 |
| POST | `/commands` | 已认证。新命令要求当前操作员和约定。 |
| POST | `/commands/cancel` | 已认证。为自己持有的操作发送 `{"request_id":"..."}`。 |

智能体使用 `Authorization: Bearer <session_token>`。Portal 从服务器内存提供令牌，浏览器不能替换。智能体只能读取自己客户端的记录。受信任的 Console 管理可读取全部记录。如果 ID 匹配多个客户端，管理查询返回 409。

版本 1 命令目录包含 `version`、`contract_id` 和 `actions`。摘要以 `sha256:` 开头。每个动作包含 `id`、`label`、`description`、`params`、`defaults`、`device`、`available`、`reason` 和 `revision`。可用状态可能变化，而定义摘要不变。

只提交 `request_id`、`boot_id`、`contract_id`、`control_epoch`、`action` 和 `params`，以及可选的 `preconditions`。最后 2 个字段必须为 JSON 对象。Portal 明确发送 `preconditions`。执行前，服务器检查当前标识、命令摘要、操作员授权和动作约定。

为保持兼容，服务器接受旧版或手动客户端发送的 `{}`。本地模型命令流程要求位移台运动和传送配置操作使用已保存的位移台上下文。如果 Console 未提供上下文，Portal 拒绝这些动作。其他动作使用 `{}`。

将控制状态中的 `command_boot_id` 用作命令的 `boot_id`。其 `identity_scope` 为 `command_control`。原样保留返回的 `control_epoch` 字符串。状态的 `command_contract_id` 必须匹配目录的 `contract_id`。只读数据流有独立的 `boot_id` 和约定摘要，不得用它们替换命令标识。

请求 ID 属于客户端。会话有效时，相同 ID 和请求体返回原记录，不重复执行。同一 ID 使用不同请求体时返回 409。会话过期后不能读取或重发请求。结果未知时，查询原 ID。不得自动用新 ID 重发运动命令。

操作状态为 `accepted`、`running`、`succeeded`、`failed`、`rejected`、`cancelling`、`stopped` 或 `unknown`。Cancel my operation 请求取消本会话的操作。它不能证明设备已停止。取消加热炉命令保留原有运行与恢复规则。必须停止这些输出时，使用明确的加热炉或气体 Stop 控件。

原生 `command` 路径将完成结果绑定到接受时的 `command_id`、服务 `instance`、动作和服务 `client`。服务客户端属于 Console 设备租约，与配对智能体的 `client_id` 分开。完成结果必须匹配进程、`last_command.id`、`last_command.action` 和 `last_command.client`。接受时身份缺失、进程身份变化或结果身份冲突时，结果为 `unknown`。另一进程复用命令 ID 不能证明命令完成。查询已存操作，不得自动重放。

身份和修订均可用时，受保护的位移台运动携带 `expected_instance` 和 `expected_reference_revision`。排队决策和后续轴保留原参考。旧版运动保留原生限位、Stop 版本和命令 ID 检查。缺少服务进程身份时，该路径不能验证进程连续性。只有身份而没有修订时，保留进程检查，但只有修订而没有身份时，不能运动。新增的身份字段要求创建新操作。

记录保存在权限数据库旁的 `agent-connect/commands.db`。重启会把未完成记录改为 `unknown`，不会重放。本地模拟使用内存或临时存储。重启本地测试会清空其记录和配置。

历史返回 `items` 和 `count`。摘要省略参数和结果。展开操作可读取完整记录。

### Stop 与发送者历史

Stop 使用同一控制基础路径：

| 方法 | 路径 | 结果 |
| --- | --- | --- |
| POST | `/stop` | 执行 Stop 并返回初始记录。 |
| GET | `/stops` | 读取有上限的摘要和审计状态。 |
| GET | `/stops/<id>` | 读取投递详情和设备结果。 |

只发送请求 ID 和目标：

```json
{"request_id":"example-stop-1","target":"all_motion"}
```

| 目标 | 效果 |
| --- | --- |
| `stage`、`robot`、`microscope` | 停止所选运动设备。 |
| `all_motion` | 停止上述运动设备，不包括加热炉。 |
| `furnace` | 请求原生加热炉 Stop，停止加热和气体。 |
| `gas_all` | 停止全部 3 个气路。 |
| `gas_0`、`gas_1`、`gas_2` | 停止气路 0、1 或 2。 |

Stop 不要求会话、操作员授权、当前版本或约定摘要。它不等待普通命令处理或状态刷新。服务器确定发送者，调用者不能在请求体中指定身份。权限服务可用时，不带 `Authorization` 的请求可归为受信任的人工调用。凭据无法识别或无法确定身份时，发送者标为未验证。智能体有令牌时携带令牌，但不得因缺少访问权限阻止 Stop。

每次明确按下按钮都创建新 ID。同一 ID 和目标的请求共用历史记录，但每次投递仍执行 Stop。详情保留所有传输路径、发送者和设备结果。混合发送者的请求不会继承另一次投递的已验证身份。

Stop 历史状态为 `pending`、`confirmed`、`unconfirmed`、`unknown` 或 `disabled`。收到请求不能证明设备已停止。后续投递成功也不会把未知投递改为已确认。现有人工紧急路径保留原有执行方式，并在执行后添加发送者历史。

Console 把历史保存在 `runs/agent-connect/stops.db`。审计存储不能延迟 Stop 执行。历史会报告存储失败。进程在保存前退出时，近期记录可能丢失。重启把未完成投递改为 `unknown`。已存 Stop 从不重放。

### 独立 Stop 网关

普通 Stop 路径出现网络错误或 HTTP 503 时，Portal 使用固定备用路径。它保留请求 ID，且不发送凭据：

| 来源 | 备用基础路径 |
| --- | --- |
| 真机 | `http://100.64.0.1:8082/lab/api/agent/emergency` |
| 模拟 | `http://127.0.0.1:8794` |

两者都提供 `/stop`、`/status`、`/stops` 和 `/stops/<id>`。备用进程没有 Console 会话存储，因此调用者标为未验证。恢复后，历史仍保留 Console 和备用路径的记录。服务或网络故障仍可能使 Stop 未确认。备用路径不重放普通命令，也不把实时读数替换成模拟数据。

Olares 网关监听 `127.0.0.2:8874`。8082 代理把 `/lab/api/agent/emergency/*` 转发到该网关。真机部署运行 `orchestrator.agent_connect.fallback --physical`，并保存 `runs/agent-connect/emergency-stops.db`。此模式只提供固定的原生 Stop 和状态处理器。网关不提供普通真机命令。

库默认仍禁用网关真机操作。使用 `service_unit(checkout, physical=True)` 生成真机单元。将其安装为 `mg400-agent-emergency-test.service`，然后启用开机启动：

```sh
sudo systemctl enable --now mg400-agent-emergency-test.service
```

普通 `deploy/twin.py` 注册表不会重启此单元。修改网关代码后，单独重启：

```sh
sudo systemctl restart mg400-agent-emergency-test.service
```

检查紧急状态中的 `equipment_mode: "physical"` 和 `stop_enabled: true`。网关独立于 Console 进程，但仍依赖代理、网络和设备服务。它不改变 8080 Console，也不重启共用设备进程。


## 可选模拟测试

Enable simulation tests（启用模拟测试）默认关闭。启用后，页面停止实时读取，并打开测试区。关闭后，页面断开测试连接，并恢复实时读取。测试未启用时，不在后台读取模拟数据。只有当前选中的测试标签页运行。

可选测试使用 Simulated Console（模拟 Console）：`http://127.0.0.1:8790/lab/`。从测试区的链接打开它。启动命令不会自动打开此页。测试数据源标有 `MOCK`。测试控件只修改本地模拟数据，不访问 Olares。

模拟适配器使用固定回环地址，并忽略代理变量。它拒绝重定向和非模拟发现响应。

### 数据格式测试

选择 Data format tests（数据格式测试），可使用 `/lab/api/agent-fixtures` 下的兼容测试。

Simulated Console 运行更新时，位移台位置和温度持续变化。它还包含 2 个芯片样例。Earlier agent version（A，旧版智能体）和 Newer agent version（B，新版智能体）均为脚本测试。每个脚本各自保存数据约定，即消息格式、字段和单位。新版脚本支持新增的可选字段。

1. 打开 Simulated Console。在 Data format tests 中查看发现响应、收到的读数和已校验的数据约定。
2. 选择 Pause updates（暂停更新）。读数超过 3 秒后，显示 Outdated reading（读数已过期）。轮询不改变原始采样时间。
3. 选择 Resume updates（恢复更新），接收新样本。Simulate old readings（模拟过期读数）会暂停更新，并立即将当前样本变为过期。
4. 选择测试设置。比较当前约定摘要、已保存的摘要、定义变化和各脚本的兼容结果。
5. 断开脚本连接。选择 Simulate Console restart 后重新连接，查看新的进程 ID。
6. 运行基本场景。将收到的消息、约定文件和预期/实际结果导出为 JSON。

| 测试设置 | 检查内容 |
| --- | --- |
| Original format（1.0，原始格式） | 使用原有定义。 |
| Add optional field（1.1，新增可选字段） | 接受兼容的新增字段。 |
| Change position units（2.0，更改位置单位） | 检测单位变化。 |
| Unrecognized schema format（无法识别的模式格式） | 拒绝不支持的模式方言。 |
| Damaged agreement file（损坏的约定文件） | 拒绝摘要不匹配的文件。 |

Change coordinate reference（更改坐标参考）增加位移台参考修订号，并保留当前位置。Simulate Console restart（模拟 Console 重启）更改 Console 和位移台进程 ID，重置序号，并增加参考修订号。它保留读数和芯片记录。Reset demo（重置演示）恢复原始格式、初始读数和芯片记录，然后恢复更新。它还重置序号、参考修订号和数据源历史，并生成新的进程 ID。

场景测试使用独立的临时数据源，不重置交互页面的数据源。导出内容保留有数量上限的消息历史。重启测试页服务器会清除内存记录。

### 样品与数据流测试

Samples & stream 使用 `/lab/api/agent`。Connection A 和 Connection B 是独立的浏览器 WebSocket 连接。每个连接校验自己的约定摘要，并保留自己的重放游标。选择设备、芯片或操作数据组，以及 1、2 或 4 Hz 的频率。默认频率为 2 Hz。每行输入一个完整芯片 ID，或选择工作中的芯片。将选择应用到两个连接。

对任一连接使用 Read slowly（慢速读取），另一连接会继续接收。断开后重新连接，可从保留的游标继续读取。Simulate restart（模拟重启）更改进程标识。Expire saved events（清除已存事件）移除重放历史。连接会报告历史缺失，并读取新快照。约定变化会触发重新发现。即使丢失变化事件，未知的帧摘要也会触发重新发现。不支持的定义会停止读数解释。

Browse saved chips（浏览已存芯片）分页读取芯片列表。如果翻页期间样品修订号变化，使用 Browse saved chips 重新开始。人工报告和 Mark location unknown（标记位置未知）只修改模拟样品库。证据按钮可模拟待完成取片、推断放片、相机连续观测、旧帧和身份冲突。模拟入炉使用测试映射。丢失参考后，位移台承载位置保留，入炉状态变为未知。

## 样品证据

阶段 2 在现有 `runs.db` 中保存有序 `chip_events` 和 `sample_current` 行。事件写入、当前状态和修订号更新共用一个事务。`samples` 中的原有运行测量不变。读取不会创建事件或当前状态行。当前状态行可从芯片事件重建。

芯片 ID 保持原样。历史运行、步骤、测量、文件和事件记录可确定芯片身份，但不能确定当前位置。导入芯片的初始位置为未知。最新运行和步骤只取自该芯片已开始的工作。较新的未开始运行或其他芯片的工作不会替换这些值。

证据依据为 `observed`、`reported`、`inferred` 或 `unknown`。接受操作不证明芯片已到达。交接期间，承载位置可变为未知，但保留上次已知位置和待完成目标。确认运动前已拒绝操作时，保留原承载位置。旧帧不能恢复过时位置。冲突会使有争议的判断变为未知。来源观测时间与记录更新时间分开保存。

承载位置与是否入炉是两个判断。丢失入炉参考时，保留已确定的位移台承载位置。真机尚未配置炉内映射，因此 Olares 上的 `inside_furnace` 保持 `null`。模拟映射不能确定真机位置。

芯片履历在 Console 基础路径下读取 `/api/chip/location?id=...`，并同时显示当前证据和原有历史。真机 Report location（报告位置）和 Mark unknown（标记未知）仍禁用。本地模拟表单仍是唯一的人工报告接口。

## 读取接口与重放

Console 在 `/lab/api/agent` 下提供以下读取路径：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/discovery` | 读取进程标识、当前约定和接口路径。 |
| GET | `/contracts/{digest}.json` | 读取不可变的 JSON Schema 2020-12 定义包。 |
| GET | `/v1/state` | 读取所选当前快照。 |
| GET | `/v1/samples` | 读取有数量上限的样品页。 |
| WebSocket | `/v1/stream` | 接收当前状态和有序事件，不支持发送命令。 |

状态与数据流选项为 `groups`、`rate_hz`、`selection` 和可重复的 `chip_id` 字段。数据组为 `devices`、`samples` 和 `operations`。选择 `active` 或 `ids`。活动芯片有待完成操作，或在运行中、暂停的运行里已有开始的工作。指定芯片最多接受 50 个 ID，每个最多 256 个字符。ID 含逗号时，用重复的 `chip_id` 字段保留完整文本。另一种 `chip_ids` 字段会按逗号拆分 ID。

样品页还接受 `selection=all`、`limit`、`offset` 和 `expected_revision`。每页最多 100 条。后续页在 `expected_revision` 中传入返回的修订号。修订号不匹配时，必须重新分页。读取 `count`、`total`、`complete` 和 `next_offset`。完整标记只适用于所选芯片。

共用数据源按 4 Hz 组装帧。订阅者按 1、2 或 4 Hz 接收完整帧。设备测量可能更新得更慢。来源时间和数据年龄与帧时间分开保存。订阅者读取共用设备缓存，不增加硬件轮询。状态、重放、订阅者数量和消息大小均有固定上限。慢速读取者保留最新的未发送状态，不累积旧帧。

同一个共用读取器按 1 Hz 读取热成像摘要。视觉任务变化时，它只读取 1 次已有的最新结果。它不启动采集任务。所有订阅者复用这些摘要，不单独轮询硬件。

较早的视觉结果对当前场景的质量为 `unknown`。来源未提供采集时间时，该时间保持 `null`。

重连时同时发送 `boot_id` 和 `event_seq`。数据流先发送当前快照，再发送保留的事件。事件保留原 `contract_id`，重放不改变当前约定。`resync` 消息报告 `boot_changed`、`cursor_ahead` 或 `history_evicted`。出现缺口后，读取新快照。共用采样历史与重复广播分开保存。

## 缓存字段与版本标识

每台设备添加 `cached_state`，包含 `values`、`units`、`complete`、`omitted` 和 `omitted_count`。这些字段提供有上限的原生缓存值，不增加硬件轮询。副本排除私密数据、路径、图像和热像矩阵。大小限制可能省略更多字段。判定副本完整前，先检查省略原因。

设备的 `data_gaps` 列出缺失的来源标识或时间戳。未知值保持未知。Portal 显示收到的嵌套字段和原始 JSON。Console 帧是新的，不代表传感器读数也是新的。来源单位、观测时效和帧生成时间分别保留。

发现接口的 `release` 包含 `release_id`、`source_bundle_id`、`read_contract_id`、`command_contract_id`、`source`、`complete` 和 `scope`。范围为 `this-console`。版本 ID 由启动时的源码摘要和当前两个约定摘要共同确定。源码摘要只覆盖有上限的 Console 源文件集合，不覆盖设备固件或全部共用服务。

`source` 对象报告 `digest`、`complete`、`file_count`、`bytes_hashed` 和 `scope`。即使磁盘文件变化，运行进程仍保留启动时的源码标识。元数据缺失或源码副本不完整时，仍报告不完整。识别已审查的 Console 版本时，对比版本 ID 和两个约定 ID。

## 当前范围

命令目录把智能体操作限制在已审查的原生能力内。启用真机不会启用不受支持的动作，也不会移除设备限制。实时读取、模拟设备和格式测试保留独立的标识与结果。模拟检查不能确定真机行为。

部署前，核对 Console 身份检查与当前共用服务的实现。检查目标 Console 的实际服务路径、进程身份和已接受命令字段。缺失的服务 ID 仍显示不可用。Console `boot_id`、客户端身份和 `control_epoch` 保持启用。
