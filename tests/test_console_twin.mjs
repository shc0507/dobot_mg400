import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const path = new URL("../src/orchestrator/web/static/console/twin.js", import.meta.url).pathname;

function load() {
  delete require.cache[path];
  let registered = null;
  globalThis.window = { LabConsole: { register: item => { registered = item; } } };
  require(path);
  return registered;
}

test("the twin workspace registers with its logic and both languages", () => {
  const ws = load();
  assert.equal(ws.id, "twin");
  assert.deepEqual(Object.keys(ws.i18n.en).sort(), Object.keys(ws.i18n.zh).sort());
});

test("Blender axes become glTF axes and robot links mix the joints", () => {
  const { gltfAxis, nodeValue } = load().logic;
  assert.deepEqual(gltfAxis([0, 1, 0]), [0, 0, -1]);
  assert.deepEqual(gltfAxis([0, 0, 1]), [0, 1, 0]);
  const values = { J1: 5, J2: 10, J3: 30, stage_bottom: -4 };
  assert.equal(nodeValue({ joint: "robot", joint_coef: [0, -1, 1, 0] }, values), 20);
  assert.equal(nodeValue({ joint: "stage_bottom" }, values), -4);
  assert.equal(nodeValue({ joint: "turret" }, values), 0);
});

test("the furnace glows only above 50 C and saturates at 1000 C", () => {
  const { furnaceGlow } = load().logic;
  assert.equal(furnaceGlow(21).k, 0);
  assert.equal(furnaceGlow(null).k, 0);
  assert.ok(furnaceGlow(500).k > 0.4 && furnaceGlow(500).k < 0.5);
  assert.equal(furnaceGlow(1200).k, 1);
});

test("entity looks follow the signals", () => {
  const { entityMode } = load().logic;
  assert.equal(entityMode("robot", { robot_alarm: true, robot_stale: true }), "alarm");
  assert.equal(entityMode("robot", { robot_stale: true }), "ghost");
  assert.equal(entityMode("stage", { stage_calibrated: true, stage_moving: true }), "moving");
  assert.equal(entityMode("insert", { stage_calibrated: false }), "ghost");
  assert.equal(entityMode("furnace", {}, { furnace: { available: true } }), "normal");
  assert.equal(entityMode("microscope", {}, {}), "ghost");
});

test("device rows show values and states", () => {
  const ws = load();
  const t = (key, vars) => String(ws.i18n.en[key]).replace(/\{(\w+)\}/g, (_, k) => vars?.[k]);
  const body = {
    devices: { robot: { available: true, enabled: true }, stage: { available: true }, microscope: { available: true, objective_id: 1 },
      furnace: { available: true } },
    joints: { J1: 1, J2: 2, J3: 3, J4: 4, stage_bottom: -3.9, stage_top: -0.6, insert: -2.6 },
    signals: { stage_calibrated: true, furnace_c: 21.3, gas_on: false },
  };
  const rows = ws.logic.rows(body, t);
  assert.deepEqual(rows.map(r => r.key), ["robot", "stage", "microscope", "furnace"]);
  assert.equal(rows[0].state, "enabled");
  assert.match(rows[1].detail, /Y -3\.9 mm · X -0\.6 mm · insert -2\.6 mm/);
  assert.match(rows[2].detail, /objective 1/);
  assert.match(rows[3].detail, /21\.3 °C · gas off/);
});
