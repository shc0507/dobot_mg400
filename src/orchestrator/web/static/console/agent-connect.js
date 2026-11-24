(function () {
  "use strict";
  const { LabConsole } = window;
  const view = { api: null, root: null, timer: null, busy: false, body: null, error: null, owner: null };
  const i18n = {
    en: {
      "ac.title": "Agent Connect", "ac.mode": "Connection and control", "ac.source": "Shared device cache",
      "ac.phase0": "Phase 0 · Local mock portal", "ac.phase1": "Phase 1 · Service observations",
      "ac.phase2": "Phase 2 · Chip locations and evidence", "ac.phase3": "Phase 3 · Read-only message stream",
      "ac.phase4": "Phase 4 · Connection and handover", "ac.phase5": "Phase 5 · Simulated commands",
      "ac.phase6": "Phase 6 · Simulated Stop and recovery",
      "ac.phase7": "Phase 7 · Live data and release identity", "ac.phase8": "Phase 8 · Live control",
      "ac.phase9": "Phase 9 · Connection-loss warning", "ac.note": "Grant operator control to enable agent commands. Equipment limits remain active.",
      "ac.diagnostics": "Data and service details", "ac.loading": "Loading connection controls…",
      "ac.stream": "Message stream", "ac.connections": "Connected readers", "ac.events": "Saved events for reconnect", "ac.discovery": "Open the data agreement discovery", "ac.samples": "Browse chip records (JSON)",
      "ac.local": "On your PC, run python scripts/agent_portal.py. Open http://127.0.0.1:8792/.",
      "ac.service": "Service", "ac.state": "Cache state", "ac.age": "Cache age", "ac.instance": "Process instance",
      "ac.details": "Observed fields", "ac.missing": "Not published", "ac.wait": "Waiting for cache diagnostics.",
    },
    zh: {
      "ac.title": "智能体连接", "ac.mode": "连接与控制", "ac.source": "共用设备缓存",
      "ac.phase0": "阶段 0 · 本地模拟测试页", "ac.phase1": "阶段 1 · 服务观测数据",
      "ac.phase2": "阶段 2 · 芯片位置与证据", "ac.phase3": "阶段 3 · 只读消息流",
      "ac.phase4": "阶段 4 · 连接与交接", "ac.phase5": "阶段 5 · 模拟命令",
      "ac.phase6": "阶段 6 · 模拟停止与恢复",
      "ac.phase7": "阶段 7 · 实时数据与版本", "ac.phase8": "阶段 8 · 真机控制",
      "ac.phase9": "阶段 9 · 断连警告", "ac.note": "授予操作权限后，代理可发送命令。设备限位保持生效。",
      "ac.diagnostics": "数据与服务详情", "ac.loading": "正在加载连接控制…",
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
      el("div", { class: "ac-phases" }, ...["phase0", "phase1", "phase2", "phase3", "phase4", "phase5", "phase6", "phase7", "phase8", "phase9"].map(key => el("div", { class: "lc-card", text: t(`ac.${key}`) }))),
      view.body?.stream ? el("section", { class: "lc-card" }, el("h2", { text: t("ac.stream") }),
        el("p", { text: `${t("ac.connections")}: ${view.body.stream.subscribers} · ${t("ac.events")}: ${view.body.stream.retained_events}` }),
        el("p", {}, el("a", { href: "api/agent/discovery", target: "_blank", text: t("ac.discovery") })),
        el("p", {}, el("a", { href: "api/agent/v1/samples?selection=all", target: "_blank", text: t("ac.samples") }))) : null,
      el("section", { class: "lc-card" }, el("h2", { text: t("ac.source") }),
        view.error ? el("p", { class: "ac-error", text: view.error }) : null,
        view.body ? el("div", { class: "ac-scroll" }, table) : el("p", { text: t("ac.wait") })),
      el("section", { class: "lc-card" }, el("h2", { text: t("ac.details") }),
        el("div", { class: "ac-observations" }, ...(view.body?.services || []).map(row => el("article", {},
          el("h3", { text: deviceName(row.service) }), el("pre", { text: JSON.stringify(row.observation, null, 2) }))))));
  }
  async function refresh() {
    if (view.busy) return; view.busy = true;
    try { view.body = await view.api.request("status"); view.error = null; }
    catch (error) { view.error = error.message; }
    finally { view.busy = false; render(); }
  }
  function loadOwnerPanel(api) {
    const path = `${api.base}/api/agent/control/assets/`;
    if (window.AgentOwnerPanel) return Promise.resolve(window.AgentOwnerPanel);
    const stylesheet = document.createElement("link"); stylesheet.rel = "stylesheet"; stylesheet.href = path + "control-owner.css";
    document.head.append(stylesheet);
    return new Promise((resolve, reject) => {
      const script = document.createElement("script"); script.src = path + "control-owner.js";
      script.onload = () => resolve(window.AgentOwnerPanel); script.onerror = () => reject(new Error("Connection controls could not load. Reload the page."));
      document.head.append(script);
    });
  }
  function start() { view.owner?.start(); if (!view.timer) { refresh(); view.timer = setInterval(refresh, 1500); } }
  function stop() { view.owner?.stop(); if (view.timer) clearInterval(view.timer); view.timer = null; }
  LabConsole.register({ id: "agent-connect", i18n, logic: { age },
    mount(root, api) {
      view.api = api; const { el, t } = api;
      const owner = el("div", { text: t("ac.loading") });
      view.root = el("div", { class: "ac-diagnostics" });
      root.replaceChildren(el("div", { class: "ac-heading" }, el("h1", { text: t("ac.title") }), el("span", { class: "ac-mode", text: t("ac.mode") })),
        owner, el("details", { class: "lc-card ac-details" }, el("summary", { text: t("ac.diagnostics") }), view.root),
        el("p", { class: "lc-muted ac-local", text: t("ac.local") }));
      loadOwnerPanel(api).then(module => {
        view.owner = module.mount(owner, { request: (path, body) => api.consoleRequest(`/api/agent/control/${path}`, body), changed: status => LabConsole.setAuthority?.(status) });
        if (!view.timer) view.owner.stop();
      }).catch(error => { owner.textContent = error.message; owner.className = "ac-error"; });
      api.onLanguage(render); render(); start();
    },
    show: start, hide: stop,
  });
})();
