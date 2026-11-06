import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(import.meta.url);
const shellPath = new URL("../src/orchestrator/web/static/console/shell.js", import.meta.url).pathname;
const path = new URL("../src/orchestrator/web/static/console/overview.js", import.meta.url).pathname;

class Element {
  constructor(tag) {
    Object.assign(this, { tagName: tag.toUpperCase(), children: [], attributes: {}, listeners: new Map(), hidden: false, disabled: false,
      className: "", ownText: "", value: "", scrollTop: 0 });
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(""); }
  set innerHTML(_value) { throw new Error("The workspace must not parse data as HTML."); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.ownText = ""; this.children = [...children]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  addEventListener(event, callback) { this.listeners.set(event, callback); }
  click() { if (!this.disabled) this.listeners.get("click")?.({ preventDefault() {} }); }
  all() { return [this, ...this.children.flatMap(child => child.all())]; }
  button(text) { return this.all().find(node => node.tagName === "BUTTON" && node.textContent.startsWith(text)); }
}

function mount(snapshot) {
  delete require.cache[path];
  const shell = require(shellPath);
  let registered = null;
  globalThis.window = { LabConsole: { logic: shell.logic, SERVICES: shell.SERVICES, register: item => { registered = item; } } };
  globalThis.setInterval = () => 1;
  require(path);
  const calls = [];
  const listeners = { state: [], language: [] };
  const tables = { en: { ...shell.strings.en, ...registered.i18n.en } };
  const api = {
    t: (key, vars) => shell.logic.translate(tables, "en", key, vars),
    state: () => snapshot.value,
    onState: fn => listeners.state.push(fn),
    onLanguage: fn => listeners.language.push(fn),
    deviceName: id => id[0].toUpperCase() + id.slice(1),
    safetyStop: async action => { calls.push(["stop", action]); return {}; },
    take: async device => { calls.push(["take", device]); },
    release: async device => { calls.push(["release", device]); },
    releaseAll: async () => { calls.push(["release-all"]); },
    consoleRequest: async () => ({ events: [{ service: "robot", level: "warning", message: "Hot", ts_unix: 1 }], errors: {} }),
    notify: () => {},
    el(tag, props, ...children) {
      const node = new Element(tag);
      for (const [key, value] of Object.entries(props || {})) {
        if (value === undefined || value === null || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
        else if (key === "hidden" || key === "disabled") node[key] = Boolean(value);
        else node.setAttribute(key, String(value));
      }
      for (const child of children.flat()) {
        if (child === null || child === undefined || child === false) continue;
        node.append(typeof child === "string" ? Object.assign(new Element("#text"), { ownText: child }) : child);
      }
      return node;
    },
  };
  const root = new Element("section");
  registered.mount(root, api);
  return { root, calls, refresh: () => listeners.state.forEach(fn => fn()), setLanguage: () => listeners.language.forEach(fn => fn()) };
}

test("the Overview updates in place, so a state update keeps every button", async () => {
  const snapshot = { value: { services: [{ id: "robot", phase: "ready" }], control: [], approvals: [] } };
  const page = mount(snapshot);
  const stop = page.root.button("STOP ALL MOTION");
  const take = page.root.all().find(node => node.getAttribute("aria-label") === "Take control of Robot");
  assert.equal(take.disabled, false);
  snapshot.value = { services: [{ id: "robot", phase: "ready" }], control: [{ device: "robot", mine: true, state: "held" }], approvals: [{ decision: "pending" }] };
  page.refresh();
  assert.equal(page.root.button("STOP ALL MOTION"), stop);
  assert.equal(take.textContent, "Release");
  assert.equal(take.getAttribute("aria-label"), "Release Robot");
  assert.match(page.root.textContent, /1 waiting\./);
  // A device that is not configured says so and offers no button.
  const stageRow = page.root.all().find(node => node.className === "lc-row" && node.textContent.startsWith("Stage"));
  assert.match(stageRow.textContent, /Not configured/);
  assert.equal(stageRow.children[2].hidden, true);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(page.root.textContent, /Robotwarning.*Hot/);
});

test("each Safety button acts on 1 press and is never disabled", () => {
  const page = mount({ value: { services: [], control: [] } });
  for (const text of ["STOP ALL MOTION", "Stop heat and gas", "Stop all gas"]) {
    const button = page.root.button(text);
    button.click();
    button.click();
    assert.equal(button.disabled, false);
  }
  assert.deepEqual(page.calls, [["stop", "motion"], ["stop", "motion"], ["stop", "heat"], ["stop", "heat"], ["stop", "gas"], ["stop", "gas"]]);
});

test("the event filters update in place", () => {
  const page = mount({ value: { services: [], control: [] } });
  const select = page.root.all().find(node => node.tagName === "SELECT");
  select.listeners.get("change")({ target: { value: "stage" } });
  assert.equal(select.value, "stage");
  assert.equal(page.root.button("All").getAttribute("aria-pressed"), "false");
  page.root.button("Warnings").click();
  assert.equal(select.value, "");
  assert.equal(page.root.button("Warnings").getAttribute("aria-pressed"), "true");
});
