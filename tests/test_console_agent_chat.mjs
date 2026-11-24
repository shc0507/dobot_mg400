import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
const require = createRequire(import.meta.url);
const { ChatClient, ActivityTimeline, activity, phaseLabel, tabFromHash } = require("../src/orchestrator/agent_connect/static/chat-portal.js");
const ready = () => ({ model: { configured: true, model: "test-model" }, busy: false, messages: [], active: null });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function client(request) { const value = new ChatClient({ request, createId: () => "message-1" }); value.status = ready(); return value; }

test("one explicit message posts its source and ID without a confirmation request", async () => {
  const calls = [], task = deferred();
  const instance = client(async (path, body, source) => { calls.push({ path, body, source }); return path === "message" ? task.promise : ready(); });
  const first = instance.send(" Move X to 2 rotations. ");
  assert.equal(await instance.send("Move X to 3 rotations."), false);
  assert.deepEqual(calls[0], { path: "message", body: { source: "live", message: "Move X to 2 rotations.", request_id: "message-1" }, source: "live" });
  task.resolve({ ...ready(), busy: true });
  assert.equal(await first, true);
  assert.equal(calls.filter(value => value.path === "message").length, 1);
});

test("an uncertain message is not resent automatically and an explicit retry keeps its ID", async () => {
  const calls = [], instance = client(async (path, body) => { if (path === "message") { calls.push(body); throw new Error("Connection lost."); } return ready(); });
  let ids = 0; instance.createId = () => `message-${++ids}`;
  assert.equal(await instance.send("Move X to 2 rotations."), false);
  await instance.refresh();
  assert.equal(calls.length, 1);
  assert.match(instance.error, /No request was sent again automatically/);
  assert.equal(await instance.send("Move X to 2 rotations."), false);
  assert.equal(calls[0].request_id, calls[1].request_id);
  assert.equal(ids, 1);
});

test("a status response started before a message cannot replace its result", async () => {
  const old = deferred(), current = { ...ready(), messages: [{ id: "new", text: "Submitted" }] };
  const instance = client((path) => path === "status" ? old.promise : Promise.resolve(current));
  const pending = instance.refresh();
  await instance.send("Move X to 2 rotations.");
  old.resolve({ ...ready(), messages: [{ id: "old" }] });
  await pending;
  assert.deepEqual(instance.status.messages, current.messages);
});

test("source switching cancels old inference and ignores its late response", async () => {
  const old = deferred(), calls = [];
  const instance = client(async (path, body, source) => { calls.push({ path, body, source }); return path === "message" ? old.promise : ready(); });
  const request = instance.send("Move X to 2 rotations.");
  await instance.setSource("mock");
  assert.equal(instance.source, "mock");
  assert.deepEqual(calls.find(value => value.path === "cancel"), { path: "cancel", body: { source: "live" }, source: "live" });
  old.resolve({ ...ready(), messages: [{ text: "Old live message" }] });
  assert.equal(await request, false);
  assert.deepEqual(instance.status.messages, []);
  assert.equal(instance.sending, false);
});

test("cancel is available during inference and a late response does not undo it", async () => {
  const old = deferred();
  const instance = client(async path => path === "message" ? old.promise : ready());
  const request = instance.send("Move X to 2 rotations.");
  await instance.cancel();
  old.resolve({ ...ready(), busy: true, messages: [{ text: "Late" }] });
  assert.equal(await request, false);
  assert.deepEqual(instance.status.messages, []);
});

test("settings send the key only to the local settings route, without retaining it in state", async () => {
  const calls = [], instance = client(async (path, body) => { calls.push({ path, body }); return { ...ready(), messages: [{ text: "Other source" }] }; });
  await instance.configure(" test-model ", "private-key");
  assert.deepEqual(calls[0], { path: "settings", body: { model: "test-model", api_key: "private-key" } });
  assert.equal(JSON.stringify(instance.status).includes("private-key"), false);
  assert.equal(JSON.stringify(instance.pending).includes("private-key"), false);
});

test("hiding the page stops UI polling without canceling server work", () => {
  const calls = [], instance = client((...args) => { calls.push(args); return Promise.resolve(ready()); });
  instance.sending = true; instance.stop();
  assert.deepEqual(calls, []);
  assert.equal(instance.sending, false);
});

test("activity distinguishes interpretation from command completion", () => {
  assert.match(activity({ busy: true }), /Interpreting/);
  assert.match(activity({ active: { action: "stage.position", status: "running" } }), /running/);
  assert.match(activity(ready()), /Ready/);
  assert.match(activity(null), /Set up/);
  assert.equal(activity({ busy: true, activity: [{ phase: "reading" }] }), "Reading Console state");
  assert.equal(activity({ busy: true, activity: [{ phase: "cancelling" }] }), "Cancellation requested");
  assert.equal(phaseLabel("__proto__"), "Activity update");
});

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.dataset = {}; this.textContent = "";
    this.className = ""; this.scrollHeight = 500; this.scrollTop = 200; this.clientHeight = 300;
    this.classList = { toggle: (name, enabled) => { const values = new Set(this.className.split(" ").filter(Boolean)); enabled ? values.add(name) : values.delete(name); this.className = [...values].join(" "); } };
  }
  set innerHTML(_) { throw new Error("Activity must render as text."); }
  append(...nodes) { for (const node of nodes) { node.remove(); node.parent = this; this.children.push(node); } }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; }
  replaceChildren(...nodes) { for (const node of [...this.children]) node.remove(); this.append(...nodes); }
}
function timeline() {
  const document = { createElement: tag => new Element(tag) };
  return new ActivityTimeline({ document, list: new Element("ol"), empty: new Element("p"), summary: new Element("span") });
}
function descendants(node, tag) { return node.children.flatMap(child => [...(child.tagName === tag ? [child] : []), ...descendants(child, tag)]); }

test("activity renders each progress phase and preserves the exact command and result as text", () => {
  const view = timeline(), markup = '<img src=x onerror="alert(1)"><script>bad()</script>';
  const command = { request_id: "command-1", action: "robot.jog", params: { axis: "z", direction: 1, step: 1.25, speed: 1 }, note: markup };
  const result = { status: "failed", error: markup, detail: { code: "device_busy" } };
  const phases = ["reading", "interpreting", "validating", "sending", "executing", "completed", "failed", "cancelling", "cancelled"];
  const entries = phases.map((phase, index) => ({ id: String(index), at: "2026-11-24T12:10:00Z", request_id: "message-1", phase, text: markup }));
  entries[3].command = command; entries[6].result = result;
  view.render(entries, "live");
  assert.equal(view.list.children.length, phases.length);
  assert.deepEqual(view.list.children.map(row => row.dataset.phase), phases);
  assert.deepEqual(descendants(view.list, "pre").map(pre => JSON.parse(pre.textContent)), [command, result]);
  assert.equal(descendants(view.list, "script").length, 0);
  assert.equal(descendants(view.list, "img").length, 0);
  assert.ok(descendants(view.list, "p").every(node => node.textContent === markup));
  assert.equal(descendants(view.list, "time")[0].dateTime, entries[0].at);
  assert.equal(view.summary.textContent, "Cancelled");
  assert.equal(view.empty.hidden, true);
});

test("new activity preserves expanded command JSON and reading position across polls", () => {
  const view = timeline(), first = { id: "1", phase: "sending", command: { action: "stage.position", params: { axis: "top", position: 2 } } };
  view.render([first], "live");
  const row = view.list.children[0], details = descendants(row, "details")[0]; details.open = true;
  view.list.scrollTop = 0;
  view.render([{ ...first }], "live");
  view.render([first, { id: "2", phase: "completed", result: { status: "succeeded" } }], "live");
  assert.equal(view.list.children[0], row);
  assert.equal(descendants(row, "details")[0], details);
  assert.equal(details.open, true);
  assert.equal(view.list.scrollTop, 0);
  assert.equal(view.summary.textContent, "Completed");
  assert.match(view.list.children[1].className, /latest/);
  assert.doesNotMatch(row.className, /latest/);
});

test("activity follows server history trimming and separates live from simulated history", () => {
  const view = timeline(), first = { id: "1", phase: "reading" }, second = { id: "2", phase: "completed", result: null };
  view.render([first, second], "live");
  const retained = view.list.children[1];
  view.render([second], "live");
  assert.deepEqual(view.list.children, [retained]);
  assert.equal(descendants(retained, "pre")[0].textContent, "null");
  view.render([{ id: "2", phase: "reading", text: "Simulation" }], "mock");
  assert.notEqual(view.list.children[0], retained);
  assert.equal(descendants(view.list, "p")[0].textContent, "Simulation");
  view.render([], "mock");
  assert.equal(view.list.children.length, 0);
  assert.equal(view.empty.hidden, false);
  assert.equal(view.summary.textContent, "No activity yet");
});

test("workspace hashes preserve manual deep links and Agent is the default", () => {
  for (const hash of ["#manual", "#manual-testing", "#agent-commands", "#live-view"]) assert.equal(tabFromHash(hash), "manual");
  for (const hash of ["", "#agent", "#unknown"]) assert.equal(tabFromHash(hash), "agent");
});

test("shared control and Stop stay outside both workspaces; manual controls keep their IDs", () => {
  const html = fs.readFileSync(new URL("../src/orchestrator/agent_connect/static/portal.html", import.meta.url), "utf8");
  assert.ok(html.indexOf('id="agent-stop-panel"') < html.indexOf('id="agent-chat-view"'));
  assert.ok(html.indexOf('id="agent-approval"') < html.indexOf('id="agent-chat-view"'));
  assert.match(html, /id="manual-testing"[^>]+hidden/);
  for (const id of ["agent-command-form", "agent-command-send", "live-devices", "enable-tests", "advanced-tests"]) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(html, /id="chat-api-key" type="password"/);
  assert.match(html, /data-workspace="manual"/);
  const source = fs.readFileSync(new URL("../src/orchestrator/agent_connect/static/chat-portal.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /localStorage|sessionStorage|innerHTML/);
});
