const MAX_FRAME = 4 * 1024 * 1024;
const FRAME_FRESH_MS = 1500;
const FRAME_HOLD_MS = 5000;
const HEADER_END = new Uint8Array([13, 10, 13, 10]);
const visionQueue = [];
let visionBusy = false;

function visionSlot(signal) {
  if (signal.aborted) return Promise.resolve(null);
  return new Promise(resolve => {
    function grant() {
      signal.removeEventListener("abort", cancel);
      visionBusy = true;
      resolve(() => {
        visionBusy = false;
        visionQueue.shift()?.();
      });
    }
    function cancel() {
      const index = visionQueue.indexOf(grant);
      if (index >= 0) visionQueue.splice(index, 1);
      resolve(null);
    }
    if (visionBusy) {
      visionQueue.push(grant);
      signal.addEventListener("abort", cancel, { once: true });
    } else grant();
  });
}

const className = label => label.toLowerCase().replace(/[ _]/g, "");

function abortable(promise, signal) {
  if (signal.aborted) return Promise.reject(new Error("Aborted"));
  return new Promise((resolve, reject) => {
    const abort = () => reject(new Error("Aborted"));
    signal.addEventListener("abort", abort, { once: true });
    Promise.resolve(promise).then(value => {
      signal.removeEventListener("abort", abort);
      resolve(value);
    }, error => {
      signal.removeEventListener("abort", abort);
      reject(error);
    });
  });
}

function findBytes(bytes, pattern) {
  outer: for (let i = 0; i <= bytes.length - pattern.length; i++) {
    for (let j = 0; j < pattern.length; j++) {
      if (bytes[i + j] !== pattern[j]) continue outer;
    }
    return i;
  }
  return -1;
}

async function readFrames(response, signal, onFrame) {
  const type = response.headers.get("Content-Type") || "";
  const boundary = /boundary="?([^";\s]+)"?/i.exec(type)?.[1];
  if (!type.toLowerCase().startsWith("multipart/x-mixed-replace") || !boundary || boundary.length > 70) {
    throw new Error("Invalid camera stream type.");
  }
  const prefix = `--${boundary}\r\n`;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = new Uint8Array(), length;
  try {
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      if (done) throw new Error("Camera stream ended.");
      const joined = new Uint8Array(buffer.length + value.length);
      joined.set(buffer);
      joined.set(value, buffer.length);
      buffer = joined;
      while (true) {
        if (length === undefined) {
          const end = findBytes(buffer, HEADER_END);
          if (end < 0) {
            if (buffer.length > 8192) throw new Error("Camera headers are too large.");
            break;
          }
          if (end > 8192) throw new Error("Camera headers are too large.");
          const headers = decoder.decode(buffer.subarray(0, end));
          const size = /(?:^|\r\n)Content-Length:\s*(\d+)\s*(?:\r\n|$)/i.exec(headers);
          if (!headers.startsWith(prefix) || !/(?:^|\r\n)Content-Type:\s*image\/jpeg\s*(?:\r\n|$)/i.test(headers) || !size) {
            throw new Error("Invalid camera frame headers.");
          }
          length = Number(size[1]);
          if (length < 4 || length > MAX_FRAME) throw new Error("Invalid camera frame length.");
          buffer = buffer.subarray(end + 4);
        }
        if (buffer.length < length + 2) break;
        if (buffer[0] !== 255 || buffer[1] !== 216 || buffer[length - 2] !== 255 || buffer[length - 1] !== 217 || buffer[length] !== 13 || buffer[length + 1] !== 10) {
          throw new Error("Incomplete camera JPEG.");
        }
        onFrame(buffer.slice(0, length));
        buffer = buffer.subarray(length + 2);
        length = undefined;
      }
    }
  } finally {
    try { await reader.cancel(); } catch { /* The aborted fetch can already be closed. */ }
    reader.releaseLock();
  }
}

function detectionOverlay(image, figure) {
  const toggle = figure.querySelector("[data-overlay]");
  const canvas = figure.querySelector("canvas");
  const status = figure.querySelector("[data-overlay-status]");
  const context = canvas.getContext("2d");
  if (!context) {
    toggle.disabled = true;
    status.textContent = "识别不可用";
    return { frame() {}, pause() {} };
  }
  let visible = false, dimensions, latest, session, timer, expiry, generation = 0;

  function eligible() {
    return toggle.checked && visible && !document.hidden && dimensions && !figure.dataset.paused;
  }

  function clear(message = "") {
    clearTimeout(expiry);
    latest = undefined;
    context.clearRect(0, 0, canvas.width, canvas.height);
    canvas.hidden = true;
    status.textContent = message;
  }

  function halt() {
    generation++;
    clearTimeout(timer);
    timer = undefined;
    session?.abort();
    session = undefined;
    clear();
  }

  function valid(result) {
    if (!result || result.available !== true || !dimensions ||
        result.coordinate_system !== "oriented_original_image_pixels" ||
        result.image_size?.width !== dimensions.width || result.image_size?.height !== dimensions.height ||
        !Array.isArray(result.detections) || result.detections.length > 256) return false;
    const point = value => Array.isArray(value) && value.length === 2 &&
      Number.isFinite(value[0]) && Number.isFinite(value[1]) && value[0] >= 0 && value[1] >= 0 &&
      value[0] <= dimensions.width && value[1] <= dimensions.height;
    return result.detections.every(item => item && Number.isSafeInteger(item.class_id) && item.class_id >= 0 &&
      typeof item.label === "string" && item.label.length <= 80 &&
      ["chip", "roboticarm"].includes(className(item.label)) &&
      Number.isFinite(item.confidence) && item.confidence >= 0 && item.confidence <= 1 &&
      Array.isArray(item.xyxy) && item.xyxy.length === 4 &&
      point(item.xyxy.slice(0, 2)) && point(item.xyxy.slice(2)) &&
      item.xyxy[2] > item.xyxy[0] && item.xyxy[3] > item.xyxy[1] &&
      (item.polygon == null || (Array.isArray(item.polygon) && item.polygon.length >= 3 &&
        item.polygon.length <= 4096 && item.polygon.every(point))));
  }

  function draw() {
    if (!latest || !eligible()) return;
    canvas.width = image.clientWidth;
    canvas.height = image.clientHeight;
    const scale = Math.min(canvas.width / dimensions.width, canvas.height / dimensions.height);
    const left = (canvas.width - dimensions.width * scale) / 2;
    const top = (canvas.height - dimensions.height * scale) / 2;
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.lineWidth = 2;
    context.font = "13px system-ui";
    context.textBaseline = "top";
    for (const item of latest.detections) {
      const color = className(item.label) === "chip" ? "#18b85a" : "#ed8a24";
      const [x0, y0, x1, y1] = item.xyxy;
      context.strokeStyle = color;
      context.strokeRect(left + x0 * scale, top + y0 * scale, (x1 - x0) * scale, (y1 - y0) * scale);
      if (item.polygon) {
        context.beginPath();
        item.polygon.forEach(([x, y], index) => {
          context[index ? "lineTo" : "moveTo"](left + x * scale, top + y * scale);
        });
        context.closePath();
        context.stroke();
      }
      const label = `${item.label} ${Math.round(item.confidence * 100)}%`;
      const x = Math.min(left + x0 * scale, Math.max(0, canvas.width - context.measureText(label).width - 6));
      const y = Math.max(top, top + y0 * scale - 20);
      context.fillStyle = color;
      context.fillRect(x, y, context.measureText(label).width + 6, 20);
      context.fillStyle = "#080d12";
      context.fillText(label, x + 3, y + 3);
    }
    canvas.hidden = false;
  }

  async function poll() {
    timer = undefined;
    if (!eligible() || session) return;
    const request = new AbortController();
    const current = generation;
    let started = performance.now(), release, timeout;
    session = request;
    try {
      release = await visionSlot(request.signal);
      if (!release || request.signal.aborted || generation !== current || !eligible()) return;
      started = performance.now();
      timeout = setTimeout(() => request.abort(), 2000);
      const response = await abortable(
        fetch(image.dataset.detections, { cache: "no-store", signal: request.signal }), request.signal,
      );
      if (!response.ok) throw new Error("Vision is unavailable.");
      const result = await abortable(response.json(), request.signal);
      if (request.signal.aborted || generation !== current || !eligible()) return;
      if (!valid(result)) throw new Error("Vision result is unavailable.");
      latest = result;
      status.textContent = "";
      draw();
      clearTimeout(expiry);
      expiry = setTimeout(() => clear("识别结果已过期"), FRAME_FRESH_MS);
    } catch {
      if (generation === current && eligible()) clear("识别不可用");
    } finally {
      release?.();
      clearTimeout(timeout);
      if (session === request) session = undefined;
      if (generation === current && eligible()) {
        timer = setTimeout(poll, Math.max(0, 1000 - (performance.now() - started)));
      }
    }
  }

  function update() {
    if (!eligible()) halt();
    else if (!session && timer === undefined) timer = setTimeout(poll, 0);
  }

  toggle.addEventListener("change", update);
  document.addEventListener("visibilitychange", update);
  if (typeof IntersectionObserver !== "undefined") {
    new IntersectionObserver(entries => {
      visible = entries.some(entry => entry.target === figure && entry.isIntersecting);
      update();
    }).observe(figure);
  }
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(draw).observe(image);

  return {
    frame(width, height) {
      if (dimensions && (width !== dimensions.width || height !== dimensions.height)) halt();
      dimensions = { width, height };
      update();
    },
    pause() { dimensions = undefined; halt(); },
  };
}

for (const image of document.querySelectorAll("img[data-source]")) {
  const figure = image.closest("figure");
  const status = figure.querySelector("small");
  const overlay = detectionOverlay(image, figure);
  let controller, currentUrl, pendingFrame, decoding = false;
  let shown = 0, measuredAt = 0, fps;
  let displayedAt, staleTimer, expiryTimer;

  function clearImage(message) {
    overlay.pause();
    clearTimeout(staleTimer);
    clearTimeout(expiryTimer);
    pendingFrame = undefined;
    image.removeAttribute("src");
    if (currentUrl) URL.revokeObjectURL(currentUrl);
    currentUrl = undefined;
    displayedAt = undefined;
    delete figure.dataset.paused;
    status.textContent = message;
  }

  function pauseImage() {
    overlay.pause();
    if (!currentUrl || performance.now() - displayedAt >= FRAME_HOLD_MS) {
      clearImage("连接中断，正在重连…");
      return;
    }
    figure.dataset.paused = "true";
    status.textContent = "画面已暂停（非实时），等待新画面…";
  }

  async function decodeLatest() {
    if (decoding) return;
    decoding = true;
    try {
      while (pendingFrame) {
        const { bytes, session, receivedAt } = pendingFrame;
        pendingFrame = undefined;
        if (session.signal.aborted || document.hidden || controller !== session) continue;
        let url = URL.createObjectURL(new Blob([bytes], { type: "image/jpeg" }));
        try {
          const frame = new Image();
          frame.src = url;
          await frame.decode();
          if (session.signal.aborted || document.hidden || controller !== session) continue;
          const age = performance.now() - receivedAt;
          if (age >= FRAME_HOLD_MS) continue;
          image.src = url;
          if (currentUrl) URL.revokeObjectURL(currentUrl);
          currentUrl = url;
          url = undefined;
          displayedAt = receivedAt;
          clearTimeout(staleTimer);
          clearTimeout(expiryTimer);
          delete figure.dataset.paused;
          // Reconnects must not extend the displayed frame's lifetime.
          staleTimer = setTimeout(pauseImage, Math.max(0, FRAME_FRESH_MS - age));
          expiryTimer = setTimeout(
            () => clearImage("画面已过期，等待新画面…"), FRAME_HOLD_MS - age,
          );
          shown++;
          const elapsed = performance.now() - measuredAt;
          if (elapsed >= 1000) {
            fps = (shown * 1000 / elapsed).toFixed(1);
            shown = 0;
            measuredAt = performance.now();
          }
          status.textContent = `${frame.naturalWidth} × ${frame.naturalHeight} · ${fps === undefined ? "帧率测量中…" : `${fps} 帧/秒`}`;
          if (age >= FRAME_FRESH_MS) pauseImage();
          else overlay.frame(frame.naturalWidth, frame.naturalHeight);
        } catch {
          if (controller === session && !session.signal.aborted) {
            session.abort();
            pauseImage();
          }
        } finally {
          if (url) URL.revokeObjectURL(url);
        }
      }
    } finally {
      decoding = false;
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      controller?.abort();
      clearImage("预览已暂停");
    }
  });

  async function preview() {
    while (true) {
      if (!document.hidden) {
        const session = new AbortController();
        controller = session;
        shown = 0;
        measuredAt = performance.now();
        fps = undefined;
        let heartbeat;
        function receivedFrame() {
          clearTimeout(heartbeat);
          heartbeat = setTimeout(() => {
            session.abort();
            if (document.hidden) clearImage("预览已暂停");
            else pauseImage();
          }, FRAME_FRESH_MS);
        }
        receivedFrame();
        try {
          const response = await fetch(image.dataset.source, {
            cache: "no-store", signal: session.signal,
          });
          if (!response.ok) throw new Error("Camera request failed.");
          await readFrames(response, session.signal, bytes => {
            if (session.signal.aborted || document.hidden) return;
            receivedFrame();
            pendingFrame = { bytes, session, receivedAt: performance.now() };
            decodeLatest();
          });
        } catch {
          if (document.hidden) clearImage("预览已暂停");
          else pauseImage();
        } finally {
          clearTimeout(heartbeat);
          session.abort();
        }
      }
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  }

  preview();
}
