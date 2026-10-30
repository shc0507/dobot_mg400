"""Stage contract and manual/automation boundaries, with no device or socket access."""

import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from contract_helpers import check_contract

from labkit.errors import ServiceError
from stage_controller.app import create_app
from stage_controller.protocol import ControllerError, DemoTransport, LinkError

CLIENT = {"X-MG400-Client": "automation-a"}
HOLD_ID = "automation-hold-0001"


@pytest.fixture
def contract(stage_rig):
    controller, transport, clock = stage_rig
    app = create_app(controller)
    app.config["TESTING"] = True
    web = app.test_client()
    page = web.get("/").text
    token = re.search(r'name="control-token" content="([^"]+)"', page)[1]
    return controller, transport, clock, web, {"X-Control-Token": token}


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


def test_contract_access_does_not_bypass_legacy_page_token(contract):
    _, _, _, web, page_headers = contract
    assert post(web, "heartbeat").status_code == 200
    assert web.post("/api/stop", json={}, headers=CLIENT).status_code == 403
    assert web.post("/contract/api/stop", json={}, headers=page_headers).status_code == 400
    assert web.post("/api/stop", json={}, headers=page_headers).status_code == 200
    assert post(web, "stop", client={"X-MG400-Client": "other"}).status_code == 200


def test_contract_uses_the_existing_access_token_and_origin_rules(stage_rig):
    controller, _, _ = stage_rig
    token = "stage-contract-access-token-0123456789"
    web = create_app(controller, token).test_client()
    headers = {"Host": "stage.example", "Authorization": f"Bearer {token}"}
    assert web.get("/contract/api/state", headers={"Host": "stage.example"}).status_code == 401
    assert web.get("/contract/api/state", headers=headers).status_code == 200
    assert (
        web.get(
            "/contract/api/state", headers={**headers, "Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert (
        web.post("/contract/api/heartbeat", json={}, headers={**headers, **CLIENT}).status_code
        == 200
    )


@pytest.mark.parametrize(
    "route,data",
    [
        ("/motors/bottom/move", dict(direction="cw", speed=10, steps=100)),
        (
            "/motors/bottom/hold",
            dict(direction="cw", speed=10, hold_id=HOLD_ID, sampled_at=100),
        ),
        ("/motors/bottom/hold/keepalive", dict(hold_id=HOLD_ID)),
        ("/microstep", dict(microstep=2)),
    ],
)
def test_automation_excludes_existing_motion_routes(contract, route, data):
    controller, transport, _, web, headers = contract
    acquire(web)
    before = list(transport.commands)
    response = web.post("/api" + route, json=data, headers=headers)
    assert response.status_code == 409
    assert response.json == {"error": "Stage is controlled by automation"}
    assert transport.commands == before
    assert web.get("/api/state").json["lease"]["holder"] == "automation-a"
    assert controller.lease.holder == "automation-a"


def test_manual_stop_and_hold_release_remain_available_during_automation(contract):
    controller, transport, clock, web, headers = contract
    controller.hold("bottom", "cw", 10, HOLD_ID, clock())
    acquire(web)
    response = web.post("/api/motors/bottom/hold/stop", json={"hold_id": HOLD_ID}, headers=headers)
    assert response.status_code == 200
    assert response.json["motors"][0]["mode"] == "idle"
    assert web.post("/api/stop", json={}, headers=headers).status_code == 200
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


def test_contract_maps_microstep_hold_keepalive_release_and_stop(contract):
    controller, transport, clock, web, _ = contract
    acquire(web)
    for args in (
        dict(action="set_microstep", microstep=2),
        dict(
            action="hold",
            motor="top",
            direction="ccw",
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
    assert web.post("/api/disconnect", json={}, headers=headers).status_code == 200
    assert web.post("/api/connect", json={"port": "DEMO"}, headers=headers).status_code == 200
    assert controller.snapshot()["locked"] is False


def test_manual_serial_transaction_finishes_before_lease_acquisition(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    entered = threading.Event()
    continue_move = threading.Event()
    taking_lease = threading.Event()
    original = transport.command

    def block_last_command(command):
        if command.startswith("steps"):
            entered.set()
            assert continue_move.wait(2)
        return original(command)

    def acquire_lease():
        taking_lease.set()
        return controller.lease.acquire("automation-a")

    monkeypatch.setattr(transport, "command", block_last_command)
    with ThreadPoolExecutor(max_workers=2) as pool:
        manual = pool.submit(controller.manual, "move", "bottom", "cw", 1, 100)
        try:
            assert entered.wait(1)
            acquiring = pool.submit(acquire_lease)
            assert taking_lease.wait(1)
            assert not acquiring.done()
        finally:
            continue_move.set()
        manual.result(timeout=2)
        acquiring.result(timeout=2)
    assert transport.commands == ["motor bottom", "dir cw", "speed 1", "steps 100"]
    with pytest.raises(ControllerError, match="controlled by automation"):
        controller.manual("move", "top", "cw", 1, 100)
    assert len(transport.commands) == 4


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
                controller.manual("move", "top", "cw", 1, 100)
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
        with pytest.raises(ControllerError, match="Stop confirmation"):
            controller.lease.release("automation-a")
            controller.manual("move", "top", "cw", 1, 100)
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
    controller.poll()
    response = web.post("/api/connect", json={"port": "DEMO"}, headers=headers)
    assert response.status_code == 200
    assert response.json["connected"] is True
    assert response.json["locked"] is True
    assert response.json["fault"]["code"] == "stop_unconfirmed"
    with pytest.raises(ControllerError, match="not idle"):
        controller.manual("move", "top", "cw", 1, 100)
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
            response = web.post("/api/connect", json={"port": "DEMO"}, headers=headers)
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
    recovered = web.post("/api/connect", json={"port": "DEMO"}, headers=headers)
    assert recovered.status_code == 200
    assert recovered.json["locked"] is True
    assert move(web, motor="top").status_code == 409
    assert "motor top" not in transport.commands
