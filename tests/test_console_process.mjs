import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const path = new URL("../src/orchestrator/web/static/console/process.js", import.meta.url).pathname;

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = new Map();
    this.hidden = false;
    this.disabled = false;
    this.className = "";
    this.ownText = "";
    this.value = "";
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("The workspace must not parse data as HTML."); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ""; this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  focus() { Element.focused = this; }
  addEventListener(event, callback) { this.listeners.set(event, callback); }
  click() { if (!this.disabled) this.listeners.get("click")?.({ preventDefault() {} }); }
  type(value) { this.value = value; this.listeners.get("input")?.({ target: this }); }
}

function walk(node, found = []) {
  found.push(node);
  for (const child of node.children || []) walk(child, found);
  return found;
}
const role = (root, name) => walk(root).find(node => node.getAttribute("data-role") === name);

function translate(tables, lang, key, vars = {}) {
  const text = tables[lang]?.[key] ?? tables.en?.[key] ?? key;
  return text.replace(/\{(\w+)\}/g, (_, name) => String(vars[name] ?? ""));
}

const FURNACE_DESCRIBE = {
  device: "furnace",
  commands: {
    apply_setpoint: { params: { properties: { temperature_c: { maximum: 1000 }, ramp_rate_c_per_min: { maximum: 100 } } } },
    start_sequence: { params: {} }, set_sequence: { params: {} }, set_gas_flow: { params: {} }, connect: { params: {} },
  },
};
const SCOPE_DESCRIBE = {
  device: "microscope",
  commands: {
    select_objective: { params: { properties: { id: { enum: [0, 1, 2, 3], names: { 0: "5x", 1: "10x", 2: "20x" } } } } },
    objective_nudge: { params: { properties: { steps: { enum: [-10, -1, 1, 10] } } } },
  },
};

function statusBody(overrides = {}) {
  return {
    now: 10_000, age_s: 0.5, crucible: { max_c: 31.5 },
    history: [[10_000 - 1800, 24, 0], [10_000, 25, 0]],
    furnace: {
      connected: true, locked: false, pv: 25, sv: 0, control_mode: "STOPPED",
      sequence: { running: false, phase: "idle", steps: [{ temperature_c: 300, ramp_rate_c_per_min: 10, hold_minutes: 5 }] },
      flow: { connected: true, gas_channels: [0, 1, 2].map(i => ({ channel_index: i, gas_name: `Gas ${i + 1}` })) },
    },
    microscope: {
      connected: true, busy: false, locked: false, objective_id: 2, focus_position_pulses: 1200, focus_zero_known: true,
      objective_offset_steps: 0, objective_offset_limit_steps: 20,
      focus_limits_pulses: [0, 10000], recording: { status: "idle" }, recording_sequence: { armed: false }, stabilization: false,
    },
    ...overrides,
  };
}

async function load({ owns = ["furnace", "microscope"], status = statusBody(), cameras = null } = {}) {
  delete require.cache[path];
  const logic = require(path);
  const calls = [];
  const notices = [];
  let registered = null;
  const global = {
    LabCameras: cameras,
    crypto: { getRandomValues: array => array.fill(171) },
    document: { createElementNS: (_ns, tag) => new Element(tag) },
  };
  logic.install({ register: item => { registered = item; } }, global);
  let lang = "en";
  const languages = [];
  const api = {
    t: (key, vars) => translate(registered.i18n, lang, key, vars),
    lang: () => lang,
    onLanguage: fn => languages.push(fn),
    onState: () => () => {},
    state: () => ({ services: [] }),
    controls: () => owns.map(device => ({ device, state: "held", mine: true })),
    owns: device => owns.includes(device),
    take: async device => { calls.push(["take", device]); },
    release: async device => { calls.push(["release", device]); },
    relay: async (device, action, params) => { calls.push(["relay", device, action, params]); return { outcome: "accepted" }; },
    stop: async device => { calls.push(["stop", device]); return { device, outcome: "confirmed" }; },
    safetyStop: async action => { calls.push(["safety", action]); return { operation_id: "op-" + action }; },
    describe: async device => (device === "furnace" ? FURNACE_DESCRIBE : SCOPE_DESCRIBE),
    request: async (name, data) => {
      calls.push(data === undefined ? ["request", name] : ["request", name, data]);
      return data === undefined ? status : { outcome: "accepted", command_id: "1" };
    },
    consoleRequest: async (url, data) => { calls.push(["console", url, data]); return {}; },
    notify: (text, level) => notices.push([level, text]),
    deviceName: id => id[0].toUpperCase() + id.slice(1),
    el(tag, props, ...children) {
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
        node.append(typeof child === "string" ? Object.assign(new Element("#text"), { ownText: child }) : child);
      }
      return node;
    },
  };
  globalThis.setInterval = () => 1;
  globalThis.clearInterval = () => {};
  const root = new Element("section");
  registered.mount(root, api);
  await logic._internal.poll();
  await new Promise(resolve => setImmediate(resolve));
  await logic._internal.poll();
  logic._internal.hide();
  const setLang = value => { lang = value; for (const fn of languages) fn(value); };
  return { logic, root, calls, notices, registered, setLang };
}

const flush = () => new Promise(resolve => setImmediate(resolve));

test("chart scales the last 60 minutes and grows its top above 600 °C", () => {
  const { chartModel } = require(path).logic;
  const model = chartModel([[0, 0, 0], [3600, 900, 950], [-10, 5, 5]], 3600);
  assert.equal(model.top, 1200);
  assert.equal(model.count, 2);
  assert.equal(model.pv, "40.0,120.0 550.0,45.0");
  assert.equal(chartModel([], 0).top, 600);
});

test("sequence params follow the furnace page rules", () => {
  const { sequenceParams, editorRows } = require(path).logic;
  const rows = editorRows({ steps: [{ temperature_c: 200, ramp_rate_c_per_min: 5, hold_minutes: 10, gas: { 0: 20 } }] });
  assert.deepEqual(rows[0].gas, { 0: "20", 1: "", 2: "" });
  assert.throws(() => sequenceParams(rows, {}, ""), error => error.key === "seq.needEnd");
  assert.deepEqual(sequenceParams(rows, { 0: "5", 1: "0", 2: "" }, "150"), {
    steps: [{ temperature_c: 200, ramp_rate_c_per_min: 5, hold_minutes: 10, gas: { 0: 20 } }],
    end_gas_until_c: 150, end_gas: { 0: 5, 1: 0, 2: 0 },
  });
  assert.throws(() => sequenceParams([{ temperature_c: "100", ramp_rate_c_per_min: "0", hold_minutes: "1", gas: {} }], {}, ""), error => error.key === "seq.invalid");
  assert.throws(() => sequenceParams([], {}, ""), error => error.key === "seq.none");
});

test("focus bar uses the measured limits only", () => {
  const { focusFraction } = require(path).logic;
  assert.equal(focusFraction(1200, [0, 10000]), 0.12);
  assert.equal(focusFraction(null, [0, 10000]), null);
  assert.equal(focusFraction(5, []), null);
  assert.equal(focusFraction(20000, [0, 10000]), 1);
});

test("every English string has a Chinese string", () => {
  const { i18n } = require(path);
  assert.deepEqual(Object.keys(i18n.zh).sort(), Object.keys(i18n.en).sort());
});

test("apply and run needs a confirmation", async () => {
  const { root, calls } = await load();
  assert.equal(walk(root).some(node => node.className === "pr-banner"), false);
  role(root, "target").type("250");
  role(root, "apply").click();
  assert.equal(calls.filter(call => call[0] === "relay").length, 0);
  const box = walk(root).find(node => node.getAttribute("role") === "alertdialog");
  assert.match(box.textContent, /250 °C/);
  role(root, "confirm-apply").click();
  await flush();
  assert.deepEqual(calls.filter(call => call[0] === "relay"), [["relay", "furnace", "apply_setpoint", { temperature_c: 250, ramp_rate_c_per_min: 10 }]]);
});

test("a confirmation takes the focus, has a coral Confirm, and Esc gives the focus back", async () => {
  const { root, calls } = await load();
  role(root, "apply").click();
  const dialog = role(root, "dialog-apply");
  assert.equal(Element.focused, dialog);
  assert.equal(role(root, "confirm-apply").className, "lc-danger pr-danger");
  dialog.listeners.get("keydown")({ key: "Escape", preventDefault() {} });
  assert.equal(role(root, "dialog-apply"), undefined);
  assert.equal(Element.focused, role(root, "apply"));
  assert.equal(calls.filter(call => call[0] === "relay").length, 0);
});

test("gas text is amber only when the flow or the valve is not confirmed", async () => {
  const flow = { connected: true, gas_channels: [
    { channel_index: 0, gas_name: "Ar", actual_percent: 10 }, { channel_index: 1, gas_name: "H2" }, { channel_index: 2, gas_name: "N2", actual_percent: 0 } ],
    gas_valves: [{ channel_index: 0, open: true, verified: true }, { channel_index: 1, open: false, verified: true }, { channel_index: 2, open: false, verified: false }] };
  const base = statusBody();
  const { root } = await load({ status: { ...base, furnace: { ...base.furnace, flow } } });
  const states = walk(root).filter(node => node.className.startsWith("pr-gas-state"));
  assert.deepEqual(states.map(node => node.className), ["pr-gas-state", "pr-gas-state pr-doubt", "pr-gas-state pr-doubt"]);
});

test("old data dims the readings and the chart; a sent command shows its button label", async () => {
  const fresh = await load();
  assert.doesNotMatch(walk(fresh.root).find(node => node.className.startsWith("pr-tile-value pr-tone-pv")).className, /pr-stale/);
  role(fresh.root, "af-local").click();
  await flush();
  assert.deepEqual(fresh.notices.at(-1), ["info", "Autofocus sent."]);
  const old = await load({ status: statusBody({ age_s: 8 }) });
  assert.equal(walk(old.root).find(node => node.className.startsWith("pr-tile-value pr-tone-pv")).className, "pr-tile-value pr-tone-pv pr-stale");
  assert.equal(walk(old.root).find(node => node.className.startsWith("pr-figure")).className, "pr-figure pr-stale");
  assert.match(walk(old.root).find(node => node.className === "pr-warn").textContent, /Data is stale \(8 s\)/);
});

test("a target above the described cap is refused in the page", async () => {
  const { root, calls, notices } = await load();
  role(root, "target").type("1500");
  role(root, "apply").click();
  role(root, "confirm-apply").click();
  await flush();
  assert.equal(calls.filter(call => call[0] === "relay").length, 0);
  assert.equal(notices.at(-1)[0], "error");
});

test("the sequence loads from the furnace and starts after a confirmation", async () => {
  const { root, calls } = await load();
  assert.equal(walk(root).filter(node => node.getAttribute("data-role") === "step").length, 1);
  role(root, "add-step").click();
  assert.equal(walk(root).filter(node => node.getAttribute("data-role") === "step").length, 2);
  role(root, "start-sequence").click();
  role(root, "confirm-start").click();
  await flush();
  const sent = calls.find(call => call[2] === "start_sequence");
  assert.equal(sent[3].steps.length, 2);
  assert.equal(sent[3].steps[0].temperature_c, 300);
});

test("gas flow needs no gas map names", async () => {
  const { root } = await load();
  assert.equal(role(root, "send-flow-0").disabled, false);
});

test("stops need no control and are sent once; heat and gas use the Overview routes", async () => {
  const { root, calls } = await load({ owns: [] });
  assert.equal(role(root, "apply").disabled, true);
  role(root, "stop-furnace").click();
  role(root, "stop-microscope").click();
  role(root, "stop-gas").click();
  await flush();
  assert.deepEqual(calls.filter(call => call[0] !== "request"), [
    ["safety", "heat"], ["stop", "microscope"], ["safety", "gas"],
  ]);
});

test("the cards name only another holder, the caps sit at the fields, and the ramp shows only while heating", async () => {
  const status = statusBody();
  status.furnace.ramp = 5;
  const { root } = await load({ owns: [], status });
  const text = walk(root).map(node => node.ownText);
  for (const gone of ["no control", "you have control", "focus zero unknown", "zero set", "━ PV", "╍ SV"]) {
    assert.equal(text.some(value => value.includes(gone)), false, gone);
  }
  assert.equal(text.some(value => value.startsWith("Caps")), false);
  assert.ok(text.includes("Target °C ≤ 1000") && text.includes("Ramp °C/min ≤ 100"));
  const rampItem = walk(root).find(node => node.className === "pr-state-item");
  assert.equal(rampItem.hidden, true);
  status.furnace.control_mode = "RUN";
  const running = await load({ owns: [], status });
  assert.equal(walk(running.root).find(node => node.className === "pr-state-item").hidden, false);
});

test("Close shuts 1 gas channel without control; the gas rows show the valve state and the ramp tile shows", async () => {
  const status = statusBody();
  status.furnace.ramp = 5;
  status.furnace.flow.gas_valves = [{ channel_index: 0, open: true, verified: true }, { channel_index: 1, open: false, verified: false }];
  const { root, calls, notices } = await load({ owns: [], status });
  const text = walk(root).map(node => node.ownText);
  assert.ok(text.includes("Ramp °C/min") && text.includes("5.0"));
  assert.ok(text.some(value => value.endsWith("· valve open")));
  assert.ok(text.some(value => value.endsWith("· valve closed (not confirmed)")));
  assert.ok(text.some(value => value.endsWith("· valve unknown")));
  assert.equal(role(root, "send-flow-1").disabled, true);
  role(root, "close-gas-1").click();
  await flush();
  assert.deepEqual(calls.filter(call => call[1] === "gas-stop"), [["request", "gas-stop", { channel_index: 1 }]]);
  assert.equal(calls.filter(call => call[0] === "relay").length, 0);
  assert.ok(notices.some(([, text]) => text.startsWith("Gas 2 close:")));
});

test("snapshot path and recording fields are checked in the page and sent with the command", async () => {
  const { root, calls, notices } = await load();
  role(root, "snapshot-path").type("/etc/passwd");
  role(root, "snapshot").click();
  await flush();
  assert.ok(notices.some(([level]) => level === "error"));
  role(root, "snapshot-path").type("runs/chip-7/before.png");
  role(root, "snapshot").click();
  await flush();
  role(root, "rec-name").type("anneal 7");
  role(root, "rec-interval").type("12.5");
  role(root, "rec-fps").type("4");
  role(root, "record").click();
  await flush();
  role(root, "rec-fps").type("500");
  role(root, "arm").click();
  await flush();
  const sent = calls.filter(call => call[0] === "relay").map(call => [call[2], call[3]]);
  assert.deepEqual(sent, [
    ["snapshot", { path: "runs/chip-7/before.png" }],
    ["start_recording", { recording_id: "ab".repeat(16), mode: "time_lapse", run_name: "anneal 7", output_fps: 4, capture_interval_seconds: 12.5 }],
  ]);
});

test("recording errors show, and the snapshot and recording helpers follow the service rules", async () => {
  const { logic } = await load();
  assert.deepEqual(logic.logic.snapshotParams(""), {});
  assert.deepEqual(logic.logic.snapshotParams(" a/b.png "), { path: "a/b.png" });
  for (const bad of ["/abs", "a//b", "../x", "a\\b", "a/./b"]) assert.equal(logic.logic.snapshotParams(bad), null);
  assert.deepEqual(logic.logic.recordingParams({ mode: "live", interval: "", fps: "30" }), { mode: "live", output_fps: 30 });
  assert.throws(() => logic.logic.recordingParams({ mode: "time_lapse", interval: "0", fps: "1" }));
  assert.throws(() => logic.logic.recordingParams({ name: "x".repeat(81), mode: "live", fps: "1" }));
  assert.deepEqual(logic.logic.valveState({ open: false, verified: true }), { key: "valve.closed" });
  assert.deepEqual(logic.logic.valveState(null), { key: "valve.unknown" });
  const status = statusBody();
  status.microscope.recording = { status: "failed", error: "Camera stream ended." };
  const { root } = await load({ status });
  assert.equal(role(root, "rec-error").hidden, false);
  assert.equal(role(root, "rec-error").textContent, "Recording error: Camera stream ended.");
});

test("microscope objective, jog, autofocus and recording go through the relay", async () => {
  const { root, calls } = await load();
  const objective = role(root, "objective-2");
  assert.equal(objective.getAttribute("aria-checked"), "true");
  assert.equal(objective.textContent, "20x");
  assert.equal(objective.getAttribute("aria-label"), "Objective 2, 20x");
  // An ID without a described name keeps its number.
  assert.equal(role(root, "objective-3").textContent, "3");
  role(root, "objective-1").click();
  await flush();
  role(root, "jog").type("50");
  role(root, "jog-down").click();
  await flush();
  role(root, "af-local").click();
  await flush();
  role(root, "record").click();
  await flush();
  const sent = calls.filter(call => call[0] === "relay").map(call => [call[2], call[3]]);
  assert.deepEqual(sent, [
    ["select_objective", { id: 1 }],
    ["focus_move", { steps: -50 }],
    ["autofocus", { mode: "local" }],
    ["start_recording", { recording_id: "ab".repeat(16), mode: "time_lapse", output_fps: 1, capture_interval_seconds: 30 }],
  ]);
});

test("the 4 turret nudges relay objective_nudge and stay inside the window", async () => {
  const status = statusBody();
  status.microscope.objective_offset_steps = 15;
  const { root, calls } = await load({ status });
  const buttons = ["nudge-down-10", "nudge-down-1", "nudge-up-1", "nudge-up-10"].map(name => role(root, name));
  assert.deepEqual(buttons.map(node => node.textContent), ["−10", "−1", "+1", "+10"]);
  assert.deepEqual(buttons.map(node => node.disabled), [false, false, false, true]);
  assert.equal(role(root, "nudge-row").hidden, false);
  assert.equal(role(root, "nudge-offset").textContent, "Turret +15 / ±20 full steps");
  for (const node of buttons) {
    node.click();
    await flush();
  }
  const sent = calls.filter(call => call[0] === "relay").map(call => [call[1], call[2], call[3]]);
  assert.deepEqual(sent, [
    ["microscope", "objective_nudge", { steps: -10 }],
    ["microscope", "objective_nudge", { steps: -1 }],
    ["microscope", "objective_nudge", { steps: 1 }],
  ]);
});

test("nudges wait for a counted turret offset and the focus zero", async () => {
  const unknown = statusBody();
  unknown.microscope.objective_offset_steps = null;
  let { root } = await load({ status: unknown });
  assert.ok(["nudge-down-10", "nudge-down-1", "nudge-up-1", "nudge-up-10"].every(name => role(root, name).disabled));
  assert.equal(role(root, "nudge-offset").textContent, "Turret offset unknown. Select another objective first.");
  const noZero = statusBody();
  noZero.microscope.focus_zero_known = false;
  ({ root } = await load({ status: noZero }));
  assert.equal(role(root, "nudge-up-1").disabled, true);
  ({ root } = await load({ owns: ["furnace"] }));
  assert.equal(role(root, "nudge-up-1").disabled, true);
});

test("nudges are hidden when the service has no nudge window", async () => {
  const off = statusBody();
  off.microscope.objective_offset_limit_steps = 0;
  const { root, logic } = await load({ status: off });
  assert.equal(role(root, "nudge-row").hidden, true);
  assert.deepEqual(logic.logic.nudgeSteps({ commands: {} }), []);
  assert.deepEqual(logic.logic.nudgeSteps(SCOPE_DESCRIBE), [-10, -1, 1, 10]);
});

test("motion waits for the focus zero, and Set focus zero goes through the relay once without a dialog", async () => {
  const status = statusBody();
  status.microscope.focus_zero_known = false;
  status.microscope.focus_position_pulses = null;
  const { root, calls } = await load({ status });
  assert.equal(role(root, "objective-0").disabled, true);
  assert.equal(role(root, "jog-up").disabled, true);
  assert.equal(role(root, "zero").disabled, false);
  role(root, "zero").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(calls.filter(call => call[2] === "set_focus_zero"), [["relay", "microscope", "set_focus_zero", {}]]);
  assert.equal(calls.filter(call => call[1] === "focus-zero").length, 0);
});

test("Set focus zero needs microscope control", async () => {
  const { root } = await load({ owns: ["furnace"] });
  assert.equal(role(root, "zero").disabled, true);
});

test("a furnace fault shows Acknowledge, which is sent once after a confirmation", async () => {
  const status = statusBody();
  status.furnace.locked = true;
  status.furnace.fault = { code: "x", message: "Saved fault requires acknowledgement" };
  const { root, calls } = await load({ status });
  assert.equal(role(root, "acknowledge").hidden, false);
  role(root, "acknowledge").click();
  role(root, "confirm-ack").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(calls.filter(call => call[0] === "relay"), [["relay", "furnace", "acknowledge", {}]]);
  const healthy = await load();
  assert.equal(role(healthy.root, "acknowledge").hidden, true);
});

test("a failed microscope command shows its reason until the next command", async () => {
  const status = statusBody();
  status.microscope.last_command = { action: "select_objective", result: "failed", message: "Objective identification failed: black_frame" };
  const { root } = await load({ status });
  const note = role(root, "scope-note");
  assert.equal(note.hidden, false);
  assert.equal(note.textContent, "select_objective failed: Objective identification failed: black_frame");
  assert.equal(role(root, "objective-1").disabled, false);
  status.microscope.last_command = { action: "select_objective", result: "ok", message: "" };
  const healthy = await load({ status });
  assert.equal(role(healthy.root, "scope-note").hidden, true);
});

test("camera tiles come from LabCameras when it exists", async () => {
  const views = [];
  const cameras = { tile: (view, options) => { assert.equal(typeof options.api.t, "function"); views.push(view); return new Element("figure"); } };
  await load({ cameras });
  assert.deepEqual(views, ["microscope", "camera3"]);
  const { root } = await load();
  assert.ok(walk(root).some(node => node.className === "pr-view pr-offline"));
});

test("the language switch rebuilds the workspace in Chinese", async () => {
  const { root, setLang } = await load();
  setLang("zh");
  assert.match(root.textContent, /炉子 · AI-808/);
  assert.equal(role(root, "stop-furnace").textContent, "停止加热和气体");
});

test("the stabilization toggle relays set_stabilization once", async () => {
  const { root, calls } = await load();
  role(root, "stabilization").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(calls.filter(call => call[0] === "relay" && call[2] === "set_stabilization"), [["relay", "microscope", "set_stabilization", { enabled: true }]]);
});

test("the preview shows 1 view, and the switch inside it changes the view", async () => {
  const { root } = await load();
  const panes = walk(root).filter(node => node.className === "pr-pane");
  assert.deepEqual(panes.map(node => [node.getAttribute("data-view"), node.hidden]), [["microscope", false], ["camera3", true]]);
  assert.equal(role(root, "view-microscope").getAttribute("aria-pressed"), "true");
  role(root, "view-camera3").click();
  assert.deepEqual(panes.map(node => node.hidden), [true, false]);
  assert.equal(role(root, "view-camera3").getAttribute("aria-pressed"), "true");
  assert.equal(role(root, "view-microscope").getAttribute("aria-pressed"), "false");
});
