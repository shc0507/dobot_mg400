(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const base = document.body.dataset.basePath || "";
  const validBase = base === "" || (base.startsWith("/") && !/[\\?#\u0000-\u0020]/.test(base) && !base.includes("//") && !base.endsWith("/") && !base.split("/").some(part => part === "." || part === ".."));
  const KINDS = ["steps", "samples", "artifacts", "events"];
  // The page text in both languages. The console keeps the same language choice under LANG_KEY.
  const LANG_KEY = "labconsole.lang";
  const strings = {
    en: {
      "page.title": "Chip passport", "page.console": "Lab Console", "lang.switch": "中文", "lang.label": "Switch language to Chinese",
      "page.nav": "Pages", "find.label": "Find a chip", "find.field": "QR text / chip ID", "find.placeholder": "Paste the exact QR text.",
      "find.submit": "Search records",
      "history.title": "Recorded history",
      "footer": "Only saved records appear here. Missing measurements do not mean zero. Open each run to read its records.",
      "field.steps": "Steps and transfers", "field.samples": "Measurements and thermal readings",
      "field.artifacts": "Images and other artifacts", "field.events": "Events and transfer records",
      "read.steps": "Read steps", "read.samples": "Read samples", "read.artifacts": "Read artifacts", "read.events": "Read events",
      "more.steps": "Load more steps", "more.samples": "Load more samples", "more.artifacts": "Load more artifacts", "more.events": "Load more events",
      "retry.steps": "Retry reading steps", "retry.samples": "Retry reading samples", "retry.artifacts": "Retry reading artifacts", "retry.events": "Retry reading events",
      "none": "No records loaded.", "time.none": "Time not recorded", "time.bad": "Time cannot be displayed",
      "error.path": "Invalid passport path.", "error.refused": "The service refused the record request.",
      "error.changed": "The selected chip or access settings changed.", "error.size": "Artifact exceeds the 16 MiB read limit.",
      "error.page": "The service returned an invalid record page.", "error.chip": "The record page does not match the selected chip.",
      "error.next": "The record page has an invalid continuation.", "error.artifactId": "The artifact identifier is invalid.",
      "error.runId": "The run identifier is invalid.",
      "pv.title": "Recorded furnace PV", "pv.none": "No explicit furnace PV points in the loaded measurements and events.",
      "pv.count": "{n} recorded points loaded. Lines connect recorded points. Unloaded pages are not included.",
      "pv.label": "Recorded furnace PV in degrees Celsius, ordered by recorded time",
      "pv.flat": "The recorded range cannot be plotted. Read the values below.",
      "pv.time": "Time (UTC)", "pv.source": "Time source", "pv.value": "PV (°C)", "pv.field": "Field", "pv.record": "Record",
      "pv.sample": "Sample time", "pv.recordTime": "Record time", "pv.read": "Read recorded PV values",
      "artifact.reading": "Reading the recorded artifact…", "artifact.image": "Recorded image",
      "artifact.noPreview": "This artifact has no safe raster preview.", "artifact.download": "Download recorded artifact",
      "artifact.name": "Artifact", "artifact.read": "Read artifact",
      "step.title": "Step {position}: {skill}", "step.status": "Status: {status}", "unknown": "Unknown",
      "step.times": "Started: {start} · Finished: {end}", "step.resolution": "Resolution: {text}",
      "step.params": "Recorded parameters", "step.result": "Recorded result",
      "record.where": "Step {step} · {date}", "record.noStep": "not recorded", "record.event": "Event",
      "record.measurement": "Measurement {id}", "record.data": "Recorded data",
      "records.reading": "Reading records…", "records.count": "{n} records loaded. {more}",
      "records.end": "End of records.", "records.more": "More records are available.", "records.error": "Records unavailable. {error}",
      "run.id": "Run: {id}", "run.created": "Created: {date}", "run.updated": "Updated: {date}", "run.open": "Read this run",
      "runs.reading": "Reading recorded runs…", "runs.count": "{n} runs loaded. {more}", "runs.end": "End of matching runs.",
      "runs.more": "More runs are available.", "runs.load": "Load more runs", "runs.retry": "Retry reading runs",
      "runs.loaded": "Recorded history loaded. Open a run to read its paginated records.",
      "runs.none": "No recorded runs match this exact chip ID.", "search.empty": "Enter the exact QR text or chip ID.",
      "search.path": "Invalid passport path. No requests were sent.",
      "paste.none": "Clipboard text is unavailable. Enter the exact chip ID.",
      "url.bad": "The chip URL is invalid. Enter the exact QR text to search.",
    },
    zh: {
      "page.title": "芯片履历", "page.console": "实验室控制台", "lang.switch": "English", "lang.label": "切换到英文",
      "page.nav": "页面", "find.label": "查找芯片", "find.field": "二维码文本 / 芯片 ID", "find.placeholder": "粘贴完整的二维码文本。",
      "find.submit": "查找记录",
      "history.title": "历史记录",
      "footer": "这里只显示已保存的记录。缺少测量值不表示为零。打开每次运行以读取其记录。",
      "field.steps": "步骤和转移", "field.samples": "测量和热像读数",
      "field.artifacts": "图像和其他文件", "field.events": "事件和转移记录",
      "read.steps": "读取步骤", "read.samples": "读取测量", "read.artifacts": "读取文件", "read.events": "读取事件",
      "more.steps": "加载更多步骤", "more.samples": "加载更多测量", "more.artifacts": "加载更多文件", "more.events": "加载更多事件",
      "retry.steps": "重新读取步骤", "retry.samples": "重新读取测量", "retry.artifacts": "重新读取文件", "retry.events": "重新读取事件",
      "none": "尚未加载记录。", "time.none": "未记录时间", "time.bad": "时间无法显示",
      "error.path": "履历路径无效。", "error.refused": "服务拒绝了记录请求。",
      "error.changed": "所选芯片或访问设置已改变。", "error.size": "文件超过 16 MiB 读取上限。",
      "error.page": "服务返回的记录页无效。", "error.chip": "记录页与所选芯片不符。",
      "error.next": "记录页的续页无效。", "error.artifactId": "文件标识无效。",
      "error.runId": "运行标识无效。",
      "pv.title": "炉子 PV 记录", "pv.none": "已加载的测量和事件中没有明确的炉子 PV 点。",
      "pv.count": "已加载 {n} 个记录点。连线连接记录点，不含未加载的页。",
      "pv.label": "炉子 PV 记录（摄氏度），按记录时间排序",
      "pv.flat": "记录范围无法绘图。请读取下方数值。",
      "pv.time": "时间 (UTC)", "pv.source": "时间来源", "pv.value": "PV (°C)", "pv.field": "字段", "pv.record": "记录",
      "pv.sample": "采样时间", "pv.recordTime": "记录时间", "pv.read": "读取 PV 记录值",
      "artifact.reading": "正在读取记录的文件…", "artifact.image": "记录的图像",
      "artifact.noPreview": "此文件没有安全的图像预览。", "artifact.download": "下载记录的文件",
      "artifact.name": "文件", "artifact.read": "读取文件",
      "step.title": "步骤 {position}：{skill}", "step.status": "状态：{status}", "unknown": "未知",
      "step.times": "开始：{start} · 结束：{end}", "step.resolution": "处理：{text}",
      "step.params": "记录的参数", "step.result": "记录的结果",
      "record.where": "步骤 {step} · {date}", "record.noStep": "未记录", "record.event": "事件",
      "record.measurement": "测量 {id}", "record.data": "记录的数据",
      "records.reading": "正在读取记录…", "records.count": "已加载 {n} 条记录。{more}",
      "records.end": "没有更多记录。", "records.more": "还有更多记录。", "records.error": "记录不可用。{error}",
      "run.id": "运行：{id}", "run.created": "创建：{date}", "run.updated": "更新：{date}", "run.open": "读取此次运行",
      "runs.reading": "正在读取运行记录…", "runs.count": "已加载 {n} 次运行。{more}", "runs.end": "没有更多匹配的运行。",
      "runs.more": "还有更多运行。", "runs.load": "加载更多运行", "runs.retry": "重新读取运行",
      "runs.loaded": "历史记录已加载。打开一次运行以分页读取其记录。",
      "runs.none": "没有与此芯片 ID 完全匹配的运行记录。", "search.empty": "输入完整的二维码文本或芯片 ID。",
      "search.path": "履历路径无效，未发送请求。",
      "paste.none": "无法读取剪贴板文本。请输入完整的芯片 ID。",
      "url.bad": "芯片网址无效。请输入完整的二维码文本进行查找。",
    },
  };
  let lang = "en";
  try {
    const stored = localStorage.getItem(LANG_KEY);
    lang = stored === "zh" || stored === "en" ? stored : typeof navigator !== "undefined" && String(navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
  } catch { /* Storage may be blocked; English stays. */ }
  function t(key, vars = {}) {
    let value = strings[lang][key] ?? strings.en[key] ?? key;
    for (const [name, item] of Object.entries(vars)) value = value.split("{" + name + "}").join(String(item));
    return value;
  }
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
    if (!Number.isFinite(value)) return t("time.none");
    const converted = new Date(value * 1000);
    return Number.isFinite(converted.getTime()) ? converted.toISOString() : t("time.bad");
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
    $("run-count").textContent = t("none");
    $("pp-footer").hidden = true;
    $("more-runs").hidden = true;
    $("more-runs").disabled = false;
  }

  async function get(path, savedGeneration, binary = false) {
    if (!validBase || !path.startsWith("/api/chip")) throw new Error(t("error.path"));
    const controller = new AbortController();
    controllers.add(controller);
    const timeout = setTimeout(() => controller.abort(), 10000);
    const headers = { Accept: binary ? "image/png,image/jpeg,image/gif,image/webp,application/octet-stream" : "application/json" };
    try {
      const response = await fetch(base + path, { method: "GET", headers, signal: controller.signal, redirect: "error", credentials: "omit", cache: "no-store" });
      if (!response.ok) {
        let message = t("error.refused");
        try {
          const data = await response.json();
          if (typeof data.error === "string") message = data.error;
        } catch { /* Do not render an untrusted HTML error page. */ }
        throw new Error(message);
      }
      const data = binary ? await response.blob() : await response.json();
      if (savedGeneration !== generation) throw new Error(t("error.changed"));
      if (binary) {
        if (data.size > 16 * 1024 * 1024) throw new Error(t("error.size"));
      } else if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error(t("error.page"));
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
    if (result.chip_id !== chip || result.kind !== kind || result.run_id !== runId || !Array.isArray(result.items)) throw new Error(t("error.chip"));
    if (result.next_offset !== null && (!Number.isSafeInteger(result.next_offset) || result.next_offset <= offset || result.items.length === 0)) throw new Error(t("error.next"));
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
          result.push({ time: timestamp, value, field, source: `${kind} ${row.id}`, clock: hasSampleTime ? t("pv.sample") : t("pv.recordTime") });
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
    panel.replaceChildren(node("h4", t("pv.title")));
    if (!points.length) {
      panel.append(node("p", t("pv.none"), "muted"));
      return;
    }
    panel.append(node("p", t("pv.count", { n: points.length }), "muted"));
    let minTime = Infinity, maxTime = -Infinity, minValue = Infinity, maxValue = -Infinity;
    for (const point of points) {
      minTime = Math.min(minTime, point.time); maxTime = Math.max(maxTime, point.time);
      minValue = Math.min(minValue, point.value); maxValue = Math.max(maxValue, point.value);
    }
    const dx = maxTime - minTime, dy = maxValue - minValue;
    if (Number.isFinite(dx) && Number.isFinite(dy)) {
      const svg = svgNode("svg", { viewBox: "0 0 720 230", class: "pv-plot", role: "img", "aria-label": t("pv.label") });
      const x = value => dx ? 65 + ((value - minTime) / dx) * 625 : 377;
      const y = value => dy ? 180 - ((value - minValue) / dy) * 150 : 105;
      svg.append(svgNode("path", { d: "M65 25V185H695", fill: "none", stroke: "#E0D8CF" }));
      svg.append(svgNode("text", { x: 8, y: 25 }, `${maxValue} °C`), svgNode("text", { x: 8, y: 185 }, `${minValue} °C`));
      svg.append(svgNode("text", { x: 65, y: 213 }, date(minTime)), svgNode("text", { x: 695, y: 228, "text-anchor": "end" }, date(maxTime)));
      // The same colours as the legend in passport.css: Warm Graphite and Count Grey (DESIGN.md).
      for (const [field, color] of [["pv_c", "#2A2622"], ["furnace.pv_c", "#8A7F74"]]) {
        const series = points.filter(point => point.field === field);
        if (series.length > 1) svg.append(svgNode("polyline", { points: series.map(point => `${x(point.time)},${y(point.value)}`).join(" "), fill: "none", stroke: color, "stroke-width": 2 }));
        for (const point of series) svg.append(svgNode("circle", { cx: x(point.time), cy: y(point.value), r: 3, fill: color }));
      }
      panel.append(svg);
      const legend = node("div", undefined, "pv-legend");
      legend.append(node("span", "pv_c"), node("span", "furnace.pv_c"));
      panel.append(legend);
    } else panel.append(node("p", t("pv.flat"), "muted"));
    const table = node("table"), head = node("tr");
    for (const key of ["pv.time", "pv.source", "pv.value", "pv.field", "pv.record"]) head.append(node("th", t(key)));
    const thead = node("thead"); thead.append(head); table.append(thead);
    const tbody = node("tbody");
    for (const point of points) {
      const row = node("tr");
      for (const value of [date(point.time), point.clock, point.value, point.field, point.source]) row.append(node("td", value));
      tbody.append(row);
    }
    table.append(tbody);
    const data = node("details", undefined, "pv-data");
    data.append(node("summary", t("pv.read")), table);
    panel.append(data);
  }

  async function artifact(row, button, result, savedGeneration) {
    button.disabled = true;
    result.replaceChildren(node("p", t("artifact.reading"), "muted"));
    try {
      if (!/^[0-9a-f]{32}$/.test(row.id)) throw new Error(t("error.artifactId"));
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
        image.alt = text(row.name || t("artifact.image"));
        image.src = url;
        result.append(image);
      } else result.append(node("p", t("artifact.noPreview"), "muted"));
      const link = node("a", t("artifact.download"));
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
      item.append(node("h5", t("step.title", { position: row.position, skill: row.skill })), node("p", t("step.status", { status: row.status || t("unknown") })));
      item.append(node("p", t("step.times", { start: date(row.started_at), end: date(row.finished_at) }), "muted"));
      if (row.error) item.append(node("p", row.error, "record-error"));
      if (row.resolution) item.append(node("p", t("step.resolution", { text: row.resolution })));
      item.append(details(t("step.params"), row.params), details(t("step.result"), row.result));
    } else if (kind === "artifacts") {
      item.append(node("h5", row.name || t("artifact.name")), node("p", t("record.where", { step: row.step, date: date(row.created_at) }), "muted"));
      const button = node("button", t("artifact.read"), "secondary");
      button.type = "button";
      const result = node("div", undefined, "artifact-result");
      button.addEventListener("click", () => artifact(row, button, result, savedGeneration));
      item.append(button, result);
    } else {
      item.append(node("h5", kind === "events" ? row.kind || t("record.event") : t("record.measurement", { id: row.id })));
      item.append(node("p", t("record.where", { step: row.step ?? t("record.noStep"), date: date(row.created_at) }), "muted"));
      item.append(details(t("record.data"), row.data));
    }
    return item;
  }

  async function loadSection(run, kind, savedGeneration) {
    const section = run.sections[kind];
    if (section.loading || section.next === null) return;
    section.loading = true;
    section.button.disabled = true;
    section.info.textContent = t("records.reading");
    try {
      const result = await page(kind, run.id, section.next, savedGeneration);
      if (savedGeneration !== generation) return;
      section.items.push(...result.items);
      section.next = result.next_offset;
      section.list.append(...result.items.map(row => record(kind, row, savedGeneration)));
      section.info.textContent = t("records.count", { n: section.items.length, more: t(section.next === null ? "records.end" : "records.more") });
      section.button.hidden = section.next === null;
      section.button.textContent = t("more." + kind);
      if (kind === "samples" || kind === "events") renderPV(run);
    } catch (error) {
      if (savedGeneration === generation) {
        section.info.textContent = t("records.error", { error: text(error.message) });
        section.button.textContent = t("retry." + kind);
      }
    } finally { section.loading = false; section.button.disabled = false; }
  }

  // The tone of a run status in passport.css. The status word always shows beside it.
  const TONES = { completed: "tone-ok", running: "tone-info", held: "tone-warn", failed: "tone-bad", aborted: "tone-bad" };
  const statusTone = status => TONES[status] || "tone-off";

  function runCard(row, savedGeneration) {
    if (!/^[0-9a-f]{32}$/.test(row.id)) throw new Error(t("error.runId"));
    const card = node("article", undefined, "panel passport-run");
    const heading = node("div", undefined, "section-heading");
    heading.append(node("h3", row.name || row.id), node("span", row.status || t("unknown"), "badge " + statusTone(row.status)));
    card.append(heading, node("p", row.detail || ""));
    const meta = node("div", undefined, "run-meta");
    meta.append(node("span", t("run.id", { id: row.id })), node("span", t("run.created", { date: date(row.created_at) })), node("span", t("run.updated", { date: date(row.updated_at) })));
    card.append(meta);
    const open = node("button", t("run.open"), "secondary"); open.type = "button";
    const sections = node("div", undefined, "record-sections");
    card.append(open, sections);
    const run = { id: row.id, sections: {}, pv: node("section", undefined, "pv-panel") };
    open.addEventListener("click", () => {
      if (savedGeneration !== generation || open.disabled) return;
      open.disabled = true;
      open.hidden = true;
      sections.append(run.pv);
      renderPV(run);
      for (const kind of KINDS) {
        const section = node("section", undefined, "record-section");
        const info = node("p", t("none"), "muted");
        const list = node("div", undefined, "record-list");
        const button = node("button", t("read." + kind), "secondary"); button.type = "button";
        section.append(node("h4", t("field." + kind)), info, list, button);
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
    status(t("runs.reading"));
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
      $("run-count").textContent = t("runs.count", { n: runs.size, more: t(nextRun === null ? "runs.end" : "runs.more") });
      $("more-runs").hidden = nextRun === null;
      $("pp-footer").hidden = !runs.size;
      $("more-runs").textContent = t("runs.load");
      status(t(runs.size ? "runs.loaded" : "runs.none"));
    } catch (error) {
      if (savedGeneration === generation) {
        status(t("records.error", { error: error.message }), true);
        $("more-runs").hidden = false;
        $("more-runs").textContent = t("runs.retry");
      }
    } finally {
      if (savedGeneration === generation) { loadingRuns = false; $("more-runs").disabled = false; }
    }
  }

  function search(updateLocation = true) {
    reset();
    chip = exactInput === null ? $("chip-id").value : exactInput;
    if (!chip.trim()) { status(t("search.empty"), true); return; }
    if (!validBase) { status(t("search.path"), true); return; }
    // The history section shows from the first search on.
    $("pp-history").hidden = false;
    $("selected-chip").textContent = text(chip);
    if (updateLocation) history.replaceState(null, "", base + "/chip?" + new URLSearchParams({ id: chip }).toString());
    nextRun = 0;
    loadRuns(generation);
  }
  $("passport-form").addEventListener("submit", event => { event.preventDefault(); search(); });
  $("chip-id").addEventListener("input", () => { exactInput = $("chip-id").value; });
  $("chip-id").addEventListener("paste", event => {
    event.preventDefault();
    if (!event.clipboardData) { status(t("paste.none"), true); return; }
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

  /** Put the page text in the selected language. A loaded chip is read again in that language. */
  function applyLanguage() {
    if (document.documentElement) document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
    document.title = t("page.title");
    for (const [id, key] of [["pp-title", "page.title"], ["pp-console", "page.console"], ["pp-lang", "lang.switch"], ["pp-label", "find.field"],
      ["pp-search", "find.submit"], ["passport-heading", "history.title"], ["pp-footer", "footer"]]) $(id).textContent = t(key);
    $("pp-lang").setAttribute("aria-label", t("lang.label"));
    $("pp-nav").setAttribute("aria-label", t("page.nav"));
    $("pp-find").setAttribute("aria-label", t("find.label"));
    $("chip-id").setAttribute("placeholder", t("find.placeholder"));
  }
  $("pp-lang").addEventListener("click", () => {
    lang = lang === "en" ? "zh" : "en";
    try { localStorage.setItem(LANG_KEY, lang); } catch { /* Storage may be blocked. */ }
    applyLanguage();
    if (chip) search(false);
  });
  applyLanguage();
  try {
    const url = new URL(location.href);
    const path = base + "/chip/";
    const initial = url.searchParams.has("id") ? url.searchParams.get("id") : url.pathname.startsWith(path) ? decodeURIComponent(url.pathname.slice(path.length)) : "";
    exactInput = initial;
    $("chip-id").value = initial;
    if (initial) search(false);
  } catch { status(t("url.bad"), true); }
})();
