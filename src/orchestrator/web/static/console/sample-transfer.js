/* Sample Transfer uses server measurements and bounded stage commands. */
(function (root, factory) {
  const exported = factory(root);
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root.LabConsole) root.LabConsole.register(exported.workspace());
})(typeof window === "undefined" ? globalThis : window, function (root) {
  "use strict";
  const ENDPOINTS = ["fully_out", "fully_in"];
  const STAGE_TARGETS = ["out_stage", "microscope"];
  const ACTIVE = ["moving", "measuring", "settling"];
  const MAX_UPLOAD = 128 * 1024;
  const i18n = {
    en: {
      title: "Sample Transfer", operate: "Operate", calibrationTab: "Calibration", calibration: "Calibration profile", profiles: "Saved profiles",
      extract: "Extract Sample", insert: "Insert Sample", stop: "STOP", stopLabel: "Stop stage motion",
      fully_out: "Fully out", fully_in: "Fully in", motionNote: "Move Insert to 0 for extraction or to its current upper limit for insertion.",
      moveOutStage: "Move Out Stage", moveMicroscope: "Move to Microscope", stageSequence: "Bottom (Y) → Top (X)",
      endpointTitle: "Stage positions", outStagePosition: "Stage Out position", microscopePosition: "Microscope position",
      endpointGuide: "Move X/Y to each position, record it, then save the profile. Insert stays in place during these moves.",
      endpointSetup: "Auto calibrate X/Y in Motion and wait for them to stop before recording.",
      notRecorded: "Not recorded", endpointRecord: "Record position", endpointRetake: "Record again",
      endpointCount: "{count}/2 stage positions",
      take: "Take control", release: "Release", demo: "DEMO", reference: "Position source",
      position: "Stage coordinates", measure: "Refresh", named: "Saved positions", movePosition: "Move to position", noSelected: "Select a saved profile in Calibration.", legacy: "Legacy mm profile — create a rotation profile to move.", sequence: "Bottom (Y) → Top (X) → Insert", source: "{name}",
      noSource: "Stage encoder feedback is unavailable.", waiting: "Waiting for stage encoders",
      current: "Current position", target: "Target", remaining: "Distance to target", tolerance: "Tolerance {value} rot",
      idle: "Ready", measuring: "Measuring…", moving: "Moving…", settling: "Settling…", stopping: "Stopping…", completed: "Complete", stopped: "Stopped", failed: "Failed", loading: "Loading…",
      "phase.preparing": "Setting 1/8 stepping…", "phase.measuring": "Reading position…", "phase.learning": "Checking pulse scale…", "phase.moving": "Moving…", "phase.verifying": "Confirming position…",
      hybridScale: "1/{step} step · {value} pulses/mm", nominal: "nominal",
      unavailable: "Sample Transfer is unavailable.", controlNeeded: "Take stage control to move or record positions.",
      setupNeeded: "Calibrate Insert and set its upper limit in Motion.",
      stageSetupNeeded: "Record stage positions in Calibration, then save and select the profile.",
      new: "New profile", name: "Profile name", save: "Save profile",
      guide: "Move the stage to a position, enter its name, then record and save. Use Operate to move Bottom, Top and Insert in order.",
      record: "Record position", retake: "Record again", positionName: "Position name", requiredPositionName: "Enter a position name.", noPositions: "No positions recorded.", progress: "{count} positions recorded", positionCount: "{count} positions",
      manual: "Manual stage adjustment", stepSize: "Each step moves 0.1 rotations.", left: "← Left", right: "Right →", down: "↓ Down", up: "Up ↑", out: "← Out", in: "In →",
      x: "X", y: "Y", insertAxis: "Insert", newHint: "Create a profile to record positions.",
      upload: "Upload JSON", download: "Download", remove: "Delete", select: "Select", load: "Load draft", selected: "Selected", complete: "Complete", partial: "Partial", empty: "No saved profiles.",
      file: "Calibration JSON file", fileSize: "Profile file must be 128 KiB or smaller.", fileRead: "Could not read the profile file.", fileJson: "Select a valid JSON profile file.",
      requiredName: "Enter a profile name.", dirty: "Unsaved name", saved: "Profile saved.", uploaded: "Profile uploaded.", deleted: "Profile deleted.",
      discard: "Discard the unsaved name?", deleteConfirm: "Delete {name}?", confirm: "Continue", cancel: "Cancel", dismiss: "Dismiss error",
    },
    zh: {
      title: "样品转移", operate: "操作", calibrationTab: "校准", calibration: "校准配置", profiles: "已存配置",
      extract: "取出样品", insert: "插入样品", stop: "停止", stopLabel: "停止位移台",
      fully_out: "完全移出", fully_in: "完全移入", motionNote: "取出时将插入轴移到 0，插入时移到当前上限。",
      moveOutStage: "移出位移台", moveMicroscope: "移到显微镜", stageSequence: "底层 (Y) → 顶层 (X)",
      endpointTitle: "位移台位置", outStagePosition: "位移台移出位置", microscopePosition: "显微镜位置",
      endpointGuide: "将 X/Y 移到各位置，记录后保存配置。移动到这些位置时，插入轴保持不动。",
      endpointSetup: "先在 Motion 自动校准 X/Y，等待停止后再记录。",
      notRecorded: "尚未记录", endpointRecord: "记录位置", endpointRetake: "重新记录",
      endpointCount: "{count}/2 个位移台位置",
      take: "获取控制权", release: "释放", demo: "演示", reference: "位置来源",
      position: "位移台坐标", measure: "刷新", named: "已存位置", movePosition: "移动到位置", noSelected: "在校准中选择配置。", legacy: "旧毫米配置不能移动，请新建转数配置。", sequence: "底层 (Y) → 顶层 (X) → 插入", source: "{name}",
      noSource: "尚未连接位置来源。", waiting: "等待位置来源",
      current: "当前位置", target: "目标", remaining: "距目标", tolerance: "误差范围 {value} rot",
      idle: "就绪", measuring: "测量中…", moving: "移动中…", settling: "等待静止…", stopping: "停止中…", completed: "完成", stopped: "已停止", failed: "失败", loading: "加载中…",
      "phase.preparing": "设置 1/8 细分…", "phase.measuring": "读取位置…", "phase.learning": "检查脉冲换算…", "phase.moving": "移动中…", "phase.verifying": "确认位置…",
      hybridScale: "1/{step} 细分 · {value} 脉冲/mm", nominal: "标称值",
      unavailable: "样品转移不可用。", controlNeeded: "先获取位移台控制权，再移动或记录位置。",
      setupNeeded: "在 Motion 校准插入轴并设置上限。",
      stageSetupNeeded: "在校准页记录位移台位置，再保存并选择配置。",
      new: "新建配置", name: "配置名称", save: "保存配置",
      guide: "移动位移台到目标位置，输入名称，再记录并保存。在操作页依次移动底层、顶层和插入轴。",
      record: "记录位置", retake: "重新记录", positionName: "位置名称", requiredPositionName: "请输入位置名称。", noPositions: "尚未记录位置。", progress: "已记录 {count} 个位置", positionCount: "{count} 个位置",
      manual: "手动调整位移台", stepSize: "每次移动 0.1 转。", left: "← 左", right: "右 →", down: "↓ 下", up: "上 ↑", out: "← 出", in: "入 →",
      x: "X", y: "Y", insertAxis: "插入", newHint: "新建配置后，记录位置。",
      upload: "上传 JSON", download: "下载", remove: "删除", select: "选择", load: "加载草稿", selected: "已选", complete: "完整", partial: "未完成", empty: "没有已存配置。",
      file: "校准 JSON 文件", fileSize: "配置文件不得超过 128 KiB。", fileRead: "无法读取配置文件。", fileJson: "请选择有效的 JSON 配置文件。",
      requiredName: "请输入配置名称。", dirty: "名称未保存", saved: "配置已保存。", uploaded: "配置已上传。", deleted: "配置已删除。",
      discard: "放弃未保存的名称？", deleteConfirm: "删除 {name}？", confirm: "继续", cancel: "取消", dismiss: "关闭错误",
    },
  };
  const number = value => typeof value === "number" && Number.isFinite(value) ? value.toFixed(4) : "—";
  const defaultProfileName = date => "Stage Coordinate Calibration " + [date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"), String(date.getDate()).padStart(2, "0")].join("-");

  function workspace(environment = root) {
    let api, host, state = null, visible = false, timer = null, polling = false, pending = false, measuring = false;
    let available = false, generation = 0, stopping = 0, ownMotion = null, error = "", formName = "", dirty = false, confirmation = null;
    let nodes = {}, profileSignature = "", positionSignature = "", positionName = "";
    let section = "operate", namedSignature = "";
    const h = (tag, props, ...children) => api.el(tag, props, ...children);
    const t = (key, values = {}) => (i18n[api.lang?.() || "en"]?.[key] || i18n.en[key] || key).replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
    const busy = () => pending || measuring || stopping > 0 || state?.operation?.settling === true || ACTIVE.includes(state?.operation?.status);
    const owns = () => Boolean(state?.has_control);
    const writable = () => available && owns() && !busy();
    const hasPosition = () => state?.position_available === true;
    const endpointReady = target => state?.transfer_reference?.ready_targets?.[target] === true;
    const stageReady = target => state?.stage_reference?.ready_targets?.[target] === true;
    const defaultName = () => defaultProfileName(new (environment.Date || Date)());
    const draftName = () => state?.draft ? state.draft_name_is_default || !state.draft.name ? defaultName() : state.draft.name : "";
    const button = (key, action, role, extra = "") => h("button", { type: "button", class: "st-button " + extra, text: t(key), "data-role": "st-" + role, onclick: action });
    const field = (key, input) => h("label", { class: "st-field" }, h("span", { text: t(key) }), input);
    const setText = (node, value) => { if (node.textContent !== value) node.textContent = value; };
    function issue(value) { error = value?.message || String(value); render(); }
    function apply(value, resetName = false) {
      if (state?.draft?.id !== value?.draft?.id) positionName = "";
      state = value; available = true;
      if (resetName || !dirty) { formName = draftName(); dirty = false; }
      if (ownMotion && !ownMotion.pending && (state?.operation?.mine === false || (ownMotion.id && state?.operation?.id && ownMotion.id !== state.operation.id) || !ACTIVE.includes(state?.operation?.status))) ownMotion = null;
      render();
    }
    function ask(message, action) { confirmation = { message, action }; render(); nodes.confirmYes.focus?.(); }
    function replaceName(action) { if (dirty) ask(t("discard"), action); else return action(); }
    function build() {
      nodes = {}; profileSignature = positionSignature = namedSignature = "";
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
      const stageActions = STAGE_TARGETS.map(id => {
        const move = button(id === "out_stage" ? "moveOutStage" : "moveMicroscope", () => moveStage(id), "move-" + id, "st-primary st-move");
        const stop = button("stop", () => stopMotion(), "stop-" + id, "st-stop"); stop.setAttribute("aria-label", t("stopLabel"));
        const target = h("span", { class: "st-action-target", "data-role": "st-target-" + id }); nodes[id] = { move, stop, target };
        return h("div", { class: "st-action" }, h("div", { class: "st-action-buttons" }, move, stop), target);
      });
      nodes.status = h("strong", { "data-role": "st-status" });
      nodes.message = h("span", { class: "st-muted" });
      nodes.motionScale = h("span", { class: "st-motion-scale", "data-role": "st-motion-scale", hidden: true });
      nodes.distance = h("span", { class: "st-distance", "data-role": "st-distance" });
      nodes.operation = h("div", { class: "st-operation", role: "status", "aria-live": "polite" },
        h("span", { class: "st-status-dot", "aria-hidden": "true" }), nodes.status, nodes.message, nodes.motionScale, nodes.distance);
      nodes.help = h("p", { class: "st-note", "data-role": "st-help" });
      nodes.stageHelp = h("p", { class: "st-note", "data-role": "st-stage-help" });
      nodes.measure = button("measure", measure, "measure");
      nodes.coordinates = {};
      const coordinates = ["x", "y", "insert"].map(axis => {
        const value = h("strong", { "data-role": "st-position-" + axis, text: "—" }); nodes.coordinates[axis] = value;
        return h("div", { class: "st-coordinate" }, h("span", { text: t(axis === "insert" ? "insertAxis" : axis) }), value, h("small", { text: "rot" }));
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
        h("div", { class: "st-actions" }, actions), nodes.help,
        h("div", { class: "st-stage-actions" }, h("p", { class: "st-muted", text: t("stageSequence") }),
          h("div", { class: "st-actions" }, stageActions), nodes.stageHelp));
      const positionCard = h("section", { class: "st-card st-position", "aria-labelledby": "st-position-title" },
        h("div", { class: "st-heading" }, h("h2", { id: "st-position-title", text: t("position") }), nodes.measure),
        h("div", { class: "st-coordinates" }, coordinates), h("div", { class: "st-source-row" }, nodes.source), nodes.operation);
      nodes.name = h("input", { type: "text", maxlength: "120", "data-role": "st-name", value: formName, oninput: event => {
        formName = event.target.value; dirty = formName !== draftName(); render();
      } });
      nodes.newProfile = button("new", () => replaceName(() => mutate("profile/new", { name: defaultName() }, true)), "new");
      nodes.save = button("save", save, "save", "st-primary");
      nodes.dirty = h("span", { class: "st-muted", text: t("dirty"), hidden: true });
      nodes.progress = h("span", { class: "st-muted", "data-role": "st-progress" });
      nodes.positionName = h("input", { type: "text", maxlength: "120", "data-role": "st-position-name", value: positionName, oninput: event => {
        positionName = event.target.value; render();
      } });
      nodes.record = button("record", recordPosition, "record", "st-primary");
      nodes.endpoints = {};
      nodes.endpointHelp = h("p", { class: "st-note", text: t("endpointSetup"), "data-role": "st-endpoint-help" });
      const endpoints = STAGE_TARGETS.map(target => {
        const title = t(target === "out_stage" ? "outStagePosition" : "microscopePosition");
        const record = button("endpointRecord", () => mutate("endpoint/record", { target }), "record-" + target, "st-primary");
        const remove = button("remove", () => ask(t("deleteConfirm", { name: title }), () => mutate("endpoint/delete", { target })), "delete-" + target);
        remove.setAttribute("aria-label", title + ": " + t("remove"));
        const values = {};
        const coordinates = ["x", "y"].map(axis => {
          const value = h("strong", { text: "—", "data-role": "st-endpoint-" + target + "-" + axis }); values[axis] = value;
          return h("span", { class: "st-position-value" }, h("span", { text: t(axis === "insert" ? "insertAxis" : axis) }), value, h("small", { text: "rot" }));
        });
        const status = h("span", { class: "st-muted", "data-role": "st-endpoint-status-" + target });
        nodes.endpoints[target] = { record, remove, values, status, title };
        return h("div", { class: "st-endpoint", "data-role": "st-endpoint-" + target },
          h("div", { class: "st-heading" }, h("h3", { text: title }), status),
          h("div", { class: "st-position-values" }, coordinates), h("div", { class: "st-row" }, record, remove));
      });
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
        h("div", { class: "st-name-row" }, field("name", nodes.name), nodes.save),
        h("section", { class: "st-endpoint-calibration" }, h("h3", { text: t("endpointTitle") }),
          h("p", { class: "st-muted", text: t("endpointGuide") }), nodes.endpointHelp, h("div", { class: "st-endpoints" }, endpoints)),
        h("h3", { text: t("named") }), h("p", { class: "st-muted", text: t("guide") }),
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
      nodes.named = h("ul", { class: "st-positions st-named-grid", "data-role": "st-named-positions" });
      nodes.selected = h("span", { class: "st-muted", "data-role": "st-selected-profile" });
      const namedCard = h("section", { class: "st-card st-named" }, h("div", { class: "st-heading" }, h("h2", { text: t("named") }), nodes.selected), h("p", { class: "st-muted", text: t("sequence") }), nodes.named);
      nodes.operatePanel = h("div", { id: "st-panel-operate", role: "tabpanel", "aria-labelledby": "st-tab-operate" }, operationCard, namedCard);
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
            h("div", { class: "st-profile-title" }, h("strong", { text: item.name }), h("span", { class: "st-muted", text: t("positionCount", { count: item.position_count ?? 0 }) + " · " + t("endpointCount", { count: item.stage_position_count ?? 0 }) + (item.compatible === false ? " · " + t("legacy") : "") })),
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
            h("span", { text: t(axis === "insert" ? "insertAxis" : axis) }), h("strong", { text: number((capture.position_rotations || capture.position_mm)?.[axis]) }), h("small", { text: capture.position_rotations ? "rot" : "mm" })));
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
    function renderNamed() {
      const profile = state?.active_profile, compatible = profile?.schema_version === 5 && profile?.unit === "rotations";
      const signature = JSON.stringify([profile, available, owns(), busy(), state?.named_motion_ready]);
      setText(nodes.selected, profile ? profile.name + (compatible ? "" : " · " + t("legacy")) : t("noSelected"));
      if (namedSignature === signature) return;
      nodes.named.replaceChildren(...Object.entries(profile?.positions || {}).map(([name, capture], index) => {
        const move = button("movePosition", () => command("position/move", { name }), "named-move-" + index, "st-primary");
        move.disabled = !writable() || !compatible || !state?.named_motion_ready;
        const values = ["y", "x", "insert"].map(axis => h("span", { class: "st-position-value" }, h("span", { text: t(axis === "insert" ? "insertAxis" : axis) }), h("strong", { text: number((capture.position_rotations || capture.position_mm)?.[axis]) }), h("small", { text: compatible ? "rot" : "mm" })));
        return h("li", { class: "st-position-record" }, h("strong", { text: name }), h("div", { class: "st-position-values" }, values), h("div", { class: "st-row" }, move, button("stop", () => stopMotion(), "named-stop-" + index, "st-stop")));
      }));
      namedSignature = signature;
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
      renderProfiles(); renderPositions(); renderNamed();
      nodes.demo.hidden = !state?.demo; nodes.control.hidden = false;
      nodes.control.disabled = !available || working;
      setText(nodes.control, t(owns() ? "release" : "take"));
      nodes.measure.disabled = !available || working || !hasPosition();
      nodes.operation.setAttribute("data-status", op?.status || "idle");
      const motionPhase = op?.status === "moving" && Object.hasOwn(i18n.en, "phase." + op?.phase) ? "phase." + op.phase : op?.status || "idle";
      setText(nodes.status, t(!available ? state ? "unavailable" : "loading" : stopping ? "stopping" : measuring ? "measuring" : !hasPosition() && motionPhase === "idle" ? "waiting" : motionPhase));
      setText(nodes.message, op?.message || "");
      nodes.motionScale.hidden = !op?.motor;
      setText(nodes.motionScale, op?.motor ? `${op.motor} · ${op.completed_axes || 0}/${op.total_axes || 1}` : "");
      const target = op?.target_rotations;
      const remaining = typeof target === "number" && typeof state?.position?.[({ top: "x", bottom: "y", insert: "insert" })[op?.motor]] === "number" ? Math.abs(target - state.position[({ top: "x", bottom: "y", insert: "insert" })[op?.motor]]) : null;
      setText(nodes.distance, op?.status === "moving" && remaining !== null ? t("remaining") + " " + number(remaining) + " rot" : "");
      setText(nodes.help, !owns() && available ? t("controlNeeded") : !reference?.ready ? reference?.message || t("setupNeeded") : t("tolerance", { value: number(state?.tolerance_rotations) }));
      nodes.help.hidden = false;
      const positionSource = state?.position_source;
      setText(nodes.reference, positionSource ? t("source", { name: positionSource.name || t("reference") }) : t("noSource"));
      for (const axis of ["x", "y", "insert"]) setText(nodes.coordinates[axis], number(hasPosition() ? state?.position?.[axis] : null));
      const source = state?.position_source;
      setText(nodes.source, hasPosition() && source ? t("source", { name: source.name || source.profile_id || t("reference") }) : t("noSource"));
      nodes.source.hidden = !hasPosition();
      for (const id of ENDPOINTS) {
        nodes[id].move.disabled = !canWrite || !endpointReady(id);
        nodes[id].stop.disabled = false;
        setText(nodes[id].target, t(id) + " · " + number(reference?.targets_rotations?.[id]) + " rot");
      }
      for (const id of STAGE_TARGETS) {
        nodes[id].move.disabled = !canWrite || !stageReady(id);
        nodes[id].stop.disabled = false;
        const target = state?.stage_reference?.targets_rotations?.[id];
        setText(nodes[id].target, target ? ["x", "y"].map(axis => t(axis) + " " + number(target[axis]) + " rot").join(" · ") : t("notRecorded"));
        const endpoint = nodes.endpoints[id], capture = state?.draft?.stage_positions?.[id];
        for (const axis of ["x", "y"]) setText(endpoint.values[axis], number(capture?.position_rotations?.[axis]));
        setText(endpoint.status, capture ? t("complete") : t("notRecorded"));
        setText(endpoint.record, t(capture ? "endpointRetake" : "endpointRecord"));
        endpoint.record.setAttribute("aria-label", endpoint.title + ": " + t(capture ? "endpointRetake" : "endpointRecord"));
        endpoint.record.disabled = !canWrite || !state?.endpoint_record_ready || state?.draft?.schema_version !== 5;
        endpoint.remove.disabled = !canWrite || !capture;
        endpoint.remove.hidden = !capture;
      }
      nodes.stageHelp.hidden = !owns() || STAGE_TARGETS.every(stageReady);
      setText(nodes.stageHelp, state?.stage_reference?.message || t("stageSetupNeeded"));
      nodes.name.disabled = !canWrite || !state?.draft;
      nodes.endpointHelp.hidden = !owns() || state?.endpoint_record_ready || state?.draft?.schema_version !== 5;
      if (nodes.name.value !== formName) nodes.name.value = formName;
      nodes.newProfile.disabled = !canWrite;
      nodes.save.disabled = !canWrite || !state?.draft;
      nodes.dirty.hidden = !dirty;
      nodes.positionName.disabled = !canWrite || !state?.draft;
      if (nodes.positionName.value !== positionName) nodes.positionName.value = positionName;
      nodes.record.disabled = !canWrite || !state?.record_ready || state?.draft?.schema_version !== 5 || !positionName.trim();
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
      if (!available || busy() || !visible || !hasPosition()) return;
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
    const moveTo = target => { if (endpointReady(target)) return command("move", { target }); };
    const moveStage = target => { if (stageReady(target)) return command("stage/move", { target }); };
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
      if (!hasPosition()) return;
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
      },
      hide() { visible = false; environment.clearInterval(timer); timer = null; return leave(); },
      _test: { apply, state: () => state, refresh, ownMotion: () => ownMotion, measure },
    };
  }
  return { workspace, i18n, logic: { number, ENDPOINTS, STAGE_TARGETS, MAX_UPLOAD, defaultProfileName } };
});
