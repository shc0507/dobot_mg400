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
  // Each station shows the view of its own device first. Any of its views can take the frame.
  const STATION_VIEWS = { arm: ["camera2", "camera4", "camera3", "camera1", "microscope", "thermal"], stage: ["microscope", "camera1", "camera3", "camera2", "camera4", "thermal"] };
  // Keyboard jog: the robot service moves while it sees a key held (lease 0.4 s).
  const KEYS = "WASDQERFZX";
  // The keyboard jog keys in pairs, each with the label of its pair. The first key of a pair moves out, +, up or opens.
  const KEY_PAIRS = [["W", "S", "pair.reach"], ["A", "D", "pair.j1"], ["R", "F", "pair.z"], ["Q", "E", "pair.j4"], ["Z", "X", "pair.suction"]];
  // The key that moves like each jog button, shown on the button while the keyboard is on. The letters R and Z
  // are keys here, not axes: R moves Z up, and Q turns R + (J4 +).
  const JOG_KEYS = { "z+": "R", "z-": "F", "r+": "Q", "r-": "E" };
  const KEYS_MS = 150;
  // Stage hold: the console keeps 1 axis moving while this page refreshes the hold (dead-man 0.5 s).
  // Each pair keeps its existing ccw / cw bindings. Physical directions match the original stage controller.
  const STAGE_KEYS = [["I", "K", "bottom"], ["J", "L", "top"], ["U", "O", "insert"]];
  const STAGE_DIRECTIONS = {
    bottom: { ccw: "up", cw: "down" },
    top: { ccw: "right", cw: "left" },
    insert: { ccw: "in", cw: "out" },
  };
  const HOLD_MS = 150;
  const POLL_MS = 1000;
  const EVENTS_MS = 2000;
  const LOG_LIMIT = 40;

  // Pure functions. Tests load them without a DOM.

  /** The actual pose [x, y, z, r] or null when any value is unknown. */
  function actualPose(robot) {
    const values = Array.isArray(robot?.actual) ? robot.actual.slice(0, 4) : [];
    return values.length === 4 && values.every(value => typeof value === "number" && Number.isFinite(value)) ? values : null;
  }

  // Equal X/Y scales, centred on the origin. This is a plot scale, not a motion limit.
  function armXY(pose) {
    if (!Array.isArray(pose) || !pose.slice(0, 2).every(value => typeof value === "number" && Number.isFinite(value)) || pose.length < 2) return null;
    const extent = Math.max(500, Math.ceil(Math.max(Math.abs(pose[0]), Math.abs(pose[1])) / 100) * 100);
    return { extent, x: (pose[0] / extent + 1) / 2, y: (pose[1] / extent + 1) / 2 };
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

  /** A short port label: the description and the device name, without the /dev/serial/by-id/ path. */
  function portLabel(item) {
    const name = String(item?.device || "").split("/").pop();
    const short = name.length > 24 ? "" : name;
    return [item?.description, short].filter(Boolean).join(" · ") || String(item?.device || "");
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

  /** The measured suction state: "suck", "blow", "off" or null, and held (true, false or null without a grip switch). */
  function vacuumView(vacuum) {
    const known = vacuum && typeof vacuum.suck === "boolean" && typeof vacuum.blow === "boolean";
    const state = !known ? null : vacuum.suck ? "suck" : vacuum.blow ? "blow" : "off";
    return { state, held: typeof vacuum?.held === "boolean" ? vacuum.held : null };
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

  /** The letter of one keyboard event from its physical key, so the layout does not matter. */
  function keyName(event) {
    const code = String(event?.code || "");
    return code.startsWith("Key") ? code.slice(3) : String(event?.key || "").toUpperCase();
  }

  /** The jog key of one keyboard event (W, A, …), or null. */
  function jogKey(event) {
    const key = keyName(event);
    return key.length === 1 && KEYS.includes(key) ? key : null;
  }

  /** The stage hold of one keyboard event: { key, motor, direction }, or null. */
  function holdKey(event) {
    const key = keyName(event);
    for (const [minus, plus, motor] of STAGE_KEYS) {
      if (key === minus || key === plus) return { key, motor, direction: key === minus ? "ccw" : "cw" };
    }
    return null;
  }

  /** A new stage hold id. The page is not a secure context, so no randomUUID. */
  function holdId() {
    let bytes;
    try { bytes = root.crypto.getRandomValues(new Uint8Array(12)); }
    catch { bytes = Array.from({ length: 12 }, () => Math.floor(Math.random() * 256)); }
    return "hold-" + Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  }

  /** The short stepping label: Full for full step, otherwise the step fraction. */
  function stepLabel(value, full) { return value === 1 ? full : "1/" + value; }

  const logic = { actualPose, jogTarget, parsePose, speedCap, keyboardCap, jogKey, holdKey, holdId, clampSpeed, parseCount, axisReference, axisState, portLabel, activityRows, clock, fixed, stepLabel, STEPS, CAMERAS, STATION_VIEWS };

  const i18n = {
    en: {
      "arm.title": "Robot arm", "stage.title": "Stage",
      "badge.demo": "DEMO",
      "dev.unconfigured": "Not configured", "dev.offline": "Not running", "dev.unreachable": "Not reachable", "dev.loading": "Loading…",
      "arm.enabled": "Enabled", "arm.disabled": "Disabled", "arm.busy": "Moving", "arm.alarm": "Alarm",
      "arm.locked": "Stop not confirmed", "arm.lockedHelp": "Check the arm, then press Recover.", "arm.errorOnly": "The controller reports an error.",
      "arm.recover": "Recover", "arm.noFeedback": "Not connected", "arm.cap": "speed cap {cap}",
      "ctl.take": "Take control", "ctl.release": "Release",
      "ctl.console": "In use by another page", "ctl.other": "In use by another client",
      "ctl.takeLabel": "Take control of {device}", "ctl.releaseLabel": "Release {device}",
      "arm.enable": "Enable", "arm.disable": "Disable", "arm.clear": "Clear alarm",
      "arm.stop": "Stop arm", "arm.home": "Home…", "arm.homeConfirm": "Clear the path of the arm. Home moves all 4 joints to 0°.",
      "arm.alarmItem": "Alarm {id}",
      "jog.title": "Jog", "jog.step": "Jog step, mm, or degrees for R", "jog.units": "mm · R in °", "jog.key": "Key {key}",
      "jog.move": "Move {label} by the jog step",
      "move.title": "Move to a pose", "move.speed": "Speed", "move.speedLabel": "Speed, 1 to {cap}",
      "move.useActual": "Use actual", "move.execute": "Execute", "move.invalid": "Enter 4 numbers for X, Y, Z and R.",
      "kb.switch": "Keyboard jog", "kb.keys": "Keyboard jog keys", "kb.speedLabel": "Keyboard speed, 1 to {cap}", "kb.off": "Off: {reason}",
      "key.Q": "J4 +", "key.W": "Reach out", "key.E": "J4 −", "key.R": "Z up", "key.A": "J1 +", "key.S": "Reach in",
      "key.D": "J1 −", "key.F": "Z down", "key.Z": "Suck", "key.X": "Drop",
      "pair.reach": "Out / In", "pair.j1": "J1 + / −", "pair.z": "Z up / down", "pair.j4": "J4 + / −", "pair.suction": "Suck / Drop",
      "pose.actual": "Actual pose", "pose.joints": "Joint angles", "fold.jog": "Jog buttons", "fold.stage": "Step moves and zero",
      "pose.xy": "X/Y position", "pose.current": "Current", "pose.last": "Last known", "pose.unknown": "No position",
      "vac.title": "Suction", "vac.suck": "Suck", "vac.release": "Drop",
      "vac.state.suck": "Sucking", "vac.state.blow": "Blowing", "vac.state.off": "Off",
      "vac.held": "Held", "vac.notHeld": "Not held", "vac.label": "Suction: {action}",
      "last": "Last: {action} · {result}", "last.message": "Last: {action} · {result} · {message}",
      "confirm": "Confirm", "cancel": "Cancel",
      "stage.stop": "Stop stage", "stage.connected": "Connected", "stage.disconnected": "Arduino not connected",
      "stage.stepping": "Stepping", "stage.full": "Full", "stage.keys": "Keys",
      "stage.steppingConfirm": "Set {mode}? This clears the zero and soft limits of all 3 axes.",
      "stage.zeroNone": "Set zero needs the stage calibration file and zero support in the firmware. Hold and step work without them.",
      "axis.bottom": "Bottom (Y)", "axis.top": "Top (X)", "axis.insert": "Insert",
      "direction.up": "Up", "direction.down": "Down", "direction.left": "Left", "direction.right": "Right",
      "direction.in": "In", "direction.out": "Out",
      "axis.idle": "Idle", "axis.moving": "Moving", "axis.cooldown": "Cooling down",
      "axis.pulses": "{n} pulses", "axis.mm": "{mm} mm",
      "ref.homed": "Homed", "ref.zeroed": "Zero confirmed", "ref.homeRequired": "Home required",
      "ref.zeroRequired": "Zero required", "ref.unavailable": "Reference unavailable",
      "axis.steps": "Steps", "axis.speed": "Pulses/s", "axis.speedUnit": "p/s", "axis.speedLabel": "{axis} speed, pulses/s", "axis.unit": "pulses",
      "axis.step": "Step {direction}", "axis.stepLabel": "Move {axis} {direction} by the steps",
      "axis.badCount": "Enter whole steps from 1 to {max} and a speed from 1 to {speed}.",
      "axis.hold": "Hold {direction}", "axis.holdLabel": "Hold to move {axis} {direction}",
      "axis.badSpeed": "Enter a speed from 1 to {speed} pulses/s.",
      "kb.stage": "Stage keyboard", "kb.axis": "{axis} − / +",
      "axis.home": "Home…", "axis.homeConfirm": "Clear the travel path. {axis} moves to its reference switch.",
      "axis.check": "Check reference", "axis.zero": "Set zero…", "axis.teach": "Teach reference…",
      "axis.teachConfirm": "Is {axis} at its safe zero position? The current marker position replaces the camera reference.",
      "axis.offset": "Marker offset {x}, {y} px", "axis.checkFailed": "Reference check failed: {error}",
      "stage.port": "Serial port", "stage.connect": "Connect", "stage.noPorts": "No serial port found", "stage.refresh": "Refresh ports",
      "pose.title": "Named poses", "pose.needRef": "Home or zero all 3 axes before a named pose.",
      "axis.zeroConfirm": "Set the current position of {axis} as zero. Check the physical position first.",
      "named.title": "Named positions",
      "named.go": "{axis} → {name}",
      "log.title": "Robot and stage activity", "log.none": "No activity yet.", "log.page": "this page",
      "out.accepted": "{device} {action}: sent", "out.refused": "{device} {action}: refused · {error}",
      "out.unknown": "{device} {action}: outcome unknown. Check the device. It is not retried.",
      "out.stop": "{device} stop: {outcome}",
      "res.confirmed": "confirmed", "res.pending": "pending", "res.unconfirmed": "not confirmed", "res.unknown": "unknown",
      "act.enable": "Enable", "act.disable": "Disable", "act.clear_alarm": "Clear alarm", "act.execute": "Move", "act.vacuum": "Suction",
      "act.home": "Home", "act.stage.home": "Home", "act.keyboard": "Keyboard", "act.keys": "Keyboard keys",
      "act.hold": "Hold", "act.connect": "Connect", "act.goto": "Named position", "act.move": "Step", "act.pose": "Named pose",
      "act.set_microstep": "Stepping", "act.teach_reference": "Teach reference", "act.zero": "Set zero",
      "act.stop": "Stop", "act.stop_sync": "Stop", "act.release_hold": "Release hold", "act.keepalive": "Hold refresh",
      "res.ok": "ok", "res.failed": "failed", "res.refused": "refused",
      "stage.needControl": "Take control of the stage to step, home or set zero.",
      "cam.offline": "OFFLINE",
      "view.arm": "Arm view", "view.stage": "Stage view", "view.camera2": "Insert rail", "view.camera4": "Tray",
      "view.camera3": "Microscope side", "view.microscope": "Microscope", "view.camera1": "Stage", "view.thermal": "Thermal",
    },
    zh: {
      "arm.title": "机械臂", "stage.title": "位移台",
      "badge.demo": "演示",
      "dev.unconfigured": "未配置", "dev.offline": "未运行", "dev.unreachable": "无法连接", "dev.loading": "加载中…",
      "arm.enabled": "已使能", "arm.disabled": "未使能", "arm.busy": "运动中", "arm.alarm": "报警",
      "arm.locked": "停止未确认", "arm.lockedHelp": "检查机械臂，然后按“恢复”。", "arm.errorOnly": "控制器报告错误。",
      "arm.recover": "恢复", "arm.noFeedback": "未连接", "arm.cap": "速度上限 {cap}",
      "ctl.take": "获取控制权", "ctl.release": "释放",
      "ctl.console": "另一个页面正在使用", "ctl.other": "另一个客户端正在使用",
      "ctl.takeLabel": "获取{device}的控制权", "ctl.releaseLabel": "释放{device}",
      "arm.enable": "使能", "arm.disable": "下使能", "arm.clear": "清除报警",
      "arm.stop": "停止机械臂", "arm.home": "回原点…", "arm.homeConfirm": "请清空机械臂的运动路径。回原点会把 4 个关节都转到 0°。",
      "arm.alarmItem": "报警 {id}",
      "jog.title": "点动", "jog.step": "点动步长，mm，R 为度", "jog.units": "mm · R 为 °", "jog.key": "按键 {key}",
      "jog.move": "按点动步长移动 {label}",
      "move.title": "移动到位姿", "move.speed": "速度", "move.speedLabel": "速度，1 到 {cap}",
      "move.useActual": "使用当前值", "move.execute": "执行", "move.invalid": "请为 X、Y、Z 和 R 输入 4 个数字。",
      "kb.switch": "键盘点动", "kb.keys": "键盘点动按键", "kb.speedLabel": "键盘速度，1 到 {cap}", "kb.off": "已关闭：{reason}",
      "key.Q": "J4 +", "key.W": "伸出", "key.E": "J4 −", "key.R": "Z 上", "key.A": "J1 +", "key.S": "收回",
      "key.D": "J1 −", "key.F": "Z 下", "key.Z": "吸取", "key.X": "放下",
      "pair.reach": "伸出 / 收回", "pair.j1": "J1 + / −", "pair.z": "Z 上 / 下", "pair.j4": "J4 + / −", "pair.suction": "吸取 / 放下",
      "pose.actual": "实际位姿", "pose.joints": "关节角度", "fold.jog": "点动按钮", "fold.stage": "步进和零点",
      "pose.xy": "X/Y 位置", "pose.current": "当前位置", "pose.last": "最后位置", "pose.unknown": "无位置",
      "vac.title": "吸盘", "vac.suck": "吸取", "vac.release": "放下",
      "vac.state.suck": "吸取中", "vac.state.blow": "吹气中", "vac.state.off": "已关闭",
      "vac.held": "已吸住", "vac.notHeld": "未吸住", "vac.label": "吸盘：{action}",
      "last": "上一条：{action} · {result}", "last.message": "上一条：{action} · {result} · {message}",
      "confirm": "确认", "cancel": "取消",
      "stage.stop": "停止位移台", "stage.connected": "已连接", "stage.disconnected": "Arduino 未连接",
      "stage.stepping": "细分", "stage.full": "整步", "stage.keys": "按键",
      "stage.steppingConfirm": "设为{mode}？这会清除全部 3 轴的零点和软限位。",
      "stage.zeroNone": "设零需要位移台标定文件和固件的设零支持。没有它们时，按住移动和步进仍可使用。",
      "axis.bottom": "底部轴 (Y)", "axis.top": "顶部轴 (X)", "axis.insert": "插入轴",
      "direction.up": "上", "direction.down": "下", "direction.left": "左", "direction.right": "右",
      "direction.in": "进", "direction.out": "出",
      "axis.idle": "空闲", "axis.moving": "运动中", "axis.cooldown": "冷却中",
      "axis.pulses": "{n} 脉冲", "axis.mm": "{mm} mm",
      "ref.homed": "已回零", "ref.zeroed": "零点已确认", "ref.homeRequired": "需要回零",
      "ref.zeroRequired": "需要设零", "ref.unavailable": "参考不可用",
      "axis.steps": "步数", "axis.speed": "脉冲/秒", "axis.speedUnit": "脉冲/秒", "axis.speedLabel": "{axis}速度，脉冲/秒", "axis.unit": "脉冲",
      "axis.step": "向{direction}步进", "axis.stepLabel": "{axis}按步数向{direction}移动",
      "axis.badCount": "请输入 1 到 {max} 的整数步数和 1 到 {speed} 的速度。",
      "axis.hold": "按住向{direction}", "axis.holdLabel": "按住使{axis}向{direction}移动",
      "axis.badSpeed": "请输入 1 到 {speed} 的速度（脉冲/秒）。",
      "kb.stage": "位移台键盘", "kb.axis": "{axis} − / +",
      "axis.home": "回零…", "axis.homeConfirm": "请清空行程。{axis}将移动到参考开关。",
      "axis.check": "检查参考", "axis.zero": "设零…", "axis.teach": "示教参考…",
      "axis.teachConfirm": "{axis}在安全零点吗？当前标记位置将替换摄像头参考。",
      "axis.offset": "标记偏移 {x}, {y} px", "axis.checkFailed": "参考检查失败：{error}",
      "stage.port": "串口", "stage.connect": "连接", "stage.noPorts": "未找到串口", "stage.refresh": "刷新串口",
      "pose.title": "命名位姿", "pose.needRef": "使用命名位姿前，先让全部 3 轴回零或设零。",
      "axis.zeroConfirm": "把{axis}的当前位置设为零点。请先检查实际位置。",
      "named.title": "命名位置",
      "named.go": "{axis} → {name}",
      "log.title": "机械臂和位移台活动", "log.none": "暂无活动。", "log.page": "本页面",
      "out.accepted": "{device} {action}：已发送", "out.refused": "{device} {action}：被拒绝 · {error}",
      "out.unknown": "{device} {action}：结果未知。请检查设备。不会重试。",
      "out.stop": "{device} 停止：{outcome}",
      "res.confirmed": "已确认", "res.pending": "等待中", "res.unconfirmed": "未确认", "res.unknown": "未知",
      "act.enable": "使能", "act.disable": "下使能", "act.clear_alarm": "清除报警", "act.execute": "移动", "act.vacuum": "吸盘",
      "act.home": "回原点", "act.stage.home": "回零", "act.keyboard": "键盘", "act.keys": "键盘按键",
      "act.hold": "按住移动", "act.connect": "连接", "act.goto": "命名位置", "act.move": "步进", "act.pose": "命名位姿",
      "act.set_microstep": "细分", "act.teach_reference": "示教参考", "act.zero": "设零",
      "act.stop": "停止", "act.stop_sync": "停止", "act.release_hold": "松开按住", "act.keepalive": "刷新按住",
      "res.ok": "完成", "res.failed": "失败", "res.refused": "被拒绝",
      "stage.needControl": "获取位移台控制权后，才能步进、回零或设零。",
      "cam.offline": "离线",
      "view.arm": "机械臂视图", "view.stage": "位移台视图", "view.camera2": "插入导轨", "view.camera4": "托盘",
      "view.camera3": "显微镜侧视", "view.microscope": "显微镜", "view.camera1": "位移台", "view.thermal": "热像仪",
    },
  };

  Object.assign(logic, { vacuumView, alarmRows });

  function workspace() {
    let api = null, rootNode = null, timer = null, eventsTimer = null, shown = false;
    let positionMonitor = null, positionHost = null;
    const data = { robot: null, stage: null, events: [], cap: null, kbCap: null, loaded: false };
    // on: this page armed the keyboard. seen: the service reported it on or arming since then.
    const kb = { on: false, seen: false, speed: null, held: new Set(), seq: 0, timer: null, pending: 0 };
    // on: the stage keyboard switch. active: this page's stage hold, 1 axis at a time.
    const sh = { on: false, active: null, timer: null };
    const model = {
      step: 2, speed: null, pose: { x: "", y: "", z: "", r: "" }, views: { arm: STATION_VIEWS.arm[0], stage: STATION_VIEWS.stage[0] },
      axis: {}, local: [], confirm: null, inflight: new Set(), describing: false,
      ports: null, port: "", portsLoading: false, refView: {},
    };
    let refs = {};

    const t = (key, vars) => api.t(key, vars);
    /** The label of one command in the activity text. A device can name an action its own way. */
    const actionName = (device, action) => {
      for (const key of ["act." + device + "." + action, "act." + action]) { const text = t(key); if (text !== key) return text; }
      return action;
    };
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
        if (result.outcome === "unknown") api.notify(t("out.unknown", { device: api.deviceName("robot"), action: actionName("robot", "execute") }), "error");
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
        const vars = { device: api.deviceName(device), action: actionName(device, action), error: result?.error || "" };
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
      if (device === "stage") holdEnd();
      const result = await api.stop(device);
      const outcome = ["confirmed", "pending", "unconfirmed"].includes(result?.outcome) ? result.outcome : "unknown";
      note(device, outcome === "confirmed" ? "info" : "warning", t("out.stop", { device: api.deviceName(device), outcome: t("res." + outcome) }));
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

    // An open confirmation takes the focus. Cancel, Esc and Confirm give it back to the button that opened it.
    function askConfirm(slot, text, run) {
      model.confirm = { slot, text, run, opener: root.document?.activeElement || null };
      renderConfirms();
      refs.confirms?.[slot]?.focus?.();
    }

    function closeConfirm() {
      const opener = model.confirm?.opener;
      model.confirm = null;
      renderConfirms();
      opener?.focus?.();
    }

    function renderConfirms() {
      for (const [slot, node] of Object.entries(refs.confirms || {})) {
        const item = model.confirm?.slot === slot ? model.confirm : null;
        if (!item) { node.replaceChildren(); node.hidden = true; node.removeAttribute("aria-label"); continue; }
        node.hidden = false;
        node.setAttribute("aria-label", item.text);
        node.replaceChildren(
          el("p", { text: item.text }),
          el("div", { class: "mo-grid2" },
            el("button", { type: "button", class: "mo-btn mo-danger", text: t("confirm"), onclick: () => { closeConfirm(); item.run(); } }),
            el("button", { type: "button", class: "mo-btn", text: t("cancel"), onclick: closeConfirm })));
      }
    }

    function confirmSlot(name) {
      const node = el("div", { class: "lc-confirm", role: "alertdialog", hidden: true, tabindex: "-1",
        onkeydown: event => { if (event.key === "Escape" && model.confirm?.slot === name) { event.preventDefault?.(); closeConfirm(); } } });
      refs.confirms[name] = node;
      return node;
    }

    /** Take control or Release. When another client holds the device, the button says so. */
    function controlButton(device) {
      const button = el("button", { type: "button", class: "mo-btn", onclick: () => control(device) });
      refs["ctl-" + device] = { button };
      return button;
    }

    function fold(title, ...content) {
      return el("details", { class: "mo-fold" }, el("summary", { text: title }), el("div", { class: "mo-fold-body" }, ...content));
    }

    // Arm station -----------------------------------------------------------

    function buildArm() {
      refs.armStatus = el("span", { class: "mo-state" });
      refs.armBadge = el("span", { class: "mo-badge", text: t("badge.demo"), hidden: true });
      refs.armStop = el("button", { type: "button", class: "mo-stop", text: t("arm.stop"), onclick: () => stopDevice("robot") });
      // One button follows the arm: Disable (no control needed) while the arm is enabled, moves or jogs; else Enable.
      refs.enable = el("button", { type: "button", class: "mo-btn", onclick: () => send("robot", model.armOn ? "disable" : "enable", {}) });
      refs.home = el("button", {
        type: "button", class: "mo-btn mo-primary", text: t("arm.home"),
        onclick: () => askConfirm("home", t("arm.homeConfirm"), () => send("robot", "home", {}, "move")),
      });
      refs.clear = el("button", { type: "button", class: "mo-btn", text: t("arm.clear"), onclick: () => send("robot", "clear_alarm", {}) });
      // The alarm note wins the eye: the alarm IDs, an unconfirmed stop and the button that clears them.
      refs.alarmTitle = el("p", { class: "mo-alarm-title" });
      refs.alarms = el("div", { class: "mo-alarm-items" });
      refs.alarmHelp = el("p", { hidden: true });
      refs.alarmNote = el("div", { class: "mo-alarm", role: "alert", hidden: true }, refs.alarmTitle, refs.alarms, refs.alarmHelp, el("div", { class: "mo-row" }, refs.clear));
      refs.armMessage = el("p", { class: "mo-message", role: "status", hidden: true });

      // Keyboard jog: the main arm control.
      refs.kbSwitch = el("button", { type: "button", role: "switch", class: "mo-switch", "aria-checked": "false", onclick: () => (kb.on ? disarm("user") : arm()) },
        el("span", { class: "mo-knob", "aria-hidden": "true" }), el("span", { text: t("kb.switch") }));
      refs.kbSpeed = el("input", {
        type: "range", min: "1", max: String(data.kbCap || 1), step: "1",
        "aria-label": t("kb.speedLabel", { cap: data.kbCap || 1 }),
        oninput: event => { kb.speed = clampSpeed(event.target.value, data.kbCap); update(); },
      });
      refs.kbSpeed.value = String(clampSpeed(kb.speed, data.kbCap));
      refs.kbSpeedText = el("span", { class: "mo-num-small" });
      refs.kbOff = el("p", { class: "mo-kb-off", role: "status" });
      refs.kbKeys = {};
      const cap = key => (refs.kbKeys[key] = el("kbd", { class: "mo-cap", title: t("key." + key) }, key));
      const kbBox = el("div", { class: "mo-kb" },
        el("div", { class: "mo-kb-head" }, refs.kbSwitch,
          el("label", { class: "mo-speed" }, el("span", { text: t("move.speed") }), refs.kbSpeed, refs.kbSpeedText)),
        refs.kbOff,
        el("div", { class: "mo-keyboard", role: "group", "aria-label": t("kb.keys") },
          ...KEY_PAIRS.map(([a, b, label]) => el("span", { class: "mo-pair" }, cap(a), cap(b), el("span", { text: t(label) })))));

      // Readout: pose, joints and suction.
      refs.pose = AXES.map(() => el("b", { class: "mo-num" }));
      refs.armXYDot = el("span", { class: "mo-xy-point", hidden: true });
      refs.armXYState = el("span", { class: "mo-xy-state" });
      refs.armXYScale = el("span", { class: "mo-xy-scale" });
      refs.armXYMap = el("div", { class: "mo-xy-map", role: "img", "aria-label": t("pose.xy") },
        refs.armXYDot, el("span", { class: "mo-xy-zero", text: "0" }),
        el("span", { class: "mo-xy-x", text: "X →" }), el("span", { class: "mo-xy-y", text: "Y ↑" }));
      const poseReading = (axis, i) => el("div", { class: "mo-reading" }, el("span", { text: axis.toUpperCase() }),
        el("span", { class: "mo-pose-value" }, refs.pose[i], el("span", { class: "mo-unit", text: axis === "r" ? "°" : "mm" })));
      refs.joints = [0, 1, 2, 3].map(() => el("b", { class: "mo-num-small mo-deg" }));
      refs.vacState = el("span", { class: "mo-vac-state" });
      // The grip switch is not installed yet. Held shows when the robot service reports it.
      refs.vacHeld = el("span", { class: "mo-vac-held", hidden: true });
      refs.vac = Object.fromEntries(["suck", "release"].map(operation => [operation,
        el("button", { type: "button", class: "mo-btn", text: t("vac." + operation), "aria-label": t("vac.label", { action: t("vac." + operation) }),
          onclick: () => send("robot", "vacuum", { operation }, "vacuum") })]));
      const readout = el("div", { class: "mo-readout" },
        el("div", { class: "mo-arm-position", role: "group", "aria-label": t("pose.actual") },
          el("div", { class: "mo-xy-heading" }, el("span", { text: t("pose.xy") }), refs.armXYState),
          el("div", { class: "mo-pose" }, ...AXES.slice(0, 2).map(poseReading)),
          refs.armXYMap, refs.armXYScale,
          el("div", { class: "mo-pose mo-zr-readings" }, poseReading("z", 2), poseReading("r", 3))),
        el("div", { class: "mo-joints", role: "group", "aria-label": t("pose.joints") },
          ...refs.joints.map((node, i) => el("div", { class: "mo-reading" }, el("span", { text: "J" + (i + 1) }), node))),
        el("div", { class: "mo-vac" }, el("span", { class: "mo-label", text: t("vac.title") }), refs.vacState, refs.vacHeld),
        el("div", { class: "mo-grid2" }, refs.vac.suck, refs.vac.release));

      // Jog buttons: rarely used, so folded.
      refs.jog = {};
      refs.jogHints = [];
      const jogButton = item => {
        const key = JOG_KEYS[item.id];
        const hint = key ? el("kbd", { class: "mo-kcap mo-hint", text: key, title: t("jog.key", { key }), hidden: true }) : null;
        const button = el("button", { type: "button", class: "mo-jog", "aria-label": t("jog.move", { label: item.label }), onclick: () => jog(item) },
          el("span", { text: item.label }), hint);
        if (hint) refs.jogHints.push(hint);
        refs.jog[item.id] = button;
        return button;
      };
      const pad = el("div", { class: "mo-pad" },
        el("span"), jogButton(JOG[0]), el("span"),
        jogButton(JOG[1]), el("span", { class: "mo-pad-mid", text: "XY" }), jogButton(JOG[2]),
        el("span"), jogButton(JOG[3]), el("span"));
      const side = el("div", { class: "mo-zr" }, ...JOG.slice(4).map(jogButton));
      refs.steps = STEPS.map(step => el("button", {
        type: "button", role: "radio", class: "mo-seg-btn", text: String(step),
        onclick: () => { model.step = step; update(); },
      }));
      const jogFold = fold(t("fold.jog"),
        el("div", { class: "mo-jogrow" }, pad, side),
        el("div", { class: "mo-seg-row" },
          el("div", { class: "mo-seg", role: "radiogroup", "aria-label": t("jog.step") }, ...refs.steps),
          el("span", { class: "mo-muted", text: t("jog.units") })));

      // Move to a pose: rarely used, so folded.
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
      refs.speedText = el("span", { class: "mo-num-small" });
      refs.useActual = el("button", { type: "button", class: "mo-btn", text: t("move.useActual"), onclick: useActual });
      refs.execute = el("button", { type: "button", class: "mo-btn mo-primary", text: t("move.execute"), onclick: executePose });
      const moveFold = fold(t("move.title"),
        el("div", { class: "mo-fields" }, ...fields),
        el("label", { class: "mo-speed" }, el("span", { text: t("move.speed") }), refs.speed, refs.speedText),
        el("div", { class: "mo-grid2" }, refs.useActual, refs.execute));

      refs.armLast = el("p", { class: "mo-muted mo-last", role: "status" });

      return el("section", { class: "mo-station mo-station-arm", "aria-labelledby": "mo-arm-title" },
        el("div", { class: "mo-body" },
          el("div", { class: "mo-head" },
            el("h2", { id: "mo-arm-title", text: t("arm.title") }), refs.armStatus, refs.armBadge,
            el("span", { class: "mo-actions" }, refs.enable, refs.home, controlButton("robot"), refs.armStop)),
          refs.alarmNote, confirmSlot("home"),
          el("div", { class: "mo-armbody" },
            el("div", { class: "mo-arm-visual" }, buildView("arm"), kbBox), readout),
          el("div", { class: "mo-folds" }, jogFold, moveFold),
          refs.armMessage, refs.armLast));
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
      try { result = await api.request("keys", { seq: ++kb.seq, held: [...kb.held] }, { accept: [409, 502], timeout: 2000, socket: true }); }
      catch (error) { result = { outcome: error.status === 0 ? "unknown" : "refused", error: error.message }; }
      finally { kb.pending--; }
      if (result?.outcome !== "accepted" && kb.held.size) {
        note("robot", "warning", t(result?.outcome === "unknown" ? "out.unknown" : "out.refused", { device: api.deviceName("robot"), action: actionName("robot", "keys"), error: result?.error || "" }));
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
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const key = kb.on ? jogKey(event) : null;
      const stage = sh.on ? holdKey(event) : null;
      const tag = String(event.target?.tagName || "").toUpperCase();
      if ((!key && !stage) || tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA") return;
      event.preventDefault?.();
      if (event.repeat || !focused()) return;
      if (key && !kb.held.has(key) && api.owns("robot")) {
        kb.held.add(key);
        keysChanged();
      }
      if (stage) holdStart(stage.motor, stage.direction, stage.key);
    }

    function onKeyUp(event) {
      const key = jogKey(event);
      if (key && kb.held.delete(key)) keysChanged();
      const stage = holdKey(event);
      if (stage && sh.active?.source === stage.key) holdEnd();
    }

    function renderKeys() {
      for (const [key, node] of Object.entries(refs.kbKeys || {})) node.className = "mo-cap" + (kb.held.has(key) ? " mo-on" : "");
    }

    function updateKeyboard(robot, ready) {
      if (kb.on && !api.owns("robot")) { kb.on = kb.seen = false; releaseKeys(); }
      if (kb.on && robot && (robot.keyboard_enabled || robot.keyboard_arming)) kb.seen = true;
      // The service turned keys off (Stop, a button command, an alarm): follow it.
      else if (kb.on && kb.seen && robot && !model.inflight.has("robot:keyboard")) { kb.on = kb.seen = false; releaseKeys(); }
      refs.kbSwitch.setAttribute("aria-checked", kb.on ? "true" : "false");
      refs.kbSwitch.className = "mo-switch" + (kb.on ? " mo-on" : "");
      refs.kbSwitch.disabled = !kb.on && !(ready && robot.enabled === true && !robot.busy && data.kbCap && !model.inflight.has("robot:keyboard-off"));
      refs.kbSpeed.disabled = kb.on || !data.kbCap;
      refs.kbSpeedText.textContent = data.kbCap ? `${clampSpeed(kb.speed, data.kbCap)} / ${data.kbCap}` : "—";
      // The robot service says why it turned keys off (for example, late key updates).
      const reason = !kb.on && typeof robot?.keyboard_disarm_reason === "string" ? robot.keyboard_disarm_reason : "";
      refs.kbOff.textContent = reason ? t("kb.off", { reason }) : "";
      refs.kbOff.hidden = !reason;
      for (const hint of refs.jogHints || []) hint.hidden = !kb.on;
      renderKeys();
    }

    // Stage hold ------------------------------------------------------------

    /** Start 1 stage hold. The console sends hold and keepalive while this page refreshes it. */
    function holdStart(motor, direction, source) {
      if (sh.active || !api.owns("stage") || !focused()) return;
      const limit = data.stage?.limits?.speed || 1000;
      const speed = parseCount(model.axis[motor]?.speed, limit);
      if (!speed) { api.notify(t("axis.badSpeed", { speed: limit }), "warn"); return; }
      const item = { id: holdId(), motor, direction, speed, source, pending: 0 };
      sh.active = item;
      sendHold(item);
      sh.timer = setInterval(() => {
        if (!focused()) stageOff();
        else if (item.pending < 2) sendHold(item);
      }, HOLD_MS);
      renderHold();
    }

    /** Send the hold once. The first send starts it and each later send refreshes it. Never retried. */
    async function sendHold(item) {
      item.pending++;
      let result = null;
      const body = { id: item.id, motor: item.motor, direction: item.direction, speed: item.speed };
      try { result = await api.request("stage/hold", body, { accept: [409, 502], timeout: 2000, socket: true }); }
      catch (error) { result = { outcome: error.status === 0 ? "unknown" : "refused", error: error.message }; }
      finally { item.pending--; }
      if (result?.outcome !== "accepted" && sh.active === item) {
        note("stage", "warning", t(result?.outcome === "unknown" ? "out.unknown" : "out.refused", { device: api.deviceName("stage"), action: actionName("stage", "hold"), error: result?.error || "" }));
        holdEnd();
      }
    }

    /** End this page's hold once. If the release is lost, the console stops the hold after 0.5 s. */
    function holdEnd() {
      if (sh.timer) clearInterval(sh.timer);
      sh.timer = null;
      const item = sh.active;
      if (!item) return;
      sh.active = null;
      api.request("stage/release", { id: item.id }, { timeout: 2000, socket: true }).catch(() => {});
      renderHold();
    }

    function buttonUp() {
      if (sh.active?.source === "button") holdEnd();
    }

    /** End the hold and turn the stage keyboard off. */
    function stageOff() {
      sh.on = false;
      holdEnd();
      renderHold();
    }

    function renderHold() {
      if (refs.stageKb) {
        refs.stageKb.setAttribute("aria-checked", sh.on ? "true" : "false");
        refs.stageKb.className = "mo-switch mo-small" + (sh.on ? " mo-on" : "");
      }
      for (const [key, node] of Object.entries(refs.holdKeys || {})) node.className = "mo-kcap" + (sh.active?.source === key ? " mo-on" : "");
      for (const [id, row] of Object.entries(refs.axes || {})) {
        for (const [direction, node] of [["ccw", row.holdMinus], ["cw", row.holdPlus]]) {
          const on = sh.active?.motor === id && sh.active.direction === direction;
          node.className = "mo-hold" + (on ? " mo-on" : "");
          node.setAttribute("aria-pressed", on ? "true" : "false");
        }
      }
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

    // Station views -------------------------------------------------------
    // Each station frame shows 1 live view. Its tabs inside the frame pick the view.

    function viewTile(view) {
      const factory = root.LabCameras && typeof root.LabCameras.tile === "function" ? root.LabCameras.tile : null;
      let node = null;
      if (factory) {
        try { node = factory(view, { api, large: true }); } catch { node = null; }
      }
      return node || el("div", { class: "mo-cam-off", role: "img", "aria-label": t("view." + view) + ", " + t("cam.offline") },
        el("span", { class: "mo-cam-badge", text: t("cam.offline") }));
    }

    function buildView(station) {
      const slot = el("div", { class: "mo-view-media" });
      const tabs = el("div", { class: "mo-tabs", role: "group", "aria-label": t("view." + station) });
      refs.viewTabs[station] = STATION_VIEWS[station].map(view => {
        const button = el("button", { type: "button", class: "mo-tab", text: t("view." + view), "aria-pressed": "false", onclick: () => pickView(station, view) });
        tabs.append(button);
        return [view, button];
      });
      refs.views[station] = slot;
      return el("div", { class: "mo-view mo-view-" + station }, slot, tabs);
    }

    function dropTile(station) {
      const node = refs.tiles?.[station];
      if (node) { try { node.labCameras?.stop?.(); } catch { /* The camera engine stops a detached tile. */ } }
      if (refs.tiles) delete refs.tiles[station];
    }

    function dropTiles() {
      for (const station of Object.keys(refs.tiles || {})) dropTile(station);
    }

    function renderView(station) {
      const slot = refs.views?.[station];
      if (!slot) return;
      dropTile(station);
      const node = viewTile(model.views[station]);
      refs.tiles[station] = node;
      slot.replaceChildren(node);
      for (const [view, button] of refs.viewTabs[station] || []) {
        const on = view === model.views[station];
        button.setAttribute("aria-pressed", on ? "true" : "false");
        button.className = "mo-tab" + (on ? " mo-on" : "");
      }
    }

    function renderCameras() {
      renderView("arm");
      renderView("stage");
    }

    function pickView(station, view) {
      if (model.views[station] === view) return;
      model.views[station] = view;
      renderView(station);
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
        type: "button", role: "radio", class: "mo-seg-btn", text: stepLabel(mode.value, t("stage.full")), "aria-label": mode.label, "data-value": String(mode.value),
        onclick: () => {
          if (stage && mode.value === data.stage?.microstep) return;
          askConfirm("stage", t("stage.steppingConfirm", { mode: mode.label }), () => send("stage", "set_microstep", { microstep: mode.value }, "microstep"));
        },
      }));
      refs.axes = {};
      refs.holdKeys = {};
      const keyCap = (key, direction) => (refs.holdKeys[key] = el("kbd", { class: "mo-kcap", text: key, title: direction }));
      const keysFor = id => STAGE_KEYS.find(([, , motor]) => motor === id);
      const rows = [];
      const setup = [];
      for (const motor of motors) {
        const id = motor.id;
        const axis = t("axis." + id);
        const directionLabel = direction => t("direction." + STAGE_DIRECTIONS[id][direction]);
        const directions = id === "top" ? ["cw", "ccw"] : ["ccw", "cw"];
        model.axis[id] = model.axis[id] || { steps: String(stage?.defaults?.steps || 50), speed: String(stage?.defaults?.speed || 50) };
        const row = {};
        row.state = el("span", { class: "mo-axstate" });
        row.num = el("b", { class: "mo-num" });
        row.unit = el("span", { class: "mo-unit" });
        row.value = el("span", { class: "mo-axval" }, row.num, row.unit);
        row.ref = el("span", { class: "mo-axstate" });
        const input = (field, props) => {
          const node = el("input", { type: "text", inputmode: "numeric", class: "mo-input mo-narrow", autocomplete: "off", ...props, oninput: event => { model.axis[id][field] = event.target.value; } });
          node.value = model.axis[id][field];
          return node;
        };
        const move = direction => () => {
          const limits = data.stage?.limits || {};
          const steps = parseCount(model.axis[id].steps, limits.steps || 100000);
          const speed = parseCount(model.axis[id].speed, limits.speed || 1000);
          if (!steps || !speed) { api.notify(t("axis.badCount", { max: limits.steps || 100000, speed: limits.speed || 1000 }), "warn"); return; }
          send("stage", "move", { motor: id, direction, speed, steps }, "move-" + id);
        };
        row.minus = el("button", { type: "button", class: "mo-btn", text: t("axis.step", { direction: directionLabel("ccw") }), "aria-label": t("axis.stepLabel", { axis, direction: directionLabel("ccw") }), onclick: move("ccw") });
        row.plus = el("button", { type: "button", class: "mo-btn", text: t("axis.step", { direction: directionLabel("cw") }), "aria-label": t("axis.stepLabel", { axis, direction: directionLabel("cw") }), onclick: move("cw") });
        // The pointer capture sends pointerup to the pressed button, also outside it.
        const holdButton = (direction, text, label) => el("button", {
          type: "button", class: "mo-hold", text, "aria-label": label,
          onpointerdown: event => {
            if (event.button) return;
            event.preventDefault?.();
            try { (event.currentTarget || event.target)?.setPointerCapture?.(event.pointerId); } catch { /* The window pointerup ends it. */ }
            holdStart(id, direction, "button");
          },
          onpointerup: buttonUp, onpointercancel: buttonUp, onlostpointercapture: buttonUp,
          oncontextmenu: event => event.preventDefault?.(),
        });
        row.holdMinus = holdButton("ccw", t("axis.hold", { direction: directionLabel("ccw") }), t("axis.holdLabel", { axis, direction: directionLabel("ccw") }));
        row.holdPlus = holdButton("cw", t("axis.hold", { direction: directionLabel("cw") }), t("axis.holdLabel", { axis, direction: directionLabel("cw") }));
        row.home = motor.homing_available ? el("button", {
          type: "button", class: "mo-btn", text: t("axis.home"),
          onclick: () => askConfirm("stage", t("axis.homeConfirm", { axis }), () => send("stage", "home", { motor: id }, "ref-" + id)),
        }) : null;
        row.check = el("button", { type: "button", class: "mo-btn", text: t("axis.check"), onclick: () => checkReference(id) });
        row.teach = el("button", {
          type: "button", class: "mo-btn", text: t("axis.teach"),
          onclick: () => askConfirm("stage", t("axis.teachConfirm", { axis }), () => send("stage", "teach_reference", { motor: id, confirmed: true }, "ref-" + id, "stage/teach")),
        });
        row.view = el("div", { class: "mo-ref", hidden: true });
        row.zero = el("button", {
          type: "button", class: "mo-btn", text: t("axis.zero"),
          onclick: () => askConfirm("stage", t("axis.zeroConfirm", { axis }), () => send("stage", "zero", { motor: id, confirm: true }, "ref-" + id)),
        });
        refs.axes[id] = row;
        const keys = keysFor(id);
        rows.push(el("div", { class: "mo-axis", role: "group", "aria-label": axis },
          el("span", { class: "mo-axname" }, el("b", { text: axis }), row.state),
          row.value, ...directions.map(direction => direction === "ccw" ? row.holdMinus : row.holdPlus),
          el("label", { class: "mo-axspeed" }, input("speed", { "aria-label": t("axis.speedLabel", { axis }) }), el("span", { text: t("axis.speedUnit") })),
          keys ? el("span", { class: "mo-axkeys", "aria-hidden": "true" }, ...directions.map(direction => keyCap(keys[direction === "ccw" ? 0 : 1], directionLabel(direction)))) : el("span")));
        setup.push(el("div", { class: "mo-setup", role: "group", "aria-label": axis },
          el("span", { class: "mo-axname" }, el("b", { text: axis }), row.ref),
          el("label", { class: "mo-inline" }, t("axis.steps"), input("steps", {})), ...directions.map(direction => direction === "ccw" ? row.minus : row.plus),
          row.home, row.zero, row.check, row.teach, row.view));
      }
      refs.named = [];
      const named = [];
      for (const motor of motors) {
        for (const name of motor.named_positions || []) {
          const button = el("button", {
            type: "button", class: "mo-btn", text: t("named.go", { axis: t("axis." + motor.id), name }),
            onclick: () => send("stage", "goto", { motor: motor.id, name }, "move-" + motor.id),
          });
          refs.named.push({ button, motor: motor.id, name });
          named.push(button);
        }
      }
      refs.stageKb = el("button", {
        type: "button", role: "switch", class: "mo-switch mo-small", "aria-label": t("kb.stage"), "aria-checked": "false",
        onclick: () => {
          if (sh.on) stageOff();
          else if (api.owns("stage")) { sh.on = true; renderHold(); }
        },
      }, el("span", { class: "mo-knob", "aria-hidden": "true" }), el("span", { text: t("stage.keys") }));
      refs.zeroNote = el("p", { class: "mo-reason", hidden: true, text: t("stage.zeroNone") });
      refs.setupNeed = el("p", { class: "mo-reason", hidden: true, text: t("stage.needControl") });
      refs.stageBody.replaceChildren(...rows);
      refs.namedBlock.hidden = !named.length;
      refs.namedBlock.replaceChildren(...(named.length ? [el("span", { class: "mo-label", text: t("named.title") }), ...named] : []));
      refs.stageFoot.replaceChildren(
        refs.modes.length ? el("div", { class: "mo-seg-row" }, el("span", { class: "mo-label", id: "mo-step-label", text: t("stage.stepping") }),
          el("div", { class: "mo-seg", role: "radiogroup", "aria-labelledby": "mo-step-label" }, ...refs.modes)) : "",
        rows.length ? refs.stageKb : "",
        rows.length ? refs.stageFoldEl : "");
      refs.stageFold.replaceChildren(refs.zeroNote, refs.setupNeed, ...setup);
    }

    function buildStageCard() {
      refs.stageStatus = el("span", { class: "mo-state" });
      refs.stageBadge = el("span", { class: "mo-badge", text: t("badge.demo"), hidden: true });
      refs.stageStop = el("button", { type: "button", class: "mo-stop", text: t("stage.stop"), onclick: () => stopDevice("stage") });
      refs.stageBody = el("div", { class: "mo-axes" });
      refs.stageFoot = el("div", { class: "mo-stagefoot" });
      refs.stageFold = el("div", { class: "mo-fold-body" });
      refs.stageFoldEl = el("details", { class: "mo-fold" }, el("summary", { text: t("fold.stage") }), refs.stageFold);
      refs.namedBlock = el("div", { class: "mo-named", hidden: true });
      refs.portSelect = el("select", { class: "mo-input", "aria-label": t("stage.port"), onchange: event => { model.port = event.target.value; } });
      refs.connect = el("button", { type: "button", class: "mo-btn mo-primary", text: t("stage.connect"), onclick: () => send("stage", "connect", { port: model.port }, "connect", "stage/connect").then(() => { model.ports = null; }) });
      refs.refreshPorts = el("button", { type: "button", class: "mo-btn", text: t("stage.refresh"), onclick: () => { if (!model.portsLoading) loadPorts(); } });
      refs.connectRow = el("div", { class: "mo-row mo-connect", hidden: true }, el("label", { class: "mo-inline" }, t("stage.port"), refs.portSelect), refs.connect, refs.refreshPorts);
      refs.stageLast = el("p", { class: "mo-muted mo-last", role: "status" });
      refs.poseBlock = el("div", { class: "mo-named", hidden: true });
      const card = el("section", { class: "mo-station mo-station-stage", "aria-labelledby": "mo-stage-title" },
        buildView("stage"),
        el("div", { class: "mo-body" },
          el("div", { class: "mo-head" },
            el("h2", { id: "mo-stage-title", text: t("stage.title") }), refs.stageStatus, refs.stageBadge,
            el("span", { class: "mo-actions" }, controlButton("stage"), refs.stageStop)),
          refs.connectRow, refs.stageBody, refs.stageFoot, confirmSlot("stage"), refs.namedBlock, refs.poseBlock,
          refs.stageLast));
      buildStage();
      return card;
    }

    /** Named poses move several axes in the configured order with the described `pose` command. */
    function buildPoses() {
      if (!refs.poseBlock) return;
      const poses = data.stage?.available && Array.isArray(data.stage.poses) ? data.stage.poses : [];
      refs.poses = poses.map(name => ({ name, button: el("button", { type: "button", class: "mo-btn", text: name, onclick: () => send("stage", "pose", { name }, "pose") }) }));
      refs.poseNote = el("p", { class: "mo-muted" });
      refs.poseBlock.hidden = !poses.length;
      refs.poseBlock.replaceChildren(...(poses.length ? [el("span", { class: "mo-label", text: t("pose.title") }), ...refs.poses.map(item => item.button), refs.poseNote] : []));
    }

    // Activity --------------------------------------------------------------

    function renderLog() {
      if (!refs.log) return;
      const rows = activityRows(data.events, model.local);
      refs.log.replaceChildren(...(rows.length ? rows.map(row => el("div", { class: "lc-event level-" + row.level },
        el("span", { class: "lc-mono", text: clock(row.ts) }),
        el("span", { class: "lc-event-src", text: api.deviceName(row.src) + (row.page ? " · " + t("log.page") : "") }),
        el("span", { class: "mo-msg", text: row.message }))) : [el("p", { class: "mo-muted", text: t("log.none") })]));
    }

    // Whole workspace -------------------------------------------------------

    function build() {
      if (!rootNode) return;
      dropTiles();
      refs = { confirms: {}, tiles: {}, views: {}, viewTabs: {} };
      refs.log = el("div", { class: "lc-events mo-events" });
      if (root.LabTwin && !positionMonitor) {
        positionHost = el("div", { class: "mo-position tw-compact" });
        positionMonitor = root.LabTwin.positionMonitor();
        positionMonitor.mount(positionHost, api);
      } else positionMonitor?.rebuild();
      rootNode.replaceChildren(
        el("div", { class: "mo-stations" }, buildStageCard(), positionHost, buildArm()),
        el("section", { class: "mo-log", "aria-labelledby": "mo-log-title" },
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

    function lastText(device, command) {
      if (!command?.action) return "";
      const result = command.result ? (t("res." + command.result) === "res." + command.result ? command.result : t("res." + command.result)) : "…";
      const vars = { action: actionName(device, command.action), result, message: command.message || "" };
      return t(command.message && command.result !== "ok" ? "last.message" : "last", vars);
    }

    function updateControl(device, info) {
      const item = refs["ctl-" + device];
      if (!item) return;
      const mine = api.owns(device);
      const other = !mine && Boolean(info?.available && info.holder && info.holder !== "mine");
      item.button.textContent = mine ? t("ctl.release") : other ? t(info.holder === "console" ? "ctl.console" : "ctl.other") : t("ctl.take");
      item.button.setAttribute("aria-label", t(mine ? "ctl.releaseLabel" : "ctl.takeLabel", { device: api.deviceName(device) }));
      item.button.className = "mo-btn" + (mine ? " mo-held" : "");
      // A demo device starts on its first Take control, so "offline" stays clickable.
      item.button.disabled = !mine && (info?.reason === "unconfigured" || other);
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
      const missing = deviceStatus(data.robot);
      // Without fresh feedback the arm is not connected; its enabled flag is then old.
      const fresh = Boolean(robot?.feedback_fresh);
      refs.armStatus.textContent = missing || t(robot.busy ? "arm.busy" : !fresh ? "arm.noFeedback" : robot.enabled ? "arm.enabled" : "arm.disabled");
      refs.armStatus.className = "mo-state" + (missing ? "" : robot.busy ? " mo-info" : !fresh ? " mo-warn" : robot.enabled ? " mo-ok" : "");
      refs.armBadge.hidden = !(robot && robot.demo);
      updateControl("robot", data.robot);
      const ready = Boolean(robot && ownsRobot && robot.feedback_fresh && !robot.locked && !robot.stop_unconfirmed);
      // The robot service reports enabled only in modes 5 and 7, not while it jogs.
      const enabled = model.armOn = Boolean(robot && (robot.enabled === true || robot.busy === true || robot.keyboard_enabled === true));
      refs.enable.textContent = t(enabled ? "arm.disable" : "arm.enable");
      refs.enable.disabled = enabled ? !robot : !ready || busy("robot");
      refs.clear.disabled = !(robot && ownsRobot);
      refs.clear.textContent = t(robot && (robot.locked || robot.stop_unconfirmed) ? "arm.recover" : "arm.clear");
      refs.home.disabled = !(ready && robot.enabled === true && !robot.busy) || model.inflight.has("robot:move");
      // Change live regions only when the text changes, so a screen reader reads it once.
      const locked = Boolean(robot && (robot.locked || robot.stop_unconfirmed));
      const alarms = alarmRows(robot).map(item => t("arm.alarmItem", { id: item.id }) + ": " + item.text);
      if (robot?.robot_error && !alarms.length) alarms.push(t("arm.errorOnly"));
      const alarmText = [locked, ...alarms].join("\n");
      if (alarmText !== refs.alarmText) {
        refs.alarmText = alarmText;
        refs.alarmTitle.textContent = t(locked ? "arm.locked" : "arm.alarm");
        refs.alarms.replaceChildren(...alarms.map(text => el("p", { text })));
        refs.alarmHelp.textContent = locked ? t("arm.lockedHelp") : "";
      }
      refs.alarmHelp.hidden = !locked;
      refs.alarmNote.hidden = !locked && !alarms.length;
      const message = robot?.message ? String(robot.message) : "";
      if (refs.armMessage.textContent !== message) refs.armMessage.textContent = message;
      refs.armMessage.hidden = !message;
      const joints = Array.isArray(robot?.joints) ? robot.joints : [];
      refs.joints.forEach((node, i) => {
        node.textContent = typeof joints[i] === "number" ? fixed(joints[i], 1) : "—";
        node.className = "mo-num-small mo-deg" + (fresh ? "" : " mo-stale");
      });
      // The robot service sends no pose while the arm moves. Keep the last pose, in grey, until a new one arrives.
      if (pose) model.lastPose = pose;
      const shownPose = pose || (robot?.busy ? model.lastPose : null);
      AXES.forEach((axis, i) => {
        refs.pose[i].textContent = shownPose ? fixed(shownPose[i], 2) : "—";
        // Old feedback stays visible in grey, so it never reads as the current pose.
        refs.pose[i].className = "mo-num" + (pose && fresh ? "" : " mo-stale");
      });
      const point = armXY(shownPose), current = Boolean(pose && fresh);
      const positionState = t(!point ? "pose.unknown" : current ? "pose.current" : "pose.last");
      refs.armXYDot.hidden = !point;
      refs.armXYDot.setAttribute("data-stale", String(!current));
      if (point) refs.armXYDot.setAttribute("style", `left:${point.x * 100}%;bottom:${point.y * 100}%`);
      refs.armXYState.textContent = positionState;
      refs.armXYState.setAttribute("data-stale", String(!current));
      refs.armXYScale.textContent = `−${point?.extent || 500} … ${point?.extent || 500} mm`;
      refs.armXYMap.setAttribute("aria-label", t("pose.xy") + ": " + positionState + (point ? `; X ${fixed(shownPose[0], 2)} mm; Y ${fixed(shownPose[1], 2)} mm` : ""));
      const canMove = ready && robot.enabled === true && !robot.busy && Boolean(pose) && !model.inflight.has("robot:move");
      for (const node of Object.values(refs.jog)) node.disabled = !canMove;
      refs.steps.forEach((node, i) => {
        const on = STEPS[i] === model.step;
        node.setAttribute("aria-checked", on ? "true" : "false");
        node.className = "mo-seg-btn" + (on ? " mo-on" : "");
      });
      refs.speed.disabled = !data.cap;
      refs.speedText.textContent = data.cap ? `${clampSpeed(model.speed, data.cap)} / ${data.cap}` : "—";
      refs.useActual.disabled = !pose;
      refs.execute.disabled = !canMove || !data.cap;
      // Suction: the measured outputs, never the last button pressed.
      const vac = vacuumView(robot?.vacuum);
      refs.vacState.textContent = vac.state ? t("vac.state." + vac.state) : "—";
      refs.vacState.className = "mo-vac-state" + (vac.state === "suck" ? " mo-info" : vac.state === "blow" ? " mo-warn" : "");
      refs.vacHeld.hidden = vac.held === null;
      refs.vacHeld.textContent = vac.held === null ? "" : t(vac.held ? "vac.held" : "vac.notHeld");
      refs.vacHeld.className = "mo-vac-held" + (vac.held ? " mo-ok" : "");
      const vacReady = ready && vac.state !== null && !robot.busy && !busy("robot");
      for (const [operation, node] of Object.entries(refs.vac)) {
        node.disabled = !vacReady;
        node.className = "mo-btn" + (operation === "suck" && vac.state === "suck" ? " mo-held" : "");
      }
      // The message line above already says what an ok command did.
      refs.armLast.textContent = robot && robot.last_command?.result !== "ok" ? lastText("robot", robot.last_command) : "";
      updateKeyboard(robot, ready);

      // Stage.
      const stageMissing = deviceStatus(data.stage);
      refs.stageStatus.textContent = stageMissing || (stage.connected ? t("stage.connected") : t("stage.disconnected"))
        + (stage.fault?.message ? ": " + stage.fault.message : "");
      refs.stageStatus.className = "mo-state" + (stageMissing ? "" : stage.fault?.message ? " mo-bad" : stage.connected ? " mo-ok" : " mo-warn");
      refs.stageBadge.hidden = !(stage && stage.demo);
      updateControl("stage", data.stage);
      if ((sh.on || sh.active) && !ownsStage) stageOff();
      const stageReady = Boolean(stage && ownsStage && stage.connected && !stage.locked);
      const idle = (stage?.motors || []).every(motor => motor.mode === "idle");
      for (const node of refs.modes || []) {
        const on = Number(node.getAttribute("data-value")) === stage?.microstep;
        node.setAttribute("aria-checked", on ? "true" : "false");
        node.className = "mo-seg-btn" + (on ? " mo-on" : "");
        node.disabled = !stageReady || !idle || stage?.busy || busy("stage");
      }
      for (const motor of stage?.motors || []) {
        const row = refs.axes?.[motor.id];
        if (!row) continue;
        // An idle axis shows no state; a disconnected stage shows no position.
        const state = axisState(motor);
        row.state.textContent = state.key === "axis.idle" ? "" : t(state.key);
        row.state.className = "mo-axstate mo-tone-" + state.tone;
        // A position in mm needs a confirmed reference. Without one the pulse count is only a count.
        const mm = typeof motor.position_mm === "number" && Number.isFinite(motor.position_mm);
        const known = stage.connected === true && (mm || (motor.position !== null && motor.position !== undefined));
        row.num.textContent = !known ? "—" : mm ? fixed(motor.position_mm, 2) : String(motor.position);
        row.unit.textContent = !known ? "" : mm ? "mm" : t("axis.unit");
        row.value.className = "mo-axval";
        const reference = axisReference(motor);
        row.ref.textContent = t(reference.key);
        row.ref.className = "mo-axstate mo-tone-" + reference.tone;
        const moving = model.inflight.has("stage:move-" + motor.id);
        const canStep = stageReady && motor.available === true && !stage.busy && !moving;
        row.minus.disabled = row.plus.disabled = !canStep;
        // Never disable a held button: a disabled button can miss its pointerup.
        row.holdMinus.disabled = row.holdPlus.disabled = !canStep && sh.active?.motor !== motor.id;
        const refBusy = model.inflight.has("stage:ref-" + motor.id);
        if (row.home) row.home.disabled = !stageReady || motor.mode !== "idle" || stage.busy || refBusy;
        row.zero.hidden = motor.zero_available !== true;
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
        item.button.className = "mo-btn" + (at ? " mo-at" : "");
        item.button.setAttribute("aria-pressed", at ? "true" : "false");
        item.button.disabled = !posesReady || model.inflight.has("stage:pose");
      }
      if (refs.poseNote) refs.poseNote.textContent = stage && !referenced ? t("pose.needRef") : "";
      for (const item of refs.named || []) {
        const motor = (stage?.motors || []).find(m => m.id === item.motor);
        const at = (motor?.at_positions || []).includes(item.name);
        item.button.className = "mo-btn" + (at ? " mo-at" : "");
        item.button.setAttribute("aria-pressed", at ? "true" : "false");
        item.button.disabled = !stageReady || !motor || stage.busy || motor.mode !== "idle" || model.inflight.has("stage:move-" + item.motor);
      }
      if (refs.stageKb) refs.stageKb.disabled = !sh.on && !stageReady;
      if (refs.zeroNote) refs.zeroNote.hidden = !stage?.connected || (stage.motors || []).some(motor => motor.zero_available === true || motor.homing_available === true);
      if (refs.setupNeed) refs.setupNeed.hidden = !stage?.connected || ownsStage;
      renderHold();
      refs.stageLast.textContent = stage ? lastText("stage", stage.last_command) : "";
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
        // The full by-id path is long; it goes in the tooltip.
        ? model.ports.map(item => el("option", { value: item.device, title: item.device, text: portLabel(item) }))
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
        el("p", { class: "mo-muted", text: view.error ? t("axis.checkFailed", { error: view.error }) : offset ? t("axis.offset", { x: fixed(offset[0], 1), y: fixed(offset[1], 1) }) : "" }));
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
          root.addEventListener("blur", () => { disarm("focus"); stageOff(); });
          root.addEventListener("pointerup", buttonUp);
          root.addEventListener("pointercancel", buttonUp);
        }
        root.document?.addEventListener?.("visibilitychange", () => {
          if (root.document.visibilityState !== "visible") { disarm("focus"); stageOff(); }
        });
        build();
        start();
      },
      show() { shown = true; if (api) { renderCameras(); start(); positionMonitor?.show(); } },
      hide() { disarm("focus"); stageOff(); shown = false; stop(); positionMonitor?.hide(); dropTiles(); for (const slot of Object.values(refs.views || {})) slot.replaceChildren(); },
      _test: { data, model, kb, sh, refs: () => refs, refresh, update },
    };
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { logic, i18n, workspace };
  if (root && root.LabConsole && typeof root.LabConsole.register === "function") root.LabConsole.register(workspace());
})(typeof window !== "undefined" ? window : globalThis);
