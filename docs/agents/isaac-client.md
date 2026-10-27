# WebRTC 客户端自动操作

在本项目中自动操作 Isaac Sim WebRTC 客户端时，使用本页流程。
日常连接和机器人操作见 [Isaac Sim 操作](../isaacsim.md)。

使用 `scripts/isaac_client.mjs`，要求 Node.js 22+。
脚本只连接 `/Applications/Isaac Sim WebRTC Streaming Client.app`。
同名安装盘应用不能替代此路径。
脚本从远端视频帧生成截图，不截取 macOS 屏幕。
输入通过客户端内部接口发送，不改变 macOS 焦点或系统鼠标位置。
各调用共用客户端锁，临时调试接口为 `127.0.0.1:9229`。
脚本在结束时清理接口，并报告清理失败。

## 操作流程

1. 读取窗口状态：

   ```sh
   node scripts/isaac_client.mjs status
   ```

   客户端必须已运行。
   截图和输入要求窗口未隐藏、未最小化。
   客户端已聚焦时可以继续，不必要求用户切换应用。
   操作期间保持当前窗口焦点。

2. 截取远端视频：

   ```sh
   node scripts/isaac_client.mjs capture work/isaac-before
   ```

   输出目录必须已存在。
   查看生成的 `work/isaac-before.png`。
   保留配套的 `work/isaac-before.json`。
   JSON 含截图令牌（本次画面的标识），有效期为 180 秒。
   新截图会使旧令牌失效。

   坐标使用 PNG 原始像素，不扣标题栏或换算缩放。
   该图仅含远端视频，不含 macOS 窗口或客户端本地浮层。
   客户端本地浮层覆盖目标时，脚本拒绝点击。

3. 将动作数组写入 `work/isaac-actions.json`。
   每批最多 8 项。
   以下仅说明格式，坐标须按当前图填写：

   ```json
   [
     {"type": "click", "x": 120, "y": 80},
     {"type": "key", "key": "a", "modifiers": ["Control"]},
     {"type": "text", "text": "example"}
   ]
   ```

   `click` 只支持远端视频内未被遮挡的目标。
   视频须保持客户端默认的居中 `contain` 布局。
   `key` 发送一次按下和松开，不支持长按。
   `key` 支持单个英文字母和数字。
   其他按键为 `Enter`、`Tab`、`Escape`、`Backspace`、`Delete`、`ArrowLeft`、`ArrowRight`、`ArrowUp`、`ArrowDown`、`Home`、`End`、`Space`。
   组合键支持 `Control`、`Shift`、`Alt`、`Meta`。

   `text` 支持可打印 ASCII、制表符和换行。
   每批 `text` 总计最多 256 个字符。
   只合并目标及后续界面已知的动作。
   未知菜单或弹窗须单独观察后再操作。
   已展开的菜单可因悬停切换。
   避免再次点击标题将其收起。

   校准只用菜单或标签页，不用机器人动作按钮。

4. 使用截图令牌执行动作：

   ```sh
   node scripts/isaac_client.mjs act work/isaac-before.json work/isaac-actions.json work/isaac-after
   ```

   `act` 接受令牌后即耗用，不可再次使用。
   窗口几何变化或视频停滞时，脚本拒绝输入。
   操作期间焦点变化时，脚本中止余下动作。
   不要追加 `app.click`、系统按键或粘贴。
   不得改用 Quartz 或前台鼠标脚本发送输入。

5. 查看返回的 `work/isaac-after.png`。
   确认远端界面达到预期状态后，再报告完成。
   `ok: true` 表示输入已发送，仍须核对远端结果。
   下一批可使用结果图及配套 JSON，无需逐次点击截图。
   若结果图过期或界面变化，重新执行 `capture`。

## 异常处理

- CLI 不提供连接或重连命令。
  远端视频不可用时，停止自动输入并报告。
  恢复连接后重新执行 `capture`。
- 若客户端隐藏或最小化，停止输入并报告状态。
  不自动恢复、激活、抬升或重启窗口。
- 若令牌或窗口状态不符，重新读取状态并截图。
  先确认当前远端界面，再决定后续动作。
- 若调用超时或结果未知，不要重发整批动作。
  检查 `completed`、`outcomeUnknown` 和错误信息。
  `completed` 只记录已完成发送的动作序号，从 0 开始。
  无法确认结果或调试接口清理失败时，停止自动输入。
- 若返回 `CLEANUP_UNCONFIRMED`，输入或焦点的清理结果未确认。
  此状态会持续阻止后续调用，不会自动解除。
  停止操作并报告错误，不要自动重试或清除此状态。
