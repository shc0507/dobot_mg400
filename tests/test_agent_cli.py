"""labctl agent groups with a fake console transport: parsing, JSON, exit codes, waits, stops."""

import io
import json

import pytest

from orchestrator import agent as agent_module
from orchestrator import agent_cli
from orchestrator.agent import Agent, Unreachable, code_for, exit_code, parse_params


class FakeHttp:
    """Answer console paths from a table of handlers; record every request."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def request(self, path, data=None, *, raw=False):
        self.calls.append((path, data))
        handler = self.routes.get(path)
        if handler is None:
            return 404, {"error": "Not found."}
        if isinstance(handler, Exception):
            raise handler
        return handler(data) if callable(handler) else handler

    def sent(self, path):
        return [data for item, data in self.calls if item == path]


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def make(routes, *, direct=False, **kwargs):
    clock = Clock()
    http = FakeHttp(routes)
    agent = Agent(
        session="t", http=http, settings={}, direct=direct, clock=clock, sleep=clock.sleep, **kwargs
    )
    return agent, http


def beat(*entries):
    return 200, {"control": [dict(entry, mine=True) for entry in entries]}


def states(*values):
    """Return a state handler that steps through values and then repeats the last."""
    values = list(values)

    def handler(data):
        state = values.pop(0) if len(values) > 1 else values[0]
        return 200, {"device": "x", "state": state, "control": None}

    return handler


def run_cli(argv, agent):
    out = io.StringIO()
    code = agent_cli.main(argv, out=out, agent_factory=lambda url, session: agent)
    lines = out.getvalue().splitlines()
    assert len(lines) == 1
    return code, json.loads(lines[0])


def test_values_are_json_strings_or_files(tmp_path):
    path = tmp_path / "p.json"
    path.write_text('{"a": [1, 2]}')
    params = parse_params(
        ["n=3", "s=bottom", "b=true", f"f=@{path}", "i=@-"], io.StringIO('{"x": 1}')
    )
    assert params == {"n": 3, "s": "bottom", "b": True, "f": {"a": [1, 2]}, "i": {"x": 1}}
    for bad in (["novalue"], ["=1"], ["a=1", "a=2"]):
        with pytest.raises(ValueError):
            parse_params(bad)


def test_error_codes_and_exit_codes():
    assert code_for(409, "Take control of this device first.") == "not_controlled"
    assert code_for(409, "Another console page controls this device.") == "held_by_other"
    assert code_for(409, "The service is locked.") == "locked"
    assert code_for(409, "A run is using this device.") == "busy"
    assert code_for(400, "x") == "invalid" and code_for(0, "x") == "unreachable"
    assert exit_code({"ok": True}) == 0
    assert exit_code({"ok": False, "outcome": "refused", "error": {"code": "refused"}}) == 1
    assert exit_code({"ok": False, "outcome": "refused", "error": {"code": "usage"}}) == 2
    assert exit_code({"ok": False, "outcome": "unknown", "error": {"code": "timeout"}}) == 3
    assert exit_code({"ok": False, "outcome": "interrupted", "error": {}}) == 130


def test_usage_errors_print_one_json_object_and_exit_2():
    agent, http = make({})
    code, result = run_cli(["robot", "fly"], agent)
    assert code == 2 and result["error"]["code"] == "usage"
    code, result = run_cli(["stage", "move", "bottom", "0"], agent)
    assert code == 2 and http.calls == []


def test_session_option_works_before_and_after_the_verb():
    seen = []

    def factory(url, session):
        seen.append(session)
        return make({"api/console/state/robot": states({})})[0]

    for argv in (["robot", "--session", "a", "state"], ["robot", "state", "--session", "b"]):
        agent_cli.main(argv, out=io.StringIO(), agent_factory=factory)
    assert seen == ["a", "b"]


def test_contract_command_waits_for_its_own_result():
    pending = {"busy": True, "last_command": {"id": "41", "result": "ok"}}
    done = {"busy": False, "last_command": {"id": "42", "result": "ok", "message": "Done."}}
    agent, http = make(
        {
            "api/console/heartbeat": beat({"device": "stage", "holder": "agent-t-1"}),
            "api/console/state/stage": states(pending, pending, pending, done),
            "api/console/command/stage": (202, {"outcome": "accepted", "command_id": "42"}),
        }
    )
    code, result = run_cli(["stage", "move", "bottom", "-100", "--speed", "20"], agent)
    assert code == 0 and result["outcome"] == "ok" and result["message"] == "Done."
    assert result["state"] == done
    assert http.sent("api/console/command/stage") == [
        {
            "action": "move",
            "params": {"motor": "bottom", "direction": "ccw", "steps": 100, "speed": 20},
        }
    ]


@pytest.mark.parametrize(
    ("result", "code", "outcome"),
    [("failed", 1, "failed"), ("stopped", 1, "stopped"), ("rejected", 1, "rejected")],
)
def test_contract_failures_exit_1(result, code, outcome):
    final = {"busy": False, "last_command": {"id": "9", "result": result, "message": "No."}}
    agent, _ = make(
        {
            "api/console/heartbeat": beat({"device": "furnace", "holder": "h"}),
            "api/console/state/furnace": states({"last_command": None}, final),
            "api/console/command/furnace": (202, {"command_id": "9"}),
        }
    )
    exit_, report = run_cli(["furnace", "gas", "1", "20"], agent)
    assert exit_ == code and report["outcome"] == outcome and report["error"]["message"] == "No."


def test_a_newer_command_of_this_session_makes_the_outcome_unknown():
    newer = {"last_command": {"id": "10", "result": "ok", "client": "h"}}
    agent, _ = make(
        {
            "api/console/heartbeat": beat({"device": "stage", "holder": "h"}),
            "api/console/state/stage": states({"last_command": {"id": "8"}}, newer),
            "api/console/command/stage": (202, {"command_id": "9"}),
        }
    )
    code, result = run_cli(["stage", "home", "top"], agent)
    assert code == 3 and result["outcome"] == "unknown"


def test_timeout_is_unknown_and_never_resends():
    agent, http = make(
        {
            "api/console/heartbeat": beat({"device": "stage", "holder": "h"}),
            "api/console/state/stage": states({"busy": True, "last_command": None}),
            "api/console/command/stage": (202, {"command_id": "9"}),
        }
    )
    code, result = run_cli(["stage", "pose", "load", "--timeout", "2"], agent)
    assert code == 3 and result["error"]["code"] == "timeout"
    assert len(http.sent("api/console/command/stage")) == 1
    # The wait beats the agent page about every second.
    assert len(http.sent("api/console/heartbeat")) >= 2


def robot_state(last, **fields):
    return {"instance": "i1", "busy": False, "locked": False, "last_command": last, **fields}


def test_robot_move_uses_the_shared_completion_predicate_and_relative_pose():
    before = robot_state({"id": "i1:1", "action": "enable", "client": "h", "result": "ok"})
    before["actual"] = [250.0, 10.0, 40.0, 30.0]
    after = robot_state({"id": "i1:2", "action": "execute", "client": "h", "result": "ok"})
    agent, http = make(
        {
            "api/console/heartbeat": beat({"device": "robot", "holder": "h"}),
            "api/console/state/robot": states(before, before, after),
            "api/console/command/robot": (202, {"outcome": "accepted", "command_id": None}),
        }
    )
    code, result = run_cli(["robot", "move", "0", "0", "-10", "0", "--relative"], agent)
    assert code == 0 and result["outcome"] == "ok"
    assert http.sent("api/console/command/robot") == [
        {"action": "execute", "params": {"targets": {"x": 250.0, "y": 10.0, "z": 30.0, "r": 30.0}}}
    ]


def test_relative_move_waits_briefly_for_a_known_pose():
    moving = robot_state(None, actual=[None] * 4)
    agent, http = make(
        {
            "api/console/heartbeat": beat({"device": "robot", "holder": "h"}),
            "api/console/state/robot": states(moving),
        }
    )
    code, result = run_cli(["robot", "move", "0", "0", "1", "0", "--relative"], agent)
    assert code == 1 and result["error"]["code"] == "busy"
    assert http.sent("api/console/command/robot") == []


def test_robot_superseding_stop_is_unknown_and_alarms_give_the_clear_hint():
    before = robot_state({"id": "i1:1", "action": "enable", "client": "h", "result": "ok"})
    stopped = robot_state({"id": "i1:2", "action": "stop", "client": "x", "result": "ok"})
    agent, _ = make(
        {
            "api/console/heartbeat": beat({"device": "robot", "holder": "h"}),
            "api/console/state/robot": states(before, stopped),
            "api/console/command/robot": (202, {"command_id": None}),
        }
    )
    code, result = run_cli(["robot", "home"], agent)
    assert code == 3 and result["outcome"] == "unknown"
    failed = robot_state(
        {"id": "i1:2", "action": "home", "client": "h", "result": "failed"}, alarms=[{"id": 1}]
    )
    agent, _ = make(
        {
            "api/console/heartbeat": beat({"device": "robot", "holder": "h"}),
            "api/console/state/robot": states(before, failed),
            "api/console/command/robot": (202, {"command_id": None}),
        }
    )
    code, result = run_cli(["robot", "home"], agent)
    assert code == 1 and result["outcome"] == "failed"
    assert result["hint"] == "labctl robot clear-alarm"


def test_refusal_reports_the_code_and_the_next_command():
    agent, _ = make(
        {
            "api/console/heartbeat": beat(),
            "api/console/state/robot": states({}),
            "api/console/command/robot": (
                409,
                {"outcome": "refused", "error": "Take control of this device first."},
            ),
        }
    )
    code, result = run_cli(["robot", "enable"], agent)
    assert code == 1 and result["error"]["code"] == "not_controlled"
    assert result["hint"] == "labctl robot take"


def test_lost_connection_after_send_is_unknown():
    agent, _ = make(
        {
            "api/console/heartbeat": beat(),
            "api/console/state/stage": states({}),
            "api/console/command/stage": Unreachable("reset"),
        }
    )
    code, result = run_cli(["stage", "home", "top"], agent)
    assert code == 3 and result["outcome"] == "unknown"


def test_interrupt_during_a_wait_sends_one_device_stop(monkeypatch):
    calls = {"n": 0}

    def state(data):
        calls["n"] += 1
        if calls["n"] > 2:
            raise KeyboardInterrupt
        return 200, {"state": {"busy": True, "last_command": None}}

    agent, http = make(
        {
            "api/console/heartbeat": beat({"device": "stage", "holder": "h"}),
            "api/console/state/stage": state,
            "api/console/command/stage": (202, {"command_id": "9"}),
            "api/console/stop/stage": (200, {"device": "stage", "outcome": "confirmed"}),
        }
    )
    code, result = run_cli(["stage", "home", "top"], agent)
    assert code == 130 and result["outcome"] == "interrupted"
    assert result["stop"]["results"] == {"stage": "confirmed"}
    assert len(http.sent("api/console/stop/stage")) == 1


def test_no_wait_returns_the_command_id_and_wait_finds_it():
    agent, _ = make(
        {
            "api/console/heartbeat": beat({"device": "microscope", "holder": "h"}),
            "api/console/command/microscope": (202, {"command_id": "7"}),
            "api/console/state/microscope": states({"last_command": {"id": "7", "result": "ok"}}),
        }
    )
    code, result = run_cli(["microscope", "focus", "100", "--no-wait"], agent)
    assert code == 0 and result == {
        "ok": True,
        "device": "microscope",
        "action": "focus_move",
        "outcome": "accepted",
        "command_id": "7",
    }
    code, result = run_cli(["microscope", "wait", "7"], agent)
    assert code == 0 and result["outcome"] == "ok"


def test_record_start_makes_a_recording_id():
    agent, http = make(
        {
            "api/console/heartbeat": beat({"device": "microscope", "holder": "h"}),
            "api/console/command/microscope": (202, {"command_id": "7"}),
        }
    )
    run_cli(["microscope", "record", "start", "--mode", "live", "--no-wait"], agent)
    params = http.sent("api/console/command/microscope")[0]["params"]
    assert len(params["recording_id"]) == 32 and params["mode"] == "live"


def test_stops_never_take_control_and_never_beat():
    agent, http = make(
        {
            "api/dashboard/motion": (202, {"operation_id": "op"}),
            "api/console/stop/robot": (200, {"outcome": "confirmed"}),
            "api/console/stop/stage": (200, {"outcome": "confirmed"}),
            "api/console/stop/microscope": (409, {"error": "The service is not configured."}),
        }
    )
    code, result = run_cli(["all", "stop"], agent)
    assert code == 0 and result["results"] == {
        "robot": "confirmed",
        "stage": "confirmed",
        "microscope": "not-configured",
    }
    paths = {path for path, _ in http.calls}
    assert not any("heartbeat" in path or "control" in path for path in paths)


def test_direct_stops_work_when_the_console_is_down(monkeypatch):
    sent = []
    monkeypatch.setattr(
        Agent, "_direct_stop", lambda self, device: sent.append(device) or "confirmed"
    )
    down = Unreachable("refused")
    agent, _ = make(
        {
            "api/dashboard/motion": down,
            "api/console/stop/robot": down,
            "api/console/stop/stage": down,
            "api/console/stop/microscope": down,
        },
        direct=True,
    )
    code, result = run_cli(["all", "stop"], agent)
    assert code == 0 and sorted(sent) == ["microscope", "robot", "stage"]
    assert result["dashboard"] == {"result": "unreachable"}


def test_a_demo_console_url_sends_no_direct_stops():
    assert Agent("http://127.0.0.1:18772/lab/", settings={}).direct is False
    assert Agent(settings={}).direct is True


def test_furnace_stop_confirms_heat_off():
    agent, http = make(
        {
            "api/dashboard/heat": (202, {"operation_id": "op"}),
            "api/console/stop/furnace": (200, {"outcome": "confirmed"}),
        }
    )
    code, result = run_cli(["furnace", "stop"], agent)
    assert code == 0 and result["kind"] == "heat"
    assert http.sent("api/dashboard/heat") == [{"confirmed": True}]


def test_gas_off_stops_every_channel():
    agent, http = make(
        {
            "api/console/ws/process/gas-stop": lambda data: (
                200,
                {"channel_index": data["channel_index"], "outcome": "confirmed"},
            )
        }
    )
    code, result = run_cli(["furnace", "gas-off"], agent)
    assert code == 0 and result["results"] == {"0": "confirmed", "1": "confirmed", "2": "confirmed"}


def test_sessions_are_checked():
    with pytest.raises(ValueError):
        Agent(session="a b", settings={})
    assert Agent(session="bench-1", settings={}).page == "agent-bench-1"


def test_unreachable_console_read_exits_1():
    agent, _ = make({"api/console/state/robot": Unreachable("refused")})
    code, result = run_cli(["robot", "state"], agent)
    assert code == 1 and result["error"]["code"] == "unreachable"


def test_frames_are_saved_to_the_out_path(tmp_path):
    agent, _ = make({})
    url = "http://127.0.0.1:8770/api/camera/side/frame.jpg"
    agent.http.routes[url] = lambda data: (200, b"\xff\xd8jpeg")
    code, result = run_cli(["microscope", "frame", "side", "--out", str(tmp_path / "s.jpg")], agent)
    assert code == 0 and (tmp_path / "s.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert result["bytes"] == 6


def test_constants_match_the_design():
    assert agent_module.POLL_S == 0.2 and agent_module.DEFAULT_TIMEOUT_S == 300
