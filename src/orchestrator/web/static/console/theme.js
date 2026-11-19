/* Apply the saved appearance before styles load. Keep storage optional. */
(function () {
  "use strict";
  let theme = "light";
  try {
    if (localStorage.getItem("labconsole.theme") === "dark") theme = "dark";
  } catch { /* Storage may be blocked. */ }
  document.documentElement.dataset.theme = theme;
})();
