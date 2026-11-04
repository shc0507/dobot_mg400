/* Placeholder for the Motion workspace. Its owner task replaces this file. */
(function () {
  "use strict";
  if (typeof window === "undefined" || !window.LabConsole) return;
  const links = [["robot", "/robot/"], ["stage", "/stage/"]];
  window.LabConsole.register({
    id: "motion",
    i18n: {
      en: { "placeholder.title": "Motion", "placeholder.text": "This workspace is not built yet. Use these pages:", "page.robot": "Robot panel", "page.stage": "Stage panel", "page.camera": "Camera preview", "page.thermal": "Thermal camera", "page.furnace": "Furnace", "page.microscope": "Microscope" },
      zh: { "placeholder.title": "运动", "placeholder.text": "此工作区尚未完成。请使用这些页面：", "page.robot": "机械臂面板", "page.stage": "位移台面板", "page.camera": "相机预览", "page.thermal": "热像仪", "page.furnace": "炉子", "page.microscope": "显微镜" },
    },
    mount(root, api) {
      const render = () => root.replaceChildren(api.el("section", { class: "lc-card", "aria-labelledby": "motion-title" },
        api.el("h2", { id: "motion-title", text: api.t("placeholder.title") }),
        api.el("p", { class: "lc-muted", text: api.t("placeholder.text") }),
        api.el("ul", { class: "lc-links" }, ...links.map(([name, href]) => api.el("li", {}, api.el("a", { class: "lc-link", href, text: api.t("page." + name) }))))));
      api.onLanguage(render);
      render();
    },
  });
})();
