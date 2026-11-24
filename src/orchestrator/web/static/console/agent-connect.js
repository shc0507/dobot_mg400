(function () {
  "use strict";
  const { LabConsole } = window;
  const DOCS = "https://github.com/shc0507/dobot_mg400/blob/main/";
  const view = { api: null, root: null, timer: null, busy: false, body: null, error: null, owner: null,
    visible: false, tab: "connections", tabs: {}, panels: {} };
  const i18n = {
    en: {
      "ac.title": "Agent Connect", "ac.mode": "Connection and control", "ac.source": "Shared device cache",
      "ac.connectionsTab": "Connections", "ac.testingTab": "Testing", "ac.tabs": "Agent Connect views",
      "ac.guide": "Connect your agent", "ac.guideNote": "Give these guides to your coding agent before connecting.",
      "ac.prompt": "Setup prompt", "ac.knowledge": "Project knowledge", "ac.contract": "Communication contract",
      "ac.diagnostics": "Data and service details", "ac.loading": "Loading connection controls…",
      "ac.stream": "Message stream", "ac.connections": "Connected readers", "ac.events": "Saved events for reconnect", "ac.discovery": "Open the data agreement discovery", "ac.samples": "Browse chip records (JSON)",
      "ac.service": "Service", "ac.state": "Cache state", "ac.age": "Cache age", "ac.instance": "Process instance",
      "ac.details": "Observed fields", "ac.missing": "Not published", "ac.wait": "Waiting for cache diagnostics.",
    },
    zh: {
      "ac.title": "智能体连接", "ac.mode": "连接与控制", "ac.source": "共用设备缓存",
      "ac.connectionsTab": "连接", "ac.testingTab": "测试", "ac.tabs": "智能体连接视图",
      "ac.guide": "接入智能体", "ac.guideNote": "连接前，请让编程智能体阅读这些文档。",
      "ac.prompt": "接入提示词", "ac.knowledge": "项目知识", "ac.contract": "通信契约",
      "ac.diagnostics": "数据与服务详情", "ac.loading": "正在加载连接控制…",
      "ac.stream": "消息流", "ac.connections": "已连接读取端", "ac.events": "重连可读取的事件", "ac.discovery": "打开数据协议发现接口", "ac.samples": "浏览芯片记录（JSON）",
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
    view.root.replaceChildren(...[
      view.body?.stream ? el("section", { class: "lc-card" }, el("h2", { text: t("ac.stream") }),
        el("p", { text: `${t("ac.connections")}: ${view.body.stream.subscribers} · ${t("ac.events")}: ${view.body.stream.retained_events}` }),
        el("p", {}, el("a", { href: "api/agent/discovery", target: "_blank", rel: "noopener noreferrer", text: t("ac.discovery") })),
        el("p", {}, el("a", { href: "api/agent/v1/samples?selection=all", target: "_blank", rel: "noopener noreferrer", text: t("ac.samples") }))) : null,
      el("section", { class: "lc-card" }, el("h2", { text: t("ac.source") }),
        view.error ? el("p", { class: "ac-error", text: view.error }) : null,
        view.body ? el("div", { class: "ac-scroll" }, table) : el("p", { text: t("ac.wait") })),
      el("section", { class: "lc-card" }, el("h2", { text: t("ac.details") }),
        el("div", { class: "ac-observations" }, ...(view.body?.services || []).map(row => el("article", {},
          el("h3", { text: deviceName(row.service) }), el("pre", { text: JSON.stringify(row.observation, null, 2) }))))),
    ].filter(Boolean));
  }
  async function refresh() {
    if (view.busy || !view.visible || view.tab !== "testing") return; view.busy = true;
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
  function polling() {
    if (view.visible && view.tab === "testing") {
      if (!view.timer) { refresh(); view.timer = setInterval(refresh, 1500); }
    } else { if (view.timer) clearInterval(view.timer); view.timer = null; }
  }
  function selectTab(name, focus = false) {
    if (!Object.hasOwn(view.tabs, name)) return;
    view.tab = name;
    for (const [key, button] of Object.entries(view.tabs)) {
      button.setAttribute("aria-selected", String(key === name)); button.tabIndex = key === name ? 0 : -1;
      view.panels[key].hidden = key !== name;
    }
    if (focus) view.tabs[name].focus();
    polling();
  }
  function start() { view.visible = true; view.owner?.start(); polling(); }
  function stop() { view.visible = false; view.owner?.stop(); polling(); }
  LabConsole.register({ id: "agent-connect", i18n, logic: { age },
    mount(root, api) {
      view.api = api; const { el, t } = api; const labels = [];
      function label(tag, key, attributes = {}) {
        const node = el(tag, { ...attributes, text: t(`ac.${key}`) }); labels.push([node, key]); return node;
      }
      const owner = el("div", { text: t("ac.loading") });
      const tests = el("div", {}), stops = el("div", {}), messages = el("div", { class: "ac-messages" });
      view.root = el("div", { class: "ac-diagnostics" });
      view.tabs = Object.fromEntries(["connections", "testing"].map(name => [name,
        label("button", `${name}Tab`, { type: "button", id: `ac-tab-${name}`, role: "tab", "aria-controls": `ac-panel-${name}` })]));
      const names = Object.keys(view.tabs);
      for (const [name, button] of Object.entries(view.tabs)) {
        button.addEventListener("click", () => selectTab(name));
        button.addEventListener("keydown", event => {
          const index = names.indexOf(name);
          const target = event.key === "Home" ? 0 : event.key === "End" ? names.length - 1
            : event.key === "ArrowRight" ? (index + 1) % names.length : event.key === "ArrowLeft" ? (index + names.length - 1) % names.length : null;
          if (target !== null) { event.preventDefault(); selectTab(names[target], true); }
        });
      }
      const nav = el("nav", { class: "ac-tabs", role: "tablist", "aria-label": t("ac.tabs") }, ...Object.values(view.tabs));
      const guide = el("section", { class: "lc-card ac-guide" }, label("h2", "guide"), label("p", "guideNote"),
        el("div", { class: "ac-guide-links" }, ...[
          ["prompt", "README.en.md#connect-an-external-agent"],
          ["knowledge", "docs/agent-connect/project-knowledge.md"],
          ["contract", "docs/agent-connect/communication-contract.md"],
        ].map(([key, path]) => label("a", key, { href: DOCS + path, target: "_blank", rel: "noopener noreferrer" }))));
      view.panels = {
        connections: el("section", { id: "ac-panel-connections", role: "tabpanel", "aria-labelledby": "ac-tab-connections" }, owner, guide),
        testing: el("section", { id: "ac-panel-testing", role: "tabpanel", "aria-labelledby": "ac-tab-testing" }, tests,
          el("details", { class: "lc-card ac-details" }, label("summary", "diagnostics"), view.root)),
      };
      root.replaceChildren(el("div", { class: "ac-heading" }, label("h1", "title"), label("span", "mode", { class: "ac-mode" })),
        nav, messages, view.panels.connections, view.panels.testing, stops);
      selectTab("connections");
      loadOwnerPanel(api).then(module => {
        view.owner = module.mount(owner, { testContainer: tests, stopContainer: stops, messageContainer: messages,
          request: (path, body) => api.consoleRequest(`/api/agent/control/${path}`, body), changed: status => LabConsole.setAuthority?.(status) });
        if (!view.visible) view.owner.stop();
      }).catch(error => { owner.textContent = error.message; owner.className = "ac-error"; });
      api.onLanguage(() => { for (const [node, key] of labels) node.textContent = t(`ac.${key}`); nav.setAttribute("aria-label", t("ac.tabs")); render(); });
      render(); start();
    },
    show: start, hide: stop,
  });
})();
