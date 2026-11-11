[English](DEV_NOTE.en.md)

# 本地控制台开发

本地预览使用实际 Lab Console 界面和数字孪生接口，摄像头数据为模拟数据。它不连接 Olares，也不使用 LarePass VPN。

外观开发使用此预览。共享仪表板的页头显示 AI Chemistry Console，标志来自 `src/orchestrator/web/static/console/console-logo.png`。Digital Twin 标签显示为 Position Finder。工作区地址仍为 `#twin`。只有明确要求时，才把外观修改部署到 Olares。

## 启动与停止

打开 PowerShell。使用现有 Python 环境启动预览：

```powershell
Set-Location C:\Users\Swift\Documents\Github\dobot_mg400
.\.venv\Scripts\python.exe scripts\console_preview.py
```

打开[本地数字孪生控制台](http://127.0.0.1:8790/lab/#twin)。保持终端打开。在该终端按 `Ctrl+C` 停止预览。关闭浏览器不会停止服务。

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
- 用语言按钮旁的 **Dark mode** / **Light mode** 切换主题，检查两种模式下的外观。浏览器按控制台地址记住所选模式。
- 优先完善英文标签。翻译使用现有字体或系统后备字体。本地预览不必重新构建字体。

## 配置与模拟数据

- 配置和摄像头选择保存在 `work/console-preview/calibration/twin/`。Git 忽略 `work/`。本地配置不会与 Olares 同步。
- 如果该目录不存在，启动时创建 `Mock stage` 配置。用它检查监控读数，以及配置的保存、加载、上传和删除。
- 每个演示摄像头都返回同一张固定的 `640 × 480` 标记图像。切换摄像头不会选择物理摄像头。读数不模拟位移台运动。
- 固定图像无法提供不同的校准端点。重复采集低端点和高端点无法建立新的轴校准。
- 上传的配置即使通过格式校验，图像尺寸仍可能不匹配。用于 `3840 × 2160` 图像的配置无法测量 `640 × 480` 模拟帧。
- 如须重置本地配置，先停止预览。把 `work/console-preview/calibration/twin/` 改成未使用的备份名称。重启预览后会再次创建 `Mock stage`。如须保留原配置，保留备份。在界面中删除配置不会重新创建初始配置。

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

同时在浏览器中检查修改后的视图。自动测试无法确认布局是否正确。代码更新后运行测试。提交时沿用这些结果。手动推送和发布分支。

## 仍须在 Olares 上测试的内容

预览中的其他设备面板显示不可用或离线。设备命令和停止按钮不会控制物理设备。

真实摄像头分辨率、采样耗时、视频流中断、校准精度和设备命令须在 Olares 上测试。模拟检查通过不代表这些行为正确。Linux 演示参见[控制台指南](docs/dashboard.md#离线运行)。
