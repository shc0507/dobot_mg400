(function (root) {
  "use strict";
  const targets = ["stage", "robot", "microscope", "all_motion", "furnace", "gas_all", "gas_0", "gas_1", "gas_2"];
  const labels = { stage: "Stage", robot: "Robot", microscope: "Microscope", all_motion: "All motion", furnace: "Heat and gas", gas_all: "All gas", gas_0: "Gas channel 0", gas_1: "Gas channel 1", gas_2: "Gas channel 2" };
  function senderLabel(sender) {
    if (!sender) return "Unidentified sender";
    const identity = sender.name || sender.id || (sender.kind === "system" ? "System" : sender.kind === "human" ? "Human" : "Unidentified sender");
    return [identity, sender.role, sender.verified ? "verified" : "unverified"].filter(Boolean).join(" · ");
  }
  function auditMessage(audit) {
    if (!audit) return "";
    if (audit.error || audit.dropped_records || ["failed", "unavailable", "error"].includes(audit.status)) return "Stop history could not be saved completely. Stop dispatch does not wait for history storage.";
    return audit.pending ? "Stop history is being saved." : "";
  }
  function sourceMessage(sources) {
    return Object.entries(sources || {}).map(([origin, source]) => {
      const label = origin === "fallback" ? "Fallback" : "Console";
      return source.available ? auditMessage(source.audit) ? `${label}: ${auditMessage(source.audit)}` : "" : `${label} Stop history is unavailable.`;
    }).filter(Boolean).join(" ");
  }
  function mergeRecord(previous, record, origin = "console") {
    const saved = previous?.sources?.[origin];
    if (saved && (saved.updated_at || 0) > (record.updated_at || 0)) return previous;
    const sources = { ...previous?.sources, [origin]: record };
    if (origin !== "pending") delete sources.pending;
    const rows = Object.values(sources), states = rows.map(item => item.status);
    const status = ["unknown", "unconfirmed", "pending", "disabled", "confirmed"].find(value => states.includes(value)) || "unknown";
    return { ...previous, ...record, status, sources, updated_at: Math.max(...rows.map(item => item.updated_at || 0)) };
  }
  class StopClient {
    constructor({ request, changed = () => {}, createId = () => root.crypto.randomUUID() }) {
      Object.assign(this, { request, changed, createId, source: "live", status: null, items: [], audit: null, sourceStatus: null, error: "", generation: 0, reading: false });
      this.details = new Map(); this.detailLoading = new Set();
    }
    setContext(source, status) {
      if (source !== this.source) { this.generation++; this.source = source; this.items = []; this.details.clear(); this.detailLoading.clear(); this.audit = null; this.sourceStatus = null; this.error = ""; this.reading = false; }
      this.status = status; this.changed();
    }
    get enabled() { return ["mock", "live"].includes(this.source); }
    save(record, origin = record?.relay_source || "console") {
      if (!record?.request_id) return;
      if (record.relay_sources) { for (const [source, item] of Object.entries(record.relay_sources)) this.save(item, source); return; }
      const matches = item => item.request_id === record.request_id && item.target === record.target;
      const previous = this.items.find(matches);
      if ((record.updated_at || 0) > (this.details.get(record.request_id)?.updated_at || 0)) this.details.delete(record.request_id);
      this.items = [mergeRecord(previous, record, origin), ...this.items.filter(item => !matches(item))].sort((a, b) => (b.requested_at || 0) - (a.requested_at || 0)).slice(0, 50);
    }
    async send(target) {
      if (!this.enabled || !targets.includes(target)) return;
      const source = this.source, generation = this.generation;
      const body = { request_id: this.createId(), target };
      this.save({ ...body, requested_at: Date.now() / 1000, status: "pending", sender: null }, "pending"); this.changed();
      try { const result = await this.request("stop", body, source); if (generation === this.generation) { this.save(result); this.error = ""; } }
      catch { if (generation === this.generation) { this.save({ ...body, requested_at: Date.now() / 1000, status: "unknown" }, "pending"); this.error = "Stop is unconfirmed. Check device status. You can send Stop again."; } }
      finally { if (generation === this.generation) { this.changed(); this.refresh(); } }
    }
    async refresh() {
      if (this.reading) return;
      const source = this.source, generation = this.generation; this.reading = true;
      try {
        const result = await this.request("stops", undefined, source);
        if (generation !== this.generation) return;
        for (const record of result.items || []) this.save(record, record.relay_source || result.relay_source || "console");
        this.audit = result.audit;
        this.sourceStatus = result.source_status;
        if (this.error.startsWith("Stop history is unavailable")) this.error = "";
      } catch { if (generation === this.generation) this.error = "Stop history is unavailable. The Stop buttons remain available."; }
      finally { if (generation === this.generation) { this.reading = false; this.changed(); } }
    }
    async loadDetails(id) {
      if (this.detailLoading.has(id)) return;
      const generation = this.generation, source = this.source; this.detailLoading.add(id);
      try { const item = await this.request("stops/" + encodeURIComponent(id), undefined, source); if (generation === this.generation) { this.save(item); this.details.set(id, item); } }
      catch { if (generation === this.generation) this.error = "Detailed Stop results are unavailable."; }
      finally { if (generation === this.generation) { this.detailLoading.delete(id); this.changed(); } }
    }
    start() { if (!this.timer) { this.refresh(); this.timer = setInterval(() => this.refresh(), 1500); } }
    stop() { this.generation++; clearInterval(this.timer); this.timer = null; this.reading = false; }
  }
  function mount(container, { request }) {
    const make = (tag, text, className) => { const node = document.createElement(tag); if (text) node.textContent = text; if (className) node.className = className; return node; };
    const title = make("h3", "Device Stop"), note = make("p", "", "section-note aoc-help"), buttons = make("div", "", "stream-actions aoc-actions"), error = make("p", "", "live-error aoc-error"), audit = make("p", "", "section-note aoc-help");
    error.setAttribute("role", "alert"); audit.setAttribute("role", "status");
    const history = make("details", "", "stop-history"), summary = make("summary", "Stop history"), rows = make("div", "", "stop-rows"); history.append(summary, rows);
    const buttonNodes = targets.map(target => { const button = make("button", "Stop " + labels[target], "aoc-stop"); button.type = "button"; button.dataset.stopTarget = target; button.onclick = () => client.send(target); buttons.append(button); return button; });
    const refresh = make("button", "Refresh Stop history"); refresh.type = "button"; refresh.onclick = () => client.refresh(); history.append(refresh);
    container.append(title, note, buttons, error, audit, history);
    let displayed = null;
    function render() {
      note.textContent = client.source === "mock" ? "Stops local simulated devices. Operator control is not required." : "Stops physical equipment. Connection access and operator control are not required.";
      buttonNodes.forEach(button => { button.disabled = !client.enabled; });
      error.hidden = !client.error; error.textContent = client.error;
      audit.textContent = [auditMessage(client.audit), sourceMessage(client.sourceStatus)].filter(Boolean).join(" "); audit.hidden = !audit.textContent;
      const signature = JSON.stringify([client.items, [...client.details]]);
      if (signature === displayed) return; displayed = signature;
      const open = new Set([...rows.children].filter(row => row.open).map(row => row.dataset.requestId));
      rows.replaceChildren(...client.items.map(item => {
        const sender = Object.entries(item.sources || {}).map(([origin, record]) => `${origin === "pending" ? "Portal" : origin === "fallback" ? "Fallback" : "Console"}: ${senderLabel(record.sender)}`).join(" / ") || senderLabel(item.sender);
        const row = make("details", "", "stop-record"), heading = make("summary"), actor = make("p", sender, "section-note aoc-help");
        row.dataset.requestId = item.request_id; row.open = open.has(item.request_id);
        const time = typeof item.requested_at === "number" ? new Date(item.requested_at * 1000).toLocaleTimeString() : "Unknown time";
        heading.textContent = `${time} · ${labels[item.target] || item.target} · ${item.status} · ${sender}`;
        const raw = make("pre", JSON.stringify({ ...item, ...client.details.get(item.request_id), sources: item.sources }, null, 2));
        row.addEventListener("toggle", () => { if (row.open && !client.details.has(item.request_id)) client.loadDetails(item.request_id); });
        row.append(heading, actor, make("p", `Request: ${item.request_id} · Deliveries: ${item.delivery_count ?? 1}`, "section-note aoc-help"), raw); return row;
      }));
      if (!client.items.length) rows.append(make("p", "No Stop requests received.", "section-note aoc-help"));
    }
    const client = new StopClient({ request, changed: render }); render(); client.start(); return client;
  }
  const exports = { targets, senderLabel, auditMessage, sourceMessage, mergeRecord, StopClient, mount };
  if (typeof module !== "undefined") module.exports = exports;
  root.AgentStopPanel = exports;
})(typeof window === "undefined" ? globalThis : window);
