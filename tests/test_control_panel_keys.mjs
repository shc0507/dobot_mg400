// Check the keyboard and connection rules of the control panel page with a fake browser.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test, { afterEach } from "node:test";
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
const WINDOW_EVENTS = new Set(["blur", "focus", "pagehide", "pageshow"]);
// crypto.getRandomValues fills every byte with 171.
const CLIENT_ID = "ab".repeat(16);
const LOST = "Connection lost. Reconnecting…";
const REFUSED = "The robot service refused the connection. Retrying…";
const TOKEN_PROMPT = "Enter the panel access token";
const hello = (token = "") => ({ type: "hello", protocol: 1, client: CLIENT_ID, token });

// Check each frame the way the service does. A refused frame fails the test that sent it.
const violations = [];
afterEach(() => assert.deepEqual(violations.splice(0), [], "the page sent frames that the service refuses"));
function frameProblem(socket, data) {
  if (typeof data !== "string") return "a frame is not text";
  if (Buffer.byteLength(data, "utf8") > 4096) return "a frame has more than 4096 UTF-8 bytes";
  let message;
  try { message = JSON.parse(data); } catch { return "a frame is not JSON"; }
  if (!message || typeof message !== "object" || Array.isArray(message)) return "a frame is not a JSON object";
  const keys = Object.keys(message).sort().join();
  if (!socket.sent.length) {
    if (keys !== "client,protocol,token,type" || message.type !== "hello") return `the first frame is not a hello: ${data}`;
    if (message.protocol !== 1) return `hello has protocol ${JSON.stringify(message.protocol)}`;
    if (typeof message.token !== "string") return "the hello token is not a string";
    const client = message.client;
    if (typeof client !== "string" || !/^[\x20-\x7e]{1,128}$/.test(client) || !client.trim()) return "the hello client is invalid";
    return null;
  }
  if (keys !== "body,id,type") return `a request has the keys ${keys}`;
  if (!["command", "heartbeat", "keys"].includes(message.type)) return `unknown request type ${message.type}`;
  if (!Number.isInteger(message.id) || message.id < 0 || message.id > 2 ** 53) return `invalid request id ${message.id}`;
  if (!message.body || typeof message.body !== "object" || Array.isArray(message.body)) return "a request body is not an object";
  return null;
}

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

function browser({ href = "http://panel.test/mg400/", storedToken = null } = {}) {
  let now = 0, timerId = 0, version = 0, focused = true, lastState = null;
  // requests holds socket requests (not hello) and HTTP requests in send order.
  const timers = new Map(), requests = [], sockets = [], stored = [], elements = new Map();
  const listeners = { window: new Map(), document: new Map() };
  const body = new FakeElement("body");
  const element = id => {
    if (!elements.has(id)) {
      const node = /^(speed|token)$|(Target|Range)$/.test(id) ? new FakeInput(id) : new FakeElement(id);
      if (id === "speed") node.value = "1";
      if (id === "keyboard") node.hidden = node.disabled = true;
      if (id === "auth") node.hidden = true;
      if (id === "status") {
        // Count writes: the page writes the status line only when its text changes.
        let text = "";
        node.writes = 0;
        Object.defineProperty(node, "textContent", { get: () => text, set: value => { text = value; node.writes++; } });
      }
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
  const deliver = (socket, message) => socket.onmessage?.({ data: JSON.stringify(message) });

  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    constructor(url) {
      Object.assign(this, {
        url, readyState: 0, bufferedAmount: 0, sent: [], closedBy: null,
        onopen: null, onmessage: null, onclose: null,
      });
      sockets.push(this);
    }
    send(data) {
      if (this.readyState === 0) throw new Error("InvalidStateError: the socket is still connecting");
      // A browser drops data sent after the close started, without an error.
      if (this.readyState !== 1) return;
      const problem = frameProblem(this, data);
      if (problem) violations.push(problem);
      if (problem?.includes("JSON")) return;
      const message = JSON.parse(data);
      this.sent.push(message);
      if (message.type === "hello") return;
      const request = {
        via: "socket", socket: this, message, type: message.type, id: message.id, body: message.body,
        at: now, settled: false,
      };
      requests.push(request);
      if (message.type === "heartbeat" && page.autoHeartbeat) {
        request.settled = true;
        Promise.resolve().then(() => deliver(this, { type: "reply", id: message.id, status: 200, state: lastState }));
      }
    }
    close(code) {
      if (this.readyState >= 2) return;
      this.readyState = 2;
      this.closedBy = code ?? 1005;
    }
  }

  const context = vm.createContext({
    window, document, console, URL,
    location: { href },
    WebSocket: FakeWebSocket,
    HTMLInputElement: FakeInput,
    crypto: { getRandomValues: array => array.fill(171) },
    sessionStorage: {
      getItem: key => (key === "mg400-panel-token" ? storedToken : null),
      setItem: (key, value) => stored.push([key, value]),
    },
    AbortSignal: { timeout: milliseconds => ({ timeout: milliseconds, aborted: false, addEventListener() {} }) },
    setTimeout: (callback, delay) => schedule(callback, delay, 0),
    setInterval: (callback, delay) => schedule(callback, delay, Math.max(1, Number(delay) || 0)),
    clearTimeout: id => timers.delete(id),
    clearInterval: id => timers.delete(id),
    fetch(path, options = {}) {
      let settle;
      const promise = new Promise((resolve, reject) => { settle = { resolve, reject }; });
      requests.push({
        via: "http", path, options, at: now, settled: false, socketState: sockets.at(-1)?.readyState,
        type: path === "api/command" ? "command" : path,
        body: options.body === undefined ? undefined : JSON.parse(options.body),
        timeout: options.signal?.timeout,
        resolveWith: (data, status) => settle.resolve({ status, ok: status >= 200 && status < 300, json: async () => data }),
        rejectWith: error => settle.reject(error),
      });
      return promise;
    },
  });
  vm.runInContext(script, context);

  const page = {
    requests, sockets, stored, body, $: element, clientId: CLIENT_ID,
    // Answer heartbeats at once, like a healthy service.
    autoHeartbeat: true,
    get socket() { return sockets.at(-1); },
    get now() { return now; },
    // A robot snapshot for this page: owner, syncing, fresh, enabled (mode 5), keys OFF.
    snapshot(overrides = {}) {
      return {
        mode: "robot", robot_host: "robot.test", controller_id: CLIENT_ID,
        syncing: true, feedback_fresh: true, enabled: true, robot_mode: 5, robot_error: false,
        locked: false, busy: false, stop_unconfirmed: false, message: "Ready.",
        actual: [250, 0, 100, 0, 500], joints: [0, 0, 0, 0], speed_factor: 1,
        gripper_available: true, gripper_can_initialize: false, gripper_busy: false, gripper_message: "",
        keyboard_enabled: false, keyboard_arming: false, keyboard_active_key: null,
        keyboard_speed_percent: 1, keyboard_disarm_reason: null,
        version: ++version, ...overrides,
      };
    },
    async open(socket = page.socket) {
      assert.equal(socket.readyState, 0, "the socket is not connecting");
      socket.readyState = 1;
      socket.onopen?.({ type: "open" });
      await flush();
    },
    async message(message, socket = page.socket) {
      deliver(socket, message);
      await flush();
    },
    push(state, socket = page.socket) {
      lastState = state;
      return page.message({ type: "state", state }, socket);
    },
    // The browser reports a refused handshake or a dropped connection as 1006.
    async serverClose(code = 1006, socket = page.socket) {
      socket.readyState = 3;
      socket.onclose?.({ type: "close", code, reason: "", wasClean: code !== 1006 });
      await flush();
    },
    async respond(request, data, status = 200) {
      assert.equal(request.settled, false, `${request.type} ${request.id ?? request.path} was already settled`);
      request.settled = true;
      if (request.via === "http") request.resolveWith(data, status);
      else if (status === 200) deliver(request.socket, { type: "reply", id: request.id, status, state: data });
      else deliver(request.socket, { type: "reply", id: request.id, status, error: data.error });
      await flush();
    },
    // HTTP: a network error. Socket: an error reply.
    async fail(request, error = new Error("Network down"), status = 409) {
      assert.equal(request.settled, false, `${request.type} ${request.id ?? request.path} was already settled`);
      request.settled = true;
      if (request.via === "http") request.rejectWith(error);
      else deliver(request.socket, { type: "reply", id: request.id, status, error: error.message });
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
    async submit(id) {
      const target = element(id);
      for (const listener of target.listeners.get("submit") ?? []) listener({ type: "submit", target, preventDefault() {} });
      await flush();
    },
    setFocus(value) { focused = value; },
    setVisibility(value) { document.visibilityState = value; },
    evaluate: code => vm.runInContext(code, context),
    pending: () => vm.runInContext("pending.size", context),
    keyUpdates: () => requests.filter(request => request.type === "keys"),
    commands: action => requests.filter(
      request => request.type === "command" && (action === undefined || request.body?.action === action),
    ),
    heartbeats: () => requests.filter(request => request.type === "heartbeat"),
    http: () => requests.filter(request => request.via === "http"),
    arms: () => page.commands("keyboard").filter(request => request.body.enabled === true),
    disarms: () => page.commands("keyboard").filter(request => request.body.enabled === false),
  };
  return page;
}

async function ready(overrides = {}) {
  const page = browser();
  await page.open();
  await page.push(page.snapshot(overrides));
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

// Expect the next socket after exactly delay milliseconds.
async function reconnectsAfter(page, delay) {
  const count = page.sockets.length;
  await page.advance(delay - 1);
  assert.equal(page.sockets.length, count, `reconnected before ${delay} ms`);
  await page.advance(1);
  assert.equal(page.sockets.length, count + 1, `no reconnect after ${delay} ms`);
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
  assert.equal(request.via, "socket");
  assert.deepEqual(request.body, { action: "keyboard", enabled: false, reason, seq: request.body.seq });
}

function assertPanelPost(request, action, token = "") {
  assert.equal(request.via, "http");
  assert.equal(request.path, "api/command");
  assert.equal(request.options.method, "POST");
  assert.deepEqual(request.body, { action });
  assert.equal(request.options.headers["Content-Type"], "application/json");
  assert.equal(request.options.headers["X-MG400-Panel"], "1");
  assert.equal(request.options.headers["X-MG400-Client"], CLIENT_ID);
  assert.equal(request.options.headers.Authorization, token ? `Bearer ${token}` : "");
}

test("the page has one inline script that starts by opening the socket and sending one hello", async () => {
  assert.equal(scripts.length, 1);
  assert.doesNotMatch(scripts[0][1], /\bsrc=/);
  const page = browser();
  assert.equal(page.sockets.length, 1);
  assert.equal(page.socket.url, "ws://panel.test/mg400/api/ws");
  assert.deepEqual(page.requests, [], "no request before the socket opens");
  assert.deepEqual(page.socket.sent, []);
  await page.open();
  assert.deepEqual(page.socket.sent, [hello()]);
  await page.push(page.snapshot());
  await page.advance(1000);
  assert.equal(page.sockets.length, 1);
  assert.equal(page.socket.sent.filter(message => message.type === "hello").length, 1);
  assert.equal(page.http().length, 0);
  assert.equal(page.keyUpdates().length, 0);
  assert.equal(page.commands().length, 0);
  const secure = browser({ href: "https://lab.test/mg400-panel/index.html?view=1#top", storedToken: "stored-token" });
  assert.equal(secure.socket.url, "wss://lab.test/mg400-panel/api/ws");
  await secure.open();
  assert.deepEqual(secure.socket.sent, [hello("stored-token")]);
  // All timing comes from timers, so the fake clock drives every rule.
  assert.match(script, /\bconst PROTOCOL = 1\b/);
  assert.doesNotMatch(script, /\bDate\b|\bperformance\b/);
});

test("state pushes render, and nothing renders before the first one", async () => {
  const page = browser();
  await page.open();
  assert.equal(page.$("rows").innerHTML, "");
  await page.push(page.snapshot({ message: "First push.", actual: [250, 1, 100, 2, 500], joints: [1, 2, 3, 4] }));
  assert.equal(page.$("mode").textContent, "REAL ROBOT");
  assert.equal(page.$("status").textContent, "First push.");
  assert.equal(page.$("xActual").textContent, "250.000");
  assert.match(page.$("joints").innerHTML, /J1 <b>1\.00°<\/b>/);
  assert.equal(page.$("enable").disabled, true);
  await page.push(page.snapshot({ message: "Second push.", robot_mode: 4, enabled: false }));
  assert.equal(page.$("status").textContent, "Second push.");
  assert.equal(page.$("robotState").textContent, "Mode 4");
  assert.equal(page.$("enable").disabled, false);
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
    assert.equal(update.via, "socket");
    assert.deepEqual(Object.keys(update.message).sort(), ["body", "id", "type"]);
    assert.deepEqual(Object.keys(update.body).sort(), ["held", "seq"]);
  }
  assert.deepEqual(page.keyUpdates().map(update => update.id), [1, 2, 3, 4]);
  assertSequenced(page);
  await page.advance(999);
  assert.equal(page.pending(), 4, "a key update gave up before 1 s");
  assert.equal(page.$("status").classList.contains("error"), false);
  await page.advance(1);
  assert.equal(page.pending(), 0, "a key update waited longer than 1 s");
  assert.equal(page.$("status").textContent, "Request timed out");
  assert.equal(page.keyUpdates().length, 4, "no keys were held, so no release follows");
});

test("keyup removes a held key and sends at once from an input or without focus", async () => {
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
    await page.push(page.snapshot({ keyboard_enabled: true }));
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
  assert.deepEqual(sent.filter(request => request.type === "keys").map(request => request.body.held), [[]]);
  const commands = sent.filter(request => request.type === "command");
  assert.deepEqual(commands.map(request => [request.via, request.body]), [
    ["http", { action: "disable" }], ["socket", { action: "disable" }],
  ]);
  assertPanelPost(commands[0], "disable");
  await page.advance(500);
  assert.equal(page.keyUpdates().length, 3, "renewals continued");
  assertSequenced(page);
});

test("Esc disables while keys are CHECKING", async () => {
  const page = await ready({ keyboard_arming: true, busy: true });
  const event = await page.key("keydown", "Escape", { metaKey: true });
  assert.equal(event.defaultPrevented, true);
  assert.deepEqual(page.commands().map(request => [request.via, request.body]), [
    ["http", { action: "disable" }], ["socket", { action: "disable" }],
  ]);
  assertPanelPost(page.commands()[0], "disable");
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

test("at most two renewals await replies; key changes and Stop go out at once", async () => {
  const page = await ready({ keyboard_enabled: true });
  await page.key("keydown", "KeyW");
  assert.equal(page.keyUpdates().length, 1);
  // Stay below the 1 s key timeout, which would end the hold.
  await page.advance(500);
  assert.equal(page.keyUpdates().length, 3, "the key change and two renewals");
  await page.respond(page.keyUpdates()[1], page.snapshot({ keyboard_enabled: true }));
  await page.advance(100);
  assert.equal(page.keyUpdates().length, 4);
  await page.advance(300);
  assert.equal(page.keyUpdates().length, 4);
  // The key change does not use a renewal slot.
  await page.respond(page.keyUpdates()[0], page.snapshot({ keyboard_enabled: true }));
  await page.advance(250);
  assert.equal(page.keyUpdates().length, 4);
  await page.key("keydown", "KeyA");
  assert.equal(page.keyUpdates().length, 5);
  assert.deepEqual([...page.keyUpdates()[4].body.held].sort(), ["A", "W"]);
  await page.click("stop");
  assert.deepEqual(page.commands("stop").map(request => request.via), ["http", "socket"]);
  assert.ok(page.keyUpdates().slice(1, 4).every(update => update.body.held.join() === "W"));
  assertSequenced(page);
});

test("renewals are skipped while the socket has unsent data; key changes are not", async () => {
  const page = await ready({ keyboard_enabled: true });
  await page.key("keydown", "KeyW");
  page.socket.bufferedAmount = 64;
  await page.advance(300);
  assert.equal(page.keyUpdates().length, 1, "a renewal joined the backlog");
  await page.key("keydown", "KeyA");
  assert.deepEqual(page.keyUpdates().map(update => [...update.body.held].sort()), [["W"], ["A", "W"]]);
  page.socket.bufferedAmount = 0;
  await page.advance(100);
  assert.equal(page.keyUpdates().length, 3);
  assertSequenced(page);
});

for (const [name, fail, count] of [
  ["an error reply", (page, request) => page.respond(request, { error: "The panel is closing" }, 409), 3],
  ["its timeout", async (page, request) => {
    // Answer the key change, so that this renewal is the update that times out.
    await page.respond(page.keyUpdates()[0], page.snapshot({ keyboard_enabled: true }));
    await page.advance(request.at + 1000 - page.now);
    assert.equal(page.$("status").textContent, "Request timed out");
  }, 4],
  ["a lost connection", page => page.serverClose(1006), 2],
]) {
  test(`a key update that fails with ${name} clears held keys and stops renewals`, async () => {
    const page = await holding();
    const socket = page.socket;
    await fail(page, page.keyUpdates()[1]);
    assert.equal(page.keyUpdates().length, count);
    if (socket.readyState === 1) {
      // A sent press cannot be withdrawn, so exactly one release follows on the same socket.
      const release = page.keyUpdates().at(-1);
      assert.deepEqual(release.body.held, []);
      assert.equal(release.socket, socket);
    }
    await page.advance(500);
    assert.equal(page.keyUpdates().length, count, "renewals continued");
    await page.key("keyup", "KeyW");
    assert.equal(page.keyUpdates().length, count, "W was still held");
    assert.equal(page.disarms().length, 0);
    // A new press starts over, on a new socket after a lost connection.
    if (socket.readyState !== 1) {
      await page.open();
      await page.push(page.snapshot({ keyboard_enabled: true }));
    }
    await page.key("keydown", "KeyW");
    assert.deepEqual(page.keyUpdates().at(-1).body.held, ["W"]);
    assert.equal(page.keyUpdates().at(-1).socket, page.socket);
    assertSequenced(page);
  });
}

test("a failed key change clears held keys like a failed renewal and sends one release", async () => {
  const page = await ready({ keyboard_enabled: true });
  await page.key("keydown", "KeyW");
  await page.fail(page.keyUpdates()[0]);
  assert.deepEqual(page.keyUpdates().map(update => update.body.held), [["W"], []]);
  await page.advance(500);
  await page.key("keyup", "KeyW");
  assert.equal(page.keyUpdates().length, 2);
  assertSequenced(page);
});

test("a failing release does not send another release", async () => {
  const page = await ready({ keyboard_enabled: true });
  await page.key("keydown", "KeyW");
  await page.fail(page.keyUpdates()[0]);
  const [, release] = page.keyUpdates();
  assert.deepEqual(release.body.held, []);
  await page.fail(release, new Error("Still closing"));
  assert.equal(page.keyUpdates().length, 2);
  assert.equal(page.$("status").textContent, "Still closing");
  // Timeouts do not loop either: one release after the first timeout, none after its own.
  await page.key("keydown", "KeyA");
  const start = page.keyUpdates().length;
  await page.advance(2500);
  assert.deepEqual(page.keyUpdates().slice(start - 1).map(update => update.body.held), [["A"], ["A"], ["A"], []]);
  assert.equal(page.pending(), 0);
  assertSequenced(page);
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
  await page.push(page.snapshot({ keyboard_enabled: true }));
  assert.equal(page.disarms().length, 1);
  await page.fail(page.disarms()[0]);
  assert.ok(inFlight().length <= 1);
  await page.push(page.snapshot({ keyboard_arming: true, busy: true }));
  assert.equal(inFlight().length, 1, "a CHECKING snapshot must re-send the disarm");
  // The server can answer with ON when the disarm was stale.
  await page.respond(inFlight()[0], page.snapshot({ keyboard_enabled: true }));
  assert.ok(inFlight().length <= 1);
  await page.push(page.snapshot({ keyboard_enabled: true }));
  assert.equal(inFlight().length, 1, "an ON snapshot must re-send the disarm");
  assert.ok(page.disarms().length >= 3);
  assert.ok(page.disarms().every(request => request.body.reason === "focus"));
  await page.respond(inFlight()[0], page.snapshot());
  const total = page.disarms().length;
  // OFF ends the retries, and a new arm is not turned off.
  await page.fire("focus");
  await page.click("keyboard");
  await page.respond(page.arms()[0], page.snapshot({ keyboard_arming: true, busy: true }));
  await page.push(page.snapshot({ keyboard_enabled: true }));
  assert.equal(page.disarms().length, total);
  assertSequenced(page);
});

test("a reply older than a push is never rendered over it", async () => {
  const page = await ready({ keyboard_enabled: true, message: "Keys on." });
  await page.key("keydown", "KeyW");
  const [change] = page.keyUpdates();
  const older = page.snapshot({ message: "Older: keys off." });
  await page.push(page.snapshot({ keyboard_enabled: true, message: "Newer: keys on." }));
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
  await page.push(page.snapshot({ keyboard_enabled: true, message: "Newest." }));
  await page.respond(page.commands("home")[0], stale);
  assert.equal(page.$("status").textContent, "Newest.");
  assert.equal(page.$("keyboard").textContent, "Keyboard: ON 1%");
});

test("a push older than a reply is never rendered over it", async () => {
  const page = await ready({ keyboard_enabled: true, message: "Keys on." });
  await page.key("keydown", "KeyW");
  const older = page.snapshot({ message: "Older push: keys off." });
  await page.respond(page.keyUpdates()[0], page.snapshot({ keyboard_enabled: true, message: "Newer reply." }));
  await page.push(older);
  assert.equal(page.$("status").textContent, "Newer reply.");
  assert.equal(page.$("keyboard").textContent, "Keyboard: ON 1%");
  // The older OFF push did not clear the held key, so renewals go on.
  await page.advance(100);
  assert.deepEqual(page.keyUpdates().at(-1).body.held, ["W"]);
  await page.key("keyup", "KeyW");
  // The same holds for a command reply.
  await page.click("home");
  const olderPush = page.snapshot({ message: "Older push before Home." });
  await page.respond(page.commands("home")[0], page.snapshot({ keyboard_enabled: true, busy: true, message: "Home reply." }));
  await page.push(olderPush);
  assert.equal(page.$("status").textContent, "Home reply.");
});

test("the Keyboard label shows the session speed and the Speed field locks while CHECKING or busy", async () => {
  const page = await ready({ speed_factor: 25 });
  assert.equal(page.$("keyboard").hidden, false);
  assert.equal(page.$("keyboard").textContent, "Keyboard: OFF");
  assert.equal(page.$("speed").disabled, false);
  assert.match(`${page.$("speedField").title} ${page.$("speed").title}`, /\b25\b/);
  const show = overrides => page.push(page.snapshot(overrides));
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
    await page.push(page.snapshot(blocked));
    assert.equal(page.$("keyboard").disabled, true, JSON.stringify(blocked));
  }
  await page.push(page.snapshot());
  page.$("speed").value = "37";
  await page.click("keyboard");
  assert.equal(page.arms().length, 1);
  const [arm] = page.arms();
  assert.equal(arm.via, "socket");
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
  await page.push(page.snapshot({ keyboard_enabled: true, keyboard_speed_percent: 5 }));
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
  await page.open();
  await page.push({
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
  await page.advance(1000);
  assert.equal(page.keyUpdates().length, 0);
  assert.equal(page.commands().length, 0);
  assert.equal(page.heartbeats().length, 0);
});

test("a restarted server renders although its version is lower, and the old process cannot overwrite it", async () => {
  const page = browser();
  await page.open();
  await page.push(page.snapshot({ instance: "old", version: 5000, keyboard_enabled: true }));
  assert.equal(page.$("keyboard").textContent, "Keyboard: ON 1%");
  // The old process answers an HTTP stop late, after the new process took over.
  await page.click("stop");
  const restarted = { instance: "new", version: 1, syncing: false, controller_id: null, message: "Start sync." };
  await page.push(page.snapshot(restarted));
  assert.equal(page.$("status").textContent, "Start sync.");
  assert.equal(page.$("keyboard").textContent, "Keyboard: OFF");
  assert.equal(page.$("startSync").disabled, false);
  await page.push(page.snapshot({ instance: "old", version: 5001, message: "Late push." }));
  assert.equal(page.$("status").textContent, "Start sync.");
  await page.respond(page.commands("stop")[0], page.snapshot({ instance: "old", version: 5002, message: "Late reply." }));
  assert.equal(page.$("status").textContent, "Start sync.");
  // The rendered state says that this page controls nothing, so leaving sends no stop.
  assert.equal(page.commands("stop").length, 2);
  await page.fire("pagehide");
  assert.equal(page.commands("stop").length, 2);
});

test("a requested disarm wins over a new key press", async () => {
  const page = await ready({ keyboard_enabled: true });
  await page.click("keyboard");
  assertDisarm(page.disarms()[0], "user");
  const down = await page.key("keydown", "KeyW");
  assert.equal(down.defaultPrevented, true);
  await page.advance(300);
  assert.equal(page.keyUpdates().length, 0);
  await page.respond(page.disarms()[0], page.snapshot());
  await page.key("keydown", "KeyA");
  assert.equal(page.keyUpdates().length, 0);
});

test("a failed disarm sent during the arm request is sent again when the arm reply renders", async () => {
  const page = await ready();
  await page.click("keyboard");
  const arm = page.arms()[0];
  await page.fire("blur");
  assertDisarm(page.disarms()[0], "focus");
  await page.fail(page.disarms()[0]);
  await page.respond(arm, page.snapshot({ keyboard_arming: true }));
  assert.equal(page.disarms().length, 2);
  assertDisarm(page.disarms()[1], "focus");
  assertSequenced(page);
});

test("a failed disarm sent during the arm request is sent again when the arm request fails", async () => {
  const page = await ready();
  await page.click("keyboard");
  const [arm] = page.arms();
  await page.fire("blur");
  assertDisarm(page.disarms()[0], "focus");
  await page.fail(page.disarms()[0]);
  await page.fail(arm, new Error("Enable the robot before keyboard control"));
  assert.equal(page.disarms().length, 2);
  assertDisarm(page.disarms()[1], "focus");
  assertSequenced(page);
});

test("clicks during the arm request send one arm", async () => {
  const page = await ready();
  await page.click("keyboard");
  await page.click("keyboard");
  assert.equal(page.arms().length, 1);
});

for (const [name, hide] of [
  ["without focus", page => page.setFocus(false)],
  ["while hidden", page => page.setVisibility("hidden")],
]) {
  test(`a snapshot that shows keys on ${name} turns keys off`, async () => {
    const page = await ready();
    hide(page);
    await page.advance(500);
    await page.push(page.snapshot({ keyboard_enabled: true }));
    assert.equal(page.disarms().length, 1);
    assertDisarm(page.disarms()[0], "focus");
  });
}

test("socket requests number ids from 1, and requests other than keys time out after 2.5 s", async () => {
  const page = await ready();
  await page.click("home");
  const [home] = page.commands("home");
  assert.deepEqual(home.message, { type: "command", id: 1, body: { action: "home" } });
  await page.advance(2499);
  assert.equal(page.$("home").disabled, true, "Home gave up before 2.5 s");
  await page.advance(1);
  assert.equal(page.$("status").textContent, "Request timed out");
  assert.equal(page.$("home").disabled, false);
  const ids = page.requests.filter(request => request.via === "socket").map(request => request.id);
  assert.ok(ids.length > 1);
  ids.forEach((id, index) => assert.equal(id, index + 1));
});

test("heartbeats go over the socket every 500 ms only while this page has control", async () => {
  const page = await ready();
  await page.advance(1000);
  assert.deepEqual(page.heartbeats().map(request => [request.via, request.at, request.body]), [
    ["socket", 500, {}], ["socket", 1000, {}],
  ]);
  await page.push(page.snapshot({ controller_id: "another-page" }));
  await page.advance(1000);
  assert.equal(page.heartbeats().length, 2);
  await page.push(page.snapshot());
  await page.advance(500);
  assert.equal(page.heartbeats().length, 3);
  await page.serverClose(1006);
  await page.advance(400);
  assert.equal(page.heartbeats().length, 3, "a heartbeat was sent without a connection");
});

test("requests reject at once and send nothing until the socket is open and has sent state", async () => {
  const page = browser();
  await page.open();
  assert.equal(await page.evaluate("request('heartbeat',{}).catch(error => error.message)"), "Not connected");
  await page.push(page.snapshot({ keyboard_enabled: true }));
  // The close started, but its event has not arrived yet.
  page.socket.readyState = 2;
  const sent = page.socket.sent.length;
  await page.key("keydown", "KeyW");
  await page.click("home");
  assert.equal(page.socket.sent.length, sent);
  assert.equal(page.pending(), 0);
  assert.equal(page.evaluate("held.size"), 0);
  assert.equal(page.$("status").classList.contains("error"), true);
});

test("a closed socket clears held keys, stops renewals, rejects pending requests and disables controls", async () => {
  const page = await ready({ keyboard_enabled: true });
  await page.click("home");
  await page.key("keydown", "KeyW");
  await page.advance(100);
  assert.equal(page.pending(), 3);
  // Both key updates get replies, so only the close can clear the held key.
  for (const update of page.keyUpdates()) await page.respond(update, page.snapshot({ keyboard_enabled: true }));
  assert.equal(page.pending(), 1);
  const socket = page.socket;
  const sent = socket.sent.length;
  await page.serverClose(1006);
  assert.equal(page.evaluate("held.size"), 0);
  assert.equal(page.evaluate("renewTimer"), null, "renewals did not stop at once");
  assert.equal(page.pending(), 0);
  assert.equal(page.evaluate("busy"), false, "the rejected Home must end the busy state");
  assert.equal(page.$("status").textContent, LOST);
  assert.equal(page.$("status").classList.contains("error"), true);
  for (const id of ["startSync", "stopSync", "enable", "home", "execute", "copyActual", "keyboard", "xTarget", "xRange"]) {
    assert.equal(page.$(id).disabled, true, `${id} is enabled without a connection`);
  }
  // Stop arm and Disable robot use HTTP and stay available.
  assert.equal(page.$("stop").disabled, false);
  assert.equal(page.$("disable").disabled, false);
  await page.advance(400);
  await page.key("keyup", "KeyW");
  assert.equal(socket.sent.length, sent);
  assert.equal(page.keyUpdates().length, 2, "a renewal, release or key update followed the close");
  assert.equal(page.disarms().length, 0);
});

test("reconnects after 0.5 s, 1 s and 2 s, capped at 2 s, and starts over after a state", async () => {
  const page = await ready();
  await page.serverClose(1006);
  await reconnectsAfter(page, 500);
  // Too many connections: the service sends the error, then closes with 1013.
  await page.open();
  await page.message({ type: "error", status: 503, error: "Too many open panel connections" });
  await page.serverClose(1013);
  assert.equal(page.$("status").textContent, "Too many open panel connections");
  await reconnectsAfter(page, 1000);
  await page.open();
  await page.serverClose(1011);
  assert.equal(page.$("status").textContent, LOST);
  await reconnectsAfter(page, 2000);
  await page.open();
  await page.serverClose(1001);
  await reconnectsAfter(page, 2000);
  await page.open();
  await page.push(page.snapshot({ message: "Back." }));
  assert.equal(page.$("status").textContent, "Back.");
  assert.equal(page.$("startSync").disabled, true, "this page owns a syncing robot");
  assert.equal(page.$("home").disabled, false);
  await page.serverClose(1006);
  await reconnectsAfter(page, 500);
  for (const socket of page.sockets.slice(0, -1)) {
    assert.deepEqual(socket.sent.filter(message => message.type === "hello"), [hello()]);
  }
});

test("three connections closed before open wait 5 s and say the service refused the connection", async () => {
  const page = await ready();
  await page.serverClose(1006);
  await reconnectsAfter(page, 500);
  await page.serverClose(1006);
  assert.equal(page.$("status").textContent, LOST);
  await reconnectsAfter(page, 1000);
  await page.serverClose(1006);
  assert.equal(page.$("status").textContent, LOST);
  await reconnectsAfter(page, 2000);
  await page.serverClose(1006);
  assert.equal(page.$("status").textContent, REFUSED);
  assert.equal(page.$("status").classList.contains("error"), true);
  await reconnectsAfter(page, 5000);
  await page.serverClose(1006);
  assert.equal(page.$("status").textContent, REFUSED);
  await reconnectsAfter(page, 5000);
  // An open ends the refused run. The plain backoff goes on until a state arrives.
  await page.open();
  await page.serverClose(1006);
  assert.equal(page.$("status").textContent, LOST);
  await reconnectsAfter(page, 2000);
  await page.open();
  await page.push(page.snapshot());
  await page.serverClose(1006);
  await reconnectsAfter(page, 500);
});

test("three seconds of silence drop the socket and reconnect", async () => {
  // No page has control, so no heartbeat replies keep the socket alive.
  const page = await ready({ controller_id: null });
  const first = page.socket;
  await page.advance(2900);
  await page.push(page.snapshot({ controller_id: null }));
  await page.advance(2999);
  assert.equal(first.closedBy, null);
  assert.equal(page.$("startSync").disabled, false);
  await page.advance(1);
  assert.notEqual(first.closedBy, null, "the page did not close the silent socket");
  assert.equal(page.$("status").textContent, LOST);
  assert.equal(page.$("startSync").disabled, true);
  await reconnectsAfter(page, 500);
  // A socket that does not open, or opens without sending state, is dropped after 3 s too.
  await page.advance(3000);
  assert.notEqual(page.sockets[1].closedBy, null);
  await reconnectsAfter(page, 1000);
  await page.open();
  await page.advance(3000);
  assert.notEqual(page.sockets[2].closedBy, null);
  await reconnectsAfter(page, 2000);
});

test("events from a superseded socket are ignored", async () => {
  const page = await ready({ keyboard_enabled: true });
  const old = page.socket;
  page.autoHeartbeat = false;
  await page.advance(3000);
  assert.notEqual(old.closedBy, null);
  await reconnectsAfter(page, 500);
  page.autoHeartbeat = true;
  const current = page.socket;
  await page.open(current);
  await page.push(page.snapshot({ keyboard_enabled: true, message: "Second socket." }), current);
  await page.key("keydown", "KeyW");
  const [press] = page.keyUpdates().filter(update => update.socket === current);
  // A late state, a reply to the current request id, an auth error, and the close that
  // the service sends when it replaces the old socket all come from the old socket.
  await page.push(page.snapshot({ message: "Late old socket." }), old);
  await page.message({ type: "reply", id: press.id, status: 409, error: "Old socket reply." }, old);
  await page.message({ type: "error", status: 401, error: TOKEN_PROMPT }, old);
  await page.serverClose(1006, old);
  assert.equal(page.$("status").textContent, "Second socket.");
  assert.equal(page.$("keyboard").textContent, "Keyboard: ON 1%");
  assert.equal(page.$("auth").hidden, true);
  assert.equal(page.$("home").disabled, false);
  // The current request is still pending and W is still held.
  await page.respond(press, page.snapshot({ keyboard_enabled: true, message: "Reply on the current socket." }));
  assert.equal(page.$("status").textContent, "Reply on the current socket.");
  await page.advance(100);
  assert.deepEqual(page.keyUpdates().at(-1).body.held, ["W"]);
  assert.equal(page.keyUpdates().at(-1).socket, current);
  await page.key("keyup", "KeyW");
  await page.advance(2000);
  assert.equal(page.sockets.length, 2, "the old socket's close scheduled a reconnect");
  // A late open of the old socket sends no hello and leaves the current liveness check alone.
  old.readyState = 1;
  old.onopen?.({ type: "open" });
  await page.advance(0);
  assert.equal(old.sent.filter(message => message.type === "hello").length, 1);
  page.autoHeartbeat = false;
  await page.advance(3000);
  assert.notEqual(current.closedBy, null, "the current socket was not dropped after 3 s of silence");
});

for (const [name, refuse] of [
  ["a 401 error before close 4401", async page => {
    await page.message({ type: "error", status: 401, error: TOKEN_PROMPT });
    assert.equal(page.$("auth").hidden, false);
    await page.serverClose(4401);
  }],
  ["a 4401 close alone", page => page.serverClose(4401)],
]) {
  test(`${name} shows the token form and waits for a submit, then reconnects with the new token`, async () => {
    const page = browser({ storedToken: "old-token" });
    await page.open();
    assert.deepEqual(page.socket.sent, [hello("old-token")]);
    await refuse(page);
    assert.equal(page.$("auth").hidden, false);
    assert.equal(page.$("status").textContent, TOKEN_PROMPT);
    await page.advance(10000);
    assert.equal(page.sockets.length, 1, "the page reconnected before the submit");
    page.$("token").value = "new-token";
    await page.submit("auth");
    assert.equal(page.sockets.length, 2);
    await page.open();
    assert.deepEqual(page.socket.sent, [hello("new-token")]);
    assert.deepEqual(page.stored, [], "the token was stored before the service accepted it");
    await page.push(page.snapshot());
    assert.equal(page.$("auth").hidden, true);
    assert.deepEqual(page.stored, [["mg400-panel-token", "new-token"]]);
    assert.equal(page.$("token").value, "");
    // HTTP commands carry the new token.
    await page.click("stop");
    assertPanelPost(page.commands("stop")[0], "stop", "new-token");
  });
}

test("Stop arm, Disable robot and Esc use HTTP and the open socket together; Disconnect uses HTTP only", async () => {
  const page = await ready({ keyboard_enabled: true });
  for (const id of ["stop", "disable", "stopSync"]) await page.click(id);
  await page.key("keydown", "Escape");
  const sent = page.commands();
  assert.deepEqual(sent.map(request => [request.via, request.body.action]), [
    ["http", "stop"], ["socket", "stop"], ["http", "disable"], ["socket", "disable"], ["http", "stop_sync"],
    ["http", "disable"], ["socket", "disable"],
  ]);
  for (const request of sent) {
    if (request.via === "socket") {
      assert.deepEqual(request.message, { type: "command", id: request.id, body: { action: request.body.action } });
      continue;
    }
    assertPanelPost(request, request.body.action);
    assert.equal(request.timeout, 2500);
  }
  // Each reply renders when it is newer than the rendered state.
  await page.respond(sent[1], page.snapshot({ keyboard_enabled: true, message: "Stopping (socket)." }));
  assert.equal(page.$("status").textContent, "Stopping (socket).");
  await page.respond(sent[0], page.snapshot({ keyboard_enabled: true, message: "Stopping (HTTP)." }));
  assert.equal(page.$("status").textContent, "Stopping (HTTP).");
  // Without a connection, Stop arm, Disable robot and Esc use HTTP only. Disconnect waits.
  await page.serverClose(1006);
  assert.equal(page.$("stop").disabled, false);
  assert.equal(page.$("disable").disabled, false);
  assert.equal(page.$("stopSync").disabled, true);
  await page.click("stop");
  await page.click("disable");
  await page.key("keydown", "Escape");
  assert.deepEqual(page.commands().slice(sent.length).map(request => [request.via, request.body.action]), [
    ["http", "stop"], ["http", "disable"], ["http", "disable"],
  ]);
  // An HTTP 401 shows the token form.
  await page.respond(page.commands()[sent.length], { error: TOKEN_PROMPT }, 401);
  assert.equal(page.$("auth").hidden, false);
  assert.equal(page.$("status").textContent, TOKEN_PROMPT);
});

test("Stop arm and Disable robot show an error only when every path failed", async () => {
  const page = await ready({ message: "Ready." });
  // The HTTP path fails, the socket path succeeds: no error.
  await page.click("stop");
  const [httpStop, socketStop] = page.commands("stop");
  await page.fail(httpStop, new Error("Failed to fetch"));
  assert.equal(page.$("status").textContent, "Ready.", "an error showed before the socket answered");
  await page.respond(socketStop, page.snapshot({ message: "Stopping." }));
  assert.equal(page.$("status").textContent, "Stopping.");
  assert.equal(page.$("status").classList.contains("error"), false);
  // The HTTP path succeeds, the socket path times out: no error.
  await page.click("disable");
  const [httpDisable, socketDisable] = page.commands("disable");
  await page.respond(httpDisable, page.snapshot({ message: "Disabling." }));
  await page.advance(2500);
  assert.equal(page.pending(), 0, `${socketDisable.type} ${socketDisable.id} did not time out`);
  assert.equal(page.$("status").textContent, "Disabling.");
  assert.equal(page.$("status").classList.contains("error"), false);
  // Both paths fail: the error shows.
  await page.click("disable");
  const [, , httpAgain, socketAgain] = page.commands("disable");
  await page.respond(socketAgain, { error: "The panel is closing" }, 409);
  assert.equal(page.$("status").textContent, "Disabling.");
  await page.fail(httpAgain, new Error("Failed to fetch"));
  assert.equal(page.$("status").textContent, "Failed to fetch");
  assert.equal(page.$("status").classList.contains("error"), true);
});

test("the pagehide beacon still posts a stop over HTTP", async () => {
  const page = await ready();
  await page.fire("pagehide");
  const [stop] = page.commands("stop");
  assertPanelPost(stop, "stop");
  assert.equal(stop.options.keepalive, true);
  assert.equal(page.commands().length, 1);
  assert.equal(page.socket.sent.filter(message => message.type === "command").length, 0);
});

test("without a connection the sync line says so, the dot is off, and jog keys do nothing; Esc still disables", async () => {
  const page = await ready({ keyboard_enabled: true });
  assert.equal(page.$("syncState").textContent, "Live feedback");
  assert.equal(page.$("syncDot").classList.contains("on"), true);
  await page.serverClose(1006);
  assert.equal(page.$("syncState").textContent, "Reconnecting…");
  assert.equal(page.$("syncDot").classList.contains("on"), false);
  assert.equal(page.$("keyboard").textContent, "Keyboard: ON 1%", "the last known state was cleared");
  const seq = page.evaluate("keySeq");
  await page.key("keydown", "KeyW");
  assert.equal(page.evaluate("held.size"), 0);
  assert.equal(page.evaluate("keySeq"), seq, "a jog key started a key update without a connection");
  await page.advance(200);
  await page.key("keyup", "KeyW");
  assert.equal(page.keyUpdates().length, 0);
  // Esc works from the last known state, over HTTP.
  const escape = await page.key("keydown", "Escape");
  assert.equal(escape.defaultPrevented, true);
  assert.deepEqual(page.commands().map(request => [request.via, request.body.action]), [["http", "disable"]]);
  assert.equal(page.$("stop").disabled, false);
  assert.equal(page.$("disable").disabled, false);
  // Back online, the sync line shows the feedback again and jog keys work.
  await page.advance(300);
  await page.open();
  await page.push(page.snapshot({ keyboard_enabled: true }));
  assert.equal(page.$("syncState").textContent, "Live feedback");
  assert.equal(page.$("syncDot").classList.contains("on"), true);
  await page.key("keydown", "KeyW");
  assert.deepEqual(page.keyUpdates().map(update => update.body.held), [["W"]]);
  await page.key("keyup", "KeyW");
  // While the page waits for a token, the sync line says Not connected and the status says why.
  await page.serverClose(1006);
  await reconnectsAfter(page, 500);
  await page.open();
  await page.serverClose(4401);
  assert.equal(page.$("syncState").textContent, "Not connected");
  await page.advance(3000);
  assert.equal(page.$("status").textContent, TOKEN_PROMPT, "the stale state message replaced the reason");
});

for (const [name, refuse, text] of [
  ["a 426 error before close 4426", async page => {
    await page.message({ type: "error", status: 426, error: "The robot service uses protocol 2. Reload the page." });
    await page.serverClose(4426);
  }, "The robot service uses protocol 2. Reload the page."],
  ["a 4426 close alone", page => page.serverClose(4426), "The robot service was updated. Reload the page."],
  ["a 426 error before a dropped connection", async page => {
    await page.message({ type: "error", status: 426, error: "The robot service uses protocol 2. Reload the page." });
    await page.serverClose(1006);
  }, "The robot service uses protocol 2. Reload the page."],
]) {
  test(`${name} asks for a reload and stops reconnecting`, async () => {
    const page = await ready({ keyboard_enabled: true });
    await page.serverClose(1006);
    await reconnectsAfter(page, 500);
    await page.open();
    assert.deepEqual(page.socket.sent, [hello()]);
    await refuse(page);
    assert.equal(page.$("status").textContent, text);
    assert.equal(page.$("status").classList.contains("error"), true);
    assert.equal(page.$("auth").hidden, true);
    assert.equal(page.$("syncState").textContent, "Not connected");
    for (const id of ["startSync", "stopSync", "enable", "home", "execute", "keyboard"]) {
      assert.equal(page.$(id).disabled, true, `${id} is enabled`);
    }
    assert.equal(page.$("stop").disabled, false);
    assert.equal(page.$("disable").disabled, false);
    await page.advance(30000);
    assert.equal(page.sockets.length, 2, "the page reconnected to a service with another protocol");
    assert.equal(page.$("status").textContent, text);
    await page.fire("pageshow", { persisted: true });
    assert.equal(page.sockets.length, 2, "a restored page reconnected to a service with another protocol");
  });
}

test("pagehide sends the stop beacon first, then closes the socket and does not reconnect", async () => {
  const page = await ready({ keyboard_enabled: true });
  const socket = page.socket;
  await page.key("keydown", "KeyW");
  await page.fire("pagehide");
  const [beacon] = page.commands("stop");
  assertPanelPost(beacon, "stop");
  assert.equal(beacon.options.keepalive, true);
  assert.equal(beacon.socketState, 1, "the socket closed before the beacon went out");
  assert.equal(socket.closedBy, 1000);
  assert.equal(page.evaluate("held.size"), 0);
  assert.equal(page.pending(), 0);
  // The close event and late messages of that socket change nothing.
  await page.push(page.snapshot({ message: "Late." }), socket);
  await page.serverClose(1000, socket);
  await page.advance(30000);
  assert.equal(page.sockets.length, 1);
  assert.equal(page.heartbeats().length, 0);
  assert.deepEqual(page.keyUpdates().map(update => update.body.held), [["W"]]);
});

test("pageshow from the back/forward cache reconnects at once with held keys cleared", async () => {
  const page = await ready({ keyboard_enabled: true });
  await page.fire("pagehide");
  await page.fire("pageshow", { persisted: false });
  assert.equal(page.sockets.length, 1, "a normal pageshow reconnected");
  page.evaluate("held.add('W')");
  await page.fire("pageshow", { persisted: true });
  assert.equal(page.sockets.length, 2);
  assert.equal(page.evaluate("held.size"), 0);
  assert.equal(page.evaluate("connected"), false);
  await page.open();
  assert.deepEqual(page.socket.sent, [hello()]);
  await page.push(page.snapshot({ keyboard_enabled: true, message: "Restored." }));
  assert.equal(page.$("status").textContent, "Restored.");
  await page.key("keydown", "KeyW");
  assert.deepEqual(page.keyUpdates().at(-1).body.held, ["W"]);
  assert.equal(page.keyUpdates().at(-1).socket, page.socket);
  await page.key("keyup", "KeyW");
});

test("an error stays in the status line for 3 s while pushes arrive, and a newer error restarts the time", async () => {
  const page = await ready({ message: "Ready." });
  const failHome = async message => {
    await page.click("home");
    await page.respond(page.commands("home").at(-1), { error: message }, 409);
  };
  await failHome("Enable the robot before movement");
  for (let i = 0; i < 2; i++) {
    await page.advance(50);
    await page.push(page.snapshot({ message: "Pushed." }));
  }
  assert.equal(page.$("status").textContent, "Enable the robot before movement");
  assert.equal(page.$("status").classList.contains("error"), true);
  await page.advance(2899);
  assert.equal(page.$("status").textContent, "Enable the robot before movement");
  await page.advance(1);
  assert.equal(page.$("status").textContent, "Pushed.");
  assert.equal(page.$("status").classList.contains("error"), false);
  await failHome("First error");
  await page.advance(2000);
  await failHome("Second error");
  await page.advance(2999);
  assert.equal(page.$("status").textContent, "Second error");
  assert.equal(page.$("status").classList.contains("error"), true);
  await page.advance(1);
  assert.equal(page.$("status").textContent, "Pushed.");
  // Pushes with the same message do not write the status line again.
  const writes = page.$("status").writes;
  await page.push(page.snapshot({ message: "Pushed." }));
  await page.push(page.snapshot({ message: "Pushed." }));
  assert.equal(page.$("status").writes, writes);
});

test("the fake socket refuses the frames that the service refuses", async () => {
  const page = await ready();
  const refused = [
    { type: "keys", id: 1, body: { seq: 1, held: [] }, extra: true },
    { type: "keys", body: { seq: 1, held: [] } },
    { type: "keys", id: -1, body: {} },
    { type: "keys", id: 1.5, body: {} },
    { type: "keys", id: "1", body: {} },
    { type: "keys", id: 2 ** 53 + 2, body: {} },
    { type: "ping", id: 1, body: {} },
    { type: "keys", id: 1, body: [] },
    { type: "keys", id: 1, body: null },
    hello(),
    { type: "command", id: 1, body: { action: "x".repeat(4100) } },
  ];
  for (const message of refused) page.socket.send(JSON.stringify(message));
  page.socket.send("not json");
  page.socket.send(JSON.stringify({ type: "keys", id: 2 ** 53, body: { seq: 1, held: [] } }));
  const hellos = [
    { type: "hello", client: CLIENT_ID, token: "" },
    { ...hello(), protocol: "1" },
    { ...hello(), protocol: 2 },
    { ...hello(), token: null },
    { ...hello(), client: "tab\there" },
    { ...hello(), client: "   " },
    { ...hello(), client: "x".repeat(129) },
    { type: "keys", id: 1, body: {} },
  ];
  for (const message of hellos) {
    const fresh = browser();
    fresh.socket.readyState = 1;
    fresh.socket.send(JSON.stringify(message));
  }
  const found = violations.splice(0);
  assert.equal(found.length, refused.length + 1 + hellos.length, found.join("\n"));
});

test("without a socket the page reads the state over HTTP, so Stop arm and Disable robot work", async () => {
  // Every slot is taken: the service refuses this page's socket before any state push.
  const page = browser();
  await page.open();
  await page.message({ type: "error", status: 503, error: "Too many open panel connections" });
  await page.serverClose(1013);
  const read = page.http().find(request => request.path === "api/state");
  assert.ok(read, "the page did not read the state over HTTP");
  assert.equal(read.options.method ?? "GET", "GET");
  assert.equal(read.options.headers["X-MG400-Client"], page.clientId);
  assert.equal(read.timeout, 2500);
  await page.respond(read, page.snapshot({ controller_id: "other-page", busy: true }));
  assert.equal(page.evaluate("connected"), false);
  assert.equal(page.$("stop").hidden, false);
  assert.equal(page.$("stop").disabled, false);
  assert.equal(page.$("disable").disabled, false);
  for (const id of ["home", "execute", "enable", "startSync", "stopSync", "keyboard"]) {
    assert.equal(page.$(id).disabled, true, `${id} is enabled without a socket`);
  }
  assert.equal(page.$("syncState").textContent, "Reconnecting…");
  await page.click("stop");
  assertPanelPost(page.commands("stop").at(-1), "stop");
  // A late HTTP state never replaces a live socket state.
  await page.advance(500);
  await page.open();
  await page.push(page.snapshot({ message: "Live." }));
  const late = page.http().filter(request => request.path === "api/state");
  assert.equal(late.length, 1);
  assert.equal(page.$("status").textContent, "Live.");
});

test("a failed Stop arm stays visible across a reconnect; a connection error does not", async () => {
  const page = await ready({ busy: true, message: "Robot operation started: home." });
  await page.serverClose(1006);
  assert.equal(page.$("status").textContent, "Connection lost. Reconnecting…");
  await page.click("stop");
  await page.fail(page.commands("stop").at(-1), new Error("Failed to fetch"));
  assert.equal(page.$("status").textContent, "Failed to fetch");
  await page.advance(500);
  await page.open();
  await page.push(page.snapshot({ busy: true, message: "Robot operation started: home." }));
  assert.equal(page.$("status").textContent, "Failed to fetch", "the failed stop was hidden at once");
  assert.equal(page.$("status").classList.contains("error"), true);
  // A plain connection error clears as soon as the page is back.
  await page.advance(3000);
  await page.serverClose(1006);
  await page.advance(1000);
  await page.open();
  await page.push(page.snapshot({ message: "Back." }));
  assert.equal(page.$("status").textContent, "Back.");
});

test("a requested disarm waits for the socket and never hides a failed Stop arm", async () => {
  const page = await ready({ keyboard_enabled: true });
  await page.serverClose(1006);
  await page.fire("blur");
  assert.equal(page.disarms().length, 0, "a disarm was attempted without a socket");
  page.setFocus(true);
  await page.click("stop");
  await page.fail(page.commands("stop").at(-1), new Error("Failed to fetch"));
  assert.equal(page.$("status").textContent, "Failed to fetch");
  // Any render while keys still show ON retries the disarm. Without a socket it must wait.
  page.evaluate("render(state)");
  await page.advance(0);
  assert.equal(page.$("status").textContent, "Failed to fetch");
  assert.equal(page.disarms().length, 0);
  await page.advance(500);
  await page.open();
  // The service still shows keys ON (a replaced socket), so the page sends the disarm now.
  await page.push(page.snapshot({ keyboard_enabled: true }));
  assertDisarm(page.disarms().at(-1), "focus");
});

test("pageshow from the back/forward cache shows the disconnected view at once", async () => {
  const page = await ready({ keyboard_enabled: true });
  assert.equal(page.$("home").disabled, false);
  await page.fire("pageshow", { persisted: true });
  assert.equal(page.$("syncState").textContent, "Reconnecting…");
  assert.equal(page.$("syncDot").classList.contains("on"), false);
  for (const id of ["home", "execute", "keyboard", "stopSync"]) {
    assert.equal(page.$(id).disabled, true, `${id} stayed enabled after pageshow`);
  }
  assert.equal(page.$("stop").disabled, false);
  assert.equal(page.$("disable").disabled, false);
});

test("a failed Stop arm stays visible through failed reconnect attempts for its 3 s", async () => {
  const page = await ready({ busy: true, message: "Robot operation started: home." });
  await page.serverClose(1006);
  await page.click("disable");
  await page.fail(page.commands("disable").at(-1), new Error("Failed to fetch"));
  await page.advance(500);
  await page.serverClose(1006);
  assert.equal(page.$("status").textContent, "Failed to fetch", "a refused retry hid the failed disable");
  await page.advance(1000);
  await page.serverClose(1006);
  assert.equal(page.$("status").textContent, "Failed to fetch");
  await page.advance(1500);
  assert.equal(page.$("status").textContent, "Connection lost. Reconnecting…");
});

test("without a socket the page polls the state every second, one read at a time, until state arrives", async () => {
  const page = await ready();
  await page.serverClose(1006);
  const reads = () => page.http().filter(request => request.path === "api/state");
  assert.equal(reads().length, 1);
  await page.advance(1000);
  assert.equal(reads().length, 1, "a second read started while the first was in flight");
  await page.respond(reads()[0], page.snapshot({ syncing: false }));
  assert.equal(page.$("stop").disabled, true);
  await page.advance(1000);
  assert.equal(reads().length, 2);
  // Another client starts motion; the next read enables Stop arm within a second.
  await page.respond(reads()[1], page.snapshot({ syncing: true, controller_id: "other-page" }));
  assert.equal(page.$("stop").disabled, false);
  // Once the socket delivers state, polling stops.
  await page.advance(1000);
  await page.respond(reads()[2], page.snapshot());
  await page.open(page.sockets.at(-1));
  await page.push(page.snapshot({ message: "Live." }));
  const count = reads().length;
  await page.advance(5000);
  assert.equal(reads().length, count);
});

test("a refused handshake with a missing token shows the token form from the HTTP read", async () => {
  const page = browser();
  await page.serverClose(1006);
  const read = page.http().find(request => request.path === "api/state");
  await page.respond(read, { error: "Enter the panel access token" }, 401);
  assert.equal(page.$("auth").hidden, false);
  page.$("token").value = "secret-token-1234567890abc";
  await page.submit("auth");
  await page.advance(1000);
  const next = page.http().filter(request => request.path === "api/state").at(-1);
  assert.equal(next.options.headers.Authorization, "Bearer secret-token-1234567890abc");
});

test("pagehide stops the state polling", async () => {
  const page = await ready();
  await page.serverClose(1006);
  const reads = () => page.http().filter(request => request.path === "api/state");
  await page.respond(reads()[0], page.snapshot());
  await page.fire("pagehide");
  const count = reads().length;
  await page.advance(5000);
  assert.equal(reads().length, count);
});
