import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { runInNewContext } from "node:vm";
const require = createRequire(import.meta.url);
const { controlView, processStatus, ControlClient } = require("../src/orchestrator/agent_connect/static/control-portal.js");
const owner = require("../src/orchestrator/agent_connect/static/control-owner.js");
const tick = () => new Promise(resolve => setImmediate(resolve));
test("offline refresh preserves saved pairing status without inventing operator access", async () => {
  const local = { continuity: { paired: true, state: "offline", connect_intent: true, name: "Saved agent", message: "The Console is offline." } };
  const error = Object.assign(new Error("Offline"), { localAgent: local });
  const client = new ControlClient({ request: async () => { throw error; } });
  await client.refresh();
  assert.deepEqual(client.status.local_agent, local);
  const view = controlView(client.status);
  assert.equal(view.paired, true);
  assert.equal(view.connected, false);
  assert.equal(view.mine, false);
  assert.equal(view.role, "Saved pairing · Offline");
});
const operator = {
  paired: true, connected: true, client_status: "approved", control_epoch: "grant-1",
  principal: { id: "session-a", kind: "agent", name: "A" },
  operator: { id: "session-a", kind: "agent", name: "A" },
};

test("connection access and connection never imply operator permission", () => {
  assert.equal(controlView({ paired: true, client_status: "pending" }).role, "Use a new invitation");
  assert.equal(controlView({ paired: true, client_status: "approved" }).role, "Access granted · Disconnected");
  assert.equal(controlView({ ...operator, operator: null }).role, "Observer");
  assert.equal(controlView(operator).mine, true);
  assert.equal(controlView({ ...operator, connected: false }).mine, false);
  assert.equal(controlView({ ...operator, principal: { name: "A" } }).mine, false);
  assert.equal(controlView({ paired: true, client_status: "revoked" }).role, "Access revoked");
});

test("pending and revoked clients cannot infer access from stale connection fields", () => {
  for (const client_status of ["pending", "revoked"]) {
    const view = controlView({ ...operator, client_status, approved: true });
    assert.equal(view.approved, false);
    assert.equal(view.mine, false);
  }
});

test("process status separates raw background receipt from verified browser data", () => {
  const local_agent = { heartbeat_interval_s: 2, read_collection: { running: true, received_at: 100 } };
  assert.match(processStatus({ local_agent }, 103), /PC process sends heartbeats every 2 seconds/);
  assert.match(processStatus({ local_agent }, 103), /raw data is unverified\. Last received 3 seconds ago/);
  local_agent.read_collection.error = "The PC process cannot read the latest state.";
  assert.match(processStatus({ local_agent }, 103), /cannot read the latest state/);
  assert.doesNotMatch(processStatus({ local_agent }, 103), /Last received/);
});

const staticFile = name => readFileSync(new URL(`../src/orchestrator/agent_connect/static/${name}`, import.meta.url), "utf8");
function domNode(tag = "div") {
  return {
    tag, children: [], textContent: "", value: "", hidden: false, disabled: false, events: {},
    classList: { add() {} },
    setAttribute(name, value) { this[name] = value; },
    addEventListener(name, callback) { this.events[name] = callback; },
    append(...children) { for (const child of children) { child.remove(); child.parent = this; this.children.push(child); } },
    insertBefore(child, before) { child.remove(); child.parent = this; this.children.splice(before ? this.children.indexOf(before) : this.children.length, 0, child); },
    replaceChildren(...children) { for (const child of [...this.children]) child.remove(); this.append(...children); },
    remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); this.parent = null; },
  };
}
function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }
function portalFixture(initial, responses = {}) {
  const html = staticFile("portal.html"), calls = [];
  const nodes = Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], domNode()]));
  nodes["agent-pair-name"].value = "Test agent";
  let status = initial;
  runInNewContext(staticFile("control-portal.js"), {
    document: { getElementById: id => nodes[id] }, AbortController,
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1, clearInterval() {}, addEventListener() {},
    fetch: async (url, options) => {
      const path = url.split("/api/agent-control/")[1].split("?")[0];
      calls.push({ path, body: options.body ? JSON.parse(options.body) : undefined });
      status = responses[path] || status;
      return { ok: true, json: async () => status };
    },
  });
  return { nodes, calls, html };
}

test("using an invitation grants access but requires an explicit connection", async () => {
  const approved = { paired: true, connected: false, client_status: "approved", name: "Test agent" };
  const { nodes, calls, html } = portalFixture({ paired: false, connected: false }, {
    pair: approved, connect: { ...operator, operator: null },
  });
  await tick();
  assert.match(html, /Agent connection and control/);
  assert.match(html, /id="agent-pair">Use invitation<\/button>/);
  assert.doesNotMatch(html, /agent-control-match|Request approval/);
  nodes["agent-pair-invitation"].value = "AB12-CD34";
  nodes["agent-pair-form"].onsubmit({ preventDefault() {} });
  await tick();
  assert.deepEqual(calls.map(call => call.path), ["status", "pair"]);
  assert.deepEqual(calls[1].body, { invitation: "AB12-CD34", name: "Test agent", source: "live" });
  assert.equal(nodes["agent-pair-invitation"].value, "");
  assert.equal(nodes["agent-pair-form"].hidden, true);
  assert.equal(nodes["agent-control-role"].textContent, "Access granted · Disconnected");
  assert.equal(nodes["agent-control-connect"].disabled, false);
  assert.equal(nodes["agent-control-start"].disabled, true);
  await nodes["agent-control-connect"].onclick();
  assert.equal(calls.at(-1).path, "connect");
  assert.equal(nodes["agent-control-role"].textContent, "Observer");
  assert.equal(nodes["agent-control-start"].disabled, true);
});

test("legacy pending portal clients can use a new invitation without approval controls", async () => {
  const { nodes, calls } = portalFixture({ paired: true, connected: false, client_status: "pending", pairing_code: "OLD-CODE" });
  await tick();
  assert.equal(nodes["agent-pair-form"].hidden, false);
  assert.equal(nodes["agent-control-connect"].disabled, true);
  assert.equal(nodes["agent-control-role"].textContent, "Use a new invitation");
  assert.equal(nodes["agent-control-session"].textContent, "Use a new invitation from the Console.");
  assert.deepEqual(calls.map(call => call.path), ["status"]);
});

test("physical operator and connection-loss warning are explicit in the portal", async () => {
  const status = { ...operator, equipment_mode: "physical", watchdog: { policy: "warn_only", shutdown_enabled: false, pending: null, active: { sender: { name: "Agent A" } } } };
  const { nodes, calls } = portalFixture(status);
  await tick();
  assert.equal(nodes["agent-control-role"].textContent, "Operator · Physical commands");
  assert.equal(nodes["agent-control-watchdog"].hidden, false);
  assert.equal(nodes["agent-control-watchdog"].textContent, "Agent A: Agent connection lost. Motion control is released. Furnace follows its existing run/recovery behavior.");
  assert.ok(calls.every(call => call.path === "status"));
  assert.match(nodes["agent-control-session"].textContent, /PC process sends heartbeats/);
});

test("only the Console offers an explicit warning dismissal", () => {
  const context = { document: { createElement: domNode } };
  runInNewContext(staticFile("control-owner.js"), context);
  const panel = Object.create(context.AgentOwnerPanel.OwnerPanel.prototype), calls = [];
  panel.container = domNode();
  panel.busy = false;
  panel.status = { management_available: true, equipment_mode: "physical", watchdog: { active: { sender: { name: "Agent A" } } } };
  panel.action = (path, body) => calls.push({ path, body });
  panel.build();
  assert.equal(panel.nodes.dismissWarning.textContent, "Dismiss warning");
  assert.equal(panel.nodes.warningPanel.hidden, false);
  assert.equal(panel.nodes.dismissWarning.disabled, false);
  assert.match(panel.nodes.boundary.textContent, /Physical equipment/);
  assert.match(panel.nodes.warning.textContent, /Furnace follows its existing run\/recovery behavior/);
  assert.deepEqual(calls, []);
  panel.nodes.dismissWarning.events.click();
  assert.equal(calls[0].path, "acknowledge-warning");
  assert.equal(JSON.stringify(calls[0].body), "{}");
  panel.status.watchdog.active = null; panel.render();
  assert.equal(panel.nodes.warningPanel.hidden, true);
  assert.doesNotMatch(staticFile("control-portal.js"), /acknowledge-warning/);
});

test("Console client rows offer explicit grants only for clients with connection access", () => {
  const context = { document: { createElement: domNode } };
  runInNewContext(staticFile("control-owner.js"), context);
  const panel = Object.create(context.AgentOwnerPanel.OwnerPanel.prototype), calls = [];
  panel.busy = false;
  panel.status = { sessions: [{ id: "session-a", client_id: "agent-a", connected: true }] };
  panel.action = (path, body) => calls.push({ path, body });
  const client = { id: "agent-a", name: "Agent A", status: "pending", matching_code: "OLD-CODE" };
  const pending = descendants(panel.clientRow(client, {}));
  assert.deepEqual(pending.filter(node => node.tag === "button").map(node => node.textContent), ["Revoke access"]);
  assert.ok(pending.some(node => node.textContent === "Use a new invitation"));
  assert.equal(pending.some(node => node.tag === "code"), false);
  const approved = descendants(panel.clientRow({ ...client, status: "approved" }, {}));
  assert.ok(approved.some(node => node.textContent === "Access granted"));
  const grant = approved.find(node => node.textContent === "Grant control");
  assert.equal(grant.disabled, false);
  assert.deepEqual(calls, []);
  grant.events.click();
  assert.equal(calls[0].path, "grant");
  assert.equal(calls[0].body.session_id, "session-a");
  const current = descendants(panel.clientRow({ ...client, status: "approved" }, { operator: { id: "session-a" } }));
  assert.equal(current.find(node => node.textContent === "Grant control").disabled, true);
});

test("trusted Console management and matching display names do not grant control", () => {
  assert.equal(owner.stateView({ management_available: true }).manager, true);
  assert.equal(owner.stateView({ management_available: true }).mine, false);
  assert.equal(owner.stateView({ principal: { name: "Human" }, operator: { name: "Human" } }).mine, false);
  assert.equal(owner.stateView({ management_available: true, principal: { id: "human-1", kind: "human" }, operator: { id: "human-1", kind: "human" } }).mine, true);
  assert.equal(owner.stateView({ principal: { id: "human-1" }, operator: { id: "human-1" } }).mine, false);
  assert.equal(owner.stateView({ simulation: { status: "cancelled" } }).running, false);
});

test("trusted Console status polling does not require sign-in or a human heartbeat", async () => {
  const calls = [], panel = Object.create(owner.OwnerPanel.prototype);
  Object.assign(panel, {
    generation: 0, request: async path => { calls.push(path); return { management_available: true }; },
    changed() {}, render() {},
  });
  await panel.refresh();
  assert.deepEqual(calls, ["status"]);
  assert.equal(owner.stateView(panel.status).manager, true);
});

test("public human status cannot imply an agent connection or trigger agent heartbeats", async () => {
  const calls = [], status = {
    management_available: true, paired: true, connected: false, client_status: "approved",
    principal: { id: "human-1", kind: "human" }, operator: { id: "human-1", kind: "human" },
  };
  const client = new ControlClient({ request: async path => { calls.push(path); return status; } });
  await client.refresh();
  assert.deepEqual(calls, ["status"]);
  assert.equal(controlView(client.status).role, "Access granted · Disconnected");
  assert.equal(controlView(client.status).mine, false);
});

function clipboardDocument(result = true) {
  const calls = [];
  const input = {
    style: {}, setAttribute() {}, select() { calls.push("select"); },
    setSelectionRange() {}, remove() { calls.push("remove"); },
  };
  const document = {
    activeElement: { focus() { calls.push("restore focus"); } },
    createElement: () => input, body: { append() { calls.push("append"); } },
    execCommand(command) { calls.push(command); if (result instanceof Error) throw result; return result; },
  };
  return { document, input, calls };
}

test("short invitations use two readable groups", () => {
  assert.equal(owner.formatInvitation("ab12cd34"), "AB12-CD34");
  assert.equal(owner.formatInvitation(" AB12-CD34 "), "AB12-CD34");
  assert.equal(owner.formatInvitation(undefined), "");
});

test("HTTP invitation copying uses synchronous selection before yielding", async () => {
  const env = clipboardDocument();
  env.isSecureContext = false;
  env.navigator = { clipboard: { writeText() { assert.fail("HTTP must use the synchronous fallback"); } } };
  const copied = owner.copyInvitation("ab12cd34", env);
  assert.deepEqual(env.calls, ["append", "select", "copy", "remove", "restore focus"]);
  assert.equal(env.input.value, "AB12-CD34");
  assert.equal(await copied, true);
});

test("secure invitation copying reports success after the clipboard confirms it", async () => {
  let finish, text;
  const env = clipboardDocument();
  env.isSecureContext = true;
  env.navigator = { clipboard: { writeText(value) { text = value; return new Promise(resolve => { finish = resolve; }); } } };
  const result = owner.copyInvitation("AB12CD34", env);
  assert.equal(text, "AB12-CD34");
  assert.deepEqual(env.calls, []);
  finish();
  assert.equal(await result, true);
});

test("copy rejection and fallback failures do not report success", async () => {
  for (const failure of [false, new Error("Copy unavailable")]) {
    const env = clipboardDocument(failure);
    env.isSecureContext = true;
    env.navigator = { clipboard: { writeText: async () => { throw new Error("Permission denied"); } } };
    assert.equal(await owner.copyInvitation("AB12CD34", env), false);
    assert.deepEqual(env.calls, ["append", "select", "copy", "remove", "restore focus"]);
  }
  assert.equal(await owner.copyInvitation("", {}), false);
});

test("a rejected clipboard permission can use a successful selection fallback", async () => {
  const env = clipboardDocument();
  env.isSecureContext = true;
  env.navigator = { clipboard: { writeText: async () => { throw new Error("Permission denied"); } } };
  assert.equal(await owner.copyInvitation("AB12CD34", env), true);
});

test("status polling never connects or restores an operator grant", async () => {
  const calls = [];
  const client = new ControlClient({ request: async (path, body, source) => {
    calls.push({ path, body, source }); return { paired: true, connected: false, client_status: "approved" };
  } });
  await client.refresh();
  assert.deepEqual(calls.map(call => call.path), ["status"]);
  assert.equal(controlView(client.status).mine, false);
});

test("status failure clears the browser view without sending a heartbeat", async () => {
  const calls = [];
  const client = new ControlClient({ request: async path => {
    calls.push(path); throw new Error("Session expired");
  } });
  client.status = operator;
  await client.refresh();
  assert.deepEqual(calls, ["status"]);
  assert.equal(client.status, null);
  assert.equal(client.error, "Session expired");
});

test("closing the view stops its polling without disconnecting the PC agent", async () => {
  const calls = [];
  const client = new ControlClient({ request: async path => { calls.push(path); return operator; } });
  await client.refresh();
  client.stop();
  assert.deepEqual(calls, ["status"]);
  assert.equal(client.status.connected, true);
  assert.equal(client.timer, null);
});

test("an old source response cannot restore control after switching to simulation", async () => {
  let finish;
  const client = new ControlClient({ request: async (_path, _body, source) => {
    if (source === "live") return new Promise(resolve => { finish = resolve; });
    return { paired: false, connected: false };
  } });
  const read = client.refresh(); await tick();
  await client.setSource("mock"); finish(operator); await read;
  assert.equal(client.status, null);
  await client.refresh();
  assert.equal(client.source, "mock");
  assert.equal(client.status.paired, false);
});

test("switching source disconnects its old control session", async () => {
  const calls = [];
  const client = new ControlClient({ request: async (path, _body, source) => { calls.push([path, source]); return { connected: false }; } });
  client.status = operator;
  await client.setSource("mock");
  assert.deepEqual(calls, [["disconnect", "live"], ["status", "mock"]]);
  assert.equal(client.status.connected, false);
});

test("Stop test bypasses a pending simulated action and discards its late result", async () => {
  let finish;
  const calls = [];
  const stopped = { ...operator, simulation: { id: "test-1", status: "cancelled" } };
  const client = new ControlClient({ request: async (path, body) => {
    calls.push({ path, body });
    if (path === "simulate") return new Promise(resolve => { finish = resolve; });
    return stopped;
  } });
  client.status = operator;
  const start = client.action("simulate", { control_epoch: "grant-1" }); await tick();
  await client.action("simulate/stop");
  assert.equal(client.status.simulation.status, "cancelled");
  finish({ ...operator, simulation: { status: "running" } }); await start;
  assert.equal(client.status.simulation.status, "cancelled");
  assert.deepEqual(calls.map(call => call.path), ["simulate", "simulate/stop"]);
});

test("operator handover updates role and disables pending test steps", async () => {
  const client = new ControlClient({ request: async () => ({ ...operator, operator: { id: "owner" }, simulation: { status: "cancelled", completed_steps: 1 } }) });
  client.status = operator; await client.refresh();
  const view = controlView(client.status);
  assert.equal(view.mine, false); assert.equal(view.running, false); assert.equal(view.role, "Observer");
});

test("Console connections hide revoked clients without changing the saved status", () => {
  const context = { document: { createElement: domNode } };
  runInNewContext(staticFile("control-owner.js"), context);
  const panel = Object.create(context.AgentOwnerPanel.OwnerPanel.prototype);
  panel.container = domNode();
  panel.status = {
    management_available: true,
    clients: [
      { id: "agent-a", name: "Connected agent", status: "approved" },
      { id: "agent-b", name: "Revoked agent", status: "revoked" },
      { id: "agent-c", name: "Disconnected agent", status: "approved" },
    ],
    sessions: [{ id: "session-a", client_id: "agent-a", connected: true }],
  };
  panel.build();
  const names = descendants(panel.nodes.clients).filter(node => node.tag === "strong").map(node => node.textContent);
  assert.deepEqual(names, ["Connected agent", "Disconnected agent"]);
  assert.equal(panel.status.clients.length, 3);
  assert.equal(panel.status.clients[1].status, "revoked");
  panel.status.clients = [panel.status.clients[1]];
  panel.render();
  assert.equal(panel.nodes.clients.children.length, 1);
  assert.equal(panel.nodes.clients.children[0].textContent, "No agents registered.");
});

function ownerFixture({ external = false } = {}) {
  const context = { document: { createElement: domNode } };
  runInNewContext(staticFile("control-owner.js"), context);
  const panel = Object.create(context.AgentOwnerPanel.OwnerPanel.prototype), calls = [];
  panel.container = domNode(); panel.busy = false;
  if (external) { panel.testContainer = domNode(); panel.stopContainer = domNode(); panel.messageContainer = domNode(); }
  panel.status = {
    management_available: true, equipment_mode: "physical",
    clients: [{ id: "a", name: "Agent A", status: "approved", created_at: 1000, last_connected_at: null }],
    sessions: [{ id: "s", client_id: "a", connected: true }],
  };
  panel.action = (path, body) => calls.push({ path, body });
  panel.build(); return { panel, calls };
}

test("last connection uses persisted Unix seconds and never substitutes registration time", () => {
  for (const value of [null, undefined, "1795543200", NaN, Infinity, -1, 1e20]) {
    assert.deepEqual(owner.lastConnected(value), { text: "Not recorded", iso: null });
  }
  assert.equal(owner.lastConnected(0).iso, "1970-01-01T00:00:00.000Z");
  const value = Date.parse("2026-11-24T15:00:00Z") / 1000;
  assert.equal(owner.lastConnected(value).iso, "2026-11-24T15:00:00.000Z");
  const { panel } = ownerFixture(), row = panel.nodes.clients.children[0];
  assert.equal(row.clientNodes.lastConnected.textContent, "Not recorded");
  panel.status.clients[0].last_connected_at = value;
  panel.render();
  assert.equal(row.clientNodes.lastConnected.title, "2026-11-24T15:00:00.000Z");
  assert.notEqual(row.clientNodes.lastConnected.textContent, "Not recorded");
});

test("polling preserves connection rows, action buttons and disclosure state", () => {
  const { panel, calls } = ownerFixture();
  const row = panel.nodes.clients.children[0], grant = row.clientNodes.grants.get("s"), revoke = row.clientNodes.revoke;
  panel.nodes.testing.open = true;
  panel.status.sessions[0].last_seen_at = 1234;
  panel.status.clients[0].name = "Agent A updated";
  panel.render();
  assert.equal(panel.nodes.clients.children[0], row);
  assert.equal(row.clientNodes.grants.get("s"), grant);
  assert.equal(row.clientNodes.revoke, revoke);
  assert.equal(row.clientNodes.name.textContent, "Agent A updated");
  assert.equal(row.clientNodes.role.textContent, "Observer");
  assert.equal(panel.nodes.testing.open, true);
  panel.status.operator = { id: "s", name: "Agent A updated", kind: "agent" }; panel.render();
  assert.equal(row.clientNodes.role.textContent, "Operator");
  assert.equal(grant.disabled, true);
  panel.status.sessions[0].connected = false; panel.render();
  assert.equal(row.clientNodes.connection.textContent, "Not connected");
  assert.equal(row.clientNodes.role.hidden, true);
  assert.equal(row.clientNodes.grants.size, 0);
  assert.equal(row.clientNodes.revoke, revoke);
  assert.deepEqual(calls, []);
});

test("separate Testing and Stop containers keep Stop outside both connection and test content", () => {
  const { panel, calls } = ownerFixture({ external: true });
  assert.equal(panel.nodes.stopPanel.parent, panel.stopContainer);
  assert.equal(descendants(panel.container).includes(panel.nodes.start), false);
  assert.equal(descendants(panel.testContainer).includes(panel.nodes.start), true);
  assert.equal(descendants(panel.testContainer).includes(panel.nodes.stopPanel), false);
  assert.equal(panel.nodes.testing, undefined);
  panel.error = "Test request failed";
  panel.status.watchdog = { active: { sender: { name: "Agent A" } } };
  panel.container.hidden = true;
  panel.render();
  assert.equal(panel.nodes.error.parent, panel.messageContainer);
  assert.equal(panel.nodes.error.hidden, false);
  assert.equal(panel.nodes.warningPanel.parent, panel.messageContainer);
  assert.equal(panel.nodes.warningPanel.hidden, false);
  assert.equal(panel.messageContainer.hidden, false);
  assert.equal(panel.container.hidden, true);
  assert.equal(panel.stopContainer.hidden, false);
  assert.equal(panel.testContainer.hidden, false);
  assert.deepEqual(calls, []);
});

test("standalone management keeps testing collapsed and Device Stop available", () => {
  const { panel } = ownerFixture();
  assert.equal(panel.nodes.testing.tag, "details");
  assert.notEqual(panel.nodes.testing.open, true);
  assert.equal(panel.nodes.stopPanel.parent, panel.container);
  assert.equal(descendants(panel.nodes.testing).includes(panel.nodes.stopPanel), false);
});


