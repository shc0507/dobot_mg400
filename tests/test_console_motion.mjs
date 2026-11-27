import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const { logic, i18n, workspace } = require("../src/orchestrator/web/static/console/motion.js");

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = []; this.attributes = {}; this.listeners = new Map();
    this.hidden = false; this.disabled = false; this.className = ""; this.ownText = ""; this.value = ""; this.dataset = {};
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("The workspace must not parse data as HTML."); }
  get firstChild() { return this.children[0] || null; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ""; this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { Element.focused = this; }
  addEventListener(event, callback) { this.listeners.set(event, callback); }
  click() { if (!this.disabled) this.listeners.get("click")?.({ target: this }); }
  all() { return [this, ...this.children.flatMap(child => (child instanceof Element ? child.all() : []))]; }
}

function el(tag, props, ...children) {
  const node = new Element(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else if (key === "hidden" || key === "disabled") node[key] = Boolean(value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    if (typeof child === "string") { const text = new Element("#text"); text.textContent = child; node.append(text); } else node.append(child);
  }
  return node;
}

const ROBOT = {
  available: true, holder: "mine", actual: [250, 10, 40, 30, null], enabled: true, busy: false, locked: false,
  stop_unconfirmed: false, feedback_fresh: true, vacuum: { suck: false, blow: false, held: null }, demo: true, last_command: null,
};
const STAGE = {
  available: true, holder: "mine", connected: true, demo: true, busy: false, locked: false, microstep: 8,
  closed_loop: true, telemetry_streaming: true, travel_limits: true, home_switches: true,
  absolute_positioning: true, auto_calibration: true, hold_direction_warnings: true,
  limits: { speed: 5, min_speed: 0.001, min_rotations: 0.001, rotations: 10000 }, defaults: { speed: 0.25, rotations: 0.25 },
  motors: [{ id: "insert", mode: "idle", position_rotations: 0, encoder: "ok", fault: "none", warning: "none",
    motion_ready: true, available: true, positioning_ready: false, max_speed: 0.5, telemetry_age_ms: 20,
    measured_speed: 0, switch_mask: 0, limit_telemetry_age_ms: 20, calibration: "none", cooldown: 0,
    lower_limit_set: false, upper_limit_set: false }],
};

async function mounted({ owns = ["robot", "stage"], robot = ROBOT, stage = STAGE, position = null, operation = null, replies = {} } = {}) {
  const calls = [];
  const sent = [];
  const tables = { en: i18n.en };
  const api = {
    t: (key, vars = {}) => String(tables.en[key] ?? key).replace(/\{(\w+)\}/g, (_, name) => vars[name] ?? ""),
    el, onLanguage() {}, onState() {}, notify: (text, level) => calls.push(["notify", text, level]),
    owns: device => owns.includes(device), deviceName: id => id[0].toUpperCase() + id.slice(1),
    async request(path, data, options = {}) {
      calls.push(["request", path, data]);
      sent.push([path, options]);
      if (replies[path]) return replies[path](data);
      if (path === "state") return { robot, stage, stage_position: position, stage_operation: operation };
      if (path.startsWith("stage/reference/")) return { image: "data:image/jpeg;base64,AAAA", offset_px: [1.5, -2], distance_px: 2.5 };
      return { outcome: "accepted" };
    },
    async consoleRequest(path) { calls.push(["console", path]); return { events: [] }; },
    async describe() {
      return { commands: { execute: { params: { properties: { speed: { maximum: 20 } } } }, keyboard: { params: { properties: { speed: { maximum: 100 } } } } } };
    },
    async relay(device, action, params) { calls.push(["relay", device, action, params]); return { outcome: "accepted" }; },
    async stop(device) { calls.push(["stop", device]); return { device, outcome: "confirmed" }; },
    take: async () => {}, release: async () => {},
  };
  const ws = workspace();
  const root = new Element("section");
  const realSetInterval = globalThis.setInterval;
  globalThis.setInterval = () => 0;
  try { ws.mount(root, api); } finally { globalThis.setInterval = realSetInterval; }
  for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve));
  ws.hide();
  const find = label => root.all().find(node => node.getAttribute("aria-label") === label || (node.tagName === "BUTTON" && node.textContent === label));
  return { ws, root, calls, sent, find, settle: async () => { for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r)); } };
}

function calibratedStage() {
  return { ...structuredClone(STAGE), stop_epoch: 0, motors: [
    ["top", 15, 10, 50], ["bottom", 0, -20, 20], ["insert", 4, 2, 8],
  ].map(([id, value, min, max]) => ({ ...structuredClone(STAGE.motors[0]), id, position_rotations: value,
    positioning_ready: true, lower_limit_set: true, upper_limit_set: true, lower_limit_rotations: min, upper_limit_rotations: max, tolerance: 0.001 })) };
}

function pickTarget(page, kind = "xy", clientX = 160, clientY = 80) {
  const position = page.ws._test.refs().stagePosition, surface = position[kind === "xy" ? "xyMap" : "insertMap"];
  surface.getBoundingClientRect = () => ({ left: 60, top: 30, width: 200, height: 100 });
  surface.listeners.get("dblclick")({ clientX, clientY, preventDefault() {} });
  return position;
}

const pointRequests = page => page.calls.filter(call => call[0] === "request" && call[1] === "stage/position");

test("chart target mapping uses absolute limits, offsets and inverted Y", () => {
  const axes = logic.stagePosition(logic.encoderPosition(calibratedStage()));
  const box = { left: 60, top: 30, width: 200, height: 100 };
  assert.deepEqual(logic.stageTargetAt(axes, "xy", box, 60, 30), { x: 10, y: 20 });
  assert.deepEqual(logic.stageTargetAt(axes, "xy", box, 260, 130), { x: 50, y: -20 });
  assert.deepEqual(logic.stageTargetAt(axes, "xy", box, 110, 55), { x: 20, y: 10 });
  assert.deepEqual(logic.stageTargetAt(axes, "insert", box, 210, 30), { insert: 6.5 });
  assert.equal(logic.stageTargetAt(axes, "xy", { ...box, width: 0 }, 60, 30), null);
  assert.equal(logic.stageTargetAt({ ...axes, x: { range: null } }, "xy", box, 60, 30), null);
});

test("plot mapping excludes borders and padding from the content box", () => {
  const original = globalThis.getComputedStyle;
  globalThis.getComputedStyle = () => ({ borderLeftWidth: "2px", borderRightWidth: "4px", borderTopWidth: "1px", borderBottomWidth: "3px", paddingLeft: "5px", paddingRight: "7px", paddingTop: "6px", paddingBottom: "8px" });
  try {
    assert.deepEqual(logic.plotContentBox({ getBoundingClientRect: () => ({ left: 60, top: 30, width: 218, height: 118 }) }),
      { left: 67, top: 37, width: 200, height: 100 });
  } finally { globalThis.getComputedStyle = original; }
});

test("chart clicks select one target without commands and preserve the measured dots", async () => {
  const page = await mounted({ stage: calibratedStage() });
  try {
    const position = page.ws._test.refs().stagePosition, measured = position.xyDot.getAttribute("style");
    position.xyMap.click();
    assert.equal(page.ws._test.model.stageTarget, null);
    pickTarget(page, "xy", 110, 55);
    assert.deepEqual(page.ws._test.model.stageTarget.target, { x: 20, y: 10 });
    assert.equal(position.xyTarget.getAttribute("style"), "left:25%;bottom:75%");
    assert.equal(position.xyDot.getAttribute("style"), measured);
    assert.equal(position.targetReadout.textContent, "Target · X 20.000 rot · Y 10.000 rot");
    assert.equal(position.xyTarget.hidden, false);
    pickTarget(page, "insert", 210);
    assert.deepEqual(page.ws._test.model.stageTarget.target, { insert: 6.5 });
    assert.equal(position.xyTarget.hidden, true);
    assert.equal(position.insertTarget.hidden, false);
    assert.equal(position.insertDot.hidden, false);
    assert.equal(pointRequests(page).length, 0);
    position.targetCancel.click();
    assert.equal(page.ws._test.model.stageTarget, null);
  } finally { page.ws.hide(); }
});

test("an unconfirmed chart target expires after 5 seconds without sending", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "Date"], now: 100000 });
  const page = await mounted({ stage: calibratedStage() });
  try {
    const position = pickTarget(page);
    t.mock.timers.tick(4999);
    assert.equal(position.xyTarget.hidden, false);
    t.mock.timers.tick(1);
    assert.equal(page.ws._test.model.stageTarget, null);
    assert.equal(position.xyTarget.hidden, true);
    assert.equal(pointRequests(page).length, 0);
  } finally { page.ws.hide(); }
});

test("Confirm sends one target once and shows backend motion and completion", async () => {
  const stage = calibratedStage(), operation = { id: "point-1", status: "moving", mine: true, target: { x: 30, y: 0 } };
  let accept;
  const page = await mounted({ stage, replies: { "stage/position": () => new Promise(resolve => { accept = resolve; }) } });
  try {
    const position = pickTarget(page);
    position.targetConfirm.click(); position.targetConfirm.click();
    assert.equal(pointRequests(page).length, 1);
    assert.deepEqual(pointRequests(page)[0][2], { target: { x: 30, y: 0 }, stop_epoch: 0 });
    assert.equal(position.targetStatus.textContent, "Sending move…");
    assert.equal(position.targetConfirm.hidden, true);
    accept({ operation }); await page.settle();
    assert.equal(position.targetStatus.textContent, "Moving…");
    assert.equal(position.xyTarget.hidden, false);
    page.ws._test.data.stageOperation = { ...operation, status: "completed" };
    page.ws._test.update();
    assert.equal(position.targetStatus.textContent, "Target reached");
    assert.equal(position.xyTarget.getAttribute("data-active"), "false");
  } finally { page.ws.hide(); }
});

test("Insert confirmation sends only Insert and shows a rejected move", async () => {
  const page = await mounted({ stage: calibratedStage(), replies: { "stage/position": () => { throw new Error("Encoder feedback is stale."); } } });
  try {
    const position = pickTarget(page, "insert", 210);
    position.targetConfirm.click(); await page.settle();
    assert.deepEqual(pointRequests(page)[0][2], { target: { insert: 6.5 }, stop_epoch: 0 });
    assert.equal(position.targetStatus.textContent, "Move failed: Encoder feedback is stale.");
    assert.equal(position.insertTarget.hidden, false);
    assert.equal(position.targetConfirm.hidden, true);
  } finally { page.ws.hide(); }
});

test("a lost response reports an unknown result and follows the accepted operation", async () => {
  const page = await mounted({ stage: calibratedStage(), replies: { "stage/position": () => { throw Object.assign(new Error("Connection lost."), { status: 0 }); } } });
  try {
    const position = pickTarget(page); position.targetConfirm.click(); await page.settle();
    assert.equal(position.targetStatus.textContent, "Move result unknown. Check the stage.");
    page.ws._test.data.stageOperation = { id: "point-recovered", status: "moving", mine: true, target: { x: 30, y: 0 } };
    page.ws._test.update();
    assert.equal(position.targetStatus.textContent, "Moving…");
    assert.equal(pointRequests(page).length, 1);
  } finally { page.ws.hide(); }
});

test("target selection requires control, fresh encoders, calibrated limits and idle motors", async () => {
  const mutations = [stage => { stage.connected = false; }, stage => { stage.locked = true; }, stage => { stage.fault = { message: "Fault" }; },
    stage => { stage.motors[0].telemetry_age_ms = 1001; }, stage => { stage.motors[0].positioning_ready = false; },
    stage => { stage.motors[0].upper_limit_set = false; }, stage => { stage.motors[0].upper_limit_rotations = 10; },
    stage => { stage.motors[2].mode = "continuous"; }, stage => { stage.motors[2].calibration = "lower"; }];
  for (const mutate of mutations) {
    const stage = calibratedStage(); mutate(stage);
    const page = await mounted({ stage });
    pickTarget(page);
    assert.equal(page.ws._test.model.stageTarget, null);
    assert.equal(pointRequests(page).length, 0);
  }
  const noControl = await mounted({ stage: calibratedStage(), owns: ["robot"] });
  const position = pickTarget(noControl);
  assert.equal(noControl.ws._test.model.stageTarget, null);
  assert.equal(position.xyMap.getAttribute("aria-disabled"), "true");
  assert.equal(noControl.find("Stop stage").disabled, false);
});

test("Confirm revalidates the lease, feedback, limits, stop epoch and idle state", async () => {
  for (const mutate of [page => { page.ws._test.data.stage.motors[0].telemetry_age_ms = 1001; },
    page => { page.ws._test.data.stage.motors[0].upper_limit_rotations = 60; },
    page => { page.ws._test.data.stage.stop_epoch++; }, page => { page.ws._test.data.stage.motors[1].mode = "continuous"; },
    page => { page.ws._test.data.stage.fault = { message: "Fault" }; }]) {
    const page = await mounted({ stage: calibratedStage() });
    const position = pickTarget(page); mutate(page); position.targetConfirm.click();
    assert.equal(page.ws._test.model.stageTarget, null);
    assert.equal(pointRequests(page).length, 0);
  }
  const owns = ["robot", "stage"], page = await mounted({ stage: calibratedStage(), owns });
  const position = pickTarget(page); owns.pop(); position.targetConfirm.click();
  assert.equal(page.ws._test.model.stageTarget, null);
  assert.equal(pointRequests(page).length, 0);
});

test("poll changes clear pending targets on stale feedback, changed bounds and stop", async () => {
  for (const mutate of [stage => { stage.motors[0].telemetry_age_ms = 1001; },
    stage => { stage.motors[1].upper_limit_rotations = 30; }, stage => { stage.stop_epoch++; }]) {
    const stage = calibratedStage(), page = await mounted({ stage });
    const position = pickTarget(page); mutate(stage); await page.ws._test.refresh();
    assert.equal(position.xyTarget.hidden, true);
    assert.equal(page.ws._test.model.stageTarget, null);
  }
});

test("Stop clears the target and a late acceptance cannot restore it", async () => {
  let accept;
  const page = await mounted({ stage: calibratedStage(), replies: { "stage/position": () => new Promise(resolve => { accept = resolve; }) } });
  const position = pickTarget(page); position.targetConfirm.click(); page.find("Stop stage").click();
  assert.equal(page.ws._test.model.stageTarget, null);
  assert.equal(page.calls.some(call => call[0] === "stop" && call[1] === "stage"), true);
  accept({ operation: { id: "point-1", status: "moving" } }); await page.settle();
  assert.equal(page.ws._test.model.stageTarget, null);
  assert.equal(position.xyTarget.hidden, true);
  // The move was accepted after the Stop: the page stops the stage once more.
  assert.equal(page.calls.filter(call => call[0] === "stop" && call[1] === "stage").length, 2);
});

test("a robot move accepted after a Stop pressed while it was in flight is stopped again", async () => {
  let accept;
  const page = await mounted({ replies: { jog: () => new Promise(resolve => { accept = resolve; }) } });
  try {
    page.ws._test.model.speed = 5;
    page.ws._test.send("robot", "jog X+", { axis: "x", direction: 1, step: 2, speed: 5 }, "move", "jog");
    page.find("Stop arm").click();
    await page.settle();
    accept({ outcome: "accepted" }); await page.settle();
    assert.equal(page.calls.filter(call => call[0] === "stop" && call[1] === "robot").length, 2);
    // Without a Stop in flight, a move sends no stop.
    page.ws._test.send("robot", "jog X+", { axis: "x", direction: 1, step: 2, speed: 5 }, "move", "jog");
    accept({ outcome: "accepted" }); await page.settle();
    assert.equal(page.calls.filter(call => call[0] === "stop" && call[1] === "robot").length, 2);
  } finally { page.ws.hide(); }
});

test("jog targets add one bounded step to one axis", () => {
  assert.deepEqual(logic.jogTarget([250, 10, 40, 30], "x", 1, 2), { x: 252, y: 10, z: 40, r: 30 });
  assert.deepEqual(logic.jogTarget([250, 10, 40, 30], "z", -1, 0.5), { x: 250, y: 10, z: 39.5, r: 30 });
  assert.equal(logic.jogTarget([250, 10, 40, 30], "x", 1, 50), null);
  assert.equal(logic.jogTarget([250, 10, 40, 30], "j1", 1, 2), null);
  assert.equal(logic.jogTarget(null, "x", 1, 2), null);
  assert.equal(logic.actualPose({ actual: [null, null, null, null, 1000] }), null);
});

test("pose, speed and pulse inputs are parsed strictly", () => {
  assert.deepEqual(logic.parsePose({ x: "232.5", y: "-33", z: "+9", r: ".5" }), { x: 232.5, y: -33, z: 9, r: 0.5 });
  assert.equal(logic.parsePose({ x: "1e3", y: "0", z: "0", r: "0" }), null);
  assert.equal(logic.parsePose({ x: "", y: "0", z: "0", r: "0" }), null);
  assert.equal(logic.speedCap({ commands: { execute: { params: { properties: { speed: { maximum: 20 } } } } } }), 20);
  assert.equal(logic.speedCap({}), null);
  assert.equal(logic.clampSpeed(99, 20), 20);
  assert.equal(logic.clampSpeed(0, 20), 1);
  assert.equal(logic.parseCount("50", 100000), 50);
  assert.equal(logic.parseCount("0", 100000), null);
  assert.equal(logic.parseCount("2.5", 100000), null);
});

test("reference badges and the activity list", () => {
  assert.equal(logic.axisReference({ automation_ready: true, homing_available: false, homed: false }).key, "ref.zeroed");
  assert.equal(logic.axisReference({ automation_ready: false, homing_available: true, homed: false }).key, "ref.homeRequired");
  assert.equal(logic.axisReference({ homed: null }).key, "ref.unavailable");
  const rows = logic.activityRows([
    { service: "robot", ts_unix: 2, message: "b" }, { service: "furnace", ts_unix: 3, message: "x" },
    { service: "stage", ts_unix: 1, message: "a" },
  ], [{ ts: 4, src: "robot", level: "info", message: "mine", page: true }]);
  assert.deepEqual(rows.map(row => row.message), ["mine", "b", "a"]);
});

test("both languages define the same strings", () => {
  assert.deepEqual(Object.keys(i18n.zh).sort(), Object.keys(i18n.en).sort());
});

test("stage charts read live encoder rotations without loading Position Finder", async () => {
  const previous = globalThis.LabPositionFinder;
  globalThis.LabPositionFinder = { positionMonitor() { throw new Error("Legacy monitor must stay asleep"); } };
  try {
    const ui = await mounted();
    const position = ui.ws._test.refs().stagePosition;
    assert.equal(position.values.insert.textContent, "0.000");
    assert.equal(position.state.textContent, "Live encoder position");
    assert.equal(position.xyDot.hidden, true);
    assert.equal(position.insertDot.hidden, true);
    assert.ok(ui.root.textContent.includes("X/Y travel"));
    assert.ok(ui.root.textContent.includes("Insert travel"));
    assert.ok(!ui.root.textContent.includes("Target rate"));
    assert.ok(!ui.calls.some(call => String(call[1]).includes("/twin/")));
  } finally { if (previous === undefined) delete globalThis.LabPositionFinder; else globalThis.LabPositionFinder = previous; }
});

test("stage charts use the position connector and clear readings when it becomes unavailable", async () => {
  const position = { available: true, values_rotations: { x: 25, y: 10, insert: 50 },
    limits_rotations: { x: { min: 0, max: 100 }, y: { min: 0, max: 20 }, insert: { min: 0, max: 100 } } };
  const ui = await mounted({ position });
  const refs = ui.ws._test.refs().stagePosition;
  assert.equal(refs.values.x.textContent, "25.000");
  assert.equal(refs.xyDot.hidden, false);
  assert.equal(refs.xyDot.getAttribute("style"), "left:25%;bottom:50%");
  assert.equal(refs.insertDot.getAttribute("style"), "left:50%");
  position.available = false;
  await ui.ws._test.refresh();
  assert.equal(refs.values.x.textContent, "—");
  assert.equal(refs.xyDot.hidden, true);
  assert.equal(refs.xyDot.getAttribute("style"), null);
  assert.equal(refs.insertDot.hidden, true);
});

test("XY corners show each live bound in rotations and clear unavailable bounds", async () => {
  const position = { available: true, values_rotations: { x: 0, y: 0 },
    limits_rotations: { x: { min: -1.125, max: 2.375 }, y: { min: -3.25, max: 4.5 } } };
  const page = await mounted({ position });
  const refs = page.ws._test.refs().stagePosition;
  const labels = name => refs.corners[name].values.map(node => node.textContent);
  assert.deepEqual(labels("lower-left"), ["X min -1.125 rot", "Y min -3.250 rot"]);
  assert.deepEqual(labels("upper-left"), ["X min -1.125 rot", "Y max 4.500 rot"]);
  assert.deepEqual(labels("lower-right"), ["X max 2.375 rot", "Y min -3.250 rot"]);
  assert.deepEqual(labels("upper-right"), ["X max 2.375 rot", "Y max 4.500 rot"]);
  position.limits_rotations.x = { min: -2.625, max: 5.875 };
  position.limits_rotations.y = { min: 10, max: 10 };
  await page.ws._test.refresh();
  assert.deepEqual(labels("upper-left"), ["X min -2.625 rot", "Y max — rot"]);
  assert.deepEqual(labels("lower-right"), ["X max 5.875 rot", "Y min — rot"]);
  position.available = false;
  await page.ws._test.refresh();
  assert.deepEqual(labels("lower-left"), ["X min — rot", "Y min — rot"]);
  assert.deepEqual(labels("upper-right"), ["X max — rot", "Y max — rot"]);
});

test("stage position rejects missing measurements and invalid plot ranges", () => {
  const reading = logic.stagePosition({ available: true, values_rotations: { x: true, y: NaN, insert: 101 },
    limits_rotations: { x: { min: 0, max: 100 }, y: { min: 0, max: Infinity }, insert: { min: 0, max: 100 } } });
  assert.equal(reading.x.value, null);
  assert.equal(reading.y.value, null);
  assert.equal(reading.y.range, null);
  assert.equal(reading.insert.value, 101);
  assert.equal(reading.insert.fraction, null);
});

test("encoder charts map top to X and bottom to Y and never use pulse positions", async () => {
  const motors = ["top", "bottom", "insert"].map((id, index) => ({ ...STAGE.motors[0], id,
    position: 9999, position_mm: 999, position_rotations: index + 0.25,
    lower_limit_set: true, upper_limit_set: true, lower_limit_rotations: 0, upper_limit_rotations: 10 }));
  const stage = { ...STAGE, motors };
  const page = await mounted({ stage });
  const refs = page.ws._test.refs().stagePosition;
  assert.equal(refs.values.x.textContent, "0.250");
  assert.equal(refs.values.y.textContent, "1.250");
  assert.equal(refs.values.insert.textContent, "2.250");
  assert.equal(refs.xyDot.getAttribute("style"), "left:2.5%;bottom:12.5%");
  assert.equal(refs.corners["upper-right"].values[0].textContent, "X max 10.000 rot");
  motors[0].encoder = "untrusted";
  await page.ws._test.refresh();
  assert.equal(refs.values.x.textContent, "—");
  assert.equal(refs.xyDot.hidden, true);
  assert.equal(refs.corners["upper-right"].values[0].textContent, "X max — rot");
});

test("rotation inputs reject invalid numbers and speeds above each motor maximum", async () => {
  for (const invalid of ["", " ", "NaN", "Infinity", "1e2", "0.0", "0.0001", "-0.2"]) {
    assert.equal(logic.parseRotation(invalid, 0.001, 0.5), null);
  }
  assert.equal(logic.parseRotation(".125", 0.001, 0.5), 0.125);
  const page = await mounted();
  const input = page.find("Insert speed, rotations/s");
  input.value = "0.51";
  input.listeners.get("input")({ target: input });
  page.find("Move Insert Out by the rotations").click();
  await page.settle();
  assert.equal(page.calls.some(call => call[0] === "relay"), false);
  assert.ok(page.calls.some(call => call[0] === "notify" && call[1].includes("0.5 rotations/s")));
});

test("calibration phases block new moves while warning history stays advisory", async () => {
  const motor = { ...STAGE.motors[0], calibration: "upper", direction_warning_count: 7, recalibration_recommended: true };
  const page = await mounted({ stage: { ...STAGE, motors: [motor] } });
  assert.equal(page.find("Move Insert Out by the rotations").disabled, true);
  assert.equal(page.find("Insert: Reset position").disabled, true);
  assert.equal(page.find("Stop stage").disabled, false);
  assert.ok(page.root.textContent.includes("seeking upper"));
  motor.calibration = "none";
  await page.ws._test.refresh();
  assert.equal(page.find("Insert: Auto calibrate").disabled, false);
  assert.ok(page.root.textContent.includes("calibration recommended"));
});

test("limit clearing and fault reset remain available with an encoder fault", async () => {
  const motor = { ...STAGE.motors[0], encoder: "untrusted", fault: "tracking_lost", motion_ready: false,
    upper_limit_set: true, lower_limit_set: true };
  const page = await mounted({ stage: { ...STAGE, motors: [motor] } });
  for (const label of ["Clear upper", "Clear lower", "Clear fault"]) assert.equal(page.find("Insert: " + label).disabled, false);
  assert.equal(page.find("Move Insert Out by the rotations").disabled, true);
  assert.ok(page.root.textContent.includes("Fault: tracking_lost"));
});

test("each visible stage axis row clears only its own fault", async () => {
  const motors = ["bottom", "top", "insert"].map(id => ({ ...STAGE.motors[0], id,
    encoder: "untrusted", fault: "tracking_lost", motion_ready: false }));
  const page = await mounted({ stage: { ...STAGE, motors } });
  const refs = page.ws._test.refs();
  const visibleRows = page.root.all().filter(node => node.className === "mo-axis");
  assert.equal(visibleRows.length, 3);
  for (const [index, motor] of motors.entries()) {
    const button = refs.axes[motor.id].clearFault;
    assert.equal(button.textContent, "Clear fault");
    assert.ok(visibleRows[index].all().includes(button));
    assert.ok(!refs.stageFold.all().includes(button));
    assert.equal(button.disabled, false);
    button.click();
    button.click();
    await page.settle();
  }
  assert.deepEqual(page.calls.filter(call => call[0] === "relay"), motors.map(motor =>
    ["relay", "stage", "clear_fault", { motor: motor.id }]));
});

test("Clear fault needs stage control, an idle axis and an available encoder controller", async () => {
  for (const options of [
    { owns: [] },
    { stage: { ...STAGE, connected: false } },
    { stage: { ...STAGE, closed_loop: false } },
    { stage: { ...STAGE, locked: true } },
    ...[{ mode: "continuous" }, { calibration: "upper" }, { cooldown: 1 }].map(state =>
      ({ stage: { ...STAGE, motors: [{ ...STAGE.motors[0], ...state }] } })),
  ]) {
    const page = await mounted(options);
    const button = page.find("Insert: Clear fault");
    assert.equal(button.disabled, true);
    button.click();
    await page.settle();
    assert.equal(page.calls.some(call => call[0] === "relay"), false);
    assert.equal(page.find("Stop stage").disabled, false);
  }
});

test("a jog goes through the checked jog route once", async () => {
  const page = await mounted();
  page.find("Move +X by the jog step").click();
  page.find("Move +X by the jog step").click();
  await page.settle();
  const jogs = page.calls.filter(call => call[0] === "request" && call[1] === "jog");
  assert.deepEqual(jogs, [["request", "jog", { axis: "x", direction: 1, step: 2, speed: 10 }]]);
  assert.equal(page.calls.some(call => call[0] === "relay" && call[2] === "execute"), false);
});

test("without control the arm cannot move but Stop stage and Disable stay available", async () => {
  const page = await mounted({ owns: [], robot: { ...ROBOT, holder: null }, stage: { ...STAGE, holder: null } });
  assert.equal(page.find("Move +X by the jog step").disabled, true);
  assert.equal(page.find("Execute").disabled, true);
  assert.equal(page.find("Disable").disabled, false);
  page.find("Stop stage").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "stop"), [["stop", "stage"]]);
});

test("the suction state comes from the measured outputs; alarm rows are parsed strictly", () => {
  assert.deepEqual(logic.vacuumView({ suck: true, blow: false, held: null }), { state: "suck", held: null });
  assert.deepEqual(logic.vacuumView({ suck: false, blow: true, held: true }), { state: "blow", held: true });
  assert.deepEqual(logic.vacuumView({ suck: false, blow: false, held: false }), { state: "off", held: false });
  assert.deepEqual(logic.vacuumView({ suck: null, blow: null, held: null }), { state: null, held: null });
  assert.deepEqual(logic.vacuumView(undefined), { state: null, held: null });
  assert.deepEqual(logic.alarmRows({ alarms: [
    { id: 22, source: "controller", description: "Joint 2 limit", solution: "Jog J2 back." },
    { id: 3, source: "servo", description: "Overload", solution: "" },
  ] }), [{ id: "22", text: "Joint 2 limit Jog J2 back." }, { id: "3", text: "Overload (servo)" }]);
  assert.deepEqual(logic.alarmRows({}), []);
});

test("the arm card shows joints, alarms and the service message; Stop arm needs no control", async () => {
  const robot = { ...ROBOT, robot_mode: 9, robot_error: true, joints: [1.5, -20, 3, 44.26], message: "Robot stop is confirmed. Disable the robot before recovery.",
    alarms: [{ id: 22, source: "controller", description: "Joint 2 limit", solution: "Jog J2 back." }] };
  const page = await mounted({ owns: [], robot: { ...robot, holder: null } });
  const texts = page.root.all().map(node => node.ownText);
  assert.ok(texts.includes("44.3") && texts.includes("-20.0"));
  assert.ok(texts.includes("Alarm 22: Joint 2 limit Jog J2 back."));
  // The alarm shows in a note with the button that clears it.
  const note = page.root.all().find(node => node.className === "mo-alarm");
  assert.equal(note.hidden, false);
  assert.equal(note.getAttribute("role"), "alert");
  assert.match(note.textContent, /^Alarm.*Alarm 22: Joint 2 limit.*Clear alarm$/);
  assert.ok(texts.includes("Robot stop is confirmed. Disable the robot before recovery."));
  assert.equal(page.find("Home…").disabled, true);
  assert.equal(page.find("Suction: Suck").disabled, true);
  page.find("Stop arm").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "stop"), [["stop", "robot"]]);
});

const clearButton = page => page.root.all().find(node => node.tagName === "BUTTON" && /^(Recover|Clear alarm)$/.test(node.textContent));

test("an unconfirmed stop shows in the alarm note with Recover; a quiet arm has no note", async () => {
  const locked = await mounted({ robot: { ...ROBOT, locked: true, stop_unconfirmed: true, recoverable: true } });
  const note = locked.root.all().find(node => node.className === "mo-alarm");
  assert.equal(note.hidden, false);
  assert.match(note.textContent, /^Stop not confirmed.*Check the arm, then press Recover\..*Recover$/);
  assert.equal(clearButton(locked).hidden, false);
  assert.equal(clearButton(locked).disabled, false);
  assert.doesNotMatch(locked.root.all().find(node => node.ownText.startsWith("Enabled")).ownText, /Stop not confirmed|Alarm/);
  const quiet = await mounted();
  assert.equal(quiet.root.all().find(node => node.className === "mo-alarm").hidden, true);
  // An error without an alarm list still shows the note.
  const error = await mounted({ robot: { ...ROBOT, robot_error: true } });
  assert.match(error.root.all().find(node => node.className === "mo-alarm").textContent, /The controller reports an error\./);
});

test("Recover shows only when the service accepts it; a confirmed stop asks for Disable", async () => {
  // A confirmed stop keeps the lock until a verified disable. The service refuses clear_alarm.
  for (const enabled of [true, false]) {
    const stopped = await mounted({ robot: { ...ROBOT, enabled, locked: true, stop_unconfirmed: false, recoverable: false } });
    const note = stopped.root.all().find(node => node.className === "mo-alarm");
    assert.equal(note.hidden, false);
    assert.match(note.textContent, /^Stopped.*Press Disable to unlock the arm\./);
    assert.equal(clearButton(stopped).hidden, true);
    assert.equal(stopped.find("Disable").disabled, false);
    stopped.find("Disable").click();
    await stopped.settle();
    assert.deepEqual(stopped.calls.filter(call => call[0] === "relay").map(call => call[2]), ["disable"]);
  }
  // An unconfirmed stop without fresh feedback is not recoverable yet.
  const waiting = await mounted({ robot: { ...ROBOT, feedback_fresh: false, locked: true, stop_unconfirmed: true, recoverable: false } });
  assert.equal(clearButton(waiting).hidden, true);
  assert.match(waiting.root.all().find(node => node.className === "mo-alarm").textContent, /Waiting for fresh feedback\./);
  // During an alarm the service clears the alarm first.
  const alarm = await mounted({ robot: { ...ROBOT, robot_mode: 9, locked: true, stop_unconfirmed: true, recoverable: false } });
  assert.equal(clearButton(alarm).hidden, false);
  assert.equal(clearButton(alarm).textContent, "Clear alarm");
  assert.match(alarm.root.all().find(node => node.className === "mo-alarm").textContent, /Fix the alarm cause, then press Clear alarm\./);
});

test("the activity names each command by its button label", async () => {
  const page = await mounted();
  page.find("Disable").click();
  await page.settle();
  assert.match(page.root.textContent, /Robot Disable: sent/);
  page.find("Stop arm").click();
  await page.settle();
  assert.match(page.root.textContent, /Robot stop: confirmed/);
});

test("Disable stays offered while the arm jogs; Enable needs a disabled arm and control", async () => {
  // The robot service reports enabled only in modes 5 and 7, not while it jogs.
  const jogging = await mounted({ robot: { ...ROBOT, enabled: false, keyboard_enabled: true } });
  assert.equal(jogging.find("Disable").disabled, false);
  jogging.find("Disable").click();
  await jogging.settle();
  assert.deepEqual(jogging.calls.filter(call => call[0] === "relay").map(call => call[2]), ["disable"]);
  const off = await mounted({ owns: [], robot: { ...ROBOT, enabled: false } });
  assert.equal(off.find("Enable").disabled, true);
  assert.equal(off.find("Disable"), undefined);
});

test("each station switches its view with the tabs inside the frame", async () => {
  const page = await mounted();
  for (const [station, labels] of [
    ["stage", ["Microscope", "Stage", "Microscope side", "Insert rail", "Tray", "Thermal"]],
    ["arm", ["Insert rail", "Tray", "Microscope side", "Stage", "Microscope", "Thermal"]],
  ]) {
    const frame = page.root.all().find(node => node.className === "mo-view mo-view-" + station);
    const tabs = frame.all().filter(node => node.className.startsWith("mo-tab") && node.tagName === "BUTTON");
    assert.deepEqual(tabs.map(node => node.textContent), labels);
    assert.equal(tabs[0].getAttribute("aria-pressed"), "true");
    for (const tab of [...tabs.slice(1), tabs[0]]) {
      tab.click();
      assert.equal(tab.getAttribute("aria-pressed"), "true");
      assert.equal(tabs.filter(node => node.getAttribute("aria-pressed") === "true").length, 1);
      assert.ok(frame.all().some(node => node.getAttribute("aria-label") === `${tab.textContent}, OFFLINE`));
    }
  }
  assert.equal(page.root.all().some(node => /Swap view/.test(node.textContent)), false);
  assert.equal(page.calls.some(call => call[0] === "relay"), false);
});

test("only a demo device shows a badge, and a held device names its holder on the button", async () => {
  const demo = await mounted();
  assert.equal(demo.root.all().filter(node => node.className === "mo-badge" && !node.hidden).length, 2);
  const real = await mounted({ owns: [], robot: { ...ROBOT, demo: false, holder: "other" }, stage: { ...STAGE, demo: false, holder: null } });
  assert.equal(real.root.all().filter(node => node.className === "mo-badge" && !node.hidden).length, 0);
  assert.equal(real.root.all().some(node => /REAL/.test(node.ownText)), false);
  const robot = real.find("Take control of Robot");
  assert.equal(robot.textContent, "In use by another client");
  assert.equal(robot.disabled, true);
  assert.equal(real.find("Take control of Stage").textContent, "Take control");
  assert.equal(real.root.all().some(node => ["Free", "You control it"].includes(node.ownText)), false);
});

test("old pulse firmware cannot move; Stop remains available and stepping is fixed", async () => {
  const page = await mounted({ stage: { ...STAGE, closed_loop: false } });
  assert.equal(page.find("Hold to move Insert Out").disabled, true);
  assert.equal(page.find("Insert: Auto calibrate").disabled, true);
  assert.equal(page.find("Stop stage").disabled, false);
  const note = page.ws._test.refs().zeroNote;
  assert.equal(note.hidden, false);
  assert.ok(page.root.textContent.includes("1/8 stepping"));
  assert.equal(page.find("Half step (1/2)"), undefined);
});

test("a confirmation takes the focus, and Esc closes it without sending", async () => {
  const page = await mounted();
  page.find("Home…").click();
  const dialog = Element.focused;
  assert.equal(dialog.getAttribute("role"), "alertdialog");
  assert.match(dialog.getAttribute("aria-label"), /Home moves all 4 joints/);
  dialog.listeners.get("keydown")({ key: "Escape", preventDefault() {} });
  assert.equal(dialog.hidden, true);
  await page.settle();
  assert.equal(page.calls.some(call => call[0] === "relay"), false);
});

test("Home sends only after the page confirmation", async () => {
  const page = await mounted();
  page.find("Home…").click();
  await page.settle();
  assert.equal(page.calls.some(call => call[0] === "relay"), false);
  page.find("Confirm").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "relay"), [["relay", "robot", "home", {}]]);
});

test("Suck and Drop send 1 vacuum command each and show the measured state; no Off button", async () => {
  const page = await mounted({ robot: { ...ROBOT, vacuum: { suck: true, blow: false, held: null } } });
  const texts = page.root.all().map(node => node.ownText);
  assert.ok(texts.includes("Sucking"));
  assert.equal(page.find("Suction: Suck").className, "mo-btn mo-held");
  // Without a grip switch, the page shows no held state.
  assert.equal(page.root.all().find(node => node.className.startsWith("mo-vac-held")).hidden, true);
  assert.equal(page.find("Suction: Off"), undefined);
  for (const label of ["Suck", "Drop"]) {
    page.find("Suction: " + label).click();
    await page.settle();
  }
  assert.deepEqual(page.calls.filter(call => call[0] === "relay"), [
    ["relay", "robot", "vacuum", { operation: "suck" }],
    ["relay", "robot", "vacuum", { operation: "release" }],
  ]);
});

test("the suction buttons wait for a measured state and show held when the service reports it", async () => {
  const unknown = await mounted({ robot: { ...ROBOT, vacuum: { suck: null, blow: null, held: null } } });
  assert.equal(unknown.find("Suction: Suck").disabled, true);
  assert.equal(unknown.find("Suction: Drop").disabled, true);
  const held = await mounted({ robot: { ...ROBOT, vacuum: { suck: true, blow: false, held: true } } });
  const node = held.root.all().find(item => item.className.startsWith("mo-vac-held"));
  assert.equal(node.hidden, false);
  assert.equal(node.textContent, "Held");
});

test("the suction row shows the gauge line pressure, or a dash without a fresh reading", async () => {
  assert.equal(logic.pressureText({ abs_hpa: 960.1, gauge_hpa: -52.34 }), "−52.3");
  assert.equal(logic.pressureText({ abs_hpa: 1009.3, gauge_hpa: 0.06 }), "0.1");
  assert.equal(logic.pressureText({ abs_hpa: 1009.3, gauge_hpa: -0.04 }), "0.0");
  assert.equal(logic.pressureText(null), null);
  assert.equal(logic.pressureText({ gauge_hpa: "1" }), null);
  const reading = await mounted({ robot: { ...ROBOT, pressure: { abs_hpa: 960.1, gauge_hpa: -52.34 } } });
  const node = reading.root.all().find(item => item.className === "mo-vac-pressure");
  assert.equal(node.textContent, "−52.3hPa");
  const missing = await mounted({ robot: { ...ROBOT, pressure: null } });
  assert.equal(missing.root.all().find(item => item.className === "mo-vac-pressure").textContent, "—hPa");
});

test("an absolute target needs calibration and sends fractional encoder coordinates", async () => {
  const motor = { ...STAGE.motors[0], positioning_ready: true, calibration: "done",
    lower_limit_set: true, upper_limit_set: true, lower_limit_rotations: 0, upper_limit_rotations: 12.5 };
  const page = await mounted({ stage: { ...STAGE, motors: [motor] } });
  const input = page.find("Insert: Target rotation");
  input.value = "2.375";
  input.listeners.get("input")({ target: input });
  page.find("Insert: Move to target").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "relay"), [
    ["relay", "stage", "move_to_position", { motor: "insert", position: 2.375, speed: 0.25 }],
  ]);
  const waiting = await mounted();
  assert.equal(waiting.find("Insert: Move to target").disabled, true);
});

test("encoder setup actions use the stage relay and Stop needs no control", async () => {
  const motor = { ...STAGE.motors[0], position_rotations: 1.25, upper_limit_set: true, lower_limit_set: true };
  const page = await mounted({ stage: { ...STAGE, motors: [motor] } });
  for (const [label, action] of [["Reset position", "reset_position"], ["Set limit here", "set_limit"],
    ["Clear upper", "clear_upper_limit"], ["Clear lower", "clear_lower_limit"], ["Clear fault", "clear_fault"]]) {
    page.find("Insert: " + label).click();
    await page.settle();
    assert.deepEqual(page.calls.filter(call => call[0] === "relay").at(-1), ["relay", "stage", action, { motor: "insert" }]);
  }
  page.find("Insert: Auto calibrate").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "relay").at(-1), ["relay", "stage", "calibrate", { motor: "insert", speed: 0.25 }]);
  const unowned = await mounted({ owns: [] });
  unowned.find("Insert: Stop").click();
  await unowned.settle();
  assert.deepEqual(unowned.calls.filter(call => call[0] === "stop"), [["stop", "stage"]]);
});

test("fractional relative moves need encoder feedback but no calibration", async () => {
  const page = await mounted();
  const rotations = page.find("Insert: Relative rotations");
  rotations.value = "0.125";
  rotations.listeners.get("input")({ target: rotations });
  const speed = page.find("Insert speed, rotations/s");
  speed.value = "0.1";
  speed.listeners.get("input")({ target: speed });
  page.find("Move Insert Out by the rotations").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "relay"), [["relay", "stage", "move", { motor: "insert", direction: "cw", speed: 0.1, rotations: 0.125 }]]);
  assert.equal(rotations.value, "0.125");
  assert.equal(speed.value, "0.1");
});

test("an arm without fresh feedback reads Not connected and greys its last pose", async () => {
  const page = await mounted({ robot: { ...ROBOT, enabled: false, feedback_fresh: false, joints: [1, 2, 3, 4],
    message: "Robot disable and rest are confirmed.", last_command: { action: "stop_sync", result: "ok", message: "Robot disable and rest are confirmed." } } });
  const texts = page.root.all().map(node => node.ownText);
  assert.ok(texts.includes("Not connected"));
  assert.equal(texts.includes("Disabled"), false);
  const pose = page.root.all().filter(node => node.className === "mo-num mo-stale" || node.className.startsWith("mo-num-small mo-deg"));
  assert.equal(pose.length, 8);
  assert.ok(pose.every(node => node.className.includes("mo-stale")));
  // An ok command shows only the service message, not a second Last line.
  assert.equal(texts.filter(text => text.includes("Robot disable and rest are confirmed.")).length, 1);
  assert.equal(texts.some(text => text.startsWith("Last:")), false);
});

test("arm XY plot follows controller coordinates, marks retained poses and hides unknown positions", async () => {
  const page = await mounted({ robot: { ...ROBOT, actual: [250, -100, 40, 30] } });
  const refs = page.ws._test.refs();
  assert.equal(refs.armXYDot.getAttribute("style"), "left:75%;bottom:40%");
  assert.equal(refs.armXYDot.getAttribute("data-stale"), "false");
  assert.match(refs.armXYMap.getAttribute("aria-label"), /X 250.00 mm; Y -100.00 mm/);
  assert.equal(page.root.all().find(node => node.className === "mo-pose mo-zr-readings").textContent, "Z40.00mmR30.00°");
  page.ws._test.data.robot = { ...ROBOT, actual: null, busy: true };
  page.ws._test.update();
  assert.equal(refs.armXYState.textContent, "Last known");
  assert.equal(refs.armXYDot.hidden, false);
  assert.equal(refs.armXYDot.getAttribute("data-stale"), "true");
  page.ws._test.data.robot = { ...ROBOT, actual: [650, -200, 40, 30] };
  page.ws._test.update();
  assert.equal(refs.armXYScale.textContent, "−700 … 700 mm");
  page.ws._test.data.robot = { ...ROBOT, actual: null };
  page.ws._test.update();
  assert.equal(refs.armXYDot.hidden, true);
  assert.equal(refs.armXYState.textContent, "No position");
});

test("an idle axis shows no state, a disconnected stage no position, and ports get short labels", async () => {
  const connected = await mounted();
  assert.equal(connected.root.all().some(node => node.ownText === "Idle"), false);
  const page = await mounted({ stage: { ...STAGE, connected: false } });
  await page.settle();
  assert.ok(page.root.all().some(node => node.ownText === "—"));
  assert.equal(logic.portLabel({ device: "/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_1453-if00", description: "ttyACM0" }), "ttyACM0");
  assert.equal(logic.portLabel({ device: "/dev/ttyACM0", description: "Arduino" }), "Arduino · ttyACM0");
  assert.equal(logic.portLabel({ device: "/dev/ttyUSB0" }), "ttyUSB0");
});

test("a disconnected stage lists the state ports and sends 1 connect command", async () => {
  const page = await mounted({ stage: { ...STAGE, connected: false, ports: [{ device: "/dev/ttyACM0", description: "Arduino" }] } });
  await page.settle();
  // The card is rebuilt after the robot description loads; the new select still lists the port.
  const select = page.root.all().find(node => node.tagName === "SELECT");
  assert.deepEqual(select.children.map(node => node.ownText), ["Arduino · ttyACM0"]);
  page.find("Connect").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "relay"), [["relay", "stage", "connect", { port: "/dev/ttyACM0" }]]);
});

test("stale encoder feedback hides measured position and disables new moves without a camera reference", async () => {
  const stage = structuredClone(STAGE);
  const page = await mounted({ stage });
  assert.equal(page.find("Check reference"), undefined);
  stage.motors[0].telemetry_age_ms = 1500;
  await page.ws._test.refresh();
  assert.equal(page.ws._test.refs().stagePosition.values.insert.textContent, "—");
  assert.equal(page.find("Hold to move Insert Out").disabled, true);
  assert.ok(page.root.textContent.includes("Feedback stale"));
  assert.equal(page.find("Stop stage").disabled, false);
});

/** A window and a document with listeners, and interval callbacks the test runs by hand. */
async function keyboardPage(options = {}) {
  const win = new Map(), docListeners = new Map(), intervals = [];
  const saved = { add: globalThis.addEventListener, document: globalThis.document, set: globalThis.setInterval, clear: globalThis.clearInterval };
  globalThis.addEventListener = (event, callback) => win.set(event, callback);
  globalThis.document = { visibilityState: "visible", hasFocus: () => true, addEventListener: (event, callback) => docListeners.set(event, callback) };
  const page = await mounted(options);
  globalThis.setInterval = callback => intervals.push(callback);
  globalThis.clearInterval = id => { if (id) intervals[id - 1] = null; };
  page.ws.show();
  await page.settle();
  page.calls.length = 0;
  const restore = () => {
    globalThis.addEventListener = saved.add; globalThis.document = saved.document;
    globalThis.setInterval = saved.set; globalThis.clearInterval = saved.clear;
  };
  const key = (type, code, extra = {}) => win.get(type)({ code, target: null, preventDefault() {}, ...extra });
  const keyCalls = () => page.calls.filter(call => (call[0] === "request" && call[1] === "keys") || (call[0] === "relay" && call[2] === "keyboard"));
  const holdCalls = () => page.calls.filter(call => call[0] === "request" && (call[1] === "stage/hold" || call[1] === "stage/release"));
  return { ...page, win, docListeners, intervals, restore, key, keyCalls, holdCalls };
}

test("pending targets clear on STOP ALL, pagehide, blur, hidden tabs and destroy", async () => {
  for (const leave of [page => page.docListeners.get("click")({ target: { closest: selector => selector === "#lc-stop-all" } }),
    page => page.win.get("pagehide")(), page => page.win.get("blur")(),
    page => { globalThis.document.visibilityState = "hidden"; page.docListeners.get("visibilitychange")(); },
    page => page.ws.hide(), page => page.ws.destroy()]) {
    const page = await keyboardPage({ stage: calibratedStage() });
    try {
      const position = pickTarget(page);
      assert.equal(position.xyTarget.hidden, false);
      leave(page);
      assert.equal(position.xyTarget.hidden, true);
      assert.equal(page.ws._test.model.stageTarget, null);
      assert.equal(pointRequests(page).length, 0);
    } finally { page.ws.hide(); page.restore(); }
  }
});

test("stage keyboard and other motion commands clear an unconfirmed chart target", async () => {
  const page = await keyboardPage({ stage: calibratedStage() });
  try {
    page.find("Stage keyboard").click();
    let position = pickTarget(page);
    page.key("keydown", "KeyI");
    assert.equal(position.xyTarget.hidden, true);
    assert.equal(page.ws._test.model.stageTarget, null);
    page.key("keyup", "KeyI"); await page.settle();
    position = pickTarget(page);
    page.ws._test.refs().axes.insert.plus.click();
    assert.equal(position.xyTarget.hidden, true);
    assert.equal(pointRequests(page).length, 0);
  } finally { page.ws.hide(); page.restore(); }
});

test("while the keyboard is on, the Z and R jog buttons show the key that moves the same way", async () => {
  const page = await keyboardPage();
  try {
    const hints = ["Z ↑", "Z ↓", "R +", "R −"].map(label => page.find(`Move ${label} by the jog step`).children[1]);
    assert.deepEqual(hints.map(hint => hint.ownText), ["R", "F", "Q", "E"]);
    assert.ok(hints.every(hint => hint.hidden));
    assert.equal(page.find("Move +X by the jog step").children.length, 1);
    page.find("Keyboard jog").click();
    await page.settle();
    assert.ok(hints.every(hint => !hint.hidden));
    // The step chips carry numbers; the unit line names both units.
    assert.ok(page.root.all().some(node => node.ownText === "mm · R in °"));
    assert.equal(page.find("2").getAttribute("role"), "radio");
  } finally { page.restore?.(); }
});

test("keyboard jog arms, refreshes held keys and releases them on blur", async () => {
  const page = await keyboardPage();
  try {
    const before = page.intervals.length;
    page.find("Keyboard jog").click();
    await page.settle();
    page.key("keydown", "KeyW");
    page.key("keydown", "KeyW", { repeat: true });
    page.key("keydown", "KeyP");
    await page.settle();
    // The refresh timer resends the held set while a key is held.
    const refreshTimer = page.intervals.slice(before).find(Boolean);
    refreshTimer();
    await page.settle();
    page.win.get("blur")();
    await page.settle();
    assert.deepEqual(page.keyCalls(), [
      ["relay", "robot", "keyboard", { enabled: true, seq: 1, speed: 10 }],
      ["request", "keys", { seq: 2, held: ["W"] }],
      ["request", "keys", { seq: 3, held: ["W"] }],
      ["request", "keys", { seq: 4, held: [] }],
      ["relay", "robot", "keyboard", { enabled: false, seq: 5, reason: "focus" }],
    ]);
    assert.equal(page.ws._test.kb.on, false);
  } finally { page.restore(); }
});

test("a modifier, a context menu or a drag start releases held jog and stage keys", async () => {
  const leaves = [
    page => page.key("keydown", "MetaLeft", { key: "Meta", metaKey: true }),
    page => page.key("keydown", "KeyC", { ctrlKey: true }),
    page => page.key("keydown", "AltLeft", { key: "Alt" }),
    page => page.win.get("contextmenu")(),
    page => page.win.get("dragstart")(),
  ];
  for (const [index, leave] of leaves.entries()) {
    const page = await keyboardPage({ owns: ["robot", "stage"] });
    try {
      page.find("Keyboard jog").click();
      page.find("Stage keyboard").click();
      await page.settle();
      const before = page.intervals.length;
      page.key("keydown", "KeyW");
      page.key("keydown", "KeyU");
      await page.settle();
      leave(page);
      await page.settle();
      assert.equal(page.ws._test.kb.held.size, 0, `leave ${index}`);
      assert.equal(page.ws._test.kb.on, false, `leave ${index}`);
      assert.equal(page.ws._test.sh.on, false, `leave ${index}`);
      assert.equal(page.ws._test.sh.active, null, `leave ${index}`);
      assert.deepEqual(page.holdCalls().map(call => call[1]), ["stage/hold", "stage/release"], `leave ${index}`);
      const off = page.keyCalls().filter(call => call[3]?.enabled === false).map(call => call[3].reason);
      assert.deepEqual(off, [index < 3 ? "modifier" : "focus"], `leave ${index}`);
      // No timer keeps sending the held key after the release.
      const timers = page.intervals.slice(before);
      assert.equal(timers.length, 2, `leave ${index}`);
      assert.ok(timers.every(callback => callback === null), `leave ${index}`);
    } finally { page.restore(); }
  }
});

test("a modifier without a held key leaves the keyboard armed", async () => {
  const page = await keyboardPage();
  try {
    page.find("Keyboard jog").click();
    await page.settle();
    page.key("keydown", "MetaLeft", { key: "Meta", metaKey: true });
    page.key("keydown", "KeyW", { metaKey: true });
    await page.settle();
    assert.equal(page.ws._test.kb.on, true);
    assert.equal(page.keyCalls().length, 1);
  } finally { page.restore(); }
});

test("keyboard jog disarms when the tab is hidden and when the Motion tab is left", async () => {
  const page = await keyboardPage();
  try {
    page.find("Keyboard jog").click();
    await page.settle();
    globalThis.document.visibilityState = "hidden";
    page.docListeners.get("visibilitychange")();
    await page.settle();
    globalThis.document.visibilityState = "visible";
    page.find("Keyboard jog").click();
    await page.settle();
    page.ws.hide();
    await page.settle();
    assert.deepEqual(page.keyCalls().filter(call => call[3]?.enabled === false).map(call => call[3]), [
      { enabled: false, seq: 2, reason: "focus" }, { enabled: false, seq: 4, reason: "focus" },
    ]);
    // With the keyboard off no key is sent.
    page.ws.show();
    page.key("keydown", "KeyW");
    assert.equal(page.keyCalls().some(call => call[1] === "keys"), false);
  } finally { page.restore(); }
});

test("the switch turns off when the service disarms before the page saw it on, and on Stop arm", async () => {
  const robot = { ...ROBOT, keyboard_enabled: false, keyboard_disarm_reason: "old reason." };
  const page = await keyboardPage({ robot });
  try {
    page.find("Keyboard jog").click();
    await page.settle();
    // The old reason from before the arm keeps the switch on.
    await page.ws._test.refresh();
    assert.equal(page.ws._test.kb.on, true);
    Object.assign(robot, { keyboard_disarm_reason: "the arm stopped." });
    await page.ws._test.refresh();
    assert.equal(page.ws._test.kb.on, false);
    assert.equal(page.ws._test.refs().kbOff.textContent, "Off: the arm stopped.");
    Object.assign(robot, { keyboard_enabled: true, keyboard_disarm_reason: null });
    page.find("Keyboard jog").click();
    await page.settle();
    assert.equal(page.ws._test.kb.on, true);
    page.find("Stop arm").click();
    assert.equal(page.ws._test.kb.on, false);
  } finally { page.restore(); }
});

test("the switch shows why the robot service turned the keyboard off", async () => {
  const robot = { ...ROBOT, keyboard_enabled: true, keyboard_disarm_reason: null };
  const page = await keyboardPage({ robot });
  try {
    const off = page.ws._test.refs().kbOff;
    page.find("Keyboard jog").click();
    await page.settle();
    assert.equal(page.ws._test.kb.on, true);
    assert.equal(off.hidden, true);
    // The service turns keys off on its own: the page follows and says why.
    Object.assign(robot, { keyboard_enabled: false, keyboard_disarm_reason: "key updates stopped." });
    await page.ws._test.refresh();
    assert.equal(page.ws._test.kb.on, false);
    assert.equal(page.find("Keyboard jog").getAttribute("aria-checked"), "false");
    assert.equal(off.hidden, false);
    assert.equal(off.textContent, "Off: key updates stopped.");
    // A new arming clears the reason.
    Object.assign(robot, { keyboard_enabled: true, keyboard_disarm_reason: null });
    page.find("Keyboard jog").click();
    await page.settle();
    assert.equal(off.hidden, true);
    assert.equal(off.textContent, "");
  } finally { page.restore(); }
});

test("stage hold keys map to 1 axis and 1 direction", () => {
  assert.deepEqual(logic.holdKey({ code: "KeyI" }), { key: "I", motor: "bottom", direction: "ccw" });
  assert.deepEqual(logic.holdKey({ code: "KeyK" }), { key: "K", motor: "bottom", direction: "cw" });
  assert.deepEqual(logic.holdKey({ code: "KeyJ" }), { key: "J", motor: "top", direction: "ccw" });
  assert.deepEqual(logic.holdKey({ key: "o" }), { key: "O", motor: "insert", direction: "cw" });
  assert.equal(logic.holdKey({ code: "KeyW" }), null);
  assert.match(logic.holdId(), /^hold-[0-9a-f]{24}$/);
  assert.notEqual(logic.holdId(), logic.holdId());
});

const press = { button: 0, pointerId: 1, preventDefault() {} };

test("physical stage labels send the original controller directions for holds and steps", async () => {
  const motors = ["bottom", "top", "insert"].map(id => ({ ...STAGE.motors[0], id }));
  const page = await keyboardPage({ stage: { ...STAGE, motors } });
  try {
    for (const [motor, axis, label, direction] of [
      ["bottom", "Bottom (Y)", "Up", "ccw"], ["bottom", "Bottom (Y)", "Down", "cw"],
      ["top", "Top (X)", "Left", "cw"], ["top", "Top (X)", "Right", "ccw"],
      ["insert", "Insert", "In", "ccw"], ["insert", "Insert", "Out", "cw"],
    ]) {
      const button = page.find(`Hold to move ${axis} ${label}`);
      assert.equal(button.textContent, `Hold ${label}`);
      button.listeners.get("pointerdown")(press);
      await page.settle();
      const hold = page.holdCalls().filter(call => call[1] === "stage/hold").at(-1)[2];
      assert.equal(hold.motor, motor);
      assert.equal(hold.direction, direction);
      button.listeners.get("pointerup")({});
      await page.settle();
      page.find(`Move ${axis} ${label} by the rotations`).click();
      await page.settle();
      assert.deepEqual(page.calls.filter(call => call[0] === "relay").at(-1),
        ["relay", "stage", "move", { motor, direction, speed: 0.25, rotations: 0.25 }]);
    }
    const top = page.root.all().find(node => node.className === "mo-encoder-axis" && node.getAttribute("aria-label") === "Top (X)");
    assert.deepEqual(top.all().filter(node => node.className === "mo-hold").map(node => node.textContent), ["Hold Left", "Hold Right"]);
    assert.deepEqual(top.all().filter(node => node.tagName === "KBD").map(node => [node.textContent, node.getAttribute("title")]), [["L", "Left"], ["J", "Right"]]);
  } finally { page.restore(); }
});

test("stage holds, their releases and arm keys ask for the control socket", async () => {
  const page = await keyboardPage();
  try {
    page.find("Keyboard jog").click();
    await page.settle();
    page.key("keydown", "KeyW");
    page.key("keyup", "KeyW");
    const button = page.find("Hold to move Insert Out");
    button.listeners.get("pointerdown")(press);
    button.listeners.get("pointerup")({});
    await page.settle();
    const control = page.sent.filter(([path]) => ["keys", "stage/hold", "stage/release"].includes(path));
    assert.deepEqual(control.map(([path]) => path), ["keys", "keys", "stage/hold", "stage/release"]);
    assert.ok(control.every(([, options]) => options.socket === true));
    // Other requests stay on HTTP.
    assert.ok(page.sent.filter(([path]) => path === "state").every(([, options]) => !options.socket));
  } finally { page.restore(); }
});

test("a Hold button refreshes the hold while pressed and releases it on pointerup", async () => {
  const stage = structuredClone(STAGE);
  const page = await keyboardPage({ stage });
  try {
    const before = page.intervals.length;
    const button = page.find("Hold to move Insert Out");
    button.listeners.get("pointerdown")(press);
    await page.settle();
    page.intervals[before]();
    // The page's own hold makes the axis busy. The held button stays enabled for its pointerup.
    Object.assign(stage, { busy: true });
    Object.assign(stage.motors[0], { mode: "continuous", available: false });
    await page.ws._test.refresh();
    assert.equal(button.disabled, false);
    assert.equal(button.getAttribute("aria-pressed"), "true");
    assert.equal(page.find("Move Insert Out by the rotations").disabled, true);
    button.listeners.get("pointerup")({});
    button.listeners.get("lostpointercapture")({});
    await page.settle();
    const calls = page.holdCalls();
    const id = calls[0][2].id;
    assert.deepEqual(calls, [
      ["request", "stage/hold", { id, motor: "insert", direction: "cw", speed: 0.25 }],
      ["request", "stage/hold", { id, motor: "insert", direction: "cw", speed: 0.25 }],
      ["request", "stage/release", { id }],
    ]);
    assert.equal(page.intervals[before], null);
    assert.equal(button.getAttribute("aria-pressed"), "false");
    // A pointerup anywhere in the window also ends a button hold.
    page.find("Hold to move Insert In").listeners.get("pointerdown")(press);
    page.win.get("pointerup")({});
    await page.settle();
    assert.deepEqual(page.holdCalls().slice(3).map(call => call[1]), ["stage/hold", "stage/release"]);
  } finally { page.restore(); }
});

test("a refused hold ends at once and needs a new press", async () => {
  let sent = 0;
  const refused = { outcome: "refused", error: "Hold has ended. Release and press again." };
  const page = await keyboardPage({ replies: { "stage/hold": () => (++sent > 1 ? refused : { outcome: "accepted" }) } });
  try {
    const before = page.intervals.length;
    const button = page.find("Hold to move Insert In");
    button.listeners.get("pointerdown")(press);
    await page.settle();
    page.intervals[before]();
    await page.settle();
    assert.equal(page.ws._test.sh.active, null);
    assert.deepEqual(page.holdCalls().map(call => call[1]), ["stage/hold", "stage/hold", "stage/release"]);
    assert.match(page.root.textContent, /Stage Hold: refused · Hold has ended/);
    button.listeners.get("pointerup")({});
    assert.equal(page.holdCalls().length, 3);
  } finally { page.restore(); }
});

test("the stage keyboard holds 1 axis and stops on keyup, blur, a hidden tab, loss of control and leaving Motion", async () => {
  const owns = ["robot", "stage"];
  const page = await keyboardPage({ owns });
  try {
    const sw = page.find("Stage keyboard");
    page.key("keydown", "KeyO");
    assert.equal(page.holdCalls().length, 0);
    sw.click();
    page.key("keydown", "KeyO");
    page.key("keydown", "KeyO", { repeat: true });
    // A second axis key waits for the first to be released.
    page.key("keydown", "KeyU");
    page.key("keyup", "KeyU");
    assert.equal(page.holdCalls().length, 1);
    page.key("keyup", "KeyO");
    await page.settle();
    const ends = [];
    const holdOn = () => { if (!page.ws._test.sh.on) sw.click(); page.key("keydown", "KeyU"); };
    holdOn();
    page.win.get("blur")();
    ends.push(page.ws._test.sh.on);
    holdOn();
    globalThis.document.visibilityState = "hidden";
    page.docListeners.get("visibilitychange")();
    globalThis.document.visibilityState = "visible";
    ends.push(page.ws._test.sh.on);
    holdOn();
    owns.splice(owns.indexOf("stage"), 1);
    page.ws._test.update();
    ends.push(page.ws._test.sh.on);
    owns.push("stage");
    page.ws._test.update();
    holdOn();
    page.ws.hide();
    ends.push(page.ws._test.sh.on);
    await page.settle();
    assert.deepEqual(ends, [false, false, false, false]);
    const calls = page.holdCalls();
    assert.deepEqual(calls.map(call => [call[1], call[2].motor, call[2].direction]), [
      ["stage/hold", "insert", "cw"], ["stage/release", undefined, undefined],
      ...Array(4).fill([["stage/hold", "insert", "ccw"], ["stage/release", undefined, undefined]]).flat(),
    ]);
    // The robot keyboard stays off: stage keys never reach the robot.
    assert.equal(page.keyCalls().length, 0);
  } finally { page.restore(); }
});

test("without stage control the Hold buttons and the stage keyboard are unavailable", async () => {
  const page = await mounted({ owns: [], stage: { ...STAGE, holder: null } });
  assert.equal(page.find("Hold to move Insert Out").disabled, true);
  assert.equal(page.find("Stage keyboard").disabled, true);
});
