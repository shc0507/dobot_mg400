import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const turret = require("../src/orchestrator/web/static/console/turret.js");

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.hidden = false; this.disabled = false; this.value = ""; this.ownText = ""; }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_) { throw new Error("Profile data must not be parsed as HTML."); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.ownText = ""; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  click() { if (!this.disabled) return this.listeners.click?.({ target: this }); }
  change(value) { if (!this.disabled) { this.value = value; return this.listeners.change?.({ target: this }); } }
  type(value) { this.value = value; this.listeners.input?.({ target: this }); }
  focus() {}
}

function el(tag, props = {}, ...children) {
  const node = new Element(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (["hidden", "disabled"].includes(key)) node[key] = Boolean(value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) if (child !== undefined && child !== null && child !== false) node.append(typeof child === "string" ? Object.assign(new Element("text"), { ownText: child }) : child);
  return node;
}
function walk(node) { return [node, ...node.children.flatMap(walk)]; }
function role(ui, name) { return [...walk(ui.element), ...walk(ui.profileSelector)].find(node => node.getAttribute("data-role") === "turret-" + name); }
const flush = () => new Promise(resolve => setImmediate(resolve));
const cap = () => ({ shape: [360, 640], objective_corners: [[40, 40], [48, 40], [48, 48], [40, 48]], preview_jpeg: "/9j/2w==" });
const profile = (captures = {}) => ({ schema_version: 2, profile_id: "a".repeat(32), name: "Bench turret", camera: "camera3", focus_position: 100, dictionary: "DICT_4X4_50", objective_side_mm: 8, captures });
const state = (overrides = {}) => ({ session: { active: false, mine: false }, draft: null, profiles: [], selected_profile: null, camera: { stream: "camera3", dimensions: [640, 360] }, ...overrides });

function setup(initial = state(), { owns = true, request } = {}) {
  let response = initial, lang = "en", interval = null, stops = 0;
  const calls = [], notices = [], revoked = [], languageListeners = [];
  const api = {
    el, lang: () => lang, owns: () => owns, notify: (...args) => notices.push(args), onLanguage: fn => languageListeners.push(fn), onState() {},
    request: async (...args) => { calls.push(args); return request ? request(...args) : response; },
  };
  const global = {
    setInterval: fn => { interval = fn; return 1; }, clearInterval: () => { interval = null; }, setTimeout: fn => fn(),
    Blob, URL: { createObjectURL: () => "blob:profile", revokeObjectURL: url => revoked.push(url) },
    LabCameras: { tile: name => { assert.equal(name, "camera3"); const node = el("div"); node.labCameras = { stop: () => { stops++; } }; return node; } },
  };
  const ui = turret.create(api, global);
  ui._test.applyState(initial);
  return { ui, calls, notices, revoked, setResponse: value => { response = value; }, interval: () => interval, stops: () => stops,
    lang: value => { lang = value; languageListeners.forEach(fn => fn()); } };
}

test("objective IDs and bilingual instructions agree", () => {
  assert.deepEqual(turret.logic.OBJECTIVES, ["5x", "10x", "20x", "50x"]);
  assert.deepEqual(Object.keys(turret.i18n.en).sort(), Object.keys(turret.i18n.zh).sort());
  assert.equal(turret.logic.MAX_UPLOAD, 2 * 1024 * 1024);
  const { ui, lang } = setup();
  assert.match(ui.element.textContent, /Start releases only the turret motor/);
  assert.match(ui.element.textContent, /current objective marker in focus and fully visible/);
  assert.match(ui.element.textContent, /Alignment uses recorded image coordinates/);
  assert.match(ui.element.textContent, /Retake all 4 objectives after moving Camera3 or changing camera focus or zoom/);
  assert.doesNotMatch(ui.element.textContent, /marker 35|fixed reference|perspective correction/i);
  lang("zh");
  assert.match(ui.element.textContent, /对焦轴不会移动/);
  assert.match(ui.element.textContent, /当前物镜标记必须清晰且完整可见/);
  assert.match(ui.element.textContent, /图像坐标/);
  assert.match(ui.element.textContent, /改变摄像头对焦、变焦后，重新记录全部 4 个物镜/);
  assert.doesNotMatch(ui.element.textContent, /35|固定参考|透视校正/);
});

test("Start sends only begin and requires microscope control except in demo", async () => {
  const denied = setup(state(), { owns: false });
  assert.equal(role(denied.ui, "start").disabled, true);
  const demo = setup(state({ demo: true }), { owns: false });
  role(demo.ui, "name").type("My turret");
  assert.equal(role(demo.ui, "start").disabled, false);
  await role(demo.ui, "start").click();
  assert.deepEqual(demo.calls.map(call => call.slice(0, 2)), [["turret/begin", { name: "My turret", new: true }]]);
});

test("recording each manual detent keeps progress and allows Retake", async () => {
  let value = state({ session: { active: true, mine: true }, draft: profile() });
  const { ui, calls } = setup(value, { request: async (path, data) => {
    assert.equal(path, "turret/record");
    value = { ...value, draft: { ...value.draft, captures: { ...value.draft.captures, [data.objective_id]: cap() } } };
    return value;
  } });
  ui.show();
  for (let id = 0; id < 4; id++) {
    if (id) await role(ui, "next").click();
    assert.match(role(ui, "record").textContent, new RegExp("Record " + turret.logic.OBJECTIVES[id]));
    await role(ui, "record").click();
    assert.equal(role(ui, "progress").textContent, `${id + 1} of 4 objectives recorded`);
    assert.match(role(ui, "record").textContent, /Retake/);
    assert.equal(role(ui, "still").getAttribute("src"), "data:image/jpeg;base64,/9j/2w==");
    assert.equal(role(ui, "finish").disabled, id !== 3);
  }
  await role(ui, "step-1").click();
  await role(ui, "record").click();
  assert.deepEqual(calls.filter(call => call[0] === "turret/record").map(call => call[1].objective_id), [0, 1, 2, 3, 1]);
  assert.equal(role(ui, "progress").textContent, "4 of 4 objectives recorded");
  ui.hide();
});

test("a rejected record preserves prior captures and leaves an inline error", async () => {
  const before = profile({ 0: cap() });
  const { ui } = setup(state({ session: { active: true, mine: true }, draft: before }), { request: async () => { throw new Error("Objective marker 0 is missing."); } });
  await role(ui, "record").click();
  assert.equal(ui._test.state().draft, before);
  assert.equal(role(ui, "progress").textContent, "1 of 4 objectives recorded");
  assert.match(role(ui, "error").textContent, /Objective marker 0 is missing/);
  assert.equal(role(ui, "error").hidden, false);
});

test("finish retains captures, then Save & use saves and selects without recorded focus", async () => {
  const complete = { ...profile(Object.fromEntries([0, 1, 2, 3].map(id => [id, cap()]))), focus_position: null };
  const catalog = [{ profile_id: complete.profile_id, name: complete.name, complete: true }];
  let value = state({ session: { active: true, mine: true }, draft: complete });
  const { ui, calls } = setup(value, { request: async (path, data) => {
    if (path === "turret/finish") value = { ...value, session: { active: false, mine: false } };
    if (path === "turret/save") value = { ...value, profiles: catalog, draft: { ...complete, name: data.name } };
    if (path === "turret/select") value = { ...value, selected_profile: data.id };
    return value;
  } });
  assert.equal(role(ui, "save").disabled, true);
  await role(ui, "finish").click();
  assert.equal(role(ui, "save").disabled, false);
  assert.equal(role(ui, "save").textContent, "Save & use");
  await role(ui, "save").click();
  assert.deepEqual(calls.map(call => call.slice(0, 2)), [["turret/finish", {}], ["turret/save", { name: "Bench turret" }], ["turret/select", { id: complete.profile_id }]]);
  assert.equal(role(ui, "select-" + complete.profile_id).textContent, "Selected");
  assert.equal(role(ui, "profile-select").value, complete.profile_id);
  assert.equal(role(ui, "draft-state").textContent, "Saved");
});

test("partial Save profile saves without selecting or moving a lens", async () => {
  const draft = profile({ 0: cap() });
  const { ui, calls, notices } = setup(state({ draft }), { request: async () => state({ draft, profiles: [{ ...draft, complete: false }] }) });
  assert.equal(role(ui, "save").textContent, "Save profile");
  assert.equal(await role(ui, "save").click(), true);
  assert.deepEqual(calls.map(call => call.slice(0, 2)), [["turret/save", { name: draft.name }]]);
  assert.deepEqual(notices, [["Calibration profile saved.", "info"]]);
});

test("Save & use retains the saved profile and prior selection when activation fails", async () => {
  const draft = profile(Object.fromEntries([0, 1, 2, 3].map(id => [id, cap()])));
  const old = { profile_id: "b".repeat(32), name: "Old profile", complete: true };
  const saved = state({ draft, profiles: [old, { ...draft, complete: true }], selected_profile: old.profile_id });
  let finishSave, finishSelect;
  const { ui, calls, notices } = setup(state({ draft, profiles: [old], selected_profile: old.profile_id }), {
    request: path => path === "turret/save" ? new Promise(resolve => { finishSave = resolve; }) : new Promise((_, reject) => { finishSelect = reject; }),
  });
  const pending = role(ui, "save").click();
  assert.equal(role(ui, "profile-select").disabled, true);
  assert.equal(calls.length, 1);
  finishSave(saved);
  await flush();
  assert.equal(role(ui, "profile-select").disabled, true);
  assert.deepEqual(calls[1].slice(0, 2), ["turret/select", { id: draft.profile_id }]);
  finishSelect(new Error("Microscope is busy."));
  assert.equal(await pending, false);
  assert.equal(ui._test.state().profiles.length, 2);
  assert.equal(role(ui, "draft-state").textContent, "Saved");
  assert.equal(role(ui, "profile-select").value, old.profile_id);
  assert.equal(role(ui, "profile-select").disabled, false);
  assert.match(role(ui, "error").textContent, /Profile saved, but not selected: Microscope is busy/);
  assert.match(role(ui, "profile-error").textContent, /Profile saved, but not selected/);
  assert.deepEqual(notices, []);
});

test("Save & use does not select when saving fails", async () => {
  const draft = profile(Object.fromEntries([0, 1, 2, 3].map(id => [id, cap()])));
  const { ui, calls, notices } = setup(state({ draft }), { request: async () => { throw new Error("Could not save profile."); } });
  assert.equal(await role(ui, "save").click(), false);
  assert.deepEqual(calls.map(call => call.slice(0, 2)), [["turret/save", { name: draft.name }]]);
  assert.equal(ui._test.state().draft, draft);
  assert.match(role(ui, "error").textContent, /Could not save profile/);
  assert.doesNotMatch(role(ui, "error").textContent, /saved, but not selected/);
  assert.deepEqual(notices, []);
});

test("lens calibration lists complete profiles and follows selected poll metadata", async () => {
  const first = { profile_id: "a".repeat(32), name: "Bench turret", complete: true };
  const second = { profile_id: "b".repeat(32), name: "Other turret", complete: true };
  const partial = { profile_id: "c".repeat(32), name: "Partial", complete: false };
  let value = state({ profiles: [first, second, partial], selected_profile: first.profile_id });
  const { ui, calls, lang } = setup(value, { request: async (path, data) => {
    assert.equal(path, "turret/select");
    return value = { ...value, selected_profile: data.id };
  } });
  assert.equal(role(ui, "profile-select").getAttribute("aria-label"), "Lens calibration");
  assert.deepEqual(role(ui, "profile-select").children.map(option => option.textContent), ["Bench turret", "Other turret"]);
  assert.equal(role(ui, "profile-select").value, first.profile_id);
  await role(ui, "profile-select").change(second.profile_id);
  assert.deepEqual(calls.map(call => call.slice(0, 2)), [["turret/select", { id: second.profile_id }]]);
  assert.equal(role(ui, "profile-select").value, second.profile_id);
  ui._test.applyState({ ...value, selected_profile: first.profile_id });
  assert.equal(role(ui, "profile-select").value, first.profile_id);
  lang("zh");
  assert.equal(role(ui, "profile-select").getAttribute("aria-label"), "物镜校准");
  assert.equal(role(ui, "profile-select").value, first.profile_id);
  assert.equal(calls.length, 1);
});

test("lens calibration restores the prior selection on rejection and ignores unconfirmed selection", async () => {
  const first = { profile_id: "a".repeat(32), name: "First", complete: true };
  const second = { profile_id: "b".repeat(32), name: "Second", complete: true };
  const before = state({ profiles: [first, second], selected_profile: first.profile_id });
  for (const request of [async () => { throw new Error("Selection refused."); }, async () => before]) {
    const { ui } = setup(before, { request });
    assert.equal(await role(ui, "profile-select").change(second.profile_id), false);
    assert.equal(role(ui, "profile-select").value, first.profile_id);
    assert.equal(ui._test.state().selected_profile, first.profile_id);
    assert.equal(role(ui, "profile-error").hidden, false);
  }
});

test("lens calibration requires control and waits for requests, motion and sessions", async () => {
  const item = { profile_id: "a".repeat(32), name: "Profile", complete: true };
  const value = state({ profiles: [item] });
  const denied = setup(value, { owns: false });
  assert.equal(role(denied.ui, "profile-select").disabled, true);
  assert.equal(role(denied.ui, "select-" + item.profile_id).disabled, true);
  const demo = setup({ ...value, demo: true }, { owns: false });
  assert.equal(role(demo.ui, "profile-select").disabled, false);
  let finish;
  const { ui } = setup(value, { request: () => new Promise(resolve => { finish = resolve; }) });
  assert.equal(role(ui, "profile-select").children[0].textContent, "No profile selected");
  ui.setMicroscopeBusy(true);
  assert.equal(role(ui, "profile-select").disabled, true);
  ui.setMicroscopeBusy(false);
  const pending = role(ui, "profile-select").change(item.profile_id);
  assert.equal(role(ui, "profile-select").disabled, true);
  finish({ ...value, selected_profile: item.profile_id });
  await pending;
  assert.equal(role(ui, "profile-select").disabled, false);
  ui._test.applyState({ ...value, session: { active: true, mine: true } });
  assert.equal(role(ui, "profile-select").disabled, true);
});

test("a missing selected profile is shown as unavailable without a fallback selection", () => {
  const id = "a".repeat(32);
  const { ui, calls } = setup(state({ selected_profile: id }));
  assert.equal(role(ui, "profile-select").value, id);
  assert.equal(role(ui, "profile-select").children[0].textContent, "Selected profile unavailable");
  assert.equal(role(ui, "profile-select").children[0].disabled, true);
  assert.deepEqual(calls, []);
});

test("resuming a draft preserves captures; New profile starts a fresh draft", async () => {
  const { ui, calls } = setup(state({ draft: profile({ 0: cap() }) }));
  await role(ui, "start").click();
  assert.deepEqual(calls[0].slice(0, 2), ["turret/begin", {}]);
  await role(ui, "new").click();
  assert.equal(role(ui, "progress").textContent, "0 of 4 objectives recorded");
  role(ui, "name").type("New turret");
  await role(ui, "start").click();
  assert.deepEqual(calls[1].slice(0, 2), ["turret/begin", { name: "New turret", new: true }]);
});

test("upload rejects oversized, malformed and invalid profiles without clearing captures", async () => {
  const before = profile({ 0: cap() });
  const { ui, calls } = setup(state({ draft: before }), { request: async () => { throw new Error("Unsupported schema_version."); } });
  const file = (contents, size = contents.length) => ({ size, text: async () => contents });
  const upload = item => ui._test.upload({ target: { files: [item] } });
  await upload(file("{}", 2 * 1024 * 1024 + 1));
  assert.match(role(ui, "error").textContent, /2 MiB/);
  await upload(file("broken"));
  assert.match(role(ui, "error").textContent, /valid JSON/);
  assert.equal(calls.length, 0);
  await upload(file('{"schema_version":999}'));
  assert.match(role(ui, "error").textContent, /Unsupported schema_version/);
  assert.equal(ui._test.state().draft, before);
  assert.equal(role(ui, "progress").textContent, "1 of 4 objectives recorded");
});

test("profile names are text, and actions use the saved profile ID", async () => {
  const id = "b".repeat(32), name = '<img src=x onerror="alert(1)">';
  const { ui, calls, revoked } = setup(state({ profiles: [{ profile_id: id, name, complete: true }] }), {
    request: async path => path.startsWith("turret/profile/") ? profile() : state(),
  });
  assert.match(ui.element.textContent, /<img src=x/);
  await role(ui, "download-" + id).click();
  assert.equal(calls[0][0], "turret/profile/" + id);
  assert.deepEqual(revoked, ["blob:profile"]);
  await role(ui, "select-" + id).click();
  assert.deepEqual(calls[1].slice(0, 2), ["turret/select", { id }]);
});

test("another client's session disables mutations", () => {
  const id = "c".repeat(32);
  const { ui } = setup(state({ session: { active: true, mine: false }, draft: profile(), profiles: [{ profile_id: id, name: "Profile", complete: true }] }));
  assert.equal(role(ui, "record").hidden, true);
  assert.equal(role(ui, "new").disabled, true);
  assert.equal(role(ui, "upload").disabled, true);
  assert.equal(role(ui, "select-" + id).disabled, true);
  assert.equal(role(ui, "delete-" + id).disabled, true);
  assert.match(role(ui, "session-note").textContent, /Another client/);
});

test("an incomplete saved profile loads a draft without selecting it", async () => {
  const id = "d".repeat(32);
  const { ui, calls } = setup(state({ profiles: [{ profile_id: id, name: "Partial", complete: false }] }));
  assert.equal(role(ui, "select-" + id).textContent, "Load draft");
  await role(ui, "select-" + id).click();
  assert.deepEqual(calls[0].slice(0, 2), ["turret/load", { id }]);
});

test("a saved profile is labeled Saved before it is selected", () => {
  const draft = profile({ 0: cap() });
  const { ui } = setup(state({ draft, profiles: [{ profile_id: draft.profile_id, name: draft.name, complete: false }] }));
  assert.equal(role(ui, "draft-state").textContent, "Saved");
  assert.equal(role(ui, "select-" + draft.profile_id).textContent, "Load draft");
});

test("focus guidance recommends clearance while all captures enable Save & use", () => {
  const draft = profile(Object.fromEntries([0, 1, 2, 3].map(id => [id, cap()])));
  const { ui, lang } = setup(state({ draft, switch_focus: 800, compatible_for_switch: false }));
  assert.equal(role(ui, "focus-summary").textContent, "Clearance: 800 full steps · Recorded: 100 full steps");
  assert.equal(role(ui, "switch-warning").hidden, false);
  assert.equal(role(ui, "switch-warning").textContent, "We recommend recording at clearance 800 full steps.");
  assert.match(ui.element.textContent, /Before Start, we recommend moving focus manually to clearance/);
  ui._test.applyState(state({ draft: { ...draft, focus_position: null }, switch_focus: 800, compatible_for_switch: false }));
  assert.match(role(ui, "focus-summary").textContent, /Recorded: \(focus not zeroed\)/);
  assert.equal(role(ui, "save").textContent, "Save & use");
  assert.equal(role(ui, "save").disabled, false);
  lang("zh");
  assert.match(role(ui, "focus-summary").textContent, /记录位置：（对焦未归零）/);
  ui._test.applyState(state({ draft: { ...draft, focus_position: 800 }, switch_focus: 800, compatible_for_switch: true }));
  assert.equal(role(ui, "switch-warning").hidden, false);
});

test("selection errors remain visible until backend state clears them", () => {
  const { ui } = setup(state({ selection_error: "The selected profile is missing." }));
  assert.equal(role(ui, "selection-error").hidden, false);
  assert.equal(role(ui, "selection-error").textContent, "The selected profile is missing.");
  assert.equal(role(ui, "profile-error").textContent, "The selected profile is missing.");
  assert.equal(role(ui, "profile-select").children[0].textContent, "Selected profile unavailable");
  ui._test.applyState(state({ selection_error: null }));
  assert.equal(role(ui, "selection-error").hidden, true);
});

test("hide stops polling, camera streaming and late refresh updates", async () => {
  let resolve;
  const before = state({ draft: profile({ 0: cap() }) });
  const { ui, calls, interval, stops } = setup(before, { request: () => new Promise(done => { resolve = done; }) });
  ui.show();
  assert.equal(typeof interval(), "function");
  ui.hide();
  assert.equal(interval(), null);
  assert.ok(stops() > 0);
  resolve(state());
  await flush();
  await ui.refresh();
  assert.equal(calls.length, 1);
  assert.equal(ui._test.state().draft, before.draft);
});

test("demo live image clears its source when the workspace hides", async () => {
  const value = state({ demo: true, camera: { frame_url: "/lab/api/console/ws/process/turret/frame" } });
  const { ui } = setup(value);
  ui.show();
  await flush();
  const image = walk(ui.element).find(node => node.tagName === "img" && node.getAttribute("alt") === "Camera3");
  assert.match(image.getAttribute("src"), /\/turret\/frame\?t=/);
  ui.hide();
  assert.equal(image.getAttribute("src"), null);
});
