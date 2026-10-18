// Relay read-only gripper samples through the existing WebRTC session. Requires Node.js 22+.
// Use the temporary loopback inspector only to configure or stop the relay.
import { execFileSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';

const action = process.argv[2] || 'start';
if (!['start', 'stop', 'status'].includes(action)) throw new Error('Use start, stop, or status.');
const script = fileURLToPath(new URL('./gripper_feedback.py', import.meta.url));
const statusFile = fileURLToPath(new URL('../work/gripper_feedback_status.json', import.meta.url));
const python = execFileSync('which', ['python3'], { encoding: 'utf8' }).trim();

const executable = '/Applications/Isaac Sim WebRTC Streaming Client.app/Contents/MacOS/Isaac Sim WebRTC Streaming Client';
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

async function bridgeClient({action, script, python, statusFile}) {
  const { BrowserWindow, app } = process.mainModule.require('electron');
  const windows = BrowserWindow.getAllWindows().filter(w => w.webContents.getURL() ===
    'file:///Applications/Isaac%20Sim%20WebRTC%20Streaming%20Client.app/Contents/Resources/app.asar/dist/renderer/index.html');
  if (windows.length !== 1) throw new Error('Expected one streaming window.');
  const c = windows[0].webContents;
  const previous = process._mg400GripperFeedback;
  if (action === 'status') return previous?.status || {running: false};
  if (action === 'stop') {
    previous?.child?.kill('SIGINT');
    return {stopRequested: Boolean(previous?.child)};
  }
  if (previous?.status.running) throw new Error('Gripper feedback is already running.');
  if (app.getVersion() !== '2.0.0') throw new Error('This relay supports streaming client 2.0.0.');
  if (c.debugger.isAttached()) throw new Error('Another debugger is attached.');
  c.debugger.attach('1.3');
  try {
    await c.debugger.sendCommand('Debugger.enable');
    const result = await c.debugger.sendCommand('Runtime.evaluate', {expression: `(()=>{
      const v=document.getElementById('remote-video');
      if (!v) throw new Error('Stream video missing');
      let el=v, f; while(el&&!f){f=el[Object.keys(el).find(k=>k.startsWith('__reactFiber$'))];el=el.parentElement;}
      while(f){const C=f.stateNode?.constructor;if(typeof C?.getStageLoadingState==='function')return C.getStageLoadingState;f=f.return;}
      throw new Error('Streaming SDK component missing');
    })()`});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description);
    if (!result.result.objectId) throw new Error(JSON.stringify(result));
    const props=await c.debugger.sendCommand('Runtime.getProperties',{objectId:result.result.objectId});
    if (!props.internalProperties?.some(p=>p.name==='[[Scopes]]'))throw new Error('SDK scope missing.');
    const scopes=props.internalProperties.find(p=>p.name==='[[Scopes]]');
    const list=await c.debugger.sendCommand('Runtime.getProperties',{objectId:scopes.value.objectId});
    let sdk;
    for(const scope of list.result){
      if(!scope.value?.objectId)continue;
      const items=await c.debugger.sendCommand('Runtime.getProperties',{objectId:scope.value.objectId});
      sdk=items.result.find(p=>p.name==='ts' && p.value?.type==='function')?.value;
      if(sdk)break;
    }
    if(!sdk)throw new Error('The installed streaming SDK is not supported.');
    const installed = await c.debugger.sendCommand('Runtime.callFunctionOn', {
      objectId: sdk.objectId,
      functionDeclaration: `function(){
        if(typeof this.sendMessage!=='function')throw new Error('SDK sendMessage missing');
        const sdk=this;
        window._mg400SendFeedback=payload=>sdk.sendMessage({
          event_type:'mg400.gripper.feedback',id:crypto.randomUUID(),payload
        });
        return {ready:true,streamStatus:this.streamStatus};
      }`, returnByValue:true
    });
    if(installed.exceptionDetails)throw new Error(installed.exceptionDetails.exception?.description);
    const {spawn}=process.mainModule.require('node:child_process');
    const fs=process.mainModule.require('node:fs');
    const child=spawn(python,['-u',script],{stdio:['ignore','pipe','pipe']});
    const bridge={child,status:{running:true,pid:child.pid,sent:0,error:null},latest:null,busy:false};
    process._mg400GripperFeedback=bridge;
    const save=()=>{fs.writeFileSync(statusFile+'.tmp',JSON.stringify(bridge.status));fs.renameSync(statusFile+'.tmp',statusFile);};
    let pending='',lastSave=0;
    const stop=()=>child.kill('SIGINT');
    c.once('destroyed',stop);
    const forward=async()=>{
      if(bridge.busy)return;
      bridge.busy=true;
      try{
        while(bridge.latest){
          const payload=bridge.latest;bridge.latest=null;
          const result=await c.executeJavaScript(`window._mg400SendFeedback(${JSON.stringify(payload)})`);
          if(result?.status==='error')throw new Error(result.info || 'WebRTC send failed');
          bridge.status.sent++;bridge.status.sample=payload;
          if(Date.now()-lastSave>1000){save();lastSave=Date.now();}
        }
      }catch(error){bridge.status.error=String(error);stop();}
      finally{bridge.busy=false;}
    };
    child.stdout.setEncoding('utf8');
    child.stdout.on('data',chunk=>{
      pending+=chunk;
      for(let at;(at=pending.indexOf('\n'))>=0;){
        const line=pending.slice(0,at);pending=pending.slice(at+1);
        try{bridge.latest=JSON.parse(line);}catch(error){bridge.status.error=String(error);stop();return;}
      }
      void forward();
    });
    child.stderr.on('data',data=>{bridge.status.error=String(data).trim();});
    child.once('error',error=>{bridge.status.error=String(error);});
    child.once('close',(code,signal)=>{
      bridge.status.running=false;bridge.status.exitCode=code;bridge.status.signal=signal;
      c.removeListener('destroyed',stop);save();
    });
    save();
    return {started:true,pid:child.pid,transport:'existing WebRTC',sdk:installed.result.value};
  } finally { c.debugger.detach(); }
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
    expression: `(${bridgeClient.toString()})(${JSON.stringify({action, script, python, statusFile})})`,
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
