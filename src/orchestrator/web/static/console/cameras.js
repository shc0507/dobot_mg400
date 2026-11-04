/* Lab Console Cameras workspace and the shared camera tile (window.LabCameras).
 * The console relays every view. The browser gets no camera key and no service token:
 * an <img> stream uses a short-lived single-use ticket. See docs/agents/console-design/CONTRACT.md. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else {
    root.LabCameras = api;
    api.install();
  }
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";

  const VIEWS = ["camera1", "camera2", "camera3", "camera4", "thermal", "microscope"];
  const MJPEG = new Set(["camera1", "camera2", "camera3", "camera4", "microscope"]);
  const STATUS_MS = 1000;
  const THERMAL_MS = 250;
  const DETECT_MS = 1000;
  const STALE_S = 2;
  const W = 160, H = 120;
  const BLANK = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
  const PREFIX = "/api/console/ws/cameras/";

  const i18n = {
    en: {
      "title.camera1": "camera1", "title.camera2": "camera2", "title.camera3": "camera3", "title.camera4": "camera4",
      "title.thermal": "Thermal", "title.microscope": "Microscope",
      "role.camera1": "Stage", "role.camera2": "Route", "role.camera3": "Load station", "role.camera4": "Tray",
      "role.thermal": "P1", "role.microscope": "Toupcam",
      "badge.live": "LIVE", "badge.stale": "STALE", "badge.offline": "OFFLINE", "badge.frozen": "FROZEN",
      "age": "{age} s", "mode.unknown": "Mode unknown",
      "tile.snapshot": "Snapshot", "tile.expand": "Expand", "tile.select": "Select {name}",
      "tile.noFrame": "No frame", "tile.notConfigured": "Not configured",
      "layout.label": "Layout", "layout.grid": "Grid · 6", "layout.focus": "Focus · 1 + strip",
      "overlay.label": "Overlays", "overlay.detections": "Chip detections", "overlay.thermal": "Thermal markers",
      "snapshot.all": "Snapshot all",
      "side.title": "Selected view", "side.mode": "Mode", "side.state": "State", "side.streams": "Relayed streams",
      "side.error": "Last error", "side.page": "Open the service page →",
      "side.detections": "Chip detections", "side.detectionsNone": "No detections",
      "side.detectionsOff": "Vision is not available", "side.detectionsCount": "{count} found",
      "reading.max": "Max", "reading.min": "Min", "reading.center": "Center", "reading.mean": "Mean",
      "units": "Units", "palette": "Palette", "palette.ironbow": "Ironbow", "palette.rainbow": "Rainbow", "palette.grey": "Grey",
      "scale": "Colour scale", "scale.auto": "Auto", "scale.fixed": "Fixed", "scale.low": "Low", "scale.high": "High",
      "region": "Region · crucible", "region.none": "Not defined yet. Draw it once on this view; the furnace screen and runs read it.",
      "region.bounds": "x {x0}–{x1}, y {y0}–{y1}", "region.draw": "Draw region", "region.drawing": "Drag on the thermal view.",
      "region.save": "Save region", "region.cancel": "Cancel",
      "region.manual": "The thermal service does not offer Save. Put these bounds under [regions.crucible] in rois.toml.",
      "region.confirm": "Save x {x0}–{x1}, y {y0}–{y1} as the crucible region? The furnace screen and runs read it.",
      "shutter": "Calibrate shutter", "shutter.confirm": "Close the shutter and recalibrate the thermal camera now? The view pauses for a moment.",
      "confirm": "Confirm", "cancel": "Cancel",
      "command.missing": "The thermal service does not offer shutter calibration.",
      "command.control": "Take thermal control first.", "command.take": "Take thermal control", "command.release": "Release thermal control",
      "command.accepted": "Sent: {action}", "command.refused": "Refused: {error}", "command.unknown": "Outcome unknown: {action}. Check the camera.",
      "freeze": "Freeze view", "unfreeze": "Resume view",
      "export": "Export this frame", "export.csv": "CSV ↓", "export.raw": "Raw + JSON ↓", "export.png": "PNG ↓",
      "export.none": "No thermal frame to export.",
      "snapshot.failed": "Snapshot failed: {error}",
      "colorbar": "Colour bar",
    },
    zh: {
      "title.camera1": "camera1", "title.camera2": "camera2", "title.camera3": "camera3", "title.camera4": "camera4",
      "title.thermal": "热像仪", "title.microscope": "显微镜",
      "role.camera1": "位移台", "role.camera2": "路线", "role.camera3": "装样位", "role.camera4": "托盘",
      "role.thermal": "P1", "role.microscope": "Toupcam",
      "badge.live": "实时", "badge.stale": "过期", "badge.offline": "离线", "badge.frozen": "已冻结",
      "age": "{age} 秒", "mode.unknown": "模式未知",
      "tile.snapshot": "快照", "tile.expand": "放大", "tile.select": "选择 {name}",
      "tile.noFrame": "无画面", "tile.notConfigured": "未配置",
      "layout.label": "布局", "layout.grid": "网格 · 6", "layout.focus": "聚焦 · 1 + 条",
      "overlay.label": "叠加", "overlay.detections": "芯片识别", "overlay.thermal": "温度标记",
      "snapshot.all": "全部快照",
      "side.title": "选中画面", "side.mode": "模式", "side.state": "状态", "side.streams": "中转流",
      "side.error": "最近错误", "side.page": "打开服务页面 →",
      "side.detections": "芯片识别", "side.detectionsNone": "未识别到",
      "side.detectionsOff": "视觉服务不可用", "side.detectionsCount": "找到 {count} 个",
      "reading.max": "最高", "reading.min": "最低", "reading.center": "中心", "reading.mean": "平均",
      "units": "单位", "palette": "色板", "palette.ironbow": "铁红", "palette.rainbow": "彩虹", "palette.grey": "灰度",
      "scale": "色标范围", "scale.auto": "自动", "scale.fixed": "固定", "scale.low": "下限", "scale.high": "上限",
      "region": "区域 · crucible", "region.none": "尚未定义。在此画面上画一次；炉子页面和运行会读取它。",
      "region.bounds": "x {x0}–{x1}，y {y0}–{y1}", "region.draw": "画区域", "region.drawing": "在热像画面上拖动。",
      "region.save": "保存区域", "region.cancel": "取消",
      "region.manual": "热像仪服务未提供保存。把这些边界写入 rois.toml 的 [regions.crucible]。",
      "region.confirm": "把 x {x0}–{x1}，y {y0}–{y1} 保存为 crucible 区域？炉子页面和运行会读取它。",
      "shutter": "快门校准", "shutter.confirm": "现在关闭快门并重新校准热像仪？画面会短暂暂停。",
      "confirm": "确认", "cancel": "取消",
      "command.missing": "热像仪服务未提供快门校准。",
      "command.control": "请先获取热像仪控制。", "command.take": "获取热像仪控制", "command.release": "释放热像仪控制",
      "command.accepted": "已发送：{action}", "command.refused": "被拒绝：{error}", "command.unknown": "结果未知：{action}。请检查相机。",
      "freeze": "冻结画面", "unfreeze": "恢复画面",
      "export": "导出此帧", "export.csv": "CSV ↓", "export.raw": "原始 + JSON ↓", "export.png": "PNG ↓",
      "export.none": "没有可导出的热像帧。",
      "snapshot.failed": "快照失败：{error}",
      "colorbar": "色标",
    },
  };

  // Pure logic (tested in Node) ---------------------------------------------

  function translate(lang, key, vars) {
    const table = i18n[lang] || i18n.en;
    let text = key in table ? table[key] : key in i18n.en ? i18n.en[key] : key;
    for (const [name, value] of Object.entries(vars || {})) text = text.split("{" + name + "}").join(String(value));
    return text;
  }

  /** The badge for one view: tone, text key and the frame age in seconds or null. */
  function badge(row, options = {}) {
    if (options.frozen) return { tone: "warn", key: "badge.frozen", age: null };
    if (!row || options.fresh === false) return { tone: "off", key: "badge.offline", age: null };
    const age = typeof row.age_s === "number" && Number.isFinite(row.age_s) ? row.age_s : null;
    if (row.state === "live" && age !== null && age <= STALE_S) return { tone: "ok", key: "badge.live", age };
    if ((row.state === "live" || row.state === "stale") && age !== null) return { tone: "warn", key: "badge.stale", age };
    return { tone: "off", key: "badge.offline", age };
  }

  /** The thermal row from a frame fetched at fetchedAt (ms), with an error if the last fetch failed. */
  function thermalRow(frame, fetchedAt, nowMs, failed) {
    if (failed || !frame) return { state: "offline", age_s: null };
    const age = (typeof frame.age_s === "number" ? frame.age_s : 0) + Math.max(0, nowMs - fetchedAt) / 1000;
    return { state: age <= STALE_S ? "live" : "stale", age_s: Math.round(age * 10) / 10 };
  }

  const STOPS = {
    ironbow: ["#1B0638", "#6A1C7A", "#C23B4B", "#F08A24", "#FBE38E"],
    rainbow: ["#2B1C9E", "#1E7BE0", "#2EC4A0", "#B8E04A", "#F5A623", "#E0332E"],
    grey: ["#000000", "#FFFFFF"],
  };

  function hex(color) { return [1, 3, 5].map(i => parseInt(color.slice(i, i + 2), 16)); }

  /** A 256-entry RGB table for a palette. */
  function paletteTable(name) {
    const stops = (STOPS[name] || STOPS.ironbow).map(hex);
    const table = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const position = (i / 255) * (stops.length - 1);
      const k = Math.min(stops.length - 2, Math.floor(position));
      const f = position - k;
      for (let c = 0; c < 3; c++) table[i * 3 + c] = Math.round(stops[k][c] + (stops[k + 1][c] - stops[k][c]) * f);
    }
    return table;
  }

  function convert(celsius, unit) {
    if (unit === "F") return celsius * 9 / 5 + 32;
    if (unit === "K") return celsius + 273.15;
    return celsius;
  }

  function toCelsius(value, unit) {
    if (unit === "F") return (value - 32) * 5 / 9;
    if (unit === "K") return value - 273.15;
    return value;
  }

  function unitLabel(unit) { return unit === "F" ? "°F" : unit === "K" ? "K" : "°C"; }

  function formatTemp(celsius, unit) {
    if (typeof celsius !== "number" || !Number.isFinite(celsius)) return "—";
    return convert(celsius, unit).toFixed(1) + " " + unitLabel(unit);
  }

  /** The colour scale in °C: the frame range, or the fixed low and high. */
  function scaleRange(stats, settings) {
    if (settings.scale === "fixed" && Number.isFinite(settings.low) && Number.isFinite(settings.high) && settings.high > settings.low) {
      return [settings.low, settings.high];
    }
    const low = stats?.min?.celsius, high = stats?.max?.celsius;
    if (Number.isFinite(low) && Number.isFinite(high)) return high > low ? [low, high] : [low - 0.5, high + 0.5];
    return [0, 1];
  }

  /** RGBA pixels for a 160×120 temperature matrix. */
  function thermalPixels(temperatures, table, low, high) {
    const out = new Uint8ClampedArray(W * H * 4);
    const span = high - low || 1;
    for (let y = 0; y < H; y++) {
      const row = temperatures[y] || [];
      for (let x = 0; x < W; x++) {
        const t = Math.max(0, Math.min(1, (row[x] - low) / span));
        const index = Number.isFinite(t) ? Math.round(t * 255) : 0;
        const o = (y * W + x) * 4;
        out[o] = table[index * 3]; out[o + 1] = table[index * 3 + 1]; out[o + 2] = table[index * 3 + 2]; out[o + 3] = 255;
      }
    }
    return out;
  }

  function thermalCsv(frame, unit) {
    const lines = [`# sequence=${frame.sequence}, timestamp=${frame.timestamp}, unit=${unitLabel(unit)}`];
    for (const row of frame.temperatures_c) lines.push(row.map(value => convert(value, unit).toFixed(2)).join(","));
    return lines.join("\n") + "\n";
  }

  /** Normalize a dragged rectangle in sensor pixels to region bounds, or null if it is too small. */
  function regionFrom(a, b) {
    const clamp = (v, max) => Math.max(0, Math.min(max, Math.round(v)));
    const x0 = clamp(Math.min(a.x, b.x), W), x1 = clamp(Math.max(a.x, b.x), W);
    const y0 = clamp(Math.min(a.y, b.y), H), y1 = clamp(Math.max(a.y, b.y), H);
    if (x1 - x0 < 2 || y1 - y0 < 2) return null;
    return { x0, y0, x1, y1 };
  }

  /** Where an image of size (w, h) is drawn inside a box with object-fit: contain. */
  function containRect(w, h, boxW, boxH) {
    if (!w || !h || !boxW || !boxH) return { x: 0, y: 0, scale: 0 };
    const scale = Math.min(boxW / w, boxH / h);
    return { x: (boxW - w * scale) / 2, y: (boxH - h * scale) / 2, scale };
  }

  function backoff(failures) { return Math.min(15000, 1000 * 2 ** Math.min(4, failures)); }

  function stamp(date = new Date()) { return date.toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z"); }

  // Shared engine ---------------------------------------------------------------

  const engine = {
    api: null, tiles: new Set(), status: new Map(), statusAt: -Infinity, statusBusy: false,
    thermal: null, thermalAt: -Infinity, thermalFailed: false, thermalBusy: false, thermalTimer: null,
    detections: null, detectBusy: false, detectTimer: null, timer: null, regions: {},
    settings: { unit: "C", palette: "ironbow", scale: "auto", low: 20, high: 40, frozen: false, markers: true, detections: true },
    listeners: new Set(),
  };

  function doc() { return root.document; }
  function lang() { return engine.api?.lang?.() || root.LabConsole?.shell?.lang || "en"; }
  function t(key, vars) { return translate(lang(), key, vars); }
  function base() { return doc()?.body?.dataset?.basePath || ""; }
  function nowMs() { return root.performance?.now ? root.performance.now() : Date.now(); }

  function request(path, data) {
    const send = engine.api?.consoleRequest || root.LabConsole?._internal?.request;
    if (!send) return Promise.reject(new Error("The console shell is not loaded."));
    return send(PREFIX + path, data);
  }

  function h(tag, props, ...children) {
    const node = doc().createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else if (key === "hidden" || key === "disabled") node[key] = Boolean(value);
      else node.setAttribute(key, String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(typeof child === "string" ? doc().createTextNode(child) : child);
    }
    return node;
  }

  function changed() { for (const fn of engine.listeners) { try { fn(); } catch (error) { console.error(error); } } }

  function setSettings(patch) {
    Object.assign(engine.settings, patch);
    for (const tile of engine.tiles) tile.paint();
    changed();
  }

  function running(view) { return [...engine.tiles].some(tile => tile.running && (!view || tile.view === view)); }

  async function pollStatus() {
    if (engine.statusBusy || !running()) return;
    engine.statusBusy = true;
    try {
      const body = await request("status");
      engine.status = new Map((body.views || []).map(row => [row.id, row]));
      engine.statusAt = nowMs();
    } catch { /* Badges go OFFLINE when the status is old. */ }
    finally { engine.statusBusy = false; }
  }

  async function pollThermal() {
    engine.thermalTimer = null;
    if (!running("thermal")) return;
    if (!engine.settings.frozen && !engine.thermalBusy) {
      engine.thermalBusy = true;
      try {
        engine.thermal = await request("thermal/frame");
        engine.thermalAt = nowMs();
        engine.thermalFailed = false;
      } catch { engine.thermalFailed = true; }
      finally { engine.thermalBusy = false; }
      for (const tile of engine.tiles) if (tile.view === "thermal") tile.paint();
      changed();
    }
    engine.thermalTimer = setTimeout(pollThermal, engine.thermalFailed ? 2000 : THERMAL_MS);
  }

  async function pollDetections() {
    engine.detectTimer = null;
    if (!running("camera4") || !engine.settings.detections) { engine.detections = null; return; }
    if (!engine.detectBusy) {
      engine.detectBusy = true;
      try { engine.detections = await request("detections/4"); }
      catch { engine.detections = { available: false }; }
      finally { engine.detectBusy = false; }
      for (const tile of engine.tiles) if (tile.view === "camera4") tile.paint();
      changed();
    }
    engine.detectTimer = setTimeout(pollDetections, DETECT_MS);
  }

  async function loadRegions() {
    try { engine.regions = (await request("thermal/regions")).regions || {}; }
    catch { engine.regions = {}; }
    for (const tile of engine.tiles) if (tile.view === "thermal") tile.paint();
    changed();
  }

  function rowFor(view) {
    if (view === "thermal") {
      return { ...(engine.status.get("thermal") || {}), ...thermalRow(engine.thermal, engine.thermalAt, nowMs(), engine.thermalFailed) };
    }
    return engine.status.get(view) || null;
  }

  function viewBadge(view) {
    if (view === "thermal") return badge(rowFor(view), { frozen: engine.settings.frozen });
    return badge(rowFor(view), { fresh: nowMs() - engine.statusAt < 3000 });
  }

  function visible(node) {
    if (!node.isConnected || doc().visibilityState === "hidden") return false;
    return node.getClientRects().length > 0;
  }

  function tick() {
    for (const tile of [...engine.tiles]) {
      if (!tile.el.isConnected && !tile.keep) { tile.stop(); engine.tiles.delete(tile); continue; }
      const show = visible(tile.el);
      if (show && !tile.running) tile.start();
      else if (!show && tile.running) tile.stop();
      tile.refresh();
    }
    pollStatus();
    if (running("thermal") && engine.thermalTimer === null) pollThermal();
    if (running("camera4") && engine.settings.detections && engine.detectTimer === null) pollDetections();
  }

  function download(name, blob) {
    const url = root.URL.createObjectURL(blob);
    const link = h("a", { href: url, download: name });
    doc().body.append(link);
    link.click();
    link.remove();
    setTimeout(() => root.URL.revokeObjectURL(url), 10000);
  }

  async function snapshot(view, tile) {
    if (view === "thermal") {
      const canvas = tile?.canvas;
      if (!canvas || !engine.thermal) throw new Error(t("export.none"));
      await new Promise(resolve => canvas.toBlob(blob => { if (blob) download(`thermal-${stamp()}.png`, blob); resolve(); }, "image/png"));
      return;
    }
    const body = await request("ticket", { view, kind: "snapshot" });
    const link = h("a", { href: body.url, download: `${view}-${stamp()}.jpg` });
    doc().body.append(link);
    link.click();
    link.remove();
  }

  // Tile ------------------------------------------------------------------------

  class Tile {
    constructor(view, options) {
      this.view = view;
      this.options = options;
      this.running = false;
      this.failures = 0;
      this.retry = null;
      this.generation = 0;
      this.drawing = null;
      this.build();
    }

    build() {
      const view = this.view;
      this.title = h("span", { class: "lcam-title" });
      this.role = h("span", { class: "lcam-role" });
      this.badgeText = h("span", { class: "lcam-badge-text" });
      this.badge = h("span", { class: "lcam-badge tone-off", role: "status" }, h("span", { class: "lc-dot", "aria-hidden": "true" }), this.badgeText);
      this.mode = h("span", { class: "lcam-mode" });
      this.note = h("span", { class: "lcam-note" });
      this.overlay = h("canvas", { class: "lcam-overlay", "aria-hidden": "true" });
      if (view === "thermal") {
        this.canvas = h("canvas", { class: "lcam-media", width: W * 4, height: H * 4 });
        this.colorbar = h("div", { class: "lcam-colorbar" });
        this.media = this.canvas;
        this.bindDrawing();
      } else {
        this.img = h("img", { class: "lcam-media", alt: "", decoding: "async" });
        this.img.addEventListener("error", () => this.failed());
        this.img.addEventListener("load", () => { if (this.running && this.img.src !== BLANK) { this.failures = 0; this.paint(); } });
        this.media = this.img;
      }
      this.empty = h("div", { class: "lcam-empty" });
      this.stage = h("div", { class: "lcam-stage", role: "button", tabindex: "0",
        onclick: () => this.options.onSelect?.(view),
        onkeydown: event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); this.options.onSelect?.(view); } } },
      this.media, this.overlay, this.empty, this.colorbar || null);
      this.snapButton = h("button", { type: "button", class: "lc-button lcam-small", onclick: event => { event.stopPropagation(); this.snapshot(); } });
      this.expandButton = h("button", { type: "button", class: "lc-button lcam-small", onclick: event => { event.stopPropagation(); this.expand(); } });
      this.el = h("figure", { class: "lcam-tile", "data-view": view },
        h("div", { class: "lcam-head" }, h("div", { class: "lcam-names" }, this.title, this.role), this.badge),
        this.stage,
        h("figcaption", { class: "lcam-foot" },
          h("div", { class: "lcam-meta" }, this.mode, this.note),
          h("div", { class: "lcam-buttons" }, this.snapButton, this.expandButton)));
      this.el.labCameras = this;
      this.refresh();
    }

    start() {
      this.running = true;
      const generation = ++this.generation;
      if (this.view === "thermal") { if (engine.thermalTimer === null) pollThermal(); this.paint(); return; }
      request("ticket", { view: this.view, kind: "stream" }).then(body => {
        if (!this.running || generation !== this.generation) return;
        this.img.src = body.url;
      }, () => this.failed());
      pollStatus();
    }

    stop() {
      this.running = false;
      this.generation++;
      if (this.retry) { clearTimeout(this.retry); this.retry = null; }
      // Replacing the source closes the stream, so the console ends its relay.
      if (this.img) this.img.src = BLANK;
      this.paint();
    }

    failed() {
      if (!this.running || (this.img && this.img.src === BLANK)) return;
      this.failures++;
      if (this.img) this.img.src = BLANK;
      if (this.retry) clearTimeout(this.retry);
      const generation = this.generation;
      // Retry the view only; a view is not a device command.
      this.retry = setTimeout(() => {
        this.retry = null;
        if (this.running && generation === this.generation) { this.running = false; this.start(); }
      }, backoff(this.failures));
    }

    async snapshot() {
      try { await snapshot(this.view, this); }
      catch (error) { (engine.api?.notify || console.warn)(t("snapshot.failed", { error: error.message }), "warn"); }
    }

    expand() {
      if (this.options.onExpand) this.options.onExpand(this.view);
      else if (this.el.requestFullscreen) this.el.requestFullscreen().catch(() => {});
    }

    refresh() {
      const view = this.view;
      this.title.textContent = t("title." + view);
      this.role.textContent = t("role." + view);
      this.snapButton.textContent = t("tile.snapshot");
      this.expandButton.textContent = t("tile.expand");
      this.stage.setAttribute("aria-label", t("tile.select", { name: t("title." + view) + " · " + t("role." + view) }));
      if (this.img) this.img.alt = t("title." + view) + " · " + t("role." + view);
      const row = rowFor(view);
      const state = viewBadge(view);
      this.badge.className = "lcam-badge tone-" + state.tone;
      this.badgeText.textContent = t(state.key) + (state.age !== null && state.age !== undefined ? " · " + t("age", { age: state.age.toFixed(1) }) : "");
      const mode = view === "thermal" ? "160×120" : row?.mode;
      this.mode.textContent = mode || t("mode.unknown");
      const offline = state.key === "badge.offline";
      this.empty.hidden = !offline;
      this.empty.textContent = row && row.configured === false ? t("tile.notConfigured") : t("tile.noFrame");
      this.snapButton.disabled = offline;
      if (view === "camera4") {
        const d = engine.detections;
        this.note.textContent = engine.settings.detections && d?.available ? t("side.detectionsCount", { count: d.detections.length }) : "";
      } else if (view === "thermal") {
        const stats = engine.thermal?.stats;
        this.note.textContent = stats ? t("reading.mean") + " " + formatTemp(stats.mean_celsius, engine.settings.unit) : "";
      }
    }

    paint() {
      this.refresh();
      if (this.view === "thermal") this.paintThermal();
      else this.paintDetections();
    }

    paintDetections() {
      const ctx = this.overlay.getContext?.("2d");
      if (!ctx) return;
      const boxW = this.stage.clientWidth, boxH = this.stage.clientHeight;
      this.overlay.width = boxW; this.overlay.height = boxH;
      ctx.clearRect(0, 0, boxW, boxH);
      const d = engine.detections;
      if (this.view !== "camera4" || !this.running || !engine.settings.detections || !d?.available) return;
      const rect = containRect(d.image_size.width, d.image_size.height, boxW, boxH);
      ctx.lineWidth = 2;
      ctx.font = "600 13px system-ui, sans-serif";
      for (const item of d.detections) {
        const [x0, y0, x1, y1] = item.xyxy.map(v => v * rect.scale);
        ctx.strokeStyle = "#4DA3FF";
        ctx.strokeRect(rect.x + x0, rect.y + y0, x1 - x0, y1 - y0);
        const label = `${item.label || "chip"} ${Math.round(item.confidence * 100)}%`;
        const width = ctx.measureText(label).width + 10;
        ctx.fillStyle = "rgba(10,14,17,0.85)";
        ctx.fillRect(rect.x + x0, Math.max(0, rect.y + y0 - 20), width, 20);
        ctx.fillStyle = "#E6EDF2";
        ctx.fillText(label, rect.x + x0 + 5, Math.max(14, rect.y + y0 - 6));
      }
    }

    paintThermal() {
      const frame = engine.thermal;
      const ctx = this.canvas.getContext?.("2d");
      const s = engine.settings;
      const [low, high] = scaleRange(frame?.stats, s);
      this.paintColorbar(low, high);
      if (!ctx || !frame) return;
      const source = h("canvas", { width: W, height: H });
      source.getContext("2d").putImageData(new root.ImageData(thermalPixels(frame.temperatures_c, paletteTable(s.palette), low, high), W, H), 0, 0);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(source, 0, 0, W * 4, H * 4);
      const region = this.drawing?.region || engine.regions.crucible;
      if (region) {
        ctx.strokeStyle = this.drawing ? "#F2B84B" : "#E6EDF2";
        ctx.setLineDash(this.drawing ? [6, 4] : []);
        ctx.lineWidth = 2;
        ctx.strokeRect(region.x0 * 4, region.y0 * 4, (region.x1 - region.x0) * 4, (region.y1 - region.y0) * 4);
        ctx.setLineDash([]);
      }
      if (!s.markers || !frame.stats) return;
      const marks = [["max", "#FF7A7A"], ["min", "#7DBBFF"], ["center", "#E6EDF2"]];
      ctx.font = "600 14px system-ui, sans-serif";
      for (const [key, color] of marks) {
        const point = frame.stats[key];
        if (!point) continue;
        const x = point.x * 4 + 2, y = point.y * 4 + 2;
        ctx.strokeStyle = color; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(x - 8, y); ctx.lineTo(x + 8, y); ctx.moveTo(x, y - 8); ctx.lineTo(x, y + 8); ctx.stroke();
        const label = convert(point.celsius, s.unit).toFixed(1) + "°";
        const tx = Math.min(W * 4 - 52, x + 8), ty = Math.max(16, y - 8);
        ctx.fillStyle = "rgba(10,14,17,0.85)"; ctx.fillRect(tx, ty - 14, ctx.measureText(label).width + 8, 18);
        ctx.fillStyle = color; ctx.fillText(label, tx + 4, ty);
      }
    }

    paintColorbar(low, high) {
      if (!this.colorbar) return;
      const s = engine.settings;
      this.colorbar.hidden = !s.markers;
      const stops = STOPS[s.palette] || STOPS.ironbow;
      this.colorbar.replaceChildren(
        h("span", { class: "lcam-cb-label", text: convert(high, s.unit).toFixed(1) + unitLabel(s.unit) }),
        h("span", { class: "lcam-cb-bar", role: "img", "aria-label": t("colorbar"), style: `background: linear-gradient(to top, ${stops.join(", ")})` }),
        h("span", { class: "lcam-cb-label", text: convert(low, s.unit).toFixed(1) + unitLabel(s.unit) }));
    }

    bindDrawing() {
      const point = event => {
        const box = this.canvas.getBoundingClientRect();
        const rect = containRect(W, H, box.width, box.height);
        if (!rect.scale) return { x: 0, y: 0 };
        return { x: (event.clientX - box.left - rect.x) / rect.scale, y: (event.clientY - box.top - rect.y) / rect.scale };
      };
      this.canvas.addEventListener("pointerdown", event => {
        if (!this.drawing) return;
        event.preventDefault();
        this.drawing.anchor = point(event);
        this.canvas.setPointerCapture?.(event.pointerId);
      });
      this.canvas.addEventListener("pointermove", event => {
        if (!this.drawing?.anchor) return;
        this.drawing.region = regionFrom(this.drawing.anchor, point(event)) || this.drawing.region;
        this.paintThermal();
      });
      this.canvas.addEventListener("pointerup", event => {
        if (!this.drawing?.anchor) return;
        this.drawing.region = regionFrom(this.drawing.anchor, point(event)) || this.drawing.region;
        this.drawing.anchor = null;
        this.paintThermal();
        this.drawing.onChange?.(this.drawing.region);
      });
    }
  }

  /** A live camera tile for any workspace. view: camera1–camera4, thermal or microscope.
   * options: {api, large, onSelect(view), onExpand(view)}. The tile streams only while it is visible. */
  function tile(view, options = {}) {
    if (!VIEWS.includes(view)) throw new Error("Unknown camera view: " + view);
    if (options.api && !engine.api) engine.api = options.api;
    const item = new Tile(view, options);
    if (options.large) item.el.classList?.add("lcam-large");
    engine.tiles.add(item);
    if (!engine.timer && root.document) {
      engine.timer = setInterval(tick, STATUS_MS);
      root.document.addEventListener?.("visibilitychange", tick);
      loadRegions();
    }
    setTimeout(tick, 0);
    return item.el;
  }

  // Workspace -------------------------------------------------------------------

  function workspace(rootNode, api) {
    engine.api = api;
    const ui = { layout: "grid", selected: "thermal", focus: "camera2", tiles: new Map(), confirm: null, describe: null, note: "" };
    const tiles = () => ui.tiles;
    for (const view of VIEWS) {
      ui.tiles.set(view, tile(view, {
        api,
        onSelect: v => { ui.selected = v; renderSide(); markSelected(); },
        onExpand: v => { ui.layout = "focus"; ui.focus = v; ui.selected = v; arrange(); renderSide(); },
      }));
    }
    const grid = h("div", { class: "lcam-grid" });
    const large = h("div", { class: "lcam-focus" });
    const strip = h("div", { class: "lcam-strip" });
    const toolbar = h("div", { class: "lcam-toolbar" });
    const side = h("section", { class: "lc-card lcam-side", "aria-labelledby": "lcam-side-title" });
    rootNode.replaceChildren(h("div", { class: "lc-layout" },
      h("div", { class: "lc-col-main" }, toolbar, grid, large, strip),
      h("div", { class: "lc-col-side" }, side)));

    function toggle(label, pressed, onclick) {
      return h("button", { type: "button", class: "lc-filter", "aria-pressed": pressed ? "true" : "false", onclick, text: label });
    }

    function renderToolbar() {
      const s = engine.settings;
      toolbar.replaceChildren(
        h("div", { class: "lc-filters", role: "group", "aria-label": api.t("layout.label") },
          toggle(api.t("layout.grid"), ui.layout === "grid", () => { ui.layout = "grid"; arrange(); }),
          toggle(api.t("layout.focus"), ui.layout === "focus", () => { ui.layout = "focus"; ui.focus = ui.selected; arrange(); })),
        h("div", { class: "lc-filters", role: "group", "aria-label": api.t("overlay.label") },
          toggle((s.detections ? "✓ " : "") + api.t("overlay.detections"), s.detections, () => setSettings({ detections: !s.detections })),
          toggle((s.markers ? "✓ " : "") + api.t("overlay.thermal"), s.markers, () => setSettings({ markers: !s.markers }))),
        h("button", { type: "button", class: "lc-button", text: api.t("snapshot.all"), onclick: snapshotAll }));
    }

    async function snapshotAll() {
      for (const view of VIEWS) {
        if (viewBadge(view).key === "badge.offline") continue;
        try { await snapshot(view, ui.tiles.get(view).labCameras); } catch { /* One failed view does not stop the others. */ }
      }
    }

    function markSelected() {
      for (const [view, node] of tiles()) node.classList.toggle("lcam-selected", view === ui.selected);
    }

    function arrange() {
      grid.hidden = ui.layout !== "grid";
      large.hidden = strip.hidden = ui.layout !== "focus";
      for (const [view, node] of tiles()) {
        node.classList.toggle("lcam-large", ui.layout === "focus" && view === ui.focus);
        if (ui.layout === "grid") grid.append(node);
        else (view === ui.focus ? large : strip).append(node);
      }
      renderToolbar();
      markSelected();
      setTimeout(tick, 0);
    }

    function row(label, value) {
      return h("div", { class: "lc-result" }, h("span", { class: "lc-muted", text: label }), h("span", { class: "lc-strong", text: value }));
    }

    function segmented(label, options, current, onPick) {
      return h("div", { class: "lcam-field" }, h("span", { class: "lcam-label", id: "lcam-l-" + label, text: api.t(label) }),
        h("div", { class: "lc-filters", role: "group", "aria-labelledby": "lcam-l-" + label },
          ...options.map(([value, text]) => toggle(text, current === value, () => onPick(value)))));
    }

    function commandInfo(action) {
      const commands = ui.describe?.commands || {};
      return commands[action] || null;
    }

    async function send(action, params) {
      const result = await api.relay("thermal", action, params);
      if (result.outcome === "accepted") api.notify(api.t("command.accepted", { action }), "info");
      else if (result.outcome === "unknown") api.notify(api.t("command.unknown", { action }), "error");
      else api.notify(api.t("command.refused", { error: result.error || "" }), "warn");
      if (action === "set_region") loadRegions();
      return result;
    }

    function confirmBox(text, onConfirm) {
      return h("div", { class: "lc-confirm", role: "alertdialog", "aria-label": text },
        h("p", { text }),
        h("div", { class: "lc-filters" },
          h("button", { type: "button", class: "lc-danger lcam-confirm", text: api.t("confirm"), onclick: () => { ui.confirm = null; renderSide(); onConfirm(); } }),
          h("button", { type: "button", class: "lc-button", text: api.t("cancel"), onclick: () => { ui.confirm = null; renderSide(); } })));
    }

    function commandButton(action, label, onClick) {
      const info = commandInfo(action);
      const button = h("button", { type: "button", class: "lc-button", text: label, disabled: !info, onclick: onClick });
      return button;
    }

    function controlLine() {
      const needs = ["shutter", "set_region"].some(action => { const info = commandInfo(action); return info && !info.any_client; });
      if (!needs) return null;
      if (api.owns("thermal")) return h("button", { type: "button", class: "lc-button", text: api.t("command.release"), onclick: () => api.release("thermal").catch(error => api.notify(error.message, "warn")).then(renderSide) });
      return h("div", { class: "lcam-field" }, h("span", { class: "lc-muted", text: api.t("command.control") }),
        h("button", { type: "button", class: "lc-button", text: api.t("command.take"), onclick: () => api.take("thermal").catch(error => api.notify(error.message, "warn")).then(renderSide) }));
    }

    function thermalPanel() {
      const s = engine.settings;
      const stats = engine.thermal?.stats;
      const thermalTile = ui.tiles.get("thermal").labCameras;
      const region = engine.regions.crucible;
      const parts = [];
      parts.push(h("div", { class: "lc-grid2" },
        ...[["max", stats?.max?.celsius], ["min", stats?.min?.celsius], ["center", stats?.center?.celsius], ["mean", stats?.mean_celsius]]
          .map(([key, value]) => h("div", { class: "lcam-reading" }, h("span", { class: "lc-muted", text: api.t("reading." + key) }), h("span", { class: "lcam-value", text: formatTemp(value, s.unit) })))));
      parts.push(segmented("units", [["C", "°C"], ["F", "°F"], ["K", "K"]], s.unit, unit => setSettings({ unit })));
      const palette = h("select", { id: "lcam-palette", class: "lcam-select", onchange: event => setSettings({ palette: event.target.value }) },
        ...["ironbow", "rainbow", "grey"].map(name => h("option", { value: name, text: api.t("palette." + name), selected: s.palette === name ? "selected" : null })));
      palette.value = s.palette;
      parts.push(h("div", { class: "lcam-field" }, h("label", { class: "lcam-label", for: "lcam-palette", text: api.t("palette") }), palette));
      parts.push(segmented("scale", [["auto", api.t("scale.auto")], ["fixed", api.t("scale.fixed")]], s.scale, scale => setSettings({ scale })));
      const limit = (key) => {
        const input = h("input", { id: "lcam-" + key, class: "lcam-input", type: "number", step: "0.5", inputmode: "decimal", disabled: s.scale !== "fixed",
          value: convert(s[key], s.unit).toFixed(1),
          onchange: event => { const value = Number(event.target.value); if (Number.isFinite(value)) setSettings({ [key]: toCelsius(value, s.unit) }); } });
        return h("div", { class: "lcam-field lcam-inline" }, h("label", { class: "lcam-label", for: "lcam-" + key, text: api.t("scale." + key) + " (" + unitLabel(s.unit) + ")" }), input);
      };
      parts.push(h("div", { class: "lc-grid2" }, limit("low"), limit("high")));
      parts.push(h("button", { type: "button", class: "lc-button", "aria-pressed": s.frozen ? "true" : "false", text: s.frozen ? api.t("unfreeze") : api.t("freeze"), onclick: () => setSettings({ frozen: !s.frozen }) }));

      // Region and shutter change calibration: both go through the relay with a confirmation.
      const regionParts = [h("h3", { class: "lcam-h3", text: api.t("region") })];
      if (thermalTile.drawing) {
        const proposed = thermalTile.drawing.region;
        regionParts.push(h("p", { class: "lc-muted", text: proposed ? api.t("region.bounds", proposed) : api.t("region.drawing") }));
        if (ui.confirm === "region" && proposed) {
          regionParts.push(confirmBox(api.t("region.confirm", proposed), () => {
            thermalTile.drawing = null;
            send("set_region", { name: "crucible", ...proposed });
            thermalTile.paint();
            renderSide();
          }));
        } else {
          regionParts.push(h("div", { class: "lc-filters" },
            h("button", { type: "button", class: "lc-button", text: api.t("region.save"), disabled: !proposed || !commandInfo("set_region"), onclick: () => { ui.confirm = "region"; renderSide(); } }),
            h("button", { type: "button", class: "lc-button", text: api.t("region.cancel"), onclick: () => { thermalTile.drawing = null; thermalTile.paint(); renderSide(); } })));
        }
      } else {
        regionParts.push(h("p", { class: "lc-muted", text: region ? api.t("region.bounds", region) : api.t("region.none") }));
        regionParts.push(h("button", { type: "button", class: "lc-button", text: api.t("region.draw"), onclick: () => {
          thermalTile.drawing = { region: region ? { ...region } : null, anchor: null, onChange: renderSide };
          thermalTile.paint();
          renderSide();
        } }));
      }
      if (thermalTile.drawing && thermalTile.drawing.region && !commandInfo("set_region")) regionParts.push(h("p", { class: "lc-muted", text: api.t("region.manual") }));
      parts.push(h("div", { class: "lcam-group" }, ...regionParts));
      if (ui.confirm === "shutter") parts.push(confirmBox(api.t("shutter.confirm"), () => send("shutter", {})));
      else parts.push(commandButton("shutter", api.t("shutter"), () => { ui.confirm = "shutter"; renderSide(); }));
      if (!commandInfo("shutter")) parts.push(h("p", { class: "lc-muted", text: api.t("command.missing") }));
      const control = controlLine();
      if (control) parts.push(control);

      parts.push(h("h3", { class: "lcam-h3", text: api.t("export") }));
      parts.push(h("div", { class: "lc-filters" },
        h("button", { type: "button", class: "lc-button", text: api.t("export.csv"), disabled: !engine.thermal, onclick: () => {
          if (engine.thermal) download(`thermal-${engine.thermal.sequence}.csv`, new root.Blob([thermalCsv(engine.thermal, s.unit)], { type: "text/csv" }));
        } }),
        h("button", { type: "button", class: "lc-button", text: api.t("export.raw"), disabled: !engine.thermal, onclick: async () => {
          try {
            const frame = await request("thermal/frame?full=1");
            download(`thermal-${frame.sequence}.json`, new root.Blob([JSON.stringify(frame)], { type: "application/json" }));
          } catch (error) { api.notify(api.t("snapshot.failed", { error: error.message }), "warn"); }
        } }),
        h("button", { type: "button", class: "lc-button", text: api.t("export.png"), disabled: !engine.thermal, onclick: () => snapshot("thermal", thermalTile).catch(error => api.notify(error.message, "warn")) })));
      return parts;
    }

    function viewPanel(view) {
      const status = rowFor(view) || {};
      const node = ui.tiles.get(view).labCameras;
      const parts = [
        row(api.t("side.mode"), status.mode || api.t("mode.unknown")),
        row(api.t("side.streams"), String(status.streams ?? 0)),
      ];
      if (status.error) parts.push(row(api.t("side.error"), status.error));
      if (view === "camera4") {
        const d = engine.detections;
        const text = !engine.settings.detections ? "—" : !d?.available ? api.t("side.detectionsOff")
          : d.detections.length ? api.t("side.detectionsCount", { count: d.detections.length }) : api.t("side.detectionsNone");
        parts.push(row(api.t("side.detections"), text));
      }
      parts.push(h("div", { class: "lc-filters" },
        h("button", { type: "button", class: "lc-button", text: api.t("tile.snapshot"), onclick: () => node.snapshot() }),
        h("button", { type: "button", class: "lc-button", text: api.t("tile.expand"), onclick: () => node.expand() })));
      const pageId = view === "microscope" ? "microscope" : "camera";
      const link = (api.state()?.cameras || []).find(item => item.id === pageId && item.url);
      if (link) parts.push(h("a", { class: "lc-link", href: link.url, target: "_blank", rel: "noopener noreferrer", text: api.t("side.page") }));
      return parts;
    }

    function renderSide() {
      const view = ui.selected;
      const state = viewBadge(view);
      side.replaceChildren(
        h("div", { class: "lc-card-head" },
          h("h2", { id: "lcam-side-title", text: api.t("side.title") }),
          h("span", { class: "lcam-badge tone-" + state.tone }, h("span", { class: "lc-dot", "aria-hidden": "true" }),
            h("span", { text: api.t(state.key) + (state.age !== null && state.age !== undefined ? " · " + api.t("age", { age: state.age.toFixed(1) }) : "") }))),
        h("p", { class: "lc-strong", text: api.t("title." + view) + " · " + api.t("role." + view) }),
        ...(view === "thermal" ? thermalPanel() : viewPanel(view)));
    }

    let sideTimer = null;
    engine.listeners.add(() => {
      // Repaint the side panel at most once a second; inputs keep their focus between updates.
      const active = doc().activeElement;
      if (sideTimer || (active && side.contains(active) && ["INPUT", "SELECT"].includes(active.tagName))) return;
      sideTimer = setTimeout(() => { sideTimer = null; renderSide(); }, 1000);
    });
    api.describe("thermal").then(value => { ui.describe = value; renderSide(); }, () => { ui.describe = null; });
    api.onLanguage(() => { arrange(); renderSide(); for (const node of tiles().values()) node.labCameras.refresh(); });
    api.onState(() => { if (!sideTimer) sideTimer = setTimeout(() => { sideTimer = null; renderSide(); }, 1000); });
    arrange();
    renderSide();
  }

  function install() {
    if (!root.LabConsole || !root.document) return;
    root.LabConsole.register({
      id: "cameras",
      i18n,
      mount: workspace,
      show() { setTimeout(tick, 0); },
      hide() { setTimeout(tick, 0); },
    });
  }

  return {
    tile, install, VIEWS, i18n, engine,
    logic: { translate, badge, thermalRow, paletteTable, convert, toCelsius, formatTemp, scaleRange, thermalPixels, thermalCsv, regionFrom, containRect, backoff, stamp },
  };
});
