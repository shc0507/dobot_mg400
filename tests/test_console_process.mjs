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
const SCOPE_DESCRIBE = { device: "microscope", commands: { select_objective: { params: { properties: { id: { enum: [0, 1, 2, 3] } } } } } };

function statusBody(overrides = {}) {
  return {
    now: 10_000, age_s: 0.5, cutoff_confirmed: false, gas_ready: false, crucible: { max_c: 31.5 },
    history: [[10_000 - 1800, 24, 0], [10_000, 25, 0]],
    furnace: {
      connected: true, locked: false, pv: 25, sv: 0, control_mode: "STOPPED",
      sequence: { running: false, phase: "idle", steps: [{ temperature_c: 300, ramp_rate_c_per_min: 10, hold_minutes: 5 }] },
      flow: { connected: true, gas_channels: [0, 1, 2].map(i => ({ channel_index: i, gas_name: `Gas ${i + 1}` })) },
    },
    microscope: {
      connected: true, busy: false, locked: false, objective_id: 2, focus_position_pulses: 1200, focus_zero_known: true,
      focus_limits_pulses: [0, 10000], recording: { status: "idle" }, recording_sequence: { armed: false },
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

test("apply and run needs a confirmation and warns before the cutoff", async () => {
  const { root, calls } = await load();
  assert.equal(walk(root).find(node => node.className === "pr-banner").hidden, false);
  role(root, "target").type("250");
  role(root, "apply").click();
  assert.equal(calls.filter(call => call[0] === "relay").length, 0);
  const box = walk(root).find(node => node.getAttribute("role") === "alertdialog");
  assert.match(box.textContent, /250 °C/);
  assert.match(box.textContent, /H-11/);
  role(root, "confirm-apply").click();
  await flush();
  assert.deepEqual(calls.filter(call => call[0] === "relay"), [["relay", "furnace", "apply_setpoint", { temperature_c: 250, ramp_rate_c_per_min: 10 }]]);
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

test("gas is refused until the gas map names real gases", async () => {
  const refused = await load();
  assert.equal(role(refused.root, "send-flow-0").disabled, true);
  const ready = statusBody({ gas_ready: true });
  ready.furnace.flow.gas_channels = [0, 1, 2].map(i => ({ channel_index: i, gas_name: ["Argon", "N2", "H2"][i] }));
  const allowed = await load({ status: ready });
  assert.equal(role(allowed.root, "send-flow-0").disabled, false);
});

test("stops need no control and are sent once", async () => {
  const { root, calls } = await load({ owns: [] });
  assert.equal(role(root, "apply").disabled, true);
  role(root, "stop-furnace").click();
  role(root, "stop-microscope").click();
  role(root, "stop-gas").click();
  await flush();
  assert.deepEqual(calls.filter(call => call[0] !== "request"), [
    ["stop", "furnace"], ["stop", "microscope"], ["console", "/api/dashboard/gas", { confirmed: true }],
  ]);
});

test("microscope objective, jog, autofocus and recording go through the relay", async () => {
  const { root, calls } = await load();
  const objective = role(root, "objective-2");
  assert.equal(objective.getAttribute("aria-checked"), "true");
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
    ["start_recording", { recording_id: "ab".repeat(16) }],
  ]);
});

test("motion waits for the focus zero, and Set focus zero is sent once after a confirmation", async () => {
  const status = statusBody();
  status.microscope.focus_zero_known = false;
  status.microscope.focus_position_pulses = null;
  const { root, calls } = await load({ status });
  assert.equal(role(root, "objective-0").disabled, true);
  assert.equal(role(root, "jog-up").disabled, true);
  assert.equal(role(root, "zero").disabled, false);
  role(root, "zero").click();
  assert.equal(calls.filter(call => call[1] === "focus-zero").length, 0);
  role(root, "confirm-zero").click();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.deepEqual(calls.filter(call => call[1] === "focus-zero"), [["request", "focus-zero", { confirmed: true }]]);
  assert.equal(calls.filter(call => call[0] === "relay").length, 0);
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
  assert.deepEqual(calls.filter(call => call[1] === "acknowledge"), [["request", "acknowledge", { confirmed: true }]]);
  const healthy = await load();
  assert.equal(role(healthy.root, "acknowledge").hidden, true);
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
