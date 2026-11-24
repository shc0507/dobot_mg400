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
    constructor(container, { request, changed = () => {} }) {
      this.container = container; this.request = request; this.changed = changed;
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
      n.stopPanel = element("section", { class: "aoc-card aoc-connections agent-stop-panel", "aria-label": "Device Stop and history" });
      n.test = element("div", { class: "aoc-test-progress", role: "status" });
      n.start = this.button("Start test", "simulate", () => ({ control_epoch: this.status?.control_epoch }));
      n.step = this.button("Run next step", "simulate/step", () => ({ operation_id: this.status?.simulation?.id || this.status?.simulation?.operation_id, control_epoch: this.status?.control_epoch }));
      n.stop = this.button("Stop test", "simulate/stop"); n.stop.className = "aoc-stop";
      const operator = element("section", { class: "aoc-card" }, element("h2", { text: "Operator control" }),
        element("div", { class: "aoc-operator" }, element("span", { class: "aoc-help", text: "Current operator" }), n.operator, n.version),
        element("div", { class: "aoc-actions" }, n.take, n.release, n.invite), n.invitation);
      const simulation = element("section", { class: "aoc-card" }, element("span", { class: "aoc-badge", text: "SIMULATED" }), element("h2", { text: "Test a handover" }),
        element("p", { class: "aoc-help", text: "Start a test, then take control from the other window. Pending steps must stop. No equipment moves." }),
        n.test, element("div", { class: "aoc-actions" }, n.start, n.step, n.stop));
      this.container.classList.add("agent-owner-control");
      this.container.replaceChildren(n.boundary,
        n.error, n.warningPanel, element("div", { class: "aoc-grid" }, operator, simulation), n.stopPanel, element("section", { class: "aoc-card aoc-connections" }, element("h2", { text: "Agent connections" }), n.clients));
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
      n.clients.replaceChildren(...(clients.length ? clients.map(client => this.clientRow(client, state)) : [element("p", { class: "aoc-help", text: state.manager ? "No agents registered." : "Waiting for connection controls." })]));
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
      const id = client.id || client.client_id;
      const sessions = (this.status?.sessions || []).filter(session => session.client_id === id && session.connected !== false);
      const pending = client.status === "pending", approved = client.status === "approved";
      return element("article", { class: "aoc-client" }, element("div", { class: "aoc-client-info" },
        element("strong", { text: client.name || id }), element("span", { class: "aoc-help", text: pending ? "Use a new invitation" : approved ? "Access granted" : "Access revoked" }),
        approved ? element("span", { class: "aoc-help", text: sessions.length ? "Connected" : "Not connected" }) : null),
      element("div", { class: "aoc-actions" },
        ...(approved ? sessions : []).map(session => this.rowButton("Grant control", "grant", { session_id: session.id || session.session_id }, (state.operator?.id || state.operator?.session_id) === (session.id || session.session_id))),
        client.status !== "revoked" ? this.rowButton("Revoke access", "revoke", { client_id: id }) : null));
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
  const exports = { stateView, formatInvitation, copyInvitation, OwnerPanel, mount: (container, options) => new OwnerPanel(container, options) };
  if (typeof module !== "undefined") module.exports = exports;
  root.AgentOwnerPanel = exports;
})(typeof window === "undefined" ? globalThis : window);
