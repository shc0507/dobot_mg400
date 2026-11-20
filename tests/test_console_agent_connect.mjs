import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { webcrypto, createHash } from "node:crypto";
const require = createRequire(import.meta.url);
const portal = require("../src/orchestrator/agent_connect/static/portal.js");

test("retained client data becomes stale after disconnect or elapsed source age", () => {
  const client = { connected: true, compatibility: { status: "compatible" }, received_at: 100, state: { age_s: 0.5, quality: "good" } };
  assert.equal(portal.stateView(client, 101).stale, false);
  assert.equal(portal.stateView(client, 104).stale, true);
  assert.equal(portal.stateView({ ...client, connected: false }, 100).stale, true);
  assert.equal(portal.stateView({ ...client, compatibility: { status: "client update required" } }, 100).usable, false);
  assert.equal(portal.stateView({ ...client, error: "digest mismatch" }, 100).usable, false);
});

test("unknown values never turn into numeric zero", () => {
  assert.equal(portal.number(null, "rotation"), "Unknown");
  assert.equal(portal.number(NaN, "rotation"), "Unknown");
  assert.equal(portal.number(0, "rotation"), "0.000 rotation");
});

test("stream adapter requires known units and uses explicit location labels", () => {
  const stream = require("../src/orchestrator/agent_connect/static/stream-portal.js");
  const definition = {$schema:"https://json-schema.org/draft/2020-12/schema",contract:"agent-connect",version:"1.0.0",semantics:{units:{position_rotations:"rotation",pv_c:"degC"}},schemas:{state:{properties:{type:{const:"state"}}}}};
  assert.equal(stream.compatible(definition),true);
  assert.equal(stream.compatible({...definition,version:"2.0.0"}),false);
  assert.equal(stream.compatible({...definition,semantics:{units:{position_rotations:"mm",pv_c:"degC"}}}),false);
  assert.equal(stream.locationText({holder:"unknown"}),"Unknown");
  assert.equal(stream.locationText({holder:"tray_slot",tray_id:"T1",slot_id:"A1"}),"Tray T1 / A1");
  assert.equal(stream.canonical({b:2,a:{y:1,x:0}}),'{"a":{"x":0,"y":1},"b":2}');
});

test("reading quality controls availability and freshness independently", () => {
  const client = { connected: true, compatibility: { status: "compatible" }, received_at: 100,
    state: { age_s: 0, quality: "good", stage: { position: 12, quality: "unknown" }, temperature: { value: 30, quality: "stale" } } };
  const view = portal.stateView(client, 100);
  assert.equal(view.stale, false);
  assert.deepEqual(view.readings.stage, { usable: false, stale: true });
  assert.deepEqual(view.readings.temperature, { usable: true, stale: true });
  client.state.stage.quality = "good";
  client.state.temperature.quality = "unknown";
  assert.deepEqual(portal.stateView(client, 100).readings, {
    stage: { usable: true, stale: false }, temperature: { usable: false, stale: true },
  });
  client.state.quality = "unknown";
  assert.equal(portal.stateView(client, 100).usable, false);
  assert.equal(portal.stateView(client, 100).readings.stage.usable, false);
});

test("read-only workspace has matching language keys and visible unknown ages", () => {
  let workspace;
  globalThis.window = { LabConsole: { register(value) { workspace = value; } } };
  require("../src/orchestrator/web/static/console/agent-connect.js");
  assert.equal(workspace.id, "agent-connect");
  assert.deepEqual(Object.keys(workspace.i18n.en), Object.keys(workspace.i18n.zh));
  assert.equal(workspace.logic.age(null), "—");
  assert.equal(workspace.logic.age(1.23), "1.2 s");
});

const live = require("../src/orchestrator/agent_connect/static/live-portal.js");
const agreement = require("../src/orchestrator/agent_connect/static/stream-portal.js");
const definition = {$schema:"https://json-schema.org/draft/2020-12/schema",contract:"agent-connect",version:"1.0.0",semantics:{units:{position_rotations:"rotation",pv_c:"degC"}},schemas:{state:{properties:{type:{const:"state"}}}}};
const identity = value => "sha256:" + createHash("sha256").update(agreement.canonical(value)).digest("hex");
const nextTurn = () => new Promise(resolve=>setImmediate(resolve));
function liveFixture(bundle = definition) {
  const id=identity(bundle), channels=[], reads=[];
  const discovery={mode:"live",read_only:true,discovery_version:1,boot_id:"process-1",contract_id:id,contract_url:`/lab/api/agent/contracts/${id.slice(7)}.json`};
  const reader=new live.LiveReader({
    read: async path=>{reads.push(path);return path.endsWith("discovery") ? discovery : bundle;},
    source: url=>{ const channel={url,closed:false,close(){this.closed=true;},addEventListener(name,handler){this[name]=handler;}};channels.push(channel);return channel; },
    digest: value=>webcrypto.subtle.digest("SHA-256",value),
  });
  const frame={type:"state",mode:"live",boot_id:"process-1",contract_id:id,event_seq:3,seq:1,generated_at:100,devices:{stage:{quality:"valid",age_s:0,values:{position_rotations:0}}},samples:{items:[],count:0,complete:true},operations:{items:[],count:0}};
  return {reader,channels,reads,frame,discovery};
}
async function opened(fixture) {
  fixture.reader.start();
  for(let i=0;i<30 && !fixture.channels.length;i++) await nextTurn();
  assert.equal(fixture.channels.length,1);
}
test("live values preserve null, false, zero and every nested field",()=>{
  assert.deepEqual(live.rows({a:null,b:false,c:0,d:{items:[]}}),[["a",null],["b",false],["c",0],["d.items",[]]]);
  assert.equal(live.display(null),"Unknown"); assert.equal(live.display(false),"No");assert.equal(live.display(0),"0");
  assert.equal(live.readingQuality({quality:"valid",age_s:1},100,true,101),"Current");
  assert.equal(live.readingQuality({quality:"valid",age_s:1},100,true,104),"Outdated");
  assert.equal(live.readingQuality({quality:"uncalibrated"},100,true,101),"Not calibrated");
  assert.equal(live.readingQuality({quality:"valid",values:{connected:false}},100,true,101),"Not connected");
  assert.equal(live.readingQuality({quality:"valid",age_s:0},100,false,100),"Last received");
});
test("one live reader verifies its agreement and closes its stream when stopped",async()=>{
  const f=liveFixture();
  try {
    await opened(f);assert.match(f.channels[0].url,/rate_hz=2/);
    f.channels[0].onmessage({data:JSON.stringify(f.frame)});await nextTurn();
    assert.deepEqual(f.reader.state,f.frame);assert.equal(f.reader.connected,true);
    assert.equal(live.readingQuality(f.reader.state.devices.stage,f.reader.receivedAt,true),"Current"); // Remote frame clock is deliberately 100.
    assert.ok(f.reads.every(path=>path.startsWith("/lab/api/agent/")));
    f.reader.stop();assert.equal(f.channels[0].closed,true);assert.equal(f.reader.connected,false);
    f.channels[0].onmessage({data:JSON.stringify({...f.frame,seq:2})});await nextTurn();assert.equal(f.reader.state.seq,1);
  }finally {f.reader.stop();}
});
test("incompatible live definitions retain raw data without decoded readings",async()=>{
  const f=liveFixture({...definition,version:"2.0.0"});
  try {
    await opened(f);f.channels[0].onmessage({data:JSON.stringify(f.frame)});await nextTurn();
    assert.equal(f.reader.state,null);assert.deepEqual(f.reader.rawState,f.frame);assert.match(f.reader.error,/new data format/);
  }finally{f.reader.stop();}
});
test("a cancelled discovery cannot open a live connection later",async()=>{
  const f=liveFixture();let release;
  const original=f.reader.read;
  f.reader.read=async path=>{await new Promise(resolve=>{release=resolve;});return original(path);};
  f.reader.start();f.reader.stop();release();await nextTurn();
  // The next agreement read can finish later, but no connection may be created.
  if(release)release();await nextTurn();if(release)release();await nextTurn();
  assert.equal(f.channels.length,0);f.reader.stop();
});
test("live replay keeps event history bounded and uses its saved cursor",async()=>{
  const f=liveFixture();
  try{
    await opened(f);await f.reader.consume(f.frame,f.reader.generation);
    for(let n=4;n<109;n++)await f.reader.consume({type:"event",boot_id:"process-1",contract_id:f.frame.contract_id,event_seq:n,kind:"test",data:{}},f.reader.generation);
    assert.equal(f.reader.events.length,100);assert.equal(f.reader.cursor.event_seq,108);
    await f.reader.consume({type:"event",boot_id:"process-1",contract_id:f.frame.contract_id,event_seq:108,kind:"test",data:{}},f.reader.generation);
    assert.equal(f.reader.events.length,100);
    f.reader.stop();f.reader.start();for(let i=0;i<30 && f.channels.length<2;i++)await nextTurn();
    assert.match(f.channels[1].url,/boot_id=process-1&event_seq=108/);
  }finally{f.reader.stop();}
});
