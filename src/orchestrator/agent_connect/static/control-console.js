(function () {
  "use strict";
  let epoch = null;
  const host = document.getElementById("owner-control");
  if (!host || !window.AgentOwnerPanel) return;
  async function request(path, body) {
    const headers = { Accept: "application/json", "X-MG400-Client": "phase4-mock-human" };
    if (epoch != null) headers["X-MG400-Control-Epoch"] = String(epoch);
    const options = { credentials: "same-origin", cache: "no-store", redirect: "error", headers };
    if (body !== undefined) { options.method = "POST"; headers["Content-Type"] = "application/json"; options.body = JSON.stringify(body); }
    const response = await fetch(`/lab/api/agent/control/${path}`, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "The simulated Console refused the request.");
    return data;
  }
  const panel = window.AgentOwnerPanel.mount(host, { request, changed: status => { epoch = status.control_epoch; } });
  addEventListener("pagehide", () => panel.stop());
  addEventListener("pageshow", () => panel.start());
})();
