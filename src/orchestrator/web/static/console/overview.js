/* Overview workspace: the current run, merged events, safety and control.
 * The page is built once per language. State updates change text and states in place. */
(function () {
  "use strict";
  const LabConsole = (typeof window !== "undefined" && window.LabConsole) || null;
  if (!LabConsole) return;
  const { logic } = LabConsole;
  const DEVICES = LabConsole.SERVICES;

  const i18n = {
    en: {
      "run.title": "Run", "run.status": "Status: {status}",
      "events.title": "Events · all services", "events.all": "All", "events.warnings": "Warnings",
      "events.service": "Service", "events.none": "No events yet.", "events.unavailable": "Events unavailable: {services}",
      "safety.title": "Safety",
      "control.title": "Control", "control.releaseAll": "Release all",
      "control.thisPage": "This page", "control.console": "Console (other page)", "control.unknown": "Unknown",
      "control.take": "Take control", "control.release": "Release",
      "control.takeLabel": "Take control of {device}", "control.releaseLabel": "Release {device}",
    },
    zh: {
      "run.title": "运行", "run.status": "状态：{status}",
      "events.title": "事件 · 全部服务", "events.all": "全部", "events.warnings": "警告",
      "events.service": "服务", "events.none": "暂无事件。", "events.unavailable": "事件不可用：{services}",
      "safety.title": "安全",
      "control.title": "控制权", "control.releaseAll": "全部释放",
      "control.thisPage": "本页", "control.console": "控制台（其他页面）", "control.unknown": "未知",
      "control.take": "获取控制", "control.release": "释放",
      "control.takeLabel": "获取 {device} 的控制", "control.releaseLabel": "释放 {device}",
    },
  };

  // n: the nodes that update() changes. keys: the last content of the parts that rebuild their rows.
  const view = { api: null, root: null, filter: "all", events: { events: [], errors: {} }, busy: new Set(), timer: null, n: null };

  function setText(node, text) { if (node.textContent !== text) node.textContent = text; }

  function section(id, title, ...children) {
    const { el } = view.api;
    return el("section", { class: "lc-card", "aria-labelledby": id }, el("h2", { id, text: title }), ...children);
  }

  function runRows(run) {
    const { el, t } = view.api;
    if (!run) return [];
    return [
      el("p", { class: "lc-strong", text: `${run.name || run.id} · ${t("run.status", { status: run.status })}` }),
      run.detail ? el("p", { class: "lc-muted", text: run.detail }) : null,
      el("ol", { class: "lc-steps" }, ...(run.steps || []).map(step => el("li", { class: "lc-step" + (step.position === run.step_position ? " current" : "") },
        el("span", { class: "lc-small", text: `${step.position + 1} · ${step.status || ""}` }),
        el("span", { class: "lc-strong", text: step.skill || "" }),
        step.error ? el("span", { class: "lc-muted", text: step.error }) : null))),
    ];
  }

  /** 24-hour time, as on the Motion page. */
  function clock(seconds) {
    if (!seconds) return "--:--:--";
    try { return new Date(seconds * 1000).toLocaleTimeString("en-GB", { hour12: false }); }
    catch { return "--:--:--"; }
  }

  /** One event row. Only a warning or an error shows its level. */
  function eventRow(item) {
    const { el, deviceName } = view.api;
    const marked = item.level && item.level !== "info" && item.level !== "debug";
    const fields = item.fields || {};
    const detail = [fields.action, fields.result, fields.message_detail].filter(value => typeof value === "string" && value).join(" · ");
    return el("div", { class: "lc-event level-" + item.level },
      el("span", { class: "lc-mono", text: clock(item.ts_unix) }),
      el("span", { class: "lc-event-src", text: deviceName(item.service) }),
      el("span", { class: "lc-event-text" }, marked ? el("span", { class: "lc-event-level", text: item.level }) : null, item.message,
        detail ? el("span", { class: "lc-muted", text: " · " + detail }) : null));
  }

  /** One press sends the stop. The stop result band of the shell shows the outcome. A stop is never disabled. */
  function safety(action) {
    view.api.safetyStop(action).catch(() => {});
  }

  async function control(device, action) {
    const { take, release, notify } = view.api;
    const key = "control-" + device;
    if (view.busy.has(key)) return;
    view.busy.add(key);
    update();
    try { await (action === "take" ? take(device) : release(device)); }
    catch (error) { notify(error.message, "error"); }
    finally { view.busy.delete(key); update(); }
  }

  /** Build the page once per language. A click is never lost to a rebuild. */
  function build() {
    const { el, t, deviceName, releaseAll, notify } = view.api;
    const n = view.n = { keys: {} };

    n.run = el("div", { class: "ov-slot" });
    n.filters = ["all", "warnings"].map(id => [id, el("button", {
      type: "button", class: "lc-filter", "aria-pressed": "false", text: t("events." + id),
      onclick: () => { view.filter = id; update(); },
    })]);
    n.select = el("select", { id: "ov-event-service", "aria-label": t("events.service"), onchange: event => { view.filter = event.target.value || "all"; update(); } },
      el("option", { value: "", text: t("events.service") }),
      ...[...DEVICES, "runs"].map(id => el("option", { value: id, text: deviceName(id) })));
    n.eventsError = el("p", { class: "lc-muted", hidden: true });
    n.events = el("div", { class: "lc-events" });
    const events = el("section", { class: "lc-card", "aria-labelledby": "ov-events" },
      el("div", { class: "lc-card-head" }, el("h2", { id: "ov-events", text: t("events.title") }),
        el("div", { class: "lc-filters" }, el("div", { class: "ov-seg" }, ...n.filters.map(([, node]) => node)), n.select)),
      n.eventsError, n.events);

    // STOP ALL MOTION is in the header on every page.
    const safetyCard = section("ov-safety", t("safety.title"),
      el("div", { class: "ov-stops" },
        el("button", { type: "button", class: "lc-stop", onclick: () => safety("heat"), text: t("stop.heat") }),
        el("button", { type: "button", class: "lc-danger", onclick: () => safety("gas"), text: t("stop.gas") })));

    n.releaseAll = el("button", { type: "button", class: "lc-button", onclick: () => releaseAll().catch(error => notify(error.message, "error")), text: t("control.releaseAll") });
    n.rows = DEVICES.map(device => {
      const row = { device, mine: false, holder: el("span", { class: "lc-muted lc-row-main" }) };
      row.button = el("button", { type: "button", class: "lc-button lc-small-button", onclick: () => control(device, row.mine ? "release" : "take") });
      row.node = el("div", { class: "lc-row" }, el("span", { class: "lc-strong lc-row-name", text: deviceName(device) }), row.holder, row.button);
      return row;
    });
    const controlCard = el("section", { class: "lc-card ov-control", "aria-labelledby": "ov-control" },
      el("div", { class: "lc-card-head" }, el("h2", { id: "ov-control", text: t("control.title") }), n.releaseAll),
      el("div", { class: "lc-rows" }, ...n.rows.map(row => row.node)));

    // The run card shows only when it has content.
    n.runCard = section("ov-run", t("run.title"), n.run);
    view.root.replaceChildren(el("div", { class: "lc-layout" },
      el("div", { class: "lc-col-main" }, controlCard, n.runCard, events),
      el("aside", { class: "lc-col-side" }, safetyCard)));
    update();
  }

  /** Change text and states in place. Only the run steps and the event rows rebuild, and only when they change. */
  function update() {
    const n = view.n;
    if (!n || !view.api) return;
    const { el, t, deviceName } = view.api;
    const snapshot = view.api.state();

    const run = snapshot?.run || null;
    const runKey = JSON.stringify(run);
    if (runKey !== n.keys.run) {
      n.keys.run = runKey;
      n.run.replaceChildren(...runRows(run).filter(Boolean));
    }
    n.runCard.hidden = !run;

    for (const [id, node] of n.filters) node.setAttribute("aria-pressed", String(view.filter === id));
    const service = [...DEVICES, "runs"].includes(view.filter) ? view.filter : "";
    if (n.select.value !== service) n.select.value = service;
    const errors = Object.keys(view.events.errors || {});
    n.eventsError.hidden = !errors.length;
    setText(n.eventsError, errors.length ? t("events.unavailable", { services: errors.map(deviceName).join(", ") }) : "");
    const rows = logic.filterEvents(view.events.events, view.filter).slice(0, 200);
    const eventsKey = JSON.stringify([view.filter, rows]);
    if (eventsKey !== n.keys.events) {
      n.keys.events = eventsKey;
      const scroll = n.events.scrollTop || 0;
      n.events.replaceChildren(...(rows.length ? rows.map(eventRow) : [el("p", { class: "lc-muted", text: t("events.none") })]));
      n.events.scrollTop = scroll;
    }

    const cards = new Map((snapshot?.services || []).map(card => [card.id, card]));
    const sessions = new Map((snapshot?.control || []).map(item => [item.device, item]));
    for (const row of n.rows) {
      const card = cards.get(row.device);
      const session = sessions.get(row.device);
      // In demo mode Take control starts the owned demo, so offer it for those devices.
      const configured = (card && card.phase !== "unconfigured") || (snapshot?.console?.demo === true && row.device !== "microscope");
      // A free device shows no holder line.
      let holder = configured ? card?.lease_holder || "" : t("chip.unconfigured");
      if (session) holder = session.mine ? t("control.thisPage") : t("control.console");
      row.mine = Boolean(session?.mine && session.state === "held");
      setText(row.holder, holder);
      row.holder.hidden = !holder;
      row.holder.className = "lc-muted lc-row-main" + (session?.mine ? " ov-mine" : "");
      row.button.className = "lc-button lc-small-button" + (row.mine ? " ov-on" : "");
      row.button.hidden = !configured;
      row.button.disabled = view.busy.has("control-" + row.device) || (Boolean(session) && !row.mine);
      setText(row.button, t(row.mine ? "control.release" : "control.take"));
      row.button.setAttribute("aria-label", t(row.mine ? "control.releaseLabel" : "control.takeLabel", { device: deviceName(row.device) }));
    }
    n.releaseAll.disabled = !(snapshot?.control || []).some(item => item.state === "held");
  }

  async function loadEvents() {
    try { view.events = await view.api.consoleRequest("/api/console/events"); update(); }
    catch { /* The status strip shows the console state. */ }
  }

  LabConsole.register({
    id: "overview",
    i18n,
    mount(root, api) {
      view.root = root;
      view.api = api;
      api.onState(() => update());
      api.onLanguage(() => build());
      build();
      loadEvents();
      view.timer = setInterval(loadEvents, 2000);
    },
  });
})();
