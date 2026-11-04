(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const base = document.body.dataset.basePath || "";
  const validBase = base === "" || (base.startsWith("/") && !/[\\?#\u0000-\u0020]/.test(base) && !base.includes("//") && !base.endsWith("/") && !base.split("/").some(part => part === "." || part === ".."));
  const fields = { steps: "Steps and transfers", samples: "Measurements and thermal readings", artifacts: "Images and other artifacts", events: "Events and transfer records" };
  const controllers = new Set(), blobs = new Set(), runs = new Map();
  let chip = "", generation = 0, nextRun = null, loadingRuns = false;
  // Textareas normalize CR and CRLF. Keep the exact URL/clipboard identity
  // separately until a user edit supplies a new identity.
  let exactInput = null;

  const text = value => String(value ?? "");
  function node(tag, value, className = "") {
    const result = document.createElement(tag);
    if (value !== undefined) result.textContent = text(value);
    if (className) result.className = className;
    return result;
  }
  function status(message, error = false) {
    $("passport-status").textContent = text(message);
    $("passport-status").className = "notice" + (error ? " error" : "");
  }
  function date(value) {
    if (!Number.isFinite(value)) return "Time not recorded";
    const converted = new Date(value * 1000);
    return Number.isFinite(converted.getTime()) ? converted.toISOString() : "Time cannot be displayed";
  }
  function details(label, value) {
    const item = node("details");
    item.append(node("summary", label), node("pre", JSON.stringify(value, null, 2), "record-json"));
    return item;
  }
  function reset() {
    generation++;
    for (const controller of controllers) controller.abort();
    controllers.clear();
    for (const url of blobs) URL.revokeObjectURL(url);
    blobs.clear();
    runs.clear();
    nextRun = null;
    loadingRuns = false;
    $("passport-runs").replaceChildren();
    $("selected-chip").textContent = "";
    $("run-count").textContent = "No records loaded.";
    $("more-runs").hidden = true;
    $("more-runs").disabled = false;
  }

  async function get(path, savedGeneration, binary = false) {
    if (!validBase || !path.startsWith("/api/chip")) throw new Error("Invalid passport path.");
    const controller = new AbortController();
    controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 10000);
    const headers = { Accept: binary ? "image/png,image/jpeg,image/gif,image/webp,application/octet-stream" : "application/json" };
    try {
      const response = await fetch(base + path, { method: "GET", headers, signal: controller.signal, redirect: "error", credentials: "omit", cache: "no-store" });
      if (!response.ok) {
        let message = "The service refused the record request.";
        try {
          const data = await response.json();
          if (typeof data.error === "string") message = data.error;
        } catch { /* Do not render an untrusted HTML error page. */ }
        throw new Error(message);
      }
      const data = binary ? await response.blob() : await response.json();
      if (savedGeneration !== generation) throw new Error("The selected chip or access settings changed.");
      if (binary) {
        if (data.size > 16 * 1024 * 1024) throw new Error("Artifact exceeds the 16 MiB read limit.");
      } else if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error("The service returned an invalid record page.");
      return data;
    } finally {
      clearTimeout(timeout);
      controllers.delete(controller);
    }
  }

  async function page(kind, runId, offset, savedGeneration) {
    const query = new URLSearchParams({ id: chip, kind, limit: kind === "runs" ? "25" : "100", offset: String(offset) });
    if (runId !== null) query.set("run_id", runId);
    const result = await get("/api/chip?" + query.toString(), savedGeneration);
    if (result.chip_id !== chip || result.kind !== kind || result.run_id !== runId || !Array.isArray(result.items)) throw new Error("The record page does not match the selected chip.");
    if (result.next_offset !== null && (!Number.isSafeInteger(result.next_offset) || result.next_offset <= offset || result.items.length === 0)) throw new Error("The record page has an invalid continuation.");
    return result;
  }

  function pvPoints(run) {
    const result = [];
    for (const kind of ["samples", "events"]) {
      for (const row of run.sections[kind]?.items || []) {
        const data = row.data;
        if (!data || typeof data !== "object" || Array.isArray(data)) continue;
        for (const [field, value, sampleTime] of [["pv_c", data.pv_c, data.sample_time], ["furnace.pv_c", data.furnace?.pv_c, data.furnace?.sample_time]]) {
          if (!Number.isFinite(value)) continue;
          const hasSampleTime = Number.isFinite(sampleTime) && sampleTime > 0;
          const timestamp = hasSampleTime ? sampleTime : row.created_at;
          if (!Number.isFinite(timestamp)) continue;
          result.push({ time: timestamp, value, field, source: `${kind} ${row.id}`, clock: hasSampleTime ? "Sample time" : "Record time" });
        }
      }
    }
    return result.sort((left, right) => left.time - right.time);
  }
  function svgNode(tag, attributes, value) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, item] of Object.entries(attributes)) element.setAttribute(key, String(item));
    if (value !== undefined) element.textContent = text(value);
    return element;
  }
  function renderPV(run) {
    const points = pvPoints(run);
    const panel = run.pv;
    panel.replaceChildren(node("h4", "Recorded furnace PV"));
    if (!points.length) {
      panel.append(node("p", "No explicit furnace PV points in the loaded measurements and events.", "muted"));
      return;
    }
    panel.append(node("p", `${points.length} recorded points loaded. Lines connect recorded points. Unloaded pages are not included.`, "muted"));
    let minTime = Infinity, maxTime = -Infinity, minValue = Infinity, maxValue = -Infinity;
    for (const point of points) {
      minTime = Math.min(minTime, point.time); maxTime = Math.max(maxTime, point.time);
      minValue = Math.min(minValue, point.value); maxValue = Math.max(maxValue, point.value);
    }
    const dx = maxTime - minTime, dy = maxValue - minValue;
    if (Number.isFinite(dx) && Number.isFinite(dy)) {
      const svg = svgNode("svg", { viewBox: "0 0 720 230", class: "pv-plot", role: "img", "aria-label": "Recorded furnace PV in degrees Celsius, ordered by recorded time" });
      const x = value => dx ? 65 + ((value - minTime) / dx) * 625 : 377;
      const y = value => dy ? 180 - ((value - minValue) / dy) * 150 : 105;
      svg.append(svgNode("path", { d: "M65 25V185H695", fill: "none", stroke: "#a9bfc5" }));
      svg.append(svgNode("text", { x: 8, y: 25 }, `${maxValue} °C`), svgNode("text", { x: 8, y: 185 }, `${minValue} °C`));
      svg.append(svgNode("text", { x: 65, y: 213 }, date(minTime)), svgNode("text", { x: 695, y: 228, "text-anchor": "end" }, date(maxTime)));
      for (const [field, color] of [["pv_c", "#075f68"], ["furnace.pv_c", "#8b4b18"]]) {
        const series = points.filter(point => point.field === field);
        if (series.length > 1) svg.append(svgNode("polyline", { points: series.map(point => `${x(point.time)},${y(point.value)}`).join(" "), fill: "none", stroke: color, "stroke-width": 2 }));
        for (const point of series) svg.append(svgNode("circle", { cx: x(point.time), cy: y(point.value), r: 3, fill: color }));
      }
      panel.append(svg);
      const legend = node("div", undefined, "pv-legend");
      legend.append(node("span", "pv_c"), node("span", "furnace.pv_c"));
      panel.append(legend);
    } else panel.append(node("p", "The recorded range cannot be plotted. Read the values below.", "muted"));
    const table = node("table"), head = node("tr");
    for (const label of ["Time (UTC)", "Time source", "PV (°C)", "Field", "Record"]) head.append(node("th", label));
    const thead = node("thead"); thead.append(head); table.append(thead);
    const tbody = node("tbody");
    for (const point of points) {
      const row = node("tr");
      for (const value of [date(point.time), point.clock, point.value, point.field, point.source]) row.append(node("td", value));
      tbody.append(row);
    }
    table.append(tbody);
    const data = node("details", undefined, "pv-data");
    data.append(node("summary", "Read recorded PV values"), table);
    panel.append(data);
  }

  async function artifact(row, button, result, savedGeneration) {
    button.disabled = true;
    result.replaceChildren(node("p", "Reading the recorded artifact…", "muted"));
    try {
      if (!/^[0-9a-f]{32}$/.test(row.id)) throw new Error("The artifact identifier is invalid.");
      const query = new URLSearchParams({ id: chip });
      const blob = await get("/api/chip/artifact/" + row.id + "?" + query.toString(), savedGeneration, true);
      if (savedGeneration !== generation) return;
      const raster = ["image/png", "image/jpeg", "image/gif", "image/webp"].includes(blob.type);
      const url = URL.createObjectURL(raster ? blob : new Blob([blob], { type: "application/octet-stream" }));
      blobs.add(url);
      result.replaceChildren();
      const filename = String(row.name || "artifact").replace(/[\\/\u0000-\u001f\u007f]/g, "_");
      if (raster) {
        const image = node("img", undefined, "record-image");
        image.alt = text(row.name || "Recorded image");
        image.src = url;
        result.append(image);
      } else result.append(node("p", "This artifact has no safe raster preview.", "muted"));
      const link = node("a", "Download recorded artifact");
      link.href = url;
      link.download = filename;
      result.append(link);
      button.hidden = true;
    } catch (error) {
      if (savedGeneration === generation) result.replaceChildren(node("p", error.message, "record-error"));
    } finally { button.disabled = false; }
  }

  function record(kind, row, savedGeneration) {
    const item = node("article", undefined, "record-item");
    if (kind === "steps") {
      item.append(node("h5", `Step ${row.position}: ${row.skill}`), node("p", `Status: ${row.status || "Unknown"}`));
      item.append(node("p", `Started: ${date(row.started_at)} · Finished: ${date(row.finished_at)}`, "muted"));
      if (row.error) item.append(node("p", row.error, "record-error"));
      if (row.resolution) item.append(node("p", "Resolution: " + row.resolution));
      item.append(details("Recorded parameters", row.params), details("Recorded result", row.result));
    } else if (kind === "artifacts") {
      item.append(node("h5", row.name || "Artifact"), node("p", `Step ${row.step} · ${date(row.created_at)}`, "muted"));
      const button = node("button", "Read artifact", "secondary");
      button.type = "button";
      const result = node("div", undefined, "artifact-result");
      button.addEventListener("click", () => artifact(row, button, result, savedGeneration));
      item.append(button, result);
    } else {
      item.append(node("h5", kind === "events" ? row.kind || "Event" : "Measurement " + row.id));
      item.append(node("p", `Step ${row.step ?? "not recorded"} · ${date(row.created_at)}`, "muted"));
      item.append(details("Recorded data", row.data));
    }
    return item;
  }

  async function loadSection(run, kind, savedGeneration) {
    const section = run.sections[kind];
    if (section.loading || section.next === null) return;
    section.loading = true;
    section.button.disabled = true;
    section.info.textContent = "Reading records…";
    try {
      const result = await page(kind, run.id, section.next, savedGeneration);
      if (savedGeneration !== generation) return;
      section.items.push(...result.items);
      section.next = result.next_offset;
      section.list.append(...result.items.map(row => record(kind, row, savedGeneration)));
      section.info.textContent = `${section.items.length} records loaded. ${section.next === null ? "End of records." : "More records are available."}`;
      section.button.hidden = section.next === null;
      section.button.textContent = "Load more " + kind;
      if (kind === "samples" || kind === "events") renderPV(run);
    } catch (error) {
      if (savedGeneration === generation) {
        section.info.textContent = "Records unavailable. " + text(error.message);
        section.button.textContent = "Retry reading " + kind;
      }
    } finally { section.loading = false; section.button.disabled = false; }
  }

  function runCard(row, savedGeneration) {
    if (!/^[0-9a-f]{32}$/.test(row.id)) throw new Error("The run identifier is invalid.");
    const card = node("article", undefined, "panel passport-run");
    const heading = node("div", undefined, "section-heading");
    heading.append(node("h3", row.name || row.id), node("span", row.status || "Unknown", "badge neutral"));
    card.append(heading, node("p", row.detail || ""));
    const meta = node("div", undefined, "run-meta");
    meta.append(node("span", "Run: " + row.id), node("span", "Created: " + date(row.created_at)), node("span", "Updated: " + date(row.updated_at)));
    card.append(meta);
    const open = node("button", "Read this run", "secondary"); open.type = "button";
    const sections = node("div", undefined, "record-sections");
    card.append(open, sections);
    const run = { id: row.id, sections: {}, pv: node("section", undefined, "pv-panel") };
    open.addEventListener("click", () => {
      if (savedGeneration !== generation || open.disabled) return;
      open.disabled = true;
      open.hidden = true;
      sections.append(run.pv);
      renderPV(run);
      for (const [kind, label] of Object.entries(fields)) {
        const section = node("section", undefined, "record-section");
        const info = node("p", "No records loaded.", "muted");
        const list = node("div", undefined, "record-list");
        const button = node("button", "Read " + kind, "secondary"); button.type = "button";
        section.append(node("h4", label), info, list, button);
        sections.append(section);
        run.sections[kind] = { items: [], next: 0, loading: false, info, list, button };
        button.addEventListener("click", () => loadSection(run, kind, savedGeneration));
        loadSection(run, kind, savedGeneration);
      }
    });
    return card;
  }

  async function loadRuns(savedGeneration) {
    if (loadingRuns || nextRun === null) return;
    loadingRuns = true;
    $("more-runs").disabled = true;
    status("Reading recorded runs…");
    try {
      const result = await page("runs", null, nextRun, savedGeneration);
      if (savedGeneration !== generation) return;
      const additions = [];
      for (const row of result.items) {
        if (runs.has(row.id)) continue;
        additions.push([row.id, runCard(row, savedGeneration)]);
      }
      for (const [id, card] of additions) { runs.set(id, card); $("passport-runs").append(card); }
      nextRun = result.next_offset;
      $("run-count").textContent = `${runs.size} runs loaded. ${nextRun === null ? "End of matching runs." : "More runs are available."}`;
      $("more-runs").hidden = nextRun === null;
      $("more-runs").textContent = "Load more runs";
      status(runs.size ? "Recorded history loaded. Open a run to read its paginated records." : "No recorded runs match this exact chip ID.");
    } catch (error) {
      if (savedGeneration === generation) {
        status("Records unavailable. " + error.message, true);
        $("more-runs").hidden = false;
        $("more-runs").textContent = "Retry reading runs";
      }
    } finally {
      if (savedGeneration === generation) { loadingRuns = false; $("more-runs").disabled = false; }
    }
  }

  function search(updateLocation = true) {
    reset();
    chip = exactInput === null ? $("chip-id").value : exactInput;
    if (!chip.trim()) { status("Enter the exact QR text or chip ID.", true); return; }
    if (!validBase) { status("Invalid passport path. No requests were sent.", true); return; }
    $("selected-chip").textContent = text(chip);
    if (updateLocation) history.replaceState(null, "", base + "/chip?" + new URLSearchParams({ id: chip }).toString());
    nextRun = 0;
    loadRuns(generation);
  }
  $("passport-form").addEventListener("submit", event => { event.preventDefault(); search(); });
  $("chip-id").addEventListener("input", () => { exactInput = $("chip-id").value; });
  $("chip-id").addEventListener("paste", event => {
    event.preventDefault();
    if (!event.clipboardData) { status("Clipboard text is unavailable. Enter the exact chip ID.", true); return; }
    const field = $("chip-id");
    const original = exactInput === null ? field.value : exactInput;
    const rawOffset = offset => {
      let position = 0, visible = 0;
      while (position < original.length && visible < offset) {
        position += original[position] === "\r" && original[position + 1] === "\n" ? 2 : 1;
        visible++;
      }
      return position;
    };
    const start = rawOffset(field.selectionStart), end = rawOffset(field.selectionEnd);
    const pasted = event.clipboardData.getData("text/plain");
    exactInput = original.slice(0, start) + pasted + original.slice(end);
    field.value = exactInput;
    const cursor = (original.slice(0, start) + pasted).replace(/\r\n?/g, "\n").length;
    field.setSelectionRange(cursor, cursor);
  });
  $("more-runs").addEventListener("click", () => loadRuns(generation));
  window.addEventListener("pagehide", reset);
  try {
    const url = new URL(location.href);
    const path = base + "/chip/";
    const initial = url.searchParams.has("id") ? url.searchParams.get("id") : url.pathname.startsWith(path) ? decodeURIComponent(url.pathname.slice(path.length)) : "";
    exactInput = initial;
    $("chip-id").value = initial;
    if (initial) search(false);
  } catch { status("The chip URL is invalid. Enter the exact QR text to search.", true); }
})();
