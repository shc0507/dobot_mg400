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
const endpoint = insert => ({ position_mm: { x: 4, y: 8, insert }, captured_at: "2026-11-12T00:00:00Z", image_sha256: "a".repeat(64) });
const profile = (id = "p1", positions = { Loading: endpoint(10), Furnace: endpoint(130) }) => ({ format: "sample-transfer.calibration-profile", schema_version: 2, id, name: "Transfer " + id, positions });
const state = (overrides = {}) => ({ demo: false, has_control: true, position: { x: 4, y: 8, insert: 42 }, position_source: { camera: "camera1", profile_id: "optics", name: "Bench camera" }, draft: profile(), active_profile: profile(), profiles: [{ id: "p1", name: "Transfer p1", complete: true }], selected_profile: "p1", operation: { status: "idle", target: null, message: "", mine: false, id: null }, tolerance_mm: 0.5, transfer_reference: { ready: true, source: { name: "Bench camera", camera: "camera1" }, targets_mm: { fully_out: 0, fully_in: 150 } }, ...overrides });

function setup(initial = state(), handler) {
  let response = initial, language = "en", interval = null;
  const calls = [], notices = [], listeners = {}, languageListeners = [], revoked = [], downloads = [];
  const environment = {
    document: { hidden: false, addEventListener: (name, fn) => { listeners[name] = fn; } },
    addEventListener: (name, fn) => { listeners[name] = fn; },
    setInterval: fn => { interval = fn; return 1; }, clearInterval: () => { interval = null; }, setTimeout: fn => fn(),
    Blob, URL: { createObjectURL: value => { downloads.push(value); return "blob:profile"; }, revokeObjectURL: url => revoked.push(url) },
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

test("show measures without motion, while passive polls do not start new measurements", async () => {
  const page = setup(); await page.ui.show();
  assert.deepEqual(page.calls.map(call => call.slice(0, 2)), [["state"], ["measure", {}]]);
  assert.equal(page.role("position-insert").textContent, "42.00");
  await page.tick(); await page.tick();
  assert.equal(page.calls.filter(call => call[0] === "measure").length, 1);
  page.response(state({ position: { x: null, y: 9, insert: null } })); await page.tick();
  assert.equal(page.role("position-x").textContent, "—");
  assert.equal(page.role("position-insert").textContent, "—");
  await page.ui.hide(); assert.equal(page.calls.some(call => ["move", "jog", "stop"].includes(call[0])), false);
});

test("read-only access preserves coordinates and both STOP buttons need no control", async () => {
  const page = setup(state({ has_control: false }));
  for (const id of ["move-fully_out", "move-fully_in", "record", "new", "save", "jog-insert-cw"]) assert.equal(page.role(id).disabled, true, id);
  for (const id of ["stop-fully_out", "stop-fully_in"]) { assert.equal(page.role(id).disabled, false); await page.role(id).click(); }
  assert.deepEqual(page.calls.map(call => call.slice(0, 2)), [["stop", {}], ["stop", {}]]);
  const demo = setup(state({ demo: true, has_control: false }));
  assert.equal(demo.role("move-fully_out").disabled, false); assert.equal(demo.role("control").hidden, true);
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
  assert.match(page.role("recorded-0").textContent, /18.00mm/);
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
  await page.role("new").click(); assert.deepEqual(page.calls[0].slice(0, 2), ["profile/new", { name: "Sample positions" }]);
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

test("hybrid motion shows its phase and pulse scale without adding motion commands", () => {
  const operation = { id: "hybrid", status: "moving", target: "fully_out", target_mm: 0, microstep: 8, pulses_per_mm: 640, scale_source: "nominal", phase: "preparing", mine: true };
  const page = setup(state({ operation }));
  assert.equal(page.role("status").textContent, "Setting 1/8 stepping…");
  assert.equal(page.role("motion-scale").textContent, "1/8 step · 640.00 pulses/mm · nominal");
  assert.equal(page.role("motion-scale").hidden, false);
  for (const [phase, label] of [["measuring", "Reading position…"], ["learning", "Checking pulse scale…"], ["moving", "Moving…"], ["verifying", "Confirming position…"]]) {
    page.ui._test.apply(state({ operation: { ...operation, phase, pulses_per_mm: 638.5, scale_source: "measured" } }));
    assert.equal(page.role("status").textContent, label);
    assert.equal(page.role("motion-scale").textContent, "1/8 step · 638.50 pulses/mm");
  }
  assert.equal(page.role("stop-fully_out").disabled, false);
  assert.equal(page.role("move-fully_in").disabled, true);
  page.lang("zh");
  assert.equal(page.role("status").textContent, "确认位置…");
  page.ui._test.apply(state({ operation: { ...operation, phase: "future-phase", pulses_per_mm: null } }));
  assert.equal(page.role("status").textContent, "移动中…");
  assert.equal(page.role("motion-scale").hidden, true);
  page.ui._test.apply(state({ operation: { ...operation, target: null } }));
  assert.equal(page.role("motion-scale").hidden, true);
  assert.deepEqual(page.calls, []);
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

test("transfer uses Position Finder endpoints and the operation keeps its admitted target", () => {
  const page = setup(state({ draft: profile("draft", { Pickup: endpoint(88) }), active_profile: profile("saved", { Other: endpoint(99) }), operation: { status: "moving", target: "fully_out", target_mm: 5, mine: false } }));
  const targets = walk(page.root).filter(node => node.className === "st-action-target").map(node => node.textContent);
  assert.deepEqual(targets, ["Fully out · 0.00 mm", "Fully in · 150.00 mm"]);
  assert.match(page.role("distance").textContent, /37.00 mm/);
  assert.match(page.role("recorded-0").textContent, /88.00mm/);
  assert.equal(page.role("selector"), undefined);
  assert.equal(page.role("reference").textContent, "Bench camera · camera1");
});

test("transfer readiness is independent of the named-position profile library", async () => {
  const page = setup(state({ draft: profile("empty", {}), active_profile: null, profiles: [], selected_profile: null }));
  assert.equal(page.role("move-fully_out").disabled, false);
  assert.equal(page.role("move-fully_in").disabled, false);
  await page.role("move-fully_in").click();
  assert.deepEqual(page.calls.at(-1).slice(0, 2), ["move", { target: "fully_in" }]);
  page.ui._test.apply(state({ transfer_reference: { ready: false, source: null, targets_mm: {}, message: "Calibrate Insert in Position Finder." } }));
  assert.equal(page.role("move-fully_out").disabled, true);
  assert.equal(page.role("move-fully_in").disabled, true);
  assert.equal(page.role("help").textContent, "Calibrate Insert in Position Finder.");
  assert.equal(page.role("stop-fully_out").disabled, false);
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

test("calibration tabs preserve named position input and contain no endpoint recording", async () => {
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
  assert.equal(page.role("record-fully_out"), undefined);
  assert.equal(page.role("record-fully_in"), undefined);
  assert.equal(page.calls.length, 0);
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
  const page = setup(state(), async path => path === "move" ? state({ operation: { id: "own", mine: true, status: "moving", target: "fully_out", target_mm: 0, message: "Moving Insert" } }) : state());
  await page.role("move-fully_out").click();
  assert.deepEqual(page.calls[0].slice(0, 2), ["move", { target: "fully_out" }]);
  assert.match(page.role("distance").textContent, /42.00 mm/);
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
  assert.equal(page.role("position-insert").textContent, "42.00"); assert.equal(page.calls.some(call => call[0] === "stop"), false);
});
