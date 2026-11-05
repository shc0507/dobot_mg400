import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const path = new URL("../src/orchestrator/web/static/console/cameras.js", import.meta.url).pathname;
const cameras = require(path);
const { logic } = cameras;

test("badge shows LIVE, STALE or OFFLINE with the frame age", () => {
  assert.deepEqual(logic.badge({ state: "live", age_s: 0.4 }), { tone: "ok", key: "badge.live", age: 0.4 });
  assert.deepEqual(logic.badge({ state: "live", age_s: 3.1 }), { tone: "warn", key: "badge.stale", age: 3.1 });
  assert.deepEqual(logic.badge({ state: "stale", age_s: 9 }), { tone: "warn", key: "badge.stale", age: 9 });
  assert.equal(logic.badge({ state: "offline", age_s: null }).key, "badge.offline");
  assert.equal(logic.badge(null).key, "badge.offline");
  // An old status poll means the console may be gone: never claim LIVE.
  assert.equal(logic.badge({ state: "live", age_s: 0.1 }, { fresh: false }).key, "badge.offline");
  assert.equal(logic.badge({ state: "live", age_s: 0.1 }, { frozen: true }).key, "badge.frozen");
});

test("thermal row ages the frame by the time since the fetch", () => {
  assert.deepEqual(logic.thermalRow({ age_s: 0.5 }, 1000, 2000, false), { state: "live", age_s: 1.5 });
  assert.deepEqual(logic.thermalRow({ age_s: 0.5 }, 1000, 4000, false), { state: "stale", age_s: 3.5 });
  assert.equal(logic.thermalRow({ age_s: 0 }, 1000, 1000, true).state, "offline");
  assert.equal(logic.thermalRow(null, 0, 0, false).state, "offline");
});

test("units, scale and palettes", () => {
  assert.equal(logic.convert(100, "F"), 212);
  assert.equal(logic.convert(0, "K"), 273.15);
  assert.ok(Math.abs(logic.toCelsius(212, "F") - 100) < 1e-9);
  assert.equal(logic.formatTemp(20, "C"), "20.0 °C");
  assert.equal(logic.formatTemp(Number.NaN, "C"), "—");
  const stats = { min: { celsius: 10 }, max: { celsius: 30 } };
  assert.deepEqual(logic.scaleRange(stats, { scale: "auto" }), [10, 30]);
  assert.deepEqual(logic.scaleRange(stats, { scale: "fixed", low: 0, high: 100 }), [0, 100]);
  assert.deepEqual(logic.scaleRange(stats, { scale: "fixed", low: 50, high: 40 }), [10, 30]);
  for (const name of ["ironbow", "rainbow", "grey"]) assert.equal(logic.paletteTable(name).length, 768);
  const grey = logic.paletteTable("grey");
  assert.deepEqual([...grey.slice(0, 3)], [0, 0, 0]);
  assert.deepEqual([...grey.slice(765)], [255, 255, 255]);
});

test("thermal pixels map the scale to the palette", () => {
  const temps = Array.from({ length: 120 }, () => Array.from({ length: 160 }, (_, x) => (x < 80 ? 10 : 30)));
  const pixels = logic.thermalPixels(temps, logic.paletteTable("grey"), 10, 30);
  assert.equal(pixels.length, 160 * 120 * 4);
  assert.deepEqual([...pixels.slice(0, 4)], [0, 0, 0, 255]);
  assert.deepEqual([...pixels.slice(159 * 4, 160 * 4)], [255, 255, 255, 255]);
});

test("CSV export uses the selected unit", () => {
  const frame = { sequence: 3, timestamp: "t", temperatures_c: [[0, 100]] };
  assert.equal(logic.thermalCsv(frame, "F"), "# sequence=3, timestamp=t, unit=°F\n32.00,212.00\n");
});

test("the pixel probe reads 1 sensor pixel inside the frame", () => {
  const frame = { temperatures_c: Array.from({ length: 120 }, (_, y) => Array.from({ length: 160 }, (_, x) => x + y / 100)) };
  assert.deepEqual(logic.probeReading(frame, { x: 10, y: 5 }), { x: 10, y: 5, celsius: 10.05 });
  assert.deepEqual(logic.probeReading(frame, { x: 159, y: 119, pinned: true }), { x: 159, y: 119, celsius: 160.19 });
  for (const probe of [null, { x: 160, y: 0 }, { x: -1, y: 0 }, { x: 0, y: 120 }, { x: 1.5, y: 0 }]) assert.equal(logic.probeReading(frame, probe), null);
  assert.equal(logic.probeReading(null, { x: 1, y: 1 }), null);
  assert.equal(logic.translate("en", "probe", { x: 10, y: 5, value: "25.0 °C" }), "Pixel 10, 5: 25.0 °C");
});

test("region drag gives bounded sensor bounds", () => {
  assert.deepEqual(logic.regionFrom({ x: 50.4, y: 70 }, { x: 10, y: 20.6 }), { x0: 10, y0: 21, x1: 50, y1: 70 });
  assert.deepEqual(logic.regionFrom({ x: -5, y: -5 }, { x: 500, y: 500 }), { x0: 0, y0: 0, x1: 160, y1: 120 });
  assert.equal(logic.regionFrom({ x: 5, y: 5 }, { x: 6, y: 6 }), null);
});

test("overlay geometry, retry delay and translations", () => {
  assert.deepEqual(logic.containRect(640, 480, 320, 320), { x: 0, y: 40, scale: 0.5 });
  assert.equal(logic.backoff(0), 1000);
  assert.equal(logic.backoff(10), 15000);
  assert.equal(logic.stamp(new Date("2026-01-02T03:04:05.678Z")), "20260102T030405Z");
  assert.equal(logic.translate("zh", "badge.live"), "实时");
  assert.equal(logic.translate("en", "age", { age: "1.0" }), "1.0 s");
  for (const key of Object.keys(cameras.i18n.en)) assert.ok(key in cameras.i18n.zh, key);
  assert.deepEqual(cameras.VIEWS, ["camera1", "camera2", "camera3", "camera4", "thermal", "microscope"]);
  assert.throws(() => cameras.tile("camera9"), /Unknown camera view/);
});

test("the workspace never parses data as HTML and never stores a token", () => {
  const source = readFileSync(path, "utf8");
  assert.ok(!/innerHTML|outerHTML|insertAdjacentHTML/.test(source));
  assert.ok(!/localStorage|sessionStorage/.test(source));
  assert.ok(!/Authorization/.test(source));
});

test("cameras load from the proxy and the microscope from the console relay", () => {
  assert.equal(logic.streamUrl("camera2", false, "/lab"), "/camera/camera2.mjpeg");
  assert.equal(logic.snapshotUrl("camera4", "/lab"), "/camera/camera4.jpg");
  assert.equal(logic.streamUrl("microscope", false, "/lab"), "/lab/api/console/ws/cameras/stream/microscope?fps=10");
  assert.equal(logic.streamUrl("microscope", true, "/lab"), "/lab/api/console/ws/cameras/stream/microscope?fps=30");
  assert.equal(logic.snapshotUrl("microscope", "/lab"), "/lab/api/console/ws/cameras/snapshot/microscope");
});

test("the thermal panel relays connect and shutter and links no service page", () => {
  const source = readFileSync(path, "utf8");
  assert.match(source, /send\("connect", \{\}\)/);
  assert.match(source, /send\("shutter", \{\}\)/);
  assert.equal(logic.translate("en", "connect"), "Connect camera");
  assert.ok(!/side\.page|target: "_blank"/.test(source));
});
