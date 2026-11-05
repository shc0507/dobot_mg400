"""Lab Console relay, lease rules, stop, events and readiness with in-process fakes."""

import copy
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from labkit.errors import ServiceError
from orchestrator.app import OrchestratorService, create_app
from orchestrator.clients import SERVICES
from orchestrator.web import console as console_module

PRIVATE = "private-stage-service-token-0123456789"


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out")


class Device:
    """A fake contract service with one lease, commands and an event log."""

    def __init__(self, name):
        self.name = name
        self.lock = threading.Lock()
        self.holder = None
        self.calls = []
        self.fail = {}
        self.replies = {}
        self.events = []
        self.seq = 0
        self.state = {
            "connected": True,
            "busy": False,
            "locked": False,
            "fault": None,
            "demo": True,
            "syncing": True,
            "feedback_fresh": True,
            "robot_error": False,
            "stop_unconfirmed": False,
        }
        self.commands = {
            "move": {"params": {"type": "object"}, "any_client": False},
            "clear": {"params": {"type": "object"}, "any_client": True},
            "start_sync": {"params": {"type": "object"}, "any_client": False},
        }

    def event(self, level, message, ts):
        self.seq += 1
        self.events.append(
            {"seq": self.seq, "ts_unix": ts, "level": level, "message": message, "fields": {}}
        )

    def _maybe_fail(self, call):
        error = self.fail.get(call)
        if error is not None:
            raise error

    def adapter(self, identity):
        device = self

        class Adapter:
            def __init__(self):
                self.identity = identity
                self.timeout = 2

            def describe(self):
                device.calls.append(("describe", identity))
                return {
                    "contract": 1,
                    "demo": True,
                    "lease_seconds": 3,
                    "commands": device.commands,
                }

            def state(self):
                with device.lock:
                    return {
                        **copy.deepcopy(device.state),
                        "lease": {"holder": device.holder, "remaining_s": 3},
                        "controller_id": device.holder,
                    }

            def owned(self, state):
                return state["lease"]["holder"] == identity

            def acquire(self):
                device.calls.append(("acquire", identity))
                device._maybe_fail("acquire")
                with device.lock:
                    if device.holder not in (None, identity):
                        raise ServiceError(409, "Another client holds the control lease.")
                    device.holder = identity

            def heartbeat(self):
                device.calls.append(("renew", identity))
                device._maybe_fail("renew")
                with device.lock:
                    if device.holder != identity:
                        raise RuntimeError("Service lease was lost.")

            def run(self, action, *, timeout, **params):
                device.calls.append(("run", identity, action))
                device._maybe_fail("run")
                return {}

            def release(self):
                device.calls.append(("release", identity))
                with device.lock:
                    if device.holder not in (None, identity):
                        raise ServiceError(409, "Another client holds the control lease.")
                    device.holder = None

            def command(self, action, **params):
                device.calls.append(("command", identity, action, params))
                device._maybe_fail("command")
                spec = device.commands[action]
                with device.lock:
                    if not spec["any_client"] and device.holder != identity:
                        raise ServiceError(409, "Another client holds the control lease.")
                return {"command_id": "c-1"}

            def stop(self):
                device.calls.append(("stop", identity))
                device._maybe_fail("stop")
                return {"stop": "confirmed"}

            def events(self, since=0):
                return {
                    "events": [e for e in device.events if e["seq"] > since],
                    "next": device.seq,
                }

            def request(self, path, data=None):
                # Operator routes: a POST needs this identity's lease.
                device.calls.append(("request", identity, path, data))
                device._maybe_fail("request")
                with device.lock:
                    if data is not None and device.holder != identity:
                        raise ServiceError(
                            409, "Acquire the control lease before sending a command."
                        )
                return copy.deepcopy(device.replies.get(path, {"command_id": "op-1"}))

            def gas_stop(self, channel):
                return {"ok": False}

            def close(self):
                device.calls.append(("close", identity))

        return Adapter()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Console test attempted real I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


@pytest.fixture
def rig(tmp_path):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    devices = {name: Device(name) for name in SERVICES}
    clock = Clock()

    def factory(required, identity):
        return {name: devices[name].adapter(identity) for name in required}

    service = OrchestratorService(
        settings={
            "MG400_ORCH_STAGE_TOKEN": PRIVATE,
            "MG400_ORCH_STAGE_URL": "http://private.invalid:9876",
            "MG400_CONSOLE_COMMIT": "abc1234",
        },
        recipes_dir=recipes,
        demo=True,
        client_factory=factory,
        clock=clock,
    ).start(background=False)
    app = create_app(service, base_path="/lab")
    yield SimpleNamespace(
        service=service, devices=devices, app=app, client=app.test_client(), clock=clock
    )
    service.close()


def headers(page="console-page-a"):
    result = {}
    if page:
        result["X-MG400-Client"] = page
    return result


def post(rig, path, data=None, page="console-page-a"):
    return rig.client.post(
        "/lab/api/console/" + path, json={} if data is None else data, headers=headers(page)
    )


def test_shell_and_apis_need_no_token(rig):
    page = rig.client.get("/lab/")
    assert page.status_code == 200
    text = page.get_data(as_text=True)
    assert "STOP ALL MOTION" in text and "abc1234" in text
    for name in ("overview", "motion", "cameras", "process"):
        assert f'data-workspace="{name}"' in text
        assert rig.client.get(f"/lab/console-static/{name}.js").status_code == 200
    assert 'href="/lab/chip"' in text and rig.client.get("/lab/chip").status_code == 200
    assert PRIVATE not in text and "lc-token" not in text
    assert "Access-Control-Allow-Origin" not in page.headers
    assert rig.client.get("/lab/console-static/shell.js").status_code == 200
    assert rig.client.get("/lab/console-static/..%2Fconsole.py").status_code == 404
    assert rig.client.get("/lab/console-static/Shell.py").status_code == 404
    assert rig.client.get("/lab/dashboard/", headers=headers()).status_code == 404
    assert rig.client.get("/lab/api/console/state").status_code == 200
    foreign = rig.client.get(
        "/lab/api/console/state", headers={**headers(), "Origin": "http://foreign.invalid"}
    )
    assert foreign.status_code == 200


def test_state_reports_frame_data_without_secrets(rig):
    response = rig.client.get("/lab/api/console/state", headers=headers())
    assert response.status_code == 200
    body = response.get_json()
    assert body["console"]["commit"] == "abc1234"
    assert [item["id"] for item in body["workspaces"]] == [
        "overview",
        "motion",
        "cameras",
        "process",
    ]
    assert "readiness" not in body
    assert PRIVATE not in response.get_data(as_text=True)
    assert "Access-Control-Allow-Origin" not in response.headers


def test_take_relay_and_release_use_one_fresh_console_identity(rig):
    stage = rig.devices["stage"]
    response = post(rig, "control/stage/take")
    assert response.status_code == 200
    holder = response.get_json()["control"]["holder"]
    assert holder.startswith("console-control-") and stage.holder == holder
    sent = post(rig, "command/stage", {"action": "move", "params": {"motor": 1}})
    assert sent.status_code == 202 and sent.get_json()["outcome"] == "accepted"
    assert ("command", holder, "move", {"motor": 1}) in stage.calls
    released = post(rig, "control/stage/release")
    assert released.get_json()["result"] == "released"
    assert stage.holder is None and ("close", holder) in stage.calls


def test_take_never_takes_over_another_holder(rig):
    stage = rig.devices["stage"]
    stage.holder = "stage-panel"
    response = post(rig, "control/stage/take")
    assert response.status_code == 409
    assert stage.holder == "stage-panel"
    assert not any(call[0] == "release" for call in stage.calls)
    assert rig.service.console.control() == []


def test_a_second_page_cannot_take_or_release_this_pages_control(rig):
    assert post(rig, "control/robot/take").status_code == 200
    assert post(rig, "control/robot/take", page="console-page-b").status_code == 409
    assert post(rig, "control/robot/release", page="console-page-b").status_code == 409
    other = post(rig, "command/robot", {"action": "move"}, page="console-page-b")
    assert other.status_code == 409 and "Take control" in other.get_json()["error"]
    state = rig.client.get("/lab/api/console/state", headers=headers("console-page-b"))
    assert [item["mine"] for item in state.get_json()["control"]] == [False]


def test_relay_allows_only_described_commands_and_no_lease_actions(rig):
    assert post(rig, "control/stage/take").status_code == 200
    for action in ("unknown", "start_sync", "release"):
        response = post(rig, "command/stage", {"action": action})
        assert response.status_code == 400
    assert post(rig, "command/stage", {"action": "move", "extra": 1}).status_code == 400
    assert post(rig, "command/stage", {"action": "move", "params": []}).status_code == 400
    assert post(rig, "command/nope", {"action": "move"}).status_code == 404
    assert not any(call[0] == "command" for call in rig.devices["stage"].calls)


def test_any_client_command_needs_no_control(rig):
    response = post(rig, "command/furnace", {"action": "clear"})
    assert response.status_code == 202
    assert rig.devices["furnace"].holder is None
    refused = post(rig, "command/furnace", {"action": "move"})
    assert refused.status_code == 409
    assert not any(
        call[0] == "command" and call[2] == "move" for call in rig.devices["furnace"].calls
    )


def test_unknown_command_outcome_is_reported_and_never_retried(rig):
    stage = rig.devices["stage"]
    assert post(rig, "control/stage/take").status_code == 200
    stage.fail["command"] = ServiceError(0, "Service request failed: timed out")
    response = post(rig, "command/stage", {"action": "move"})
    assert response.status_code == 502 and response.get_json()["outcome"] == "unknown"
    assert sum(call[0] == "command" for call in stage.calls) == 1
    stage.fail["command"] = ServiceError(409, "The service is locked.")
    refused = post(rig, "command/stage", {"action": "move"})
    assert refused.status_code == 409 and refused.get_json()["outcome"] == "refused"


def test_stop_needs_no_control_and_is_sent_once(rig):
    stage = rig.devices["stage"]
    stage.holder = "stage-panel"
    response = post(rig, "stop/stage", page="console-page-z")
    assert response.get_json() == {"device": "stage", "outcome": "confirmed"}
    assert stage.holder == "stage-panel"
    assert not any(call[0] in {"acquire", "describe"} for call in stage.calls)
    stage.fail["stop"] = TimeoutError()
    unknown = post(rig, "stop/stage")
    assert unknown.get_json()["outcome"] == "unknown"
    assert sum(call[0] == "stop" for call in stage.calls) == 2


def test_heartbeat_renews_and_missing_heartbeat_releases_after_3_s(rig, monkeypatch):
    monkeypatch.setattr(console_module, "WATCH_INTERVAL_S", 0.01)
    stage = rig.devices["stage"]
    holder = post(rig, "control/stage/take").get_json()["control"]["holder"]
    assert post(rig, "heartbeat").status_code == 200
    wait_for(lambda: ("renew", holder) in stage.calls)
    # A heartbeat from another page renews nothing.
    count = sum(call[0] == "renew" for call in stage.calls)
    post(rig, "heartbeat", page="console-page-b")
    time.sleep(0.05)
    assert sum(call[0] == "renew" for call in stage.calls) == count
    rig.clock.value += 2.9
    time.sleep(0.05)
    assert stage.holder == holder
    rig.clock.value += 0.2
    wait_for(lambda: stage.holder is None)
    assert ("release", holder) in stage.calls
    assert rig.service.console.control() == []


def lease_calls(device, holder):
    return [
        call[:3] if call[0] == "run" else call[0]
        for call in device.calls
        if call[1] == holder and call[0] in {"run", "release"}
    ]


def test_robot_release_disables_the_arm_before_it_ends_control(rig):
    robot = rig.devices["robot"]
    holder = post(rig, "control/robot/take").get_json()["control"]["holder"]
    released = post(rig, "control/robot/release")
    assert released.get_json()["result"] == "released"
    assert lease_calls(robot, holder) == [("run", holder, "disable"), "release"]
    # Other devices release without a disable.
    stage = rig.devices["stage"]
    other = post(rig, "control/stage/take").get_json()["control"]["holder"]
    post(rig, "control/stage/release")
    assert lease_calls(stage, other) == ["release"]


def test_missing_heartbeat_disables_the_robot_before_it_ends_control(rig, monkeypatch):
    monkeypatch.setattr(console_module, "WATCH_INTERVAL_S", 0.01)
    robot = rig.devices["robot"]
    holder = post(rig, "control/robot/take").get_json()["control"]["holder"]
    rig.clock.value += 3.1
    wait_for(lambda: robot.holder is None)
    assert lease_calls(robot, holder) == [("run", holder, "disable"), "release"]
    assert rig.service.console.control() == []


def test_robot_release_ends_control_when_the_disable_fails(rig):
    robot = rig.devices["robot"]
    holder = post(rig, "control/robot/take").get_json()["control"]["holder"]
    robot.fail["run"] = RuntimeError("Reply timed out; command delivery may be unknown.")
    released = post(rig, "control/robot/release")
    assert released.get_json()["result"] == "unconfirmed"
    assert lease_calls(robot, holder) == [("run", holder, "disable"), "release"]
    assert robot.holder is None and rig.service.console.control() == []


def test_lost_lease_is_never_reacquired_or_released_for_the_new_holder(rig):
    stage = rig.devices["stage"]
    holder = post(rig, "control/stage/take").get_json()["control"]["holder"]
    stage.holder = "stage-panel"
    post(rig, "heartbeat")
    wait_for(lambda: rig.service.console.control() == [])
    assert stage.holder == "stage-panel"
    assert ("release", holder) not in stage.calls
    assert sum(call[0] == "acquire" for call in stage.calls) == 1


def test_page_close_and_release_all_release_only_console_leases(rig):
    post(rig, "control/stage/take")
    post(rig, "control/furnace/take", page="console-page-b")
    rig.devices["robot"].holder = "robot-panel"
    closed = post(rig, "release-page")
    assert closed.get_json()["results"] == {"stage": "released"}
    assert rig.devices["furnace"].holder is not None
    everything = post(rig, "release-all")
    assert everything.get_json()["results"] == {"furnace": "released"}
    assert rig.devices["robot"].holder == "robot-panel"


def test_ambiguous_take_cleans_up_its_own_identity_once(rig):
    stage = rig.devices["stage"]
    stage.fail["acquire"] = ServiceError(0, "Service request failed: reset")
    response = post(rig, "control/stage/take")
    assert response.status_code == 502
    assert [call[0] for call in stage.calls if call[0] in {"acquire", "release"}] == [
        "acquire",
        "release",
    ]
    assert rig.service.console.control() == []


def test_events_merge_newest_first_and_reset_after_service_restart(rig):
    rig.devices["stage"].event("info", "Stage moved", 10.0)
    rig.devices["robot"].event("warning", "Robot alarm " + PRIVATE, 12.0)
    rig.devices["stage"].event("error", "Stage fault", 11.0)
    console = rig.service.console
    console._collect()
    rows = console.event_report()["events"]
    service_rows = [row for row in rows if row["service"] != "runs"]
    assert [row["message"] for row in service_rows] == [
        "Robot alarm [redacted]",
        "Stage fault",
        "Stage moved",
    ]
    console._collect()
    assert len([row for row in console.event_report()["events"] if row["service"] != "runs"]) == 3
    stage = rig.devices["stage"]
    stage.events, stage.seq = [], 0
    stage.event("info", "Restarted", 20.0)
    console._collect()
    stage_rows = [row for row in console.event_report()["events"] if row["service"] == "stage"]
    assert [row["message"] for row in stage_rows] == ["Restarted"]


def test_rehearsal_console_profile_adds_events_only():
    from orchestrator.rehearsal import _DemoClient

    class Owner:
        _condition = threading.Condition()
        _active = True
        _operations = 0

    class Adapter:
        identity, timeout = "console-x", 2

        def events(self, since=0):
            return {"events": [], "next": 0}

        def close(self):
            pass

    runner = _DemoClient(Owner(), Adapter())
    with pytest.raises(AttributeError):
        runner.events
    console = _DemoClient(Owner(), Adapter(), profile="console")
    assert console.events() == {"events": [], "next": 0}
    with pytest.raises(AttributeError):
        console.renew
    with pytest.raises(ValueError):
        _DemoClient(Owner(), Adapter(), profile="other")


def test_workspace_registry_needs_unique_ids_and_both_titles():
    from orchestrator.web.workspaces import Workspace, discover

    assert [item.id for item in discover()][0] == "overview"
    with pytest.raises(ValueError):
        Workspace(id="Bad Id", order=1, title={"en": "x", "zh": "x"})
    with pytest.raises(ValueError):
        Workspace(id="good", order=1, title={"en": "x"})


def test_commit_reads_a_setting_or_the_checkout_head(tmp_path):
    assert console_module.deployed_commit({"MG400_CONSOLE_COMMIT": " 1234567 "}) == "1234567"
    git = tmp_path / ".git"
    (git / "refs/heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs/heads/main").write_text("0123456789abcdef0123456789abcdef01234567\n")
    assert console_module.deployed_commit({}, tmp_path) == "0123456"
    assert console_module.deployed_commit({}, tmp_path / "missing") == "unknown"


def test_proxy_root_redirects_to_the_console():
    from pathlib import Path

    caddy = (Path(__file__).parents[1] / "config/proxy/Caddyfile").read_text()
    assert "\tredir / /lab/ 302\n" in caddy
    assert caddy.index("redir / /lab/") < caddy.index("handle /lab/*")


def test_an_unconfigured_service_stays_not_configured_across_refreshes():
    from orchestrator.web.backend import Dashboard

    reads = threading.Event()

    def client(name, identity):
        if name == "robot":
            assert reads.wait(3)
        return None

    def summary():
        raise RuntimeError("No summary in this test.")

    service = SimpleNamespace(
        settings={},
        demo=False,
        dashboard_configured=lambda name: name == "robot",
        dashboard_client=client,
        dashboard_summary=summary,
    )
    dashboard = Dashboard(service)
    try:
        for _ in range(5):
            dashboard.refresh()
            phases = {card["id"]: card["phase"] for card in dashboard.snapshot()["services"]}
            assert phases["microscope"] == "unconfigured"
            # A configured service shows loading until its first read settles.
            assert phases["robot"] == "loading"
            wait_for(lambda: dashboard.reading <= {"robot"})
    finally:
        reads.set()
        wait_for(lambda: not dashboard.threads)
