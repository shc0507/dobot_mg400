import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
const require = createRequire(import.meta.url);
const { ChatClient, ActivityTimeline, ProfileForm, activity, phaseLabel, tabFromHash, providerName, profileView, isLiteralStop, adapterSummary } = require("../src/orchestrator/agent_connect/static/chat-portal.js");
const ready = () => ({ model: { configured: true, model: "test-model" }, busy: false, messages: [], active: null });
const { usageView } = require("../src/orchestrator/agent_connect/static/chat-portal.js");

test("usage distinguishes missing values from reported zero cost", () => {
  const value = usageView([
    { provider: "openai", route: "token_station", model: "vendor/test", input_tokens: 100, output_tokens: 20, total_tokens: 120, elapsed_ms: 1500, status: "completed", cost_usd: 0 },
    { provider: "gemini", model: "test", status: "cancelled", input_tokens: null, cost_usd: null },
  ]);
  assert.equal(value.summary, "2 requests");
  assert.match(value.rows[0].detail, /Input: Not reported/);
  assert.match(value.rows[0].timing, /Cost: Not reported/);
  assert.match(value.rows[1].title, /Token Station/);
  assert.match(value.rows[1].timing, /1.50 s.*\$0.000000/);
  assert.match(usageView([{ input_tokens: -1, cost_usd: Infinity }]).rows[0].timing, /Not reported/);
});

test("history reconciliation requests a read-only lookup operation", async () => {
  const calls = [], instance = client(async (path, body, source) => { calls.push({ path, body, source }); return ready(); });
  await instance.reconcile();
  assert.deepEqual(calls, [{ path: "reconcile", body: { source: "live" }, source: "live" }]);
});
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

const profileList = () => ({ profiles: [{ id: "saved-1", name: "Lab model", provider: "anthropic", model: "test-model", base_url: "https://api.anthropic.com", has_key: true }], active_profile_id: null, storage_available: true, storage_message: "Keys use protected storage on this PC." });

test("saving a profile sends its key once and does not activate it or send an instruction", async () => {
  const calls = [], saved = profileList();
  const instance = client(async (path, body) => { calls.push({ path, body }); return path === "profiles" ? saved : ready(); });
  assert.equal(await instance.addProfile({ name: " Lab model ", provider: "anthropic", model: " test-model ", base_url: " ", api_key: "private-key" }), true);
  assert.deepEqual(calls[0], { path: "profiles", body: { name: "Lab model", route: "direct", provider: "anthropic", model: "test-model", base_url: "", api_key: "private-key" } });
  assert.deepEqual(instance.profiles, saved);
  assert.equal(JSON.stringify(instance).includes("private-key"), false);
  assert.deepEqual(calls.map(call => call.path), ["profiles", "status"]);
});

test("Token Station saves the route and prefixed model with compatibility provider metadata", async () => {
  const calls = [], saved = profileList();
  const instance = client(async (path, body) => { calls.push({ path, body }); return path === "profiles" ? saved : ready(); });
  assert.equal(await instance.addProfile({ name: " Gateway ", route: "token_station", provider: "anthropic", model: " vendor/model-1 ", base_url: " https://physics.bytefuture.ai/v1 ", api_key: "test-key" }), true);
  assert.deepEqual(calls[0], { path: "profiles", body: { name: "Gateway", route: "token_station", provider: "openai", model: "vendor/model-1", base_url: "https://physics.bytefuture.ai/v1", api_key: "test-key" } });
  assert.equal(JSON.stringify(instance).includes("test-key"), false);
  assert.deepEqual(calls.map(call => call.path), ["profiles", "status"]);
});

function profileForm() {
  const fields = Object.fromEntries(["route", "provider", "providerLabel", "model", "baseUrl", "endpoint", "endpointTitle", "endpointNote"].map(name => [name, { value: "" }]));
  fields.route.value = "direct"; fields.provider.value = "openai";
  return new ProfileForm(fields);
}

test("route changes show Token Station defaults and preserve custom endpoint and model input", () => {
  const form = profileForm();
  assert.equal(form.baseUrl.placeholder, "https://api.openai.com/v1");
  assert.equal(form.provider.hidden, false);
  form.provider.value = "anthropic"; form.provider.onchange();
  form.route.value = "token_station"; form.route.onchange();
  assert.equal(form.provider.hidden, true); assert.equal(form.providerLabel.hidden, true); assert.equal(form.provider.disabled, true);
  assert.equal(form.baseUrl.value, "https://physics.bytefuture.ai/v1");
  assert.equal(form.model.placeholder, "openai/gpt-6.1-sol");
  assert.equal(form.endpoint.open, true);
  form.model.value = "vendor/my-model"; form.baseUrl.value = "https://models.example.test/v1";
  form.route.value = "direct"; form.route.onchange();
  assert.equal(form.provider.value, "anthropic");
  assert.equal(form.provider.hidden, false); assert.equal(form.providerLabel.hidden, false); assert.equal(form.provider.disabled, false);
  assert.equal(form.baseUrl.placeholder, "https://api.anthropic.com/v1");
  assert.equal(form.baseUrl.value, "https://models.example.test/v1");
  assert.equal(form.model.value, "vendor/my-model");
  form.provider.value = "gemini"; form.provider.onchange();
  assert.equal(form.baseUrl.placeholder, "https://generativelanguage.googleapis.com/v1beta");
  assert.equal(form.baseUrl.value, "https://models.example.test/v1");
  form.route.value = "token_station"; form.route.onchange();
  assert.equal(form.baseUrl.value, "https://models.example.test/v1");
  assert.equal(form.model.value, "vendor/my-model");
});

test("route changes replace an unchanged default endpoint and form reset restores direct fields", () => {
  const form = profileForm();
  form.baseUrl.value = "https://api.openai.com/v1/";
  form.route.value = "token_station"; form.route.onchange();
  assert.equal(form.baseUrl.value, "https://physics.bytefuture.ai/v1");
  form.route.value = "direct"; form.route.onchange();
  assert.equal(form.baseUrl.value, "");
  form.route.value = "token_station"; form.route.onchange();
  form.route.value = "direct"; form.provider.value = "openai"; form.baseUrl.value = ""; form.model.value = ""; form.update();
  assert.equal(form.provider.disabled, false); assert.equal(form.provider.hidden, false);
  assert.equal(form.baseUrl.value, ""); assert.equal(form.baseUrl.placeholder, "https://api.openai.com/v1");
  assert.equal(form.model.placeholder, "Model ID");
});

test("profile presentation labels legacy profiles as direct and always shows Token Station endpoints", () => {
  assert.deepEqual(profileView({ provider: "anthropic", base_url: "https://api.anthropic.com/v1/" }), { name: "Anthropic", description: "Direct provider · Anthropic", endpoint: "" });
  assert.deepEqual(profileView({ route: "token_station", provider: "openai" }), { name: "Token Station", description: "Token Station", endpoint: "https://physics.bytefuture.ai/v1" });
  assert.equal(profileView({ route: "token_station", provider: "gemini", base_url: "https://gateway.example.test/v1" }).endpoint, "https://gateway.example.test/v1");
});

test("activation, deactivation, and deletion use explicit profile routes only", async () => {
  const calls = [], instance = client(async (path, body) => { calls.push({ path, body }); return path === "status" ? ready() : profileList(); });
  await instance.activateProfile("saved-1");
  await instance.activateProfile(null);
  await instance.deleteProfile("saved-1");
  assert.deepEqual(calls.filter(call => call.path !== "status"), [
    { path: "profiles/activate", body: { profile_id: "saved-1" } },
    { path: "profiles/activate", body: { profile_id: null } },
    { path: "profiles/delete", body: { profile_id: "saved-1" } },
  ]);
});

test("profile mutations cannot run during chat or overlap another mutation", async () => {
  const task = deferred(), calls = [], instance = client(async path => { calls.push(path); return path === "status" ? ready() : task.promise; });
  instance.status.busy = true;
  assert.equal(await instance.activateProfile("saved-1"), false);
  assert.deepEqual(calls, []);
  instance.status.busy = false;
  const first = instance.activateProfile("saved-1");
  assert.equal(instance.busy, true);
  assert.equal(await instance.deleteProfile("saved-1"), false);
  assert.equal(await instance.send("Read stage coordinates."), false);
  task.resolve(profileList()); await first;
  assert.equal(instance.profileSaving, false);
  assert.deepEqual(calls, ["profiles/activate", "status"]);
});

test("a stale profiles read cannot replace a completed mutation", async () => {
  const stale = deferred(), saved = profileList();
  const instance = client(async path => path === "profiles" ? stale.promise : path === "status" ? ready() : saved);
  const read = instance.refreshProfiles();
  await instance.activateProfile("saved-1");
  stale.resolve({ profiles: [], active_profile_id: null }); await read;
  assert.deepEqual(instance.profiles, saved);
});

test("a profile change waits for matching model status before another instruction", async () => {
  const status = deferred(), calls = [], saved = { ...profileList(), active_profile_id: "saved-1" };
  const instance = client(async path => { calls.push(path); return path === "status" ? status.promise : saved; });
  await instance.activateProfile("saved-1");
  assert.equal(instance.modelReady, false);
  assert.equal(await instance.send("Read stage coordinates."), false);
  status.resolve({ ...ready(), model: { configured: true, model: "test-model", profile_id: "saved-1" } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(instance.modelReady, true);
  instance.profiles = { ...saved, active_profile_id: null };
  assert.equal(instance.modelReady, false);
  assert.deepEqual(calls, ["profiles/activate", "status"]);
});

test("model status changes from another tab refresh the saved profile list", async () => {
  const saved = { ...profileList(), active_profile_id: "saved-1" }, calls = [];
  const instance = client(async path => { calls.push(path); return saved; });
  instance.profiles = profileList();
  instance.apply({ ...ready(), model: { configured: true, profile_id: "saved-1" } });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, ["profiles"]);
  assert.equal(instance.profiles.active_profile_id, "saved-1");
  assert.equal(instance.modelReady, true);
});

test("literal Stop remains available without a model and during an adapter update", async () => {
  const calls = [], instance = client(async (path, body) => { calls.push({ path, body }); return ready(); });
  instance.status = { model: { configured: false }, busy: true, adaptation: { status: "update_required" } };
  instance.profileSaving = true;
  assert.equal(instance.canSend("Read stage coordinates."), false);
  for (const text of [" Stop!! ", "stop all", "stop all motion", "STOP ROBOT.", "stop stage", "stop microscope", "stop furnace", "stop gas"]) {
    assert.equal(isLiteralStop(text), true); assert.equal(instance.canSend(text), true);
  }
  for (const text of ["", "stop gas 0", "move and stop", "stopwatch"]) assert.equal(isLiteralStop(text), false);
  assert.equal(await instance.send("Stop robot!"), true);
  assert.equal(calls[0].path, "message");
  assert.equal(calls[0].body.message, "Stop robot!");
});

test("literal Stop can interrupt inference without a late response clearing its pending state", async () => {
  const first = deferred(), stop = deferred(); let count = 0;
  const instance = client(async path => path === "message" ? (++count === 1 ? first.promise : stop.promise) : ready());
  const normal = instance.send("Read stage coordinates.");
  const stopping = instance.send("Stop");
  first.resolve(ready()); assert.equal(await normal, false);
  assert.equal(instance.sending, true);
  stop.resolve(ready()); assert.equal(await stopping, true);
  assert.equal(instance.sending, false);
});

test("profile errors keep the saved list and do not repeat the mutation", async () => {
  const calls = [], instance = client(async path => { calls.push(path); if (path === "status") return ready(); throw new Error("Protected storage is unavailable."); });
  const saved = profileList(); instance.profiles = saved;
  assert.equal(await instance.deleteProfile("saved-1"), false);
  assert.equal(instance.profiles, saved);
  assert.equal(instance.profileError, "Protected storage is unavailable.");
  assert.equal(instance.profileSaving, false);
  assert.deepEqual(calls, ["profiles/delete", "status"]);
});

test("adapter readiness and links use reviewed repository documents", () => {
  const pending = adapterSummary(null);
  assert.equal(pending.label, "Checking");
  assert.equal(pending.documents.length, 3);
  assert.ok(pending.documents.every(doc => doc.url.startsWith("https://github.com/shc0507/dobot_mg400/blob/main/docs/agent-connect/")));
  const changed = adapterSummary({ status: "update_required", message: "Review changed position units.", documents: [{ title: "Unsafe", url: "javascript:alert(1)" }] });
  assert.equal(changed.label, "Update required");
  assert.equal(changed.warning, true);
  assert.equal(changed.message, "Review changed position units.");
  assert.deepEqual(changed.documents, pending.documents);
  assert.equal(adapterSummary({ status: "ready" }).label, "Ready");
  assert.match(activity({ ...ready(), adaptation: { status: "update_required" } }), /needs an update/);
  assert.deepEqual(["openai", "anthropic", "gemini"].map(providerName), ["OpenAI", "Anthropic", "Gemini"]);
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

test("workspace hashes route incoming data to Digital Twin and keep command links in Manual testing", () => {
  for (const hash of ["#digital-twin", "#live-view", "#live-devices", "#live-samples", "#live-operations", "#live-events", "#live-raw", "#live-agreement"]) assert.equal(tabFromHash(hash), "digital-twin");
  for (const hash of ["#manual", "#manual-testing", "#agent-commands"]) assert.equal(tabFromHash(hash), "manual");
  for (const hash of ["", "#agent", "#unknown"]) assert.equal(tabFromHash(hash), "agent");
});

test("shared control and Stop stay outside all workspaces; incoming data stays separate from manual tools", () => {
  const html = fs.readFileSync(new URL("../src/orchestrator/agent_connect/static/portal.html", import.meta.url), "utf8");
  assert.ok(html.indexOf('id="agent-stop-panel"') < html.indexOf('id="agent-chat-view"'));
  assert.ok(html.indexOf('id="agent-approval"') < html.indexOf('id="agent-chat-view"'));
  assert.match(html, /id="manual-testing"[^>]+hidden/);
  assert.match(html, /id="digital-twin"[^>]+aria-labelledby="tab-digital-twin"[^>]+hidden/);
  const twin = html.slice(html.indexOf('<section id="digital-twin"'), html.indexOf('<section id="manual-testing"'));
  const manual = html.slice(html.indexOf('<section id="manual-testing"'));
  for (const id of ["live-view", "live-devices", "live-samples", "live-operations", "live-events", "live-raw", "live-agreement", "live-last-message", "live-download"]) {
    assert.match(twin, new RegExp(`id="${id}"`)); assert.doesNotMatch(manual, new RegExp(`id="${id}"`));
  }
  for (const id of ["agent-command-form", "agent-command-send", "enable-tests", "advanced-tests"]) {
    assert.match(manual, new RegExp(`id="${id}"`)); assert.doesNotMatch(twin, new RegExp(`id="${id}"`));
  }
  assert.match(html, /id="chat-api-key" type="password"/);
  assert.match(html, /id="chat-route"><option value="direct">Direct provider<\/option><option value="token_station">Token Station<\/option>/);
  assert.match(html, /data-workspace="manual"/);
  const source = fs.readFileSync(new URL("../src/orchestrator/agent_connect/static/chat-portal.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /localStorage|sessionStorage|innerHTML/);
});
