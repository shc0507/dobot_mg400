/* Overview workspace: readiness, the current run, merged events, safety and control. */
(function () {
  "use strict";
  const LabConsole = (typeof window !== "undefined" && window.LabConsole) || null;
  if (!LabConsole) return;
  const { logic } = LabConsole;
  const DEVICES = LabConsole.SERVICES;

  const i18n = {
    en: {
      "ready.title": "Readiness before a run",
      "ready.summary": "{ready} ready · {todo} to do · {blocked} blocked",
      "ready.ready": "Ready", "ready.todo": "To do", "ready.blocked": "Blocked",
      "ready.open": "Open {workspace} →",
      "item.tool-offset": "Robot tool offset", "item.camera-calibration": "Camera-to-robot calibration",
      "item.stage-reference": "Stage zero and soft limits", "item.focus-zero": "Microscope focus zero",
      "item.gas-map": "Gas map", "item.cutoff": "Over-temperature cutoff",
      "ws.motion": "Motion", "ws.cameras": "Cameras", "ws.process": "Furnace",
      "run.title": "Run", "run.none": "No run is active.", "run.status": "Status: {status}",
      "run.controls": "Hold and Abort are in the lab dashboard.", "run.open": "Open the lab dashboard →",
      "events.title": "Events · all services", "events.all": "All", "events.warnings": "Warnings",
      "events.service": "Service", "events.none": "No events yet.", "events.unavailable": "Events unavailable: {services}",
      "safety.title": "Safety", "safety.motionTargets": "Robot · stage · microscope",
      "safety.heat": "Heat off…", "safety.gas": "Gas off…",
      "safety.heatConfirm": "Turn the furnace heat off.", "safety.gasConfirm": "Close all 3 gas channels.",
      "safety.confirm": "Confirm", "safety.cancel": "Cancel",
      "safety.note": "The physical e-stops stay the primary stop. Each software stop works without control and never waits for a lease.",
      "safety.none": "No stop sent yet.",
      "control.title": "Control", "control.releaseAll": "Release all", "control.free": "Free",
      "control.thisPage": "This page", "control.console": "Console (other page)", "control.unknown": "Unknown",
      "control.take": "Take control", "control.release": "Release",
      "control.takeLabel": "Take control of {device}", "control.releaseLabel": "Release {device}",
      "pages.title": "Service pages", "pages.dashboard": "Lab dashboard",
      "pages.robot": "Robot panel", "pages.stage": "Stage panel", "pages.camera": "Camera preview",
      "pages.thermal": "Thermal camera", "pages.furnace": "Furnace", "pages.microscope": "Microscope",
      "result.pending": "Pending", "result.confirmed": "Confirmed", "result.unconfirmed": "Not confirmed",
      "result.failed": "Failed", "result.unavailable": "Unavailable",
      "proposals.title": "Agent proposals", "proposals.count": "{count} waiting. Decide in the lab dashboard.",
      "proposals.none": "No proposal waiting.",
    },
    zh: {
      "ready.title": "运行前检查",
      "ready.summary": "{ready} 项就绪 · {todo} 项待办 · {blocked} 项受阻",
      "ready.ready": "就绪", "ready.todo": "待办", "ready.blocked": "受阻",
      "ready.open": "打开{workspace} →",
      "item.tool-offset": "机械臂工具偏移", "item.camera-calibration": "相机到机械臂标定",
      "item.stage-reference": "位移台零点和软限位", "item.focus-zero": "显微镜对焦零点",
      "item.gas-map": "气体映射", "item.cutoff": "超温切断",
      "ws.motion": "运动", "ws.cameras": "相机", "ws.process": "炉子",
      "run.title": "运行", "run.none": "当前没有运行。", "run.status": "状态：{status}",
      "run.controls": "暂停和中止在实验室看板中操作。", "run.open": "打开实验室看板 →",
      "events.title": "事件 · 全部服务", "events.all": "全部", "events.warnings": "警告",
      "events.service": "服务", "events.none": "暂无事件。", "events.unavailable": "事件不可用：{services}",
      "safety.title": "安全", "safety.motionTargets": "机械臂 · 位移台 · 显微镜",
      "safety.heat": "关闭加热…", "safety.gas": "关闭气体…",
      "safety.heatConfirm": "关闭炉子加热。", "safety.gasConfirm": "关闭全部 3 路气体。",
      "safety.confirm": "确认", "safety.cancel": "取消",
      "safety.note": "物理急停仍是首要停止手段。每个软件停止都不需要控制权，也不等待租约。",
      "safety.none": "尚未发送停止。",
      "control.title": "控制权", "control.releaseAll": "全部释放", "control.free": "空闲",
      "control.thisPage": "本页", "control.console": "控制台（其他页面）", "control.unknown": "未知",
      "control.take": "获取控制", "control.release": "释放",
      "control.takeLabel": "获取 {device} 的控制", "control.releaseLabel": "释放 {device}",
      "pages.title": "服务页面", "pages.dashboard": "实验室看板",
      "pages.robot": "机械臂面板", "pages.stage": "位移台面板", "pages.camera": "相机预览",
      "pages.thermal": "热像仪", "pages.furnace": "炉子", "pages.microscope": "显微镜",
      "result.pending": "进行中", "result.confirmed": "已确认", "result.unconfirmed": "未确认",
      "result.failed": "失败", "result.unavailable": "不可用",
      "proposals.title": "智能体提议", "proposals.count": "{count} 项待处理。请在实验室看板中决定。",
      "proposals.none": "没有待处理的提议。",
    },
  };

  const view = { api: null, root: null, filter: "all", events: { events: [], errors: {} }, confirm: null, busy: new Set(), timer: null };

  function section(id, title, ...children) {
    const { el } = view.api;
    return el("section", { class: "lc-card", "aria-labelledby": id }, el("h2", { id, text: title }), ...children);
  }

  function readinessCard(rows) {
    const { el, t } = view.api;
    const counts = logic.readinessSummary(rows);
    const head = el("div", { class: "lc-card-head" },
      el("h2", { id: "ov-ready", text: t("ready.title") }),
      el("span", { class: "lc-muted", text: t("ready.summary", counts) }));
    const list = el("div", { class: "lc-rows" }, ...(rows || []).map(row => el("div", { class: "lc-row" },
      el("span", { class: "lc-state tone-" + ({ ready: "ok", todo: "warn", blocked: "bad" }[row.state] || "off") },
        el("span", { class: "lc-dot", "aria-hidden": "true" }), t("ready." + row.state)),
      el("div", { class: "lc-row-main" },
        el("span", { class: "lc-strong", text: t("item." + row.id) }),
        el("span", { class: "lc-muted", text: row.detail })),
      el("a", { href: "#" + row.workspace, class: "lc-link", text: t("ready.open", { workspace: t("ws." + row.workspace) }) }))));
    return el("section", { class: "lc-card", "aria-labelledby": "ov-ready" }, head, list);
  }

  function runCard(run) {
    const { el, t, base } = view.api;
    const children = [];
    if (!run) children.push(el("p", { class: "lc-muted", text: t("run.none") }));
    else {
      children.push(el("p", { class: "lc-strong", text: `${run.name || run.id} · ${t("run.status", { status: run.status })}` }));
      if (run.detail) children.push(el("p", { class: "lc-muted", text: run.detail }));
      children.push(el("ol", { class: "lc-steps" }, ...(run.steps || []).map(step => el("li", { class: "lc-step" + (step.position === run.step_position ? " current" : "") },
        el("span", { class: "lc-small", text: `${step.position + 1} · ${step.status || ""}` }),
        el("span", { class: "lc-strong", text: step.skill || "" }),
        step.error ? el("span", { class: "lc-muted", text: step.error }) : null))));
    }
    children.push(el("p", { class: "lc-muted" }, t("run.controls") + " ", el("a", { href: base + "/dashboard/", class: "lc-link", text: t("run.open") })));
    return section("ov-run", t("run.title"), ...children);
  }

  function eventsCard() {
    const { el, t, deviceName } = view.api;
    const filters = ["all", "warnings"].map(id => el("button", {
      type: "button", class: "lc-filter", "aria-pressed": String(view.filter === id), text: t("events." + id),
      onclick: () => { view.filter = id; render(); },
    }));
    const select = el("select", { id: "ov-event-service", "aria-label": t("events.service"), onchange: event => { view.filter = event.target.value || "all"; render(); } },
      el("option", { value: "", text: t("events.service") }),
      ...[...DEVICES, "runs"].map(id => el("option", { value: id, text: deviceName(id), selected: view.filter === id ? "selected" : null })));
    const rows = logic.filterEvents(view.events.events, view.filter).slice(0, 200);
    const errors = Object.keys(view.events.errors || {});
    const list = el("div", { class: "lc-events" }, ...(rows.length ? rows.map(item => el("div", { class: "lc-event level-" + item.level },
      el("span", { class: "lc-mono", text: item.ts_unix ? new Date(item.ts_unix * 1000).toLocaleTimeString() : "--:--:--" }),
      el("span", { class: "lc-event-src", text: deviceName(item.service) }),
      el("span", { class: "lc-event-level", text: item.level }),
      el("span", { text: item.message }))) : [el("p", { class: "lc-muted", text: t("events.none") })]));
    return el("section", { class: "lc-card", "aria-labelledby": "ov-events" },
      el("div", { class: "lc-card-head" }, el("h2", { id: "ov-events", text: t("events.title") }),
        el("div", { class: "lc-filters" }, ...filters, select)),
      errors.length ? el("p", { class: "lc-muted", text: t("events.unavailable", { services: errors.map(deviceName).join(", ") }) }) : null,
      list);
  }

  async function safety(action) {
    const { consoleRequest, notify } = view.api;
    if (view.busy.has(action)) return;
    view.busy.add(action);
    view.confirm = null;
    render();
    try { await consoleRequest("/api/dashboard/" + action, action === "motion" ? {} : { confirmed: true }); }
    catch (error) { notify(error.message, "error"); }
    finally { view.busy.delete(action); render(); }
  }

  function safetyCard(snapshot) {
    const { el, t } = view.api;
    const results = [];
    for (const action of ["motion", "heat", "gas"]) {
      const operation = logic.latestOperation(snapshot?.operations, action);
      if (!operation) continue;
      for (const [target, result] of Object.entries(operation.results || {})) {
        results.push(el("div", { class: "lc-result" },
          el("span", { text: target }),
          el("span", { class: "lc-strong tone-text-" + ({ confirmed: "ok", pending: "info", unconfirmed: "warn" }[result.status] || "bad"), text: t("result." + result.status) })));
      }
    }
    const confirm = view.confirm ? el("div", { class: "lc-confirm" },
      el("p", { text: t(view.confirm === "heat" ? "safety.heatConfirm" : "safety.gasConfirm") }),
      el("div", { class: "lc-grid2" },
        el("button", { type: "button", class: "lc-danger", onclick: () => safety(view.confirm), text: t("safety.confirm") }),
        el("button", { type: "button", class: "lc-button", onclick: () => { view.confirm = null; render(); }, text: t("safety.cancel") }))) : null;
    return section("ov-safety", t("safety.title"),
      el("button", { type: "button", class: "lc-stop lc-stop-big", disabled: view.busy.has("motion"), onclick: () => safety("motion") },
        el("span", { text: t("stop.all") }), el("span", { class: "lc-small", text: t("safety.motionTargets") })),
      el("div", { class: "lc-grid2" },
        el("button", { type: "button", class: "lc-danger", disabled: view.busy.has("heat"), onclick: () => { view.confirm = "heat"; render(); }, text: t("safety.heat") }),
        el("button", { type: "button", class: "lc-danger", disabled: view.busy.has("gas"), onclick: () => { view.confirm = "gas"; render(); }, text: t("safety.gas") })),
      confirm,
      el("div", { class: "lc-results" }, ...(results.length ? results : [el("p", { class: "lc-muted", text: t("safety.none") })])),
      el("p", { class: "lc-muted", text: t("safety.note") }));
  }

  async function control(device, action) {
    const { take, release, notify } = view.api;
    const key = "control-" + device;
    if (view.busy.has(key)) return;
    view.busy.add(key);
    render();
    try { await (action === "take" ? take(device) : release(device)); }
    catch (error) { notify(error.message, "error"); }
    finally { view.busy.delete(key); render(); }
  }

  function controlCard(snapshot) {
    const { el, t, deviceName, releaseAll, notify } = view.api;
    const cards = new Map((snapshot?.services || []).map(card => [card.id, card]));
    const sessions = new Map((snapshot?.control || []).map(item => [item.device, item]));
    const rows = DEVICES.map(device => {
      const card = cards.get(device);
      const session = sessions.get(device);
      let holder = card?.lease_holder || t("control.free");
      if (session) holder = session.mine ? t("control.thisPage") : t("control.console");
      const mine = session?.mine && session.state === "held";
      // In demo mode Take control starts the owned demo, so offer it for those devices.
      const configured = (card && card.phase !== "unconfigured") || (snapshot?.console?.demo === true && device !== "microscope");
      return el("div", { class: "lc-row" },
        el("span", { class: "lc-strong lc-row-name", text: deviceName(device) }),
        el("span", { class: "lc-muted lc-row-main", text: holder }),
        configured ? el("button", {
          type: "button", class: "lc-button lc-small-button", disabled: view.busy.has("control-" + device) || (!!session && !mine),
          "aria-label": t(mine ? "control.releaseLabel" : "control.takeLabel", { device: deviceName(device) }),
          onclick: () => control(device, mine ? "release" : "take"),
          text: t(mine ? "control.release" : "control.take"),
        }) : null);
    });
    const anyMine = (snapshot?.control || []).some(item => item.state === "held");
    return el("section", { class: "lc-card", "aria-labelledby": "ov-control" },
      el("div", { class: "lc-card-head" }, el("h2", { id: "ov-control", text: t("control.title") }),
        el("button", { type: "button", class: "lc-button", disabled: !anyMine, onclick: () => releaseAll().catch(error => notify(error.message, "error")), text: t("control.releaseAll") })),
      el("div", { class: "lc-rows" }, ...rows));
  }

  function proposalsCard(snapshot) {
    const { el, t, base } = view.api;
    const waiting = (snapshot?.approvals || []).filter(item => item.decision === "pending").length;
    return section("ov-proposals", t("proposals.title"),
      el("p", { class: "lc-muted" }, waiting ? t("proposals.count", { count: waiting }) : t("proposals.none"), " ",
        el("a", { href: base + "/dashboard/", class: "lc-link", text: t("run.open") })));
  }

  function pagesCard() {
    const { el, t, base } = view.api;
    const links = [["dashboard", base + "/dashboard/"], ["robot", "/robot/"], ["stage", "/stage/"], ["camera", "/camera/"], ["thermal", "/thermal/"], ["furnace", "/furnace/"], ["microscope", "/microscope/"]];
    return section("ov-pages", t("pages.title"),
      el("ul", { class: "lc-links" }, ...links.map(([id, href]) => el("li", {}, el("a", { href, class: "lc-link", text: t("pages." + id) })))));
  }

  function render() {
    if (!view.api) return;
    const snapshot = view.api.state();
    const { el } = view.api;
    const scroll = view.root.querySelector?.(".lc-events")?.scrollTop || 0;
    view.root.replaceChildren(el("div", { class: "lc-layout" },
      el("div", { class: "lc-col-main" }, readinessCard(snapshot?.readiness || []), runCard(snapshot?.run), eventsCard()),
      el("aside", { class: "lc-col-side" }, safetyCard(snapshot), controlCard(snapshot), proposalsCard(snapshot), pagesCard())));
    const events = view.root.querySelector?.(".lc-events");
    if (events) events.scrollTop = scroll;
  }

  async function loadEvents() {
    try { view.events = await view.api.consoleRequest("/api/console/events"); render(); }
    catch { /* The status strip shows the console state. */ }
  }

  LabConsole.register({
    id: "overview",
    i18n,
    mount(root, api) {
      view.root = root;
      view.api = api;
      api.onState(() => render());
      api.onLanguage(() => render());
      render();
      loadEvents();
      view.timer = setInterval(loadEvents, 2000);
    },
  });
})();
