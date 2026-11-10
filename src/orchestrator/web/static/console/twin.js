/* Digital Twin: camera measurements and numerical stage calibration.
 * The console owns camera access and profile storage. This page sends no motion commands.
 */
(function (root, factory) {
  "use strict";
  const exported = factory(root);
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else if (root.LabConsole) root.LabConsole.register(exported.workspace());
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";

  const AXES = ["x", "y", "insert"];
  const MAX_PROFILE_BYTES = 1024 * 1024;
  const RATES = [0.25, 0.5, 1, 2, 5];
  const ENDPOINTS = [
    { id: "x_low", axis: "x", high: false }, { id: "x_high", axis: "x", high: true },
    { id: "y_low", axis: "y", high: false }, { id: "y_high", axis: "y", high: true },
    { id: "insert_out", axis: "insert", high: false }, { id: "insert_in", axis: "insert", high: true },
  ];
  const GROUPS = { desk: [10, 11, 12], y: [14, 15, 16], x: [18, 19, 20], insert: [22] };
  const i18n = {
    en: {
      "twin.monitor": "Monitor", "twin.calibration": "Calibration", "twin.camera": "Camera",
      "twin.profile": "Saved profile", "twin.draft": "Unsaved profile", "twin.noSavedProfile": "No saved profile", "twin.refresh": "Refresh",
      "twin.new": "New profile", "twin.name": "Profile name", "twin.save": "Save calibration",
      "twin.download": "Download profile", "twin.saved": "Calibration saved.",
      "twin.profiles": "Saved profiles", "twin.upload": "Upload profile", "twin.profileFile": "Profile JSON file",
      "twin.load": "Load", "twin.delete": "Delete", "twin.selected": "Selected",
      "twin.loadNamed": "Load {name}", "twin.deleteNamed": "Delete {name}",
      "twin.loaded": "Calibration loaded.", "twin.uploaded": "Calibration uploaded and selected.", "twin.deleted": "Saved profile deleted.",
      "twin.fileSize": "Profile file must be 1 MiB or smaller.", "twin.fileJson": "Select a valid JSON profile file.",
      "twin.fileRead": "Could not read the profile file.",
      "twin.start": "Start sampling", "twin.pause": "Pause sampling", "twin.paused": "Paused",
      "twin.sampling": "Sampling", "twin.working": "Reading frame…", "twin.loading": "Loading…",
      "twin.rate": "Target rate", "twin.outlines": "Tag outlines", "twin.frame": "Sampled frame",
      "twin.preview": "Live preview", "twin.snapshot": "Show last snap", "twin.waiting": "Waiting for a sample",
      "twin.time": "Sample time", "twin.effective": "Effective rate", "twin.processing": "Processing time",
      "twin.positions": "Stage position", "twin.axis.x": "X", "twin.axis.y": "Y", "twin.axis.insert": "Insert",
      "twin.xy": "X/Y travel", "twin.insert": "Insert travel", "twin.out": "Fully out", "twin.in": "Fully in",
      "twin.pending": "Calibration pending", "twin.unknown": "Unknown", "twin.current": "Measured",
      "twin.held": "Last recorded {time}", "twin.outside": "Outside travel", "twin.travel": "{axis} travel (mm)",
      "twin.tags": "Marker visibility", "twin.group.desk": "Desk", "twin.group.x": "X", "twin.group.y": "Y", "twin.group.insert": "Insert",
      "twin.detected": "Detected", "twin.missing": "Missing", "twin.details": "Conversion details",
      "twin.pair": "Moving tag → reference", "twin.estimate": "Estimate (mm)", "twin.offtrack": "Off-track (px)",
      "twin.noPair": "No calibrated tag pair in this frame.", "twin.log": "Sample log", "twin.count": "{count} samples",
      "twin.tune": "Detector auto-tune", "twin.automatic": "Automatic search", "twin.frames": "Maximum frames",
      "twin.tuneNow": "Tune now", "twin.preset": "Active setting", "twin.standard": "Standard",
      "twin.tuning": "Searching · {count}/{limit} frames", "twin.tuned": "Tested {count} frames · {found}/{total} calibrated axes readable",
      "twin.tuneReady": "Search when a calibrated axis loses its marker pair.",
      "twin.capture": "Snap", "twin.clear": "Clear capture", "twin.captured": "{count}/6 limits captured",
      "twin.captureAuto": "Auto-tune snaps", "twin.captureWorking": "Reading {endpoint}…",
      "twin.captureKept": "Previous capture kept.", "twin.captureMissing": "Missing tags: {ids}",
      "twin.captureNone": "Not captured yet.", "twin.fitPending": "Capture both limits to calibrate this axis.",
      "twin.partial": "Partial profiles can be saved and completed later.",
      "twin.guidance": "Move one axis between its limits. Hold the other axes still. Keep the camera and marker mounts fixed.",
      "twin.reference": "Moving: {moving} · Reference: {parent}",
      "twin.x_low": "X low · Left", "twin.x_high": "X high · Right", "twin.y_low": "Y low · Down", "twin.y_high": "Y high · Up",
      "twin.insert_out": "Insert out", "twin.insert_in": "Insert in", "twin.invalidTravel": "Travel must be a positive finite number.",
      "twin.noProfile": "Create a profile or select a saved profile to calibrate the axes.",
      "twin.recovery.recovered": "ID 22 recovered along the calibrated path.",
      "twin.recovery.missing_reference": "Waiting for a fitted X-stage reference.",
      "twin.recovery.unreadable": "ID 22 unreadable along the calibrated path.",
      "twin.recovery.ambiguous": "Ambiguous insert detection in this frame.",
      "twin.recovery.unavailable": "Insert path geometry unavailable in this profile.",
      "twin.imageError": "The sampled image could not be decoded.", "twin.axis": "Axis", "twin.noCamera": "No configured camera",
      "twin.replace": "Replace the unsaved draft?", "twin.discard": "Discard draft", "twin.cancel": "Cancel",
    },
    zh: {
      "twin.monitor": "监测", "twin.calibration": "校准", "twin.camera": "摄像头",
      "twin.profile": "已存配置", "twin.draft": "未保存配置", "twin.noSavedProfile": "没有已存配置", "twin.refresh": "刷新",
      "twin.new": "新建配置", "twin.name": "配置名称", "twin.save": "保存校准",
      "twin.download": "下载配置", "twin.saved": "校准已保存。",
      "twin.profiles": "已存配置", "twin.upload": "上传配置", "twin.profileFile": "配置 JSON 文件",
      "twin.load": "加载", "twin.delete": "删除", "twin.selected": "已选",
      "twin.loadNamed": "加载 {name}", "twin.deleteNamed": "删除 {name}",
      "twin.loaded": "校准已加载。", "twin.uploaded": "校准已上传并选中。", "twin.deleted": "已删除保存的配置。",
      "twin.fileSize": "配置文件不得超过 1 MiB。", "twin.fileJson": "请选择有效的 JSON 配置文件。",
      "twin.fileRead": "无法读取配置文件。",
      "twin.start": "开始采样", "twin.pause": "暂停采样", "twin.paused": "已暂停",
      "twin.sampling": "采样中", "twin.working": "正在读取图像…", "twin.loading": "加载中…",
      "twin.rate": "目标频率", "twin.outlines": "标记轮廓", "twin.frame": "采样图像",
      "twin.preview": "实时预览", "twin.snapshot": "显示最近拍摄", "twin.waiting": "等待采样",
      "twin.time": "采样时间", "twin.effective": "实际频率", "twin.processing": "处理时间",
      "twin.positions": "位移台位置", "twin.axis.x": "X", "twin.axis.y": "Y", "twin.axis.insert": "Insert",
      "twin.xy": "X/Y 行程", "twin.insert": "Insert 行程", "twin.out": "完全抽出", "twin.in": "完全插入",
      "twin.pending": "待校准", "twin.unknown": "未知", "twin.current": "实测",
      "twin.held": "上次记录 {time}", "twin.outside": "超出行程", "twin.travel": "{axis} 行程（mm）",
      "twin.tags": "标记可见性", "twin.group.desk": "桌面", "twin.group.x": "X", "twin.group.y": "Y", "twin.group.insert": "Insert",
      "twin.detected": "已检测", "twin.missing": "缺失", "twin.details": "换算详情",
      "twin.pair": "移动标记 → 参考标记", "twin.estimate": "估计值（mm）", "twin.offtrack": "偏离拟合线（px）",
      "twin.noPair": "本帧没有已校准的标记对。", "twin.log": "采样记录", "twin.count": "{count} 次采样",
      "twin.tune": "检测器自动调节", "twin.automatic": "自动搜索", "twin.frames": "最多帧数",
      "twin.tuneNow": "立即调节", "twin.preset": "当前设置", "twin.standard": "标准",
      "twin.tuning": "搜索中 · {count}/{limit} 帧", "twin.tuned": "已测 {count} 帧 · 可读取 {found}/{total} 个已校准轴",
      "twin.tuneReady": "已校准轴丢失标记对时搜索检测设置。",
      "twin.capture": "拍摄", "twin.clear": "清除拍摄", "twin.captured": "已拍摄 {count}/6 个端点",
      "twin.captureAuto": "拍摄时自动调节", "twin.captureWorking": "正在读取{endpoint}…",
      "twin.captureKept": "已保留原拍摄。", "twin.captureMissing": "缺失标记：{ids}",
      "twin.captureNone": "尚未拍摄。", "twin.fitPending": "拍摄两个端点以校准此轴。",
      "twin.partial": "可保存部分配置，稍后补齐。",
      "twin.guidance": "每次只移动一个轴到两个端点。其他轴保持不动。固定摄像头和标记安装位置。",
      "twin.reference": "移动：{moving} · 参考：{parent}",
      "twin.x_low": "X 低端 · 左", "twin.x_high": "X 高端 · 右", "twin.y_low": "Y 低端 · 下", "twin.y_high": "Y 高端 · 上",
      "twin.insert_out": "Insert 抽出端", "twin.insert_in": "Insert 插入端", "twin.invalidTravel": "行程必须为正有限数。",
      "twin.noProfile": "新建配置或选择已存配置以校准各轴。",
      "twin.recovery.recovered": "已沿校准路径找回标记 22。",
      "twin.recovery.missing_reference": "等待已拟合的 X 轴参考标记。",
      "twin.recovery.unreadable": "无法沿校准路径读取标记 22。",
      "twin.recovery.ambiguous": "本帧的 Insert 检测结果不唯一。",
      "twin.recovery.unavailable": "此配置没有 Insert 路径几何数据。",
      "twin.imageError": "无法解码采样图像。", "twin.axis": "轴", "twin.noCamera": "没有已配置的摄像头",
      "twin.replace": "替换未保存的配置？", "twin.discard": "丢弃草稿", "twin.cancel": "取消",
    },
  };

  function finite(value) { return typeof value === "number" && Number.isFinite(value); }
  function number(value) { return finite(value) ? value.toFixed(2).replace(/^-0\.00$/, "0.00") : "—"; }
  function calibrated(profile, axis) {
    const fit = profile?.axes?.[axis];
    return finite(fit?.min) && finite(fit?.max) && fit.max > fit.min;
  }
  function readings(previous, axes, profile, capturedAt) {
    const cache = {}, display = {};
    for (const name of AXES) {
      const axis = axes?.[name];
      if (!calibrated(profile, name) || axis?.reason === "calibration_missing") continue;
      if (finite(axis?.value_mm)) cache[name] = display[name] = { ...axis, recordedAt: capturedAt, held: false };
      else if ((!axis || axis.reason === "no_calibrated_pair_visible") && previous[name]) {
        cache[name] = previous[name];
        display[name] = { ...previous[name], held: true };
      }
    }
    return { cache, display };
  }
  function fraction(value, fit) {
    if (!finite(value) || !finite(fit?.min) || !finite(fit?.max) || fit.max <= fit.min) return null;
    const result = (value - fit.min) / (fit.max - fit.min);
    return result >= 0 && result <= 1 ? result : null;
  }
  function mapPosition(display, profile) {
    const x = fraction(display.x?.value_mm, profile?.axes?.x), y = fraction(display.y?.value_mm, profile?.axes?.y);
    return x === null || y === null ? null : { x, y, held: Boolean(display.x.held || display.y.held) };
  }
  function travel(values) {
    const result = {};
    for (const axis of AXES) {
      const text = String(values?.[axis] ?? "").trim(), value = Number(text);
      if (!text || !finite(value) || value <= 0 || value > 1000000) return null;
      result[axis] = value;
    }
    return result;
  }
  function effectiveRate(times) {
    const span = times.at(-1) - times[0];
    return times.length > 1 && span > 0 ? (times.length - 1) * 1000 / span : null;
  }
  function nextDue(start, now, hz) {
    const interval = 1000 / (RATES.includes(hz) ? hz : 1);
    return start + Math.max(1, Math.ceil((now - start) / interval)) * interval;
  }
  function safeImage(value) { return typeof value === "string" && /^data:image\/(jpeg|png);base64,[A-Za-z0-9+/=]+$/.test(value) ? value : null; }
  function detectionGeometry(detection) {
    const corners = detection?.corners_px, center = detection?.center_px;
    if (!Number.isInteger(detection?.id) || !Array.isArray(corners) || corners.length !== 4 || !corners.every(p => Array.isArray(p) && p.length === 2 && p.every(finite))) return null;
    return { id: detection.id, corners, center: Array.isArray(center) && center.length === 2 && center.every(finite) ? center : corners[0] };
  }
  function preferences(storage) {
    try {
      const saved = JSON.parse(storage?.getItem("lab-twin-tuning") || "{}");
      return { automatic: saved.automatic !== false, maxFrames: Number.isInteger(saved.maxFrames) && saved.maxFrames >= 1 && saved.maxFrames <= 8 ? saved.maxFrames : 4 };
    } catch { return { automatic: true, maxFrames: 4 }; }
  }

  function workspace() {
    let host, api, nodes = {}, state = null, section = "monitor", visible = false, busy = false, loading = false;
    let running = false, measuring = false, version = 0, timer = null, lastStarted = null, previewTimer = null, previewVersion = 0, previewPending = false, editTimer = null;
    let latest = null, lastSnap = null, cache = {}, display = {}, log = [], times = [], tuning = null, pendingAction = null;
    let dirty = false, formDraft = null, errorMessage = "", captureMessages = {}, rate = 1, outlines = true, showSnapshot = false, captureAuto = true;
    let settings;
    try { settings = preferences(root.localStorage); } catch { settings = preferences(null); }
    const profile = () => state?.calibration?.profile;
    const monitorProfile = () => state?.active_profile;
    const t = (key, vars) => api.t("twin." + key, vars);
    const h = (tag, props, ...children) => api.el(tag, props, ...children);
    const time = value => {
      const stamp = new Date(value);
      return Number.isNaN(stamp.getTime()) ? "—" : stamp.toLocaleTimeString(api.lang?.() === "zh" ? "zh-CN" : "en-GB", { hour12: false });
    };
    const setText = (name, value) => { if (nodes[name]) nodes[name].textContent = value; };
    function message(text = "") { errorMessage = text; if (nodes.message) { nodes.message.textContent = text; nodes.message.hidden = !text; } }
    function button(key, action, props = {}) { return h("button", { type: "button", class: "lc-button tw-button", text: t(key), onclick: action, ...props }); }
    function label(key, input, vars) { return h("label", { class: "tw-field" }, h("span", { text: t(key, vars) }), input); }
    function check(key, checked, action) {
      const input = h("input", { type: "checkbox", onchange: event => action(event.target.checked) }); input.checked = checked;
      return h("label", { class: "tw-check" }, input, h("span", { text: t(key) }));
    }
    function select(options, value, action, name) {
      const input = h("select", { "aria-label": name, onchange: event => action(event.target.value) }, options.map(item => h("option", { value: item.value, text: item.label })));
      input.value = String(value ?? ""); return input;
    }
    function heading(key, extra) { return h("div", { class: "tw-heading" }, h("h2", { text: t(key) }), extra); }
    function table(headers, body) { return h("div", { class: "tw-table-scroll" }, h("table", {}, h("thead", {}, h("tr", {}, headers.map(text => h("th", { scope: "col", text })))), body)); }
    function card(...children) { return h("section", { class: "lc-card tw-card" }, ...children); }
    function invalidate(clear = true) {
      version++; clearTimeout(timer); timer = null; lastStarted = null; running = false; tuning = null;
      if (clear) { latest = null; cache = {}; display = {}; log = []; times = []; }
    }
    function saveSettings() { try { root.localStorage?.setItem("lab-twin-tuning", JSON.stringify(settings)); } catch { /* Storage can be unavailable. */ } }

    function build() {
      clearPreview(); nodes = {};
      const cameras = (state?.cameras || []).map(item => ({ value: item.id, label: item.label || item.id }));
      nodes.camera = select(cameras.length ? cameras : [{ value: "", label: t("noCamera") }], state?.camera || "", value => replaceDraft(() => mutate("source", { camera: value })), t("camera"));
      nodes.profiles = select([{ value: "", label: t("noSavedProfile") }, ...(state?.profiles || []).map(item => ({ value: item.id, label: item.name }))], state?.selected_profile || "", value => {
        if (value) replaceDraft(() => mutate("profile/select", { id: value }));
      }, t("profile"));
      nodes.refresh = button("refresh", refresh);
      nodes.monitorTab = button("monitor", () => switchSection("monitor"), { role: "tab" });
      nodes.calibrationTab = button("calibration", () => switchSection("calibration"), { role: "tab" });
      nodes.message = h("p", { class: "tw-message", role: "status", text: errorMessage, hidden: !errorMessage });
      nodes.warning = h("p", { class: "tw-message", role: "status", text: state?.warning || "", hidden: !state?.warning });
      nodes.discard = button("discard", () => {
        if (busy || loading) return;
        const action = pendingAction; pendingAction = null; nodes.confirm.hidden = true; render(); action?.();
      });
      nodes.cancel = button("cancel", () => { pendingAction = null; nodes.confirm.hidden = true; syncSelection(); render(); });
      nodes.confirm = h("div", { class: "tw-confirm", hidden: !pendingAction }, h("span", { text: t("replace") }), nodes.discard, nodes.cancel);
      nodes.monitor = h("div", { class: "tw-section" }); nodes.calibration = h("div", { class: "tw-section" });
      host.replaceChildren(h("div", { class: "tw-toolbar" }, h("div", { class: "tw-tabs", role: "tablist", "aria-label": "Digital Twin" }, nodes.monitorTab, nodes.calibrationTab),
        label("camera", nodes.camera), label("profile", nodes.profiles), nodes.refresh), nodes.warning, nodes.message, nodes.confirm, nodes.monitor, nodes.calibration);
      buildMonitor(); buildCalibration(); render(); startPreview();
    }

    function media(name) {
      const image = h("img", { alt: t("frame"), hidden: true });
      const overlay = root.document.createElementNS("http://www.w3.org/2000/svg", "svg");
      overlay.setAttribute("preserveAspectRatio", "xMidYMid meet"); overlay.setAttribute("aria-hidden", "true");
      const placeholder = h("span", { class: "tw-placeholder", text: t("waiting") });
      nodes[name] = { image, overlay, placeholder };
      return h("div", { class: "tw-media" }, image, overlay, placeholder);
    }
    function buildMonitor() {
      nodes.sampling = button("start", toggleSampling, { class: "lc-button tw-button tw-primary" });
      nodes.rate = select(RATES.map(value => ({ value, label: value + " Hz" })), rate, value => { rate = Number(value); if (running || tuning?.searching) schedule(); }, t("rate"));
      nodes.sampleState = h("span", { class: "tw-state", role: "status" });
      const tools = h("div", { class: "tw-row" }, nodes.sampling, label("rate", nodes.rate), nodes.sampleState);
      const frame = media("sampleMedia");
      nodes.preview = h("img", { alt: t("preview"), hidden: true });
      nodes.previewInset = h("div", { class: "tw-inset" }, nodes.preview, h("span", { text: t("preview") })); frame.append(nodes.previewInset);
      nodes.time = h("strong"); nodes.effective = h("strong"); nodes.processing = h("strong"); nodes.dimensions = h("span", { class: "tw-muted" });
      const stats = h("div", { class: "tw-stats" }, ...["time", "effective", "processing"].map(key => h("div", {}, h("span", { text: t(key) }), nodes[key])));
      nodes.tuneNow = button("tuneNow", () => sample(true));
      nodes.tuneFrames = select(Array.from({ length: 8 }, (_, i) => ({ value: i + 1, label: String(i + 1) })), settings.maxFrames, value => { settings.maxFrames = Number(value); saveSettings(); }, t("frames"));
      nodes.preset = h("strong"); nodes.tuneStatus = h("p", { class: "tw-muted", role: "status" });
      const tune = h("details", { class: "tw-fold", open: true }, h("summary", { text: t("tune") }),
        h("div", { class: "tw-row" }, check("automatic", settings.automatic, value => { settings.automatic = value; saveSettings(); }), label("frames", nodes.tuneFrames), nodes.tuneNow),
        h("p", { class: "tw-muted" }, t("preset") + " · ", nodes.preset), nodes.tuneStatus);
      const cameraCard = card(heading("frame", check("outlines", outlines, value => { outlines = value; paintSample(); })), tools, frame, nodes.dimensions, stats, tune);
      nodes.axis = {};
      const values = h("div", { class: "tw-readings" }, AXES.map(axis => {
        const value = h("strong"), status = h("span", { class: "tw-axis-status" }); nodes.axis[axis] = { value, status };
        return h("div", { class: "tw-reading" }, h("span", { text: t("axis." + axis) }), h("div", {}, value, h("span", { class: "tw-unit", text: "mm" })), status);
      }));
      nodes.xyDot = h("span", { class: "tw-point", hidden: true }); nodes.xyLabel = h("span"); nodes.xyPending = h("p", { class: "tw-muted", text: t("pending") });
      nodes.xyMap = h("div", { class: "tw-xy", role: "img", "aria-label": t("xy") }, nodes.xyDot, h("span", { class: "tw-map-zero", text: "0" }), h("span", { class: "tw-map-x", text: "X →" }), h("span", { class: "tw-map-y", text: "Y ↑" }));
      nodes.insertDot = h("span", { class: "tw-point", hidden: true }); nodes.insertLabel = h("span"); nodes.insertPending = h("p", { class: "tw-muted", text: t("pending") });
      nodes.insertMap = h("div", { class: "tw-insert-track", role: "img", "aria-label": t("insert") }, nodes.insertDot);
      nodes.recovery = h("p", { class: "tw-muted" });
      const positionCard = card(heading("positions"), values, h("div", { class: "tw-map-title" }, h("h3", { text: t("xy") }), nodes.xyLabel), nodes.xyPending, nodes.xyMap,
        h("div", { class: "tw-map-title" }, h("h3", { text: t("insert") }), nodes.insertLabel), nodes.insertPending,
        h("div", { class: "tw-map-title tw-muted" }, h("span", { text: t("out") }), h("span", { text: t("in") })), nodes.insertMap, nodes.recovery);
      nodes.markers = h("div", { class: "tw-markers" }); nodes.tracks = h("tbody"); nodes.logs = h("tbody"); nodes.count = h("span", { class: "tw-muted" });
      const details = h("details", { class: "lc-card tw-card tw-fold" }, h("summary", { text: t("details") }), table([t("axis"), t("pair"), t("estimate"), t("offtrack")], nodes.tracks));
      nodes.monitor.append(h("div", { class: "tw-monitor-grid" }, cameraCard, positionCard), card(heading("tags"), nodes.markers), details,
        card(heading("log", nodes.count), table([t("time"), "X (mm)", "Y (mm)", "Insert (mm)", t("tags"), t("processing")], nodes.logs)));
    }
    function buildCalibration() {
      nodes.profileFile = h("input", { type: "file", accept: ".json,application/json", "aria-label": t("profileFile"), hidden: true, onchange: uploadFile });
      nodes.upload = button("upload", () => {
        if (nodes.upload.disabled) return;
        clearTimeout(editTimer); editTimer = null; nodes.profileFile.click();
      });
      nodes.profileActions = [];
      const savedRows = (state?.profiles || []).map(item => {
        const selected = item.id === state?.selected_profile;
        const load = button("load", () => replaceDraft(async () => {
          if (await mutate("profile/select", { id: item.id })) api.notify(t("loaded"), "info");
        }), { "aria-label": t("loadNamed", { name: item.name }) });
        const remove = button("delete", async () => {
          if (await mutate("profile/delete", { id: item.id })) api.notify(t("deleted"), "info");
        }, { "aria-label": t("deleteNamed", { name: item.name }) });
        nodes.profileActions.push(load, remove);
        return h("li", { class: "tw-profile-row" + (selected ? " tw-profile-selected" : ""), "aria-current": selected ? "true" : null },
          h("span", { class: "tw-profile-name", text: item.name }),
          h("div", { class: "tw-row" }, selected ? h("span", { class: "tw-muted", text: t("selected") }) : null, load, remove));
      });
      const savedProfiles = card(heading("profiles", nodes.upload), nodes.profileFile,
        savedRows.length ? h("ul", { class: "tw-profile-list", "aria-label": t("profiles") }, savedRows) : h("p", { class: "tw-muted", text: t("noSavedProfile") }));
      nodes.name = h("input", { type: "text", maxlength: 120, "aria-label": t("name"), oninput: formChanged, onchange: queueEdit });
      nodes.name.value = formDraft?.name ?? profile()?.name ?? ""; nodes.travel = {};
      const fields = AXES.map(axis => {
        const field = h("input", { type: "number", min: "0.000001", max: "1000000", step: "any", "aria-label": t("travel", { axis: t("axis." + axis) }), oninput: formChanged, onchange: queueEdit });
        field.value = String(formDraft?.travel_mm?.[axis] ?? profile()?.travel_mm?.[axis] ?? ({ x: 45, y: 40, insert: 150 })[axis]); nodes.travel[axis] = field;
        return label("travel", field, { axis: t("axis." + axis) });
      });
      nodes.newProfile = button("new", () => replaceDraft(() => mutate("profile/new", {})));
      nodes.save = button("save", save, { class: "lc-button tw-button tw-primary" }); nodes.download = button("download", download);
      nodes.captured = h("span", { class: "tw-muted" }); nodes.activeName = h("span", { class: "tw-muted" });
      const form = card(h("div", { class: "tw-profile-form" }, label("name", nodes.name), ...fields),
        h("div", { class: "tw-row" }, nodes.newProfile, nodes.save, nodes.download, nodes.captured), nodes.activeName);
      nodes.calPreview = h("img", { alt: t("preview"), hidden: true });
      const snapshot = media("snapMedia"); snapshot.append(nodes.calPreview);
      nodes.snapToggle = check("snapshot", showSnapshot, value => { showSnapshot = value; renderSnapshot(); });
      nodes.snapInfo = h("p", { class: "tw-muted", role: "status" });
      nodes.snapMarkers = h("div", { class: "tw-markers" });
      const cameraCard = card(heading("camera", nodes.snapToggle), snapshot, nodes.snapInfo, nodes.snapMarkers, h("p", { class: "tw-guidance", text: t("guidance") }));
      nodes.endpoints = {}; nodes.fits = {};
      const groups = AXES.map(axis => {
        const fit = h("p", { class: "tw-fit" }); nodes.fits[axis] = fit;
        const parent = { x: "y", y: "desk", insert: "x" }[axis], groups = profile()?.marker_groups || GROUPS;
        const pair = h("div", { class: "tw-endpoint-pair" }, ENDPOINTS.filter(item => item.axis === axis).map(item => {
          const snap = button("capture", () => capture(item.id), { "aria-label": t("capture") + " " + t(item.id) });
          const clear = button("clear", () => mutate("clear", { endpoint: item.id }), { "aria-label": t("clear") + " " + t(item.id) });
          const status = h("p", { class: "tw-muted", role: "status" }), meta = h("p", { class: "tw-muted" }), position = h("span", { class: "tw-endpoint-position" });
          nodes.endpoints[item.id] = { snap, clear, status, meta, position };
          return h("div", { class: "tw-endpoint" }, h("h3", { text: t(item.id) }), position, h("div", { class: "tw-row" }, snap, clear), status, meta);
        }));
        return card(h("h2", { text: t("axis." + axis) }), h("p", { class: "tw-muted", text: t("reference", { moving: (groups[axis] || []).join(" / "), parent: (groups[parent] || []).join(" / ") }) }), pair, fit);
      });
      nodes.calibration.append(savedProfiles, form, h("div", { class: "tw-calibration-grid" }, cameraCard,
        h("div", { class: "tw-endpoint-groups" }, check("captureAuto", captureAuto, value => { captureAuto = value; }), ...groups, h("p", { class: "tw-muted", text: t("partial") }))));
    }

    function syncSelection() {
      nodes.camera.value = state?.camera || ""; nodes.profiles.value = state?.selected_profile || "";
    }
    function render() {
      if (!nodes.monitor) return;
      nodes.monitor.hidden = section !== "monitor"; nodes.calibration.hidden = section !== "calibration";
      for (const [key, tab] of [["monitor", nodes.monitorTab], ["calibration", nodes.calibrationTab]]) { tab.setAttribute("aria-selected", String(section === key)); tab.className = "tw-button tw-tab" + (section === key ? " tw-selected" : ""); }
      const unavailable = busy || loading || !state || Boolean(pendingAction);
      for (const node of [nodes.camera, nodes.profiles, nodes.refresh, nodes.newProfile, nodes.save, nodes.download, nodes.name, ...Object.values(nodes.travel), ...nodes.profileActions]) node.disabled = unavailable;
      nodes.upload.disabled = nodes.profileFile.disabled = unavailable || !state?.camera;
      nodes.discard.disabled = nodes.cancel.disabled = busy || loading;
      nodes.save.disabled = unavailable || !state?.camera;
      nodes.sampling.disabled = unavailable || !state?.camera; nodes.sampling.textContent = t(running ? "pause" : "start");
      nodes.tuneNow.disabled = unavailable || !state?.camera || running || measuring || tuning?.searching || !AXES.some(axis => calibrated(monitorProfile(), axis));
      nodes.previewInset.hidden = Boolean(running || measuring || tuning?.searching);
      nodes.tuneFrames.disabled = Boolean(tuning?.searching);
      setText("sampleState", t(loading ? "loading" : measuring ? "working" : running ? "sampling" : "paused"));
      setText("time", latest ? time(latest.captured_at) : "—"); setText("effective", effectiveRate(times) === null ? "—" : number(effectiveRate(times)) + " Hz");
      setText("processing", finite(latest?.processing_ms) ? Math.round(latest.processing_ms) + " ms" : "—");
      setText("dimensions", latest?.dimensions?.join(" × ") || "");
      setText("preset", latest?.detector_method || t("standard"));
      setText("tuneStatus", tuning?.searching ? t("tuning", { count: tuning.tested, limit: tuning.max_frames }) : tuning && ["complete", "limit", "expired"].includes(tuning.outcome)
        ? t("tuned", { count: tuning.tested, found: tuning.best_axes?.length || 0, total: tuning.calibrated_axes?.length || 0 }) : t("tuneReady"));
      renderReadings(); renderTables(); renderCalibration(); paintSample(); renderSnapshot();
    }
    function axisStatus(axis, name) {
      if (!calibrated(monitorProfile(), name)) return t("pending");
      if (!finite(axis?.value_mm)) return t("unknown");
      const status = axis.held ? t("held", { time: time(axis.recordedAt) }) : t("current");
      return status + (axis.status === "extrapolated" ? " · " + t("outside") : "");
    }
    function renderReadings() {
      for (const axis of AXES) {
        nodes.axis[axis].value.textContent = number(display[axis]?.value_mm); nodes.axis[axis].status.textContent = axisStatus(display[axis], axis);
        nodes.axis[axis].status.setAttribute("data-state", display[axis]?.held || display[axis]?.status === "extrapolated" ? "held" : finite(display[axis]?.value_mm) ? "current" : "unknown");
      }
      const point = mapPosition(display, monitorProfile()); nodes.xyDot.hidden = !point;
      if (point) { nodes.xyDot.style.left = point.x * 100 + "%"; nodes.xyDot.style.bottom = point.y * 100 + "%"; nodes.xyDot.setAttribute("data-held", String(point.held)); }
      const hasXY = calibrated(monitorProfile(), "x") && calibrated(monitorProfile(), "y"); nodes.xyMap.hidden = !hasXY; nodes.xyPending.hidden = hasXY;
      if (hasXY) nodes.xyMap.style.aspectRatio = (monitorProfile().axes.x.max - monitorProfile().axes.x.min) + " / " + (monitorProfile().axes.y.max - monitorProfile().axes.y.min);
      nodes.xyLabel.textContent = hasXY ? monitorProfile().axes.x.max + " × " + monitorProfile().axes.y.max + " mm" : "";
      nodes.xyMap.setAttribute("aria-label", t("xy") + ": " + ["x", "y"].map(axis => axis.toUpperCase() + " " + number(display[axis]?.value_mm) + " mm, " + axisStatus(display[axis], axis)).join("; "));
      const insert = fraction(display.insert?.value_mm, monitorProfile()?.axes?.insert), hasInsert = calibrated(monitorProfile(), "insert");
      nodes.insertDot.hidden = insert === null; nodes.insertMap.hidden = !hasInsert; nodes.insertPending.hidden = hasInsert;
      if (insert !== null) { nodes.insertDot.style.left = insert * 100 + "%"; nodes.insertDot.setAttribute("data-held", String(Boolean(display.insert?.held))); }
      nodes.insertLabel.textContent = hasInsert ? monitorProfile().axes.insert.min + "–" + monitorProfile().axes.insert.max + " mm" : "";
      nodes.insertMap.setAttribute("aria-label", t("insert") + ": " + number(display.insert?.value_mm) + " mm, " + axisStatus(display.insert, "insert"));
      const recoveryKey = "twin.recovery." + latest?.insert_recovery?.status;
      nodes.recovery.textContent = i18n.en[recoveryKey] ? api.t(recoveryKey) : "";
      renderMarkers(nodes.markers, latest?.detections || [], monitorProfile());
    }
    function renderMarkers(container, detections, calibration = profile()) {
      const found = new Set(detections.map(item => item.id));
      container.replaceChildren(...Object.entries(calibration?.marker_groups || GROUPS).flatMap(([group, ids]) => ids.map(id =>
        h("span", { class: "tw-marker" + (found.has(id) ? " tw-seen" : ""), text: t("group." + group) + " " + id,
          title: t(found.has(id) ? "detected" : "missing"), "aria-label": t("group." + group) + " " + id + ": " + t(found.has(id) ? "detected" : "missing") }))));
    }
    function renderTables() {
      const rows = AXES.filter(axis => calibrated(monitorProfile(), axis)).flatMap(axis => (latest?.axes?.[axis]?.tracks || []).map(track => h("tr", {},
        [t("axis." + axis), track.moving_id + " → " + track.reference_id, number(track.estimate_mm), number(track.off_track_px)].map(text => h("td", { text })))));
      nodes.tracks.replaceChildren(...(rows.length ? rows : [h("tr", {}, h("td", { colspan: 4, class: "tw-muted", text: t("noPair") }))]));
      nodes.logs.replaceChildren(...(log.length ? log.map(row => h("tr", {}, h("td", { text: time(row.time) }), ...AXES.map(axis => h("td", {}, number(row.readings[axis]?.value_mm),
        row.readings[axis]?.held ? h("small", { text: t("held", { time: time(row.readings[axis].recordedAt) }) }) : null)), h("td", { text: row.tags }), h("td", { text: Math.round(row.ms) + " ms" })))
        : [h("tr", {}, h("td", { colspan: 6, class: "tw-muted", text: t("waiting") }))]));
      setText("count", t("count", { count: log.length }));
    }
    function renderCalibration() {
      setText("captured", t("captured", { count: Object.keys(profile()?.endpoints || {}).length }));
      setText("activeName", dirty || !state?.selected_profile ? t("draft") : state.profiles.find(item => item.id === state.selected_profile)?.name || profile()?.name || "");
      for (const endpoint of ENDPOINTS) {
        const node = nodes.endpoints[endpoint.id], observation = profile()?.endpoints?.[endpoint.id], status = state?.calibration?.endpoint_status?.[endpoint.id] || profile()?.endpoint_status?.[endpoint.id];
        node.snap.disabled = busy || loading || Boolean(pendingAction) || !state?.camera; node.clear.disabled = busy || loading || Boolean(pendingAction) || !observation; node.clear.hidden = !observation;
        node.position.textContent = (endpoint.high ? profile()?.travel_mm?.[endpoint.axis] ?? "—" : 0) + " mm";
        node.status.textContent = captureMessages[endpoint.id] || status?.message || t("captureNone");
        node.status.setAttribute("data-state", status?.status || "missing");
        node.meta.textContent = observation ? "IDs " + (observation.detections || []).map(item => item.id).join(", ") + " · " + time(observation.captured_at) : "";
      }
      for (const axis of AXES) {
        const fit = state?.calibration?.axis_status?.[axis] || profile()?.axis_status?.[axis];
        nodes.fits[axis].textContent = fit?.message || t("fitPending"); nodes.fits[axis].setAttribute("data-state", fit?.status || "pending");
      }
      nodes.snapInfo.textContent = lastSnap ? (lastSnap.dimensions || lastSnap.image_size_px || []).join(" × ") + " · " + time(lastSnap.captured_at) : t("waiting");
      renderMarkers(nodes.snapMarkers, lastSnap?.detections || []);
    }
    function svg(tag, attributes) {
      const node = root.document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
      return node;
    }
    function paint(media, sample) {
      const image = safeImage(sample?.image), dimensions = sample?.dimensions || sample?.image_size_px;
      media.image.hidden = !image; media.placeholder.hidden = Boolean(image); media.overlay.replaceChildren();
      if (!image) { media.image.removeAttribute("src"); return; }
      if (media.image.getAttribute("src") !== image) media.image.setAttribute("src", image);
      if (!outlines || !Array.isArray(dimensions) || dimensions.length !== 2 || !dimensions.every(value => finite(value) && value > 0)) return;
      media.overlay.setAttribute("viewBox", "0 0 " + dimensions.join(" "));
      const unit = dimensions[0] / 720;
      for (const detection of sample.detections || []) {
        const tag = detectionGeometry(detection); if (!tag) continue;
        const points = tag.corners.map(point => point.join(",")).join(" ");
        media.overlay.append(svg("polygon", { points, fill: "none", stroke: "#171310", "stroke-width": unit * 4 }),
          svg("polygon", { points, fill: "none", stroke: "#FFFFFF", "stroke-width": unit * 2 }),
          svg("circle", { cx: tag.center[0], cy: tag.center[1], r: unit * 2, fill: "#FFFFFF" }));
        const text = svg("text", { x: tag.corners[0][0], y: Math.max(unit * 14, tag.corners[0][1] - unit * 5), fill: "#FFFFFF", stroke: "#171310", "stroke-width": unit * 3, "paint-order": "stroke", "font-size": unit * 16, "font-weight": 700 });
        text.textContent = String(tag.id); media.overlay.append(text);
      }
    }
    function paintSample() { if (nodes.sampleMedia) paint(nodes.sampleMedia, latest); }
    function renderSnapshot() {
      if (!nodes.snapMedia) return;
      paint(nodes.snapMedia, showSnapshot ? lastSnap : null);
      nodes.calPreview.hidden = showSnapshot || !nodes.calPreview.getAttribute("src");
      if (!showSnapshot && nodes.calPreview.getAttribute("src")) nodes.snapMedia.placeholder.hidden = true;
    }
    function pausePreview() {
      previewVersion++; clearTimeout(previewTimer); previewTimer = null;
      if (previewPending) for (const image of [nodes.preview, nodes.calPreview]) image?.removeAttribute("src");
      previewPending = false;
    }
    function clearPreview() {
      pausePreview();
      for (const image of [nodes.preview, nodes.calPreview]) { image?.removeAttribute("src"); if (image) image.hidden = true; }
    }
    function canPreview() { return visible && state?.camera && !busy && !running && !measuring && !tuning?.searching && !root.document?.hidden; }
    function startPreview() {
      if (!canPreview() || previewTimer || previewPending) return;
      const generation = ++previewVersion;
      function update() {
        previewTimer = null;
        if (generation !== previewVersion || !canPreview()) return;
        const image = section === "monitor" ? nodes.preview : nodes.calPreview;
        if (!(section === "calibration" && showSnapshot)) {
          const done = failed => {
            if (generation !== previewVersion || !canPreview()) return;
            previewPending = false; image.hidden = failed || (section === "calibration" && showSnapshot); previewTimer = setTimeout(update, 1000);
            if (section === "calibration" && !showSnapshot) nodes.snapMedia.placeholder.hidden = !failed;
          };
          image.onload = () => done(false); image.onerror = () => done(true); previewPending = true;
          image.setAttribute("src", (api.base || "") + "/api/console/ws/twin/frame?camera=" + encodeURIComponent(state.camera) + "&t=" + Date.now()); image.hidden = false;
          if (section === "calibration") nodes.snapMedia.placeholder.hidden = true;
        } else previewTimer = setTimeout(update, 1000);
      }
      update();
    }
    async function decodeImage(sample) {
      if (!safeImage(sample?.image)) throw new Error(t("imageError"));
      if (typeof root.Image !== "function") return;
      const image = new root.Image(); image.src = sample.image;
      if (typeof image.decode === "function") { try { await image.decode(); } catch { throw new Error(t("imageError")); } }
    }
    function replaceDraft(action) {
      if (busy || loading || pendingAction || !state) { syncSelection(); return; }
      if (!dirty) { action(); return; }
      clearTimeout(editTimer); editTimer = null;
      pendingAction = action; nodes.confirm.hidden = false; syncSelection(); render();
    }
    async function refresh() {
      if (loading || busy) return;
      loading = true; render();
      try { state = await api.request("state"); message(); build(); }
      catch (error) { message(error.message); }
      finally { loading = false; render(); }
    }
    async function mutate(path, data) {
      if (busy || loading || pendingAction) return false;
      clearTimeout(editTimer); editTimer = null;
      const deleting = path === "profile/delete", deletingSelected = deleting && data.id === state?.selected_profile;
      if (!deleting || deletingSelected) invalidate();
      busy = true; pausePreview(); message(); render();
      try {
        state = await api.request(path, data);
        if (deleting) dirty = dirty || deletingSelected;
        else {
          captureMessages = {}; formDraft = null;
          dirty = !["source", "profile/select", "profile/save", "profile/upload"].includes(path);
          if (path !== "profile/edit") { lastSnap = null; showSnapshot = false; }
        }
        if (path === "profile/edit") render();
        else build();
        return true;
      } catch (error) { message(error.message); syncSelection(); return false; }
      finally { busy = false; render(); startPreview(); schedule(); }
    }
    async function uploadFile(event) {
      const input = event.target, file = input.files?.[0]; input.value = "";
      if (!file || busy || loading || pendingAction || !state?.camera) return;
      clearTimeout(editTimer); editTimer = null;
      busy = true; pausePreview(); message(); render();
      let contents;
      try {
        if (file.size > MAX_PROFILE_BYTES) throw new Error(t("fileSize"));
        try { contents = await file.text(); } catch { throw new Error(t("fileRead")); }
        let parsed;
        try { parsed = JSON.parse(contents); } catch { throw new Error(t("fileJson")); }
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(t("fileJson"));
      } catch (error) { contents = null; message(error.message); }
      finally { busy = false; render(); startPreview(); schedule(); }
      if (contents != null) replaceDraft(async () => {
        if (await mutate("profile/upload", { profile: contents })) api.notify(t("uploaded"), "info");
      });
    }
    function formData() {
      const values = travel(Object.fromEntries(AXES.map(axis => [axis, nodes.travel[axis].value])));
      if (!values) { message(t("invalidTravel")); return null; }
      return { name: nodes.name.value.trim(), travel_mm: values };
    }
    function formChanged() {
      dirty = true; clearTimeout(editTimer); editTimer = null;
      formDraft = { name: nodes.name.value, travel_mm: Object.fromEntries(AXES.map(axis => [axis, nodes.travel[axis].value])) };
    }
    function queueEdit() {
      formChanged();
      editTimer = setTimeout(() => { editTimer = null; const data = formData(); if (data) mutate("profile/edit", data); }, 250);
    }
    async function save() {
      const data = formData(); if (!data) return;
      if (await mutate("profile/save", data)) api.notify(t("saved"), "info");
    }
    async function download() {
      if (!profile()) return;
      const data = formData(); if (!data) return;
      if (data.name !== profile().name || JSON.stringify(data.travel_mm) !== JSON.stringify(profile().travel_mm)) {
        if (!await mutate("profile/edit", data)) return;
      }
      clearTimeout(editTimer); editTimer = null;
      const blob = new root.Blob([JSON.stringify(profile(), null, 2) + "\n"], { type: "application/json" });
      const url = root.URL.createObjectURL(blob), link = h("a", { href: url, download: (profile().name || "calibration").replace(/[^a-zA-Z0-9_-]+/g, "-") + ".json" });
      root.document.body.append(link); link.click(); link.remove(); setTimeout(() => root.URL.revokeObjectURL(url), 1000);
    }
    async function capture(endpoint) {
      if (busy || loading) return;
      const data = formData(); if (!data) return;
      if (data.name !== profile()?.name || JSON.stringify(data.travel_mm) !== JSON.stringify(profile()?.travel_mm)) {
        if (!await mutate("profile/edit", data)) return;
      }
      clearTimeout(editTimer); editTimer = null;
      invalidate(); busy = true; pausePreview(); const generation = version; message(t("captureWorking", { endpoint: t(endpoint) })); render();
      let captureApplied = false;
      const hadCapture = Boolean(profile()?.endpoints?.[endpoint]);
      try {
        const result = await api.request("capture", { endpoint, automatic: captureAuto, max_frames: 8 }, { timeout: 45000 });
        if (generation !== version) return;
        state = result.state; dirty = true; captureApplied = true;
        if (result.sample?.image) { await decodeImage(result.sample); if (generation !== version) return; lastSnap = result.sample; showSnapshot = true; }
        const missing = result.tuning?.missing || [];
        captureMessages[endpoint] = [result.accepted === false && hadCapture ? t("captureKept") : "", missing.length ? t("captureMissing", { ids: missing.join(", ") }) : "", result.tuning?.reason || ""].filter(Boolean).join(" ");
        message(); build();
      } catch (error) {
        if (generation !== version) return;
        captureMessages[endpoint] = error.message + (!captureApplied && profile()?.endpoints?.[endpoint] ? " " + t("captureKept") : ""); message(captureMessages[endpoint]);
      } finally {
        busy = false; render(); startPreview();
        if (generation !== version && visible) refresh();
      }
    }
    function switchSection(next) {
      if (next === section) return;
      cancelTuning(); invalidate(false); section = next; clearPreview(); render(); startPreview();
    }
    function cancelTuning() {
      if (!state || (!tuning?.searching && !measuring)) return;
      api.request("tuning/cancel", {}, { timeout: 45000 }).catch(error => { if (visible) message(error.message); });
    }
    function toggleSampling() {
      if (busy || loading || !state) return;
      if (running) { cancelTuning(); invalidate(false); render(); startPreview(); return; }
      running = true; lastStarted = null; times = []; clearPreview(); message(); render(); schedule();
    }
    function schedule() {
      clearTimeout(timer); timer = null;
      const now = Date.now(), delay = lastStarted === null ? 0 : Math.max(0, nextDue(lastStarted, now, rate) - now);
      if (visible && section === "monitor" && !root.document?.hidden && (running || tuning?.searching) && !busy && !loading && !measuring) timer = setTimeout(() => sample(false), delay);
    }
    async function sample(manual = false) {
      if (measuring || busy || loading || !state?.camera || !visible || section !== "monitor" || (manual && running) || (!manual && !running && !tuning?.searching)) return;
      measuring = true; clearTimeout(timer); timer = null; clearPreview();
      const generation = version, started = Date.now(); lastStarted = started; render();
      try {
        const result = await api.request("measure", { automatic: settings.automatic, max_frames: settings.maxFrames, tune: manual }, { timeout: 45000 });
        if (generation !== version || !visible) return;
        if (result.camera && result.camera !== state.camera) return;
        if (result.profile_id && result.profile_id !== monitorProfile()?.id) {
          invalidate(); await refresh(); return;
        }
        await decodeImage(result); if (generation !== version || !visible) return;
        latest = result; const reading = readings(cache, result.axes, monitorProfile(), result.captured_at);
        cache = reading.cache; display = reading.display; tuning = result.tuning || result.tuning_policy || null;
        times = [...times.slice(-7), started];
        log = [{ time: result.captured_at, readings: display, tags: result.detections?.length || 0, ms: result.processing_ms || 0 }, ...log].slice(0, 12); message();
      } catch (error) {
        if (generation !== version) return;
        display = readings(cache, {}, monitorProfile()).display; tuning = null; message(error.message);
      } finally {
        measuring = false; render(); startPreview();
        schedule();
      }
    }
    return {
      id: "twin", i18n,
      mount(node, shellApi) {
        host = node; api = shellApi; build();
        api.onLanguage(() => build());
        root.document.addEventListener?.("visibilitychange", () => {
          if (root.document.hidden) { cancelTuning(); version++; clearTimeout(timer); clearPreview(); }
          else if (visible) { startPreview(); schedule(); }
        });
      },
      show() { visible = true; refresh(); startPreview(); schedule(); },
      hide() { visible = false; cancelTuning(); invalidate(false); clearPreview(); clearTimeout(editTimer); editTimer = null; render(); },
      _state: () => ({ state, section, running, latest, display, log, busy, measuring, dirty }),
    };
  }
  return { workspace, i18n, AXES, ENDPOINTS, RATES, logic: { readings, calibrated, fraction, mapPosition, travel, effectiveRate, nextDue, safeImage, detectionGeometry, preferences, number } };
});
