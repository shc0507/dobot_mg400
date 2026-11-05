/* Furnace + microscope workspace (DSH-06). Described commands go through the console relay; focus zero and fault acknowledgement use the workspace routes. */
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
  const SVG_NS = "http://www.w3.org/2000/svg";
  const DEFAULT_STEP = { temperature_c: 100, ramp_rate_c_per_min: 5, hold_minutes: 10 };
  const ACTIVE_RECORDING = new Set(["starting", "recording"]);

  const i18n = {
    en: {
      "furnace.title": "Furnace · AI-808",
      "furnace.caps": "Caps {max} °C · {ramp} °C/min",
      "control.mine": "you have control", "control.other": "another client has control",
      "control.free": "no control", "control.take": "Take control", "control.release": "Release",
      "control.takeLabel": "Take control of the {device}", "control.releaseLabel": "Release the {device}",
      "furnace.stop": "Stop heat and gas", "furnace.connect": "Connect furnace", "furnace.connectFlow": "Connect gas flow",
      "tile.pv": "PV", "tile.sv": "SV", "tile.mode": "Mode", "tile.crucible": "Crucible (thermal)",
      "mode.HOLD": "Hold", "mode.RUN": "Run", "mode.STOPPED": "Stopped", "mode.DISCONNECTED": "Disconnected", "mode.UNKNOWN": "Unknown", "mode.RAMP": "Ramping",
      "chart.label": "Temperature over the last 60 minutes: PV {pv}, SV {sv}", "chart.start": "−60 min", "chart.now": "now",
      "chart.empty": "No samples yet. The chart fills while this console is open.",
      "set.target": "Target °C", "set.ramp": "Ramp °C/min", "set.apply": "Apply and run…",
      "set.confirm": "Heat the furnace to {target} °C at {ramp} °C/min?",
      "seq.title": "Sequence", "seq.add": "+ Add step", "seq.save": "Save sequence", "seq.start": "Start sequence…",
      "seq.step": "Step", "seq.target": "Target °C", "seq.ramp": "Ramp °C/min", "seq.hold": "Hold min",
      "seq.gas": "{gas} %", "seq.remove": "Remove step {n}", "seq.noChange": "No change",
      "seq.endGas": "End gas {gas} %", "seq.endUntil": "End gas until °C",
      "seq.confirm": "Start the {count}-step sequence? It heats the furnace.",
      "seq.state": "{phase}", "seq.stateRunning": "Step {n} · {phase}",
      "seq.holding": "Holding step {n}: {seconds} s left.", "seq.cooling": "End gas continues until the temperature is below {until} °C.",
      "seq.needEnd": "Enter an end-gas temperature when a step sets gas.", "seq.invalid": "Step {n}: enter a target, a ramp above 0 and a hold of 0 or more.",
      "seq.none": "Add at least 1 step.",
      "gas.title": "Gas", "gas.stopAll": "Stop all gas", "gas.flow": "Flow % for {gas}", "gas.send": "Send flow",
      "gas.actual": "actual {value}", "gas.unverified": "unverified",
      "confirm.ok": "Confirm", "confirm.cancel": "Cancel",
      "scope.title": "Microscope · Toupcam", "scope.zero": "zero set", "scope.noZero": "focus zero unknown",
      "scope.stop": "Stop", "scope.view": "Toupcam", "scope.side": "Side camera (camera3) on the turret",
      "tile.offline": "OFFLINE",
      "obj.title": "Objective", "obj.name": "Objective {id}", "obj.note": "Switching raises focus to clearance first, then turns the turret with the side camera.",
      "obj.nudge": "Turn the turret {steps} full steps", "obj.offset": "Turret {offset} / ±{limit} full steps",
      "obj.offsetUnknown": "Turret offset unknown. Select another objective first.",
      "focus.title": "Focus", "focus.position": "{pos} / {max} pulses", "focus.unknown": "Position unknown",
      "focus.noLimits": "Limits not measured", "focus.min": "{min} · window side", "focus.max": "{max} · away",
      "focus.bar": "Focus position", "focus.jog": "Jog pulses", "focus.down": "− Jog", "focus.up": "+ Jog",
      "focus.local": "Local autofocus", "focus.full": "Full autofocus",
      "zero.reminder": "The focus gears can slip. Re-zero at the 50x focus before each use.",
      "zero.set": "Set focus zero…",
      "zero.confirm": "Is the 50x objective focused on the sample? The current focus position becomes zero.",
      "zero.done": "Focus zero sent.",
      "rec.snapshot": "Save snapshot", "rec.start": "Start recording", "rec.stop": "Stop recording",
      "rec.arm": "Record next furnace sequence", "rec.disarm": "Cancel sequence recording",
      "rec.stabOn": "Turn stabilization on", "rec.stabOff": "Turn stabilization off",
      "rec.state": "Recording: {status} · {frames} frames", "rec.armed": "Armed for the next furnace sequence.",
      "sent": "{action} sent.", "refused": "Refused: {error}", "unknown": "The outcome is unknown. Check the device before you send it again.",
      "stop.result": "{device} stop: {outcome}", "gas.stopResult": "Gas stop sent.",
      "outcome.confirmed": "confirmed", "outcome.pending": "pending", "outcome.unconfirmed": "not confirmed", "outcome.unknown": "unknown",
      "status.stale": "Data is stale ({age} s).", "status.unavailable": "Status is unavailable.",
      "fault": "Fault: {message}. Make sure the controller shows STOP, then acknowledge.",
      "fault.ack": "Acknowledge fault…", "fault.confirm": "Is the furnace safe and the controller at STOP? Acknowledging clears the fault.",
      "fault.done": "Fault acknowledgement sent.",
      "scope.fault": "Fault: {message}. Only Stop is allowed.",
      "scope.failed": "{action} failed: {message}",
      "phase.idle": "Idle", "phase.ramping": "Ramping", "phase.holding": "Holding", "phase.cooling": "Cooling",
      "phase.stopped": "Stopped", "phase.fault": "Fault", "phase.applying": "Applying", "phase.complete": "Complete",
      "none": "—",
    },
    zh: {
      "furnace.title": "炉子 · AI-808",
      "furnace.caps": "上限 {max} °C · {ramp} °C/min",
      "control.mine": "你有控制权", "control.other": "其他客户端有控制权",
      "control.free": "无控制权", "control.take": "获取控制权", "control.release": "释放",
      "control.takeLabel": "获取{device}控制权", "control.releaseLabel": "释放{device}",
      "furnace.stop": "停止加热和气体", "furnace.connect": "连接炉子", "furnace.connectFlow": "连接气体流量计",
      "tile.pv": "PV", "tile.sv": "SV", "tile.mode": "模式", "tile.crucible": "坩埚（热像）",
      "mode.HOLD": "保持", "mode.RUN": "运行", "mode.STOPPED": "已停止", "mode.DISCONNECTED": "未连接", "mode.UNKNOWN": "未知", "mode.RAMP": "升温中",
      "chart.label": "最近 60 分钟温度：PV {pv}，SV {sv}", "chart.start": "−60 分钟", "chart.now": "现在",
      "chart.empty": "暂无数据。控制台打开时记录曲线。",
      "set.target": "目标 °C", "set.ramp": "速率 °C/min", "set.apply": "应用并运行…",
      "set.confirm": "以 {ramp} °C/min 把炉子加热到 {target} °C？",
      "seq.title": "程序", "seq.add": "+ 添加步骤", "seq.save": "保存程序", "seq.start": "启动程序…",
      "seq.step": "步骤", "seq.target": "目标 °C", "seq.ramp": "速率 °C/min", "seq.hold": "保持 min",
      "seq.gas": "{gas} %", "seq.remove": "删除步骤 {n}", "seq.noChange": "不变",
      "seq.endGas": "结束气体 {gas} %", "seq.endUntil": "结束气体直到 °C",
      "seq.confirm": "启动 {count} 步程序？炉子会加热。",
      "seq.state": "{phase}", "seq.stateRunning": "步骤 {n} · {phase}",
      "seq.holding": "步骤 {n} 保持中：剩余 {seconds} s。", "seq.cooling": "结束气体持续到温度低于 {until} °C。",
      "seq.needEnd": "步骤设置了气体时，填写结束气体温度。", "seq.invalid": "步骤 {n}：填写目标温度、大于 0 的速率和不小于 0 的保持时间。",
      "seq.none": "至少添加 1 个步骤。",
      "gas.title": "气体", "gas.stopAll": "停止全部气体", "gas.flow": "{gas} 流量 %", "gas.send": "发送流量",
      "gas.actual": "实际 {value}", "gas.unverified": "未确认",
      "confirm.ok": "确认", "confirm.cancel": "取消",
      "scope.title": "显微镜 · Toupcam", "scope.zero": "零点已设", "scope.noZero": "对焦零点未知",
      "scope.stop": "停止", "scope.view": "Toupcam", "scope.side": "转盘侧面相机（camera3）",
      "tile.offline": "离线",
      "obj.title": "物镜", "obj.name": "物镜 {id}", "obj.note": "切换时先把焦距升到安全高度，再用侧面相机转动转盘。",
      "obj.nudge": "转盘转动 {steps} 整步", "obj.offset": "转盘 {offset} / ±{limit} 整步",
      "obj.offsetUnknown": "转盘偏移未知。先选择其他物镜。",
      "focus.title": "对焦", "focus.position": "{pos} / {max} 脉冲", "focus.unknown": "位置未知",
      "focus.noLimits": "限位未测量", "focus.min": "{min} · 窗口侧", "focus.max": "{max} · 远离",
      "focus.bar": "对焦位置", "focus.jog": "点动脉冲", "focus.down": "− 点动", "focus.up": "+ 点动",
      "focus.local": "局部自动对焦", "focus.full": "全程自动对焦",
      "zero.reminder": "对焦齿轮可能打滑。每次使用前在 50x 焦点处重新设零。",
      "zero.set": "设置对焦零点…",
      "zero.confirm": "50x 物镜已对焦到样品上吗？当前对焦位置将设为零点。",
      "zero.done": "已发送对焦零点。",
      "rec.snapshot": "保存快照", "rec.start": "开始录制", "rec.stop": "停止录制",
      "rec.arm": "录制下一次炉子程序", "rec.disarm": "取消程序录制",
      "rec.stabOn": "开启防抖", "rec.stabOff": "关闭防抖",
      "rec.state": "录制：{status} · {frames} 帧", "rec.armed": "已准备录制下一次炉子程序。",
      "sent": "已发送 {action}。", "refused": "已拒绝：{error}", "unknown": "结果未知。再次发送前先检查设备。",
      "stop.result": "{device}停止：{outcome}", "gas.stopResult": "已发送气体停止。",
      "outcome.confirmed": "已确认", "outcome.pending": "等待中", "outcome.unconfirmed": "未确认", "outcome.unknown": "未知",
      "status.stale": "数据已过期（{age} s）。", "status.unavailable": "状态不可用。",
      "fault": "故障：{message}。确认控制器显示 STOP 后再确认故障。",
      "fault.ack": "确认故障…", "fault.confirm": "炉子安全且控制器处于 STOP 吗？确认后将清除故障。",
      "fault.done": "已发送故障确认。",
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

  /** Position of the focus on the measured limits, as 0..1, or null. */
  function focusFraction(position, limits) {
    const pos = finite(position);
    if (pos === null || !Array.isArray(limits) || limits.length !== 2) return null;
    const [low, high] = limits.map(finite);
    if (low === null || high === null || high <= low) return null;
    return Math.min(1, Math.max(0, (pos - low) / (high - low)));
  }

  function modeKey(furnace) {
    const mode = furnace?.control_mode || "DISCONNECTED";
    if (mode === "RUN" && furnace?.waiting_for_program_step_2) return "mode.RAMP";
    return i18n.en["mode." + mode] ? "mode." + mode : "mode.UNKNOWN";
  }

  function recordingId(cryptoObject) {
    const bytes = cryptoObject.getRandomValues(new Uint8Array(16));
    return Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  }

  function objectiveIds(description) {
    const ids = description?.commands?.select_objective?.params?.properties?.id?.enum;
    return Array.isArray(ids) && ids.every(Number.isSafeInteger) ? ids.slice(0, 8) : [0, 1, 2, 3];
  }

  /** The described turret nudges in full steps, ascending. None when the service has no objective_nudge. */
  function nudgeSteps(description) {
    const steps = description?.commands?.objective_nudge?.params?.properties?.steps?.enum;
    return Array.isArray(steps) && steps.every(Number.isSafeInteger) ? steps.slice(0, 8).sort((a, b) => a - b) : [];
  }

  function signed(value) { return value > 0 ? "+" + value : value < 0 ? "−" + Math.abs(value) : "0"; }

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
    confirm: null, busy: new Set(), tiles: {}, nodes: {} };

  function show() {
    if (!view.api || view.timer) return;
    poll();
    view.timer = setInterval(poll, POLL_MS);
  }

  function hide() {
    if (view.timer) clearInterval(view.timer);
    view.timer = null;
  }

  async function poll() {
    try { view.status = await view.api.request("status"); }
    catch { view.status = null; }
    // Read each description once; retry a missing one at most every DESCRIBE_RETRY_MS.
    const at = Date.now();
    for (const device of ["furnace", "microscope"]) {
      if (view.describe[device] || at - (view.describeAt[device] || -Infinity) < DESCRIBE_RETRY_MS) continue;
      view.describeAt[device] = at;
      view.api.describe(device).then(value => { view.describe[device] = value; build(); }).catch(() => {});
    }
    const names = gasNames().join("|");
    if (view.gasKey !== names) { view.gasKey = names; build(); }
    if (!view.rows || (!view.dirty && !view.loaded && view.status?.furnace?.sequence)) {
      view.rows = editorRows(view.status?.furnace?.sequence);
      if (view.status?.furnace?.sequence) {
        view.loaded = true;
        const seq = view.status.furnace.sequence;
        if (seq.end_gas_until_c !== null && seq.end_gas_until_c !== undefined) view.endUntil = String(seq.end_gas_until_c);
        for (const channel of [0, 1, 2]) view.endGas[channel] = String(seq.end_gas?.[channel] ?? seq.end_gas?.[String(channel)] ?? 0);
      }
      buildSequence();
    }
    update();
  }

  function mount(node, api, global) {
    view.root = node;
    view.api = api;
    view.global = global || {};
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

  function confirmBox(id, text, onConfirm, extra) {
    const { el, t } = view.api;
    if (view.confirm !== id) return el("div", { hidden: true });
    return el("div", { class: "lc-confirm", role: "alertdialog", "aria-label": text },
      el("p", { text }),
      el("div", { class: "pr-row" },
        extra || button(t("confirm.ok"), () => { view.confirm = null; build(); onConfirm(); }, { class: "lc-button pr-primary", "data-role": "confirm-" + id }),
        button(t("confirm.cancel"), () => { view.confirm = null; build(); }, { "data-role": "cancel-" + id })));
  }

  function ask(id) { view.confirm = id; build(); }

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
    const tiles = ["pv", "sv", "mode", "crucible"].map(key => {
      n.tiles[key] = el("span", { class: "pr-tile-value" });
      return el("div", { class: "pr-tile" }, el("span", { class: "pr-tile-label", text: t("tile." + key) }), n.tiles[key]);
    });
    n.chart = chartNode();
    n.connect = button(t("furnace.connect"), () => send("furnace", "connect", {}), { "data-role": "connect" });
    n.connectFlow = button(t("furnace.connectFlow"), () => send("furnace", "connect_flow", {}), { "data-role": "connect-flow" });
    n.apply = button(t("set.apply"), () => ask("apply"), { class: "lc-button pr-primary", "data-role": "apply" });
    const target = input({ type: "number", min: "0", step: "1", max: furnaceCaps.max ?? undefined, class: "pr-input", "data-role": "target" }, view.target, value => { view.target = value; });
    const ramp = input({ type: "number", min: "0.1", step: "0.1", max: furnaceCaps.ramp ?? undefined, class: "pr-input", "data-role": "ramp" }, view.ramp, value => { view.ramp = value; });
    const setpoint = el("div", { class: "pr-row pr-end" },
      el("label", { class: "pr-field" }, t("set.target"), target),
      el("label", { class: "pr-field" }, t("set.ramp"), ramp),
      n.apply);
    const applyConfirm = confirmBox("apply", t("set.confirm", { target: view.target, ramp: view.ramp }), applySetpoint);

    n.seqState = el("span", { class: "lc-muted", "data-role": "seq-state" });
    n.seqProgress = el("p", { class: "lc-muted" });
    n.add = button(t("seq.add"), () => { view.rows.push(editorRows(null)[0]); view.dirty = true; buildSequence(); update(); }, { "data-role": "add-step" });
    n.save = button(t("seq.save"), () => sendSequence("set_sequence"), { "data-role": "save-sequence" });
    n.start = button(t("seq.start"), () => ask("start"), { class: "lc-button pr-strong", "data-role": "start-sequence" });
    n.sequenceBody = el("tbody");
    n.sequenceTable = el("div", { class: "pr-table-wrap" }, el("table", { class: "pr-table" },
      el("thead", {}, el("tr", {}, ...[t("seq.step"), t("seq.target"), t("seq.ramp"), t("seq.hold"), ...gasNames().map(gas => t("seq.gas", { gas })), ""].map(text => el("th", { scope: "col", text })))),
      n.sequenceBody));
    const endGas = el("div", { class: "pr-row pr-end" },
      ...[0, 1, 2].map(channel => el("label", { class: "pr-field" }, t("seq.endGas", { gas: gasNames()[channel] }),
        input({ type: "number", min: "0", max: "100", step: "0.1", class: "pr-input" }, view.endGas[channel], value => { view.endGas[channel] = value; view.dirty = true; }))),
      el("label", { class: "pr-field" }, t("seq.endUntil"),
        input({ type: "number", min: "0", step: "1", class: "pr-input", "data-role": "end-until" }, view.endUntil, value => { view.endUntil = value; view.dirty = true; })));
    const startConfirm = confirmBox("start", t("seq.confirm", { count: view.rows?.length || 0 }), () => sendSequence("start_sequence"));

    n.gasRows = [0, 1, 2].map(channel => {
      const row = { name: el("span", { class: "pr-gas-name" }), state: el("span", { class: "pr-gas-state" }) };
      row.send = button(t("gas.send"), () => sendFlow(channel), { "data-role": "send-flow-" + channel });
      row.input = input({ type: "number", min: "0", max: "100", step: "0.1", class: "pr-input pr-narrow", "aria-label": t("gas.flow", { gas: gasNames()[channel] }) }, view.flows[channel], value => { view.flows[channel] = value; });
      row.node = el("div", { class: "pr-gas" }, row.name, row.state, row.input, row.send);
      return row;
    });

    const furnace = el("section", { class: "lc-card pr-card", "aria-labelledby": "pr-furnace-title" },
      el("div", { class: "lc-card-head" },
        el("div", { class: "pr-head" }, el("h2", { id: "pr-furnace-title", text: t("furnace.title") }), n.furnaceSub),
        el("div", { class: "pr-row" }, ...controlButtons("furnace"),
          el("button", { type: "button", class: "lc-stop", "data-role": "stop-furnace", text: t("furnace.stop"), onclick: stopFurnace }))),
      n.furnaceNote,
      n.ack = button(t("fault.ack"), () => ask("ack"), { class: "lc-danger pr-danger", "data-role": "acknowledge", hidden: true }),
      confirmBox("ack", t("fault.confirm"), () => operator("acknowledge", "fault.done")),
      el("div", { class: "pr-row" }, n.connect, n.connectFlow),
      el("div", { class: "pr-tiles" }, ...tiles),
      n.chart.figure,
      setpoint, applyConfirm,
      el("div", { class: "pr-block" },
        el("div", { class: "lc-card-head" }, el("div", { class: "pr-head" }, el("h3", { text: t("seq.title") }), n.seqState),
          el("div", { class: "pr-row" }, n.add, n.save, n.start)),
        n.sequenceTable, endGas, n.seqProgress, startConfirm),
      el("div", { class: "pr-block" },
        el("div", { class: "lc-card-head" }, el("h3", { text: t("gas.title") }),
          button(t("gas.stopAll"), stopGas, { class: "lc-danger pr-danger", "data-role": "stop-gas" })),
        ...n.gasRows.map(row => row.node)));

    // Microscope.
    n.scopeSub = el("span", { class: "lc-muted" });
    n.scopeNote = el("p", { class: "pr-note", "data-role": "scope-note", hidden: true });
    n.objectives = objectiveIds(view.describe.microscope).map(id => {
      const node = el("button", { type: "button", class: "lc-button pr-objective", role: "radio", "aria-checked": "false", "data-role": "objective-" + id, text: t("obj.name", { id }), onclick: () => send("microscope", "select_objective", { id }) });
      node.dataset.id = String(id);
      return node;
    });
    n.nudges = nudgeSteps(view.describe.microscope).map(steps => {
      const node = button(signed(steps), () => send("microscope", "objective_nudge", { steps }, "nudge"),
        { "data-role": "nudge-" + (steps > 0 ? "up-" : "down-") + Math.abs(steps), "aria-label": t("obj.nudge", { steps: signed(steps) }) });
      node.dataset.steps = String(steps);
      return node;
    });
    n.offset = el("span", { class: "pr-mono", "data-role": "nudge-offset" });
    n.nudgeRow = el("div", { class: "pr-row", "data-role": "nudge-row", hidden: true }, ...n.nudges, n.offset);
    n.focusText = el("span", { class: "pr-mono" });
    n.focusFill = el("span", { class: "pr-bar-fill" });
    n.focusBar = el("div", { class: "pr-bar", role: "meter", "aria-label": t("focus.bar"), "aria-valuemin": "0", "aria-valuemax": "100" }, n.focusFill);
    n.focusMin = el("span", {});
    n.focusMax = el("span", {});
    const jog = input({ type: "number", min: "1", step: "1", class: "pr-input", "data-role": "jog" }, view.jog, value => { view.jog = value; });
    n.motion = [
      button(t("focus.down"), () => jogFocus(-1), { "data-role": "jog-down" }),
      button(t("focus.up"), () => jogFocus(1), { "data-role": "jog-up" }),
      button(t("focus.local"), () => send("microscope", "autofocus", { mode: "local" }), { "data-role": "af-local" }),
      button(t("focus.full"), () => send("microscope", "autofocus", { mode: "full" }), { "data-role": "af-full" }),
    ];
    const zeroConfirm = confirmBox("zero", t("zero.confirm"), () => operator("focus-zero", "zero.done"));
    n.snapshot = button(t("rec.snapshot"), () => send("microscope", "snapshot", {}), { "data-role": "snapshot" });
    n.record = button(t("rec.start"), toggleRecording, { "data-role": "record" });
    n.arm = button(t("rec.arm"), toggleArm, { "data-role": "arm" });
    n.stab = button(t("rec.stabOn"), () => send("microscope", "set_stabilization", { enabled: view.status?.microscope?.stabilization !== true }, "stab"), { "data-role": "stabilization" });
    n.recState = el("p", { class: "lc-muted", "data-role": "rec-state" });

    const scope = el("section", { class: "lc-card pr-card", "aria-labelledby": "pr-scope-title" },
      el("div", { class: "lc-card-head" },
        el("div", { class: "pr-head" }, el("h2", { id: "pr-scope-title", text: t("scope.title") }), n.scopeSub),
        el("div", { class: "pr-row" }, ...controlButtons("microscope"),
          el("button", { type: "button", class: "lc-danger pr-danger", "data-role": "stop-microscope", text: t("scope.stop"), onclick: stopMicroscope }))),
      n.scopeNote,
      el("div", { class: "pr-views" },
        el("div", { class: "pr-main-view" }, cameraTile("microscope", t("scope.view"))),
        el("div", { class: "pr-side-view" }, cameraTile("camera3", "camera3"), el("span", { class: "lc-muted", text: t("scope.side") }))),
      el("div", { class: "pr-block" }, el("h3", { id: "pr-obj-title", text: t("obj.title") }),
        el("div", { class: "pr-objectives", role: "radiogroup", "aria-labelledby": "pr-obj-title" }, ...n.objectives),
        el("p", { class: "lc-muted", text: t("obj.note") }),
        n.nudgeRow),
      el("div", { class: "pr-block" },
        el("div", { class: "lc-card-head" }, el("h3", { text: t("focus.title") }), n.focusText),
        n.focusBar,
        el("div", { class: "pr-bar-labels" }, n.focusMin, n.focusMax),
        el("div", { class: "pr-row pr-end" }, el("label", { class: "pr-field" }, t("focus.jog"), jog), n.motion[0], n.motion[1]),
        el("div", { class: "pr-row" }, n.motion[2], n.motion[3]),
        el("p", { class: "pr-warn", text: t("zero.reminder") }),
        n.zero = button(t("zero.set"), () => ask("zero"), { class: "lc-danger pr-danger", "data-role": "zero" }),
        zeroConfirm),
      el("div", { class: "pr-row" }, n.snapshot, n.record, n.arm, n.stab),
      n.recState);

    view.root.replaceChildren(el("div", { class: "pr-workspace" }, n.stale = el("p", { class: "pr-warn", hidden: true }),
      el("div", { class: "pr-layout" }, furnace, scope)));
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
      el("figcaption", { class: "pr-legend" }, el("span", { class: "pr-key-pv", text: "━ PV" }), el("span", { class: "pr-key-sv", text: "╍ SV" }), empty));
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
    setText(n.furnaceSub, [furnaceCaps.max !== null ? t("furnace.caps", { max: fmt(furnaceCaps.max, 0), ramp: fmt(furnaceCaps.ramp, 0) }) : null, t("control." + fHolder)].filter(Boolean).join(" · "));
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
    n.connectFlow.hidden = !furnace || furnace.flow?.connected === true;
    n.connectFlow.disabled = !fMine || busy("connect_flow");
    setText(n.tiles.pv, degrees(furnace?.pv));
    setText(n.tiles.sv, degrees(furnace?.sv));
    setText(n.tiles.mode, furnace ? t(modeKey(furnace)) : t("none"));
    const crucible = status?.crucible;
    setText(n.tiles.crucible, degrees(crucible?.max_c));
    n.tiles.pv.className = "pr-tile-value pr-tone-pv";
    n.tiles.sv.className = "pr-tile-value pr-tone-sv";

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
    Array.from(n.sequenceBody.children || []).forEach((row, index) => { row.className = seq.running && index === seq.current_index ? "pr-current" : ""; });

    const channels = furnace?.flow?.gas_channels || [];
    n.gasRows.forEach((row, channel) => {
      const item = channels.find(value => value.channel_index === channel) || {};
      setText(row.name, item.gas_name || `Gas ${channel + 1}`);
      setText(row.state, t("gas.actual", { value: finite(item.actual_percent) === null ? t("gas.unverified") : fmt(item.actual_percent) + " %" }));
      row.send.disabled = !fMine || !furnace?.flow?.connected || busy("flow-" + channel);
    });

    // Microscope.
    const mHolder = holder(view.api, "microscope");
    const mMine = mHolder === "mine";
    setText(n.scopeSub, [t("control." + mHolder), scope ? t(scope.focus_zero_known ? "scope.zero" : "scope.noZero") : null].filter(Boolean).join(" · "));
    const scopeFault = scope?.locked ? scope?.fault?.message || "locked" : null;
    const failed = !scopeFault && scope?.last_command?.result === "failed" ? scope.last_command : null;
    n.scopeNote.hidden = !scopeFault && !failed;
    setText(n.scopeNote, scopeFault ? t("scope.fault", { message: scopeFault })
      : failed ? t("scope.failed", { action: failed.action || "", message: failed.message || "" }) : "");
    setDisabled("take-microscope", mMine || mHolder === "other" || busy("take-microscope"));
    setDisabled("release-microscope", !mMine || busy("release-microscope"));
    const idle = mMine && scope?.connected === true && !scope?.busy && !scope?.locked;
    const canMove = idle && scope?.focus_zero_known === true;
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
    for (const node of n.nudges) node.disabled = !canMove || !counted || Math.abs(offset + Number(node.dataset.steps)) > limit || busy("nudge");
    setText(n.offset, counted ? t("obj.offset", { offset: signed(offset), limit: String(limit) }) : t("obj.offsetUnknown"));
    for (const node of n.motion) node.disabled = !canMove;
    const limits = Array.isArray(scope?.focus_limits_pulses) ? scope.focus_limits_pulses : [];
    const fraction = focusFraction(scope?.focus_position_pulses, limits);
    n.focusFill.setAttribute("style", `width: ${fraction === null ? 0 : (fraction * 100).toFixed(1)}%`);
    if (fraction === null) n.focusBar.removeAttribute("aria-valuenow");
    else n.focusBar.setAttribute("aria-valuenow", (fraction * 100).toFixed(0));
    const pos = finite(scope?.focus_position_pulses);
    setText(n.focusText, pos === null ? t("focus.unknown") : limits.length === 2 ? t("focus.position", { pos: String(pos), max: String(limits[1]) }) : String(pos));
    setText(n.focusMin, limits.length === 2 ? t("focus.min", { min: String(limits[0]) }) : t("focus.noLimits"));
    setText(n.focusMax, limits.length === 2 ? t("focus.max", { max: String(limits[1]) }) : "");
    n.zero.disabled = !idle || busy("focus-zero");
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
      if (result?.outcome === "accepted") view.api.notify(view.api.t("sent", { action }), "info");
      else if (result?.outcome === "refused") view.api.notify(view.api.t("refused", { error: result.error || "" }), "error");
      return result;
    });
  }

  /** Send one operator action (focus zero, fault acknowledgement) once. Never retry. */
  async function operator(path, doneKey) {
    return act(path, async () => {
      const result = await view.api.request(path, { confirmed: true }, { accept: [409, 502], timeout: 8000 });
      const t = view.api.t;
      if (result?.outcome === "accepted") view.api.notify(t(doneKey), "info");
      else if (result?.outcome === "refused") view.api.notify(t("refused", { error: result.error || "" }), "error");
      else view.api.notify(t("unknown"), "warn");
      return result;
    });
  }

  function applySetpoint() {
    const temperature = Number(view.target);
    const rate = Number(view.ramp);
    const limit = caps(view.describe.furnace);
    if (String(view.target).trim() === "" || !Number.isFinite(temperature) || temperature < 0 || (limit.max !== null && temperature > limit.max)
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

  function toggleRecording() {
    const recording = view.status?.microscope?.recording || {};
    if (ACTIVE_RECORDING.has(recording.status) && recording.id) return send("microscope", "stop_recording", { recording_id: recording.id }, "record");
    return send("microscope", "start_recording", { recording_id: recordingId(view.global.crypto) }, "record");
  }

  function toggleArm() {
    const armed = view.status?.microscope?.recording_sequence;
    if (armed?.armed && armed.id) return send("microscope", "record_with_sequence", { recording_id: armed.id, enabled: false }, "arm");
    return send("microscope", "record_with_sequence", { recording_id: recordingId(view.global.crypto), enabled: true }, "arm");
  }

  async function stopDevice(device) {
    return act("stop-" + device, async () => {
      const result = await view.api.stop(device);
      const outcome = result?.outcome || "unknown";
      view.api.notify(view.api.t("stop.result", { device: view.api.deviceName(device), outcome: view.api.t("outcome." + outcome) }), outcome === "confirmed" ? "info" : "warn");
      return result;
    });
  }

  function stopFurnace() { return stopDevice("furnace"); }
  function stopMicroscope() { return stopDevice("microscope"); }

  /** The DSH-01 gas-off route: every gas channel, no control needed, sent once. */
  function stopGas() {
    return act("stop-gas", async () => {
      const body = await view.api.consoleRequest("/api/dashboard/gas", { confirmed: true });
      view.api.notify(view.api.t("gas.stopResult"), "warn");
      return body;
    });
  }

  return {
    install, i18n, view,
    logic: { chartModel, sequenceParams, editorRows, focusFraction, modeKey, recordingId, objectiveIds, nudgeSteps, signed, caps, fmt },
    _internal: { mount, build, update, poll, show, hide, findRole },
  };
});
