// Check the keyboard rules of the control panel page with a fake browser.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const html = readFileSync(new URL("../apps/control_panel/index.html", import.meta.url), "utf8");
const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
const script = scripts[0]?.[2] ?? "";
const flush = () => new Promise(resolve => setImmediate(resolve));
const KEY_VALUES = {
  Escape: "Escape", ShiftLeft: "Shift", ShiftRight: "Shift", ControlLeft: "Control",
  ControlRight: "Control", AltLeft: "Alt", AltRight: "Alt", MetaLeft: "Meta", MetaRight: "Meta",
};
// The browser fires these at the window only. Other page events bubble through the document.
const WINDOW_EVENTS = new Set(["blur", "focus", "pagehide"]);

class FakeElement {
  constructor(id = "") {
    this.id = id;
    this.value = "";
    this.disabled = false;
    this.hidden = false;
    this.textContent = "";
    this.innerHTML = "";
    this.title = "";
    this.validity = { valid: true };
    this.listeners = new Map();
    const names = new Set();
    this.classList = {
      add: name => names.add(name),
      remove: name => names.delete(name),
      contains: name => names.has(name),
      toggle(name, force) {
        const on = force === undefined ? !names.has(name) : Boolean(force);
        if (on) names.add(name); else names.delete(name);
        return on;
      },
    };
  }
  addEventListener(type, listener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  blur() {}
  focus() {}
}

class FakeInput extends FakeElement {}

function browser() {
  let now = 0, timerId = 0, version = 0, focused = true;
  const timers = new Map(), requests = [], elements = new Map();
  const listeners = { window: new Map(), document: new Map() };
  const body = new FakeElement("body");
  const element = id => {
    if (!elements.has(id)) {
      const node = /^(speed|token)$|(Target|Range)$/.test(id) ? new FakeInput(id) : new FakeElement(id);
      if (id === "speed") node.value = "1";
      if (id === "keyboard") node.hidden = node.disabled = true;
      elements.set(id, node);
    }
    return elements.get(id);
  };
  const listen = map => (type, listener) => map.set(type, [...(map.get(type) ?? []), listener]);
  const document = {
    visibilityState: "visible",
    hasFocus: () => focused,
    getElementById: element,
    addEventListener: listen(listeners.document),
  };
  const window = { addEventListener: listen(listeners.window) };
  const dispatch = (type, event) => {
    const path = WINDOW_EVENTS.has(type) ? [listeners.window] : [listeners.document, listeners.window];
    for (const map of path) for (const listener of map.get(type) ?? []) listener(event);
  };
  const schedule = (callback, delay, every) => {
    const id = ++timerId;
    timers.set(id, { at: now + Math.max(0, Number(delay) || 0), callback, every });
    return id;
  };
  const context = vm.createContext({
    window, document, console,
    HTMLInputElement: FakeInput,
    crypto: { getRandomValues: array => array.fill(171) },
    sessionStorage: { getItem: () => null, setItem() {} },
    AbortSignal: { timeout: milliseconds => ({ timeout: milliseconds, aborted: false, addEventListener() {} }) },
    setTimeout: (callback, delay) => schedule(callback, delay, 0),
    setInterval: (callback, delay) => schedule(callback, delay, Math.max(1, Number(delay) || 0)),
    clearTimeout: id => timers.delete(id),
    clearInterval: id => timers.delete(id),
    fetch(path, options = {}) {
      let settle;
      const promise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
      requests.push({
        path, options, at: now, settled: false,
        body: options.body === undefined ? undefined : JSON.parse(options.body),
        timeout: options.signal?.timeout,
        respond(data, status) {
          this.settled = true;
          settle.resolve({ status, ok: status >= 200 && status < 300, json: async () => data });
        },
        fail(error) {
          this.settled = true;
          settle.reject(error);
        },
      });
      return promise;
    },
  });
  vm.runInContext(script, context);

  const page = {
    requests, body, $: element,
    get clientId() { return requests[0].options.headers["X-MG400-Client"]; },
    // A robot snapshot for this page: owner, syncing, fresh, enabled (mode 5), keys OFF.
    snapshot(overrides = {}) {
      return {
        mode: "robot", robot_host: "robot.test", controller_id: page.clientId,
        syncing: true, feedback_fresh: true, enabled: true, robot_mode: 5, robot_error: false,
        locked: false, busy: false, stop_unconfirmed: false, message: "Ready.",
        actual: [250, 0, 100, 0, 500], joints: [0, 0, 0, 0], speed_factor: 1,
        gripper_available: true, gripper_can_initialize: false, gripper_busy: false, gripper_message: "",
        keyboard_enabled: false, keyboard_arming: false, keyboard_active_key: null,
        keyboard_speed_percent: 1, keyboard_disarm_reason: null,
        version: ++version, ...overrides,
      };
    },
    async respond(request, data, status = 200) {
      assert.equal(request.settled, false, `${request.path} was already settled`);
      request.respond(data, status);
      await flush();
    },
    async fail(request, error = new Error("Network down")) {
      assert.equal(request.settled, false, `${request.path} was already settled`);
      request.fail(error);
      await flush();
    },
    async advance(milliseconds) {
      const end = now + milliseconds;
      await flush();
      for (;;) {
        let next = null;
        for (const entry of timers) if (entry[1].at <= end && (!next || entry[1].at < next[1].at)) next = entry;
        if (!next) break;
        const [id, timer] = next;
        now = timer.at;
        if (timer.every) timer.at += timer.every; else timers.delete(id);
        timer.callback();
        await flush();
      }
      now = end;
      await flush();
    },
    async key(type, code, init = {}) {
      const key = KEY_VALUES[code] ?? (code.startsWith("Key") ? code.slice(3).toLowerCase() : code);
      const event = {
        type, code, key, repeat: false, isComposing: false, ctrlKey: false, metaKey: false,
        altKey: false, shiftKey: false, target: body, defaultPrevented: false, ...init,
      };
      event.preventDefault = () => { event.defaultPrevented = true; };
      dispatch(type, event);
      await flush();
      return event;
    },
    async fire(type, init = {}) {
      if (type === "blur") focused = false;
      if (type === "focus") focused = true;
      const event = { type, target: WINDOW_EVENTS.has(type) ? window : body, defaultPrevented: false, ...init };
      event.preventDefault = () => { event.defaultPrevented = true; };
      dispatch(type, event);
      await flush();
      return event;
    },
    async click(id) {
      const target = element(id);
      // A browser does not dispatch clicks to a disabled button.
      assert.equal(target.disabled, false, `${id} is disabled`);
      for (const listener of target.listeners.get("click") ?? []) listener({ type: "click", target, preventDefault() {} });
      await flush();
    },
    setFocus(value) { focused = value; },
    setVisibility(value) { document.visibilityState = value; },
    evaluate: code => vm.runInContext(code, context),
    keyUpdates: () => requests.filter(request => request.path === "api/keys"),
    commands: action => requests.filter(
      request => request.path === "api/command" && (action === undefined || request.body?.action === action),
    ),
    arms: () => page.commands("keyboard").filter(request => request.body.enabled === true),
    disarms: () => page.commands("keyboard").filter(request => request.body.enabled === false),
    lastPoll() {
      const polls = requests.filter(request => request.path === "api/state" && !request.settled);
      assert.equal(polls.length, 1, "expected one state poll in flight");
      return polls[0];
    },
  };
  return page;
}

async function ready(overrides = {}) {
  const page = browser();
  await page.respond(page.lastPoll(), page.snapshot(overrides));
  return page;
}

// Keys ON for this page, W held, and one renewal sent.
async function holding() {
  const page = await ready({ keyboard_enabled: true });
  await page.key("keydown", "KeyW");
  await page.advance(100);
  assert.deepEqual(page.keyUpdates().map(update => update.body.held), [["W"], ["W"]]);
  return page;
}

// The same, while a Home command keeps the page busy.
async function busyHolding() {
  const page = await ready({ keyboard_enabled: true });
  await page.click("home");
  assert.equal(page.commands("home").length, 1);
  assert.equal(page.$("home").disabled, true, "the page must be busy with Home");
  await page.key("keydown", "KeyW");
  await page.advance(100);
  assert.deepEqual(page.keyUpdates().map(update => update.body.held), [["W"], ["W"]]);
  return page;
}

// Arm, disarm and keys share one server sequence, so the page must send increasing numbers.
function assertSequenced(page) {
  const numbers = page.requests
    .filter(request => request.body && Object.hasOwn(request.body, "seq"))
    .map(request => request.body.seq);
  numbers.forEach((seq, index) => {
    assert.ok(Number.isInteger(seq) && seq >= 0 && seq <= 2 ** 53, `invalid seq ${seq}`);
    if (index) assert.ok(seq > numbers[index - 1], `seq ${seq} does not follow ${numbers[index - 1]}`);
  });
}

function assertDisarm(request, reason) {
  assert.ok(request, `no ${reason} disarm was sent`);
  assert.deepEqual(request.body, { action: "keyboard", enabled: false, reason, seq: request.body.seq });
}

test("the page has one inline script that starts by reading state only", async () => {
  assert.equal(scripts.length, 1);
  assert.doesNotMatch(scripts[0][1], /\bsrc=/);
  const page = browser();
  assert.deepEqual(page.requests.map(request => request.path), ["api/state"]);
  await page.respond(page.lastPoll(), page.snapshot());
  await page.advance(1000);
  assert.equal(page.keyUpdates().length, 0);
  assert.equal(page.commands().length, 0);
});

test("key updates go out at once with a strict body and a 1 s timeout", async () => {
  const page = await ready({ keyboard_enabled: true });
  const first = await page.key("keydown", "KeyW");
  assert.equal(first.defaultPrevented, true);
  await page.key("keydown", "KeyZ");
  await page.key("keyup", "KeyW");
  await page.key("keyup", "KeyZ");
  assert.deepEqual(page.keyUpdates().map(update => [...update.body.held].sort()), [["W"], ["W", "Z"], ["Z"], []]);
  for (const update of page.keyUpdates()) {
    assert.deepEqual(Object.keys(update.body).sort(), ["held", "seq"]);
    assert.equal(update.timeout, 1000);
    assert.equal(update.options.method, "POST");
    assert.equal(update.options.headers["X-MG400-Panel"], "1");
    assert.equal(update.options.headers["X-MG400-Client"], page.clientId);
  }
  assertSequenced(page);
});

test("keyup removes a held key and posts at once from an input or without focus", async () => {
  const page = await ready({ keyboard_enabled: true });
  await page.key("keydown", "KeyW");
  await page.key("keydown", "KeyA");
  page.setFocus(false);
  await page.key("keyup", "KeyA", { target: page.$("xTarget") });
  await page.key("keyup", "KeyW", { target: page.$("speed") });
  assert.deepEqual(page.keyUpdates().map(update => [...update.body.held].sort()), [["W"], ["A", "W"], ["W"], []]);
  assertSequenced(page);
});

test("keyup is not gated on the keyboard view", async () => {
  const page = await ready();
  // A rendered OFF view already clears held keys, so no page event can reach this state.
  // Put a held key in place directly to check that the keyup listener has no view gate.
  page.evaluate("held.add('D')");
  await page.key("keyup", "KeyD");
  assert.deepEqual(page.keyUpdates().map(update => update.body.held), [[]]);
});

const BUSY_DROPS = [
  ["window blur", "focus", page => page.fire("blur")],
  ["hidden page", "focus", page => { page.setVisibility("hidden"); return page.fire("visibilitychange"); }],
  ["context menu", "focus", page => page.fire("contextmenu")],
  ["drag start", "focus", page => page.fire("dragstart")],
  ["Shift+W keydown", "modifier", page => page.key("keydown", "KeyW", { shiftKey: true })],
  ["Ctrl+D keydown", "modifier", page => page.key("keydown", "KeyD", { ctrlKey: true })],
  ["Alt+A keydown", "modifier", page => page.key("keydown", "KeyA", { altKey: true })],
  ["Meta+1 keydown", "modifier", page => page.key("keydown", "Digit1", { metaKey: true })],
  ["ShiftLeft keydown", "modifier", page => page.key("keydown", "ShiftLeft", { shiftKey: true })],
  ["MetaLeft keydown without a flag", "modifier", page => page.key("keydown", "MetaLeft")],
  ["ControlRight keydown without a flag", "modifier", page => page.key("keydown", "ControlRight")],
  ["AltLeft keydown without a flag", "modifier", page => page.key("keydown", "AltLeft")],
];

for (const [name, reason, trigger] of BUSY_DROPS) {
  test(`${name} drops held keys and turns keys off past a busy command`, async () => {
    const page = await busyHolding();
    const before = page.keyUpdates().length;
    await trigger(page);
    assert.deepEqual(page.keyUpdates().slice(before).map(update => update.body.held), [[]]);
    assert.equal(page.disarms().length, 1);
    assertDisarm(page.disarms()[0], reason);
    await page.advance(500);
    assert.equal(page.keyUpdates().length, before + 1, "renewals continued");
    await page.key("keyup", "KeyW");
    assert.equal(page.keyUpdates().length, before + 1, "W was still held");
    assertSequenced(page);
  });
}

for (const [field, reason] of [["xTarget", "edit"], ["gripTarget", "edit"], ["speed", "speed"]]) {
  test(`focus in ${field} drops held keys and turns keys off for ${reason}`, async () => {
    const page = await holding();
    await page.fire("focusin", { target: page.$(field) });
    assert.deepEqual(page.keyUpdates().slice(2).map(update => update.body.held), [[]]);
    assert.equal(page.disarms().length, 1);
    assertDisarm(page.disarms()[0], reason);
    await page.advance(500);
    await page.key("keyup", "KeyW");
    assert.equal(page.keyUpdates().length, 3);
    assertSequenced(page);
  });
}

for (const [name, reason, trigger] of [
  ["window blur", "focus", page => page.fire("blur")],
  ["focus in Speed %", "speed", page => page.fire("focusin", { target: page.$("speed") })],
  ["Shift keydown", "modifier", page => page.key("keydown", "ShiftLeft", { shiftKey: true })],
]) {
  test(`a ${name} while the arm request is in flight still turns keys off`, async () => {
    const page = await ready();
    await page.click("keyboard");
    const [arm] = page.arms();
    assert.ok(arm && !arm.settled);
    await trigger(page);
    await page.respond(arm, page.snapshot({ keyboard_arming: true, busy: true }));
    await page.advance(500);
    await page.respond(page.lastPoll(), page.snapshot({ keyboard_enabled: true }));
    const disarm = page.disarms().find(request => request.body.reason === reason);
    assertDisarm(disarm, reason);
    assert.ok(disarm.body.seq > arm.body.seq, "the disarm must be newer than the arm");
  });
}

test("Esc disables at once, before the modifier rule and past a busy command", async () => {
  const page = await busyHolding();
  const before = page.requests.length;
  const event = await page.key("keydown", "Escape", { shiftKey: true, ctrlKey: true });
  assert.equal(event.defaultPrevented, true);
  const sent = page.requests.slice(before);
  assert.deepEqual(sent.filter(request => request.path === "api/keys").map(request => request.body.held), [[]]);
  assert.deepEqual(sent.filter(request => request.path === "api/command").map(request => request.body), [{ action: "disable" }]);
  await page.advance(500);
  assert.equal(page.keyUpdates().length, 3, "renewals continued");
  assertSequenced(page);
});

test("Esc disables while keys are CHECKING", async () => {
  const page = await ready({ keyboard_arming: true, busy: true });
  const event = await page.key("keydown", "Escape", { metaKey: true });
  assert.equal(event.defaultPrevented, true);
  assert.deepEqual(page.commands().map(request => request.body), [{ action: "disable" }]);
});

test("Esc is left alone while keys are OFF or another page has control", async () => {
  for (const overrides of [{}, { keyboard_enabled: true, controller_id: "another-page" }]) {
    const page = await ready(overrides);
    const event = await page.key("keydown", "Escape");
    assert.equal(event.defaultPrevented, false);
    assert.equal(page.commands().length, 0);
    assert.equal(page.keyUpdates().length, 0);
  }
});

for (const [name, lose] of [
  ["focus", page => page.setFocus(false)],
  ["visibility", page => page.setVisibility("hidden")],
]) {
  test(`renewals stop at the next tick once the page has no ${name}`, async () => {
    const page = await holding();
    lose(page); // No blur or visibilitychange event arrives.
    await page.advance(100);
    assert.deepEqual(page.keyUpdates().slice(2).map(update => update.body.held), [[]]);
    assert.equal(page.disarms().length, 1);
    assertDisarm(page.disarms()[0], "focus");
    await page.advance(500);
    assert.equal(page.keyUpdates().length, 3, "renewals continued");
  });
}

test("at most two renewals are in flight; key changes and Stop go out at once", async () => {
  const page = await ready({ keyboard_enabled: true });
  await page.key("keydown", "KeyW");
  assert.equal(page.keyUpdates().length, 1);
  await page.advance(1000);
  assert.equal(page.keyUpdates().length, 3, "the key change and two renewals");
  await page.respond(page.keyUpdates()[1], page.snapshot({ keyboard_enabled: true }));
  await page.advance(100);
  assert.equal(page.keyUpdates().length, 4);
  await page.advance(500);
  assert.equal(page.keyUpdates().length, 4);
  // The key change does not use a renewal slot.
  await page.respond(page.keyUpdates()[0], page.snapshot({ keyboard_enabled: true }));
  await page.advance(500);
  assert.equal(page.keyUpdates().length, 4);
  await page.key("keydown", "KeyA");
  assert.equal(page.keyUpdates().length, 5);
  assert.deepEqual([...page.keyUpdates()[4].body.held].sort(), ["A", "W"]);
  await page.click("stop");
  assert.equal(page.commands("stop").length, 1);
  assert.ok(page.keyUpdates().slice(1, 4).every(update => update.body.held.join() === "W"));
  assertSequenced(page);
});

for (const [name, fail] of [
  ["a network error", (page, request) => page.fail(request, new Error("Failed to fetch"))],
  ["its timeout", (page, request) => {
    const error = Object.assign(new Error("The operation timed out."), { name: "TimeoutError" });
    return page.fail(request, error);
  }],
  ["an HTTP error", (page, request) => page.respond(request, { error: "The panel is closing" }, 409)],
]) {
  test(`a key update that fails with ${name} clears held keys and stops renewals`, async () => {
    const page = await holding();
    await fail(page, page.keyUpdates()[1]);
    await page.advance(500);
    assert.equal(page.keyUpdates().length, 2, "renewals continued");
    await page.key("keyup", "KeyW");
    assert.equal(page.keyUpdates().length, 2, "W was still held");
    assert.equal(page.disarms().length, 0);
    // A new press starts over.
    await page.key("keydown", "KeyW");
    assert.deepEqual(page.keyUpdates().at(-1).body.held, ["W"]);
  });
}

test("a failed key change clears held keys like a failed renewal", async () => {
  const page = await ready({ keyboard_enabled: true });
  await page.key("keydown", "KeyW");
  await page.fail(page.keyUpdates()[0]);
  await page.advance(500);
  await page.key("keyup", "KeyW");
  assert.equal(page.keyUpdates().length, 1);
});

for (const [name, overrides] of [
  ["OFF", {}],
  ["CHECKING", { keyboard_arming: true, busy: true }],
  ["another page's", { keyboard_enabled: true, controller_id: "another-page" }],
]) {
  test(`a rendered ${name} snapshot clears held keys and stops renewals`, async () => {
    const page = await holding();
    await page.respond(page.keyUpdates()[1], page.snapshot(overrides));
    const count = page.keyUpdates().length;
    await page.advance(500);
    assert.equal(page.keyUpdates().length, count, "renewals continued");
    await page.key("keyup", "KeyW");
    assert.equal(page.keyUpdates().length, count, "W was still held");
    assert.equal(page.disarms().length, 0);
  });
}

test("a requested disarm is re-sent on each rendered ON or CHECKING snapshot until one shows OFF", async () => {
  const page = await ready({ keyboard_enabled: true });
  const inFlight = () => page.disarms().filter(request => !request.settled);
  await page.fire("blur");
  assert.equal(page.disarms().length, 1);
  // One disarm in flight at a time.
  await page.advance(500);
  await page.respond(page.lastPoll(), page.snapshot({ keyboard_enabled: true }));
  assert.equal(page.disarms().length, 1);
  await page.fail(page.disarms()[0]);
  assert.ok(inFlight().length <= 1);
  await page.advance(500);
  await page.respond(page.lastPoll(), page.snapshot({ keyboard_arming: true, busy: true }));
  assert.equal(inFlight().length, 1, "a CHECKING snapshot must re-send the disarm");
  // The server can answer with ON when the disarm was stale.
  await page.respond(inFlight()[0], page.snapshot({ keyboard_enabled: true }));
  assert.ok(inFlight().length <= 1);
  await page.advance(500);
  await page.respond(page.lastPoll(), page.snapshot({ keyboard_enabled: true }));
  assert.equal(inFlight().length, 1, "an ON snapshot must re-send the disarm");
  assert.ok(page.disarms().length >= 3);
  assert.ok(page.disarms().every(request => request.body.reason === "focus"));
  await page.respond(inFlight()[0], page.snapshot());
  const total = page.disarms().length;
  // OFF ends the retries, and a new arm is not turned off.
  await page.fire("focus");
  await page.click("keyboard");
  await page.respond(page.arms()[0], page.snapshot({ keyboard_arming: true, busy: true }));
  await page.advance(500);
  await page.respond(page.lastPoll(), page.snapshot({ keyboard_enabled: true }));
  assert.equal(page.disarms().length, total);
  assertSequenced(page);
});

test("an older snapshot is never rendered over a newer one", async () => {
  const page = await ready({ keyboard_enabled: true, message: "Keys on." });
  await page.key("keydown", "KeyW");
  const [change] = page.keyUpdates();
  const older = page.snapshot({ message: "Older: keys off." });
  await page.advance(500);
  await page.respond(page.lastPoll(), page.snapshot({ keyboard_enabled: true, message: "Newer: keys on." }));
  await page.respond(change, older);
  assert.equal(page.$("status").textContent, "Newer: keys on.");
  assert.equal(page.$("keyboard").textContent, "Keyboard: ON 1%");
  // The older OFF snapshot did not clear the held key.
  const count = page.keyUpdates().length;
  await page.key("keyup", "KeyW");
  assert.deepEqual(page.keyUpdates().slice(count).map(update => update.body.held), [[]]);
  // A command reply older than the rendered snapshot is not rendered either.
  await page.click("home");
  const stale = page.snapshot({ message: "Stale command reply.", keyboard_enabled: false });
  await page.advance(500);
  await page.respond(page.lastPoll(), page.snapshot({ keyboard_enabled: true, message: "Newest." }));
  await page.respond(page.commands("home")[0], stale);
  assert.equal(page.$("status").textContent, "Newest.");
  assert.equal(page.$("keyboard").textContent, "Keyboard: ON 1%");
});

test("the Keyboard label shows the session speed and the Speed field locks while CHECKING or busy", async () => {
  const page = await ready({ speed_factor: 25 });
  assert.equal(page.$("keyboard").hidden, false);
  assert.equal(page.$("keyboard").textContent, "Keyboard: OFF");
  assert.equal(page.$("speed").disabled, false);
  assert.match(`${page.$("speedField").title} ${page.$("speed").title}`, /\b25\b/);
  const show = async overrides => {
    await page.advance(500);
    await page.respond(page.lastPoll(), page.snapshot(overrides));
  };
  await show({ keyboard_arming: true, keyboard_speed_percent: 37, busy: true });
  assert.equal(page.$("keyboard").textContent, "Keyboard: CHECKING…");
  assert.equal(page.$("speed").disabled, true);
  await show({ keyboard_arming: true, keyboard_speed_percent: 37 });
  assert.equal(page.$("speed").disabled, true, "CHECKING alone must lock the Speed field");
  await show({ keyboard_enabled: true, keyboard_speed_percent: 37 });
  assert.equal(page.$("keyboard").textContent, "Keyboard: ON 37%");
  await show({ keyboard_enabled: true, keyboard_speed_percent: 100, busy: true, keyboard_active_key: "W" });
  assert.equal(page.$("keyboard").textContent, "Keyboard: ON 100%");
  assert.equal(page.$("speed").disabled, true);
  await show({ busy: true });
  assert.equal(page.$("keyboard").textContent, "Keyboard: OFF");
  assert.equal(page.$("speed").disabled, true);
});

test("the Keyboard button turns keys off while CHECKING or ON whatever busy, locked or fresh say", async () => {
  for (const view of [{ keyboard_arming: true }, { keyboard_enabled: true }]) {
    const page = await ready({
      ...view, busy: true, locked: true, feedback_fresh: false, robot_mode: 11, gripper_busy: true,
    });
    await page.click("keyboard");
    assert.equal(page.disarms().length, 1);
    assertDisarm(page.disarms()[0], "user");
    assert.equal(page.arms().length, 0);
  }
});

test("the Keyboard button arms only when the arm preconditions look satisfied", async () => {
  const page = await ready();
  assert.equal(page.$("keyboard").disabled, false);
  for (const blocked of [
    { busy: true }, { locked: true }, { feedback_fresh: false }, { syncing: false }, { robot_mode: 4 },
    { robot_mode: 7 }, { gripper_busy: true }, { controller_id: "another-page" },
  ]) {
    await page.advance(500);
    await page.respond(page.lastPoll(), page.snapshot(blocked));
    assert.equal(page.$("keyboard").disabled, true, JSON.stringify(blocked));
  }
  await page.advance(500);
  await page.respond(page.lastPoll(), page.snapshot());
  page.$("speed").value = "37";
  await page.click("keyboard");
  assert.equal(page.arms().length, 1);
  const [arm] = page.arms();
  assert.deepEqual(arm.body, { action: "keyboard", enabled: true, speed: 37, seq: arm.body.seq });
});

test("a key held while keys are CHECKING needs a new press after keys turn ON", async () => {
  const page = await ready();
  page.$("speed").value = "5";
  await page.click("keyboard");
  const [arm] = page.arms();
  assert.deepEqual(arm.body, { action: "keyboard", enabled: true, speed: 5, seq: arm.body.seq });
  await page.respond(arm, page.snapshot({ keyboard_arming: true, keyboard_speed_percent: 5, busy: true }));
  await page.key("keydown", "KeyW");
  for (let i = 0; i < 3; i++) await page.key("keydown", "KeyW", { repeat: true });
  await page.advance(500);
  await page.respond(page.lastPoll(), page.snapshot({ keyboard_enabled: true, keyboard_speed_percent: 5 }));
  assert.equal(page.$("keyboard").textContent, "Keyboard: ON 5%");
  for (let i = 0; i < 3; i++) await page.key("keydown", "KeyW", { repeat: true });
  await page.advance(300);
  assert.ok(page.keyUpdates().every(update => !update.body.held.includes("W")), "the held W started a hold");
  await page.key("keyup", "KeyW");
  await page.key("keydown", "KeyW");
  assert.deepEqual(page.keyUpdates().at(-1).body.held, ["W"]);
  assertSequenced(page);
});

test("read-only mode hides the Keyboard button and sends no key input", async () => {
  const page = browser();
  await page.respond(page.lastPoll(), {
    mode: "read-only", robot_host: "robot.test", enabled: null, syncing: false,
    actual: [null, null, null, null, null], joints: null, robot_mode: null, robot_error: null,
    feedback_fresh: false, message: "Read-only mode.",
  });
  assert.equal(page.$("keyboard").hidden, true);
  for (const code of ["KeyW", "Escape"]) {
    await page.key("keydown", code);
    await page.key("keyup", code);
  }
  await page.fire("blur");
  assert.equal(page.keyUpdates().length, 0);
  assert.equal(page.commands().length, 0);
});
