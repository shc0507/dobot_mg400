const $ = (selector, root = document) => root.querySelector(selector);
const cards = [...document.querySelectorAll('[data-motor]')];
const keyButtons = [...document.querySelectorAll('[data-key]')];
const shortcutButtons = new Map(keyButtons.map(button => [button.dataset.key, button]));
const token = $('meta[name="control-token"]').content;
let accessToken = '';
try { accessToken = sessionStorage.getItem('stage-panel-token') || ''; } catch { /* Ask for it. */ }
let authWait = false;
let tokenSubmitted = false;
let keyboardEnabled = false;
let state = null;
let online = false;
let connectionPending = false;
let stopPending = false;
let microstepPending = false;
let microstepDirty = false;
let referencePending = false;
let poseButtons = [];
const positionButtons = new Map();
let lastEvent = -1;
let toastTimer;
let pollTimer;
let polling = false;
const pendingMotors = new Set();
const holds = new Map();

async function api(path, data, extra = {}) {
  const sent = accessToken;
  const headers = sent ? { Authorization: `Bearer ${sent}` } : {};
  const options = { signal: AbortSignal.timeout(12000), cache: 'no-store', ...extra, headers };
  if (data !== undefined) {
    options.method = 'POST';
    Object.assign(headers, { 'Content-Type': 'application/json', 'X-Control-Token': token });
    options.body = JSON.stringify(data);
  }
  const response = await fetch(`/api${path}`, options);
  const result = await response.json();
  if (response.status === 401) {
    requireToken(sent);
    throw Object.assign(new Error(result.error || 'Enter the panel access token'), { auth: true });
  }
  if (sent === accessToken) tokenAccepted();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}

function requireToken(sent) {
  // A reply to an older token must not discard the token the user just entered.
  if (sent !== accessToken) return;
  accessToken = '';
  tokenSubmitted = false;
  try { sessionStorage.removeItem('stage-panel-token'); } catch { /* Nothing stored. */ }
  authWait = true;
  $('#auth').hidden = false;
}

function tokenAccepted() {
  authWait = false;
  $('#auth').hidden = true;
  if (!tokenSubmitted) return;
  tokenSubmitted = false;
  $('#access-token').value = '';
  try { sessionStorage.setItem('stage-panel-token', accessToken); } catch { /* Ask again next time. */ }
}

function showError(message) {
  $('#notice').textContent = message;
  $('#notice').hidden = false;
}

function clearError() { $('#notice').hidden = true; }

function toast(message) {
  clearTimeout(toastTimer);
  $('#toast').textContent = message;
  $('#toast').hidden = false;
  toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 4000);
}

function syncThemeButton() {
  const dark = document.documentElement.dataset.theme === 'dark';
  $('#theme-toggle').setAttribute('aria-pressed', String(dark));
  $('#theme-toggle').setAttribute('aria-label', `Switch to ${dark ? 'light' : 'dark'} mode`);
  $('#theme-label').textContent = dark ? 'Light mode' : 'Dark mode';
}
$('#theme-toggle').addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('stage-theme', theme); } catch { /* Optional preference. */ }
  syncThemeButton();
});
syncThemeButton();

function sliderFill(card) {
  const slider = $('.speed-slider', card);
  slider.style.setProperty('--fill', `${(slider.value - 1) / (slider.max - 1) * 100}%`);
}

function holdButtonsFor(card) {
  return [...card.querySelectorAll('[data-hold-direction]'),
    ...keyButtons.filter(button => button.dataset.keyMotor === card.dataset.motor)];
}

function isEditing(target) {
  return target instanceof Element && !!target.closest(
    'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="textbox"]',
  );
}

function automationControlled() {
  return state?.lease?.holder != null;
}

function motionBlocked() {
  return automationControlled() || state?.locked === true || referencePending
    || !!state?.automation?.active;
}

function namedButtons(container, names, previous, activate) {
  if (names.length === previous.length
      && names.every((name, index) => name === previous[index].dataset.name)) return previous;
  const buttons = names.map(name => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'named-button';
    button.dataset.name = name;
    button.textContent = name;
    button.disabled = true;
    button.addEventListener('click', () => activate(button));
    return button;
  });
  container.replaceChildren(...buttons);
  return buttons;
}

async function referenceCommand(button, path, data) {
  if (motionBlocked() || button.disabled) return;
  referencePending = true;
  clearError();
  refreshControls();
  try { render(await api(path, data)); }
  catch (error) {
    showError(`${error.message} Check motor status before retrying.`);
    // A lost reply can mean that motion started. Never retry automatically.
  } finally { referencePending = false; refreshControls(); }
}

function refreshControls() {
  const connected = online && state?.connected;
  const automation = automationControlled();
  const blocked = motionBlocked();
  $('#automation-banner').hidden = !automation;
  $('#keyboard-toggle').setAttribute('aria-checked', String(keyboardEnabled));
  $('#keyboard-toggle-label').textContent = keyboardEnabled ? 'On' : 'Off';
  $('#keyboard-panel').classList.toggle('is-enabled', keyboardEnabled);
  $('#keyboard-status').textContent = automation ? 'Paused while automation controls the stage.'
    : state?.locked ? 'Paused until the stage fault is resolved.'
    : referencePending || state?.automation?.active ? 'Paused during homing or a named move.'
    : !keyboardEnabled ? 'Off. Enable to use the keys or buttons.'
    : !connected ? 'On. Connect a device to use these controls.'
      : 'On. Hold W / A / S / D or J / K to move. Release to stop.';
  $('#serial-port').disabled = connected || connectionPending;
  $('#refresh-ports').disabled = connected || connectionPending;
  $('#connect').disabled = connectionPending || microstepPending || !online || (!connected && !$('#serial-port').value);
  $('#connect span').textContent = connectionPending ? 'Please wait…' : connected ? 'Disconnect' : 'Connect';
  $('#stop-all').disabled = !connected || stopPending || connectionPending;
  const moving = state?.motors.some(motor => motor.mode !== 'idle');
  const referenceBlocked = blocked || !connected || moving || pendingMotors.size > 0
    || holds.size > 0 || connectionPending || stopPending || microstepPending;
  const active = state?.automation?.active;
  $('#pose-feedback').textContent = !connected ? 'Connect to use named poses.'
    : active ? `In progress: ${active.action}${active.pose ? ` · ${active.pose}` : ''}`
    : state?.automation?.error ? state.automation.error
    : !state?.automation?.configured ? 'Stage calibration is unavailable.'
    : !state.motors.every(motor => motor.automation_ready === true) ? 'Home all axes before a named pose.'
    : poseButtons.length ? 'Moves one axis at a time in the configured order.' : 'No named poses configured.';
  for (const button of poseButtons) {
    button.disabled = referenceBlocked || state?.motors.length !== 3
      || !state.motors.every(motor => motor.automation_ready === true && motor.available);
  }
  const supported = connected && state.microstep !== null;
  const modeBlocked = blocked || !supported || moving || pendingMotors.size > 0 || holds.size > 0
    || connectionPending || stopPending || microstepPending;
  $('#microstep-mode').disabled = modeBlocked;
  $('#apply-microstep').disabled = modeBlocked || Number($('#microstep-mode').value) === state?.microstep;
  $('#apply-microstep').textContent = microstepPending ? 'Applying…' : 'Apply to all';
  $('#microstep-feedback').textContent = automation ? 'Stage is controlled by automation.'
    : state?.locked ? 'Resolve the stage fault before changing the mode.'
    : !connected ? 'Connect your device to change the mode.'
    : !supported ? 'Upload the updated Arduino sketch to enable stepping modes.'
      : microstepPending ? 'Applying the mode to all three motors…'
        : moving || holds.size > 0 || pendingMotors.size > 0 ? 'Stop all motors before changing the mode.'
          : 'All motors are idle. Mode changes apply to all three.';
  for (const card of cards) {
    const motor = state?.motors.find(item => item.id === card.dataset.motor);
    const hold = holds.get(card.dataset.motor);
    const busy = pendingMotors.has(card.dataset.motor) || !!hold;
    const speedValid = $('.speed-number', card).checkValidity();
    const stepsValid = $('.steps-number', card).checkValidity();
    const valid = speedValid && stepsValid;
    $('.home-button', card).disabled = referenceBlocked || !motor?.available
      || motor?.homing_available !== true;
    for (const button of positionButtons.get(card.dataset.motor) || []) {
      button.disabled = referenceBlocked || !motor?.available || motor?.automation_ready !== true;
    }
    const feedback = $('.input-error', card);
    feedback.hidden = valid;
    feedback.textContent = !speedValid ? 'Enter a whole speed from 1 to 1,000 pulses/s.'
      : !stepsValid ? 'Enter a whole step count from 1 to 100,000.' : '';
    for (const button of card.querySelectorAll('[data-direction]')) {
      button.disabled = blocked || !connected || !motor?.available || !valid || busy
        || connectionPending || stopPending || microstepPending;
    }
    for (const button of holdButtonsFor(card)) {
      // Keep the held button enabled so it continues receiving release/key events.
      const pressed = hold?.button === button && !hold.released;
      button.disabled = blocked || (!!button.dataset.key && !keyboardEnabled)
        || (!pressed && (!connected || !motor?.available || !speedValid || busy
          || connectionPending || stopPending || microstepPending));
    }
    $('.speed-number', card).disabled = !!hold || motor?.mode === 'continuous';
    $('.speed-slider', card).disabled = !!hold || motor?.mode === 'continuous';
  }
}

function finishHold(hold) {
  if (hold.starting || !hold.stopped || holds.get(hold.motor) !== hold) return;
  holds.delete(hold.motor);
  refreshControls();
}

function releaseHold(hold) {
  if (hold.released) return hold.stopPromise;
  hold.released = true;
  clearTimeout(hold.timer);
  hold.button.classList.remove('is-held');
  hold.button.setAttribute('aria-pressed', 'false');
  if (hold.pointerId !== undefined && hold.button.hasPointerCapture(hold.pointerId)) {
    hold.button.releasePointerCapture(hold.pointerId);
  }
  // Send immediately, including before a pending start reply. The backend cancels
  // this hold ID even if the stop request arrives before the start request.
  hold.stopPromise = api(`/motors/${hold.motor}/hold/stop`, { hold_id: hold.id }, { keepalive: true })
    .then(render)
    .catch(error => showError(`Could not confirm release: ${error.message}`))
    .finally(() => { hold.stopped = true; finishHold(hold); });
  refreshControls();
  return hold.stopPromise;
}

function releaseAllHolds() {
  return Promise.all([...holds.values()].map(releaseHold));
}

function renewHold(hold) {
  if (hold.released) return;
  hold.timer = setTimeout(async () => {
    if (hold.released) return;
    try {
      const next = await api(`/motors/${hold.motor}/hold/keepalive`, { hold_id: hold.id });
      render(next);
      renewHold(hold);
    } catch (error) {
      if (!hold.released) showError(`${error.message} Release and press again.`);
      releaseHold(hold);
    }
  }, 200);
}

// The UUID helper of crypto exists only in secure contexts, and the Olares panel is plain HTTP.
function newHoldId() {
  return [...crypto.getRandomValues(new Uint8Array(16))]
    .map(byte => byte.toString(16).padStart(2, '0')).join('');
}

function beginHold(card, button, input) {
  const motor = card.dataset.motor;
  const number = $('.speed-number', card);
  if (motionBlocked() || button.disabled || holds.has(motor) || !number.reportValidity()) return;
  const hold = {
    motor, button, id: newHoldId(), starting: true, released: false, stopped: false, ...input,
  };
  holds.set(motor, hold);
  button.classList.add('is-held');
  button.setAttribute('aria-pressed', 'true');
  clearError();
  refreshControls();
  api(`/motors/${motor}/hold`, {
    direction: button.dataset.holdDirection, speed: Number(number.value),
    hold_id: hold.id, sampled_at: state.sampled_at,
  }).then(next => {
    render(next);
    if (!hold.released) renewHold(hold);
  }).catch(error => {
    if (!hold.released) showError(error.message);
    releaseHold(hold);
  }).finally(() => {
    hold.starting = false;
    finishHold(hold);
  });
  return hold;
}

function render(next) {
  // Polls and command responses can arrive out of order over separate requests.
  if (state && next.sampled_at < state.sampled_at) return;
  const modeChanged = state?.microstep !== next.microstep;
  state = next;
  const connected = online && next.connected;
  const activeMode = connected ? next.microstep_modes.find(mode => mode.value === next.microstep) : null;
  if (!microstepDirty || modeChanged || !connected || !activeMode) {
    $('#microstep-mode').value = String(activeMode?.value ?? 1);
    microstepDirty = false;
  }
  $('#microstep-active').textContent = activeMode ? `Active: ${activeMode.label}`
    : connected ? 'Stepping mode unavailable on this firmware' : 'Connect to read the active mode';
  $('#microstep-badge').textContent = activeMode
    ? next.microstep === 1 ? 'Full step' : `1/${next.microstep} step`
    : 'Mode unavailable';
  if (!connected || motionBlocked()) releaseAllHolds();
  $('#connection-status').classList.toggle('connected', connected);
  $('#connection-status span:last-child').textContent = !online
    ? authWait ? 'Access token required' : 'Server unavailable' : connected
    ? `${next.port} · ${next.demo ? 'Demo connected' : 'Arduino connected'}` : 'Not connected';
  if (connected && $('#serial-port').value !== next.port) {
    // A second tab may have connected a board while this tab had an old list.
    if (![...$('#serial-port').options].some(option => option.value === next.port)) {
      $('#serial-port').add(new Option(next.port, next.port));
    }
    $('#serial-port').value = next.port;
  }
  for (const motor of next.motors) {
    const card = $(`#motor-${motor.id}`);
    const moving = connected && motor.mode !== 'idle';
    const cooling = connected && !moving && motor.cooldown > 0;
    const homing = connected && next.automation?.active?.action === 'home'
      && next.automation.active.motor === motor.id;
    card.classList.toggle('moving', moving);
    card.classList.toggle('ready', connected && motor.available);
    card.classList.toggle('reverse', motor.direction === 'ccw');
    $('.position', card).textContent = connected ? motor.position.toLocaleString() : '—';
    $('.remaining', card).textContent = !connected ? 'Connect to read count' : homing
      ? 'Finding home and confirming soft limits' : moving
      ? motor.mode === 'continuous' ? 'Continuous rotation' : `${Math.abs(motor.remaining).toLocaleString()} pulses remaining`
      : 'At rest';
    $('.motor-status', card).textContent = !connected ? 'Offline' : moving ? 'Moving' : cooling ? 'Cooldown' : 'Ready';
    const badge = $('.homing-status', card);
    badge.textContent = !connected ? 'Homing unavailable' : homing ? 'Homing…'
      : motor.homing_available !== true ? 'Homing unavailable'
      : motor.automation_ready === true ? 'Homed' : 'Home required';
    badge.classList.toggle('is-homed', connected && motor.automation_ready === true);
    $('.position-mm', card).textContent = connected && Number.isFinite(motor.position_mm)
      ? `${motor.position_mm.toLocaleString(undefined, {maximumFractionDigits: 3})} mm from home` : '';
    if (motor.id === 'insert') {
      positionButtons.set(motor.id, namedButtons($('.named-positions', card),
        motor.named_positions || [], positionButtons.get(motor.id) || [], button =>
          referenceCommand(button, `/motors/${motor.id}/goto`, {name: button.dataset.name})));
    }
    const directionLabel = $(`[data-direction="${motor.direction}"] span`, card).textContent;
    $('.motor-hint', card).textContent = !connected ? 'Connect a device to begin' : moving
      ? `Moving ${directionLabel} · ${motor.speed.toLocaleString()} pulses/s`
      : cooling ? `Ready in ${motor.cooldown.toFixed(1)}s` : 'Ready for your next move';
  }
  poseButtons = namedButtons($('#pose-buttons'), next.automation?.poses || [], poseButtons,
    button => referenceCommand(button, `/poses/${encodeURIComponent(button.dataset.name)}`, {}));
  const newest = next.events[0]?.id ?? 0;
  if (newest !== lastEvent && next.events.length) {
    lastEvent = newest;
    $('#activity-log').replaceChildren(...next.events.map(event => {
      const row = document.createElement('li');
      if (event.level === 'error') row.className = 'log-error';
      const timestamp = document.createElement('time');
      timestamp.dateTime = event.time;
      timestamp.textContent = new Date(event.time).toLocaleTimeString([], { hour12: false });
      const message = document.createElement('span');
      message.textContent = event.message;
      row.append(timestamp, message);
      return row;
    }));
  }
  if (next.error || next.fault?.message) showError(next.error || next.fault.message);
  refreshControls();
}

async function refreshPorts() {
  $('#refresh-ports').disabled = true;
  try {
    const { ports } = await api('/ports');
    const select = $('#serial-port');
    const previous = select.value || state?.port;
    select.replaceChildren(new Option(ports.length ? 'Select a serial port' : 'No serial ports found', ''));
    for (const port of ports) select.add(new Option(`${port.device} — ${port.description}`, port.device));
    if (ports.some(port => port.device === previous)) select.value = previous;
    else if (ports.length === 1) select.value = ports[0].device;
  } catch (error) { showError(error.message); }
  finally { refreshControls(); }
}
$('#refresh-ports').addEventListener('click', refreshPorts);
$('#serial-port').addEventListener('change', refreshControls);

$('#auth').addEventListener('submit', event => {
  event.preventDefault();
  const entered = $('#access-token').value.trim();
  if (!entered) return;
  accessToken = entered;
  tokenSubmitted = true;
  authWait = false;
  clearError();
  poll();
  refreshPorts();
});

$('#keyboard-toggle').addEventListener('click', () => {
  keyboardEnabled = !keyboardEnabled;
  if (!keyboardEnabled) {
    for (const hold of holds.values()) {
      if (hold.button.dataset.key) releaseHold(hold);
    }
  }
  refreshControls();
});

$('#microstep-mode').addEventListener('change', () => {
  microstepDirty = Number($('#microstep-mode').value) !== state?.microstep;
  refreshControls();
});
$('#apply-microstep').addEventListener('click', async () => {
  if (motionBlocked() || $('#apply-microstep').disabled) return;
  const microstep = Number($('#microstep-mode').value);
  microstepPending = true;
  clearError();
  refreshControls();
  try {
    const next = await api('/microstep', { microstep });
    microstepDirty = false;
    render(next);
    toast(`All motors: ${next.microstep_modes.find(mode => mode.value === next.microstep).label}`);
  } catch (error) { showError(error.message); }
  finally { microstepPending = false; refreshControls(); }
});

$('#connect').addEventListener('click', async () => {
  if (connectionPending) return;
  connectionPending = true;
  clearError();
  refreshControls();
  try {
    await releaseAllHolds();
    const next = state.connected ? await api('/disconnect', {})
      : await api('/connect', { port: $('#serial-port').value });
    render(next);
    toast(next.connected ? 'Device connected. Your workbench is ready.' : 'Motors stopped. Device disconnected.');
  } catch (error) { showError(error.message); }
  finally { connectionPending = false; refreshControls(); }
});

for (const card of cards) {
  const home = $('.home-button', card);
  home.addEventListener('click', () => referenceCommand(home, `/motors/${card.dataset.motor}/home`, {}));
  const slider = $('.speed-slider', card);
  const number = $('.speed-number', card);
  sliderFill(card);
  slider.addEventListener('input', () => {
    number.value = slider.value;
    sliderFill(card);
    refreshControls();
  });
  number.addEventListener('input', () => {
    if (number.checkValidity()) { slider.value = number.value; sliderFill(card); }
    refreshControls();
  });
  $('.steps-number', card).addEventListener('input', refreshControls);
  for (const button of holdButtonsFor(card)) {
    button.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      const hold = beginHold(card, button, { pointerId: event.pointerId });
      if (hold) {
        try { button.setPointerCapture(event.pointerId); }
        catch { releaseHold(hold); }
      }
    });
    for (const name of ['pointerup', 'pointercancel', 'lostpointercapture']) {
      button.addEventListener(name, event => {
        const hold = holds.get(card.dataset.motor);
        if (hold?.pointerId === event.pointerId) releaseHold(hold);
      });
    }
    button.addEventListener('pointermove', event => {
      const hold = holds.get(card.dataset.motor);
      if (!hold || hold.pointerId !== event.pointerId || hold.released) return;
      const rect = button.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right
          || event.clientY < rect.top || event.clientY > rect.bottom) releaseHold(hold);
    });
    button.addEventListener('keydown', event => {
      if (![' ', 'Enter'].includes(event.key)) return;
      event.preventDefault();
      if (!event.repeat) beginHold(card, button, { key: event.key });
    });
    button.addEventListener('blur', () => {
      const hold = holds.get(card.dataset.motor);
      if (hold?.button === button && hold.key !== undefined) releaseHold(hold);
    });
    button.addEventListener('contextmenu', event => event.preventDefault());
    button.addEventListener('click', event => event.preventDefault());
  }
  for (const button of card.querySelectorAll('[data-direction]')) {
    button.addEventListener('click', async () => {
      const motor = card.dataset.motor;
      if (motionBlocked() || button.disabled || pendingMotors.has(motor)) return;
      const steps = $('.steps-number', card);
      if (!number.reportValidity() || !steps.reportValidity()) return;
      pendingMotors.add(motor);
      clearError();
      refreshControls();
      try {
        render(await api(`/motors/${motor}/move`, {
          direction: button.dataset.direction, speed: Number(number.value), steps: Number(steps.value),
        }));
      } catch (error) {
        showError(`${error.message} Check motor status before retrying.`);
        // Never retry movement automatically: a lost reply may still mean a move happened.
      } finally { pendingMotors.delete(motor); refreshControls(); }
    });
  }
}

$('#stop-all').addEventListener('click', async () => {
  stopPending = true;
  clearError();
  refreshControls();
  releaseAllHolds();
  try { render(await api('/stop', {})); toast('All motors stopped.'); }
  catch (error) { showError(error.message); }
  finally { stopPending = false; refreshControls(); }
});

$('#reset').addEventListener('click', () => {
  if (!state) return;
  for (const card of cards) {
    $('.speed-number', card).value = state.defaults.speed;
    $('.speed-slider', card).value = state.defaults.speed;
    $('.steps-number', card).value = state.defaults.steps;
    sliderFill(card);
  }
  refreshControls();
  toast(`Defaults restored: ${state.defaults.speed} pulses/s · ${state.defaults.steps} steps per click`);
});

window.addEventListener('keyup', event => {
  for (const hold of holds.values()) {
    if (hold.key === event.key
        || (hold.shortcutCode && hold.shortcutCode === (event.code || event.key.toLowerCase()))) {
      releaseHold(hold);
    }
  }
});
window.addEventListener('keydown', event => {
  if (event.key === 'Escape') {
    releaseAllHolds();
    return;
  }
  if (motionBlocked() || !keyboardEnabled || event.defaultPrevented || event.isComposing || document.hidden
      || event.ctrlKey || event.metaKey || event.altKey || isEditing(event.target)) return;
  const button = shortcutButtons.get(event.key.toLowerCase());
  if (!button) return;
  event.preventDefault();
  // A held key never queues a new move after a cooldown, stop, or toggle change.
  if (event.repeat) return;
  beginHold($(`#motor-${button.dataset.keyMotor}`), button, {
    shortcutCode: event.code || event.key.toLowerCase(),
  });
});
document.addEventListener('focusin', event => {
  if (!isEditing(event.target)) return;
  for (const hold of holds.values()) {
    if (hold.shortcutCode) releaseHold(hold);
  }
});
window.addEventListener('blur', releaseAllHolds);
window.addEventListener('pagehide', releaseAllHolds);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) releaseAllHolds();
});

async function poll() {
  clearTimeout(pollTimer);
  if (polling) return;
  polling = true;
  try {
    const next = await api('/state');
    const recovered = !online;
    online = true;
    if (recovered) clearError();
    render(next);
  } catch (error) {
    online = false;
    releaseAllHolds();
    if (state) render(state);
    else refreshControls();
    showError(error.auth ? error.message
      : 'Cannot reach the stage panel service. Motion status is unknown.');
  } finally {
    polling = false;
    // Schedule after completion so slow requests never accumulate.
    // A refused token pauses polling until the user enters another one.
    if (!authWait) pollTimer = setTimeout(poll, 250);
  }
}
poll();
refreshPorts();
