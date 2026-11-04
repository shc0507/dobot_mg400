import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const shellPath = new URL("../src/orchestrator/web/static/console/shell.js", import.meta.url).pathname;

class Element {
  constructor(tag, id = "") {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = new Map();
    this.hidden = false;
    this.className = "";
    this.ownText = "";
    this.value = "";
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("The console must not parse data as HTML."); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ""; this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(event, callback) { this.listeners.set(event, callback); }
  click() { this.listeners.get("click")?.({ preventDefault() {} }); }
}

function page() {
  const nodes = new Map();
  const make = (tag, id) => { const node = new Element(tag, id); nodes.set(id, node); return node; };
  for (const id of ["lc-lang", "lc-stop-all", "lc-connect"]) make("button", id);
  for (const id of ["lc-strip", "lc-access", "lc-notice", "lc-controls", "lc-controls-text", "lc-token"]) make("div", id);
  const i18n = new Element("span"); i18n.dataset.i18n = "stop.all";
  const tabs = ["overview", "motion"].map(id => {
    const link = new Element("a");
    Object.assign(link.dataset, { workspace: id, titleEn: id === "overview" ? "Overview" : "Motion", titleZh: id === "overview" ? "总览" : "运动" });
    return link;
  });
  const sections = ["overview", "motion"].map(id => { const node = new Element("section"); node.dataset.workspace = id; return node; });
  const doc = {
    body: { dataset: { basePath: "/lab" } },
    documentElement: { lang: "en" },
    getElementById: id => nodes.get(id) || null,
    createElement: tag => new Element(tag),
    createTextNode: text => { const node = new Element("#text"); node.textContent = text; return node; },
    querySelectorAll: selector => selector === "[data-i18n]" ? [i18n] : selector === "#lc-tabs a" ? tabs : selector === ".lc-workspace" ? sections : [],
  };
  return { doc, nodes, i18n, tabs, sections };
}

function load({ stored = null, language = "en-US", responses = [] } = {}) {
  delete require.cache[shellPath];
  const storage = new Map(stored ? [["labconsole.lang", stored]] : []);
  const calls = [];
  const listeners = new Map();
  for (const [name, value] of Object.entries({ navigator: { language }, crypto: { getRandomValues: array => array.fill(7) } })) {
    Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
  }
  Object.assign(globalThis, {
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    location: { hash: "" },
    addEventListener: (event, callback) => listeners.set(event, callback),
    performance: { now: () => 0 },
    setInterval: () => 0,
    fetch: async (url, init) => {
      calls.push({ url, init });
      const reply = responses.shift() || { status: 200, body: { control: [], services: [], cameras: [] } };
      return { status: reply.status, ok: reply.status >= 200 && reply.status < 300, json: async () => reply.body };
    },
  });
  const LabConsole = require(shellPath);
  return { LabConsole, storage, calls, listeners };
}

test("language follows storage, then the browser, and the switch persists it", () => {
  const { LabConsole: first } = load({ language: "zh-CN" });
  assert.equal(first.logic.pickLanguage(null, "zh-CN"), "zh");
  assert.equal(first.logic.pickLanguage("en", "zh-CN"), "en");
  assert.equal(first.logic.pickLanguage("fr", "de"), "en");
  const { LabConsole, storage } = load({ stored: "en" });
  const view = page();
  LabConsole.start(view.doc);
  assert.equal(view.i18n.textContent, "STOP ALL MOTION");
  assert.equal(view.nodes.get("lc-lang").textContent, "中文");
  view.nodes.get("lc-lang").click();
  assert.equal(storage.get("labconsole.lang"), "zh");
  assert.equal(view.doc.documentElement.lang, "zh-CN");
  assert.equal(view.i18n.textContent, "停止全部运动");
  assert.equal(view.tabs[1].textContent, "运动");
  assert.equal(view.nodes.get("lc-lang").getAttribute("aria-label"), "切换到英文");
  view.nodes.get("lc-lang").click();
  assert.equal(view.tabs[1].textContent, "Motion");
});

test("translation falls back to English and fills variables", () => {
  const { LabConsole } = load();
  const { translate } = LabConsole.logic;
  const tables = { en: { a: "Hello {name}", b: "Only English" }, zh: { a: "你好 {name}" } };
  assert.equal(translate(tables, "zh", "a", { name: "X" }), "你好 X");
  assert.equal(translate(tables, "zh", "b"), "Only English");
  assert.equal(translate(tables, "zh", "missing"), "missing");
  for (const key of Object.keys(LabConsole.strings.en)) assert.ok(key in LabConsole.strings.zh, key);
});

test("status chips always carry text and turn stale without fresh state", () => {
  const { LabConsole } = load();
  const { serviceChip, chips, runsChip } = LabConsole.logic;
  assert.deepEqual(serviceChip({ phase: "ready", connected: true }, false), { tone: "warn", key: "chip.stale" });
  assert.equal(serviceChip({ phase: "ready", connected: true, fault: "x" }, true).key, "chip.fault");
  assert.equal(serviceChip({ phase: "ready", connected: true, busy: true }, true).key, "chip.busy");
  assert.equal(serviceChip({ phase: "ready", connected: false }, true).key, "chip.disconnected");
  assert.equal(serviceChip({ phase: "unconfigured" }, true).tone, "off");
  assert.equal(serviceChip({ phase: "ready", connected: true, demo: true }, true).demo, true);
  const rows = chips({ services: [], cameras: [], orchestrator: { stale: false } }, true);
  assert.deepEqual(rows.map(row => row.id), ["robot", "stage", "cameras", "thermal", "vision", "microscope", "furnace", "runs"]);
  assert.ok(rows.every(row => row.key || row.text !== undefined));
  assert.deepEqual(runsChip({ stale: false }, { status: "running" }, true), { tone: "info", text: "running" });
});

test("the cameras chip follows the console camera relay, not a dashboard link", () => {
  const { LabConsole } = load();
  const { camerasChip, chips } = LabConsole.logic;
  assert.deepEqual(camerasChip({ state: "live" }, true), { tone: "ok", key: "chip.live" });
  assert.deepEqual(camerasChip({ state: "idle" }, true), { tone: "ok", key: "chip.configured" });
  assert.deepEqual(camerasChip({ state: "stale" }, true), { tone: "warn", key: "chip.stale" });
  assert.deepEqual(camerasChip({ state: "unavailable" }, true), { tone: "bad", key: "chip.unavailable" });
  assert.deepEqual(camerasChip({ state: "unconfigured" }, true), { tone: "off", key: "chip.unconfigured" });
  assert.deepEqual(camerasChip(undefined, true), { tone: "off", key: "chip.unconfigured" });
  assert.deepEqual(camerasChip({ state: "live" }, false), { tone: "warn", key: "chip.stale" });
  // A blank MG400_DASH_CAMERA_URL must not hide live console cameras.
  const snapshot = { services: [], cameras: [{ id: "camera", url: null }], camera_preview: { state: "live" }, orchestrator: { stale: false } };
  assert.equal(chips(snapshot, true).find(row => row.id === "cameras").key, "chip.live");
});

test("the overview links the camera preview to the Cameras workspace without a key", () => {
  const source = readFileSync(new URL("../src/orchestrator/web/static/console/overview.js", import.meta.url), "utf8");
  assert.match(source, /\["camera", "#cameras"\]/);
  assert.doesNotMatch(source, /"\/camera\/"/);
});

test("the strip renders 8 chips with a hidden dot and visible text", async () => {
  const { LabConsole } = load({ responses: [{ status: 200, body: { services: [{ id: "robot", phase: "ready", connected: true }], cameras: [], orchestrator: { stale: false }, control: [] } }] });
  const view = page();
  LabConsole.start(view.doc);
  await new Promise(resolve => setTimeout(resolve, 0));
  const chips = view.nodes.get("lc-strip").children;
  assert.equal(chips.length, 8);
  assert.equal(chips[0].children[0].attributes["aria-hidden"], "true");
  assert.match(chips[0].textContent, /Robot.*Ready/);
});

test("event filters select warnings or one service", () => {
  const { LabConsole } = load();
  const events = [
    { service: "robot", level: "info" },
    { service: "stage", level: "warning" },
    { service: "stage", level: "error" },
  ];
  const { filterEvents, readinessSummary, latestOperation } = LabConsole.logic;
  assert.equal(filterEvents(events, "all").length, 3);
  assert.equal(filterEvents(events, "warnings").length, 2);
  assert.deepEqual(filterEvents(events, "robot"), [events[0]]);
  assert.deepEqual(readinessSummary([{ state: "ready" }, { state: "todo" }, { state: "blocked" }, { state: "blocked" }]), { ready: 1, todo: 1, blocked: 2 });
  assert.equal(latestOperation([{ action: "motion", id: 1 }, { action: "heat", id: 2 }, { action: "motion", id: 3 }], "motion").id, 3);
});

test("relay sends one request with the page identity and reports an unknown outcome", async () => {
  const { LabConsole, calls } = load({ responses: [{ status: 502, body: { outcome: "unknown", error: "Command outcome is unknown." } }] });
  const view = page();
  LabConsole.shell.doc = view.doc;
  LabConsole.shell.base = "/lab";
  LabConsole.shell.page = "console-page-test";
  LabConsole.shell.token = "secret-token-value";
  const result = await LabConsole._internal.relay("stage", "move", { motor: 1 });
  assert.equal(result.outcome, "unknown");
  assert.equal(calls.length, 1);
  const { url, init } = calls[0];
  assert.equal(url, "/lab/api/console/command/stage");
  assert.equal(init.headers["X-MG400-Client"], "console-page-test");
  assert.equal(init.headers.Authorization, "Bearer secret-token-value");
  assert.deepEqual(JSON.parse(init.body), { action: "move", params: { motor: 1 } });
  assert.equal(init.credentials, "omit");
  assert.match(view.nodes.get("lc-notice").textContent, /unknown/);
});

test("a heartbeat is sent only while this page holds control", async () => {
  const { LabConsole, calls } = load({ responses: [{ status: 200, body: { control: [{ device: "stage", mine: true, state: "held" }] } }] });
  const view = page();
  LabConsole.shell.doc = view.doc;
  LabConsole.shell.base = "/lab";
  LabConsole.shell.snapshot = { control: [] };
  await LabConsole._internal.beat();
  assert.equal(calls.length, 0);
  LabConsole.shell.snapshot = { control: [{ device: "stage", mine: true, state: "held" }] };
  await LabConsole._internal.beat();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/lab/api/console/heartbeat");
  assert.ok(LabConsole.BEAT_MS <= 1000);
});

test("page close releases this page's leases with a keepalive request", async () => {
  const { LabConsole, calls, listeners } = load();
  const view = page();
  LabConsole.start(view.doc);
  await new Promise(resolve => setTimeout(resolve, 0));
  calls.length = 0;
  LabConsole.shell.snapshot = { control: [{ device: "robot", mine: true, state: "held" }] };
  listeners.get("pagehide")();
  assert.equal(calls[0].url, "/lab/api/console/release-page");
  assert.equal(calls[0].init.keepalive, true);
});

test("pages keep fonts and scripts local", () => {
  const html = readFileSync(new URL("../src/orchestrator/web/templates/console.html", import.meta.url), "utf8");
  const css = readFileSync(new URL("../src/orchestrator/web/static/console/shell.css", import.meta.url), "utf8");
  assert.doesNotMatch(html + css, /https?:\/\//);
});
