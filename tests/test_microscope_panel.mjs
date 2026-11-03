import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const script = readFileSync(new URL('../src/microscope/static/app.js', import.meta.url), 'utf8');
const html = readFileSync(new URL('../src/microscope/templates/index.html', import.meta.url), 'utf8');
const drain = async () => { for (let i = 0; i < 50; i++) await Promise.resolve(); };
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = new Map();
    this.value = ''; this.disabled = this.hidden = false; this.ownText = ''; this.parent = null;
  }
  set textContent(text) { this.ownText = String(text); this.children = []; }
  get textContent() { return this.ownText + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_value) { throw new Error('Untrusted content must remain text.'); }
  get firstElementChild() { return this.children[0]; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.ownText = ''; this.children = []; this.append(...children); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  removeAttribute(name) { delete this[name]; }
  addEventListener(event, callback) { this.listeners.set(event, callback); }
  emit(event) { if (event !== 'click' || !this.disabled) this.listeners.get(event)?.({ preventDefault() {} }); }
}
const client = 'microscope-' + '11'.repeat(16);
const lease = (holder = client, remaining_s = 3) => ({ holder, remaining_s });
const state = overrides => ({
  connected: true, busy: false, locked: false, fault: null, demo: true,
  lease: lease(null, 0), manual_client: null, focus_zero_known: true,
  focus_position_pulses: 0, focus_limits_pulses: [-200, 200], objective_id: 2,
  cameras: { microscope: false, side: false }, last_command: null, ...overrides,
});
const recordingId = '12'.repeat(16);
const recording = overrides => ({
  id: recordingId, status: 'recording', client, start_command_id: 'record-start', stop_command_id: null,
  run_name: 'Example', mode: 'time_lapse', capture_interval_seconds: 30, output_fps: 1,
  frame_count: 3, started_at: 123, finished_at: null, segments: ['capture.avi'],
  timestamps: 'capture.timestamps.csv', summary: null, archived: false, error: null, ...overrides,
});
const recordState = overrides => owned({
  cameras: { microscope: true, side: false }, recording: { status: 'idle' },
  recording_sequence: { armed: false, id: null, client: null, sequence_started_at: null },
  stabilization: false, ...overrides,
});
const owned = overrides => state({ lease: lease(), ...overrides });
const manual = overrides => owned({ manual_client: client, ...overrides });
const completed = (id = 'zero-one', result = 'ok') => ({ id, action: 'set_focus_zero', client, result, message: '', finished_at: 123 });
const description = { contract: 1, renew_only_heartbeat: true, commands: Object.fromEntries(['select_objective', 'autofocus', 'focus_move', 'home_focus', 'snapshot', 'start_recording', 'stop_recording', 'record_with_sequence', 'set_stabilization'].map(name => [name, {}])) };

function browser({ base = '/lab', identity = true } = {}) {
  let now = 0, timerId = 0, objectId = 0, randomCall = 0;
  const nodes = new Map(), timers = new Map(), requests = [], windowListeners = new Map(), documentListeners = new Map();
  const revoked = [], blobs = new Map(), confirmations = [];
  for (const match of html.matchAll(/<(\w+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const element = new Element(match[1]);
    element.disabled = /\sdisabled(?:\s|>)/.test(match[0]);
    element.hidden = /\shidden(?:\s|>)/.test(match[0]);
    element.value = /\bvalue="([^"]*)"/.exec(match[0])?.[1] || '';
    nodes.set(match[2], element);
  }
  nodes.get('recording-mode').value = 'time_lapse';
  const document = {
    hidden: false, body: { dataset: { basePath: base } },
    getElementById: id => { assert.ok(nodes.has(id), `Template contains ${id}`); return nodes.get(id); },
    createElement: tag => new Element(tag),
    addEventListener: (event, callback) => documentListeners.set(event, callback),
  };
  const page = {
    nodes, requests, revoked, blobs, confirmations, confirm: true,
    get: id => document.getElementById(id),
    pending: suffix => requests.find(request => !request.answered && !request.options.signal.aborted && request.url.endsWith(suffix)),
    async respond(suffix, data, status = 200) {
      const request = typeof suffix === 'number' ? requests[suffix] : page.pending(suffix);
      assert.ok(request, `Pending request ${suffix}; actual ${requests.filter(item => !item.answered && !item.options.signal.aborted).map(item => item.url)}`);
      request.answered = true;
      request.resolve({ ok: status >= 200 && status < 300, status, json: async () => data });
      await drain(); return request;
    },
    async reject(suffix) {
      const request = page.pending(suffix); assert.ok(request, suffix);
      request.answered = true; request.reject(new Error('Fake connection lost')); await drain();
    },
    async stream(name = 'microscope') {
      const request = page.pending(`/api/camera/${name}/mjpeg`); assert.ok(request);
      request.answered = true;
      const stream = new ReadableStream({ start(controller) { request.stream = controller; }, cancel() { request.cancelled = true; } });
      request.resolve({ ok: true, headers: { get: () => 'multipart/x-mixed-replace; boundary=frame' }, body: stream });
      await drain(); return request;
    },
    async chunk(request, bytes) { request.stream.enqueue(bytes); await drain(); },
    async click(id) { (typeof id === 'string' ? page.get(id) : id).emit('click'); await drain(); },
    async input(id, value) { page.get(id).value = value; page.get(id).emit('input'); await drain(); },
    async select(id, value) { page.get(id).value = value; page.get(id).emit('change'); await drain(); },
    async submit() { page.get('access-form').emit('submit'); await drain(); },
    async hide() { windowListeners.get('pagehide')(); await drain(); },
    async visibility(hidden) { document.hidden = hidden; documentListeners.get('visibilitychange')(); await drain(); },
    async advance(ms) {
      const end = now + ms; await drain(); let budget = 2000;
      while (budget-- > 0) {
        const first = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!first || first[1].at > end) break;
        now = first[1].at; timers.delete(first[0]); first[1].callback(); await drain();
      }
      assert.ok(budget > 0, 'Timers do not spin'); now = end; await drain();
    },
    async jump(ms) { now += ms; await drain(); },
  };
  vm.runInNewContext(script, {
    document, window: {
      addEventListener: (event, callback) => windowListeners.set(event, callback),
      confirm: message => { confirmations.push(message); return page.confirm; },
    },
    crypto: { getRandomValues: bytes => { if (!identity) throw new Error('Missing identity'); return bytes.fill(17 + randomCall++); } },
    AbortController, Uint8Array, TextDecoder, Blob,
    URL: { createObjectURL: blob => { const url = `blob:fake-${++objectId}`; blobs.set(url, blob); return url; }, revokeObjectURL: url => revoked.push(url) },
    performance: { now: () => now },
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, { at: now + delay, callback }); return id; },
    clearTimeout: id => timers.delete(id),
    fetch: (url, options) => {
      const pending = deferred(), request = { ...pending, url, options, answered: false };
      options.signal.addEventListener('abort', () => { pending.reject(new Error('Aborted')); try { request.stream?.error(new Error('Aborted')); } catch { /* Closed fake stream. */ } });
      requests.push(request); return pending.promise;
    },
    localStorage: new Proxy({}, { get() { throw new Error('No persistent credentials'); } }),
    sessionStorage: new Proxy({}, { get() { throw new Error('No persistent credentials'); } }),
  });
  return page;
}
const posts = page => page.requests.filter(request => request.options.method === 'POST');
const paths = page => posts(page).map(request => request.url.replace('/lab', ''));
async function connect(page, value = state(), panel = {}) {
  await page.respond('/api/panel-info', { objective_ids: [2, 4], calibration_available: true, calibration_error: null, home_switch: false, ...panel });
  await page.respond('/api/describe', description);
  await page.respond('/api/state', value);
  await page.respond('/api/events?since=0', { events: [], next: 0 });
}
async function acquire(page, value = owned()) {
  await page.click('acquire'); await page.respond('/api/heartbeat', { lease: lease() }); await page.respond('/api/state', value);
}
async function zeroAccepted(page, value = manual({ busy: true })) {
  await page.click('zero'); await page.respond('/api/state', owned());
  await page.respond('/api/release', { lease: lease(null, 0) });
  await page.respond('/api/state', state());
  await page.respond('/api/manual/heartbeat', { lease: lease() });
  await page.respond('/api/manual/set_focus_zero', { command_id: 'zero-one', state: value }, 202);
}
const encoder = new TextEncoder();
function frame({ length = 6, id = '7', captured = '123.5', extra = '' } = {}) {
  const header = encoder.encode(`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${length}\r\nX-Frame-Id: ${id}\r\nX-Captured-At: ${captured}\r\n${extra}\r\n`);
  const bytes = new Uint8Array(header.length + 8); bytes.set(header); bytes.set([255, 216, 1, 2, 255, 217, 13, 10], header.length); return bytes;
}

test('public shell contains only an empty credential field and prefixed static files', () => {
  assert.match(html, /data-base-path="\{\{ base_path \}\}"/);
  assert.match(html, /type="password" autocomplete="off"/);
  assert.match(html, /\{\{ base_path \}\}\/static\/app.js/);
  assert.doesNotMatch(html, /\{\{ (?!base_path)[^}]+\}\}/);
  assert.doesNotMatch(script, /innerHTML|localStorage|sessionStorage|sendBeacon|EventSource/);
  for (const base of ['//evil', '/a/../b', '/a?token', '/a%2fb']) assert.equal(browser({ base }).requests.length, 0);
  assert.equal(browser({ identity: false }).requests.length, 0);
  assert.equal(browser({ base: '/' }).requests[0].url, '/api/panel-info');
});

test('no-token demo reads start without acquiring control and use actual calibration IDs', async () => {
  const page = browser(); await connect(page);
  assert.equal(posts(page).length, 0);
  assert.match(page.get('connection').textContent, /Demo/);
  assert.deepEqual(page.get('objectives').children.map(item => item.textContent), ['Objective 2', 'Objective 4']);
  assert.equal(page.get('focus-plus').disabled, true);
  assert.equal(page.get('acquire').disabled, false);
});

test('bearer credentials stay in headers, access failure stops polling, and new input aborts old work', async () => {
  const page = browser(); await page.respond('/api/panel-info', { error: 'Unauthorized' }, 401);
  await page.advance(10000); assert.equal(page.requests.length, 1);
  await page.input('access-token', 'synthetic-private-token'); await page.submit(); await connect(page);
  await page.click('acquire');
  const request = page.pending('/api/heartbeat');
  assert.equal(request.options.headers.Authorization, 'Bearer synthetic-private-token');
  assert.equal(request.options.headers['X-MG400-Client'], client);
  assert.equal(request.options.headers['Content-Type'], 'application/json');
  for (const request of page.requests) {
    assert.equal(request.options.redirect, 'error'); assert.equal(request.options.credentials, 'omit');
    assert.equal(request.options.cache, 'no-store'); assert.ok(!request.url.includes('synthetic-private-token'));
  }
  await page.respond('/api/heartbeat', { error: 'Refused synthetic-private-token' }, 503);
  assert.doesNotMatch(page.get('notice').textContent, /synthetic-private-token/);
  assert.match(page.get('notice').textContent, /redacted/);
  await page.input('access-token', 'replacement'); assert.equal(page.get('stop').disabled, true);
});

test('foreign automation control disables every normal action but leaves stop available', async () => {
  const page = browser(); await connect(page, state({ lease: lease('automation'), busy: true, locked: true }));
  for (const id of ['acquire', 'release', 'focus-full', 'focus-local', 'focus-minus', 'focus-plus', 'home', 'zero', 'snapshot']) assert.equal(page.get(id).disabled, true, id);
  assert.equal(page.get('stop').disabled, false);
  await page.click('stop'); assert.deepEqual(paths(page), ['/api/stop']);
  assert.deepEqual(JSON.parse(posts(page)[0].options.body), { wait_s: 0 });
  await page.respond('/api/stop', { stop: 'pending', state: state({ busy: true }) });
  assert.match(page.get('notice').textContent, /Stop pending/);
});

test('ordinary control renews only with renew; an unanswered renewal expires without hidden reacquisition', async () => {
  const page = browser(); await connect(page); await acquire(page);
  await page.advance(1000);
  assert.deepEqual(paths(page), ['/api/heartbeat', '/api/renew']);
  await page.advance(2100);
  assert.equal(page.pending('/api/renew'), undefined);
  assert.equal(page.get('focus-plus').disabled, true);
  await page.advance(10000);
  assert.equal(paths(page).filter(path => path === '/api/heartbeat').length, 1);
  assert.equal(paths(page).filter(path => path === '/api/renew').length, 1);
});

test('a lease response received after its deadline cannot enable commands', async () => {
  const page = browser(); await connect(page); await page.click('acquire');
  await page.jump(3500); await page.respond('/api/heartbeat', { lease: lease() });
  assert.match(page.get('notice').textContent, /deadline/);
  assert.equal(page.get('focus-plus').disabled, true);
  assert.deepEqual(paths(page), ['/api/heartbeat']);
});

test('a state response started before acquisition cannot revoke the new lease', async () => {
  const page = browser(); await connect(page); await page.advance(750);
  const stale = page.pending('/api/state'); assert.ok(stale);
  await page.click('acquire'); await page.respond('/api/heartbeat', { lease: lease() });
  const fresh = page.requests.findLastIndex(request => request.url.endsWith('/api/state'));
  await page.respond(fresh, owned());
  assert.equal(page.get('focus-plus').disabled, false);
  stale.answered = true; stale.resolve({ ok: true, json: async () => state() }); await drain();
  assert.equal(page.get('focus-plus').disabled, false);
});

test('reported loss of an owned lease cancels the command wait and never claims a replacement lease', async () => {
  const page = browser(); await connect(page); await acquire(page);
  await page.click('focus-full'); const commandRequest = page.pending('/api/command');
  await page.respond('/api/command', { command_id: 'focus-one', state: owned({ busy: true }) }, 202);
  await page.advance(750); await page.respond('/api/state', state({ lease: lease('automation') }));
  assert.equal(page.get('focus-plus').disabled, true);
  await page.advance(10000);
  assert.equal(paths(page).filter(path => path === '/api/heartbeat').length, 1);
  assert.equal(paths(page).filter(path => path === '/api/command').length, 1);
  assert.equal(commandRequest.options.signal.aborted, false, 'The accepted request already completed');
});

test('a state sample that spent too long in flight cannot enable motion', async () => {
  const page = browser(); await connect(page); await page.click('acquire');
  await page.respond('/api/heartbeat', { lease: lease() });
  await page.jump(2600); await page.respond('/api/state', owned());
  assert.equal(page.get('focus-plus').disabled, true);
  await page.click('focus-plus'); assert.deepEqual(paths(page), ['/api/heartbeat']);
});

test('jog validation uses finite integer pulses and the configured limits', async () => {
  const page = browser(); await connect(page); await acquire(page, owned({ focus_position_pulses: 195 }));
  assert.equal(page.get('focus-plus').disabled, true); assert.equal(page.get('focus-minus').disabled, false);
  for (const value of ['0', '-1', '1.5', 'Infinity', '2147483648']) {
    await page.input('focus-step', value); assert.equal(page.get('focus-minus').disabled, true);
  }
  await page.input('focus-step', '5'); await page.click('focus-plus');
  assert.deepEqual(JSON.parse(posts(page).at(-1).options.body), { action: 'focus_move', steps: 5 });
  await page.respond('/api/command', { command_id: 'jog', state: owned({ busy: true }) }, 202);
  assert.equal(page.get('focus-plus').disabled, true);
  await page.advance(750); await page.respond('/api/state', owned({ last_command: { ...completed('other'), action: 'snapshot' } }));
  assert.equal(page.get('focus-plus').disabled, true, 'An unrelated receipt must not settle the command');
  await page.respond('/api/events?since=0', { events: [], next: 0 });
});

for (const [button, args] of [['focus-full', { action: 'autofocus', mode: 'full' }], ['focus-local', { action: 'autofocus', mode: 'local' }]]) {
  test(`${button} uses the contract only under an ordinary lease`, async () => {
    const page = browser(); await connect(page); await acquire(page); await page.click(button);
    assert.deepEqual(JSON.parse(posts(page).at(-1).options.body), args);
    await page.reject('/api/command'); await page.advance(10000);
    assert.equal(paths(page).filter(path => path === '/api/command').length, 1);
    assert.match(page.get('notice').textContent, /unknown|State unavailable/);
  });
}

test('objective controls use calibration IDs and unavailable calibration disables them', async () => {
  const page = browser(); await connect(page); await acquire(page); await page.click(page.get('objectives').children[1]);
  assert.deepEqual(JSON.parse(posts(page).at(-1).options.body), { action: 'select_objective', id: 4 });
  const missing = browser(); await connect(missing, state(), { objective_ids: [], calibration_available: false, calibration_error: '<missing>' });
  assert.equal(missing.get('objectives').children.length, 0); assert.equal(missing.get('calibration-status').textContent, '<missing>');
});

test('home requires a configured switch and explicit confirmation', async () => {
  const page = browser(); await connect(page, state(), { home_switch: true }); await acquire(page, owned({ focus_zero_known: false }));
  assert.equal(page.get('zero').hidden, true); assert.equal(page.get('home').hidden, false);
  page.confirm = false; await page.click('home'); assert.equal(paths(page).length, 1);
  page.confirm = true; await page.click('home');
  assert.deepEqual(JSON.parse(posts(page).at(-1).options.body), { action: 'home_focus' });
});

test('snapshot accepts a relative run path and rejects traversal before dispatch', async () => {
  const page = browser(); await connect(page); await acquire(page, owned({ cameras: { microscope: true, side: false } }));
  for (const path of ['/tmp/file', '../file', 'run/../file', 'run\\file', 'a//b']) {
    await page.input('snapshot-path', path); await page.click('snapshot'); assert.equal(paths(page).length, 1);
  }
  await page.input('snapshot-path', 'run-one/image.png'); await page.click('snapshot');
  assert.deepEqual(JSON.parse(posts(page).at(-1).options.body), { action: 'snapshot', path: 'run-one/image.png' });
});

test('manual zero waits for its exact receipt and renews manual control before the ordinary handoff', async () => {
  const page = browser(); await connect(page); await acquire(page); await zeroAccepted(page);
  assert.match(page.confirmations[0], /physical clearance/);
  assert.deepEqual(JSON.parse(posts(page).at(-1).options.body), { confirm: true });
  await page.advance(150); await page.respond('/api/state', manual({ busy: true, last_command: completed('unrelated') }));
  assert.equal(paths(page).filter(path => path === '/api/heartbeat').length, 1);
  await page.advance(850); await page.respond('/api/manual/renew', { lease: lease() });
  assert.equal(paths(page).includes('/api/renew'), false);
  await page.respond('/api/state', manual({ last_command: completed() }));
  await page.respond('/api/manual/release', { lease: lease(null, 0) });
  await page.respond('/api/heartbeat', { lease: lease() });
  await page.respond('/api/state', owned({ last_command: completed() }));
  assert.match(page.get('notice').textContent, /Focus zero established/);
  assert.equal(page.get('focus-plus').disabled, false);
  assert.deepEqual(paths(page), ['/api/heartbeat', '/api/release', '/api/manual/heartbeat', '/api/manual/set_focus_zero', '/api/manual/renew', '/api/manual/release', '/api/heartbeat']);
});

test('a poll from before manual zero cannot revoke the completed ordinary handoff', async () => {
  const page = browser(); await connect(page); await acquire(page); await page.advance(750);
  const old = page.pending('/api/state'); assert.ok(old);
  const latestState = data => page.respond(page.requests.findLastIndex(request => request.url.endsWith('/api/state')), data);
  await page.click('zero'); await latestState(owned());
  await page.respond('/api/release', { lease: lease(null, 0) }); await latestState(state());
  await page.respond('/api/manual/heartbeat', { lease: lease() });
  await page.respond('/api/manual/set_focus_zero', { command_id: 'zero-one', state: manual({ last_command: completed() }) }, 202);
  await page.respond('/api/manual/release', { lease: lease(null, 0) });
  await page.respond('/api/heartbeat', { lease: lease() }); await latestState(owned({ last_command: completed() }));
  old.answered = true; old.resolve({ ok: true, json: async () => state() }); await drain();
  assert.equal(page.get('focus-plus').disabled, false);
  assert.match(page.get('notice').textContent, /Focus zero established/);
});

test('cancelled confirmation sends no manual request', async () => {
  const page = browser(); await connect(page); await acquire(page); page.confirm = false; await page.click('zero');
  assert.deepEqual(paths(page), ['/api/heartbeat']);
});

test('ordinary release must complete before manual acquire, and a lost release stops the flow', async () => {
  const page = browser(); await connect(page); await acquire(page); await page.click('zero');
  await page.respond('/api/state', owned());
  assert.deepEqual(paths(page), ['/api/heartbeat', '/api/release']);
  await page.reject('/api/release'); await page.advance(5000);
  assert.equal(paths(page).includes('/api/manual/heartbeat'), false);
});

test('late manual release cannot cause ordinary reacquisition after the handoff deadline', async () => {
  const page = browser(); await connect(page); await acquire(page);
  await zeroAccepted(page, manual({ last_command: completed() }));
  const release = page.pending('/api/manual/release'); assert.ok(release);
  await page.jump(3100); await page.respond('/api/manual/release', { lease: lease(null, 0) });
  assert.equal(paths(page).filter(path => path === '/api/heartbeat').length, 1);
  assert.match(page.get('notice').textContent, /expired/);
});

test('ordinary release is explicit and leaves the panel without authority', async () => {
  const page = browser(); await connect(page); await acquire(page); await page.click('release');
  await page.respond('/api/release', { lease: lease(null, 0) });
  assert.equal(page.get('focus-plus').disabled, true);
  await page.advance(10000); assert.deepEqual(paths(page), ['/api/heartbeat', '/api/release']);
});

test('manual handoff waits for release stop to settle and cannot take a foreign lease', async () => {
  const page = browser(); await connect(page); await acquire(page); await page.click('zero');
  await page.respond('/api/state', owned()); await page.respond('/api/release', { lease: lease(null, 0) });
  await page.respond('/api/state', state({ busy: true }));
  assert.deepEqual(paths(page), ['/api/heartbeat', '/api/release']);
  await page.advance(100); await page.respond('/api/state', state({ lease: lease('automation') }));
  assert.deepEqual(paths(page), ['/api/heartbeat', '/api/release']);
  assert.match(page.get('notice').textContent, /handoff/);
});

for (const failure of ['failed', 'stopped', 'rejected']) {
  test(`manual zero ${failure} receipt prevents release and reacquisition`, async () => {
    const page = browser(); await connect(page); await acquire(page); await zeroAccepted(page, manual({ last_command: completed('zero-one', failure) }));
    assert.equal(paths(page).includes('/api/manual/release'), false);
    assert.equal(paths(page).filter(path => path === '/api/heartbeat').length, 1);
    assert.equal(page.get('focus-plus').disabled, true);
  });
}

test('lost manual response, expiry, and token change never advance the zero flow', async () => {
  for (const event of ['lost', 'expired', 'token', 'pagehide', 'stop']) {
    const page = browser(); await connect(page); await acquire(page); await zeroAccepted(page);
    if (event === 'lost') { await page.advance(150); await page.reject('/api/state'); }
    if (event === 'expired') await page.advance(3100);
    if (event === 'token') await page.input('access-token', 'new-token');
    if (event === 'pagehide') await page.hide();
    if (event === 'stop') await page.click('stop');
    await page.advance(5000);
    assert.equal(paths(page).includes('/api/manual/release'), false, event);
    assert.equal(paths(page).filter(path => path === '/api/heartbeat').length, 1, event);
    assert.equal(paths(page).filter(path => path === '/api/manual/set_focus_zero').length, 1, event);
  }
});

test('page exit cancels requests and timers without sending release, stop, or any other mutation', async () => {
  const page = browser(); await connect(page); await acquire(page); const before = paths(page);
  await page.hide(); await page.advance(30000); assert.deepEqual(paths(page), before);
  assert.equal(page.get('access-token').value, '');
});

test('authenticated MJPEG supports fragmented frames and releases replaced and hidden object URLs', async () => {
  const page = browser(); await page.input('access-token', 'camera-private-token'); await page.submit();
  await connect(page, state({ cameras: { microscope: true, side: true } }));
  assert.equal(page.requests.filter(item => item.url.endsWith('/mjpeg')).length, 2);
  const request = await page.stream(); assert.equal(request.options.headers.Authorization, 'Bearer camera-private-token');
  const bytes = frame(); await page.chunk(request, bytes.slice(0, 15)); assert.equal(page.blobs.size, 0);
  await page.chunk(request, bytes.slice(15)); assert.equal(page.blobs.size, 1);
  const first = page.get('microscope-image').src;
  assert.match(page.get('microscope-status').textContent, /Frame 7 · Captured 123.5/);
  await page.chunk(request, frame({ id: '8' })); assert.ok(page.revoked.includes(first));
  const second = page.get('microscope-image').src;
  await page.visibility(true); assert.ok(page.revoked.includes(second)); assert.equal(request.options.signal.aborted, true);
  assert.equal(page.get('microscope-image').src, undefined);
  await page.visibility(false); assert.equal(page.requests.filter(item => item.url.endsWith('/microscope/mjpeg')).length, 2);
});

for (const [name, bytes] of [
  ['oversized frame', frame({ length: 16777217 })], ['invalid length', frame({ length: '-1' })],
  ['nonfinite timestamp', frame({ captured: 'Infinity' })], ['duplicate header', frame({ extra: 'Content-Length: 6\r\n' })],
  ['oversized header', encoder.encode('--frame\r\n' + 'x'.repeat(4097))], ['oversized chunk', new Uint8Array(16777216 + 4099)],
]) {
  test(`MJPEG rejects ${name} and closes the old reader`, async () => {
    const page = browser(); await connect(page, state({ cameras: { microscope: true } })); const request = await page.stream();
    await page.chunk(request, bytes); assert.equal(request.options.signal.aborted, true); assert.equal(page.blobs.size, 0);
    assert.equal(posts(page).length, 0);
  });
}

test('slow partial MJPEG cannot extend the frame deadline or retain a stale image', async () => {
  const page = browser(); await connect(page, state({ cameras: { microscope: true } })); const request = await page.stream();
  await page.chunk(request, frame()); const shown = page.get('microscope-image').src;
  await page.advance(4000); await page.chunk(request, encoder.encode('--fra'));
  await page.advance(1000); assert.equal(request.options.signal.aborted, true); assert.ok(page.revoked.includes(shown));
  assert.equal(page.get('microscope-image').src, undefined);
});

test('token replacement cancels previews and no old stream or late command can restore state', async () => {
  const page = browser(); await connect(page, state({ cameras: { microscope: true } })); const stream = await page.stream();
  await page.chunk(stream, frame()); await acquire(page, owned({ cameras: { microscope: true } }));
  await page.click('focus-full'); const request = page.pending('/api/command');
  await page.input('access-token', 'replacement');
  assert.equal(stream.options.signal.aborted, true); assert.equal(request.options.signal.aborted, true);
  request.resolve({ ok: true, json: async () => ({ command_id: 'late', state: owned() }) }); await drain();
  assert.equal(page.get('focus-plus').disabled, true); assert.equal(page.get('microscope-image').src, undefined);
  await page.advance(10000); assert.equal(paths(page).filter(path => path === '/api/heartbeat').length, 1);
});

test('event text is bounded and never parsed as HTML', async () => {
  const page = browser();
  await page.respond('/api/panel-info', { objective_ids: [], calibration_available: false, home_switch: false });
  await page.respond('/api/describe', description); await page.respond('/api/state', state());
  await page.respond('/api/events?since=0', { events: Array.from({ length: 100 }, (_, i) => ({ level: 'info', message: '<script>' + i })), next: 100 });
  assert.equal(page.get('events').children.length, 50); assert.match(page.get('events').textContent, /<script>99/);
});

async function recordingPage(overrides = {}) {
  const page = browser(); await connect(page, state()); await acquire(page, recordState(overrides)); return page;
}
async function recordingReceipt(page, action, value, id = 'record-start') {
  const result = action === 'record_with_sequence' ? { recording_sequence: value.recording_sequence } : { recording: value.recording };
  await page.respond('/api/command', { command_id: id, state: { last_result: result, ...value, last_command: { ...completed(id), action } } }, 202);
}

test('recording requires explicit ordinary control and never claims authority on load', async () => {
  const page = browser(); await connect(page, state({ ...recordState(), lease: lease('automation') }));
  for (const id of ['recording-start', 'recording-stop', 'sequence-arm', 'sequence-disarm', 'stabilization']) assert.equal(page.get(id).disabled, true, id);
  assert.equal(page.get('stop').disabled, false); assert.equal(posts(page).length, 0);
});

test('missing recording state cannot enable a new recording or sequence arm', async () => {
  const page = await recordingPage({ recording: undefined });
  assert.equal(page.get('recording-start').disabled, true);
  assert.equal(page.get('sequence-arm').disabled, true);
  await page.click('recording-start'); assert.deepEqual(paths(page), ['/api/heartbeat']);
});

test('time-lapse start sends a new caller ID and explicit defaults, then confirms the exact recording receipt', async () => {
  const page = await recordingPage(); await page.input('recording-name', 'Chip <one>'); await page.click('recording-start');
  const body = JSON.parse(posts(page).at(-1).options.body);
  assert.deepEqual(body, { action: 'start_recording', recording_id: recordingId, run_name: 'Chip <one>', mode: 'time_lapse', output_fps: 1, capture_interval_seconds: 30 });
  assert.match(page.get('recording-attempt').textContent, new RegExp(recordingId));
  assert.equal(page.get('recording-start').disabled, true);
  await recordingReceipt(page, 'start_recording', recordState({ recording: recording() }));
  assert.match(page.get('recording-attempt').textContent, /recording started/);
  assert.equal(page.get('recording-stop').disabled, false);
  assert.equal(page.get('focus-plus').disabled, false, 'Recording does not replace motion busy state');
});

test('live mode omits a time-lapse interval and changes the output default to 30 fps', async () => {
  const page = await recordingPage(); await page.select('recording-mode', 'live');
  assert.equal(page.get('recording-interval').disabled, true);
  assert.equal(page.get('recording-fps').value, '30');
  await page.input('recording-interval', 'invalid'); await page.click('recording-start');
  assert.deepEqual(JSON.parse(posts(page).at(-1).options.body), { action: 'start_recording', recording_id: recordingId, mode: 'live', output_fps: 30 });
});

test('recording options reject nonfinite, out-of-range, and oversized values before a request', async () => {
  const page = await recordingPage();
  for (const [id, bad] of [['recording-fps', 'Infinity'], ['recording-fps', '0.09'], ['recording-fps', '120.1'], ['recording-interval', '3601'], ['recording-interval', '-1'], ['recording-name', 'x'.repeat(81)]]) {
    const previous = page.get(id).value;
    await page.input(id, bad); await page.click('recording-start'); assert.equal(paths(page).length, 1, `${id}=${bad}`);
    await page.input(id, previous);
  }
  await page.input('recording-fps', '0.1'); await page.input('recording-interval', '3600'); await page.click('recording-start');
  const body = JSON.parse(posts(page).at(-1).options.body); assert.equal(body.output_fps, 0.1); assert.equal(body.capture_interval_seconds, 3600);
});

test('a lost start reply retains its recording ID without replay or implicit reacquisition', async () => {
  const page = await recordingPage(); await page.click('recording-start'); await page.reject('/api/command');
  assert.match(page.get('recording-attempt').textContent, new RegExp(recordingId));
  assert.match(page.get('recording-attempt').textContent, /outcome unknown/);
  await page.advance(10000);
  assert.equal(paths(page).filter(path => path === '/api/command').length, 1);
  assert.equal(paths(page).filter(path => path === '/api/heartbeat').length, 1);
});

for (const event of ['token', 'expiry', 'stop', 'pagehide']) {
  test(`${event} cancels a pending recording request without losing its ID or retrying`, async () => {
    const page = await recordingPage(); await page.click('recording-start');
    if (event === 'token') await page.input('access-token', 'different-token');
    if (event === 'expiry') await page.advance(3100);
    if (event === 'stop') await page.click('stop');
    if (event === 'pagehide') await page.hide();
    assert.match(page.get('recording-attempt').textContent, new RegExp(recordingId));
    assert.match(page.get('recording-attempt').textContent, /outcome unknown/);
    await page.advance(5000);
    assert.equal(paths(page).filter(path => path === '/api/command').length, 1);
    assert.equal(paths(page).filter(path => path === '/api/heartbeat').length, 1);
  });
}

test('a new explicit recording attempt uses a fresh ID and never replays the earlier one', async () => {
  const page = await recordingPage(); await page.click('recording-start');
  await recordingReceipt(page, 'start_recording', recordState({ recording: recording() }));
  await page.click('recording-stop');
  await recordingReceipt(page, 'stop_recording', recordState({ recording: recording({ status: 'completed', stop_command_id: 'record-stop', finished_at: 140 }) }), 'record-stop');
  await page.click('recording-start');
  const starts = posts(page).map(request => JSON.parse(request.options.body)).filter(body => body.action === 'start_recording');
  assert.equal(starts.length, 2); assert.match(starts[1].recording_id, /^[0-9a-f]{32}$/); assert.notEqual(starts[0].recording_id, starts[1].recording_id);
});

test('finish recording targets the exact current ID and waits for matched finalization', async () => {
  const page = await recordingPage({ recording: recording() }); await page.click('recording-stop');
  assert.deepEqual(JSON.parse(posts(page).at(-1).options.body), { action: 'stop_recording', recording_id: recordingId });
  await page.respond('/api/command', { command_id: 'record-stop', state: recordState({ recording: recording({ status: 'finalizing', stop_command_id: 'record-stop' }) }) }, 202);
  assert.match(page.get('recording-attempt').textContent, /waiting/); assert.doesNotMatch(page.get('recording-attempt').textContent, /finalized/);
  await page.advance(750);
  const finalized = recording({ status: 'completed', stop_command_id: 'record-stop', summary: 'recording.json', archived: true });
  await page.respond('/api/state', recordState({ recording: finalized, last_result: { recording: finalized }, last_command: { ...completed('record-stop'), action: 'stop_recording' } }));
  assert.match(page.get('recording-attempt').textContent, /recording finalized/);
  assert.match(page.get('recording-status').textContent, /Archive copy saved/);
  assert.deepEqual(page.get('recording-files').children.map(item => item.textContent), ['capture.avi', 'capture.timestamps.csv', 'recording.json']);
});

for (const mismatch of [{ id: 'ff'.repeat(16) }, { stop_command_id: 'other-stop' }, { status: 'finalizing' }, { status: 'failed', error: 'Fake archive failure' }]) {
  test(`stop receipt does not claim completion for mismatched state ${JSON.stringify(mismatch)}`, async () => {
    const page = await recordingPage({ recording: recording() }); await page.click('recording-stop');
    await recordingReceipt(page, 'stop_recording', recordState({ recording: recording({ status: 'completed', stop_command_id: 'record-stop', ...mismatch }) }), 'record-stop');
    assert.doesNotMatch(page.get('recording-attempt').textContent, /recording finalized/);
    assert.match(page.get('recording-attempt').textContent, /outcome unknown/);
    assert.match(page.get('command-status').textContent, /outcome unknown/);
    assert.equal(page.get('recording-start').disabled, true);
  });
}

test('sequence recording arms once and cancels only its exact arm ID', async () => {
  const page = await recordingPage(); await page.click('sequence-arm');
  assert.deepEqual(JSON.parse(posts(page).at(-1).options.body), { action: 'record_with_sequence', recording_id: recordingId, enabled: true, mode: 'time_lapse', output_fps: 1, capture_interval_seconds: 30 });
  await recordingReceipt(page, 'record_with_sequence', recordState({ recording_sequence: { armed: true, id: recordingId, client, sequence_started_at: null } }), 'arm-one');
  assert.match(page.get('sequence-status').textContent, /Armed for the next/);
  assert.equal(page.get('recording-start').disabled, true); assert.equal(page.get('sequence-arm').disabled, true);
  await page.click('sequence-disarm');
  assert.deepEqual(JSON.parse(posts(page).at(-1).options.body), { action: 'record_with_sequence', recording_id: recordingId, enabled: false });
  await recordingReceipt(page, 'record_with_sequence', recordState({ last_result: { recording_sequence: { armed: false, id: recordingId, client, sequence_started_at: null } } }), 'disarm-one');
  assert.match(page.get('recording-attempt').textContent, /sequence recording cancelled/);
  assert.equal(paths(page).filter(path => path === '/api/heartbeat').length, 1);
});

test('stabilization changes only after an explicit command, never from a read or reconnect', async () => {
  const page = await recordingPage(); await page.click('stabilization');
  assert.deepEqual(JSON.parse(posts(page).at(-1).options.body), { action: 'set_stabilization', enabled: true });
  await recordingReceipt(page, 'set_stabilization', recordState({ stabilization: true }), 'stabilize');
  assert.equal(page.get('stabilization').textContent, 'Disable stabilization');
  assert.equal(paths(page).filter(path => path === '/api/command').length, 1);
  await page.click('stabilization'); assert.equal(JSON.parse(posts(page).at(-1).options.body).enabled, false);
});

test('recording status cannot enable another client ID or parse filenames and errors as HTML', async () => {
  const page = await recordingPage({ recording: recording({ client: 'another-page', segments: ['<video>.avi', '/private/data.avi'], error: '<script>untrusted</script>' }) });
  assert.equal(page.get('recording-stop').disabled, true); assert.equal(page.get('recording-start').disabled, true);
  assert.equal(page.get('recording-error').textContent, '<script>untrusted</script>');
  assert.deepEqual(page.get('recording-files').children.map(item => item.textContent), ['<video>.avi', 'capture.timestamps.csv']);
  assert.equal(page.get('stop').disabled, false);
});

for (const [name, mismatch] of [
  ['missing result', { last_result: null }],
  ['wrong recording ID', { last_result: { recording: recording({ id: 'ff'.repeat(16) }) } }],
  ['wrong start ID', { last_result: { recording: recording({ start_command_id: 'other-start' }) } }],
  ['wrong action', { last_command: { ...completed('record-start'), action: 'snapshot' } }],
  ['wrong client', { last_command: { ...completed('record-start'), action: 'start_recording', client: 'other-page' } }],
]) {
  test(`start receipt rejects ${name}`, async () => {
    const page = await recordingPage(); await page.click('recording-start');
    await page.respond('/api/command', { command_id: 'record-start', state: recordState({ recording: recording(), last_result: { recording: recording() }, last_command: { ...completed('record-start'), action: 'start_recording' }, ...mismatch }) }, 202);
    assert.match(page.get('recording-attempt').textContent, /outcome unknown/);
    assert.doesNotMatch(page.get('recording-attempt').textContent, /recording started/);
  });
}

test('an accepted arm snapshot stays valid after the watcher consumes the arm, without claiming finalization', async () => {
  const page = await recordingPage(); await page.click('sequence-arm');
  await recordingReceipt(page, 'record_with_sequence', recordState({
    recording: recording({ start_command_id: 'arm-one' }),
    last_result: { recording_sequence: { armed: true, id: recordingId, client, sequence_started_at: null } },
  }), 'arm-one');
  assert.match(page.get('recording-attempt').textContent, /sequence recording armed/);
  assert.doesNotMatch(page.get('recording-attempt').textContent, /finalized|recording started/);
  assert.match(page.get('recording-status').textContent, /recording/);
});

test('disarm cannot confirm from an unrelated armed=false state or wrong arm result ID', async () => {
  for (const result of [null, { recording_sequence: { armed: false, id: 'ff'.repeat(16), client } }, { recording_sequence: { armed: false, id: recordingId, client: 'another-page' } }]) {
    const page = await recordingPage({ recording_sequence: { armed: true, id: recordingId, client, sequence_started_at: null } });
    await page.click('sequence-disarm');
    await recordingReceipt(page, 'record_with_sequence', recordState({ last_result: result }), 'disarm-one');
    assert.match(page.get('recording-attempt').textContent, /outcome unknown/);
    assert.doesNotMatch(page.get('recording-attempt').textContent, /sequence recording cancelled/);
  }
});

test('completed current state cannot replace a missing or mismatched immutable stop result', async () => {
  for (const result of [null, { recording: recording({ status: 'completed', stop_command_id: 'other-stop' }) }]) {
    const page = await recordingPage({ recording: recording() }); await page.click('recording-stop');
    await recordingReceipt(page, 'stop_recording', recordState({ recording: recording({ status: 'completed', stop_command_id: 'record-stop' }), last_result: result }), 'record-stop');
    assert.match(page.get('recording-attempt').textContent, /outcome unknown/);
    assert.doesNotMatch(page.get('recording-attempt').textContent, /recording finalized/);
  }
});

test('recording finalization has a finite 600-second observation budget and leaves Stop available', async () => {
  for (const [elapsed, expired] of [[121000, false], [601000, true]]) {
    const page = browser(); await connect(page); await page.click('acquire');
    // A long fake lease isolates the observation budget from independent expiry tests.
    await page.respond('/api/heartbeat', { lease: lease(client, 1000) });
    const longLease = recordState({ lease: lease(client, 1000), recording: recording() });
    await page.respond('/api/state', longLease); await page.click('recording-stop');
    const finalizing = { ...longLease, recording: recording({ status: 'finalizing', stop_command_id: 'record-stop' }) };
    await page.respond('/api/command', { command_id: 'record-stop', state: finalizing }, 202);
    await page.advance(750); await page.jump(elapsed); await page.respond('/api/state', finalizing);
    assert.equal(page.get('stop').disabled, false);
    assert.match(page.get('recording-attempt').textContent, expired ? /outcome unknown/ : /waiting/);
    assert.doesNotMatch(page.get('recording-attempt').textContent, /recording finalized/);
    assert.equal(paths(page).filter(path => path === '/api/command').length, 1);
    assert.equal(paths(page).filter(path => path === '/api/heartbeat').length, 1);
  }
});
