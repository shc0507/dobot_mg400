import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import vm from 'node:vm';
import { validateActions } from '../scripts/isaac_client.mjs';
import { clientOperation } from '../scripts/isaac_client_runtime.mjs';
import { acquireClientLock, executable, withInspector } from '../scripts/isaac_client_transport.mjs';

const clientURL = 'file:///Applications/Isaac%20Sim%20WebRTC%20Streaming%20Client.app/Contents/Resources/app.asar/dist/renderer/index.html';
const copy = value => JSON.parse(JSON.stringify(value));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

function client(options = {}) {
  const calls = [], nativeChanges = [];
  const state = {
    focused: false, minimized: false, visible: true, hidden: false,
    cursor: { x: 721, y: 420 },
    bounds: { x: 20, y: 30, width: 1280, height: 752 },
    contentBounds: { x: 20, y: 62, width: 1280, height: 720 },
    zoom: 1, url: clientURL, windowId: 3, contentsId: 9,
    renderer: {
      width: 1280, height: 720, dpr: 2, timeOrigin: 100,
      video: { x: 100, y: 50, width: 640, height: 360, intrinsicWidth: 1280, intrinsicHeight: 720, ready: 4, fit: 'contain', position: '50% 50%' },
    },
    covered: false, empty: false, attached: false, videoFrames: 100, stalled: false, canvasError: false, contextUnavailable: false,
    ...options.state,
  };
  const window = new EventEmitter();
  const hooks = { send: null, capture: null };
  const focusCalls = [];
  const body = { isConnected: true, focus: () => focusCalls.push('body.focus') };
  const rendererState = { activeElement: body };
  const videoParent = { id: 'remote-video-input-parent', dispatchEvent() {} };
  const video = {
    isConnected: true, parentElement: videoParent,
    get videoWidth() { return state.renderer.video?.intrinsicWidth || 0; },
    get videoHeight() { return state.renderer.video?.intrinsicHeight || 0; },
    get readyState() { return state.renderer.video?.ready || 0; },
    focus() { focusCalls.push('video.focus'); rendererState.activeElement = video; },
    blur() { focusCalls.push('video.blur'); rendererState.activeElement = body; },
  };
  const rendererContext = vm.createContext({
    document: {
      get activeElement() { return rendererState.activeElement; },
      hasFocus: () => state.focused,
      getElementById: id => id === 'remote-video' ? (state.renderer.video ? video : null) : id === 'remote-video-input-parent' ? videoParent : null,
      createElement: name => {
        assert.equal(name, 'canvas');
        return {
          width: 0, height: 0,
          getContext: kind => {
            assert.equal(kind, '2d');
            return state.contextUnavailable ? null : { drawImage() { if (state.canvasError) throw new Error('video canvas read failed'); } };
          },
          toDataURL: kind => { assert.equal(kind, 'image/png'); return state.empty ? '' : `data:image/png;base64,${Buffer.from('test-png').toString('base64')}`; },
        };
      },
    },
    window: { dispatchEvent() {} },
    Event: class { constructor(type) { this.type = type; } },
    FocusEvent: class { constructor(type) { this.type = type; } },
  });
  const debuggerAPI = {
    isAttached: () => state.attached,
    attach: version => { calls.push({ method: 'attach', version }); state.attached = true; },
    detach: () => { calls.push({ method: 'detach' }); state.attached = false; },
    async sendCommand(method, params) {
      calls.push({ method, params: copy(params) });
      if (hooks.send) {
        const response = await hooks.send(method, params);
        if (response !== undefined) return response;
      }
      if (method === 'Runtime.evaluate') {
        if (params.expression.includes('drawImage')) await hooks.capture?.();
        if (params.expression.includes('drawImage') || params.expression.includes('__mg400ClientFocusV1') || params.expression.includes('v.focus(')) {
          try { return { result: { value: vm.runInContext(params.expression, rendererContext) } }; }
          catch (error) { return { exceptionDetails: { text: error.message, exception: { description: error.message } } }; }
        }
        if (params.expression.includes('getVideoPlaybackQuality')) return { result: { value: { time: 1, frames: state.stalled ? state.videoFrames : state.videoFrames++, ready: 4 } } };
        if (params.expression.includes('getBoundingClientRect')) return { result: { value: copy(state.renderer) } };
        if (params.expression.includes('elementFromPoint')) return { result: { value: !state.covered } };
        return { result: { value: true } };
      }
      return {};
    },
  };
  const contents = {
    get id() { return state.contentsId; },
    getURL: () => state.url,
    getZoomFactor: () => state.zoom,
    debugger: debuggerAPI,
    async capturePage() { throw new Error('Electron capturePage must not be used for video captures.'); },
  };
  Object.assign(window, {
    webContents: contents,
    isFocused: () => state.focused,
    isMinimized: () => state.minimized,
    isVisible: () => state.visible,
    isDestroyed: () => false,
    getBounds: () => copy(state.bounds),
    getContentBounds: () => copy(state.contentBounds),
  });
  Object.defineProperty(window, 'id', { get: () => state.windowId });
  for (const name of ['focus', 'show', 'hide', 'restore', 'minimize', 'setBounds']) {
    window[name] = () => { nativeChanges.push(name); throw new Error(`Unexpected native window mutation: ${name}`); };
  }
  const electron = {
    app: { isHidden: () => state.hidden },
    BrowserWindow: { getAllWindows: () => options.windows === 0 ? [] : [window] },
    screen: { getCursorScreenPoint: () => copy(state.cursor) },
  };
  let id = 0;
  const context = vm.createContext({
    process: { pid: 1234, mainModule: { require(name) {
      if (name === 'electron') return electron;
      if (name === 'node:crypto') return { randomUUID: () => `frame-${++id}` };
      throw new Error(`Unexpected require: ${name}`);
    } } },
    setTimeout: (callback, ms) => setTimeout(callback, ms >= 3000 ? 20 : 0),
    clearTimeout,
  });
  const run = vm.runInContext(`(${clientOperation.toString()})`, context);
  return { state, window, hooks, calls, nativeChanges, run, context, rendererState, body, focusCalls,
    input: () => calls.filter(call => call.method.startsWith('Input.')),
    async capture() {
      const result = await run({ command: 'capture' });
      assert.equal(result.ok, true, JSON.stringify(result));
      return result.frame;
    },
  };
}

test('action validation rejects invalid batches before connecting', () => {
  for (const actions of [[], Array(9).fill({ type: 'key', key: 'Escape' }), [{ type: 'click', x: NaN, y: 1 }], [{ type: 'click', x: -1, y: 0 }], [{ type: 'key', key: 'F12' }], [{ type: 'key', key: 'a', modifiers: ['Meta', 'Meta'] }], [{ type: 'text', text: '中' }], [{ type: 'text', text: 'x'.repeat(257) }]]) {
    assert.throws(() => validateActions(actions));
  }
  assert.doesNotThrow(() => validateActions([{ type: 'click', x: 20, y: 30 }, { type: 'key', key: 'a', modifiers: ['Control'] }, { type: 'text', text: '/tmp/script.py\n' }]));
});

test('the local lock excludes concurrent owners and releases for the next owner', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'isaac-client-test-'));
  const path = join(directory, 'client.lock');
  try {
    const release = await acquireClientLock(path, 0);
    await assert.rejects(acquireClientLock(path, 0), /CLIENT_BUSY/);
    release();
    const nextRelease = await acquireClientLock(path, 0);
    nextRelease();
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('release does not delete a lock whose owner changed', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'isaac-client-test-'));
  const path = join(directory, 'client.lock');
  try {
    const release = await acquireClientLock(path, 0);
    writeFileSync(path, 'other-owner');
    release();
    assert.equal(readFileSync(path, 'utf8'), 'other-owner');
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('status reports focused, hidden and minimized separately without desktop mutation', async () => {
  const h = client({ state: { focused: true, hidden: true, minimized: true } });
  const result = await h.run({ command: 'status' });
  assert.equal(result.ok, true);
  assert.equal(result.state.focused, true);
  assert.equal(result.state.hidden, true);
  assert.equal(result.state.minimized, true);
  assert.deepEqual(h.nativeChanges, []);
  assert.deepEqual(h.input(), []);
  assert.equal(h.state.attached, false);
});

test('missing installed window and an existing debugger are left untouched', async () => {
  for (const options of [{ windows: 0 }, { state: { attached: true } }, { state: { url: 'file:///Volumes/Installer/client.html' } }]) {
    const h = client(options);
    const result = await h.run({ command: 'capture' });
    assert.equal(result.ok, false);
    assert.deepEqual(h.calls, []);
    assert.deepEqual(h.nativeChanges, []);
  }
});

for (const [name, state, code] of [
  ['minimized', { minimized: true }, 'CLIENT_MINIMIZED'],
  ['hidden application', { hidden: true }, 'CLIENT_HIDDEN'],
  ['invisible window', { visible: false }, 'CLIENT_HIDDEN'],
]) {
  test(`capture refuses a ${name} without restoring it`, async () => {
    const h = client({ state });
    const result = await h.run({ command: 'capture' });
    assert.equal(result.ok, false);
    assert.equal(result.code, code);
    assert.deepEqual(h.nativeChanges, []);
    assert.deepEqual(h.input(), []);
    assert.equal(h.state.attached, false);
  });
}

test('capture reads native video pixels without an Electron window capture', async () => {
  const h = client();
  const frame = await h.capture();
  assert.deepEqual(copy(frame.size), { width: 1280, height: 720 });
  assert.equal(frame.source, 'remote-video');
  assert.equal(frame.png, Buffer.from('test-png').toString('base64'));
  assert.ok(h.calls.some(call => call.method === 'Runtime.evaluate' && call.params.expression.includes('drawImage')));
  assert.deepEqual(h.nativeChanges, []);
  assert.deepEqual(h.input(), []);
});

for (const [name, change] of [
  ['missing video', state => { state.renderer.video = null; }],
  ['unready video', state => { state.renderer.video.ready = 1; }],
  ['zero-size video', state => { state.renderer.video.intrinsicWidth = 0; }],
  ['canvas context unavailable', state => { state.contextUnavailable = true; }],
  ['canvas read failure', state => { state.canvasError = true; }],
  ['empty image', state => { state.empty = true; }],
]) {
  test(`capture with ${name} returns no frame and invalidates the previous token`, async () => {
    const h = client();
    const previous = await h.capture();
    change(h.state);
    const result = await h.run({ command: 'capture' });
    assert.equal(result.ok, false);
    assert.equal(result.frame, undefined);
    const action = await h.run({ command: 'act', frame: previous, actions: [{ type: 'key', key: 'Escape' }] });
    assert.equal(action.ok, false);
    assert.equal(action.code, 'FRAME_EXPIRED');
    assert.deepEqual(h.input(), []);
  });
}

test('capture rejects a focus transition while the image is being read', async () => {
  const h = client();
  h.hooks.capture = () => { h.state.focused = true; h.window.emit('focus'); };
  const result = await h.run({ command: 'capture' });
  assert.equal(result.ok, false);
  assert.equal(result.frame, undefined);
  assert.deepEqual(h.nativeChanges, []);
});

test('a newer capture invalidates the previous capture token', async () => {
  const h = client();
  const oldFrame = await h.capture();
  await h.capture();
  const result = await h.run({ command: 'act', frame: oldFrame, actions: [{ type: 'key', key: 'Escape' }] });
  assert.equal(result.ok, false);
  assert.equal(result.code, 'FRAME_EXPIRED');
  assert.deepEqual(h.input(), []);
});

for (const [name, change] of [
  ['same-size window movement', state => { state.bounds.x++; }],
  ['window replacement', state => { state.windowId++; }],
  ['web contents replacement', state => { state.contentsId++; }],
  ['renderer navigation', state => { state.renderer.timeOrigin++; }],
  ['renderer zoom', state => { state.zoom = 1.25; }],
  ['pixel ratio', state => { state.renderer.dpr = 1; }],
  ['video dimensions', state => { state.renderer.video.intrinsicWidth = 1920; }],
]) {
  test(`a capture becomes invalid after ${name}`, async () => {
    const h = client();
    const frame = await h.capture();
    change(h.state);
    const result = await h.run({ command: 'act', frame, actions: [{ type: 'key', key: 'Escape' }] });
    assert.equal(result.ok, false);
    assert.equal(result.code, 'FRAME_CHANGED');
    assert.deepEqual(h.input(), []);
  });
}


for (const focused of [false, true]) {
  test(`click preserves ${focused ? 'foreground' : 'background'} focus and converts capture pixels once`, async () => {
    const h = client({ state: { focused } });
    const frame = await h.capture();
    const result = await h.run({ command: 'act', frame, actions: [{ type: 'click', x: 200, y: 160 }] });
    assert.equal(result.ok, true, JSON.stringify(result));
    const mouse = h.input().filter(call => call.method === 'Input.dispatchMouseEvent');
    assert.deepEqual(mouse.map(call => call.params.type), ['mouseMoved', 'mousePressed', 'mouseReleased']);
    assert.ok(mouse.every(call => call.params.x === 200 && call.params.y === 130));
    assert.equal(result.before.focused, focused);
    assert.equal(result.after.focused, focused);
    assert.deepEqual(copy(result.before.cursor), copy(result.after.cursor));
    assert.deepEqual(h.nativeChanges, []);
    assert.notEqual(result.frame.id, frame.id);
    assert.match(result.verification, /dispatched/);
    const reuse = await h.run({ command: 'act', frame, actions: [{ type: 'key', key: 'Escape' }] });
    assert.equal(reuse.ok, false);
  });
}

test('covered and out-of-bounds targets do not send mouse input', async () => {
  for (const covered of [false, true]) {
    const h = client({ state: { covered } });
    const frame = await h.capture();
    const result = await h.run({ command: 'act', frame, actions: [{ type: 'click', x: covered ? 10 : 1280, y: 10 }] });
    assert.equal(result.ok, false);
    assert.deepEqual(h.input(), []);
    assert.equal(h.state.attached, false);
  }
});

test('a focus change during hover prevents mouse down', async () => {
  const h = client();
  const frame = await h.capture();
  h.hooks.send = (method, params) => {
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseMoved') {
      h.state.focused = true;
      h.window.emit('focus');
    }
  };
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'click', x: 20, y: 20 }] });
  assert.equal(result.ok, false);
  assert.deepEqual(h.input().map(call => call.params.type), ['mouseMoved']);
});

test('a focus transition during geometry validation prevents key down', async () => {
  const h = client();
  const frame = await h.capture();
  let reads = 0;
  h.hooks.send = (method, params) => {
    if (method === 'Runtime.evaluate' && params.expression.includes('getBoundingClientRect') && ++reads === 2) {
      h.state.focused = true;
      h.window.emit('focus');
    }
  };
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'key', key: 'Escape' }] });
  assert.equal(result.ok, false);
  assert.deepEqual(h.input(), []);
});

test('mouse release is attempted after a rejected mouse down, without a retry', async () => {
  const h = client();
  const frame = await h.capture();
  h.hooks.send = (method, params) => {
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mousePressed') throw new Error('lost down reply');
  };
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'click', x: 20, y: 20 }] });
  assert.equal(result.ok, false);
  assert.equal(result.outcomeUnknown, true);
  assert.deepEqual(h.input().map(call => call.params.type), ['mouseMoved', 'mousePressed', 'mouseReleased']);
  assert.equal(h.state.attached, false);
});

test('key failure releases the key and held modifiers in reverse order', async () => {
  const h = client();
  const frame = await h.capture();
  h.hooks.send = (method, params) => {
    if (method === 'Input.dispatchKeyEvent' && params.type === 'keyDown' && params.code === 'KeyA') throw new Error('lost key reply');
  };
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'key', key: 'a', modifiers: ['Control', 'Shift'] }] });
  assert.equal(result.ok, false);
  assert.deepEqual(h.input().filter(call => call.params.type === 'keyUp').map(call => call.params.key), ['Unidentified', 'Shift', 'Control']);
  assert.equal(h.state.attached, false);
});

test('a failed cleanup key release does not prevent releasing other held modifiers', async () => {
  const h = client();
  const frame = await h.capture();
  h.hooks.send = (method, params) => {
    if (method !== 'Input.dispatchKeyEvent') return;
    if (params.type === 'keyDown' && params.code === 'KeyA') throw new Error('lost down reply');
    if (params.type === 'keyUp' && params.code === 'KeyA') throw new Error('lost release reply');
  };
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'key', key: 'a', modifiers: ['Control', 'Shift'] }] });
  assert.equal(result.ok, false);
  assert.deepEqual(h.input().filter(call => call.params.type === 'keyUp').map(call => call.params.key), ['Unidentified', 'Shift', 'Control']);
  assert.ok(result.cleanupErrors.some(error => error.includes('lost release reply')));
});

test('an operation remains excluded while a timed-out dispatch is unresolved', async () => {
  const h = client();
  const frame = await h.capture();
  const outstanding = deferred();
  h.hooks.send = (method, params) => {
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mousePressed') return outstanding.promise;
  };
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'click', x: 20, y: 20 }] });
  assert.equal(result.ok, false);
  assert.equal(result.outcomeUnknown, true);
  const retry = await h.run({ command: 'capture' });
  assert.equal(retry.ok, false, 'unresolved prior dispatch must prevent a new capture/action session');
  assert.equal(h.state.attached, true, 'keep the debugger available for final input release');
  outstanding.resolve({});
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(h.input().at(-1).params.type, 'mouseReleased');
  assert.equal(h.state.attached, false);
  assert.equal((await h.run({ command: 'capture' })).ok, true);
});

test('timed-out focus emulation still attempts disable cleanup', async () => {
  const h = client();
  const frame = await h.capture();
  const outstanding = deferred();
  h.hooks.send = (method, params) => {
    if (method === 'Emulation.setFocusEmulationEnabled' && params.enabled) return outstanding.promise;
  };
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'key', key: 'Escape' }] });
  outstanding.resolve({});
  assert.equal(result.ok, false);
  assert.ok(h.calls.some(call => call.method === 'Emulation.setFocusEmulationEnabled' && call.params.enabled === false));
});

test('concurrent runtime calls do not attach a second debugger', async () => {
  const h = client();
  const image = deferred();
  h.hooks.capture = () => image.promise;
  const capture = h.run({ command: 'capture' });
  const second = await h.run({ command: 'status' });
  image.resolve();
  assert.equal(second.ok, false);
  assert.equal(second.code, 'CLIENT_BUSY');
  assert.equal((await capture).ok, true);
  assert.equal(h.calls.filter(call => call.method === 'attach').length, 1);
});

test('a stalled video refuses input before enabling renderer focus', async () => {
  const h = client({ state: { stalled: true } });
  const frame = await h.capture();
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'key', key: 'Escape' }] });
  assert.equal(result.ok, false);
  assert.equal(result.code, 'STREAM_STALLED');
  assert.deepEqual(h.input(), []);
  assert.ok(!h.calls.some(call => call.method === 'Emulation.setFocusEmulationEnabled'));
});

test('ASCII text uses physical key codes and shift for punctuation and capitals', async () => {
  const h = client();
  const frame = await h.capture();
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'text', text: '/A: _\r\n' }] });
  assert.equal(result.ok, true, JSON.stringify(result));
  const keys = h.input().filter(call => call.params.type === 'keyDown' && call.params.key !== 'Shift').map(call => ({ key: call.params.key, code: call.params.code, vk: call.params.windowsVirtualKeyCode, modifiers: call.params.modifiers }));
  assert.deepEqual(keys, [
    { key: '/', code: 'Slash', vk: 191, modifiers: 0 },
    { key: 'A', code: 'KeyA', vk: 65, modifiers: 8 },
    { key: ':', code: 'Semicolon', vk: 186, modifiers: 8 },
    { key: ' ', code: 'Space', vk: 32, modifiers: 0 },
    { key: '_', code: 'Minus', vk: 189, modifiers: 8 },
    { key: 'Enter', code: 'Enter', vk: 13, modifiers: 0 },
  ]);
  assert.deepEqual(h.nativeChanges, []);
});

test('a focus transition after modifier down stops the remaining key downs', async () => {
  const h = client();
  const frame = await h.capture();
  h.hooks.send = (method, params) => {
    if (method === 'Input.dispatchKeyEvent' && params.type === 'keyDown' && params.key === 'Control') {
      h.state.focused = true;
      h.window.emit('focus');
    }
  };
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'key', key: 'a', modifiers: ['Control', 'Shift'] }] });
  assert.equal(result.ok, false);
  assert.deepEqual(h.input().filter(call => call.params.type === 'keyDown').map(call => call.params.key), ['Control']);
  assert.ok(h.input().some(call => call.params.type === 'keyUp' && call.params.key === 'Control'));
});

test('an overlay appearing during hover prevents mouse down', async () => {
  const h = client();
  const frame = await h.capture();
  h.hooks.send = (method, params) => {
    if (method === 'Input.dispatchMouseEvent' && params.type === 'mouseMoved') h.state.covered = true;
  };
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'click', x: 20, y: 20 }] });
  assert.equal(result.ok, false);
  assert.deepEqual(h.input().map(call => call.params.type), ['mouseMoved']);
});

function transport(options = {}) {
  const requests = [], signals = [];
  let listening = options.existingListener || false, releases = 0, sockets = 0;
  class Socket extends EventEmitter {
    static OPEN = 1;
    readyState = 1;
    constructor() {
      super();
      sockets++;
      queueMicrotask(() => this.emit('open'));
    }
    addEventListener(event, listener, settings) { settings?.once ? this.once(event, listener) : this.on(event, listener); }
    send(data) {
      const request = JSON.parse(data);
      requests.push(request);
      queueMicrotask(() => {
        if (request.params.expression === 'test-operation' && options.loseDuringOperation) { this.close(); return; }
        let result;
        if (request.params.expression.startsWith('JSON.stringify({pid:')) {
          result = { result: { value: JSON.stringify({ pid: options.identityMismatch ? 1 : 4321, exe: executable }) } };
        } else if (request.params.expression.includes("getBuiltinModule('inspector').close()")) {
          if (!options.cleanupFails) listening = false;
          result = { result: { value: true } };
        } else result = { result: { value: 'done' } };
        this.emit('message', { data: JSON.stringify({ id: request.id, result }) });
      });
    }
    close() { this.readyState = 3; this.emit('close'); }
  }
  const context = vm.createContext({
    acquireClientLock: async () => () => { releases++; },
    executable,
    execFileSync: name => { assert.equal(name, 'ps'); return options.missingClient ? '' : `4321 ${executable}\n`; },
    process: { kill: (pid, signal) => { signals.push({ pid, signal }); listening = true; } },
    portInUse: () => listening,
    fetch: async () => ({ json: async () => [{ webSocketDebuggerUrl: options.remoteEndpoint ? 'ws://example.com:9229/client' : 'ws://127.0.0.1:9229/client' }] }),
    AbortSignal, URL, WebSocket: Socket,
    setTimeout: (callback, ms) => setTimeout(callback, ms >= 3000 ? 20 : 0),
    clearTimeout, delay: async () => {},
  });
  const run = vm.runInContext(`(${withInspector.toString()})`, context);
  return { run, requests, signals, listening: () => listening, releases: () => releases, sockets: () => sockets };
}

test('transport verifies the installed process before evaluating input and closes its inspector', async () => {
  const h = transport();
  const value = await h.run(rpc => rpc('Runtime.evaluate', { expression: 'test-operation' }));
  assert.equal(value.result.value, 'done');
  assert.deepEqual(h.signals, [{ pid: 4321, signal: 'SIGUSR1' }]);
  assert.ok(h.requests[0].params.expression.startsWith('JSON.stringify({pid:'));
  assert.equal(h.requests[1].params.expression, 'test-operation');
  assert.ok(h.requests[2].params.expression.includes("getBuiltinModule('inspector').close()"));
  assert.equal(h.listening(), false);
  assert.equal(h.releases(), 1);
});

test('transport leaves an existing listener untouched', async () => {
  const h = transport({ existingListener: true });
  await assert.rejects(h.run(() => { assert.fail('operation must not run'); }), /INSPECTOR_BUSY/);
  assert.deepEqual(h.signals, []);
  assert.deepEqual(h.requests, []);
  assert.equal(h.sockets(), 0);
  assert.equal(h.listening(), true);
  assert.equal(h.releases(), 1);
});

test('transport refuses a missing client before sending a signal', async () => {
  const h = transport({ missingClient: true });
  await assert.rejects(h.run(() => { assert.fail('operation must not run'); }), /CLIENT_IDENTITY/);
  assert.deepEqual(h.signals, []);
  assert.equal(h.releases(), 1);
});

test('transport does not close an inspector with an unverified identity', async () => {
  const h = transport({ identityMismatch: true });
  await assert.rejects(h.run(() => { assert.fail('operation must not run'); }), /INSPECTOR_IDENTITY.*CLEANUP_FAILED/);
  assert.equal(h.requests.length, 1);
  assert.ok(!h.requests.some(request => request.params.expression.includes("getBuiltinModule('inspector').close()")));
  assert.equal(h.listening(), true);
  assert.equal(h.releases(), 1);
});

test('transport rejects a non-loopback websocket before connecting', async () => {
  const h = transport({ remoteEndpoint: true });
  await assert.rejects(h.run(() => { assert.fail('operation must not run'); }), /INSPECTOR_IDENTITY/);
  assert.equal(h.sockets(), 0);
  assert.deepEqual(h.requests, []);
  assert.equal(h.releases(), 1);
});

test('transport reports both the operation error and failed cleanup', async () => {
  const h = transport({ cleanupFails: true });
  await assert.rejects(h.run(() => { throw new Error('action result unknown'); }), /action result unknown.*CLEANUP_FAILED.*still open/);
  assert.equal(h.listening(), true);
  assert.equal(h.releases(), 1);
});

test('transport loss rejects a pending action without replay', async () => {
  const h = transport({ loseDuringOperation: true });
  await assert.rejects(h.run(rpc => rpc('Runtime.evaluate', { expression: 'test-operation' })), /INSPECTOR_LOST.*CLEANUP_FAILED/);
  assert.equal(h.requests.filter(request => request.params.expression === 'test-operation').length, 1);
  assert.equal(h.releases(), 1);
});

for (const failedRelease of ['mouse', 'key', 'focus emulation']) {
  test(`an immediate ${failedRelease} release failure blocks new operations`, async () => {
    const h = client();
    const frame = await h.capture();
    h.hooks.send = (method, params) => {
      if ((failedRelease === 'mouse' && method === 'Input.dispatchMouseEvent' && params.type === 'mouseReleased') ||
          (failedRelease === 'key' && method === 'Input.dispatchKeyEvent' && params.type === 'keyUp') ||
          (failedRelease === 'focus emulation' && method === 'Emulation.setFocusEmulationEnabled' && params.enabled === false)) {
        throw new Error(`${failedRelease} release rejected`);
      }
    };
    const actions = failedRelease === 'mouse' ? [{ type: 'click', x: 20, y: 20 }] : [{ type: 'key', key: 'Escape' }];
    const result = await h.run({ command: 'act', frame, actions });
    assert.equal(result.ok, false);
    assert.equal(result.outcomeUnknown, true);
    assert.ok(result.cleanupErrors.some(error => error.includes('release rejected')));
    const callsBeforeRetry = h.calls.length;
    const retry = await h.run({ command: 'capture' });
    assert.equal(retry.ok, false);
    assert.equal(retry.code, 'CLEANUP_UNCONFIRMED');
    assert.equal(h.calls.length, callsBeforeRetry, 'no new debugger session may start after an unconfirmed release');
  });
}

test('late cleanup attempts all input releases even when the first release fails', async () => {
  const h = client();
  const frame = await h.capture();
  const outstanding = deferred();
  let lateCleanup = false;
  h.hooks.send = (method, params) => {
    if (method === 'Input.dispatchKeyEvent' && params.type === 'keyDown' && params.code === 'KeyA') return outstanding.promise;
    if (lateCleanup && method === 'Input.dispatchMouseEvent' && params.type === 'mouseReleased') throw new Error('late mouse release rejected');
    if (lateCleanup && method === 'Input.dispatchKeyEvent' && params.type === 'keyUp' && params.code === 'KeyA') throw new Error('late key release rejected');
  };
  const result = await h.run({ command: 'act', frame, actions: [
    { type: 'click', x: 20, y: 20 },
    { type: 'key', key: 'a', modifiers: ['Control', 'Shift'] },
  ] });
  assert.equal(result.ok, false);
  assert.equal(result.outcomeUnknown, true);
  const start = h.calls.length;
  lateCleanup = true;
  outstanding.resolve({});
  await new Promise(resolve => setTimeout(resolve, 5));
  const cleanup = h.calls.slice(start);
  assert.ok(cleanup.some(call => call.method === 'Input.dispatchMouseEvent' && call.params.type === 'mouseReleased'));
  assert.deepEqual(cleanup.filter(call => call.method === 'Input.dispatchKeyEvent' && call.params.type === 'keyUp').map(call => call.params.key), ['Unidentified', 'Shift', 'Control']);
  assert.ok(cleanup.some(call => call.method === 'Emulation.setFocusEmulationEnabled' && call.params.enabled === false));
  const retry = await h.run({ command: 'capture' });
  assert.equal(retry.ok, false);
  assert.equal(retry.code, 'CLEANUP_UNCONFIRMED');
});

for (const [name, rectangle, expected] of [
  ['vertical letterboxing', { width: 640, height: 480, fit: 'contain' }, { x: 200, y: 190 }],
  ['horizontal letterboxing', { width: 1000, height: 360, fit: 'contain' }, { x: 380, y: 130 }],
]) {
  test(`native video pixels map through ${name}`, async () => {
    const h = client();
    Object.assign(h.state.renderer.video, rectangle);
    const frame = await h.capture();
    const result = await h.run({ command: 'act', frame, actions: [{ type: 'click', x: 200, y: 160 }] });
    assert.equal(result.ok, true, JSON.stringify(result));
    const mouse = h.input().filter(call => call.method === 'Input.dispatchMouseEvent');
    assert.ok(mouse.every(call => call.params.x === expected.x && call.params.y === expected.y));
  });
}

for (const layout of [{ fit: 'fill' }, { fit: 'cover' }, { position: '0% 0%' }]) {
  test(`unsupported video layout ${JSON.stringify(layout)} rejects clicks`, async () => {
    const h = client();
    Object.assign(h.state.renderer.video, layout);
    const frame = await h.capture();
    const result = await h.run({ command: 'act', frame, actions: [{ type: 'click', x: 20, y: 20 }] });
    assert.equal(result.ok, false);
    assert.equal(result.code, 'VIDEO_LAYOUT');
    assert.deepEqual(h.input(), []);
  });
}

for (const modifier of ['Control', 'Meta', 'Alt']) {
  test(`${modifier} shortcuts preserve physical keys without generating localized text`, async () => {
    const h = client();
    const frame = await h.capture();
    const result = await h.run({ command: 'act', frame, actions: [{ type: 'key', key: 'A', modifiers: [modifier] }] });
    assert.equal(result.ok, true, JSON.stringify(result));
    const key = h.input().find(call => call.params.type === 'keyDown' && call.params.code === 'KeyA');
    assert.equal(key.params.key, 'Unidentified');
    assert.equal(key.params.windowsVirtualKeyCode, 65);
    assert.equal(key.params.modifiers, { Control: 2, Meta: 4, Alt: 1 }[modifier]);
    assert.ok(!h.input().some(call => call.params.key === 'Shift'));
  });
}

test('renderer cleanup restores BODY after focusing the remote video', async () => {
  const h = client();
  const frame = await h.capture();
  assert.equal(h.rendererState.activeElement, h.body);
  const result = await h.run({ command: 'act', frame, actions: [{ type: 'key', key: 'Escape' }] });
  assert.equal(result.ok, true, JSON.stringify(result));
  assert.equal(h.rendererState.activeElement, h.body);
  assert.deepEqual(h.focusCalls, ['video.focus', 'video.blur', 'body.focus']);
  assert.deepEqual(h.nativeChanges, []);
});
