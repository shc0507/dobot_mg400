import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
const require = createRequire(import.meta.url);
const { stageAxes, summary, observationQuality, targetPreview, number, deviceVisual } = require("../src/orchestrator/agent_connect/static/portal-visuals.js");
const { requestPreview } = require("../src/orchestrator/agent_connect/static/command-portal.js");
const { ParameterForm } = require("../src/orchestrator/agent_connect/static/command-form.js");

const device = () => ({ quality: "valid", age_s: .1, values: { connected: true, axes: ["top", "bottom", "insert"].map(id => ({ id, position_rotations: 0, lower_limit_rotations: -1, upper_limit_rotations: 3, unit: "rotation", quality: "valid", reference_valid: true, telemetry_age_s: .2, encoder_age_s: .2 })) } });

test("legacy encoder readings show unverified markers without inventing reference validity", () => {
  const state = device();
  for (const axis of state.values.axes) Object.assign(axis, { quality: "unknown", reference_valid: null, reference_revision: null, motion_ready: true, telemetry_age_s: .31, encoder_age_s: .30 });
  Object.assign(state.values.axes[0], { position_rotations: 4.32177734375, lower_limit_rotations: 0, upper_limit_rotations: 8.64111328125 });
  Object.assign(state.values.axes[1], { lower_limit_rotations: null, upper_limit_rotations: null });
  Object.assign(state.values.axes[2], { position_rotations: .0126953125, lower_limit_rotations: 0, upper_limit_rotations: 28.091552734375 });
  const [x, y, insert] = stageAxes(state);
  assert.equal(x.unverified, true); assert.equal(x.quality, "Reference unverified"); assert.ok(x.position > .5);
  assert.equal(y.position, null); assert.equal(y.quality, "Limits unavailable");
  assert.ok(insert.position > 0 && insert.position < .001); assert.equal(insert.quality, "Reference unverified");
  assert.ok(stageAxes(state, 3).every(axis => axis.position === null));
  for (const change of [{ quality: "stale" }, { quality: "unavailable" }, { quality: "uncalibrated" }, { reference_valid: false }, { encoder_age_s: null }, { encoder_age_s: 4 }, { motion_ready: false }, { unit: "mm" }, { unit: null }]) {
    const altered = structuredClone(state); Object.assign(altered.values.axes[2], change);
    assert.equal(stageAxes(altered)[2].position, null, JSON.stringify(change));
  }
  const wrongUnit = structuredClone(state); wrongUnit.values.axes[2].unit = "mm";
  assert.equal(stageAxes(wrongUnit)[2].quality, "Unsupported unit");
  assert.equal(stageAxes(wrongUnit)[2].bounds, false);
});

test("unverified encoder markers have a distinct style and reference label", () => {
  const previous = globalThis.document;
  const make = tag => ({ tag, textContent: "", children: [], style: {}, attributes: {}, append(...items) { this.children.push(...items); }, setAttribute(key, value) { this.attributes[key] = value; } });
  globalThis.document = { createElement: make, createElementNS: (_, tag) => make(tag) };
  const descendants = node => [node, ...node.children.flatMap(descendants)];
  try {
    const state = device();
    for (const axis of state.values.axes) Object.assign(axis, { quality: "unknown", reference_valid: null, motion_ready: true });
    const nodes = descendants(deviceVisual("stage", state));
    assert.ok(nodes.some(node => node.attributes.class === "stage-point stage-point-unverified"));
    assert.ok(nodes.some(node => node.className === "axis-marker axis-marker-unverified" && node.title === "Reference unverified"));
    assert.ok(nodes.some(node => node.textContent.includes("Insert: Reference unverified")));
    state.values.axes[1].lower_limit_rotations = null;
    const noXY = descendants(deviceVisual("stage", state));
    assert.ok(!noXY.some(node => node.tag === "circle"));
    assert.ok(noXY.some(node => node.className === "axis-marker axis-marker-unverified"));
  } finally { globalThis.document = previous; }
});

test("stage axes use each received rotation range without inventing missing limits", () => {
  const state = device();
  state.values.axes[1].lower_limit_rotations = null;
  state.values.axes[2].lower_limit_rotations = 0;
  const [x, y, insert] = stageAxes(state);
  assert.equal(x.label, "X"); assert.equal(x.position, .25);
  assert.equal(y.label, "Y"); assert.equal(y.position, null); assert.equal(y.quality, "Limits unavailable");
  assert.equal(insert.position, 0); assert.equal(insert.value, 0);
  assert.ok(stageAxes(null).every(axis => axis.position === null && axis.value === undefined));
});

test("stage ranges use canonical values and explain each native calibration state", () => {
  const state = device();
  state.cached_state = { values: { motors: state.values.axes.map(axis => ({ id: axis.id, lower_limit_set: true, upper_limit_set: true, calibration: "done" })) } };
  Object.assign(state.values.axes[0], { position_rotations: 4.322998046875, lower_limit_rotations: 0, upper_limit_rotations: 8.64306640625, quality: "unknown", reference_valid: null, motion_ready: true });
  for (const index of [1, 2]) {
    Object.assign(state.values.axes[index], { lower_limit_rotations: null, upper_limit_rotations: null });
    Object.assign(state.cached_state.values.motors[index], { lower_limit_set: false, upper_limit_set: false, lower_limit: -244140.625, upper_limit: 244140.625, calibration: "none" });
  }
  const [x, y, insert] = stageAxes(state);
  assert.equal(x.min, 0); assert.equal(x.max, 8.64306640625); assert.match(x.status, /Calibration complete/); assert.match(x.status, /Reference unverified/);
  for (const axis of [y, insert]) {
    assert.equal(axis.min, null); assert.equal(axis.max, null); assert.equal(axis.bounds, false); assert.equal(axis.position, null);
    assert.equal(axis.status, "Limits not set · Not calibrated");
  }
  state.values.axes[0].upper_limit_rotations = 9;
  assert.equal(stageAxes(state)[0].max, 9);
  Object.assign(state.cached_state.values.motors[0], { upper_limit_set: false, calibration: "none" });
  const cleared = stageAxes(state)[0];
  assert.equal(cleared.max, null); assert.equal(cleared.bounds, false); assert.equal(cleared.position, null); assert.match(cleared.status, /Maximum not set/);
  assert.ok(stageAxes(state, 4).every(axis => axis.status.includes("Outdated reading") && axis.position === null));
});

test("stage faults remain explicit and remove current markers", () => {
  const state = device();
  state.values.axes[0].fault = "tracking";
  const fault = stageAxes(state)[0];
  assert.equal(fault.position, null); assert.equal(fault.quality, "Fault: tracking"); assert.match(fault.status, /Fault: tracking/);
  assert.match(stageAxes(state, 4)[0].status, /Outdated reading.*Fault: tracking/);
  state.values.axes[0].fault = "none";
  assert.equal(stageAxes(state)[0].position, .25);
});

test("stage table shows current position and each bound when another axis has no range", () => {
  const previous = globalThis.document;
  const make = tag => ({ tag, textContent: "", children: [], style: {}, attributes: {}, append(...items) { this.children.push(...items); }, setAttribute(key, value) { this.attributes[key] = value; } });
  globalThis.document = { createElement: make, createElementNS: (_, tag) => make(tag) };
  const descendants = node => [node, ...node.children.flatMap(descendants)];
  try {
    const state = device(); state.values.axes[1].lower_limit_rotations = null;
    const nodes = descendants(deviceVisual("stage", state));
    const wrapper = nodes.find(node => node.className === "stage-axis-table");
    assert.equal(wrapper.tag, "div"); assert.equal(wrapper.children[0].tag, "table");
    const rows = descendants(wrapper).filter(node => node.tag === "tr").map(row => row.children.map(cell => cell.textContent));
    assert.deepEqual(rows[0], ["Axis", "Current (rot)", "Min (rot)", "Max (rot)", "Status"]);
    assert.deepEqual(rows[1], ["X", "0", "-1", "3", "Current"]);
    assert.deepEqual(rows[2], ["Y", "0", "Unknown", "3", "Limits unavailable"]);
    assert.equal(rows[3][0], "Insert"); assert.equal(rows[3][2], "-1");
    assert.ok(!nodes.some(node => node.tag === "circle"));
    assert.ok(nodes.some(node => node.textContent === "Y limits unavailable"));
    state.values.axes[0].lower_limit_rotations = null;
    assert.ok(descendants(deviceVisual("stage", state)).some(node => node.textContent === "X/Y limits unavailable"));
    state.values.axes[0].position_rotations = null;
    assert.ok(descendants(deviceVisual("stage", state)).some(node => node.textContent === "XY position unavailable"));
  } finally { globalThis.document = previous; }
});

test("stale, disconnected, uncalibrated and out-of-range axes never draw current markers", () => {
  assert.ok(stageAxes(device(), 4).every(axis => axis.position === null));
  const disconnected = device(); disconnected.values.connected = false;
  assert.ok(stageAxes(disconnected).every(axis => axis.position === null));
  const stale = device(); stale.values.axes[0].quality = "uncalibrated";
  assert.equal(stageAxes(stale)[0].position, null);
  stale.values.axes[0].quality = "valid"; stale.values.axes[0].position_rotations = 5;
  assert.equal(stageAxes(stale)[0].position, null); assert.equal(stageAxes(stale)[0].quality, "Outside limits");
  stale.values.axes[0].position_rotations = 0; stale.values.axes[0].telemetry_age_s = null;
  assert.equal(stageAxes(stale)[0].position, null);
  stale.values.axes[0].telemetry_age_s = 0; stale.values.axes[0].position_rotations = null;
  assert.equal(stageAxes(stale)[0].quality, "Position unknown");
});

test("missing readings remain unknown and normalized units remain explicit", () => {
  assert.equal(number(null, "mm"), "Unknown"); assert.equal(number(0, "mm"), "0 mm");
  const robot = Object.fromEntries(summary("robot", { values: { pose_mm_deg: [0, null, 1.25, -4], vacuum: { suck: false } } }));
  assert.equal(robot.X, "0 mm"); assert.equal(robot.Y, "Unknown"); assert.equal(robot.R, "-4 °"); assert.equal(robot.Suction, "No");
  const furnace = Object.fromEntries(summary("furnace", { values: { pv_c: 21.5, sp_c: null }, cached_state: { values: { flow: { connected: false } } } }));
  assert.equal(furnace.Measured, "21.5 °C"); assert.equal(furnace.Setpoint, "Unknown"); assert.equal(furnace["Gas controller"], "Disconnected");
  const microscope = Object.fromEntries(summary("microscope", { values: { focus_position_pulses: null, focus_zero_known: false } }));
  assert.equal(microscope.Focus, "Unknown"); assert.equal(microscope["Zero known"], "No");
});

test("observation quality is independent of a current device cache", () => {
  const observation = { available: true, quality: "unknown", cache_age_s: 0, stats: { mean_celsius: 20.5 } };
  const thermal = Object.fromEntries(summary("thermal", { quality: "valid", observations: observation }));
  assert.equal(thermal.Mean, "20.5 °C"); assert.equal(thermal.Statistics, "Unverified");
  assert.equal(observationQuality({ ...observation, quality: "valid" }, 4), "Outdated");
  assert.equal(observationQuality({ ...observation, available: false }), "Unavailable");
  assert.equal(observationQuality({ ...observation, quality: "valid", cache_age_s: null }), "Outdated");
});

test("selected target preview uses current calibrated limits and never clamps a target", () => {
  const action = { id: "stage.position" }, params = { motor: "top", position: 1 };
  const stage = { fresh: true, available: true, connected: true, age_s: 0, axes: [{ id: "top", telemetry_fresh: true, position_rotations: 0, lower_limit_rotations: 0, upper_limit_rotations: 2 }] };
  assert.equal(targetPreview(action, params, stage).position, .5);
  assert.equal(targetPreview(action, { ...params, position: 3 }, stage).position, null);
  assert.equal(targetPreview(action, params, stage, 4).position, null);
  assert.equal(targetPreview(action, params, null).current, null);
  assert.equal(targetPreview({ id: "robot.enable" }, {}, stage), null);
});

test("request preview preserves exact fields and clearly marks the generated request ID", () => {
  const client = { selected: "stage.position", status: { boot_id: "boot", control_epoch: "boot:2" }, catalog: { contract_id: "sha256:abc" } };
  assert.deepEqual(requestPreview(client, { motor: "top", position: 0 }, { stable: false }), { request_id: "<generated on Send>", boot_id: "boot", contract_id: "sha256:abc", control_epoch: "boot:2", action: "stage.position", params: { motor: "top", position: 0 }, preconditions: { stable: false } });
});

test("choice polling preserves a focused form and refreshes choices after focus leaves", () => {
  const previous = globalThis.document, focused = {};
  globalThis.document = { activeElement: focused };
  try {
    const form = Object.create(ParameterForm.prototype);
    let renders = 0;
    Object.assign(form, { choices: {}, model: { action: { id: "transfer.position" } }, container: { contains: value => value === focused }, render: () => renders++ });
    form.setChoices({ name: [{ value: "new" }] });
    assert.equal(renders, 0); assert.equal(form.choicesPending, true);
    globalThis.document.activeElement = null;
    form.setChoices({ name: [{ value: "new" }] });
    assert.equal(renders, 1); assert.equal(form.choicesPending, false);
  } finally { globalThis.document = previous; }
});
