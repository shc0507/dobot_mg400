"""Motion workspace: the state route and the relay paths with the robot and stage simulators."""

import gc
import json
import time
import uuid
from types import SimpleNamespace

import pytest

from orchestrator.app import DemoResources, OrchestratorService, create_app
from orchestrator.web import console as console_module
from orchestrator.web.workspaces import discover, motion

PRIVATE = "private-robot-service-token-0123456789"


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Timed out")


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value


def headers(page):
    result = {}
    if page:
        result["X-MG400-Client"] = page
    return result


def post(rig, path, data=None, page="console-page-a"):
    return rig.client.post(
        "/lab/api/console/" + path, json={} if data is None else data, headers=headers(page)
    )


def motion_state(rig, page="console-page-a"):
    response = rig.client.get("/lab/api/console/ws/motion/state", headers=headers(page))
    assert response.status_code == 200
    return response.get_json()


# Fake services -------------------------------------------------------------


class FakeAdapter:
    def __init__(self, state, holder):
        self._state, self._holder = state, holder

    def state(self):
        return {**self._state, "lease": {"holder": self._holder(), "remaining_s": 3}}

    def close(self):
        pass


@pytest.fixture
def fake(tmp_path):
    holders = {"robot": None, "stage": None}
    robot = {
        "actual": [232.5, -33.1, 99.1, 44.6, 1000],
        "enabled": True,
        "robot_host": "192.168.1.6",
        "controller_id": None,
        "feedback_fresh": True,
        "last_command": {"action": "execute", "result": "rejected", "client": "x", "id": "1"},
        "keyboard_disarm_reason": "key updates stopped.",
        "token": PRIVATE,
    }
    stage = {
        "connected": True,
        "port": "/dev/ttyUSB0",
        "automation": {
            "configured": True,
            "poses": ["inspect", "load"],
            "at_poses": {"load": {"matched": True, "axes": {"insert": True}}},
            "active": None,
        },
        "motors": [
            {
                "id": "insert",
                "position": 10,
                "position_mm": None,
                "reference": {"stream": "camera3", "marker": 12},
                "events": ["private"],
            }
        ],
    }

    def factory(required, identity):
        def adapter(name):
            def holder():
                return holders[name]

            state = dict(robot, controller_id=holders["robot"]) if name == "robot" else stage
            return FakeAdapter(state, holder)

        return {name: adapter(name) for name in required}

    service = OrchestratorService(
        settings={"MG400_ORCH_ROBOT_TOKEN": PRIVATE},
        recipes_dir=tmp_path,
        demo=True,
        client_factory=factory,
        clock=Clock(),
    ).start(background=False)
    app = create_app(service, base_path="/lab")
    yield SimpleNamespace(service=service, client=app.test_client(), holders=holders)
    service.close()


def test_motion_workspace_registers_its_routes():
    ids = [item.id for item in discover()]
    assert "motion" in ids
    item = next(item for item in discover() if item.id == "motion")
    assert item.register is not None and item.title == {"en": "Motion", "zh": "运动"}


def test_state_route_keeps_only_page_fields_and_hides_identities(fake):
    fake.holders["robot"] = "someone-else"
    body = motion_state(fake)
    text = json.dumps(body)
    assert PRIVATE not in text and "someone-else" not in text
    assert "192.168.1.6" not in text and "/dev/ttyUSB0" not in text and "private" not in text
    robot, stage = body["robot"], body["stage"]
    assert robot["available"] is True and robot["actual"][:2] == [232.5, -33.1]
    assert robot["holder"] == "other"
    assert robot["last_command"] == {"action": "execute", "result": "rejected"}
    assert robot["keyboard_disarm_reason"] == "key updates stopped."
    assert stage["holder"] is None
    assert stage["motors"] == [
        {"id": "insert", "position": 10, "position_mm": None, "reference_configured": True}
    ]
    assert stage["poses"] == ["inspect", "load"] and stage["at_poses"] == ["load"]
    assert "automation" not in stage


def test_unreachable_device_is_reported_without_an_error(fake, monkeypatch):
    def broken(name, identity):
        raise OSError("down")

    monkeypatch.setattr(fake.service, "console_client", broken)
    body = motion_state(fake)
    assert body == {
        "robot": {"available": False, "reason": "unreachable"},
        "stage": {"available": False, "reason": "unreachable"},
    }


# Robot and stage simulators ------------------------------------------------


@pytest.fixture
def sim(tmp_path, monkeypatch, request):
    # The robot service stops the arm when its feedback is older than 0.25 s. A full
    # garbage collection of the whole pytest heap can stop all threads for longer.
    # Collections do not scan frozen objects, so they stay short while the rig runs.
    gc.freeze()
    request.addfinalizer(gc.unfreeze)
    monkeypatch.setattr(console_module, "WATCH_INTERVAL_S", 0.01)
    resources = DemoResources(tmp_path / "demos")
    started = set()

    def factory(required, identity):
        missing = [name for name in required if name not in started]
        if missing:
            for client in resources.clients(missing, "start-" + uuid.uuid4().hex).values():
                client.close()
            started.update(missing)
        return {
            name: resources.existing_client(name, identity, profile="console") for name in required
        }

    clock = Clock()
    service = OrchestratorService(
        settings={}, recipes_dir=tmp_path, demo=True, client_factory=factory, clock=clock
    ).start(background=False)
    # Only the robot and the stage run in this rig.
    service.dashboard_configured = lambda name: name in {"robot", "stage"}
    app = create_app(service, base_path="/lab")
    rig = SimpleNamespace(service=service, client=app.test_client(), clock=clock)
    yield rig
    service.close()
    resources.close()


def command(rig, device, action, params=None, page="console-page-a"):
    response = post(rig, "command/" + device, {"action": action, "params": params or {}}, page)
    return response.status_code, response.get_json()


def take(rig, device, page="console-page-a"):
    response = post(rig, f"control/{device}/take", page=page)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["control"]


def robot_ready(rig):
    def ready():
        robot = motion_state(rig)["robot"]
        return robot if robot.get("feedback_fresh") and robot.get("actual")[0] is not None else None

    return wait_for(ready)


def enable_robot(rig):
    robot_ready(rig)
    assert command(rig, "robot", "enable")[0] == 202
    return wait_for(lambda: (lambda r: r if r["enabled"] else None)(settled(rig)))


def settled(rig):
    """Wait for an idle arm with a known pose."""

    def ready():
        robot = motion_state(rig)["robot"]
        return robot if robot["actual"][0] is not None and not robot["busy"] else None

    return wait_for(ready)


def test_take_and_release_control_of_both_devices(sim):
    for device in ("robot", "stage"):
        assert take(sim, device)["mine"] is True
        assert motion_state(sim)[device]["holder"] == "mine"
        assert motion_state(sim, page="console-page-b")[device]["holder"] == "console"
        # Another page cannot take it over.
        assert post(sim, f"control/{device}/take", page="console-page-b").status_code == 409
        assert post(sim, f"control/{device}/release").get_json()["result"] == "released"
        wait_for(lambda device=device: motion_state(sim)[device]["holder"] is None)


def test_commands_need_control(sim):
    take(sim, "robot")
    status, body = command(sim, "robot", "enable", page="console-page-b")
    assert status == 409 and "Take control" in body["error"]


def test_missing_heartbeat_releases_the_robot_and_the_stage(sim):
    for device in ("robot", "stage"):
        take(sim, device)
    assert post(sim, "heartbeat").status_code == 200
    sim.clock.value += 3.1
    wait_for(lambda: sim.service.console.control() == [])
    state = wait_for(
        lambda: (
            lambda s: s if s["robot"]["holder"] is None and s["stage"]["holder"] is None else None
        )(motion_state(sim))
    )
    assert state["robot"]["available"] and state["stage"]["available"]


def speed_cap(rig):
    body = rig.client.get("/lab/api/console/describe/robot", headers=headers("console-page-a"))
    return body.get_json()["commands"]["execute"]["params"]["properties"]["speed"]["maximum"]


def ws(rig, path, data, page="console-page-a"):
    response = rig.client.post(
        "/lab/api/console/ws/motion/" + path, json=data, headers=headers(page)
    )
    return response.status_code, response.get_json()


def test_jog_step_moves_the_arm_once_from_the_actual_pose(sim):
    take(sim, "robot")
    x = enable_robot(sim)["actual"][0]
    jog = {"axis": "x", "direction": 1, "step": 2, "speed": speed_cap(sim)}
    status, body = ws(sim, "jog", jog)
    assert status == 202 and body["outcome"] == "accepted", body
    moved = settled(sim)
    assert moved["actual"][0] == pytest.approx(x + 2, abs=0.05)
    assert moved["last_command"] == {
        "action": "execute",
        "result": "ok",
        "message": moved["last_command"]["message"],
        "finished_at": moved["last_command"]["finished_at"],
    }
    # Only the listed steps and one axis are accepted.
    assert ws(sim, "jog", dict(jog, step=50))[0] == 400
    assert ws(sim, "jog", dict(jog, axis="j1"))[0] == 400
    # The service refuses a speed above its cap.
    status, body = ws(sim, "jog", dict(jog, speed=speed_cap(sim) + 1))
    assert status == 409 and body["outcome"] == "refused"
    # Without control the console refuses the jog.
    assert ws(sim, "jog", jog, page="console-page-b")[0] == 409


def test_jog_and_move_need_no_bounds_or_tool_file(sim):
    take(sim, "robot")
    x, y, z, r = enable_robot(sim)["actual"][:4]
    cap = speed_cap(sim)
    assert not any(key.startswith("MG400_ORCH_X_") for key in sim.service.settings)
    status, body = ws(sim, "jog", {"axis": "x", "direction": 1, "step": 2, "speed": cap})
    assert status == 202, body
    assert settled(sim)["actual"][0] == pytest.approx(x + 2, abs=0.05)
    status, body = ws(sim, "move", {"targets": {"x": x, "y": y, "z": z + 5, "r": r}, "speed": cap})
    assert status == 202, body
    assert settled(sim)["actual"][2] == pytest.approx(z + 5, abs=0.05)


def test_suction_commands_show_the_measured_outputs(sim):
    take(sim, "robot")
    enable_robot(sim)
    for operation, suck in (("suck", True), ("off", False)):
        assert command(sim, "robot", "vacuum", {"operation": operation})[0] == 202
        robot = wait_for(
            lambda suck=suck: (
                lambda s: s if not s["busy"] and s["vacuum"]["suck"] is suck else None
            )(motion_state(sim)["robot"])
        )
        assert robot["vacuum"] == {"suck": suck, "blow": False, "held": None}
        assert "gripper" not in robot


def test_stage_moves_without_a_zero_and_stops_without_control(sim):
    take(sim, "stage")
    stage = wait_for(lambda: (lambda s: s if s.get("motors") else None)(motion_state(sim)["stage"]))
    motor = stage["motors"][0]["id"]
    # Set zero needs the explicit confirm flag from the page confirmation.
    assert command(sim, "stage", "zero", {"motor": motor})[0] == 409
    move = {"motor": motor, "direction": "cw", "speed": 50, "steps": 800}
    status, body = command(sim, "stage", "move", move)
    assert status == 202, body
    wait_for(
        lambda: (
            next(m for m in motion_state(sim)["stage"]["motors"] if m["id"] == motor)["mode"]
            != "idle"
        )
    )
    # Another page without control stops the stage.
    response = post(sim, "stop/stage", page="console-page-b")
    assert response.status_code == 200
    assert response.get_json()["outcome"] in {"confirmed", "pending"}
    wait_for(
        lambda: (
            next(m for m in motion_state(sim)["stage"]["motors"] if m["id"] == motor)["mode"]
            == "idle"
        )
    )


def test_robot_stop_works_without_control(sim):
    take(sim, "robot")
    robot_ready(sim)
    response = post(sim, "stop/robot", page="console-page-b")
    assert response.status_code == 200
    assert response.get_json()["outcome"] in {"confirmed", "pending"}


def test_home_moves_all_joints_to_zero_and_the_state_shows_the_joints(sim):
    take(sim, "robot")
    robot = enable_robot(sim)
    assert len(robot["joints"]) == 4 and all(type(value) is float for value in robot["joints"])
    # The service reports no alarms as null.
    assert isinstance(robot["message"], str) and not robot["alarms"]
    status, body = command(sim, "robot", "home")
    assert status == 202, body
    homed = wait_for(
        lambda: (lambda r: r if r["last_command"]["action"] == "home" and not r["busy"] else None)(
            motion_state(sim)["robot"]
        )
    )
    assert homed["last_command"]["result"] == "ok"
    assert homed["joints"] == pytest.approx([0.0, 0.0, 0.0, 0.0], abs=0.01)


def test_generic_relay_and_the_move_route_both_send_robot_execute(sim):
    take(sim, "robot")
    x, y, z, r = enable_robot(sim)["actual"][:4]
    params = {"targets": {"x": x - 2, "y": y, "z": z, "r": r}, "speed": speed_cap(sim)}
    status, body = command(sim, "robot", "execute", params)
    assert status == 202, body
    assert settled(sim)["actual"][0] == pytest.approx(x - 2, abs=0.05)
    params = {"targets": {"x": x - 4, "y": y, "z": z, "r": r}, "speed": speed_cap(sim)}
    status, body = ws(sim, "move", params)
    assert status == 202 and body["outcome"] == "accepted", body
    assert settled(sim)["actual"][0] == pytest.approx(x - 4, abs=0.05)


def test_keys_route_forwards_held_keys_only_for_the_controlling_page(sim):
    take(sim, "robot")
    enable_robot(sim)
    arm = {"enabled": True, "seq": 1, "speed": 1}
    assert command(sim, "robot", "keyboard", arm)[0] == 202
    wait_for(lambda: motion_state(sim)["robot"]["keyboard_enabled"])
    sent = []
    session = sim.service.console.sessions["robot"]
    original = session.adapter.keys
    session.adapter.keys = lambda seq, held: sent.append((seq, held)) or original(seq, held)
    status, body = ws(sim, "keys", {"seq": 2, "held": ["W"]})
    assert status == 200 and body == {"outcome": "accepted"}, body
    status, body = ws(sim, "keys", {"seq": 3, "held": []})
    assert status == 200, body
    # Another page and a page without an identity are refused and nothing is sent.
    assert ws(sim, "keys", {"seq": 4, "held": ["W"]}, page="console-page-b")[0] == 409
    assert ws(sim, "keys", {"seq": 4, "held": ["W"]}, page=None)[0] == 400
    assert ws(sim, "keys", {"seq": "4", "held": ["W"]})[0] == 400
    assert sent == [(2, ["W"]), (3, [])]
    # The service validates the key names.
    status, body = ws(sim, "keys", {"seq": 5, "held": ["P"]})
    assert status == 409 and body["outcome"] == "refused"
    disarm = {"enabled": False, "seq": 6, "reason": "user"}
    assert command(sim, "robot", "keyboard", disarm)[0] == 202
    wait_for(lambda: not motion_state(sim)["robot"]["keyboard_enabled"])


# Stage hold ----------------------------------------------------------------

HOLD = "hold-0123456789abcdef"


def stage_motors(rig):
    stage = motion.read(rig.service, "stage", None)
    return {item["id"]: item for item in stage.get("motors") or ()}


def stage_ready(rig):
    take(rig, "stage")
    return list(wait_for(lambda: stage_motors(rig)))


def mode(rig, motor):
    return stage_motors(rig)[motor]["mode"]


def hold(rig, hold_id=HOLD, motor="bottom", direction="cw", speed=50, page="console-page-a"):
    body = {"id": hold_id, "motor": motor, "direction": direction, "speed": speed}
    return ws(rig, "stage/hold", body, page)


def release_hold(rig, hold_id=HOLD, page="console-page-a"):
    return ws(rig, "stage/release", {"id": hold_id}, page)


def test_stage_hold_moves_while_the_page_refreshes_it_and_stops_on_release(sim):
    motor = stage_ready(sim)[0]
    start = stage_motors(sim)[motor]["position"]
    assert hold(sim, motor=motor) == (200, {"outcome": "accepted"})
    wait_for(lambda: mode(sim, motor) == "continuous")
    # The console renews the hold, so it outlives the 1 s firmware watchdog.
    deadline = time.monotonic() + 1.5
    while time.monotonic() < deadline:
        assert hold(sim, motor=motor)[0] == 200
        time.sleep(0.1)
    assert mode(sim, motor) == "continuous"
    assert release_hold(sim) == (200, {"outcome": "accepted"})
    wait_for(lambda: mode(sim, motor) == "idle")
    assert stage_motors(sim)[motor]["position"] > start
    # A released hold cannot restart. The page must press again with a new id.
    status, body = hold(sim, motor=motor)
    assert status == 409 and "press again" in body["error"]


def test_stage_hold_stops_when_the_page_stops_refreshing_it(sim):
    motor = stage_ready(sim)[0]
    sent = []
    adapter = sim.service.console.sessions["stage"].adapter
    command = adapter.command
    adapter.command = lambda action, **params: sent.append(action) or command(action, **params)
    assert hold(sim, motor=motor)[0] == 200
    wait_for(lambda: mode(sim, motor) == "continuous")
    wait_for(lambda: "keepalive" in sent)
    # The console clock: no page update for 0.6 s.
    sim.clock.value += 0.6
    wait_for(lambda: "release_hold" in sent)
    wait_for(lambda: mode(sim, motor) == "idle")
    assert sent[0] == "hold" and sent[-1] == "release_hold"
    status, body = hold(sim, motor=motor)
    assert status == 409 and "No page update for 0.5 s" in body["error"]
    events = sim.service.events.since(0)["events"]
    assert any("No page update" in item["message"] for item in events)


def test_stage_hold_needs_this_pages_control_and_moves_one_axis(sim):
    status, body = hold(sim)
    assert status == 409 and "Take control" in body["error"]
    first, second = stage_ready(sim)[:2]
    assert hold(sim, motor=first, page="console-page-b")[0] == 409
    assert hold(sim, motor=first, page=None)[0] == 400
    assert hold(sim, hold_id="short", motor=first)[0] == 400
    assert hold(sim, motor=first, direction="up")[0] == 400
    assert hold(sim, motor=first, speed="50")[0] == 400
    assert ws(sim, "stage/hold", {"id": HOLD, "motor": first})[0] == 400
    # A release that arrives before its start cancels the start.
    early = "hold-early-0123456789"
    assert release_hold(sim, early)[0] == 200
    assert hold(sim, hold_id=early, motor=first)[0] == 409
    assert hold(sim, motor=first)[0] == 200
    status, body = hold(sim, hold_id="hold-second-0123456789", motor=second)
    assert status == 409 and "Release the held axis first" in body["error"]
    assert release_hold(sim)[0] == 200
    wait_for(lambda: mode(sim, first) == "idle")
    assert mode(sim, second) == "idle"


def test_stop_and_release_of_control_end_the_stage_hold(sim):
    first, second = stage_ready(sim)[:2]
    assert hold(sim, motor=first)[0] == 200
    wait_for(lambda: mode(sim, first) == "continuous")
    # Stop needs no control. The next keepalive is refused and ends the hold.
    assert post(sim, "stop/stage", page="console-page-b").status_code == 200
    wait_for(lambda: mode(sim, first) == "idle")
    wait_for(lambda: hold(sim, motor=first)[0] == 409)
    # Release of control sends release_hold before it releases the lease.
    sent = []
    adapter = sim.service.console.sessions["stage"].adapter
    command, release = adapter.command, adapter.release
    adapter.command = lambda action, **params: sent.append(action) or command(action, **params)
    adapter.release = lambda: sent.append("release lease") or release()
    assert hold(sim, hold_id="hold-other-axis-0123", motor=second)[0] == 200
    wait_for(lambda: mode(sim, second) == "continuous")
    assert post(sim, "control/stage/release").get_json()["result"] == "released"
    assert sent[0] == "hold" and sent[-2:] == ["release_hold", "release lease"]
    wait_for(lambda: mode(sim, second) == "idle")
