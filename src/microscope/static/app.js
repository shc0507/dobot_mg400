(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const base = (document.body.dataset.basePath || '').replace(/\/$/, '');
  const now = () => performance.now();
  const requests = new Map();
  const cameras = ['microscope', 'side'].map(name => ({ name, controller: null, reader: null, url: null, retry: null }));
  const MAX_FRAME = 16 * 1024 * 1024, MAX_HEADER = 4096, FRAME_TIMEOUT = 5000;
  let token = '', epoch = 0, flow = 0, closed = false, active = false;
  let state = null, info = null, discovery = null, stateAt = 0, cursor = 0, stateRevision = 0;
  let mode = null, deadline = 0, transition = false, pending = null;
  let pollTimer = null, renewTimer = null, expiryTimer = null, renewPromise = null, renewPaused = false;
  let client;
  try {
    client = 'microscope-' + [...crypto.getRandomValues(new Uint8Array(16))].map(byte => byte.toString(16).padStart(2, '0')).join('');
  } catch {
    $('notice').textContent = 'A secure browser identity is required.';
    return;
  }
  if (base && (!base.startsWith('/') || /[?#\\%]/.test(base) || base.includes('//') || base.split('/').some(part => part === '..' || part === '.'))) {
    $('notice').textContent = 'Invalid panel base path.';
    return;
  }

  function safe(value) {
    const text = String(value ?? '');
    return (token ? text.split(token).join('[redacted]') : text).slice(0, 1500);
  }
  function notice(message) { $('notice').textContent = safe(message); }
  function live() { return mode !== null && now() < deadline; }
  function idle(value = state) { return value?.connected === true && value.busy === false && value.locked === false && !value.fault; }
  function owns() { return active && mode === 'ordinary' && live() && state?.lease?.holder === client && !state.manual_client; }
  function ready() { return owns() && idle() && !transition && !pending && now() - stateAt < 2500; }
  function guard(version, generation = epoch) {
    if (closed || !active || generation !== epoch || version !== flow) throw new Error('Control flow cancelled.');
  }
  function requireLive(version, generation = epoch) {
    guard(version, generation);
    if (!live()) throw new Error('Control expired. Acquire control explicitly.');
  }
  function cancelControl() {
    flow++;
    mode = null;
    deadline = 0;
    pending = null;
    transition = false;
    renewPaused = true;
    clearTimeout(renewTimer);
    clearTimeout(expiryTimer);
    for (const [controller, mutation] of requests) if (mutation) controller.abort();
  }
  function loseControl(message) {
    cancelControl();
    notice(message);
    render();
  }

  async function api(path, method = 'GET', body, version = null) {
    const generation = epoch, sent = now(), controller = new AbortController();
    if (closed || (version !== null && version !== flow)) throw new Error('Request cancelled.');
    requests.set(controller, method !== 'GET');
    const timeout = setTimeout(() => controller.abort(), 4000);
    const headers = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    const options = { method, headers, cache: 'no-store', redirect: 'error', credentials: 'omit', signal: controller.signal };
    if (method !== 'GET') {
      headers['X-MG400-Client'] = client;
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body ?? {});
    }
    try {
      const response = await fetch(base + path, options);
      const data = await response.json();
      if (controller.signal.aborted || generation !== epoch || closed || (version !== null && version !== flow)) throw new Error('Request cancelled.');
      if (!response.ok) {
        const error = new Error(safe(data.error || `Request failed (${response.status}).`));
        error.status = response.status;
        throw error;
      }
      return { data, sent };
    } finally {
      clearTimeout(timeout);
      requests.delete(controller);
    }
  }

  function render() {
    const fresh = active && state && now() - stateAt < 2500;
    $('connection').textContent = !fresh ? 'State unavailable' : `${state.demo ? 'Demo · ' : ''}${state.connected ? 'Connected' : 'Disconnected'}${state.busy ? ' · Busy' : ''}${state.locked ? ' · Locked' : ''}`;
    $('stop').disabled = !active || closed;
    $('acquire').disabled = !fresh || transition || pending !== null || live() || !idle() || Boolean(state.lease?.holder && state.lease.holder !== client);
    $('release').disabled = !ready();
    $('lease-status').textContent = live() ? (mode === 'manual' ? 'Setting focus zero' : 'This page holds control') : state?.lease?.holder ? `Held by ${safe(state.lease.holder)}` : 'No control';
    $('objective-status').textContent = state?.objective_id == null ? 'Objective unknown' : `Objective ${state.objective_id}`;
    $('focus-status').textContent = state?.focus_zero_known ? `${state.focus_position_pulses} pulses · Limits ${state.focus_limits_pulses?.join(' to ')}` : 'Position unknown. Establish focus zero before motion.';
    const motion = ready() && state.focus_zero_known;
    for (const button of $('objectives').children) button.disabled = !motion || !info?.calibration_available;
    $('focus-full').disabled = $('focus-local').disabled = !motion;
    $('focus-minus').disabled = !motion || !validJog(-1);
    $('focus-plus').disabled = !motion || !validJog(1);
    $('home').hidden = info?.home_switch !== true;
    $('zero').hidden = info?.home_switch !== false;
    $('home').disabled = $('zero').disabled = !ready();
    $('snapshot').disabled = !ready() || state?.cameras?.microscope !== true;
    if (state?.fault) notice(state.fault.message || 'The service is locked. Only stop is available.');
  }
  function validJog(direction) {
    const raw = $('focus-step').value;
    const amount = Number(raw), position = state?.focus_position_pulses, limits = state?.focus_limits_pulses;
    return /^\d+$/.test(raw) && Number.isSafeInteger(amount) && amount > 0 && amount <= 2147483647 &&
      Number.isSafeInteger(position) && Array.isArray(limits) && limits.length === 2 && limits.every(Number.isSafeInteger) &&
      position + direction * amount >= limits[0] && position + direction * amount <= limits[1];
  }
  function applyState(value, sampled = now()) {
    if (!value || typeof value !== 'object' || !value.lease) throw new Error('Invalid service state.');
    state = value;
    stateAt = sampled;
    if (mode && (value.lease.holder !== client || !(value.lease.remaining_s > 0) ||
      (mode === 'ordinary' ? Boolean(value.manual_client) : value.manual_client !== client))) {
      loseControl('Control was lost. Check the state before acquiring control again.');
    }
    if (mode && (!value.connected || value.locked || value.fault)) loseControl('The service cannot accept control. Only stop is available.');
    if (pending && value.last_command?.id === pending.id) {
      const result = value.last_command;
      $('command-status').textContent = `${safe(result.action)}: ${safe(result.result)}${result.message ? ' · ' + safe(result.message) : ''}`;
      pending = null;
      if (result.result !== 'ok') loseControl('The command did not complete successfully. Check the state before acquiring control.');
    }
    if (pending && now() - pending.started > 120000) loseControl('Command completion is unknown. No command will be retried.');
    render();
    syncPreviews();
  }

  function scheduleLease(lease, sent, version) {
    guard(version);
    if (lease?.holder !== client || !Number.isFinite(lease.remaining_s) || lease.remaining_s <= 0) throw new Error('No active control lease.');
    const next = sent + lease.remaining_s * 1000;
    if (now() >= next) throw new Error('The lease response arrived after its deadline.');
    deadline = next;
    clearTimeout(expiryTimer);
    expiryTimer = setTimeout(() => {
      if (version === flow && now() >= deadline) loseControl('Control expired. Acquire control explicitly.');
    }, deadline - now());
    clearTimeout(renewTimer);
    if (!renewPaused) renewTimer = setTimeout(() => renew(version), Math.max(20, Math.min(1000, (deadline - now()) / 3)));
  }
  async function renew(version) {
    if (renewPaused || renewPromise || version !== flow) return;
    const generation = epoch;
    renewPromise = (async () => {
      requireLive(version, generation);
      const { data, sent } = await api(mode === 'manual' ? '/api/manual/renew' : '/api/renew', 'POST', {}, version);
      requireLive(version, generation);
      scheduleLease(data.lease, sent, version);
    })();
    try { await renewPromise; }
    catch (error) { if (generation === epoch && version === flow) loseControl(`Control renewal failed. ${error.message}`); }
    finally { renewPromise = null; }
  }
  async function pauseRenew(version) {
    renewPaused = true;
    clearTimeout(renewTimer);
    if (renewPromise) await renewPromise;
    requireLive(version);
  }
  async function acquire() {
    if ($('acquire').disabled) return;
    cancelControl();
    const version = flow, generation = epoch;
    transition = true;
    stateRevision++;
    render();
    try {
      const { data, sent } = await api('/api/heartbeat', 'POST', {}, version);
      guard(version, generation);
      mode = 'ordinary';
      renewPaused = false;
      scheduleLease(data.lease, sent, version);
      const fresh = await api('/api/state', 'GET', undefined, version);
      requireLive(version, generation);
      applyState(fresh.data, fresh.sent);
      guard(version, generation);
      transition = false;
      notice('Control acquired.');
      render();
    } catch (error) { if (generation === epoch && version === flow) loseControl(`Control was not acquired. ${error.message}`); }
  }
  async function release() {
    if (!ready()) return;
    const version = flow, generation = epoch;
    transition = true;
    stateRevision++;
    render();
    try {
      await pauseRenew(version);
      await api('/api/release', 'POST', {}, version);
      guard(version, generation);
      cancelControl();
      notice('Control released.');
      render();
    } catch (error) { if (generation === epoch && version === flow) loseControl(`Release outcome is unknown. ${error.message}`); }
  }
  async function command(action, args = {}) {
    if (!ready() || !discovery?.commands?.[action]) return;
    const version = flow, generation = epoch;
    transition = true;
    stateRevision++;
    render();
    try {
      requireLive(version, generation);
      const { data, sent } = await api('/api/command', 'POST', { action, ...args }, version);
      requireLive(version, generation);
      if (typeof data.command_id !== 'string' || !data.command_id) throw new Error('No command receipt.');
      pending = { id: data.command_id, started: now() };
      $('command-status').textContent = `${action}: waiting for completion`;
      transition = false;
      applyState(data.state, sent);
    } catch (error) { if (generation === epoch && version === flow) loseControl(`Command outcome is unknown. No retry was sent. ${error.message}`); }
    render();
  }

  async function setZero() {
    if (!ready() || info?.home_switch !== false || !window.confirm('Confirm the focus is at the known zero position. Check physical clearance and keep the work area clear. Set focus zero?')) return;
    const version = flow, generation = epoch;
    transition = true;
    stateRevision++;
    render();
    try {
      await pauseRenew(version);
      const fresh = (await api('/api/state', 'GET', undefined, version)).data;
      requireLive(version, generation);
      if (!idle(fresh) || fresh.lease?.holder !== client || fresh.manual_client) throw new Error('Focus zero requires idle operator control.');
      const handoff = deadline;
      await api('/api/release', 'POST', {}, version);
      requireLive(version, generation);
      while (true) {
        const released = (await api('/api/state', 'GET', undefined, version)).data;
        requireLive(version, generation);
        if (!released.connected || released.locked || released.fault || released.lease?.holder || now() >= handoff) throw new Error('The manual handoff is no longer available.');
        if (idle(released)) break;
        await new Promise(resolve => setTimeout(resolve, 100));
        requireLive(version, generation);
      }
      const manual = await api('/api/manual/heartbeat', 'POST', {}, version);
      requireLive(version, generation);
      mode = 'manual';
      renewPaused = false;
      scheduleLease(manual.data.lease, manual.sent, version);
      const accepted = await api('/api/manual/set_focus_zero', 'POST', { confirm: true }, version);
      requireLive(version, generation);
      const id = accepted.data.command_id;
      if (typeof id !== 'string' || !id) throw new Error('No focus-zero receipt.');
      $('command-status').textContent = 'set_focus_zero: waiting for completion';
      const until = now() + 20000;
      let current = accepted.data.state;
      while (true) {
        requireLive(version, generation);
        if (!current?.connected || current.locked || current.fault || current.lease?.holder !== client || current.manual_client !== client) throw new Error('Manual control was lost.');
        if (current.last_command?.id === id) {
          if (current.last_command.result !== 'ok' || !current.focus_zero_known || !idle(current)) throw new Error('Focus zero did not complete successfully.');
          break;
        }
        if (now() >= until) throw new Error('Focus-zero completion is unknown.');
        await new Promise(resolve => setTimeout(resolve, 150));
        requireLive(version, generation);
        current = (await api('/api/state', 'GET', undefined, version)).data;
      }
      await pauseRenew(version);
      const manualDeadline = deadline;
      await api('/api/manual/release', 'POST', {}, version);
      requireLive(version, generation);
      if (now() >= manualDeadline) throw new Error('The ordinary-control handoff expired.');
      const ordinary = await api('/api/heartbeat', 'POST', {}, version);
      requireLive(version, generation);
      mode = 'ordinary';
      renewPaused = false;
      scheduleLease(ordinary.data.lease, ordinary.sent, version);
      const final = await api('/api/state', 'GET', undefined, version);
      requireLive(version, generation);
      applyState(final.data, final.sent);
      guard(version, generation);
      transition = false;
      $('command-status').textContent = 'set_focus_zero: ok';
      notice('Focus zero established. This page holds ordinary control.');
      render();
    } catch (error) { if (generation === epoch && version === flow) loseControl(`Focus-zero flow stopped. No retry was sent. ${error.message}`); }
  }
  async function stop() {
    if (!active || closed) return;
    cancelControl();
    const version = flow, generation = epoch;
    transition = true;
    stateRevision++;
    render();
    try {
      const { data, sent } = await api('/api/stop', 'POST', { wait_s: 0 }, version);
      guard(version, generation);
      transition = false;
      applyState(data.state, sent);
      notice(`Stop ${data.stop}.`);
    } catch (error) {
      if (generation === epoch && version === flow) {
        transition = false;
        notice(`Stop outcome is unknown. ${error.message}`);
        render();
      }
    }
  }

  async function poll(generation) {
    if (!active || closed || generation !== epoch) return;
    const version = flow, revision = stateRevision;
    try {
      if (!transition) {
        const value = await api('/api/state');
        if (generation !== epoch) return;
        if (version === flow && revision === stateRevision && !transition) applyState(value.data, value.sent);
        const events = (await api(`/api/events?since=${cursor}`)).data;
        if (generation !== epoch) return;
        if (Number.isSafeInteger(events.next) && events.next >= cursor) cursor = events.next;
        for (const event of (Array.isArray(events.events) ? events.events : []).slice(-50)) {
          const item = document.createElement('li');
          item.textContent = `${safe(event.level)} · ${safe(event.message)}`;
          $('events').append(item);
        }
        while ($('events').children.length > 50) $('events').firstElementChild.remove();
      }
    } catch (error) {
      if (generation !== epoch) return;
      loseControl(`State unavailable. ${error.message}`);
      state = null;
      if (error.status === 401 || error.status === 403) {
        disconnect();
        notice('Access denied. Enter a valid service token and connect.');
      }
      render();
    } finally {
      if (active && generation === epoch) pollTimer = setTimeout(() => poll(generation), 750);
    }
  }
  function disconnect() {
    epoch++;
    active = false;
    cancelControl();
    clearTimeout(pollTimer);
    for (const controller of requests.keys()) controller.abort();
    for (const camera of cameras) stopPreview(camera);
    state = info = discovery = null;
    cursor = 0;
    $('objectives').replaceChildren();
    $('events').replaceChildren();
    render();
  }
  async function connect() {
    disconnect();
    token = $('access-token').value;
    const generation = epoch;
    notice('Connecting…');
    try {
      info = (await api('/api/panel-info')).data;
      discovery = (await api('/api/describe')).data;
      if (generation !== epoch) return;
      if (discovery.contract !== 1 || discovery.renew_only_heartbeat !== true) throw new Error('The service does not support safe lease renewal.');
      if (!Array.isArray(info.objective_ids) || info.objective_ids.length > 32 || !info.objective_ids.every(Number.isSafeInteger) || typeof info.home_switch !== 'boolean') throw new Error('Invalid panel configuration.');
      for (const id of info.objective_ids) {
        const button = document.createElement('button');
        button.type = 'button';
        button.textContent = `Objective ${id}`;
        button.disabled = true;
        button.addEventListener('click', () => { if (ready() && state.focus_zero_known && info.calibration_available) command('select_objective', { id }); });
        $('objectives').append(button);
      }
      $('calibration-status').textContent = info.calibration_available ? '' : safe(info.calibration_error || 'Objective calibration is unavailable.');
      active = true;
      notice('Connected. Acquire control to use the controls.');
      await poll(generation);
    } catch (error) {
      if (generation !== epoch) return;
      disconnect();
      notice(`Connection failed. ${error.message}`);
    }
  }

  function stopPreview(camera) {
    clearTimeout(camera.retry);
    camera.retry = null;
    camera.controller?.abort();
    camera.controller = null;
    camera.reader?.cancel().catch(() => {});
    camera.reader = null;
    if (camera.url) URL.revokeObjectURL(camera.url);
    camera.url = null;
    $(camera.name + '-image').removeAttribute('src');
    $(camera.name + '-status').textContent = 'No live frame';
  }
  function syncPreviews() {
    for (const camera of cameras) {
      if (!active || closed || document.hidden || state?.cameras?.[camera.name] !== true) stopPreview(camera);
      else if (!camera.controller && !camera.retry) preview(camera);
    }
  }
  async function preview(camera) {
    const generation = epoch, controller = new AbortController();
    camera.controller = controller;
    let reader = null, timer, buffer = new Uint8Array(), expected = null, metadata;
    const current = () => generation === epoch && camera.controller === controller && !controller.signal.aborted && active && !closed && !document.hidden;
    const arm = () => { clearTimeout(timer); timer = setTimeout(() => controller.abort(), FRAME_TIMEOUT); };
    arm();
    try {
      const headers = token ? { Authorization: `Bearer ${token}` } : {};
      const response = await fetch(`${base}/api/camera/${camera.name}/mjpeg`, { headers, cache: 'no-store', redirect: 'error', credentials: 'omit', signal: controller.signal });
      if (!current()) { response.body?.cancel().catch(() => {}); return; }
      if (!response.ok || !/^multipart\/x-mixed-replace;\s*boundary=frame$/i.test(response.headers.get('Content-Type') || '')) throw new Error('Camera stream unavailable.');
      reader = response.body.getReader();
      camera.reader = reader;
      while (current()) {
        const chunk = await reader.read();
        if (!current()) break;
        if (chunk.done) throw new Error('Camera stream ended.');
        if (!(chunk.value instanceof Uint8Array) || buffer.length + chunk.value.length > MAX_FRAME + MAX_HEADER + 2) throw new Error('Camera frame exceeds the buffer limit.');
        const combined = new Uint8Array(buffer.length + chunk.value.length);
        combined.set(buffer);
        combined.set(chunk.value, buffer.length);
        buffer = combined;
        while (current()) {
          if (expected === null) {
            let end = -1;
            for (let i = 0; i + 3 < Math.min(buffer.length, MAX_HEADER + 4); i++) if (buffer[i] === 13 && buffer[i + 1] === 10 && buffer[i + 2] === 13 && buffer[i + 3] === 10) { end = i; break; }
            if (end < 0) { if (buffer.length > MAX_HEADER) throw new Error('Camera header is too large.'); break; }
            const lines = new TextDecoder('ascii', { fatal: true }).decode(buffer.slice(0, end)).split('\r\n');
            if (lines.shift() !== '--frame') throw new Error('Invalid camera boundary.');
            const fields = new Map();
            for (const line of lines) {
              const at = line.indexOf(':');
              if (at < 1) throw new Error('Invalid camera header.');
              const key = line.slice(0, at).toLowerCase(), value = line.slice(at + 1).trim();
              if (fields.has(key)) throw new Error('Duplicate camera header.');
              fields.set(key, value);
            }
            const length = fields.get('content-length'), frameId = fields.get('x-frame-id'), captured = fields.get('x-captured-at');
            if (fields.get('content-type') !== 'image/jpeg' || !/^\d+$/.test(length || '') || !/^\d+$/.test(frameId || '') || !Number.isSafeInteger(Number(frameId)) || !captured || !Number.isFinite(Number(captured)) || Number(captured) < 0) throw new Error('Invalid camera frame metadata.');
            expected = Number(length);
            if (!Number.isSafeInteger(expected) || expected < 4 || expected > MAX_FRAME) throw new Error('Invalid camera frame length.');
            metadata = `Frame ${frameId} · Captured ${captured}`;
            buffer = buffer.slice(end + 4);
          }
          if (buffer.length < expected + 2) break;
          if (buffer[0] !== 255 || buffer[1] !== 216 || buffer[expected - 2] !== 255 || buffer[expected - 1] !== 217 || buffer[expected] !== 13 || buffer[expected + 1] !== 10) throw new Error('Invalid JPEG frame.');
          const url = URL.createObjectURL(new Blob([buffer.slice(0, expected)], { type: 'image/jpeg' }));
          $(camera.name + '-image').src = url;
          if (camera.url) URL.revokeObjectURL(camera.url);
          camera.url = url;
          $(camera.name + '-status').textContent = metadata;
          buffer = buffer.slice(expected + 2);
          expected = null;
          arm();
        }
      }
    } catch {
      // Camera errors do not change instrument authority or retry a command.
    } finally {
      clearTimeout(timer);
      controller.abort();
      reader?.cancel().catch(() => {});
      if (camera.controller === controller) {
        stopPreview(camera);
        if (generation === epoch && active && !closed && !document.hidden && state?.cameras?.[camera.name] === true) {
          camera.retry = setTimeout(() => { camera.retry = null; syncPreviews(); }, 2000);
        }
      }
    }
  }

  $('access-form').addEventListener('submit', event => { event.preventDefault(); connect(); });
  $('access-token').addEventListener('input', () => { disconnect(); token = ''; notice('Connect to use the new token.'); });
  $('acquire').addEventListener('click', acquire);
  $('release').addEventListener('click', release);
  $('stop').addEventListener('click', stop);
  $('zero').addEventListener('click', setZero);
  $('home').addEventListener('click', () => { if (ready() && info?.home_switch === true && window.confirm('Confirm the focus travel path is clear. Home focus?')) command('home_focus'); });
  $('focus-step').addEventListener('input', render);
  for (const [id, direction] of [['focus-minus', -1], ['focus-plus', 1]]) $(id).addEventListener('click', () => { if (validJog(direction) && state?.focus_zero_known) command('focus_move', { steps: direction * Number($('focus-step').value) }); });
  $('focus-full').addEventListener('click', () => { if (state?.focus_zero_known) command('autofocus', { mode: 'full' }); });
  $('focus-local').addEventListener('click', () => { if (state?.focus_zero_known) command('autofocus', { mode: 'local' }); });
  $('snapshot').addEventListener('click', () => {
    const path = $('snapshot-path').value;
    if (path && (path.length > 4096 || path.startsWith('/') || path.includes('\\') || path.split('/').some(part => !part || part === '.' || part === '..') || /[\x00-\x1f\x7f]/.test(path))) { notice('Use a relative run path without empty or traversal components.'); return; }
    if (state?.cameras?.microscope) command('snapshot', path ? { path } : {});
  });
  document.addEventListener('visibilitychange', syncPreviews);
  window.addEventListener('pagehide', () => { closed = true; disconnect(); token = ''; $('access-token').value = ''; });
  connect();
})();
