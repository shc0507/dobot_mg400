"use strict";
async function refresh() {
  try {
    const [discovery, state] = await Promise.all(["discovery", "v1/state"].map(path => fetch(`/lab/api/agent/${path}`, { cache: "no-store" }).then(response => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })));
    document.getElementById("discovery").textContent = JSON.stringify(discovery, null, 2);
    document.getElementById("source-state").textContent = JSON.stringify(state, null, 2);
    const stage = state.devices?.stage, furnace = state.devices?.furnace;
    const position = stage?.values?.axes?.find(axis => axis.id === "insert")?.position_rotations;
    const temperature = furnace?.values?.pv_c;
    document.getElementById("source-readings").replaceChildren(...[["Insert position", typeof position === "number" ? `${position} rotations` : "Unknown"], ["Temperature", typeof temperature === "number" ? `${temperature} °C` : "Unknown"], ["Reading status", `${stage?.quality === "valid" ? "Up to date" : stage?.quality === "stale" ? "Outdated reading" : "Reading unavailable"} (${stage?.age_s ?? "unknown"} seconds old)`]].map(([label, value]) => {
      const card = document.createElement("article"); card.className = "reading" + (stage?.quality === "stale" ? " stale" : "");
      const title = document.createElement("span"); title.textContent = label;
      const number = document.createElement("strong"); number.textContent = value; card.append(title, number); return card;
    }));
  } catch (error) { document.getElementById("source-state").textContent = `Simulated Console unavailable: ${error.message}`; }
}
refresh(); setInterval(refresh, 1000);
