/* Lab Console shell: the frame, the relay client, control heartbeats and i18n.
 * Workspaces call LabConsole.register({id, i18n, mount(root, api), show(), hide()}).
 * The browser never receives a service token. See docs/agents/console-design/CONTRACT.md. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.LabConsole = api;
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";

  const STALE_MS = 3000;
  const BEAT_MS = 500;
  const POLL_MS = 1000;
  const LANG_KEY = "labconsole.lang";
  const SERVICES = ["robot", "stage", "thermal", "vision", "furnace", "microscope"];

  const strings = {
    en: {
      "console.name": "WuLab Console",
      "stop.all": "STOP ALL MOTION",
      "lang.switch": "中文",
      "lang.switchLabel": "Switch language to Chinese",
      "access.label": "Console access token",
      "access.connect": "Connect",
      "access.needed": "Enter the console access token.",
      "controls.none": "You control: none",
      "controls.some": "You control: {devices}",
      "service.robot": "Robot", "service.stage": "Stage", "service.cameras": "Cameras",
      "service.thermal": "Thermal", "service.vision": "Vision", "service.microscope": "Microscope",
      "service.furnace": "Furnace", "service.runs": "Runs",
      "chip.unconfigured": "Not configured", "chip.loading": "Loading", "chip.unavailable": "Unavailable",
      "chip.stale": "Stale", "chip.fault": "Fault", "chip.locked": "Locked", "chip.disconnected": "Disconnected",
      "chip.busy": "Busy", "chip.ready": "Ready", "chip.demo": "demo", "chip.linked": "Page linked",
      "chip.noRun": "No run", "chip.offline": "Console offline",
      "stop.sent": "Stop all motion sent.", "stop.failed": "Stop all motion could not be sent: {error}",
      "outcome.unknown": "The outcome is unknown. Check the device before you continue.",
      "control.lost": "Control of {device} ended: {message}",
    },
    zh: {
      "console.name": "WuLab 控制台",
      "stop.all": "停止全部运动",
      "lang.switch": "English",
      "lang.switchLabel": "切换到英文",
      "access.label": "控制台访问令牌",
      "access.connect": "连接",
      "access.needed": "请输入控制台访问令牌。",
      "controls.none": "本页控制：无",
      "controls.some": "本页控制：{devices}",
      "service.robot": "机械臂", "service.stage": "位移台", "service.cameras": "相机",
      "service.thermal": "热像仪", "service.vision": "视觉", "service.microscope": "显微镜",
      "service.furnace": "炉子", "service.runs": "运行",
      "chip.unconfigured": "未配置", "chip.loading": "读取中", "chip.unavailable": "不可用",
      "chip.stale": "过期", "chip.fault": "故障", "chip.locked": "已锁定", "chip.disconnected": "未连接",
      "chip.busy": "忙碌", "chip.ready": "就绪", "chip.demo": "演示", "chip.linked": "已链接页面",
      "chip.noRun": "无运行", "chip.offline": "控制台离线",
      "stop.sent": "已发送停止全部运动。", "stop.failed": "无法发送停止全部运动：{error}",
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
    else if (card.connected !== true) chip = { tone: "warn", key: "chip.disconnected" };
    else if (card.busy === true) chip = { tone: "info", key: "chip.busy" };
    else chip = { tone: "ok", key: "chip.ready" };
    chip.demo = card.demo === true;
    return chip;
  }

  function runsChip(orchestrator, run, fresh) {
    if (!fresh || !orchestrator || orchestrator.stale) return { tone: "warn", key: "chip.stale" };
    if (!run) return { tone: "off", key: "chip.noRun" };
    return { tone: run.status === "running" ? "info" : run.status === "failed" ? "bad" : "ok", text: run.status || "" };
  }

  function camerasChip(cameras, fresh) {
    if (!fresh) return { tone: "warn", key: "chip.stale" };
    const linked = (cameras || []).some(item => item && item.id === "camera" && item.url);
    return linked ? { tone: "info", key: "chip.linked" } : { tone: "off", key: "chip.unconfigured" };
  }

  function chips(snapshot, fresh) {
    const cards = new Map((snapshot?.services || []).map(card => [card.id, card]));
    const rows = [];
    for (const id of ["robot", "stage"]) rows.push({ id, ...serviceChip(cards.get(id), fresh) });
    rows.push({ id: "cameras", ...camerasChip(snapshot?.cameras, fresh) });
    for (const id of ["thermal", "vision", "microscope", "furnace"]) rows.push({ id, ...serviceChip(cards.get(id), fresh) });
    rows.push({ id: "runs", ...runsChip(snapshot?.orchestrator, snapshot?.run, fresh) });
    return rows;
  }

  function filterEvents(events, filter) {
    return (events || []).filter(item => {
      if (!filter || filter === "all") return true;
      if (filter === "warnings") return ["warning", "error", "critical"].includes(item.level);
      return item.service === filter;
    });
  }

  function readinessSummary(rows) {
    const counts = { ready: 0, todo: 0, blocked: 0 };
    for (const row of rows || []) if (row.state in counts) counts[row.state]++;
    return counts;
  }

  function latestOperation(operations, action) {
    const rows = (operations || []).filter(item => item.action === action);
    return rows.length ? rows[rows.length - 1] : null;
  }

  // Browser shell -------------------------------------------------------------

  const workspaces = new Map();
  const shell = {
    base: "", token: "", page: "", lang: "en", snapshot: null, stateAt: -Infinity,
    stateListeners: new Set(), languageListeners: new Set(), mounted: new Set(),
    active: "", beatTimer: null, pollTimer: null, beatBusy: false, polling: false,
    knownControl: new Map(), doc: null,
  };

  function now() { return (root.performance && root.performance.now) ? root.performance.now() : Date.now(); }
  function fresh() { return shell.snapshot !== null && now() - shell.stateAt < STALE_MS; }
  function t(key, vars) { return translate(strings, shell.lang, key, vars); }

  function clean(text) {
    const value = String(text ?? "");
    return shell.token ? value.split(shell.token).join("[redacted]") : value;
  }

  class RequestError extends Error {
    constructor(message, status = 0, body = null) { super(message); this.status = status; this.body = body; }
  }

  async function request(path, data, options = {}) {
    if (!path.startsWith("/api/")) throw new RequestError("Invalid console path.");
    const headers = { Accept: "application/json" };
    if (shell.token) headers.Authorization = "Bearer " + shell.token;
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
      if (response.status === 401) showAccess(true);
      if (!body || typeof body !== "object" || Array.isArray(body)) throw new RequestError("The console returned an unreadable response.", response.status);
      if (!response.ok && !(options.accept || []).includes(response.status)) {
        throw new RequestError(clean(typeof body.error === "string" ? body.error : "The console refused the request."), response.status, body);
      }
      return body;
    } finally { if (timer) clearTimeout(timer); }
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

  function showAccess(show) {
    const box = $("lc-access");
    if (box) box.hidden = !show;
    if (show) notify(t("access.needed"), "warn");
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
    if (text) text.textContent = mine.length ? t("controls.some", { devices: mine.join(", ") }) : t("controls.none");
    const pill = $("lc-controls");
    if (pill) pill.className = "lc-pill" + (mine.length ? " active" : "");
  }

  function renderStrip() {
    const strip = $("lc-strip");
    if (!strip) return;
    const ok = fresh();
    const rows = chips(shell.snapshot, ok);
    strip.replaceChildren(...rows.map(row => {
      const detail = row.key ? t(row.key) : row.text;
      return el("span", { class: "lc-chip tone-" + row.tone, "data-service": row.id },
        el("span", { class: "lc-dot", "aria-hidden": "true" }),
        el("span", { class: "lc-chip-name", text: deviceName(row.id) }),
        el("span", { class: "lc-chip-detail", text: detail + (row.demo ? " · " + t("chip.demo") : "") }));
    }));
  }

  function applyLanguage() {
    const doc = shell.doc;
    doc.documentElement.lang = shell.lang === "zh" ? "zh-CN" : "en";
    for (const node of doc.querySelectorAll("[data-i18n]")) node.textContent = t(node.dataset.i18n);
    for (const node of doc.querySelectorAll("#lc-tabs a")) node.textContent = node.dataset["title" + (shell.lang === "zh" ? "Zh" : "En")];
    const button = $("lc-lang");
    button.textContent = t("lang.switch");
    button.setAttribute("aria-label", t("lang.switchLabel"));
    renderControls();
    renderStrip();
    for (const listener of shell.languageListeners) { try { listener(shell.lang); } catch (error) { console.error(error); } }
  }

  function setLanguage(lang) {
    shell.lang = lang === "zh" ? "zh" : "en";
    try { root.localStorage.setItem(LANG_KEY, shell.lang); } catch { /* Storage may be blocked. */ }
    applyLanguage();
  }

  async function poll() {
    if (shell.polling) return;
    shell.polling = true;
    try {
      const body = await request("/api/console/state");
      shell.snapshot = body;
      shell.stateAt = now();
      showAccess(false);
      if ($("lc-notice")?.textContent === t("access.needed")) notify("");
      applyControl(body.control || []);
      for (const listener of shell.stateListeners) { try { listener(body); } catch (error) { console.error(error); } }
    } catch (error) {
      if (error.status !== 401) { /* The strip shows stale after 3 s. */ }
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
    try { return await request(`/api/console/stop/${encodeURIComponent(device)}`, {}); }
    catch (error) { return { device, outcome: "unknown", error: error.message }; }
  }

  async function stopAll() {
    try {
      const body = await request("/api/dashboard/motion", {});
      notify(t("stop.sent"), "warn");
      poll();
      return body;
    } catch (error) {
      notify(t("stop.failed", { error: error.message }), "error");
      throw error;
    }
  }

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
      take, release, releaseAll, relay, stop, stopAll,
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

  function start(doc = root.document) {
    shell.doc = doc;
    shell.base = doc.body.dataset.basePath || "";
    shell.page = newPage();
    let stored = null;
    try { stored = root.localStorage.getItem(LANG_KEY); } catch { /* Storage may be blocked. */ }
    shell.lang = pickLanguage(stored, root.navigator?.language);
    $("lc-lang").addEventListener("click", () => setLanguage(shell.lang === "en" ? "zh" : "en"));
    $("lc-stop-all").addEventListener("click", () => { stopAll().catch(() => {}); });
    $("lc-connect").addEventListener("click", () => {
      shell.token = $("lc-token").value.trim();
      $("lc-token").value = "";
      poll();
    });
    root.addEventListener("hashchange", () => show(String(root.location.hash || "").slice(1)));
    root.addEventListener("pagehide", () => {
      if (myControls().length) request("/api/console/release-page", {}, { keepalive: true }).catch(() => {});
    });
    applyLanguage();
    show(String(root.location?.hash || "").slice(1) || "overview");
    poll();
    shell.pollTimer = setInterval(() => { poll(); }, POLL_MS);
    shell.beatTimer = setInterval(() => { beat(); }, BEAT_MS);
    setInterval(renderStrip, 1000);
  }

  if (root.document && typeof module === "undefined") {
    root.document.addEventListener("DOMContentLoaded", () => start(root.document));
  }

  return {
    register, start, strings, shell,
    logic: { translate, pickLanguage, serviceChip, runsChip, camerasChip, chips, filterEvents, readinessSummary, latestOperation },
    STALE_MS, BEAT_MS, SERVICES,
    _internal: { request, relay, take, release, beat, poll, setLanguage, myControls },
  };
});
