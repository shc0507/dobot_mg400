"use strict";

const $ = (id) => document.getElementById(id);
const canvas = $("thermal");
const ctx = canvas.getContext("2d");
const sourceCanvas = document.createElement("canvas");
const sourceCtx = sourceCanvas.getContext("2d");
const displayCanvas = document.createElement("canvas");
const displayCtx = displayCanvas.getContext("2d");
const viewerPanel = $("viewerPanel");
let frame = null;
let receivedAt = 0;
let frozen = false;
let pinned = false;
let probe = null;
let status = {state: "disconnected", source: "camera"};
let history = [];
let actionPending = false;
let actionError = "";
let transportError = "";
let lastGain = "high";
let gainKnown = false;
let frameError = "";
const palettes = {
  inferno: [[0,0,4],[31,12,72],[85,15,109],[136,34,106],[186,54,85],[227,89,51],[249,140,10],[249,201,50],[252,255,164]],
  iron: [[0,0,0],[35,0,70],[80,0,114],[142,16,103],[198,44,61],[239,99,25],[255,169,36],[255,227,117],[255,255,255]],
  gray: [[0,0,0],[255,255,255]],
  "gray-inverse": [[255,255,255],[0,0,0]],
};

function unit() { return $("units").value; }
function unitLabel() { return unit() === "K" ? "K" : `°${unit()}`; }
function converted(c) { return unit() === "F" ? c * 9 / 5 + 32 : unit() === "K" ? c + 273.15 : c; }
function temp(c, suffix = true) { return Number.isFinite(c) ? `${converted(c).toFixed(1)}${suffix ? ` ${unitLabel()}` : ""}` : "—"; }
function delta(c) { return Number.isFinite(c) ? `${(unit() === "F" ? c * 9 / 5 : c).toFixed(2)} ${unitLabel()}` : "—"; }
function timestampDate(value) { return new Date(typeof value === "number" ? value * 1000 : value); }
function frameDate() { return frame ? timestampDate(frame.timestamp) : null; }
function errorDetail(data, fallback) { return typeof data.detail === "string" ? data.detail : typeof data.error === "string" ? data.error : fallback; }

function renderingMetadata() {
  const width = frame?.width || 160, height = frame?.height || 120;
  const smooth = $("renderMode").value === "smooth";
  return {
    render_mode: smooth ? "smooth" : "native",
    interpolation: smooth ? "browser high-quality image interpolation" : "nearest neighbor",
    native_resolution: {width, height},
    display_resolution: {width: height * (smooth ? 2 : 1), height: width * (smooth ? 2 : 1)},
    canvas_resolution: {width: canvas.width, height: canvas.height},
    rotation_degrees: -90,
    aspect_ratio: "3:4",
  };
}

function resizeThermalCanvas() {
  const bounds = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  // Multiples of 3 and 4 preserve the rotated sensor ratio at every screen DPI.
  // The minimum keeps PNGs readable; the cap bounds large-screen memory use.
  const ratioScale = Math.max(240, Math.min(960,
    Math.ceil(Math.max(bounds.width / 3, bounds.height / 4) * dpr)));
  const width = ratioScale * 3, height = ratioScale * 4;
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
}

function renderingUI() {
  const details = renderingMetadata();
  const {width, height} = details.display_resolution;
  $("resolution").textContent = `${width} × ${height} ${details.render_mode} · 90° CCW`;
  $("renderDetail").textContent = details.render_mode === "smooth"
    ? "240 × 320 interpolated view, rotated 90° counterclockwise. Measurements and coordinates use the original 160 × 120 sensor grid."
    : "120 × 160 view, rotated 90° counterclockwise without smoothing. Measurements and coordinates use the original 160 × 120 sensor grid.";
  const fullscreen = document.fullscreenElement === viewerPanel || viewerPanel.classList.contains("expanded");
  $("fullscreen").textContent = fullscreen ? "Exit full screen" : "Full screen";
  $("fullscreen").setAttribute("aria-pressed", String(fullscreen));
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers);
  const token = $("accessToken").value;
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, {cache: "no-store", signal: AbortSignal.timeout(12000), ...options, headers});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorDetail(data, `${response.status}: ${response.statusText}`));
  return data;
}

function connectionUI() {
  const streaming = status.state === "streaming" && !transportError;
  const connecting = status.state === "connecting";
  $("connection").className = `status-badge ${transportError ? "error" : status.state}`;
  const labels = {streaming: "Camera streaming", connecting: "Connecting…", disconnected: "Disconnected", error: "Camera unavailable"};
  $("connectionText").textContent = transportError ? "Server unavailable" : status.source === "demo" && streaming ? "Demo streaming" : (labels[status.state] || status.state);
  $("connect").textContent = status.state === "error" || transportError ? "Reconnect" : "Connect camera";
  $("connect").disabled = actionPending || connecting || streaming;
  $("disconnect").disabled = actionPending || (!streaming && !connecting);
  $("shutter").disabled = actionPending || !streaming || status.source === "demo";
  $("gain").disabled = actionPending || !streaming || status.source === "demo";
  $("freeze").disabled = !frame;
  $("demoNotice").hidden = status.source !== "demo";
  const message = actionError || transportError || status.error || frameError || "";
  $("error").textContent = message;
  $("error").hidden = !message;
  $("fps").textContent = `${Number(status.fps || 0).toFixed(1)} fps`;
  $("frameCount").textContent = `${Number(status.frames || 0).toLocaleString()} frames`;
  const device = status.device || {};
  const firmware = device.fw_version || device.firmware || device.version;
  $("deviceInfo").textContent = `${status.source === "demo" ? "Synthetic P1" : (device.model || device.name || "P1")} · 160 × 120 native sensor${firmware ? ` · FW ${firmware}` : ""}`;
  if (device.gain && !actionPending) { $("gain").value = device.gain; lastGain = device.gain; gainKnown = true; }
  if (status.gain && !actionPending) { $("gain").value = status.gain; lastGain = status.gain; gainKnown = true; }
  if (status.camera_stats?.gain && !actionPending) { $("gain").value = status.camera_stats.gain; lastGain = status.camera_stats.gain; gainKnown = true; }
  if (!gainKnown) $("gain").title = "High is the driver startup default. Changing this sends a command to the camera.";
  freshnessUI();
}

function freshnessUI() {
  const stale = !!frame && (status.state !== "streaming" || !!transportError || !!frameError || (!frozen && performance.now() - receivedAt > 2500) || Number(status.frame_age_s || 0) > 2.5);
  $("staleOverlay").hidden = !stale;
  $("staleOverlay").textContent = status.state !== "streaming" || transportError ? "Last frame · camera unavailable" : "Last frame · waiting for fresh data";
  $("freezeOverlay").hidden = !frozen || !frame;
  $("frozenSequence").textContent = frame ? `#${frame.sequence}` : "";
  $("freeze").textContent = frozen ? "Resume view" : "Freeze view";
  $("freeze").setAttribute("aria-pressed", String(frozen));
  $("viewState").textContent = !frame ? "WAITING" : stale ? "STALE" : frozen ? "FROZEN" : frame.source === "demo" ? "DEMO" : "LIVE";
  $("lastUpdate").textContent = !frame ? "Awaiting camera" : `${frozen ? "Frozen" : stale ? "Last frame" : "Updated"} ${frameDate().toLocaleTimeString()}`;
}

async function poll() {
  try {
    status = await request("/api/status");
    transportError = "";
    connectionUI();
    if (status.state === "streaming" && !frozen) {
      try {
        const next = await request("/api/frame");
        if (frozen) return;
        frameError = "";
        if (!frame || next.sequence !== frame.sequence || next.timestamp !== frame.timestamp) {
          frame = next;
          receivedAt = performance.now();
          const now = Date.now();
          history.push({time: now, min: frame.stats.min.celsius, max: frame.stats.max.celsius, center: frame.stats.center.celsius});
          history = history.filter((sample) => sample.time >= now - 60000);
          $("emptyState").hidden = true;
          $("historyEmpty").hidden = true;
          ["exportCsv", "exportJson", "exportPng"].forEach((id) => $(id).disabled = false);
          render();
        }
      } catch (error) {
        frameError = `Waiting for a valid thermal frame: ${error.message}`;
      }
    }
  } catch (error) {
    transportError = `Cannot reach the controller. Make sure the Python server is running. ${error.message}`;
  } finally {
    connectionUI();
    setTimeout(poll, 120);
  }
}

async function command(path, payload) {
  if (actionPending) return false;
  actionPending = true;
  actionError = "";
  connectionUI();
  try {
    await request(path, {method: "POST", signal: AbortSignal.timeout(20000), headers: {"Content-Type": "application/json"}, ...(payload ? {body: JSON.stringify(payload)} : {})});
    status = await request("/api/status");
    transportError = "";
    return true;
  } catch (error) {
    actionError = error.message;
    return false;
  } finally {
    actionPending = false;
    connectionUI();
  }
}

function paletteColor(value) {
  const stops = palettes[$("palette").value];
  const pos = Math.min(1, Math.max(0, value)) * (stops.length - 1);
  const lower = Math.min(stops.length - 2, Math.floor(pos));
  const mix = pos - lower;
  return stops[lower].map((v, i) => Math.round(v + (stops[lower + 1][i] - v) * mix));
}

function scaleRange() {
  if ($("scale").value === "fixed") {
    const lowText = $("rangeLow").value, highText = $("rangeHigh").value;
    const low = Number(lowText), high = Number(highText);
    const valid = lowText !== "" && highText !== "" && Number.isFinite(low) && Number.isFinite(high) && high > low;
    $("rangeError").hidden = valid;
    if (valid) return [low, high];
  } else $("rangeError").hidden = true;
  return frame ? [frame.stats.min.celsius, frame.stats.max.celsius] : [0, 1];
}

function marker(point, color, label, cross = false) {
  // Draw in CSS pixels so marker strokes and labels stay legible on high-DPI screens.
  const cssWidth = canvas.getBoundingClientRect().width || canvas.width;
  const scale = canvas.width / cssWidth;
  const cssHeight = canvas.height / scale;
  const x = (point.y + 0.5) * cssWidth / frame.height;
  const y = (frame.width - point.x - 0.5) * cssHeight / frame.width;
  ctx.save();
  ctx.scale(scale, scale);
  ctx.strokeStyle = "#070d12"; ctx.lineWidth = 5;
  function shape() {
    ctx.beginPath();
    if (cross) { ctx.moveTo(x - 12,y); ctx.lineTo(x - 4,y); ctx.moveTo(x + 4,y); ctx.lineTo(x + 12,y); ctx.moveTo(x,y - 12); ctx.lineTo(x,y - 4); ctx.moveTo(x,y + 4); ctx.lineTo(x,y + 12); }
    else { ctx.arc(x,y,7,0,Math.PI * 2); ctx.moveTo(x - 12,y); ctx.lineTo(x - 8,y); ctx.moveTo(x + 8,y); ctx.lineTo(x + 12,y); ctx.moveTo(x,y - 12); ctx.lineTo(x,y - 8); ctx.moveTo(x,y + 8); ctx.lineTo(x,y + 12); }
    ctx.stroke();
  }
  shape(); ctx.strokeStyle = color; ctx.lineWidth = 2; shape();
  if (label) {
    ctx.font = "bold 14px Consolas, monospace";
    const width = ctx.measureText(label).width;
    const tx = Math.max(5, Math.min(cssWidth - width - 10, x + 14));
    const ty = Math.max(21, Math.min(cssHeight - 5, y - 13));
    ctx.fillStyle = "#0a111dc9"; ctx.fillRect(tx - 3,ty - 15,width + 7,21);
    ctx.fillStyle = color; ctx.fillText(label,tx,ty);
  }
  ctx.restore();
}

function renderThermal() {
  resizeThermalCanvas();
  renderingUI();
  const stops = palettes[$("palette").value];
  $("colorbar").style.background = `linear-gradient(90deg, ${stops.map((rgb) => `rgb(${rgb.join(",")})`).join(",")})`;
  const [low, high] = scaleRange();
  if (!frame) return;
  const spread = high - low;
  sourceCanvas.width = frame.width; sourceCanvas.height = frame.height;
  const image = sourceCtx.createImageData(frame.width, frame.height);
  const lut = Array.from({length: 256}, (_, i) => paletteColor(i / 255));
  for (let y = 0; y < frame.height; y++) for (let x = 0; x < frame.width; x++) {
    const t = frame.temperatures_c[y][x];
    const color = lut[Math.round(Math.max(0, Math.min(1, spread > 0 ? (t - low) / spread : 0.5)) * 255)];
    const i = (y * frame.width + x) * 4;
    image.data[i] = color[0]; image.data[i + 1] = color[1]; image.data[i + 2] = color[2]; image.data[i + 3] = 255;
  }
  sourceCtx.putImageData(image, 0, 0);
  const details = renderingMetadata();
  const smooth = details.render_mode === "smooth";
  // This is ordinary display interpolation, not the manufacturer's proprietary X³IR.
  // The frame's native temperature/raw arrays are never modified by rendering.
  displayCanvas.width = details.display_resolution.width;
  displayCanvas.height = details.display_resolution.height;
  displayCtx.imageSmoothingEnabled = smooth;
  displayCtx.imageSmoothingQuality = "high";
  displayCtx.save();
  displayCtx.translate(0, displayCanvas.height);
  displayCtx.rotate(-Math.PI / 2);
  displayCtx.drawImage(sourceCanvas, 0, 0, displayCanvas.height, displayCanvas.width);
  displayCtx.restore();
  ctx.imageSmoothingEnabled = smooth;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(displayCanvas, 0, 0, canvas.width, canvas.height);
  if ($("markers").checked) {
    marker(frame.stats.min, "#96c5ff", "MIN");
    marker(frame.stats.max, "#ffc591", "MAX");
    marker(frame.stats.center, "#dcfff0", "", true);
  }
  if (probe) marker(probe, "#ffffff", pinned ? "PIN" : "", true);
  $("scaleMin").textContent = temp(low);
  $("scaleMax").textContent = temp(high);
}

function renderStats() {
  if (!frame) return;
  const s = frame.stats;
  ["min", "max", "center"].forEach((key) => { $(`${key}Temp`).textContent = temp(s[key].celsius, false); $(`${key}Location`).textContent = `x ${s[key].x} / y ${s[key].y}`; });
  $("meanTemp").textContent = temp(s.mean_celsius, false);
  $("rangeTemp").textContent = delta(s.range_celsius);
  $("stdTemp").textContent = delta(s.stddev_celsius);
  document.querySelectorAll(".unit-label").forEach((el) => el.textContent = unitLabel());
  $("frameLabel").textContent = `#${frame.sequence} · ${frameDate().toLocaleTimeString()}`;
  renderProbe();
}

function renderProbe() {
  if (!frame || !probe) { $("probeTemp").textContent = "—"; $("probeCoords").textContent = "Move over image"; return; }
  $("probeTemp").textContent = temp(frame.temperatures_c[probe.y][probe.x]);
  $("probeCoords").textContent = `x ${probe.x} / y ${probe.y}${pinned ? " · pinned" : ""}`;
  $("clearProbe").hidden = !pinned;
}

function renderHistory() {
  const chart = $("history"), c = chart.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const bounds = chart.getBoundingClientRect();
  chart.width = Math.round(bounds.width * dpr); chart.height = Math.round(bounds.height * dpr);
  c.scale(dpr, dpr);
  const w = bounds.width, h = bounds.height, left = 49, right = w - 9, top = 14, bottom = h - 23;
  c.clearRect(0,0,w,h);
  let max = history.length ? Math.max(...history.map(p => converted(p.max))) : 1;
  let min = history.length ? Math.min(...history.map(p => converted(p.min))) : 0;
  const margin = Math.max((max - min) * 0.1, 0.5); min -= margin; max += margin;
  c.font = "10px Consolas, monospace"; c.textAlign = "right";
  for (let i = 0; i < 4; i++) {
    const y = top + (bottom - top) * i / 3;
    c.strokeStyle = "#26323e"; c.lineWidth = 1; c.beginPath(); c.moveTo(left,y); c.lineTo(right,y); c.stroke();
    c.fillStyle = "#748a9c"; c.fillText(history.length ? (max - (max-min) * i/3).toFixed(1) : "—", left-8,y+3);
  }
  c.textAlign = "left"; c.fillStyle = "#748a9c"; c.fillText("−60s",left,h-5); c.textAlign = "right"; c.fillText(frozen ? "frozen" : "last sample",right,h-5);
  if (!history.length) return;
  const end = history[history.length - 1].time;
  for (const [key, color] of [["min","#80b6ff"],["max","#ffac74"],["center","#b8ead8"]]) {
    c.beginPath(); c.lineWidth = 1.5; c.strokeStyle = color;
    history.forEach((p,i) => { const x = left + (p.time - (end - 60000)) / 60000 * (right - left); const y = bottom - (converted(p[key]) - min) / (max - min) * (bottom - top); if (!i || p.time - history[i-1].time > 2000) c.moveTo(x,y); else c.lineTo(x,y); });
    c.stroke();
  }
}

function render() { renderThermal(); renderStats(); renderHistory(); freshnessUI(); }
function pointerPosition(event) {
  const r = canvas.getBoundingClientRect();
  const displayX = Math.min(frame.height - 1, Math.max(0, Math.floor((event.clientX - r.left) / r.width * frame.height)));
  const displayY = Math.min(frame.width - 1, Math.max(0, Math.floor((event.clientY - r.top) / r.height * frame.width)));
  // Invert the display rotation to inspect the original temperature sample.
  return {x: frame.width - 1 - displayY, y: displayX};
}
canvas.addEventListener("pointermove", (event) => { if (!frame || pinned) return; probe = pointerPosition(event); renderThermal(); renderProbe(); });
canvas.addEventListener("pointerleave", () => { if (pinned) return; probe = null; renderThermal(); renderProbe(); });
canvas.addEventListener("click", (event) => { if (!frame) return; probe = pointerPosition(event); pinned = true; renderThermal(); renderProbe(); });
canvas.addEventListener("keydown", (event) => {
  if (!frame || !["ArrowUp","ArrowDown","ArrowLeft","ArrowRight","Escape"].includes(event.key)) return;
  event.preventDefault();
  if (event.key === "Escape") { clearProbe(); return; }
  probe ||= {x: Math.floor(frame.width/2), y: Math.floor(frame.height/2)};
  pinned = true;
  if (event.key === "ArrowUp") probe.x = Math.min(frame.width-1,probe.x+1);
  if (event.key === "ArrowDown") probe.x = Math.max(0,probe.x-1);
  if (event.key === "ArrowLeft") probe.y = Math.max(0,probe.y-1);
  if (event.key === "ArrowRight") probe.y = Math.min(frame.height-1,probe.y+1);
  renderThermal(); renderProbe();
});
function clearProbe() { pinned = false; probe = null; $("clearProbe").hidden = true; renderThermal(); renderProbe(); }
$("clearProbe").addEventListener("click",clearProbe);
$("connect").addEventListener("click", async () => { frozen = false; frameError = ""; if (await command("/api/connect")) { history = []; renderHistory(); } });
$("disconnect").addEventListener("click", () => command("/api/disconnect"));
$("shutter").addEventListener("click", () => command("/api/shutter"));
$("gain").addEventListener("change", async () => { const chosen = $("gain").value; if (await command("/api/gain",{gain: chosen})) { lastGain = chosen; gainKnown = true; $("gain").value = chosen; } else $("gain").value = lastGain; });
$("freeze").addEventListener("click", () => { frozen = !frozen; render(); });
["units","palette","markers","rangeLow","rangeHigh","renderMode"].forEach((id) => $(id).addEventListener("input",render));
$("scale").addEventListener("change", () => { $("fixedRange").hidden = $("scale").value !== "fixed"; render(); });
let resizeRequest = null;
function queueResizeRender() {
  if (resizeRequest !== null) return;
  resizeRequest = requestAnimationFrame(() => { resizeRequest = null; render(); });
}
window.addEventListener("resize", queueResizeRender);
new ResizeObserver(queueResizeRender).observe($("viewport"));
document.addEventListener("fullscreenchange", queueResizeRender);
function exitExpandedView() {
  viewerPanel.classList.remove("expanded");
  document.body.classList.remove("viewer-expanded");
  queueResizeRender();
}
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && viewerPanel.classList.contains("expanded")) exitExpandedView();
});
$("fullscreen").addEventListener("click", async () => {
  actionError = "";
  if (viewerPanel.classList.contains("expanded")) {
    exitExpandedView();
    connectionUI();
    return;
  }
  try {
    if (document.fullscreenElement === viewerPanel) {
      await document.exitFullscreen();
    } else {
      if (!viewerPanel.requestFullscreen) throw new Error("This browser does not support fullscreen viewing.");
      await viewerPanel.requestFullscreen();
    }
  } catch (_error) {
    // Background tabs and embedded browsers may deny the native fullscreen API.
    // Keep the same usable expanded viewer inside the page instead.
    viewerPanel.classList.add("expanded");
    document.body.classList.add("viewer-expanded");
  }
  connectionUI();
  queueResizeRender();
});

function exportName(extension) { return `p1-${frame.source || "camera"}-${frameDate().toISOString().replace(/[:.]/g,"-")}-frame-${frame.sequence}.${extension}`; }
function download(blob, name) { const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url),1000); }
$("exportCsv").addEventListener("click", () => { if (frame) download(new Blob([frame.temperatures_c.map(row => row.join(",")).join("\r\n") + "\r\n"], {type:"text/csv;charset=utf-8"}), exportName("csv")); });
$("exportJson").addEventListener("click", () => { if (frame) download(new Blob([JSON.stringify({...frame, export_metadata: {temperature_unit:"celsius", coordinates:"zero-based native sensor [y][x]; origin top-left before display rotation", raw_unit:"1/64 kelvin", view_frozen:frozen, palette:$("palette").value, display_unit:unit(), display_range_c:scaleRange(), ...renderingMetadata()}},null,2)], {type:"application/json"}), exportName("json")); });
$("exportPng").addEventListener("click", () => {
  if (!frame) return;
  renderThermal();
  const snapshot = frame, name = exportName("png"), details = renderingMetadata(), out = document.createElement("canvas");
  const footerScale = canvas.width / 960;
  out.width = canvas.width; out.height = canvas.height + Math.round(102 * footerScale);
  const c = out.getContext("2d"); c.fillStyle = "#10151b"; c.fillRect(0,0,out.width,out.height); c.drawImage(canvas,0,0);
  c.translate(0, canvas.height); c.scale(footerScale, footerScale);
  c.font = "17px Consolas, monospace"; c.fillStyle = "#e8edf4";
  c.fillText(`P1 ${snapshot.source || "camera"} | ${frameDate().toISOString()} | frame #${snapshot.sequence}`,18,28,924);
  c.font = "15px Consolas, monospace"; c.fillStyle = "#a7bacb";
  c.fillText(`Min ${temp(snapshot.stats.min.celsius)} | Max ${temp(snapshot.stats.max.celsius)} | Center ${temp(snapshot.stats.center.celsius)} | ${$("palette").value}`,18,54,924);
  c.font = "14px Consolas, monospace";
  c.fillText(`${snapshot.width} × ${snapshot.height} native | ${details.display_resolution.width} × ${details.display_resolution.height} ${details.render_mode}${details.render_mode === "smooth" ? " (interpolated)" : ""} | 90° CCW | image ${canvas.width} × ${canvas.height}`,18,80,924);
  out.toBlob((blob) => { if (blob) download(blob,name); },"image/png");
});

renderHistory();
renderThermal();
poll();
setInterval(freshnessUI, 500);
