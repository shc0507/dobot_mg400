/* Furnace + microscope workspace (DSH-06). Described commands go through the console relay. */
(function (root, factory) {
  "use strict";
  const logic = factory();
  if (typeof module === "object" && module.exports) module.exports = logic;
  if (root && root.LabConsole) logic.install(root.LabConsole, root);
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const POLL_MS = 1000;
  const DESCRIBE_RETRY_MS = 10000;
  const HISTORY_S = 3600;
  const CHART = { width: 560, height: 160, left: 40, right: 550, top: 20, bottom: 120 };
  const AF_LENSES = [
    { field: "five_x_half_range_pulses", label: "5×" },
    { field: "ten_x_half_range_pulses", label: "10×" },
    { field: "twenty_x_half_range_pulses", label: "20×" },
    { field: "fifty_x_half_range_pulses", label: "50×" },
  ];
  const AF_PRESETS = [100, 300, 500, 1500, 3000, 5000, 10000];
  const SVG_NS = "http://www.w3.org/2000/svg";
  const DEFAULT_STEP = { temperature_c: 100, ramp_rate_c_per_min: 5, hold_minutes: 10 };
  const ACTIVE_RECORDING = new Set(["starting", "recording"]);

  const i18n = {
    en: {
      "furnace.title": "Furnace · AI-808",
      "furnace.caps": "Caps {max} °C · {ramp} °C/min",
      "control.other": "another client has control",
      "control.take": "Take control", "control.release": "Release",
      "control.takeLabel": "Take control of the {device}", "control.releaseLabel": "Release the {device}",
      "furnace.stop": "Stop heat and gas", "furnace.connect": "Connect furnace", "furnace.connectFlow": "Connect gas flow",
      "tile.pv": "PV", "tile.sv": "SV", "tile.mode": "Mode", "tile.crucible": "Crucible (thermal)",
      "mode.HOLD": "Hold", "mode.RUN": "Run", "mode.STOPPED": "Stopped", "mode.DISCONNECTED": "Disconnected", "mode.UNKNOWN": "Unknown", "mode.RAMP": "Ramping",
      "chart.label": "Temperature over the last 60 minutes: PV {pv}, SV {sv}", "chart.start": "−60 min", "chart.now": "now",
      "chart.empty": "No samples yet. The chart fills while this console is open.",
      "set.target": "Target °C", "set.ramp": "Ramp °C/min", "set.cap": "{label} ≤ {cap}", "set.apply": "Apply and run…",
      "set.confirm": "Heat the furnace to {target} °C at {ramp} °C/min?",
      "seq.title": "Sequence", "seq.add": "+ Add step", "seq.save": "Save sequence", "seq.start": "Start sequence…",
      "seq.step": "Step", "seq.target": "Target °C", "seq.ramp": "Ramp °C/min", "seq.hold": "Hold min",
      "seq.gas": "{gas} %", "seq.remove": "Remove step {n}", "seq.noChange": "No change",
      "seq.endGas": "End gas {gas} %", "seq.endUntil": "End gas until °C",
      "seq.confirm": "Start the {count}-step sequence? It heats the furnace.",
      "seq.state": "{phase}", "seq.stateRunning": "Step {n} · {phase}",
      "seq.holding": "Holding step {n}: {seconds} s left.", "seq.cooling": "End gas continues until the temperature is below {until} °C.",
      "seq.stepSlow": "Step {n} has not reached {target} °C after {minutes} min. The run continues.",
      "seq.endGasSlow": "The temperature is still above {until} °C after {hours} h of end gas. The run continues.",
      "seq.needEnd": "Enter an end-gas temperature when a step sets gas.", "seq.invalid": "Step {n}: enter a target, a ramp above 0 and a hold of 0 or more.",
      "seq.none": "Add at least 1 step.",
      "gas.title": "Gas", "gas.stopAll": "Stop all gas", "gas.flow": "Flow % for {gas}", "gas.send": "Send flow",
      "gas.actual": "actual {value}", "gas.unverified": "unverified",
      "gas.close": "Close", "gas.closeLabel": "Close {gas}", "gas.closeResult": "{gas} close: {outcome}",
      "valve.open": "valve open", "valve.closed": "valve closed", "valve.unknown": "valve unknown", "valve.unverified": "{state} (not confirmed)",
      "tile.ramp": "Ramp °C/min",
      "confirm.ok": "Confirm", "confirm.cancel": "Cancel",
      "scope.title": "Microscope",
      "scope.stop": "Stop", "scope.view": "Toupcam", "view.scope": "Microscope", "view.side": "Side camera", "view.switch": "Preview",
      "tile.offline": "OFFLINE",
      "obj.title": "Objective", "obj.name": "Objective {id}", "obj.named": "Objective {id}, {name}", "turret.title": "Turret",
      "obj.nudge": "Turn the turret {steps} full steps", "obj.offset": "Turret {offset} / ±{limit} full steps",
      "obj.offsetUnknown": "Turret offset unknown. Select another objective first.",
      "focus.title": "Focus", "focus.jog": "Jog pulses", "focus.down": "Jog focus −", "focus.up": "Jog focus +",
      "focus.local": "Autofocus",
      "scope.operate": "Operate", "scope.calibration": "Calibration", "scope.tabs": "Microscope sections",
      "scope.profileRequired": "Select a turret profile with a 5× starting point to use lens switching and focus.",
      "scope.profileLegacy": "Load this profile in Calibration, then record its 5× starting point.",
      "scope.rough5x": "Rough position 5×", "scope.sequence": "Focus 5× → 50×", "scope.sequenceHint": "Select any lens directly. The optional autofocus sequence runs 5× → 10× → 20× → 50×.",
      "focus.travel": "Manual / rough travel limit", "focus.travelUnit": "quarter-step pulses", "focus.travelPresets": "Preset", "focus.travelCustom": "Custom",
      "focus.travelApply": "Apply", "focus.travelSaved": "Max focus travel saved.", "focus.travelInvalid": "Enter a whole number from 1 to 2147483647 pulses.",
      "focus.travelHelp": "Total focus movement allowed for manual jogs and rough 5× positioning. Autofocus uses its own search budget. Neither setting is an absolute position limit.",
      "focus.travelCurrent": "Saved limit: {pulses} pulses", "focus.travelUnknown": "Saved limit unavailable",
      "scope.visual": "5× rough reference · last Y {current} px · target {target} px", "scope.phase": "{phase}",
      "af.title": "Autofocus status", "af.idle": "Not started", "af.waiting": "Waiting", "af.running": "Running", "af.verified": "Verified", "af.failed": "Failed", "af.stopped": "Stopped",
      "af.score": "Scan score {score}", "af.noScore": "No scan score", "af.phase": "{lens} · {phase}", "af.travel": "Total movement budget · {used} used / {limit} allowed pulses",
      "af.range": "Search half-range", "af.rangeLens": "Lens", "af.rangeSaved": "{lens} search range saved.", "af.rangeInvalid": "Enter a whole number from 4 to 1000000 pulses.",
      "af.rangeHelp": "{lens}: search ±{range} pulses around the starting point ({span} total span). Apply saves only this lens.",
      "af.rangeCurrent": "Saved ranges · quarter-step pulses", "af.rangeUnknown": "Search range unavailable", "af.rangeDirty": "Unsaved value for {lens}",
      "af.scanRange": "Scan half-range ±{range} pulses · {span} total span",
      "power.objective": "Turret", "power.focus": "Focus", "power.sleeping": "Asleep", "power.enabled": "Enabled", "power.disabled": "Released", "power.unknown": "Unknown",
      "focus.visualReady": "5× starting reference", "focus.visualIdle": "Camera Y is used only for rough 5× positioning.",
      "rec.title": "Recording", "rec.off": "off", "rec.snapshot": "Save snapshot", "rec.start": "Start recording", "rec.stop": "Stop recording",
      "rec.arm": "Record next furnace sequence", "rec.disarm": "Cancel sequence recording",
      "rec.stabOn": "Turn stabilization on", "rec.stabOff": "Turn stabilization off",
      "rec.state": "Recording: {status} · {frames} frames", "rec.armed": "Armed for the next furnace sequence.",
      "rec.path": "Snapshot path", "rec.pathInvalid": "Use a relative snapshot path without empty, . or .. parts.",
      "rec.name": "Run name", "rec.mode": "Mode", "rec.timeLapse": "Time lapse", "rec.live": "Live",
      "rec.interval": "Interval s", "rec.fps": "Output fps", "rec.error": "Recording error: {error}",
      "rec.invalid": "Enter a run name of at most 80 characters, an interval from 0.1 to 3600 s and an output rate from 0.1 to 120 fps.",
      "sent": "{action} sent.",
      "act.apply_setpoint": "Apply and run", "act.connect": "Connect furnace", "act.connect_flow": "Connect gas flow",
      "act.set_gas_flow": "Send flow", "act.set_sequence": "Save sequence", "act.start_sequence": "Start sequence",
      "act.autofocus": "Autofocus", "act.focus_move": "Focus jog", "act.objective_nudge": "Turret nudge",
      "act.record_with_sequence": "Record next furnace sequence", "act.select_objective": "Objective", "act.rough_focus_5x": "Rough position 5×", "act.autofocus_sequence": "Focus 5× → 50×",
      "act.set_stabilization": "Stabilization", "act.snapshot": "Save snapshot", "act.start_recording": "Start recording",
      "act.stop_recording": "Stop recording", "refused": "Refused: {error}", "unknown": "The outcome is unknown. Check the device before you send it again.",
      "stop.result": "{device} stop: {outcome}",
      "outcome.confirmed": "confirmed", "outcome.pending": "pending", "outcome.unconfirmed": "not confirmed", "outcome.unknown": "unknown",
      "status.stale": "Data is stale ({age} s).", "status.unavailable": "Status is unavailable.",
      "fault": "Fault: {message}. Make sure the controller shows STOP, then acknowledge.",
      "fault.ack": "Acknowledge fault…", "fault.confirm": "Is the furnace safe and the controller at STOP? Acknowledging clears the fault.",
      "scope.fault": "Fault: {message}. Only Stop is allowed.",
      "scope.failed": "{action} failed: {message}",
      "phase.idle": "Idle", "phase.ramping": "Ramping", "phase.holding": "Holding", "phase.cooling": "Cooling",
      "phase.stopped": "Stopped", "phase.fault": "Fault", "phase.applying": "Applying", "phase.complete": "Complete",
      "none": "—",
    },
    zh: {
      "furnace.title": "炉子 · AI-808",
      "furnace.caps": "上限 {max} °C · {ramp} °C/min",
      "control.other": "其他客户端有控制权",
      "control.take": "获取控制权", "control.release": "释放",
      "control.takeLabel": "获取{device}控制权", "control.releaseLabel": "释放{device}",
      "furnace.stop": "停止加热和气体", "furnace.connect": "连接炉子", "furnace.connectFlow": "连接气体流量计",
      "tile.pv": "PV", "tile.sv": "SV", "tile.mode": "模式", "tile.crucible": "坩埚（热像）",
      "mode.HOLD": "保持", "mode.RUN": "运行", "mode.STOPPED": "已停止", "mode.DISCONNECTED": "未连接", "mode.UNKNOWN": "未知", "mode.RAMP": "升温中",
      "chart.label": "最近 60 分钟温度：PV {pv}，SV {sv}", "chart.start": "−60 分钟", "chart.now": "现在",
      "chart.empty": "暂无数据。控制台打开时记录曲线。",
      "set.target": "目标 °C", "set.ramp": "速率 °C/min", "set.cap": "{label} ≤ {cap}", "set.apply": "应用并运行…",
      "set.confirm": "以 {ramp} °C/min 把炉子加热到 {target} °C？",
      "seq.title": "程序", "seq.add": "+ 添加步骤", "seq.save": "保存程序", "seq.start": "启动程序…",
      "seq.step": "步骤", "seq.target": "目标 °C", "seq.ramp": "速率 °C/min", "seq.hold": "保持 min",
      "seq.gas": "{gas} %", "seq.remove": "删除步骤 {n}", "seq.noChange": "不变",
      "seq.endGas": "结束气体 {gas} %", "seq.endUntil": "结束气体直到 °C",
      "seq.confirm": "启动 {count} 步程序？炉子会加热。",
      "seq.state": "{phase}", "seq.stateRunning": "步骤 {n} · {phase}",
      "seq.holding": "步骤 {n} 保持中：剩余 {seconds} s。", "seq.cooling": "结束气体持续到温度低于 {until} °C。",
      "seq.stepSlow": "步骤 {n} 在 {minutes} min 后仍未到达 {target} °C。程序继续运行。",
      "seq.endGasSlow": "结束气体 {hours} h 后温度仍高于 {until} °C。程序继续运行。",
      "seq.needEnd": "步骤设置了气体时，填写结束气体温度。", "seq.invalid": "步骤 {n}：填写目标温度、大于 0 的速率和不小于 0 的保持时间。",
      "seq.none": "至少添加 1 个步骤。",
      "gas.title": "气体", "gas.stopAll": "停止全部气体", "gas.flow": "{gas} 流量 %", "gas.send": "发送流量",
      "gas.actual": "实际 {value}", "gas.unverified": "未确认",
      "gas.close": "关闭", "gas.closeLabel": "关闭 {gas}", "gas.closeResult": "{gas} 关闭：{outcome}",
      "valve.open": "阀门开", "valve.closed": "阀门关", "valve.unknown": "阀门未知", "valve.unverified": "{state}（未确认）",
      "tile.ramp": "速率 °C/min",
      "confirm.ok": "确认", "confirm.cancel": "取消",
      "scope.title": "显微镜",
      "scope.stop": "停止", "scope.view": "Toupcam", "view.scope": "显微镜", "view.side": "侧面相机", "view.switch": "预览",
      "tile.offline": "离线",
      "obj.title": "物镜", "obj.name": "物镜 {id}", "obj.named": "物镜 {id}，{name}", "turret.title": "转盘",
      "obj.nudge": "转盘转动 {steps} 整步", "obj.offset": "转盘 {offset} / ±{limit} 整步",
      "obj.offsetUnknown": "转盘偏移未知。先选择其他物镜。",
      "focus.title": "对焦", "focus.jog": "点动脉冲", "focus.down": "对焦点动 −", "focus.up": "对焦点动 +",
      "focus.local": "自动对焦",
      "scope.operate": "操作", "scope.calibration": "校准", "scope.tabs": "显微镜分区",
      "scope.profileRequired": "选择包含 5× 起点的转盘配置后，才能切换物镜和对焦。",
      "scope.profileLegacy": "在校准页加载此配置，再记录 5× 起点。",
      "scope.rough5x": "5× 粗定位", "scope.sequence": "对焦 5× → 50×", "scope.sequenceHint": "可以直接选择任意物镜。也可以按 5× → 10× → 20× → 50× 运行自动对焦序列。",
      "focus.travel": "手动 / 粗定位行程上限", "focus.travelUnit": "四分之一步脉冲", "focus.travelPresets": "预设", "focus.travelCustom": "自定义",
      "focus.travelApply": "应用", "focus.travelSaved": "已保存最大对焦行程。", "focus.travelInvalid": "请输入 1 到 2147483647 之间的整数脉冲。",
      "focus.travelHelp": "手动点动和 5× 粗定位允许的对焦总移动量。自动对焦使用独立搜索预算。两种设置都不是绝对位置上限。",
      "focus.travelCurrent": "已存上限：{pulses} 脉冲", "focus.travelUnknown": "已存上限不可用",
      "scope.visual": "5× 粗定位参考 · 最后 Y {current} px · 目标 {target} px", "scope.phase": "{phase}",
      "af.title": "自动对焦状态", "af.idle": "尚未开始", "af.waiting": "等待", "af.running": "运行中", "af.verified": "已验证", "af.failed": "失败", "af.stopped": "已停止",
      "af.score": "扫描评分 {score}", "af.noScore": "暂无扫描评分", "af.phase": "{lens} · {phase}", "af.travel": "累计移动预算 · 已用 {used} / 允许 {limit} 脉冲",
      "af.range": "搜索半范围", "af.rangeLens": "物镜", "af.rangeSaved": "已保存 {lens} 搜索范围。", "af.rangeInvalid": "请输入 4 到 1000000 之间的整数脉冲。",
      "af.rangeHelp": "{lens}：在起点两侧搜索 ±{range} 脉冲（总跨度 {span}）。应用只保存当前物镜。",
      "af.rangeCurrent": "已存范围 · 四分之一步脉冲", "af.rangeUnknown": "搜索范围不可用", "af.rangeDirty": "{lens} 有未保存的值",
      "af.scanRange": "扫描半范围 ±{range} 脉冲 · 总跨度 {span}",
      "power.objective": "转盘", "power.focus": "对焦", "power.sleeping": "休眠", "power.enabled": "已启用", "power.disabled": "已释放", "power.unknown": "未知",
      "focus.visualReady": "5× 起点参考", "focus.visualIdle": "相机 Y 仅用于 5× 粗定位。",
      "rec.title": "录制", "rec.off": "关", "rec.snapshot": "保存快照", "rec.start": "开始录制", "rec.stop": "停止录制",
      "rec.arm": "录制下一次炉子程序", "rec.disarm": "取消程序录制",
      "rec.stabOn": "开启防抖", "rec.stabOff": "关闭防抖",
      "rec.state": "录制：{status} · {frames} 帧", "rec.armed": "已准备录制下一次炉子程序。",
      "rec.path": "快照路径", "rec.pathInvalid": "快照路径必须是相对路径，不能有空段、. 或 ..。",
      "rec.name": "运行名称", "rec.mode": "模式", "rec.timeLapse": "延时", "rec.live": "实时",
      "rec.interval": "间隔 s", "rec.fps": "输出帧率", "rec.error": "录制错误：{error}",
      "rec.invalid": "运行名称最多 80 个字符，间隔为 0.1 到 3600 s，输出帧率为 0.1 到 120 fps。",
      "sent": "已发送 {action}。",
      "act.apply_setpoint": "应用并运行", "act.connect": "连接炉子", "act.connect_flow": "连接气体流量计",
      "act.set_gas_flow": "发送流量", "act.set_sequence": "保存程序", "act.start_sequence": "启动程序",
      "act.autofocus": "自动对焦", "act.focus_move": "对焦点动", "act.objective_nudge": "转盘微调",
      "act.record_with_sequence": "录制下一次炉子程序", "act.select_objective": "物镜", "act.rough_focus_5x": "5× 粗定位", "act.autofocus_sequence": "对焦 5× → 50×",
      "act.set_stabilization": "防抖", "act.snapshot": "保存快照", "act.start_recording": "开始录制",
      "act.stop_recording": "停止录制", "refused": "已拒绝：{error}", "unknown": "结果未知。再次发送前先检查设备。",
      "stop.result": "{device}停止：{outcome}",
      "outcome.confirmed": "已确认", "outcome.pending": "等待中", "outcome.unconfirmed": "未确认", "outcome.unknown": "未知",
      "status.stale": "数据已过期（{age} s）。", "status.unavailable": "状态不可用。",
      "fault": "故障：{message}。确认控制器显示 STOP 后再确认故障。",
      "fault.ack": "确认故障…", "fault.confirm": "炉子安全且控制器处于 STOP 吗？确认后将清除故障。",
      "scope.fault": "故障：{message}。只允许停止。",
      "scope.failed": "{action} 失败：{message}",
      "phase.idle": "空闲", "phase.ramping": "升温中", "phase.holding": "保持中", "phase.cooling": "冷却中",
      "phase.stopped": "已停止", "phase.fault": "故障", "phase.applying": "应用中", "phase.complete": "已完成",
      "none": "—",
    },
  };

  // Pure logic ---------------------------------------------------------------

  function degrees(value) { return finite(value) === null ? "—" : fmt(value) + " °C"; }

  function finite(value) { return typeof value === "number" && Number.isFinite(value) ? value : null; }

  function fmt(value, digits = 1) {
    const number = finite(value);
    return number === null ? "—" : number.toFixed(digits);
  }

  /** Scale the history to SVG points. Rows are [unix_s, pv, sv]. */
  function chartModel(history, now) {
    const rows = (Array.isArray(history) ? history : []).filter(row => Array.isArray(row) && finite(row[0]) !== null && now - row[0] <= HISTORY_S);
    const values = rows.flatMap(row => [finite(row[1]), finite(row[2])]).filter(value => value !== null);
    const top = Math.max(600, Math.ceil(Math.max(0, ...values) / 300) * 300);
    const x = stamp => CHART.left + (CHART.right - CHART.left) * (1 - Math.min(HISTORY_S, Math.max(0, now - stamp)) / HISTORY_S);
    const y = value => CHART.bottom - (CHART.bottom - CHART.top) * Math.max(0, value) / top;
    const line = index => rows.filter(row => finite(row[index]) !== null).map(row => `${x(row[0]).toFixed(1)},${y(row[index]).toFixed(1)}`).join(" ");
    return { top, pv: line(1), sv: line(2), count: rows.length };
  }

  /** Build the set_sequence parameters from editor rows; throw {key, vars} on invalid input. */
  function sequenceParams(rows, endGas, endUntil) {
    if (!rows.length) throw { key: "seq.none" };
    const steps = rows.map((row, index) => {
      const step = {
        temperature_c: Number(row.temperature_c),
        ramp_rate_c_per_min: Number(row.ramp_rate_c_per_min),
        hold_minutes: Number(row.hold_minutes),
      };
      if (String(row.temperature_c).trim() === "" || !Number.isFinite(step.temperature_c) || step.temperature_c < 0
        || !Number.isFinite(step.ramp_rate_c_per_min) || step.ramp_rate_c_per_min <= 0
        || String(row.hold_minutes).trim() === "" || !Number.isFinite(step.hold_minutes) || step.hold_minutes < 0) {
        throw { key: "seq.invalid", vars: { n: index + 1 } };
      }
      const gas = {};
      for (const [channel, value] of Object.entries(row.gas || {})) {
        if (String(value).trim() === "") continue;
        const number = Number(value);
        if (!Number.isFinite(number) || number < 0 || number > 100) throw { key: "seq.invalid", vars: { n: index + 1 } };
        gas[channel] = number;
      }
      if (Object.keys(gas).length) step.gas = gas;
      return step;
    });
    const params = { steps };
    const until = String(endUntil ?? "").trim();
    if (steps.some(step => step.gas) && until === "") throw { key: "seq.needEnd" };
    if (until !== "") {
      params.end_gas_until_c = Number(until);
      params.end_gas = {};
      for (const [channel, value] of Object.entries(endGas || {})) params.end_gas[channel] = Number(value) || 0;
    }
    return params;
  }

  function editorRows(sequence) {
    const steps = Array.isArray(sequence?.steps) && sequence.steps.length ? sequence.steps : [DEFAULT_STEP];
    return steps.map(step => ({
      temperature_c: String(step.temperature_c ?? ""),
      ramp_rate_c_per_min: String(step.ramp_rate_c_per_min ?? ""),
      hold_minutes: String(step.hold_minutes ?? ""),
      gas: Object.fromEntries([0, 1, 2].map(channel => [String(channel), step.gas?.[channel] ?? step.gas?.[String(channel)] ?? ""]).map(([k, v]) => [k, String(v)])),
    }));
  }

  /** The editor form of a furnace sequence, to see when another client changed it. */
  function sequenceKey(sequence) {
    return JSON.stringify([editorRows(sequence), sequence?.end_gas_until_c ?? null, sequence?.end_gas ?? null]);
  }

  function modeKey(furnace) {
    const mode = furnace?.control_mode || "DISCONNECTED";
    if (mode === "RUN" && furnace?.waiting_for_program_step_2) return "mode.RAMP";
    return i18n.en["mode." + mode] ? "mode." + mode : "mode.UNKNOWN";
  }

  /** The text of a sequence warning from the furnace service, or "". */
  function seqWarning(warning) {
    if (!warning || typeof warning !== "object") return "";
    const t = view.api.t;
    if (warning.code === "step_slow") {
      return t("seq.stepSlow", { n: (warning.step_index ?? 0) + 1, target: fmt(warning.target_c, 0), minutes: warning.minutes });
    }
    if (warning.code === "end_gas_slow") return t("seq.endGasSlow", { until: fmt(warning.end_gas_until_c, 0), hours: warning.hours });
    return typeof warning.message === "string" ? warning.message : "";
  }

  function recordingId(cryptoObject) {
    const bytes = cryptoObject.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  }

  function objectiveIds(description) {
    const ids = description?.commands?.select_objective?.params?.properties?.id?.enum;
    return Array.isArray(ids) && ids.every(Number.isSafeInteger) ? ids.slice(0, 8) : [0, 1, 2, 3];
  }

  /** The described objective name for an ID, such as "10x". Null when the service gives none. */
  function objectiveName(description, id) {
    const name = description?.commands?.select_objective?.params?.properties?.id?.names?.[String(id)];
    return typeof name === "string" && name.trim() ? name.trim().slice(0, 12) : null;
  }

  /** The described turret nudges in full steps, ascending. None when the service has no objective_nudge. */
  function nudgeSteps(description) {
    const steps = description?.commands?.objective_nudge?.params?.properties?.steps?.enum;
    return Array.isArray(steps) && steps.every(Number.isSafeInteger) ? steps.slice(0, 8).sort((a, b) => a - b) : [];
  }

  function signed(value) { return value > 0 ? "+" + value : value < 0 ? "−" + Math.abs(value) : "0"; }

  /** The snapshot params: {} for the service default, {path} for a relative path, or null. */
  function snapshotParams(text) {
    const value = String(text ?? "").trim();
    if (!value) return {};
    if (value.length > 4096 || value.startsWith("/") || value.includes("\\") || /[\x00-\x1f\x7f]/.test(value)
      || value.split("/").some(part => !part || part === "." || part === "..")) return null;
    return { path: value };
  }

  /** The start_recording options from the page fields; throw {key} on invalid input. */
  function recordingParams(fields) {
    const params = { mode: fields?.mode === "live" ? "live" : "time_lapse" };
    const name = String(fields?.name ?? "").trim();
    if (name.length > 80 || /[\x00-\x1f\x7f]/.test(name)) throw { key: "rec.invalid" };
    if (name) params.run_name = name;
    const number = (text, max) => {
      const value = String(text ?? "").trim();
      const result = Number(value);
      return /^(?:\d+(?:\.\d*)?|\.\d+)$/.test(value) && result >= 0.1 && result <= max ? result : null;
    };
    params.output_fps = number(fields?.fps, 120);
    if (params.output_fps === null) throw { key: "rec.invalid" };
    if (params.mode === "time_lapse") {
      params.capture_interval_seconds = number(fields?.interval, 3600);
      if (params.capture_interval_seconds === null) throw { key: "rec.invalid" };
    }
    return params;
  }

  /** The valve state text key and its vars for 1 gas channel. */
  function valveState(valve) {
    if (valve?.open !== true && valve?.open !== false) return { key: "valve.unknown" };
    const key = valve.open ? "valve.open" : "valve.closed";
    return valve.verified === true ? { key } : { key: "valve.unverified", state: key };
  }

  function caps(description) {
    const props = description?.commands?.apply_setpoint?.params?.properties || {};
    return { max: finite(props.temperature_c?.maximum), ramp: finite(props.ramp_rate_c_per_min?.maximum) };
  }

  function holder(api, device) {
    const entry = api.controls().find(item => item.device === device && item.state === "held");
    if (api.owns(device)) return "mine";
    const card = (api.state()?.services || []).find(item => item.id === device);
    return entry || card?.lease_holder ? "other" : "free";
  }

  // Workspace ----------------------------------------------------------------

  function install(LabConsole, root) {
    LabConsole.register({ id: "process", i18n, mount: (node, api) => mount(node, api, root), show, hide });
  }

  const view = { api: null, root: null, global: null, timer: null, status: null, describe: {}, describeAt: {}, rows: null, dirty: false,
    endGas: { 0: "0", 1: "0", 2: "0" }, endUntil: "", target: "200", ramp: "10", jog: "100", flows: { 0: "0", 1: "0", 2: "0" },
    confirm: null, busy: new Set(), tiles: {}, nodes: {}, open: {}, scopeView: "microscope", scopeTab: "operate", turretCalibration: null,
    focusTravel: "20000", focusTravelDirty: false, focusTravelSaved: null, focusTravelRead: false,
    afLens: 0, afDrafts: {}, afDirty: new Set(), afSettings: null, afSettingsRead: false, afSettingsEpoch: 0, afSettingsPending: false, afPollSequence: 0, afAcceptedSequence: 0,
    snapPath: "", rec: { name: "", mode: "time_lapse", interval: "30", fps: "1" } };

  function show() {
    if (!view.api || view.timer) return;
    view.turretCalibration?.show();
    view.turretCalibration?.setCalibrationVisible?.(view.scopeTab === "calibration");
    poll();
    view.timer = setInterval(poll, POLL_MS);
  }

  function hide() {
    view.turretCalibration?.hide();
    if (view.timer) clearInterval(view.timer);
    view.timer = null;
  }

  async function poll() {
    // Keep status and settings reads in one poll so older replies cannot replace them.
    if (view.polling) return;
    view.polling = true;
    try {
      const afEpoch = view.afSettingsEpoch, afPollSequence = ++view.afPollSequence;
      const currentSettingsRead = () => afEpoch === view.afSettingsEpoch && afPollSequence >= view.afAcceptedSequence;
      const acceptSettings = settings => {
        if (currentSettingsRead()) { view.afSettings = settings; view.afAcceptedSequence = afPollSequence; }
      };
      try { view.status = await view.api.request("status"); }
      catch { view.status = null; }
      const travel = view.status?.microscope?.focus_travel_limit_pulses;
      if (Number.isSafeInteger(travel) && travel > 0) {
        view.focusTravelSaved = travel;
        if (!view.focusTravelDirty) view.focusTravel = String(travel);
      } else if (!view.focusTravelRead) {
        view.focusTravelRead = true;
        try {
          const result = await view.api.request("microscope/focus-travel-limit");
          if (Number.isSafeInteger(result?.focus_travel_limit_pulses)) {
            view.focusTravelSaved = result.focus_travel_limit_pulses;
            if (!view.focusTravelDirty) view.focusTravel = String(view.focusTravelSaved);
          }
        } catch { view.focusTravelRead = false; }
      }
      if (currentSettingsRead()) {
        const settings = view.status?.microscope?.autofocus_settings;
        const matchesSaved = AF_LENSES.every(({ field }) => settings?.[field] === view.afSettings?.[field]);
        if (view.afSettingsPending && !matchesSaved) {
          // A Console cache can lag a successful save. Read the settings endpoint until it catches up.
          try {
            const fresh = await view.api.request("microscope/autofocus-settings");
            acceptSettings(fresh);
          } catch { /* Keep the last confirmed settings while the direct read is unavailable. */ }
        } else if (Number.isSafeInteger(settings?.five_x_half_range_pulses)) {
          acceptSettings(settings); view.afSettingsPending = false;
        } else if (!view.afSettingsRead) {
          view.afSettingsRead = true;
          try {
            const fresh = await view.api.request("microscope/autofocus-settings");
            acceptSettings(fresh);
          } catch { view.afSettingsRead = false; }
        }
      }
      syncAutofocusDrafts();
    } finally { view.polling = false; }
    // Read each description once; retry a missing one at most every DESCRIBE_RETRY_MS.
    const at = Date.now();
    for (const device of ["furnace", "microscope"]) {
      if (view.describe[device] || at - (view.describeAt[device] || -Infinity) < DESCRIBE_RETRY_MS) continue;
      view.describeAt[device] = at;
      view.api.describe(device).then(value => { view.describe[device] = value; build(); }).catch(() => {});
    }
    const names = gasNames().join("|");
    if (view.gasKey !== names) { view.gasKey = names; build(); }
    // An unedited editor follows the furnace sequence, also when another client replaces it.
    const furnaceSeq = view.status?.furnace?.sequence;
    if (!view.rows || (!view.dirty && furnaceSeq && sequenceKey(furnaceSeq) !== view.loaded)) {
      view.rows = editorRows(furnaceSeq);
      if (furnaceSeq) {
        view.loaded = sequenceKey(furnaceSeq);
        const seq = furnaceSeq;
        if (seq.end_gas_until_c !== null && seq.end_gas_until_c !== undefined) view.endUntil = String(seq.end_gas_until_c);
        for (const channel of [0, 1, 2]) view.endGas[channel] = String(seq.end_gas?.[channel] ?? seq.end_gas?.[String(channel)] ?? 0);
      }
      buildSequence();
    }
    update();
  }

  function syncAutofocusDrafts() {
    for (const { field } of AF_LENSES) {
      const value = view.afSettings?.[field];
      if (!view.afDirty.has(field) && Number.isSafeInteger(value)) view.afDrafts[field] = String(value);
    }
  }

  function mount(node, api, global) {
    view.root = node;
    view.api = api;
    view.global = global || {};
    view.turretCalibration = view.global.LabTurret?.create(api, view.global) || null;
    view.rows = editorRows(null);
    api.onLanguage(() => build());
    api.onState(() => update());
    build();
    show();
  }

  // Building ---------------------------------------------------------------

  function input(props, value, onChange) {
    const node = view.api.el("input", { ...props, oninput: event => onChange(event.target.value) });
    node.value = value;
    return node;
  }

  function button(text, onclick, props = {}) {
    return view.api.el("button", { type: "button", class: "lc-button", text, onclick, ...props });
  }

  // The button that opens each confirmation. Cancel, Esc and Confirm give the focus back to it.
  const OPENERS = { apply: "apply", start: "start-sequence", ack: "acknowledge" };

  function confirmBox(id, text, onConfirm, extra) {
    const { el, t } = view.api;
    if (view.confirm !== id) return el("div", { hidden: true });
    return el("div", { class: "lc-confirm", role: "alertdialog", "aria-label": text, tabindex: "-1", "data-role": "dialog-" + id,
      onkeydown: event => { if (event.key === "Escape") { event.preventDefault?.(); closeConfirm(); } } },
      el("p", { text }),
      el("div", { class: "pr-row" },
        extra || button(t("confirm.ok"), () => { closeConfirm(); onConfirm(); }, { class: "lc-danger pr-danger", "data-role": "confirm-" + id }),
        button(t("confirm.cancel"), closeConfirm, { "data-role": "cancel-" + id })));
  }

  function ask(id) {
    view.confirm = id;
    build();
    findRole(view.root, "dialog-" + id)?.focus?.();
  }

  function closeConfirm() {
    const opener = OPENERS[view.confirm];
    view.confirm = null;
    build();
    if (opener) findRole(view.root, opener)?.focus?.();
  }

  function cameraTile(name, label) {
    if (view.tiles[name]) return view.tiles[name];
    const { el, t } = view.api;
    let tile = null;
    try { tile = view.global.LabCameras?.tile?.(name, { api: view.api, large: name === "microscope" }) || null; } catch { tile = null; }
    if (!tile) {
      tile = el("div", { class: "pr-view pr-offline", role: "img", "aria-label": label + " · " + t("tile.offline") },
        el("span", { class: "pr-badge", text: "● " + t("tile.offline") }),
        el("span", { class: "pr-view-name", text: label }));
    }
    view.tiles[name] = tile;
    return tile;
  }

  /** A closed group for rarely used controls. It keeps its open state across rebuilds. */
  function disclosure(id, title, state, ...body) {
    const { el } = view.api;
    const node = el("details", { class: "pr-details", "data-role": "group-" + id },
      el("summary", { class: "pr-summary" }, el("span", { class: "pr-summary-title", text: title }), state),
      el("div", { class: "pr-details-body" }, ...body));
    node.open = view.open[id] === true;
    node.addEventListener("toggle", () => { view.open[id] = node.open === true; });
    return node;
  }

  /** Show 1 preview view. The hidden view stops its stream. */
  function showScopeTab(tab) {
    view.scopeTab = tab === "calibration" ? "calibration" : "operate";
    const calibration = view.scopeTab === "calibration", n = view.nodes;
    if (n.operatePanel) n.operatePanel.hidden = calibration;
    if (n.calibrationPanel) n.calibrationPanel.hidden = !calibration;
    n.tabButtons?.forEach((button, index) => {
      const selected = (index === 1) === calibration;
      button.setAttribute("aria-selected", String(selected));
      button.setAttribute("tabindex", "0");
    });
    view.turretCalibration?.setCalibrationVisible?.(calibration);
    if (calibration) Object.values(view.tiles).forEach(tile => tile?.labCameras?.stop?.());
    else showView(view.scopeView);
  }

  function showView(name) {
    view.scopeView = name === "camera3" ? "camera3" : "microscope";
    const n = view.nodes;
    for (const pane of n.viewPanes || []) pane.hidden = pane.getAttribute("data-view") !== view.scopeView;
    for (const node of n.viewButtons || []) node.setAttribute("aria-pressed", node.getAttribute("data-role") === "view-" + view.scopeView ? "true" : "false");
  }

  function capped(label, cap) {
    return cap === null || cap === undefined ? label : view.api.t("set.cap", { label, cap: fmt(cap, 0) });
  }

  function controlButtons(device) {
    const { t, deviceName } = view.api;
    const name = deviceName(device);
    return [
      button(t("control.take"), () => act("take-" + device, async () => { await view.api.take(device); delete view.describeAt[device]; }), { "data-role": "take-" + device, "aria-label": t("control.takeLabel", { device: name }) }),
      button(t("control.release"), () => act("release-" + device, () => view.api.release(device)), { "data-role": "release-" + device, "aria-label": t("control.releaseLabel", { device: name }) }),
    ];
  }

  function build() {
    if (!view.api) return;
    const { el, t } = view.api;
    const n = view.nodes = {};
    const furnaceCaps = caps(view.describe.furnace);

    // Furnace.
    n.furnaceSub = el("span", { class: "lc-muted" });
    n.furnaceNote = el("p", { class: "pr-note", hidden: true });
    n.tiles = {};
    const tiles = ["pv", "sv", "crucible"].map(key => {
      n.tiles[key] = el("span", { class: "pr-tile-value" });
      return el("div", { class: "pr-tile" }, el("span", { class: "pr-tile-label", text: t("tile." + key) }), n.tiles[key]);
    });
    n.tiles.mode = el("span", { class: "pr-tile-value" });
    n.tiles.ramp = el("span", { class: "pr-tile-value" });
    n.chart = chartNode();
    n.connect = button(t("furnace.connect"), () => send("furnace", "connect", {}), { "data-role": "connect" });
    n.connectFlow = button(t("furnace.connectFlow"), () => send("furnace", "connect_flow", {}), { "data-role": "connect-flow" });
    n.apply = button(t("set.apply"), () => { view.applyValues = { target: view.target, ramp: view.ramp }; ask("apply"); }, { class: "lc-button pr-primary", "data-role": "apply" });
    const target = input({ type: "number", min: "0", step: "1", max: furnaceCaps.max ?? undefined, class: "pr-input", "data-role": "target" }, view.target, value => { view.target = value; });
    const ramp = input({ type: "number", min: "0.1", step: "0.1", max: furnaceCaps.ramp ?? undefined, class: "pr-input", "data-role": "ramp" }, view.ramp, value => { view.ramp = value; });
    const setpoint = el("div", { class: "pr-row pr-end" },
      // The caps show at the fields that they limit.
      el("label", { class: "pr-field" }, capped(t("set.target"), furnaceCaps.max), target),
      el("label", { class: "pr-field" }, capped(t("set.ramp"), furnaceCaps.ramp), ramp),
      n.apply);
    // Confirm sends the values that the confirmation shows, not values typed after it opened.
    const shown = view.applyValues || { target: view.target, ramp: view.ramp };
    const applyConfirm = confirmBox("apply", t("set.confirm", shown), () => applySetpoint(shown));

    n.seqState = el("span", { class: "pr-summary-state", "data-role": "seq-state" });
    n.seqProgress = el("p", { class: "lc-muted" });
    n.seqWarning = el("p", { class: "pr-warn", "data-role": "seq-warning", hidden: true });
    n.add = button(t("seq.add"), () => { view.rows.push(editorRows(null)[0]); view.dirty = true; buildSequence(); update(); }, { "data-role": "add-step" });
    n.save = button(t("seq.save"), () => sendSequence("set_sequence"), { "data-role": "save-sequence" });
    n.start = button(t("seq.start"), () => ask("start"), { class: "lc-button pr-strong", "data-role": "start-sequence" });
    n.sequenceBody = el("tbody");
    n.sequenceTable = el("div", { class: "pr-table-wrap" }, el("table", { class: "pr-table" },
      el("thead", {}, el("tr", {}, ...[t("seq.step"), t("seq.target"), t("seq.ramp"), t("seq.hold"), ...gasNames().map(gas => t("seq.gas", { gas })), ""].map(text => el("th", { scope: "col", text })))),
      n.sequenceBody));
    const endGas = el("div", { class: "pr-row pr-end" },
      ...[0, 1, 2].map(channel => el("label", { class: "pr-field" }, t("seq.endGas", { gas: gasNames()[channel] }),
        input({ type: "number", min: "0", max: "100", step: "0.1", class: "pr-input pr-narrow" }, view.endGas[channel], value => { view.endGas[channel] = value; view.dirty = true; }))),
      el("label", { class: "pr-field" }, t("seq.endUntil"),
        input({ type: "number", min: "0", step: "1", class: "pr-input pr-narrow", "data-role": "end-until" }, view.endUntil, value => { view.endUntil = value; view.dirty = true; })));
    const startConfirm = confirmBox("start", t("seq.confirm", { count: view.rows?.length || 0 }), () => sendSequence("start_sequence"));

    n.gasRows = [0, 1, 2].map(channel => {
      const row = { name: el("span", { class: "pr-gas-name" }), state: el("span", { class: "pr-gas-state" }) };
      row.send = button(t("gas.send"), () => sendFlow(channel), { "data-role": "send-flow-" + channel });
      row.input = input({ type: "number", min: "0", max: "100", step: "0.1", class: "pr-input pr-narrow", "aria-label": t("gas.flow", { gas: gasNames()[channel] }) }, view.flows[channel], value => { view.flows[channel] = value; });
      row.close = button(t("gas.close"), () => closeGas(channel), { class: "lc-danger pr-danger", "data-role": "close-gas-" + channel, "aria-label": t("gas.closeLabel", { gas: gasNames()[channel] }) });
      row.node = el("div", { class: "pr-gas" }, el("div", { class: "pr-gas-text" }, row.name, row.state), row.input, row.send, row.close);
      return row;
    });

    const furnace = el("section", { class: "lc-card pr-card pr-furnace", "aria-labelledby": "pr-furnace-title" },
      el("div", { class: "lc-card-head" },
        el("div", { class: "pr-head" }, el("h2", { id: "pr-furnace-title", text: t("furnace.title") }), n.furnaceSub),
        el("div", { class: "pr-row" }, ...controlButtons("furnace"))),
      el("button", { type: "button", class: "lc-stop pr-stop-wide", "data-role": "stop-furnace", text: t("furnace.stop"), onclick: stopFurnace }),
      n.furnaceNote,
      n.seqWarning,
      n.ack = button(t("fault.ack"), () => ask("ack"), { class: "lc-danger pr-danger", "data-role": "acknowledge", hidden: true }),
      confirmBox("ack", t("fault.confirm"), () => send("furnace", "acknowledge", {})),
      n.connectRow = el("div", { class: "pr-row" }, n.connect),
      el("div", { class: "pr-tiles" }, ...tiles),
      el("p", { class: "pr-state" },
        n.tiles.mode,
        n.rampItem = el("span", { class: "pr-state-item" }, el("span", { class: "pr-state-label", text: t("tile.ramp") }), n.tiles.ramp)),
      n.chart.figure,
      setpoint, applyConfirm,
      el("div", { class: "pr-block pr-gas-block" },
        el("div", { class: "lc-card-head" }, el("h3", { text: t("gas.title") }),
          el("div", { class: "pr-row" }, n.connectFlow,
            button(t("gas.stopAll"), stopGas, { class: "lc-danger pr-danger", "data-role": "stop-gas" }))),
        el("div", { class: "pr-gas-list" }, ...n.gasRows.map(row => row.node))),
      disclosure("sequence", t("seq.title"), n.seqState,
        el("div", { class: "pr-row" }, n.add, n.save, n.start),
        n.sequenceTable, endGas, n.seqProgress, startConfirm));

    // Microscope.
    n.scopeSub = el("span", { class: "lc-muted" });
    n.scopeNote = el("p", { class: "pr-note", "data-role": "scope-note", hidden: true });
    n.objectives = objectiveIds(view.describe.microscope).map(id => {
      const name = objectiveName(view.describe.microscope, id);
      const node = el("button", { type: "button", class: "lc-button pr-objective", role: "radio", "aria-checked": "false", "aria-label": name ? t("obj.named", { id, name }) : t("obj.name", { id }), "data-role": "objective-" + id, text: name ?? String(id), onclick: () => send("microscope", "select_objective", { id }) });
      node.dataset.id = String(id);
      return node;
    });
    n.nudges = nudgeSteps(view.describe.microscope).map(steps => {
      const node = button(signed(steps), () => send("microscope", "objective_nudge", { steps }, "nudge"),
        { class: "lc-button pr-mono-button", "data-role": "nudge-" + (steps > 0 ? "up-" : "down-") + Math.abs(steps), "aria-label": t("obj.nudge", { steps: signed(steps) }) });
      node.dataset.steps = String(steps);
      return node;
    });
    n.offset = el("span", { class: "pr-mono", "data-role": "nudge-offset" });
    n.nudgeRow = el("div", { class: "pr-row", "data-role": "nudge-row", hidden: true }, ...n.nudges, n.offset);
    n.focusTravel = input({ type: "number", min: "1", max: "2147483647", step: "1", class: "pr-input pr-travel-input", "data-role": "focus-travel" }, view.focusTravel, value => { view.focusTravel = value; view.focusTravelDirty = true; update(); });
    n.focusTravelPreset = el("select", { class: "pr-input", "aria-label": t("focus.travelPresets"), "data-role": "focus-travel-preset", onchange: event => {
      if (!event.target.value) return;
      view.focusTravel = event.target.value; view.focusTravelDirty = true; update();
    } }, el("option", { value: "", text: t("focus.travelCustom") }), ...[200, 500, 1000, 2000, 20000].map(value => el("option", { value: String(value), text: String(value) })));
    n.focusTravelApply = button(t("focus.travelApply"), applyFocusTravel, { "data-role": "focus-travel-apply" });
    n.focusTravelCurrent = el("span", { class: "pr-profile-note", "data-role": "focus-travel-current" });
    n.afLens = el("select", { class: "pr-input", "data-role": "af-range-lens", onchange: event => {
      const index = Number(event.target.value);
      if (Number.isInteger(index) && AF_LENSES[index]) { view.afLens = index; update(); }
    } }, ...AF_LENSES.map((lens, index) => el("option", { value: String(index), text: lens.label })));
    n.afRange = input({ type: "number", min: "4", max: "1000000", step: "1", class: "pr-input pr-travel-input", "data-role": "af-range" }, view.afDrafts[AF_LENSES[view.afLens].field] || "", value => {
      const field = AF_LENSES[view.afLens].field; view.afDrafts[field] = value; view.afDirty.add(field); update();
    });
    n.afPreset = el("select", { class: "pr-input", "aria-label": t("focus.travelPresets"), "data-role": "af-range-preset", onchange: event => {
      if (!event.target.value) return;
      const field = AF_LENSES[view.afLens].field; view.afDrafts[field] = event.target.value; view.afDirty.add(field); update();
    } }, el("option", { value: "", text: t("focus.travelCustom") }), ...AF_PRESETS.map(value => el("option", { value: String(value), text: "±" + value })));
    n.afApply = button(t("focus.travelApply"), applyAutofocusRange, { "data-role": "af-range-apply" });
    n.afRangeCurrent = el("span", { class: "pr-profile-note", "data-role": "af-range-current" });
    n.afRangeHelp = el("p", { class: "pr-profile-note", "data-role": "af-range-help" });
    n.afSavedRanges = el("div", { class: "pr-af-saved", "data-role": "af-saved-ranges" });
    n.afDraftNote = el("span", { class: "pr-profile-note", "data-role": "af-draft-note" });
    const jog = input({ type: "number", min: "1", step: "1", class: "pr-step-input", "data-role": "jog", "aria-label": t("focus.jog") }, view.jog, value => { view.jog = value; });
    n.motion = [
      button("−", () => jogFocus(-1), { class: "pr-step-button", "data-role": "jog-down", "aria-label": t("focus.down") }),
      button("+", () => jogFocus(1), { class: "pr-step-button", "data-role": "jog-up", "aria-label": t("focus.up") }),
      button(t("focus.local"), () => send("microscope", "autofocus", { mode: "local" }), { "data-role": "af-local" }),
    ];
    n.snapshot = button(t("rec.snapshot"), saveSnapshot, { "data-role": "snapshot" });
    n.snapPath = input({ type: "text", maxlength: "4096", class: "pr-input pr-wide", autocomplete: "off", "data-role": "snapshot-path" }, view.snapPath, value => { view.snapPath = value; });
    n.record = button(t("rec.start"), toggleRecording, { "data-role": "record" });
    n.arm = button(t("rec.arm"), toggleArm, { "data-role": "arm" });
    n.stab = button(t("rec.stabOn"), () => send("microscope", "set_stabilization", { enabled: view.status?.microscope?.stabilization !== true }, "stab"), { "data-role": "stabilization" });
    n.recSummary = el("span", { class: "pr-summary-state" });
    n.recState = el("p", { class: "lc-muted", "data-role": "rec-state" });
    n.recError = el("p", { class: "pr-warn", "data-role": "rec-error", hidden: true });
    n.recName = input({ type: "text", maxlength: "80", class: "pr-input pr-wide", autocomplete: "off", "data-role": "rec-name" }, view.rec.name, value => { view.rec.name = value; });
    n.recMode = el("select", { class: "pr-input", "data-role": "rec-mode", onchange: event => {
      view.rec.mode = event.target.value === "live" ? "live" : "time_lapse";
      view.rec.fps = view.rec.mode === "live" ? "30" : "1";
      build();
    } }, ...[["time_lapse", "rec.timeLapse"], ["live", "rec.live"]].map(([value, key]) => el("option", { value, text: t(key), selected: view.rec.mode === value ? "selected" : null })));
    n.recMode.value = view.rec.mode;
    n.recInterval = input({ type: "number", min: "0.1", max: "3600", step: "0.1", class: "pr-input pr-narrow", "data-role": "rec-interval" }, view.rec.interval, value => { view.rec.interval = value; });
    n.recFps = input({ type: "number", min: "0.1", max: "120", step: "0.1", class: "pr-input pr-narrow", "data-role": "rec-fps" }, view.rec.fps, value => { view.rec.fps = value; });

    // One preview: the view switch sits inside the frame, and only the shown view streams.
    const views = [["microscope", t("view.scope"), t("scope.view")], ["camera3", t("view.side"), "camera3"]];
    n.viewButtons = views.map(([name, text]) => button(text, () => showView(name), { class: "pr-seg-button", "data-role": "view-" + name, "aria-pressed": "false" }));
    n.viewPanes = views.map(([name, , label]) => el("div", { class: "pr-pane", "data-view": name }, cameraTile(name, label)));
    const preview = el("div", { class: "pr-preview" }, ...n.viewPanes,
      el("div", { class: "pr-seg", role: "group", "aria-label": t("view.switch") }, ...n.viewButtons));

    n.profileNote = el("p", { class: "pr-profile-note", "data-role": "profile-note", role: "status" });
    n.power = el("div", { class: "pr-power", "data-role": "motor-power" });
    n.visual = el("p", { class: "pr-visual", "data-role": "visual-focus", role: "status" });
    n.rough5x = button(t("scope.rough5x"), () => send("microscope", "rough_focus_5x", {}), { "data-role": "rough-focus-5x" });
    n.afSequence = button(t("scope.sequence"), () => send("microscope", "autofocus_sequence", {}), { "data-role": "autofocus-sequence" });
    n.afPhase = el("strong", { "data-role": "af-phase" });
    n.afScore = el("span", { class: "pr-mono", "data-role": "af-score" });
    n.afTravel = el("span", { class: "pr-profile-note", "data-role": "af-travel" });
    n.afScanRange = el("p", { class: "pr-profile-note", "data-role": "af-scan-range" });
    n.afSteps = el("ol", { class: "pr-af-steps", "data-role": "af-steps" });
    n.afError = el("p", { class: "pr-warn", "data-role": "af-error", hidden: true });
    const afStatus = el("section", { class: "pr-af-status", "data-role": "af-status", "aria-label": t("af.title") },
      el("div", { class: "pr-row" }, el("span", { class: "pr-band-label", text: t("af.title") }), n.afPhase), n.afSteps,
      el("div", { class: "pr-row" }, n.afScore), n.afScanRange,
      el("div", { class: "pr-row" }, n.afTravel), n.afError);
    n.tabButtons = ["operate", "calibration"].map(tab => button(t("scope." + tab), () => showScopeTab(tab), {
      class: "pr-tab", role: "tab", id: "pr-tab-" + tab, "aria-controls": "pr-panel-" + tab,
      "aria-selected": String(view.scopeTab === tab), "data-role": "scope-tab-" + tab,
    }));
    const scope = el("section", { class: "lc-card pr-card pr-scope", "aria-labelledby": "pr-scope-title" },
      el("div", { class: "lc-card-head" },
        el("div", { class: "pr-head" }, el("h2", { id: "pr-scope-title", text: t("scope.title") }), n.scopeSub),
        el("div", { class: "pr-row" }, ...controlButtons("microscope"),
          el("button", { type: "button", class: "lc-danger pr-danger", "data-role": "stop-microscope", text: t("scope.stop"), onclick: stopMicroscope }))),
      n.scopeNote, n.power,
      el("div", { class: "pr-tabs", role: "tablist", "aria-label": t("scope.tabs") }, ...n.tabButtons),
      n.operatePanel = el("div", { class: "pr-operate", id: "pr-panel-operate", role: "tabpanel", "aria-labelledby": "pr-tab-operate", "data-role": "scope-operate" },
      preview, n.profileNote, n.visual, afStatus,
      el("div", { class: "pr-band" },
        el("span", { class: "pr-band-label", id: "pr-obj-title", text: t("obj.title") }),
        el("div", { class: "tr-objective-controls" },
          el("div", { class: "pr-objectives", role: "radiogroup", "aria-labelledby": "pr-obj-title" }, ...n.objectives),
          view.turretCalibration?.profileSelector),
        el("span", { class: "pr-band-label", text: t("focus.title") }),
        el("div", { class: "pr-focus" },
          el("div", { class: "pr-row" },
            el("div", { class: "pr-stepper" }, n.motion[0], jog, n.motion[1]),
            n.motion[2],
            el("span", { class: "pr-push" })),
          el("div", { class: "pr-row pr-focus-actions" }, n.rough5x, n.afSequence),
          el("p", { class: "pr-profile-note pr-af-hint", text: t("scope.sequenceHint") }),
          el("div", { class: "pr-focus-budget" },
            el("div", { class: "pr-row pr-end" }, el("label", { class: "pr-field" }, t("af.rangeLens"), n.afLens),
              el("label", { class: "pr-field" }, t("af.range"), n.afRange),
              el("label", { class: "pr-field" }, t("focus.travelPresets"), n.afPreset), n.afApply,
              el("span", { class: "pr-profile-note", text: t("focus.travelUnit") })), n.afRangeHelp, n.afDraftNote,
            n.afRangeCurrent, n.afSavedRanges),
          el("div", { class: "pr-focus-budget" },
            el("div", { class: "pr-row pr-end" }, el("label", { class: "pr-field" }, t("focus.travel"), n.focusTravel),
              el("label", { class: "pr-field" }, t("focus.travelPresets"), n.focusTravelPreset), n.focusTravelApply,
              el("span", { class: "pr-profile-note", text: t("focus.travelUnit") })),
            n.focusTravelCurrent, el("p", { class: "pr-profile-note", text: t("focus.travelHelp") })))),
      el("div", { class: "pr-more" },
        n.turret = disclosure("turret", t("turret.title"), null, n.nudgeRow),
        disclosure("recording", t("rec.title"), n.recSummary,
          el("div", { class: "pr-row pr-end" }, el("label", { class: "pr-field" }, t("rec.path"), n.snapPath), n.snapshot),
          el("div", { class: "pr-row pr-end" },
            el("label", { class: "pr-field" }, t("rec.name"), n.recName),
            el("label", { class: "pr-field" }, t("rec.mode"), n.recMode),
            el("label", { class: "pr-field" }, t("rec.interval"), n.recInterval),
            el("label", { class: "pr-field" }, t("rec.fps"), n.recFps)),
          el("div", { class: "pr-row" }, n.record, n.arm, n.stab),
          n.recState, n.recError))),
      n.calibrationPanel = el("div", { id: "pr-panel-calibration", role: "tabpanel", "aria-labelledby": "pr-tab-calibration", "data-role": "scope-calibration" }, view.turretCalibration?.element));

    view.root.replaceChildren(el("div", { class: "pr-workspace" }, n.stale = el("p", { class: "pr-warn", hidden: true }),
      el("div", { class: "pr-layout" }, scope, furnace)));
    showScopeTab(view.scopeTab);
    buildSequence();
    update();
  }

  function gasNames() {
    const channels = view.status?.furnace?.flow?.gas_channels || [];
    return [0, 1, 2].map(channel => channels.find(item => item.channel_index === channel)?.gas_name || `Gas ${channel + 1}`);
  }

  function chartNode() {
    const doc = view.global.document;
    const { el, t } = view.api;
    const svg = make => (doc && doc.createElementNS ? doc.createElementNS(SVG_NS, make) : el(make));
    const attrs = (node, values) => { for (const [key, value] of Object.entries(values)) node.setAttribute(key, String(value)); return node; };
    const root = attrs(svg("svg"), { viewBox: `0 0 ${CHART.width} ${CHART.height}`, role: "img", class: "pr-chart" });
    const grid = [];
    for (const y of [CHART.top, (CHART.top + CHART.bottom) / 2, CHART.bottom]) {
      root.append(attrs(svg("line"), { x1: CHART.left, x2: CHART.right, y1: y, y2: y, class: "pr-grid" }));
      const label = attrs(svg("text"), { x: 4, y: y + 4, class: "pr-axis" });
      grid.push(label);
      root.append(label);
    }
    const start = attrs(svg("text"), { x: CHART.left, y: 148, class: "pr-axis" });
    start.textContent = t("chart.start");
    const end = attrs(svg("text"), { x: 516, y: 148, class: "pr-axis" });
    end.textContent = t("chart.now");
    const pv = attrs(svg("polyline"), { fill: "none", class: "pr-pv", points: "" });
    const sv = attrs(svg("polyline"), { fill: "none", class: "pr-sv", points: "" });
    root.append(start, end, pv, sv);
    const empty = el("span", { class: "lc-muted", text: t("chart.empty") });
    const figure = el("figure", { class: "pr-figure" }, root,
      el("figcaption", { class: "pr-legend" }, empty));
    return { figure, root, grid, pv, sv, empty };
  }

  function buildSequence() {
    const n = view.nodes;
    if (!n.sequenceBody || !view.rows) return;
    const { el, t } = view.api;
    const field = (row, key, props) => el("td", {}, input({ type: "number", class: "pr-input pr-cell", ...props }, row[key], value => { row[key] = value; view.dirty = true; }));
    n.sequenceBody.replaceChildren(...view.rows.map((row, index) => {
      const number = index + 1;
      return el("tr", { "data-role": "step" },
        el("td", { class: "pr-mono", text: String(number) }),
        field(row, "temperature_c", { min: "0", step: "1", "aria-label": `${t("seq.step")} ${number} ${t("seq.target")}` }),
        field(row, "ramp_rate_c_per_min", { min: "0.1", step: "0.1", "aria-label": `${t("seq.step")} ${number} ${t("seq.ramp")}` }),
        field(row, "hold_minutes", { min: "0", step: "0.1", "aria-label": `${t("seq.step")} ${number} ${t("seq.hold")}` }),
        ...[0, 1, 2].map(channel => el("td", {}, input({ type: "number", min: "0", max: "100", step: "0.1", class: "pr-input pr-cell", placeholder: "—", title: t("seq.noChange"), "aria-label": `${t("seq.step")} ${number} ${t("seq.gas", { gas: gasNames()[channel] })}` },
          row.gas[channel] ?? "", value => { row.gas[channel] = value; view.dirty = true; }))),
        el("td", {}, button("×", () => { view.rows.splice(index, 1); view.dirty = true; buildSequence(); update(); }, { "aria-label": t("seq.remove", { n: number }), "data-role": "remove-step" })));
    }));
  }

  // Updating ---------------------------------------------------------------

  function setText(node, text) { if (node && node.textContent !== text) node.textContent = text; }

  function update() {
    const n = view.nodes;
    if (!n.stale || !view.api) return;
    const { t } = view.api;
    const status = view.status;
    const furnace = status?.furnace || null;
    const scope = status?.microscope || null;
    const busy = key => view.busy.has(key);

    const age = status?.age_s;
    n.stale.hidden = Boolean(status) && !(typeof age === "number" && age > 3);
    setText(n.stale, status ? t("status.stale", { age: fmt(age, 0) }) : t("status.unavailable"));

    // Furnace.
    const furnaceCaps = caps(view.describe.furnace);
    const fHolder = holder(view.api, "furnace");
    const fMine = fHolder === "mine";
    // The buttons show this page's control; only another holder needs a line.
    setText(n.furnaceSub, fHolder === "other" ? t("control.other") : "");
    const fault = furnace?.locked ? furnace?.fault?.message || furnace?.error || "locked" : null;
    n.furnaceNote.hidden = !fault;
    setText(n.furnaceNote, fault ? t("fault", { message: fault }) : "");
    n.ack.hidden = !fault;
    n.ack.disabled = !fMine || busy("acknowledge");
    setDisabled("take-furnace", fMine || fHolder === "other" || busy("take-furnace"));
    setDisabled("release-furnace", !fMine || busy("release-furnace"));
    const ready = fMine && furnace?.connected === true && !furnace?.locked;
    n.connect.hidden = !furnace || furnace.connected === true;
    n.connect.disabled = !fMine || busy("connect");
    n.connectRow.hidden = n.connect.hidden;
    n.connectFlow.hidden = !furnace || furnace.flow?.connected === true;
    n.connectFlow.disabled = !fMine || busy("connect_flow");
    setText(n.tiles.pv, degrees(furnace?.pv));
    setText(n.tiles.sv, degrees(furnace?.sv));
    setText(n.tiles.mode, furnace ? t(modeKey(furnace)) : t("none"));
    setText(n.tiles.ramp, finite(furnace?.ramp) === null ? t("none") : fmt(furnace.ramp));
    // The controller ramp matters only while the furnace heats.
    n.rampItem.hidden = !["mode.RUN", "mode.RAMP", "mode.HOLD"].includes(modeKey(furnace));
    const crucible = status?.crucible;
    setText(n.tiles.crucible, degrees(crucible?.max_c));
    // Old or missing data dims the readings and the chart. The warning above says how old.
    const dim = !status || (typeof age === "number" && age > 3) ? " pr-stale" : "";
    for (const key of ["pv", "sv", "crucible"]) n.tiles[key].className = "pr-tile-value" + (key === "pv" || key === "sv" ? " pr-tone-" + key : "") + dim;
    n.tiles.mode.className = "pr-state-mode" + dim;
    n.tiles.ramp.className = "pr-mono" + dim;
    n.chart.figure.className = "pr-figure" + dim;

    const chart = chartModel(status?.history, finite(status?.now) ?? 0);
    n.chart.pv.setAttribute("points", chart.pv);
    n.chart.sv.setAttribute("points", chart.sv);
    n.chart.grid.forEach((label, index) => setText(label, String([chart.top, chart.top / 2, 0][index])));
    n.chart.root.setAttribute("aria-label", t("chart.label", { pv: fmt(furnace?.pv), sv: fmt(furnace?.sv) }));
    n.chart.empty.hidden = chart.count > 0;

    const seq = furnace?.sequence || {};
    n.apply.disabled = !ready || seq.running === true || busy("apply_setpoint");
    n.save.disabled = !fMine || seq.running === true || busy("set_sequence");
    n.start.disabled = !ready || seq.running === true || seq.phase === "cooling" || busy("start_sequence");
    const phase = seq.phase ? (i18n.en["phase." + seq.phase] ? t("phase." + seq.phase) : String(seq.phase)) : "";
    setText(n.seqState, phase ? (seq.running ? t("seq.stateRunning", { n: (seq.current_index ?? 0) + 1, phase }) : t("seq.state", { phase })) : "");
    setText(n.seqProgress, seq.phase === "holding" && finite(seq.hold_remaining_seconds) !== null
      ? t("seq.holding", { n: (seq.current_index ?? 0) + 1, seconds: Math.ceil(seq.hold_remaining_seconds) })
      : seq.phase === "cooling" ? t("seq.cooling", { until: fmt(seq.end_gas_until_c, 0) }) : "");
    const warning = seqWarning(seq.warning);
    n.seqWarning.hidden = !warning;
    setText(n.seqWarning, warning);
    // Mark the running step only when the editor shows the running sequence.
    const shownSeq = seq.running === true && JSON.stringify(view.rows) === JSON.stringify(editorRows(seq));
    Array.from(n.sequenceBody.children || []).forEach((row, index) => { row.className = shownSeq && index === seq.current_index ? "pr-current" : ""; });

    const channels = furnace?.flow?.gas_channels || [];
    n.gasRows.forEach((row, channel) => {
      const item = channels.find(value => value.channel_index === channel) || {};
      setText(row.name, item.gas_name || `Gas ${channel + 1}`);
      const valve = valveState((furnace?.flow?.gas_valves || []).find(value => value.channel_index === channel));
      setText(row.state, t("gas.actual", { value: finite(item.actual_percent) === null ? t("gas.unverified") : fmt(item.actual_percent) + " %" })
        + " · " + t(valve.key, { state: valve.state ? t(valve.state) : "" }));
      // Amber only when the flow or the valve is not confirmed.
      const doubt = finite(item.actual_percent) === null || !["valve.open", "valve.closed"].includes(valve.key);
      row.state.className = "pr-gas-state" + (doubt ? " pr-doubt" : "");
      row.send.disabled = !fMine || !furnace?.flow?.connected || busy("flow-" + channel);
      row.close.disabled = busy("close-gas-" + channel);
    });

    // Microscope.
    const mHolder = holder(view.api, "microscope");
    const mMine = mHolder === "mine";
    // A profile supplies turret X and the rough 5x starting point; autofocus verifies focus.
    setText(n.scopeSub, mHolder === "other" ? t("control.other") : "");
    const scopeFault = scope?.locked ? scope?.fault?.message || "locked" : null;
    const failed = !scopeFault && scope?.last_command?.result === "failed" ? scope.last_command : null;
    n.scopeNote.hidden = !scopeFault && !failed;
    setText(n.scopeNote, scopeFault ? t("scope.fault", { message: scopeFault })
      : failed ? t("scope.failed", { action: failed.action || "", message: failed.message || "" }) : "");
    setDisabled("take-microscope", mMine || mHolder === "other" || busy("take-microscope"));
    setDisabled("release-microscope", !mMine || busy("release-microscope"));
    const idle = mMine && scope?.connected === true && !scope?.busy && !scope?.locked;
    view.turretCalibration?.setMicroscopeBusy(scope?.busy || ["select_objective", "nudge", "focus_move", "autofocus", "rough_focus_5x", "autofocus_sequence"].some(key => view.busy.has(key)));
    const profileReady = scope?.turret_profile?.ready === true;
    const canMove = idle && profileReady;
    n.profileNote.hidden = profileReady;
    setText(n.profileNote, t(scope?.turret_profile?.needs_height_calibration ? "scope.profileLegacy" : "scope.profileRequired"));
    const power = scope?.motor_power || {};
    n.power.replaceChildren(...["objective", "focus"].map(axis => {
      const value = power[axis];
      const status = value?.sleeping === true ? "sleeping" : value?.enabled === true ? "enabled" : value?.enabled === false ? "disabled" : "unknown";
      return view.api.el("span", { class: "pr-power-chip pr-power-" + status, text: t("power." + axis) + " · " + t("power." + status) });
    }));
    const visual = scope?.visual_focus;
    const details = visual?.objective_id === 0 && finite(visual.y_px) !== null
      ? t("scope.visual", { current: fmt(visual.y_px, 1), target: fmt(visual.target_y_px, 1) }) : t("focus.visualIdle");
    const focusPhase = typeof visual?.phase === "string" ? visual.phase.replaceAll("_", " ") : "";
    setText(n.visual, profileReady ? [focusPhase, details].filter(Boolean).join(" · ") : "");
    n.visual.hidden = !profileReady;
    // Manual calibration can change the seated lens without updating cached objective_id.
    // The service identifies 5x from a fresh image before these commands move.
    n.rough5x.disabled = n.afSequence.disabled = !canMove;
    const af = scope?.autofocus_run || {};
    const afLens = Number.isInteger(af.objective_id) ? ["5×", "10×", "20×", "50×"][af.objective_id] : "—";
    const afPhase = typeof af.phase === "string" ? af.phase.replaceAll("_", " ") : t("af.idle");
    setText(n.afPhase, af.phase ? t("af.phase", { lens: afLens, phase: [afPhase, af.stage].filter(Boolean).join(" · ") }) : t("af.idle"));
    setText(n.afScore, finite(af.score) === null ? t("af.noScore") : t("af.score", { score: fmt(af.score, 2) }));
    const completed = new Set((Array.isArray(af.completed) ? af.completed : []).map(item => Number.isInteger(item) ? item : item?.objective_id));
    n.afSteps.replaceChildren(...["5×", "10×", "20×", "50×"].map((lens, id) => {
      const current = af.objective_id === id, failed = current && (Boolean(af.error) || af.phase === "failed"), stopped = current && ["stopped", "cancelled"].includes(af.phase);
      const status = failed ? "failed" : stopped ? "stopped" : completed.has(id) || current && af.verified === true ? "verified" : current && af.phase && !["idle", "complete", "completed"].includes(af.phase) ? "running" : "waiting";
      return view.api.el("li", { class: "pr-af-step pr-af-" + status, "data-role": "af-step-" + id }, view.api.el("strong", { text: lens }), view.api.el("span", { text: t("af." + status) }));
    }));
    n.afError.hidden = !af.error;
    setText(n.afError, af.error || "");
    const travelUsed = finite(visual?.travel_used_pulses), travelLimit = finite(visual?.travel_limit_pulses);
    n.afTravel.hidden = travelUsed === null || travelLimit === null;
    setText(n.afTravel, t("af.travel", { used: travelUsed ?? "—", limit: travelLimit ?? "—" }));
    const scanRange = Number.isSafeInteger(af.search_half_range_pulses) ? af.search_half_range_pulses : null;
    n.afScanRange.hidden = scanRange === null;
    setText(n.afScanRange, t("af.scanRange", { range: scanRange ?? "—", span: scanRange === null ? "—" : scanRange * 2 }));
    for (const node of n.objectives) {
      const selected = scope?.objective_id === Number(node.dataset.id);
      node.setAttribute("aria-checked", selected ? "true" : "false");
      node.className = "lc-button pr-objective" + (selected ? " pr-selected" : "");
      node.disabled = !canMove;
    }
    // Nudges stay inside ±limit full steps of the objective that select_objective aligned.
    const limit = scope?.objective_offset_limit_steps;
    const offset = scope?.objective_offset_steps;
    const counted = Number.isSafeInteger(offset) && Number.isSafeInteger(scope?.objective_id);
    n.nudgeRow.hidden = !n.nudges.length || !Number.isSafeInteger(limit) || limit <= 0;
    n.turret.hidden = n.nudgeRow.hidden;
    for (const node of n.nudges) node.disabled = !canMove || !counted || Math.abs(offset + Number(node.dataset.steps)) > limit || busy("nudge");
    setText(n.offset, counted ? t("obj.offset", { offset: signed(offset), limit: String(limit) }) : t("obj.offsetUnknown"));
    for (const node of n.motion) node.disabled = !canMove;
    n.focusTravel.value = view.focusTravel;
    n.focusTravelPreset.value = ["200", "500", "1000", "2000", "20000"].includes(view.focusTravel) ? view.focusTravel : "";
    n.focusTravel.disabled = n.focusTravelPreset.disabled = n.focusTravelApply.disabled = (!mMine && !view.turretCalibration?.isDemo?.()) || Boolean(scope?.busy) || busy("focus-travel") || view.turretCalibration?.isCalibrating?.() === true;
    setText(n.focusTravelCurrent, view.focusTravelSaved === null ? t("focus.travelUnknown") : t("focus.travelCurrent", { pulses: view.focusTravelSaved }));
    const editedLens = AF_LENSES[view.afLens], draftRange = view.afDrafts[editedLens.field] || "";
    const proposedRange = draftRange.trim() && Number.isSafeInteger(Number(draftRange)) && Number(draftRange) >= 4 && Number(draftRange) <= 1000000 ? Number(draftRange) : null;
    n.afLens.value = String(view.afLens);
    n.afRange.value = draftRange;
    n.afPreset.value = AF_PRESETS.map(String).includes(draftRange) ? draftRange : "";
    n.afLens.disabled = n.afRange.disabled = n.afPreset.disabled = n.afApply.disabled = n.focusTravel.disabled;
    setText(n.afRangeCurrent, t("af.rangeCurrent"));
    setText(n.afRangeHelp, proposedRange === null ? t("af.rangeInvalid") : t("af.rangeHelp", { lens: editedLens.label, range: proposedRange, span: proposedRange * 2 }));
    n.afDraftNote.hidden = !view.afDirty.has(editedLens.field);
    setText(n.afDraftNote, t("af.rangeDirty", { lens: editedLens.label }));
    n.afSavedRanges.replaceChildren(...AF_LENSES.map(lens => {
      const saved = view.afSettings?.[lens.field];
      return view.api.el("span", { class: "pr-af-saved-lens", text: lens.label + " · " + (Number.isSafeInteger(saved) ? "±" + saved : "—") });
    }));
    const recording = scope?.recording || {};
    const armed = scope?.recording_sequence?.armed === true;
    const active = ACTIVE_RECORDING.has(recording.status);
    n.snapshot.disabled = !idle || busy("snapshot");
    setText(n.record, t(active ? "rec.stop" : "rec.start"));
    n.record.disabled = !mMine || !scope?.connected || busy("record") || (!active && (armed || !idle));
    setText(n.arm, t(armed ? "rec.disarm" : "rec.arm"));
    n.arm.disabled = !mMine || !scope?.connected || busy("arm") || (!armed && (active || !idle));
    setText(n.stab, t(scope?.stabilization === true ? "rec.stabOff" : "rec.stabOn"));
    n.stab.disabled = !idle || typeof scope?.stabilization !== "boolean" || busy("stab");
    setText(n.recState, armed ? t("rec.armed") : recording.status && recording.status !== "idle"
      ? t("rec.state", { status: recording.status, frames: String(recording.frame_count ?? 0) }) : "");
    setText(n.recSummary, armed ? t("rec.armed") : active ? t("rec.state", { status: recording.status, frames: String(recording.frame_count ?? 0) }) : t("rec.off"));
    n.recSummary.className = "pr-summary-state" + (active || armed ? " pr-live" : "");
    for (const node of [n.recName, n.recMode, n.recFps]) node.disabled = active || armed;
    n.recInterval.disabled = active || armed || view.rec.mode === "live";
    n.recError.hidden = !recording.error;
    setText(n.recError, recording.error ? t("rec.error", { error: String(recording.error) }) : "");
  }

  function setDisabled(role, value) {
    const node = findRole(view.root, role);
    if (node) node.disabled = Boolean(value);
  }

  function findRole(node, role) {
    if (!node) return null;
    if (node.getAttribute?.("data-role") === role) return node;
    for (const child of node.children || []) {
      const found = findRole(child, role);
      if (found) return found;
    }
    return null;
  }

  // Actions ----------------------------------------------------------------

  async function act(key, work) {
    if (view.busy.has(key)) return undefined;
    view.busy.add(key);
    update();
    try { return await work(); }
    catch (error) { view.api.notify(error?.message || String(error), "error"); return undefined; }
    finally { view.busy.delete(key); update(); }
  }

  /** Send one relay command once. Never retry; the shell shows an unknown outcome. */
  async function send(device, action, params, key = action) {
    return act(key, async () => {
      const result = await view.api.relay(device, action, params);
      const key = "act." + action;
      const name = view.api.t(key) === key ? action : view.api.t(key);
      if (result?.outcome === "accepted") view.api.notify(view.api.t("sent", { action: name }), "info");
      else if (result?.outcome === "refused") view.api.notify(view.api.t("refused", { error: result.error || "" }), "error");
      return result;
    });
  }

  function applySetpoint({ target, ramp }) {
    const temperature = Number(target);
    const rate = Number(ramp);
    const limit = caps(view.describe.furnace);
    if (String(target).trim() === "" || !Number.isFinite(temperature) || temperature < 0 || (limit.max !== null && temperature > limit.max)
      || !Number.isFinite(rate) || rate <= 0 || (limit.ramp !== null && rate > limit.ramp)) {
      view.api.notify(view.api.t("refused", { error: view.api.t("furnace.caps", { max: fmt(limit.max, 0), ramp: fmt(limit.ramp, 0) }) }), "error");
      return undefined;
    }
    return send("furnace", "apply_setpoint", { temperature_c: temperature, ramp_rate_c_per_min: rate });
  }

  function sendSequence(action) {
    let params;
    try { params = sequenceParams(view.rows, view.endGas, view.endUntil); }
    catch (error) { view.api.notify(view.api.t(error.key || "seq.none", error.vars), "error"); return undefined; }
    return send("furnace", action, params).then(result => { if (result?.outcome === "accepted") view.dirty = false; return result; });
  }

  function sendFlow(channel) {
    const percent = Number(view.flows[channel]);
    if (!Number.isFinite(percent) || percent < 0 || percent > 100) return undefined;
    return send("furnace", "set_gas_flow", { channel_index: channel, flow_percent: percent }, "flow-" + channel);
  }

  function jogFocus(direction) {
    const steps = Math.trunc(Number(view.jog));
    if (!Number.isSafeInteger(steps) || steps <= 0) return undefined;
    return send("microscope", "focus_move", { steps: direction * steps }, "jog");
  }

  function applyFocusTravel() {
    const pulses = Number(view.focusTravel);
    if (!view.focusTravel.trim() || !Number.isSafeInteger(pulses) || pulses < 1 || pulses > 2147483647) {
      view.api.notify(view.api.t("focus.travelInvalid"), "error");
      return undefined;
    }
    return act("focus-travel", async () => {
      const result = await view.api.request("microscope/focus-travel-limit", { pulses });
      if (result?.focus_travel_limit_pulses !== pulses) throw new Error(result?.error || view.api.t("unknown"));
      view.focusTravelSaved = pulses; view.focusTravelDirty = false;
      if (view.status?.microscope) view.status.microscope.focus_travel_limit_pulses = pulses;
      view.api.notify(view.api.t("focus.travelSaved"), "info");
      return result;
    });
  }

  function applyAutofocusRange() {
    const lens = AF_LENSES[view.afLens], draft = view.afDrafts[lens.field] || "", pulses = Number(draft);
    if (!draft.trim() || !Number.isSafeInteger(pulses) || pulses < 4 || pulses > 1000000) {
      view.api.notify(view.api.t("af.rangeInvalid"), "error");
      return undefined;
    }
    return act("focus-travel", async () => {
      const result = await view.api.request("microscope/autofocus-settings", { [lens.field]: pulses });
      if (result?.[lens.field] !== pulses) throw new Error(result?.error || view.api.t("unknown"));
      view.afSettingsEpoch++; view.afSettingsPending = true;
      view.afSettings = result; view.afDirty.delete(lens.field); syncAutofocusDrafts();
      if (view.status?.microscope) view.status.microscope.autofocus_settings = result;
      view.api.notify(view.api.t("af.rangeSaved", { lens: lens.label }), "info");
      return result;
    });
  }

  function saveSnapshot() {
    const params = snapshotParams(view.snapPath);
    if (!params) { view.api.notify(view.api.t("rec.pathInvalid"), "error"); return undefined; }
    return send("microscope", "snapshot", params).then(result => {
      // The service refuses a path that exists, so a saved path is cleared for the next snapshot.
      if (result?.outcome === "accepted" && params.path && view.snapPath.trim() === params.path) {
        view.snapPath = "";
        if (view.nodes.snapPath) view.nodes.snapPath.value = "";
      }
      return result;
    });
  }

  /** The recording fields, or null after a notice. */
  function recordingFields() {
    try { return recordingParams(view.rec); }
    catch (error) { view.api.notify(view.api.t(error.key || "rec.invalid"), "error"); return null; }
  }

  function toggleRecording() {
    const recording = view.status?.microscope?.recording || {};
    if (ACTIVE_RECORDING.has(recording.status) && recording.id) return send("microscope", "stop_recording", { recording_id: recording.id }, "record");
    const fields = recordingFields();
    if (!fields) return undefined;
    return send("microscope", "start_recording", { recording_id: recordingId(view.global.crypto), ...fields }, "record");
  }

  function toggleArm() {
    const armed = view.status?.microscope?.recording_sequence;
    if (armed?.armed && armed.id) return send("microscope", "record_with_sequence", { recording_id: armed.id, enabled: false }, "arm");
    const fields = recordingFields();
    if (!fields) return undefined;
    return send("microscope", "record_with_sequence", { recording_id: recordingId(view.global.crypto), enabled: true, ...fields }, "arm");
  }

  /** Close 1 gas channel through the workspace gas-stop route. No control; sent once, never retried. */
  function closeGas(channel) {
    return act("close-gas-" + channel, async () => {
      const result = await view.api.request("gas-stop", { channel_index: channel }, { accept: [409, 502], timeout: 8000 });
      const outcome = ["confirmed", "unconfirmed"].includes(result?.outcome) ? result.outcome : "unknown";
      const t = view.api.t;
      view.api.notify(t("gas.closeResult", { gas: gasNames()[channel], outcome: t("outcome." + outcome) }), outcome === "confirmed" ? "info" : "warn");
      return result;
    });
  }

  /** Send every Stop press, also while an earlier Stop is pending; stops are never blocked. */
  async function stopDevice(device) {
    try {
      const result = await view.api.stop(device);
      const outcome = result?.outcome || "unknown";
      view.api.notify(view.api.t("stop.result", { device: view.api.deviceName(device), outcome: view.api.t("outcome." + outcome) }), outcome === "confirmed" ? "info" : "warn");
      return result;
    } catch (error) { view.api.notify(error?.message || String(error), "error"); return undefined; }
  }

  /** Stop heat and gas holds the run like the Overview button. The stop result band shows the outcome. */
  function stopFurnace() { return view.api.safetyStop("heat").catch(() => {}); }
  function stopMicroscope() { return stopDevice("microscope"); }

  /** The Overview gas route: every gas channel, no control needed, sent once. */
  function stopGas() { return view.api.safetyStop("gas").catch(() => {}); }

  return {
    install, i18n, view,
    logic: { chartModel, sequenceParams, editorRows, modeKey, recordingId, objectiveIds, objectiveName, nudgeSteps, signed, caps, fmt, snapshotParams, recordingParams, valveState },
    _internal: { mount, build, update, poll, show, hide, findRole },
  };
});
