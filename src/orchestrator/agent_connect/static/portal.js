(function (global) {
  "use strict";
  function stateView(client, now = Date.now() / 1000) {
    const frame = client?.state;
    const compatible = ["compatible", "supported subset"].includes(client?.compatibility?.status);
    const age = frame ? Math.max(0, Number(frame.age_s || 0) + now - Number(client.received_at || now)) : null;
    const usable = Boolean(frame && compatible && !client.error && ["good", "stale"].includes(frame.quality));
    const stale = !client?.connected || age === null || age > 3 || frame?.quality !== "good";
    const readings = Object.fromEntries(["stage", "temperature"].map(name => [name, {
      usable: usable && ["good", "stale"].includes(frame?.[name]?.quality),
      stale: stale || frame?.[name]?.quality !== "good",
    }]));
    return { frame, age, usable, stale, readings };
  }
  function number(value, unit) { return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(3)} ${unit}` : "Unknown"; }
  const logic = { stateView, number };
  if (typeof module !== "undefined") module.exports = logic;
  global.AgentPortal = logic;
  if (typeof document === "undefined" || !document.getElementById("clients")) return;
  const $ = id => document.getElementById(id);
  let latest = null, busy = false, polling = false, enabled = false, pollTimer = null;
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
  async function request(path, data) {
    const response = await fetch(path, { method: data === undefined ? "GET" : "POST", headers: { "Content-Type": "application/json" }, body: data === undefined ? undefined : JSON.stringify(data), cache: "no-store" });
    const body = await response.json(); if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`); return body;
  }
  const names = { A: "Earlier agent version", B: "Newer agent version" };
  const setups = {
    baseline: ["Original format (1.0)", "Both versions can read the original fields. The newer agent can work without its optional extra field."],
    compatible: ["Add optional field (1.1)", "Adds a source description. Existing fields and units stay the same, so both versions can continue."],
    incompatible: ["Change position units (2.0)", "Changes rotations to millimetres. Both versions should request an update instead of guessing a conversion."],
    "unsupported-dialect": ["Unsupported agreement format", "Uses a format these test agents cannot understand. They should stop interpreting the readings."],
    "broken-digest": ["Damaged agreement file", "Simulates a file that fails its integrity check. A client that downloads it should reject it. Run automatic checks to test this with a new client."],
  };
  const actions = {
    pause: "Reading updates paused", resume: "Reading updates resumed", stale: "Old readings simulated",
    "reference-reset": "Coordinate version increased", restart: "Console restart simulated",
    reset: "Original demo values restored", fixture: "Test setup changed",
  };
  const checks = {
    "Old client / baseline": "Earlier agent reads the original format",
    "New client / older release": "Newer agent works with the original fields",
    "Source values change": "Simulated readings update",
    "Paused sample becomes stale": "Paused readings become outdated",
    "Polling preserves source time": "Checking again does not make an old reading look new",
    "Old client / optional field": "Earlier agent accepts an optional field",
    "New client / new release": "Newer agent accepts the updated format",
    "Changed units need a client update": "Changed units require an agent update",
    "Unknown dialect is blocked": "An unsupported agreement format is rejected",
    "Broken digest is blocked": "A damaged agreement file is rejected",
    "Disconnected client retains its last frame": "A disconnected agent keeps its last message",
    "Reconnect discovers restart": "Reconnecting detects a Console restart",
    "Reference reset changes revision": "A coordinate change updates the version",
    "Reset restores starting values": "Reset restores the original demo",
  };
  const statusText = value => ({
    compatible: "Ready to read", "supported subset": "Ready - original fields available",
    "client update required": "Agent update needed", blocked: "Data check failed", connecting: "Connecting...",
  }[value] || "Waiting for data");
  function explainProblem(value) {
    if (/digest mismatch/i.test(value)) return "The agreement file failed its integrity check. The agent did not accept it.";
    if (/Unsupported schema dialect/i.test(value)) return "This agent cannot understand the selected agreement format.";
    if (/Unsupported major contract version/i.test(value)) return "This data agreement needs a newer agent version.";
    if (/stage.position.*unit|stage.position must use rotation/i.test(value)) return "Stage units changed. This agent expects rotations and will not guess a conversion.";
    return String(value).replaceAll("mock Console", "simulated Console").replaceAll("Mock Console", "Simulated Console").replaceAll("contract", "data agreement").replaceAll("Contract", "Data agreement");
  }
  let clientKey = "", resultKey = "";
  function error(message) { $("error").hidden = !message; $("error").textContent = message || ""; }
  function setupHelp() { $("fixture-help").textContent = setups[$("fixture").value]?.[1] || "Choose a test setup."; }
  async function control(data) {
    if (busy) return; busy = true;
    try {
      await request("/api/control", data);
      if (data.action === "reset") { $("fixture").value = "baseline"; setupHelp(); }
      error(null); await poll();
    } catch (e) { error(explainProblem(e.message)); }
    finally { busy = false; }
  }
  function clientCard(client) {
    const card = el("article", null, "client");
    const head = el("h2", names[client.name] || client.name);
    const button = el("button", client.connected ? "Disconnect" : "Reconnect");
    button.setAttribute("aria-label", `${client.connected ? "Disconnect" : "Reconnect"} ${names[client.name].toLowerCase()}`);
    button.addEventListener("click", () => control({ action: client.connected ? "disconnect" : "connect", client: client.name }));
    head.append(button); card.append(head);
    card.append(el("p", client.generation === 1 ? "Test script v1: understands the original data fields." : "Test script v2: also supports the optional source description.", "muted"));
    const status = client.connected ? statusText(client.compatibility.status) : "Disconnected - showing last received data";
    card.append(el("p", status, "status" + (client.error || client.compatibility.problems.length ? " bad" : "")));
    card.append(el("p", client.connected ? "Disconnect to test what happens when this agent stops receiving updates." : "Reconnect to check the current Console version and receive updates.", "connection-help"));
    const problems = [...(client.compatibility.problems || []), ...(client.error ? [client.error] : [])];
    for (const problem of [...new Set(problems.map(explainProblem))]) card.append(el("p", problem, "status bad"));
    return card;
  }
  function reading(title, value, detail, stale) {
    const card = el("article", null, "reading" + (stale ? " stale" : ""));
    card.append(el("span", title), el("strong", value), el("small", detail)); return card;
  }
  function render() {
    if (!latest) return;
    const sourceSetup = latest.clients.find(c => c.connected && c.discovery)?.discovery.server_release?.replace(/^mock-/, "");
    if (!busy && document.activeElement !== $("fixture") && setups[sourceSetup]) {
      $("fixture").value = sourceSetup; setupHelp();
    }
    const nextClientKey = JSON.stringify(latest.clients.map(c => [c.name, c.connected, c.compatibility, c.error]));
    if (nextClientKey !== clientKey) { $("clients").replaceChildren(...latest.clients.map(clientCard)); clientKey = nextClientKey; }
    const client = latest.clients.find(row => row.name === $("selected").value);
    const view = stateView(client), frame = view.frame;
    const health = !view.usable ? "Readings unavailable for this data agreement" : !client.connected ? "Disconnected - last received values" : view.stale ? "Outdated reading - last known values" : "Up to date";
    const readingHealth = row => !row.usable ? "No usable reading received." : !client.connected ? "Last received value. Reconnect for updates." : row.stale ? "Outdated or flagged as old. Do not treat this as the current value." : "Current simulated reading.";
    $("frame-meta").textContent = frame ? `${health}. Measured ${view.age.toFixed(1)} seconds ago. Readings older than 3 seconds are outdated.` : "No message received yet.";
    const observer = latest.clients.find(c => c.connected && c.state && !c.error);
    $("source-status").textContent = !observer ? "Waiting for an agent" : observer.state.paused ? "Updates paused" : "Updating readings";
    $("readings").replaceChildren(
      reading("Stage position", view.readings.stage.usable ? number(frame.stage.position, frame.stage.unit === "rotation" ? "rotations" : frame.stage.unit) : "Unavailable", readingHealth(view.readings.stage), view.readings.stage.stale),
      reading("Temperature", view.readings.temperature.usable ? number(frame.temperature.value, "\u00b0C") : "Unavailable", readingHealth(view.readings.temperature), view.readings.temperature.stale),
      reading("Coordinate version", view.usable ? String(frame.stage.reference_revision) : "Unavailable", "Increases when the coordinate reference changes or the simulated Console restarts.", view.stale));
    const samples = view.usable ? frame.samples || [] : [];
    $("samples").replaceChildren(...samples.map(sample => {
      const row = el("tr");
      const basis = { "mock observation": "Simulated observation", "mock placement": "Simulated placement" }[sample.basis] || sample.basis;
      for (const text of [sample.chip_id, sample.holder, basis, sample.pending_action || "None", `${sample.last_run} / ${sample.last_step}`]) row.append(el("td", text));
      return row;
    }));
    $("samples-empty").hidden = samples.length > 0;
    const release = client.discovery?.server_release?.replace(/^mock-/, "");
    const fields = [["Console test setup", setups[release]?.[0]], ["Current agreement ID", client.discovery?.contract_id], ["Agent's saved agreement ID", client.cached_contract_id], ["Console process ID (changes after a restart)", client.discovery?.boot_id], ["Can this agent read the format?", statusText(client.compatibility.status)]];
    $("contract").replaceChildren(...fields.flatMap(([key, value]) => [el("dt", key), el("dd", value || "Not received yet")]));
    const changes = client.changes.slice(0, 16).map(row => el("div", `${row.path}: ${JSON.stringify(row.before)} -> ${JSON.stringify(row.after)}`, "change"));
    $("changes").replaceChildren(...(changes.length ? changes : [el("p", "No changes from the previous saved agreement.", "muted")]));
    $("raw").textContent = JSON.stringify(client.raw_state || frame, null, 2);
    const history = (frame?.events || []).slice().reverse().map(row => el("div", `${new Date(row.at * 1000).toLocaleTimeString()}: ${actions[row.kind] || row.message}`, "event"));
    $("history").replaceChildren(...(history.length ? history : [el("p", "No test activity received.", "muted")]));
    const results = latest.results?.results;
    if (results && JSON.stringify(results) !== resultKey) {
      resultKey = JSON.stringify(results);
      $("results-summary").textContent = `${results.filter(row => row.passed).length} of ${results.length} checks passed.`;
      $("results").replaceChildren(...results.map(row => {
        const div = el("div", null, "result" + (row.passed ? "" : " failed"));
        div.append(el("strong", row.passed ? "Passed" : "Needs attention"), el("span", checks[row.name] || row.name));
        const detail = el("details"); detail.append(el("summary", "Technical result: expected / received"), el("pre", JSON.stringify({ expected: row.expected, actual: row.actual }, null, 2))); div.append(detail); return div;
      }));
    }
  }
  async function poll() { if (!enabled || polling) return; polling = true; try { latest = await request("/api/status"); render(); error(null); } catch (e) { error(`The simulated Console is unavailable: ${explainProblem(e.message)}`); } finally { polling = false; } }
  for (const button of document.querySelectorAll("[data-action]")) button.addEventListener("click", () => control({ action: button.dataset.action }));
  $("fixture").addEventListener("change", () => { setupHelp(); control({ action: "fixture", value: $("fixture").value }); });
  $("selected").addEventListener("change", () => { $("bundle-json").hidden = true; render(); });
  $("bundle").addEventListener("click", async () => { try { $("bundle-json").textContent = JSON.stringify(await request(`/api/client/${$("selected").value}/bundle`), null, 2); $("bundle-json").hidden = false; } catch (e) { error(explainProblem(e.message)); } });
  $("run").addEventListener("click", async () => { $("run").disabled = true; try { await request("/api/scenarios", {}); await poll(); $("check-details").open = true; } catch (e) { error(explainProblem(e.message)); } finally { $("run").disabled = false; } });
  setupHelp();
  addEventListener("portal-test-mode", event => {
    enabled = event.detail === "format-tests";
    clearInterval(pollTimer); pollTimer = null;
    if (enabled) { poll(); pollTimer = setInterval(poll, 1000); }
  });
})(globalThis);
