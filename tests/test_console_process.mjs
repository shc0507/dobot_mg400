import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const path = require.resolve("../src/orchestrator/web/static/console/process.js");

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
      connected: true, busy: false, locked: false, objective_id: 2, focus_position_pulses: 1200, focus_zero_known: false, turret_profile: { ready: true, id: "a".repeat(32) }, motor_power: { objective: { enabled: false, sleeping: true }, focus: { enabled: false, sleeping: false } },
      objective_offset_steps: 0, objective_offset_limit_steps: 20,
      autofocus_run: { phase: "complete", objective_id: 2, verified: true, completed: [2], score: 42.5, error: null },
      autofocus_settings: { five_x_half_range_pulses: 5000, ten_x_half_range_pulses: 300, twenty_x_half_range_pulses: 300, fifty_x_half_range_pulses: 300 },
      focus_limits_pulses: [0, 10000], focus_travel_limit_pulses: 1000, recording: { status: "idle" }, recording_sequence: { armed: false }, stabilization: false,
    },
    ...overrides,
  };
}

async function load({ owns = ["furnace", "microscope"], status = statusBody(), cameras = null, turret = null, requestOverride = null } = {}) {
  delete require.cache[path];
  const logic = require(path);
  const calls = [];
  const notices = [];
  let registered = null;
  const global = {
    LabCameras: cameras,
    LabTurret: turret,
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
      if (requestOverride) {
        const result = requestOverride(name, data);
        if (result !== undefined) return result;
      }
      if (name === "microscope/focus-travel-limit") return { focus_travel_limit_pulses: data?.pulses ?? 1000 };
      if (name === "microscope/autofocus-settings") {
        status.microscope.autofocus_settings = { ...status.microscope.autofocus_settings, ...data };
        return { ...status.microscope.autofocus_settings };
      }
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

test("a saved snapshot path is cleared, so the next press uses the service default", async () => {
  const { root, calls } = await load();
  role(root, "snapshot-path").type("runs/chip-7/before.png");
  role(root, "snapshot").click();
  await flush();
  assert.equal(role(root, "snapshot-path").value, "");
  role(root, "snapshot").click();
  await flush();
  const sent = calls.filter(call => call[2] === "snapshot").map(call => call[3]);
  assert.deepEqual(sent, [{ path: "runs/chip-7/before.png" }, {}]);
});

test("Confirm sends the setpoint that the confirmation shows", async () => {
  const { root, calls } = await load();
  role(root, "target").type("250");
  role(root, "apply").click();
  role(root, "target").type("900");
  role(root, "confirm-apply").click();
  await flush();
  assert.deepEqual(calls.filter(call => call[0] === "relay").map(call => call[3]), [{ temperature_c: 250, ramp_rate_c_per_min: 10 }]);
});

test("every microscope Stop press is sent, also while an earlier Stop is pending", async () => {
  const { root, calls } = await load();
  role(root, "stop-microscope").click();
  role(root, "stop-microscope").click();
  await flush();
  assert.deepEqual(calls.filter(call => call[0] === "stop"), [["stop", "microscope"], ["stop", "microscope"]]);
});

test("an unedited sequence editor follows a sequence that another client set", async () => {
  const status = statusBody();
  const { root, logic } = await load({ status });
  const steps = () => walk(root).filter(node => node.getAttribute("data-role") === "step");
  assert.equal(steps().length, 1);
  status.furnace.sequence = { running: true, phase: "holding", current_index: 1, steps: [
    { temperature_c: 200, ramp_rate_c_per_min: 5, hold_minutes: 1 }, { temperature_c: 400, ramp_rate_c_per_min: 5, hold_minutes: 1 }] };
  await logic._internal.poll();
  assert.equal(steps().length, 2);
  assert.deepEqual(steps().map(row => row.className), ["", "pr-current"]);
  // An edited editor keeps its rows and marks no running step.
  role(root, "add-step").click();
  status.furnace.sequence = { ...status.furnace.sequence, steps: [{ temperature_c: 500, ramp_rate_c_per_min: 5, hold_minutes: 1 }] };
  await logic._internal.poll();
  assert.equal(steps().length, 3);
  assert.ok(steps().every(row => row.className === ""));
});

test("status polls do not overlap", async () => {
  const { logic, calls } = await load();
  const before = calls.filter(call => call[1] === "status").length;
  await Promise.all([logic._internal.poll(), logic._internal.poll()]);
  assert.equal(calls.filter(call => call[1] === "status").length, before + 1);
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
  assert.equal(role(root, "af-full"), undefined);
  assert.doesNotMatch(root.textContent, /Full autofocus/);
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

test("direct lens selection allows any order without verified autofocus", async () => {
  const status = statusBody();
  status.microscope.objective_id = 0;
  status.microscope.autofocus_run = { phase: "failed", objective_id: 0, verified: false };
  const { root, calls, logic, setLang } = await load({ status });
  for (const id of [3, 1, 0, 2]) {
    assert.equal(role(root, "objective-" + id).disabled, false);
    role(root, "objective-" + id).click();
    await flush();
    status.microscope.objective_id = id;
    await logic._internal.poll();
  }
  assert.deepEqual(calls.filter(call => call[0] === "relay").map(call => call.slice(2)),
    [3, 1, 0, 2].map(id => ["select_objective", { id }]));
  assert.match(root.textContent, /Select any lens directly.*optional autofocus sequence runs 5× → 10× → 20× → 50×/);
  setLang("zh");
  assert.match(root.textContent, /可以直接选择任意物镜.*5× → 10× → 20× → 50×/);
});

test("direct lens selection preserves control, connection, busy, fault and profile guards", async () => {
  for (const overrides of [{ connected: false }, { busy: true }, { locked: true }, { turret_profile: { ready: false } }]) {
    const status = statusBody();
    Object.assign(status.microscope, overrides);
    const { root } = await load({ status });
    assert.ok([0, 1, 2, 3].every(id => role(root, "objective-" + id).disabled));
  }
  const { root } = await load({ owns: ["furnace"] });
  assert.ok([0, 1, 2, 3].every(id => role(root, "objective-" + id).disabled));
});

test("lens calibration stays beside the objective buttons and uses the shared turret lifecycle", async () => {
  const events = [], status = statusBody();
  let selector;
  const turret = { create(api) {
    selector = api.el("label", { "data-role": "turret-profile-selector", text: "Lens calibration" });
    return {
      element: api.el("section", { "data-role": "turret-calibration" }), profileSelector: selector,
      show: () => events.push("show"), hide: () => events.push("hide"),
      setMicroscopeBusy: value => { selector.disabled = Boolean(value); },
    };
  } };
  const { root, calls, setLang, logic } = await load({ turret, status });
  const controls = walk(root).find(node => node.className === "tr-objective-controls");
  assert.equal(controls.children[1], selector);
  assert.equal(controls.children[0].getAttribute("role"), "radiogroup");
  assert.equal(walk(root).filter(node => node === selector).length, 1);
  assert.equal(selector.disabled, false);
  assert.deepEqual(events, ["show", "hide"]);
  assert.equal(calls.some(call => call[0] === "relay"), false);
  status.microscope.busy = true;
  await logic._internal.poll();
  assert.equal(selector.disabled, true);
  setLang("zh");
  assert.equal(walk(root).filter(node => node === selector).length, 1);
  assert.ok(role(root, "turret-calibration"));
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

test("nudges wait for a counted turret offset and a height profile", async () => {
  const unknown = statusBody();
  unknown.microscope.objective_offset_steps = null;
  let { root } = await load({ status: unknown });
  assert.ok(["nudge-down-10", "nudge-down-1", "nudge-up-1", "nudge-up-10"].every(name => role(root, name).disabled));
  assert.equal(role(root, "nudge-offset").textContent, "Turret offset unknown. Select another objective first.");
  const noZero = statusBody();
  noZero.microscope.turret_profile = { ready: false };
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

test("profile-guided motion does not need a user focus zero", async () => {
  const status = statusBody();
  status.microscope.focus_zero_known = false;
  status.microscope.focus_position_pulses = null;
  status.microscope.objective_id = 0;
  const { root, calls } = await load({ status });
  assert.equal(role(root, "objective-0").disabled, false);
  assert.equal(role(root, "jog-up").disabled, false);
  assert.equal(role(root, "zero"), undefined);
  assert.equal(role(root, "focus-clearance"), undefined);
  assert.equal(role(root, "focus-saved-height"), undefined);
  role(root, "rough-focus-5x").click();
  await flush();
  role(root, "autofocus-sequence").click();
  await flush();
  assert.deepEqual(calls.filter(call => call[0] === "relay"), [
    ["relay", "microscope", "rough_focus_5x", {}],
    ["relay", "microscope", "autofocus_sequence", {}],
  ]);
});

test("profile absence blocks motion but preserves Stop and calibration access", async () => {
  const status = statusBody();
  status.microscope.turret_profile = { ready: false, needs_height_calibration: true };
  const { root, calls } = await load({ status });
  assert.equal(role(root, "objective-0").disabled, true);
  assert.equal(role(root, "af-local").disabled, true);
  assert.match(role(root, "profile-note").textContent, /record its 5× starting point/);
  role(root, "scope-tab-calibration").click();
  assert.equal(role(root, "scope-operate").hidden, true);
  assert.equal(role(root, "scope-calibration").hidden, false);
  assert.equal(role(root, "scope-tab-calibration").getAttribute("aria-selected"), "true");
  role(root, "stop-microscope").click();
  await flush();
  assert.ok(calls.some(call => call[0] === "stop" && call[1] === "microscope"));
  role(root, "scope-tab-operate").click();
  assert.equal(role(root, "scope-calibration").hidden, true);
  assert.match(role(root, "motor-power").textContent, /Turret.*Asleep.*Focus.*Released/);
});

test("rough position and sequence need control but do not rely on a cached lens ID", async () => {
  const { root } = await load({ owns: ["furnace"] });
  assert.equal(role(root, "rough-focus-5x").disabled, true);
  assert.equal(role(root, "autofocus-sequence").disabled, true);
  const status = statusBody();
  status.microscope.objective_id = null;
  const controlled = await load({ status });
  assert.equal(role(controlled.root, "rough-focus-5x").disabled, false);
  assert.equal(role(controlled.root, "autofocus-sequence").disabled, false);
});

test("human focus travel setting accepts presets and custom pulses without equipment commands", async () => {
  const { root, calls, notices } = await load();
  assert.equal(role(root, "focus-travel").value, "1000");
  assert.ok(role(root, "focus-travel-preset").children.some(option => option.getAttribute("value") === "20000"));
  assert.match(root.textContent, /manual jogs and rough 5× positioning/);
  assert.doesNotMatch(root.textContent, /window side|10000 · away/);
  role(root, "focus-travel-preset").listeners.get("change")({ target: { value: "500" } });
  assert.equal(role(root, "focus-travel").value, "500");
  role(root, "focus-travel-apply").click();
  await flush();
  assert.match(role(root, "focus-travel-current").textContent, /500/);
  role(root, "focus-travel").type("735");
  role(root, "focus-travel-apply").click();
  await flush();
  assert.deepEqual(calls.filter(call => call[0] === "request" && call[2]), [
    ["request", "microscope/focus-travel-limit", { pulses: 500 }],
    ["request", "microscope/focus-travel-limit", { pulses: 735 }],
  ]);
  assert.equal(calls.some(call => call[0] === "relay"), false);
  for (const value of ["0", "-1", "1.5", "2147483648", ""]) {
    role(root, "focus-travel").type(value);
    role(root, "focus-travel-apply").click();
  }
  assert.equal(calls.filter(call => call[2]?.pulses).length, 2);
  assert.match(notices.at(-1)[1], /whole number/);
  const denied = await load({ owns: ["furnace"] });
  assert.equal(role(denied.root, "focus-travel-apply").disabled, true);
});

test("5x search range is separate from manual travel and displays total span", async () => {
  const { root, calls, notices } = await load();
  assert.equal(role(root, "af-range").value, "5000");
  assert.ok(role(root, "af-range-preset").children.some(option => option.getAttribute("value") === "3000"));
  assert.match(role(root, "af-range-help").textContent, /±5000.*10000 total span/);
  assert.match(role(root, "af-saved-ranges").textContent, /5×.*±5000.*10×.*±300.*20×.*±300.*50×.*±300/);
  role(root, "af-range-preset").listeners.get("change")({ target: { value: "1500" } });
  role(root, "af-range-apply").click();
  await flush();
  assert.deepEqual(calls.find(call => call[1] === "microscope/autofocus-settings" && call[2]), ["request", "microscope/autofocus-settings", { five_x_half_range_pulses: 1500 }]);
  assert.equal(role(root, "focus-travel").value, "1000");
  assert.match(role(root, "af-range-help").textContent, /±1500.*3000 total span/);
  for (const value of ["0", "3", "1.5", "1000001", ""]) {
    role(root, "af-range").type(value); role(root, "af-range-apply").click();
  }
  assert.equal(calls.filter(call => call[1] === "microscope/autofocus-settings" && call[2]).length, 1);
  assert.match(notices.at(-1)[1], /4 to 1000000/);
  assert.equal(calls.some(call => call[0] === "relay"), false);
});

test("lens edits survive switches and polls, and Apply changes only the selected lens", async () => {
  const status = statusBody(), { root, calls, logic } = await load({ status });
  const selectLens = id => role(root, "af-range-lens").listeners.get("change")({ target: { value: String(id) } });
  selectLens(1); role(root, "af-range").type("450");
  selectLens(2); role(root, "af-range").type("600");
  status.microscope.autofocus_settings = { five_x_half_range_pulses: 5500, ten_x_half_range_pulses: 320, twenty_x_half_range_pulses: 350, fifty_x_half_range_pulses: 400 };
  await logic._internal.poll();
  assert.equal(role(root, "af-range").value, "600");
  assert.match(role(root, "af-draft-note").textContent, /Unsaved value for 20×/);
  role(root, "af-range-apply").click(); await flush();
  assert.deepEqual(calls.filter(call => call[1] === "microscope/autofocus-settings" && call[2]), [["request", "microscope/autofocus-settings", { twenty_x_half_range_pulses: 600 }]]);
  selectLens(1); assert.equal(role(root, "af-range").value, "450");
  assert.equal(role(root, "af-draft-note").hidden, false);
  selectLens(0); assert.equal(role(root, "af-range").value, "5500");
  selectLens(2); assert.equal(role(root, "af-range").value, "600");
  assert.equal(role(root, "af-draft-note").hidden, true);
  assert.match(role(root, "af-saved-ranges").textContent, /10×.*±320.*20×.*±600.*50×.*±400/);
});

test("an old poll and lagging Console cache cannot overwrite a confirmed settings save", async () => {
  const status = statusBody();
  let delayed = false, delaySettings = false, finishOld, finishSettings, saved = { ...status.microscope.autofocus_settings };
  const { root, logic } = await load({ status, requestOverride: (name, data) => {
    if (name === "status" && delayed) {
      delayed = false; const old = structuredClone(status);
      return new Promise(resolve => { finishOld = () => resolve(old); });
    }
    if (name === "status") return structuredClone(status);
    if (name === "microscope/autofocus-settings") {
      saved = { ...saved, ...data };
      const snapshot = structuredClone(saved);
      if (!data && delaySettings) {
        delaySettings = false;
        return new Promise(resolve => { finishSettings = () => resolve(snapshot); });
      }
      return snapshot;
    }
  } });
  delayed = true;
  const oldPoll = logic._internal.poll();
  role(root, "af-range").type("6100"); role(root, "af-range-apply").click(); await flush();
  finishOld(); await oldPoll;
  assert.equal(role(root, "af-range").value, "6100");
  await logic._internal.poll();
  assert.equal(role(root, "af-range").value, "6100");
  assert.match(role(root, "af-saved-ranges").textContent, /5×.*±6100/);
  delaySettings = true;
  const oldSettings = logic._internal.poll(); await flush();
  assert.equal(typeof finishSettings, "function");
  role(root, "af-range").type("6200"); role(root, "af-range-apply").click(); await flush();
  finishSettings(); await oldSettings;
  assert.equal(role(root, "af-range").value, "6200");
  delayed = true;
  const lateCachedPoll = logic._internal.poll();
  status.microscope.autofocus_settings = structuredClone(saved);
  await logic._internal.poll();
  finishOld(); await lateCachedPoll;
  assert.equal(role(root, "af-range").value, "6200");
});

test("autofocus steps reflect verified results and never turn a scan score into success", async () => {
  const status = statusBody();
  status.microscope.busy = true;
  status.microscope.autofocus_run = { phase: "autofocus", objective_id: 1, stage: "fine", verified: false, completed: [0], score: 123.45, sequence: true, search_half_range_pulses: 750 };
  status.microscope.visual_focus = { travel_used_pulses: 2000, travel_limit_pulses: 12000 };
  status.microscope.objective_id = 1;
  let result = await load({ status });
  assert.match(role(result.root, "af-phase").textContent, /10×.*autofocus.*fine/);
  assert.match(role(result.root, "af-score").textContent, /123.45/);
  assert.match(role(result.root, "af-scan-range").textContent, /±750 pulses.*1500 total span/);
  assert.match(role(result.root, "af-travel").textContent, /Total movement budget.*2000 used.*12000 allowed/);
  assert.match(role(result.root, "af-step-0").textContent, /Verified/);
  assert.match(role(result.root, "af-step-1").textContent, /Running/);
  assert.match(role(result.root, "af-step-2").textContent, /Waiting/);
  assert.equal(role(result.root, "objective-2").disabled, true);
  assert.equal(role(result.root, "autofocus-sequence").disabled, true);
  status.microscope.busy = false;
  status.microscope.autofocus_run.phase = "failed";
  status.microscope.autofocus_run.error = "Sharpness verification failed";
  result = await load({ status });
  assert.match(role(result.root, "af-step-1").textContent, /Failed/);
  assert.match(role(result.root, "af-error").textContent, /Sharpness verification failed/);
  status.microscope.autofocus_run = { phase: "complete", objective_id: 1, verified: true, completed: [0, 1] };
  result = await load({ status });
  assert.equal(role(result.root, "objective-2").disabled, false);
  assert.equal(role(result.root, "objective-3").disabled, false);
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

test("a slow step or end gas shows a warning line", async () => {
  const status = statusBody();
  status.furnace.sequence = { ...status.furnace.sequence, running: true, phase: "ramping", current_index: 0,
    warning: { code: "step_slow", step_index: 0, target_c: 300, minutes: 30, message: "x" } };
  const { root } = await load({ status });
  assert.equal(role(root, "seq-warning").hidden, false);
  assert.equal(role(root, "seq-warning").className, "pr-warn");
  assert.match(role(root, "seq-warning").textContent, /Step 1 has not reached 300 °C after 30 min/);
  status.furnace.sequence.warning = { code: "end_gas_slow", end_gas_until_c: 40, hours: 12 };
  status.furnace.sequence.phase = "cooling";
  const cooling = await load({ status });
  assert.match(role(cooling.root, "seq-warning").textContent, /above 40 °C after 12 h/);
  const healthy = await load();
  assert.equal(role(healthy.root, "seq-warning").hidden, true);
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
