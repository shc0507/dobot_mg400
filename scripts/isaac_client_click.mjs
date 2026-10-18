// Send clicks through the client's temporary loopback inspector. Requires Node.js 22+.
import { execFileSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const executable = '/Applications/Isaac Sim WebRTC Streaming Client.app/Contents/MacOS/Isaac Sim WebRTC Streaming Client';
const numbers = process.argv.slice(2).map(Number);
const [width, height, ...coordinates] = numbers;
if (numbers.length < 4 || numbers.length % 2 || !numbers.every(Number.isFinite) || width <= 0 || height <= 0) {
  throw new Error('Usage: node scripts/isaac_client_click.mjs screenshot_width screenshot_height x y [x y ...]');
}
if (numbers.length > 18) throw new Error('Use at most eight clicks per batch, then verify the result.');
const points = [];
for (let i = 0; i < coordinates.length; i += 2) {
  const [x, y] = coordinates.slice(i, i + 2);
  if (x < 0 || x >= width || y < 0 || y >= height) throw new Error('Click is outside the screenshot.');
  points.push([x, y]);
}
const rows = execFileSync('ps', ['-axo', 'pid=,comm='], { encoding: 'utf8' }).split('\n');
const matches = rows.map(row => row.trim().match(/^(\d+) (.+)$/)).filter(row => row?.[2] === executable);
if (matches.length !== 1) throw new Error('Expected one installed client process. No signal sent.');
const pid = Number(matches[0][1]);
function portInUse() {
  try {
    execFileSync('lsof', ['-nP', '-iTCP:9229', '-sTCP:LISTEN'], { stdio: 'pipe' });
    return true;
  } catch (error) {
    if (error.status !== 1) throw error;
    return false;
  }
}
if (portInUse()) throw new Error('Port 9229 is already in use. No signal sent.');

let ws;
let nextID = 0;
let identityConfirmed = false;
const pending = new Map();
async function targets() {
  const response = await fetch('http://127.0.0.1:9229/json/list', { signal: AbortSignal.timeout(500) });
  return response.json();
}
function rpc(method, params) {
  const id = ++nextID;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('Inspector request timeout.')); }, 10000);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, method, params }));
  });
}
async function connect() {
  identityConfirmed = false;
  const list = await targets();
  if (list.length !== 1) throw new Error('Expected one inspector target.');
  const endpoint = new URL(list[0].webSocketDebuggerUrl);
  if (endpoint.hostname !== '127.0.0.1' || endpoint.port !== '9229') throw new Error('Inspector is not loopback-only.');
  ws = new WebSocket(endpoint);
  ws.addEventListener('message', ({ data }) => {
    const message = JSON.parse(data);
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    clearTimeout(request.timer);
    if (message.error) request.reject(new Error(message.error.message));
    else request.resolve(message.result);
  });
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Inspector connection timeout.')), 2000);
    ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Inspector connection failed.')); }, { once: true });
  });
  const response = await rpc('Runtime.evaluate', { expression: 'JSON.stringify({pid:process.pid,exe:process.execPath})', returnByValue: true });
  const actual = JSON.parse(response.result.value);
  if (actual.pid !== pid || actual.exe !== executable) throw new Error('Inspector process identity mismatch.');
  identityConfirmed = true;
}

async function clickInClient({ width, height, points }) {
  const { BrowserWindow } = process.mainModule.require('electron');
  const windows = BrowserWindow.getAllWindows().filter(w => w.webContents.getURL() ===
    'file:///Applications/Isaac%20Sim%20WebRTC%20Streaming%20Client.app/Contents/Resources/app.asar/dist/renderer/index.html');
  if (windows.length !== 1) throw new Error('Expected one streaming window.');
  const window = windows[0], contents = window.webContents;
  if (contents.debugger.isAttached()) throw new Error('Another debugger is attached.');
  if (window.isFocused() || window.isMinimized()) throw new Error('Keep the client in the background and unminimized.');
  const bounds = window.getBounds(), content = window.getContentBounds(), zoom = contents.getZoomFactor();
  const scaleX = width / bounds.width, scaleY = height / bounds.height;
  if (Math.abs(scaleX / scaleY - 1) > .01) throw new Error('Window dimensions changed. Capture again.');
  contents.debugger.attach('1.3');
  let held = null;
  const sent = [];
  const deadline = Date.now() + 8000;
  try {
    for (const [sx, sy] of points) {
      if (JSON.stringify(window.getBounds()) !== JSON.stringify(bounds) || window.isFocused()) throw new Error('Window state changed. Stop and capture again.');
      const x = (sx / scaleX - content.x + bounds.x) / zoom;
      const y = (sy / scaleY - content.y + bounds.y) / zoom;
      const result = await contents.debugger.sendCommand('Runtime.evaluate', {
        expression: `JSON.stringify({scale:devicePixelRatio,video:(()=>{const v=document.getElementById('remote-video');return !!v && v.readyState>=2 && document.elementFromPoint(${x},${y})===v;})()})`,
        returnByValue: true
      });
      const geometry = JSON.parse(result.result.value);
      if (Math.abs(scaleX / (geometry.scale / zoom) - 1) > .01) throw new Error('Screenshot scale changed. Capture the full window again.');
      if (!geometry.video) throw new Error('Target is outside the active video or an overlay covers it.');
      await contents.debugger.sendCommand('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', buttons: 0 });
      await new Promise(resolve => setTimeout(resolve, 100));
      if (Date.now() >= deadline) throw new Error('Batch deadline reached. No further clicks sent.');
      held = { x, y };
      await contents.debugger.sendCommand('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', buttons: 1, clickCount: 1 });
      await new Promise(resolve => setTimeout(resolve, 150));
      await contents.debugger.sendCommand('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', buttons: 0, clickCount: 1 });
      held = null;
      sent.push({ screenshot: [sx, sy], content: [x, y] });
    }
    if (window.isFocused()) throw new Error('Client acquired focus. Stop automation.');
    return { sent, clientFocused: false, verification: 'Check the remote result in a new screenshot.' };
  } finally {
    try {
      if (held) await contents.debugger.sendCommand('Input.dispatchMouseEvent', { type: 'mouseReleased', ...held, button: 'left', buttons: 0, clickCount: 1 });
    } finally {
      contents.debugger.detach();
    }
  }
}

process.kill(pid, 'SIGUSR1');
try {
  for (let attempt = 0; attempt < 30; attempt++) {
    try { await connect(); break; }
    catch (error) {
      ws?.close();
      if (attempt === 29) throw error;
      await delay(100);
    }
  }
  const result = await rpc('Runtime.evaluate', {
    expression: `(${clickInClient.toString()})(${JSON.stringify({ width, height, points })})`,
    returnByValue: true, awaitPromise: true
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  console.log(JSON.stringify(result.result.value));
} finally {
  try {
    if (!identityConfirmed || ws?.readyState !== WebSocket.OPEN) {
      ws?.close();
      await connect();
    }
    await rpc('Runtime.evaluate', { expression: "setTimeout(()=>process.getBuiltinModule('inspector').close(),100);true", returnByValue: true });
    ws.close();
    await delay(200);
    if (portInUse()) throw new Error('Port 9229 is still listening.');
  } catch (error) {
    ws?.close();
    throw new Error(`Cannot confirm inspector cleanup: ${error.message}`);
  }
}
