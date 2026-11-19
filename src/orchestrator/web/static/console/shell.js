/* Lab Console shell: the frame, the relay client, control heartbeats and i18n.
 * Workspaces call LabConsole.register({id, i18n, mount(root, api), show(), hide()}).
 * See docs/agents/console-design/CONTRACT.md. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.LabConsole = api;
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";

  const STALE_MS = 3000;
  const BEAT_MS = 500;
  const POLL_MS = 1000;
  const SOCKET_RETRY_MS = 2000;
  const LANG_KEY = "labconsole.lang";
  const SERVICES = ["robot", "stage", "thermal", "vision", "furnace", "microscope"];

  const strings = {
    en: {
      "console.name": "WuLab Console",
      "stop.all": "STOP ALL MOTION",
      "lang.switch": "中文",
      "lang.switchLabel": "Switch language to Chinese",
      "theme.light": "Light mode", "theme.dark": "Dark mode",
      "theme.lightLabel": "Switch to light mode", "theme.darkLabel": "Switch to dark mode",
      "controls.some": "You control: {devices}",
      "service.robot": "Robot", "service.stage": "Stage", "service.cameras": "Cameras",
      "service.thermal": "Thermal", "service.vision": "Vision", "service.microscope": "Microscope",
      "service.furnace": "Furnace", "service.runs": "Runs",
      "chip.unconfigured": "Not configured", "chip.loading": "Loading", "chip.unavailable": "Unavailable",
      "chip.stale": "Stale", "chip.fault": "Fault", "chip.locked": "Locked", "chip.disconnected": "Not connected",
      "chip.busy": "Busy", "chip.ready": "Ready", "chip.demo": "demo",
      "chip.offline": "Console offline",
      "chip.lastState": "last state {age} s ago", "chip.noState": "no state yet",
      "stop.heat": "Stop heat and gas", "stop.gas": "Stop all gas", "stop.title": "{action} · {time}",
      "stop.phase.stopping": "Stopping", "stop.phase.holding": "Holding run", "stop.phase.final-stop": "Final stop",
      "stop.phase.done": "Done", "stop.sending": "Sending…", "stop.notSent": "Not sent: {error}",
      "stop.result.pending": "Pending", "stop.result.provisional": "Confirmed (provisional)", "stop.result.confirmed": "Confirmed",
      "stop.result.unconfirmed": "Not confirmed", "stop.result.failed": "Failed", "stop.result.unavailable": "Not available",
      "stop.holdUnconfirmed": "The run hold is not confirmed.", "stop.dismiss": "Dismiss", "stop.dismissLabel": "Dismiss the stop result",
      "target.gas": "Gas {n}",
      "outcome.unknown": "The outcome is unknown. Check the device before you continue.",
      "control.lost": "Control of {device} ended: {message}",
    },
    zh: {
      "console.name": "WuLab 控制台",
      "stop.all": "停止全部运动",
      "lang.switch": "English",
      "lang.switchLabel": "切换到英文",
      "theme.light": "Light mode", "theme.dark": "Dark mode",
      "theme.lightLabel": "Switch to light mode", "theme.darkLabel": "Switch to dark mode",
      "controls.some": "本页控制：{devices}",
      "service.robot": "机械臂", "service.stage": "位移台", "service.cameras": "相机",
      "service.thermal": "热像仪", "service.vision": "视觉", "service.microscope": "显微镜",
      "service.furnace": "炉子", "service.runs": "运行",
      "chip.unconfigured": "未配置", "chip.loading": "读取中", "chip.unavailable": "不可用",
      "chip.stale": "过期", "chip.fault": "故障", "chip.locked": "已锁定", "chip.disconnected": "未连接",
      "chip.busy": "忙碌", "chip.ready": "就绪", "chip.demo": "演示",
      "chip.offline": "控制台离线",
      "chip.lastState": "最后状态 {age} s 前", "chip.noState": "尚无状态",
      "stop.heat": "停止加热和气体", "stop.gas": "停止全部气体", "stop.title": "{action} · {time}",
      "stop.phase.stopping": "停止中", "stop.phase.holding": "暂停运行", "stop.phase.final-stop": "最终停止",
      "stop.phase.done": "完成", "stop.sending": "发送中…", "stop.notSent": "未发送：{error}",
      "stop.result.pending": "等待中", "stop.result.provisional": "已确认（暂定）", "stop.result.confirmed": "已确认",
      "stop.result.unconfirmed": "未确认", "stop.result.failed": "失败", "stop.result.unavailable": "不可用",
      "stop.holdUnconfirmed": "运行暂停未确认。", "stop.dismiss": "关闭", "stop.dismissLabel": "关闭停止结果",
      "target.gas": "气体 {n}",
      "outcome.unknown": "结果未知。继续前请检查设备。",
      "control.lost": "{device} 的控制已结束：{message}",
    },
  };

  // Pure logic (tested in Node) ---------------------------------------------

  function translate(tables, lang, key, vars) {
    const table = tables[lang] || {};
    let text = Object.prototype.hasOwnProperty.call(table, key) ? table[key]
      : Object.prototype.hasOwnProperty.call(tables.en || {}, key) ? tables.en[key] : key;
    for (const [name, value] of Object.entries(vars || {})) text = text.split("{" + name + "}").join(String(value));
    return text;
  }

  function pickLanguage(stored, navigatorLanguage) {
    if (stored === "en" || stored === "zh") return stored;
    return String(navigatorLanguage || "").toLowerCase().startsWith("zh") ? "zh" : "en";
  }

  /** One status chip: a tone for the dot and a text key. Text always names the state. */
  function serviceChip(card, fresh) {
    if (!fresh) return { tone: "warn", key: "chip.stale" };
    if (!card) return { tone: "off", key: "chip.unconfigured" };
    const phase = card.phase;
    let chip;
    if (phase === "unconfigured") chip = { tone: "off", key: "chip.unconfigured" };
    else if (phase === "loading") chip = { tone: "off", key: "chip.loading" };
    else if (phase === "unavailable") chip = { tone: "bad", key: "chip.unavailable" };
    else if (phase === "stale") chip = { tone: "warn", key: "chip.stale" };
    else if (card.fault) chip = { tone: "bad", key: "chip.fault" };
    else if (card.locked === true) chip = { tone: "bad", key: "chip.locked" };
    else if (card.connected !== true) chip = { tone: "off", key: "chip.disconnected" };
    else if (card.busy === true) chip = { tone: "info", key: "chip.busy" };
    else chip = { tone: "ok", key: "chip.ready" };
    chip.demo = card.demo === true;
    return chip;
  }

  /** The runs chip, or null when no run exists. */
  function runsChip(orchestrator, run, fresh) {
    if (!fresh || !orchestrator || orchestrator.stale) return { tone: "warn", key: "chip.stale" };
    if (!run) return null;
    return { tone: run.status === "running" ? "info" : run.status === "failed" ? "bad" : "ok", text: run.status || "" };
  }

  /** The strip chips. When the console itself does not answer, 1 chip says so instead of 8 stale devices. */
  function chips(snapshot, fresh, offline = false) {
    if (offline && !fresh) return [{ id: "console", tone: "bad", key: "chip.offline" }];
    const cards = new Map((snapshot?.services || []).map(card => [card.id, card]));
    const rows = [];
    for (const id of ["robot", "stage"]) rows.push({ id, ...serviceChip(cards.get(id), fresh) });
    for (const id of ["thermal", "vision", "microscope", "furnace"]) rows.push({ id, ...serviceChip(cards.get(id), fresh) });
    const runs = runsChip(snapshot?.orchestrator, snapshot?.run, fresh);
    if (runs) rows.push({ id: "runs", ...runs });
    return rows;
  }

  function filterEvents(events, filter) {
    return (events || []).filter(item => {
      if (!filter || filter === "all") return true;
      if (filter === "warnings") return ["warning", "error", "critical"].includes(item.level);
      return item.service === filter;
    });
  }

  function latestOperation(operations, action) {
    const rows = (operations || []).filter(item => item.action === action);
    return rows.length ? rows[rows.length - 1] : null;
  }

  // The stop routes of the header and the Overview. Motion and heat also hold the run.
  const STOP_LABELS = { motion: "stop.all", heat: "stop.heat", gas: "stop.gas" };
  const STOP_PHASES = { motion: ["stopping", "holding", "final-stop", "done"], heat: ["stopping", "holding", "final-stop", "done"], gas: ["stopping", "done"] };
  const RESULT_TONES = { pending: "info", provisional: "info", confirmed: "ok", unconfirmed: "warn", failed: "bad", unavailable: "off" };

  /** The band model of 1 stop press and its operation. A confirmation stays provisional until the operation is done. */
  function stopView(press, operation) {
    const phases = STOP_PHASES[press.action] || STOP_PHASES.motion;
    const done = operation?.done === true || operation?.phase === "done";
    const phase = press.error || press.sending ? "" : done ? "done" : phases.includes(operation?.phase) ? operation.phase : "stopping";
    const targets = Object.entries(operation?.results || {}).map(([target, result]) => {
      let status = Object.prototype.hasOwnProperty.call(RESULT_TONES, result?.status) && result.status !== "provisional" ? result.status : "failed";
      if (status === "confirmed" && !done && press.action !== "gas") status = "provisional";
      return { target, status, tone: RESULT_TONES[status] };
    });
    const holdFailed = operation?.hold === "unconfirmed";
    let tone = "info";
    if (press.error) tone = "bad";
    else if (done) {
      if (holdFailed || targets.some(item => item.status === "unconfirmed" || item.status === "failed")) tone = "bad";
      else tone = targets.some(item => item.status === "confirmed") ? "ok" : "off";
    }
    return { action: press.action, phases, phase, done, targets, holdFailed, tone, sending: press.sending === true, error: press.error || "" };
  }

  // Browser shell -------------------------------------------------------------

  const workspaces = new Map();
  const shell = {
    base: "", page: "", lang: "en", snapshot: null, stateAt: -Infinity,
    stateListeners: new Set(), languageListeners: new Set(), mounted: new Set(),
    active: "", beatTimer: null, pollTimer: null, beatBusy: false, polling: false,
    knownControl: new Map(), doc: null, band: null,
  };
  // current: the stop that the band shows. known: operation ids that this page has seen.
  const stops = { current: null, known: new Set(), primed: false };

  function now() { return (root.performance && root.performance.now) ? root.performance.now() : Date.now(); }
  function fresh() { return shell.snapshot !== null && now() - shell.stateAt < STALE_MS; }
  function t(key, vars) { return translate(strings, shell.lang, key, vars); }

  function clean(text) { return String(text ?? ""); }

  class RequestError extends Error {
    constructor(message, status = 0, body = null) { super(message); this.status = status; this.body = body; }
  }

  async function request(path, data, options = {}) {
    if (!path.startsWith("/api/")) throw new RequestError("Invalid console path.");
    if (options.socket && link.open && data !== undefined) return viaSocket(path, data, options);
    const headers = { Accept: "application/json" };
    if (shell.page) headers["X-MG400-Client"] = shell.page;
    const init = { headers, redirect: "error", credentials: "omit", cache: "no-store", keepalive: options.keepalive === true };
    if (data !== undefined) {
      init.method = "POST";
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(data);
    }
    let controller = null, timer = null;
    if (!options.keepalive && typeof AbortController !== "undefined") {
      controller = new AbortController();
      init.signal = controller.signal;
      timer = setTimeout(() => controller.abort(), options.timeout || 6000);
    }
    try {
      let response;
      try { response = await root.fetch(shell.base + path, init); }
      catch { throw new RequestError("The console did not answer.", 0); }
      let body = null;
      try { body = await response.json(); } catch { body = null; }
      if (!body || typeof body !== "object" || Array.isArray(body)) throw new RequestError("The console returned an unreadable response.", response.status);
      if (!response.ok && !(options.accept || []).includes(response.status)) {
        throw new RequestError(clean(typeof body.error === "string" ? body.error : "The console refused the request."), response.status, body);
      }
      return body;
    } finally { if (timer) clearTimeout(timer); }
  }

  // Control socket ------------------------------------------------------------
  // Chrome opens at most 6 HTTP/1.1 connections to one host, and camera streams keep
  // some of them busy. One WebSocket per page carries holds, arm keys and stops, so
  // they never wait for a free connection. HTTP is the fallback. Stops use both.

  const link = { ws: null, open: false, seq: 0, waiting: new Map(), timer: null, delay: SOCKET_RETRY_MS };

  function connect() {
    if (link.ws || !shell.page || typeof root.WebSocket !== "function") return;
    let ws;
    try {
      const url = new URL(shell.base + "/api/console/socket", root.location.href);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      url.searchParams.set("client", shell.page);
      ws = new root.WebSocket(url.href);
    } catch { reconnect(); return; }
    link.ws = ws;
    ws.onopen = () => { link.open = true; link.delay = SOCKET_RETRY_MS; };
    ws.onmessage = event => {
      let reply = null;
      try { reply = JSON.parse(event.data); } catch { return; }
      link.waiting.get(reply?.seq)?.(reply);
    };
    ws.onclose = () => {
      link.ws = null;
      link.open = false;
      for (const answer of [...link.waiting.values()]) answer(null);
      reconnect();
    };
  }

  function reconnect() {
    if (link.timer) return;
    link.timer = setTimeout(() => { link.timer = null; connect(); }, link.delay);
    link.delay = Math.min(link.delay * 2, 30000);
  }

  /** Send one POST over the socket. Resolve or reject as request() does. */
  function viaSocket(path, data, options = {}) {
    return new Promise((resolve, reject) => {
      const seq = ++link.seq;
      const timer = setTimeout(() => answer(null), options.timeout || 6000);
      function answer(reply) {
        clearTimeout(timer);
        link.waiting.delete(seq);
        const body = reply?.body;
        if (!reply) reject(new RequestError("The console did not answer.", 0));
        else if (!body || typeof body !== "object" || Array.isArray(body)) reject(new RequestError("The console returned an unreadable response.", reply.status));
        else if ((reply.status < 200 || reply.status > 299) && !(options.accept || []).includes(reply.status)) {
          reject(new RequestError(clean(typeof body.error === "string" ? body.error : "The console refused the request."), reply.status, body));
        } else resolve(body);
      }
      link.waiting.set(seq, answer);
      try { link.ws.send(JSON.stringify({ seq, path, body: data })); }
      catch { answer(null); }
    });
  }

  /** Send one stop over the socket and over HTTP. Resolve with the first answer. */
  function both(path, data = {}) {
    const sent = [request(path, data)];
    if (link.open) sent.unshift(viaSocket(path, data));
    return Promise.any(sent).catch(error => { throw error.errors[error.errors.length - 1]; });
  }

  function $(id) { return shell.doc.getElementById(id); }

  function el(tag, props, ...children) {
    const node = shell.doc.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = clean(value);
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else if (key === "hidden" || key === "disabled") node[key] = Boolean(value);
      else node.setAttribute(key, String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(typeof child === "string" ? shell.doc.createTextNode(clean(child)) : child);
    }
    return node;
  }

  function notify(text, level = "info") {
    const box = $("lc-notice");
    if (!box) return;
    box.textContent = clean(text);
    box.className = "lc-notice " + level;
    box.hidden = !text;
  }

  function deviceName(id) { return t("service." + id); }

  function myControls() {
    return (shell.snapshot?.control || []).filter(item => item.mine && item.state === "held");
  }

  function applyControl(list) {
    if (!shell.snapshot) return;
    const previous = shell.knownControl;
    shell.snapshot.control = list;
    const next = new Map(list.filter(item => item.mine).map(item => [item.device, item]));
    for (const [device] of previous) {
      if (!next.has(device)) {
        const ended = list.find(item => item.device === device);
        if (!ended && shell.expectRelease?.has(device)) continue;
        if (!ended) notify(t("control.lost", { device: deviceName(device), message: "lease ended" }), "warn");
      }
    }
    shell.expectRelease?.clear();
    shell.knownControl = next;
    renderControls();
  }

  function renderControls() {
    const mine = myControls().map(item => deviceName(item.device));
    const text = $("lc-controls-text");
    if (text) text.textContent = mine.length ? t("controls.some", { devices: mine.join(", ") }) : "";
    // The pill shows only while this page holds a device.
    const pill = $("lc-controls");
    if (pill) { pill.className = "lc-pill" + (mine.length ? " active" : ""); pill.hidden = !mine.length; }
  }

  function renderStrip() {
    const strip = $("lc-strip");
    if (!strip) return;
    const rows = chips(shell.snapshot, fresh(), shell.offline).map(row => {
      if (row.id === "console") {
        const age = shell.snapshot ? Math.round((now() - shell.stateAt) / 1000) : null;
        return { ...row, name: t(row.key), detail: age === null ? t("chip.noState") : t("chip.lastState", { age }) };
      }
      return { ...row, name: deviceName(row.id), detail: (row.key ? t(row.key) : row.text) + (row.demo ? " · " + t("chip.demo") : "") };
    });
    // The strip is a live region. Replace it only when its text changes, so a screen reader reads a change once.
    const sig = JSON.stringify(rows);
    if (sig === shell.stripSig) return;
    shell.stripSig = sig;
    strip.replaceChildren(...rows.map(row => el("span", { class: "lc-chip tone-" + row.tone, "data-service": row.id },
      el("span", { class: "lc-dot", "aria-hidden": "true" }),
      el("span", { class: "lc-chip-name", text: row.name }),
      el("span", { class: "lc-chip-detail", text: row.detail }))));
  }

  function clock(at) {
    try { return new Date(at).toLocaleTimeString(shell.lang === "zh" ? "zh-CN" : "en-GB", { hour12: false }); }
    catch { return ""; }
  }

  function targetName(target) {
    const gas = /^gas-(\d+)$/.exec(String(target));
    return gas ? t("target.gas", { n: Number(gas[1]) + 1 }) : deviceName(target);
  }

  /** Follow the stop operations in the state. A new stop from any client takes the band. */
  function trackStops(operations) {
    for (const item of Array.isArray(operations) ? operations : []) {
      if (!item || typeof item.id !== "string" || stops.known.has(item.id)) continue;
      stops.known.add(item.id);
      if (!STOP_LABELS[item.action] || (!stops.primed && item.done)) continue;
      // This page's own press gets its id from the reply.
      if (stops.current?.sending && stops.current.action === item.action) continue;
      stops.current = { action: item.action, id: item.id, at: Date.now() };
    }
    stops.primed = true;
  }

  function buildBand() {
    const node = $("lc-stopband");
    if (!node) return;
    const band = shell.band = {
      node, sig: "",
      dot: el("span", { class: "lc-dot", "aria-hidden": "true" }),
      title: el("span", { class: "lc-band-title" }),
      phases: el("ol", { class: "lc-phases" }),
      targets: el("ul", { class: "lc-band-targets" }),
      note: el("p", { class: "lc-band-note", hidden: true }),
      dismiss: el("button", { type: "button", class: "lc-button lc-band-dismiss", onclick: () => { if (stops.current) stops.current.dismissed = true; renderBand(); } }),
    };
    node.replaceChildren(el("div", { class: "lc-band-head" }, band.dot, band.title, band.phases, band.dismiss), band.targets, band.note);
  }

  function renderBand() {
    const band = shell.band;
    if (!band) return;
    const press = stops.current;
    band.node.hidden = !press || press.dismissed === true;
    if (band.node.hidden) { band.sig = ""; return; }
    const found = press.id ? (shell.snapshot?.operations || []).find(item => item?.id === press.id) : null;
    if (found) press.last = found;
    const model = stopView(press, found || press.last || null);
    const sig = JSON.stringify([shell.lang, press.at, model]);
    if (sig === band.sig) return;
    band.sig = sig;
    band.node.className = "lc-stopband tone-" + model.tone;
    band.title.textContent = t("stop.title", { action: t(STOP_LABELS[model.action]), time: clock(press.at) });
    band.phases.hidden = Boolean(model.error);
    const index = model.phases.indexOf(model.phase);
    band.phases.replaceChildren(...model.phases.map((phase, i) => el("li", {
      class: "lc-phase" + (i < index ? " past" : i === index ? " current" : ""),
      "aria-current": i === index ? "step" : null, text: t("stop.phase." + phase),
    })));
    band.targets.hidden = !model.targets.length;
    band.targets.replaceChildren(...model.targets.map(item => el("li", { class: "lc-band-target" },
      el("span", { class: "lc-chip-name", text: targetName(item.target) }),
      el("span", { class: "tone-text-" + item.tone, text: t("stop.result." + item.status) }))));
    const note = model.error ? t("stop.notSent", { error: model.error }) : model.sending ? t("stop.sending") : model.holdFailed ? t("stop.holdUnconfirmed") : "";
    band.note.textContent = note;
    band.note.hidden = !note;
    band.dismiss.textContent = t("stop.dismiss");
    band.dismiss.setAttribute("aria-label", t("stop.dismissLabel"));
  }

  function applyLanguage() {
    const doc = shell.doc;
    doc.documentElement.lang = shell.lang === "zh" ? "zh-CN" : "en";
    for (const node of doc.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
    for (const node of doc.querySelectorAll("#lc-tabs a")) node.textContent = node.dataset["title" + (shell.lang === "zh" ? "Zh" : "En")];
    const button = $("lc-lang");
    button.textContent = t("lang.switch");
    button.setAttribute("aria-label", t("lang.switchLabel"));
    renderTheme();
    renderControls();
    renderStrip();
    renderBand();
    for (const listener of shell.languageListeners) { try { listener(shell.lang); } catch (error) { console.error(error); } }
  }

  function setLanguage(lang) {
    shell.lang = lang === "zh" ? "zh" : "en";
    try { root.localStorage.setItem(LANG_KEY, shell.lang); } catch { /* Storage may be blocked. */ }
    applyLanguage();
  }

  function renderTheme() {
    const button = $("lc-theme");
    if (!button) return;
    const next = shell.doc.documentElement.dataset.theme === "dark" ? "light" : "dark";
    button.textContent = t("theme." + next);
    button.setAttribute("aria-label", t("theme." + next + "Label"));
  }

  function toggleTheme() {
    const page = shell.doc.documentElement;
    page.dataset.theme = page.dataset.theme === "dark" ? "light" : "dark";
    try { root.localStorage.setItem("labconsole.theme", page.dataset.theme); } catch { /* Storage may be blocked. */ }
    renderTheme();
  }

  async function poll() {
    if (shell.polling) return;
    shell.polling = true;
    try {
      const body = await request("/api/console/state");
      shell.snapshot = body;
      shell.stateAt = now();
      trackStops(body.operations);
      renderBand();
      applyControl(body.control || []);
      for (const listener of shell.stateListeners) { try { listener(body); } catch (error) { console.error(error); } }
      shell.offline = false;
    } catch {
      /* After 3 s without a state the strip shows Console offline. */
      shell.offline = true;
    } finally {
      shell.polling = false;
      renderStrip();
    }
  }

  async function beat() {
    if (shell.beatBusy || !myControls().length) return;
    shell.beatBusy = true;
    try {
      const body = await request("/api/console/heartbeat", {}, { timeout: 2000 });
      applyControl(body.control || []);
    } catch { /* The server releases after 3 s without a heartbeat. */ }
    finally { shell.beatBusy = false; }
  }

  async function take(device) {
    const body = await request(`/api/console/control/${encodeURIComponent(device)}/take`, {});
    await poll();
    return body.control;
  }

  async function release(device) {
    shell.expectRelease = shell.expectRelease || new Set();
    shell.expectRelease.add(device);
    const body = await request(`/api/console/control/${encodeURIComponent(device)}/release`, {});
    await poll();
    return body;
  }

  async function releaseAll() {
    shell.expectRelease = new Set(myControls().map(item => item.device));
    const body = await request("/api/console/release-all", {});
    await poll();
    return body.results;
  }

  /** Send one manual command once. Resolve {outcome: accepted|refused|unknown, ...}. */
  async function relay(device, action, params = {}) {
    try {
      const body = await request(`/api/console/command/${encodeURIComponent(device)}`, { action, params }, { accept: [409, 502], timeout: 8000 });
      if (body.outcome === "unknown") notify(t("outcome.unknown"), "error");
      return body;
    } catch (error) {
      if (error.status === 0) {
        notify(t("outcome.unknown"), "error");
        return { outcome: "unknown", error: error.message };
      }
      return { outcome: "refused", error: error.message };
    }
  }

  async function stop(device) {
    try { return await both(`/api/console/stop/${encodeURIComponent(device)}`); }
    catch (error) { return { device, outcome: "unknown", error: error.message }; }
  }

  /** Send 1 stop of the header or the Overview: motion, heat or gas. The band shows its result. */
  async function safetyStop(action) {
    if (!STOP_LABELS[action]) throw new RequestError("Unknown stop.");
    const press = { action, id: null, at: Date.now(), sending: true, error: "" };
    stops.current = press;
    renderBand();
    try {
      const body = await both("/api/dashboard/" + action, action === "motion" ? {} : { confirmed: true });
      if (typeof body.operation_id === "string") {
        press.id = body.operation_id;
        stops.known.add(press.id);
      }
      return body;
    } catch (error) {
      press.error = error.message || String(error);
      throw error;
    } finally {
      press.sending = false;
      renderBand();
      poll();
    }
  }

  function stopAll() { return safetyStop("motion"); }

  function apiFor(workspace) {
    const tables = { en: { ...strings.en, ...(workspace.i18n?.en || {}) }, zh: { ...strings.zh, ...(workspace.i18n?.zh || {}) } };
    return {
      base: shell.base,
      t: (key, vars) => translate(tables, shell.lang, key, vars),
      lang: () => shell.lang,
      onLanguage(listener) { shell.languageListeners.add(listener); return () => shell.languageListeners.delete(listener); },
      state: () => shell.snapshot,
      fresh,
      onState(listener) { shell.stateListeners.add(listener); return () => shell.stateListeners.delete(listener); },
      controls: () => (shell.snapshot?.control || []).slice(),
      owns: device => myControls().some(item => item.device === device),
      take, release, releaseAll, relay, stop, stopAll, safetyStop,
      describe: device => request(`/api/console/describe/${encodeURIComponent(device)}`),
      request: (path, data, options) => request(`/api/console/ws/${workspace.id}/${String(path).replace(/^\/+/, "")}`, data, options),
      consoleRequest: request,
      notify, el,
      deviceName,
    };
  }

  function register(workspace) {
    if (!workspace || typeof workspace.id !== "string" || typeof workspace.mount !== "function") throw new Error("A workspace needs an id and mount().");
    workspaces.set(workspace.id, workspace);
  }

  function show(id) {
    const doc = shell.doc;
    const sections = [...doc.querySelectorAll(".lc-workspace")];
    if (!sections.some(node => node.dataset.workspace === id)) id = "overview";
    for (const node of sections) {
      const visible = node.dataset.workspace === id;
      node.hidden = !visible;
      const workspace = workspaces.get(node.dataset.workspace);
      if (visible && workspace && !shell.mounted.has(workspace.id)) {
        shell.mounted.add(workspace.id);
        try { workspace.mount(node, apiFor(workspace)); } catch (error) { console.error(error); }
      }
      if (workspace && shell.active === workspace.id && !visible) workspace.hide?.();
      if (workspace && visible && shell.active !== workspace.id) workspace.show?.();
    }
    for (const link of doc.querySelectorAll("#lc-tabs a")) {
      if (link.dataset.workspace === id) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    shell.active = id;
  }

  function newPage() {
    try {
      const bytes = root.crypto.getRandomValues(new Uint8Array(12));
      return "console-page-" + Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
    } catch { return ""; }
  }

  // The top band wraps to more rows in a narrow window. Publish its height while it is sticky.
  function trackTop() {
    const top = $("lc-top");
    if (!top || typeof root.ResizeObserver !== "function" || typeof root.getComputedStyle !== "function") return;
    const style = shell.doc.documentElement.style;
    new root.ResizeObserver(() => {
      const sticky = root.getComputedStyle(top).position === "sticky";
      style.setProperty("--lc-top", (sticky ? top.offsetHeight : 0) + "px");
    }).observe(top);
  }

  function start(doc = root.document) {
    shell.doc = doc;
    shell.base = doc.body.dataset.basePath || "";
    shell.page = newPage();
    let stored = null;
    try { stored = root.localStorage.getItem(LANG_KEY); } catch { /* Storage may be blocked. */ }
    shell.lang = pickLanguage(stored, root.navigator?.language);
    $("lc-lang").addEventListener("click", () => setLanguage(shell.lang === "en" ? "zh" : "en"));
    $("lc-theme")?.addEventListener("click", toggleTheme);
    $("lc-stop-all").addEventListener("click", () => { stopAll().catch(() => {}); });
    root.addEventListener("hashchange", () => show(String(root.location.hash || "").slice(1)));
    root.addEventListener("pagehide", () => {
      if (myControls().length) request("/api/console/release-page", {}, { keepalive: true }).catch(() => {});
    });
    trackTop();
    buildBand();
    applyLanguage();
    show(String(root.location?.hash || "").slice(1) || "overview");
    poll();
    shell.pollTimer = setInterval(() => { poll(); }, POLL_MS);
    shell.beatTimer = setInterval(() => { beat(); }, BEAT_MS);
    setInterval(renderStrip, 1000);
    connect();
  }

  if (root.document && typeof module === "undefined") {
    root.document.addEventListener("DOMContentLoaded", () => start(root.document));
  }

  return {
    register, start, strings, shell,
    logic: { translate, pickLanguage, serviceChip, runsChip, chips, filterEvents, latestOperation, stopView },
    STALE_MS, BEAT_MS, SERVICES,
    _internal: { request, relay, take, release, beat, poll, setLanguage, myControls, connect, link, stop, stopAll, safetyStop, stops },
  };
});
