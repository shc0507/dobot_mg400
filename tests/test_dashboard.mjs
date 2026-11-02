import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const script = readFileSync(new URL("../src/orchestrator/web/static/dashboard.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../src/orchestrator/web/templates/dashboard.html", import.meta.url), "utf8");
const drain = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = new Map();
    this.value = "";
    this.checked = this.disabled = this.hidden = false;
    this.className = "";
    this.ownText = "";
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("The page must not parse data as HTML."); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ""; this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(event, callback) { this.listeners.set(event, callback); }
  focus() { this.focused = true; }
  emit(event) {
    if (event === "click" && this.disabled) return;
    this.listeners.get(event)?.({ preventDefault() {} });
  }
}

function browser(base = "/lab", identity = true) {
  let now = 0, serial = 0;
  const nodes = new Map(), timers = new Map(), requests = [], windowListeners = new Map();
  for (const match of html.matchAll(/<(\w+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const element = new Element(match[1]);
    element.disabled = /\sdisabled(?:\s|>)/.test(match[0]);
    element.hidden = /\shidden(?:\s|>)/.test(match[0]);
    nodes.set(match[2], element);
  }
  const document = {
    body: { dataset: { basePath: base } },
    getElementById: id => {
      assert.ok(nodes.has(id), "Template contains " + id);
      return nodes.get(id);
    },
    createElement: tag => new Element(tag),
  };
  vm.runInNewContext(script, {
    document,
    window: { addEventListener: (event, callback) => windowListeners.set(event, callback) },
    location: { href: "http://localhost:8772/lab/" },
    crypto: { getRandomValues: bytes => { if (!identity) throw new Error("No identity"); return bytes.fill(17); } },
    Uint8Array, URL, AbortController,
    performance: { now: () => now },
    setTimeout: (callback, delay) => { const id = ++serial; timers.set(id, { at: now + delay, callback }); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: (url, options) => {
      const pending = deferred();
      const request = { ...pending, url, options };
      options.signal.addEventListener("abort", () => pending.reject(new Error("Request timed out")));
      requests.push(request);
      return pending.promise;
    },
    // Any accidental persistence is a test failure.
    localStorage: new Proxy({}, { get() { throw new Error("No persistent tokens"); } }),
    sessionStorage: new Proxy({}, { get() { throw new Error("No persistent tokens"); } }),
  });
  const page = {
    nodes, requests,
    get: id => document.getElementById(id),
    async click(id) { page.get(id).emit("click"); await drain(); },
    async submit(id) { page.get(id).emit("submit"); await drain(); },
    async respond(index, data, status = 200) {
      requests[index].resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
      await drain();
    },
    async reject(index, message = "Network unavailable") { requests[index].reject(new Error(message)); await drain(); },
    async advance(ms) {
      const end = now + ms;
      await drain();
      let remaining = 1000;
      while (remaining-- > 0) {
        const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > end) break;
        now = next[1].at;
        timers.delete(next[0]);
        next[1].callback();
        await drain();
      }
      assert.ok(remaining > 0, "Timers must not spin");
      now = end;
      await drain();
    },
    async change(id, value) {
      const element = page.get(id);
      if (typeof value === "boolean") element.checked = value;
      else element.value = value;
      element.emit(typeof value === "boolean" ? "change" : "input");
      await drain();
    },
    async hide() { windowListeners.get("pagehide")(); await drain(); },
  };
  return page;
}

const client = "dashboard-" + "11".repeat(16);
const service = overrides => ({
  id: "robot", label: "Robot", configured: true, phase: "ready", connected: true,
  busy: false, locked: false, fault: null, lease_holder: null, demo: true, error: null, ...overrides,
});
const run = overrides => ({
  id: "run/one", name: "Example run", status: "held", detail: "Check the step outcome.",
  closed: false, settled: true, draining: false, resume_kind: "started", step_position: 0,
  steps: [{ position: 0, skill: "robot.move", status: "failed", error: "Motion outcome unknown" }], ...overrides,
});
const state = overrides => ({
  services: [service()], cameras: [
    { id: "camera", label: "Camera", url: null },
    { id: "thermal", label: "Thermal camera", url: null },
    { id: "microscope", label: "Microscope", url: null },
  ],
  run: null, orchestrator: { demo: true, connected: true, busy: false, locked: false, stale: false, lease: { holder: null, remaining_s: 0 } },
  operations: [], ...overrides,
});
const owned = overrides => state({ orchestrator: { demo: true, connected: true, busy: false, locked: false, stale: false, lease: { holder: client, remaining_s: 3 } }, ...overrides });
const posts = page => page.requests.filter(request => request.options.method === "POST");
async function control(page, data = owned({ run: run() })) {
  await page.respond(0, state({ run: data.run }));
  await page.click("acquire-control");
  await page.respond(1, { lease: { holder: client, remaining_s: 3 } });
  await page.respond(2, data);
}
async function nextState(page, data) {
  await page.advance(1000);
  const index = page.requests.findLastIndex(request => request.options.method !== "POST");
  await page.respond(index, data);
}
const descendants = element => [element, ...element.children.flatMap(descendants)];

test("template keeps access secret in a password input and uses prefixed assets", () => {
  assert.match(html, /data-base-path="\{\{ base_path \}\}"/);
  assert.match(html, /type="password" autocomplete="off"/);
  assert.match(html, /\{\{ base_path \}\}\/dashboard-static\/dashboard.js/);
  assert.doesNotMatch(html, /\|\s*safe|localStorage|sessionStorage/);
  assert.doesNotMatch(script, /innerHTML|insertAdjacentHTML|document\.write/);
});

test("token is memory-only, headers carry identity, and redirects cannot forward credentials", async () => {
  const page = browser();
  await page.respond(0, { error: "Token required" }, 401);
  await page.advance(2000);
  assert.equal(page.requests.length, 1);
  page.get("access-token").value = "private frontend bearer token";
  await page.submit("access-form");
  await page.respond(1, state());
  await page.click("stop-motion");
  const request = page.requests[2];
  assert.equal(request.url, "/lab/api/dashboard/motion");
  assert.equal(request.options.headers.Authorization, "Bearer private frontend bearer token");
  assert.equal(request.options.headers["X-MG400-Client"], client);
  assert.equal(request.options.headers["Content-Type"], "application/json");
  assert.equal(request.options.redirect, "error");
  assert.equal(request.options.credentials, "omit");
  assert.equal(request.options.cache, "no-store");
  assert.ok(page.requests.every(item => !item.url.includes("private")));
  await page.respond(2, { error: "Refused private frontend bearer token" }, 503);
  assert.doesNotMatch(page.get("notice").textContent, /private frontend bearer token/);
  assert.match(page.get("notice").textContent, /\[redacted\]/);
});

test("invalid base paths never send requests, and root or dotted prefixes work", () => {
  for (const base of ["//external.invalid", "/\\external.invalid", "/../escape", "/lab?other"]) {
    const invalid = browser(base);
    assert.equal(invalid.requests.length, 0);
    assert.match(invalid.get("notice").textContent, /Invalid dashboard path/);
  }
  assert.equal(browser("").requests[0].url, "/api/dashboard/state");
  assert.equal(browser("/lab.v1").requests[0].url, "/lab.v1/api/dashboard/state");
});

test("secure identity failure disables writes but still permits status reads", async () => {
  const page = browser("", false);
  await page.respond(0, state());
  await page.click("stop-motion");
  await page.click("acquire-control");
  assert.equal(posts(page).length, 0);
  assert.equal(page.get("stop-motion").disabled, true);
});

test("independent motion stop needs no lease and heat/gas each require an inline confirmation", async () => {
  const page = browser();
  await page.respond(0, state());
  await page.click("confirm-heat");
  await page.click("confirm-gas");
  assert.equal(posts(page).length, 0);
  await page.click("show-heat");
  await page.click("cancel-heat");
  assert.equal(posts(page).length, 0);
  await page.click("stop-motion");
  await page.click("stop-motion");
  await page.click("show-heat");
  await page.click("confirm-heat");
  await page.click("show-gas");
  await page.click("confirm-gas");
  assert.deepEqual(posts(page).map(item => [item.url, JSON.parse(item.options.body)]), [
    ["/lab/api/dashboard/motion", {}],
    ["/lab/api/dashboard/heat", { confirmed: true }],
    ["/lab/api/dashboard/gas", { confirmed: true }],
  ]);
  assert.match(html, /all furnace-controlled gas and water/);
  assert.match(html, /Furnace fault handling can also stop other outputs/);
  assert.doesNotMatch(html, /Water stays unchanged/);
});

test("accepted safety request is pending, never a confirmed stop", async () => {
  const page = browser();
  await page.respond(0, state());
  await page.click("stop-motion");
  await page.respond(1, { operation_id: "op1" }, 202);
  assert.match(page.get("notice").textContent, /outcomes are pending/);
  assert.doesNotMatch(page.get("notice").textContent, /stopped|confirmed/);
});

test("per-target confirmations stay provisional through holding and final stop", async () => {
  const page = browser();
  const operation = { id: "op", action: "motion", done: false, phase: "holding", hold: "pending", draining: true, results: { robot: { status: "confirmed", message: "First stop accepted" }, stage: { status: "unconfirmed", message: "Check stage" } } };
  await page.respond(0, state({ operations: [operation] }));
  assert.match(page.get("operations").textContent, /Run hold pending/);
  assert.match(page.get("operations").textContent, /confirmed \(provisional\)/);
  assert.match(page.get("operations").textContent, /still draining/);
  await nextState(page, state({ operations: [{ ...operation, phase: "final-stop", hold: "settled", draining: false }] }));
  assert.match(page.get("operations").textContent, /Final stop requests pending/);
  assert.match(page.get("operations").textContent, /provisional/);
  await nextState(page, state({ operations: [{ ...operation, phase: "done", done: true, hold: "settled", draining: false }] }));
  assert.doesNotMatch(page.get("operations").textContent, /provisional/);
  assert.match(page.get("operations").textContent, /stage: unconfirmed/);
  assert.match(page.get("operations").textContent, /Read each result/);
});

test("failed safety request remains uncertain and is never retried automatically", async () => {
  const page = browser();
  await page.respond(0, state());
  await page.click("stop-motion");
  await page.reject(1);
  await page.advance(2000);
  assert.equal(posts(page).length, 1);
  assert.match(page.get("notice").textContent, /outcome is not confirmed/);
});

test("null flags are unknown, and stale snapshots discard previous healthy indicators", async () => {
  const page = browser();
  await page.respond(0, state({ services: [service({ connected: null, busy: null, locked: null, demo: null })] }));
  assert.match(page.get("services").textContent, /ConnectionUnknownActivityUnknownLockUnknown/);
  await nextState(page, state({ services: [service()] }));
  assert.match(page.get("services").textContent, /Connected/);
  await nextState(page, state({ services: [service({ phase: "stale", error: "Last read failed" })] }));
  assert.doesNotMatch(page.get("services").textContent, /Connected|Unlocked|None reported/);
  assert.match(page.get("services").textContent, /Stale/);
  assert.equal(descendants(page.get("services")).some(item => item.className.includes("good")), false);
});

test("poll failure makes last results stale and disables run controls without disabling safety", async () => {
  const page = browser();
  await control(page);
  await page.advance(1000);
  const getIndex = page.requests.findLastIndex(item => item.options.method !== "POST");
  await page.reject(getIndex);
  assert.match(page.get("connection").textContent, /unavailable/);
  assert.equal(page.get("hold-run").disabled, true);
  assert.equal(page.get("stop-motion").disabled, false);
  assert.doesNotMatch(page.get("services").textContent, /Connected/);
});

test("fresh dashboard response with stale run summary cannot retain control", async () => {
  const page = browser();
  await control(page);
  await page.change("manual-completion", true);
  await page.change("resume-note", "A previous check.");
  await nextState(page, owned({ run: run(), orchestrator: { ...owned().orchestrator, stale: true } }));
  assert.equal(page.get("hold-run").disabled, true);
  assert.equal(page.get("acquire-control").disabled, true);
  assert.equal(page.get("stop-motion").disabled, false);
  assert.match(page.get("run-status").textContent, /Unknown/);
  assert.equal(page.get("resume-form").hidden, true);
  assert.doesNotMatch(page.get("resume-guidance").textContent, /complete it manually/);
  assert.equal(page.get("manual-completion").checked, false);
  assert.equal(page.get("resume-note").value, "");
  const count = posts(page).length;
  await page.advance(1500);
  assert.equal(posts(page).length, count);
});

test("cached pre-acquire summary cannot revoke a newly confirmed lease or prevent renewal", async () => {
  const page = browser();
  await control(page, state({ run: run() }));
  assert.equal(page.get("hold-run").disabled, false);
  assert.match(page.get("lease-status").textContent, /This page holds control/);
  await page.advance(500);
  assert.equal(page.requests[3].url, "/lab/api/renew");
  await page.respond(3, { lease: { holder: client, remaining_s: 3 } });
  assert.equal(page.get("hold-run").disabled, false);
});

test("cached owned summary cannot extend a lease while authoritative renewal is unanswered", async () => {
  const page = browser();
  await control(page);
  for (let i = 0; i < 3; i++) await nextState(page, owned({ run: run() }));
  await page.click("hold-run");
  assert.equal(page.get("hold-run").disabled, true);
  assert.deepEqual(posts(page).map(item => item.url), ["/lab/api/heartbeat", "/lab/api/renew"]);
});

test("untrusted labels, faults, run details and outcomes remain text", async () => {
  const page = browser();
  const injection = '<img src=x onerror="throw 1">';
  await page.respond(0, state({
    services: [service({ label: injection, fault: injection })], run: run({ detail: injection }),
    operations: [{ action: injection, results: { robot: { status: "failed", message: injection } } }],
  }));
  assert.match(page.get("services").textContent, /<img/);
  assert.match(page.get("run-detail").textContent, /<img/);
  assert.match(page.get("operations").textContent, /<img/);
  assert.equal(descendants(page.get("services")).some(element => element.tagName === "IMG"), false);
});

test("camera tiles keep unavailable links visible and reject unsafe URLs", async () => {
  const page = browser();
  await page.respond(0, state({ cameras: [
    { id: "camera", url: "javascript:alert(1)" },
    { id: "thermal", url: "http://user:secret@example.invalid/" },
    { id: "microscope", url: "https://camera.example.invalid/preview/" },
  ] }));
  assert.equal(page.get("cameras").children.length, 3);
  const links = descendants(page.get("cameras")).filter(item => item.tagName === "A");
  assert.equal(links.length, 1);
  assert.equal(links[0].href, "https://camera.example.invalid/preview/");
  assert.equal(links[0].rel, "noopener noreferrer");
  assert.equal(links[0].target, "_blank");
  assert.equal(page.get("cameras").textContent.split("Link unavailable").length - 1, 2);
});

test("polls and observed ownership never acquire control automatically", async () => {
  const page = browser();
  await page.respond(0, owned({ run: run() }));
  await nextState(page, owned({ run: run() }));
  assert.equal(posts(page).length, 0);
  assert.equal(page.get("hold-run").disabled, true);
  assert.equal(page.get("acquire-control").disabled, false);
});

test("strict renewal failure latches loss and never falls back to heartbeat", async () => {
  const page = browser();
  await control(page);
  assert.equal(page.get("hold-run").disabled, false);
  await page.advance(500);
  assert.equal(page.requests[3].url, "/lab/api/renew");
  await page.respond(3, { error: "Lease expired" }, 409);
  assert.equal(page.get("hold-run").disabled, true);
  await page.advance(500);
  await page.respond(4, owned({ run: run() }));
  await page.advance(2000);
  assert.deepEqual(posts(page).map(item => item.url), ["/lab/api/heartbeat", "/lab/api/renew"]);
  assert.equal(page.get("hold-run").disabled, true);
});

test("release stops renewal even when delivery is uncertain", async () => {
  const page = browser();
  await control(page);
  await page.click("release-control");
  await page.reject(3);
  await page.advance(2000);
  assert.deepEqual(posts(page).map(item => item.url), ["/lab/api/heartbeat", "/lab/api/release"]);
  assert.equal(page.get("hold-run").disabled, true);
});

for (const kind of ["pending", "started", null]) {
  test(`resume guidance is faithful for ${kind ?? "unknown"} durable state`, async () => {
    const page = browser();
    await control(page, owned({ run: run({ resume_kind: kind, steps: [{ position: 0, skill: "robot.move", status: kind === "pending" ? "pending" : "failed" }] }) }));
    await page.change("manual-completion", true);
    await page.change("resume-note", "Checked the held step.");
    const guidance = page.get("resume-guidance").textContent;
    if (kind === "pending") {
      assert.match(guidance, /has not run/);
      assert.match(guidance, /Do not perform it manually/);
      assert.match(guidance, /will run after resume/);
      assert.doesNotMatch(page.get("completion-label").textContent, /completed/);
    } else if (kind === "started") {
      assert.match(guidance, /will not repeat/);
      assert.match(page.get("completion-label").textContent, /verified the outcome/);
    } else assert.match(guidance, /unknown/);
    assert.equal(page.get("resume-run").disabled, kind === null);
    await page.submit("resume-form");
    if (kind === null) assert.equal(posts(page).length, 1);
    else {
      const last = posts(page).at(-1);
      assert.equal(last.url, "/lab/api/runs/run%2Fone/resume");
      assert.deepEqual(JSON.parse(last.options.body), { manually_completed: true, note: "Checked the held step.", physically_clear: false });
    }
  });
}

test("resume needs acknowledgement and a note; physical clearance is a separate explicit declaration", async () => {
  const page = browser();
  await control(page);
  await page.submit("resume-form");
  await page.change("manual-completion", true);
  await page.change("resume-note", "   ");
  await page.submit("resume-form");
  assert.equal(posts(page).length, 1);
  await page.change("resume-note", "Manually verified completion.");
  await page.change("physical-clearance", true);
  await page.submit("resume-form");
  const request = posts(page).at(-1);
  assert.equal(JSON.parse(request.options.body).physically_clear, true);
  await page.reject(page.requests.indexOf(request));
  await page.advance(1500);
  assert.equal(posts(page).filter(item => item.url.endsWith("/resume")).length, 1);
  assert.match(page.get("notice").textContent, /will not repeat automatically/);
});

for (const flags of [{ closed: true }, { draining: true }, { settled: false }]) {
  test(`resume refused for ${JSON.stringify(flags)}`, async () => {
    const page = browser();
    await control(page, owned({ run: run(flags) }));
    await page.change("manual-completion", true);
    await page.change("resume-note", "Checked.");
    await page.submit("resume-form");
    assert.equal(page.get("resume-run").disabled, true);
    assert.equal(posts(page).length, 1);
  });
}

test("run/held-step changes clear old manual declarations", async () => {
  const page = browser();
  await control(page);
  await page.change("manual-completion", true);
  await page.change("resume-note", "Old step completion.");
  await page.change("physical-clearance", true);
  await nextState(page, owned({ run: run({ step_position: 1, resume_kind: "pending" }) }));
  assert.equal(page.get("manual-completion").checked, false);
  assert.equal(page.get("physical-clearance").checked, false);
  assert.equal(page.get("resume-note").value, "");
});

test("hold and abort require ownership and send the fixed prefixed routes", async () => {
  const page = browser();
  await control(page);
  await page.click("hold-run");
  assert.equal(posts(page).at(-1).url, "/lab/api/dashboard/hold");
  await page.respond(3, { command_id: "hold1" }, 202);
  await page.respond(4, owned({ run: run() }));
  await page.click("abort-run");
  assert.equal(posts(page).at(-1).url, "/lab/api/runs/run%2Fone/abort");
});

test("run request without a command ID remains unconfirmed and does not repeat", async () => {
  const page = browser();
  await control(page);
  await page.click("hold-run");
  await page.respond(3, {}, 200);
  assert.match(page.get("notice").textContent, /not confirmed/);
  assert.match(page.get("notice").textContent, /did not return a command ID/);
  await page.advance(2000);
  assert.equal(posts(page).filter(item => item.url.endsWith("/hold")).length, 1);
});

test("late acquire reply cannot restore control after access settings change", async () => {
  const page = browser();
  await page.respond(0, state({ run: run() }));
  await page.click("acquire-control");
  page.get("access-token").value = "replacement access token";
  await page.submit("access-form");
  await page.respond(2, owned({ run: run() }));
  await page.respond(1, { lease: { holder: client, remaining_s: 3 } });
  await page.advance(1000);
  assert.equal(page.get("hold-run").disabled, true);
  assert.equal(posts(page).filter(item => item.url.endsWith("/renew")).length, 0);
});

test("forget clears password and rejects an old status response", async () => {
  const page = browser();
  page.get("access-token").value = "secret token";
  await page.submit("access-form");
  await page.respond(0, state({ run: run({ name: "Old run" }) }));
  await page.advance(0);
  await page.click("forget-token");
  await page.respond(1, state({ run: run({ name: "Another old run" }) }));
  assert.equal(page.get("access-token").value, "");
  assert.doesNotMatch(page.get("run-name").textContent, /Old|Another/);
  await page.advance(0);
  assert.equal(page.requests.at(-1).options.headers.Authorization, undefined);
});

test("status polls never overlap and a timeout does not create a mutation", async () => {
  const page = browser();
  await page.advance(4000);
  assert.equal(page.requests.length, 1);
  await page.advance(500);
  assert.match(page.get("connection").textContent, /unavailable/);
  await page.advance(1000);
  assert.equal(page.requests.length, 2);
  assert.equal(posts(page).length, 0);
});

test("page exit stops timers without a hidden acquire, release, or mutation", async () => {
  const page = browser();
  await control(page);
  await page.hide();
  await page.advance(10000);
  assert.equal(posts(page).length, 1);
  assert.equal(page.get("hold-run").disabled, true);
});
