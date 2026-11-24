import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
const require = createRequire(import.meta.url);
const { ParameterModel, ParameterForm, stagePositionView, validate, labelFor, schemaFor } = require("../src/orchestrator/agent_connect/static/command-form.js");
const obj = (properties, required = Object.keys(properties)) => ({ type: "object", properties, required, additionalProperties: false });
const number = { type: "number", minimum: 0, maximum: 10 };
const action = { id: "stage.position", device: "stage", params: obj({ motor: { type: "string", enum: ["bottom", "top", "insert"] }, position: number }), defaults: { motor: "insert", position: 0 } };
const stageState = () => ({ available: true, fresh: true, reason: "", phase: "ready", age_s: 0, connected: true, xy_blocked: false, axes: ["bottom", "top", "insert"].map(id => ({ id, position_rotations: 0, lower_limit_rotations: 0, upper_limit_rotations: 10, calibration: "done", positioning_ready: true, telemetry_fresh: true, available: true, reason: "" })) });
function stageModel() { const model = new ParameterModel(action); model.setConstraints(stageState()); return model; }

test("forms preserve zero and emit typed numbers instead of strings", () => {
  const model = stageModel();
  assert.deepEqual(model.getValue(), { motor: "insert", position: 0 });
  model.set("position", 2.5);
  assert.equal(JSON.parse(model.text).position, 2.5);
  assert.equal(model.error, "");
  model.set("position", "2.5");
  assert.match(model.error, /finite number/);
});

test("advanced JSON and form edits share one validated model", () => {
  const model = stageModel();
  model.setJSON('{"motor":"top","position":3}');
  assert.equal(model.value.motor, "top");
  model.set("position", 4);
  assert.deepEqual(JSON.parse(model.text), { motor: "top", position: 4 });
  model.setJSON('{"motor":');
  assert.throws(() => model.getValue(), /valid JSON/);
  model.set("position", 5);
  assert.equal(model.getValue().position, 5);
  model.setJSON('[]');
  assert.throws(() => model.getValue(), /JSON object/);
});

test("required fields, bounds, enums and unknown fields block send", () => {
  const model = stageModel();
  for (const value of [{ motor: "insert" }, { motor: "other", position: 1 }, { motor: "top", position: 11 }, { motor: "top", position: -1 }, { motor: "top", position: 1, token: "caller" }]) {
    assert.ok(model.setJSON(JSON.stringify(value)));
    assert.throws(() => model.getValue());
  }
});

test("optional values are omitted and boolean false remains explicit", () => {
  const model = new ParameterModel({ params: obj({ new: { type: "boolean" } }, []) });
  assert.deepEqual(model.getValue(), {});
  model.set("new", false);
  assert.deepEqual(model.getValue(), { new: false });
  model.set("new", undefined);
  assert.deepEqual(model.getValue(), {});
});

test("coordinate groups accept XY or Insert and reject a mixed target", () => {
  const groups = { oneOf: [obj({ x: number, y: number }), obj({ insert: number })] };
  const model = new ParameterModel({ params: obj({ target: groups }), defaults: { target: { x: 0, y: 0 } } });
  assert.equal(model.error, "");
  model.set("target", { insert: 1 });
  assert.equal(schemaFor(groups, model.value.target), groups.oneOf[1]);
  model.set("target.x", 2);
  assert.match(model.error, /coordinate group/);
  model.set("target", { x: 2, y: 3 });
  assert.deepEqual(model.getValue(), { target: { x: 2, y: 3 } });
});

test("labels state the unit and no-parameter actions remain valid", () => {
  assert.equal(labelFor(action, "position"), "Position (rotations)");
  assert.equal(labelFor(action, "speed"), "Speed (rotations/s)");
  assert.equal(labelFor({ device: "robot" }, "speed"), "Speed (%)");
  assert.equal(labelFor({ device: "robot" }, "r"), "R (degrees)");
  assert.equal(labelFor({ id: "microscope.select_objective", device: "microscope" }, "id"), "Objective ID");
  assert.equal(labelFor({ id: "robot.jog", device: "robot" }, "step"), "Step (mm for X/Y/Z; degrees for R)");
  assert.equal(labelFor({ id: "turret.select" }, "id"), "Saved profile");
  assert.equal(validate({}, obj({})), "");
  assert.ok(validate({ microstep: 1.5 }, obj({ microstep: { type: "integer" } })));
});

test("furnace parameters start empty and display temperature, ramp and gas units", () => {
  const furnace = { device: "furnace", params: obj({ temperature_c: number }), defaults: { temperature_c: 10 } };
  const model = new ParameterModel(furnace);
  assert.deepEqual(model.value, {}); assert.match(model.error, /required/);
  assert.equal(labelFor(furnace, "temperature_c"), "Temperature (°C)");
  assert.equal(labelFor(furnace, "ramp_rate_c_per_min"), "Ramp (°C/min)");
  assert.equal(labelFor(furnace, "flow_percent"), "Gas flow (%)");
  assert.equal(labelFor(furnace, "end_gas.0"), "Gas channel 0 (%)");
});

test("furnace sequence JSON validates each step and rejects zero ramp", () => {
  const steps = { type: "array", minItems: 1, maxItems: 2, items: obj({ temperature_c: number, ramp_rate_c_per_min: { type: "number", exclusiveMinimum: 0, maximum: 100 } }) };
  const model = new ParameterModel({ device: "furnace", params: obj({ steps }) });
  for (const value of [[], [{ temperature_c: 5, ramp_rate_c_per_min: 0 }], [{ temperature_c: "5", ramp_rate_c_per_min: 1 }]]) {
    model.set("steps", value); assert.ok(model.error);
  }
  model.set("steps", [{ temperature_c: 5, ramp_rate_c_per_min: 1 }]);
  assert.equal(model.error, ""); assert.equal(model.getValue().steps[0].temperature_c, 5);
});

test("absolute stage targets use selected calibrated bounds and preserve zero", () => {
  const model = stageModel(), stage = stageState();
  stage.axes[2].upper_limit_rotations = 3.25;
  stage.axes[1].lower_limit_rotations = -1; stage.axes[1].upper_limit_rotations = 2;
  model.setConstraints(stage); model.set("position", 0);
  assert.equal(model.error, ""); assert.equal(model.stageView.current, 0);
  model.set("position", 3); assert.equal(model.error, "");
  model.set("motor", "top"); assert.match(model.error, /between -1 and 2 rotations/);
  assert.equal(model.value.position, 3);
  model.set("position", 2); assert.equal(model.error, "");
  assert.equal(action.params.properties.position.maximum, 10);
});

test("missing, outdated and unavailable stage context blocks absolute positioning", () => {
  const model = stageModel();
  for (const stage of [null, { ...stageState(), fresh: false }, { ...stageState(), age_s: 3.1 }, { ...stageState(), connected: false }, { ...stageState(), phase: "stale" }]) {
    model.setConstraints(stage); assert.ok(model.error); assert.equal(model.stageView.fresh, false);
  }
  model.setConstraints(stageState(), Date.now() - 4000); assert.ok(model.error);
  const missing = stageState(); missing.axes[2].upper_limit_rotations = null;
  model.setConstraints(missing); assert.match(model.error, /Calibrated limits are unavailable/);
  const busy = stageState(); busy.axes[2].available = false; busy.axes[2].reason = "The axis is moving.";
  model.setConstraints(busy); assert.equal(model.error, "The axis is moving."); assert.equal(model.stageView.bounds, true);
});

test("per-axis XY targets do not apply the composite Insert interlock", () => {
  const stage = { ...stageState(), xy_blocked: true, xy_reason: "Insert is not fully out." };
  assert.equal(stagePositionView(stage, "top", 1).error, "");
  assert.equal(stagePositionView(stage, "bottom", 1).error, "");
});

function domNode(tag, doc) {
  return { tagName: tag.toUpperCase(), children: [], dataset: {}, value: "", textContent: "", attributes: {},
    addEventListener() {}, append(...nodes) { this.children.push(...nodes); }, replaceChildren(...nodes) { this.children = nodes; },
    setAttribute(name, value) { this.attributes[name] = value; }, removeAttribute(name) { delete this.attributes[name]; delete this[name]; },
    focus() { doc.activeElement = this; },
  };
}
test("constraint and axis updates preserve the typed target and focused input", () => {
  const previous = globalThis.document, doc = { createElement: tag => domNode(tag, doc) };
  globalThis.document = doc;
  try {
    const form = new ParameterForm(doc.createElement("div"), doc.createElement("textarea"));
    const descriptor = { ...action, params: obj({ ...action.params.properties, position: { type: "number", minimum: -244140.625, maximum: 244140.625 } }) };
    form.setAction(descriptor);
    const input = form.positionInput;
    assert.equal(input.max, undefined); assert.doesNotMatch(form.stageNote.textContent, /244140/);
    const stage = stageState(); stage.axes[2].upper_limit_rotations = 3.25;
    form.setConstraints(stage, Date.now());
    assert.equal(input.min, "0"); assert.equal(input.max, "3.25");
    assert.match(form.stageNote.textContent, /Current: 0 rotations.*0 to 3.25 rotations/);
    input.value = "1.250"; input.focus(); input.oninput();
    stage.axes[2].position_rotations = 0.5;
    form.setConstraints(stage, Date.now()); form.setChoices({ motor: [] });
    assert.equal(form.positionInput, input); assert.equal(doc.activeElement, input);
    assert.equal(input.value, "1.250"); assert.equal(form.model.value.position, 1.25);
    stage.axes[1].upper_limit_rotations = 2;
    form.update("motor", "top");
    assert.equal(form.positionInput, input); assert.equal(input.max, "2"); assert.equal(input.value, "1.250");
    stage.axes[1].available = false; stage.axes[1].reason = "The axis is moving.";
    form.setConstraints(stage, Date.now());
    assert.match(form.stageOptions.get("top").textContent, /Unavailable/); assert.match(form.stageNote.textContent, /axis is moving/);
    form.setConstraints(null, null);
    assert.equal(input.min, undefined); assert.equal(input.max, undefined); assert.match(form.stageNote.textContent, /limits: unavailable/);
    assert.equal(input.value, "1.250"); assert.equal(form.model.value.position, 1.25);
  } finally { globalThis.document = previous; }
});
