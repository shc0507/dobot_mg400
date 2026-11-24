(function (root) {
  "use strict";
  const assetBase = root.document?.currentScript?.src?.replace(/[^/]*$/, "");
  let stopModule;
  function loadStopPanel() {
    if (root.AgentStopPanel) return Promise.resolve(root.AgentStopPanel);
    if (!stopModule) stopModule = new Promise((resolve, reject) => {
      const script = document.createElement("script"); script.src = new URL("stop-panel.js", assetBase).href;
      script.onload = () => resolve(root.AgentStopPanel); script.onerror = () => reject(new Error("Stop history could not load.")); document.head.append(script);
    });
    return stopModule;
  }
  function stateView(status) {
    const principal = status?.principal || status?.session || null;
    const operator = status?.operator || null;
    const principalId = principal?.id || principal?.session_id;
    const operatorId = operator?.id || operator?.session_id;
    const manager = status?.management_available === true;
    const mine = Boolean(manager && principalId && operatorId && principalId === operatorId);
    const simulation = status?.simulation || null;
    const running = ["running", "pending", "waiting"].includes(simulation?.status);
    return { manager, principal, operator, mine, simulation, running };
  }
  function formatInvitation(value) {
    const text = String(value || "").trim(), code = text.replace(/[-\s]/g, "").toUpperCase();
    return /^[A-Z0-9]{8}$/.test(code) ? `${code.slice(0, 4)}-${code.slice(4)}` : text;
  }
  function lastConnected(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return { text: "Not recorded", iso: null };
    const date = new Date(value * 1000);
    if (Number.isNaN(date.valueOf())) return { text: "Not recorded", iso: null };
    return { text: date.toLocaleString([], { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }), iso: date.toISOString() };
  }
  function legacyCopy(text, doc) {
    let input;
    const previous = doc?.activeElement;
    try {
      input = doc.createElement("textarea"); input.value = text;
      input.setAttribute("readonly", "");
      Object.assign(input.style, { position: "fixed", left: "-9999px", top: "0" });
      doc.body.append(input); input.select(); input.setSelectionRange(0, text.length);
      return doc.execCommand("copy") === true;
    } catch { return false; }
    finally { input?.remove(); previous?.focus?.({ preventScroll: true }); }
  }
  function copyInvitation(value, env = root) {
    const text = formatInvitation(value);
    if (!text) return Promise.resolve(false);
    if (env.isSecureContext && env.navigator?.clipboard?.writeText) {
      try { return Promise.resolve(env.navigator.clipboard.writeText(text)).then(() => true, () => legacyCopy(text, env.document)); }
      catch { return Promise.resolve(legacyCopy(text, env.document)); }
    }
    // Keep the HTTP fallback in the click event, before any asynchronous work.
    return Promise.resolve(legacyCopy(text, env.document));
  }
  function element(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value == null || value === false) continue;
      if (key === "text") node.textContent = String(value);
      else if (key === "class") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else if (key === "hidden" || key === "disabled") node[key] = true;
      else node.setAttribute(key, String(value));
    }
    for (const child of children.flat()) if (child != null && child !== false) node.append(child);
    return node;
  }
  class OwnerPanel {
    constructor(container, { request, changed = () => {}, testContainer = null, stopContainer = null, messageContainer = null }) {
      this.container = container; this.request = request; this.changed = changed;
      this.testContainer = testContainer; this.stopContainer = stopContainer;
      this.messageContainer = messageContainer;
      this.status = null; this.invitation = null; this.error = ""; this.busy = false; this.reading = false; this.generation = 0;
      this.copying = false; this.copyMessage = "";
      this.stopPanel = null; this.stopSource = "live";
      this.build(); this.start();
      loadStopPanel().then(module => {
        this.stopPanel = module.mount(this.nodes.stopPanel, { request: this.request });
        this.stopPanel.setContext(this.stopSource, this.status);
        if (!this.timer) this.stopPanel.stop();
      }).catch(error => { this.nodes.stopPanel.textContent = error.message; });
    }
    button(text, action, body = () => ({})) {
      return element("button", { type: "button", text, onclick: () => this.action(action, body()) });
    }
    build() {
      const n = this.nodes = {};
      n.error = element("p", { class: "aoc-error", role: "alert", hidden: true });
      n.warning = element("p", { class: "aoc-error", role: "alert", hidden: true });
      n.dismissWarning = this.button("Dismiss warning", "acknowledge-warning");
      n.warningPanel = element("div", { hidden: true }, n.warning, n.dismissWarning);
      n.boundary = element("p", { class: "aoc-boundary", text: "Checking the equipment connection." });
      n.operator = element("strong", { class: "aoc-operator-name", text: "Checking operator…" });
      n.version = element("span", { class: "aoc-help" });
      n.take = this.button("Take control", "take-human");
      n.release = this.button("Release control", "release", () => ({ control_epoch: this.status?.control_epoch }));
      n.invite = this.button("Create invitation", "invitations");
      n.invitationCode = element("code");
      n.copy = element("button", { type: "button", text: "Copy", onclick: () => this.copy() });
      n.copyStatus = element("span", { class: "aoc-help aoc-copy-status", role: "status" });
      n.invitation = element("div", { class: "aoc-invitation", hidden: true }, element("strong", { text: "Single-use invitation" }),
        element("div", { class: "aoc-invitation-row" }, n.invitationCode, n.copy), n.copyStatus,
        element("p", { class: "aoc-help", text: "Use this invitation in the PC portal to grant connection access. Grant operator control separately." }));
      n.clients = element("div", { class: "aoc-clients" });
      n.clientCount = element("span", { class: "aoc-help aoc-client-count" });
      n.stopPanel = element("section", { class: "aoc-card aoc-connections agent-stop-panel", "aria-label": "Device Stop and history" });
      n.test = element("div", { class: "aoc-test-progress", role: "status" });
      n.start = this.button("Start test", "simulate", () => ({ control_epoch: this.status?.control_epoch }));
      n.step = this.button("Run next step", "simulate/step", () => ({ operation_id: this.status?.simulation?.id || this.status?.simulation?.operation_id, control_epoch: this.status?.control_epoch }));
      n.stop = this.button("Stop test", "simulate/stop"); n.stop.className = "aoc-stop";
      const operator = element("section", { class: "aoc-card aoc-control-card" }, element("h2", { text: "Operator control" }),
        element("div", { class: "aoc-operator" }, element("span", { class: "aoc-help", text: "Current operator" }), n.operator),
        element("div", { class: "aoc-actions" }, n.take, n.release));
      const details = element("details", { class: "aoc-control-details" }, element("summary", { text: "Connection details" }), n.version);
      operator.append(details);
      const connections = element("section", { class: "aoc-card aoc-connection-card" },
        element("div", { class: "aoc-card-heading" }, element("div", {}, element("h2", { text: "Agent connections" }), n.clientCount), n.invite),
        n.invitation, n.clients);
      const simulation = element("section", { class: "aoc-card" }, element("span", { class: "aoc-badge", text: "SIMULATED" }), element("h2", { text: "Test a handover" }),
        element("p", { class: "aoc-help", text: "Start a test, then take control from the other window. Pending steps must stop. No equipment moves." }),
        n.test, element("div", { class: "aoc-actions" }, n.start, n.step, n.stop));
      this.container.classList.add("agent-owner-control");
      n.connections = element("div", { class: "aoc-grid" }, operator, connections);
      this.container.replaceChildren(n.boundary, n.connections);
      if (this.messageContainer) {
        this.messageContainer.classList.add("agent-owner-control");
        this.messageContainer.replaceChildren(n.error, n.warningPanel);
      } else this.container.insertBefore(element("div", {}, n.error, n.warningPanel), n.connections);
      if (this.stopContainer) { this.stopContainer.classList.add("agent-owner-control"); this.stopContainer.replaceChildren(n.stopPanel); }
      else this.container.append(n.stopPanel);
      if (this.testContainer) { this.testContainer.classList.add("agent-owner-control"); this.testContainer.replaceChildren(simulation); }
      else {
        n.testing = element("details", { class: "aoc-testing" }, element("summary", { text: "Testing" }), simulation);
        this.container.append(n.testing);
      }
      this.render();
    }
    render() {
      const n = this.nodes, state = stateView(this.status);
      n.error.hidden = !this.error; n.error.textContent = this.error;
      n.boundary.textContent = this.status?.equipment_mode === "physical" ? "Physical equipment · An operator grant permits explicit agent commands." : this.status?.equipment_mode === "simulated" ? "Simulated equipment · Commands use the local simulation." : "Physical commands are unavailable for the current Console contract.";
      const warning = this.status?.watchdog?.active;
      n.warningPanel.hidden = !warning;
      n.warning.hidden = !warning;
      n.dismissWarning.disabled = !state.manager || this.busy;
      n.warning.textContent = warning ? `${warning.sender?.name ? warning.sender.name + ": " : ""}Agent connection lost. Motion control is released. Furnace follows its existing run/recovery behavior.` : "";
      n.operator.textContent = state.operator ? (state.operator.kind === "human" ? "Human operator" : state.operator.name || state.operator.client_id || "Agent") : "No operator";
      n.version.textContent = this.status ? `Control version: ${this.status.control_epoch ?? "Unknown"}` : "Waiting for connection controls.";
      n.take.disabled = !state.manager || state.mine || this.busy;
      n.release.disabled = !state.mine || this.busy; n.invite.disabled = !state.manager || this.busy;
      if (!state.manager) this.invitation = null;
      n.invitation.hidden = !this.invitation;
      n.invitationCode.textContent = formatInvitation(this.invitation?.invitation);
      n.copy.disabled = this.copying || !this.invitation;
      n.copyStatus.textContent = this.copyMessage; n.copyStatus.hidden = !this.copyMessage;
      const clients = state.manager ? (this.status?.clients || []).filter(client => client.status !== "revoked") : [];
      n.clientCount.textContent = `${clients.length} ${clients.length === 1 ? "agent" : "agents"}`;
      this.renderClients(clients, state);
      const simulation = state.simulation;
      const title = !simulation ? "No test running" : state.running ? "Waiting for the next step" : simulation.status === "completed" ? "Test completed" : "Test cancelled";
      n.test.replaceChildren(...[element("strong", { text: title }), simulation ? element("span", { class: "aoc-help", text: `Steps completed: ${simulation.completed_steps ?? simulation.step ?? 0} / ${simulation.total_steps ?? 3}` }) : null,
        simulation?.reason ? element("p", { class: "aoc-help", text: simulation.reason }) : null].filter(Boolean));
      n.start.disabled = !state.mine || state.running || this.busy;
      n.step.disabled = !state.mine || !state.running || this.busy;
      n.stop.disabled = false;
      if (this.status) this.stopSource = this.status.equipment_mode === "simulated" && this.status.physical_commands_enabled === false ? "mock" : "live";
      this.stopPanel?.setContext(this.stopSource, this.status);
    }
    clientRow(client, state) {
      const id = client.id || client.client_id, nodes = {};
      nodes.name = element("strong"); nodes.access = element("span", { class: "aoc-help" });
      nodes.connection = element("span", { class: "aoc-badge" }); nodes.role = element("span", { class: "aoc-badge" });
      nodes.lastConnected = element("span");
      nodes.actions = element("div", { class: "aoc-actions aoc-client-actions" }); nodes.grants = new Map();
      nodes.revoke = this.rowButton("Revoke access", "revoke", { client_id: id });
      nodes.revoke.className = "aoc-revoke"; nodes.actions.append(nodes.revoke);
      const row = element("article", { class: "aoc-client" }, element("div", { class: "aoc-client-info" },
        element("div", { class: "aoc-client-heading" }, nodes.name, nodes.connection, nodes.role), nodes.access,
        element("p", { class: "aoc-last-connected aoc-help" }, element("span", { text: "Last connected: " }), nodes.lastConnected)), nodes.actions);
      row.clientNodes = nodes; this.updateClientRow(row, client, state); return row;
    }
    updateClientRow(row, client, state) {
      const n = row.clientNodes, id = client.id || client.client_id, approved = client.status === "approved";
      const sessions = approved ? (this.status?.sessions || []).filter(session => session.client_id === id && session.connected !== false) : [];
      const operatorId = state.operator?.id || state.operator?.session_id;
      n.name.textContent = client.name || id;
      n.access.textContent = client.status === "pending" ? "Use a new invitation" : approved ? "Access granted" : "Access revoked";
      n.connection.textContent = sessions.length ? "Connected" : "Not connected";
      n.connection.className = "aoc-badge" + (sessions.length ? " aoc-connected" : ""); n.connection.hidden = !approved;
      const controls = sessions.some(session => (session.id || session.session_id) === operatorId);
      n.role.textContent = controls ? "Operator" : "Observer"; n.role.hidden = !sessions.length;
      n.role.className = "aoc-badge" + (controls ? " aoc-current-operator" : "");
      const connected = lastConnected(client.last_connected_at);
      n.lastConnected.textContent = connected.text; n.lastConnected.setAttribute("title", connected.iso || "");
      const ids = new Set();
      for (const session of sessions) {
        const sessionId = session.id || session.session_id; ids.add(sessionId);
        let button = n.grants.get(sessionId);
        if (!button) { button = this.rowButton("Grant control", "grant", { session_id: sessionId }); n.grants.set(sessionId, button); n.actions.insertBefore(button, n.revoke); }
        button.disabled = this.busy || operatorId === sessionId;
        button.setAttribute("aria-label", `Grant control to ${client.name || id}${sessions.length > 1 ? ` (${sessionId})` : ""}`);
      }
      for (const [sessionId, button] of n.grants) if (!ids.has(sessionId)) { button.remove(); n.grants.delete(sessionId); }
      n.revoke.disabled = this.busy; n.revoke.hidden = client.status === "revoked";
    }
    renderClients(clients, state) {
      if (!this.clientRows) this.clientRows = new Map();
      const ids = new Set(clients.map(client => client.id || client.client_id));
      for (const [id, row] of this.clientRows) if (!ids.has(id)) { row.remove(); this.clientRows.delete(id); }
      if (!clients.length) {
        if (!this.emptyClients) this.emptyClients = element("p", { class: "aoc-help aoc-empty" });
        this.emptyClients.textContent = state.manager ? "No agents registered." : "Waiting for connection controls.";
        if (this.nodes.clients.children[0] !== this.emptyClients) this.nodes.clients.replaceChildren(this.emptyClients);
        return;
      }
      this.emptyClients?.remove();
      for (const [index, client] of clients.entries()) {
        const id = client.id || client.client_id;
        let row = this.clientRows.get(id);
        if (!row) { row = this.clientRow(client, state); this.clientRows.set(id, row); }
        else this.updateClientRow(row, client, state);
        if (this.nodes.clients.children[index] !== row) this.nodes.clients.insertBefore(row, this.nodes.clients.children[index] || null);
      }
    }
    rowButton(text, action, body, disabled = false) {
      const button = this.button(text, action, () => body); button.disabled = disabled || this.busy; return button;
    }
    async copy() {
      if (!this.invitation || this.copying) return;
      const invitation = this.invitation;
      const result = copyInvitation(invitation.invitation);
      this.copying = true; this.copyMessage = ""; this.render();
      const copied = await result;
      this.copying = false;
      if (this.invitation === invitation) this.copyMessage = copied ? "Copied." : "Copy failed. Select the code and copy it manually.";
      this.render();
    }
    async action(path, body = {}) {
      const stopping = path === "simulate/stop";
      if (this.busy && !stopping) return;
      if (!stopping) this.busy = true;
      const generation = ++this.generation;
      this.error = ""; this.render();
      try {
        const result = await this.request(path, body);
        if (generation !== this.generation) return;
        if (path === "invitations") { this.invitation = result; this.copyMessage = ""; }
        const status = await this.request("status");
        if (generation === this.generation) { this.status = status; this.changed(status); }
      } catch (error) { if (generation === this.generation) this.error = error.message; }
      finally { if (!stopping) this.busy = false; if (generation === this.generation) this.render(); }
    }
    async refresh() {
      if (this.reading || this.busy) return;
      this.reading = true; const generation = this.generation;
      try {
        const status = await this.request("status");
        if (generation !== this.generation) return;
        this.status = status; this.changed(status); this.error = "";
      } catch (error) { if (generation === this.generation) { this.status = null; this.error = error.message; } }
      finally { this.reading = false; if (generation === this.generation) this.render(); }
    }
    start() { if (!this.timer) { this.refresh(); this.timer = setInterval(() => this.refresh(), 2000); this.stopPanel?.start(); } }
    stop() { this.generation++; clearInterval(this.timer); this.timer = null; this.stopPanel?.stop(); }
  }
  const exports = { stateView, formatInvitation, lastConnected, copyInvitation, OwnerPanel, mount: (container, options) => new OwnerPanel(container, options) };
  if (typeof module !== "undefined") module.exports = exports;
  root.AgentOwnerPanel = exports;
})(typeof window === "undefined" ? globalThis : window);
