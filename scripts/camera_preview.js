const MAX_FRAME = 4 * 1024 * 1024;
const FRAME_FRESH_MS = 1500;
const FRAME_HOLD_MS = 5000;
const HEADER_END = new Uint8Array([13, 10, 13, 10]);

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

for (const image of document.querySelectorAll("img[data-source]")) {
  const figure = image.closest("figure");
  const status = figure.querySelector("small");
  let controller, currentUrl, pendingFrame, decoding = false;
  let shown = 0, measuredAt = 0, fps;
  let displayedAt, staleTimer, expiryTimer;

  function clearImage(message) {
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
