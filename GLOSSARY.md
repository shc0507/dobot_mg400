# Glossary / 术语表

| Term | 术语 | Definition | 定义 |
| --- | --- | --- | --- |
| chip | 芯片 | The sample moved, heated, and imaged by the lab. Wafer is a synonym. Use chip in documentation. | 实验室搬运、加热和成像的样品。wafer 是同义词。文档统一使用 chip。 |
| crucible | 坩埚 | The container that holds the chip and travels into the furnace on the stage. | 承载芯片并由位移台送入炉内的容器。 |
| furnace window | 炉顶窗口 | The transparent opening above the chip through which the microscope images it inside the furnace. | 芯片上方的透明窗口，显微镜通过它观察炉内芯片。 |
| tray | 托盘 | A modeled arrangement of slots for identified chips. | 按模型排列槽位、用于放置已识别芯片的载具。 |
| slot | 槽位 | An addressed location in a tray where a chip can be picked or placed. | 托盘内具有地址的芯片取放位置。 |
| station | 工位 | A named, taught fingertip pose with an approach height, optional via stations, and a zone. | 带名称的示教指尖位姿，附接近高度、可选途经工位和区域。 |
| load position | 装卸位置 | The Insert-axis position where the robot can reach the crucible. | 机械臂可接近坩埚时的 Insert 轴位置。 |
| pose | 位姿 | A robot position and rotation, or a named combination of stage-axis positions. | 机械臂的位置与旋转，或带名称的位移台各轴位置组合。 |
| zone | 区域 | A shared physical space with exclusive ownership and entry conditions for coordinated device motion. | 具有独占使用权和进入条件、用于协调设备运动的共用空间。 |
| lease | 控制租约 | Time-limited ownership of a service by a controlling client, renewed through heartbeat requests. | 控制客户端限时持有的服务控制权，通过心跳请求续期。 |
| recipe | 配方 | A versioned TOML definition of parameters and ordered skill steps, including timeouts and failure actions. | 带版本的 TOML 定义，包含参数、有序操作步骤、超时和失败处理。 |
| skill | 操作单元 | A named orchestrator operation invoked by a recipe step with parameters. | 由配方步骤携带参数调用的具名编排器操作。 |
| run | 运行 | An execution of a recipe with recorded step results, artifacts, and chip identities. | 配方的一次执行，记录步骤结果、产物和芯片身份。 |
| rehearsal | 演练 | Recipe execution against services in demo mode, using simulated devices instead of lab hardware. | 对演示模式服务执行配方，使用模拟设备代替实验室硬件。 |
| service | 服务 | A process that owns device access or a lab capability and exposes its state and commands. | 管理设备访问或实验室能力，并提供状态与命令接口的进程。 |
| contract | 契约 | The versioned interface and behavior shared by instrument services and their clients. | 仪器服务及客户端共用的带版本接口与行为约定。 |
| control plane | 控制平台 | The shared services and orchestrator that coordinate recipes, device access, and run records. | 协调配方、设备访问和运行记录的共用服务与编排器。 |
| tool offset | 工具偏移 | The measured displacement used to convert robot flange poses to fingertip poses and back. | 用于机械臂法兰位姿与指尖位姿相互转换的实测位移。 |
| calibration | 校准 | A measured mapping or reference that relates sensor or actuator coordinates to the lab geometry. | 将传感器或执行器坐标对应到实验室几何位置的实测映射或参考。 |
| asset | 资源 | An external binary or large file installed from a manifest with size and hash verification. | 按清单安装并校验大小与哈希值的外部二进制或大文件。 |
| homing | 回零 | Establishing an axis reference by finding its configured home position. | 寻找配置的原点位置，以建立轴的位置参考。 |
| soft limit | 软限位 | A configured travel boundary enforced from a known axis reference. | 基于已知轴参考位置执行的配置行程边界。 |
| clearance position | 避让位置 | The known focus position away from the furnace window that permits an objective-turret turn. | 远离炉顶窗口、允许物镜转盘转动的已知调焦位置。 |
