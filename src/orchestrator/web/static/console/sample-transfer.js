/* Sample Transfer uses server measurements and bounded stage commands. */
(function (root, factory) {
  const exported = factory(root);
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root.LabConsole) root.LabConsole.register(exported.workspace());
})(typeof window === "undefined" ? globalThis : window, function (root) {
  "use strict";
  const ENDPOINTS = ["fully_out", "fully_in"];
  const ACTIVE = ["moving", "measuring", "settling"];
  const MAX_UPLOAD = 128 * 1024;
  const i18n = {
    en: {
      title: "Sample Transfer", operate: "Operate", calibrationTab: "Calibration", calibration: "Named positions", profiles: "Saved profiles",
      extract: "Extract Sample", insert: "Insert Sample", stop: "STOP", stopLabel: "Stop stage motion",
      fully_out: "Fully out", fully_in: "Fully in", motionNote: "Moves Insert between the endpoints calibrated in Position Finder.",
      take: "Take control", release: "Release", demo: "DEMO", reference: "Position Finder calibration",
      position: "Position Finder coordinates", measure: "Measure now", source: "{name} · {camera}",
      noSource: "Select a calibrated camera in Position Finder.", openFinder: "Open Position Finder", optical: "Optical coordinates · mm",
      current: "Current position", target: "Target", remaining: "Distance to target", tolerance: "Tolerance {value} mm",
      idle: "Ready", measuring: "Measuring…", moving: "Moving…", settling: "Settling…", stopping: "Stopping…", completed: "Complete", stopped: "Stopped", failed: "Failed", loading: "Loading…",
      "phase.preparing": "Setting 1/8 stepping…", "phase.measuring": "Reading position…", "phase.learning": "Checking pulse scale…", "phase.moving": "Moving…", "phase.verifying": "Confirming position…",
      hybridScale: "1/{step} step · {value} pulses/mm", nominal: "nominal",
      unavailable: "Sample Transfer is unavailable.", controlNeeded: "Take stage control to move or record positions.",
      setupNeeded: "Select a saved Position Finder profile with both Insert endpoints calibrated.",
      new: "New profile", name: "Profile name", defaultName: "Sample positions", save: "Save profile",
      guide: "Move the stage to a position, enter its name, then record and save. These positions are stored for future motion features.",
      record: "Record position", retake: "Record again", positionName: "Position name", requiredPositionName: "Enter a position name.", noPositions: "No positions recorded.", progress: "{count} positions recorded", positionCount: "{count} positions",
      manual: "Manual stage adjustment", stepSize: "Each step sends 10 pulses.", left: "← Left", right: "Right →", down: "↓ Down", up: "Up ↑", out: "← Out", in: "In →",
      x: "X", y: "Y", insertAxis: "Insert", newHint: "Create a profile to record positions.",
      upload: "Upload JSON", download: "Download", remove: "Delete", select: "Select", load: "Load draft", selected: "Selected", complete: "Complete", partial: "Partial", empty: "No saved profiles.",
      file: "Calibration JSON file", fileSize: "Profile file must be 128 KiB or smaller.", fileRead: "Could not read the profile file.", fileJson: "Select a valid JSON profile file.",
      requiredName: "Enter a profile name.", dirty: "Unsaved name", saved: "Profile saved.", uploaded: "Profile uploaded.", deleted: "Profile deleted.",
      discard: "Discard the unsaved name?", deleteConfirm: "Delete {name}?", confirm: "Continue", cancel: "Cancel", dismiss: "Dismiss error",
    },
    zh: {
      title: "样品转移", operate: "操作", calibrationTab: "校准", calibration: "命名位置", profiles: "已存配置",
      extract: "取出样品", insert: "插入样品", stop: "停止", stopLabel: "停止位移台",
      fully_out: "完全移出", fully_in: "完全移入", motionNote: "插入轴在 Position Finder 校准的两个端点之间移动。",
      take: "获取控制权", release: "释放", demo: "演示", reference: "Position Finder 校准",
      position: "Position Finder 坐标", measure: "测量", source: "{name} · {camera}",
      noSource: "先在 Position Finder 中选择已校准的相机。", openFinder: "打开 Position Finder", optical: "图像坐标 · mm",
      current: "当前位置", target: "目标", remaining: "距目标", tolerance: "误差范围 {value} mm",
      idle: "就绪", measuring: "测量中…", moving: "移动中…", settling: "等待静止…", stopping: "停止中…", completed: "完成", stopped: "已停止", failed: "失败", loading: "加载中…",
      "phase.preparing": "设置 1/8 细分…", "phase.measuring": "读取位置…", "phase.learning": "检查脉冲换算…", "phase.moving": "移动中…", "phase.verifying": "确认位置…",
      hybridScale: "1/{step} 细分 · {value} 脉冲/mm", nominal: "标称值",
      unavailable: "样品转移不可用。", controlNeeded: "先获取位移台控制权，再移动或记录位置。",
      setupNeeded: "选择已校准插入轴两个端点的 Position Finder 配置。",
      new: "新建配置", name: "配置名称", defaultName: "样品位置", save: "保存配置",
      guide: "移动位移台到目标位置，输入名称，再记录并保存。位置仅供以后实现移动功能使用。",
      record: "记录位置", retake: "重新记录", positionName: "位置名称", requiredPositionName: "请输入位置名称。", noPositions: "尚未记录位置。", progress: "已记录 {count} 个位置", positionCount: "{count} 个位置",
      manual: "手动调整位移台", stepSize: "每次发送 10 个脉冲。", left: "← 左", right: "右 →", down: "↓ 下", up: "上 ↑", out: "← 出", in: "入 →",
      x: "X", y: "Y", insertAxis: "插入", newHint: "新建配置后，记录位置。",
      upload: "上传 JSON", download: "下载", remove: "删除", select: "选择", load: "加载草稿", selected: "已选", complete: "完整", partial: "未完成", empty: "没有已存配置。",
      file: "校准 JSON 文件", fileSize: "配置文件不得超过 128 KiB。", fileRead: "无法读取配置文件。", fileJson: "请选择有效的 JSON 配置文件。",
      requiredName: "请输入配置名称。", dirty: "名称未保存", saved: "配置已保存。", uploaded: "配置已上传。", deleted: "配置已删除。",
      discard: "放弃未保存的名称？", deleteConfirm: "删除 {name}？", confirm: "继续", cancel: "取消", dismiss: "关闭错误",
    },
  };
  const number = value => typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "—";

  function workspace(environment = root) {
    let api, host, state = null, visible = false, timer = null, polling = false, pending = false, measuring = false;
    let available = false, generation = 0, stopping = 0, ownMotion = null, error = "", formName = "", dirty = false, confirmation = null;
    let nodes = {}, profileSignature = "", positionSignature = "", positionName = "";
    let section = "operate";
    const h = (tag, props, ...children) => api.el(tag, props, ...children);
    const t = (key, values = {}) => (i18n[api.lang?.() || "en"]?.[key] || i18n.en[key] || key).replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
    const busy = () => pending || measuring || stopping > 0 || state?.operation?.settling === true || ACTIVE.includes(state?.operation?.status);
    const owns = () => Boolean(state?.demo || state?.has_control);
    const writable = () => available && owns() && !busy();
    const button = (key, action, role, extra = "") => h("button", { type: "button", class: "st-button " + extra, text: t(key), "data-role": "st-" + role, onclick: action });
    const field = (key, input) => h("label", { class: "st-field" }, h("span", { text: t(key) }), input);
    const setText = (node, value) => { if (node.textContent !== value) node.textContent = value; };
    function issue(value) { error = value?.message || String(value); render(); }
    function apply(value, resetName = false) {
      if (state?.draft?.id !== value?.draft?.id) positionName = "";
      state = value; available = true;
      if (resetName || !dirty) { formName = state?.draft?.name || ""; dirty = false; }
      if (ownMotion && !ownMotion.pending && (state?.operation?.mine === false || (ownMotion.id && state?.operation?.id && ownMotion.id !== state.operation.id) || !ACTIVE.includes(state?.operation?.status))) ownMotion = null;
      render();
    }
    function ask(message, action) { confirmation = { message, action }; render(); nodes.confirmYes.focus?.(); }
    function replaceName(action) { if (dirty) ask(t("discard"), action); else return action(); }
    function build() {
      nodes = {}; profileSignature = positionSignature = "";
      nodes.control = button("take", control, "control");
      nodes.demo = h("span", { class: "st-badge", text: t("demo"), hidden: true });
      nodes.sectionTabs = {};
      for (const [key, label] of [["operate", "operate"], ["calibration", "calibrationTab"]]) {
        const tab = button(label, () => { section = key; render(); }, "tab-" + key, "st-tab");
        tab.setAttribute("id", "st-tab-" + key); tab.setAttribute("role", "tab");
        tab.setAttribute("aria-controls", "st-panel-" + key);
        nodes.sectionTabs[key] = tab;
      }
      nodes.calibrationStop = button("stop", () => stopMotion(), "stop-calibration", "st-stop");
      nodes.calibrationStop.setAttribute("aria-label", t("stopLabel"));
      const toolbar = h("div", { class: "st-toolbar" },
        h("div", { class: "st-tabs", role: "tablist", "aria-label": t("title") }, ...Object.values(nodes.sectionTabs)),
        h("div", { class: "st-row" }, nodes.demo, nodes.control, nodes.calibrationStop));
      nodes.reference = h("span", { class: "st-muted", "data-role": "st-reference" });
      const actions = ENDPOINTS.map(id => {
        const move = button(id === "fully_out" ? "extract" : "insert", () => moveTo(id), "move-" + id, "st-primary st-move");
        const stop = button("stop", () => stopMotion(), "stop-" + id, "st-stop"); stop.setAttribute("aria-label", t("stopLabel"));
        const target = h("span", { class: "st-action-target" }); nodes[id] = { move, stop, target };
        return h("div", { class: "st-action" }, h("div", { class: "st-action-buttons" }, move, stop), target);
      });
      nodes.status = h("strong", { "data-role": "st-status" });
      nodes.message = h("span", { class: "st-muted" });
      nodes.motionScale = h("span", { class: "st-motion-scale", "data-role": "st-motion-scale", hidden: true });
      nodes.distance = h("span", { class: "st-distance", "data-role": "st-distance" });
      nodes.operation = h("div", { class: "st-operation", role: "status", "aria-live": "polite" },
        h("span", { class: "st-status-dot", "aria-hidden": "true" }), nodes.status, nodes.message, nodes.motionScale, nodes.distance);
      nodes.help = h("p", { class: "st-note", "data-role": "st-help" });
      nodes.measure = button("measure", measure, "measure");
      nodes.coordinates = {};
      const coordinates = ["x", "y", "insert"].map(axis => {
        const value = h("strong", { "data-role": "st-position-" + axis, text: "—" }); nodes.coordinates[axis] = value;
        return h("div", { class: "st-coordinate" }, h("span", { text: t(axis === "insert" ? "insertAxis" : axis) }), value, h("small", { text: "mm" }));
      });
      nodes.source = h("span", { class: "st-muted", "data-role": "st-source" });
      nodes.error = h("span", { "data-role": "st-error-message" });
      nodes.errorBox = h("div", { class: "st-error", role: "alert", "data-role": "st-error", hidden: true }, nodes.error,
        button("dismiss", () => { error = ""; render(); }, "dismiss"));
      nodes.confirmText = h("span");
      nodes.confirmYes = button("confirm", () => { const action = confirmation?.action; confirmation = null; render(); return action?.(); }, "confirm", "st-primary");
      nodes.confirm = h("div", { class: "st-confirm", role: "alertdialog", "aria-label": t("confirm"), hidden: true }, nodes.confirmText,
        h("div", { class: "st-row" }, nodes.confirmYes, button("cancel", () => { confirmation = null; render(); }, "cancel")));
      const operationCard = h("section", { class: "st-card st-transfer", "aria-labelledby": "st-title" },
        h("h1", { id: "st-title", text: t("title") }),
        h("p", { class: "st-muted", text: t("motionNote") }),
        h("div", { class: "st-reference" }, h("span", { text: t("reference") }), nodes.reference),
        h("div", { class: "st-actions" }, actions), nodes.help);
      const positionCard = h("section", { class: "st-card st-position", "aria-labelledby": "st-position-title" },
        h("div", { class: "st-heading" }, h("h2", { id: "st-position-title", text: t("position") }), nodes.measure),
        h("div", { class: "st-coordinates" }, coordinates), h("div", { class: "st-source-row" }, nodes.source,
          h("a", { href: "#twin", class: "st-link", text: t("openFinder") })), nodes.operation);
      nodes.name = h("input", { type: "text", maxlength: "120", "data-role": "st-name", value: formName, oninput: event => {
        formName = event.target.value; dirty = formName !== (state?.draft?.name || ""); render();
      } });
      nodes.newProfile = button("new", () => replaceName(() => mutate("profile/new", { name: t("defaultName") }, true)), "new");
      nodes.save = button("save", save, "save", "st-primary");
      nodes.dirty = h("span", { class: "st-muted", text: t("dirty"), hidden: true });
      nodes.progress = h("span", { class: "st-muted", "data-role": "st-progress" });
      nodes.positionName = h("input", { type: "text", maxlength: "120", "data-role": "st-position-name", value: positionName, oninput: event => {
        positionName = event.target.value; render();
      } });
      nodes.record = button("record", recordPosition, "record", "st-primary");
      nodes.positions = h("ul", { class: "st-positions", "data-role": "st-positions" });
      nodes.noPositions = h("p", { class: "st-muted", text: t("noPositions") });
      nodes.jogs = [];
      const jogs = [["top", "x", "left", "right"], ["bottom", "y", "down", "up"], ["insert", "insertAxis", "out", "in"]].map(([motor, axis, cw, ccw]) => {
        const buttons = [[cw, "cw"], [ccw, "ccw"]].map(([label, direction]) => {
          const node = button(label, () => jog(motor, direction), "jog-" + motor + "-" + direction); nodes.jogs.push(node); return node;
        });
        return h("div", { class: "st-jog-axis" }, h("span", { text: t(axis) }), ...buttons);
      });
      const calibration = h("section", { class: "st-card st-calibration", "aria-labelledby": "st-calibration-title" },
        h("div", { class: "st-heading" }, h("h2", { id: "st-calibration-title", text: t("calibration") }), nodes.newProfile),
        h("p", { class: "st-muted", text: t("guide") }),
        h("div", { class: "st-name-row" }, field("name", nodes.name), nodes.save),
        h("div", { class: "st-row st-progress-row" }, nodes.progress, nodes.dirty),
        h("div", { class: "st-name-row" }, field("positionName", nodes.positionName), nodes.record),
        nodes.noPositions, nodes.positions,
        h("div", { class: "st-manual" }, h("div", { class: "st-heading" }, h("h3", { text: t("manual") }), h("span", { class: "st-muted", text: t("stepSize") })),
          h("div", { class: "st-jogs" }, jogs)));
      nodes.uploadFile = h("input", { type: "file", accept: ".json,application/json", "aria-label": t("file"), "data-role": "st-file", hidden: true, onchange: upload });
      nodes.upload = button("upload", () => nodes.uploadFile.click(), "upload");
      nodes.profileList = h("ul", { class: "st-profiles", "data-role": "st-profiles" });
      nodes.empty = h("p", { class: "st-muted", text: t("empty") });
      const profiles = h("section", { class: "st-card st-library", "aria-labelledby": "st-profiles-title" },
        h("div", { class: "st-heading" }, h("h2", { id: "st-profiles-title", text: t("profiles") }), nodes.upload), nodes.uploadFile, nodes.empty, nodes.profileList);
      nodes.operatePanel = h("div", { id: "st-panel-operate", role: "tabpanel", "aria-labelledby": "st-tab-operate" }, operationCard);
      nodes.calibrationPanel = h("div", { id: "st-panel-calibration", class: "st-lower", role: "tabpanel", "aria-labelledby": "st-tab-calibration" }, calibration, profiles);
      host.replaceChildren(toolbar, nodes.errorBox, nodes.confirm, nodes.operatePanel, positionCard, nodes.calibrationPanel); render();
    }
    function renderProfiles() {
      const catalog = state?.profiles || [];
      const signature = JSON.stringify([catalog, state?.selected_profile, state?.draft?.id]);
      if (profileSignature !== signature) {
        nodes.profileActions = []; nodes.downloads = [];
        nodes.profileList.replaceChildren(...catalog.map(item => {
          const selected = item.id === state?.selected_profile;
          const selectedDraft = selected && item.id === state?.draft?.id;
          const load = selected && !selectedDraft;
          const select = button(selectedDraft ? "selected" : load ? "load" : "select", () => replaceName(() => mutate(load ? "profile/load" : "profile/select", { id: item.id }, true)), "select-" + item.id);
          select._selected = selectedDraft; nodes.profileActions.push(select);
          const remove = button("remove", () => ask(t("deleteConfirm", { name: item.name }), () => mutate("profile/delete", { id: item.id }, true)), "delete-" + item.id);
          nodes.profileActions.push(remove);
          const download = button("download", () => downloadProfile(item), "download-" + item.id); nodes.downloads.push(download);
          return h("li", { class: "st-profile" + (selected ? " st-profile-selected" : "") },
            h("div", { class: "st-profile-title" }, h("strong", { text: item.name }), h("span", { class: "st-muted", text: t("positionCount", { count: item.position_count ?? 0 }) })),
            h("div", { class: "st-row" }, select, download, remove));
        })); profileSignature = signature;
      }
      nodes.empty.hidden = catalog.length > 0;
    }
    function renderPositions() {
      const positions = Object.entries(state?.draft?.positions || {});
      const signature = JSON.stringify(positions);
      if (positionSignature !== signature) {
        nodes.positionActions = [];
        nodes.positions.replaceChildren(...positions.map(([name, capture], index) => {
          const remove = button("remove", () => ask(t("deleteConfirm", { name }), () => mutate("position/delete", { name })), "position-delete-" + index);
          nodes.positionActions.push(remove);
          const values = ["x", "y", "insert"].map(axis => h("span", { class: "st-position-value" },
            h("span", { text: t(axis === "insert" ? "insertAxis" : axis) }), h("strong", { text: number(capture.position_mm?.[axis]) }), h("small", { text: "mm" })));
          const captured = new Date(capture.captured_at);
          const time = Number.isNaN(captured.getTime()) ? "" : captured.toLocaleString(api.lang?.() === "zh" ? "zh-CN" : "en-US");
          return h("li", { class: "st-position-record", "data-role": "st-recorded-" + index },
            h("div", { class: "st-heading" }, h("strong", { text: name }), remove),
            h("div", { class: "st-position-values" }, values), h("time", { class: "st-muted", datetime: capture.captured_at, text: time }));
        }));
        positionSignature = signature;
      }
      nodes.noPositions.hidden = positions.length > 0;
    }
    function render() {
      if (!host || !nodes.control) return;
      nodes.operatePanel.hidden = section !== "operate";
      nodes.calibrationPanel.hidden = section !== "calibration";
      nodes.calibrationStop.hidden = section !== "calibration";
      for (const [key, tab] of Object.entries(nodes.sectionTabs)) {
        tab.setAttribute("aria-selected", String(section === key));
        tab.className = "st-button st-tab" + (section === key ? " st-selected" : "");
      }
      const op = state?.operation, working = busy(), canWrite = writable(), reference = state?.transfer_reference;
      renderProfiles(); renderPositions();
      nodes.demo.hidden = !state?.demo; nodes.control.hidden = Boolean(state?.demo);
      nodes.control.disabled = !available || working;
      setText(nodes.control, t(owns() ? "release" : "take"));
      nodes.measure.disabled = !available || working;
      nodes.operation.setAttribute("data-status", op?.status || "idle");
      const motionPhase = op?.status === "moving" && Object.hasOwn(i18n.en, "phase." + op?.phase) ? "phase." + op.phase : op?.status || "idle";
      setText(nodes.status, t(!available ? state ? "unavailable" : "loading" : stopping ? "stopping" : measuring ? "measuring" : motionPhase));
      setText(nodes.message, op?.message || "");
      const hasScale = ENDPOINTS.includes(op?.target) && op?.microstep === 8 && Number.isFinite(op?.pulses_per_mm) && op.pulses_per_mm > 0;
      nodes.motionScale.hidden = !hasScale;
      setText(nodes.motionScale, hasScale ? t("hybridScale", { step: op.microstep, value: number(op.pulses_per_mm) }) + (op.scale_source === "nominal" ? " · " + t("nominal") : "") : "");
      const target = op?.target_mm;
      const remaining = typeof target === "number" && typeof state?.position?.insert === "number" ? Math.abs(target - state.position.insert) : null;
      setText(nodes.distance, op?.status === "moving" && remaining !== null ? t("remaining") + " " + number(remaining) + " mm" : "");
      setText(nodes.help, !owns() && available ? t("controlNeeded") : !reference?.ready ? reference?.message || t("setupNeeded") : t("tolerance", { value: number(state?.tolerance_mm) }));
      setText(nodes.reference, reference?.source ? t("source", { name: reference.source.name, camera: reference.source.camera }) : t("noSource"));
      for (const axis of ["x", "y", "insert"]) setText(nodes.coordinates[axis], number(state?.position?.[axis]));
      const source = state?.position_source;
      setText(nodes.source, source ? t("source", { name: source.name || source.profile_id || "Position Finder", camera: source.camera }) : t("noSource"));
      for (const id of ENDPOINTS) {
        nodes[id].move.disabled = !canWrite || !reference?.ready;
        nodes[id].stop.disabled = false;
        setText(nodes[id].target, t(id) + " · " + number(reference?.targets_mm?.[id]) + " mm");
      }
      nodes.name.disabled = !canWrite || !state?.draft;
      if (nodes.name.value !== formName) nodes.name.value = formName;
      nodes.newProfile.disabled = !canWrite;
      nodes.save.disabled = !canWrite || !state?.draft;
      nodes.dirty.hidden = !dirty;
      nodes.positionName.disabled = !canWrite || !state?.draft;
      if (nodes.positionName.value !== positionName) nodes.positionName.value = positionName;
      nodes.record.disabled = !canWrite || !state?.draft || !positionName.trim();
      setText(nodes.record, t(Object.hasOwn(state?.draft?.positions || {}, positionName.trim()) ? "retake" : "record"));
      setText(nodes.progress, t("progress", { count: Object.keys(state?.draft?.positions || {}).length }));
      for (const node of nodes.positionActions || []) node.disabled = !canWrite;
      for (const node of nodes.jogs) node.disabled = !canWrite;
      for (const node of nodes.profileActions || []) node.disabled = !canWrite || Boolean(node._selected);
      for (const node of nodes.downloads || []) node.disabled = !available;
      nodes.upload.disabled = nodes.uploadFile.disabled = !canWrite;
      nodes.errorBox.hidden = !error; setText(nodes.error, error);
      nodes.confirm.hidden = !confirmation; setText(nodes.confirmText, confirmation?.message || "");
    }
    async function refresh() {
      if (!visible || polling || pending || measuring) return;
      polling = true; const version = generation;
      try { const value = await api.request("state"); if (visible && version === generation) apply(value); }
      catch (failure) { if (visible && version === generation) { available = false; issue(failure); } }
      finally { polling = false; }
    }
    async function mutate(path, data, resetName = false) {
      if (!writable()) return false;
      pending = true; generation++; error = ""; render();
      try { apply(await api.request(path, data, { timeout: 45000 }), resetName); return true; }
      catch (failure) { issue(failure); return false; }
      finally { pending = false; render(); }
    }
    async function measure() {
      if (!available || busy() || !visible) return;
      measuring = true; const version = ++generation; render();
      try { const value = await api.request("measure", {}, { timeout: 45000 }); if (visible && version === generation) apply(value); }
      catch (failure) { if (visible && version === generation) issue(failure); }
      finally { measuring = false; render(); }
    }
    async function command(path, data) {
      if (!writable()) return;
      const intent = { pending: true, cancelled: false }; ownMotion = intent;
      pending = true; generation++; error = ""; render();
      try {
        const value = await api.request(path, data, { timeout: 45000 });
        intent.id = value?.operation?.id;
        intent.pending = false;
        if (ownMotion === intent) apply(value);
        if (intent.cancelled && value?.operation?.mine !== false) await stopMotion(true);
        else if (path === "jog" && visible && !ACTIVE.includes(value?.operation?.status)) { pending = false; await measure(); }
      } catch (failure) {
        intent.pending = false; issue(failure);
        // An unknown command result can still mean motion started.
        if (intent.cancelled) await stopMotion(true);
      } finally { pending = false; render(); }
    }
    const moveTo = target => command("move", { target });
    const jog = (motor, direction) => command("jog", { motor, direction });
    async function stopMotion(automatic = false, keepalive = false) {
      if (automatic && !ownMotion) return;
      const intent = ownMotion; if (intent) intent.cancelled = true;
      const version = ++generation; stopping++; render();
      try {
        const value = await api.request("stop", {}, { timeout: 45000, keepalive });
        if (version === generation) {
          if (ownMotion === intent && !intent?.pending) ownMotion = null;
          apply(value);
        }
      } catch (failure) { if (version === generation) issue(failure); }
      finally { stopping--; render(); }
    }
    async function control() {
      if (!available || busy()) return;
      pending = true; generation++; render();
      try { await api[owns() ? "release" : "take"]("stage"); pending = false; await refresh(); }
      catch (failure) { issue(failure); }
      finally { pending = false; render(); }
    }
    async function save() {
      if (!formName.trim()) { issue(t("requiredName")); return; }
      if (await mutate("profile/save", { name: formName.trim() }, true)) api.notify?.(t("saved"), "info");
    }
    async function recordPosition() {
      const name = positionName.trim();
      if (!name) { issue(t("requiredPositionName")); return; }
      return mutate("record", { name });
    }
    async function upload(event) {
      const file = event.target.files?.[0]; event.target.value = "";
      if (!file) return;
      if (file.size > MAX_UPLOAD) { issue(t("fileSize")); return; }
      let contents;
      try { contents = await file.text(); } catch { issue(t("fileRead")); return; }
      try { JSON.parse(contents); } catch { issue(t("fileJson")); return; }
      return replaceName(async () => { if (await mutate("profile/upload", { profile: contents }, true)) api.notify?.(t("uploaded"), "info"); });
    }
    async function downloadProfile(item) {
      try {
        const profile = await api.request("profile/" + encodeURIComponent(item.id));
        const url = environment.URL.createObjectURL(new environment.Blob([JSON.stringify(profile, null, 2) + "\n"], { type: "application/json" }));
        const link = h("a", { href: url, download: (item.name || "sample-transfer").replace(/[^a-zA-Z0-9_-]+/g, "-") + ".json" });
        link.click(); environment.setTimeout(() => environment.URL.revokeObjectURL(url), 0);
      } catch (failure) { issue(failure); }
    }
    function leave(keepalive = false) { generation++; if (ownMotion) return stopMotion(true, keepalive); }
    return {
      id: "sample-transfer", i18n: {},
      mount(node, shellApi) {
        host = node; api = shellApi; build(); api.onLanguage?.(build);
        environment.addEventListener?.("blur", () => leave());
        environment.addEventListener?.("pagehide", () => leave(true));
        environment.document?.addEventListener?.("visibilitychange", () => { if (environment.document.hidden) leave(); });
      },
      async show() {
        visible = true; await refresh();
        if (!visible) return;
        if (!timer) timer = environment.setInterval(() => { if (!environment.document?.hidden) refresh(); }, 500);
        await measure();
      },
      hide() { visible = false; environment.clearInterval(timer); timer = null; return leave(); },
      _test: { apply, state: () => state, refresh, ownMotion: () => ownMotion, measure },
    };
  }
  return { workspace, i18n, logic: { number, ENDPOINTS, MAX_UPLOAD } };
});
