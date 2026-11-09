/* Lab Console Cameras workspace and the shared camera tile (window.LabCameras).
 * camera1 to camera4 and the microscope play H.264 from the proxy at /camera/<view>.mp4.
 * camera1 to camera4 fall back to MJPEG at /camera/<view>.mjpeg and take snapshots at
 * /camera/<view>.full.jpg. The console relays the microscope MJPEG fallback and full-size PNG snapshot, and
 * the thermal frame. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else {
    root.LabCameras = api;
    api.install();
  }
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";

  const VIEWS = ["camera1", "camera2", "camera3", "camera4", "thermal", "microscope"];
  const MJPEG = new Set(["camera1", "camera2", "camera3", "camera4", "microscope"]);
  const STATUS_MS = 1000;
  const THERMAL_MS = 40;
  const DETECT_MS = 1000;
  const STALE_S = 2;
  const W = 160, H = 120;
  const BLANK = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
  const PREFIX = "/api/console/ws/cameras/";
  // The relay sends only the newest frame. Small tiles ask for fewer frames.
  const TILE_FPS = 10, LARGE_FPS = 30;
  // A 1280x720 USB view at the camera rate needs about 35 Mbit/s. The VPN link carries about 22 Mbit/s,
  // so the USB previews ask for less and leave room for keys, holds and stops.
  const USB_TILE_FPS = 3, USB_LARGE_FPS = 6;
  // The host caps the bitrate of each H.264 preview, so the views play it at the camera rate.
  const H264 = 'video/mp4; codecs="avc1.640029"';
  // Play near the newest frame: jump when more than JUMP_S behind, speed up above CATCH_UP_S.
  const JUMP_S = 1, CATCH_UP_S = 0.3, KEEP_S = 10, MAX_QUEUE = 4 * 1024 * 1024;
  // Tiles show the measured frame rate: a new value about once a second, none when it is old.
  const RATE_MS = 1000, FRESH_MS = 2500;

  const i18n = {
    en: {
      "title.camera1": "camera1", "title.camera2": "camera2", "title.camera3": "camera3", "title.camera4": "camera4",
      "title.thermal": "Thermal", "title.microscope": "Microscope",
      "role.camera1": "Stage", "role.camera2": "Insert rail", "role.camera3": "Microscope side", "role.camera4": "Tray",
      "role.thermal": "P1", "role.microscope": "Toupcam",
      "badge.live": "LIVE", "badge.stale": "STALE", "badge.offline": "OFFLINE", "badge.frozen": "FROZEN",
      "age": "{age} s",
      "tile.snapshot": "Snapshot", "tile.expand": "Expand", "tile.select": "Select {name}",
      "tile.noFrame": "No frame", "tile.notConfigured": "Not configured",
      "layout.label": "Layout", "layout.wall": "All views", "layout.large": "Large view",
      "overlay.label": "Overlays", "overlay.detections": "Chip detections", "overlay.thermal": "Thermal markers",
      "snapshot.all": "Snapshot all",
      "side.detectionsCount": "{count} found",
      "settings": "Settings", "settings.title": "Thermal settings", "settings.close": "Close",
      "reading.max": "Max", "reading.mean": "Mean",
      "units": "Units", "palette": "Palette", "palette.ironbow": "Ironbow", "palette.rainbow": "Rainbow", "palette.grey": "Grey",
      "scale": "Colour scale", "scale.auto": "Auto", "scale.fixed": "Fixed", "scale.low": "Low", "scale.high": "High",
      "region": "Crucible region", "region.none": "Not drawn yet",
      "region.bounds": "x {x0}–{x1}, y {y0}–{y1}", "region.draw": "Draw region", "region.drawing": "Drag on the thermal view.",
      "region.save": "Save region", "region.cancel": "Cancel",
      "region.manual": "The thermal service does not offer Save. Put these bounds under [regions.crucible] in rois.toml.",
      "region.confirm": "Save x {x0}–{x1}, y {y0}–{y1} as the crucible region? The furnace screen and runs read it.",
      "connect": "Connect camera", "camera.label": "Camera", "shutter": "Calibrate shutter…", "shutter.label": "Shutter", "shutter.confirm": "Close the shutter and recalibrate the thermal camera now? The view pauses for a moment.",
      "confirm": "Confirm", "cancel": "Cancel",
      "command.missing": "The thermal service does not offer shutter calibration.",
      "command.control": "Take thermal control first.", "command.take": "Take thermal control", "command.release": "Release thermal control",
      "command.accepted": "Sent: {action}", "command.refused": "Refused: {error}", "command.unknown": "Outcome unknown: {action}. Check the camera.",
      "freeze": "Freeze view", "unfreeze": "Resume view",
      "export": "Export frame", "export.csv": "CSV", "export.raw": "Raw + JSON", "export.png": "PNG",
      "export.none": "No thermal frame to export.",
      "snapshot.failed": "Snapshot failed: {error}",
      "colorbar": "Colour bar",
      "probe": "Pixel {x}, {y}: {value}", "probe.pinned": "Pinned pixel {x}, {y}: {value}",
    },
    zh: {
      "title.camera1": "camera1", "title.camera2": "camera2", "title.camera3": "camera3", "title.camera4": "camera4",
      "title.thermal": "热像仪", "title.microscope": "显微镜",
      "role.camera1": "位移台", "role.camera2": "插入导轨", "role.camera3": "显微镜侧视", "role.camera4": "托盘",
      "role.thermal": "P1", "role.microscope": "Toupcam",
      "badge.live": "实时", "badge.stale": "过期", "badge.offline": "离线", "badge.frozen": "已冻结",
      "age": "{age} 秒",
      "tile.snapshot": "快照", "tile.expand": "放大", "tile.select": "选择 {name}",
      "tile.noFrame": "无画面", "tile.notConfigured": "未配置",
      "layout.label": "布局", "layout.wall": "全部画面", "layout.large": "大画面",
      "overlay.label": "叠加", "overlay.detections": "芯片识别", "overlay.thermal": "温度标记",
      "snapshot.all": "全部快照",
      "side.detectionsCount": "找到 {count} 个",
      "settings": "设置", "settings.title": "热像设置", "settings.close": "关闭",
      "reading.max": "最高", "reading.mean": "平均",
      "units": "单位", "palette": "色板", "palette.ironbow": "铁红", "palette.rainbow": "彩虹", "palette.grey": "灰度",
      "scale": "色标范围", "scale.auto": "自动", "scale.fixed": "固定", "scale.low": "下限", "scale.high": "上限",
      "region": "坩埚区域", "region.none": "尚未画出",
      "region.bounds": "x {x0}–{x1}，y {y0}–{y1}", "region.draw": "画区域", "region.drawing": "在热像画面上拖动。",
      "region.save": "保存区域", "region.cancel": "取消",
      "region.manual": "热像仪服务未提供保存。把这些边界写入 rois.toml 的 [regions.crucible]。",
      "region.confirm": "把 x {x0}–{x1}，y {y0}–{y1} 保存为 crucible 区域？炉子页面和运行会读取它。",
      "connect": "连接热像仪", "camera.label": "热像仪", "shutter": "快门校准…", "shutter.label": "快门", "shutter.confirm": "现在关闭快门并重新校准热像仪？画面会短暂暂停。",
      "confirm": "确认", "cancel": "取消",
      "command.missing": "热像仪服务未提供快门校准。",
      "command.control": "请先获取热像仪控制。", "command.take": "获取热像仪控制", "command.release": "释放热像仪控制",
      "command.accepted": "已发送：{action}", "command.refused": "被拒绝：{error}", "command.unknown": "结果未知：{action}。请检查相机。",
      "freeze": "冻结画面", "unfreeze": "恢复画面",
      "export": "导出此帧", "export.csv": "CSV", "export.raw": "原始 + JSON", "export.png": "PNG",
      "export.none": "没有可导出的热像帧。",
      "snapshot.failed": "快照失败：{error}",
      "colorbar": "色标",
      "probe": "像素 {x}, {y}：{value}", "probe.pinned": "固定像素 {x}, {y}：{value}",
    },
  };

  // Pure logic (tested in Node) ---------------------------------------------

  function translate(lang, key, vars) {
    const table = i18n[lang] || i18n.en;
    let text = key in table ? table[key] : key in i18n.en ? i18n.en[key] : key;
    for (const [name, value] of Object.entries(vars || {})) text = text.split("{" + name + "}").join(String(value));
    return text;
  }

  /** The badge for one view: tone, text key and the frame age in seconds or null. */
  function badge(row, options = {}) {
    if (options.frozen) return { tone: "warn", key: "badge.frozen", age: null };
    if (!row || options.fresh === false) return { tone: "off", key: "badge.offline", age: null };
    const age = typeof row.age_s === "number" && Number.isFinite(row.age_s) ? row.age_s : null;
    if (row.state === "live" && age !== null && age <= STALE_S) return { tone: "ok", key: "badge.live", age };
    if ((row.state === "live" || row.state === "stale") && age !== null) return { tone: "warn", key: "badge.stale", age };
    return { tone: "off", key: "badge.offline", age };
  }

  /** The thermal row from a frame fetched at fetchedAt (ms), with an error if the last fetch failed. */
  function thermalRow(frame, fetchedAt, nowMs, failed) {
    if (failed || !frame) return { state: "offline", age_s: null };
    const age = (typeof frame.age_s === "number" ? frame.age_s : 0) + Math.max(0, nowMs - fetchedAt) / 1000;
    return { state: age <= STALE_S ? "live" : "stale", age_s: Math.round(age * 10) / 10 };
  }

  const STOPS = {
    ironbow: ["#1B0638", "#6A1C7A", "#C23B4B", "#F08A24", "#FBE38E"],
    rainbow: ["#2B1C9E", "#1E7BE0", "#2EC4A0", "#B8E04A", "#F5A623", "#E0332E"],
    grey: ["#000000", "#FFFFFF"],
  };

  function hex(color) { return [1, 3, 5].map(i => parseInt(color.slice(i, i + 2), 16)); }

  /** A 256-entry RGB table for a palette. */
  function paletteTable(name) {
    const stops = (STOPS[name] || STOPS.ironbow).map(hex);
    const table = new Uint8ClampedArray(256 * 3);
    for (let i = 0; i < 256; i++) {
      const position = (i / 255) * (stops.length - 1);
      const k = Math.min(stops.length - 2, Math.floor(position));
      const f = position - k;
      for (let c = 0; c < 3; c++) table[i * 3 + c] = Math.round(stops[k][c] + (stops[k + 1][c] - stops[k][c]) * f);
    }
    return table;
  }

  function convert(celsius, unit) {
    if (unit === "F") return celsius * 9 / 5 + 32;
    if (unit === "K") return celsius + 273.15;
    return celsius;
  }

  function toCelsius(value, unit) {
    if (unit === "F") return (value - 32) * 5 / 9;
    if (unit === "K") return value - 273.15;
    return value;
  }

  function unitLabel(unit) { return unit === "F" ? "°F" : unit === "K" ? "K" : "°C"; }

  function formatTemp(celsius, unit) {
    if (typeof celsius !== "number" || !Number.isFinite(celsius)) return "—";
    return convert(celsius, unit).toFixed(1) + " " + unitLabel(unit);
  }

  /** The colour scale in °C: the frame range, or the fixed low and high. */
  function scaleRange(stats, settings) {
    if (settings.scale === "fixed" && Number.isFinite(settings.low) && Number.isFinite(settings.high) && settings.high > settings.low) {
      return [settings.low, settings.high];
    }
    const low = stats?.min?.celsius, high = stats?.max?.celsius;
    if (Number.isFinite(low) && Number.isFinite(high)) return high > low ? [low, high] : [low - 0.5, high + 0.5];
    return [0, 1];
  }

  /** RGBA pixels for a 160×120 temperature matrix. */
  function thermalPixels(temperatures, table, low, high) {
    const out = new Uint8ClampedArray(W * H * 4);
    const span = high - low || 1;
    for (let y = 0; y < H; y++) {
      const row = temperatures[y] || [];
      for (let x = 0; x < W; x++) {
        const t = Math.max(0, Math.min(1, (row[x] - low) / span));
        const index = Number.isFinite(t) ? Math.round(t * 255) : 0;
        const o = (y * W + x) * 4;
        out[o] = table[index * 3]; out[o + 1] = table[index * 3 + 1]; out[o + 2] = table[index * 3 + 2]; out[o + 3] = 255;
      }
    }
    return out;
  }

  /** Celsius rows from the compact frame: base64 little-endian uint16 in 1/64 kelvin. */
  function thermalTemperatures(encoded) {
    const bytes = Uint8Array.from(root.atob(encoded), char => char.charCodeAt(0));
    if (bytes.length !== W * H * 2) throw new Error("The thermal frame has an unexpected size.");
    const view = new DataView(bytes.buffer);
    return Array.from({ length: H }, (_, y) => Array.from({ length: W }, (_, x) => view.getUint16((y * W + x) * 2, true) / 64 - 273.15));
  }

  function thermalCsv(frame, unit) {
    const lines = [`# sequence=${frame.sequence}, timestamp=${frame.timestamp}, unit=${unitLabel(unit)}`];
    for (const row of frame.temperatures_c) lines.push(row.map(value => convert(value, unit).toFixed(2)).join(","));
    return lines.join("\n") + "\n";
  }

  /** The temperature of 1 sensor pixel {x, y}, or null outside the frame. */
  function probeReading(frame, probe) {
    if (!probe || !Number.isInteger(probe.x) || !Number.isInteger(probe.y)) return null;
    if (probe.x < 0 || probe.x >= W || probe.y < 0 || probe.y >= H) return null;
    const value = frame?.temperatures_c?.[probe.y]?.[probe.x];
    return typeof value === "number" && Number.isFinite(value) ? { x: probe.x, y: probe.y, celsius: value } : null;
  }

  /** Normalize a dragged rectangle in sensor pixels to region bounds, or null if it is too small. */
  function regionFrom(a, b) {
    const clamp = (v, max) => Math.max(0, Math.min(max, Math.round(v)));
    const x0 = clamp(Math.min(a.x, b.x), W), x1 = clamp(Math.max(a.x, b.x), W);
    const y0 = clamp(Math.min(a.y, b.y), H), y1 = clamp(Math.max(a.y, b.y), H);
    if (x1 - x0 < 2 || y1 - y0 < 2) return null;
    return { x0, y0, x1, y1 };
  }

  /** Where an image of size (w, h) is drawn inside a box with object-fit: contain. */
  function containRect(w, h, boxW, boxH) {
    if (!w || !h || !boxW || !boxH) return { x: 0, y: 0, scale: 0 };
    const scale = Math.min(boxW / w, boxH / h);
    return { x: (boxW - w * scale) / 2, y: (boxH - h * scale) / 2, scale };
  }

  const CAMERAS = new Set(["camera1", "camera2", "camera3", "camera4"]);

  /** The stream URL: a large microscope tile gets the source rate, others TILE_FPS. USB views get USB_LARGE_FPS or USB_TILE_FPS. */
  function streamUrl(view, large, basePath = "") {
    if (CAMERAS.has(view)) return `/camera/${view}.mjpeg?fps=${large ? USB_LARGE_FPS : USB_TILE_FPS}`;
    return `${basePath}${PREFIX}stream/${view}?fps=${large ? LARGE_FPS : TILE_FPS}`;
  }

  /** The H.264 preview URL of camera1 to camera4 and the microscope, or null for the thermal view. */
  function videoUrl(view) { return CAMERAS.has(view) || view === "microscope" ? `/camera/${view}.mp4` : null; }

  /** The live-edge correction for a play position in the last buffered range [start, end]. */
  function chase(time, start, end) {
    if (time < start || end - time > JUMP_S) return { seek: Math.max(start, end - 0.1), rate: 1 };
    return { seek: null, rate: end - time > CATCH_UP_S ? 1.1 : 1 };
  }

  /** Frames per second between 2 samples {frames, at} (at in ms), or null. */
  function frameRate(first, last) {
    if (!first || !last || !(last.at > first.at) || last.frames < first.frames) return null;
    return (last.frames - first.frames) * 1000 / (last.at - first.at);
  }

  /** A frame counter that turns its count into a rate about once a second. */
  function meter() { return { first: null, fps: null, at: -Infinity }; }

  function measure(rate, frames, at) {
    if (!rate.first || frames < rate.first.frames) { rate.first = { frames, at }; return; }
    if (at - rate.first.at < RATE_MS) return;
    rate.fps = frameRate(rate.first, { frames, at });
    rate.at = at;
    rate.first = { frames, at };
  }

  /** The measured rate, or null when it is old. */
  function current(rate, now) { return rate && now - rate.at <= FRESH_MS ? rate.fps : null; }

  /** The tile mode with the measured rate, for example "640×480 · 29.8 fps". Unknown parts drop out. */
  function modeLine(mode, fps, ...extra) {
    const rate = typeof fps === "number" && Number.isFinite(fps) ? `${fps.toFixed(1)} fps` : "";
    return [mode, rate, ...extra].filter(Boolean).join(" · ");
  }

  /** The snapshot URL: a full-resolution JPEG for camera1 to camera4, a full-size PNG for the microscope. */
  function snapshotUrl(view, basePath = "") {
    return CAMERAS.has(view) ? `/camera/${view}.full.jpg` : `${basePath}${PREFIX}snapshot/${view}`;
  }

  function backoff(failures) { return Math.min(15000, 1000 * 2 ** Math.min(4, failures)); }

  function stamp(date = new Date()) { return date.toISOString().replace(/[-:]/g, "").replace(/\.\d+Z$/, "Z"); }

  // H.264 player ----------------------------------------------------------------

  function mediaSource() { return root.ManagedMediaSource || root.MediaSource || null; }

  /** True when this browser can play the H.264 previews through Media Source Extensions. */
  function h264Supported() {
    try { return typeof root.fetch === "function" && Boolean(mediaSource()?.isTypeSupported?.(H264)); }
    catch { return false; }
  }

  function joined(chunks) {
    if (chunks.length === 1) return chunks[0];
    const out = new Uint8Array(chunks.reduce((size, chunk) => size + chunk.byteLength, 0));
    let offset = 0;
    for (const chunk of chunks) { out.set(chunk, offset); offset += chunk.byteLength; }
    return out;
  }

  /** Play 1 fragmented MP4 stream in a video element. onFail runs at most once, but not after stop. */
  function playH264(video, url, onFail) {
    const Source = mediaSource();
    const source = new Source();
    const abort = new AbortController();
    let stopped = false, buffer = null, queue = [], queued = 0, objectUrl = null;

    function stop() {
      if (stopped) return;
      stopped = true;
      abort.abort();
      video.removeEventListener("error", fail);
      video.removeAttribute("src");
      video.srcObject = null;
      video.load?.();
      if (objectUrl) root.URL.revokeObjectURL(objectUrl);
    }

    function fail() {
      if (stopped) return;
      stop();
      onFail();
    }

    function feed() {
      if (stopped || !buffer || buffer.updating) return;
      try {
        if (queue.length) {
          const data = joined(queue);
          queue = []; queued = 0;
          buffer.appendBuffer(data);
          return;
        }
        const ranges = buffer.buffered;
        if (!ranges.length) return;
        const end = ranges.end(ranges.length - 1);
        if (end - ranges.start(0) > 2 * KEEP_S) { buffer.remove(ranges.start(0), end - KEEP_S); return; }
        const step = chase(video.currentTime, ranges.start(ranges.length - 1), end);
        if (step.seek !== null) video.currentTime = step.seek;
        video.playbackRate = step.rate;
      } catch { fail(); }
    }

    async function pump() {
      try {
        const response = await root.fetch(url, { cache: "no-store", signal: abort.signal });
        const type = response.headers.get("Content-Type") || "";
        if (!response.ok || !response.body || !Source.isTypeSupported(type)) throw new Error("No H.264 preview");
        buffer = source.addSourceBuffer(type);
        buffer.mode = "segments";
        buffer.addEventListener("updateend", feed);
        const reader = response.body.getReader();
        while (!stopped) {
          const { done, value } = await reader.read();
          if (done) break;
          queue.push(value);
          queued += value.byteLength;
          // A page that cannot keep up restarts the stream at the newest frame.
          if (queued > MAX_QUEUE) break;
          feed();
        }
      } catch { /* The stream ended or failed. */ }
      fail();
    }

    video.addEventListener("error", fail);
    source.addEventListener("sourceopen", pump, { once: true });
    if (Source === root.ManagedMediaSource) {
      video.disableRemotePlayback = true;
      video.srcObject = source;
    } else {
      objectUrl = root.URL.createObjectURL(source);
      video.src = objectUrl;
    }
    video.play?.()?.catch?.(() => {});
    return stop;
  }

  /** Count the frames that a video presents into rate. Return a function that stops it. */
  function countFrames(video, rate) {
    if (typeof video.requestVideoFrameCallback !== "function") return () => {};
    let handle = null;
    const step = (_now, metadata) => {
      measure(rate, metadata.presentedFrames, nowMs());
      handle = video.requestVideoFrameCallback(step);
    };
    handle = video.requestVideoFrameCallback(step);
    return () => {
      if (handle !== null) video.cancelVideoFrameCallback?.(handle);
      handle = null;
    };
  }

  // Shared engine ---------------------------------------------------------------

  const engine = {
    api: null, tiles: new Set(), status: new Map(), statusAt: -Infinity, statusBusy: false,
    thermal: null, thermalAt: -Infinity, thermalFailed: false, thermalBusy: false, thermalTimer: null,
    thermalFrames: 0, thermalId: null, thermalRate: meter(),
    detections: null, detectBusy: false, detectTimer: null, timer: null, regions: {}, probe: null,
    settings: { unit: "C", palette: "ironbow", scale: "auto", low: 20, high: 40, frozen: false, markers: true, detections: true },
    listeners: new Set(),
  };

  function doc() { return root.document; }
  function lang() { return engine.api?.lang?.() || root.LabConsole?.shell?.lang || "en"; }
  function t(key, vars) { return translate(lang(), key, vars); }
  function base() { return doc()?.body?.dataset?.basePath || ""; }
  function nowMs() { return root.performance?.now ? root.performance.now() : Date.now(); }

  function request(path, data) {
    const send = engine.api?.consoleRequest || root.LabConsole?._internal?.request;
    if (!send) return Promise.reject(new Error("The console shell is not loaded."));
    return send(PREFIX + path, data);
  }

  function h(tag, props, ...children) {
    const node = doc().createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else if (key === "hidden" || key === "disabled") node[key] = Boolean(value);
      else node.setAttribute(key, String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(typeof child === "string" ? doc().createTextNode(child) : child);
    }
    return node;
  }

  function changed() { for (const fn of engine.listeners) { try { fn(); } catch (error) { console.error(error); } } }

  function setSettings(patch) {
    Object.assign(engine.settings, patch);
    for (const tile of engine.tiles) tile.paint();
    changed();
  }

  function running(view) { return [...engine.tiles].some(tile => tile.running && (!view || tile.view === view)); }

  async function pollStatus() {
    if (engine.statusBusy || !running()) return;
    engine.statusBusy = true;
    try {
      const body = await request("status");
      engine.status = new Map((body.views || []).map(row => [row.id, row]));
      engine.statusAt = nowMs();
    } catch { /* Badges go OFFLINE when the status is old. */ }
    finally { engine.statusBusy = false; }
  }

  async function pollThermal() {
    engine.thermalTimer = null;
    if (!running("thermal")) return;
    if (!engine.settings.frozen && !engine.thermalBusy) {
      engine.thermalBusy = true;
      try {
        const frame = await request("thermal/frame");
        if (typeof frame?.temperatures_u16 === "string") frame.temperatures_c = thermalTemperatures(frame.temperatures_u16);
        // Count each new sensor frame once, also when a poll returns the same frame again.
        const id = frame?.sequence ?? frame?.timestamp ?? null;
        if (id === null || id !== engine.thermalId) engine.thermalFrames++;
        engine.thermalId = id;
        engine.thermal = frame;
        engine.thermalAt = nowMs();
        measure(engine.thermalRate, engine.thermalFrames, engine.thermalAt);
        engine.thermalFailed = false;
      } catch { engine.thermalFailed = true; }
      finally { engine.thermalBusy = false; }
      for (const tile of engine.tiles) if (tile.view === "thermal") tile.paint();
      changed();
    }
    engine.thermalTimer = setTimeout(pollThermal, engine.thermalFailed ? 2000 : THERMAL_MS);
  }

  async function pollDetections() {
    engine.detectTimer = null;
    if (!running("camera4") || !engine.settings.detections) { engine.detections = null; return; }
    if (!engine.detectBusy) {
      engine.detectBusy = true;
      try { engine.detections = await request("detections/4"); }
      catch { engine.detections = { available: false }; }
      finally { engine.detectBusy = false; }
      for (const tile of engine.tiles) if (tile.view === "camera4") tile.paint();
      changed();
    }
    engine.detectTimer = setTimeout(pollDetections, DETECT_MS);
  }

  async function loadRegions() {
    try { engine.regions = (await request("thermal/regions")).regions || {}; }
    catch { engine.regions = {}; }
    for (const tile of engine.tiles) if (tile.view === "thermal") tile.paint();
    changed();
  }

  function rowFor(view) {
    if (view === "thermal") {
      return { ...(engine.status.get("thermal") || {}), ...thermalRow(engine.thermal, engine.thermalAt, nowMs(), engine.thermalFailed) };
    }
    const row = engine.status.get(view) || null;
    // The browser loads camera1 to camera4 and the H.264 microscope itself: a loaded tile is live.
    const own = [...engine.tiles].filter(tile => tile.view === view && (CAMERAS.has(view) || tile.stopVideo));
    if (row && (CAMERAS.has(view) || own.length)) {
      const live = own.some(tile => tile.live);
      return { ...row, state: live ? "live" : "offline", age_s: live ? 0 : null };
    }
    return row;
  }

  function viewBadge(view) {
    if (view === "thermal") return badge(rowFor(view), { frozen: engine.settings.frozen });
    return badge(rowFor(view), { fresh: nowMs() - engine.statusAt < 3000 });
  }

  function visible(node) {
    if (!node.isConnected || doc().visibilityState === "hidden") return false;
    return node.getClientRects().length > 0;
  }

  function tick() {
    for (const tile of [...engine.tiles]) {
      if (!tile.el.isConnected && !tile.keep) { tile.stop(); engine.tiles.delete(tile); continue; }
      const show = visible(tile.el);
      if (show && !tile.running) tile.start();
      else if (!show && tile.running) tile.stop();
      tile.refresh();
    }
    pollStatus();
    if (running("thermal") && engine.thermalTimer === null) pollThermal();
    if (running("camera4") && engine.settings.detections && engine.detectTimer === null) pollDetections();
  }

  function download(name, blob) {
    const url = root.URL.createObjectURL(blob);
    const link = h("a", { href: url, download: name });
    doc().body.append(link);
    link.click();
    link.remove();
    setTimeout(() => root.URL.revokeObjectURL(url), 10000);
  }

  async function snapshot(view, tile) {
    if (view === "thermal") {
      const canvas = tile?.canvas;
      if (!canvas || !engine.thermal) throw new Error(t("export.none"));
      await new Promise(resolve => canvas.toBlob(blob => { if (blob) download(`thermal-${stamp()}.png`, blob); resolve(); }, "image/png"));
      return;
    }
    const link = h("a", { href: snapshotUrl(view, base()), download: `${view}-${stamp()}.${CAMERAS.has(view) ? "jpg" : "png"}` });
    doc().body.append(link);
    link.click();
    link.remove();
  }

  // Tile ------------------------------------------------------------------------

  class Tile {
    constructor(view, options) {
      this.view = view;
      this.options = options;
      this.large = options.large === true;
      this.running = false;
      this.failures = 0;
      this.retry = null;
      this.generation = 0;
      this.drawing = null;
      this.build();
    }

    build() {
      const view = this.view;
      this.title = h("span", { class: "lcam-title" });
      this.badgeText = h("span", { class: "lcam-badge-text" });
      this.badge = h("span", { class: "lcam-badge tone-off", role: "status" }, h("span", { class: "lc-dot", "aria-hidden": "true" }), this.badgeText);
      this.mode = h("span", { class: "lcam-mode" });
      this.note = h("span", { class: "lcam-note" });
      this.overlay = h("canvas", { class: "lcam-overlay", "aria-hidden": "true" });
      if (view === "thermal") {
        this.canvas = h("canvas", { class: "lcam-media", width: W * 4, height: H * 4 });
        this.colorbar = h("div", { class: "lcam-colorbar" });
        this.media = this.canvas;
        this.bindDrawing();
      } else {
        this.img = h("img", { class: "lcam-media", alt: "", decoding: "async" });
        this.img.addEventListener("error", () => { this.live = false; this.failed(); });
        this.img.addEventListener("load", () => { if (this.running && this.img.src !== BLANK) { this.failures = 0; this.live = true; this.paint(); } });
        this.media = this.img;
      }
      if (videoUrl(view) && h264Supported()) {
        this.video = h("video", { class: "lcam-media", hidden: true });
        this.video.muted = true;
        this.video.playsInline = true;
        this.video.addEventListener("playing", () => {
          if (this.running && this.stopVideo) { this.failures = 0; this.played = true; this.live = true; this.paint(); }
        });
      }
      this.empty = h("div", { class: "lcam-empty" });
      this.stage = h("div", { class: "lcam-stage", role: "button", tabindex: "0",
        onclick: () => this.options.onSelect?.(view),
        onkeydown: event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); this.options.onSelect?.(view); } } },
      this.media, this.video || null, this.overlay, this.empty, this.colorbar || null);
      this.snapButton = h("button", { type: "button", class: "lc-button lcam-small", onclick: event => { event.stopPropagation(); this.snapshot(); } });
      this.expandButton = h("button", { type: "button", class: "lc-button lcam-small", onclick: event => { event.stopPropagation(); this.expand(); } });
      // The thermal tile shows its live readings. A workspace may add buttons before Snapshot.
      this.readings = view === "thermal" ? h("div", { class: "lcam-readings" }) : null;
      this.meta = h("div", { class: "lcam-meta" }, this.mode, this.note);
      this.el = h("figure", { class: "lcam-tile", "data-view": view },
        h("div", { class: "lcam-head" }, h("div", { class: "lcam-names" }, this.title), this.badge, this.readings),
        this.stage,
        h("figcaption", { class: "lcam-foot" },
          this.meta,
          h("div", { class: "lcam-buttons" }, ...(this.options.buttons || []), this.snapButton, this.expandButton)));
      this.el.labCameras = this;
      this.refresh();
    }

    start() {
      this.running = true;
      this.generation++;
      if (this.view === "thermal") { if (engine.thermalTimer === null) pollThermal(); this.paint(); return; }
      if (this.video && !this.mjpeg) {
        const generation = this.generation;
        this.played = false;
        this.img.hidden = true;
        this.video.hidden = false;
        this.stopVideo = playH264(this.video, videoUrl(this.view), () => { if (generation === this.generation) this.failed(); });
        this.videoRate = meter();
        this.stopCount = countFrames(this.video, this.videoRate);
      } else {
        if (this.video) this.video.hidden = true;
        this.img.hidden = false;
        this.img.src = streamUrl(this.view, this.large, base());
      }
      pollStatus();
    }

    /** Close the H.264 stream or the MJPEG stream. */
    halt() {
      if (this.stopCount) { this.stopCount(); this.stopCount = null; }
      if (this.stopVideo) { this.stopVideo(); this.stopVideo = null; }
      // Replacing the source closes the stream, so the console ends its relay.
      if (this.img) this.img.src = BLANK;
    }

    /** Grow or shrink the tile. A running stream restarts with the new frame rate. */
    setLarge(large) {
      this.el.classList?.toggle("lcam-large", large);
      if (this.large === large) return;
      this.large = large;
      if (this.running && MJPEG.has(this.view) && !this.stopVideo) { this.stop(); this.start(); }
    }

    stop() {
      this.running = false;
      this.live = false;
      this.generation++;
      if (this.retry) { clearTimeout(this.retry); this.retry = null; }
      this.halt();
      this.paint();
    }

    failed() {
      if (!this.running || (!this.stopVideo && this.img && this.img.src === BLANK)) return;
      this.failures++;
      this.live = false;
      // H.264 that never played retries at once as MJPEG: the browser or the host cannot
      // play it. A failed MJPEG stream retries as H.264.
      const fallback = Boolean(this.stopVideo && !this.played);
      if (this.video) this.mjpeg = fallback;
      this.halt();
      if (this.retry) clearTimeout(this.retry);
      const generation = this.generation;
      // Retry the view only; a view is not a device command.
      this.retry = setTimeout(() => {
        this.retry = null;
        if (this.running && generation === this.generation) { this.running = false; this.start(); }
      }, fallback ? 0 : backoff(this.failures));
    }

    async snapshot() {
      try { await snapshot(this.view, this); }
      catch (error) { (engine.api?.notify || console.warn)(t("snapshot.failed", { error: error.message }), "warn"); }
    }

    expand() {
      if (this.options.onExpand) this.options.onExpand(this.view);
      else if (this.el.requestFullscreen) this.el.requestFullscreen().catch(() => {});
    }

    refresh() {
      const view = this.view;
      // The tile shows the role; the camera ID or the model is in the tooltip.
      const name = t("title." + view) + " · " + t("role." + view);
      this.title.textContent = t((CAMERAS.has(view) ? "role." : "title.") + view);
      this.title.setAttribute("title", name);
      this.snapButton.textContent = t("tile.snapshot");
      this.expandButton.textContent = t("tile.expand");
      this.stage.setAttribute("aria-label", t("tile.select", { name }));
      if (this.img) this.img.alt = name;
      const row = rowFor(view);
      const state = viewBadge(view);
      // A live view shows a dot only; STALE, OFFLINE and FROZEN show their word.
      const live = state.key === "badge.live";
      const label = t(state.key) + (state.age !== null && state.age !== undefined ? " · " + t("age", { age: state.age.toFixed(1) }) : "");
      this.badge.className = "lcam-badge tone-" + state.tone + (live ? " lcam-badge-dot" : "");
      this.badge.setAttribute("title", label);
      this.badge.setAttribute("aria-label", label);
      this.badgeText.textContent = live ? "" : label;
      const offline = state.key === "badge.offline";
      const d = engine.detections;
      const found = view === "camera4" && engine.settings.detections && d?.available ? t("side.detectionsCount", { count: d.detections.length }) : "";
      const mode = view === "thermal" ? null : row?.mode || (offline ? null : this.decodedMode());
      this.mode.textContent = modeLine(mode, this.rate(row), found);
      this.empty.hidden = !offline;
      this.empty.textContent = row && row.configured === false ? t("tile.notConfigured") : t("tile.noFrame");
      this.snapButton.disabled = offline;
      if (view === "thermal") {
        const stats = engine.thermal?.stats;
        const unit = engine.settings.unit;
        // Min and the center show on the color bar and the image markers.
        const values = stats ? [["reading.max", stats.max?.celsius], ["reading.mean", stats.mean_celsius]] : [];
        const text = values.map(([key, value]) => t(key) + " " + formatTemp(value, unit)).join("|");
        if (this.readings && text !== this.readingsText) {
          this.readingsText = text;
          this.readings.replaceChildren(...values.map(([key, value]) => h("span", {}, h("small", { text: t(key) }), h("b", { text: formatTemp(value, unit) }))));
        }
        const reading = probeReading(engine.thermal, engine.probe);
        this.note.textContent = reading
          ? t(engine.probe.pinned ? "probe.pinned" : "probe", { x: reading.x, y: reading.y, value: formatTemp(reading.celsius, engine.settings.unit) })
          : this.readings ? "" : stats ? t("reading.mean") + " " + formatTemp(stats.mean_celsius, engine.settings.unit) : "";
      }
      this.meta.hidden = !this.mode.textContent && !this.note.textContent;
    }

    /** The decoded size of a playing view, for example "1920×1080", or null. */
    decodedMode() {
      if (!this.running || !this.live) return null;
      const v = this.stopVideo ? this.video : this.img;
      const w = this.stopVideo ? v?.videoWidth : v?.naturalWidth, hgt = this.stopVideo ? v?.videoHeight : v?.naturalHeight;
      return w > 1 && hgt > 1 ? `${w}×${hgt}` : null;
    }

    /** The measured frame rate: H.264 and thermal count in the page, MJPEG comes from the sender. */
    rate(row) {
      if (!this.running) return null;
      if (this.view === "thermal") return current(engine.thermalRate, nowMs());
      if (this.stopVideo) return current(this.videoRate, nowMs());
      return typeof row?.fps === "number" && Number.isFinite(row.fps) ? row.fps : null;
    }

    paint() {
      this.refresh();
      if (this.view === "thermal") this.paintThermal();
      else this.paintDetections();
    }

    paintDetections() {
      const ctx = this.overlay.getContext?.("2d");
      if (!ctx) return;
      const boxW = this.stage.clientWidth, boxH = this.stage.clientHeight;
      this.overlay.width = boxW; this.overlay.height = boxH;
      ctx.clearRect(0, 0, boxW, boxH);
      const d = engine.detections;
      if (this.view !== "camera4" || !this.running || !engine.settings.detections || !d?.available) return;
      const rect = containRect(d.image_size.width, d.image_size.height, boxW, boxH);
      ctx.lineWidth = 2;
      ctx.font = "600 13px system-ui, sans-serif";
      for (const item of d.detections) {
        const [x0, y0, x1, y1] = item.xyxy.map(v => v * rect.scale);
        ctx.strokeStyle = "#4DA3FF";
        ctx.strokeRect(rect.x + x0, rect.y + y0, x1 - x0, y1 - y0);
        const label = `${item.label || "chip"} ${Math.round(item.confidence * 100)}%`;
        const width = ctx.measureText(label).width + 10;
        ctx.fillStyle = "rgba(23,19,16,0.72)";
        ctx.fillRect(rect.x + x0, Math.max(0, rect.y + y0 - 20), width, 20);
        ctx.fillStyle = "#E6EDF2";
        ctx.fillText(label, rect.x + x0 + 5, Math.max(14, rect.y + y0 - 6));
      }
    }

    paintThermal() {
      const frame = engine.thermal;
      const ctx = this.canvas.getContext?.("2d");
      const s = engine.settings;
      const [low, high] = scaleRange(frame?.stats, s);
      // Without a frame there is no scale to show.
      if (frame) this.paintColorbar(low, high);
      else if (this.colorbar) this.colorbar.hidden = true;
      if (!ctx || !frame) return;
      const source = h("canvas", { width: W, height: H });
      source.getContext("2d").putImageData(new root.ImageData(thermalPixels(frame.temperatures_c, paletteTable(s.palette), low, high), W, H), 0, 0);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(source, 0, 0, W * 4, H * 4);
      const region = this.drawing?.region || engine.regions.crucible;
      if (region) {
        ctx.strokeStyle = this.drawing ? "#F2B84B" : "#E6EDF2";
        ctx.setLineDash(this.drawing ? [6, 4] : []);
        ctx.lineWidth = 2;
        ctx.strokeRect(region.x0 * 4, region.y0 * 4, (region.x1 - region.x0) * 4, (region.y1 - region.y0) * 4);
        ctx.setLineDash([]);
      }
      const reading = probeReading(frame, engine.probe);
      if (reading) {
        const x = reading.x * 4 + 2, y = reading.y * 4 + 2;
        ctx.strokeStyle = "#FFFFFF"; ctx.lineWidth = 2;
        ctx.strokeRect(x - 6, y - 6, 12, 12);
      }
      if (!s.markers || !frame.stats) return;
      const marks = [["max", "#FF7A7A"], ["min", "#7DBBFF"], ["center", "#E6EDF2"]];
      ctx.font = "600 14px system-ui, sans-serif";
      for (const [key, color] of marks) {
        const point = frame.stats[key];
        if (!point) continue;
        const x = point.x * 4 + 2, y = point.y * 4 + 2;
        ctx.strokeStyle = color; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(x - 8, y); ctx.lineTo(x + 8, y); ctx.moveTo(x, y - 8); ctx.lineTo(x, y + 8); ctx.stroke();
        const label = convert(point.celsius, s.unit).toFixed(1) + "°";
        const tx = Math.min(W * 4 - 52, x + 8), ty = Math.max(16, y - 8);
        ctx.fillStyle = "rgba(23,19,16,0.72)"; ctx.fillRect(tx, ty - 14, ctx.measureText(label).width + 8, 18);
        ctx.fillStyle = color; ctx.fillText(label, tx + 4, ty);
      }
    }

    paintColorbar(low, high) {
      if (!this.colorbar) return;
      const s = engine.settings;
      this.colorbar.hidden = !s.markers;
      const stops = STOPS[s.palette] || STOPS.ironbow;
      this.colorbar.replaceChildren(
        h("span", { class: "lcam-cb-label", text: convert(high, s.unit).toFixed(1) + unitLabel(s.unit) }),
        h("span", { class: "lcam-cb-bar", role: "img", "aria-label": t("colorbar"), style: `background: linear-gradient(to top, ${stops.join(", ")})` }),
        h("span", { class: "lcam-cb-label", text: convert(low, s.unit).toFixed(1) + unitLabel(s.unit) }));
    }

    bindDrawing() {
      const point = event => {
        const box = this.canvas.getBoundingClientRect();
        const rect = containRect(W, H, box.width, box.height);
        if (!rect.scale) return { x: 0, y: 0 };
        return { x: (event.clientX - box.left - rect.x) / rect.scale, y: (event.clientY - box.top - rect.y) / rect.scale };
      };
      this.canvas.addEventListener("pointerdown", event => {
        if (!this.drawing) return;
        event.preventDefault();
        this.drawing.anchor = point(event);
        this.canvas.setPointerCapture?.(event.pointerId);
      });
      this.canvas.addEventListener("pointermove", event => {
        if (!this.drawing?.anchor) return;
        this.drawing.region = regionFrom(this.drawing.anchor, point(event)) || this.drawing.region;
        this.paintThermal();
      });
      this.canvas.addEventListener("pointerup", event => {
        if (!this.drawing?.anchor) return;
        this.drawing.region = regionFrom(this.drawing.anchor, point(event)) || this.drawing.region;
        this.drawing.anchor = null;
        this.paintThermal();
        this.drawing.onChange?.(this.drawing.region);
      });
      // Pixel probe: the pointer reads 1 sensor pixel. A click pins it; the next click unpins it.
      const pixel = event => { const p = point(event); return { x: Math.floor(p.x), y: Math.floor(p.y) }; };
      this.canvas.addEventListener("pointermove", event => {
        if (this.drawing || engine.probe?.pinned) return;
        engine.probe = pixel(event);
        this.paint();
      });
      this.canvas.addEventListener("pointerleave", () => {
        if (this.drawing || engine.probe?.pinned || !engine.probe) return;
        engine.probe = null;
        this.paint();
      });
      this.canvas.addEventListener("click", event => {
        if (this.drawing) return;
        engine.probe = engine.probe?.pinned ? null : { ...pixel(event), pinned: true };
        this.paint();
      });
    }
  }

  /** A live camera tile for any workspace. view: camera1–camera4, thermal or microscope.
   * options: {api, large, onSelect(view), onExpand(view), buttons}. The tile streams only while it is visible. */
  function tile(view, options = {}) {
    if (!VIEWS.includes(view)) throw new Error("Unknown camera view: " + view);
    if (options.api && !engine.api) engine.api = options.api;
    const item = new Tile(view, options);
    if (options.large) item.el.classList?.add("lcam-large");
    engine.tiles.add(item);
    if (!engine.timer && root.document) {
      engine.timer = setInterval(tick, STATUS_MS);
      root.document.addEventListener?.("visibilitychange", tick);
      loadRegions();
    }
    setTimeout(tick, 0);
    return item.el;
  }

  // Workspace -------------------------------------------------------------------

  /** The Cameras workspace: all 6 views as a wall, or 1 large view with a rail of the others.
   * Each tile carries its own tools. The thermal settings open on demand in a card on the thermal tile. */
  function workspace(rootNode, api) {
    engine.api = api;
    const ui = { layout: "wall", focus: "camera2", cells: new Map(), open: false, confirm: null, describe: null };
    const toolbar = h("div", { class: "lcam-toolbar" });
    const wall = h("div", { class: "lcam-wall" });
    // The card and the drawing bar sit in the thermal cell, beside the tile, so the tile can clip its media.
    const card = h("section", {
      class: "lcam-card", id: "lcam-card", role: "dialog", "aria-labelledby": "lcam-card-title", tabindex: "-1", hidden: true,
      onkeydown: event => { if (event.key === "Escape") { event.preventDefault?.(); if (ui.confirm) { ui.confirm = null; card.dataset.focusNext = "shutter"; renderCard(); } else closeSettings(); } },
    });
    const drawbar = h("div", { class: "lcam-drawbar", hidden: true });
    const settingsButton = h("button", { type: "button", class: "lc-button lcam-small lcam-settings", "aria-expanded": "false", "aria-controls": "lcam-card",
      onclick: event => { event.stopPropagation?.(); if (ui.open) closeSettings(); else openSettings(); } });
    for (const view of VIEWS) {
      const node = tile(view, {
        api, buttons: view === "thermal" ? [settingsButton] : [],
        // In the large view a rail tile takes the large place. In the wall a click only reads (the thermal probe).
        onSelect: v => { if (ui.layout === "large" && v !== ui.focus) { ui.focus = v; arrange(); } },
        onExpand: v => { ui.layout = "large"; ui.focus = v; arrange(); },
      });
      const cell = h("div", { class: "lcam-cell", "data-view": view }, node, view === "thermal" ? drawbar : null, view === "thermal" ? card : null);
      ui.cells.set(view, { cell, node });
      wall.append(cell);
    }
    rootNode.replaceChildren(h("div", { class: "lcam-ws" }, toolbar, wall));
    const thermalTile = () => ui.cells.get("thermal").node.labCameras;

    // Toolbar: built once per language; updateToolbar() sets the pressed states in place.
    const bar = {};
    function toggle(label, onclick, cls) {
      return h("button", { type: "button", class: cls, "aria-pressed": "false", onclick, text: label });
    }
    function renderToolbar() {
      const s = engine.settings;
      bar.wall = toggle(api.t("layout.wall"), () => { ui.layout = "wall"; arrange(); }, "lcam-seg-btn");
      bar.large = toggle(api.t("layout.large"), () => { ui.layout = "large"; arrange(); }, "lcam-seg-btn");
      bar.detections = toggle(api.t("overlay.detections"), () => { setSettings({ detections: !s.detections }); updateToolbar(); }, "lcam-chip");
      bar.markers = toggle(api.t("overlay.thermal"), () => { setSettings({ markers: !s.markers }); updateToolbar(); }, "lcam-chip");
      toolbar.replaceChildren(
        h("div", { class: "lcam-seg", role: "group", "aria-label": api.t("layout.label") }, bar.wall, bar.large),
        h("div", { class: "lcam-chips", role: "group", "aria-label": api.t("overlay.label") }, bar.detections, bar.markers),
        h("span", { class: "lcam-grow" }),
        h("button", { type: "button", class: "lc-button lcam-snapall", text: api.t("snapshot.all"), onclick: snapshotAll }));
      updateToolbar();
    }
    function updateToolbar() {
      const s = engine.settings;
      for (const [node, on] of [[bar.wall, ui.layout === "wall"], [bar.large, ui.layout === "large"], [bar.detections, s.detections], [bar.markers, s.markers]]) {
        node.setAttribute("aria-pressed", on ? "true" : "false");
        node.className = node.className.replace(/ lcam-on$/, "") + (on ? " lcam-on" : "");
      }
      settingsButton.textContent = api.t("settings");
      settingsButton.setAttribute("aria-expanded", ui.open ? "true" : "false");
      settingsButton.className = "lc-button lcam-small lcam-settings" + (ui.open ? " lcam-on" : "");
    }

    async function snapshotAll() {
      for (const view of VIEWS) {
        if (viewBadge(view).key === "badge.offline") continue;
        try { await snapshot(view, ui.cells.get(view).node.labCameras); } catch { /* One failed view does not stop the others. */ }
      }
    }

    /** Wall or large view. The tiles stay in place in the page; only their cells change class, so no stream restarts. */
    function arrange() {
      wall.className = "lcam-wall lcam-mode-" + ui.layout;
      for (const [view, { cell, node }] of ui.cells) {
        const big = ui.layout === "large" && view === ui.focus;
        cell.className = "lcam-cell" + (big ? " lcam-big" : "");
        node.labCameras.setLarge(big);
      }
      updateToolbar();
      setTimeout(tick, 0);
    }

    // Thermal settings card ---------------------------------------------------------

    function commandInfo(action) { return (ui.describe?.commands || {})[action] || null; }

    async function send(action, params) {
      const result = await api.relay("thermal", action, params);
      const name = LABELS[action] ? api.t(LABELS[action]) : action;
      if (result.outcome === "accepted") api.notify(api.t("command.accepted", { action: name }), "info");
      else if (result.outcome === "unknown") api.notify(api.t("command.unknown", { action: name }), "error");
      else api.notify(api.t("command.refused", { error: result.error || "" }), "warn");
      if (action === "set_region") loadRegions();
      return result;
    }
    // The command names in the notice are the button labels.
    const LABELS = { connect: "connect", shutter: "shutter", set_region: "region.save" };

    function openSettings() {
      ui.open = true;
      card.hidden = false;
      renderCard();
      updateToolbar();
      card.focus?.();
    }

    function closeSettings() {
      ui.open = false;
      ui.confirm = null;
      card.hidden = true;
      card.replaceChildren();
      updateToolbar();
      settingsButton.focus?.();
    }

    function seg(label, options, current, onPick) {
      return h("div", { class: "lcam-row" }, h("span", { class: "lcam-label", id: "lcam-l-" + label, text: api.t(label) }),
        h("div", { class: "lcam-seg", role: "group", "aria-labelledby": "lcam-l-" + label },
          ...options.map(([value, text]) => h("button", {
            type: "button", class: "lcam-seg-btn" + (current === value ? " lcam-on" : ""), "aria-pressed": current === value ? "true" : "false",
            "data-focus": label + "-" + value, text, onclick: () => { onPick(value); renderCard(); },
          }))));
    }

    function controlLine() {
      const needs = ["shutter", "set_region"].some(action => { const info = commandInfo(action); return info && !info.any_client; });
      if (!needs) return null;
      const mine = api.owns("thermal");
      return h("div", { class: "lcam-row" }, h("span", { class: "lcam-note", text: mine ? "" : api.t("command.control") }), h("span", { class: "lcam-grow" }),
        h("button", { type: "button", class: "lc-button", "data-focus": "control", text: api.t(mine ? "command.release" : "command.take"),
          onclick: () => (mine ? api.release("thermal") : api.take("thermal")).catch(error => api.notify(error.message, "warn")).then(renderCard) }));
    }

    /** The card content: display settings, then the calibration tools and the export. */
    function cardParts() {
      const s = engine.settings;
      const parts = [h("div", { class: "lcam-card-head" }, h("h2", { id: "lcam-card-title", text: api.t("settings.title") }),
        h("button", { type: "button", class: "lc-button lcam-small", "data-focus": "close", text: api.t("settings.close"), onclick: closeSettings }))];
      const service = (api.state()?.services || []).find(item => item.id === "thermal");
      if (service && service.connected !== true && commandInfo("connect")) {
        parts.push(h("div", { class: "lcam-row" }, h("span", { class: "lcam-label", text: api.t("camera.label") }), h("span", { class: "lcam-grow" }),
          h("button", { type: "button", class: "lc-button", "data-focus": "connect", text: api.t("connect"), onclick: () => send("connect", {}) })));
      }
      parts.push(seg("units", [["C", "°C"], ["F", "°F"], ["K", "K"]], s.unit, unit => setSettings({ unit })));
      const palette = h("select", { id: "lcam-palette", class: "lcam-select", "data-focus": "palette", onchange: event => setSettings({ palette: event.target.value }) },
        ...["ironbow", "rainbow", "grey"].map(name => h("option", { value: name, text: api.t("palette." + name), selected: s.palette === name ? "selected" : null })));
      palette.value = s.palette;
      parts.push(h("div", { class: "lcam-row" }, h("label", { class: "lcam-label", for: "lcam-palette", text: api.t("palette") }), palette));
      const limit = key => h("input", {
        id: "lcam-" + key, class: "lcam-input", type: "number", step: "0.5", inputmode: "decimal", disabled: s.scale !== "fixed", "data-focus": "limit-" + key,
        "aria-label": api.t("scale." + key) + " (" + unitLabel(s.unit) + ")", value: convert(s[key], s.unit).toFixed(1),
        onchange: event => { const value = Number(event.target.value); if (Number.isFinite(value)) setSettings({ [key]: toCelsius(value, s.unit) }); },
      });
      parts.push(seg("scale", [["auto", api.t("scale.auto")], ["fixed", api.t("scale.fixed")]], s.scale, scale => setSettings({ scale })));
      parts.push(h("div", { class: "lcam-row" }, h("span", { class: "lcam-label" }), limit("low"), h("span", { class: "lcam-note", text: "–" }), limit("high"),
        h("span", { class: "lcam-note", text: unitLabel(s.unit) })));
      parts.push(h("div", { class: "lcam-row" }, h("span", { class: "lcam-label" }),
        h("button", { type: "button", class: "lc-button" + (s.frozen ? " lcam-on" : ""), "aria-pressed": s.frozen ? "true" : "false", "data-focus": "freeze",
          text: s.frozen ? api.t("unfreeze") : api.t("freeze"), onclick: () => { setSettings({ frozen: !s.frozen }); renderCard(); } })));
      parts.push(h("div", { class: "lcam-rule" }));
      const region = engine.regions.crucible;
      parts.push(h("div", { class: "lcam-row" }, h("span", { class: "lcam-label", text: api.t("region") }), h("span", { class: "lcam-grow" }),
        h("button", { type: "button", class: "lc-button", "data-focus": "region-draw", text: api.t("region.draw"), onclick: startDrawing })));
      parts.push(h("p", { class: "lcam-note lcam-indent", text: region ? api.t("region.bounds", region) : api.t("region.none") }));
      if (ui.confirm === "shutter") {
        parts.push(h("div", { class: "lc-confirm", role: "alertdialog", "aria-label": api.t("shutter.confirm"), tabindex: "-1", "data-focus": "confirm" },
          h("p", { text: api.t("shutter.confirm") }),
          h("div", { class: "lcam-row" },
            h("button", { type: "button", class: "lc-danger lcam-confirm", text: api.t("confirm"), onclick: () => { ui.confirm = null; cardNode.dataset.focusNext = "shutter"; send("shutter", {}); renderCard(); } }),
            h("button", { type: "button", class: "lc-button", text: api.t("cancel"), onclick: () => { ui.confirm = null; cardNode.dataset.focusNext = "shutter"; renderCard(); } }))));
      } else {
        parts.push(h("div", { class: "lcam-row" }, h("span", { class: "lcam-label", text: api.t("shutter.label") }), h("span", { class: "lcam-grow" }),
          h("button", { type: "button", class: "lc-button", "data-focus": "shutter", text: api.t("shutter"), disabled: !commandInfo("shutter"),
            onclick: () => { ui.confirm = "shutter"; renderCard(); } })));
      }
      if (!commandInfo("shutter")) parts.push(h("p", { class: "lcam-note", text: api.t("command.missing") }));
      const control = controlLine();
      if (control) parts.push(control);
      parts.push(h("div", { class: "lcam-row" }, h("span", { class: "lcam-label", text: api.t("export") }), h("span", { class: "lcam-grow" }),
        h("button", { type: "button", class: "lc-button", "data-focus": "export-csv", text: api.t("export.csv"), disabled: !engine.thermal, onclick: () => {
          if (engine.thermal) download(`thermal-${engine.thermal.sequence}.csv`, new root.Blob([thermalCsv(engine.thermal, s.unit)], { type: "text/csv" }));
        } }),
        h("button", { type: "button", class: "lc-button", "data-focus": "export-raw", text: api.t("export.raw"), disabled: !engine.thermal, onclick: async () => {
          try {
            const frame = await request("thermal/frame?full=1");
            download(`thermal-${frame.sequence}.json`, new root.Blob([JSON.stringify(frame)], { type: "application/json" }));
          } catch (error) { api.notify(api.t("snapshot.failed", { error: error.message }), "warn"); }
        } }),
        h("button", { type: "button", class: "lc-button", "data-focus": "export-png", text: api.t("export.png"), disabled: !engine.thermal,
          onclick: () => snapshot("thermal", thermalTile()).catch(error => api.notify(error.message, "warn")) })));
      return parts;
    }
    const cardNode = card;

    /** Everything that changes which controls the card shows. A new frame alone never rebuilds it. */
    function cardKey() {
      const service = (api.state()?.services || []).find(item => item.id === "thermal");
      return JSON.stringify([api.lang?.(), engine.settings, ui.confirm, Boolean(ui.describe), api.owns("thermal"),
        service?.connected === true, Boolean(engine.thermal), engine.regions.crucible || null]);
    }

    let builtKey = "";
    function renderCard() {
      if (!ui.open) return;
      const active = doc().activeElement;
      const focus = card.dataset.focusNext || (active && card.contains(active) ? active.getAttribute("data-focus") : null);
      delete card.dataset.focusNext;
      builtKey = cardKey();
      card.replaceChildren(...cardParts());
      let target = focus ? card.querySelector?.(`[data-focus="${focus}"]`) : null;
      if (!target && ui.confirm) target = card.querySelector?.('[data-focus="confirm"]');
      target?.focus?.();
    }

    // Close the card on a press outside it, like any popover. Esc closes it too.
    rootNode.addEventListener?.("pointerdown", event => {
      if (!ui.open || card.contains(event.target) || settingsButton.contains?.(event.target) || settingsButton === event.target) return;
      closeSettings();
    });

    // Region drawing: the card gets out of the way and the bar on the thermal tile holds Save and Cancel.
    function startDrawing() {
      const region = engine.regions.crucible;
      thermalTile().drawing = { region: region ? { ...region } : null, anchor: null, onChange: renderDrawbar };
      ui.drawConfirm = false;
      closeSettings();
      thermalTile().paint();
      renderDrawbar();
    }

    function stopDrawing() {
      thermalTile().drawing = null;
      ui.drawConfirm = false;
      thermalTile().paint();
      renderDrawbar();
    }

    function renderDrawbar() {
      const drawing = thermalTile().drawing;
      drawbar.hidden = !drawing;
      if (!drawing) { drawbar.replaceChildren(); return; }
      const proposed = drawing.region;
      const canSave = Boolean(proposed && commandInfo("set_region"));
      const text = ui.drawConfirm && proposed ? api.t("region.confirm", proposed) : proposed ? api.t("region.bounds", proposed) : api.t("region.drawing");
      drawbar.replaceChildren(...[
        h("span", { class: "lcam-note", text }),
        proposed && !commandInfo("set_region") ? h("span", { class: "lcam-note", text: api.t("region.manual") }) : null,
        h("span", { class: "lcam-grow" }),
        ui.drawConfirm
          ? h("button", { type: "button", class: "lc-danger lcam-confirm", text: api.t("confirm"), onclick: () => { send("set_region", { name: "crucible", ...proposed }); stopDrawing(); } })
          : h("button", { type: "button", class: "lc-button lcam-on", text: api.t("region.save"), disabled: !canSave, onclick: () => { ui.drawConfirm = true; renderDrawbar(); } }),
        h("button", { type: "button", class: "lc-button", text: api.t("region.cancel"), onclick: stopDrawing })].filter(Boolean));
    }

    // At most once a second: update the toolbar and rebuild the card only when its controls changed.
    let timer = null;
    function refresh() {
      timer = null;
      updateToolbar();
      const active = doc().activeElement;
      const typing = active && card.contains(active) && ["INPUT", "SELECT"].includes(active.tagName);
      if (ui.open && !typing && cardKey() !== builtKey) renderCard();
    }
    const soon = () => { if (!timer) timer = setTimeout(refresh, 1000); };
    engine.listeners.add(soon);
    api.onState(soon);
    api.describe("thermal").then(value => { ui.describe = value; renderCard(); renderDrawbar(); }, () => { ui.describe = null; });
    api.onLanguage(() => { renderToolbar(); renderCard(); renderDrawbar(); for (const { node } of ui.cells.values()) node.labCameras.refresh(); });
    renderToolbar();
    arrange();
  }

  function install() {
    if (!root.LabConsole || !root.document) return;
    root.LabConsole.register({
      id: "cameras",
      i18n,
      mount: workspace,
      show() { setTimeout(tick, 0); },
      hide() { setTimeout(tick, 0); },
    });
  }

  return {
    tile, install, VIEWS, i18n, engine,
    logic: { translate, badge, thermalRow, paletteTable, convert, toCelsius, formatTemp, scaleRange, thermalPixels, thermalTemperatures, thermalCsv, probeReading, regionFrom, containRect, backoff, stamp, streamUrl, snapshotUrl, videoUrl, chase, frameRate, meter, measure, current, modeLine, rowFor },
  };
});
