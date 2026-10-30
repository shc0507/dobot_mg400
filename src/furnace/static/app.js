const $ = (selector) => document.querySelector(selector);
const rows = $('#sequenceRows');
const message = $('#message');
const videoImage = $('.video-wrap img');
const stabilizationTarget = $('#stabilizationTarget');

// Match the overlay to the same 22%-of-image region used by the camera tracker.
function sizeStabilizationTarget() {
  if (!videoImage.naturalWidth || !videoImage.naturalHeight) return;
  const containerWidth = videoImage.clientWidth;
  const containerHeight = videoImage.clientHeight;
  const imageRatio = videoImage.naturalWidth / videoImage.naturalHeight;
  const renderedWidth = Math.min(containerWidth, containerHeight * imageRatio);
  const renderedHeight = Math.min(containerHeight, containerWidth / imageRatio);
  const size = Math.max(32, Math.min(renderedWidth, renderedHeight) * 0.22);
  stabilizationTarget.style.width = `${size}px`;
  stabilizationTarget.style.height = `${size}px`;
}
videoImage.addEventListener('load', sizeStabilizationTarget);
window.addEventListener('resize', sizeStabilizationTarget);
let editorDirty = false;
let sequenceLoaded = false;

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
async function api(url, method = 'POST', body = null) {
  const options = { method, headers: {} };
  if (body !== null) { options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body); }
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || 'Request failed');
  return data;
}

function addStep(step = {temperature_c: 100, ramp_rate_c_per_min: 5, hold_minutes: 10}) {
  const row = document.createElement('tr');
  row.innerHTML = `<td class="step-number"></td>
    <td><input data-field="temperature_c" type="number" min="0" step="0.1" value="${step.temperature_c}"></td>
    <td><input data-field="ramp_rate_c_per_min" type="number" min="0.01" step="0.1" value="${step.ramp_rate_c_per_min}"></td>
    <td><input data-field="hold_minutes" type="number" min="0" step="0.1" value="${step.hold_minutes}"></td>
    <td><button class="remove-step" type="button" title="Remove step">×</button></td>`;
  row.querySelectorAll('input').forEach(input => input.addEventListener('input', () => editorDirty = true));
  row.querySelector('.remove-step').addEventListener('click', () => {
    row.remove(); editorDirty = true; renumberSteps();
    if (!rows.children.length) addStep();
  });
  rows.appendChild(row); renumberSteps();
}
function renumberSteps() { [...rows.children].forEach((row, index) => row.querySelector('.step-number').textContent = index + 1); }
function collectSteps() {
  return [...rows.children].map(row => ({
    temperature_c: Number(row.querySelector('[data-field=temperature_c]').value),
    ramp_rate_c_per_min: Number(row.querySelector('[data-field=ramp_rate_c_per_min]').value),
    hold_minutes: Number(row.querySelector('[data-field=hold_minutes]').value),
  }));
}
function loadSteps(steps) {
  rows.innerHTML = '';
  (steps.length ? steps : [{temperature_c:100, ramp_rate_c_per_min:5, hold_minutes:10}]).forEach(addStep);
  editorDirty = false;
  sequenceLoaded = true;
}

function getRecordingSettings() {
  const mode = $('#recordingMode').value === 'time_lapse' ? 'time_lapse' : 'live';
  const captureInterval = Number($('#recordingInterval').value);
  const fps = Number($('#recordingFps').value);
  return {
    mode,
    recording_mode: mode,
    capture_interval_seconds: mode === 'time_lapse' && Number.isFinite(captureInterval) && captureInterval > 0 ? captureInterval : null,
    output_fps: mode === 'time_lapse' && Number.isFinite(fps) && fps > 0 ? fps : 1,
  };
}

function updateRecordingControls() {
  const isTimeLapse = $('#recordingMode').value === 'time_lapse';
  $('#recordingInterval').disabled = !isTimeLapse;
  $('#recordingFps').disabled = !isTimeLapse;
}

function render(camera, furnace) {
  setPill($('#cameraStatus'), camera.camera_open, camera.error, 'Camera online', 'Camera');
  setPill($('#furnaceStatus'), furnace.connected, furnace.error, 'Furnace online', 'Furnace offline');
  const flow = furnace.flow || {connected:false, gas_channels:[], water_channels:[]};
  setPill($('#flowStatus'), flow.connected, flow.error, 'Flow online', 'Flow offline');
  $('#recordingBadge').hidden = !camera.recording;
  if (camera.recording) {
    $('#recordingMode').value = camera.recording_mode === 'time_lapse' ? 'time_lapse' : 'live';
    if (camera.recording_interval_seconds != null) {
      $('#recordingInterval').value = String(camera.recording_interval_seconds);
    }
    if (camera.recording_fps != null) {
      $('#recordingFps').value = String(camera.recording_fps);
    }
  }
  updateRecordingControls();
  $('#stabilizationToggle').checked = Boolean(camera.stabilization);
  $('#stabilizationTarget').hidden = !camera.stabilization;
  $('#startRecording').disabled = camera.recording || Boolean(camera.error);
  $('#stopRecording').disabled = !camera.recording;
  $('#recordingStatus').textContent = camera.recording ? 'Recording in progress' : 'Live view';
  $('#pvValue').textContent = fmt(furnace.pv);
  $('#svValue').textContent = fmt(furnace.sv);
  $('#rampValue').textContent = fmt(furnace.ramp);
  $('#runState').textContent = furnace.run_state || 'Offline';

  $('#connectFlow').disabled = flow.connected;
  document.querySelectorAll('.gas-flow-row').forEach(row => {
    const channelIndex = Number(row.dataset.channelIndex);
    const channel = (flow.gas_channels || []).find(
      item => item.channel_index === channelIndex
    );
    const valve = (flow.gas_valves || []).find(
      item => item.channel_index === channelIndex
    );
    const targetInput = row.querySelector('[data-flow-target]');
    const actualPercent = channel?.actual_percent == null
      ? null
      : Number(channel.actual_percent);
    row.querySelector('[data-actual-flow]').textContent = channel?.actual_percent == null
      ? '—'
      : `${fmt(actualPercent, 1)}%`;
    if (document.activeElement !== targetInput && channel?.target_percent != null) {
      targetInput.value = Number(channel.target_percent);
    }
    row.querySelector('[data-valve-state]').textContent = valve?.open == null
      ? 'Unknown'
      : valve.open ? 'Open requested' : 'Close requested';
    row.dataset.flowConnected = flow.connected ? 'true' : 'false';
    targetInput.disabled = !flow.connected;
    row.querySelector('[data-set-flow]').disabled = !flow.connected;
    row.querySelector('[data-valve-open]').disabled = !flow.connected;
    row.querySelector('[data-valve-close]').disabled = !flow.connected;
  });
  $('#stopAllFlow').disabled = !flow.connected;
  const controlMode = $('#controlModeStatus');
  const modeMessages = {
    HOLD: ['Control mode: HOLD', 'True controller HOLD is active; stored program steps were not changed.'],
    RUN: ['Control mode: RUN', 'The controller is regulating but has not confirmed HOLD.'],
    STOPPED: ['Control mode: Stopped', 'Heating is not active.'],
    DISCONNECTED: ['Control mode: Disconnected', 'Connect the furnace to determine its control mode.'],
    UNKNOWN: ['Control mode: Unknown', 'The controller returned an unrecognized state.'],
  };
  const mode = furnace.control_mode || 'DISCONNECTED';
  let [modeTitle, modeDetail] = modeMessages[mode] || modeMessages.UNKNOWN;
  if (mode === 'RUN' && furnace.waiting_for_program_step_2) {
    modeTitle = 'Control mode: Ramping';
    modeDetail = `Controller program is at step ${furnace.controller_program_step ?? '—'}; HOLD will be applied automatically when step 2 begins.`;
  }
  controlMode.querySelector('strong').textContent = modeTitle;
  controlMode.querySelector('small').textContent = modeDetail;
  controlMode.className = `control-mode-status ${mode.toLowerCase()}`;

  const seq = furnace.sequence;
  $('#sequenceState').textContent = seq.running ? `Step ${seq.current_index + 1} · ${seq.phase}` : seq.phase === 'complete' ? 'Complete' : 'Not running';
  $('#sequenceState').classList.toggle('running', seq.running);
  $('#startSequence').disabled = seq.running;
  $('#stopSequence').disabled = !seq.running;
  $('#recordSequence').disabled = seq.running;
  $('#runName').disabled = camera.recording || seq.running;
  [...rows.children].forEach((row, index) => row.classList.toggle('current', seq.running && index === seq.current_index));
  if (!sequenceLoaded) loadSteps(seq.steps);
  let progress = '';
  if (seq.running) {
    progress = seq.phase === 'holding' && seq.hold_remaining_seconds != null
      ? `Holding step ${seq.current_index + 1}: ${Math.ceil(seq.hold_remaining_seconds)} seconds remaining.`
      : `Ramping toward step ${seq.current_index + 1} target.`;
  }
  $('#sequenceProgress').textContent = progress;
  if (camera.recording_error) showMessage(camera.recording_error, true);
  else if (furnace.error) showMessage(furnace.error, true);
  else if (flow.error) showMessage(flow.error, true);
}

async function refresh() {
  try {
    const response = await fetch('/api/status', {cache:'no-store'});
    const data = await response.json();
    render(data.camera, data.furnace);
  } catch (error) { showMessage('Cannot reach the Flask server.', true); }
}

$('#stabilizationToggle').addEventListener('change', async () => {
  const enabled = $('#stabilizationToggle').checked;
  try {
    await api('/api/camera/stabilization', 'POST', {enabled});
    showMessage(`Video stabilization ${enabled ? 'enabled' : 'disabled'}.`);
  } catch (error) {
    showMessage(error.message, true);
    await refresh();
  }
});

$('#startRecording').addEventListener('click', async () => {
  try {
    showMessage('Starting recording…');
    const settings = getRecordingSettings();
    const data = await api('/api/recording/start', 'POST', {
      run_name: $('#runName').value,
      ...settings,
      recording_mode: settings.mode,
    });
    showMessage(`Recording started: ${data.path}`);
    await refresh();
  } catch (error) {
    showMessage(error.message, true);
  }
});
$('#stopRecording').addEventListener('click', async () => {
  try { showMessage('Finalizing recording…'); const data = await api('/api/recording/stop'); showMessage(`Saved ${data.segments.length} video segment(s).`); await refresh(); }
  catch (error) { showMessage(error.message, true); }
});
$('#recordingMode').addEventListener('change', updateRecordingControls);

$('#connectFurnace').addEventListener('click', async () => {
  try { showMessage('Connecting to furnace…'); await api('/api/furnace/connect'); showMessage('Furnace connected.'); await refresh(); }
  catch (error) { showMessage(error.message, true); }
});
$('#applySetpoint').addEventListener('click', async () => {
  try {
    const body = {temperature_c:Number($('#manualTemperature').value), ramp_rate_c_per_min:Number($('#manualRamp').value)};
    await api('/api/furnace/apply', 'POST', body); showMessage('Target and ramp rate applied.'); await refresh();
  } catch (error) { showMessage(error.message, true); }
});
$('#stopFurnace').addEventListener('click', async () => {
  try { await api('/api/furnace/stop'); showMessage('Heating stopped; cooling is passive.'); await refresh(); }
  catch (error) { showMessage(error.message, true); }
});

$('#connectFlow').addEventListener('click', async () => {
  try {
    showMessage('Connecting to the flow controller…');
    const data = await api('/api/flow/connect', 'POST');
    showMessage(data.status.write_only
      ? 'Flow controller connected in write-only mode. Solenoid echoes are used when available.'
      : 'Flow controller and gas channels connected.');
    await refresh();
  } catch (error) { showMessage(error.message, true); }
});
document.querySelectorAll('.gas-flow-row').forEach(row => {
  row.querySelector('[data-set-flow]').addEventListener('click', async () => {
    try {
      const channelIndex = Number(row.dataset.channelIndex);
      const flowPercent = Number(row.querySelector('[data-flow-target]').value);
      if (!Number.isFinite(flowPercent) || flowPercent < 0 || flowPercent > 100) {
        throw new Error('Enter a flow target from 0 to 100%.');
      }
      await api('/api/flow/gas/set', 'POST', {
        channel_index: channelIndex,
        flow_percent: flowPercent,
      });
      showMessage(`Gas ${channelIndex + 1}: ${flowPercent}% setpoint sent without waiting for a reply; valve opened.`);
      await refresh();
    } catch (error) { showMessage(error.message, true); }
  });
  const setValve = async open => {
    try {
      const channelIndex = Number(row.dataset.channelIndex);
      await api('/api/flow/gas/valve', 'POST', {
        channel_index: channelIndex,
        open,
      });
      showMessage(`Gas ${channelIndex + 1}: ${open ? '100% setpoint' : 'close'} command sent; physical flow is unverified.`);
      await refresh();
    } catch (error) { showMessage(error.message, true); }
  };
  row.querySelector('[data-valve-open]').addEventListener('click', () => setValve(true));
  row.querySelector('[data-valve-close]').addEventListener('click', () => setValve(false));
});
$('#stopAllFlow').addEventListener('click', async () => {
  try {
    await api('/api/flow/stop-all');
    showMessage('All three gas setpoints were zeroed and their solenoids closed.');
    await refresh();
  } catch (error) { showMessage(error.message, true); }
});
$('#addStep').addEventListener('click', () => { addStep(); editorDirty = true; });
$('#updateSequence').addEventListener('click', async () => {
  try { await api('/api/sequence', 'PUT', {steps:collectSteps()}); editorDirty = false; showMessage('Sequence saved. Running step changes apply immediately.'); await refresh(); }
  catch (error) { showMessage(error.message, true); }
});
$('#startSequence').addEventListener('click', async () => {
  try {
    const recordingSettings = getRecordingSettings();
    const body = {
      steps: collectSteps(),
      record_camera: $('#recordSequence').checked,
      run_name: $('#runName').value,
      ...recordingSettings,
      recording_mode: recordingSettings.mode,
    };
    await api('/api/sequence/start', 'POST', body); editorDirty = false; showMessage('Sequence started.'); await refresh();
  } catch (error) { showMessage(error.message, true); }
});
$('#stopSequence').addEventListener('click', async () => {
  try { await api('/api/sequence/stop'); showMessage('Sequence and heating stopped.'); await refresh(); }
  catch (error) { showMessage(error.message, true); }
});

refresh();
setInterval(refresh, 1000);
