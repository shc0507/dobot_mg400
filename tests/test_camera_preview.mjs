import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const script = readFileSync(new URL("../scripts/camera_preview.js", import.meta.url), "utf8");
const encoder = new TextEncoder();
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const drain = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const jpeg = label => new Uint8Array([255, 216, ...encoder.encode(label), 255, 217]);
function part(bytes, length = bytes.length) {
  const head = encoder.encode(`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${length}\r\n\r\n`);
  const result = new Uint8Array(head.length + bytes.length + 2);
  result.set(head);
  result.set(bytes, head.length);
  result.set([13, 10], head.length + bytes.length);
  return result;
}

function browser(count = 1) {
  let now = 0, timerId = 0, objectId = 0;
  const timers = new Map(), requests = [], decodes = [], revoked = [], listeners = [];
  const blobs = new Map();
  const images = Array.from({ length: count }, (_, index) => {
    const status = { textContent: "" };
    const figure = { dataset: {}, querySelector: () => status };
    return {
      dataset: { source: `camera${index + 1}.mjpeg` },
      status, figure, shown: [],
      set src(value) { this.value = value; this.shown.push(value); },
      get src() { return this.value; },
      removeAttribute() { this.value = undefined; },
      closest() { return figure; },
    };
  });
  const document = {
    hidden: false,
    querySelectorAll: () => images,
    addEventListener: (_event, listener) => listeners.push(listener),
  };
  vm.runInNewContext(script, {
    document, AbortController, Uint8Array, TextDecoder, Blob,
    performance: { now: () => now },
    setTimeout: (callback, delay) => {
      const id = ++timerId;
      timers.set(id, { at: now + delay, callback });
      return id;
    },
    clearTimeout: id => timers.delete(id),
    URL: {
      createObjectURL: blob => {
        const url = `blob:frame-${++objectId}`;
        blobs.set(url, blob);
        return url;
      },
      revokeObjectURL: url => revoked.push(url),
    },
    Image: class {
      naturalWidth = 1280;
      naturalHeight = 720;
      decode() {
        const pending = deferred();
        decodes.push({ ...pending, url: this.src });
        return pending.promise;
      }
    },
    fetch: (source, options) => {
      const pending = deferred();
      const request = { ...pending, source, options };
      options.signal.addEventListener("abort", () => {
        pending.reject(new Error("Aborted"));
        try { request.stream?.error(new Error("Aborted")); } catch { /* Already closed. */ }
      });
      requests.push(request);
      return pending.promise;
    },
  });
  return {
    images, requests, decodes, revoked, blobs,
    async advance(milliseconds) {
      const end = now + milliseconds;
      await drain();
      while (true) {
        const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > end) break;
        now = next[1].at;
        timers.delete(next[0]);
        next[1].callback();
        await drain();
      }
      now = end;
      await drain();
    },
    async respond(index, type = "multipart/x-mixed-replace; boundary=frame") {
      const request = requests[index];
      const body = new ReadableStream({ start(controller) { request.stream = controller; } });
      request.resolve({ ok: true, headers: { get: () => type }, body });
      await drain();
    },
    async push(index, bytes) {
      requests[index].stream.enqueue(bytes);
      await drain();
    },
    async finish(index) {
      requests[index].stream.close();
      await drain();
    },
    async visible(value) {
      document.hidden = !value;
      listeners.forEach(listener => listener());
      await drain();
    },
  };
}

async function showing(page, wait = 1000, label = "visible") {
  await page.respond(0);
  await page.push(0, part(jpeg(label)));
  page.decodes[0].resolve();
  await page.advance(wait);
}

test("three cameras use one persistent authenticated relative request each", async () => {
  const page = browser(3);
  assert.equal(page.requests.length, 3);
  assert.deepEqual(page.requests.map(request => request.source), ["camera1.mjpeg", "camera2.mjpeg", "camera3.mjpeg"]);
  assert.ok(page.requests.every(request => request.options.cache === "no-store"));
  for (let i = 0; i < 3; i++) {
    await page.respond(i);
    await page.push(i, part(jpeg(`camera-${i}`)));
  }
  page.decodes.forEach(decode => decode.resolve());
  await page.advance(0);
  assert.ok(page.images.every(image => image.shown.length === 1));
  assert.equal(page.requests.length, 3);
});

test("multipart headers, bodies and separators can split at every byte", async () => {
  const page = browser();
  await page.respond(0);
  const frame = part(jpeg("chunked"));
  for (const byte of frame.subarray(0, -1)) await page.push(0, new Uint8Array([byte]));
  assert.equal(page.decodes.length, 0);
  await page.push(0, frame.subarray(-1));
  assert.equal(page.decodes.length, 1);
  assert.deepEqual(new Uint8Array(await page.blobs.get(page.decodes[0].url).arrayBuffer()), jpeg("chunked"));
  page.decodes[0].resolve();
  await page.advance(0);
  assert.match(page.images[0].status.textContent, /1280 × 720/);
});

test("slow decoding keeps only the newest waiting frame instead of a decode queue", async () => {
  const page = browser();
  await page.respond(0);
  for (let i = 1; i <= 50; i++) await page.push(0, part(jpeg(String(i))));
  assert.equal(page.decodes.length, 1);
  assert.equal(page.blobs.size, 1);
  page.decodes[0].resolve();
  await page.advance(0);
  assert.equal(page.decodes.length, 2);
  assert.deepEqual(new Uint8Array(await page.blobs.get(page.decodes[1].url).arrayBuffer()), jpeg("50"));
  page.decodes[1].resolve();
  await page.advance(0);
  assert.equal(page.images[0].shown.length, 2);
  assert.deepEqual(page.revoked, [page.decodes[0].url]);
});

test("complete-frame heartbeats keep a long request alive and display measured update rate", async () => {
  const page = browser();
  await page.respond(0);
  for (let i = 0; i < 75; i++) {
    await page.advance(34);
    await page.push(0, part(jpeg(String(i))));
    page.decodes.at(-1).resolve();
    await page.advance(0);
  }
  assert.equal(page.requests.length, 1);
  assert.equal(page.requests[0].options.signal.aborted, false);
  assert.equal(page.images[0].shown.length, 75);
  assert.match(page.images[0].status.textContent, /1280 × 720 · 29\.4 帧\/秒/);
});

test("incomplete bytes do not reset heartbeat and timeout retains the last frame for five seconds", async () => {
  const page = browser();
  await showing(page);
  await page.push(0, part(jpeg("partial")).subarray(0, 30));
  await page.advance(499);
  assert.equal(page.requests[0].options.signal.aborted, false);
  await page.advance(1);
  assert.equal(page.requests[0].options.signal.aborted, true);
  assert.equal(page.images[0].src, page.decodes[0].url);
  assert.equal(page.images[0].figure.dataset.paused, "true");
  assert.match(page.images[0].status.textContent, /暂停/);
  assert.equal(page.revoked.includes(page.decodes[0].url), false);
  await page.advance(500);
  assert.equal(page.requests.length, 2);
  await page.advance(2999);
  assert.equal(page.images[0].src, page.decodes[0].url);
  await page.advance(1);
  assert.equal(page.images[0].src, undefined);
  assert.equal(page.images[0].figure.dataset.paused, undefined);
  assert.deepEqual(page.revoked, [page.decodes[0].url]);
});

test("repeated disconnects do not extend the last frame retention deadline", async () => {
  const page = browser();
  await showing(page);
  await page.finish(0);
  assert.equal(page.images[0].src, page.decodes[0].url);
  assert.equal(page.images[0].figure.dataset.paused, "true");
  assert.match(page.images[0].status.textContent, /暂停/);
  assert.equal(page.requests[0].options.signal.aborted, true);
  await page.advance(500);
  assert.equal(page.requests.length, 2);
  await page.respond(1);
  await page.advance(500);
  await page.finish(1);
  await page.advance(500);
  assert.equal(page.requests.length, 3);
  page.requests[2].reject(new Error("Connection unavailable"));
  await page.advance(2499);
  assert.equal(page.images[0].src, page.decodes[0].url);
  assert.equal(page.revoked.includes(page.decodes[0].url), false);
  await page.advance(1);
  assert.equal(page.images[0].src, undefined);
  assert.deepEqual(page.revoked, [page.decodes[0].url]);
});

test("decode failure retains the previous frame but revokes the failed frame", async () => {
  const page = browser();
  await showing(page);
  await page.push(0, part(jpeg("invalid")));
  page.decodes[1].reject(new Error("Cannot decode JPEG"));
  await page.advance(0);
  assert.equal(page.requests[0].options.signal.aborted, true);
  assert.equal(page.images[0].src, page.decodes[0].url);
  assert.equal(page.images[0].figure.dataset.paused, "true");
  assert.match(page.images[0].status.textContent, /暂停/);
  assert.deepEqual(page.revoked, [page.decodes[1].url]);
  await page.advance(3999);
  assert.equal(page.images[0].src, page.decodes[0].url);
  await page.advance(1);
  assert.equal(page.images[0].src, undefined);
  assert.deepEqual(page.revoked, [page.decodes[1].url, page.decodes[0].url]);
});

test("a fresh frame restores live status and sets expiry from arrival rather than decode completion", async () => {
  const page = browser();
  await showing(page, 1000, "first");
  await page.finish(0);
  await page.advance(500);
  await page.respond(1);
  await page.advance(500);
  await page.push(1, part(jpeg("fresh")));
  await page.advance(200);
  page.decodes[1].resolve();
  await page.advance(0);
  assert.equal(page.images[0].src, page.decodes[1].url);
  assert.equal(page.images[0].figure.dataset.paused, undefined);
  assert.match(page.images[0].status.textContent, /1280 × 720/);
  assert.doesNotMatch(page.images[0].status.textContent, /暂停/);
  assert.deepEqual(page.revoked, [page.decodes[0].url]);
  await page.advance(4799);
  assert.equal(page.images[0].src, page.decodes[1].url);
  await page.advance(1);
  assert.equal(page.images[0].src, undefined);
  assert.deepEqual(page.revoked, [page.decodes[0].url, page.decodes[1].url]);
});

test("complete frames cannot hide a stalled decoder or extend the displayed frame lifetime", async () => {
  const page = browser();
  await showing(page);
  await page.push(0, part(jpeg("slow")));
  await page.advance(499);
  assert.equal(page.images[0].figure.dataset.paused, undefined);
  await page.advance(1);
  assert.equal(page.images[0].figure.dataset.paused, "true");
  assert.match(page.images[0].status.textContent, /暂停/);
  await page.advance(500);
  for (let i = 2; i <= 4; i++) {
    await page.push(0, part(jpeg(String(i))));
    await page.advance(i === 4 ? 999 : 1000);
  }
  assert.equal(page.images[0].src, page.decodes[0].url);
  assert.equal(page.requests[0].options.signal.aborted, false);
  await page.advance(1);
  assert.equal(page.images[0].src, undefined);
  assert.deepEqual(page.revoked, [page.decodes[0].url]);
  await page.push(0, part(jpeg("5")));
  await page.advance(1000);
  await page.push(0, part(jpeg("fresh")));
  page.decodes[1].resolve();
  await page.advance(0);
  assert.deepEqual(page.images[0].shown, [page.decodes[0].url]);
  assert.ok(page.revoked.includes(page.decodes[1].url));
  assert.equal(page.decodes.length, 3);
  page.decodes[2].resolve();
  await page.advance(0);
  assert.equal(page.images[0].src, page.decodes[2].url);
  assert.equal(page.images[0].figure.dataset.paused, undefined);
  assert.equal(page.requests.length, 1);
});

test("a decode from a disconnected session cannot replace the retained frame", async () => {
  const page = browser();
  await showing(page, 500);
  await page.push(0, part(jpeg("old session")));
  await page.finish(0);
  await page.advance(500);
  await page.respond(1);
  await page.push(1, part(jpeg("new session")));
  page.decodes[1].resolve();
  await page.advance(0);
  assert.deepEqual(page.images[0].shown, [page.decodes[0].url]);
  assert.ok(page.revoked.includes(page.decodes[1].url));
  page.decodes[2].resolve();
  await page.advance(0);
  assert.equal(page.images[0].src, page.decodes[2].url);
  assert.ok(page.revoked.includes(page.decodes[0].url));
});

test("hidden pages abort streams, clear frames, and never display an old pending decode", async () => {
  const page = browser();
  await showing(page, 0);
  const first = page.images[0].src;
  await page.push(0, part(jpeg("pending")));
  await page.visible(false);
  assert.equal(page.requests[0].options.signal.aborted, true);
  assert.equal(page.images[0].src, undefined);
  assert.equal(page.images[0].status.textContent, "预览已暂停");
  await page.advance(1000);
  assert.equal(page.requests.length, 1);
  await page.visible(true);
  await page.advance(500);
  assert.equal(page.requests.length, 2);
  page.decodes[1].resolve();
  await page.advance(0);
  assert.deepEqual(page.images[0].shown, [first]);
  assert.ok(page.revoked.includes(page.decodes[1].url));
  await page.respond(1);
  await page.push(1, part(jpeg("new session")));
  page.decodes[2].resolve();
  await page.advance(0);
  assert.equal(page.images[0].shown.length, 2);
});

for (const [name, bytes] of [
  ["empty JPEG", part(new Uint8Array())],
  ["missing JPEG end", part(new Uint8Array([255, 216, 0, 0]))],
  ["wrong length", part(jpeg("test"), 5)],
  ["oversized declared frame", part(jpeg("test"), 4 * 1024 * 1024 + 1)],
  ["truncated JPEG", part(jpeg("truncated")).subarray(0, -4)],
]) {
  test(`${name} is never decoded`, async () => {
    const page = browser();
    await page.respond(0);
    await page.push(0, bytes);
    if (!page.requests[0].options.signal.aborted) await page.finish(0);
    assert.equal(page.decodes.length, 0);
    assert.equal(page.images[0].src, undefined);
    assert.equal(page.requests[0].options.signal.aborted, true);
  });
}

test("a complete chunk cannot bypass the multipart header size limit", async () => {
  const page = browser();
  await page.respond(0);
  const head = encoder.encode(`--frame\r\nX-Long: ${"x".repeat(8192)}\r\nContent-Type: image/jpeg\r\nContent-Length: 6\r\n\r\n`);
  const bytes = new Uint8Array(head.length + 8);
  bytes.set(head);
  bytes.set(jpeg("ok"), head.length);
  bytes.set([13, 10], head.length + 6);
  await page.push(0, bytes);
  assert.equal(page.decodes.length, 0);
  assert.equal(page.requests[0].options.signal.aborted, true);
});
