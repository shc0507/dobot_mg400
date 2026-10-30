"""Homing/geometry/sequences using only the in-memory transport and clock."""

import copy
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from labkit.errors import ServiceError
from stage_controller.app import create_app
from stage_controller.config import Axis, demo_geometry, load_geometry, parse_geometry
from stage_controller.protocol import MOTOR_IDS, ControllerError, LinkError


def finish_home(controller, clock, motor):
    controller.home(motor)
    clock.advance(0.5)
    controller.poll()
    clock.advance(1)
    assert controller._ready(motor)


def home_all(controller, clock):
    for motor in MOTOR_IDS:
        finish_home(controller, clock, motor)


def test_home_ack_is_not_completion_and_soft_confirmation_is_required(stage_rig):
    controller, transport, clock = stage_rig
    state = controller.home("insert")
    assert state["busy"] and not controller._ready("insert")
    assert transport.commands == ["motor insert", "home"]
    with pytest.raises(ControllerError, match="sequence"):
        controller.goto("insert", "furnace")
    clock.advance(0.5)
    controller.poll()
    assert transport.commands[-2:] == ["motor insert", "soft -1000 1000"]
    assert controller._ready("insert") and not controller.snapshot()["busy"]


@pytest.mark.parametrize("failure", ["timeout", "step limit", "minimum limit did not release"])
def test_failed_home_never_enables_automation_or_retries(stage_rig, failure):
    controller, transport, clock = stage_rig
    transport.home_failures["insert"] = failure
    controller.home("insert")
    clock.advance(0.5)
    controller.poll()
    assert not controller._ready("insert")
    assert failure in controller.snapshot()["automation"]["error"]
    assert transport.commands.count("home") == 1
    assert not any(command.startswith("soft ") for command in transport.commands)
    with pytest.raises(ControllerError, match="position unknown"):
        controller.goto("insert", "furnace")


def test_soft_ack_without_confirming_feedback_is_failure(stage_rig, monkeypatch):
    controller, transport, clock = stage_rig
    original = transport.command
    monkeypatch.setattr(
        transport, "command", lambda value: None if value.startswith("soft ") else original(value)
    )
    controller.home("insert")
    clock.advance(0.5)
    controller.poll()
    assert not controller._ready("insert")
    assert "confirm" in controller.snapshot()["automation"]["error"]


def test_stop_cancels_home_and_remaining_work_without_claiming_a_reference(stage_rig):
    controller, transport, clock = stage_rig
    controller.home("insert")
    assert controller.stop("any-client", 0) == "confirmed"
    clock.advance(5)
    controller.poll()
    assert not controller._ready("insert")
    assert controller.snapshot()["automation"]["active"] is None
    assert not any(command.startswith("soft ") for command in transport.commands)


def test_goto_uses_scaled_absolute_position_and_confirms_final_target(stage_rig):
    controller, transport, clock = stage_rig
    controller.set_microstep(4)
    finish_home(controller, clock, "insert")
    assert "soft -4000 4000" in transport.commands
    controller.goto("insert", "furnace")
    assert transport.commands[-1] == "steps 800"
    clock.advance(16)
    controller.poll()
    assert controller.snapshot()["busy"]  # Completion is consumed before the next leg.
    controller.poll()
    assert not controller.snapshot()["busy"]
    assert controller.snapshot()["motors"][2]["position_mm"] == 20


def test_reconnect_microstep_and_reset_invalidate_host_reference(stage_rig):
    controller, transport, clock = stage_rig
    finish_home(controller, clock, "insert")
    controller.set_microstep(2)
    assert not controller._ready("insert")
    finish_home(controller, clock, "insert")
    transport.motors["insert"].homed = False
    controller.poll()
    assert not controller._ready("insert")
    clock.advance(1)
    finish_home(controller, clock, "insert")
    controller.disconnect()
    controller.connect("DEMO")
    assert not controller._ready("insert")
    with pytest.raises(ControllerError, match="position unknown"):
        controller.goto("insert", "furnace")


def test_legacy_firmware_remains_manual_only(stage_rig):
    controller, transport, _ = stage_rig
    for state in transport.motors.values():
        state.homed = state.soft = None
    controller.poll()
    assert not any(state["homing_available"] for state in controller.snapshot()["motors"])
    with pytest.raises(ControllerError, match="unavailable"):
        controller.home("insert")
    controller.move("bottom", "cw", 10, 10)
    assert transport.commands[-1] == "steps 10"


def test_missing_config_preserves_manual_control_and_stop(stage_rig):
    controller, transport, _ = stage_rig
    controller._init_automation("/no-such-stage-test-config.toml")
    with pytest.raises(ControllerError, match="configuration unavailable"):
        controller.home("bottom")
    controller.move("bottom", "cw", 10, 10)
    assert controller.stop("any", 0) == "confirmed"
    assert not controller.snapshot()["automation"]["configured"]


def moving_pose(controller):
    geometry = demo_geometry()
    geometry.poses["test"] = (("bottom", 1), ("top", 2), ("insert", 3))
    controller._geometry = geometry


def test_pose_preflights_every_axis_before_first_write(stage_rig):
    controller, transport, clock = stage_rig
    moving_pose(controller)
    finish_home(controller, clock, "bottom")
    before = list(transport.commands)
    with pytest.raises(ControllerError, match="position unknown"):
        controller.pose("test")
    assert transport.commands == before
    home_all(controller, clock)
    controller._geometry.poses["bad"] = (("bottom", 1), ("top", 2), ("insert", 999))
    before = list(transport.commands)
    with pytest.raises(ValueError, match="outside"):
        controller.pose("bad")
    assert transport.commands == before


@pytest.mark.parametrize("cancel", ["stop", "release", "new-owner", "expiry"])
def test_no_later_pose_leg_after_stop_or_ownership_change(stage_rig, cancel):
    controller, transport, clock = stage_rig
    home_all(controller, clock)
    moving_pose(controller)
    controller.lease.acquire("original")
    controller.command("pose", {"name": "test"}, "original")
    assert transport.commands[-1] == "steps 10"
    clock.advance(0.5)
    controller.poll()
    if cancel == "stop":
        controller.stop("other", 0)
    elif cancel == "expiry":
        clock.advance(3)
    else:
        controller.lease.release("original")
        if cancel == "new-owner":
            controller.lease.acquire("other")
    controller.poll()
    assert "steps 20" not in transport.commands and "steps 30" not in transport.commands
    assert controller.snapshot()["automation"]["active"] is None


def test_pose_moves_one_axis_at_a_time_and_confirms_completion(stage_rig):
    controller, transport, clock = stage_rig
    home_all(controller, clock)
    moving_pose(controller)
    controller.pose("test")
    for _ in range(10):
        clock.advance(0.5)
        controller.poll()
        assert sum(state.mode != "idle" for state in transport.motors.values()) <= 1
    assert controller.snapshot()["automation"]["active"] is None
    assert [state.position for state in transport.motors.values()] == [10, 20, 30]


def test_early_limit_stop_fails_sequence_without_next_leg(stage_rig):
    controller, transport, clock = stage_rig
    home_all(controller, clock)
    moving_pose(controller)
    transport.hard_ranges["bottom"] = (0, 5)
    controller.pose("test")
    clock.advance(0.5)
    controller.poll()
    assert "requested position" in controller.snapshot()["automation"]["error"]
    assert "steps 20" not in transport.commands


def test_contract_move_needs_home_and_within_bounds(stage_rig):
    controller, transport, clock = stage_rig
    controller.lease.acquire("client")
    args = dict(motor="bottom", direction="cw", speed=10, steps=10)
    with pytest.raises(ServiceError, match="position unknown"):
        controller.command("move", args, "client")
    controller.lease.release("client")
    finish_home(controller, clock, "bottom")
    controller.lease.acquire("client")
    before = list(transport.commands)
    with pytest.raises(ServiceError, match="outside"):
        controller.command("move", args | {"steps": 1001}, "client")
    assert transport.commands == before


def test_awake_renewal_and_legacy_unsupported(stage_rig):
    controller, transport, clock = stage_rig
    controller.awake("insert", 60)
    assert controller.snapshot()["motors"][2]["awake"]
    clock.advance(50)
    controller.awake("insert", 60)
    clock.advance(20)
    controller.poll()
    assert controller.snapshot()["motors"][2]["awake"]
    controller.awake("insert", 0)
    assert not controller.snapshot()["motors"][2]["awake"]
    assert "awake 60000" in transport.commands


def test_awake_zero_preserves_the_normal_post_motion_hold(stage_rig):
    controller, _, clock = stage_rig
    controller.move("bottom", "cw", 50, 10)
    clock.advance(0.5)
    controller.poll()
    controller.awake("bottom", 60)
    controller.awake("bottom", 0)
    assert controller.snapshot()["motors"][0]["awake"]
    clock.advance(10)
    controller.poll()
    assert not controller.snapshot()["motors"][0]["awake"]


@pytest.mark.parametrize("seconds", [True, -1, 61, 10**1000, math.nan, math.inf, "1", None])
def test_invalid_awake_never_writes(stage_rig, seconds):
    controller, transport, _ = stage_rig
    with pytest.raises(ValueError):
        controller.awake("insert", seconds)
    assert not transport.commands


def test_quantization_rounds_bounds_inward_and_rejects_unrepresentable_target():
    axis = Axis(10, 2, 0.11, 0.29, {})
    assert axis.bounds(2) == (2, 2)
    assert axis.target(0.2, 2) == 2
    with pytest.raises(ValueError, match="Rounded"):
        axis.target(0.11, 2)
    with pytest.raises(ValueError, match="no representable"):
        Axis(1, 1, 0.1, 0.2, {}).bounds(1)
    with pytest.raises(ValueError, match="pulse range"):
        Axis(1e300, 1, 0, 10, {}).bounds(16)


def config_data():
    return {
        "motors": {
            motor: dict(
                steps_per_mm=10,
                microstep=1,
                min_mm=0,
                max_mm=100,
                positions={"load": 1, "furnace": 20} if motor == "insert" else {},
            )
            for motor in MOTOR_IDS
        }
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("steps_per_mm", True),
        ("steps_per_mm", 0),
        ("steps_per_mm", math.inf),
        ("microstep", True),
        ("microstep", 3),
        ("min_mm", 101),
        ("max_mm", math.nan),
    ],
)
def test_bad_config_is_rejected(key, value):
    data = config_data()
    data["motors"]["bottom"][key] = value
    with pytest.raises(ValueError):
        parse_geometry(data)


def test_configuration_names_pose_shape_and_bounds():
    data = config_data()
    data["poses"] = {
        "furnace": dict(
            order=["insert", "bottom", "top"], positions=dict(bottom=1, top=2, insert=20)
        )
    }
    assert parse_geometry(data).poses["furnace"][0] == ("insert", 20)
    for edit in (
        lambda value: value["poses"]["furnace"].update(order=["insert", "bottom", "bottom"]),
        lambda value: value["motors"]["insert"]["positions"].update(furnace=101),
        lambda value: value["motors"]["insert"]["positions"].update({"../bad": 1}),
        lambda value: value.update(unexpected=True),
    ):
        modified = copy.deepcopy(data)
        edit(modified)
        with pytest.raises(ValueError):
            parse_geometry(modified)


def test_config_loader_rejects_symlink_and_unsafe_mode(tmp_path):
    path = tmp_path / "stage.toml"
    path.write_text("motors = {}\n")
    path.chmod(0o666)
    with pytest.raises(ValueError, match="mode"):
        load_geometry(path)
    path.chmod(0o600)
    link = tmp_path / "link.toml"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        load_geometry(link)


def test_link_loss_during_home_never_retries_or_retains_reference(stage_rig, monkeypatch):
    controller, transport, clock = stage_rig
    controller.home("bottom")
    monkeypatch.setattr(transport, "status", lambda: (_ for _ in ()).throw(LinkError("lost")))
    clock.advance(1)
    controller.poll()
    assert controller.snapshot()["locked"]
    assert controller.snapshot()["automation"]["active"] is None
    assert transport.commands.count("home") == 1


@pytest.mark.parametrize("operation", ["stop", "disconnect"])
@pytest.mark.parametrize("action", ["home", "pose"])
def test_unknown_stop_never_records_sequence_as_stopped(stage_rig, monkeypatch, operation, action):
    controller, transport, clock = stage_rig
    if action == "pose":
        home_all(controller, clock)
        moving_pose(controller)
    controller.lease.acquire("client")
    command_id = controller.command(
        action, {"motor": "insert"} if action == "home" else {"name": "test"}, "client"
    )
    original = transport.command

    def lost(command):
        if command == "stop all":
            raise LinkError("stop packet lost")
        original(command)

    monkeypatch.setattr(transport, "command", lost)
    if operation == "stop":
        assert controller.stop("other", 0) == "unconfirmed"
    else:
        with pytest.raises(LinkError):
            controller.disconnect()
    state = controller.snapshot()
    assert state["last_command"]["id"] == command_id
    assert state["last_command"]["result"] == "failed"
    assert "unconfirmed" in state["last_command"]["message"]
    assert state["locked"] and state["automation"]["active"] is None


def test_stop_ack_without_idle_keeps_sequence_completion_pending(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    controller.lease.acquire("client")
    controller.command("home", {"motor": "insert"}, "client")
    original = transport.command
    monkeypatch.setattr(
        transport, "command", lambda value: None if value == "stop all" else original(value)
    )
    assert controller.stop("other", 0) == "pending"
    assert controller.snapshot()["last_command"] is None
    assert controller.snapshot()["locked"]
    original("stop all")
    controller.poll()
    assert controller.snapshot()["last_command"]["result"] == "stopped"
    assert controller.snapshot()["locked"]  # Stop uncertainty lock needs manual recovery.


def test_sequence_failure_with_unconfirmed_stop_stays_locked_after_late_idle(
    stage_rig, monkeypatch
):
    controller, transport, clock = stage_rig
    controller.home("insert")
    transport.homes["insert"] = clock() + 1000
    original = transport.command
    monkeypatch.setattr(
        transport, "command", lambda value: None if value == "stop all" else original(value)
    )
    clock.advance(126)
    controller.poll()
    assert controller.snapshot()["locked"]
    assert controller.snapshot()["automation"]["active"] is None
    original("stop all")
    clock.advance(2)
    controller.poll()
    assert controller.snapshot()["locked"]
    with pytest.raises(ControllerError, match="locked"):
        controller.home("insert")


def test_config_parent_swap_never_reads_a_replacement_symlink(tmp_path, monkeypatch):
    parent = tmp_path / "calibration"
    parent.mkdir()
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    for directory, scale in ((parent, 10), (replacement, 500)):
        path = directory / "stage.toml"
        path.write_text(
            "\n".join(
                f"[motors.{motor}]\nsteps_per_mm={scale}\nmicrostep=1\nmin_mm=0\nmax_mm=100\n"
                + ("[motors.insert.positions]\nload=1\nfurnace=20\n" if motor == "insert" else "")
                for motor in MOTOR_IDS
            )
        )
        path.chmod(0o640)
    original = os.open
    swapped = False

    def swap(path, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = original(path, flags, *args, **kwargs)
        if path == "calibration" and not swapped:
            swapped = True
            parent.rename(tmp_path / "original")
            parent.symlink_to(replacement, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(os, "open", swap)
    geometry = load_geometry(parent / "stage.toml")
    assert swapped and geometry.motors["insert"].steps_per_mm == 10


def test_new_panel_routes_keep_auth_lease_and_json_guards(stage_rig):
    controller, transport, clock = stage_rig
    web = create_app(controller).test_client()
    token = re.search(r'name="control-token" content="([^"]+)"', web.get("/").text)[1]
    headers = {"X-Control-Token": token}
    routes = {
        "/api/motors/insert/home": {},
        "/api/motors/insert/awake": {"seconds": 1},
        "/api/motors/insert/goto": {"name": "furnace"},
        "/api/poses/furnace": {},
    }
    for route, body in routes.items():
        assert web.post(route, json=body).status_code == 403
        assert web.post(route, data="{}", headers=headers).status_code == 415
        assert web.post(route, json=[], headers=headers).status_code == 400
    controller.lease.acquire("automation")
    for route, body in routes.items():
        assert web.post(route, json=body, headers=headers).status_code == 409
    assert not transport.commands
    controller.lease.release("automation")
    assert web.post("/api/motors/insert/home", json={}, headers=headers).status_code == 200
    clock.advance(0.5)
    controller.poll()
    clock.advance(1)
    assert (
        web.post("/api/motors/insert/awake", json={"seconds": 60}, headers=headers).status_code
        == 200
    )
    assert (
        web.post("/api/motors/insert/goto", json={"name": "furnace"}, headers=headers).status_code
        == 200
    )
    assert web.post("/api/stop", json={}, headers=headers).status_code == 200


def test_contract_home_completion_and_positive_awake_require_verified_reference(stage_rig):
    controller, transport, clock = stage_rig
    web = create_app(controller).test_client()
    headers = {"X-MG400-Client": "automation"}
    assert web.post("/contract/api/heartbeat", json={}, headers=headers).status_code == 200
    assert (
        web.post(
            "/contract/api/command",
            json=dict(action="awake", motor="insert", seconds=1),
            headers=headers,
        ).status_code
        == 409
    )
    response = web.post(
        "/contract/api/command", json=dict(action="home", motor="insert"), headers=headers
    )
    assert response.status_code == 202
    command_id = response.json["command_id"]
    assert response.json["state"]["last_command"]["id"] != command_id
    clock.advance(0.5)
    controller.poll()
    state = web.get("/contract/api/state").json
    assert state["last_command"]["id"] == command_id
    assert state["last_command"]["result"] == "ok"
    assert (
        web.post(
            "/contract/api/command",
            json=dict(action="awake", motor="insert", seconds=60),
            headers=headers,
        ).status_code
        == 202
    )
    assert transport.commands[-1] == "awake 60000"


def test_config_file_owner_regular_file_and_size_are_checked(tmp_path, monkeypatch):
    path = tmp_path / "stage.toml"
    path.write_bytes(b" " * 1_048_577)
    path.chmod(0o600)
    with pytest.raises(ValueError, match="1 MiB"):
        load_geometry(path)
    original = os.fstat

    def foreign_owner(fd):
        data = list(original(fd))
        data[4] = 2001
        return os.stat_result(data)

    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(ValueError, match="owned"):
        load_geometry(path)


def test_same_client_release_and_reacquire_cannot_resume_a_pose(stage_rig):
    controller, transport, clock = stage_rig
    home_all(controller, clock)
    moving_pose(controller)
    controller.lease.acquire("client")
    controller.command("pose", {"name": "test"}, "client")
    epoch = controller.lease.epoch
    clock.advance(0.5)
    controller.poll()
    controller.lease.release("client")
    controller.lease.acquire("client")
    assert controller.lease.epoch > epoch
    controller.poll()
    assert "steps 20" not in transport.commands
    assert controller.snapshot()["last_command"]["result"] == "failed"


def test_renewal_keeps_the_same_sequence_authority(stage_rig):
    controller, transport, clock = stage_rig
    home_all(controller, clock)
    moving_pose(controller)
    controller.lease.acquire("client")
    controller.command("pose", {"name": "test"}, "client")
    epoch = controller.lease.epoch
    clock.advance(0.5)
    controller.poll()
    controller.lease.renew("client")
    controller.lease.acquire("client")
    assert controller.lease.epoch == epoch
    controller.poll()
    assert "steps 20" in transport.commands


def test_poll_defers_while_another_request_runs_expiry_then_recovers(stage_rig, monkeypatch):
    controller, transport, clock = stage_rig
    entered, proceed = threading.Event(), threading.Event()
    original = controller.lease.on_expire

    def blocked_expiry():
        entered.set()
        assert proceed.wait(2)
        original()

    monkeypatch.setattr(controller.lease, "on_expire", blocked_expiry)
    controller.lease.acquire("client")
    controller.command("home", {"motor": "insert"}, "client")
    clock.advance(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        expiry = pool.submit(controller.lease.poll)
        try:
            assert entered.wait(1)
            before = list(transport.commands)
            controller.poll()  # A normal transition must not kill the polling thread.
            assert transport.commands == before
        finally:
            proceed.set()
        assert expiry.result(timeout=2)
    clock.advance(1)
    controller.poll()
    assert controller.snapshot()["feedback_fresh"]
    assert controller.snapshot()["automation"]["active"] is None


def test_failed_expiry_never_advances_or_reacquires_and_explicit_stop_is_available(
    stage_rig, monkeypatch
):
    controller, transport, clock = stage_rig
    controller.lease.acquire("client")
    controller.command("home", {"motor": "insert"}, "client")
    monkeypatch.setattr(
        controller.lease, "on_expire", lambda: (_ for _ in ()).throw(RuntimeError("expiry failed"))
    )
    clock.advance(3)
    with pytest.raises(RuntimeError, match="expiry failed"):
        controller.lease.poll()
    before = list(transport.commands)
    controller.poll()
    assert transport.commands == before
    with pytest.raises(ServiceError, match="Lease expiry failed"):
        controller.lease.acquire("client")
    assert controller.stop("human", 0) == "confirmed"


def test_named_position_guard_uses_calibration_idle_and_actual_feedback_age(stage_rig):
    controller, _, clock = stage_rig
    controller._geometry.motors["insert"].positions["load"] = 5
    finish_home(controller, clock, "insert")
    assert controller.snapshot()["motors"][2]["at_positions"] == []
    controller.goto("insert", "load")
    assert controller.snapshot()["motors"][2]["at_positions"] == []
    clock.advance(1)
    controller.poll()
    controller.poll()
    state = controller.snapshot()
    assert state["feedback_fresh"] and state["feedback_age_s"] == 0
    assert state["motors"][2]["at_positions"] == ["load"]
    clock.advance(2.01)
    state = controller.snapshot()
    assert not state["feedback_fresh"] and state["feedback_age_s"] > 2
    assert not state["motors"][2]["automation_ready"]
    assert state["motors"][2]["at_positions"] == []
    controller.poll()
    assert controller.snapshot()["motors"][2]["at_positions"] == ["load"]
    controller.disconnect()
    assert controller.snapshot()["feedback_age_s"] is None
    assert not controller.snapshot()["feedback_fresh"]
