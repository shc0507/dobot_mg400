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
      progress: "{count} of 4 objectives recorded", objective: "{name} · marker {id}", step: "Step {step} of 4 · {name}",
      manual: "Turn the turret to {name} and seat it in its detent. Then record the image.",
      startHelp: "Before Start, we recommend moving focus manually to clearance. Start releases only the turret motor. It does not move the focus axis.",
      setup: "Objective markers 0–3 are about 8 mm. Keep the current objective marker in focus and fully visible.",
      limitation: "Alignment uses recorded image coordinates. Keep Camera3 fixed. Retake all 4 objectives after moving Camera3 or changing camera focus or zoom.",
      record: "Record {name}", retake: "Retake {name}", recorded: "Recorded", pending: "Not recorded", review: "All 4 objectives are recorded. Review the images, then finish calibration.",
      control: "Take microscope control to start calibration.", other: "Another client has an active calibration session.",
      ended: "Session ended. Save the draft to keep these captures.", offline: "Camera3 image is unavailable.",
      noCapture: "No image recorded for this objective.", file: "Profile JSON file", fileSize: "The JSON file must be 2 MiB or smaller.",
      fileJson: "Select a valid JSON profile file.", fileRead: "Could not read the JSON file.", loading: "Loading calibration…", unavailable: "Calibration state is unavailable.",
      incomplete: "Partial", complete: "Complete", clear: "Dismiss error", working: "Working…", savedNotice: "Calibration profile saved.",
      dirty: "Unsaved name", requiredName: "Enter a profile name.", next: "Next: {name}",
      focusSummary: "Clearance: {clearance} · Recorded: {recorded}", fullSteps: "{value} full steps", unknownReference: "(focus not zeroed)",
      switchFocus: "We recommend recording at clearance {value} full steps.",
    },
    zh: {
      title: "转盘校准", camera: "Camera3", live: "实时", captured: "已记录 {name}",
      new: "新建配置", upload: "上传 JSON", download: "下载", remove: "删除", select: "选择", selected: "已选中",
      profiles: "已存配置", empty: "没有已存配置。", name: "配置名称", untitled: "转盘校准", load: "加载草稿",
      draft: "未保存草稿", saved: "已保存", save: "保存配置", saveUse: "保存并使用", start: "开始校准", finish: "完成校准", cancel: "结束会话",
      lensCalibration: "物镜校准", noSelection: "未选择配置", selectionUnavailable: "所选配置不可用",
      savedSelected: "校准配置已保存并选中。", savedNotSelected: "配置已保存，但未选中：{message}", notSelected: "未选中配置。",
      progress: "已记录 {count} / 4 个物镜", objective: "{name} · 标记 {id}", step: "第 {step} / 4 步 · {name}",
      manual: "把转盘转到 {name} 并卡入定位点。然后记录图像。",
      startHelp: "开始前，建议手动把对焦轴移到避让位置。开始时只释放转盘电机。对焦轴不会移动。",
      setup: "物镜标记 0–3 约为 8 mm。当前物镜标记必须清晰且完整可见。",
      limitation: "对齐使用已记录的图像坐标。保持 Camera3 固定。移动 Camera3 或改变摄像头对焦、变焦后，重新记录全部 4 个物镜。",
      record: "记录 {name}", retake: "重拍 {name}", recorded: "已记录", pending: "未记录", review: "已记录全部 4 个物镜。检查图像后，完成校准。",
      control: "先获取显微镜控制权，再开始校准。", other: "其他客户端正在校准。",
      ended: "会话已结束。保存草稿以保留图像。", offline: "Camera3 图像不可用。",
      noCapture: "尚未记录此物镜图像。", file: "配置 JSON 文件", fileSize: "JSON 文件不得超过 2 MiB。",
      fileJson: "请选择有效的 JSON 配置文件。", fileRead: "无法读取 JSON 文件。", loading: "正在读取校准…", unavailable: "校准状态不可用。",
      incomplete: "未完成", complete: "已完成", clear: "关闭错误提示", working: "处理中…", savedNotice: "校准配置已保存。",
      dirty: "名称未保存", requiredName: "请填写配置名称。", next: "下一步：{name}",
      focusSummary: "避让位置：{clearance} · 记录位置：{recorded}", fullSteps: "{value} 整步", unknownReference: "（对焦未归零）",
      switchFocus: "建议在避让位置 {value} 整步处记录图像。",
    },
  };

  function capture(profile, id) { return profile?.captures?.[String(id)] || null; }
  function count(profile) { return OBJECTIVES.filter((_, id) => capture(profile, id)).length; }
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
    let state = null, active = false, timer = null, epoch = 0, busy = false, polling = false, microscopeBusy = false;
    let name = "", nameDirty = false, newDraft = false, dirty = false, current = 0, imageMode = "live", error = "", selectionFailure = "";
    let nodes = {}, cameraTile = null, demoImage = null, lastKey = "";
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

    function build() {
      const record = capture(draft(), current);
      const n = nodes = {};
      const completeProfiles = (state?.profiles || []).filter(item => item.complete);
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
      n.steps.forEach((node, id) => node.append(h("span", { class: "tr-step-index", text: capture(draft(), id) ? "✓" : String(id + 1) }),
        h("span", { text: OBJECTIVES[id] }), h("small", { text: "ID " + id })));
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
      n.switchWarning = h("p", { class: "tr-note", role: "status", ...role("switch-warning") });
      n.selectionError = h("p", { class: "tr-error", role: "alert", hidden: true, ...role("selection-error") });
      n.guidance = h("p", { class: "tr-instruction", text: t("manual", { name: OBJECTIVES[current] }) });
      n.actions = [];
      const rows = (state?.profiles || []).map(item => {
        const id = profileId(item), selected = id === state?.selected_profile;
        const choose = button(selected ? "selected" : item.complete ? "select" : "load", () => mutate(item.complete ? "select" : "load", { id }), { ...role("select-" + id), disabled: selected });
        const download = button("download", () => downloadProfile(id), role("download-" + id));
        const remove = button("remove", () => mutate("delete", { id }), role("delete-" + id));
        n.actions.push({ choose, download, remove, selected, complete: item.complete });
        return h("li", { class: "tr-profile" + (selected ? " tr-profile-selected" : ""), "aria-current": selected ? "true" : null },
          h("div", { class: "tr-profile-label" }, h("span", { class: "tr-profile-name", text: item.name }),
            h("small", { text: t(item.complete ? "complete" : "incomplete") })),
          h("div", { class: "tr-row" }, choose, download, remove));
      });
      element.replaceChildren(
        h("div", { class: "lc-card-head tr-heading" }, h("div", { class: "tr-title" }, h("h2", { id: "tr-title", text: t("title") }), n.state),
          h("div", { class: "tr-row" }, n.new, n.upload)), n.file, n.message,
        h("div", { class: "tr-name-row" }, h("label", { class: "pr-field" }, t("name"), n.name), n.save, n.progress),
        h("ol", { class: "tr-steps", "aria-label": t("title") }, ...n.steps.map(node => h("li", {}, node))),
        h("div", { class: "tr-body" },
          h("div", { class: "tr-preview", ...role("preview") }, n.livePane, n.capturedPane, n.imageError,
            h("div", { class: "tr-view-switch" }, n.live, n.captured)),
          h("div", { class: "tr-guide" }, h("h3", { text: t("step", { step: current + 1, name: OBJECTIVES[current] }) }), n.focusSummary,
            n.guidance, h("div", { class: "tr-row" }, n.record, n.next),
            h("div", { class: "tr-session" }, n.sessionNote, h("div", { class: "tr-row" }, n.start, n.finish, n.cancel)),
            n.switchWarning, h("p", { class: "tr-note", text: t("startHelp") }), h("p", { class: "tr-note", text: t("setup") }))),
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
      const captured = count(draft()), canStart = state && (state.demo || api.owns?.("microscope"));
      const saved = (state?.profiles || []).some(item => profileId(item) === profileId(draft()));
      n.state.textContent = busy ? t("working") : !state ? t("loading") : nameDirty ? t("dirty") : dirty || newDraft || sessionActive || !saved ? t("draft") : t("saved");
      n.progress.textContent = t("progress", { count: captured });
      const clearance = state?.switch_focus, recorded = draft()?.focus_position;
      n.focusSummary.textContent = t("focusSummary", {
        clearance: Number.isFinite(clearance) ? t("fullSteps", { value: clearance }) : t("unknownReference"),
        recorded: !draft() ? t("pending") : Number.isFinite(recorded) ? t("fullSteps", { value: recorded }) : t("unknownReference"),
      });
      n.switchWarning.hidden = !Number.isFinite(clearance);
      n.switchWarning.textContent = t("switchFocus", { value: clearance });
      n.selectionError.hidden = !state?.selection_error;
      n.selectionError.textContent = state?.selection_error?.message || String(state?.selection_error || "");
      n.message.hidden = !error; n.error.textContent = error;
      n.profileSelect.value = state?.selected_profile || "";
      n.profileSelect.disabled = busy || microscopeBusy || sessionActive || !canStart;
      n.profileError.textContent = selectionFailure || n.selectionError.textContent;
      n.profileError.hidden = !n.profileError.textContent;
      n.new.disabled = busy || sessionActive;
      n.upload.disabled = n.file.disabled = busy || sessionActive;
      n.name.disabled = busy;
      n.save.disabled = busy || sessionActive || !draft() || !name.trim();
      n.save.textContent = t(captured === 4 ? "saveUse" : "save");
      if (captured === 4 && (microscopeBusy || !canStart)) n.save.disabled = true;
      n.start.hidden = sessionActive;
      n.start.disabled = busy || !canStart;
      n.record.hidden = !own;
      n.record.disabled = busy || !own;
      n.next.hidden = !own || !capture(draft(), current) || captured === 4;
      n.next.disabled = busy;
      n.finish.hidden = n.cancel.hidden = !own;
      n.finish.disabled = busy || captured !== 4;
      n.cancel.disabled = busy;
      n.sessionNote.textContent = sessionActive && !own ? t("other") : own && captured === 4 ? t("review")
        : !sessionActive && draft() && dirty ? t("ended") : !canStart ? t("control") : "";
      n.sessionNote.hidden = !n.sessionNote.textContent;
      for (const step of n.steps) step.disabled = busy;
      for (const item of n.actions) {
        item.choose.disabled = busy || sessionActive || item.selected || (item.complete && (microscopeBusy || !canStart));
        item.remove.disabled = busy || sessionActive;
        item.download.disabled = busy;
      }
    }

    function renderImage(refresh = false) {
      if (!nodes.livePane) return;
      const record = capture(draft(), current), source = previewSource(record);
      const live = imageMode === "live";
      nodes.livePane.hidden = !live || !active;
      nodes.capturedPane.hidden = live;
      nodes.live.setAttribute("aria-pressed", String(live));
      nodes.captured.setAttribute("aria-pressed", String(!live));
      nodes.still.hidden = !source;
      nodes.noCapture.hidden = Boolean(source);
      if (source) nodes.still.setAttribute("src", source); else nodes.still.removeAttribute("src");
      if (!live || !active) cameraTile?.labCameras?.stop?.();
      if (demoImage) {
        if (live && active && state?.camera?.frame_url) {
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
      const result = await api.request("turret/" + action, data, { timeout: ["begin", "record"].includes(action) ? 20000 : 10000 });
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
      const use = count(draft()) === 4;
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
      setMicroscopeBusy: value => { microscopeBusy = Boolean(value); renderStatus(); },
      _test: { applyState, mutate, upload, downloadProfile, startNew, begin, save, state: () => state } };
  }

  return { create, i18n, logic: { OBJECTIVES, MAX_UPLOAD, capture, count, profileId, nextObjective, previewSource, translate } };
});
