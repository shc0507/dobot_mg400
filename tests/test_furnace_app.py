"""HTTP/entrypoint checks with simulated serial, fake servers, and no sockets."""

import signal
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import minimalmodbus
import pytest
import serial
from contract_helpers import assert_error, check_contract

from furnace import controller as p
from furnace.__main__ import main
from furnace.app import create_app, make_service

TOKEN = "furnace-test-access-token-12345"
THREAD_START = threading.Thread.start


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def wall(self):
        return 1_000_000 + self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def offline(monkeypatch, clock):
    def forbidden(*args, **kwargs):
        raise AssertionError("A furnace HTTP test attempted real I/O or thread startup")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    monkeypatch.setattr(p, "time", SimpleNamespace(time=clock.wall, sleep=clock.advance))
    monkeypatch.setattr(
        minimalmodbus, "time", SimpleNamespace(monotonic=clock, sleep=lambda _: None)
    )
    monkeypatch.setattr(minimalmodbus, "_latest_read_times", {})
    monkeypatch.setattr(minimalmodbus, "_serialports", {})


@pytest.fixture
def service(clock):
    value = make_service(demo=True, clock=clock, wall_clock=clock.wall)
    value.start(background=False)
    yield value
    value.close()


def post(client, path, body=None, *, actor="page-test", token=None, method="POST", headers=None):
    request_headers = {"X-MG400-Client": actor, **(headers or {})}
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    return client.open(
        path, method=method, json={} if body is None else body, headers=request_headers
    )


def test_imports_are_inert():
    code = """
import builtins, os, socket, ssl, threading, serial
from pathlib import Path
def forbidden(*args, **kwargs): raise AssertionError("import side effect")
serial.Serial = socket.socket = threading.Thread.start = os.open = forbidden
Path.write_text = Path.write_bytes = builtins.open = forbidden
import furnace.app
import furnace.__main__
"""
    subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True, timeout=10)


@pytest.mark.parametrize("base", ["/", "/lab/furnace/"])
def test_demo_contract(service, clock, base):
    expired = []
    original = service.lifecycle.lease.on_expire

    def expiry():
        expired.append(True)
        original()

    service.lifecycle.lease.on_expire = expiry
    client = create_app(service, token=TOKEN, base_path=base).test_client()
    check_contract(
        client,
        advance=clock.advance,
        expiry_count=lambda: len(expired),
        base_path=base,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def test_public_shell_has_no_camera_or_recording_or_secret(service):
    client = create_app(service, token=TOKEN, base_path="/heat/").test_client()
    page = client.get("/heat/")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert TOKEN not in text
    assert "video_feed" not in text and "recordSequence" not in text and "/agent" not in text
    assert "Stop heat and gas" in text and 'data-end-gas="0"' in text
    assert "/heat/static/app.js" in text
    assert client.get("/heat/static/app.js").status_code == 200
    assert_error(client.get("/heat/api/status"), 401)
    assert_error(client.get("/heat/video_feed", headers={"Authorization": f"Bearer {TOKEN}"}), 404)


@pytest.mark.parametrize(
    "path,method,body",
    [
        ("/api/status", "GET", None),
        ("/api/furnace/apply", "POST", {"temperature_c": 30, "ramp_rate_c_per_min": 5}),
        ("/api/sequence", "PUT", {"steps": []}),
        ("/api/flow/gas/stop", "POST", {"channel_index": 0}),
        ("/api/stop", "POST", {}),
    ],
)
def test_legacy_and_contract_access_are_uniform(service, path, method, body):
    client = create_app(service, token=TOKEN).test_client()
    assert_error(post(client, path, body, method=method), 401)
    assert_error(
        post(client, path, body, method=method, token=TOKEN, headers={"Origin": "null"}), 403
    )
    if method != "GET":
        assert_error(
            client.open(
                path,
                method=method,
                data="{}",
                content_type="text/plain",
                headers={"Authorization": f"Bearer {TOKEN}", "X-MG400-Client": "test"},
            ),
            415,
        )
        assert_error(
            client.open(path, method=method, json={}, headers={"Authorization": f"Bearer {TOKEN}"}),
            400,
        )


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "remote.example"},
        {"X-Forwarded-For": "127.0.0.1"},
        {"Forwarded": "host=localhost"},
        {"Host": "localhost,remote"},
    ],
)
def test_no_token_loopback_and_forwarding_rules(service, headers):
    client = create_app(service).test_client()
    assert_error(client.get("/api/status", headers=headers), 403)


def test_manual_command_claims_only_when_no_automation_owner(service):
    client = create_app(service).test_client()
    assert post(client, "/api/heartbeat", actor="automation").status_code == 200
    for actor in ("page-test", "automation"):
        assert_error(
            post(
                client,
                "/api/furnace/apply",
                {"temperature_c": 30, "ramp_rate_c_per_min": 5},
                actor=actor,
            ),
            409,
        )
        assert_error(post(client, "/api/manual/heartbeat", actor=actor), 409)
    assert post(client, "/api/furnace/stop").status_code == 200
    assert service.lifecycle.lease.snapshot()["holder"] == "automation"
    assert post(client, "/api/release", actor="automation").status_code == 200
    assert (
        post(
            client, "/api/furnace/apply", {"temperature_c": 30, "ramp_rate_c_per_min": 5}
        ).status_code
        == 200
    )
    state = client.get("/api/status").get_json()["furnace"]
    assert state["manual_client"] == "page-test"
    assert state["lease"]["holder"] == "page-test"


def test_same_id_contract_acquisition_removes_manual_provenance(service):
    client = create_app(service).test_client()
    assert post(client, "/api/manual/heartbeat").status_code == 200
    assert post(client, "/api/heartbeat").status_code == 200
    assert_error(
        post(client, "/api/furnace/apply", {"temperature_c": 30, "ramp_rate_c_per_min": 5}), 409
    )
    assert_error(post(client, "/api/manual/renew"), 409)
    assert post(client, "/api/stop").status_code == 200


def test_expired_manual_renew_never_reacquires_or_steals_automation(service, clock):
    client = create_app(service).test_client()
    assert post(client, "/api/manual/heartbeat").status_code == 200
    clock.advance(4)
    assert_error(post(client, "/api/manual/renew"), 409)
    assert service.lifecycle.lease.snapshot()["holder"] is None
    assert post(client, "/api/heartbeat", actor="automation").status_code == 200
    assert_error(post(client, "/api/manual/renew"), 409)
    assert service.lifecycle.lease.snapshot()["holder"] == "automation"


def test_manual_release_starts_grace_without_stopping_bounded_run(service, clock):
    client = create_app(service).test_client()
    assert (
        post(
            client, "/api/furnace/apply", {"temperature_c": 30, "ramp_rate_c_per_min": 5}
        ).status_code
        == 200
    )
    assert post(client, "/api/manual/release").status_code == 200
    state = service.lifecycle.poll_once()
    assert state["orphan_time"] is not None and state["run_state_code"] == 0


def test_sequence_routes_preserve_editor_and_gas_plan(service):
    client = create_app(service).test_client()
    body = dict(
        steps=[dict(temperature_c=30, ramp_rate_c_per_min=5, hold_minutes=1, gas={"0": 10})],
        end_gas={"0": 0},
        end_gas_until_c=26,
    )
    assert post(client, "/api/sequence", body, method="PUT").status_code == 200
    assert post(client, "/api/sequence/start", body).status_code == 200
    state = client.get("/api/state").get_json()
    assert state["busy"] and state["sequence"]["running"]
    assert state["last_command"]["result"] == "ok"
    assert state["flow"]["gas_channels"][0]["target_percent"] == 10
    assert post(client, "/api/sequence/stop", actor="bystander").get_json()["stop"] == "confirmed"


def test_stop_stays_available_after_expiry_callback_failure(service, clock):
    client = create_app(service).test_client()
    post(client, "/api/heartbeat", actor="automation")

    def failed():
        raise RuntimeError("expiry failed")

    service.lifecycle.lease.on_expire = failed
    clock.advance(4)
    with pytest.raises(RuntimeError):
        service.lifecycle.lease.poll()
    assert post(client, "/api/stop", actor="other").status_code == 200
    assert (
        post(client, "/api/flow/gas/stop", {"channel_index": 0}, actor="other").status_code == 200
    )


def test_stop_refuses_false_confirmation_for_silent_gas(service):
    service.lifecycle.connect_flow_controller(
        client=service.lifecycle.lease.acquire("setup")["holder"]
    )
    service.lifecycle.flow_controller.serial.reply_ascii = False
    client = create_app(service).test_client()
    response = post(client, "/api/stop", actor="other")
    assert response.status_code == 200
    assert response.get_json()["stop"] == "unconfirmed"


def test_contract_command_has_completed_id_and_events(service):
    client = create_app(service).test_client()
    post(client, "/api/heartbeat", actor="automation")
    response = post(
        client,
        "/api/command",
        {"action": "apply_setpoint", "temperature_c": 30, "ramp_rate_c_per_min": 5},
        actor="automation",
    )
    assert response.status_code == 202
    result = response.get_json()
    assert result["command_id"] == result["state"]["last_command"]["id"]
    assert result["state"]["last_command"]["result"] == "ok"
    assert client.get("/api/events").get_json()["events"]


@pytest.mark.parametrize(
    "body", [[], {"wait_s": 10**1000}, {"wait_s": True}, {"wait_s": float("nan")}]
)
def test_malformed_stop_is_rejected_before_device_access(service, body):
    client = create_app(service).test_client()
    assert_error(post(client, "/api/stop", body), 400)


def test_configuration_errors_do_not_create_a_server_or_service():
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        raise AssertionError("unexpected startup")

    with pytest.raises(SystemExit) as result:
        main(
            ["--bind", "0.0.0.0", "--demo"],
            environ={},
            service_factory=factory,
            server_factory=factory,
        )
    assert result.value.code == 2 and calls == []


def test_bad_base_path_cleanup_never_opens_configured_devices(monkeypatch):
    with pytest.raises(SystemExit) as result:
        main(
            ["--base-path", "//invalid"],
            environ={
                "MG400_FURNACE_SERIAL": "/dev/must-not-open",
                "MG400_MFC_SERIAL": "/dev/must-not-open-mfc",
            },
            server_factory=lambda *a, **k: (_ for _ in ()).throw(AssertionError("server created")),
        )
    assert result.value.code == 2


def test_cli_demo_signals_stop_and_close_fake_server(service, monkeypatch):
    events = []
    original_start = service.start
    monkeypatch.setattr(service, "start", lambda: original_start(background=False))

    class Server:
        def run(self):
            events.append("run")
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)

        def close(self):
            events.append("close")

    def server(app, **options):
        assert options["port"] == 8769 and options["host"] == "127.0.0.1"
        assert options["clear_untrusted_proxy_headers"] is False
        return Server()

    assert (
        main(["--demo"], environ={}, service_factory=lambda **kw: service, server_factory=server)
        == 0
    )
    assert events == ["run", "close"]
    assert service.lifecycle.status()["run_state_code"] == 1


def test_browser_script_syntax_and_no_token_persistence():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "src/furnace/static/app.js"
    subprocess.run(["node", "--check", str(path)], check=True, capture_output=True, timeout=10)
    text = path.read_text()
    assert "localStorage" not in text and "sessionStorage.setItem('token'" not in text
    assert "'/api/manual/renew'" in text


def test_manual_dispatch_retains_lease_during_racing_automation_acquire(service, monkeypatch):
    monkeypatch.setattr(threading.Thread, "start", THREAD_START)
    entered, release, attempted, acquired = (threading.Event() for _ in range(4))
    original = service.lifecycle.apply_setpoint
    results = {}
    app = create_app(service)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.lifecycle, "apply_setpoint", blocked)

    def manual():
        results["manual"] = post(
            app.test_client(), "/api/furnace/apply", {"temperature_c": 30, "ramp_rate_c_per_min": 5}
        ).status_code

    def automation():
        attempted.set()
        results["automation"] = post(
            app.test_client(), "/api/heartbeat", actor="automation"
        ).status_code
        acquired.set()

    first, second = threading.Thread(target=manual), threading.Thread(target=automation)
    first.start()
    try:
        assert entered.wait(2)
        second.start()
        assert attempted.wait(2)
        assert not acquired.wait(0.05)
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert results == {"manual": 200, "automation": 409}
    assert post(app.test_client(), "/api/manual/release").status_code == 200
    assert post(app.test_client(), "/api/heartbeat", actor="automation").status_code == 200
    assert_error(post(app.test_client(), "/api/manual/heartbeat", actor="automation"), 409)


def test_stop_finishes_while_expiry_callback_is_pending(service, clock, monkeypatch):
    monkeypatch.setattr(threading.Thread, "start", THREAD_START)
    entered, release = threading.Event(), threading.Event()
    errors = []
    original = service.lifecycle.lease.on_expire

    def expiry():
        entered.set()
        assert release.wait(2)
        original()

    service.lifecycle.lease.on_expire = expiry
    service.lease.acquire("automation")
    clock.advance(4)

    def poll():
        try:
            service.lease.poll()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=poll)
    worker.start()
    try:
        assert entered.wait(2)
        response = post(create_app(service).test_client(), "/api/stop", actor="bystander")
        assert response.status_code == 200 and response.get_json()["stop"] == "confirmed"
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive() and not errors


def test_page_dom_keeps_stop_available_and_renew_never_reacquires():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "src/furnace/static/app.js"
    harness = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor(dataset={}) { this.dataset=dataset;
  this.children=[];
  this.nodes=new Map();
  this.listeners={};
  this.value='';
  this.disabled=false;
  this.classList={toggle(){}};
  }
  querySelector(selector) { if (!this.nodes.has(selector)) this.nodes.set(selector,new Element());
  return this.nodes.get(selector);
  }
  querySelectorAll(selector) { return this.nodes.get(selector) || []; }
  addEventListener(event, callback) { this.listeners[event]=callback; }
  appendChild(child) { this.children.push(child); }
  hasAttribute(name) { return name==='data-allow-locked' && this.allowLocked; }
  set innerHTML(value) { this.children=[]; }
}
const nodes = new Map();
const get = selector => { if(!nodes.has(selector)) nodes.set(selector,new Element());
  return nodes.get(selector);
  };
const manualButtons=['#acknowledge' ,
  '#connectFurnace' ,
  '#connectFlow' ,
  '#applySetpoint' ,
  '#updateSequence' ,
  '#startSequence'].map(get);
get('#acknowledge').allowLocked=get('#connectFurnace').allowLocked=true;
const gasRows=[0,1,2].map(index=>new Element({channelIndex:String(index)}));
const endGas=[0,1,2].map(index=>new Element({endGas:String(index)}));
const requests=[], stored=[];
let state={connected:true ,
  locked:false ,
  error:null ,
  lease:{holder:null} ,
  manual_client:null ,
  pv:25 ,
  sv:null ,
  ramp:5 ,
  run_state:'STOP' ,
  control_mode:'STOPPED' ,
  flow:{connected:true ,
  error:null ,
  gas_channels:[] ,
  gas_valves:[]} ,
  sequence:{running:false ,
  phase:'idle' ,
  current_index:0 ,
  steps:[]}};
let rejectRenew=false;
const context={
  console, assert, requests, get, stored,
  document:{body:{dataset:{basePath:'/heat'}},activeElement:null,querySelector:get,
    querySelectorAll(selector){ return selector==='[data-manual]' ? manualButtons
      : selector==='[data-end-gas]' ? endGas : selector==='.gas-flow-row' ? gasRows : [];
  } ,
  
    createElement(){ const row=new Element();
  row.nodes.set('[data-field]' ,
  ['temperature_c' ,
  'ramp_rate_c_per_min' ,
  'hold_minutes'].map(field=>new Element({field})));
  row.nodes.set('[data-gas]' ,
  [0 ,
  1 ,
  2].map(gas=>new Element({gas:String(gas)})));
  return row;
  }} ,
  
  sessionStorage:{getItem(){return null;} ,
  setItem(...args){stored.push(args);}} ,
  crypto:{randomUUID(){return 'test';}} ,
  setInterval(){} ,
  
  fetch:async(url,
  options)=>{ requests.push({url,options});
  const refused=rejectRenew&&url.endsWith('/manual/renew');
  return {ok:!refused,json:async()=>refused?{error:'expired'}:{furnace:state,ok:true}};
  },
  setState(value){state=value;},getState(){return state;},rejectRenew(){rejectRenew=true;},
};
const checks = `
(async()=>{
 await refresh();
 get('#accessToken').value='memory-only-test-token';
 get('#useToken').listeners.click();
 await refresh();
 assert.equal(get('#accessToken').value,'');
 assert.equal(JSON.stringify(stored),JSON.stringify([['furnace-client','page-test']]));
 assert(requests.some(r=>r.options.headers.Authorization==='Bearer memory-only-test-token'));
 assert(requests.every(r=>!r.url.includes('token')&&!String(r.options.body).includes('memory-only')));
 const automated={...getState(),lease:{holder:'automation'},manual_client:null};
 setState(automated);render(automated);
 assert.equal(get('#applySetpoint').disabled,true);
 assert.equal(get('#startSequence').disabled,true);
 for(const name of ['#stopFurnace' ,
  '#stopSequence' ,
  '#stopAllFlow']) assert.equal(get(name).disabled ,
  false);
 const owned={...automated,lease:{holder:client},manual_client:client};
 setState(owned);render(owned);rejectRenew();
 const before=requests.length;
 setState({...owned,lease:{holder:null},manual_client:null});
 await tick();await tick();
 const paths=requests.slice(before).map(r=>r.url);
 assert.equal(paths.filter(p=>p.endsWith('/manual/renew')).length,1);
 assert(!paths.some(p=>p.endsWith('/heartbeat')));
 const row=rows.children[0];
 row.querySelectorAll('[data-field]')[0].value='35';
 row.querySelectorAll('[data-gas]')[0].value='10';
 get('#endGasUntil').value='26';
 const endGasInputs=document.querySelectorAll('[data-end-gas]');
 endGasInputs.forEach(input=>input.value='0');
 const recipe=collectRecipe();
 assert.equal(recipe.steps[0].temperature_c,35);
 assert.deepEqual(recipe.steps[0].gas,{'0':10});
 assert.equal(recipe.end_gas_until_c,26);
 assert.deepEqual(recipe.end_gas,{'0':0,'1':0,'2':0});
})();`;
vm.runInNewContext(fs.readFileSync(process.argv[1] ,
  'utf8')+'\n'+checks ,
  context).catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run(
        ["node", "-e", harness, str(path)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
