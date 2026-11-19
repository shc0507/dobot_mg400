"""Console lease, STOP and measured-rotation command boundaries; no hardware."""

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

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
    return controller, transport, clock, app.test_client(), dict(CLIENT)


def post(web, route, body=None, client=CLIENT):
    return web.post("/api/" + route, json=body or {}, headers=client)


def reconnect(web, headers):
    return web.post("/api/command", json={"action": "connect", "port": "DEMO"}, headers=headers)


def acquire(web):
    assert post(web, "heartbeat").status_code == 200


def move(web, motor="bottom", speed=0.05, rotations=1):
    return post(
        web,
        "command",
        dict(action="move", motor=motor, direction="cw", speed=speed, rotations=rotations),
    )


def calibrate(controller, clock, motor="bottom"):
    controller.calibrate(motor, 0.25)
    clock.advance(10)
    controller.poll()
    clock.advance(1.1)
    controller.poll()


def test_stage_contract_conformance(contract):
    controller, transport, clock, web, _ = contract
    check_contract(
        web,
        advance=clock.advance,
        expiry_count=lambda: transport.commands.count("stop all"),
    )
    assert controller.describe()["on_lease_expiry"] == "stop_all"
    assert (
        controller.describe()["commands"]["move"]["params"]["properties"]["speed"]["type"]
        == "number"
    )


def test_rotation_commands_need_lease_and_old_pulse_payload_is_rejected(contract):
    _, transport, _, web, _ = contract
    assert move(web).status_code == 409
    assert transport.commands == []
    acquire(web)
    response = post(
        web, "command", dict(action="move", motor="bottom", direction="cw", speed=50, steps=10)
    )
    assert response.status_code == 400
    assert not transport.commands
    assert move(web, speed=0.2, rotations=0.1).status_code == 202
    assert transport.commands == ["motor bottom", "dir cw", "speed 0.2", "rev 0.1"]


def test_relative_completion_requires_fresh_idle_measured_position(contract, monkeypatch):
    controller, transport, clock, web, _ = contract
    acquire(web)
    response = move(web, speed=0.2, rotations=0.1)
    command_id = response.json["command_id"]
    assert response.json["state"]["last_command"]["result"] == "running"
    clock.advance(0.5)
    controller.poll()
    result = controller.snapshot()["last_command"]
    assert result["id"] == command_id and result["result"] == "ok"
    assert controller.snapshot()["motors"][0]["position_rotations"] == -0.1


@pytest.mark.parametrize("interrupt", ["stop", "release", "reconnect", "reset"])
def test_guarded_absolute_move_cannot_continue_after_interruption(contract, interrupt):
    controller, transport, clock, web, _ = contract
    calibrate(controller, clock)
    acquire(web)
    epoch = controller.snapshot()["stop_epoch"]
    if interrupt == "stop":
        controller.stop("other", 0)
    elif interrupt == "release":
        controller.lease.release("automation-a")
        controller.lease.acquire("automation-a")
    elif interrupt == "reconnect":
        controller.disconnect()
        controller.connect("DEMO")
    else:
        controller.command("reset_position", {"motor": "bottom"}, "automation-a")
    transport.commands.clear()
    result = post(
        web,
        "command",
        dict(
            action="move_to_position_guarded",
            motor="bottom",
            position=0.75,
            speed=0.25,
            stop_epoch=epoch,
        ),
    )
    assert result.status_code == 409
    assert transport.commands == []


def test_guarded_absolute_target_is_sent_directly_to_firmware(contract):
    controller, transport, clock, web, _ = contract
    calibrate(controller, clock)
    acquire(web)
    epoch = controller.snapshot()["stop_epoch"]
    transport.commands.clear()
    result = post(
        web,
        "command",
        dict(
            action="move_to_position_guarded",
            motor="bottom",
            position=0.75,
            speed=0.25,
            stop_epoch=epoch,
        ),
    )
    assert result.status_code == 202
    assert transport.commands == ["motor bottom", "speed 0.25", "goto 0.75"]
    assert controller.snapshot()["stop_epoch"] == epoch
    clock.advance(1)
    controller.poll()
    state = controller.snapshot()
    assert state["last_command"]["id"] == result.json["command_id"]
    assert state["last_command"]["result"] == "ok"
    assert state["motors"][0]["position_rotations"] == 0.75
    assert not state["motors"][0]["positioning_ready"]
    clock.advance(1.1)
    controller.poll()
    assert controller.snapshot()["motors"][0]["positioning_ready"]


def test_any_client_can_stop_and_expiry_stops_without_http(contract):
    controller, transport, clock, web, _ = contract
    acquire(web)
    assert move(web).status_code == 202
    result = post(web, "stop", client={"X-MG400-Client": "other"})
    assert result.json["stop"] == "confirmed"
    assert result.json["state"]["last_command"]["result"] == "stopped"
    clock.advance(1.1)
    controller.poll()
    acquire(web)
    assert move(web).status_code == 202
    before = transport.commands.count("stop all")
    clock.advance(3)
    controller.poll()
    assert transport.commands.count("stop all") == before + 1
    controller.poll()
    assert transport.commands.count("stop all") == before + 1
    assert not controller.snapshot()["busy"]


def test_link_loss_locks_state_and_never_retries_move(contract, monkeypatch):
    controller, transport, _, web, _ = contract
    acquire(web)
    assert move(web).status_code == 202

    def lost():
        raise LinkError("device disconnected")

    monkeypatch.setattr(transport, "poll_status", lost)
    controller.poll()
    state = controller.snapshot()
    assert state["locked"] and not state["connected"]
    assert state["last_command"]["result"] == "failed"
    assert transport.commands.count("rev 1") == 1
    assert transport.commands[-1] == "stop all"
    assert post(web, "stop").json["stop"] == "unconfirmed"


@pytest.mark.parametrize(
    "action,verb",
    [
        ("reset_position", "reset"),
        ("set_limit", "setlimit"),
        ("clear_upper_limit", "clearupper"),
        ("clear_lower_limit", "clearlower"),
        ("clear_fault", "clear"),
    ],
)
def test_encoder_controls_use_contract_lease_and_selected_axis(contract, action, verb):
    controller, transport, _, web, _ = contract
    transport.motors["top"].position = 0.1
    controller.poll()
    data = dict(action=action, motor="top")
    assert post(web, "command", data).status_code == 409
    acquire(web)
    transport.commands.clear()
    assert post(web, "command", data).status_code == 202
    assert transport.commands == ["motor top", verb]
    assert not controller.snapshot()["busy"]


def test_snapshot_uses_encoder_rotations_and_unset_bounds_are_absent(contract):
    controller, transport, clock, _, _ = contract
    state = controller.snapshot()
    assert state["coordinate_unit"] == "rotations" and not state["legacy_geometry_active"]
    motor = state["motors"][0]
    assert motor["steps_per_revolution"] == 200 and motor["max_speed"] == 0.5
    assert motor["lower_limit_rotations"] is motor["upper_limit_rotations"] is None
    transport.motors["bottom"].position = 1.234
    controller.poll()
    assert controller.snapshot()["motors"][0]["position_rotations"] == 1.234
    clock.advance(1.1)
    assert not controller.snapshot()["motors"][0]["telemetry_fresh"]
    assert not controller.snapshot()["motors"][0]["motion_ready"]


def test_calibration_counts_as_busy_even_during_idle_firmware_phase(contract, monkeypatch):
    controller, transport, _, web, _ = contract
    acquire(web)
    original = transport.status

    def idle_calibration():
        states = original()
        states["bottom"] = replace(states["bottom"], mode="idle", calibration="upper")
        return states

    monkeypatch.setattr(transport, "status", idle_calibration)
    controller._read_status()
    assert controller.snapshot()["busy"]
    assert move(web, motor="top").status_code == 409
    assert not any(command.startswith("rev") for command in transport.commands)


def test_contract_accepts_any_host_and_origin(stage_rig):
    controller, _, _ = stage_rig
    web = create_app(controller).test_client()
    headers = {"Host": "stage.example", "Origin": "https://other.example"}
    assert web.get("/api/state", headers=headers).status_code == 200
    assert web.post("/api/heartbeat", json={}, headers={**headers, **CLIENT}).status_code == 200


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
    assert reconnect(web, headers).status_code == 202
    assert controller.snapshot()["locked"] is False


def test_expiry_never_holds_device_lock_while_waiting_and_manual_stop_is_available(
    stage_rig, monkeypatch
):
    controller, _, clock = stage_rig
    controller.lease.acquire("automation-a")
    controller.move("bottom", "cw", 0.05, 1)
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
                    "move", dict(motor="top", direction="cw", speed=0.05, rotations=1), "other"
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
                "move", dict(motor="top", direction="cw", speed=0.05, rotations=1), "automation-a"
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
            response = reconnect(web, headers)
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
    assert response.json["state"]["last_command"]["result"] == (
        "rejected" if replace_connection else "failed"
    )
    assert transport.motors["bottom"].mode == "moving"
    assert replacement_attempts == []
    # Recovery against the same still-moving board keeps the lock after stop returns.
    monkeypatch.setattr(controller, "_factory", lambda _port: transport)
    recovered = reconnect(web, headers)
    assert recovered.status_code == 202
    assert recovered.json["state"]["locked"] is True
    assert move(web, motor="top").status_code == 409
    assert "motor top" not in transport.commands
