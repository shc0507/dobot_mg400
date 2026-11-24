import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
const require = createRequire(import.meta.url);
const { CommandClient, commandView, defaultsFor, commandError, mount } = require("../src/orchestrator/agent_connect/static/command-portal.js");
const tick = () => new Promise(resolve => setImmediate(resolve));
const state = {
  phase: 5, connected: true, commands_enabled: true, physical_commands_enabled: false,
  furnace_control_enabled: false, equipment_mode: "simulated", boot_id: "boot-a", control_epoch: 9,
  principal: { id: "session-a", kind: "agent" }, operator: { id: "session-a", kind: "agent" },
};
const catalog = {
  version: 1, contract_id: "sha256:catalog", actions: [
    { id: "stage.move", label: "Move stage", device: "stage", available: true, defaults: { x: 10 }, params: { type: "object" } },
    { id: "furnace.heat", label: "Heat", device: "furnace", available: false, reason: "Furnace control is unavailable." },
  ],
};
function rig(request = async () => ({ request_id: "request-1", session_id: "session-a", status: "running" })) {
  const calls = [], client = new CommandClient({ request: async (path, body, source) => {
    calls.push({ path, body, source }); return path === "commands/catalog" ? catalog : request(path, body, source);
  }, createId: () => "request-1" });
  Object.assign(client, { source: "mock", status: state, catalog, selected: "stage.move" });
  return { client, calls };
}

test("only the current mock operator can send and furnace remains unavailable", () => {
  const { client } = rig();
  assert.equal(client.canSend, true);
  for (const status of [null, { ...state, connected: false }, { ...state, operator: null }, { ...state, principal: { id: "session-a", kind: "human" } }, { ...state, phase: 4 }, { ...state, furnace_control_enabled: true }]) {
    assert.equal(commandView("mock", status).enabled, false);
  }
  assert.equal(commandView("live", state).enabled, false);
  assert.equal(commandView("live", state).label, "Physical commands unavailable");
  client.select("furnace.heat"); assert.equal(client.canSend, false);
  client.catalog = { ...catalog, actions: [{ ...catalog.actions[1], available: true }] };
  assert.equal(client.canSend, false);
});

test("selection, refresh, reconnect and operator grant never submit commands", async () => {
  const { client, calls } = rig(async path => path === "commands/catalog" ? catalog : { items: [], count: 0 });
  client.setContext("mock", { ...state, operator: null }); client.select("stage.move");
  await client.refresh(); client.setContext("mock", state); await client.refresh();
  client.setContext("mock", { ...state, connected: false });
  client.setContext("mock", state); await tick();
  assert.ok(calls.length > 0);
  assert.ok(calls.every(call => ["commands/history", "commands/catalog"].includes(call.path)));
});

const physical = { ...state, phase: 8, control_epoch: "boot-a:9", physical_commands_enabled: true, furnace_control_enabled: true, equipment_mode: "physical", command_contract_id: catalog.contract_id };
test("live operator can explicitly send only with a current command contract", async () => {
  const { client, calls } = rig();
  client.source = "live"; client.status = physical;
  assert.equal(client.canSend, true);
  client.catalog = { ...catalog, contract_id: "old-contract" };
  assert.equal(client.canSend, false);
  client.catalog = catalog;
  await client.send({ x: 1 });
  assert.equal(calls.length, 1); assert.equal(calls[0].source, "live");
  assert.equal(calls[0].body.contract_id, physical.command_contract_id);
});

test("live grants and reconnects do not dispatch and furnace availability comes from the contract", async () => {
  const { client, calls } = rig(async () => ({ items: [] }));
  client.setContext("live", { ...physical, operator: null }); await tick();
  client.setContext("live", physical); await tick();
  client.setContext("live", { ...physical, connected: false });
  client.setContext("live", physical); await tick();
  assert.ok(calls.every(call => !["commands", "commands/cancel"].includes(call.path)));
  client.catalog = { ...catalog, actions: [{ ...catalog.actions[1], available: true }] };
  client.select("furnace.heat"); assert.equal(client.canSend, true);
  client.status = { ...physical, furnace_control_enabled: false }; assert.equal(client.canSend, false);
  client.status = { ...physical, operator: null }; assert.equal(client.canSend, false);
});

test("one deliberate Send creates one stable envelope and blocks another active send", async () => {
  const { client, calls } = rig();
  await client.send({ x: 10 }); await client.send({ x: 20 });
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0], { path: "commands", source: "mock", body: {
    request_id: "request-1", boot_id: "boot-a", contract_id: "sha256:catalog", control_epoch: 9,
    action: "stage.move", params: { x: 10 }, preconditions: {},
  } });
  assert.equal(client.active.request_id, "request-1");
});

test("lost response resolves the original request without another POST", async () => {
  const { client, calls } = rig(async path => {
    if (path === "commands") throw new Error("Network response lost");
    return { request_id: "request-1", session_id: "session-a", status: "succeeded", result: { x: 10 } };
  });
  await client.send({ x: 10 });
  assert.deepEqual(calls.map(call => call.path), ["commands", "commands/request/request-1"]);
  assert.equal(client.items[0].status, "succeeded"); assert.equal(client.unresolved, false);
});

test("unresolved requests remain blocked and polling only looks up the existing ID", async () => {
  let recovered = false;
  const { client, calls } = rig(async path => {
    if (path === "commands/history") return { items: [], count: 0 };
    if (recovered) return { request_id: "request-1", session_id: "session-a", status: "stopped" };
    throw new Error("Connection lost");
  });
  await client.send({ x: 10 });
  assert.equal(client.items[0].status, "unknown"); assert.equal(client.canSend, false);
  await client.send({ x: 10 }); await client.refresh();
  assert.equal(calls.filter(call => call.path === "commands").length, 1);
  recovered = true; await client.refresh();
  assert.equal(client.items[0].status, "stopped"); assert.equal(client.canSend, true);
});

test("known validation rejection with no journal entry does not remain unknown", async () => {
  const { client } = rig(async path => { throw Object.assign(new Error("Invalid parameters"), { status: path === "commands" ? 400 : 404 }); });
  await client.send({ x: "invalid" });
  assert.equal(client.items[0].status, "rejected"); assert.equal(client.canSend, true);
});

test("cancel uses only the agent session's operation ID, including after handover", async () => {
  const { client, calls } = rig(async () => ({ request_id: "mine", status: "stopped" }));
  client.items = [{ request_id: "other", session_id: "session-b", status: "running" }, { request_id: "mine", session_id: "session-a", status: "running" }];
  client.setContext("mock", { ...state, operator: { id: "human" } });
  await client.cancel();
  assert.deepEqual(calls, [{ path: "commands/cancel", body: { request_id: "mine" }, source: "mock" }]);
});

test("source change clears the visible command state and discards late submission results", async () => {
  let finish;
  const { client, calls } = rig(async path => path === "commands" ? new Promise(resolve => { finish = resolve; }) : catalog);
  const sending = client.send({ x: 10 }); await tick();
  client.setContext("live", null); await tick();
  assert.equal(client.items.length, 0); assert.equal(client.canSend, false);
  finish({ request_id: "request-1", status: "succeeded" }); await sending;
  assert.equal(client.items.length, 0);
  assert.equal(calls.filter(call => call.path === "commands").length, 1);
});

test("a late submission response cannot overwrite confirmed cancellation", async () => {
  let finish;
  const { client } = rig(async path => path === "commands" ? new Promise(resolve => { finish = resolve; }) : { request_id: "request-1", session_id: "session-a", status: "stopped" });
  const sending = client.send({ x: 10 }); await tick(); await client.cancel();
  finish({ request_id: "request-1", session_id: "session-a", status: "running" }); await sending;
  assert.equal(client.items[0].status, "stopped"); assert.equal(client.canSend, true);
});

test("late history cannot repopulate another source", async () => {
  let finish;
  const { client } = rig(async path => path === "commands/history" ? new Promise(resolve => { finish = resolve; }) : catalog);
  const reading = client.refresh(); await tick();
  client.setContext("live", null); await tick();
  finish({ items: [{ request_id: "old", status: "running" }] }); await reading;
  assert.equal(client.items.length, 0); assert.equal(client.refreshing, false);
});

test("history restores server results after reconnect without sending an action", async () => {
  const saved = { request_id: "older", action: "stage.move", session_id: "old-session", status: "succeeded" };
  const { client, calls } = rig(async () => ({ items: [saved], count: 1 }));
  client.status = { ...state, connected: false }; client.setContext("mock", state); await tick();
  assert.deepEqual(client.items, [saved]); assert.equal(client.active, undefined);
  assert.deepEqual(calls.map(call => call.path), ["commands/catalog", "commands/history"]);
});

test("parameter defaults come from the descriptor and JSON objects are required", async () => {
  assert.deepEqual(defaultsFor(catalog.actions[0]), { x: 10 });
  assert.deepEqual(defaultsFor({ params: { properties: { amount: { default: 1 }, label: { type: "string" } } } }), { amount: 1 });
  const { client, calls } = rig();
  await assert.rejects(client.send([]), /JSON objects/);
  await assert.rejects(client.send({}, []), /JSON objects/);
  assert.deepEqual(calls, []);
});

test("expanding a history summary fetches full results and later summaries preserve them", async () => {
  const summary = { request_id: "saved:1", action: "stage.move", session_id: "session-a", status: "running", progress: { axis: "rotation", completed: 0 } };
  const full = { ...summary, params: { position: 1 }, result: { position: 1 } };
  const { client, calls } = rig(async path => path === "commands/history" ? { items: [{ ...summary, status: "succeeded", progress: { axis: "rotation", completed: 1 } }] } : full);
  client.items = [summary]; await client.loadDetails("saved:1");
  assert.equal(calls[0].path, "commands/request/saved%3A1");
  assert.deepEqual(client.items[0].params, { position: 1 });
  assert.deepEqual(client.items[0].result, { position: 1 });
  await client.refresh();
  assert.equal(client.items[0].status, "succeeded");
  assert.equal(client.items[0].progress.completed, 1);
  assert.deepEqual(client.items[0].result, { position: 1 });
});

test("phase six loads saved choices and excludes unavailable actions from initial selection", async () => {
  const choices = { actions: { "transfer.position": { name: [{ value: "position-1", label: "Position 1" }] } } };
  const { client, calls } = rig(async path => path === "commands/catalog" ? catalog : path === "commands/choices" ? choices : { items: [] });
  client.status = { ...state, phase: 6 }; client.catalog = null; client.selected = "furnace.heat";
  await client.refresh();
  assert.equal(client.selected, "stage.move");
  assert.deepEqual(client.choices, choices);
  assert.equal(client.canSend, true);
  assert.deepEqual(calls.map(call => call.path), ["commands/catalog", "commands/choices", "commands/history"]);
});

test("a changed command boot discards late choices from the old Console", async () => {
  let oldChoices;
  const next = { actions: { "transfer.position": { name: [{ value: "new", label: "New" }] } } };
  let reads = 0;
  const { client } = rig(async path => path === "commands/choices" ? ++reads === 1 ? new Promise(resolve => { oldChoices = resolve; }) : next : path === "commands/catalog" ? catalog : { items: [] });
  client.status = { ...state, phase: 6 }; client.bootId = state.boot_id;
  const reading = client.refresh(); await tick();
  client.setContext("mock", { ...state, phase: 6, boot_id: "boot-new" }); await tick();
  oldChoices({ actions: { old: {} } }); await reading;
  assert.deepEqual(client.choices, next);
});

test("catalog refresh updates availability without replacing an unchanged catalog", async () => {
  const { client } = rig();
  const initial = client.catalog;
  await client.refresh();
  assert.equal(client.catalog, initial);
  client.request = async path => path === "commands/catalog" ? { ...catalog, actions: [{ ...catalog.actions[0], available: false, reason: "Device disconnected" }, catalog.actions[1]] } : { items: [] };
  await client.refresh();
  assert.equal(client.selected, "");
  assert.equal(client.canSend, false);
});

test("catalog refresh preserves selection when another action changes availability", async () => {
  const { client } = rig();
  const revised = { ...catalog, actions: [catalog.actions[0], { ...catalog.actions[1], reason: "Still disabled" }] };
  client.request = async path => path === "commands/catalog" ? revised : { items: [] };
  await client.refresh();
  assert.equal(client.selected, "stage.move");
  assert.equal(client.catalog.contract_id, catalog.contract_id);
  assert.equal(client.action, catalog.actions[0]);
});

function positionRig() {
  const result = rig();
  const stage = { available: true, fresh: true, phase: "ready", connected: true, age_s: 0, xy_blocked: true, xy_reason: "Insert is not fully out.", axes: ["insert", "top", "bottom"].map(id => ({ id, position_rotations: 0, lower_limit_rotations: 0, upper_limit_rotations: 3.25, positioning_ready: true, telemetry_fresh: true, available: true })) };
  result.client.catalog = { ...catalog, actions: [{ ...catalog.actions[0], id: "stage.position" }] };
  result.client.selected = "stage.position"; result.client.choices = { constraints: { stage } }; result.client.choicesAt = Date.now();
  result.stage = stage; return result;
}

test("absolute stage submission checks fresh calibrated bounds before any POST", async () => {
  const { client, calls } = positionRig();
  await assert.rejects(client.send({ motor: "insert", position: 4 }), /between 0 and 3.25/);
  assert.equal(calls.length, 0);
  await client.send({ motor: "top", position: 0 });
  assert.equal(calls.length, 1); assert.equal(calls[0].body.params.position, 0);
  assert.equal(calls[0].body.action, "stage.position");
});

test("absolute stage commands cannot reuse old bounds after connection loss or source change", () => {
  const { client } = positionRig();
  assert.equal(client.canSend, true);
  client.setContext("mock", { ...state, connected: false });
  assert.equal(client.choices, null); assert.equal(client.stageConstraints, null); assert.equal(client.canSend, false);
  client.setContext("live", null); assert.equal(client.choicesAt, null); assert.equal(client.canSend, false);
});

test("absolute stage constraints expire locally and clear when any refresh read fails", async () => {
  const { client } = positionRig();
  client.choicesAt = Date.now() - 4000; assert.equal(client.canSend, false);
  client.choicesAt = Date.now(); assert.equal(client.canSend, true);
  client.request = async () => { throw new Error("The choices source is unavailable."); };
  await client.refresh();
  assert.equal(client.choices, null); assert.equal(client.choicesAt, null); assert.equal(client.canSend, false);
});

test("a different session cannot accept choices from an earlier session", async () => {
  const { client } = positionRig();
  client.status = { ...state, phase: 6 };
  let release, reads = 0;
  client.request = async path => path === "commands/catalog" ? client.catalog : path === "commands/choices" ? ++reads === 1 ? new Promise(resolve => { release = resolve; }) : { constraints: { stage: null } } : { items: [] };
  const reading = client.refresh(); await tick();
  client.setContext("mock", { ...state, phase: 6, principal: { kind: "agent", id: "session-b" } });
  await tick();
  release({ constraints: { stage: { available: true, old: true } } }); await reading;
  assert.equal(client.stageConstraints, null); assert.equal(client.canSend, false);
});

test("terminal rejection and failure messages remain separate from transport errors", () => {
  assert.equal(commandError({ status: "rejected", error: "Target exceeds the calibrated upper limit." }), "Target exceeds the calibrated upper limit.");
  assert.equal(commandError({ status: "failed", error: { message: "The device lost its reference." } }), "The device lost its reference.");
  assert.match(commandError({ status: "failed" }), /Expand its record/);
  assert.equal(commandError({ status: "running", error: "Old error" }), "");
});

test("the visible command alert shows the saved rejection reason", async () => {
  const previousDocument = globalThis.document, previousListener = globalThis.addEventListener;
  const nodes = new Map();
  const node = tag => ({ tagName: tag.toUpperCase(), children: [], dataset: {}, textContent: "", value: "", hidden: false,
    append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; }, addEventListener() {}, setAttribute() {}, removeAttribute() {},
  });
  globalThis.document = { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node("div")); return nodes.get(id); }, createElement: node };
  globalThis.addEventListener = () => {};
  let client;
  try {
    client = mount({ request: async path => path === "commands/catalog" ? catalog : { items: [] } });
    await tick();
    client.save({ request_id: "bad-position", action: "stage.position", status: "rejected", error: "Target exceeds the calibrated upper limit." });
    client.changed();
    assert.equal(nodes.get("agent-command-error").hidden, false);
    assert.match(nodes.get("agent-command-error").textContent, /Target exceeds the calibrated upper limit/);
    assert.equal(client.error, "");
    assert.match(nodes.get("agent-command-progress").textContent, /rejected/);
  } finally { client?.stop(); globalThis.document = previousDocument; globalThis.addEventListener = previousListener; }
});
