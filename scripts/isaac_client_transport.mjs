// Open one temporary inspector session for the installed client.
import { execFileSync } from 'node:child_process';
import { openSync, readFileSync, unlinkSync, writeFileSync, closeSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

export const executable = '/Applications/Isaac Sim WebRTC Streaming Client.app/Contents/MacOS/Isaac Sim WebRTC Streaming Client';
export const lockPath = join(tmpdir(), `mg400-isaac-client-${process.getuid()}.lock`);

export async function acquireClientLock(path = lockPath, waitMs = 15000) {
  const owner = `${process.pid}:${crypto.randomUUID()}`;
  const deadline = Date.now() + waitMs;
  for (;;) {
    try {
      const fd = openSync(path, 'wx', 0o600);
      writeFileSync(fd, owner);
      closeSync(fd);
      const release = () => {
        try { if (readFileSync(path, 'utf8') === owner) unlinkSync(path); }
        catch (error) { if (error.code !== 'ENOENT') throw error; }
      };
      process.once('exit', release);
      return () => { process.removeListener('exit', release); release(); };
    } catch (error) {
      if (error.code !== 'EEXIST') throw error;
      // Do not remove another process's lock. A dead owner needs explicit recovery.
      if (Date.now() >= deadline) throw new Error('CLIENT_BUSY: Another client operation holds the lock.');
      await delay(100);
    }
  }
}

function portInUse() {
  try { execFileSync('lsof', ['-nP', '-iTCP:9229', '-sTCP:LISTEN'], { stdio: 'pipe' }); return true; }
  catch (error) { if (error.status !== 1) throw error; return false; }
}

export async function withInspector(operation) {
  const release = await acquireClientLock();
  let ws, started = false, confirmed = false, nextId = 0, primaryError, value;
  const pending = new Map();
  const rejectPending = () => {
    for (const request of pending.values()) { clearTimeout(request.timer); request.reject(new Error('INSPECTOR_LOST: Result may be unknown. Do not repeat input.')); }
    pending.clear();
  };
  const rpc = (method, params, timeout = 30000) => new Promise((resolve, reject) => {
    if (ws?.readyState !== WebSocket.OPEN) { reject(new Error('INSPECTOR_LOST: Inspector is disconnected.')); return; }
    const id = ++nextId;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('INSPECTOR_TIMEOUT: Result may be unknown. Do not repeat input.')); }, timeout);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, method, params }));
  });
  try {
    const rows = execFileSync('ps', ['-axo', 'pid=,comm='], { encoding: 'utf8' }).split('\n');
    const matches = rows.map(row => row.trim().match(/^(\d+) (.+)$/)).filter(row => row?.[2] === executable);
    if (matches.length !== 1) throw new Error('CLIENT_IDENTITY: Expected one installed client process.');
    const pid = Number(matches[0][1]);
    if (portInUse()) throw new Error('INSPECTOR_BUSY: Port 9229 has an existing listener.');
    process.kill(pid, 'SIGUSR1');
    started = true;
    let targets;
    for (let attempt = 0; attempt < 20; attempt++) {
      try {
        const response = await fetch('http://127.0.0.1:9229/json/list', { signal: AbortSignal.timeout(500) });
        targets = await response.json();
        break;
      } catch (error) { if (attempt === 19) throw error; await delay(100); }
    }
    if (targets.length !== 1) throw new Error('INSPECTOR_IDENTITY: Expected one inspector target.');
    const endpoint = new URL(targets[0].webSocketDebuggerUrl);
    if (endpoint.hostname !== '127.0.0.1' || endpoint.port !== '9229') throw new Error('INSPECTOR_IDENTITY: Expected a loopback endpoint.');
    ws = new WebSocket(endpoint);
    ws.addEventListener('message', ({ data }) => {
      const message = JSON.parse(data), request = pending.get(message.id);
      if (!request) return;
      clearTimeout(request.timer);
      pending.delete(message.id);
      if (message.error) request.reject(new Error(message.error.message));
      else request.resolve(message.result);
    });
    ws.addEventListener('close', rejectPending);
    ws.addEventListener('error', rejectPending);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('INSPECTOR_TIMEOUT: Could not connect.')), 3000);
      ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
      ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('INSPECTOR_LOST: Could not connect.')); }, { once: true });
    });
    const response = await rpc('Runtime.evaluate', { expression: 'JSON.stringify({pid:process.pid,exe:process.execPath})', returnByValue: true });
    const actual = JSON.parse(response.result.value);
    if (actual.pid !== pid || actual.exe !== executable) throw new Error('INSPECTOR_IDENTITY: Client identity does not match.');
    confirmed = true;
    value = await operation(rpc);
  } catch (error) { primaryError = error; }
  finally {
    try {
      if (confirmed && ws?.readyState === WebSocket.OPEN) {
        await rpc('Runtime.evaluate', { expression: "setTimeout(()=>process.getBuiltinModule('inspector').close(),50);true", returnByValue: true }, 3000);
        await delay(200);
        if (portInUse()) throw new Error('Inspector port is still open.');
      } else if (started) throw new Error('Inspector identity or connection was lost. Closure is unconfirmed.');
    } catch (error) {
      primaryError = new Error(`${primaryError?.message || 'Operation finished.'} CLEANUP_FAILED: ${error.message}`);
    }
    ws?.close();
    rejectPending();
    release();
  }
  if (primaryError) throw primaryError;
  return value;
}
