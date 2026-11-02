(() => {
  "use strict";

  const $ = id => document.getElementById(id);
  const base = document.body.dataset.basePath || "";
  const validBase = base === "" || (base.startsWith("/") && !/[\\?#\u0000-\u0020]/.test(base) && !base.includes("//") && !base.endsWith("/") && !base.split("/").some(part => part === "." || part === ".."));
  const actions = { motion: "Stop all motion", heat: "Heat off", gas: "Gas off" };
  const cameraNames = { camera: "Camera", thermal: "Thermal camera", microscope: "Microscope" };
  let token = "", generation = 0, snapshot = null, stateAt = -Infinity;
  let pollTimer = null, renewTimer = null, polling = false, refreshAgain = false;
  let active = true, leaseIntent = false, leaseDeadline = 0, leaseEpoch = 0;
  let leaseBusy = false, runBusy = false, renewBusy = false, resumeRunId = null;
  const safetyBusy = new Set();
  const proposalPresence = new Map(), proposalBusy = new Set(), proposalConsumed = new Set();
  let client = "";
  try {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    client = "dashboard-" + Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
  } catch { /* A client identity is required before any POST. */ }

  const now = () => performance.now();
  const clean = value => {
    const text = String(value ?? "");
    return token ? text.split(token).join("[redacted]") : text;
  };
  function node(tag, text, className = "") {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = clean(text);
    if (className) element.className = className;
    return element;
  }
  function say(text, error = false) {
    $("notice").textContent = clean(text);
    $("notice").className = "notice" + (error ? " error" : "");
  }
  function connected(text, kind = "neutral") {
    $("connection").textContent = text;
    $("connection").className = "badge " + kind;
  }
  const current = () => snapshot !== null && now() - stateAt < 2500;
  const runCurrent = () => current() && snapshot.orchestrator?.stale === false && snapshot.orchestrator?.connected === true;
  const ownsControl = () => active && leaseIntent && leaseDeadline > now() && runCurrent();

  class RequestError extends Error {
    constructor(message, status = 0) { super(message); this.status = status; }
  }
  async function request(path, data, requestGeneration = generation) {
    if (!validBase || !path.startsWith("/api/")) throw new RequestError("Invalid dashboard path.");
    if (data !== undefined && !client) throw new RequestError("A secure client identity is unavailable. Reload in a supported browser.");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 4500);
    const headers = { Accept: "application/json" };
    if (token) headers.Authorization = "Bearer " + token;
    const options = { headers, signal: controller.signal, redirect: "error", credentials: "omit", cache: "no-store" };
    if (data !== undefined) {
      options.method = "POST";
      headers["Content-Type"] = "application/json";
      headers["X-MG400-Client"] = client;
      options.body = JSON.stringify(data);
    }
    try {
      const response = await fetch(base + path, options);
      let body;
      try { body = await response.json(); } catch { throw new RequestError("The service returned an unreadable response.", response.status); }
      if (!body || typeof body !== "object" || Array.isArray(body)) throw new RequestError("The service returned an invalid response.", response.status);
      if (!response.ok) throw new RequestError(typeof body.error === "string" ? body.error : "The service refused the request.", response.status);
      if (requestGeneration !== generation || !active) throw new RequestError("The page access settings changed.");
      return body;
    } finally { clearTimeout(timeout); }
  }

  function loseControl(message) {
    leaseIntent = false;
    leaseDeadline = 0;
    leaseEpoch++;
    clearTimeout(renewTimer);
    renewTimer = null;
    if (message) say(message, true);
    controls();
  }
  function acceptLease(lease) {
    if (!lease || lease.holder !== client || !Number.isFinite(lease.remaining_s) || lease.remaining_s <= 0) {
      throw new RequestError("The service did not confirm this page's control lease.");
    }
    leaseDeadline = now() + lease.remaining_s * 1000;
  }
  function scheduleRenew() {
    clearTimeout(renewTimer);
    renewTimer = leaseIntent && active ? setTimeout(renew, 500) : null;
  }
  async function renew() {
    if (!leaseIntent || !active || renewBusy) return;
    const savedGeneration = generation, savedEpoch = leaseEpoch;
    renewBusy = true;
    try {
      const result = await request("/api/renew", {}, savedGeneration);
      if (savedGeneration !== generation || savedEpoch !== leaseEpoch) return;
      acceptLease(result.lease);
    } catch (error) {
      if (savedGeneration === generation && savedEpoch === leaseEpoch) {
        loseControl("Control renewal failed. The run may be held. Take control explicitly after you check the status. " + error.message);
      }
    } finally {
      renewBusy = false;
      controls();
      if (savedGeneration === generation && savedEpoch === leaseEpoch) scheduleRenew();
    }
  }

  function controls() {
    const owned = ownsControl();
    const run = snapshot?.run;
    const live = runCurrent() && run && run.closed === false;
    $("acquire-control").disabled = !client || leaseBusy || leaseIntent || !runCurrent();
    $("release-control").disabled = !leaseIntent || leaseBusy;
    $("hold-run").disabled = !owned || !live || runBusy;
    $("abort-run").disabled = !owned || !live || runBusy;
    $("resume-form").hidden = !runCurrent() || !run || run.status !== "held" || run.closed !== false;
    $("resume-run").disabled = !owned || !live || run.status !== "held" || !["pending", "started"].includes(run.resume_kind) || run.settled !== true || run.draining !== false || runBusy || !$("manual-completion").checked || !$("resume-note").value.trim();
    if (owned) $("lease-status").textContent = "This page holds control.";
    else if (leaseIntent) $("lease-status").textContent = "Control is not confirmed. Waiting for fresh status.";
    else {
      const holder = snapshot?.orchestrator?.lease?.holder;
      $("lease-status").textContent = holder && runCurrent() ? "Control holder: " + clean(holder) : "Control not held by this page.";
    }
    $("stop-motion").disabled = !client || safetyBusy.has("motion");
    for (const action of ["heat", "gas"]) {
      $("show-" + action).disabled = !client || safetyBusy.has(action);
      $("confirm-" + action).disabled = !client || safetyBusy.has(action);
    }
    renderApprovals();
  }

  function renderApprovals() {
    const rows = Array.isArray(snapshot?.approvals) ? snapshot.approvals : [];
    const cards = rows.map(row => {
      const card = node("article", undefined, "panel approval-card");
      card.append(node("h3", row.recipe), node("p", `Requested by ${row.requester}: ${row.reason}`));
      card.append(node("p", `Decision: ${row.decision}${row.decider ? " · " + row.decider : ""}`));
      card.append(node("p", `Expires: ${new Date(row.expires_at * 1000).toISOString()}`, "muted"));
      card.append(node("pre", JSON.stringify({ definition: row.definition, params: row.params, effective: row.effective }, null, 2), "approval-definition"));
      if (row.decision === "pending") {
        const key = row.id + ":" + row.fingerprint;
        const label = node("label", undefined, "check"), presence = node("input");
        presence.type = "checkbox";
        presence.checked = proposalPresence.get(key) === true;
        presence.addEventListener("change", () => { proposalPresence.set(key, presence.checked); controls(); });
        label.append(presence, node("span", "I am in the lab, can reach a stop control, and will stay during the run."));
        const actions = node("div", undefined, "actions");
        for (const action of ["approve", "reject"]) {
          const button = node("button", action === "approve" ? "Approve and run" : "Reject", action === "reject" ? "secondary" : "");
          button.type = "button";
          button.disabled = !client || !current() || proposalBusy.has(row.id) || proposalConsumed.has(row.id) || (action === "approve" && (!ownsControl() || !presence.checked || runBusy));
          button.addEventListener("click", () => decideProposal(row, action, key));
          actions.append(button);
        }
        card.append(label, actions);
      }
      if (row.command_id) card.append(node("p", "Run command: " + row.command_id));
      if (row.dispatch === "not-confirmed") card.append(node("p", "Dispatch was not confirmed. Check the current run. This proposal cannot run again.", "service-error"));
      return card;
    });
    $("approvals").replaceChildren(...(cards.length ? cards : [node("p", "No proposals received.", "empty")]));
    const retained = new Set(rows.filter(row => row.decision === "pending").map(row => row.id + ":" + row.fingerprint));
    for (const key of proposalPresence.keys()) if (!retained.has(key)) proposalPresence.delete(key);
  }

  async function decideProposal(row, decision, key) {
    if (!current() || proposalBusy.has(row.id) || proposalConsumed.has(row.id)) return;
    if (decision === "approve" && (!ownsControl() || proposalPresence.get(key) !== true || runBusy)) return;
    const savedGeneration = generation;
    proposalBusy.add(row.id);
    proposalConsumed.add(row.id);
    controls();
    try {
      await request("/api/approvals/" + encodeURIComponent(row.id) + "/decision", { decision, present: proposalPresence.get(key) === true, fingerprint: row.fingerprint }, savedGeneration);
      say(decision === "approve" ? "Approval accepted. Read the run status and keep this page's control lease active." : "Proposal rejected.");
      refresh();
    } catch (error) {
      if (savedGeneration === generation) say("Decision outcome is not confirmed. Check the proposal and current run. No automatic retry. " + error.message, true);
    } finally { proposalBusy.delete(row.id); controls(); }
  }

  function renderServices() {
    const services = Array.isArray(snapshot?.services) ? snapshot.services : [];
    const cards = services.map(service => {
      const phase = current() ? service.phase : "stale";
      const fresh = phase === "ready";
      const card = node("article", undefined, "panel service-card");
      const title = node("div", undefined, "card-title");
      title.append(node("h3", service.label || service.id));
      const phaseNames = { ready: "Current", loading: "Loading", unavailable: "Unavailable", stale: "Stale", unconfigured: "Not configured" };
      const kind = !fresh ? "neutral" : service.locked === true || service.fault != null || service.connected === false ? "bad" : "neutral";
      title.append(node("span", phaseNames[phase] || "Unknown", "badge " + kind));
      card.append(title);
      const values = node("dl");
      const bool = (value, yes, no) => !fresh || typeof value !== "boolean" ? "Unknown" : value ? yes : no;
      const fields = [
        ["Connection", bool(service.connected, "Connected", "Disconnected")],
        ["Activity", bool(service.busy, "Busy", "Idle")],
        ["Lock", bool(service.locked, "Locked", "Unlocked")],
        ["Fault", fresh ? service.fault === null ? "None reported" : service.fault || "Unknown" : "Unknown"],
        ["Lease holder", fresh ? service.lease_holder === null ? "None" : service.lease_holder || "Unknown" : "Unknown"],
        ["Mode", bool(service.demo, "Demo", "Live")],
      ];
      for (const [label, value] of fields) values.append(node("dt", label), node("dd", value));
      card.append(values);
      if (service.error) card.append(node("p", service.error, "service-error"));
      return card;
    });
    $("services").replaceChildren(...(cards.length ? cards : [node("p", "No service status received.", "empty")]));
    $("service-summary").textContent = current() ? `${cards.length} services` : "Status is not current";
  }

  function cameraURL(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    try {
      const url = new URL(value, location.href);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
      return url.href;
    } catch { return null; }
  }
  function renderCameras() {
    const cameras = Array.isArray(snapshot?.cameras) ? snapshot.cameras : [];
    $("cameras").replaceChildren(...Object.entries(cameraNames).map(([id, label]) => {
      const source = cameras.find(camera => camera.id === id);
      const card = node("article", undefined, "panel camera-card");
      card.append(node("h3", label));
      const url = cameraURL(source?.url);
      if (url) {
        const link = node("a", "Open page");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.setAttribute("aria-label", "Open " + label.toLowerCase() + " page");
        card.append(link);
      } else card.append(node("span", "Link unavailable", "muted"));
      return card;
    }));
  }

  function renderRun() {
    const run = snapshot?.run;
    const resumeKey = run ? JSON.stringify([run.id, run.step_position, run.resume_kind, run.status, runCurrent()]) : null;
    if (resumeRunId !== resumeKey) {
      resumeRunId = resumeKey;
      $("manual-completion").checked = false;
      $("physical-clearance").checked = false;
      $("resume-note").value = "";
    }
    $("run-name").textContent = clean(run ? run.name || run.id : "No current run");
    $("run-status").textContent = runCurrent() ? run ? (run.draining ? "Draining" : run.status || "Unknown") : "None" : "Unknown";
    $("run-detail").textContent = clean(run?.detail || "");
    if (runCurrent() && run?.resume_kind === "pending") {
      $("resume-guidance").textContent = "This step has not run. Do not perform it manually. It will run after resume and the service's safety checks.";
      $("completion-label").textContent = "I checked the held run. I understand that the pending step will run after resume.";
    } else if (runCurrent() && run?.resume_kind === "started") {
      $("resume-guidance").textContent = "This step started. Verify its outcome and complete it manually if necessary. Resume will not repeat it and will advance after safety checks.";
      $("completion-label").textContent = "I verified the outcome and completed this step.";
    } else {
      $("resume-guidance").textContent = "The step's execution status is unknown. Resume is unavailable. Check the run record before any manual action.";
      $("completion-label").textContent = "Review the held step status.";
    }
    const steps = Array.isArray(run?.steps) ? run.steps : [];
    $("run-steps").replaceChildren(...steps.map(step => {
      const item = node("li");
      item.append(node("span", step.skill), node("span", step.status || "Unknown", "badge neutral"));
      if (step.error) item.append(node("p", step.error, "step-error"));
      return item;
    }));
  }

  function renderOperations() {
    const operations = Array.isArray(snapshot?.operations) ? snapshot.operations : [];
    $("operations").replaceChildren(...operations.map(operation => {
      const card = node("article", undefined, "operation");
      card.append(node("h3", actions[operation.action] || operation.action || "Stop request"));
      const final = current() && operation.done === true;
      const phase = { stopping: "Stop requests pending", holding: "Run hold pending", "final-stop": "Final stop requests pending", done: "Requests complete" };
      card.append(node("p", !current() ? "Status stale. Completion is unknown." : final ? "Requests complete. Read each result below." : phase[operation.phase] || "Requests pending"));
      if (operation.hold && operation.hold !== "not-required") card.append(node("p", "Run hold: " + operation.hold + (operation.draining ? ". Accepted work is still draining." : ""), "muted"));
      else if (operation.draining) card.append(node("p", "Accepted work is still draining.", "muted"));
      const results = node("ul");
      for (const [target, result] of Object.entries(operation.results || {})) {
        const known = ["pending", "confirmed", "unconfirmed", "unavailable", "failed"].includes(result?.status) ? result.status : "unknown";
        const outcome = known === "confirmed" && !final ? "confirmed (provisional)" : known;
        results.append(node("li", `${target}: ${outcome}${result?.message ? " — " + result.message : ""}`));
      }
      card.append(results);
      if (operation.action === "gas") card.append(node("p", "These requests target only gas. An active sequence or recipe can apply gas again. Furnace fault handling can stop other outputs.", "muted"));
      $("operations").setAttribute("aria-busy", operations.some(item => item.done !== true) ? "true" : "false");
      return card;
    }));
  }
  function render() { renderServices(); renderCameras(); renderRun(); renderOperations(); controls(); }

  async function refresh() {
    if (!active) return;
    clearTimeout(pollTimer);
    if (polling) { refreshAgain = true; return; }
    polling = true;
    const savedGeneration = generation, savedEpoch = leaseEpoch;
    let permitted = true;
    try {
      const data = await request("/api/dashboard/state", undefined, savedGeneration);
      if (!Array.isArray(data.services) || !data.orchestrator || !Array.isArray(data.operations)) throw new RequestError("The dashboard status is incomplete.");
      snapshot = data;
      stateAt = now();
      if (leaseIntent && savedEpoch === leaseEpoch && !runCurrent()) {
        loseControl("Run status is not current. Control renewal stopped. Check the status before you take control again.");
      }
      // Dashboard summaries are asynchronous and can predate a successful POST.
      // Only heartbeat/renew replies confirm or extend this page's lease.
      if (!runCurrent()) connected("Connected · Run status unavailable", "caution");
      else connected(data.orchestrator.demo === true ? "Connected · Demo" : "Connected");
      render();
    } catch (error) {
      if (savedGeneration === generation && active) {
        stateAt = -Infinity;
        connected("Status unavailable", "caution");
        say("Status unavailable. " + error.message, true);
        if (error.status === 401 || error.status === 403) {
          permitted = false;
          loseControl();
        }
        render();
      }
    } finally {
      polling = false;
      if (active && (permitted || savedGeneration !== generation || refreshAgain)) {
        const delay = refreshAgain || savedGeneration !== generation ? 0 : 1000;
        refreshAgain = false;
        pollTimer = setTimeout(refresh, delay);
      }
    }
  }

  function confirmation(action, visible) {
    $(action + "-confirmation").hidden = !visible;
    $("show-" + action).setAttribute("aria-expanded", String(visible));
    (visible ? $("confirm-" + action) : $("show-" + action)).focus();
  }
  async function safety(action) {
    if (safetyBusy.has(action)) return;
    if (action !== "motion" && $(action + "-confirmation").hidden) return;
    const savedGeneration = generation;
    safetyBusy.add(action);
    controls();
    try {
      const result = await request("/api/dashboard/" + action, action === "motion" ? {} : { confirmed: true }, savedGeneration);
      if (typeof result.operation_id !== "string" || !result.operation_id) throw new RequestError("The service did not return an operation ID.");
      say(actions[action] + " request accepted. Stop outcomes are pending. Read each result and wait for cleanup.");
      if (action !== "motion") confirmation(action, false);
      refresh();
    } catch (error) {
      if (savedGeneration === generation) say(actions[action] + " request outcome is not confirmed. Check the status. " + error.message, true);
    } finally { safetyBusy.delete(action); controls(); }
  }

  async function acquire() {
    if ($("acquire-control").disabled) return;
    const savedGeneration = generation, savedEpoch = ++leaseEpoch;
    leaseBusy = true;
    controls();
    try {
      const result = await request("/api/heartbeat", {}, savedGeneration);
      if (savedEpoch !== leaseEpoch) return;
      acceptLease(result.lease);
      leaseIntent = true;
      say("Control acquired. This page renews only this lease.");
      scheduleRenew();
      refresh();
    } catch (error) {
      if (savedGeneration === generation) loseControl("Control was not confirmed. " + error.message);
    } finally { leaseBusy = false; controls(); }
  }
  async function release() {
    if ($("release-control").disabled) return;
    const savedGeneration = generation;
    leaseBusy = true;
    loseControl();
    try {
      await request("/api/release", {}, savedGeneration);
      say("Control release accepted. Check the run state.");
      refresh();
    } catch (error) {
      if (savedGeneration === generation) say("Control release is not confirmed. This page stopped renewals. " + error.message, true);
    } finally { leaseBusy = false; controls(); }
  }
  async function runAction(action) {
    const button = $(action + "-run");
    controls();
    if (button.disabled || runBusy) return;
    const savedGeneration = generation, runId = snapshot.run.id;
    let path = "/api/runs/" + encodeURIComponent(runId) + "/" + action;
    let data = {};
    if (action === "hold") path = "/api/dashboard/hold";
    if (action === "resume") data = { manually_completed: true, note: $("resume-note").value.trim(), physically_clear: $("physical-clearance").checked };
    runBusy = true;
    controls();
    try {
      const result = await request(path, data, savedGeneration);
      if (typeof result.command_id !== "string" || !result.command_id) throw new RequestError("The service did not return a command ID.");
      say("Run " + action + " request accepted. Wait for the run status.");
      if (action === "resume") {
        $("manual-completion").checked = false;
        $("physical-clearance").checked = false;
        $("resume-note").value = "";
      }
      refresh();
    } catch (error) {
      if (savedGeneration === generation) say("Run " + action + " is not confirmed. The request will not repeat automatically. " + error.message, true);
    } finally { runBusy = false; controls(); }
  }

  function changeAccess(forget = false) {
    generation++;
    loseControl();
    token = forget ? "" : $("access-token").value.trim();
    if (forget) $("access-token").value = "";
    snapshot = null;
    proposalPresence.clear();
    proposalConsumed.clear();
    stateAt = -Infinity;
    for (const action of ["heat", "gas"]) confirmation(action, false);
    say(forget ? "Token cleared. Control renewals stopped." : "Reading status with the supplied access settings.");
    render();
    refresh();
  }
  $("access-form").addEventListener("submit", event => { event.preventDefault(); changeAccess(); });
  $("forget-token").addEventListener("click", () => changeAccess(true));
  $("stop-motion").addEventListener("click", () => safety("motion"));
  for (const action of ["heat", "gas"]) {
    $("show-" + action).addEventListener("click", () => confirmation(action, true));
    $("cancel-" + action).addEventListener("click", () => confirmation(action, false));
    $("confirm-" + action).addEventListener("click", () => safety(action));
  }
  $("acquire-control").addEventListener("click", acquire);
  $("release-control").addEventListener("click", release);
  $("hold-run").addEventListener("click", () => runAction("hold"));
  $("abort-run").addEventListener("click", () => runAction("abort"));
  $("resume-form").addEventListener("submit", event => { event.preventDefault(); runAction("resume"); });
  $("manual-completion").addEventListener("change", controls);
  $("resume-note").addEventListener("input", controls);
  window.addEventListener("pagehide", () => {
    active = false;
    clearTimeout(pollTimer);
    loseControl();
  });
  render();
  if (validBase) refresh();
  else say("Invalid dashboard path. No requests were sent.", true);
})();
