"""Run the stage page against a fake DOM and fetch, without a browser or socket."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "src/stage_controller/static/app.js"

HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const flush = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor() {
    this.value = '50'; this.min = '1'; this.max = '1000';
    this.dataset = {}; this.disabled = false; this.hidden = false;
    this.textContent = ''; this.options = []; this.listeners = new Map();
    this.attrs = {}; this.captured = new Set();
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name), remove: name => classes.delete(name),
      toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
    };
    this.style = {setProperty() {}};
  }
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(name, callback) {
    this.listeners.set(name, [...(this.listeners.get(name) || []), callback]);
  }
  emit(name, details = {}) {
    const event = {target: this, button: 0, preventDefault() {}, ...details};
    return Promise.all((this.listeners.get(name) || []).map(callback => callback(event)));
  }
  checkValidity() { return true; }
  reportValidity() { return true; }
  closest() { return null; }
  setPointerCapture(id) { this.captured.add(id); }
  hasPointerCapture(id) { return this.captured.has(id); }
  releasePointerCapture(id) { this.captured.delete(id); }
  getBoundingClientRect() { return {left: 0, top: 0, right: 100, bottom: 100}; }
  replaceChildren(...children) { this.options = children; }
  add(option) { this.options.push(option); }
  append() {}
}

function snapshot(holder = null, sampled_at = 1) {
  return {
    sampled_at, connected: true, port: 'DEMO', demo: true, error: null,
    lease: {holder, remaining_s: holder === null ? 0 : 3},
    microstep: 1, microstep_modes: [{value: 1, label: 'Full step'}, {value: 2, label: 'Half step'}],
    motors: [{id: 'bottom', available: true, mode: 'idle', cooldown: 0,
      direction: 'cw', position: 0, remaining: 0, speed: 50}],
    events: [], defaults: {speed: 50, steps: 50},
  };
}

async function browser() {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, new Element());
    return nodes.get(selector);
  };
  node('meta[name="control-token"]').content = 'page-control-token';
  node('#serial-port').value = 'DEMO';
  const card = node('#motor-bottom');
  card.dataset.motor = 'bottom';
  const directions = ['cw', 'ccw'].map(direction => {
    const button = new Element(); button.dataset.direction = direction; return button;
  });
  const heldButtons = ['cw', 'ccw'].map(direction => {
    const button = new Element(); button.dataset.holdDirection = direction; return button;
  });
  const key = new Element();
  key.dataset = {key: 'w', keyMotor: 'bottom', holdDirection: 'ccw'};
  const fields = new Map();
  card.querySelector = selector => {
    if (!fields.has(selector)) fields.set(selector, new Element());
    return fields.get(selector);
  };
  card.querySelectorAll = selector => selector === '[data-direction]' ? directions
    : selector === '[data-hold-direction]' ? heldButtons : [];
  const document = new Element();
  document.documentElement = {dataset: {theme: 'light'}};
  document.querySelector = node;
  document.querySelectorAll = selector => selector === '[data-motor]' ? [card]
    : selector === '[data-key]' ? [key] : [];
  document.createElement = () => new Element();
  const window = new Element(), requests = [], deferred = [], timers = new Map();
  let latest = snapshot(), deferHold = false, timerId = 0;
  const context = vm.createContext({
    document, window, Element, console,
    Option: class { constructor(text, value) { this.textContent = text; this.value = value; } },
    sessionStorage: {getItem() {}, setItem() {}, removeItem() {}},
    localStorage: {setItem() {}},
    AbortSignal: {timeout() {}},
    crypto: {getRandomValues: values => values.fill(42)},
    setTimeout(callback, delay) {
      const id = ++timerId; timers.set(id, {callback, delay}); return id;
    },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      const request = {url, ...options}; requests.push(request);
      const reply = value => ({ok: true, status: 200, json: async () => value});
      if (url.endsWith('/hold') && deferHold) {
        return new Promise(resolve => deferred.push(value => resolve(reply(value))));
      }
      return Promise.resolve(reply(url === '/api/ports' ? {ports: [{device: 'DEMO'}]} : latest));
    },
  });
  vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
  await flush();
  return {
    node, card, directions, heldButtons, key, window, requests, timers, deferred,
    render(value) { latest = value; context.render(value); },
    deferHold() { deferHold = true; },
    posts() { return requests.filter(request => request.method === 'POST'); },
  };
}

const scenarios = {
  async fault_blocks_motion_until_explicit_recovery(page) {
    await page.node('#keyboard-toggle').emit('click');
    const fault = {code: 'stop_unconfirmed', message: 'Check the stage before reconnecting.'};
    page.render({...snapshot(null, 2), locked: true, fault});
    assert.equal(page.node('#automation-banner').hidden, true);
    assert.equal(page.node('#notice').textContent, fault.message);
    for (const button of [...page.directions, ...page.heldButtons, page.key]) {
      assert.equal(button.disabled, true);
    }
    assert.equal(page.node('#microstep-mode').disabled, true);
    assert.equal(page.node('#stop-all').disabled, false);
    await page.directions[0].emit('click');
    await page.window.emit('keydown', {key: 'w'});
    assert.equal(page.posts().length, 0);
    await page.node('#stop-all').emit('click');
    assert.deepEqual(page.posts().map(item => item.url), ['/api/stop']);
    page.render({...snapshot(null, 3), locked: true, fault});
    assert.equal(page.directions[0].disabled, true);
    page.render({...snapshot(null, 4), locked: false, fault: null});
    assert.equal(page.directions[0].disabled, false);
  },
  async automation_blocks_motion(page) {
    await page.node('#keyboard-toggle').emit('click');
    assert.equal(page.directions[0].disabled, false);
    assert.equal(page.heldButtons[0].disabled, false);
    assert.equal(page.key.disabled, false);
    page.render(snapshot('orchestrator', 2));
    assert.equal(page.node('#automation-banner').hidden, false);
    for (const button of [...page.directions, ...page.heldButtons, page.key]) {
      assert.equal(button.disabled, true);
    }
    assert.equal(page.node('#microstep-mode').disabled, true);
    assert.equal(page.node('#apply-microstep').disabled, true);
    assert.equal(page.node('#stop-all').disabled, false);
    await page.directions[0].emit('click');
    await page.heldButtons[0].emit('pointerdown', {pointerId: 7});
    await page.key.emit('keydown', {key: 'Enter'});
    await page.window.emit('keydown', {key: 'w'});
    page.node('#microstep-mode').value = '2';
    await page.node('#apply-microstep').emit('click');
    assert.equal(page.posts().length, 0);
  },
  async release_does_not_restart_motion(page) {
    await page.node('#keyboard-toggle').emit('click');
    page.render(snapshot('orchestrator', 2));
    page.render(snapshot(null, 3));
    assert.equal(page.node('#automation-banner').hidden, true);
    assert.equal(page.directions[0].disabled, false);
    assert.equal(page.key.disabled, false);
    await page.window.emit('keydown', {key: 'w', repeat: true});
    assert.equal(page.posts().length, 0);
    await page.directions[0].emit('click');
    assert.deepEqual(page.posts().map(item => item.url), ['/api/motors/bottom/move']);
  },
  async takeover_cancels_pending_hold_once(page) {
    page.deferHold();
    await page.heldButtons[0].emit('pointerdown', {pointerId: 7});
    assert.equal(page.deferred.length, 1);
    page.render(snapshot('orchestrator', 2));
    await flush();
    assert.equal(page.heldButtons[0].hasPointerCapture(7), false);
    page.deferred[0](snapshot(null, 1));
    await flush();
    await page.heldButtons[0].emit('pointerup', {pointerId: 7});
    page.render(snapshot('orchestrator', 3));
    await flush();
    assert.deepEqual(page.posts().map(item => item.url), [
      '/api/motors/bottom/hold', '/api/motors/bottom/hold/stop',
    ]);
    assert.equal(page.node('#automation-banner').hidden, false);
    assert.equal([...page.timers.values()].some(timer => timer.delay === 200), false);
  },
  async stop_remains_available(page) {
    page.render(snapshot('orchestrator', 2));
    await page.node('#stop-all').emit('click');
    assert.deepEqual(page.posts().map(item => item.url), ['/api/stop']);
    assert.equal(page.posts()[0].headers['X-Control-Token'], 'page-control-token');
    assert.equal(page.node('#stop-all').disabled, false);
    assert.equal(page.directions[0].disabled, true);
  },
  async old_reply_cannot_restore_manual_control(page) {
    page.render(snapshot('orchestrator', 3));
    page.render(snapshot(null, 2));
    assert.equal(page.node('#automation-banner').hidden, false);
    assert.equal(page.directions[0].disabled, true);
    assert.equal(page.heldButtons[0].disabled, true);
  },
};

(async () => {
  await scenarios[process.argv[2]](await browser());
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize(
    "scenario",
    [
        "fault_blocks_motion_until_explicit_recovery",
        "automation_blocks_motion",
        "release_does_not_restart_motion",
        "takeover_cancels_pending_hold_once",
        "stop_remains_available",
        "old_reply_cannot_restore_manual_control",
    ],
)
def test_stage_page_automation_controls(scenario):
    result = subprocess.run(
        ["node", "-e", HARNESS, str(SCRIPT), scenario],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_automation_banner_starts_hidden_and_announces_control_owner():
    template = SCRIPT.parents[1] / "templates/index.html"
    assert (
        '<div id="automation-banner" class="demo-banner" role="status" hidden>'
        "Stage is controlled by automation</div>"
    ) in template.read_text()
