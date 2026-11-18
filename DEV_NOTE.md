[English](DEV_NOTE.en.md)

# 本地控制台开发

本地预览使用实际 Lab Console 界面。Position Finder、转盘校准和 Sample Transfer 接口使用模拟摄像头数据。它不连接 Olares，也不使用 LarePass VPN。

外观开发使用此预览。共享仪表板的页头显示 AI Chemistry Console，标志来自 `src/orchestrator/web/static/console/console-logo.png`。Digital Twin 标签显示为 Position Finder。工作区地址仍为 `#twin`。只有明确要求时，才把外观修改部署到 Olares。

## 启动与停止

打开 PowerShell。使用现有 Python 环境启动预览：

```powershell
Set-Location C:\Users\Swift\Documents\Github\dobot_mg400
.\.venv\Scripts\python.exe scripts\console_preview.py
```

打开[本地数字孪生控制台](http://127.0.0.1:8790/lab/#twin)。保持终端打开。在该终端按 `Ctrl+C` 停止预览。关闭浏览器不会停止服务。

打开[本地炉子工作区](http://127.0.0.1:8790/lab/#process)，检查 **Turret calibration**。

打开[本地样品转移工作区](http://127.0.0.1:8790/lab/#sample-transfer)，检查命名位置、移动和 STOP。

如果缺少环境或依赖包，从仓库根目录安装预览和测试依赖：

```powershell
uv sync --locked --no-default-groups --group vision --group dev
```

然后重新运行启动命令。如果依赖包不在缓存中，安装时须联网。预览本身可离线运行。

如果端口 `8790` 已占用，改用其他端口：

```powershell
.\.venv\Scripts\python.exe scripts\console_preview.py --port 8791
```

该实例的地址为 `http://127.0.0.1:8791/lab/#twin`。预览绑定 `127.0.0.1`，须在同一台 PC 上打开。在 Windows 上使用此启动脚本。完整编排器演示须在 Linux 上运行。

## 修改与检查

- 控制台样式和脚本位于 `src/orchestrator/web/static/console/`。修改 CSS 或 JavaScript 后，刷新浏览器。如果仍显示旧版本，按 `Ctrl+F5`。
- 修改 Python 或 HTML 模板后，重启预览。模板位于 `src/orchestrator/web/templates/`。启动脚本不会自动重新加载 Python 或模板。
- 在数字孪生监控和校准视图中检查布局、摄像头选择、校验提示和配置操作。
- 转盘校准使用 `turret.js` 和 `turret.css`，由 `process.js` 挂载。在 **Turret calibration** 中检查逐物镜记录、**Retake**、实时与已记录图像切换、错误提示和配置列表。检查 **Save & use** 是否保存并选择完整配置。检查物镜按钮旁的 **Lens calibration** 是否显示所选配置。
- Sample Transfer 使用 `sample-transfer.js` 和 `sample-transfer.css`。检查手动调整、命名位置的记录和删除，以及已存配置。检查 JSON 上传下载、插入轴自动移动和 STOP。
- 用语言按钮旁的 **Dark mode** / **Light mode** 切换主题，检查两种模式下的外观。浏览器按控制台地址记住所选模式。
- 优先完善英文标签。翻译使用现有字体或系统后备字体。本地预览不必重新构建字体。

## 配置与模拟数据

- 配置和摄像头选择保存在 `work/console-preview/calibration/twin/`。Git 忽略 `work/`。本地配置不会与 Olares 同步。
- 如果该目录不存在，启动时创建 `Mock stage` 配置。用它检查监控读数，以及配置的保存、加载、上传和删除。
- 新建的 `Mock stage` 配置使用 `3840 × 2880` Camera1 标记图像。标记随 Sample Transfer 模拟位移台移动。已有的 `640 × 480` 配置保留原尺寸。其他 Position Finder 相机返回固定图像。切换摄像头不会选择物理摄像头。
- 为 Camera1 使用现有 `Mock stage` 光学校准配置。静止图像无法提供不同的校准端点。不移动位移台而重复采集低端点和高端点，无法建立新的轴校准。
- 上传的配置即使通过格式校验，图像尺寸仍可能不匹配。用于 `3840 × 2160` 图像的配置无法测量 `3840 × 2880` 模拟帧。
- 如须重置本地配置，先停止预览。把 `work/console-preview/calibration/twin/` 改成未使用的备份名称。重启预览后会再次创建 `Mock stage`。如须保留原配置，保留备份。在界面中删除配置不会重新创建初始配置。
- 转盘校准使用 `1280 × 720` 模拟 Camera3 图像，图中只有当前物镜标记。物镜标记边长约为 8 mm。ID 0、1、2、3 分别对应 5x、10x、20x、50x。点击 **Record** 时，模拟图像切换到所选物镜。
- 转盘配置保存在 `work/console-preview/calibration/microscope/turret_profiles/`。点击 **Start calibration** 后，依次记录 4 个物镜。点击 **Finish calibration**，再点击 **Save & use**。未完成的配置使用 **Save profile** 保存，用 **Load draft** 继续编辑。JSON 上传上限为 2 MiB。
- **Lens calibration** 选择的完整配置供 5x、10x、20x 和 50x 按钮使用。重启预览后，所选配置保留。完整配置包含全部 4 个物镜的记录。记录时的对焦位置仅供参考，未知位置保留为未知，不按 `0` 处理。
- 实机 **Start calibration** 只释放转盘电机。建议先手动把对焦轴移到界面显示的避让位置。界面以整步显示避让位置和已知的记录位置。自动移动前，当前对焦零点必须已知。转盘转动前，对焦轴必须到达已配置的避让位置。
- 保持当前物镜标记清晰且完整可见。对齐使用固定摄像头下已记录的图像坐标。移动摄像头或改变其对焦、变焦后，重新记录全部 4 个物镜。本地模拟不验证定位精度或实际电机行为。

## 样品转移预览

在 Position Finder 中为 Camera1 选择 `Mock stage`。打开 **Sample Transfer → Operate**。**Extract Sample** 和 **Insert Sample** 使用该光学校准配置的完全移出和完全移入位置。它们只移动插入轴，不要求 Sample Transfer 配置。

使用新建的 `3840 × 2880` 模拟配置检查混合运动。旧的 `640 × 480` 配置图像细节不足，无法可靠检查微小移动。按上文方法备份本地配置目录并重启，即可创建新配置。系统不会自动改写已有配置。

在 **Calibration** 中新建配置。用 **Manual stage adjustment** 移到目标位置。输入 **Position name**。点击 **Record position** 采集新的 X、Y 和插入轴坐标。用其他名称记录更多位置，再点击 **Save profile**。检查 **Record again** 和单个位置删除。

命名位置保存后供未来使用。选择配置不移动位移台，也不改变 Extract/Insert 目标。

每次手动步进发送 10 个脉冲，距离取决于细分模式。Extract/Insert 选择 1/8 细分，并使用 640 脉冲/mm 的硬件模型。模拟相机随同一位移台状态变化，记录使用实际图像换算。**STOP** 和 **STOP ALL MOTION** 停止模拟转移。此预览不要求控制租约或物理设备。

预览加快了模拟运动，未模拟实际加速过程。其转移耗时不能预测实际转移耗时。

转移配置保存在 `work/console-preview/calibration/sample-transfer/`。**Save profile** 保留草稿。**Load draft** 继续编辑。**Select** 选择配置供查看。用 **Upload JSON**、**Download** 和 **Delete** 管理已存配置。配置引用对应的 Position Finder 配置和相机。复用位置配置时，保留它引用的 `Mock stage`。

自动转移先检查脉冲换算，再使用较大的移动批次，接近目标时减小移动量。每次移动停止后再测量，连续 3 次一致读数距目标不超过 0.5 mm 时完成。检查界面的阶段、脉冲换算、坐标和停止行为。学习得到的换算保存在 `work/console-preview/calibration/sample-transfer/motion-scale.json`。模拟运动不能证明实际精度。位置记录和来源要求参见[样品转移操作](docs/dashboard.md#样品转移工作区)。

## 运行本地检查

修改预览脚本或数字孪生 Python 代码后，从仓库根目录运行相关测试：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
.\.venv\Scripts\python.exe -m pytest -q tests/test_console_preview.py tests/test_console_twin.py tests/test_stage_twin.py
.\.venv\Scripts\ruff.exe check
.\.venv\Scripts\ruff.exe format --check
```

修改数字孪生 JavaScript 后，使用 `PATH` 中的 Node.js 运行：

```powershell
node --test tests/test_console_twin.mjs
```

修改转盘校准 JavaScript 后，运行：

```powershell
node --test tests/test_console_turret.mjs
```

同时在浏览器中检查修改后的视图。自动测试无法确认布局是否正确。代码更新后运行测试。提交时沿用这些结果。手动推送和发布分支。

## 仍须在 Olares 上测试的内容

预览中的其他设备面板显示不可用或离线。设备命令和停止按钮不会控制物理设备。

真实摄像头分辨率、采样耗时、视频流中断、校准精度和设备命令须在 Olares 上测试。模拟检查通过不代表这些行为正确。Linux 演示参见[控制台指南](docs/dashboard.md#离线运行)。
