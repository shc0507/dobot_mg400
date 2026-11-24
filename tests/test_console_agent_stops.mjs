import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
const require = createRequire(import.meta.url);
const { StopClient, mergeRecord, senderLabel, auditMessage, sourceMessage } = require("../src/orchestrator/agent_connect/static/stop-panel.js");
const tick = () => new Promise(resolve => setImmediate(resolve));
const sender = { id: "agent-a", name: "Agent A", role: "observer", verified: true };
const record = (id, changes = {}) => ({ id, request_id: id, target: "stage", status: "confirmed", requested_at: 10, updated_at: 11, sender, ...changes });
function rig(handler = async (path, body) => path === "stop" ? record(body.request_id, { target: body.target }) : { items: [], audit: { status: "saved" } }) {
  const calls = []; let id = 0;
  const client = new StopClient({ request: async (path, body, source) => { calls.push({ path, body, source }); return handler(path, body, source); }, createId: () => "stop-" + ++id });
  client.setContext("mock", null);
  return { client, calls };
}

test("Stop does not require connection, current contract or operator status", async () => {
  const { client, calls } = rig();
  await client.send("stage");
  assert.deepEqual(calls[0], { path: "stop", body: { request_id: "stop-1", target: "stage" }, source: "mock" });
  assert.equal(client.items.length, 1);
  assert.equal(client.items[0].status, "confirmed");
  assert.deepEqual(Object.keys(client.items[0].sources), ["console"]);
});

test("another Stop is sent while a prior Stop is waiting", async () => {
  const finishes = [];
  const { client, calls } = rig((path, body) => path === "stop" ? new Promise(resolve => finishes.push(() => resolve(record(body.request_id)))) : { items: [] });
  const first = client.send("stage"), second = client.send("stage");
  assert.equal(calls.filter(item => item.path === "stop").length, 2);
  assert.notEqual(calls[0].body.request_id, calls[1].body.request_id);
  finishes.forEach(finish => finish()); await Promise.all([first, second]);
});

test("live Stop supports furnace and gas targets without a session or status", async () => {
  const { client, calls } = rig();
  client.setContext("live", null);
  for (const target of ["all_motion", "furnace", "gas_all", "gas_0", "gas_1", "gas_2"]) await client.send(target);
  await client.send("unknown-device");
  const stops = calls.filter(call => call.path === "stop");
  assert.deepEqual(stops.map(call => call.body.target), ["all_motion", "furnace", "gas_all", "gas_0", "gas_1", "gas_2"]);
  assert.ok(stops.every(call => call.source === "live"));
});

test("late Stop and history responses cannot contaminate another source", async () => {
  let finish;
  const { client } = rig(() => new Promise(resolve => { finish = resolve; }));
  const sending = client.send("stage"); await tick(); client.setContext("live", null);
  finish(record("stop-1")); await sending;
  assert.deepEqual(client.items, []);
});

test("unknown Stop results do not retry automatically and buttons remain usable", async () => {
  const { client, calls } = rig(async path => { if (path === "stop") throw new Error("Connection failed"); return { items: [], audit: { status: "failed", error: "disk" } }; });
  await client.send("stage"); await tick();
  assert.equal(client.items[0].status, "unknown");
  assert.equal(client.enabled, true);
  assert.equal(calls.filter(item => item.path === "stop").length, 1);
  assert.match(auditMessage(client.audit), /could not be saved/);
  await client.send("robot");
  assert.equal(calls.filter(item => item.path === "stop").length, 2);
});

test("fallback and Console reports retain both senders and outcomes", () => {
  const primary = record("stop-1", { status: "unconfirmed" });
  const fallback = record("stop-1", { sender: { kind: "unknown", role: "unverified", verified: false }, updated_at: 12, relay_source: "fallback" });
  const combined = mergeRecord(mergeRecord(null, primary), fallback, "fallback");
  assert.equal(combined.status, "unconfirmed");
  assert.equal(combined.sources.console.sender.verified, true);
  assert.equal(combined.sources.fallback.sender.verified, false);
  assert.match(senderLabel(combined.sources.console.sender), /Agent A.*observer.*verified/);
  const recovered = mergeRecord(combined, { ...primary, status: "confirmed", updated_at: 13 });
  assert.equal(recovered.status, "confirmed");
  assert.equal(Object.keys(recovered.sources).length, 2);
  assert.equal(mergeRecord(recovered, primary), recovered);
});

test("fallback result replaces the local pending placeholder", async () => {
  const { client } = rig((path, body) => path === "stop" ? record(body.request_id, { relay_source: "fallback", sender: { verified: false } }) : { items: [] });
  await client.send("stage");
  assert.equal(client.items[0].status, "confirmed");
  assert.deepEqual(Object.keys(client.items[0].sources), ["fallback"]);
});

test("merged relay history and details preserve each source's attribution", async () => {
  const primary = record("shared", { sender });
  const fallback = record("shared", { sender: { name: "Fallback caller", role: "unknown", verified: false }, updated_at: 12 });
  const combined = { ...primary, relay_sources: { console: primary, fallback } };
  const { client } = rig(path => path === "stops" ? { items: [combined], source_status: { console: { available: true, audit: { status: "saved" } }, fallback: { available: true, audit: { status: "failed", error: "disk" } } } } : combined);
  await client.refresh(); await client.loadDetails("shared");
  assert.equal(client.items.length, 1);
  assert.deepEqual(Object.keys(client.items[0].sources).sort(), ["console", "fallback"]);
  assert.equal(client.items[0].sources.console.sender.verified, true);
  assert.equal(client.items[0].sources.fallback.sender.verified, false);
  assert.match(sourceMessage(client.sourceStatus), /Fallback: Stop history could not be saved/);
  assert.equal(sourceMessage({ console: { available: false } }), "Console Stop history is unavailable.");
  assert.equal(client.enabled, true);
});
