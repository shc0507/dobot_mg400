/* Manual objective-turret calibration inside the process workspace. */
(function (root, factory) {
  "use strict";
  const logic = factory();
  if (typeof module === "object" && module.exports) module.exports = logic;
  else root.LabTurret = logic;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  const OBJECTIVES = ["5x", "10x", "20x", "50x"];
  const MAX_UPLOAD = 2 * 1024 * 1024;
  const POLL_MS = 2000;
  const i18n = {
    en: {
      title: "Turret calibration", camera: "Camera3", live: "Live", captured: "Captured {name}",
      new: "New profile", upload: "Upload JSON", download: "Download", remove: "Delete", select: "Select", selected: "Selected",
      profiles: "Saved profiles", empty: "No saved profiles.", name: "Profile name", untitled: "Turret calibration", load: "Load draft",
      draft: "Unsaved draft", saved: "Saved", save: "Save profile", saveUse: "Save & use", start: "Start calibration", finish: "Finish calibration", cancel: "End session",
      lensCalibration: "Lens calibration", noSelection: "No profile selected", selectionUnavailable: "Selected profile unavailable",
      savedSelected: "Calibration profile saved and selected.", savedNotSelected: "Profile saved, but not selected: {message}", notSelected: "The profile was not selected.",
      progress: "{count} of 4 turret positions recorded", objective: "{name} · marker {id}", step: "{name} · marker {id}",
      manual: "Turn the turret by hand to seat {name}. Select Record to save its marker X for turret alignment. This does not record a focus height for this lens.",
      startHelp: "Start releases both motors. Record the 4 turret positions by hand, then record one focused 5× starting point. Calibration does not power the motors.",
      setup: "Objective markers 0–3 are about 8 mm. Keep the current objective marker in focus and fully visible.",
      limitation: "Keep Camera3 fixed. After moving it or changing camera focus or zoom, retake all 4 turret positions and the 5× starting point.",
      record: "Record {name} X", retake: "Retake {name} X", recorded: "Recorded", pending: "Not recorded", review: "All 4 turret positions and the 5× starting point are recorded. Finish calibration, then save the profile.",
      control: "Take microscope control to start calibration.", other: "Another client has an active calibration session.",
      ended: "Session ended. Save the draft to keep these captures.", offline: "Camera3 image is unavailable.",
      noCapture: "No image recorded for this objective.", file: "Profile JSON file", fileSize: "The JSON file must be 2 MiB or smaller.",
      fileJson: "Select a valid JSON profile file.", fileRead: "Could not read the JSON file.", loading: "Loading calibration…", unavailable: "Calibration state is unavailable.",
      incomplete: "Partial", complete: "Complete", clear: "Dismiss error", working: "Working…", savedNotice: "Calibration profile saved.",
      dirty: "Unsaved name", requiredName: "Enter a profile name.", next: "Next: {name}",
      focusSummary: "X {x} px · 3840 px reference width", noCoordinates: "Record this view to save its marker X.",
      initialTitle: "5× starting focus", initialHelp: "Seat 5× and focus on the sample by hand. Record this point for rough positioning only; autofocus will find the precise focus.",
      initialRecord: "Record 5× starting point", initialRetake: "Retake 5× starting point", initialMissing: "Starting point not recorded", initialSaved: "Starting point recorded · Y {y} px", initialAlt: "Recorded 5× starting point",
      sampleView: "Sample focus · live microscope", markerView: "Objective marker · Camera3", sampleOffline: "Microscope image is unavailable.",
      legacy: "Needs 5× starting point", legacyDraft: "Load this older profile to keep its turret X records, then record a new 5× starting point.", heightReady: "Ready", human: "Manual calibration", safeReference: "Turret X + one 5× starting point",
    },
    zh: {
      title: "转盘校准", camera: "Camera3", live: "实时", captured: "已记录 {name}",
      new: "新建配置", upload: "上传 JSON", download: "下载", remove: "删除", select: "选择", selected: "已选中",
      profiles: "已存配置", empty: "没有已存配置。", name: "配置名称", untitled: "转盘校准", load: "加载草稿",
      draft: "未保存草稿", saved: "已保存", save: "保存配置", saveUse: "保存并使用", start: "开始校准", finish: "完成校准", cancel: "结束会话",
      lensCalibration: "物镜校准", noSelection: "未选择配置", selectionUnavailable: "所选配置不可用",
      savedSelected: "校准配置已保存并选中。", savedNotSelected: "配置已保存，但未选中：{message}", notSelected: "未选中配置。",
      progress: "已记录 {count} / 4 个转盘位置", objective: "{name} · 标记 {id}", step: "{name} · 标记 {id}",
      manual: "用手把 {name} 转入定位点。选择记录，保存用于转盘对齐的标记 X。此操作不记录该物镜的焦点高度。",
      startHelp: "开始时释放两个电机。用手记录 4 个转盘位置，再记录 1 个已对焦的 5× 起点。校准不驱动电机。",
      setup: "物镜标记 0–3 约为 8 mm。当前物镜标记必须清晰且完整可见。",
      limitation: "保持 Camera3 固定。移动它或改变摄像头对焦、变焦后，重新记录全部 4 个转盘位置和 5× 起点。",
      record: "记录 {name} X", retake: "重拍 {name} X", recorded: "已记录", pending: "未记录", review: "已记录 4 个转盘位置和 5× 起点。完成校准后保存配置。",
      control: "先获取显微镜控制权，再开始校准。", other: "其他客户端正在校准。",
      ended: "会话已结束。保存草稿以保留图像。", offline: "Camera3 图像不可用。",
      noCapture: "尚未记录此物镜图像。", file: "配置 JSON 文件", fileSize: "JSON 文件不得超过 2 MiB。",
      fileJson: "请选择有效的 JSON 配置文件。", fileRead: "无法读取 JSON 文件。", loading: "正在读取校准…", unavailable: "校准状态不可用。",
      incomplete: "未完成", complete: "已完成", clear: "关闭错误提示", working: "处理中…", savedNotice: "校准配置已保存。",
      dirty: "名称未保存", requiredName: "请填写配置名称。", next: "下一步：{name}",
      focusSummary: "X {x} px · 参考宽度 3840 px", noCoordinates: "记录此图像以保存标记 X。",
      initialTitle: "5× 起始焦点", initialHelp: "用手把 5× 转入定位点，并对焦样品。记录此点仅用于粗定位；自动对焦负责精确对焦。",
      initialRecord: "记录 5× 起点", initialRetake: "重拍 5× 起点", initialMissing: "尚未记录起点", initialSaved: "已记录起点 · Y {y} px", initialAlt: "已记录的 5× 起点",
      sampleView: "样品焦点 · 显微镜实时图像", markerView: "物镜标记 · Camera3", sampleOffline: "显微镜图像不可用。",
      legacy: "需要 5× 起点", legacyDraft: "加载此旧配置以保留转盘 X 记录，再记录新的 5× 起点。", heightReady: "可用", human: "人工校准", safeReference: "转盘 X + 1 个 5× 起点",
    },
  };

  function capture(profile, id) { return profile?.captures?.[String(id)] || null; }
  function count(profile) { return OBJECTIVES.filter((_, id) => capture(profile, id)).length; }
  function complete(profile) { return profile?.schema_version === 5 && count(profile) === 4 && Boolean(profile.initial_5x_capture); }
  function coordinates(record) {
    const points = record?.objective_corners;
    if (!Array.isArray(points) || points.length !== 4 || !points.every(point => Array.isArray(point) && point.length === 2 && point.every(Number.isFinite))) return null;
    const shape = record?.shape;
    if (!Array.isArray(shape) || !shape.every(Number.isFinite) || shape[0] <= 0 || shape[1] <= 0) return null;
    return { x: points.reduce((sum, point) => sum + point[0], 0) / 4 * 3840 / shape[1], y: points.reduce((sum, point) => sum + point[1], 0) / 4 * 3840 / shape[1] };
  }
  function profileId(profile) { return profile?.profile_id || profile?.id || null; }
  function nextObjective(profile) { const id = OBJECTIVES.findIndex((_, index) => !capture(profile, index)); return id < 0 ? 3 : id; }
  function previewSource(record) {
    return typeof record?.preview_jpeg === "string" && /^[A-Za-z0-9+/=\r\n]+$/.test(record.preview_jpeg)
      ? "data:image/jpeg;base64," + record.preview_jpeg : "";
  }
  function translate(lang, key, vars = {}) {
    return (i18n[lang]?.[key] || i18n.en[key] || key).replace(/\{(\w+)\}/g, (_, name) => String(vars[name] ?? ""));
  }

  function create(api, global = globalThis) {
    const h = api.el;
    const t = (key, vars) => translate(api.lang?.() || "en", key, vars);
    const element = h("section", { class: "lc-card tr-card", "data-role": "turret-calibration", "aria-labelledby": "tr-title" });
    const profileSelector = h("div", { class: "tr-lens-calibration", "data-role": "turret-lens-calibration" });
    let state = null, active = false, calibrationVisible = true, timer = null, epoch = 0, busy = false, polling = false, microscopeBusy = false;
    let name = "", nameDirty = false, newDraft = false, dirty = false, current = 0, imageMode = "live", error = "", selectionFailure = "";
    let nodes = {}, cameraTile = null, sampleTile = null, demoImage = null, lastKey = "";
    const draft = () => newDraft ? null : state?.draft;
    const mine = () => state?.session?.active === true && state?.session?.mine === true;
    const setError = value => { error = value?.message || String(value || ""); if (!error) selectionFailure = ""; renderStatus(); };
    const button = (key, onclick, props = {}) => h("button", { type: "button", class: "lc-button", text: t(key), onclick, ...props });
    const role = (key, props = {}) => ({ "data-role": "turret-" + key, ...props });

    function camera() {
      if (state?.demo && state?.camera?.frame_url) {
        if (!demoImage) demoImage = h("img", { alt: t("camera"), class: "tr-image", onerror: () => { if (nodes.imageError) nodes.imageError.hidden = false; }, onload: () => { if (nodes.imageError) nodes.imageError.hidden = true; } });
        return demoImage;
      }
      if (!cameraTile) {
        try { cameraTile = global.LabCameras?.tile("camera3", { api, large: true }) || null; } catch { cameraTile = null; }
      }
      return cameraTile || h("p", { class: "tr-empty", text: t("offline") });
    }

    function sampleCamera() {
      if (!sampleTile) {
        try { sampleTile = global.LabCameras?.tile("microscope", { api, large: true }) || null; } catch { sampleTile = null; }
      }
      return sampleTile || h("p", { class: "tr-empty", text: t("sampleOffline") });
    }

    function build() {
      const record = capture(draft(), current);
      const n = nodes = {};
      const completeProfiles = (state?.profiles || []).filter(item => item.ready === true || item.complete && item.needs_height_calibration === false);
      const selected = state?.selected_profile || "";
      const available = completeProfiles.some(item => profileId(item) === selected);
      n.profileSelect = h("select", { class: "pr-input", "aria-label": t("lensCalibration"), ...role("profile-select"),
        onchange: event => {
          const id = event.target.value;
          event.target.value = state?.selected_profile || "";
          if (id && id !== state?.selected_profile) return mutate("select", { id });
        } },
      ...(!selected || !available ? [h("option", { value: selected, disabled: true, text: t(selected || state?.selection_error ? "selectionUnavailable" : "noSelection") })] : []),
      ...completeProfiles.map(item => h("option", { value: profileId(item), text: item.name })));
      n.profileError = h("span", { class: "tr-selector-error", role: "alert", hidden: true, ...role("profile-error") });
      profileSelector.replaceChildren(h("label", { class: "pr-field" }, t("lensCalibration"), n.profileSelect), n.profileError);
      n.state = h("span", { class: "tr-status", ...role("draft-state") });
      n.name = h("input", { class: "pr-input tr-name", type: "text", maxlength: "80", autocomplete: "off", ...role("name"),
        oninput: event => { name = event.target.value; nameDirty = true; renderStatus(); } });
      n.name.value = name;
      n.new = button("new", startNew, role("new"));
      n.upload = button("upload", () => n.file.click(), role("upload"));
      n.file = h("input", { type: "file", accept: ".json,application/json", hidden: true, "aria-label": t("file"), onchange: upload, ...role("file") });
      n.message = h("div", { class: "tr-error", role: "alert", hidden: true, ...role("error") },
        n.error = h("span"), button("clear", () => setError(""), { class: "lc-button tr-dismiss" }));
      n.progress = h("span", { class: "tr-progress", ...role("progress") });
      n.steps = OBJECTIVES.map((objective, id) => {
        const hasCapture = Boolean(capture(draft(), id));
        return button("objective", () => { current = id; imageMode = hasCapture ? "captured" : "live"; build(); }, {
          text: "", class: "tr-step" + (hasCapture ? " tr-done" : ""), "aria-current": current === id ? "step" : null,
          "aria-label": t("objective", { name: objective, id }) + ", " + t(hasCapture ? "recorded" : "pending"), ...role("step-" + id),
        });
      });
      n.steps.forEach((node, id) => node.append(h("span", { class: "tr-step-index", text: capture(draft(), id) ? "✓" : String(id + 1) }), h("span", { text: OBJECTIVES[id] })));
      n.live = button("live", () => { imageMode = "live"; renderImage(); }, { class: "lc-button tr-view-button", ...role("live") });
      n.captured = button("captured", () => { imageMode = "captured"; renderImage(); }, {
        text: t("captured", { name: OBJECTIVES[current] }), class: "lc-button tr-view-button", disabled: !record, ...role("captured") });
      n.livePane = h("div", { class: "tr-live" }, camera());
      n.still = h("img", { class: "tr-image", alt: t("captured", { name: OBJECTIVES[current] }), ...role("still") });
      n.noCapture = h("p", { class: "tr-empty", text: t("noCapture") });
      n.capturedPane = h("div", { class: "tr-captured" }, n.still, n.noCapture);
      n.imageError = h("p", { class: "tr-image-error", text: t("offline"), hidden: true });
      n.record = button(record ? "retake" : "record", () => mutate("record", { objective_id: current }), {
        text: t(record ? "retake" : "record", { name: OBJECTIVES[current] }), class: "lc-button tr-primary", ...role("record") });
      n.next = button("next", () => { current = nextObjective(draft()); imageMode = "live"; build(); }, {
        text: t("next", { name: OBJECTIVES[nextObjective(draft())] }), ...role("next") });
      n.start = button("start", begin, { class: "lc-button tr-primary", ...role("start") });
      n.finish = button("finish", () => mutate("finish", {}), role("finish"));
      n.cancel = button("cancel", () => mutate("cancel", {}), role("cancel"));
      n.save = button("save", save, { class: "lc-button tr-primary", ...role("save") });
      n.sessionNote = h("p", { class: "tr-note", ...role("session-note") });
      n.focusSummary = h("p", { class: "tr-note", ...role("focus-summary") });
      n.initialRecord = button(draft()?.initial_5x_capture ? "initialRetake" : "initialRecord", () => mutate("record_initial", {}), { class: "lc-button tr-primary", ...role("initial-record") });
      n.initialState = h("p", { class: "tr-note", ...role("initial-state") });
      n.initialImage = h("img", { class: "tr-initial-image", alt: t("initialAlt"), ...role("initial-image") });
      n.switchWarning = h("p", { class: "tr-note", role: "status", ...role("switch-warning") });
      n.selectionError = h("p", { class: "tr-error", role: "alert", hidden: true, ...role("selection-error") });
      n.guidance = h("p", { class: "tr-instruction", text: t("manual", { name: OBJECTIVES[current] }) });
      n.actions = [];
      const rows = (state?.profiles || []).map(item => {
        const id = profileId(item), selected = id === state?.selected_profile;
        const ready = item.ready === true || item.complete && item.needs_height_calibration === false;
        const choose = button(!ready ? "load" : selected ? "selected" : "select", () => mutate(ready ? "select" : "load", { id }), { ...role("select-" + id), disabled: selected && ready });
        const download = button("download", () => downloadProfile(id), role("download-" + id));
        const remove = button("remove", () => mutate("delete", { id }), role("delete-" + id));
        n.actions.push({ choose, download, remove, selected, complete: ready });
        return h("li", { class: "tr-profile" + (selected ? " tr-profile-selected" : ""), "aria-current": selected ? "true" : null },
          h("div", { class: "tr-profile-label" }, h("span", { class: "tr-profile-name", text: item.name }),
            h("small", { text: t(ready ? "heightReady" : item.needs_height_calibration ? "legacy" : "incomplete") })),
          h("div", { class: "tr-row" }, choose, download, remove));
      });
      element.replaceChildren(
        h("div", { class: "lc-card-head tr-heading" }, h("div", { class: "tr-title" }, h("h2", { id: "tr-title", text: t("title") }), n.state),
          h("div", { class: "tr-row" }, n.new, n.upload)), n.file, n.message,
        h("div", { class: "tr-calibration-banner" }, h("span", { text: t("human") }), h("strong", { text: t("safeReference") })),
        h("div", { class: "tr-name-row" }, h("label", { class: "pr-field" }, t("name"), n.name), n.save, n.progress),
        h("div", { class: "tr-session" }, n.sessionNote, h("div", { class: "tr-row" }, n.start, n.finish, n.cancel)),
        n.switchWarning, n.startHelp = h("p", { class: "tr-note", text: t("startHelp") }),
        h("ol", { class: "tr-steps", "aria-label": t("title") }, ...n.steps.map(node => h("li", {}, node))),
        h("div", { class: "tr-guide" }, h("h3", { text: t("step", { id: current, name: OBJECTIVES[current] }) }), n.guidance,
          h("div", { class: "tr-row" }, n.record, n.next), n.focusSummary),
        h("div", { class: "tr-body" },
          h("div", { class: "tr-camera-panel" }, h("h3", { text: t("sampleView") }),
            n.samplePane = h("div", { class: "tr-preview tr-live-sample", ...role("sample-preview") }, sampleCamera())),
          h("div", { class: "tr-camera-panel" }, h("h3", { text: t("markerView") }),
            h("div", { class: "tr-preview", ...role("preview") }, n.livePane, n.capturedPane, n.imageError,
              h("div", { class: "tr-view-switch" }, n.live, n.captured)))),
        h("p", { class: "tr-note", text: t("setup") }),
        h("section", { class: "tr-initial", ...role("initial") },
          h("div", { class: "tr-guide" }, h("h3", { text: t("initialTitle") }), h("p", { class: "tr-note", text: t("initialHelp") }), n.initialRecord, n.initialState), n.initialImage),
        n.selectionError,
        h("p", { class: "tr-limitation", text: t("limitation") }),
        h("div", { class: "tr-profiles" }, h("h3", { text: t("profiles") }), rows.length
          ? h("ul", { class: "tr-profile-list" }, ...rows) : h("p", { class: "tr-note", text: t("empty") })));
      renderStatus();
      renderImage();
    }

    function renderStatus() {
      if (!nodes.state) return;
      const n = nodes, sessionActive = state?.session?.active === true, own = mine();
      const captured = count(draft()), ready = complete(draft()), canStart = state && (state.demo || api.owns?.("microscope"));
      const saved = (state?.profiles || []).some(item => profileId(item) === profileId(draft()));
      n.state.textContent = busy ? t("working") : !state ? t("loading") : nameDirty ? t("dirty") : dirty || newDraft || sessionActive || !saved ? t("draft") : t("saved");
      n.progress.textContent = t("progress", { count: captured });
      const position = coordinates(capture(draft(), current));
      n.focusSummary.textContent = position ? t("focusSummary", { x: position.x.toFixed(1), y: position.y.toFixed(1) }) : t("noCoordinates");
      n.switchWarning.hidden = !draft() || draft().schema_version === 5;
      n.switchWarning.textContent = t("legacyDraft");
      const initial = draft()?.initial_5x_capture, initialPosition = coordinates(initial), initialImage = previewSource(initial);
      n.initialState.textContent = initialPosition ? t("initialSaved", { y: initialPosition.y.toFixed(1) }) : t("initialMissing");
      n.initialImage.hidden = !initialImage;
      if (initialImage) n.initialImage.setAttribute("src", initialImage); else n.initialImage.removeAttribute("src");
      n.initialRecord.hidden = !own;
      n.initialRecord.disabled = busy || !own;
      n.startHelp.hidden = own;
      n.selectionError.hidden = !state?.selection_error;
      n.selectionError.textContent = state?.selection_error?.message || String(state?.selection_error || "");
      n.message.hidden = !error; n.error.textContent = error;
      n.profileSelect.value = state?.selected_profile || "";
      n.profileSelect.disabled = busy || microscopeBusy || sessionActive || !canStart;
      n.profileError.textContent = selectionFailure || n.selectionError.textContent;
      n.profileError.hidden = !n.profileError.textContent;
      n.new.disabled = busy || sessionActive || !canStart;
      n.upload.disabled = n.file.disabled = busy || sessionActive || !canStart;
      n.name.disabled = busy;
      n.save.disabled = busy || sessionActive || !draft() || !name.trim() || !canStart;
      n.save.textContent = t(ready ? "saveUse" : "save");
      if (ready && microscopeBusy) n.save.disabled = true;
      n.start.hidden = sessionActive;
      n.start.disabled = busy || !canStart;
      n.record.hidden = !own;
      n.record.disabled = busy || !own;
      n.next.hidden = !own || !capture(draft(), current) || count(draft()) === 4;
      n.next.disabled = busy;
      n.finish.hidden = n.cancel.hidden = !own;
      n.finish.disabled = busy || !ready;
      n.cancel.disabled = busy;
      n.sessionNote.textContent = sessionActive && !own ? t("other") : own && ready ? t("review")
        : !sessionActive && draft() && dirty ? t("ended") : !canStart ? t("control") : "";
      n.sessionNote.hidden = !n.sessionNote.textContent;
      for (const step of n.steps) step.disabled = busy;
      for (const item of n.actions) {
        item.choose.disabled = busy || sessionActive || (item.selected && item.complete) || microscopeBusy || !canStart;
        item.remove.disabled = busy || sessionActive || !canStart;
        item.download.disabled = busy;
      }
    }

    function renderImage(refresh = false) {
      if (!nodes.livePane) return;
      const record = capture(draft(), current), source = previewSource(record);
      const live = imageMode === "live", visible = active && calibrationVisible;
      nodes.livePane.hidden = !live || !visible;
      nodes.capturedPane.hidden = live;
      nodes.live.setAttribute("aria-pressed", String(live));
      nodes.captured.setAttribute("aria-pressed", String(!live));
      nodes.still.hidden = !source;
      nodes.noCapture.hidden = Boolean(source);
      if (source) nodes.still.setAttribute("src", source); else nodes.still.removeAttribute("src");
      if (!live || !visible) cameraTile?.labCameras?.stop?.();
      nodes.samplePane.hidden = !visible;
      if (!visible) sampleTile?.labCameras?.stop?.();
      if (demoImage) {
        if (live && visible && state?.camera?.frame_url) {
          if (refresh || !demoImage.getAttribute("src")) {
            const url = state.camera.frame_url;
            demoImage.setAttribute("src", url + (url.includes("?") ? "&" : "?") + "t=" + Date.now());
          }
        } else demoImage.removeAttribute("src");
      }
    }

    function applyState(value, action) {
      if (!value || typeof value !== "object" || !Array.isArray(value.profiles)) throw new Error(t("unavailable"));
      state = value;
      if (action) {
        newDraft = false;
        if (action !== "delete") dirty = !["save", "select", "load", "upload"].includes(action);
        if (["save", "select", "load", "upload", "begin"].includes(action)) { name = state.draft?.name || ""; nameDirty = false; }
        if (action === "begin") { current = nextObjective(draft()); imageMode = "live"; }
        if (action === "record") { imageMode = "captured"; }
        if (["select", "load", "upload"].includes(action)) {
          const first = OBJECTIVES.findIndex((_, id) => capture(draft(), id));
          current = first < 0 ? 0 : first;
          imageMode = first < 0 ? "live" : "captured";
        }
      } else if (!nameDirty && !newDraft) name = state.draft?.name || "";
      const key = JSON.stringify([state.draft, state.profiles, state.selected_profile, state.selection_error, state.session, state.demo, state.camera]);
      if (action || key !== lastKey) { lastKey = key; build(); }
      else { renderStatus(); renderImage(true); }
    }

    async function poll() {
      if (!active || busy || polling) return;
      polling = true;
      const token = epoch;
      try { const result = await api.request("turret"); if (active && token === epoch && !busy) applyState(result); }
      catch (failure) { if (active && token === epoch) setError(failure); }
      finally { polling = false; }
    }

    async function requestAction(action, data) {
      const result = await api.request("turret/" + action, data, { timeout: ["begin", "record", "record_initial"].includes(action) ? 20000 : 10000 });
      if (result?.error) throw new Error(result.error);
      if (action === "select" && (result?.selected_profile !== data.id || result?.selection_error)) throw new Error(result?.selection_error || t("notSelected"));
      applyState(result, action);
    }
    async function runMutation(operation, selects = false) {
      if (busy) return false;
      busy = true; epoch++; error = ""; selectionFailure = ""; renderStatus();
      try {
        await operation();
        return true;
      } catch (failure) { if (selects) selectionFailure = failure?.message || String(failure); setError(failure); return false; }
      finally { busy = false; renderStatus(); }
    }
    function mutate(action, data) { return runMutation(() => requestAction(action, data), action === "select"); }

    function startNew() {
      newDraft = true; dirty = true; name = ""; nameDirty = false; current = 0; imageMode = "live"; error = ""; build(); nodes.name.focus?.();
    }
    async function begin() {
      return mutate("begin", newDraft || !draft() ? { name: name.trim() || t("untitled"), new: true } : {});
    }
    async function save() {
      if (!name.trim()) { setError(t("requiredName")); return false; }
      const use = complete(draft());
      return runMutation(async () => {
        await requestAction("save", { name: name.trim() });
        if (use) {
          try { await requestAction("select", { id: profileId(draft()) }); }
          catch (failure) { throw new Error(t("savedNotSelected", { message: failure?.message || String(failure) })); }
        }
        api.notify?.(t(use ? "savedSelected" : "savedNotice"), "info");
      }, use);
    }
    async function upload(event) {
      const file = event.target?.files?.[0];
      event.target.value = "";
      if (!file) return false;
      if (file.size > MAX_UPLOAD) { setError(t("fileSize")); return false; }
      let contents;
      try { contents = await file.text(); } catch { setError(t("fileRead")); return false; }
      try { const parsed = JSON.parse(contents); if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(); }
      catch { setError(t("fileJson")); return false; }
      return mutate("upload", { profile: contents });
    }
    async function downloadProfile(id) {
      try {
        const profile = await api.request("turret/profile/" + encodeURIComponent(id));
        const blob = new global.Blob([JSON.stringify(profile, null, 2) + "\n"], { type: "application/json" });
        const url = global.URL.createObjectURL(blob);
        const link = h("a", { href: url, download: String(profile.name || "turret").replace(/[^A-Za-z0-9_-]/g, "_") + ".json" });
        link.click();
        global.setTimeout(() => global.URL.revokeObjectURL(url), 1000);
        return true;
      } catch (failure) { setError(failure); return false; }
    }
    function show() {
      if (active) return;
      active = true; renderImage(); poll();
      timer = global.setInterval(poll, POLL_MS);
    }
    function hide() {
      active = false; epoch++;
      if (timer !== null) global.clearInterval(timer);
      timer = null; renderImage();
    }
    api.onLanguage?.(build);
    api.onState?.(renderStatus);
    build();
    return { element, profileSelector, show, hide, refresh: poll,
      isCalibrating: () => state?.session?.active === true, isDemo: () => state?.demo === true,
      setCalibrationVisible: value => { calibrationVisible = Boolean(value); renderImage(); },
      setMicroscopeBusy: value => { microscopeBusy = Boolean(value); renderStatus(); },
      _test: { applyState, mutate, upload, downloadProfile, startNew, begin, save, state: () => state } };
  }

  return { create, i18n, logic: { OBJECTIVES, MAX_UPLOAD, capture, count, complete, coordinates, profileId, nextObjective, previewSource, translate } };
});
