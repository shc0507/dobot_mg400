'use strict';
const $ = selector => document.querySelector(selector);
const rows = $('#sequenceRows');
const message = $('#message');
const base = document.body.dataset.basePath || '';
const client = sessionStorage.getItem('furnace-client') || `page-${crypto.randomUUID()}`;
sessionStorage.setItem('furnace-client', client);
let token = '';
let manual = false;
let sequenceLoaded = false;
let editorDirty = false;

function showMessage(text, error = false) {
  message.textContent = text || '';
  message.classList.toggle('error', error);
}
function fmt(value, digits = 1) { return value == null ? '—' : Number(value).toFixed(digits); }
function setPill(element, online, error, onlineText, offlineText) {
  element.classList.toggle('online', online && !error);
  element.classList.toggle('error', Boolean(error));
  element.querySelector('span:last-child').textContent = error ? 'Error' : online ? onlineText : offlineText;
}
async function api(path, method = 'POST', body = {}) {
  const headers = {'X-MG400-Client': client};
  if (token) headers.Authorization = `Bearer ${token}`;
  const options = {method, headers, cache: 'no-store'};
  if (method !== 'GET') {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(`${base}${path}`, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
function addStep(step = {temperature_c: 100, ramp_rate_c_per_min: 5, hold_minutes: 10}) {
  const row = document.createElement('tr');
  row.innerHTML = '<td class="step-number"></td>' +
    '<td><input data-field="temperature_c" type="number" min="0" step="0.1"></td>' +
    '<td><input data-field="ramp_rate_c_per_min" type="number" min="0.01" step="0.1"></td>' +
    '<td><input data-field="hold_minutes" type="number" min="0" step="0.1"></td>' +
    [0, 1, 2].map(i => `<td><input data-gas="${i}" type="number" min="0" max="100" step="0.1" placeholder="No change"></td>`).join('') +
    '<td><button class="remove-step" type="button" title="Remove step">×</button></td>';
  for (const input of row.querySelectorAll('[data-field]')) input.value = step[input.dataset.field];
  for (const input of row.querySelectorAll('[data-gas]')) input.value = step.gas?.[input.dataset.gas] ?? '';
  row.querySelector('.remove-step').addEventListener('click', () => { row.remove(); renumber(); editorDirty = true; });
  row.addEventListener('input', () => { editorDirty = true; });
  rows.appendChild(row);
  renumber();
}
function renumber() { [...rows.children].forEach((row, i) => { row.querySelector('.step-number').textContent = i + 1; }); }
function collectSteps() {
  return [...rows.children].map(row => {
    const step = {};
    for (const input of row.querySelectorAll('[data-field]')) step[input.dataset.field] = Number(input.value);
    const gas = {};
    for (const input of row.querySelectorAll('[data-gas]')) if (input.value !== '') gas[input.dataset.gas] = Number(input.value);
    if (Object.keys(gas).length) step.gas = gas;
    return step;
  });
}
function collectRecipe() {
  const steps = collectSteps();
  const value = {steps};
  const until = $('#endGasUntil').value;
  if (steps.some(step => step.gas) && until === '') throw new Error('Enter an end-gas temperature.');
  if (until !== '') {
    value.end_gas_until_c = Number(until);
    value.end_gas = {};
    document.querySelectorAll('[data-end-gas]').forEach(input => { value.end_gas[input.dataset.endGas] = Number(input.value); });
  }
  return value;
}
function loadSteps(sequence) {
  rows.innerHTML = '';
  (sequence.steps.length ? sequence.steps : [{temperature_c:100, ramp_rate_c_per_min:5, hold_minutes:10}]).forEach(addStep);
  document.querySelectorAll('[data-end-gas]').forEach(input => { input.value = sequence.end_gas?.[input.dataset.endGas] ?? 0; });
  $('#endGasUntil').value = sequence.end_gas_until_c ?? '';
  editorDirty = false;
  sequenceLoaded = true;
}
function render(furnace) {
  setPill($('#furnaceStatus'), furnace.connected, furnace.error, 'Furnace online', 'Furnace offline');
  const flow = furnace.flow || {connected:false, gas_channels:[], gas_valves:[]};
  setPill($('#flowStatus'), flow.connected, flow.error, 'Flow online', 'Flow offline');
  const holder = furnace.lease?.holder;
  manual = holder === client && furnace.manual_client === client;
  const allowed = !holder || manual;
  $('#controlOwner').textContent = manual ? 'You control the furnace.' : holder ? 'Another client controls the furnace. Stop remains available.' : 'No controlling client.';
  for (const button of document.querySelectorAll('[data-manual]')) button.disabled = !allowed || (furnace.locked && !button.hasAttribute('data-allow-locked'));
  $('#takeControl').disabled = !!holder && !manual;
  $('#releaseControl').disabled = !manual;
  $('#acknowledge').disabled = !allowed || !furnace.locked;
  $('#connectFurnace').disabled = !allowed || furnace.connected;
  $('#pvValue').textContent = fmt(furnace.pv);
  $('#svValue').textContent = fmt(furnace.sv);
  $('#rampValue').textContent = fmt(furnace.ramp);
  $('#runState').textContent = furnace.run_state || 'Offline';
  document.querySelectorAll('.gas-flow-row').forEach(row => {
    const channelIndex = Number(row.dataset.channelIndex);
    const channel = flow.gas_channels.find(item => item.channel_index === channelIndex);
    const valve = flow.gas_valves.find(item => item.channel_index === channelIndex);
    row.querySelector('[data-actual-flow]').textContent = channel?.actual_percent == null ? 'Unverified' : `${fmt(channel.actual_percent)}%`;
    row.querySelector('td strong').textContent = channel?.gas_name || `Gas ${channelIndex + 1}`;
    const input = row.querySelector('[data-flow-target]');
    if (document.activeElement !== input && channel?.target_percent != null) input.value = channel.target_percent;
    row.querySelector('[data-valve-state]').textContent = valve?.open == null ? 'Unknown' : `${valve.open ? 'Open' : 'Close'} ${valve.verified ? 'confirmed' : 'requested'}`;
  });
  const mode = furnace.control_mode || 'DISCONNECTED';
  const modeMessages = {
    HOLD: ['Control mode: HOLD', 'Temperature regulation continues. The controller program is paused.'],
    RUN: ['Control mode: RUN', 'The controller has not confirmed HOLD.'],
    STOPPED: ['Control mode: Stopped', 'Heating is disabled. Cooling is passive.'],
    DISCONNECTED: ['Control mode: Disconnected', 'Connect to read the controller state.'],
    UNKNOWN: ['Control mode: Unknown', 'Check the controller state.'],
  };
  let [title, detail] = modeMessages[mode] || modeMessages.UNKNOWN;
  if (mode === 'RUN' && furnace.waiting_for_program_step_2) {
    title = 'Control mode: Ramping';
    detail = `Program step ${furnace.controller_program_step ?? '—'}. HOLD starts when step 2 begins.`;
  }
  $('#controlModeStatus strong').textContent = title;
  $('#controlModeStatus small').textContent = detail;
  $('#controlModeStatus').className = `control-mode-status ${mode.toLowerCase()}`;
  const seq = furnace.sequence;
  $('#sequenceState').textContent = seq.running ? `Step ${seq.current_index + 1} · ${seq.phase}` : seq.phase;
  $('#sequenceState').classList.toggle('running', seq.running);
  $('#startSequence').disabled = !allowed || furnace.locked || seq.running || seq.phase === 'cooling';
  [...rows.children].forEach((row, i) => row.classList.toggle('current', seq.running && i === seq.current_index));
  if (!sequenceLoaded) loadSteps(seq);
  $('#sequenceProgress').textContent = seq.phase === 'holding' && seq.hold_remaining_seconds != null
    ? `Holding step ${seq.current_index + 1}: ${Math.ceil(seq.hold_remaining_seconds)} seconds remaining.`
    : seq.phase === 'cooling' ? `End gas continues until temperature is below ${seq.end_gas_until_c} °C.` : '';
  if (furnace.error || flow.error) showMessage(furnace.error || flow.error, true);
}
async function refresh() {
  try { render((await api('/api/status', 'GET')).furnace); }
  catch (error) { manual = false; showMessage(error.message, true); }
}
async function run(path, body = {}, method = 'POST', text = 'Command completed.') {
  try {
    const result = await api(path, method, body);
    if (result.ok === false) throw new Error(result.status?.error || 'Stop is unconfirmed. Check the device.');
    showMessage(text);
    await refresh();
  } catch (error) { showMessage(error.message, true); await refresh(); }
}
$('#useToken').addEventListener('click', () => { token = $('#accessToken').value; $('#accessToken').value = ''; manual = false; refresh(); });
$('#takeControl').addEventListener('click', () => run('/api/manual/heartbeat'));
$('#releaseControl').addEventListener('click', () => { manual = false; run('/api/manual/release'); });
$('#acknowledge').addEventListener('click', () => run('/api/furnace/acknowledge'));
$('#connectFurnace').addEventListener('click', () => run('/api/furnace/connect'));
$('#connectFlow').addEventListener('click', () => run('/api/flow/connect'));
$('#applySetpoint').addEventListener('click', () => run('/api/furnace/apply', {
  temperature_c: Number($('#manualTemperature').value), ramp_rate_c_per_min: Number($('#manualRamp').value),
}));
$('#stopFurnace').addEventListener('click', () => run('/api/furnace/stop', {}, 'POST', 'Heat and gas stop requested. Check the status.'));
$('#stopSequence').addEventListener('click', () => run('/api/sequence/stop', {}, 'POST', 'Sequence stop requested. Check heat and gas status.'));
$('#stopAllFlow').addEventListener('click', () => run('/api/flow/stop-all', {}, 'POST', 'Gas stop requested. Missing readback remains unverified.'));
document.querySelectorAll('.gas-flow-row').forEach(row => {
  const channel_index = Number(row.dataset.channelIndex);
  row.querySelector('[data-set-flow]').addEventListener('click', () => run('/api/flow/gas/set', {
    channel_index, flow_percent: Number(row.querySelector('[data-flow-target]').value),
  }, 'POST', 'Flow setpoint sent. Check actual flow.'));
  row.querySelector('[data-valve-open]').addEventListener('click', () => run('/api/flow/gas/valve', {channel_index, open:true}, 'POST', '100% flow requested. Check actual flow.'));
  row.querySelector('[data-valve-close]').addEventListener('click', () => run('/api/flow/gas/stop', {channel_index}, 'POST', 'Gas close requested. Check actual flow.'));
});
$('#addStep').addEventListener('click', () => { addStep(); editorDirty = true; });
for (const [id, path, method] of [['updateSequence', '/api/sequence', 'PUT'], ['startSequence', '/api/sequence/start', 'POST']]) {
  $(`#${id}`).addEventListener('click', () => {
    try { const recipe = collectRecipe(); editorDirty = false; run(path, recipe, method); }
    catch (error) { showMessage(error.message, true); }
  });
}
async function tick() {
  if (manual) {
    try { await api('/api/manual/renew'); }
    catch (error) { manual = false; showMessage('Manual control ended. Take control again explicitly.', true); }
  }
  await refresh();
}
refresh();
setInterval(tick, 1000);
