"""Exercise homing controls with a fake DOM, fetch and clocks. No sockets or devices."""

import subprocess

import pytest
from test_stage_automation_ui import HARNESS as AUTOMATION_HARNESS
from test_stage_automation_ui import SCRIPT

HARNESS = (
    AUTOMATION_HARNESS.split("function snapshot(", 1)[0]
    + r"""
function snapshot(sampled_at = 1) {
  return {
    sampled_at, connected: true, port: 'DEMO', demo: true, error: null,
    lease: {holder: null}, locked: false, fault: null,
    microstep: 1, microstep_modes: [{value: 1, label: 'Full step'}],
    automation: {configured: true, error: null, active: null, poses: ['furnace', 'image']},
    motors: ['bottom', 'top', 'insert'].map(id => ({
      id, available: true, mode: 'idle', cooldown: 0, direction: 'cw',
      position: 0, remaining: 0, speed: 50, position_mm: null,
      homed: false, limits: '00', soft: false, awake: false,
      homing_available: true, automation_ready: false,
      named_positions: id === 'insert' ? ['load', 'furnace'] : [],
    })), events: [], defaults: {speed: 50, steps: 50},
  };
}
function homed(sampled_at = 2) {
  const next = snapshot(sampled_at);
  next.motors.forEach(motor => Object.assign(motor, {
    homed: true, soft: true, automation_ready: true, position_mm: 0,
  }));
  return next;
}
async function browser() {
  const nodes = new Map();
  const node = selector => {
    if (!nodes.has(selector)) nodes.set(selector, new Element());
    return nodes.get(selector);
  };
  node('meta[name="control-token"]').content = 'page-control-token';
  node('#serial-port').value = 'DEMO';
  const cards = ['bottom', 'top', 'insert'].map(id => {
    const card = node(`#motor-${id}`);
    card.dataset.motor = id;
    const fields = new Map();
    card.querySelector = selector => {
      if (!fields.has(selector)) fields.set(selector, new Element());
      return fields.get(selector);
    };
    card.directions = ['cw', 'ccw'].map(direction => {
      const button = new Element(); button.dataset.direction = direction; return button;
    });
    card.holds = ['cw', 'ccw'].map(direction => {
      const button = new Element(); button.dataset.holdDirection = direction; return button;
    });
    card.querySelectorAll = selector => selector === '[data-direction]' ? card.directions
      : selector === '[data-hold-direction]' ? card.holds : [];
    return card;
  });
  const document = new Element();
  document.documentElement = {dataset: {theme: 'light'}};
  document.querySelector = node;
  document.querySelectorAll = selector => selector === '[data-motor]' ? cards : [];
  document.createElement = () => new Element();
  const window = new Element(), requests = [], deferred = [], timers = new Map();
  let latest = snapshot(), deferPath = null, failPath = null, timerId = 0;
  const context = vm.createContext({
    document, window, Element, console,
    Option: class { constructor(text, value) { this.textContent = text; this.value = value; } },
    sessionStorage: {getItem() {}, setItem() {}, removeItem() {}},
    localStorage: {setItem() {}}, AbortSignal: {timeout() {}},
    crypto: {getRandomValues: values => values.fill(42)},
    setTimeout(callback, delay) {
      const id = ++timerId; timers.set(id, {callback, delay}); return id;
    }, clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      requests.push({url, ...options});
      const reply = value => ({ok: true, status: 200, json: async () => value});
      if (url === failPath) return Promise.reject(new Error('Lost reply'));
      if (url === deferPath) {
        return new Promise(resolve => deferred.push(value => resolve(reply(value))));
      }
      return Promise.resolve(reply(url === '/api/ports' ? {ports: [{device: 'DEMO'}]} : latest));
    },
  });
  vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
  await flush();
  return {
    node, cards, requests, deferred, timers,
    render(value) { latest = value; context.render(value); },
    defer(path) { deferPath = path; }, fail(path) { failPath = path; },
    posts() { return requests.filter(request => request.method === 'POST'); },
    home(id) { return node(`#motor-${id}`).querySelector('.home-button'); },
    positions() { return node('#motor-insert').querySelector('.named-positions').options; },
    poses() { return node('#pose-buttons').options; },
  };
}
const scenarios = {
  async legacy_firmware_keeps_manual_controls(page) {
    const next = snapshot(2);
    delete next.automation;
    next.motors.forEach(motor => {
      const fields = ['homed', 'soft', 'awake', 'homing_available',
        'automation_ready', 'named_positions'];
      for (const field of fields) {
        delete motor[field];
      }
    });
    page.render(next);
    for (const card of page.cards) {
      assert.equal(card.querySelector('.homing-status').textContent, 'Homing unavailable');
      assert.equal(page.home(card.dataset.motor).disabled, true);
      assert.equal(card.directions[0].disabled, false);
      assert.equal(card.holds[0].disabled, false);
    }
    assert.equal(page.poses().length, 0);
    await page.home('bottom').emit('click');
    await page.cards[0].directions[0].emit('click');
    assert.deepEqual(page.posts().map(item => item.url), ['/api/motors/bottom/move']);
  },
  async home_is_explicit_and_waits_for_confirmed_reference(page) {
    assert.equal(page.home('insert').disabled, false);
    assert.equal(page.positions()[0].disabled, true);
    assert.equal(page.poses()[0].disabled, true);
    await page.home('insert').emit('click');
    assert.deepEqual(page.posts().map(item => item.url), ['/api/motors/insert/home']);
    assert.equal(page.posts()[0].headers['X-Control-Token'], 'page-control-token');
    const pending = homed(2);
    pending.motors[2].automation_ready = false;
    page.render(pending);
    assert.equal(page.positions()[0].disabled, true);
    assert.equal(page.cards[2].querySelector('.homing-status').textContent, 'Home required');
    page.render(homed(3));
    assert.equal(page.cards[2].querySelector('.homing-status').textContent, 'Homed');
    assert.equal(page.cards[2].querySelector('.position-mm').textContent, '0 mm from home');
    assert.equal(page.positions()[0].disabled, false);
    assert.equal(page.poses()[0].disabled, false);
  },
  async named_targets_use_only_the_selected_route(page) {
    page.render(homed());
    await page.positions()[0].emit('click');
    await page.poses()[0].emit('click');
    assert.deepEqual(page.posts().map(item => [item.url, JSON.parse(item.body)]), [
      ['/api/motors/insert/goto', {name: 'load'}], ['/api/poses/furnace', {}],
    ]);
  },
  async pending_home_blocks_starts_but_not_stop(page) {
    page.render(homed());
    page.defer('/api/motors/bottom/home');
    const pending = page.home('bottom').emit('click');
    assert.equal(page.deferred.length, 1);
    await page.home('top').emit('click');
    await page.positions()[0].emit('click');
    await page.poses()[0].emit('click');
    await page.cards[0].directions[0].emit('click');
    assert.equal(page.node('#stop-all').disabled, false);
    await page.node('#stop-all').emit('click');
    const stopped = snapshot(4);
    page.render(stopped);
    const old = homed(3);
    old.automation.active = {action: 'home', motor: 'bottom', phase: 'home'};
    page.deferred[0](old);
    await pending;
    assert.deepEqual(page.posts().map(item => item.url), ['/api/motors/bottom/home', '/api/stop']);
    assert.equal(page.positions()[0].disabled, true);
    assert.equal(page.cards[0].querySelector('.homing-status').textContent, 'Home required');
  },
  async active_sequence_disables_manual_and_named_starts(page) {
    const next = homed();
    next.automation.active = {action: 'home', motor: 'bottom', phase: 'soft'};
    page.render(next);
    assert.equal(page.cards[0].querySelector('.homing-status').textContent, 'Homing…');
    for (const card of page.cards) {
      assert.equal(card.directions[0].disabled, true);
      assert.equal(card.holds[0].disabled, true);
      assert.equal(page.home(card.dataset.motor).disabled, true);
    }
    assert.equal(page.positions()[0].disabled, true);
    assert.equal(page.poses()[0].disabled, true);
    assert.equal(page.node('#apply-microstep').disabled, true);
    assert.equal(page.node('#stop-all').disabled, false);
  },
  async loss_fault_and_cooldown_block_new_actions(page) {
    for (const mutate of [
      next => { next.lease.holder = 'automation'; },
      next => { next.locked = true; },
      next => { next.connected = false; },
      next => { next.motors.forEach(motor => { motor.available = false; motor.cooldown = 1; }); },
    ]) {
      const next = homed(); mutate(next); page.render(next);
      assert.equal(page.home('insert').disabled, true);
      assert.equal(page.positions()[0].disabled, true);
      assert.equal(page.poses()[0].disabled, true);
      await page.home('insert').emit('click');
      await page.positions()[0].emit('click');
      await page.poses()[0].emit('click');
    }
    assert.equal(page.posts().length, 0);
  },
  async hold_prevents_home_and_releases_normally(page) {
    await page.cards[0].holds[0].emit('pointerdown', {pointerId: 7});
    await flush();
    assert.equal(page.home('insert').disabled, true);
    await page.home('insert').emit('click');
    await page.cards[0].holds[0].emit('pointerup', {pointerId: 7});
    await flush();
    assert.deepEqual(page.posts().map(item => item.url), [
      '/api/motors/bottom/hold', '/api/motors/bottom/hold/stop',
    ]);
  },
  async lost_reply_never_retries_motion(page) {
    page.render(homed());
    page.fail('/api/poses/furnace');
    await page.poses()[0].emit('click');
    assert.match(page.node('#notice').textContent, /Lost reply.*Check motor status/);
    assert.deepEqual(page.posts().map(item => item.url), ['/api/poses/furnace']);
    assert.equal([...page.timers.values()].some(timer => timer.delay === 200), false);
  },
  async failed_home_reports_backend_error(page) {
    const failed = snapshot(2);
    failed.automation.error = 'bottom: home failed: timeout';
    page.render(failed);
    assert.equal(page.node('#pose-feedback').textContent, failed.automation.error);
    assert.equal(page.cards[0].querySelector('.homing-status').textContent, 'Home required');
    assert.equal(page.posts().length, 0);
  },
  async config_errors_and_names_are_plain_text(page) {
    const next = homed();
    next.automation = {configured: false, error: '<missing config>', active: null, poses: []};
    next.motors.forEach(motor => {
      motor.homing_available = false; motor.automation_ready = false;
    });
    page.render(next);
    assert.equal(page.node('#pose-feedback').textContent, '<missing config>');
    assert.equal(page.home('insert').disabled, true);
    assert.equal(page.cards[2].directions[0].disabled, false);
    const named = homed(3);
    named.automation.poses = ['<image/pose>'];
    page.render(named);
    assert.equal(page.poses()[0].textContent, '<image/pose>');
    await page.poses()[0].emit('click');
    assert.equal(page.posts()[0].url, '/api/poses/%3Cimage%2Fpose%3E');
  },
};
(async () => { await scenarios[process.argv[2]](await browser()); })()
  .catch(error => { console.error(error); process.exitCode = 1; });
"""
)


@pytest.mark.parametrize(
    "scenario",
    [
        "legacy_firmware_keeps_manual_controls",
        "home_is_explicit_and_waits_for_confirmed_reference",
        "named_targets_use_only_the_selected_route",
        "pending_home_blocks_starts_but_not_stop",
        "active_sequence_disables_manual_and_named_starts",
        "loss_fault_and_cooldown_block_new_actions",
        "hold_prevents_home_and_releases_normally",
        "lost_reply_never_retries_motion",
        "failed_home_reports_backend_error",
        "config_errors_and_names_are_plain_text",
    ],
)
def test_stage_homing_controls(scenario):
    result = subprocess.run(
        ["node", "-e", HARNESS, str(SCRIPT), scenario],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_template_starts_reference_controls_disabled():
    template = (SCRIPT.parents[1] / "templates/index.html").read_text()
    assert 'class="home-button" disabled' in template
    assert 'class="homing-status" role="status"' in template
    assert 'id="pose-buttons" role="group"' in template
    assert 'role="group" aria-label="Insert named positions"' in template
