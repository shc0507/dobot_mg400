(function (global) {
  "use strict";
  const agreement = typeof module !== "undefined" ? require("./stream-portal.js") : global.AgentStreamPortal;
  const visuals = typeof module !== "undefined" ? require("./portal-visuals.js") : global.AgentPortalVisuals;
  const prefix = "/lab/api/agent";
  const labels = { stage: "Stage", robot: "Robot arm", furnace: "Furnace", thermal: "Thermal camera", microscope: "Microscope", vision: "Vision" };
  const qualityNames = { valid: "Current", stale: "Outdated", unavailable: "Unavailable", uncalibrated: "Not calibrated", unknown: "Unknown" };
  function rows(value, path = []) {
    if (value && typeof value === "object" && Object.keys(value).length) return Object.entries(value).flatMap(([key, item]) => rows(item, [...path, key]));
    return [[path.join("."), value]];
  }
  function display(value) {
    if (value === null || value === undefined) return "Unknown";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    if (typeof value === "number") return Number.isFinite(value) ? String(value) : "Unknown";
    return typeof value === "object" ? JSON.stringify(value) : String(value);
  }
  function readingQuality(device, receivedAt, connected, now = Date.now() / 1000) {
    if (!connected) return "Last received";
    if (device?.values?.connected === false) return "Not connected";
    if (device?.quality !== "valid") return qualityNames[device?.quality] || "Unknown";
    const elapsed = typeof receivedAt === "number" ? Math.max(0, now - receivedAt) : Infinity;
    return typeof device.age_s !== "number" || device.age_s + elapsed > 3 ? "Outdated" : "Current";
  }
  class LiveReader {
    constructor({ read, source, changed = () => {}, digest = data => crypto.subtle.digest("SHA-256", data) }) {
      this.read = read; this.source = source; this.changed = changed; this.digest = digest;
      this.cache = new Map(); this.generation = 0; this.wanted = false; this.connected = false;
      this.rate = 2; this.status = "Not connected"; this.error = null; this.channel = null;
      this.queue = []; this.processing = false; this.events = []; this.cursor = null;
      this.state = null; this.rawState = null; this.raw = null; this.discovery = null;
      this.messages = 0; this.frames = 0; this.bytes = 0; this.lastAt = null; this.receivedAt = null; this.timer = null;
    }
    async definition(id, path) {
      if (!/^sha256:[a-f0-9]{64}$/.test(id || "")) throw new Error("The data agreement ID is invalid.");
      const expected = `${prefix}/contracts/${id.slice(7)}.json`;
      if (path && path !== expected) throw new Error("The data agreement path is invalid.");
      if (this.cache.has(id)) return this.cache.get(id);
      const bundle = await this.read(expected);
      const hash = await this.digest(new TextEncoder().encode(agreement.canonical(bundle)));
      const actual = "sha256:" + Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, "0")).join("");
      if (actual !== id) throw new Error("The data agreement failed its integrity check.");
      this.cache.set(id, bundle);
      while (this.cache.size > 8) this.cache.delete(this.cache.keys().next().value);
      return bundle;
    }
    async discover(generation) {
      const first = await this.read(`${prefix}/discovery`);
      if (first.mode !== "live" || first.discovery_version !== 1 || first.read_only !== true) throw new Error("This portal needs the live, read-only Agent Connect interface.");
      const bundle = await this.definition(first.contract_id, first.contract_url);
      const second = await this.read(`${prefix}/discovery`);
      if (second.mode !== "live" || first.boot_id !== second.boot_id || first.contract_id !== second.contract_id) throw new Error("The Console changed during connection. Checking again.");
      if (generation !== this.generation) return;
      this.discovery = first; this.supported = agreement.compatible(bundle);
      if (!this.supported) { this.state = null; this.error = "The Console uses a new data format. Update the reader. Raw messages remain available."; }
    }
    start() {
      this.stop(); this.wanted = true; this.error = null; this.status = "Connecting to Olares…";
      this.open(this.generation); this.changed();
    }
    stop() {
      this.generation++; this.wanted = false; this.connected = false; clearTimeout(this.timer);
      if (this.channel) this.channel.close(); this.channel = null; this.queue = [];
      this.status = "Disconnected · Last received data"; this.changed();
    }
    async open(generation) {
      try {
        await this.discover(generation);
        if (!this.wanted || generation !== this.generation) return;
        const query = new URLSearchParams({ rate_hz: String(this.rate) });
        if (this.cursor) { query.set("boot_id", this.cursor.boot_id); query.set("event_seq", String(this.cursor.event_seq)); }
        const channel = this.source(`/api/live/events?${query}`); this.channel = channel;
        channel.onmessage = event => {
          if (generation !== this.generation || !this.wanted) return;
          try {
            if (typeof event.data !== "string" || event.data.length > 262144) throw new Error("The stream message exceeds the supported size.");
            const message = JSON.parse(event.data);
            this.messages++; this.bytes += new TextEncoder().encode(event.data).length;
            this.lastAt = Date.now(); this.raw = message;
            if (message.type === "state") this.rawState = message;
            if (this.queue.length >= 256) throw new Error("Messages arrived faster than this reader could process. Reconnecting from the saved event.");
            this.queue.push({ message, generation, receivedAt: Date.now() / 1000 }); this.drain();
          } catch (error) { this.retry(generation, error.message); }
        };
        channel.addEventListener("connection-error", event => {
          let detail = "The Olares stream is unavailable.";
          try { detail = JSON.parse(event.data).error || detail; } catch {}
          this.retry(generation, detail);
        });
        channel.onerror = () => this.retry(generation, "Connection lost. Check LarePass and the Olares test site.");
      } catch (error) { this.retry(generation, error.message); }
    }
    retry(generation, message) {
      if (generation !== this.generation || !this.wanted) return;
      this.generation++; this.connected = false; this.error = message; this.status = "Reconnecting…";
      if (this.channel) this.channel.close(); this.channel = null; this.queue = [];
      clearTimeout(this.timer); const next = this.generation;
      this.timer = setTimeout(() => this.open(next), 2000); this.changed();
    }
    async consume(message, generation, receivedAt = Date.now() / 1000) {
      if (!message || !["state", "event", "resync"].includes(message.type)) throw new Error("Unknown stream message type.");
      if (typeof message.boot_id !== "string" || !Number.isSafeInteger(message.event_seq) || message.event_seq < 0) throw new Error("The message is missing its process or event number.");
      if (message.type === "event") {
        const definition = await this.definition(message.contract_id);
        if (generation !== this.generation) return;
        if (this.cursor?.boot_id === message.boot_id && message.event_seq <= this.cursor.event_seq) return;
        this.events.unshift({ message, supported: agreement.compatible(definition), received_at: Date.now() });
        this.events.length = Math.min(this.events.length, 100);
        this.cursor = { boot_id: message.boot_id, event_seq: message.event_seq };
        if (message.kind === "contract.changed") await this.discover(generation);
        return;
      }
      if (message.boot_id !== this.discovery?.boot_id || message.contract_id !== this.discovery?.contract_id) await this.discover(generation);
      if (generation !== this.generation) return;
      if (message.type === "resync") {
        this.cursor = { boot_id: message.boot_id, event_seq: message.event_seq };
        this.state = null;
        this.events.unshift({ message, supported: true, received_at: Date.now() });
        this.events.length = Math.min(this.events.length, 100);
        return;
      }
      if (!this.supported || message.boot_id !== this.discovery?.boot_id || message.contract_id !== this.discovery?.contract_id) { this.state = null; return; }
      if (message.mode !== "live" || !Number.isSafeInteger(message.seq) || !Number.isFinite(message.generated_at) || !message.devices || !message.samples || !message.operations) throw new Error("The state frame does not match the supported data agreement.");
      if (this.state?.boot_id === message.boot_id && message.seq <= this.state.seq) return;
      this.state = message; this.receivedAt = receivedAt; this.frames++; this.connected = true; this.error = null; this.status = "Receiving live data";
      if (!this.cursor) this.cursor = { boot_id: message.boot_id, event_seq: message.event_seq };
    }
    async drain() {
      if (this.processing) return; this.processing = true;
      try {
        while (this.wanted && this.queue.length) {
          const { message, generation, receivedAt } = this.queue.shift();
          if (generation !== this.generation) continue;
          try { await this.consume(message, generation, receivedAt); }
          catch (error) { this.state = null; this.retry(generation, error.message); }
        }
      } finally { this.processing = false; this.changed(); }
    }
  }
  const logic = { LiveReader, rows, display, readingQuality };
  if (typeof module !== "undefined") module.exports = logic;
  global.AgentLivePortal = logic;
  if (typeof document === "undefined" || !document.getElementById("live-view")) return;
  const $ = id => document.getElementById(id);
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node; };
  async function read(path) {
    const endpoint = path === `${prefix}/discovery` ? "/api/live/discovery" : `/api/live/read?path=${encodeURIComponent(path)}`;
    const response = await fetch(endpoint, { cache: "no-store" }); const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Olares returned HTTP ${response.status}.`);
    return data;
  }
  const cards = new Map(); let renderedState = null, eventKey = "";
  function table(value) {
    const body = el("tbody");
    for (const [path, data] of rows(value)) {
      const row = el("tr"), key = el("th", path || "Value"), cell = el("td", display(data));
      key.scope = "row"; row.append(key, cell); body.append(row);
    }
    const result = el("table", null, "live-fields"); result.append(body); return result;
  }
  function deviceCard(name) {
    if (cards.has(name)) return cards.get(name);
    const root = el("article", null, "card live-device"), header = el("div", null, "card-heading");
    root.dataset.device = name;
    const title = el("h3", labels[name] || name), badge = el("span", "", "status-pill"); header.append(title, badge);
    const age = el("p", "", "section-note"), values = el("div", null, "live-device-values");
    values.tabIndex = 0; values.setAttribute("aria-label", `${labels[name] || name} received fields`);
    const details = el("details", null, "live-device-details"), metadata = el("div");
    details.append(el("summary", "Inspect all received fields and units"), metadata);
    root.append(header, age, values, details); $("live-devices").append(root);
    const result = { root, badge, age, values, metadata }; cards.set(name, result); return result;
  }
  function render() {
    const frame = reader.state, hasFrame = Boolean(frame), elapsed = reader.lastAt ? Math.max(0, (Date.now() - reader.lastAt) / 1000) : null;
    $("live-status").textContent = !reader.supported && reader.discovery ? "Reader update needed" : reader.status;
    $("live-status").classList.toggle("warning", !reader.connected || (elapsed !== null && elapsed > 3));
    $("live-metrics").textContent = `${reader.frames} frames · ${reader.messages} messages · ${(reader.bytes / 1024).toFixed(1)} KiB${elapsed === null ? "" : ` · Last message ${elapsed.toFixed(1)} s ago`}`;
    $("live-connect").textContent = reader.wanted ? "Disconnect" : "Reconnect";
    $("live-error").hidden = !reader.error; $("live-error").textContent = reader.error || "";
    $("live-empty").hidden = hasFrame; $("live-devices").hidden = false;
    if (!hasFrame) $("live-empty").textContent = reader.error ? "Readings are unavailable. The last raw message remains below." : "Connect LarePass to receive data from Olares.";
    if (frame !== renderedState) {
      renderedState = frame;
      for (const [name, device] of Object.entries(frame?.devices || {})) {
        const card = deviceCard(name); card.metadata.replaceChildren(table(device));
      }
      for (const [group, noun] of [["samples", "sample"], ["operations", "operation"]]) {
        const data = frame?.[group]; const items = data?.items;
        $("live-" + noun + "-count").textContent = data ? `${data.count ?? items?.length ?? 0}${data.total == null ? "" : ` / ${data.total}`}` : "Not received";
        $("live-" + group).replaceChildren(data ? table(data) : el("p", "No data received.", "empty-state"));
      }
    }
    const names = [...new Set([...Object.keys(labels), ...Object.keys(frame?.devices || {})])];
    for (const name of names) {
      const device = frame?.devices?.[name], card = deviceCard(name);
      const quality = device ? readingQuality(device, reader.receivedAt, reader.connected) : "Not received";
      card.badge.textContent = ["thermal", "vision"].includes(name) && quality === "Current" ? "Service current" : quality;
      card.badge.classList.toggle("warning", quality !== "Current");
      const ageElapsed = reader.receivedAt == null ? Infinity : Math.max(0, Date.now() / 1000 - reader.receivedAt);
      const age = typeof device?.age_s === "number" ? device.age_s + ageElapsed : null;
      card.age.textContent = age === null ? "Source update time unknown" : `Console cache updated ${age.toFixed(1)} s ago`;
      card.values.replaceChildren(visuals.deviceVisual(name, device, reader.connected ? ageElapsed : Infinity));
      if (!device) card.metadata.replaceChildren(el("p", "No device data received.", "section-note"));
    }
    const key = JSON.stringify(reader.events.map(row => [row.message.boot_id, row.message.event_seq, row.message.type]));
    if (eventKey !== key) {
      eventKey = key;
      $("live-events").replaceChildren(...reader.events.map(row => {
        const details = el("details", null, "live-event"), message = row.message;
        const name = message.type === "resync" ? `New snapshot required: ${message.reason}` : message.kind;
        details.append(el("summary", `${new Date(row.received_at).toLocaleTimeString()} · ${name}${row.supported ? "" : " · Raw data only"}`), el("pre", JSON.stringify(message, null, 2)));
        return details;
      }));
      if (!reader.events.length) $("live-events").append(el("p", "No events received yet.", "empty-state"));
    }
    $("live-raw").textContent = reader.rawState ? JSON.stringify(reader.rawState, null, 2) : "Waiting for a state frame.";
    $("live-last-message").textContent = reader.raw ? JSON.stringify(reader.raw, null, 2) : "Waiting for a message.";
    $("live-download").disabled = !reader.rawState;
    $("live-agreement").replaceChildren(...[["Read-stream process", reader.discovery?.boot_id], ["Data agreement", reader.discovery?.contract_id], ["Console release", reader.discovery?.release?.release_id || reader.discovery?.server_release]].flatMap(([key, value]) => [el("dt", key), el("dd", value || "Not received")]));
  }
  const reader = new LiveReader({ read, source: url => new EventSource(url), changed: render });
  $("live-connect").onclick = () => reader.wanted ? reader.stop() : reader.start();
  $("live-rate").onchange = () => { reader.rate = Number($("live-rate").value); if (reader.wanted) reader.start(); };
  $("live-download").onclick = () => {
    const blob = new Blob([JSON.stringify({ discovery: reader.discovery, state: reader.rawState, events: reader.events.map(row => row.message), last_message: reader.raw }, null, 2)], { type: "application/json" });
    const link = el("a"); link.href = URL.createObjectURL(blob); link.download = "olares-received-data.json"; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  };
  $("enable-tests").onchange = () => {
    const enabled = $("enable-tests").checked; $("advanced-tests").hidden = !enabled;
    document.body.dataset.mode = enabled ? "MOCK" : "live";
    $("portal-mode").textContent = enabled ? "Simulation · No hardware" : "Live equipment";
    dispatchEvent(new CustomEvent("portal-test-mode", { detail: enabled ? document.querySelector('[data-panel][aria-pressed="true"]').dataset.panel : null }));
    if (enabled) reader.stop(); else reader.start();
  };
  reader.start();
  setInterval(() => { if (!$("live-view").hidden) render(); }, 1000);
  addEventListener("pagehide", () => reader.stop());
})(globalThis);
