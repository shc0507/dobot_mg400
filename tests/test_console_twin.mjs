import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import test from "node:test";

const require = createRequire(import.meta.url);
const { workspace, positionMonitor, logic, i18n } = require("../src/orchestrator/web/static/console/twin.js");
const FIT = { min: 0, max: 45 };
const PROFILE = {
  id: "draft-one", name: "Stage overhead", camera: { device_id: "camera1", dimensions: [640, 480] },
  travel_mm: { x: 45, y: 40, insert: 150 }, endpoints: {},
  axes: { x: FIT, y: { min: 0, max: 40 }, insert: { min: 0, max: 150 } },
  marker_groups: { desk: [10, 11, 12], y: [14, 15, 16], x: [18, 19, 20], insert: [22] },
};
const STAMP = "2026-11-09T12:34:56Z";
const IMAGE = "data:image/jpeg;base64,AAAA";
const SAMPLE = {
  captured_at: STAMP, camera: "camera1", image: IMAGE, dimensions: [640, 480], processing_ms: 22,
  detections: [{ id: 22, corners_px: [[10, 10], [20, 10], [20, 20], [10, 20]], center_px: [15, 15] }],
  axes: {
    x: { value_mm: 15, status: "measured", tracks: [{ moving_id: 18, reference_id: 14, estimate_mm: 15, off_track_px: 0.3 }] },
    y: { value_mm: 20, status: "measured" }, insert: { value_mm: 75, status: "measured" },
  },
};
const clone = value => structuredClone(value);
const state = () => ({ cameras: [{ id: "camera1", label: "Stage overhead" }, { id: "camera2", label: "Robot" }], camera: "camera1", profiles: [], selected_profile: null, active_profile: clone(PROFILE), calibration: { profile: clone(PROFILE), endpoint_status: {}, axis_status: {} } });

test("held readings preserve each axis measurement time while readable axes update", () => {
  const first = logic.readings({}, SAMPLE.axes, PROFILE, STAMP);
  const nextStamp = "2026-11-09T12:35:01Z";
  const second = logic.readings(first.cache, { x: { value_mm: 16 }, y: { value_mm: 21 }, insert: { reason: "no_calibrated_pair_visible" } }, PROFILE, nextStamp);
  assert.equal(second.display.x.recordedAt, nextStamp);
  assert.equal(second.display.insert.recordedAt, STAMP);
  assert.equal(second.display.insert.held, true);
  assert.equal(second.cache.insert.held, false);
  const third = logic.readings(second.cache, {}, PROFILE, "2026-11-09T12:36:01Z");
  assert.equal(third.display.insert.recordedAt, STAMP);
  assert.equal(third.display.x.recordedAt, nextStamp);
});

test("missing calibration and other invalid results cannot reuse an old coordinate", () => {
  const first = logic.readings({}, SAMPLE.axes, PROFILE, STAMP);
  assert.equal(logic.readings(first.cache, { x: { reason: "calibration_missing" } }, PROFILE, STAMP).display.x, undefined);
  assert.equal(logic.readings(first.cache, {}, { axes: {} }, STAMP).display.insert, undefined);
  assert.equal(logic.readings(first.cache, { insert: { reason: "size_mismatch" } }, PROFILE, STAMP).display.insert, undefined);
  assert.equal(logic.readings({}, { x: { value_mm: Number.NaN } }, PROFILE, STAMP).display.x, undefined);
});

test("travel maps hide outside positions and preserve the Cartesian direction", () => {
  assert.deepEqual(logic.mapPosition({ x: { value_mm: 11.25 }, y: { value_mm: 30, held: true } }, PROFILE), { x: 0.25, y: 0.75, held: true });
  assert.equal(logic.mapPosition({ x: { value_mm: -1 }, y: { value_mm: 20 } }, PROFILE), null);
  assert.equal(logic.fraction(150, PROFILE.axes.insert), 1);
  assert.equal(logic.fraction(151, PROFILE.axes.insert), null);
  assert.equal(logic.fraction(0, { min: 0, max: 0 }), null);
  assert.equal(logic.mapPosition({ x: { value_mm: 1 } }, PROFILE), null);
});

test("travel validation, rates, and sampling deadlines reject invalid values", () => {
  assert.deepEqual(logic.travel({ x: "45", y: "40.5", insert: "150" }), { x: 45, y: 40.5, insert: 150 });
  for (const bad of ["", " ", "0", "-1", "Infinity", "NaN", "1000001"]) assert.equal(logic.travel({ x: bad, y: 40, insert: 150 }), null);
  assert.equal(logic.effectiveRate([1000, 2000, 3000]), 1);
  assert.equal(logic.effectiveRate([1000]), null);
  assert.equal(logic.nextDue(1000, 3600, 1), 4000);
  assert.equal(logic.nextDue(1000, 1100, 5), 1200);
});

test("overlay data accepts finite corners and local image data only", () => {
  assert.equal(logic.detectionGeometry(SAMPLE.detections[0]).id, 22);
  assert.equal(logic.detectionGeometry({ ...SAMPLE.detections[0], corners_px: [[0, Number.NaN]] }), null);
  assert.equal(logic.safeImage(IMAGE), IMAGE);
  for (const source of ["https://example.com/frame.jpg", "javascript:alert(1)", "data:image/svg+xml;base64,AAAA", null]) assert.equal(logic.safeImage(source), null);
  assert.deepEqual(logic.preferences({ getItem: () => '{"automatic":false,"maxFrames":8}' }), { automatic: false, maxFrames: 8 });
  assert.deepEqual(logic.preferences({ getItem: () => '{"maxFrames":99}' }), { automatic: true, maxFrames: 4 });
});

class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.attributes = {}; this.listeners = {}; this.style = {}; this.hidden = false; this.disabled = false; this.value = ""; this.ownText = ""; }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("Do not interpret profile data as HTML."); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ""; this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  remove() {}
  addEventListener(name, action) { this.listeners[name] = action; }
  click() { if (!this.disabled) this.listeners.click?.({ target: this }); }
  change(value) { this.value = String(value); this.listeners.change?.({ target: this }); }
  selectFile(file) { this.files = file ? [file] : []; this.change(file ? "calibration.json" : ""); }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
}
function el(tag, props, ...children) {
  const node = new Element(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (["hidden", "disabled"].includes(key)) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    if (typeof child === "string") { const text = new Element("#text"); text.textContent = child; node.append(text); } else node.append(child);
  }
  return node;
}
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };
async function mounted(t, handlers = {}, compact = false) {
  const timers = new Map(); let timerId = 0, current = state();
  t.mock.method(globalThis, "setTimeout", (action, delay) => { timers.set(++timerId, { action, delay }); return timerId; });
  t.mock.method(globalThis, "clearTimeout", id => timers.delete(id));
  const previousDocument = globalThis.document;
  const documentEvents = {};
  globalThis.document = { hidden: false, createElementNS: (_ns, tag) => new Element(tag), addEventListener(name, fn) { documentEvents[name] = fn; }, body: new Element("body") };
  const calls = [], messages = [], frameRequests = [], rootNode = new Element("section");
  const api = {
    base: "/lab", el(tag, props, ...children) {
      const node = el(tag, props, ...children), setAttribute = node.setAttribute.bind(node);
      node.setAttribute = (name, value) => {
        if (tag === "img" && name === "src" && value.includes("/frame?")) frameRequests.push(value);
        setAttribute(name, value);
      };
      return node;
    }, lang: () => "en", onLanguage() {}, notify: (...args) => messages.push(args),
    t: (key, vars = {}) => String(i18n.en[key] ?? key).replace(/\{(\w+)\}/g, (_, name) => vars[name] ?? ""),
    async request(path, body, options) {
      calls.push({ path, body, options });
      if (handlers[path]) return handlers[path](body, clone(current));
      if (path === "state") return clone(current);
      if (path === "measure") return clone(SAMPLE);
      if (path === "source") { current.camera = body.camera; current.calibration.profile.axes = {}; return clone(current); }
      if (path === "profile/edit" || path === "profile/save") { Object.assign(current.calibration.profile, body); return clone(current); }
      if (path === "capture") return { state: clone(current), sample: clone(SAMPLE), tuning: { missing: [] }, accepted: true };
      return clone(current);
    },
    relay() { throw new Error("Digital Twin must not issue motor commands."); },
  };
  api.consoleRequest = (path, body, options) => {
    assert.ok(path.startsWith("/api/console/ws/twin/"));
    return api.request(path.slice("/api/console/ws/twin/".length), body, options);
  };
  const ws = compact ? positionMonitor() : workspace(); ws.mount(rootNode, api); ws.show(); await settle();
  t.after(() => { ws.hide(); globalThis.document = previousDocument; });
  const find = name => rootNode.all().find(node => node.getAttribute("aria-label") === name || (node.tagName === "BUTTON" && node.textContent === name));
  const runTimer = async delay => { const entry = [...timers].find(([, timer]) => timer.delay === delay); assert.ok(entry); timers.delete(entry[0]); entry[1].action(); await settle(); };
  return { ws, api, rootNode, calls, messages, frameRequests, find, timers, documentEvents, runTimer, async runSample() { find("Start sampling")?.click(); await runTimer(0); } };
}

test("Motion position card samples the saved calibration without calibration controls or preview requests", async t => {
  const page = await mounted(t, {}, true);
  assert.equal(page.find("Camera"), undefined);
  assert.equal(page.find("Saved profile"), undefined);
  assert.equal(page.find("Calibration"), undefined);
  assert.match(page.rootNode.textContent, /Stage overhead/);
  page.find("Target rate").change(2);
  await page.runSample();
  assert.equal(page.ws._state().display.x.value_mm, 15);
  assert.equal(page.ws._state().display.insert.value_mm, 75);
  assert.ok(page.rootNode.all().some(node => node.className === "tw-point" && node.style.left === "50%"));
  assert.ok([...page.timers.values()].some(timer => timer.delay > 0 && timer.delay <= 500));
  assert.deepEqual(page.frameRequests, []);
  page.find("Pause sampling").click(); await settle();
  assert.match(page.rootNode.textContent, /Last recorded/);
  assert.equal(page.ws._state().display.x.held, true);
  assert.equal(page.timers.size, 0);
});

test("Motion reloads the selected profile on return and disables sampling after failed refresh", async t => {
  let current = state(), fail = false;
  const page = await mounted(t, { state: () => { if (fail) throw new Error("Camera unavailable"); return clone(current); } }, true);
  await page.runSample(); page.ws.hide();
  current.active_profile = null;
  page.ws.show(); await settle();
  assert.deepEqual(page.ws._state().display, {});
  assert.equal(page.find("Start sampling").disabled, true);
  page.ws.hide(); current = state(); fail = true;
  page.ws.show(); await settle();
  assert.equal(page.find("Start sampling").disabled, true);
  assert.match(page.rootNode.textContent, /Camera unavailable/);
});

test("switching from Position Monitor to Motion waits for the pending sample and discards its result", async t => {
  let resolve, measurements = 0;
  const page = await mounted(t, { measure: () => ++measurements === 1 ? new Promise(done => { resolve = done; }) : clone(SAMPLE) });
  await page.runSample(); page.ws.hide();
  const compact = positionMonitor(), node = new Element("div");
  compact.mount(node, page.api); compact.show(); await settle();
  t.after(() => compact.hide());
  node.all().find(item => item.tagName === "BUTTON" && item.textContent === "Start sampling").click();
  await page.runTimer(0);
  assert.equal(measurements, 1);
  resolve(clone(SAMPLE)); await settle();
  assert.equal(measurements, 2);
  assert.equal(page.ws._state().latest, null);
  assert.equal(compact._state().display.x.value_mm, 15);
  compact.hide();
  assert.equal(page.timers.size, 0);
});

test("the workspace uses Console cameras and exposes all six calibration endpoints", async t => {
  const page = await mounted(t);
  assert.equal(page.find("Camera").value, "camera1");
  assert.equal(page.find("Saved profile").tagName, "SELECT");
  assert.equal(page.find("Profile JSON file").getAttribute("accept"), ".json,application/json");
  page.find("Calibration").click();
  for (const label of ["X low · Left", "X high · Right", "Y low · Down", "Y high · Up", "Insert out", "Insert in"]) assert.ok(page.find("Snap " + label));
  page.find("Snap X low · Left").click(); await settle();
  assert.deepEqual(page.calls.find(call => call.path === "capture").body, { endpoint: "x_low", automatic: true, max_frames: 8 });
});

test("measurement publishes the same image, outlines, coordinates and log", async t => {
  const page = await mounted(t);
  await page.runSample();
  assert.equal(page.ws._state().display.x.value_mm, 15);
  assert.equal(page.ws._state().log.length, 1);
  assert.ok(page.rootNode.all().some(node => node.tagName === "IMG" && node.getAttribute("src") === IMAGE));
  assert.ok(page.rootNode.all().some(node => node.tagName === "POLYGON" && node.getAttribute("points") === "10,10 20,10 20,20 10,20"));
  assert.deepEqual(page.calls.find(call => call.path === "measure").body, { automatic: true, max_frames: 4, tune: false });
});

test("a source change rejects an in-flight frame and clears previous readouts", async t => {
  let resolve;
  const page = await mounted(t, { measure: () => new Promise(done => { resolve = done; }) });
  page.find("Tune now").click(); await settle();
  page.find("Camera").change("camera2"); await settle();
  resolve(clone(SAMPLE)); await settle();
  assert.equal(page.ws._state().state.camera, "camera2");
  assert.equal(page.ws._state().latest, null);
  assert.deepEqual(page.ws._state().display, {});
  assert.equal(page.ws._state().log.length, 0);
});

test("deletion from another page clears stale samples and refreshes the active profile once", async t => {
  let stateReads = 0, measurements = 0, finishRefresh, now = 1000;
  t.mock.method(Date, "now", () => now);
  const initial = state(); initial.selected_profile = PROFILE.id;
  initial.profiles = [{ id: PROFILE.id, name: PROFILE.name }];
  const deleted = clone(initial); deleted.selected_profile = null; deleted.profiles = [];
  deleted.active_profile = { ...clone(PROFILE), id: "blank-after-deletion", axes: {} };
  const page = await mounted(t, {
    state: () => stateReads++ === 0 ? clone(initial) : new Promise(resolve => { finishRefresh = resolve; }),
    measure: () => ({ ...clone(SAMPLE), profile_id: measurements++ === 0 ? PROFILE.id : deleted.active_profile.id }),
  });
  await page.runSample();
  assert.equal(page.ws._state().display.x.value_mm, 15);
  now = 2000; await page.runTimer(1000);
  assert.equal(page.ws._state().latest, null);
  assert.deepEqual(page.ws._state().display, {});
  assert.equal(page.ws._state().log.length, 0);
  assert.equal(page.ws._state().running, false);
  assert.equal(page.calls.filter(call => call.path === "state").length, 2);
  finishRefresh(clone(deleted)); await settle();
  assert.equal(page.find("Saved profile").value, "");
  assert.equal(page.find("Profile name").value, PROFILE.name);
  assert.equal(page.ws._state().state.active_profile.id, deleted.active_profile.id);
  assert.equal(page.find("Tune now").disabled, true);
  assert.ok([...page.timers.values()].every(timer => timer.delay !== 0));
  await page.runSample();
  assert.equal(page.ws._state().latest.profile_id, deleted.active_profile.id);
  assert.deepEqual(page.ws._state().display, {});
  assert.equal(page.calls.filter(call => call.path === "state").length, 2);
});

test("a failed profile refresh clears stale samples and stops automatic retries", async t => {
  let stateReads = 0, measurements = 0, now = 1000;
  t.mock.method(Date, "now", () => now);
  const page = await mounted(t, {
    state: () => { if (stateReads++) throw new Error("Profile state unavailable"); return state(); },
    measure: () => ({ ...clone(SAMPLE), profile_id: measurements++ === 0 ? PROFILE.id : "changed-profile" }),
  });
  await page.runSample();
  now = 2000; await page.runTimer(1000);
  assert.equal(page.ws._state().latest, null);
  assert.deepEqual(page.ws._state().display, {});
  assert.equal(page.ws._state().running, false);
  assert.ok(page.rootNode.textContent.includes("Profile state unavailable"));
  assert.equal(page.calls.filter(call => call.path === "state").length, 2);
  assert.equal(page.calls.filter(call => call.path === "measure").length, 2);
  assert.ok([...page.timers.values()].every(timer => timer.delay !== 0));
});

test("failed measurements hold old values without adding a sample log row", async t => {
  let count = 0;
  const page = await mounted(t, { measure: () => { if (count++) throw new Error("Camera unavailable"); return clone(SAMPLE); } });
  page.find("Tune now").click(); await settle();
  page.find("Tune now").click(); await settle();
  assert.equal(page.ws._state().display.insert.held, true);
  assert.equal(page.ws._state().display.insert.recordedAt, STAMP);
  assert.equal(page.ws._state().log.length, 1);
  assert.ok(page.rootNode.textContent.includes("Camera unavailable"));
});

test("tuning while paused uses the selected rate without extra previews until its search ends", async t => {
  let now = 1000, measured = 0;
  t.mock.method(Date, "now", () => now);
  const page = await mounted(t, { measure: () => ({ ...clone(SAMPLE), tuning: { searching: ++measured < 3, tested: measured, max_frames: 4, active_preset: 2 } }) });
  page.find("Target rate").change("0.5");
  page.find("Tune now").click(); await settle();
  assert.equal(page.ws._state().running, false);
  assert.equal(page.calls.find(call => call.path === "measure").body.tune, true);
  assert.deepEqual([...page.timers.values()].map(timer => timer.delay), [2000]);
  assert.equal(page.frameRequests.length, 1);
  assert.ok(page.rootNode.textContent.includes("Searching · 1/4 frames"));
  now = 1500; page.find("Target rate").change("1");
  assert.deepEqual([...page.timers.values()].map(timer => timer.delay), [500]);
  now = 2000; await page.runTimer(500);
  assert.equal(measured, 2);
  assert.equal(page.frameRequests.length, 1);
  assert.deepEqual([...page.timers.values()].map(timer => timer.delay), [1000]);
  now = 3000; await page.runTimer(1000);
  assert.equal(measured, 3);
  assert.equal(page.frameRequests.length, 2);
  assert.equal(page.timers.size, 0);
  assert.equal(page.rootNode.all().find(node => node.className === "tw-inset").hidden, false);
  page.ws.hide();
  assert.equal(page.timers.size, 0);
  assert.ok(page.rootNode.all().filter(node => node.tagName === "IMG").every(node => !node.getAttribute("src")?.includes("/frame?")));
});

test("sampling fetches only scheduled measurements and restores paused and calibration previews", async t => {
  let now = 1000;
  t.mock.method(Date, "now", () => now);
  const page = await mounted(t);
  const preview = page.rootNode.all().find(node => node.tagName === "IMG" && node.getAttribute("src")?.includes("/frame?"));
  const loaded = preview.onload;
  preview.onload();
  assert.equal(page.frameRequests.length, 1);
  page.find("Start sampling").click();
  loaded();
  assert.equal(preview.getAttribute("src"), null);
  assert.equal(page.rootNode.all().find(node => node.className === "tw-inset").hidden, true);
  assert.deepEqual([...page.timers.values()].map(timer => timer.delay), [0]);
  await page.runTimer(0);
  assert.equal(page.calls.filter(call => call.path === "measure").length, 1);
  assert.equal(page.frameRequests.length, 1);
  assert.equal(page.find("Tune now").disabled, true);
  page.find("Tune now").click(); await settle();
  assert.equal(page.calls.filter(call => call.path === "measure").length, 1);
  assert.deepEqual([...page.timers.values()].map(timer => timer.delay), [1000]);
  now = 2000; await page.runTimer(1000);
  assert.equal(page.calls.filter(call => call.path === "measure").length, 2);
  assert.equal(page.frameRequests.length, 1);
  page.find("Pause sampling").click();
  assert.equal(page.timers.size, 0);
  assert.equal(page.frameRequests.length, 2);
  assert.equal(page.rootNode.all().find(node => node.className === "tw-inset").hidden, false);
  page.find("Calibration").click();
  assert.equal(page.frameRequests.length, 3);
  assert.ok(page.rootNode.all().some(node => node.tagName === "IMG" && !node.hidden && node.getAttribute("src")?.includes("/frame?")));
});

test("rate changes keep the acquisition deadline and slow samples never overlap", async t => {
  let now = 1000, finish;
  t.mock.method(Date, "now", () => now);
  const page = await mounted(t, { measure: () => new Promise(resolve => { finish = resolve; }) });
  await page.runSample();
  assert.equal(page.calls.filter(call => call.path === "measure").length, 1);
  assert.equal(page.timers.size, 0);
  now = 1100; page.find("Target rate").change("5");
  assert.equal(page.timers.size, 0);
  now = 1750; finish(clone(SAMPLE)); await settle();
  assert.deepEqual([...page.timers.values()].map(timer => timer.delay), [50]);
  assert.equal(page.frameRequests.length, 1);
  now = 1775; page.find("Target rate").change("0.5");
  assert.deepEqual([...page.timers.values()].map(timer => timer.delay), [1225]);
  assert.equal(page.calls.filter(call => call.path === "measure").length, 1);
  now = 3000; await page.runTimer(1225);
  assert.equal(page.calls.filter(call => call.path === "measure").length, 2);
  assert.equal(page.timers.size, 0);
  assert.equal(page.frameRequests.length, 1);
  finish(clone(SAMPLE)); await settle();
  assert.deepEqual([...page.timers.values()].map(timer => timer.delay), [2000]);
  assert.ok(page.rootNode.textContent.includes("0.50 Hz"));
});

test("failed resnaps show the reason and keep existing endpoint evidence", async t => {
  const previous = state(); previous.calibration.profile.endpoints.x_low = { detections: [{ id: 18 }, { id: 14 }], captured_at: STAMP };
  const page = await mounted(t, { state: () => clone(previous), capture: () => ({ state: clone(previous), sample: clone(SAMPLE), accepted: false, tuning: { missing: [14], reason: "Reference is missing." } }) });
  page.find("Calibration").click(); page.find("Snap X low · Left").click(); await settle();
  assert.deepEqual(page.ws._state().state.calibration.profile.endpoints.x_low.detections, [{ id: 18 }, { id: 14 }]);
  assert.ok(page.rootNode.textContent.includes("Previous capture kept."));
  assert.ok(page.rootNode.textContent.includes("Missing tags: 14"));
});

test("incomplete profiles remain saveable and profile names stay text", async t => {
  const draft = state(); draft.calibration.profile.axes = {}; draft.calibration.profile.name = "<img src=x onerror=alert(1)>";
  const page = await mounted(t, { state: () => clone(draft) });
  page.find("Calibration").click(); assert.equal(page.find("Save calibration").disabled, false);
  page.find("Save calibration").click(); await settle();
  assert.equal(page.calls.find(call => call.path === "profile/save").body.name, "<img src=x onerror=alert(1)>");
});

test("the first Save click includes blurred edits and cancels the delayed preview edit", async t => {
  const page = await mounted(t);
  page.find("Calibration").click();
  page.find("Profile name").change("New measurement");
  page.find("X travel (mm)").change("50");
  page.find("Save calibration").click(); await settle();
  assert.equal(page.calls.filter(call => call.path === "profile/save").length, 1);
  assert.equal(page.calls.filter(call => call.path === "profile/edit").length, 0);
  assert.deepEqual(page.calls.find(call => call.path === "profile/save").body, { name: "New measurement", travel_mm: { x: 50, y: 40, insert: 150 } });
  assert.equal(page.messages.length, 1);
  assert.ok([...page.timers.values()].every(timer => timer.delay !== 250));
});

test("a refused Save does not claim success", async t => {
  const page = await mounted(t, { "profile/save": () => { throw new Error("Profile storage unavailable"); } });
  page.find("Calibration").click(); page.find("Save calibration").click(); await settle();
  assert.equal(page.messages.length, 0);
  assert.ok(page.rootNode.textContent.includes("Profile storage unavailable"));
});

test("Download waits for edited travel and its recomputed numerical fit", async t => {
  let exported;
  t.mock.method(URL, "createObjectURL", blob => { exported = blob; return "blob:profile"; });
  t.mock.method(URL, "revokeObjectURL", () => {});
  const page = await mounted(t, { "profile/edit": body => {
    const updated = state(); Object.assign(updated.calibration.profile, body);
    updated.calibration.profile.axes.x.max = body.travel_mm.x;
    return updated;
  } });
  page.find("Calibration").click();
  page.find("Profile name").change("Exported draft"); page.find("X travel (mm)").change("90");
  page.find("Download profile").click(); await settle();
  const output = JSON.parse(await exported.text());
  assert.equal(output.name, "Exported draft");
  assert.equal(output.travel_mm.x, 90);
  assert.equal(output.axes.x.max, 90);
  assert.equal(page.calls.filter(call => call.path === "profile/edit").length, 1);
});

test("saved profile selection restores calibration and clears old measurements", async t => {
  const initial = state(); initial.profiles = [{ id: "saved-two", name: "Second calibration" }];
  const saved = clone(initial); saved.selected_profile = "saved-two";
  saved.calibration.profile.name = "Second calibration"; saved.calibration.profile.travel_mm.x = 60;
  saved.calibration.profile.endpoints.x_low = { detections: [{ id: 18 }], captured_at: STAMP };
  const page = await mounted(t, { state: () => clone(initial), "profile/select": () => clone(saved) });
  await page.runSample();
  page.find("Saved profile").change("saved-two"); await settle();
  assert.equal(page.ws._state().latest, null);
  assert.equal(page.ws._state().log.length, 0);
  assert.equal(page.find("Profile name").value, "Second calibration");
  assert.equal(page.find("X travel (mm)").value, "60");
  assert.equal(page.find("Saved profile").value, "saved-two");
  assert.ok(page.rootNode.textContent.includes("1/6 limits captured"));
});

test("the saved profile list uses text names and loads a selected row", async t => {
  const initial = state(), name = '<img src=x onerror="alert(1)">';
  initial.profiles = [{ id: "saved-one", name }];
  const loaded = clone(initial); loaded.selected_profile = "saved-one"; loaded.calibration.profile.name = name;
  const page = await mounted(t, { state: () => clone(initial), "profile/select": () => clone(loaded) });
  page.find("Calibration").click();
  assert.equal(page.find("Saved profiles").tagName, "UL");
  assert.ok(page.find("Delete " + name));
  page.find("Load " + name).click(); await settle();
  assert.deepEqual(page.calls.find(call => call.path === "profile/select").body, { id: "saved-one" });
  assert.equal(page.find("Profile name").value, name);
  assert.equal(page.find("Saved profile").value, "saved-one");
  assert.equal(page.find("Saved profiles").children[0].getAttribute("aria-current"), "true");
  assert.deepEqual(page.messages, [["Calibration loaded.", "info"]]);
});

test("a valid JSON file uploads its original text and selects the returned saved profile", async t => {
  const text = JSON.stringify(PROFILE, null, 2) + "\n";
  const uploaded = state(); uploaded.selected_profile = "uploaded-one";
  uploaded.profiles = [{ id: "uploaded-one", name: "Imported calibration" }];
  uploaded.calibration.profile.name = "Imported calibration";
  const page = await mounted(t, { "profile/upload": () => clone(uploaded) });
  page.find("Calibration").click();
  const input = page.find("Profile JSON file"); input.selectFile(new Blob([text])); await settle();
  assert.deepEqual(page.calls.find(call => call.path === "profile/upload").body, { profile: text });
  assert.equal(input.value, "");
  assert.equal(page.find("Saved profile").value, "uploaded-one");
  assert.equal(page.find("Profile name").value, "Imported calibration");
  assert.ok(page.find("Delete Imported calibration"));
  assert.equal(page.ws._state().dirty, false);
  assert.deepEqual(page.messages, [["Calibration uploaded and selected.", "info"]]);
});

test("invalid or unreadable files never send upload requests and leave the draft intact", async t => {
  const page = await mounted(t);
  page.find("Profile name").change("Keep this draft");
  const cases = [
    [new Blob(["not JSON"]), "Select a valid JSON profile file."],
    [new Blob(["null"]), "Select a valid JSON profile file."],
    [new Blob(["[]"]), "Select a valid JSON profile file."],
    [new Blob(['"profile"']), "Select a valid JSON profile file."],
    [{ size: 1024 * 1024 + 1, text: () => { throw new Error("Must not read oversized files"); } }, "Profile file must be 1 MiB or smaller."],
    [{ size: 10, text: async () => { throw new Error("Read failed"); } }, "Could not read the profile file."],
  ];
  for (const [file, error] of cases) {
    const input = page.find("Profile JSON file"); input.selectFile(file); await settle();
    assert.ok(page.rootNode.textContent.includes(error));
    assert.equal(input.value, "");
    assert.equal(page.find("Profile name").value, "Keep this draft");
    assert.equal(page.ws._state().dirty, true);
    assert.equal(page.find("Upload profile").disabled, false);
  }
  assert.equal(page.calls.filter(call => call.path === "profile/upload").length, 0);
  assert.equal(page.messages.length, 0);
});

test("a profile file at the size limit reaches server validation", async t => {
  const text = JSON.stringify(PROFILE).padEnd(1024 * 1024, " ");
  const page = await mounted(t, { "profile/upload": () => { throw new Error("Unsupported profile schema"); } });
  page.find("Profile JSON file").selectFile(new Blob([text])); await settle();
  assert.equal(page.calls.filter(call => call.path === "profile/upload").length, 1);
  assert.ok(page.rootNode.textContent.includes("Unsupported profile schema"));
  assert.equal(page.messages.length, 0);
  assert.equal(page.find("Upload profile").disabled, false);
});

test("upload replacement can be canceled and failed replacement preserves unsaved edits", async t => {
  const page = await mounted(t, { "profile/upload": () => { throw new Error("Invalid calibration geometry"); } });
  page.find("Profile name").change("Keep this draft");
  page.find("X travel (mm)").change("60");
  const file = new Blob([JSON.stringify(PROFILE)]);
  page.find("Profile JSON file").selectFile(file); await settle();
  assert.equal(page.calls.filter(call => call.path === "profile/upload").length, 0);
  assert.equal(page.find("Upload profile").disabled, true);
  assert.ok([...page.timers.values()].every(timer => timer.delay !== 250));
  page.find("Cancel").click();
  assert.equal(page.find("Profile name").value, "Keep this draft");
  assert.equal(page.find("X travel (mm)").value, "60");
  page.find("Profile JSON file").selectFile(file); await settle();
  page.find("Discard draft").click(); await settle();
  assert.equal(page.calls.filter(call => call.path === "profile/upload").length, 1);
  assert.ok(page.rootNode.textContent.includes("Invalid calibration geometry"));
  assert.equal(page.find("Profile name").value, "Keep this draft");
  assert.equal(page.find("X travel (mm)").value, "60");
  assert.equal(page.ws._state().dirty, true);
  assert.equal(page.messages.length, 0);
});

test("opening the upload picker cancels a queued edit without discarding form values", async t => {
  const page = await mounted(t);
  page.find("Profile name").change("Unsaved name");
  let pickerOpened = 0;
  page.find("Profile JSON file").addEventListener("click", () => pickerOpened++);
  page.find("Upload profile").click();
  assert.equal(pickerOpened, 1);
  assert.ok([...page.timers.values()].every(timer => timer.delay !== 250));
  page.find("Profile JSON file").selectFile(null); await settle();
  assert.equal(page.find("Profile name").value, "Unsaved name");
  assert.equal(page.ws._state().dirty, true);
  assert.equal(page.calls.filter(call => ["profile/edit", "profile/upload"].includes(call.path)).length, 0);
});

test("loading from the list waits for draft replacement and sends one request", async t => {
  const initial = state(); initial.profiles = [{ id: "saved-one", name: "First calibration" }];
  const loaded = clone(initial); loaded.selected_profile = "saved-one";
  const page = await mounted(t, { state: () => clone(initial), "profile/select": () => clone(loaded) });
  page.find("Profile name").change("Keep this draft");
  page.find("Load First calibration").click();
  assert.equal(page.calls.filter(call => call.path === "profile/select").length, 0);
  assert.equal(page.find("Delete First calibration").disabled, true);
  page.find("Cancel").click();
  assert.equal(page.find("Profile name").value, "Keep this draft");
  page.find("Load First calibration").click();
  page.find("Discard draft").click(); page.find("Discard draft").click(); await settle();
  assert.equal(page.calls.filter(call => call.path === "profile/select").length, 1);
  assert.equal(page.ws._state().dirty, false);
});

test("file reads and upload requests disable competing actions and reject repeated events", async t => {
  let finishRead, finishUpload;
  const initial = state(); initial.profiles = [{ id: "saved-one", name: "First calibration" }];
  const page = await mounted(t, { state: () => clone(initial), "profile/upload": () => new Promise(resolve => { finishUpload = resolve; }) });
  const input = page.find("Profile JSON file");
  input.selectFile({ size: 10, text: () => new Promise(resolve => { finishRead = resolve; }) });
  assert.equal(page.ws._state().busy, true);
  assert.equal(page.find("Upload profile").disabled, true);
  assert.equal(page.find("Load First calibration").disabled, true);
  assert.equal(page.find("Delete First calibration").disabled, true);
  input.selectFile(new Blob([JSON.stringify(PROFILE)]));
  page.find("Load First calibration").click(); page.find("Delete First calibration").click();
  finishRead(JSON.stringify(PROFILE)); await settle();
  assert.equal(page.ws._state().busy, true);
  assert.equal(page.find("Upload profile").disabled, true);
  input.selectFile(new Blob([JSON.stringify(PROFILE)]));
  assert.equal(page.calls.filter(call => call.path === "profile/upload").length, 1);
  assert.equal(page.calls.filter(call => ["profile/delete", "profile/select"].includes(call.path)).length, 0);
  finishUpload(clone(initial)); await settle();
  assert.equal(page.ws._state().busy, false);
  assert.equal(page.find("Upload profile").disabled, false);
});

test("deleting an unselected profile keeps local edits, the active calibration, and measurements", async t => {
  const initial = state(); initial.selected_profile = "saved-one";
  initial.profiles = [{ id: "saved-one", name: "First calibration" }, { id: "saved-two", name: "Second calibration" }];
  const deleted = clone(initial); deleted.profiles.pop();
  const page = await mounted(t, { state: () => clone(initial), "profile/delete": () => clone(deleted) });
  await page.runSample();
  page.find("Profile name").change("Unsaved edits"); page.find("X travel (mm)").change("60");
  page.find("Delete Second calibration").click(); await settle();
  assert.deepEqual(page.calls.find(call => call.path === "profile/delete").body, { id: "saved-two" });
  assert.equal(page.find("Delete Second calibration"), undefined);
  assert.equal(page.find("Saved profile").value, "saved-one");
  assert.equal(page.find("Profile name").value, "Unsaved edits");
  assert.equal(page.find("X travel (mm)").value, "60");
  assert.equal(page.ws._state().dirty, true);
  assert.equal(page.ws._state().latest.captured_at, STAMP);
  assert.equal(page.ws._state().log.length, 1);
  assert.deepEqual(page.messages, [["Saved profile deleted.", "info"]]);
});

test("deleting the selected profile clears monitoring and preserves the calibration draft", async t => {
  const initial = state(); initial.selected_profile = "saved-one";
  initial.profiles = [{ id: "saved-one", name: "First calibration" }];
  const deleted = clone(initial); deleted.profiles = []; deleted.selected_profile = null; deleted.active_profile = null;
  const page = await mounted(t, { state: () => clone(initial), "profile/delete": () => clone(deleted) });
  await page.runSample();
  page.find("Delete First calibration").click(); await settle();
  assert.equal(page.find("Saved profile").value, "");
  assert.equal(page.find("Profile name").value, PROFILE.name);
  assert.deepEqual(page.ws._state().state.calibration.profile, PROFILE);
  assert.equal(page.ws._state().latest, null);
  assert.deepEqual(page.ws._state().display, {});
  assert.equal(page.ws._state().log.length, 0);
  assert.equal(page.ws._state().dirty, true);
  assert.equal(page.find("Tune now").disabled, true);
});

test("a refused deletion preserves the saved row and does not report success", async t => {
  let rejectDelete;
  const initial = state(); initial.profiles = [{ id: "saved-one", name: "First calibration" }];
  const page = await mounted(t, { state: () => clone(initial), "profile/delete": () => new Promise((_resolve, reject) => { rejectDelete = reject; }) });
  page.find("Delete First calibration").click(); page.find("Delete First calibration").click();
  assert.equal(page.find("Load First calibration").disabled, true);
  rejectDelete(new Error("Profile storage unavailable")); await settle();
  assert.equal(page.calls.filter(call => call.path === "profile/delete").length, 1);
  assert.ok(page.find("Delete First calibration"));
  assert.ok(page.rootNode.textContent.includes("Profile storage unavailable"));
  assert.equal(page.messages.length, 0);
  assert.equal(page.find("Load First calibration").disabled, false);
});

test("sample image decode completes before the image and readouts change", async t => {
  let decoded;
  const previousImage = globalThis.Image;
  globalThis.Image = class { decode() { return new Promise(resolve => { decoded = resolve; }); } };
  t.after(() => { globalThis.Image = previousImage; });
  const page = await mounted(t);
  page.find("Tune now").click(); await settle();
  assert.equal(page.ws._state().latest, null);
  assert.equal(page.ws._state().log.length, 0);
  assert.ok(!page.rootNode.all().some(node => node.getAttribute("src") === IMAGE));
  decoded(); await settle();
  assert.equal(page.ws._state().display.insert.value_mm, 75);
  assert.equal(page.ws._state().log.length, 1);
  assert.ok(page.rootNode.all().some(node => node.getAttribute("src") === IMAGE));
});

test("an accepted capture remains accepted when its preview image fails to decode", async t => {
  const previousImage = globalThis.Image;
  globalThis.Image = class { async decode() { throw new Error("decode failed"); } };
  t.after(() => { globalThis.Image = previousImage; });
  const accepted = state(); accepted.calibration.profile.endpoints.x_low = { detections: [{ id: 18 }], captured_at: STAMP };
  const page = await mounted(t, { capture: () => ({ state: clone(accepted), sample: clone(SAMPLE), accepted: true, tuning: {} }) });
  page.find("Calibration").click(); page.find("Snap X low · Left").click(); await settle();
  assert.ok(page.ws._state().state.calibration.profile.endpoints.x_low);
  assert.ok(page.rootNode.textContent.includes("The sampled image could not be decoded."));
  assert.ok(!page.rootNode.textContent.includes("Previous capture kept."));
});

test("visibility changes discard pending frames and resume without losing the scheduler", async t => {
  let resolve, now = 1000;
  t.mock.method(Date, "now", () => now);
  const page = await mounted(t, { measure: () => new Promise(done => { resolve = done; }) });
  await page.runSample();
  globalThis.document.hidden = true; page.documentEvents.visibilitychange();
  assert.equal(page.timers.size, 0);
  assert.ok(page.calls.some(call => call.path === "tuning/cancel"));
  globalThis.document.hidden = false; page.documentEvents.visibilitychange();
  now = 1100;
  resolve(clone(SAMPLE)); await settle();
  assert.equal(page.ws._state().latest, null);
  assert.deepEqual([...page.timers.values()].map(timer => timer.delay), [900]);
  assert.equal(page.frameRequests.length, 1);
});

test("pausing a pending sample waits for its completion before restoring previews", async t => {
  let resolve;
  const page = await mounted(t, { measure: () => new Promise(done => { resolve = done; }) });
  await page.runSample();
  page.find("Pause sampling").click();
  assert.equal(page.frameRequests.length, 1);
  assert.equal(page.timers.size, 0);
  assert.equal(page.rootNode.all().find(node => node.className === "tw-inset").hidden, true);
  resolve(clone(SAMPLE)); await settle();
  assert.equal(page.ws._state().latest, null);
  assert.equal(page.frameRequests.length, 2);
  assert.equal(page.timers.size, 0);
  assert.equal(page.rootNode.all().find(node => node.className === "tw-inset").hidden, false);
});

test("slow preview frames never overlap and hide stops the next request", async t => {
  const page = await mounted(t);
  const preview = page.rootNode.all().find(node => node.tagName === "IMG" && node.getAttribute("src")?.includes("/frame?"));
  assert.ok(preview);
  assert.equal([...page.timers.values()].filter(timer => timer.delay === 1000).length, 0);
  preview.onload();
  assert.equal([...page.timers.values()].filter(timer => timer.delay === 1000).length, 1);
  page.ws.hide();
  assert.equal(page.timers.size, 0);
  assert.equal(preview.getAttribute("src"), null);
});

test("no configured camera disables acquisition and keeps the server warning visible", async t => {
  const empty = state(); empty.camera = null; empty.cameras = []; empty.warning = "The saved profile is unavailable.";
  const page = await mounted(t, { state: () => clone(empty) });
  assert.equal(page.find("Start sampling").disabled, true);
  assert.equal(page.find("Snap X low · Left").disabled, true);
  assert.equal(page.find("Save calibration").disabled, true);
  assert.equal(page.find("Upload profile").disabled, true);
  assert.ok(page.rootNode.textContent.includes(empty.warning));
  assert.ok(page.rootNode.all().filter(node => node.tagName === "IMG").every(node => !node.getAttribute("src")?.includes("/frame?")));
});

test("Monitor uses the saved calibration bounds while Calibration shows the edited draft", async t => {
  const edited = state();
  edited.calibration.profile.axes.x.max = 90; edited.calibration.profile.travel_mm.x = 90;
  edited.active_profile.name = "Saved calibration";
  edited.selected_profile = "saved-one"; edited.profiles = [{ id: "saved-one", name: "Saved calibration" }];
  const page = await mounted(t, { state: () => clone(edited) });
  await page.runSample();
  const point = page.rootNode.all().find(node => node.className === "tw-point" && node.style.bottom);
  assert.equal(point.style.left, (15 / 45) * 100 + "%");
  assert.equal(page.find("X travel (mm)").value, "90");
  assert.equal(page.find("Saved profile").value, "saved-one");
});

test("a captured draft stays uncalibrated in Monitor until Save activates it", async t => {
  const draft = state(); draft.active_profile = null;
  draft.calibration.profile.endpoints.x_low = { detections: [{ id: 18 }, { id: 14 }], captured_at: STAMP };
  const saved = clone(draft); saved.active_profile = clone(draft.calibration.profile);
  saved.selected_profile = "saved-one"; saved.profiles = [{ id: "saved-one", name: saved.active_profile.name }];
  const page = await mounted(t, { state: () => clone(draft), "profile/save": () => clone(saved) });
  assert.equal(page.find("Tune now").disabled, true);
  assert.ok(page.find("Saved profile").textContent.includes("No saved profile"));
  await page.runSample();
  assert.deepEqual(page.ws._state().display, {});
  page.find("Calibration").click(); page.find("Save calibration").click(); await settle();
  page.find("Monitor").click();
  assert.equal(page.find("Tune now").disabled, false);
  await page.runSample();
  assert.equal(page.ws._state().display.x.value_mm, 15);
});

test("English and Chinese keys match, and the browser never opens a USB camera", () => {
  assert.deepEqual(Object.keys(i18n.en).sort(), Object.keys(i18n.zh).sort());
  const script = readFileSync(new URL("../src/orchestrator/web/static/console/twin.js", import.meta.url), "utf8");
  assert.doesNotMatch(script, /getUserMedia|enumerateDevices|showOpenFilePicker|VideoCapture|\.relay\(/);
});
