(function () {
  "use strict";
  const key = "agent-test-theme";
  let theme;
  try { theme = localStorage.getItem(key); } catch (_) { /* Storage can be disabled. */ }
  if (!["light", "dark"].includes(theme)) theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  const apply = () => {
    document.documentElement.dataset.theme = theme;
    const button = document.getElementById("theme-toggle");
    if (button) {
      button.textContent = theme === "dark" ? "Light mode" : "Dark mode";
      button.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} mode`);
    }
  };
  apply();
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("theme-toggle")?.addEventListener("click", () => {
      theme = theme === "dark" ? "light" : "dark";
      try { localStorage.setItem(key, theme); } catch (_) { /* Keep this page usable. */ }
      apply();
    });
  });
})();
