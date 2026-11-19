[English](DEV_NOTE.en.md)

# 本地控制台开发

本地预览使用实际 Lab Console 界面和模拟设备，默认打开 Motion。Position Finder (legacy) 默认关闭。预览不连接 Olares，也不使用 LarePass VPN。

外观开发使用此预览。共享仪表板的页头显示 AI Chemistry Console，标志来自 `src/orchestrator/web/static/console/console-logo.webp`。只有明确要求时，才把外观修改部署到 Olares。

最后一个工作区标签为 Position Finder (legacy)，地址为 `#position-finder`。打开启用开关后可使用监控和校准。关闭后停止请求并释放临时图像数据。已存配置仍保留在磁盘上。

独立的 **Twin** 工作区使用 `#twin`，显示 3D 台面模型。它使用 `twin.js` 和 `twin.css`。离线预览可能无法加载模型资源和 CDN 库。定义模型映射前，编码器位移台几何保持未校准。

## 启动与停止

打开 PowerShell。使用现有 Python 环境启动预览：

```powershell
Set-Location C:\Users\Swift\Documents\Github\dobot_mg400
.\.venv\Scripts\python.exe scripts\console_preview.py
```

打开[本地 Motion 控制台](http://127.0.0.1:8790/lab/#motion)。保持终端打开。在该终端按 `Ctrl+C` 停止预览。关闭浏览器不会停止服务。

打开[本地炉子工作区](http://127.0.0.1:8790/lab/#process)，检查 **Turret calibration**。

打开[本地样品转移工作区](http://127.0.0.1:8790/lab/#sample-transfer)，检查已存位置、手动调整和 STOP。

如果缺少环境或依赖包，从仓库根目录安装预览和测试依赖：

```powershell
uv sync --locked --no-default-groups --group vision --group stage --group dev
```

然后重新运行启动命令。如果依赖包不在缓存中，安装时须联网。预览本身可离线运行。

如果端口 `8790` 已占用，改用其他端口：

```powershell
.\.venv\Scripts\python.exe scripts\console_preview.py --port 8791
```

该实例的地址为 `http://127.0.0.1:8791/lab/#motion`。预览绑定 `127.0.0.1`，须在同一台 PC 上打开。在 Windows 上使用此启动脚本。完整编排器演示须在 Linux 上运行。

## 修改与检查

- 控制台样式和脚本位于 `src/orchestrator/web/static/console/`。修改 CSS 或 JavaScript 后，刷新浏览器。如果仍显示旧版本，按 `Ctrl+F5`。
- 修改 Python 或 HTML 模板后，重启预览。模板位于 `src/orchestrator/web/templates/`。启动脚本不会自动重新加载 Python 或模板。
- Motion 的 X/Y 和 Insert 行程图显示实时编码器圈数。先点击 **Take control**，再对各轴执行 **Auto calibrate** 以确定行程范围。图表不请求 Position Finder 测量。
- 双击任一图表选择目标。检查另一个标记是否闪烁，且点击 **Confirm move** 前电机不移动。未确认的目标在 5 s 后清除。确认 X/Y 目标以检查 Bottom、Top 依次移动，确认 Insert 目标以检查仅 Insert 移动。检查 **Cancel** 和 STOP。
- 打开 Position Finder (legacy) 的启用开关，检查摄像头选择、校准和配置操作。检查关闭后是否停止采样和预览请求。
- 转盘校准使用 `turret.js` 和 `turret.css`，由 `process.js` 挂载。在 **Turret calibration** 中检查逐物镜记录、**Retake**、实时与已记录图像切换、错误提示和配置列表。检查 **Save & use** 是否保存并选择完整配置。检查物镜按钮旁的 **Lens calibration** 是否显示所选配置。
- Sample Transfer 使用 `sample-transfer.js` 和 `sample-transfer.css`。检查手动调整、已存位置删除、配置管理、JSON 上传下载和 STOP。记录或移动位置前，先校准模拟位移台。
- 用语言按钮旁的 **Dark mode** / **Light mode** 切换主题，检查两种模式下的外观。浏览器按控制台地址记住所选模式。
- 优先完善英文标签。翻译使用现有字体或系统后备字体。本地预览不必重新构建字体。

## 配置与模拟数据

- 配置和摄像头选择保存在 `work/console-preview/calibration/twin/`。Git 忽略 `work/`。本地配置不会与 Olares 同步。
- 如果该目录不存在，首次启用旧版功能时创建 `Mock stage` 配置。用它检查监控读数，以及配置的保存、加载、上传和删除。
- 新建的 `Mock stage` 配置使用 `3840 × 2880` Camera1 标记图像。标记随模拟位移台移动。已有的 `640 × 480` 配置保留原尺寸。其他 Position Finder 相机返回固定图像。切换摄像头不会选择物理摄像头。
- 为 Camera1 使用现有 `Mock stage` 光学校准配置。静止图像无法提供不同的校准端点。不移动位移台而重复采集低端点和高端点，无法建立新的轴校准。
- 上传的配置即使通过格式校验，图像尺寸仍可能不匹配。用于 `3840 × 2160` 图像的配置无法测量 `3840 × 2880` 模拟帧。
- 如须重置本地配置，先停止预览。把 `work/console-preview/calibration/twin/` 改成未使用的备份名称。重启预览并启用旧版功能后，会重新创建 `Mock stage`。如须保留原配置，保留备份。在界面中删除配置不会重新创建初始配置。
- 转盘校准使用 `1280 × 720` 模拟 Camera3 图像，图中只有当前物镜标记。物镜标记边长约为 8 mm。ID 0、1、2、3 分别对应 5x、10x、20x、50x。点击 **Record** 时，模拟图像切换到所选物镜。
- 转盘配置保存在 `work/console-preview/calibration/microscope/turret_profiles/`。点击 **Start calibration** 后，依次记录 4 个物镜。点击 **Finish calibration**，再点击 **Save & use**。未完成的配置使用 **Save profile** 保存，用 **Load draft** 继续编辑。JSON 上传上限为 2 MiB。
- **Lens calibration** 选择的完整配置供 5x、10x、20x 和 50x 按钮使用。重启预览后，所选配置保留。完整配置包含全部 4 个物镜的记录。记录时的对焦位置仅供参考，未知位置保留为未知，不按 `0` 处理。
- 实机 **Start calibration** 只释放转盘电机。建议先手动把对焦轴移到界面显示的避让位置。界面以 `MICRO 4` 脉冲显示避让位置和已知的记录位置。自动移动前，当前对焦零点必须已知。转盘转动前，对焦轴必须到达已配置的避让位置。
- 保持当前物镜标记清晰且完整可见。对齐使用固定摄像头下已记录的图像坐标。移动摄像头或改变其对焦、变焦后，重新记录全部 4 个物镜。本地模拟不验证定位精度或实际电机行为。

## 样品转移预览

预览与 Motion 共用编码器位移台控制器，硬件由模拟器代替。移动前获取位移台控制权。在 Motion 对各轴执行 **Auto calibrate**，并等待校准完成。固件模拟器提供转数和行程限位。无需相机或 Position Finder 配置。

在 **Operate** 中，**Extract Sample** 将 Insert 移到 `0`，**Insert Sample** 将 Insert 移到当前上限，无需已存配置。打开 **Sample Transfer → Calibration**。用 **Manual stage adjustment** 调整位置，每次按键移动 0.1 转。为 **Move Out Stage** 和 **Move to Microscope** 记录 X/Y 目标，再保存配置。这些动作先移动 Bottom，再移动 Top，Insert 保持不变。若要添加其他位置，输入名称并记录完整编码器坐标。在 **Operate** 中，**Move to position** 依次移动 Bottom、Top、Insert，每次只移动 1 个轴。**STOP** 和 **STOP ALL MOTION** 无需控制权即可取消模拟移动。

配置保存在 `work/console-preview/calibration/sample-transfer/`。默认名称为 `Stage Coordinate Calibration YYYY-MM-DD`，日期取浏览器本地日期。**Load draft** 继续编辑。**Select** 选择命名移动和 X/Y 动作使用的已存配置。用 **Upload JSON**、**Download** 和 **Delete** 管理配置。可以删除草稿中的单个位置或 X/Y 目标。保存草稿修改后再使用。重启预览后配置仍保留，但必须重新校准位移台。

旧毫米配置仍可查看、下载和删除，但不能记录编码器位置或移动电机。请新建转数配置。已有 `motion-scale.json` 文件不再使用。配置要求参见[样品转移操作](docs/dashboard.md#样品转移工作区)。

## 运行本地检查

修改预览脚本或Position Finder Python 代码后，从仓库根目录运行相关测试：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_console_preview.py tests/test_console_position_finder.py tests/test_stage_twin.py
.\.venv\Scripts\ruff.exe check
.\.venv\Scripts\ruff.exe format --check
```

修改Position Finder JavaScript 后，使用 `PATH` 中的 Node.js 运行：

```powershell
node --test tests/test_console_position_finder.mjs
```

修改转盘校准 JavaScript 后，运行：

```powershell
node --test tests/test_console_turret.mjs
```

同时在浏览器中检查修改后的视图。自动测试无法确认布局是否正确。代码更新后运行测试。提交时沿用这些结果。手动推送和发布分支。

## 仍须在 Olares 上测试的内容

预览中的其他设备面板显示不可用或离线。设备命令和停止按钮不会控制物理设备。

真实摄像头分辨率、采样耗时、视频流中断、校准精度和设备命令须在 Olares 上测试。模拟检查通过不代表这些行为正确。Linux 演示参见[控制台指南](docs/dashboard.md#离线运行)。
