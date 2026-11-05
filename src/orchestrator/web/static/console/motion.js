/* Motion workspace (DSH-05): the robot arm, the motion cameras and the stage.
 * Every device command goes through the console relay (api.relay / api.stop).
 * The page reads device state from <base>/api/console/ws/motion/state.
 */
(function (root) {
  "use strict";

  const STEPS = [0.5, 2, 10];
  const AXES = ["x", "y", "z", "r"];
  const JOG = [
    { id: "y+", axis: "y", sign: 1, label: "+Y" }, { id: "x-", axis: "x", sign: -1, label: "−X" },
    { id: "x+", axis: "x", sign: 1, label: "+X" }, { id: "y-", axis: "y", sign: -1, label: "−Y" },
    { id: "z+", axis: "z", sign: 1, label: "Z ↑" }, { id: "r+", axis: "r", sign: 1, label: "R +" },
    { id: "z-", axis: "z", sign: -1, label: "Z ↓" }, { id: "r-", axis: "r", sign: -1, label: "R −" },
  ];
  const CAMERAS = ["camera2", "camera4", "camera3"];
  // Keyboard jog: the robot service moves while it sees a key held (lease 0.4 s).
  const KEYS = "WASDQERFZX";
  const KEY_MAP = [["W", "S", "kb.reach"], ["A", "D", "kb.base"], ["R", "F", "kb.z"], ["Q", "E", "kb.yaw"], ["Z", "X", "kb.grip"]];
  const KEYS_MS = 150;
  const POLL_MS = 1000;
  const EVENTS_MS = 2000;
  const LOG_LIMIT = 40;

  // Pure functions. Tests load them without a DOM.

  /** The actual pose [x, y, z, r] or null when any value is unknown. */
  function actualPose(robot) {
    const values = Array.isArray(robot?.actual) ? robot.actual.slice(0, 4) : [];
    return values.length === 4 && values.every(value => typeof value === "number" && Number.isFinite(value)) ? values : null;
  }

  /** One jog: the actual pose plus one step on one axis. */
  function jogTarget(actual, axis, sign, step) {
    if (!Array.isArray(actual) || actual.length < 4 || !STEPS.includes(step) || (sign !== 1 && sign !== -1)) return null;
    const index = AXES.indexOf(axis);
    if (index < 0) return null;
    const target = {};
    AXES.forEach((name, i) => { target[name] = round3(actual[i] + (i === index ? sign * step : 0)); });
    return Object.values(target).every(Number.isFinite) ? target : null;
  }

  function round3(value) { return Math.round(value * 1000) / 1000; }

  /** Parse the 4 pose inputs. Return {x, y, z, r} or null. */
  function parsePose(values) {
    const target = {};
    for (const axis of AXES) {
      const text = String(values?.[axis] ?? "").trim();
      if (!/^[-+]?(\d+\.?\d*|\.\d+)$/.test(text)) return null;
      const number = Number(text);
      if (!Number.isFinite(number)) return null;
      target[axis] = number;
    }
    return target;
  }

  /** The execute speed cap from the robot's described schema, or null. */
  function speedCap(description) {
    const cap = description?.commands?.execute?.params?.properties?.speed?.maximum;
    return Number.isInteger(cap) && cap >= 1 ? cap : null;
  }

  function clampSpeed(value, cap) {
    const number = Math.round(Number(value));
    if (!Number.isFinite(number) || !cap) return 1;
    return Math.min(Math.max(number, 1), cap);
  }

  /** A whole pulse count from 1 to the stage limit, or null. */
  function parseCount(text, max) {
    const value = String(text ?? "").trim();
    if (!/^\d+$/.test(value)) return null;
    const number = Number(value);
    return number >= 1 && number <= max ? number : null;
  }

  /** The reference badge key and tone for one stage axis. */
  function axisReference(motor) {
    const switches = motor?.homing_available === true;
    if (motor?.automation_ready === true) return { key: switches ? "ref.homed" : "ref.zeroed", tone: "ok" };
    if (motor?.homed === null || motor?.homed === undefined) return { key: "ref.unavailable", tone: "off" };
    return { key: switches ? "ref.homeRequired" : "ref.zeroRequired", tone: "warn" };
  }

  function axisState(motor) {
    if (motor?.mode && motor.mode !== "idle") return { key: "axis.moving", tone: "info" };
    if (typeof motor?.cooldown === "number" && motor.cooldown > 0) return { key: "axis.cooldown", tone: "info" };
    return { key: "axis.idle", tone: "ok" };
  }

  /** Robot and stage events, newest first, merged with this page's own outcomes. */
  function activityRows(events, local, limit = LOG_LIMIT) {
    const rows = (Array.isArray(events) ? events : [])
      .filter(item => item && (item.service === "robot" || item.service === "stage"))
      .map(item => ({ ts: typeof item.ts_unix === "number" ? item.ts_unix : 0, src: item.service, level: item.level || "info", message: String(item.message ?? "") }));
    for (const item of local || []) rows.push(item);
    rows.sort((a, b) => b.ts - a.ts);
    return rows.slice(0, limit);
  }

  function clock(ts) {
    if (!ts) return "--:--:--";
    const date = new Date(ts * 1000);
    return [date.getHours(), date.getMinutes(), date.getSeconds()].map(value => String(value).padStart(2, "0")).join(":");
  }

  function fixed(value, digits = 3) {
    return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—";
  }

  /** A whole gripper position from 0 to 1000, or null. */
  function parseGrip(text) {
    const value = String(text ?? "").trim();
    if (!/^\d+$/.test(value)) return null;
    const number = Number(value);
    return number <= 1000 ? number : null;
  }

  /** The controller alarms as {id, text}: the description, the source and the solution. */
  function alarmRows(robot) {
    return (Array.isArray(robot?.alarms) ? robot.alarms : []).filter(item => item && typeof item === "object").map(item => {
      const source = item.source && item.source !== "controller" ? ` (${item.source})` : "";
      return { id: String(item.id ?? "?"), text: [String(item.description ?? "") + source, String(item.solution ?? "")].filter(Boolean).join(" ") };
    });
  }

  /** The keyboard speed cap from the robot's described schema, or null. */
  function keyboardCap(description) {
    const cap = description?.commands?.keyboard?.params?.properties?.speed?.maximum;
    return Number.isInteger(cap) && cap >= 1 ? cap : null;
  }

  /** The jog key of one keyboard event (W, A, …), or null. */
  function jogKey(event) {
    const code = String(event?.code || "");
    const key = code.startsWith("Key") ? code.slice(3) : String(event?.key || "").toUpperCase();
    return key.length === 1 && KEYS.includes(key) ? key : null;
  }

  const logic = { actualPose, jogTarget, parsePose, speedCap, keyboardCap, jogKey, clampSpeed, parseCount, axisReference, axisState, activityRows, clock, fixed, STEPS, CAMERAS };

  const i18n = {
    en: {
      "arm.title": "Robot arm · MG400", "stage.title": "Stage · 3 axes", "cams.title": "Motion cameras",
      "badge.demo": "DEMO", "badge.real": "REAL ROBOT", "badge.realStage": "REAL STAGE",
      "dev.unconfigured": "Not configured", "dev.offline": "Not running", "dev.unreachable": "Not reachable", "dev.loading": "Loading…",
      "arm.enabled": "Enabled", "arm.disabled": "Disabled", "arm.busy": "Moving", "arm.alarm": "Alarm",
      "arm.locked": "Stop not confirmed. Check the arm, then press Recover.", "arm.recover": "Recover", "arm.noFeedback": "No fresh feedback", "arm.cap": "speed cap {cap}",
      "ctl.take": "Take control", "ctl.release": "Release", "ctl.mine": "You control it", "ctl.free": "Free",
      "ctl.console": "Another console page controls it", "ctl.other": "Another client controls it",
      "ctl.takeLabel": "Take control of {device}", "ctl.releaseLabel": "Release {device}",
      "arm.enable": "Enable", "arm.disable": "Disable", "arm.clear": "Clear alarm",
      "arm.stop": "Stop arm", "arm.home": "Home…", "arm.homeConfirm": "Clear the path of the arm. Home moves all 4 joints to 0°.",
      "arm.alarmItem": "Alarm {id}",
      "jog.title": "Jog", "jog.step": "Jog step",
      "jog.move": "Move {label} by the jog step",
      "move.title": "Move to a pose", "move.speed": "Speed", "move.speedLabel": "Speed, 1 to {cap}",
      "move.useActual": "Use actual", "move.execute": "Execute ↗", "move.invalid": "Enter 4 numbers for X, Y, Z and R.",
      "kb.title": "Keyboard", "kb.speedLabel": "Keyboard speed, 1 to {cap}", "kb.off": "Off: {reason}",
      "kb.reach": "Reach out / in", "kb.base": "Base J1 + / −", "kb.z": "Z up / down", "kb.yaw": "Flange J4 + / −", "kb.grip": "Gripper open / close",
      "grip.title": "Gripper", "grip.open": "Open", "grip.close": "Close", "grip.init": "Initialize…",
      "grip.initConfirm": "Initialization opens the jaws. Remove any held object first.",
      "grip.state": "{position} / 1000 · {outcome}", "grip.busy": "Moving…", "grip.none": "No outcome yet",
      "grip.position": "Position", "grip.positionLabel": "Gripper position, 0 to 1000", "grip.move": "Move",
      "grip.badPosition": "Enter a whole gripper position from 0 to 1000.",
      "last": "Last: {action} · {result}", "last.message": "Last: {action} · {result} · {message}",
      "confirm": "Confirm", "cancel": "Cancel",
      "stage.stop": "Stop stage", "stage.connected": "Connected", "stage.disconnected": "Arduino not connected",
      "stage.stepping": "Stepping", "stage.steppingConfirm": "Set {mode}? This clears the zero and soft limits of all 3 axes.",
      "axis.bottom": "Bottom", "axis.top": "Top", "axis.insert": "Insert",
      "axis.idle": "Idle", "axis.moving": "Moving", "axis.cooldown": "Cooling down",
      "axis.pulses": "{n} pulses", "axis.mm": "{mm} mm",
      "ref.homed": "Homed", "ref.zeroed": "Zero confirmed", "ref.homeRequired": "Home required",
      "ref.zeroRequired": "Zero required", "ref.unavailable": "Reference unavailable",
      "axis.steps": "Steps", "axis.speed": "Pulses/s",
      "axis.minus": "Step −", "axis.plus": "Step +", "axis.minusLabel": "Move {axis} minus by the steps",
      "axis.plusLabel": "Move {axis} plus by the steps", "axis.badCount": "Enter whole steps from 1 to {max} and a speed from 1 to {speed}.",
      "axis.home": "Home…", "axis.homeConfirm": "Clear the travel path. {axis} moves to its reference switch.",
      "axis.check": "Check reference", "axis.zero": "Set zero…", "axis.teach": "Teach reference…",
      "axis.teachConfirm": "Is {axis} at its safe zero position? The current marker position replaces the camera reference.",
      "axis.offset": "Marker offset {x}, {y} px", "axis.checkFailed": "Reference check failed: {error}",
      "stage.port": "Serial port", "stage.connect": "Connect", "stage.noPorts": "No serial port found", "stage.refresh": "Refresh ports",
      "pose.title": "Named poses", "pose.needRef": "Home or zero all 3 axes before a named pose.",
      "axis.zeroConfirm": "Set the current position of {axis} as zero. Check the physical position first.",
      "named.title": "Named positions", "named.none": "No named positions are configured.",
      "named.go": "{axis} → {name}",
      "log.title": "Robot and stage activity", "log.none": "No activity yet.", "log.page": "this page",
      "out.accepted": "{device} {action}: sent", "out.refused": "{device} {action}: refused · {error}",
      "out.unknown": "{device} {action}: outcome unknown. Check the device. It is not retried.",
      "out.stop": "{device} stop: {outcome}",
      "cam.offline": "OFFLINE", "cam.swap": "Swap view", "cam.swapLabel": "Show {view} in the large view",
      "cam.camera2": "camera2 · Insert rail", "cam.camera4": "camera4 · Tray", "cam.camera3": "camera3 · Microscope side",
    },
    zh: {
      "arm.title": "机械臂 · MG400", "stage.title": "位移台 · 3 轴", "cams.title": "运动相机",
      "badge.demo": "演示", "badge.real": "真机", "badge.realStage": "真实位移台",
      "dev.unconfigured": "未配置", "dev.offline": "未运行", "dev.unreachable": "无法连接", "dev.loading": "加载中…",
      "arm.enabled": "已使能", "arm.disabled": "未使能", "arm.busy": "运动中", "arm.alarm": "报警",
      "arm.locked": "停止未确认。检查机械臂，然后按“恢复”。", "arm.recover": "恢复", "arm.noFeedback": "没有新反馈", "arm.cap": "速度上限 {cap}",
      "ctl.take": "获取控制权", "ctl.release": "释放", "ctl.mine": "你持有控制权", "ctl.free": "空闲",
      "ctl.console": "另一个控制台页面持有控制权", "ctl.other": "另一个客户端持有控制权",
      "ctl.takeLabel": "获取{device}的控制权", "ctl.releaseLabel": "释放{device}",
      "arm.enable": "使能", "arm.disable": "下使能", "arm.clear": "清除报警",
      "arm.stop": "停止机械臂", "arm.home": "回原点…", "arm.homeConfirm": "请清空机械臂的运动路径。回原点会把 4 个关节都转到 0°。",
      "arm.alarmItem": "报警 {id}",
      "jog.title": "点动", "jog.step": "点动步长",
      "jog.move": "按点动步长移动 {label}",
      "move.title": "移动到位姿", "move.speed": "速度", "move.speedLabel": "速度，1 到 {cap}",
      "move.useActual": "使用当前值", "move.execute": "执行 ↗", "move.invalid": "请为 X、Y、Z 和 R 输入 4 个数字。",
      "kb.title": "键盘", "kb.speedLabel": "键盘速度，1 到 {cap}", "kb.off": "已关闭：{reason}",
      "kb.reach": "伸出 / 收回", "kb.base": "底座 J1 + / −", "kb.z": "Z 上 / 下", "kb.yaw": "法兰 J4 + / −", "kb.grip": "夹爪张开 / 闭合",
      "grip.title": "夹爪", "grip.open": "张开", "grip.close": "闭合", "grip.init": "初始化…",
      "grip.initConfirm": "初始化会张开夹爪。请先取下夹持的物体。",
      "grip.state": "{position} / 1000 · {outcome}", "grip.busy": "运动中…", "grip.none": "尚无结果",
      "grip.position": "位置", "grip.positionLabel": "夹爪位置，0 到 1000", "grip.move": "移动",
      "grip.badPosition": "请输入 0 到 1000 的整数夹爪位置。",
      "last": "上一条：{action} · {result}", "last.message": "上一条：{action} · {result} · {message}",
      "confirm": "确认", "cancel": "取消",
      "stage.stop": "停止位移台", "stage.connected": "已连接", "stage.disconnected": "Arduino 未连接",
      "stage.stepping": "细分", "stage.steppingConfirm": "设为{mode}？这会清除全部 3 轴的零点和软限位。",
      "axis.bottom": "底部轴", "axis.top": "顶部轴", "axis.insert": "插入轴",
      "axis.idle": "空闲", "axis.moving": "运动中", "axis.cooldown": "冷却中",
      "axis.pulses": "{n} 脉冲", "axis.mm": "{mm} mm",
      "ref.homed": "已回零", "ref.zeroed": "零点已确认", "ref.homeRequired": "需要回零",
      "ref.zeroRequired": "需要设零", "ref.unavailable": "参考不可用",
      "axis.steps": "步数", "axis.speed": "脉冲/秒",
      "axis.minus": "步进 −", "axis.plus": "步进 +", "axis.minusLabel": "{axis}按步数负向移动",
      "axis.plusLabel": "{axis}按步数正向移动", "axis.badCount": "请输入 1 到 {max} 的整数步数和 1 到 {speed} 的速度。",
      "axis.home": "回零…", "axis.homeConfirm": "请清空行程。{axis}将移动到参考开关。",
      "axis.check": "检查参考", "axis.zero": "设零…", "axis.teach": "示教参考…",
      "axis.teachConfirm": "{axis}在安全零点吗？当前标记位置将替换摄像头参考。",
      "axis.offset": "标记偏移 {x}, {y} px", "axis.checkFailed": "参考检查失败：{error}",
      "stage.port": "串口", "stage.connect": "连接", "stage.noPorts": "未找到串口", "stage.refresh": "刷新串口",
      "pose.title": "命名位姿", "pose.needRef": "使用命名位姿前，先让全部 3 轴回零或设零。",
      "axis.zeroConfirm": "把{axis}的当前位置设为零点。请先检查实际位置。",
      "named.title": "命名位置", "named.none": "未配置命名位置。",
      "named.go": "{axis} → {name}",
      "log.title": "机械臂和位移台活动", "log.none": "暂无活动。", "log.page": "本页面",
      "out.accepted": "{device} {action}：已发送", "out.refused": "{device} {action}：被拒绝 · {error}",
      "out.unknown": "{device} {action}：结果未知。请检查设备。不会重试。",
      "out.stop": "{device} 停止：{outcome}",
      "cam.offline": "离线", "cam.swap": "切换视图", "cam.swapLabel": "在大视图中显示 {view}",
      "cam.camera2": "camera2 · 插入导轨", "cam.camera4": "camera4 · 托盘", "cam.camera3": "camera3 · 显微镜侧视",
    },
  };

  Object.assign(logic, { parseGrip, alarmRows });

  function workspace() {
    let api = null, rootNode = null, timer = null, eventsTimer = null, shown = false;
    const data = { robot: null, stage: null, events: [], cap: null, kbCap: null, loaded: false };
    // on: this page armed the keyboard. seen: the service reported it on or arming since then.
    const kb = { on: false, seen: false, speed: null, held: new Set(), seq: 0, timer: null, pending: 0 };
    const model = {
      step: 2, speed: null, pose: { x: "", y: "", z: "", r: "" }, cams: CAMERAS.slice(),
      axis: {}, local: [], confirm: null, inflight: new Set(), describing: false,
      ports: null, port: "", portsLoading: false, refView: {},
    };
    let refs = {};

    const t = (key, vars) => api.t(key, vars);
    const el = (tag, props, ...children) => api.el(tag, props, ...children);

    function note(src, level, message) {
      model.local.push({ ts: Date.now() / 1000, src, level, message, page: true });
      if (model.local.length > LOG_LIMIT) model.local.shift();
      renderLog();
    }

    /** One robot execute (jog or move). Sent once; never retried. */
    async function checked(path, body) {
      try {
        const result = await api.request(path, body, { accept: [409, 502], timeout: 8000 });
        if (result.outcome === "unknown") api.notify(t("out.unknown", { device: api.deviceName("robot"), action: "execute" }), "error");
        return result.outcome ? result : { outcome: "refused", error: result.error };
      } catch (error) {
        if (error.status === 0) return { outcome: "unknown", error: error.message };
        return { outcome: "refused", error: error.message };
      }
    }

    async function send(device, action, params, key = action, path = null) {
      if (model.inflight.has(device + ":" + key)) return null;
      model.inflight.add(device + ":" + key);
      update();
      try {
        const result = path ? await checked(path, params) : await api.relay(device, action, params);
        const vars = { device: api.deviceName(device), action, error: result?.error || "" };
        if (result?.outcome === "accepted") note(device, "info", t("out.accepted", vars));
        else if (result?.outcome === "refused") note(device, "warning", t("out.refused", vars));
        else note(device, "error", t("out.unknown", vars));
        return result;
      } finally {
        model.inflight.delete(device + ":" + key);
        refresh();
      }
    }

    async function stopDevice(device) {
      const result = await api.stop(device);
      note(device, result?.outcome === "confirmed" ? "info" : "warning", t("out.stop", { device: api.deviceName(device), outcome: result?.outcome || "unknown" }));
      refresh();
    }

    async function control(device) {
      try {
        if (api.owns(device)) await api.release(device);
        else await api.take(device);
      } catch (error) {
        api.notify(error.message, "warn");
      }
      refresh();
      if (device === "robot") describeRobot();
    }

    async function describeRobot() {
      if (model.describing || data.cap) return;
      model.describing = true;
      try {
        const description = await api.describe("robot");
        data.cap = speedCap(description);
        data.kbCap = keyboardCap(description);
        if (data.cap) model.speed = clampSpeed(model.speed || Math.min(10, data.cap), data.cap);
        if (data.kbCap) kb.speed = clampSpeed(kb.speed || Math.min(10, data.kbCap), data.kbCap);
      } catch { /* The robot may be offline. Try again on the next state. */ }
      finally { model.describing = false; }
      build();
    }

    async function refresh() {
      try {
        const body = await api.request("state");
        data.robot = body.robot || null;
        data.stage = body.stage || null;
      } catch {
        data.robot = data.stage = { available: false, reason: "unreachable" };
      }
      data.loaded = true;
      if (!data.cap && data.robot?.available) describeRobot();
      if (stageKey() !== refs.stageKey) buildStage();
      update();
    }

    async function refreshEvents() {
      try {
        const body = await api.consoleRequest("/api/console/events");
        data.events = Array.isArray(body.events) ? body.events : [];
      } catch { /* Keep the last list. */ }
      renderLog();
    }

    function askConfirm(slot, text, run) {
      model.confirm = { slot, text, run };
      renderConfirms();
    }

    function renderConfirms() {
      for (const [slot, node] of Object.entries(refs.confirms || {})) {
        const item = model.confirm?.slot === slot ? model.confirm : null;
        if (!item) { node.replaceChildren(); node.hidden = true; continue; }
        node.hidden = false;
        node.replaceChildren(
          el("p", { text: item.text }),
          el("div", { class: "mo-grid2" },
            el("button", { type: "button", class: "lc-button mo-danger", text: t("confirm"), onclick: () => { model.confirm = null; renderConfirms(); item.run(); } }),
            el("button", { type: "button", class: "lc-button", text: t("cancel"), onclick: () => { model.confirm = null; renderConfirms(); } })));
      }
    }

    function confirmSlot(name) {
      const node = el("div", { class: "lc-confirm", role: "alertdialog", hidden: true });
      refs.confirms[name] = node;
      return node;
    }

    function controlButton(device) {
      const button = el("button", { type: "button", class: "lc-button", onclick: () => control(device) });
      const status = el("span", { class: "lc-muted" });
      refs["ctl-" + device] = { button, status };
      return el("div", { class: "mo-row" }, button, status);
    }

    // Arm column ------------------------------------------------------------

    function buildArm() {
      const title = el("h2", { id: "mo-arm-title", text: t("arm.title") });
      refs.armStatus = el("span", { class: "lc-muted" });
      refs.armBadge = el("span", { class: "mo-badge" });
      refs.enable = el("button", { type: "button", class: "lc-button mo-primary", text: t("arm.enable"), onclick: () => send("robot", "enable", {}) });
      refs.disable = el("button", { type: "button", class: "lc-button", text: t("arm.disable"), onclick: () => send("robot", "disable", {}) });
      refs.clear = el("button", { type: "button", class: "lc-button", text: t("arm.clear"), onclick: () => send("robot", "clear_alarm", {}) });
      refs.home = el("button", {
        type: "button", class: "lc-button", text: t("arm.home"),
        onclick: () => askConfirm("home", t("arm.homeConfirm"), () => send("robot", "home", {}, "move")),
      });
      refs.armStop = el("button", { type: "button", class: "lc-danger mo-stop", text: t("arm.stop"), onclick: () => stopDevice("robot") });
      refs.alarms = el("div", { class: "mo-stack", role: "alert", hidden: true });
      refs.armMessage = el("p", { class: "lc-muted", role: "status", hidden: true });
      refs.joints = [0, 1, 2, 3].map(() => el("span", { class: "mo-value" }));
      const jointTiles = el("div", { class: "mo-pose" },
        ...refs.joints.map((node, i) => el("div", { class: "mo-tile" }, el("span", { class: "lc-muted", text: "J" + (i + 1) }), node)));
      refs.pose = AXES.map(axis => el("span", { class: "mo-value" }));
      const poseTiles = el("div", { class: "mo-pose", role: "group", "aria-label": t("move.useActual") },
        ...AXES.map((axis, i) => el("div", { class: "mo-tile" }, el("span", { class: "lc-muted", text: axis.toUpperCase() }), refs.pose[i])));
      refs.jog = {};
      const jogButton = item => {
        const button = el("button", { type: "button", class: "mo-jog", text: item.label, "aria-label": t("jog.move", { label: item.label }), onclick: () => jog(item) });
        refs.jog[item.id] = button;
        return button;
      };
      const pad = el("div", { class: "mo-pad" },
        el("span"), jogButton(JOG[0]), el("span"),
        jogButton(JOG[1]), el("span", { class: "mo-pad-mid", text: "XY" }), jogButton(JOG[2]),
        el("span"), jogButton(JOG[3]), el("span"));
      const side = el("div", { class: "mo-zr" }, ...JOG.slice(4).map(jogButton));
      refs.steps = STEPS.map(step => el("button", {
        type: "button", role: "radio", class: "mo-chip", text: `${step} mm`,
        onclick: () => { model.step = step; update(); },
      }));
      const jogBox = el("div", { class: "mo-block" },
        el("div", { class: "mo-head" }, el("h3", { text: t("jog.title") })),
        el("div", { class: "mo-jogrow" }, pad, side),
        el("div", { class: "mo-chips", role: "radiogroup", "aria-label": t("jog.step") }, ...refs.steps));

      refs.inputs = {};
      const units = { x: "X mm", y: "Y mm", z: "Z mm", r: "R °" };
      const fields = AXES.map(axis => {
        const input = el("input", { type: "text", inputmode: "decimal", class: "mo-input", autocomplete: "off", oninput: event => { model.pose[axis] = event.target.value; } });
        input.value = model.pose[axis];
        refs.inputs[axis] = input;
        return el("label", { class: "mo-field" }, units[axis], input);
      });
      refs.speed = el("input", {
        type: "range", min: "1", max: String(data.cap || 1), step: "1",
        "aria-label": t("move.speedLabel", { cap: data.cap || 1 }),
        oninput: event => { model.speed = clampSpeed(event.target.value, data.cap); update(); },
      });
      refs.speed.value = String(clampSpeed(model.speed, data.cap));
      refs.speedText = el("span", { class: "mo-value" });
      refs.useActual = el("button", { type: "button", class: "lc-button", text: t("move.useActual"), onclick: useActual });
      refs.execute = el("button", { type: "button", class: "lc-button mo-primary", text: t("move.execute"), onclick: executePose });
      const moveBox = el("div", { class: "mo-block" },
        el("h3", { text: t("move.title") }),
        el("div", { class: "mo-pose" }, ...fields),
        el("label", { class: "mo-speed" }, t("move.speed"), refs.speed, refs.speedText),
        el("div", { class: "mo-grid2" }, refs.useActual, refs.execute));

      refs.gripState = el("span", { class: "mo-grip-state" });
      refs.gripOpen = el("button", { type: "button", class: "lc-button", text: t("grip.open"), onclick: () => send("robot", "grip", { position: 1000 }, "grip") });
      refs.gripClose = el("button", { type: "button", class: "lc-button", text: t("grip.close"), onclick: () => send("robot", "grip", { position: 0 }, "grip") });
      refs.gripInit = el("button", {
        type: "button", class: "lc-button", text: t("grip.init"),
        onclick: () => askConfirm("arm", t("grip.initConfirm"), () => send("robot", "initialize_gripper", {}, "grip")),
      });
      refs.kbSwitch = el("button", { type: "button", role: "switch", class: "mo-chip", text: t("kb.title"), onclick: () => (kb.on ? disarm("user") : arm()) });
      refs.kbSpeed = el("input", {
        type: "range", min: "1", max: String(data.kbCap || 1), step: "1",
        "aria-label": t("kb.speedLabel", { cap: data.kbCap || 1 }),
        oninput: event => { kb.speed = clampSpeed(event.target.value, data.kbCap); update(); },
      });
      refs.kbSpeed.value = String(clampSpeed(kb.speed, data.kbCap));
      refs.kbSpeedText = el("span", { class: "mo-value" });
      refs.kbOff = el("p", { class: "mo-kb-off", role: "status" });
      refs.kbKeys = {};
      const keyCap = key => (refs.kbKeys[key] = el("kbd", { class: "mo-key", text: key }));
      const kbBox = el("div", { class: "mo-block" },
        el("div", { class: "mo-head" }, el("h3", { text: t("kb.title") }), refs.kbSwitch),
        refs.kbOff,
        el("label", { class: "mo-speed" }, t("move.speed"), refs.kbSpeed, refs.kbSpeedText),
        el("div", { class: "mo-keys" }, ...KEY_MAP.map(([a, b, label]) => el("div", { class: "mo-keyrow" },
          keyCap(a), keyCap(b), el("span", { class: "lc-muted", text: t(label) })))));

      refs.gripInput = el("input", {
        type: "text", inputmode: "numeric", class: "mo-input mo-narrow", autocomplete: "off", "aria-label": t("grip.positionLabel"),
        oninput: event => { model.grip = event.target.value; },
      });
      refs.gripInput.value = model.grip ?? "";
      refs.gripMove = el("button", { type: "button", class: "lc-button", text: t("grip.move"), onclick: moveGrip });
      refs.gripMessage = el("p", { class: "lc-muted" });
      const gripBox = el("div", { class: "mo-block" },
        el("div", { class: "mo-head" }, el("h3", { text: t("grip.title") }), refs.gripState),
        el("div", { class: "mo-grid3" }, refs.gripOpen, refs.gripClose, refs.gripInit),
        el("div", { class: "mo-row" }, el("label", { class: "mo-inline" }, t("grip.position"), refs.gripInput), refs.gripMove),
        refs.gripMessage);
      refs.armLast = el("p", { class: "lc-muted", role: "status" });

      return el("section", { class: "lc-card mo-col", "aria-labelledby": "mo-arm-title" },
        el("div", { class: "lc-card-head" }, el("div", { class: "mo-titles" }, title, refs.armStatus), el("div", { class: "mo-row" }, refs.armBadge, refs.armStop)),
        controlButton("robot"),
        el("div", { class: "mo-grid2" }, refs.enable, refs.disable),
        el("div", { class: "mo-grid2" }, refs.clear, refs.home),
        confirmSlot("home"), refs.alarms, refs.armMessage,
        poseTiles, jointTiles, jogBox, kbBox, moveBox, gripBox, confirmSlot("arm"), refs.armLast);
    }

    function moveGrip() {
      const position = parseGrip(model.grip);
      if (position === null) { api.notify(t("grip.badPosition"), "warn"); return; }
      send("robot", "grip", { position }, "grip");
    }

    function jog(item) {
      if (!jogTarget(actualPose(data.robot), item.axis, item.sign, model.step)) return;
      send("robot", "jog " + item.label, { axis: item.axis, direction: item.sign, step: model.step, speed: clampSpeed(model.speed, data.cap) }, "move", "jog");
    }

    // Keyboard jog ----------------------------------------------------------

    function focused() {
      const doc = root.document;
      return shown && (!doc || ((doc.visibilityState || "visible") === "visible" && (typeof doc.hasFocus !== "function" || doc.hasFocus())));
    }

    /** Arm the keyboard through the relay with this page's control. */
    async function arm() {
      if (kb.on || !api.owns("robot") || !data.kbCap) return;
      releaseKeys();
      kb.on = true;
      kb.seen = false;
      const result = await send("robot", "keyboard", { enabled: true, seq: ++kb.seq, speed: clampSpeed(kb.speed, data.kbCap) }, "keyboard");
      if (result?.outcome !== "accepted" && kb.on) { kb.on = false; releaseKeys(); update(); }
    }

    /** Release every key, then disarm once. Without control the service already turned keys off. */
    function disarm(reason) {
      releaseKeys();
      if (!kb.on) return;
      kb.on = kb.seen = false;
      if (api.owns("robot")) send("robot", "keyboard", { enabled: false, seq: ++kb.seq, reason }, "keyboard-off");
      update();
    }

    /** Send the held set once. The robot service validates it and never retries. */
    async function sendKeys() {
      kb.pending++;
      let result = null;
      try { result = await api.request("keys", { seq: ++kb.seq, held: [...kb.held] }, { accept: [409, 502], timeout: 2000 }); }
      catch (error) { result = { outcome: error.status === 0 ? "unknown" : "refused", error: error.message }; }
      finally { kb.pending--; }
      if (result?.outcome !== "accepted" && kb.held.size) {
        note("robot", "warning", t(result?.outcome === "unknown" ? "out.unknown" : "out.refused", { device: api.deviceName("robot"), action: "keys", error: result?.error || "" }));
        releaseKeys();
      }
    }

    function releaseKeys() {
      if (kb.timer) clearInterval(kb.timer);
      kb.timer = null;
      if (!kb.held.size) return;
      kb.held.clear();
      sendKeys();
      renderKeys();
    }

    function keysChanged() {
      sendKeys();
      if (!kb.held.size && kb.timer) { clearInterval(kb.timer); kb.timer = null; }
      if (kb.held.size && !kb.timer) {
        kb.timer = setInterval(() => {
          if (!focused()) disarm("focus");
          else if (kb.pending < 2) sendKeys();
        }, KEYS_MS);
      }
      renderKeys();
    }

    function onKeyDown(event) {
      if (!kb.on || event.ctrlKey || event.metaKey || event.altKey) return;
      const key = jogKey(event);
      const tag = String(event.target?.tagName || "").toUpperCase();
      if (!key || tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      event.preventDefault?.();
      if (event.repeat || kb.held.has(key) || !api.owns("robot") || !focused()) return;
      kb.held.add(key);
      keysChanged();
    }

    function onKeyUp(event) {
      const key = jogKey(event);
      if (key && kb.held.delete(key)) keysChanged();
    }

    function renderKeys() {
      for (const [key, node] of Object.entries(refs.kbKeys || {})) node.className = "mo-key" + (kb.held.has(key) ? " mo-on" : "");
    }

    function updateKeyboard(robot, ready) {
      if (kb.on && !api.owns("robot")) { kb.on = kb.seen = false; releaseKeys(); }
      if (kb.on && robot && (robot.keyboard_enabled || robot.keyboard_arming)) kb.seen = true;
      // The service turned keys off (Stop, a button command, an alarm): follow it.
      else if (kb.on && kb.seen && robot && !model.inflight.has("robot:keyboard")) { kb.on = kb.seen = false; releaseKeys(); }
      refs.kbSwitch.setAttribute("aria-checked", kb.on ? "true" : "false");
      refs.kbSwitch.className = "mo-chip" + (kb.on ? " mo-on" : "");
      refs.kbSwitch.disabled = !kb.on && !(ready && robot.enabled === true && !robot.busy && data.kbCap && !model.inflight.has("robot:keyboard-off"));
      refs.kbSpeed.disabled = kb.on || !data.kbCap;
      refs.kbSpeedText.textContent = data.kbCap ? `${clampSpeed(kb.speed, data.kbCap)} / ${data.kbCap}` : "—";
      // The robot service says why it turned keys off (for example, late key updates).
      const reason = !kb.on && typeof robot?.keyboard_disarm_reason === "string" ? robot.keyboard_disarm_reason : "";
      refs.kbOff.textContent = reason ? t("kb.off", { reason }) : "";
      refs.kbOff.hidden = !reason;
      renderKeys();
    }

    function useActual() {
      const pose = actualPose(data.robot);
      if (!pose) return;
      AXES.forEach((axis, i) => { model.pose[axis] = pose[i].toFixed(3); refs.inputs[axis].value = model.pose[axis]; });
    }

    function executePose() {
      const target = parsePose(model.pose);
      if (!target) { api.notify(t("move.invalid"), "warn"); return; }
      send("robot", "execute", { targets: target, speed: clampSpeed(model.speed, data.cap) }, "move", "move");
    }

    // Cameras column --------------------------------------------------------

    function tile(view, large) {
      const factory = root.LabCameras && typeof root.LabCameras.tile === "function" ? root.LabCameras.tile : null;
      let node = null;
      if (factory) {
        try { node = factory(view, large ? { api, large } : { api, large, onSelect: () => swap(view) }); } catch { node = null; }
      }
      if (!node) {
        node = el("div", { class: "mo-cam-off", role: "img", "aria-label": t("cam." + view) + " · " + t("cam.offline") },
          el("span", { class: "mo-cam-badge" }, el("span", { class: "mo-off", text: "● " + t("cam.offline") }), " " + t("cam." + view)));
      }
      const wrap = el("div", { class: large ? "mo-cam mo-cam-large" : "mo-cam" }, node);
      if (!large) {
        wrap.append(el("button", {
          type: "button", class: "lc-button mo-swap", text: t("cam.swap"), "aria-label": t("cam.swapLabel", { view }),
          onclick: () => swap(view),
        }));
      }
      return wrap;
    }

    function dropTiles() {
      for (const node of refs.tiles || []) {
        const inner = node.firstChild || node.children?.[0];
        for (const target of [inner, node]) {
          if (target && typeof target.destroy === "function") { try { target.destroy(); } catch { /* ignore */ } }
        }
      }
      refs.tiles = [];
    }

    function renderCameras() {
      if (!refs.cams) return;
      dropTiles();
      const [main, ...small] = model.cams;
      const large = tile(main, true);
      const others = small.map(view => tile(view, false));
      refs.tiles = [large, ...others];
      refs.cams.replaceChildren(large, el("div", { class: "mo-cam-grid" }, ...others));
    }

    function swap(view) {
      const index = model.cams.indexOf(view);
      if (index <= 0) return;
      [model.cams[0], model.cams[index]] = [model.cams[index], model.cams[0]];
      renderCameras();
    }

    // Stage column ----------------------------------------------------------

    function stageKey() {
      const motors = data.stage?.available ? data.stage.motors || [] : [];
      const modes = data.stage?.microstep_modes || [];
      return JSON.stringify([motors.map(m => [m.id, m.named_positions || [], m.homing_available === true]), modes.map(m => m.value), data.stage?.poses || []]);
    }

    function buildStage() {
      refs.stageKey = stageKey();
      if (!refs.stageBody) return;
      buildPoses();
      const stage = data.stage?.available ? data.stage : null;
      const motors = stage?.motors || [];
      refs.modes = (stage?.microstep_modes || []).map(mode => el("button", {
        type: "button", role: "radio", class: "mo-chip", text: mode.label, "data-value": String(mode.value),
        onclick: () => {
          if (stage && mode.value === data.stage?.microstep) return;
          askConfirm("stage", t("stage.steppingConfirm", { mode: mode.label }), () => send("stage", "set_microstep", { microstep: mode.value }, "microstep"));
        },
      }));
      refs.axes = {};
      const blocks = motors.map(motor => {
        const id = motor.id;
        const axis = t("axis." + id);
        model.axis[id] = model.axis[id] || { steps: String(stage?.defaults?.steps || 50), speed: String(stage?.defaults?.speed || 50) };
        const row = {};
        row.state = el("span", { class: "lc-state" });
        row.pulses = el("span");
        row.mm = el("span", { class: "lc-muted" });
        row.ref = el("span", { class: "lc-strong" });
        const input = (field, label) => {
          const node = el("input", { type: "text", inputmode: "numeric", class: "mo-input mo-narrow", autocomplete: "off", oninput: event => { model.axis[id][field] = event.target.value; } });
          node.value = model.axis[id][field];
          return el("label", { class: "mo-inline" }, label, node);
        };
        const move = direction => () => {
          const limits = data.stage?.limits || {};
          const steps = parseCount(model.axis[id].steps, limits.steps || 100000);
          const speed = parseCount(model.axis[id].speed, limits.speed || 1000);
          if (!steps || !speed) { api.notify(t("axis.badCount", { max: limits.steps || 100000, speed: limits.speed || 1000 }), "warn"); return; }
          send("stage", "move", { motor: id, direction, speed, steps }, "move-" + id);
        };
        row.minus = el("button", { type: "button", class: "lc-button", text: t("axis.minus"), "aria-label": t("axis.minusLabel", { axis }), onclick: move("ccw") });
        row.plus = el("button", { type: "button", class: "lc-button", text: t("axis.plus"), "aria-label": t("axis.plusLabel", { axis }), onclick: move("cw") });
        row.home = motor.homing_available ? el("button", {
          type: "button", class: "lc-button mo-small", text: t("axis.home"),
          onclick: () => askConfirm("stage", t("axis.homeConfirm", { axis }), () => send("stage", "home", { motor: id }, "ref-" + id)),
        }) : null;
        row.check = el("button", { type: "button", class: "lc-button mo-small", text: t("axis.check"), onclick: () => checkReference(id) });
        row.teach = el("button", {
          type: "button", class: "lc-button mo-small", text: t("axis.teach"),
          onclick: () => askConfirm("stage", t("axis.teachConfirm", { axis }), () => send("stage", "teach_reference", { motor: id, confirmed: true }, "ref-" + id, "stage/teach")),
        });
        row.view = el("div", { class: "mo-ref", hidden: true });
        row.zero = el("button", {
          type: "button", class: "lc-button mo-small", text: t("axis.zero"),
          onclick: () => askConfirm("stage", t("axis.zeroConfirm", { axis }), () => send("stage", "zero", { motor: id, confirm: true }, "ref-" + id)),
        });
        refs.axes[id] = row;
        return el("div", { class: "mo-axis", role: "group", "aria-label": axis },
          el("div", { class: "mo-head" }, el("span", { class: "lc-strong", text: axis }), row.state),
          el("div", { class: "mo-readout" }, row.pulses, row.mm, row.ref),
          el("div", { class: "mo-grid2" }, row.minus, row.plus),
          el("div", { class: "mo-row" }, input("steps", t("axis.steps")), input("speed", t("axis.speed"))),
          el("div", { class: "mo-row" }, row.home, row.zero, row.check, row.teach),
          row.view);
      });
      refs.named = [];
      const named = [];
      for (const motor of motors) {
        for (const name of motor.named_positions || []) {
          const button = el("button", {
            type: "button", class: "lc-button", text: t("named.go", { axis: t("axis." + motor.id), name }),
            onclick: () => send("stage", "goto", { motor: motor.id, name }, "move-" + motor.id),
          });
          refs.named.push({ button, motor: motor.id, name });
          named.push(button);
        }
      }
      refs.stageBody.replaceChildren(
        refs.modes.length ? el("div", { class: "mo-chips", role: "radiogroup", "aria-label": t("stage.stepping") },
          el("span", { class: "lc-muted", text: t("stage.stepping") }), ...refs.modes) : "",
        ...blocks,
        stage ? el("div", { class: "mo-block" }, el("h3", { text: t("named.title") }),
          named.length ? el("div", { class: "mo-grid2" }, ...named) : el("p", { class: "lc-muted", text: t("named.none") })) : "");
    }

    function buildStageCard() {
      refs.stageStatus = el("span", { class: "lc-muted" });
      refs.stageBadge = el("span", { class: "mo-badge" });
      refs.stageStop = el("button", { type: "button", class: "lc-danger mo-stop", text: t("stage.stop"), onclick: () => stopDevice("stage") });
      refs.stageBody = el("div", { class: "mo-stack" });
      refs.portSelect = el("select", { class: "mo-input", "aria-label": t("stage.port"), onchange: event => { model.port = event.target.value; } });
      refs.connect = el("button", { type: "button", class: "lc-button", text: t("stage.connect"), onclick: () => send("stage", "connect", { port: model.port }, "connect", "stage/connect").then(() => { model.ports = null; }) });
      refs.refreshPorts = el("button", { type: "button", class: "lc-button", text: t("stage.refresh"), onclick: () => { if (!model.portsLoading) loadPorts(); } });
      refs.connectRow = el("div", { class: "mo-row", hidden: true }, el("label", { class: "mo-inline" }, t("stage.port"), refs.portSelect), refs.connect, refs.refreshPorts);
      refs.stageLast = el("p", { class: "lc-muted", role: "status" });
      refs.poseBlock = el("div", { class: "mo-block", hidden: true });
      const card = el("section", { class: "lc-card mo-col", "aria-labelledby": "mo-stage-title" },
        el("div", { class: "lc-card-head" },
          el("div", { class: "mo-titles" }, el("h2", { id: "mo-stage-title", text: t("stage.title") }), refs.stageStatus),
          el("div", { class: "mo-row" }, refs.stageBadge, refs.stageStop)),
        controlButton("stage"),
        confirmSlot("stage"),
        refs.connectRow,
        refs.stageBody,
        refs.poseBlock,
        refs.stageLast);
      buildStage();
      return card;
    }

    /** Named poses move several axes in the configured order with the described `pose` command. */
    function buildPoses() {
      if (!refs.poseBlock) return;
      const poses = data.stage?.available && Array.isArray(data.stage.poses) ? data.stage.poses : [];
      refs.poses = poses.map(name => ({ name, button: el("button", { type: "button", class: "lc-button", text: name, onclick: () => send("stage", "pose", { name }, "pose") }) }));
      refs.poseNote = el("p", { class: "lc-muted" });
      refs.poseBlock.hidden = !poses.length;
      refs.poseBlock.replaceChildren(...(poses.length ? [el("h3", { text: t("pose.title") }), el("div", { class: "mo-grid2" }, ...refs.poses.map(item => item.button)), refs.poseNote] : []));
    }

    // Activity --------------------------------------------------------------

    function renderLog() {
      if (!refs.log) return;
      const rows = activityRows(data.events, model.local);
      refs.log.replaceChildren(...(rows.length ? rows.map(row => el("div", { class: "lc-event level-" + row.level },
        el("span", { class: "lc-mono", text: clock(row.ts) }),
        el("span", { class: "lc-event-src", text: api.deviceName(row.src) + (row.page ? " · " + t("log.page") : "") }),
        el("span", { class: "mo-msg", text: row.message }))) : [el("p", { class: "lc-muted", text: t("log.none") })]));
    }

    // Whole workspace -------------------------------------------------------

    function build() {
      if (!rootNode) return;
      dropTiles();
      refs = { confirms: {}, tiles: [] };
      refs.cams = el("div", { class: "mo-cams" });
      refs.log = el("div", { class: "lc-events" });
      rootNode.replaceChildren(
        el("div", { class: "mo-columns" },
          buildArm(),
          el("section", { class: "mo-col mo-col-cams", "aria-label": t("cams.title") }, refs.cams),
          buildStageCard()),
        el("section", { class: "lc-card", "aria-labelledby": "mo-log-title" },
          el("h2", { id: "mo-log-title", text: t("log.title") }), refs.log));
      renderCameras();
      renderConfirms();
      renderLog();
      update();
    }

    function deviceStatus(device) {
      if (!data.loaded) return t("dev.loading");
      if (!device?.available) return t("dev." + (device?.reason || "unreachable"));
      return null;
    }

    function holderText(device, info) {
      if (api.owns(device)) return t("ctl.mine");
      if (info?.holder === "console") return t("ctl.console");
      if (info?.holder === "other" || info?.holder === "mine") return t("ctl.other");
      return t("ctl.free");
    }

    function lastText(command) {
      if (!command?.action) return "";
      const vars = { action: command.action, result: command.result || "…", message: command.message || "" };
      return t(command.message && command.result !== "ok" ? "last.message" : "last", vars);
    }

    function updateControl(device, info) {
      const item = refs["ctl-" + device];
      if (!item) return;
      const mine = api.owns(device);
      item.button.textContent = mine ? t("ctl.release") : t("ctl.take");
      item.button.setAttribute("aria-label", t(mine ? "ctl.releaseLabel" : "ctl.takeLabel", { device: api.deviceName(device) }));
      // A demo device starts on its first Take control, so "offline" stays clickable.
      item.button.disabled = !mine && (info?.reason === "unconfigured" || Boolean(info?.available && info.holder && info.holder !== "mine"));
      item.status.textContent = info?.available ? holderText(device, info) : "";
    }

    function update() {
      if (!rootNode || !refs.enable) return;
      const robot = data.robot?.available ? data.robot : null;
      const stage = data.stage?.available ? data.stage : null;
      const ownsRobot = api.owns("robot");
      const ownsStage = api.owns("stage");
      const busy = device => [...model.inflight].some(key => key.startsWith(device + ":"));

      // Arm.
      const pose = actualPose(robot);
      const parts = [];
      const missing = deviceStatus(data.robot);
      if (missing) parts.push(missing);
      else {
        parts.push(t(robot.enabled ? "arm.enabled" : "arm.disabled"));
        if (robot.busy) parts.push(t("arm.busy"));
        if (robot.robot_error || (robot.alarms || []).length) parts.push(t("arm.alarm"));
        if (robot.locked || robot.stop_unconfirmed) parts.push(t("arm.locked"));
        if (!robot.feedback_fresh) parts.push(t("arm.noFeedback"));
        if (data.cap) parts.push(t("arm.cap", { cap: data.cap }));
      }
      refs.armStatus.textContent = parts.join(" · ");
      refs.armBadge.textContent = robot ? t(robot.demo ? "badge.demo" : "badge.real") : "";
      refs.armBadge.className = "mo-badge" + (robot && !robot.demo ? " mo-real" : "");
      refs.armBadge.hidden = !robot;
      updateControl("robot", data.robot);
      const ready = Boolean(robot && ownsRobot && robot.feedback_fresh && !robot.locked && !robot.stop_unconfirmed);
      refs.enable.disabled = !ready || robot.enabled === true || busy("robot");
      refs.disable.disabled = !robot;
      refs.clear.disabled = !(robot && ownsRobot);
      refs.clear.textContent = t(robot && (robot.locked || robot.stop_unconfirmed) ? "arm.recover" : "arm.clear");
      refs.home.disabled = !(ready && robot.enabled === true && !robot.busy) || model.inflight.has("robot:move");
      // Change live regions only when the text changes, so a screen reader reads it once.
      const alarms = alarmRows(robot).map(item => t("arm.alarmItem", { id: item.id }) + ": " + item.text);
      if (alarms.join("\n") !== refs.alarmText) {
        refs.alarmText = alarms.join("\n");
        refs.alarms.replaceChildren(...alarms.map(text => el("p", { class: "tone-text-bad", text })));
      }
      refs.alarms.hidden = !alarms.length;
      const message = robot?.message ? String(robot.message) : "";
      if (refs.armMessage.textContent !== message) refs.armMessage.textContent = message;
      refs.armMessage.hidden = !message;
      const gripMessage = robot?.gripper_message ? String(robot.gripper_message) : "";
      if (refs.gripMessage.textContent !== gripMessage) refs.gripMessage.textContent = gripMessage;
      const joints = Array.isArray(robot?.joints) ? robot.joints : [];
      refs.joints.forEach((node, i) => { node.textContent = typeof joints[i] === "number" ? fixed(joints[i], 1) : "—"; });
      AXES.forEach((axis, i) => { refs.pose[i].textContent = pose ? fixed(pose[i], 2) : "—"; });
      const canMove = ready && robot.enabled === true && !robot.busy && Boolean(pose) && !model.inflight.has("robot:move");
      for (const node of Object.values(refs.jog)) node.disabled = !canMove;
      refs.steps.forEach((node, i) => {
        const on = STEPS[i] === model.step;
        node.setAttribute("aria-checked", on ? "true" : "false");
        node.className = "mo-chip" + (on ? " mo-on" : "");
      });
      refs.speed.disabled = !data.cap;
      refs.speedText.textContent = data.cap ? `${clampSpeed(model.speed, data.cap)} / ${data.cap}` : "—";
      refs.useActual.disabled = !pose;
      refs.execute.disabled = !canMove || !data.cap;
      const grip = robot?.gripper || {};
      const position = Array.isArray(robot?.actual) ? robot.actual[4] : null;
      refs.gripState.textContent = !robot ? "" : robot.gripper_busy ? t("grip.busy")
        : t("grip.state", { position: typeof position === "number" ? position : "—", outcome: grip.outcome || t("grip.none") });
      refs.gripState.className = "mo-grip-state" + (grip.outcome === "dropped" ? " tone-text-bad" : grip.outcome ? " tone-text-ok" : "");
      const gripReady = ready && robot.gripper_available && !robot.gripper_busy && !robot.busy && !busy("robot");
      refs.gripOpen.disabled = refs.gripClose.disabled = refs.gripMove.disabled = !gripReady;
      refs.gripInit.disabled = !(ready && robot.gripper_can_initialize && !robot.gripper_busy && !robot.busy);
      refs.armLast.textContent = robot ? lastText(robot.last_command) : "";
      updateKeyboard(robot, ready);

      // Stage.
      const stageMissing = deviceStatus(data.stage);
      refs.stageStatus.textContent = stageMissing || (stage.connected ? t("stage.connected") : t("stage.disconnected"))
        + (stage.fault?.message ? " · " + stage.fault.message : "");
      refs.stageBadge.textContent = stage ? t(stage.demo ? "badge.demo" : "badge.realStage") : "";
      refs.stageBadge.className = "mo-badge" + (stage && !stage.demo ? " mo-real" : "");
      refs.stageBadge.hidden = !stage;
      updateControl("stage", data.stage);
      const stageReady = Boolean(stage && ownsStage && stage.connected && !stage.locked);
      const idle = (stage?.motors || []).every(motor => motor.mode === "idle");
      for (const node of refs.modes || []) {
        const on = Number(node.getAttribute("data-value")) === stage?.microstep;
        node.setAttribute("aria-checked", on ? "true" : "false");
        node.className = "mo-chip" + (on ? " mo-on" : "");
        node.disabled = !stageReady || !idle || stage?.busy || busy("stage");
      }
      for (const motor of stage?.motors || []) {
        const row = refs.axes?.[motor.id];
        if (!row) continue;
        const state = axisState(motor);
        row.state.textContent = t(state.key);
        row.state.className = "lc-state tone-text-" + state.tone;
        row.pulses.textContent = t("axis.pulses", { n: motor.position ?? "—" });
        row.mm.textContent = t("axis.mm", { mm: fixed(motor.position_mm, 2) });
        const reference = axisReference(motor);
        row.ref.textContent = t(reference.key);
        row.ref.className = "lc-strong tone-text-" + reference.tone;
        const moving = model.inflight.has("stage:move-" + motor.id);
        const canStep = stageReady && motor.available === true && !stage.busy && !moving;
        row.minus.disabled = row.plus.disabled = !canStep;
        const refBusy = model.inflight.has("stage:ref-" + motor.id);
        if (row.home) row.home.disabled = !stageReady || motor.mode !== "idle" || stage.busy || refBusy;
        row.zero.disabled = !stageReady || motor.zero_available !== true || motor.mode !== "idle" || stage.busy || refBusy;
        row.check.hidden = row.teach.hidden = motor.reference_configured !== true;
        row.check.disabled = !stage.connected || refBusy;
        row.teach.disabled = row.zero.disabled || motor.reference_configured !== true;
        renderReference(row, motor.id);
      }
      const referenced = (stage?.motors || []).length > 0 && stage.motors.every(motor => motor.automation_ready === true);
      const posesReady = stageReady && referenced && !stage.busy && stage.motors.every(motor => motor.available === true);
      for (const item of refs.poses || []) {
        const at = (stage?.at_poses || []).includes(item.name);
        item.button.className = "lc-button" + (at ? " mo-at" : "");
        item.button.setAttribute("aria-pressed", at ? "true" : "false");
        item.button.disabled = !posesReady || model.inflight.has("stage:pose");
      }
      if (refs.poseNote) refs.poseNote.textContent = stage && !referenced ? t("pose.needRef") : "";
      for (const item of refs.named || []) {
        const motor = (stage?.motors || []).find(m => m.id === item.motor);
        const at = (motor?.at_positions || []).includes(item.name);
        item.button.className = "lc-button" + (at ? " mo-at" : "");
        item.button.setAttribute("aria-pressed", at ? "true" : "false");
        item.button.disabled = !stageReady || !motor || stage.busy || motor.mode !== "idle" || model.inflight.has("stage:move-" + item.motor);
      }
      refs.stageLast.textContent = stage ? lastText(stage.last_command) : "";
      const disconnected = Boolean(stage && stage.connected !== true);
      refs.connectRow.hidden = !disconnected;
      if (disconnected && model.ports === null && !model.portsLoading) loadPorts();
      refs.connect.disabled = !ownsStage || !model.port || busy("stage");
      refs.refreshPorts.disabled = model.portsLoading;
    }

    async function loadPorts() {
      model.portsLoading = true;
      update();
      try {
        const body = await api.request("stage/ports");
        model.ports = Array.isArray(body.ports) ? body.ports : [];
      } catch { model.ports = []; }
      finally { model.portsLoading = false; }
      if (!model.ports.some(item => item.device === model.port)) model.port = model.ports[0]?.device || "";
      refs.portSelect.replaceChildren(...(model.ports.length
        ? model.ports.map(item => el("option", { value: item.device, text: item.description ? `${item.device} · ${item.description}` : item.device }))
        : [el("option", { value: "", text: t("stage.noPorts") })]));
      refs.portSelect.value = model.port;
      update();
    }

    async function checkReference(id) {
      const key = "stage:ref-" + id;
      if (model.inflight.has(key)) return;
      model.inflight.add(key);
      update();
      try {
        model.refView[id] = await api.request("stage/reference/" + encodeURIComponent(id));
      } catch (error) {
        model.refView[id] = { error: error.message };
      } finally {
        model.inflight.delete(key);
        update();
      }
    }

    function renderReference(row, id) {
      const view = model.refView[id];
      row.view.hidden = !view;
      if (!view || row.shown === view) return;
      row.shown = view;
      const offset = Array.isArray(view.offset_px) ? view.offset_px : null;
      row.view.replaceChildren(
        view.image ? el("img", { class: "mo-ref-img", src: view.image, alt: t("axis.check") }) : "",
        el("p", { class: "lc-muted", text: view.error ? t("axis.checkFailed", { error: view.error }) : offset ? t("axis.offset", { x: fixed(offset[0], 1), y: fixed(offset[1], 1) }) : "" }));
    }

    function start() {
      stop();
      refresh();
      refreshEvents();
      timer = setInterval(refresh, POLL_MS);
      eventsTimer = setInterval(refreshEvents, EVENTS_MS);
    }

    function stop() {
      if (timer) clearInterval(timer);
      if (eventsTimer) clearInterval(eventsTimer);
      timer = eventsTimer = null;
    }

    return {
      id: "motion",
      i18n,
      mount(node, consoleApi) {
        api = consoleApi;
        rootNode = node;
        api.onLanguage(build);
        api.onState(update);
        shown = true;
        if (typeof root.addEventListener === "function") {
          root.addEventListener("keydown", onKeyDown);
          root.addEventListener("keyup", onKeyUp);
          root.addEventListener("blur", () => disarm("focus"));
        }
        root.document?.addEventListener?.("visibilitychange", () => { if (root.document.visibilityState !== "visible") disarm("focus"); });
        build();
        start();
      },
      show() { shown = true; if (api) { renderCameras(); start(); } },
      hide() { disarm("focus"); shown = false; stop(); dropTiles(); if (refs.cams) refs.cams.replaceChildren(); },
      _test: { data, model, kb, refs: () => refs, refresh, update },
    };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { logic, i18n, workspace };
  if (root && root.LabConsole && typeof root.LabConsole.register === "function") root.LabConsole.register(workspace());
})(typeof window !== "undefined" ? window : globalThis);
