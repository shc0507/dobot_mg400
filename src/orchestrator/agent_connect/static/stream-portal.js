(function (global) {
  "use strict";
  const prefix = "/lab/api/agent";
  function canonical(value) {
    if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
    if (value && typeof value === "object") return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(",")}}`;
    return JSON.stringify(value);
  }
  function compatible(bundle) {
    return bundle?.$schema === "https://json-schema.org/draft/2020-12/schema"
      && bundle.contract === "agent-connect" && /^1\./.test(bundle.version)
      && bundle.semantics?.units?.position_rotations === "rotation"
      && bundle.semantics?.units?.pv_c === "degC"
      && bundle.schemas?.state?.properties?.type?.const === "state";
  }
  function locationText(value) {
    if (!value || value.holder === "unknown" || value.holder == null) return "Unknown";
    return ({ tray_slot: `Tray ${value.tray_id || "?"} / ${value.slot_id || "?"}`, robot: "Robot", stage_insert: "Stage insert" })[value.holder] || "Unknown";
  }
  const logic = { canonical, compatible, locationText };
  if (typeof module !== "undefined") module.exports = logic;
  global.AgentStreamPortal = logic;
  if (typeof document === "undefined" || !document.getElementById("stream-tests")) return;
  const $ = id => document.getElementById(id);
  const make = (tag, text, cls) => { const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node; };
  const activity = [];
  let browse = null, browseRevision = null, nextOffset = null;
  async function api(path, body) {
    const response = await fetch(path, { method: body === undefined ? "GET" : "POST", headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
    const data = await response.json(); if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`); return data;
  }
  const read = path => api(`/api/stream/read?path=${encodeURIComponent(path)}`);
  function log(text) { activity.unshift(`${new Date().toLocaleTimeString()}: ${text}`); activity.length = Math.min(activity.length, 40); $("stream-activity").replaceChildren(...activity.map(value => make("p", value, "section-note"))); }
  function error(value) { $("stream-error").hidden = !value; $("stream-error").textContent = value || ""; }
  function options() {
    return { groups: ["devices", "samples", "operations"].filter(name => $("stream-" + name).checked), rate_hz: Number($("stream-rate").value), selection: $("stream-selection").value, chip_ids: $("stream-ids").value.split(/\r?\n/).filter(Boolean) };
  }
  class Reader {
    constructor(name) {
      this.name = name; this.wanted = false; this.cache = new Map(); this.cursor = null; this.generation = 0;
      this.socket = null; this.discovery = null; this.state = null; this.raw = null;
      this.status = "Connecting"; this.frames = 0; this.bytes = 0; this.slow = false;
      this.pendingState = null; this.events = []; this.processing = false; this.timer = null;
      this.started = performance.now(); this.lastAt = null; this.subscription = options();
      this.card = make("article", null, "client");
      const heading = make("h2", name); this.toggle = make("button", "Disconnect");
      this.toggle.onclick = () => { if (this.wanted) this.disconnect(); else { this.wanted = true; this.open(); } render(); };
      heading.append(this.toggle); this.statusNode = make("p", "", "status"); this.metrics = make("p", "", "stream-metrics");
      this.slowButton = make("button", "Read slowly"); this.slowButton.onclick = () => { this.slow = !this.slow; this.slowButton.textContent = this.slow ? "Read normally" : "Read slowly"; this.drain(); };
      this.card.append(heading, this.statusNode, this.metrics, this.slowButton, make("p", "A slow reader keeps the newest state. Missing events are reported.", "section-note"));
      $("stream-clients").append(this.card);
    }
    async definition(identity, path) {
      if (!/^sha256:[a-f0-9]{64}$/.test(identity)) throw new Error("The agreement ID is invalid.");
      const expected = `${prefix}/contracts/${identity.slice(7)}.json`;
      if (path && path !== expected) throw new Error("The agreement path is outside the simulation.");
      if (this.cache.has(identity)) return this.cache.get(identity);
      const value = await read(expected);
      const hash = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonical(value)));
      const actual = "sha256:" + Array.from(new Uint8Array(hash), b => b.toString(16).padStart(2, "0")).join("");
      if (actual !== identity) throw new Error("The agreement file failed its integrity check.");
      this.cache.set(identity, value); while (this.cache.size > 8) this.cache.delete(this.cache.keys().next().value);
      return value;
    }
    async discover() {
      const first = await read(`${prefix}/discovery`);
      if (first.mode !== "MOCK" || first.discovery_version !== 1) throw new Error("Only the local simulated Console is supported.");
      const definition = await this.definition(first.contract_id, first.contract_url);
      const second = await read(`${prefix}/discovery`);
      if (first.boot_id !== second.boot_id || first.contract_id !== second.contract_id) throw new Error("The Console changed during the agreement check. Retrying.");
      this.discovery = first;
      this.supported = compatible(definition);
      if (!this.supported) { this.state = null; this.status = "Agent update needed - readings are not decoded"; }
    }
    async open() {
      const generation = ++this.generation;
      clearTimeout(this.timer);
      if (!this.wanted) return;
      try {
        await this.discover();
        if (!this.wanted || generation !== this.generation) return;
        const query = new URLSearchParams({ groups: this.subscription.groups.join(","), rate_hz: this.subscription.rate_hz, selection: this.subscription.selection });
        if (this.subscription.selection === "ids") for (const id of this.subscription.chip_ids) query.append("chip_id", id);
        if (this.cursor) { query.set("boot_id", this.cursor.boot_id); query.set("event_seq", this.cursor.event_seq); }
        const socket = new WebSocket(`ws://127.0.0.1:8793${prefix}/v1/stream?${query}`);
        this.socket = socket;
        socket.onopen = () => { if (this.supported) this.status = "Receiving messages"; log(`${this.name} connected`); render(); };
        socket.onmessage = event => {
          if (this.socket !== socket || !this.wanted) return;
          if (typeof event.data !== "string" || event.data.length > 524288) { socket.close(); return; }
          try {
            const message = JSON.parse(event.data); this.bytes += new TextEncoder().encode(event.data).length;
            if (message.type === "state") this.pendingState = message;
            else { this.events.push(message); if (this.events.length > 32) { this.events.shift(); this.state = null; this.status = "Some events were missed. Reconnecting."; socket.close(); } }
            this.drain();
          } catch { this.status = "Unreadable stream message"; socket.close(); }
        };
        socket.onclose = () => { if (this.socket !== socket) return; this.socket = null; if (this.wanted) { this.status = "Connection lost. Retrying"; this.timer = setTimeout(() => this.open(), 1500); } render(); };
        socket.onerror = () => { this.status = "Stream unavailable"; render(); };
      } catch (cause) { this.status = cause.message; if (this.wanted) this.timer = setTimeout(() => this.open(), 1500); render(); }
    }
    disconnect() {
      this.generation++;
      this.wanted = false; clearTimeout(this.timer); this.pendingState = null; this.events = [];
      const socket = this.socket; this.socket = null; if (socket) socket.close();
      this.status = "Disconnected - last received data"; log(`${this.name} disconnected`);
    }
    async drain() {
      if (this.processing || !this.wanted) return;
      this.processing = true;
      try {
        if (this.slow) await new Promise(resolve => setTimeout(resolve, 2500));
        if (!this.wanted) return;
        const messages = this.events.splice(0, 32);
        if (this.pendingState) { messages.push(this.pendingState); this.pendingState = null; }
        for (const message of messages) {
          this.raw = message;
          if (message.type === "resync") {
            this.cursor = { boot_id: message.boot_id, event_seq: message.event_seq };
            this.state = null; log(`${this.name}: saved events are incomplete (${message.reason}). Reading a fresh snapshot.`);
            await this.discover(); continue;
          }
          if (message.type === "event") {
            const definition = await this.definition(message.contract_id);
            if (!compatible(definition)) { log(`${this.name}: an older event uses an unsupported agreement; kept as raw data.`); }
            else log(`${this.name}: ${message.kind}`);
            this.cursor = { boot_id: message.boot_id, event_seq: message.event_seq };
            if (message.kind === "contract.changed") await this.discover();
            continue;
          }
          if (message.type !== "state") continue;
          if (message.boot_id !== this.discovery?.boot_id || message.contract_id !== this.discovery?.contract_id) await this.discover();
          if (!this.supported || message.boot_id !== this.discovery?.boot_id || message.contract_id !== this.discovery?.contract_id) { this.state = null; continue; }
          if (message.mode !== "MOCK" || !Number.isSafeInteger(message.seq) || !Number.isFinite(message.generated_at)) throw new Error("The state message does not match the verified agreement.");
          this.state = message; this.frames++; this.lastAt = Date.now(); this.status = this.slow ? "Receiving slowly - newest state retained" : "Receiving messages";
          if (!this.cursor) this.cursor = { boot_id: message.boot_id, event_seq: message.event_seq };
        }
      } catch (cause) { this.state = null; this.status = cause.message; log(`${this.name}: ${cause.message}`); }
      finally { this.processing = false; render(); if (this.wanted && (this.events.length || this.pendingState)) this.drain(); }
    }
  }
  const readers = [new Reader("Connection A"), new Reader("Connection B")];
  function render() {
    for (const reader of readers) {
      reader.toggle.textContent = reader.wanted ? "Disconnect" : "Reconnect";
      reader.statusNode.textContent = reader.status;
      const age = reader.lastAt ? ((Date.now() - reader.lastAt) / 1000).toFixed(1) : "unknown";
      reader.metrics.textContent = `${reader.frames} frames received | ${(reader.bytes / 1024).toFixed(1)} KiB total | last message ${age} s ago`;
    }
    const frame = readers[0].state;
    const deviceCards = [];
    for (const [name, label, value, unit] of [["stage", "Insert position", frame?.devices?.stage?.values?.axes?.[0]?.position_rotations, "rotations"], ["furnace", "Temperature", frame?.devices?.furnace?.values?.pv_c, "\u00b0C"]]) {
      const device = frame?.devices?.[name];
      if (!device) continue;
      const seconds = typeof device.age_s === "number" ? device.age_s + Math.max(0, Date.now() / 1000 - frame.generated_at) : null;
      const old = !readers[0].wanted || seconds === null || seconds > 3 || device.quality !== "valid";
      const card = make("article", null, `reading${old ? " stale" : ""}`);
      const usable = ["valid", "stale"].includes(device.quality) && typeof value === "number" && Number.isFinite(value);
      card.append(make("span", label), make("strong", usable ? `${value.toFixed(3)} ${unit}` : "Unavailable"), make("small", `${old ? "Outdated or unavailable reading" : "Up to date"}. Cache age: ${seconds === null ? "unknown" : seconds.toFixed(1) + " s"}.`));
      deviceCards.push(card);
    }
    $("stream-readings").replaceChildren(...deviceCards);
    const page = browse || frame?.samples;
    const rows = page?.items || [];
    $("stream-count").textContent = page ? `${page.count ?? rows.length} of ${page.total ?? rows.length} chips. ${page.complete ? "Selection complete." : "More chips are available."} ${browse ? "Saved-chip page; click Apply to return to live selection." : "From Connection A."}` : "No usable chip selection received.";
    $("stream-chip-rows").replaceChildren(...rows.map(row => {
      const tr = make("tr");
      const basis = { observed: "Camera observation", reported: "Manual report", inferred: "Inferred", unknown: "Unknown" }[row.basis] || "Unknown";
      const chipCell = make("td"), link = make("a", row.chip_id);
      link.href = `http://127.0.0.1:8790/lab/chip?id=${encodeURIComponent(row.chip_id)}`; link.target = "_blank"; link.rel = "noopener"; chipCell.append(link); tr.append(chipCell);
      for (const text of [locationText(row.location), basis, row.location?.inside_furnace === true ? "Yes" : row.location?.inside_furnace === false ? "No" : "Unknown", row.pending_operation?.action || "None"]) tr.append(make("td", text));
      tr.title = JSON.stringify({ last_known_location: row.last_known_location, last_observation: row.last_observation, conflict: row.conflict, last_run: row.last_run, last_step: row.last_step }, null, 2);
      return tr;
    }));
    $("stream-raw").textContent = JSON.stringify(readers[0].raw, null, 2);
  }
  async function act(action, extra = {}) {
    try { const result = await api("/api/stream/sample", { action, chip_id: $("report-chip").value, ...extra }); $("report-result").textContent = `Saved: ${action}. Current location: ${locationText(result.location)}.`; browse = null; error(null); log(`Sample test: ${action}`); }
    catch (cause) { error(cause.message); }
  }
  for (const button of document.querySelectorAll("[data-panel]")) button.onclick = () => {
    for (const tab of document.querySelectorAll("[data-panel]")) { const selected = tab === button; tab.setAttribute("aria-pressed", String(selected)); $(tab.dataset.panel).hidden = !selected; }
    dispatchEvent(new CustomEvent("portal-test-mode", { detail: $("enable-tests")?.checked ? button.dataset.panel : null }));
  };
  $("stream-apply").onclick = () => { browse = null; $("stream-next").hidden = true; for (const reader of readers) { reader.disconnect(); reader.cursor = null; reader.state = null; reader.subscription = options(); reader.wanted = true; reader.open(); } };
  $("sample-report").onsubmit = event => { event.preventDefault(); const holder = $("report-holder").value; act("report", { location: { holder, tray_id: holder === "tray_slot" ? $("report-tray").value : null, slot_id: holder === "tray_slot" ? $("report-slot").value : null } }); };
  $("report-unknown").onclick = () => act("unknown");
  for (const button of document.querySelectorAll("[data-sample]")) button.onclick = () => act(button.dataset.sample);
  const controls = { "stream-restart": { action: "restart" }, "stream-upgrade": { action: "fixture", value: "compatible" }, "stream-incompatible": { action: "fixture", value: "incompatible" }, "stream-original": { action: "fixture", value: "baseline" } };
  for (const [id, body] of Object.entries(controls)) $(id).onclick = async () => { try { await api("/api/control", body); error(null); } catch (cause) { error(cause.message); } };
  $("stream-expire").onclick = () => act("expire-events");
  async function list(offset = 0) {
    try {
      const page = await read(`${prefix}/v1/samples?selection=all&limit=10&offset=${offset}${offset ? `&expected_revision=${browseRevision}` : ""}`);
      if (offset && page.revision !== browseRevision) throw new Error("Chip records changed. Select Browse saved chips to start again.");
      browse = page; browseRevision = page.revision; nextOffset = page.next_offset; $("stream-next").hidden = nextOffset == null; render(); error(null);
    } catch (cause) { error(cause.message); }
  }
  $("stream-list").onclick = () => list(); $("stream-next").onclick = () => list(nextOffset);
  let renderTimer = null;
  addEventListener("portal-test-mode", event => {
    clearInterval(renderTimer); renderTimer = null;
    for (const reader of readers) reader.disconnect();
    if (event.detail === "stream-tests") {
      for (const reader of readers) { reader.wanted = true; reader.open(); }
      renderTimer = setInterval(render, 1000);
    }
  });
  addEventListener("pagehide", () => readers.forEach(reader => reader.disconnect()));
})(globalThis);
