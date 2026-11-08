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
  for (const id of ["lc-lang", "lc-stop-all"]) make("button", id);
  for (const id of ["lc-strip", "lc-stopband", "lc-notice", "lc-controls", "lc-controls-text"]) make("div", id);
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
  assert.deepEqual(serviceChip({ phase: "ready", connected: false }, true), { tone: "off", key: "chip.disconnected", demo: false });
  assert.equal(LabConsole.strings.en["chip.disconnected"], "Not connected");
  assert.equal(serviceChip({ phase: "ready", connected: false, locked: true }, true).tone, "bad");
  assert.equal(serviceChip({ phase: "unconfigured" }, true).tone, "off");
  assert.equal(serviceChip({ phase: "ready", connected: true, demo: true }, true).demo, true);
  const rows = chips({ services: [], cameras: [], orchestrator: { stale: false } }, true);
  assert.deepEqual(rows.map(row => row.id), ["robot", "stage", "thermal", "vision", "microscope", "furnace"]);
  assert.ok(rows.every(row => row.key || row.text !== undefined));
  assert.deepEqual(runsChip({ stale: false }, { status: "running" }, true), { tone: "info", text: "running" });
});

test("the strip has no cameras chip and shows runs only while a run exists", () => {
  const { LabConsole } = load();
  const { chips, runsChip } = LabConsole.logic;
  assert.equal(runsChip({ stale: false }, null, true), null);
  assert.deepEqual(runsChip({ stale: true }, null, true), { tone: "warn", key: "chip.stale" });
  const ids = snapshot => chips(snapshot, true).map(row => row.id);
  assert.deepEqual(ids({ services: [], orchestrator: { stale: false } }), ["robot", "stage", "thermal", "vision", "microscope", "furnace"]);
  assert.equal(ids({ services: [], orchestrator: { stale: false }, run: { status: "running" } }).at(-1), "runs");
});

test("no console script links to a removed service page", () => {
  for (const name of ["shell", "overview", "motion", "cameras", "process"]) {
    const source = readFileSync(new URL(`../src/orchestrator/web/static/console/${name}.js`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /href: "\/(robot|stage|thermal|furnace|microscope|camera)\//, name);
    assert.doesNotMatch(source, /"\/dashboard\/"/, name);
  }
});

test("the strip renders 6 chips with a hidden dot and visible text", async () => {
  const { LabConsole } = load({ responses: [{ status: 200, body: { services: [{ id: "robot", phase: "ready", connected: true }], cameras: [], orchestrator: { stale: false }, control: [] } }] });
  const view = page();
  LabConsole.start(view.doc);
  await new Promise(resolve => setTimeout(resolve, 0));
  const chips = view.nodes.get("lc-strip").children;
  assert.equal(chips.length, 6);
  assert.equal(chips[0].children[0].attributes["aria-hidden"], "true");
  assert.match(chips[0].textContent, /Robot.*Ready/);
});

test("a console that does not answer shows 1 Console offline chip, not 7 stale chips", async () => {
  const { LabConsole } = load({ responses: [{ status: 200, body: null }] });
  assert.deepEqual(LabConsole.logic.chips(null, false, true), [{ id: "console", tone: "bad", key: "chip.offline" }]);
  assert.equal(LabConsole.logic.chips(null, false, false).length, 7);
  const view = page();
  LabConsole.start(view.doc);
  await new Promise(resolve => setTimeout(resolve, 0));
  const strip = view.nodes.get("lc-strip");
  assert.equal(strip.children.length, 1);
  assert.equal(strip.textContent, "Console offlineno state yet");
  assert.match(strip.children[0].className, /tone-bad/);
});

test("event filters select warnings or one service", () => {
  const { LabConsole } = load();
  const events = [
    { service: "robot", level: "info" },
    { service: "stage", level: "warning" },
    { service: "stage", level: "error" },
  ];
  const { filterEvents, latestOperation } = LabConsole.logic;
  assert.equal(filterEvents(events, "all").length, 3);
  assert.equal(filterEvents(events, "warnings").length, 2);
  assert.deepEqual(filterEvents(events, "robot"), [events[0]]);
  assert.equal(latestOperation([{ action: "motion", id: 1 }, { action: "heat", id: 2 }, { action: "motion", id: 3 }], "motion").id, 3);
});

test("a stop result stays provisional until its operation is done", () => {
  const { LabConsole } = load();
  const { stopView } = LabConsole.logic;
  const press = { action: "motion", at: 0 };
  const running = stopView(press, { phase: "holding", done: false, hold: "pending", results: {
    robot: { status: "confirmed" }, stage: { status: "pending" }, microscope: { status: "unavailable" } } });
  assert.equal(running.phase, "holding");
  assert.deepEqual(running.targets.map(item => item.status), ["provisional", "pending", "unavailable"]);
  assert.equal(running.tone, "info");
  const done = { phase: "done", done: true, hold: "settled" };
  assert.deepEqual(stopView(press, { ...done, results: { robot: { status: "confirmed" }, stage: { status: "unconfirmed" } } })
    .targets.map(item => item.status), ["confirmed", "unconfirmed"]);
  assert.equal(stopView(press, { ...done, results: { robot: { status: "unconfirmed" } } }).tone, "bad");
  assert.equal(stopView(press, { ...done, results: { robot: { status: "confirmed" }, stage: { status: "unavailable" } } }).tone, "ok");
  assert.equal(stopView(press, { ...done, results: { stage: { status: "unavailable" } } }).tone, "off");
  const unheld = stopView(press, { ...done, hold: "unconfirmed", results: { robot: { status: "confirmed" } } });
  assert.equal(unheld.holdFailed, true);
  assert.equal(unheld.tone, "bad");
  // Gas holds no run, so its confirmations are final at once.
  const gas = stopView({ action: "gas", at: 0 }, { phase: "stopping", done: false, results: { "gas-0": { status: "confirmed" } } });
  assert.deepEqual(gas.phases, ["stopping", "done"]);
  assert.equal(gas.targets[0].status, "confirmed");
  assert.equal(stopView({ action: "heat", at: 0, sending: true }, null).phase, "");
  assert.equal(stopView({ action: "heat", at: 0, error: "No answer." }, null).tone, "bad");
  assert.equal(stopView(press, { phase: "stopping", results: { robot: { status: "odd" } } }).targets[0].status, "failed");
});

test("the stop band shows this page's press and any new stop, and Dismiss hides it", async () => {
  const tick = () => new Promise(resolve => setTimeout(resolve, 0));
  const heat = { id: "op-1", action: "heat", phase: "holding", hold: "pending", done: false, results: { furnace: { status: "confirmed" } } };
  const { LabConsole, calls } = load({ responses: [
    { status: 200, body: { control: [], services: [], operations: [{ id: "old", action: "motion", phase: "done", done: true, results: {} }] } },
    { status: 202, body: { operation_id: "op-1" } },
    { status: 200, body: { control: [], services: [], operations: [heat] } },
    { status: 200, body: { control: [], services: [], operations: [{ ...heat, phase: "done", done: true },
      { id: "op-2", action: "gas", phase: "stopping", done: false, results: { "gas-1": { status: "pending" } } }] } },
  ] });
  const view = page();
  LabConsole.start(view.doc);
  await tick();
  const band = view.nodes.get("lc-stopband");
  // A stop that ended before the page opened does not show.
  assert.equal(band.hidden, true);
  const sent = LabConsole._internal.safetyStop("heat");
  assert.equal(band.hidden, false);
  assert.match(band.textContent, /^Stop heat and gas · \d\d:\d\d:\d\d.*Sending…$/);
  await sent;
  await tick();
  assert.equal(calls[1].url, "/lab/api/dashboard/heat");
  assert.deepEqual(JSON.parse(calls[1].init.body), { confirmed: true });
  const [head, targets] = band.children;
  const current = head.children[2].children.find(item => item.className.includes("current"));
  assert.equal(current.textContent, "Holding run");
  assert.equal(current.getAttribute("aria-current"), "step");
  assert.equal(targets.textContent, "FurnaceConfirmed (provisional)");
  assert.match(band.className, /tone-info/);
  // A new stop from another client takes the band. Gas targets have names.
  await LabConsole._internal.poll();
  assert.match(band.textContent, /^Stop all gas · /);
  assert.equal(band.children[1].textContent, "Gas 2Pending");
  head.children[3].click();
  assert.equal(band.hidden, true);
});

test("relay sends one request with the page identity and reports an unknown outcome", async () => {
  const { LabConsole, calls } = load({ responses: [{ status: 502, body: { outcome: "unknown", error: "Command outcome is unknown." } }] });
  const view = page();
  LabConsole.shell.doc = view.doc;
  LabConsole.shell.base = "/lab";
  LabConsole.shell.page = "console-page-test";
  const result = await LabConsole._internal.relay("stage", "move", { motor: 1 });
  assert.equal(result.outcome, "unknown");
  assert.equal(calls.length, 1);
  const { url, init } = calls[0];
  assert.equal(url, "/lab/api/console/command/stage");
  assert.equal(init.headers["X-MG400-Client"], "console-page-test");
  assert.equal(init.headers.Authorization, undefined);
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

class FakeSocket {
  constructor(url) { this.url = url; this.sent = []; FakeSocket.made.push(this); }
  send(text) { this.sent.push(JSON.parse(text)); }
}
FakeSocket.made = [];

function withSocket(run) {
  return async () => {
    const loaded = load();
    FakeSocket.made = [];
    globalThis.WebSocket = FakeSocket;
    globalThis.location = { hash: "", href: "http://100.64.0.1:8080/lab/#motion" };
    const { LabConsole } = loaded;
    Object.assign(LabConsole.shell, { doc: page().doc, base: "/lab", page: "console-page-test" });
    const internal = LabConsole._internal;
    try { await run({ ...loaded, internal }); }
    finally {
      for (const ws of FakeSocket.made) ws.onclose?.();
      clearTimeout(internal.link.timer);
      delete globalThis.WebSocket;
    }
  };
}

test("control requests use the socket while it is open and HTTP otherwise", withSocket(async ({ calls, internal }) => {
  internal.connect();
  const ws = FakeSocket.made[0];
  assert.equal(ws.url, "ws://100.64.0.1:8080/lab/api/console/socket?client=console-page-test");
  await internal.request("/api/console/ws/motion/keys", { seq: 1, held: [] }, { socket: true });
  assert.equal(calls.length, 1);
  ws.onopen();
  const refused = internal.request("/api/console/ws/motion/keys", { seq: 2, held: ["W"] }, { socket: true, accept: [409] });
  assert.equal(calls.length, 1);
  assert.deepEqual(ws.sent, [{ seq: 1, path: "/api/console/ws/motion/keys", body: { seq: 2, held: ["W"] } }]);
  ws.onmessage({ data: JSON.stringify({ seq: 1, status: 409, body: { outcome: "refused", error: "Busy." }, t: 5.5 }) });
  assert.deepEqual(await refused, { outcome: "refused", error: "Busy." });
  // A request without the option stays on HTTP.
  await internal.request("/api/console/heartbeat", {});
  assert.equal(calls.length, 2);
  // A closing socket fails the waiting request with an unknown outcome. HTTP carries the next one.
  const lost = internal.request("/api/console/ws/motion/stage/hold", { id: "hold-1" }, { socket: true });
  ws.onclose();
  await assert.rejects(lost, error => error.status === 0);
  await internal.request("/api/console/ws/motion/stage/hold", { id: "hold-1" }, { socket: true });
  assert.equal(calls.length, 3);
}));

test("Stop and STOP ALL go over the socket and over HTTP", withSocket(async ({ calls, internal }) => {
  internal.connect();
  const ws = FakeSocket.made[0];
  ws.onopen();
  const result = internal.stop("stage");
  assert.deepEqual(calls.map(call => call.url), ["/lab/api/console/stop/stage"]);
  assert.deepEqual(ws.sent.map(item => item.path), ["/api/console/stop/stage"]);
  // The first answer counts.
  ws.onmessage({ data: JSON.stringify({ seq: ws.sent[0].seq, status: 200, body: { device: "stage", outcome: "confirmed" }, t: 1 }) });
  assert.equal((await result).outcome, "confirmed");
  await internal.stopAll();
  assert.equal(calls[1].url, "/lab/api/dashboard/motion");
  assert.deepEqual(ws.sent[1], { seq: 2, path: "/api/dashboard/motion", body: {} });
}));

test("pages keep fonts and scripts local", () => {
  const html = readFileSync(new URL("../src/orchestrator/web/templates/console.html", import.meta.url), "utf8");
  const css = readFileSync(new URL("../src/orchestrator/web/static/console/shell.css", import.meta.url), "utf8");
  assert.doesNotMatch(html + css, /https?:\/\//);
});
