import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const transfer = require("../src/orchestrator/web/static/console/sample-transfer.js");
class Element {
  constructor(tag) { Object.assign(this, { tagName: tag, children: [], attributes: {}, listeners: {}, ownText: "", value: "", hidden: false, disabled: false }); }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_) { throw new Error("Profile content must remain text."); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ""; this.children = children; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  click() { if (!this.disabled) return this.listeners.click?.({ target: this }); }
  type(value) { if (!this.disabled) { this.value = value; return this.listeners.input?.({ target: this }); } }
  change(value) { if (!this.disabled) { this.value = value; return this.listeners.change?.({ target: this }); } }
  focus() {}
}
function el(tag, props = {}, ...children) {
  const node = new Element(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (["hidden", "disabled", "value"].includes(key)) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) if (child !== undefined && child !== null && child !== false) node.append(typeof child === "string" ? Object.assign(new Element("text"), { ownText: child }) : child);
  return node;
}
const walk = node => [node, ...node.children.flatMap(walk)];
const flush = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const endpoint = insert => ({ position_rotations: { x: 4, y: 8, insert }, captured_at: "2026-11-12T00:00:00Z" });
const stagePosition = (x, y) => ({ position_rotations: { x, y }, captured_at: "2026-11-12T00:00:00Z" });
const profile = (id = "p1", positions = { Loading: endpoint(10), Furnace: endpoint(130) }, stage_positions = { out_stage: stagePosition(4, 8), microscope: stagePosition(12, 16) }) => ({ format: "sample-transfer.calibration-profile", schema_version: 5, unit: "rotations", id, name: "Transfer " + id, positions, stage_positions });
const state = (overrides = {}) => ({ demo: false, has_control: true, position_available: true, record_ready: true, endpoint_record_ready: true, named_motion_ready: true, position: { x: 4, y: 8, insert: 42 }, position_source: { kind: "stage_encoder", name: "Stage encoders" }, draft: profile(), active_profile: profile(), profiles: [{ id: "p1", name: "Transfer p1", complete: true, stage_position_count: 2 }], selected_profile: "p1", operation: { status: "idle", target: null, message: "", mine: false, id: null }, tolerance_rotations: 0.00001, transfer_reference: { ready: true, ready_targets: { fully_out: true, fully_in: true }, source: { name: "Stage encoders" }, targets_rotations: { fully_out: 0, fully_in: 150 } }, stage_reference: { ready_targets: { out_stage: true, microscope: true }, targets_rotations: { out_stage: { x: 4, y: 8 }, microscope: { x: 12, y: 16 } }, profile_name: "Transfer p1", profile_id: "p1" }, ...overrides });

function setup(initial = state(), handler, environmentOverrides = {}) {
  let response = initial, language = "en", interval = null;
  const calls = [], notices = [], listeners = {}, languageListeners = [], revoked = [], downloads = [];
  const environment = {
    document: { hidden: false, addEventListener: (name, fn) => { listeners[name] = fn; } },
    addEventListener: (name, fn) => { listeners[name] = fn; },
    setInterval: fn => { interval = fn; return 1; }, clearInterval: () => { interval = null; }, setTimeout: fn => fn(),
    Blob, URL: { createObjectURL: value => { downloads.push(value); return "blob:profile"; }, revokeObjectURL: url => revoked.push(url) },
    ...environmentOverrides,
  };
  const api = {
    el, lang: () => language, onLanguage: fn => languageListeners.push(fn), notify: (...args) => notices.push(args),
    request: async (...args) => { calls.push(args); return handler ? handler(...args) : response; },
    take: async device => { calls.push(["take", device]); response = { ...response, has_control: true }; },
    release: async device => { calls.push(["release", device]); response = { ...response, has_control: false }; },
  };
  const ui = transfer.workspace(environment), root = el("section"); ui.mount(root, api); ui._test.apply(initial);
  return { ui, root, calls, notices, listeners, environment, downloads, revoked,
    role: id => walk(root).find(node => node.getAttribute("data-role") === "st-" + id),
    response: value => { response = value; }, tick: () => interval?.(),
    lang: value => { language = value; languageListeners.forEach(fn => fn()); } };
}

test("show and passive polls only read encoder state", async () => {
  const page = setup(); await page.ui.show();
  assert.deepEqual(page.calls.map(call => call.slice(0, 2)), [["state"]]);
  assert.equal(page.role("position-insert").textContent, "42.0000");
  await page.tick(); await page.tick();
  assert.equal(page.calls.filter(call => call[0] === "measure").length, 0);
  page.response(state({ position: { x: null, y: 9, insert: null } })); await page.tick();
  assert.equal(page.role("position-x").textContent, "—");
  assert.equal(page.role("position-insert").textContent, "—");
  await page.ui.hide(); assert.equal(page.calls.some(call => ["move", "jog", "stop"].includes(call[0])), false);
});

test("read-only access preserves coordinates and all STOP buttons need no control", async () => {
  const page = setup(state({ has_control: false }));
  for (const id of ["move-fully_out", "move-fully_in", "move-out_stage", "move-microscope", "record", "record-out_stage", "record-microscope", "delete-out_stage", "new", "save", "jog-insert-cw"]) assert.equal(page.role(id).disabled, true, id);
  for (const id of ["stop-fully_out", "stop-fully_in", "stop-out_stage", "stop-microscope", "stop-calibration"]) { assert.equal(page.role(id).disabled, false); await page.role(id).click(); }
  assert.deepEqual(page.calls.map(call => call.slice(0, 2)), Array.from({ length: 5 }, () => ["stop", {}]));
  const demo = setup(state({ demo: true, has_control: false }));
  assert.equal(demo.role("move-fully_out").disabled, true); assert.equal(demo.role("control").hidden, false);
});

test("stage ownership uses the Console lease and refreshes the controls", async () => {
  const page = setup(state({ has_control: false })); await page.ui.show(); page.calls.length = 0;
  await page.role("control").click(); assert.equal(page.role("control").textContent, "Release");
  await page.role("control").click(); assert.equal(page.role("control").textContent, "Take control");
  assert.deepEqual(page.calls.filter(call => ["take", "release"].includes(call[0])), [["take", "stage"], ["release", "stage"]]);
  await page.ui.hide();
});

test("record sends only a position name, and polling preserves both input names", async () => {
  const initial = state({ draft: profile("p1", {}) }); let response = initial;
  const page = setup(initial, async (path, data) => {
    if (path === "record") { assert.deepEqual(data, { name: "Robot pickup" }); response = { ...response, draft: profile("p1", { "Robot pickup": endpoint(18) }) }; }
    return response;
  }); await page.ui.show();
  const input = page.role("name"); input.type("My edited profile"); page.role("position-name").type("Robot pickup");
  await page.tick(); assert.equal(page.role("name"), input); assert.equal(input.value, "My edited profile");
  await page.role("record").click();
  assert.equal(input.value, "My edited profile");
  assert.equal(page.role("progress").textContent, "1 positions recorded");
  assert.equal(page.role("position-name").value, "Robot pickup");
  assert.equal(page.role("record").textContent, "Record again");
  assert.match(page.role("recorded-0").textContent, /18.0000rot/);
  page.lang("zh"); assert.equal(page.role("name").value, "My edited profile");
  await page.ui.hide();
});

test("new, save, selection, empty profiles and delete use their separate routes", async () => {
  let value = state({ draft: null, profiles: [], selected_profile: null });
  const page = setup(value, async (path, data) => {
    if (path === "profile/new") value = { ...value, draft: profile("draft", {}) };
    if (path === "profile/save") value = { ...value, draft: { ...profile("p2"), name: data.name }, profiles: [{ id: "p2", name: data.name, complete: true }, { id: "partial", name: "Partial", complete: false }], selected_profile: "p2" };
    if (path === "profile/load") value = { ...value, draft: profile("partial", {}) };
    if (path === "profile/select") value = { ...value, draft: profile(data.id), selected_profile: data.id };
    if (path === "profile/delete") value = { ...value, profiles: value.profiles.filter(item => item.id !== data.id) };
    return value;
  });
  await page.role("new").click(); assert.deepEqual(page.calls[0].slice(0, 2), ["profile/new", { name: transfer.logic.defaultProfileName(new Date()) }]);
  page.role("name").type("My transfer"); await page.role("save").click();
  assert.deepEqual(page.calls.at(-1).slice(0, 2), ["profile/save", { name: "My transfer" }]);
  await page.role("select-partial").click(); assert.equal(page.calls.at(-1)[0], "profile/select");
  page.ui._test.apply({ ...value, selected_profile: null }); await page.role("select-p2").click(); assert.equal(page.calls.at(-1)[0], "profile/select");
  await page.role("delete-p2").click(); assert.equal(page.calls.at(-1)[0], "profile/select");
  await page.role("cancel").click(); assert.equal(page.calls.at(-1)[0], "profile/select");
  await page.role("delete-p2").click(); await page.role("confirm").click();
  assert.deepEqual(page.calls.at(-1).slice(0, 2), ["profile/delete", { id: "p2" }]);
});

test("discard confirmation protects a typed name and upload passes JSON without HTML", async () => {
  const page = setup(); page.role("name").type("Keep this name");
  await page.role("new").click(); assert.equal(page.calls.length, 0);
  await page.role("cancel").click(); assert.equal(page.role("name").value, "Keep this name");
  const file = page.role("file"); file.files = [{ size: 20, text: async () => "{bad" }]; await file.listeners.change({ target: file });
  assert.equal(page.calls.length, 0); assert.match(page.role("error-message").textContent, /valid JSON/);
  const contents = JSON.stringify({ ...profile(), name: "<img src=x>" });
  file.files = [{ size: contents.length, text: async () => contents }]; await file.listeners.change({ target: file });
  assert.equal(page.calls.length, 0); await page.role("confirm").click();
  assert.deepEqual(page.calls[0].slice(0, 2), ["profile/upload", { profile: contents }]);
  await page.role("download-p1").click();
  assert.equal(page.calls.at(-1)[0], "profile/p1"); assert.equal(page.downloads.length, 1); assert.deepEqual(page.revoked, ["blob:profile"]);
});

test("busy and unavailable states disable actions but never disable STOP", async () => {
  const page = setup(state({ operation: { status: "settling", mine: false } })); await page.ui.show();
  assert.equal(page.calls.some(call => call[0] === "measure"), false);
  for (const id of ["move-fully_out", "record", "jog-top-cw", "measure"]) assert.equal(page.role(id).disabled, true);
  assert.equal(page.role("stop-fully_out").disabled, false);
  await page.ui.hide(); assert.equal(page.calls.some(call => call[0] === "stop"), false);
  const failed = setup(state(), async () => { throw new Error("Console offline"); }); await failed.ui.show();
  assert.equal(failed.role("move-fully_in").disabled, true); assert.equal(failed.role("stop-fully_in").disabled, false);
  await failed.role("stop-fully_in").click(); assert.match(failed.role("error-message").textContent, /Console offline/); await failed.ui.hide();
});

test("sequential motion shows its current motor and completed axis count", () => {
  const page = setup(state({ operation: { id: "sequence", status: "moving", target: "Pickup", target_rotations: 5, motor: "bottom", completed_axes: 0, total_axes: 3, mine: true } }));
  assert.equal(page.role("motion-scale").textContent, "bottom · 0/3");
  assert.equal(page.role("stop-fully_out").disabled, false);
  assert.equal(page.role("move-fully_in").disabled, true);
  assert.equal(page.calls.length, 0);
});


test("a stopped worker still settling blocks starts and jogs until it finishes", async () => {
  const page = setup(state({ operation: { status: "stopped", settling: true, mine: true } })); await page.ui.show();
  assert.equal(page.role("move-fully_out").disabled, true); assert.equal(page.role("jog-insert-cw").disabled, true);
  assert.equal(page.role("measure").disabled, true); assert.equal(page.role("stop-fully_in").disabled, false);
  assert.equal(page.calls.some(call => call[0] === "measure"), false);
  page.response(state({ operation: { status: "stopped", settling: false, mine: true } })); await page.tick();
  assert.equal(page.role("move-fully_out").disabled, false); assert.equal(page.role("jog-insert-cw").disabled, false);
  await page.ui.hide();
});

test("transfer uses live limits and the operation keeps its admitted target", () => {
  const page = setup(state({ draft: profile("draft", { Pickup: endpoint(88) }), active_profile: profile("saved", { Other: endpoint(99) }), operation: { status: "moving", target: "fully_out", target_rotations: 5, motor: "insert", mine: false } }));
  const targets = walk(page.root).filter(node => node.className === "st-action-target").slice(0, 2).map(node => node.textContent);
  assert.deepEqual(targets, ["Fully out · 0.0000 rot", "Fully in · 150.0000 rot"]);
  assert.match(page.role("distance").textContent, /37.0000 rot/);
  assert.match(page.role("recorded-0").textContent, /88.0000rot/);
  assert.equal(page.role("selector"), undefined);
  assert.equal(page.role("reference").textContent, "Stage encoders");
});

test("Insert limit controls use the shared position source without a profile source", () => {
  const page = setup(state({ active_profile: null, transfer_reference: { ready: true, ready_targets: { fully_out: true, fully_in: true }, targets_rotations: { fully_out: 0, fully_in: 1 } } }));
  assert.equal(page.role("reference").textContent, "Stage encoders");
  assert.equal(page.role("move-fully_out").disabled, false);
  assert.equal(page.role("move-fully_in").disabled, false);
});

test("transfer needs no saved profile and enables each live Insert target independently", async () => {
  const page = setup(state({ draft: profile("empty", {}), active_profile: null, profiles: [], selected_profile: null }));
  assert.equal(page.role("move-fully_out").disabled, false);
  assert.equal(page.role("move-fully_in").disabled, false);
  page.ui._test.apply(state({ active_profile: null, transfer_reference: { ready: false, ready_targets: { fully_out: true, fully_in: false }, source: { name: "Stage encoders" }, targets_rotations: { fully_out: 0, fully_in: null }, message: "Set Insert upper limit." } }));
  assert.equal(page.role("move-fully_out").disabled, false);
  assert.equal(page.role("move-fully_in").disabled, true);
  await page.role("move-fully_out").click();
  assert.deepEqual(page.calls.at(-1).slice(0, 2), ["move", { target: "fully_out" }]);
  page.ui._test.apply(state({ transfer_reference: { ready: false, source: null, targets_rotations: {}, message: "Calibrate Insert." } }));
  assert.equal(page.role("move-fully_out").disabled, true);
  assert.equal(page.role("move-fully_in").disabled, true);
  assert.equal(page.role("help").textContent, "Calibrate Insert.");
  assert.equal(page.role("stop-fully_out").disabled, false);
});

test("stage move buttons use saved XY targets and show the Y then X sequence", async () => {
  const page = setup(state(), async path => path === "stage/move" ? state({ operation: { id: "xy", mine: true, status: "moving", target: "out_stage", target_rotations: 8, motor: "bottom", completed_axes: 0, total_axes: 2 } }) : state());
  assert.equal(page.role("move-out_stage").textContent, "Move Out Stage");
  assert.equal(page.role("move-microscope").textContent, "Move to Microscope");
  assert.match(page.root.textContent, /Bottom \(Y\) → Top \(X\)/);
  await page.role("move-out_stage").click();
  assert.deepEqual(page.calls[0].slice(0, 2), ["stage/move", { target: "out_stage" }]);
  assert.equal(page.role("motion-scale").textContent, "bottom · 0/2");
  for (const role of ["move-fully_out", "move-fully_in", "move-out_stage", "move-microscope"]) assert.equal(page.role(role).disabled, true, role);
  for (const role of ["stop-fully_out", "stop-fully_in", "stop-out_stage", "stop-microscope"]) assert.equal(page.role(role).disabled, false, role);
  await page.ui.hide();
  assert.equal(page.calls.at(-1)[0], "stop");
});

test("XY readiness is independent of Insert and each saved stage position", async () => {
  const page = setup(state({ position: { x: 4, y: 8, insert: null }, record_ready: false, transfer_reference: { ready: false, ready_targets: {}, targets_rotations: {} }, stage_reference: { ready_targets: { out_stage: false, microscope: true }, targets_rotations: { out_stage: null, microscope: { x: 12, y: 16 } } } }));
  assert.equal(page.role("move-fully_out").disabled, true);
  assert.equal(page.role("move-fully_in").disabled, true);
  assert.equal(page.role("move-out_stage").disabled, true);
  assert.equal(page.role("move-microscope").disabled, false);
  assert.equal(page.role("record-out_stage").disabled, false);
  assert.equal(page.role("record-microscope").disabled, false);
  assert.equal(page.role("target-out_stage").textContent, "Not recorded");
  await page.role("move-microscope").click();
  assert.deepEqual(page.calls[0].slice(0, 2), ["stage/move", { target: "microscope" }]);
  page.role("position-name").type("Full coordinates");
  assert.equal(page.role("record").disabled, true);
  page.ui._test.apply(state({ active_profile: null, stage_reference: { ready_targets: {}, targets_rotations: {}, message: "Select a saved profile." } }));
  assert.equal(page.role("move-out_stage").disabled, true);
  assert.equal(page.role("move-microscope").disabled, true);
  assert.equal(page.role("stage-help").textContent, "Select a saved profile.");
  assert.equal(page.role("move-fully_in").disabled, false);
});

test("automatic profile names use the browser local date for initial drafts, New and empty-name fallback", async () => {
  class BrowserDate extends Date {
    constructor() { super("2026-11-20T02:30:00Z"); }
    getFullYear() { return 2026; }
    getMonth() { return 10; }
    getDate() { return 19; }
  }
  const expected = "Stage Coordinate Calibration 2026-11-19";
  let value = state({ draft_name_is_default: true, draft: { ...profile("draft", {}, {}), name: "Stage Coordinate Calibration 2026-11-20" } });
  const page = setup(value, async (path, data) => {
    if (path === "profile/new") value = { ...value, draft_name_is_default: false, draft: { ...profile("new", {}, {}), name: data.name } };
    if (path === "profile/save") value = { ...value, draft_name_is_default: false, draft: { ...value.draft, name: data.name } };
    return value;
  }, { Date: BrowserDate });
  assert.equal(page.role("name").value, expected);
  page.lang("zh");
  assert.equal(page.role("name").value, expected);
  await page.role("save").click();
  assert.deepEqual(page.calls.at(-1).slice(0, 2), ["profile/save", { name: expected }]);
  await page.role("new").click();
  assert.deepEqual(page.calls.at(-1).slice(0, 2), ["profile/new", { name: expected }]);
  page.ui._test.apply(state({ draft: { ...profile("empty", {}, {}), name: "" } }));
  assert.equal(page.role("name").value, expected);
  page.ui._test.apply(state({ draft: { ...profile(), name: "Sample positions" } }));
  assert.equal(page.role("name").value, "Sample positions");
  page.ui._test.apply(state({ draft: { ...profile(), name: "Stage Coordinate Calibration 2026-11-01" } }));
  assert.equal(page.role("name").value, "Stage Coordinate Calibration 2026-11-01");
  page.role("name").type("My calibration");
  page.ui._test.apply(value);
  assert.equal(page.role("name").value, "My calibration");
});

test("named coordinate deletion changes only the draft and requires confirmation", async () => {
  let value = state({ draft: profile("p1", { "<Pickup>": endpoint(88) }) });
  const page = setup(value, async (path, data) => {
    assert.equal(path, "position/delete");
    assert.deepEqual(data, { name: "<Pickup>" });
    value = { ...value, draft: profile("p1", {}) }; return value;
  });
  assert.match(page.role("recorded-0").textContent, /<Pickup>/);
  await page.role("position-delete-0").click();
  assert.equal(page.calls.length, 0);
  await page.role("cancel").click();
  assert.equal(page.calls.length, 0);
  await page.role("position-delete-0").click();
  await page.role("confirm").click();
  assert.equal(page.role("recorded-0"), undefined);
  assert.equal(page.role("move-fully_out").disabled, false);
  assert.equal(page.calls.length, 1);
});

test("calibration tabs preserve named inputs and contain dedicated XY position recording", async () => {
  const page = setup();
  assert.equal(page.role("tab-operate").getAttribute("aria-selected"), "true");
  await page.role("tab-calibration").click();
  page.role("name").type("Stored positions");
  page.role("position-name").type("Robot pickup");
  await page.role("tab-operate").click();
  await page.role("tab-calibration").click();
  assert.equal(page.role("position-name").value, "Robot pickup");
  assert.equal(page.role("name").value, "Stored positions");
  assert.equal(page.role("tab-calibration").getAttribute("aria-selected"), "true");
  assert.equal(page.role("stop-calibration").hidden, false);
  assert.equal(page.role("stop-calibration").disabled, false);
  assert.equal(page.role("record-out_stage").disabled, false);
  assert.equal(page.role("record-microscope").disabled, false);
  assert.match(page.role("record-out_stage").getAttribute("aria-label"), /^Stage Out position:/);
  assert.match(page.role("record-microscope").getAttribute("aria-label"), /^Microscope position:/);
  assert.equal(page.calls.length, 0);
});

test("recorded XY stage positions remain draft values until the profile is saved", async () => {
  let value = state({ draft: profile("p1", {}, {}) });
  const page = setup(value, async (path, data) => {
    if (path === "endpoint/record") {
      assert.deepEqual(Object.keys(data), ["target"]);
      value = { ...value, draft: { ...value.draft, stage_positions: { ...value.draft.stage_positions, [data.target]: data.target === "out_stage" ? stagePosition(1, 2) : stagePosition(3, 4) } } };
    } else if (path === "profile/save") {
      assert.deepEqual(data, { name: "My positions" });
      value = { ...value, active_profile: value.draft, stage_reference: { ready_targets: { out_stage: true, microscope: true }, profile_name: "My positions", targets_rotations: { out_stage: { x: 1, y: 2 }, microscope: { x: 3, y: 4 } } } };
    } else if (path === "endpoint/delete") {
      assert.deepEqual(data, { target: "out_stage" });
      value = { ...value, draft: { ...value.draft, stage_positions: { microscope: value.draft.stage_positions.microscope } } };
    }
    return value;
  });
  assert.equal(page.role("endpoint-status-out_stage").textContent, "Not recorded");
  assert.equal(page.role("delete-out_stage").hidden, true);
  page.role("name").type("My positions");
  await page.role("record-out_stage").click();
  await page.role("record-microscope").click();
  assert.equal(page.role("endpoint-out_stage-x").textContent, "1.0000");
  assert.equal(page.role("endpoint-out_stage-y").textContent, "2.0000");
  assert.equal(page.role("endpoint-microscope-x").textContent, "3.0000");
  assert.equal(page.role("endpoint-microscope-y").textContent, "4.0000");
  assert.equal(page.role("endpoint-out_stage-insert"), undefined);
  assert.equal(page.role("endpoint-microscope-insert"), undefined);
  assert.equal(page.role("record-out_stage").textContent, "Record again");
  assert.equal(page.role("name").value, "My positions");
  const targets = () => [page.role("target-out_stage").textContent, page.role("target-microscope").textContent];
  assert.deepEqual(targets(), ["X 4.0000 rot · Y 8.0000 rot", "X 12.0000 rot · Y 16.0000 rot"]);
  await page.role("save").click();
  assert.deepEqual(targets(), ["X 1.0000 rot · Y 2.0000 rot", "X 3.0000 rot · Y 4.0000 rot"]);
  await page.role("delete-out_stage").click();
  assert.notEqual(page.calls.at(-1)[0], "endpoint/delete");
  await page.role("confirm").click();
  assert.equal(page.role("endpoint-status-out_stage").textContent, "Not recorded");
  assert.equal(page.role("endpoint-status-microscope").textContent, "Complete");
  assert.deepEqual(targets(), ["X 1.0000 rot · Y 2.0000 rot", "X 3.0000 rot · Y 4.0000 rot"]);
  assert.equal(page.calls.some(call => ["move", "stage/move"].includes(call[0])), false);
});

test("profile changes refresh XY cards and stale encoders block recording", () => {
  const page = setup();
  page.ui._test.apply(state({ draft: profile("new", {}, {}) }));
  assert.equal(page.role("endpoint-status-out_stage").textContent, "Not recorded");
  assert.equal(page.role("endpoint-microscope-x").textContent, "—");
  page.ui._test.apply(state({ draft: profile("loaded", {}, { out_stage: stagePosition(15, 30) }), endpoint_record_ready: false }));
  assert.equal(page.role("endpoint-out_stage-x").textContent, "15.0000");
  assert.equal(page.role("record-out_stage").disabled, true);
  assert.equal(page.role("record-microscope").disabled, true);
  assert.equal(page.role("delete-out_stage").disabled, false);
  assert.equal(page.role("stop-calibration").disabled, false);
});

test("the selected profile can load its draft after New or a different draft", async () => {
  const initial = state({ draft: profile("other", {}) });
  const page = setup(initial, async path => {
    assert.equal(path, "profile/load");
    return state();
  });
  assert.equal(page.role("select-p1").textContent, "Load draft");
  assert.equal(page.role("select-p1").disabled, false);
  await page.role("select-p1").click();
  assert.deepEqual(page.calls[0].slice(0, 2), ["profile/load", { id: "p1" }]);
  assert.equal(page.role("name").value, "Transfer p1");
  assert.equal(page.role("progress").textContent, "2 positions recorded");
  assert.equal(page.role("select-p1").textContent, "Selected");
  assert.equal(page.role("select-p1").disabled, true);
  page.ui._test.apply(state({ draft: profile("new", {}) }));
  assert.equal(page.role("select-p1").textContent, "Load draft");
  assert.equal(page.role("select-p1").disabled, false);
});

test("upload rejects files above the backend limit before reading or sending them", async () => {
  const page = setup(); const file = page.role("file");
  file.files = [{ size: 128 * 1024 + 1, text: async () => { throw new Error("Oversized file must not be read"); } }];
  await file.listeners.change({ target: file });
  assert.equal(page.calls.length, 0); assert.match(page.role("error-message").textContent, /128 KiB/);
  const contents = JSON.stringify(profile()); file.files = [{ size: 128 * 1024, text: async () => contents }];
  await file.listeners.change({ target: file });
  assert.deepEqual(page.calls[0].slice(0, 2), ["profile/upload", { profile: contents }]);
});

test("move targets the Insert endpoint and hide cancels only an operation started here", async () => {
  const page = setup(state(), async path => path === "move" ? state({ operation: { id: "own", mine: true, status: "moving", target: "fully_out", target_rotations: 0, motor: "insert", message: "Moving Insert" } }) : state());
  await page.role("move-fully_out").click();
  assert.deepEqual(page.calls[0].slice(0, 2), ["move", { target: "fully_out" }]);
  assert.match(page.role("distance").textContent, /42.0000 rot/);
  await page.ui.hide(); assert.equal(page.calls.at(-1)[0], "stop");
  const observer = setup(state({ operation: { id: "other", mine: false, status: "moving" } }));
  await observer.ui.hide(); assert.equal(observer.calls.length, 0);
});

test("page close sends an owned operation STOP with keepalive", async () => {
  const page = setup(state(), async path => path === "move" ? state({ operation: { id: "own", mine: true, status: "moving" } }) : state());
  await page.role("move-fully_out").click(); await page.listeners.pagehide();
  assert.equal(page.calls.at(-1)[0], "stop"); assert.equal(page.calls.at(-1)[2].keepalive, true);
});

test("hide during pending motion stops immediately and again after late acceptance", async () => {
  const move = deferred(); let stops = 0;
  const page = setup(state(), async path => {
    if (path === "move") return move.promise;
    if (path === "stop") { stops++; return state({ operation: { id: "own", mine: true, status: "stopped" } }); }
    return state();
  });
  const command = page.role("move-fully_in").click(); await page.ui.hide();
  assert.equal(stops, 1);
  move.resolve(state({ operation: { id: "own", mine: true, status: "moving", target: "fully_in" } })); await command;
  assert.equal(stops, 2); assert.equal(page.ui._test.ownMotion(), null);
});

test("a stale poll cannot clear local motion ownership and STOP errors retain cancellation", async () => {
  const poll = deferred(); let holdPoll = false, stopFails = true;
  const page = setup(state(), async path => {
    if (path === "state" && holdPoll) return poll.promise;
    if (path === "move") return state({ operation: { id: "own", mine: true, status: "moving", target: "fully_out" } });
    if (path === "stop" && stopFails) throw new Error("Stop outcome unknown");
    return state();
  });
  await page.ui.show(); holdPoll = true; const polling = page.tick();
  await page.role("move-fully_out").click(); poll.resolve(state()); await polling;
  assert.equal(page.role("status").textContent, "Moving…");
  await page.listeners.blur(); assert.match(page.role("error-message").textContent, /Stop outcome unknown/);
  assert.ok(page.ui._test.ownMotion()); stopFails = false; await page.ui.hide(); assert.equal(page.ui._test.ownMotion(), null);
});

test("jog uses bounded steps and measures after completion", async () => {
  const page = setup(); await page.ui.show(); page.calls.length = 0;
  await page.role("jog-insert-ccw").click();
  assert.deepEqual(page.calls.map(call => call.slice(0, 2)), [["jog", { motor: "insert", direction: "ccw" }], ["measure", {}]]);
  assert.equal(page.calls.some(call => call[1]?.steps || call[1]?.pulses), false);
  await page.ui.hide();
});

test("pending STOP blocks new motion and an older STOP reply cannot replace the newer result", async () => {
  const first = deferred(); let stops = 0;
  const page = setup(state(), async path => {
    if (path === "stop") { stops++; return stops === 1 ? first.promise : state({ operation: { status: "stopped" } }); }
    return state();
  });
  const stopping = page.role("stop-fully_out").click();
  assert.equal(page.role("move-fully_out").disabled, true); assert.equal(page.role("stop-fully_in").disabled, false);
  await page.role("move-fully_out").click(); assert.equal(page.calls.some(call => call[0] === "move"), false);
  await page.role("stop-fully_in").click();
  first.resolve(state({ operation: { status: "moving", mine: false } })); await stopping;
  assert.equal(page.role("status").textContent, "Stopped");
  assert.equal(page.role("move-fully_out").disabled, false);
});

test("late measurement responses after hide do not overwrite the last visible state", async () => {
  const measured = deferred(); const page = setup(state(), async path => path === "measure" ? measured.promise : state());
  const showing = page.ui.show(); await flush(); await page.ui.hide();
  measured.resolve(state({ position: { x: 99, y: 99, insert: 99 } })); await showing;
  assert.equal(page.role("position-insert").textContent, "42.0000"); assert.equal(page.calls.some(call => call[0] === "stop"), false);
});


test("disconnected position source keeps the layout and profiles without reads or moves", async () => {
  const page = setup(state({ position_available: false, record_ready: false, endpoint_record_ready: false, stage_reference: { ready_targets: {}, targets_rotations: {} }, transfer_reference: { ready: false, source: null, targets_rotations: {}, message: "No position source connected." } }));
  await page.ui.show();
  assert.deepEqual(page.calls.map(call => call[0]), ["state"]);
  for (const role of ["measure", "record", "record-out_stage", "record-microscope", "move-fully_out", "move-fully_in", "move-out_stage", "move-microscope"]) assert.equal(page.role(role).disabled, true, role);
  for (const role of ["jog-insert-cw", "new", "save", "upload", "download-p1", "stop-fully_out", "stop-fully_in"]) assert.equal(page.role(role).disabled, false, role);
  for (const axis of ["x", "y", "insert"]) assert.equal(page.role("position-" + axis).textContent, "\u2014");
  assert.equal(walk(page.root).some(node => node.getAttribute("href") === "#twin"), false);
  assert.equal(page.root.textContent.includes("Position Finder"), false);
  assert.equal(page.role("status").textContent, "Waiting for stage encoders");
  await page.tick();
  assert.equal(page.calls.some(call => call[0] === "measure"), false);
  await page.ui.hide();
});


test("named position buttons send only a saved position name", async () => {
  const page = setup();
  await page.role("named-move-0").click();
  assert.deepEqual(page.calls[0].slice(0, 2), ["position/move", { name: "Loading" }]);
  assert.match(page.role("named-positions").textContent, /10.0000rot/);
});

test("legacy millimetre profiles remain visible but cannot command rotation moves", async () => {
  const old = { ...profile(), schema_version: 2, unit: undefined, positions: { Old: { position_mm: { x: 1, y: 2, insert: 3 } } } };
  const page = setup(state({ active_profile: old, draft: old, named_motion_ready: false }));
  assert.equal(page.role("named-move-0").disabled, true);
  assert.match(page.role("named-positions").textContent, /3.0000mm/);
  assert.match(page.role("selected-profile").textContent, /Legacy mm/);
  page.role("position-name").type("New");
  assert.equal(page.role("record").disabled, true);
  assert.equal(page.role("stop-fully_out").disabled, false);
  assert.equal(page.role("move-fully_in").disabled, false);
  assert.equal(page.role("record-microscope").disabled, true);
});
