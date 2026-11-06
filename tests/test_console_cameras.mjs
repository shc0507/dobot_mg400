import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const path = new URL("../src/orchestrator/web/static/console/cameras.js", import.meta.url).pathname;
const cameras = require(path);
const { logic } = cameras;

test("badge shows LIVE, STALE or OFFLINE with the frame age", () => {
  assert.deepEqual(logic.badge({ state: "live", age_s: 0.4 }), { tone: "ok", key: "badge.live", age: 0.4 });
  assert.deepEqual(logic.badge({ state: "live", age_s: 3.1 }), { tone: "warn", key: "badge.stale", age: 3.1 });
  assert.deepEqual(logic.badge({ state: "stale", age_s: 9 }), { tone: "warn", key: "badge.stale", age: 9 });
  assert.equal(logic.badge({ state: "offline", age_s: null }).key, "badge.offline");
  assert.equal(logic.badge(null).key, "badge.offline");
  // An old status poll means the console may be gone: never claim LIVE.
  assert.equal(logic.badge({ state: "live", age_s: 0.1 }, { fresh: false }).key, "badge.offline");
  assert.equal(logic.badge({ state: "live", age_s: 0.1 }, { frozen: true }).key, "badge.frozen");
});

test("thermal row ages the frame by the time since the fetch", () => {
  assert.deepEqual(logic.thermalRow({ age_s: 0.5 }, 1000, 2000, false), { state: "live", age_s: 1.5 });
  assert.deepEqual(logic.thermalRow({ age_s: 0.5 }, 1000, 4000, false), { state: "stale", age_s: 3.5 });
  assert.equal(logic.thermalRow({ age_s: 0 }, 1000, 1000, true).state, "offline");
  assert.equal(logic.thermalRow(null, 0, 0, false).state, "offline");
});

test("units, scale and palettes", () => {
  assert.equal(logic.convert(100, "F"), 212);
  assert.equal(logic.convert(0, "K"), 273.15);
  assert.ok(Math.abs(logic.toCelsius(212, "F") - 100) < 1e-9);
  assert.equal(logic.formatTemp(20, "C"), "20.0 °C");
  assert.equal(logic.formatTemp(Number.NaN, "C"), "—");
  const stats = { min: { celsius: 10 }, max: { celsius: 30 } };
  assert.deepEqual(logic.scaleRange(stats, { scale: "auto" }), [10, 30]);
  assert.deepEqual(logic.scaleRange(stats, { scale: "fixed", low: 0, high: 100 }), [0, 100]);
  assert.deepEqual(logic.scaleRange(stats, { scale: "fixed", low: 50, high: 40 }), [10, 30]);
  for (const name of ["ironbow", "rainbow", "grey"]) assert.equal(logic.paletteTable(name).length, 768);
  const grey = logic.paletteTable("grey");
  assert.deepEqual([...grey.slice(0, 3)], [0, 0, 0]);
  assert.deepEqual([...grey.slice(765)], [255, 255, 255]);
});

test("thermal pixels map the scale to the palette", () => {
  const temps = Array.from({ length: 120 }, () => Array.from({ length: 160 }, (_, x) => (x < 80 ? 10 : 30)));
  const pixels = logic.thermalPixels(temps, logic.paletteTable("grey"), 10, 30);
  assert.equal(pixels.length, 160 * 120 * 4);
  assert.deepEqual([...pixels.slice(0, 4)], [0, 0, 0, 255]);
  assert.deepEqual([...pixels.slice(159 * 4, 160 * 4)], [255, 255, 255, 255]);
});

test("CSV export uses the selected unit", () => {
  const frame = { sequence: 3, timestamp: "t", temperatures_c: [[0, 100]] };
  assert.equal(logic.thermalCsv(frame, "F"), "# sequence=3, timestamp=t, unit=°F\n32.00,212.00\n");
});

test("the pixel probe reads 1 sensor pixel inside the frame", () => {
  const frame = { temperatures_c: Array.from({ length: 120 }, (_, y) => Array.from({ length: 160 }, (_, x) => x + y / 100)) };
  assert.deepEqual(logic.probeReading(frame, { x: 10, y: 5 }), { x: 10, y: 5, celsius: 10.05 });
  assert.deepEqual(logic.probeReading(frame, { x: 159, y: 119, pinned: true }), { x: 159, y: 119, celsius: 160.19 });
  for (const probe of [null, { x: 160, y: 0 }, { x: -1, y: 0 }, { x: 0, y: 120 }, { x: 1.5, y: 0 }]) assert.equal(logic.probeReading(frame, probe), null);
  assert.equal(logic.probeReading(null, { x: 1, y: 1 }), null);
  assert.equal(logic.translate("en", "probe", { x: 10, y: 5, value: "25.0 °C" }), "Pixel 10, 5: 25.0 °C");
});

test("region drag gives bounded sensor bounds", () => {
  assert.deepEqual(logic.regionFrom({ x: 50.4, y: 70 }, { x: 10, y: 20.6 }), { x0: 10, y0: 21, x1: 50, y1: 70 });
  assert.deepEqual(logic.regionFrom({ x: -5, y: -5 }, { x: 500, y: 500 }), { x0: 0, y0: 0, x1: 160, y1: 120 });
  assert.equal(logic.regionFrom({ x: 5, y: 5 }, { x: 6, y: 6 }), null);
});

test("overlay geometry, retry delay and translations", () => {
  assert.deepEqual(logic.containRect(640, 480, 320, 320), { x: 0, y: 40, scale: 0.5 });
  assert.equal(logic.backoff(0), 1000);
  assert.equal(logic.backoff(10), 15000);
  assert.equal(logic.stamp(new Date("2026-01-02T03:04:05.678Z")), "20260102T030405Z");
  assert.equal(logic.translate("zh", "badge.live"), "实时");
  assert.equal(logic.translate("en", "age", { age: "1.0" }), "1.0 s");
  for (const key of Object.keys(cameras.i18n.en)) assert.ok(key in cameras.i18n.zh, key);
  assert.deepEqual(cameras.VIEWS, ["camera1", "camera2", "camera3", "camera4", "thermal", "microscope"]);
  assert.throws(() => cameras.tile("camera9"), /Unknown camera view/);
});

test("the workspace never parses data as HTML and never stores a token", () => {
  const source = readFileSync(path, "utf8");
  assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML/.test(source));
  assert.ok(!/localStorage|sessionStorage/.test(source));
  assert.ok(!/Authorization/.test(source));
});

test("cameras load from the proxy and the microscope from the console relay", () => {
  assert.equal(logic.streamUrl("camera2", false, "/lab"), "/camera/camera2.mjpeg?fps=3");
  assert.equal(logic.streamUrl("camera2", true, "/lab"), "/camera/camera2.mjpeg?fps=6");
  assert.equal(logic.snapshotUrl("camera4", "/lab"), "/camera/camera4.full.jpg");
  assert.equal(logic.streamUrl("microscope", false, "/lab"), "/lab/api/console/ws/cameras/stream/microscope?fps=10");
  assert.equal(logic.streamUrl("microscope", true, "/lab"), "/lab/api/console/ws/cameras/stream/microscope?fps=30");
  assert.equal(logic.snapshotUrl("microscope", "/lab"), "/lab/api/console/ws/cameras/snapshot/microscope");
});

test("USB cameras and the microscope play H.264 from the proxy; the thermal view has none", () => {
  assert.equal(logic.videoUrl("camera1"), "/camera/camera1.mp4");
  assert.equal(logic.videoUrl("camera4"), "/camera/camera4.mp4");
  assert.equal(logic.videoUrl("microscope"), "/camera/microscope.mp4");
  assert.equal(logic.videoUrl("thermal"), null);
});

test("a playing H.264 microscope tile is live; the relay state applies to its MJPEG fallback", () => {
  const relay = { id: "microscope", state: "offline", age_s: null, fps: null };
  cameras.engine.status.set("microscope", relay);
  const tile = { view: "microscope", live: false, stopVideo: () => {} };
  cameras.engine.tiles.add(tile);
  try {
    assert.deepEqual(logic.rowFor("microscope"), { ...relay, state: "offline", age_s: null });
    tile.live = true;
    assert.deepEqual(logic.rowFor("microscope"), { ...relay, state: "live", age_s: 0 });
    // An MJPEG fallback tile shows the state that the console relay reports.
    tile.stopVideo = null;
    assert.deepEqual(logic.rowFor("microscope"), relay);
  } finally {
    cameras.engine.tiles.delete(tile);
    cameras.engine.status.delete("microscope");
  }
});

test("the H.264 player stays near the newest frame", () => {
  // Near the end: play at normal speed.
  assert.deepEqual(logic.chase(9.9, 0, 10), { seek: null, rate: 1 });
  // A little behind: catch up slowly.
  assert.deepEqual(logic.chase(9.5, 0, 10), { seek: null, rate: 1.1 });
  // Far behind, or before the newest buffered range: jump close to the end.
  assert.deepEqual(logic.chase(8, 0, 10), { seek: 9.9, rate: 1 });
  assert.deepEqual(logic.chase(0, 4, 4.05), { seek: 4, rate: 1 });
});

test("tiles show the measured frame rate and drop an old rate", () => {
  assert.equal(logic.frameRate({ frames: 10, at: 1000 }, { frames: 40, at: 2000 }), 30);
  assert.equal(logic.frameRate({ frames: 10, at: 1000 }, { frames: 10, at: 1000 }), null);
  assert.equal(logic.frameRate({ frames: 10, at: 1000 }, { frames: 5, at: 2000 }), null);
  const rate = logic.meter();
  logic.measure(rate, 3, 0);
  logic.measure(rate, 18, 500);
  // Less than 1 s of frames: no rate yet.
  assert.equal(logic.current(rate, 500), null);
  logic.measure(rate, 33, 1000);
  assert.equal(logic.current(rate, 1000), 30);
  // A new video source counts from 0 again.
  logic.measure(rate, 0, 1100);
  assert.deepEqual(rate.first, { frames: 0, at: 1100 });
  // No new frames for 2.5 s: the rate is old.
  assert.equal(logic.current(rate, 3600), null);
  assert.equal(logic.modeLine("640×480", 29.84), "640×480 · 29.8 fps");
  assert.equal(logic.modeLine("640×480", 4.9), "640×480 · 4.9 fps");
  assert.equal(logic.modeLine("640×480", null), "640×480");
  assert.equal(logic.modeLine("640×480", Number.NaN), "640×480");
});

test("the thermal panel relays connect and shutter and links no service page", () => {
  const source = readFileSync(path, "utf8");
  assert.match(source, /send\("connect", \{\}\)/);
  assert.match(source, /send\("shutter", \{\}\)/);
  assert.equal(logic.translate("en", "connect"), "Connect camera");
  assert.ok(!/side\.page|target: "_blank"/.test(source));
});

// A small DOM for mounting the workspace. Nodes move between parents like real nodes.
class FakeNode {
  constructor(tag, doc) {
    Object.assign(this, { tagName: tag.toUpperCase(), doc, children: [], parent: null, attributes: {}, dataset: {}, listeners: new Map(),
      hidden: false, disabled: false, className: "", ownText: "", value: "", style: {} });
  }
  get classList() {
    const node = this;
    const list = () => node.className.split(" ").filter(Boolean);
    return {
      add: name => { if (!list().includes(name)) node.className = [...list(), name].join(" "); },
      remove: name => { node.className = list().filter(item => item !== name).join(" "); },
      toggle: (name, on) => { if (on ?? !list().includes(name)) node.classList.add(name); else node.classList.remove(name); },
      contains: name => list().includes(name),
    };
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  append(...nodes) {
    for (const node of nodes) {
      if (node.parent) node.parent.children = node.parent.children.filter(item => item !== node);
      node.parent = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) { for (const node of this.children) node.parent = null; this.children = []; this.ownText = ""; this.append(...nodes); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(event, callback) { this.listeners.set(event, callback); }
  dispatch(event, data = {}) { this.listeners.get(event)?.({ preventDefault() {}, ...data }); }
  click() { if (!this.disabled) this.dispatch("click"); }
  contains(node) { for (let item = node; item; item = item.parent) if (item === this) return true; return false; }
  focus() { this.doc.activeElement = this; }
  get isConnected() { return false; }
  getClientRects() { return []; }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
  querySelector(selector) {
    const key = /^\[data-focus="([^"]+)"\]$/.exec(selector)?.[1];
    return this.all().slice(1).find(node => key && node.getAttribute("data-focus") === key) || null;
  }
}

test("the side panel updates in place; a confirmation takes the focus and Esc closes it", async () => {
  const doc = { activeElement: null, visibilityState: "visible", body: { dataset: { basePath: "/lab" } }, addEventListener() {} };
  doc.createElement = tag => new FakeNode(tag, doc);
  doc.createTextNode = text => Object.assign(new FakeNode("#text", doc), { ownText: String(text) });
  const timers = [];
  const saved = { document: globalThis.document, LabConsole: globalThis.LabConsole, setTimeout: globalThis.setTimeout, setInterval: globalThis.setInterval };
  let registered = null;
  Object.assign(globalThis, {
    document: doc, LabConsole: { register: item => { registered = item; } },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, setInterval: () => 1,
  });
  try {
    const sent = [];
    const api = {
      t: (key, vars) => logic.translate("en", key, vars), lang: () => "en",
      describe: async () => ({ commands: { shutter: { any_client: true }, connect: { any_client: true } } }),
      relay: async (device, action) => { sent.push([device, action]); return { outcome: "accepted" }; },
      notify() {}, owns: () => false, take: async () => {}, release: async () => {},
      state: () => ({ services: [{ id: "thermal", connected: true }] }),
      onLanguage() {}, onState() {}, consoleRequest: async () => ({}),
    };
    cameras.install();
    const rootNode = doc.createElement("section");
    registered.mount(rootNode, api);
    await new Promise(resolve => saved.setTimeout(resolve, 0));
    const side = rootNode.all().find(node => node.className === "lc-card lcam-side");
    const refresh = () => { for (const item of timers.splice(0)) if (item.fn.name === "refreshSide") item.fn(); };
    cameras.engine.thermal = { sequence: 1, stats: { max: { celsius: 30 } } };
    cameras.engine.listeners.forEach(fn => fn());
    refresh();
    const shutter = side.querySelector('[data-focus="shutter"]');
    const max = () => side.all().find(node => node.className === "lcam-value");
    assert.match(max().textContent, /^30/);
    // A new reading changes the text, not the nodes.
    cameras.engine.thermal = { sequence: 2, stats: { max: { celsius: 31.5 } } };
    cameras.engine.listeners.forEach(fn => fn());
    refresh();
    assert.equal(side.querySelector('[data-focus="shutter"]'), shutter);
    assert.match(max().textContent, /^31\.5/);
    // The confirmation takes the focus. Esc closes it and gives the focus back to the button.
    shutter.click();
    const dialog = side.querySelector('[data-focus="confirm"]');
    assert.equal(dialog.getAttribute("role"), "alertdialog");
    assert.equal(doc.activeElement, dialog);
    dialog.dispatch("keydown", { key: "Escape" });
    assert.equal(side.querySelector('[data-focus="confirm"]'), null);
    assert.equal(doc.activeElement.getAttribute("data-focus"), "shutter");
    assert.deepEqual(sent, []);
    // The overlay switches show their new state at once.
    const detections = rootNode.all().find(node => node.tagName === "BUTTON" && /Detections|detection/i.test(node.textContent));
    assert.equal(detections.getAttribute("aria-pressed"), "true");
    detections.click();
    assert.equal(detections.getAttribute("aria-pressed"), "false");
    assert.doesNotMatch(detections.textContent, /✓/);
  } finally {
    Object.assign(globalThis, saved);
  }
});
