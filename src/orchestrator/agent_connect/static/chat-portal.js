(function (root) {
  "use strict";
  const terminal = new Set(["succeeded", "failed", "rejected", "cancelled", "canceled", "unknown"]);
  const phases = { reading: "Reading Console state", interpreting: "Interpreting instruction", validating: "Checking command", sending: "Sending command", executing: "Waiting for command result", completed: "Completed", failed: "Failed", cancelling: "Cancellation requested", cancelled: "Cancelled" };
  function phaseLabel(phase) { return Object.hasOwn(phases, phase) ? phases[phase] : "Activity update"; }
  function activity(status) {
    const latest = status?.activity?.at(-1);
    if (status?.busy && latest) return phaseLabel(latest.phase);
    const command = status?.active;
    if (command && !terminal.has(command.status)) return `${command.action || "Command"} · ${command.status || "Submitted"}`;
    if (status?.busy) return "Interpreting your instruction…";
    return status?.model?.configured ? "Ready for your instruction." : "Set up the OpenAI model to begin.";
  }
  function tabFromHash(hash) { return ["#manual", "#manual-testing", "#agent-commands", "#live-view"].includes(hash) ? "manual" : "agent"; }
  class ActivityTimeline {
    constructor({ list, empty, summary, document }) {
      Object.assign(this, { list, empty, summary, document, source: null, rows: new Map(), latest: null });
    }
    node(tag, text = "", className = "") {
      const element = this.document.createElement(tag);
      element.textContent = text; element.className = className;
      return element;
    }
    row(entry) {
      const item = this.node("li", "", "chat-event"), heading = this.node("header");
      item.dataset.phase = Object.hasOwn(phases, entry.phase) ? entry.phase : "unknown";
      heading.append(this.node("strong", phaseLabel(entry.phase)));
      const date = new Date(entry.at);
      if (entry.at && !Number.isNaN(date.valueOf())) {
        const time = this.node("time", date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
        time.dateTime = entry.at; time.title = date.toLocaleString(); heading.append(time);
      }
      item.append(heading);
      if (entry.text) item.append(this.node("p", entry.text));
      for (const [field, label] of [["command", "Exact outgoing command"], ["result", "Returned result"]]) {
        if (!Object.hasOwn(entry, field)) continue;
        const details = this.node("details"), pre = this.node("pre", JSON.stringify(entry[field], null, 2));
        details.append(this.node("summary", label), pre); item.append(details);
      }
      return item;
    }
    render(entries, source) {
      const events = Array.isArray(entries) ? entries : [];
      const atEnd = this.list.scrollHeight - this.list.scrollTop - this.list.clientHeight < 70;
      let changed = false;
      if (source !== this.source) {
        this.source = source; this.rows.clear(); this.list.replaceChildren(); this.latest = null; changed = true;
      }
      const ids = new Set();
      for (const [index, entry] of events.entries()) {
        const id = String(entry.id ?? index); ids.add(id);
        if (this.rows.has(id)) continue;
        const item = this.row(entry); this.rows.set(id, item); this.list.append(item); changed = true;
      }
      for (const [id, row] of this.rows) if (!ids.has(id)) { row.remove(); this.rows.delete(id); changed = true; }
      if (this.latest) this.latest.classList.toggle("latest", false);
      const last = events.at(-1);
      this.latest = last ? this.rows.get(String(last.id ?? events.length - 1)) : null;
      if (this.latest) this.latest.classList.toggle("latest", true);
      this.empty.hidden = events.length > 0;
      this.summary.textContent = last ? phaseLabel(last.phase) : "No activity yet";
      if (changed && atEnd) this.list.scrollTop = this.list.scrollHeight;
    }
  }
  class ChatClient {
    constructor({ request, changed = () => {}, createId = () => root.crypto.randomUUID() }) {
      Object.assign(this, { request, changed, createId, source: "live", status: null, error: "", sending: false, configuring: false, cancelling: false, generation: 0, mutation: 0, refreshing: null, timer: null, pending: null });
    }
    apply(value) { this.status = value; this.changed(); }
    get busy() { return this.sending || this.configuring || this.status?.busy === true; }
    async refresh() {
      if (this.refreshing || this.sending || this.configuring) return;
      const marker = {}, generation = this.generation, mutation = this.mutation, source = this.source;
      this.refreshing = marker;
      try {
        const value = await this.request("status", undefined, source);
        if (generation === this.generation && mutation === this.mutation) this.apply(value);
      } catch (error) { if (generation === this.generation && mutation === this.mutation) { this.error = error.message; this.changed(); } }
      finally { if (this.refreshing === marker) this.refreshing = null; }
    }
    async send(message) {
      const text = message.trim();
      if (!text || this.busy) return false;
      const source = this.source, generation = this.generation;
      const pending = this.pending?.source === source && this.pending.message === text ? this.pending : { source, message: text, request_id: this.createId() };
      this.pending = pending; this.sending = true; this.mutation++; this.error = ""; this.changed();
      const mutation = this.mutation;
      try {
        const value = await this.request("message", pending, source);
        if (generation !== this.generation || mutation !== this.mutation) return false;
        this.pending = null; this.apply(value); return true;
      } catch (error) {
        if (generation === this.generation) { this.error = error.message + " No request was sent again automatically. Check the conversation before retrying."; this.changed(); }
        return false;
      } finally { if (generation === this.generation) { this.sending = false; this.changed(); this.refresh(); } }
    }
    async configure(model, apiKey) {
      if (this.busy) return false;
      const generation = this.generation;
      this.configuring = true; this.mutation++; this.error = ""; this.changed();
      try {
        const value = await this.request("settings", { model: model.trim(), api_key: apiKey }, this.source);
        if (generation === this.generation) this.apply({ ...this.status, model: value.model });
        return true;
      } catch (error) { if (generation === this.generation) { this.error = error.message; this.changed(); } return false; }
      finally { if (generation === this.generation) { this.configuring = false; this.changed(); this.refresh(); } }
    }
    async cancel() {
      if (this.cancelling) return;
      const generation = this.generation;
      this.cancelling = true; this.mutation++; this.error = ""; this.changed();
      try {
        const value = await this.request("cancel", { source: this.source }, this.source);
        if (generation === this.generation) this.apply(value);
      } catch (error) { if (generation === this.generation) this.error = error.message; }
      finally { if (generation === this.generation) { this.cancelling = false; this.changed(); this.refresh(); } }
    }
    async setSource(source) {
      if (!["live", "mock"].includes(source) || source === this.source) return;
      const previous = this.source;
      this.source = source; this.generation++; this.mutation++; this.status = null; this.pending = null; this.refreshing = null; this.sending = false; this.configuring = false; this.cancelling = false; this.error = ""; this.changed();
      const generation = this.generation;
      try { await this.request("cancel", { source: previous }, previous); }
      catch { if (generation === this.generation) { this.error = "Cancellation of the previous agent task was not confirmed. Check its status before returning to that source."; this.changed(); } }
      if (generation === this.generation) await this.refresh();
    }
    start() { if (!this.timer) { this.refresh(); this.timer = setInterval(() => this.refresh(), 1000); } }
    stop() { this.generation++; clearInterval(this.timer); this.timer = null; this.refreshing = null; this.sending = false; this.configuring = false; this.cancelling = false; }
  }
  function mount() {
    const $ = id => document.getElementById(id);
    if (!$("agent-chat-view")) return null;
    const tabs = [...document.querySelectorAll("[data-workspace]")];
    function selectTab(name, focus = false, updateHash = true) {
      for (const tab of tabs) {
        const selected = tab.dataset.workspace === name;
        tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1;
        $(tab.getAttribute("aria-controls")).hidden = !selected;
        if (selected && focus) tab.focus();
      }
      if (updateHash) history.replaceState(null, "", name === "manual" ? "#manual" : "#agent");
    }
    for (const [index, tab] of tabs.entries()) {
      tab.onclick = () => selectTab(tab.dataset.workspace);
      tab.onkeydown = event => {
        let target;
        if (event.key === "ArrowRight") target = (index + 1) % tabs.length;
        else if (event.key === "ArrowLeft") target = (index + tabs.length - 1) % tabs.length;
        else if (event.key === "Home") target = 0;
        else if (event.key === "End") target = tabs.length - 1;
        if (target !== undefined) { event.preventDefault(); selectTab(tabs[target].dataset.workspace, true); }
      };
    }
    addEventListener("hashchange", () => selectTab(tabFromHash(location.hash), false, false));
    selectTab(tabFromHash(location.hash), false, false);
    async function request(path, data, source) {
      const options = { credentials: "same-origin", cache: "no-store", redirect: "error", headers: { Accept: "application/json" } };
      let url = "/api/agent-chat/" + path;
      if (data === undefined) url += "?source=" + encodeURIComponent(source);
      else { options.method = "POST"; options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(data); }
      const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 10000);
      options.signal = controller.signal;
      try { const response = await fetch(url, options), value = await response.json(); if (!response.ok) throw new Error(value.error || "The local agent refused the request."); return value; }
      finally { clearTimeout(timeout); }
    }
    const node = (tag, text, className) => { const value = document.createElement(tag); if (text) value.textContent = text; if (className) value.className = className; return value; };
    const timeline = new ActivityTimeline({ list: $("chat-events"), empty: $("chat-events-empty"), summary: $("chat-phase"), document });
    let renderedMessages = null, modelInitialized = false;
    function renderMessages(messages) {
      const key = JSON.stringify([client.source, messages]);
      if (key === renderedMessages) return;
      renderedMessages = key;
      const list = $("chat-messages"), atEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 70;
      list.replaceChildren();
      if (!messages.length) list.append(node("p", "Ask for a reading, a position, or one equipment action. You can inspect every action and result here.", "chat-welcome"));
      for (const message of messages) {
        const item = node("article", "", "chat-message" + (message.role === "user" ? " user" : ""));
        const heading = node("header"), role = message.role === "user" ? "You" : message.role === "system" ? "Console" : "Agent";
        heading.append(node("strong", role));
        if (message.kind && !["answer", "user"].includes(message.kind)) heading.append(node("span", message.kind.replaceAll("_", " "), "status-pill"));
        item.append(heading, node("p", message.text || ""));
        if (message.action || message.params || message.command) {
          const details = node("details"), data = {};
          for (const field of ["action", "params", "command"]) if (message[field] !== undefined) data[field] = message[field];
          details.append(node("summary", message.action || "Action and result"), node("pre", JSON.stringify(data, null, 2)));
          item.append(details);
        }
        list.append(item);
      }
      if (atEnd) list.scrollTop = list.scrollHeight;
    }
    function render() {
      const value = client.status, configured = value?.model?.configured === true;
      $("chat-source").textContent = client.source === "live" ? "Olares · Live equipment" : "Local simulation";
      $("chat-source").classList.toggle("warning", client.source === "mock");
      $("chat-error").hidden = !client.error; $("chat-error").textContent = client.error;
      $("chat-model-status").textContent = configured ? "Ready" : "Not configured";
      if (!modelInitialized && value?.model?.model) { $("chat-model").value = value.model.model; modelInitialized = true; if (configured) $("chat-model-settings").open = false; }
      $("chat-model-save").disabled = client.busy;
      $("chat-send").disabled = !configured || client.busy || !$("chat-message").value.trim();
      $("chat-cancel").disabled = client.cancelling || !(client.busy || value?.active && !terminal.has(value.active.status));
      $("chat-activity").textContent = client.sending ? "Sending your instruction…" : activity(value);
      $("chat-activity").classList.toggle("working", client.busy);
      renderMessages(value?.messages || []);
      timeline.render(value?.activity || [], client.source);
    }
    const client = new ChatClient({ request, changed: render });
    $("chat-message").oninput = render;
    $("chat-message").onkeydown = event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!$("chat-send").disabled) $("chat-form").requestSubmit(); } };
    $("chat-form").onsubmit = async event => {
      event.preventDefault(); const text = $("chat-message").value;
      if (await client.send(text) && $("chat-message").value === text) $("chat-message").value = "";
      render();
    };
    $("chat-model-form").onsubmit = async event => {
      event.preventDefault(); const key = $("chat-api-key").value; $("chat-api-key").value = "";
      if (await client.configure($("chat-model").value, key)) $("chat-model-settings").open = false;
    };
    $("chat-cancel").onclick = () => client.cancel();
    $("chat-open-manual").onclick = () => selectTab("manual", true);
    for (const button of document.querySelectorAll("[data-chat-example]")) button.onclick = () => { $("chat-message").value = button.dataset.chatExample; $("chat-message").focus(); render(); };
    addEventListener("portal-test-mode", event => client.setSource(event.detail ? "mock" : "live"));
    addEventListener("pagehide", () => client.stop());
    addEventListener("pageshow", () => client.start());
    render(); client.start(); return client;
  }
  const exports = { activity, phaseLabel, tabFromHash, ActivityTimeline, ChatClient, mount };
  if (typeof module !== "undefined") module.exports = exports;
  root.AgentChatPortal = exports;
  if (typeof document !== "undefined") mount();
})(typeof window === "undefined" ? globalThis : window);
