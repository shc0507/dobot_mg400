(function (root) {
  "use strict";
  function controlView(status) {
    const principal = status?.principal || status?.session;
    const operator = status?.operator;
    const id = principal?.id || principal?.session_id;
    const operatorId = operator?.id || operator?.session_id;
    const connected = status?.connected === true;
    const continuity = status?.local_agent?.continuity;
    const paired = status?.paired === true || continuity?.paired === true;
    const revoked = status?.client_status === "revoked";
    const pending = status?.client_status === "pending";
    const approved = !revoked && !pending && (status?.client_status === "approved" || status?.approved === true || connected);
    const mine = Boolean(approved && connected && id && operatorId && id === operatorId);
    const simulation = status?.simulation || null;
    const running = ["running", "pending", "waiting"].includes(simulation?.status);
    const restoring = paired && ["pending", "connecting", "retrying"].includes(continuity?.state);
    const role = restoring ? "Restoring observer connection" : paired && continuity?.state === "offline" ? "Saved pairing · Offline" : revoked ? "Access revoked" : !paired ? "Use an invitation" : !approved ? "Use a new invitation" : !connected ? "Access granted · Disconnected" : mine ? status?.equipment_mode === "physical" ? "Operator · Physical commands" : "Operator · Simulated actions" : "Observer";
    return { connected, paired, approved, mine, simulation, running, revoked, role };
  }
  function processStatus(status, now = Date.now() / 1000) {
    const process = status?.local_agent, read = process?.read_collection;
    let text = `The PC process sends heartbeats every ${process?.heartbeat_interval_s || 2} seconds. This page is a view.`;
    if (read?.error) text += ` ${read.error}`;
    else if (read?.running) text += typeof read.received_at === "number"
      ? ` Background raw data is unverified. Last received ${Math.max(0, Math.floor(now - read.received_at))} seconds ago.`
      : " Waiting for background state.";
    return text;
  }
  class ControlClient {
    constructor({ request, changed = () => {} }) {
      this.request = request; this.changed = changed; this.source = "live";
      this.status = null; this.error = ""; this.busy = false; this.reading = false; this.generation = 0;
    }
    async refresh() {
      if (this.reading || this.busy) return;
      this.reading = true; const generation = this.generation, source = this.source;
      try {
        const status = await this.request("status", undefined, source);
        if (generation === this.generation) { this.status = status; this.error = ""; }
      } catch (error) {
        if (generation === this.generation) { this.status = error.localAgent ? { local_agent: error.localAgent } : null; this.error = error.message; }
      } finally { this.reading = false; if (generation === this.generation) this.changed(); }
    }
    async action(path, body = {}) {
      const stopping = path === "simulate/stop";
      if (this.busy && !stopping) return;
      if (!stopping) this.busy = true;
      const generation = ++this.generation, source = this.source;
      this.error = ""; this.changed();
      try {
        const status = await this.request(path, body, source);
        if (generation === this.generation) this.status = status;
      } catch (error) { if (generation === this.generation) this.error = error.message; }
      finally { if (!stopping) this.busy = false; if (generation === this.generation) this.changed(); }
    }
    async setSource(source) {
      if (!["live", "mock"].includes(source)) throw new Error("Choose Olares or the local simulation.");
      if (this.source === source) return;
      const previous = this.source, connected = this.status?.connected;
      this.generation++; this.source = source; this.status = null; this.error = ""; this.changed();
      if (connected) {
        try { await this.request("disconnect", {}, previous); }
        catch { this.error = "The previous connection did not confirm disconnection. Its control will expire without heartbeats."; }
      }
      await this.refresh();
    }
    start() { if (!this.timer) { this.refresh(); this.timer = setInterval(() => this.refresh(), 2000); } }
    stop() { this.generation++; clearInterval(this.timer); this.timer = null; }
  }
  const exports = { controlView, processStatus, ControlClient };
  if (typeof module !== "undefined") module.exports = exports;
  root.AgentControlPortal = exports;
  if (typeof document === "undefined") return;
  const $ = id => document.getElementById(id);
  if (!$("agent-approval")) return;
  async function request(path, data, source) {
    const headers = { Accept: "application/json", "X-MG400-Client": "agent-portal-control" };
    const options = { credentials: "same-origin", redirect: "error", cache: "no-store", headers };
    let url = `/api/agent-control/${path}`;
    if (data === undefined) url += `?source=${encodeURIComponent(source)}`;
    else { options.method = "POST"; headers["Content-Type"] = "application/json"; options.body = JSON.stringify({ ...data, source }); }
    const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 8000);
    options.signal = controller.signal;
    try {
      const response = await fetch(url, options);
      const body = await response.json();
      if (!response.ok) { const error = new Error(body.error || "The Console refused the request."); error.status = response.status; error.localAgent = body.local_agent; throw error; }
      return body;
    } finally { clearTimeout(timeout); }
  }
  function render() {
    const state = controlView(client.status), status = client.status;
    $("agent-control-target").textContent = client.source === "live" ? "Olares test Console" : "Local simulated Console";
    $("agent-owner-link").href = client.source === "live" ? "http://100.64.0.1:8082/lab/#agent-connect" : "http://127.0.0.1:8790/lab/";
    $("agent-control-role").textContent = state.role;
    $("agent-control-error").hidden = !client.error; $("agent-control-error").textContent = client.error;
    const warning = status?.watchdog?.active;
    $("agent-control-watchdog").hidden = !warning;
    $("agent-control-watchdog").textContent = warning ? `${warning.sender?.name ? warning.sender.name + ": " : ""}Agent connection lost. Motion control is released. Furnace follows its existing run/recovery behavior.` : "";
    $("agent-pair-form").hidden = state.approved || Boolean(status?.local_agent?.continuity?.paired);
    $("agent-pair").disabled = client.busy;
    $("agent-control-name").textContent = status?.name || status?.local_agent?.continuity?.name || (state.connected && status?.principal?.kind === "agent" ? status.principal.name : null) || (state.paired ? "Agent" : "No agent registered");
    $("agent-control-session").textContent = state.connected ? processStatus(status) : status?.local_agent?.continuity?.message || status?.local_agent?.error || (!state.approved && state.paired ? "Use a new invitation from the Console." : state.approved ? "Connect, then grant control from the Console." : "The data stream works without an operator grant.");
    $("agent-control-connect").disabled = !state.approved || state.connected || client.busy;
    $("agent-control-disconnect").disabled = !(state.connected || status?.local_agent?.continuity?.connect_intent) || client.busy;
    $("agent-control-release").disabled = !state.mine || client.busy;
    $("agent-control-start").disabled = !state.mine || state.running || client.busy;
    $("agent-control-step").disabled = !state.mine || !state.running || client.busy;
    $("agent-control-stop").disabled = false;
    const simulation = state.simulation;
    $("agent-control-test").textContent = simulation ? `${state.running ? "Waiting for next step" : simulation.status === "completed" ? "Test completed" : "Test cancelled"} · ${simulation.completed_steps ?? simulation.step ?? 0} / ${simulation.total_steps ?? 3} steps${simulation.reason ? ". " + simulation.reason : ""}` : "No test running.";
    commands?.setContext(client.source, status);
    stops?.setContext(client.source, status);
  }
  let commands = null, stops = null;
  const client = new ControlClient({ request, changed: render });
  commands = root.AgentCommandPortal?.mount({ request });
  stops = root.AgentStopPanel?.mount($("agent-stop-panel"), { request });
  $("agent-pair-form").onsubmit = event => {
    event.preventDefault();
    const invitation = $("agent-pair-invitation").value; $("agent-pair-invitation").value = "";
    client.action("pair", { invitation, name: $("agent-pair-name").value });
  };
  $("agent-control-connect").onclick = () => client.action("connect");
  $("agent-control-disconnect").onclick = () => client.action("disconnect");
  $("agent-control-release").onclick = () => client.action("release", { control_epoch: client.status?.control_epoch });
  $("agent-control-start").onclick = () => client.action("simulate", { control_epoch: client.status?.control_epoch });
  $("agent-control-step").onclick = () => client.action("simulate/step", { operation_id: client.status?.simulation?.id || client.status?.simulation?.operation_id, control_epoch: client.status?.control_epoch });
  $("agent-control-stop").onclick = () => client.action("simulate/stop");
  addEventListener("portal-test-mode", event => { $("agent-pair-invitation").value = ""; client.setSource(event.detail ? "mock" : "live"); });
  addEventListener("pagehide", () => { client.stop(); stops?.stop(); });
  addEventListener("pageshow", () => { client.start(); stops?.start(); });
  render(); client.start();
})(typeof window === "undefined" ? globalThis : window);
