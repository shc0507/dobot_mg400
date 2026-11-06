/* Overview workspace: the current run, merged events, safety and control. */
(function () {
  "use strict";
  const LabConsole = (typeof window !== "undefined" && window.LabConsole) || null;
  if (!LabConsole) return;
  const { logic } = LabConsole;
  const DEVICES = LabConsole.SERVICES;

  const i18n = {
    en: {
      "run.title": "Run", "run.none": "No run is active.", "run.status": "Status: {status}",
      "events.title": "Events · all services", "events.all": "All", "events.warnings": "Warnings",
      "events.service": "Service", "events.none": "No events yet.", "events.unavailable": "Events unavailable: {services}",
      "safety.title": "Safety", "safety.motionTargets": "Robot · stage · microscope",
      "safety.note": "The physical e-stops stay the primary stop. Each software stop works without control and never waits for a lease.",
      "control.title": "Control", "control.releaseAll": "Release all", "control.free": "Free",
      "control.thisPage": "This page", "control.console": "Console (other page)", "control.unknown": "Unknown",
      "control.take": "Take control", "control.release": "Release",
      "control.takeLabel": "Take control of {device}", "control.releaseLabel": "Release {device}",
      "proposals.title": "Agent proposals", "proposals.count": "{count} waiting.",
      "proposals.none": "No proposal waiting.",
    },
    zh: {
      "run.title": "运行", "run.none": "当前没有运行。", "run.status": "状态：{status}",
      "events.title": "事件 · 全部服务", "events.all": "全部", "events.warnings": "警告",
      "events.service": "服务", "events.none": "暂无事件。", "events.unavailable": "事件不可用：{services}",
      "safety.title": "安全", "safety.motionTargets": "机械臂 · 位移台 · 显微镜",
      "safety.note": "物理急停仍是首要停止手段。每个软件停止都不需要控制权，也不等待租约。",
      "control.title": "控制权", "control.releaseAll": "全部释放", "control.free": "空闲",
      "control.thisPage": "本页", "control.console": "控制台（其他页面）", "control.unknown": "未知",
      "control.take": "获取控制", "control.release": "释放",
      "control.takeLabel": "获取 {device} 的控制", "control.releaseLabel": "释放 {device}",
      "proposals.title": "智能体提议", "proposals.count": "{count} 项待处理。",
      "proposals.none": "没有待处理的提议。",
    },
  };

  const view = { api: null, root: null, filter: "all", events: { events: [], errors: {} }, busy: new Set(), timer: null };

  function section(id, title, ...children) {
    const { el } = view.api;
    return el("section", { class: "lc-card", "aria-labelledby": id }, el("h2", { id, text: title }), ...children);
  }

  function runCard(run) {
    const { el, t } = view.api;
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

  /** One press sends the stop. The stop result band of the shell shows the outcome. A stop is never disabled. */
  function safety(action) {
    view.api.safetyStop(action).catch(() => {});
  }

  function safetyCard() {
    const { el, t } = view.api;
    return section("ov-safety", t("safety.title"),
      el("button", { type: "button", class: "lc-stop lc-stop-big", onclick: () => safety("motion") },
        el("span", { text: t("stop.all") }), el("span", { class: "lc-small", text: t("safety.motionTargets") })),
      el("div", { class: "lc-grid2" },
        el("button", { type: "button", class: "lc-stop", onclick: () => safety("heat"), text: t("stop.heat") }),
        el("button", { type: "button", class: "lc-danger", onclick: () => safety("gas"), text: t("stop.gas") })),
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
    const { el, t } = view.api;
    const waiting = (snapshot?.approvals || []).filter(item => item.decision === "pending").length;
    return section("ov-proposals", t("proposals.title"),
      el("p", { class: "lc-muted", text: waiting ? t("proposals.count", { count: waiting }) : t("proposals.none") }));
  }

  function render() {
    if (!view.api) return;
    const snapshot = view.api.state();
    const { el } = view.api;
    const scroll = view.root.querySelector?.(".lc-events")?.scrollTop || 0;
    view.root.replaceChildren(el("div", { class: "lc-layout" },
      el("div", { class: "lc-col-main" }, runCard(snapshot?.run), eventsCard()),
      el("aside", { class: "lc-col-side" }, safetyCard(), controlCard(snapshot), proposalsCard(snapshot))));
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
