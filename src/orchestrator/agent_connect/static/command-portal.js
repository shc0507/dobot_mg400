(function (root) {
  "use strict";
  const parameters = typeof module !== "undefined" ? require("./command-form.js") : root.AgentCommandForm;
  const visuals = typeof module !== "undefined" ? require("./portal-visuals.js") : root.AgentPortalVisuals;
  const activeStatuses = new Set(["accepted", "running", "cancelling", "unknown"]);
  const identity = value => value?.id || value?.session_id;
  function commandView(source, status) {
    const mine = Boolean(status?.connected && status?.principal?.kind === "agent" && identity(status.principal) && identity(status.principal) === identity(status.operator));
    const current = [8, 9].includes(status?.phase);
    const simulated = source === "mock" && ([5, 6].includes(status?.phase) && status?.furnace_control_enabled === false || current) && status?.commands_enabled === true && status?.physical_commands_enabled === false && status?.equipment_mode === "simulated";
    const physical = source === "live" && current && status?.commands_enabled === true && status?.physical_commands_enabled === true && status?.equipment_mode === "physical";
    return { mine, enabled: mine && (simulated || physical), label: source === "mock" ? "Simulated commands" : physical ? "Physical commands" : "Physical commands unavailable" };
  }
  function defaultsFor(action) {
    if (action?.defaults) return action.defaults;
    return Object.fromEntries(Object.entries(action?.params?.properties || {}).filter(([, field]) => field.default !== undefined).map(([key, field]) => [key, field.default]));
  }
  function commandError(record) {
    if (!["rejected", "failed"].includes(record?.status)) return "";
    const detail = typeof record.error === "string" ? record.error : record.error?.message || record.error?.error;
    return typeof detail === "string" && detail ? detail : `The command ${record.status === "rejected" ? "was rejected" : "failed"}. Expand its record for details.`;
  }
  function requestPreview(client, params, preconditions) {
    return { request_id: "<generated on Send>", boot_id: client.status?.boot_id ?? null, contract_id: client.catalog?.contract_id ?? null, control_epoch: client.status?.control_epoch ?? null, action: client.selected || null, params, preconditions };
  }
  class CommandClient {
    constructor({ request, changed = () => {}, createId = () => root.crypto.randomUUID() }) {
      Object.assign(this, { request, changed, createId, source: "live", status: null, catalog: null, choices: null, choicesAt: null, items: [], error: "", selected: "", generation: 0, busy: false, refreshing: false });
      this.pending = new Map();
      this.detailStatus = new Map(); this.detailLoading = new Set();
    }
    setContext(source, status) {
      const switched = source !== this.source;
      const sessionChanged = identity(this.status?.principal) !== identity(status?.principal);
      const disconnected = this.status?.connected === true && status?.connected !== true;
      const connected = status?.connected === true && (!this.status?.connected || identity(status.principal) !== identity(this.status.principal));
      if (sessionChanged || disconnected) { this.generation++; this.choices = null; this.choicesAt = null; this.refreshing = false; this.busy = false; }
      if (switched) {
        this.generation++; this.source = source; this.bootId = null; this.catalog = null; this.choices = null; this.choicesAt = null; this.items = []; this.selected = ""; this.error = ""; this.busy = false; this.refreshing = false;
        this.detailStatus.clear(); this.detailLoading.clear();
      }
      if (this.bootId && status?.boot_id && this.bootId !== status.boot_id) { this.generation++; this.catalog = null; this.choices = null; this.choicesAt = null; this.refreshing = false; this.busy = false; }
      if (status?.connected !== true) { this.choices = null; this.choicesAt = null; }
      if (status?.boot_id) this.bootId = status.boot_id;
      this.status = status;
      this.changed();
      if (switched || connected || (!this.catalog && status)) this.refresh();
    }
    select(id) { this.selected = id; this.error = ""; this.changed(); }
    get action() { return this.catalog?.actions?.find(action => action.id === this.selected); }
    get view() { return commandView(this.source, this.status); }
    get active() {
      const session = identity(this.status?.principal);
      return this.items.find(item => activeStatuses.has(item.status) && item.session_id === session);
    }
    get unresolved() { return [...this.pending.values()].some(item => item.source === this.source); }
    get currentContract() {
      return this.status?.phase < 8 || Boolean(this.status?.boot_id && typeof this.status?.control_epoch === "string" && this.status.control_epoch.length > 0 && this.status?.command_contract_id && this.catalog?.contract_id === this.status.command_contract_id);
    }
    get stageConstraints() { return this.status?.connected === true ? this.choices?.constraints?.stage || null : null; }
    stageView(params = {}) { return parameters.stagePositionView(this.stageConstraints, params.motor, params.position, this.choicesAt == null ? Infinity : Math.max(0, Date.now() - this.choicesAt) / 1000); }
    get canSend() { return this.view.enabled && this.currentContract && this.action?.available === true && (this.selected !== "stage.position" || this.stageView().fresh) && (this.action?.device !== "furnace" || this.status?.furnace_control_enabled === true) && !this.busy && !this.active && !this.unresolved; }
    save(item) {
      if (!item?.request_id) return;
      const previous = this.items.find(previous => previous.request_id === item.request_id);
      if (previous && !activeStatuses.has(previous.status) && activeStatuses.has(item.status)) return;
      item = { ...previous, ...item };
      this.items = [item, ...this.items.filter(previous => previous.request_id !== item.request_id)].slice(0, 30);
      if (item.status !== "unknown") this.pending.delete(this.source + ":" + item.request_id);
    }
    async lookup(id, generation = this.generation, source = this.source) {
      const item = await this.request("commands/request/" + encodeURIComponent(id), undefined, source);
      if (generation === this.generation) { this.save(item); this.detailStatus.set(id, item.status); }
      return item;
    }
    async loadDetails(id) {
      const current = this.items.find(item => item.request_id === id);
      if (!current || this.detailLoading.has(id) || this.detailStatus.get(id) === current.status) return;
      const generation = this.generation;
      this.detailLoading.add(id);
      try { await this.lookup(id); }
      catch (error) { if (generation === this.generation) this.error = error.message; }
      finally {
        if (generation === this.generation) { this.detailLoading.delete(id); this.changed(); }
      }
    }
    async refresh() {
      if (this.refreshing) return;
      const generation = this.generation, source = this.source;
      this.refreshing = true;
      try {
        const catalog = await this.request("commands/catalog", undefined, source);
        if (generation !== this.generation) return;
        if (JSON.stringify(catalog) !== JSON.stringify(this.catalog)) { if (this.catalog?.contract_id !== catalog.contract_id) { this.choices = null; this.choicesAt = null; } this.catalog = catalog; }
        if (!catalog.actions?.some(action => action.id === this.selected && action.available)) this.selected = catalog.actions?.find(action => action.available)?.id || "";
        if (this.status?.connected) {
          if (this.status.phase >= 6) {
            const choices = await this.request("commands/choices", undefined, source);
            if (generation !== this.generation) return;
            this.choices = choices;
            this.choicesAt = Date.now();
          }
          const history = await this.request("commands/history", undefined, source);
          if (generation !== this.generation) return;
          const pendingItems = [...this.pending.values()].filter(item => item.source === source).map(item => item.operation);
          const received = [...(history.items || []), ...pendingItems.filter(item => !(history.items || []).some(saved => saved.request_id === item.request_id))];
          for (const item of received.reverse()) this.save(item);
          for (const entry of [...this.pending.values()].filter(item => item.source === source)) {
            try { await this.lookup(entry.operation.request_id, generation, source); }
            catch { /* A lookup failure never starts another command. */ }
            if (generation !== this.generation) return;
          }
        }
        if (!this.unresolved) this.error = "";
      } catch (error) { if (generation === this.generation) { this.choices = null; this.choicesAt = null; this.error = error.message; } }
      finally { if (generation === this.generation) { this.refreshing = false; this.changed(); } }
    }
    async send(params, preconditions = {}) {
      if (!this.canSend) return;
      if (!params || Array.isArray(params) || typeof params !== "object" || !preconditions || Array.isArray(preconditions) || typeof preconditions !== "object") throw new Error("Use JSON objects for parameters and preconditions.");
      if (this.selected === "stage.position" && this.stageView(params).error) throw new Error(this.stageView(params).error);
      const generation = this.generation, source = this.source;
      const body = { request_id: this.createId(), boot_id: this.status.boot_id, contract_id: this.catalog.contract_id, control_epoch: this.status.control_epoch, action: this.selected, params, preconditions };
      const operation = { ...body, session_id: identity(this.status.principal), status: "unknown", request: body };
      this.pending.set(source + ":" + body.request_id, { source, operation });
      this.save(operation); this.busy = true; this.error = ""; this.changed();
      try {
        const item = await this.request("commands", body, source);
        if (generation === this.generation) this.save({ ...item, request: body });
      } catch (error) {
        if (generation !== this.generation) return;
        try { await this.lookup(body.request_id, generation, source); }
        catch (lookupError) {
          if (generation !== this.generation) return;
          if (error.status >= 400 && error.status < 500 && lookupError.status === 404) this.save({ ...operation, status: "rejected", error: error.message });
          else this.error = "Result unknown. Checking the same request ID. The command will not be sent again.";
        }
      } finally { if (generation === this.generation) { this.busy = false; this.changed(); } }
    }
    async cancel() {
      const active = this.active;
      if (!active || !this.status?.connected) return;
      const generation = this.generation, source = this.source;
      try {
        const item = await this.request("commands/cancel", { request_id: active.request_id }, source);
        if (generation === this.generation) { this.save(item); this.error = ""; }
      } catch (error) {
        if (generation !== this.generation) return;
        this.error = "Cancellation is unconfirmed. Checking the same request ID.";
        try { await this.lookup(active.request_id, generation, source); } catch { /* Preserve the unconfirmed result. */ }
      } finally { if (generation === this.generation) this.changed(); }
    }
    start() { if (!this.timer) { this.refresh(); this.timer = setInterval(() => this.refresh(), 1500); } }
    stop() { this.generation++; clearInterval(this.timer); this.timer = null; this.refreshing = false; this.busy = false; }
  }
  function mount({ request }) {
    const $ = id => document.getElementById(id);
    if (!$("agent-commands")) return null;
    let displayedAction = null, displayedCatalog = null, displayedMenu = null, displayedHistory = null, form;
    function render() {
      const view = client.view, action = client.action;
      $("agent-command-mode").textContent = view.label;
      $("agent-command-note").textContent = !client.status?.connected ? "Connect this agent, then grant operator control from the Console." : !view.mine ? "Grant this agent operator control from the Console." : !view.enabled ? "Commands are unavailable for the current Console contract." : !client.currentContract ? "The command catalog changed. Refresh before sending a command." : client.source === "live" ? "Commands operate physical equipment. Review the parameters, then select Send command." : "Commands use local simulated equipment.";
      if (displayedCatalog !== client.catalog) {
        displayedCatalog = client.catalog;
        $("agent-command-device")?.replaceChildren(...[...new Set((client.catalog?.actions || []).filter(action => action.available).map(action => action.device))].map(device => {
          const option = document.createElement("option"); option.value = device; option.textContent = visuals.labels[device] || device; return option;
        }));
        const unavailable = (client.catalog?.actions || []).filter(action => !action.available);
        $("agent-command-unavailable-count").textContent = `Unavailable actions (${unavailable.length})`;
        $("agent-command-unavailable").replaceChildren(...unavailable.map(action => {
          const row = document.createElement("li"), label = document.createElement("strong"), reason = document.createElement("span");
          label.textContent = action.label; reason.textContent = action.reason || "Unavailable for this source."; row.append(label, reason); return row;
        }));
      }
      const menu = JSON.stringify([client.catalog, action?.device]);
      if (displayedMenu !== menu) {
        displayedMenu = menu;
        if ($("agent-command-device")) $("agent-command-device").value = action?.device || "";
        $("agent-command-action").replaceChildren(...(client.catalog?.actions || []).filter(candidate => candidate.available && candidate.device === action?.device).map(candidate => {
          const option = document.createElement("option"); option.value = candidate.id; option.textContent = candidate.label; return option;
        }));
        $("agent-command-action").value = client.selected;
      }
      const key = client.source + ":" + (client.catalog?.contract_id || "") + ":" + (action?.id || "");
      if (key !== displayedAction) {
        displayedAction = key; $("agent-command-action").value = client.selected;
        form?.setAction(action, client.choices?.actions?.[action?.id] || {});
        $("agent-command-preconditions").value = "{}";
      }
      form?.setChoices(client.choices?.actions?.[action?.id] || {});
      form?.setConstraints(client.stageConstraints, client.choicesAt);
      if ($("agent-command-target")) visuals.renderTarget($("agent-command-target"), visuals.targetPreview(action, form?.model.value, client.stageConstraints, client.choicesAt == null ? Infinity : Math.max(0, Date.now() - client.choicesAt) / 1000));
      let previewError = "";
      try {
        const preconditions = JSON.parse($("agent-command-preconditions").value || "{}");
        if (!preconditions || Array.isArray(preconditions) || typeof preconditions !== "object") throw new Error("Use a JSON object for preconditions.");
        if ($("agent-command-request")) $("agent-command-request").textContent = form?.model.parseError || JSON.stringify(requestPreview(client, form?.model.value || {}, preconditions), null, 2);
      } catch (error) { previewError = "Preconditions must contain a valid JSON object."; if ($("agent-command-request")) $("agent-command-request").textContent = previewError; }
      $("agent-command-description").textContent = action ? [action.description, action.available ? "" : action.reason].filter(Boolean).join(" ") : client.catalog ? "No commands are available for this source. See unavailable actions below." : "No command catalog received.";
      $("agent-command-schema").textContent = JSON.stringify(action?.params || {}, null, 2);
      $("agent-command-action").disabled = !(client.catalog?.actions || []).some(action => action.available);
      $("agent-command-send").disabled = !client.canSend || Boolean(form?.model.error) || Boolean(previewError);
      $("agent-command-validation").textContent = action ? form?.model.error || previewError : "";
      $("agent-command-cancel").disabled = !client.active || !client.status?.connected;
      const current = client.active || client.items[0];
      const error = [client.error, commandError(current)].filter(Boolean).join(" ");
      $("agent-command-error").hidden = !error; $("agent-command-error").textContent = error;
      const progress = Object.entries(["accepted", "running", "cancelling"].includes(current?.status) ? current.progress || {} : {}).filter(([, value]) => value !== null && ["string", "number", "boolean"].includes(typeof value)).slice(0, 4).map(([key, value]) => `${key}: ${value}`).join(" · ");
      $("agent-command-progress").textContent = current ? `${current.action} · ${current.status}${progress ? " · " + progress : ""}` : "No commands sent.";
      const history = JSON.stringify(client.items);
      if (history !== displayedHistory) {
        const open = new Set(Array.from($("agent-command-history").children).filter(item => item.open).map(item => item.dataset.requestId));
        displayedHistory = history;
        $("agent-command-history").replaceChildren(...client.items.map(item => {
          const details = document.createElement("details"), summary = document.createElement("summary"), raw = document.createElement("pre");
          details.dataset.requestId = item.request_id; details.open = open.has(item.request_id);
          details.addEventListener("toggle", () => { if (details.open) client.loadDetails(item.request_id); });
          summary.textContent = `${item.action} · ${item.status} · ${item.request_id}`;
          raw.textContent = JSON.stringify(item, null, 2); details.append(summary, raw); return details;
        }));
      }
    }
    const client = new CommandClient({ request, changed: render });
    form = new root.AgentCommandForm.ParameterForm($("agent-command-fields"), $("agent-command-params"), render);
    $("agent-command-action").onchange = event => client.select(event.target.value);
    if ($("agent-command-device")) $("agent-command-device").onchange = event => client.select(client.catalog?.actions?.find(action => action.available && action.device === event.target.value)?.id || "");
    $("agent-command-preconditions").addEventListener("input", render);
    $("agent-command-form").onsubmit = async event => {
      event.preventDefault();
      try { await client.send(form.getValue(), JSON.parse($("agent-command-preconditions").value)); }
      catch (error) { client.error = error.message; render(); }
    };
    $("agent-command-cancel").onclick = () => client.cancel();
    $("agent-command-refresh").onclick = () => client.refresh();
    addEventListener("pagehide", () => client.stop());
    addEventListener("pageshow", () => client.start());
    render(); client.start(); return client;
  }
  const exports = { commandView, defaultsFor, commandError, requestPreview, CommandClient, mount };
  if (typeof module !== "undefined") module.exports = exports;
  root.AgentCommandPortal = exports;
})(typeof window === "undefined" ? globalThis : window);
