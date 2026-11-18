"""Stage contract and operator-route boundaries, with no device or socket access."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from contract_helpers import check_contract
from stage_helpers import home_stage

from labkit.errors import ServiceError
from stage_controller.app import create_app
from stage_controller.protocol import ControllerError, DemoTransport, LinkError

CLIENT = {"X-MG400-Client": "automation-a"}
HOLD_ID = "automation-hold-0001"


@pytest.fixture
def contract(stage_rig):
    controller, transport, clock = stage_rig
    home_stage(controller, clock)
    transport.commands.clear()
    app = create_app(controller)
    app.config["TESTING"] = True
    return controller, transport, clock, app.test_client(), dict(CLIENT)


def post(web, route, body=None, client=CLIENT):
    return web.post("/contract/api/" + route, json=body or {}, headers=client)


def acquire(web):
    assert post(web, "heartbeat").status_code == 200


def move(web, motor="bottom", speed=1, steps=100):
    return post(
        web, "command", dict(action="move", motor=motor, direction="cw", speed=speed, steps=steps)
    )


def test_stage_contract_conformance(contract):
    controller, transport, clock, web, _ = contract
    check_contract(
        web,
        base_path="/contract",
        advance=clock.advance,
        expiry_count=lambda: transport.commands.count("stop all"),
    )
    assert controller.describe()["service"] == "stage"
    assert controller.describe()["on_lease_expiry"] == "stop_all"


def test_contract_accepts_any_host_and_origin(stage_rig):
    controller, _, _ = stage_rig
    web = create_app(controller).test_client()
    headers = {"Host": "stage.example", "Origin": "https://other.example"}
    assert web.get("/contract/api/state", headers=headers).status_code == 200
    assert (
        web.post("/contract/api/heartbeat", json={}, headers={**headers, **CLIENT}).status_code
        == 200
    )


def test_any_client_can_stop_a_hold_during_automation(contract):
    controller, transport, clock, web, _ = contract
    controller.hold("bottom", "cw", 10, HOLD_ID, clock())
    acquire(web)
    response = post(web, "stop", client={"X-MG400-Client": "other"})
    assert response.status_code == 200
    assert response.json["stop"] == "confirmed"
    assert controller.snapshot()["motors"][0]["mode"] == "idle"
    assert transport.commands[-1] == "stop all"
    assert controller.lease.holder == "automation-a"


def test_fixed_motion_finishes_only_after_fresh_idle_feedback(contract):
    controller, _, clock, web, _ = contract
    assert move(web).status_code == 409
    acquire(web)
    response = move(web, speed=100, steps=10)
    assert response.status_code == 202
    command_id = response.json["command_id"]
    assert response.json["state"]["busy"] is True
    assert response.json["state"]["last_command"] is None
    clock.advance(2)
    controller.poll()
    state = web.get("/contract/api/state").json
    assert state["busy"] is False
    assert state["last_command"]["id"] == command_id
    assert state["last_command"]["result"] == "ok"


def test_unreferenced_finite_move_confirms_pulses_without_claiming_absolute_position(stage_rig):
    controller, _, clock = stage_rig
    web = create_app(controller).test_client()
    acquire(web)
    response = move(web, speed=100, steps=10)
    assert response.status_code == 202
    clock.advance(1)
    controller.poll()
    state = controller.snapshot()
    assert state["last_command"]["result"] == "ok"
    assert state["motors"][0]["automation_ready"] is False
    assert state["motors"][0]["position_mm"] is None


@pytest.mark.parametrize("interruption", ["stop", "stop_all", "release", "manual_move"])
def test_guarded_relative_move_rejects_interrupted_continuation(stage_rig, interruption):
    controller, transport, clock = stage_rig
    web = create_app(controller).test_client()
    acquire(web)
    epoch = controller.snapshot()["stop_epoch"]
    if interruption == "stop":
        post(web, "stop", client={"X-MG400-Client": "another-client"})
    elif interruption == "stop_all":
        post(web, "command", {"action": "stop_all"})
    elif interruption == "release":
        post(web, "release")
        acquire(web)
    else:
        assert move(web, speed=100, steps=10).status_code == 202
        clock.advance(0.2)
        controller.poll()
        clock.advance(1)
    before = list(transport.commands)
    response = post(
        web,
        "command",
        dict(
            action="move_guarded",
            motor="insert",
            direction="cw",
            speed=50,
            steps=10,
            stop_epoch=epoch,
        ),
    )
    assert response.status_code == 409
    assert "cancelled" in response.json["error"]
    assert transport.commands == before


def test_guarded_relative_move_finishes_and_preserves_its_epoch(stage_rig):
    controller, _, clock = stage_rig
    web = create_app(controller).test_client()
    acquire(web)
    epoch = controller.snapshot()["stop_epoch"]
    response = post(
        web,
        "command",
        dict(
            action="move_guarded",
            motor="insert",
            direction="cw",
            speed=50,
            steps=10,
            stop_epoch=epoch,
        ),
    )
    assert response.status_code == 202
    clock.advance(0.3)
    controller.poll()
    state = controller.snapshot()
    assert state["stop_epoch"] == epoch
    assert state["last_command"]["id"] == response.json["command_id"]
    assert state["last_command"]["result"] == "ok"


def test_contract_maps_microstep_hold_keepalive_release_and_stop(contract):
    controller, transport, clock, web, _ = contract
    acquire(web)
    assert post(web, "command", dict(action="set_microstep", microstep=2)).status_code == 202
    # A real mode change invalidates every reference. A manual move needs none.
    assert not any(m["automation_ready"] for m in controller.snapshot()["motors"])
    assert move(web).status_code == 202
    assert post(web, "command", dict(action="home", motor="top")).status_code == 202
    clock.advance(0.5)
    controller.poll()
    clock.advance(1)
    for args in (
        dict(
            action="hold",
            motor="top",
            direction="cw",
            speed=30,
            hold_id=HOLD_ID,
            sampled_at=clock(),
        ),
        dict(action="keepalive", motor="top", hold_id=HOLD_ID),
        dict(action="release_hold", motor="top", hold_id=HOLD_ID),
    ):
        response = post(web, "command", args)
        assert response.status_code == 202, response.json
        assert response.json["state"]["last_command"]["action"] == args["action"]
    assert "microstep 2" in transport.commands
    assert "hold" in transport.commands
    assert "keepalive" in transport.commands
    assert (
        post(web, "command", {"action": "stop_all"}, {"X-MG400-Client": "other"}).status_code == 202
    )
    assert all(m["mode"] == "idle" for m in controller.snapshot()["motors"])


def prepare_microstep(web, epoch, microstep=8):
    return post(
        web,
        "command",
        dict(action="set_microstep_guarded", microstep=microstep, stop_epoch=epoch),
    )


def test_guarded_microstep_prepares_idle_stage_without_motion_or_new_epoch(stage_rig):
    controller, transport, _ = stage_rig
    web = create_app(controller).test_client()
    acquire(web)
    epoch = controller.snapshot()["stop_epoch"]
    transport.commands.clear()
    response = prepare_microstep(web, epoch)
    assert response.status_code == 202, response.json
    state = controller.snapshot()
    assert state["microstep"] == 8
    assert state["stop_epoch"] == epoch
    assert state["feedback_fresh"]
    assert state["last_command"]["id"] == response.json["command_id"]
    assert state["last_command"]["result"] == "ok"
    assert not state["busy"]
    assert transport.commands == ["microstep 8"]
    assert all(motor["mode"] == "idle" for motor in state["motors"])
    assert prepare_microstep(web, epoch).status_code == 202
    assert transport.commands == ["microstep 8"]
    spec = controller.describe()["commands"]["set_microstep_guarded"]
    assert spec["any_client"] is False
    assert set(spec["params"]["required"]) == {"microstep", "stop_epoch"}


@pytest.mark.parametrize("interruption", ["stop", "stop_all", "release", "microstep"])
def test_guarded_microstep_rejects_old_epoch_before_serial_mutation(stage_rig, interruption):
    controller, transport, _ = stage_rig
    web = create_app(controller).test_client()
    acquire(web)
    epoch = controller.snapshot()["stop_epoch"]
    if interruption == "stop":
        assert post(web, "stop", client={"X-MG400-Client": "another-client"}).status_code == 200
    elif interruption == "stop_all":
        assert post(web, "command", {"action": "stop_all"}).status_code == 202
    elif interruption == "release":
        assert post(web, "release").status_code == 200
        acquire(web)
    else:
        assert post(web, "command", dict(action="set_microstep", microstep=4)).status_code == 202
    before = list(transport.commands)
    response = prepare_microstep(web, epoch)
    assert response.status_code == 409
    assert "cancelled" in response.json["error"]
    assert transport.commands == before


def test_guarded_microstep_needs_control_and_idle_motors(stage_rig):
    controller, transport, _ = stage_rig
    web = create_app(controller).test_client()
    epoch = controller.snapshot()["stop_epoch"]
    assert prepare_microstep(web, epoch).status_code == 409
    assert "microstep 8" not in transport.commands
    acquire(web)
    assert move(web, speed=1, steps=100).status_code == 202
    epoch = controller.snapshot()["stop_epoch"]
    before = list(transport.commands)
    response = prepare_microstep(web, epoch)
    assert response.status_code == 409
    assert "Stop all motors" in response.json["error"]
    assert transport.commands == before
    assert controller.snapshot()["stop_epoch"] == epoch


def test_stop_during_microstep_preparation_invalidates_following_motion(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    web = create_app(controller).test_client()
    acquire(web)
    epoch = controller.snapshot()["stop_epoch"]
    entered, complete = threading.Event(), threading.Event()
    original = transport.command

    def slow_microstep(command):
        if command == "microstep 8":
            entered.set()
            assert complete.wait(2)
        return original(command)

    monkeypatch.setattr(transport, "command", slow_microstep)
    with ThreadPoolExecutor(max_workers=2) as pool:
        prepare = pool.submit(
            controller.command,
            "set_microstep_guarded",
            {"microstep": 8, "stop_epoch": epoch},
            "automation-a",
        )
        assert entered.wait(2)
        stop = pool.submit(controller.stop, "another-client", 1)
        complete.set()
        assert prepare.result(timeout=2)
        assert stop.result(timeout=2) == "confirmed"
    before = list(transport.commands)
    response = post(
        web,
        "command",
        dict(
            action="move_guarded",
            motor="insert",
            direction="cw",
            speed=50,
            steps=10,
            stop_epoch=epoch,
        ),
    )
    assert response.status_code == 409
    assert "cancelled" in response.json["error"]
    assert transport.commands == before
    assert all(motor["mode"] == "idle" for motor in controller.snapshot()["motors"])


def test_lease_expiry_stops_all_motors_once_without_http_requests(contract):
    controller, transport, clock, web, _ = contract
    acquire(web)
    assert move(web).status_code == 202
    assert move(web, motor="top").status_code == 202
    clock.advance(controller.lease.seconds)
    controller.poll()
    assert transport.commands.count("stop all") == 1
    assert controller.lease.holder is None
    assert all(m["mode"] == "idle" for m in controller.snapshot()["motors"])
    assert controller.snapshot()["last_command"]["result"] == "stopped"
    controller.poll()
    assert transport.commands.count("stop all") == 1


def test_contract_hold_retains_the_original_shorter_watchdog(contract):
    controller, transport, clock, web, _ = contract
    acquire(web)
    assert (
        post(
            web,
            "command",
            dict(
                action="hold",
                motor="bottom",
                direction="cw",
                speed=10,
                hold_id=HOLD_ID,
                sampled_at=clock(),
            ),
        ).status_code
        == 202
    )
    clock.advance(1.1)
    controller.poll()
    assert controller.lease.holder == "automation-a"
    assert controller.snapshot()["motors"][0]["mode"] == "idle"
    before = list(transport.commands)
    response = post(web, "command", dict(action="keepalive", motor="bottom", hold_id=HOLD_ID))
    assert response.status_code == 409
    assert transport.commands == before


def test_an_accepted_keepalive_adds_no_activity_event(contract):
    controller, _, clock, web, _ = contract
    acquire(web)
    start = dict(
        action="hold", motor="bottom", direction="cw", speed=10, hold_id=HOLD_ID, sampled_at=clock()
    )
    renew = dict(action="keepalive", motor="bottom", hold_id=HOLD_ID)
    assert post(web, "command", start).status_code == 202
    cursor = web.get("/contract/api/events?since=0").json["next"]
    assert post(web, "command", renew).status_code == 202
    assert web.get(f"/contract/api/events?since={cursor}").json["events"] == []
    assert controller.snapshot()["last_command"]["action"] == "keepalive"
    # A refused keepalive is still logged.
    clock.advance(1.1)
    controller.poll()
    assert post(web, "command", renew).status_code == 409
    events = web.get(f"/contract/api/events?since={cursor}").json["events"]
    assert any("keepalive rejected" in event["message"] for event in events)


def test_activity_entries_are_mirrored_once_without_poll_replay(contract):
    controller, _, _, web, _ = contract
    acquire(web)
    move(web)
    controller.poll()
    legacy = controller.snapshot()["events"]
    response = web.get("/contract/api/events?since=0").json
    assert [event["message"] for event in response["events"]] == [
        event["message"] for event in reversed(legacy)
    ]
    assert len({event["fields"]["activity_id"] for event in response["events"]}) == len(legacy)
    cursor = response["next"]
    controller.poll()
    assert web.get(f"/contract/api/events?since={cursor}").json["events"] == []


def test_unknown_outcome_locks_contract_without_retrying_motion(contract, monkeypatch):
    controller, transport, _, web, _ = contract
    acquire(web)
    original = transport.command

    def lose_reply(command):
        original(command)
        if command.startswith("steps"):
            raise LinkError("Move reply lost")

    monkeypatch.setattr(transport, "command", lose_reply)
    response = move(web)
    assert response.status_code == 503
    state = web.get("/contract/api/state").json
    assert state["locked"] is True
    assert state["fault"]["code"] == "device_link_lost"
    assert state["last_command"]["result"] == "failed"
    assert move(web).status_code == 409
    assert transport.commands.count("steps 100") == 1
    assert post(web, "stop", client={"X-MG400-Client": "other"}).json["stop"] == "unconfirmed"


def test_unconfirmed_stop_keeps_a_fault_until_manual_reconnect(contract, monkeypatch):
    controller, transport, _, web, headers = contract
    acquire(web)
    assert move(web).status_code == 202
    original = transport.command

    def ignore_stop(command):
        if command == "stop all":
            transport.commands.append(command)
            return
        return original(command)

    monkeypatch.setattr(transport, "command", ignore_stop)
    response = post(web, "stop")
    assert response.json["stop"] == "pending"
    assert response.json["state"]["locked"] is True
    assert response.json["state"]["fault"]["code"] == "stop_unconfirmed"
    assert response.json["state"]["last_command"]["result"] == "failed"
    assert move(web).status_code == 409
    DemoTransport.command(transport, "stop all")  # Simulate the operator's physical stop.
    controller.poll()
    assert controller.snapshot()["locked"] is True
    controller.disconnect()
    assert (
        web.post("/contract/api/connect", json={"port": "DEMO"}, headers=headers).status_code == 200
    )
    assert controller.snapshot()["locked"] is False


def test_expiry_never_holds_device_lock_while_waiting_and_manual_stop_is_available(
    stage_rig, monkeypatch
):
    controller, _, clock = stage_rig
    controller.lease.acquire("automation-a")
    controller.move("bottom", "cw", 1, 100)
    expiry_started = threading.Event()
    continue_expiry = threading.Event()
    original = controller.lease.on_expire

    def wait_during_expiry():
        expiry_started.set()
        assert continue_expiry.wait(2)
        original()

    monkeypatch.setattr(controller.lease, "on_expire", wait_during_expiry)
    clock.advance(controller.lease.seconds)
    with ThreadPoolExecutor(max_workers=2) as pool:
        polling = pool.submit(controller.poll)
        try:
            assert expiry_started.wait(1)
            with pytest.raises(ServiceError, match="expiry is still in progress"):
                controller.command(
                    "move", dict(motor="top", direction="cw", speed=1, steps=100), "other"
                )
            stopped = pool.submit(controller.stop, "manual-stop", 0)
            assert stopped.result(timeout=1) == "confirmed"
        finally:
            continue_expiry.set()
        polling.result(timeout=2)


def test_stop_can_wait_for_idle_feedback_without_blocking_another_stop(contract, monkeypatch):
    controller, transport, _, web, _ = contract
    acquire(web)
    assert move(web).status_code == 202
    original = transport.command
    waits = []

    def delayed_stop(command):
        if command == "stop all" and not waits:
            transport.commands.append(command)
            return
        return original(command)

    def wait_for_feedback(seconds):
        waits.append(seconds)
        assert controller.snapshot()["fault"]["code"] == "stop_pending"
        with pytest.raises(ServiceError, match="Only stop is allowed"):
            controller.command(
                "move", dict(motor="top", direction="cw", speed=1, steps=100), "automation-a"
            )
        with pytest.raises(ControllerError, match="Stop confirmation"):
            controller.connect("DEMO", client="automation-a")
        # Another caller can use the device lock while this stop waits for feedback.
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(controller.stop, "other", 0).result(timeout=1)
        assert response == "confirmed"

    monkeypatch.setattr(transport, "command", delayed_stop)
    monkeypatch.setattr("stage_controller.contract.time.sleep", wait_for_feedback)
    response = post(web, "stop", {"wait_s": 1})
    assert response.status_code == 200
    assert response.json["stop"] == "confirmed"
    assert response.json["state"]["locked"] is False
    assert len(waits) == 1


def test_releasing_ownership_does_not_implicitly_stop_motion(contract):
    controller, transport, _, web, _ = contract
    acquire(web)
    assert move(web).status_code == 202
    before = list(transport.commands)
    assert post(web, "release").status_code == 200
    assert controller.lease.holder is None
    assert transport.commands == before
    assert controller.snapshot()["busy"] is True


def test_reconnect_cannot_clear_unknown_outcome_while_a_motor_reports_motion(contract, monkeypatch):
    controller, transport, _, web, headers = contract
    controller.move("bottom", "cw", 1, 100)
    original_status = transport.status
    original_command = transport.command

    def lose_status_once():
        monkeypatch.setattr(transport, "status", original_status)
        raise LinkError("Status reply lost")

    def lose_stop(command):
        if command == "stop all":
            transport.commands.append(command)
            raise LinkError("Stop could not be sent")
        return original_command(command)

    monkeypatch.setattr(transport, "status", lose_status_once)
    monkeypatch.setattr(transport, "command", lose_stop)
    acquire(web)
    controller.poll()
    response = web.post("/contract/api/connect", json={"port": "DEMO"}, headers=headers)
    assert response.status_code == 200
    assert response.json["connected"] is True
    assert response.json["locked"] is True
    assert response.json["fault"]["code"] == "stop_unconfirmed"
    assert move(web, motor="top").status_code == 409
    with pytest.raises(ControllerError, match="Only stop is allowed"):
        controller.move("top", "cw", 1, 100)
    assert transport.commands.count("steps 100") == 1


@pytest.mark.parametrize("replace_connection", [False, True])
def test_disconnect_during_stop_cannot_hide_motion_or_replace_the_board(
    contract, monkeypatch, replace_connection
):
    controller, transport, clock, web, headers = contract
    acquire(web)
    assert move(web).status_code == 202
    original_command = transport.command
    replacement_attempts = []

    def ignore_stop(command):
        if command == "stop all":
            transport.commands.append(command)
            return
        return original_command(command)

    def replace_transport(_port):
        replacement_attempts.append(True)
        return DemoTransport(clock)

    def disconnect_during_wait(_seconds):
        controller.disconnect()
        if replace_connection:
            response = web.post("/contract/api/connect", json={"port": "DEMO"}, headers=headers)
            assert response.status_code == 409
            assert "Stop confirmation" in response.json["error"]

    monkeypatch.setattr(transport, "command", ignore_stop)
    if replace_connection:
        monkeypatch.setattr(controller, "_factory", replace_transport)
    monkeypatch.setattr("stage_controller.contract.time.sleep", disconnect_during_wait)
    response = post(web, "stop", {"wait_s": 1})
    assert response.json["stop"] == "unconfirmed"
    assert response.json["state"]["locked"] is True
    assert response.json["state"]["fault"]["code"] == "stop_unconfirmed"
    assert response.json["state"]["last_command"]["result"] == "failed"
    assert transport.motors["bottom"].mode == "moving"
    assert replacement_attempts == []
    # Recovery against the same still-moving board keeps the lock after stop returns.
    monkeypatch.setattr(controller, "_factory", lambda _port: transport)
    recovered = web.post("/contract/api/connect", json={"port": "DEMO"}, headers=headers)
    assert recovered.status_code == 200
    assert recovered.json["locked"] is True
    assert move(web, motor="top").status_code == 409
    assert "motor top" not in transport.commands


def test_prefixed_contract_keeps_its_lease_and_operator_guard_boundaries(stage_rig):
    controller, transport, clock = stage_rig
    home_stage(controller, clock)
    web = create_app(controller, base_path="/lab/stage/").test_client()
    base = "/lab/stage"
    proxy = {"Host": "lab.example:8080", "X-Forwarded-Host": "lab.example:8080"}
    access = dict(proxy)
    headers = {**access, **CLIENT}
    assert web.get(base + "/contract/api/state", headers=headers).status_code == 200
    assert web.post(base + "/contract/api/heartbeat", json={}, headers=headers).status_code == 200
    assert web.post(base + "/contract/api/stop", json={}, headers=access).status_code == 400
    assert web.get(base + "/contract/api/ports", headers=access).status_code == 200
    teach = base + "/contract/api/motors/insert/teach_reference"
    other = {**access, "X-MG400-Client": "other"}
    assert web.post(teach, json={"confirm": True}, headers=other).status_code == 409
    assert (
        web.post(
            base + "/contract/api/stop", json={}, headers={**headers, "X-MG400-Client": "other"}
        ).status_code
        == 200
    )
