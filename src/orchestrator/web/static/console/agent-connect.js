(function () {
  "use strict";
  const { LabConsole } = window;
  const view = { api: null, root: null, timer: null, busy: false, body: null, error: null };
  const i18n = {
    en: {
      "ac.title": "Agent Connect", "ac.mode": "Read-only diagnostics", "ac.source": "Shared device cache",
      "ac.phase0": "Phase 0 · Local mock portal", "ac.phase1": "Phase 1 · Service observations",
      "ac.phase2": "Phase 2 · Chip locations and evidence", "ac.phase3": "Phase 3 · Read-only message stream",
      "ac.later": "Phases 4–9 · Not enabled", "ac.note": "Agent commands, approval and control are not enabled.",
      "ac.stream": "Message stream", "ac.connections": "Connected readers", "ac.events": "Saved events for reconnect", "ac.discovery": "Open the data agreement discovery", "ac.samples": "Browse chip records (JSON)",
      "ac.local": "On your PC, run python scripts/agent_portal.py. Open http://127.0.0.1:8792/.",
      "ac.service": "Service", "ac.state": "Cache state", "ac.age": "Cache age", "ac.instance": "Process instance",
      "ac.details": "Observed fields", "ac.missing": "Not published", "ac.wait": "Waiting for cache diagnostics.",
    },
    zh: {
      "ac.title": "智能体连接", "ac.mode": "只读诊断", "ac.source": "共用设备缓存",
      "ac.phase0": "阶段 0 · 本地模拟测试页", "ac.phase1": "阶段 1 · 服务观测数据",
      "ac.phase2": "阶段 2 · 芯片位置与证据", "ac.phase3": "阶段 3 · 只读消息流",
      "ac.later": "阶段 4–9 · 未启用", "ac.note": "智能体命令、审批和控制尚未启用。",
      "ac.stream": "消息流", "ac.connections": "已连接读取端", "ac.events": "重连可读取的事件", "ac.discovery": "打开数据协议发现接口", "ac.samples": "浏览芯片记录（JSON）",
      "ac.local": "在本机运行 python scripts/agent_portal.py。打开 http://127.0.0.1:8792/。",
      "ac.service": "服务", "ac.state": "缓存状态", "ac.age": "缓存时长", "ac.instance": "进程实例",
      "ac.details": "观测字段", "ac.missing": "未发布", "ac.wait": "等待缓存诊断数据。",
    },
  };
  function age(value) { return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)} s` : "—"; }
  function render() {
    if (!view.root) return;
    const { el, t, deviceName } = view.api;
    const table = el("table", { class: "ac-table" },
      el("thead", {}, el("tr", {}, ...["service", "state", "age", "instance"].map(key => el("th", { text: t(`ac.${key}`) })))),
      el("tbody", {}, ...(view.body?.services || []).map(row => el("tr", {},
        el("td", { text: deviceName(row.service) }), el("td", { text: row.phase || "—" }),
        el("td", { text: age(row.age_s) }), el("td", { class: "ac-instance", text: row.observation.instance || t("ac.missing") })))));
    view.root.replaceChildren(
      el("div", { class: "ac-heading" }, el("h1", { text: t("ac.title") }), el("span", { class: "ac-mode", text: t("ac.mode") })),
      el("div", { class: "ac-phases" }, ...["phase0", "phase1", "phase2", "phase3", "later"].map(key => el("div", { class: "lc-card", text: t(`ac.${key}`) }))),
      el("p", { class: "lc-muted", text: t("ac.note") }),
      view.body?.stream ? el("section", { class: "lc-card" }, el("h2", { text: t("ac.stream") }),
        el("p", { text: `${t("ac.connections")}: ${view.body.stream.subscribers} · ${t("ac.events")}: ${view.body.stream.retained_events}` }),
        el("p", {}, el("a", { href: "api/agent/discovery", target: "_blank", text: t("ac.discovery") })),
        el("p", {}, el("a", { href: "api/agent/v1/samples?selection=all", target: "_blank", text: t("ac.samples") }))) : null,
      el("section", { class: "lc-card" }, el("h2", { text: t("ac.source") }),
        view.error ? el("p", { class: "ac-error", text: view.error }) : null,
        view.body ? el("div", { class: "ac-scroll" }, table) : el("p", { text: t("ac.wait") })),
      el("section", { class: "lc-card" }, el("h2", { text: t("ac.details") }),
        el("div", { class: "ac-observations" }, ...(view.body?.services || []).map(row => el("article", {},
          el("h3", { text: deviceName(row.service) }), el("pre", { text: JSON.stringify(row.observation, null, 2) }))))),
      el("p", { class: "lc-muted ac-local", text: t("ac.local") }));
  }
  async function refresh() {
    if (view.busy) return; view.busy = true;
    try { view.body = await view.api.request("status"); view.error = null; }
    catch (error) { view.error = error.message; }
    finally { view.busy = false; render(); }
  }
  function start() { if (!view.timer) { refresh(); view.timer = setInterval(refresh, 1500); } }
  function stop() { if (view.timer) clearInterval(view.timer); view.timer = null; }
  LabConsole.register({ id: "agent-connect", i18n, logic: { age },
    mount(root, api) { view.root = root; view.api = api; api.onLanguage(render); render(); start(); },
    show: start, hide: stop,
  });
})();
