import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const { logic, i18n, workspace } = require("../src/orchestrator/web/static/console/motion.js");

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = []; this.attributes = {}; this.listeners = new Map();
    this.hidden = false; this.disabled = false; this.className = ""; this.ownText = ""; this.value = "";
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("The workspace must not parse data as HTML."); }
  get firstChild() { return this.children[0] || null; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ""; this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
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
  available: true, holder: "mine", actual: [250, 10, 40, 30, 1000], enabled: true, busy: false, locked: false,
  stop_unconfirmed: false, feedback_fresh: true, gripper_available: true, gripper_can_initialize: true,
  gripper_busy: false, gripper: { outcome: "arrived" }, demo: true, last_command: null,
};
const STAGE = {
  available: true, holder: "mine", connected: true, demo: true, busy: false, locked: false, microstep: 1,
  microstep_modes: [{ value: 1, label: "Full step" }, { value: 2, label: "Half step (1/2)" }],
  limits: { speed: 1000, steps: 100000 }, defaults: { speed: 50, steps: 50 },
  motors: [{ id: "insert", mode: "idle", position: 0, position_mm: 0, homed: false, automation_ready: false,
    homing_available: false, zero_available: true, named_positions: ["load"], at_positions: [], available: true, cooldown: 0 }],
};

async function mounted({ owns = ["robot", "stage"], robot = ROBOT, stage = STAGE } = {}) {
  const calls = [];
  const tables = { en: i18n.en };
  const api = {
    t: (key, vars = {}) => String(tables.en[key] ?? key).replace(/\{(\w+)\}/g, (_, name) => vars[name] ?? ""),
    el, onLanguage() {}, onState() {}, notify: (text, level) => calls.push(["notify", text, level]),
    owns: device => owns.includes(device), deviceName: id => id[0].toUpperCase() + id.slice(1),
    async request(path, data) {
      calls.push(["request", path, data]);
      if (path === "state") return { robot, stage };
      if (path === "stage/ports") return { ports: [{ device: "/dev/ttyACM0", description: "Arduino" }] };
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
  return { ws, root, calls, find, settle: async () => { for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r)); } };
}

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
  assert.equal(page.find("Execute ↗").disabled, true);
  assert.equal(page.find("Disable").disabled, false);
  page.find("Stop stage").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "stop"), [["stop", "stage"]]);
});

test("gripper positions and alarm rows are parsed strictly", () => {
  assert.equal(logic.parseGrip("500"), 500);
  assert.equal(logic.parseGrip("0"), 0);
  assert.equal(logic.parseGrip("1001"), null);
  assert.equal(logic.parseGrip("-1"), null);
  assert.equal(logic.parseGrip("2.5"), null);
  assert.deepEqual(logic.alarmRows({ alarms: [
    { id: 22, source: "controller", description: "Joint 2 limit", solution: "Jog J2 back." },
    { id: 3, source: "servo", description: "Overload", solution: "" },
  ] }), [{ id: "22", text: "Joint 2 limit Jog J2 back." }, { id: "3", text: "Overload (servo)" }]);
  assert.deepEqual(logic.alarmRows({}), []);
});

test("the arm card shows joints, alarms and the service message; Stop arm needs no control", async () => {
  const robot = { ...ROBOT, joints: [1.5, -20, 3, 44.26], message: "Robot stop is confirmed. Disable the robot before recovery.",
    gripper_message: "Control is locked. Check the gripper.",
    alarms: [{ id: 22, source: "controller", description: "Joint 2 limit", solution: "Jog J2 back." }] };
  const page = await mounted({ owns: [], robot: { ...robot, holder: null } });
  const texts = page.root.all().map(node => node.ownText);
  assert.ok(texts.includes("44.3") && texts.includes("-20.0"));
  assert.ok(texts.includes("Alarm 22: Joint 2 limit Jog J2 back."));
  assert.ok(texts.includes("Robot stop is confirmed. Disable the robot before recovery."));
  assert.ok(texts.includes("Control is locked. Check the gripper."));
  assert.equal(page.find("Home…").disabled, true);
  assert.equal(page.find("Move").disabled, true);
  page.find("Stop arm").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "stop"), [["stop", "robot"]]);
});

test("Home sends only after the page confirmation; Move sends 1 gripper position", async () => {
  const page = await mounted();
  page.find("Home…").click();
  await page.settle();
  assert.equal(page.calls.some(call => call[0] === "relay"), false);
  page.find("Confirm").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "relay"), [["relay", "robot", "home", {}]]);
  const input = page.find("Gripper position, 0 to 1000");
  input.value = "1200";
  input.listeners.get("input")({ target: input });
  page.find("Move").click();
  await page.settle();
  assert.ok(page.calls.some(call => call[0] === "notify" && call[1] === "Enter a whole gripper position from 0 to 1000."));
  input.value = "400";
  input.listeners.get("input")({ target: input });
  page.find("Move").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "relay" && call[2] === "grip"), [["relay", "robot", "grip", { position: 400 }]]);
});

test("Refresh ports reads the serial ports again", async () => {
  const page = await mounted({ stage: { ...STAGE, connected: false } });
  await page.settle();
  const reads = () => page.calls.filter(call => call[1] === "stage/ports").length;
  assert.equal(reads(), 1);
  page.find("Refresh ports").click();
  await page.settle();
  assert.equal(reads(), 2);
});

test("named poses need all axes referenced and send 1 pose command", async () => {
  const ready = { ...STAGE.motors[0], automation_ready: true };
  const page = await mounted({ stage: { ...STAGE, motors: [ready], poses: ["load", "inspect"], at_poses: ["load"] } });
  assert.equal(page.find("load").getAttribute("aria-pressed"), "true");
  page.find("inspect").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "relay"), [["relay", "stage", "pose", { name: "inspect" }]]);
  const waiting = await mounted({ stage: { ...STAGE, poses: ["load"], at_poses: [] } });
  assert.equal(waiting.find("load").disabled, true);
  assert.ok(waiting.root.all().some(node => node.ownText === "Home or zero all 3 axes before a named pose."));
  const none = await mounted();
  assert.equal(none.root.all().some(node => node.ownText === "Named poses"), false);
});

test("Set zero sends only after the page confirmation", async () => {
  const page = await mounted();
  page.find("Set zero…").click();
  await page.settle();
  assert.equal(page.calls.some(call => call[0] === "relay"), false);
  page.find("Confirm").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[0] === "relay"), [["relay", "stage", "zero", { motor: "insert", confirm: true }]]);
});

test("a stage step needs no zero or reference", async () => {
  const second = await mounted();
  second.find("Move Insert plus by the steps").click();
  await second.settle();
  assert.deepEqual(second.calls.filter(call => call[0] === "relay"), [["relay", "stage", "move", { motor: "insert", direction: "cw", speed: 50, steps: 50 }]]);
});

test("a disconnected stage lists ports and connects on this page's control", async () => {
  const page = await mounted({ stage: { ...STAGE, connected: false } });
  await page.settle();
  assert.ok(page.calls.some(call => call[1] === "stage/ports"));
  page.find("Connect").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[1] === "stage/connect"), [["request", "stage/connect", { port: "/dev/ttyACM0" }]]);
  const connected = await mounted();
  assert.equal(connected.calls.some(call => call[1] === "stage/ports"), false);
});

test("Check reference shows the marked frame and Teach reference needs a confirmation", async () => {
  const stage = { ...STAGE, motors: [{ ...STAGE.motors[0], reference_configured: true }] };
  const page = await mounted({ stage });
  assert.equal(page.root.all().some(node => node.tagName === "A"), false);
  page.find("Check reference").click();
  await page.settle();
  const image = page.root.all().find(node => node.tagName === "IMG");
  assert.equal(image.getAttribute("src"), "data:image/jpeg;base64,AAAA");
  assert.match(page.root.textContent, /Marker offset 1\.5, -2\.0 px/);
  page.find("Teach reference…").click();
  await page.settle();
  assert.equal(page.calls.some(call => call[1] === "stage/teach"), false);
  page.find("Confirm").click();
  await page.settle();
  assert.deepEqual(page.calls.filter(call => call[1] === "stage/teach"), [["request", "stage/teach", { motor: "insert", confirmed: true }]]);
});

/** A window and a document with listeners, and interval callbacks the test runs by hand. */
async function keyboardPage() {
  const win = new Map(), docListeners = new Map(), intervals = [];
  const saved = { add: globalThis.addEventListener, document: globalThis.document, set: globalThis.setInterval, clear: globalThis.clearInterval };
  globalThis.addEventListener = (event, callback) => win.set(event, callback);
  globalThis.document = { visibilityState: "visible", hasFocus: () => true, addEventListener: (event, callback) => docListeners.set(event, callback) };
  const page = await mounted();
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
  return { ...page, win, docListeners, intervals, restore, key, keyCalls };
}

test("keyboard jog arms, refreshes held keys and releases them on blur", async () => {
  const page = await keyboardPage();
  try {
    const before = page.intervals.length;
    page.find("Keyboard").click();
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

test("keyboard jog disarms when the tab is hidden and when the Motion tab is left", async () => {
  const page = await keyboardPage();
  try {
    page.find("Keyboard").click();
    await page.settle();
    globalThis.document.visibilityState = "hidden";
    page.docListeners.get("visibilitychange")();
    await page.settle();
    globalThis.document.visibilityState = "visible";
    page.find("Keyboard").click();
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
