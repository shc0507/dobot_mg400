"""Encoder profile recording and sequential stage motion without physical hardware."""

import copy
import json
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask import Blueprint, Flask

from labkit.errors import ServiceError
from orchestrator.transfer_profiles import (
    TransferProfileStore,
    new_profile,
    record_endpoint,
    record_position,
)
from orchestrator.web.sample_transfer import LiveTransferAdapter, SampleTransfer
from orchestrator.web.workspaces import motion
from orchestrator.web.workspaces.sample_transfer import register

PAGE = "sample-transfer-test"
SOURCE = {"kind": "stage_encoder", "device_id": "test-stage", "name": "Stage encoders"}


def report(position):
    return {
        "source": SOURCE,
        "received_at": "2026-11-12T12:00:00+00:00",
        "axes": {
            axis: {"value_rotations": value, "ready": True} for axis, value in position.items()
        },
    }


class Adapter:
    def __init__(self):
        self.control = True
        self.epoch = 1
        self.values = {"top": 2.0, "bottom": 3.0, "insert": 0.0}
        self.moves = []
        self.stops = 0
        self.last = None
        self.moving = None
        self.finish_move = threading.Event()
        self.finish_move.set()
        self.started = threading.Event()
        self.after_complete = None
        self.before_move = None
        self.overrides = {}

    def owns(self, page):
        return self.control and page == PAGE

    def stage_state(self, page):
        if self.moving and self.finish_move.is_set():
            self.values[self.moving["motor"]] = self.moving["position"]
            self.moving = None
            self.last["result"] = "ok"
            if self.after_complete:
                self.after_complete()
        return {
            "connected": True,
            "feedback_fresh": True,
            "stop_epoch": self.epoch,
            "port": "test-stage",
            "last_command": copy.deepcopy(self.last),
            "motors": [
                {
                    "id": motor,
                    "position_rotations": value,
                    "mode": "moving" if self.moving and self.moving["motor"] == motor else "idle",
                    "encoder": "ok",
                    "fault": "none",
                    "telemetry_fresh": True,
                    "available": self.moving is None,
                    "positioning_ready": self.moving is None,
                    "lower_limit_rotations": 0.0,
                    "upper_limit_rotations": 10.0,
                    "tolerance": 4 / 4096,
                    "calibration": "done",
                    "cooldown": 0,
                    **self.overrides.get(motor, {}),
                }
                for motor, value in self.values.items()
            ],
        }

    def position(self, page, **params):
        if self.before_move:
            self.before_move()
        if params["stop_epoch"] != self.epoch:
            return {"outcome": "refused", "error": "Stage motion was cancelled."}
        assert self.moving is None, "Axes must never overlap"
        self.moves.append(params)
        identifier = str(uuid4())
        self.last = {"id": identifier, "result": "running"}
        self.moving = params
        self.started.set()
        return {"outcome": "accepted", "command_id": identifier}

    def move(self, page, **params):
        position = self.values[params["motor"]] + params.pop("rotations") * (
            1 if params.pop("direction") == "ccw" else -1
        )
        return self.position(page, position=position, **params)

    def stop(self):
        self.epoch += 1
        self.stops += 1
        self.moving = None
        return {"outcome": "confirmed"}


@pytest.fixture
def rig(tmp_path):
    store, adapter = TransferProfileStore(tmp_path), Adapter()
    profile = record_position(
        new_profile("Named positions"), "Pickup", SOURCE, report({"x": 6, "y": 5, "insert": 8})
    )
    for target, value in (("out_stage", 1), ("microscope", 9)):
        profile = record_endpoint(
            profile, target, SOURCE, report({"x": 6, "y": 5, "insert": value})
        )
    saved = store.save(profile)
    store.select(saved["id"])
    manager = SampleTransfer(store, adapter, demo=True)
    yield manager, adapter
    manager.cancel()
    adapter.finish_move.set()
    if manager.operation:
        assert manager.operation["done"].wait(3)


def finish(manager):
    assert manager.operation["done"].wait(3)
    return manager.state(PAGE)["operation"]


@pytest.mark.parametrize(("target", "position"), [("fully_out", 0), ("fully_in", 10)])
def test_transfer_uses_live_limits_without_profile_and_moves_only_insert(rig, target, position):
    manager, adapter = rig
    adapter.values["insert"] = 4.0
    manager.store.delete(manager.store.selection())
    for motor in ("top", "bottom"):
        adapter.overrides[motor] = {"positioning_ready": False, "encoder": "unavailable"}
    manager.start(PAGE, target)
    assert finish(manager)["status"] == "completed"
    assert adapter.moves == [
        {"motor": "insert", "position": position, "speed": 0.25, "stop_epoch": 1}
    ]
    assert adapter.values["insert"] == position
    assert adapter.values["top"] == 2 and adapter.values["bottom"] == 3


def test_transfer_uses_absolute_zero_and_current_upper_limit(rig):
    manager, adapter = rig
    adapter.overrides["insert"] = {"lower_limit_rotations": -2, "upper_limit_rotations": 8}
    state = manager.state(PAGE)
    assert state["transfer_reference"]["targets_rotations"] == {"fully_out": 0, "fully_in": 8}
    manager.start(PAGE, "fully_in")
    assert finish(manager)["status"] == "completed"
    assert adapter.values["insert"] == 8
    manager.start(PAGE, "fully_out")
    assert finish(manager)["status"] == "completed"
    assert adapter.values["insert"] == 0
    adapter.overrides["insert"]["lower_limit_rotations"] = 1
    assert manager.state(PAGE)["transfer_reference"]["ready_targets"] == {
        "fully_out": False,
        "fully_in": True,
    }
    with pytest.raises(ValueError, match="within its travel limits"):
        manager.start(PAGE, "fully_out")


@pytest.mark.parametrize(
    "override",
    [
        {"positioning_ready": False},
        {"telemetry_fresh": False},
        {"encoder": "unavailable"},
        {"fault": "encoder"},
        {"upper_limit_rotations": None},
        {"lower_limit_rotations": None},
    ],
)
def test_transfer_requires_calibrated_fresh_insert(rig, override):
    manager, adapter = rig
    adapter.overrides["insert"] = override
    with pytest.raises(ValueError, match="Calibrate Insert"):
        manager.start(PAGE, "fully_in")
    assert adapter.moves == []


@pytest.mark.parametrize("target", ["out_stage", "microscope"])
def test_stage_targets_move_bottom_then_top_without_insert(rig, target):
    manager, adapter = rig
    manager.start_stage(PAGE, target)
    assert finish(manager)["status"] == "completed"
    assert [(move["motor"], move["position"]) for move in adapter.moves] == [
        ("bottom", 5),
        ("top", 6),
    ]
    assert adapter.values["insert"] == 0


@pytest.mark.parametrize(
    ("insert", "override"),
    [
        (10.0, {}),
        (5 / 4096, {}),
        (0.0, {"encoder": "unavailable"}),
        (0.0, {"positioning_ready": False}),
    ],
)
def test_xy_moves_require_insert_fully_out(rig, insert, override):
    manager, adapter = rig
    adapter.values["insert"] = insert
    adapter.overrides["insert"] = override
    reference = manager.state(PAGE)["stage_reference"]
    assert reference["ready_targets"] == {"out_stage": False, "microscope": False}
    assert reference["message"].startswith("Extract Sample first")
    for start in (
        lambda: manager.start_stage(PAGE, "out_stage"),
        lambda: manager.start_coordinates(PAGE, {"x": 6, "y": 5}),
        lambda: manager.jog(PAGE, "top", "ccw"),
        lambda: manager.jog(PAGE, "bottom", "cw"),
    ):
        with pytest.raises(ValueError, match="Extract Sample first"):
            start()
    assert adapter.moves == []


def test_xy_already_at_target_does_not_need_insert_out(rig):
    manager, adapter = rig
    adapter.values.update(top=6.0, bottom=5.0, insert=10.0)
    manager.start_coordinates(PAGE, {"x": 6, "y": 5})
    assert finish(manager)["status"] == "completed"
    manager.start_position(PAGE, "Pickup")
    assert finish(manager)["status"] == "completed"
    assert [(move["motor"], move["position"]) for move in adapter.moves] == [("insert", 8)]


def test_named_position_from_inserted_extracts_before_xy(rig):
    manager, adapter = rig
    adapter.values["insert"] = 10.0
    manager.start_position(PAGE, "Pickup")
    result = finish(manager)
    assert result["status"] == "completed"
    assert result["completed_axes"] == result["total_axes"] == 4
    assert [(move["motor"], move["position"]) for move in adapter.moves] == [
        ("insert", 0),
        ("bottom", 5),
        ("top", 6),
        ("insert", 8),
    ]


def test_insert_moved_during_sequence_stops_before_next_xy_axis(rig):
    manager, adapter = rig
    adapter.after_complete = lambda: adapter.values.update(insert=3.0)
    manager.start_coordinates(PAGE, {"x": 6, "y": 5})
    result = finish(manager)
    assert result["status"] == "failed"
    assert result["message"].startswith("Extract Sample first")
    assert [move["motor"] for move in adapter.moves] == ["bottom"]


def test_stage_record_is_draft_only_until_save_and_survives_restart(rig):
    manager, adapter = rig
    state = manager.mutate(PAGE, "endpoint/record", {"target": "out_stage"})
    capture = state["draft"]["stage_positions"]["out_stage"]
    assert capture["position_rotations"] == {"x": 2, "y": 3}
    assert state["stage_reference"]["targets_rotations"]["out_stage"] == {"x": 6, "y": 5}
    state = manager.mutate(PAGE, "profile/save", {"name": "Stage coordinates"})
    assert state["stage_reference"]["targets_rotations"]["out_stage"] == {"x": 2, "y": 3}
    assert state["stage_reference"]["ready_targets"] == {"out_stage": True, "microscope": False}
    saved_id = state["selected_profile"]
    restarted = SampleTransfer(TransferProfileStore(manager.store.root), adapter)
    assert restarted.state(PAGE)["active_profile"]["stage_positions"]["out_stage"] == capture
    adapter.values.update(top=6, bottom=7)
    restarted.start_stage(PAGE, "out_stage")
    assert finish(restarted)["status"] == "completed"
    assert adapter.values == {"top": 2, "bottom": 3, "insert": 0}
    restarted.mutate(PAGE, "profile/load", {"id": saved_id})
    state = restarted.mutate(PAGE, "endpoint/delete", {"target": "out_stage"})
    assert state["draft"]["stage_positions"] == {}
    assert state["stage_reference"]["ready_targets"]["out_stage"]
    state = restarted.mutate(PAGE, "profile/save", {"name": "Cleared targets"})
    assert state["selected_profile"] != saved_id
    assert state["selected_profile"] == state["draft"]["id"]
    assert state["stage_reference"]["ready_targets"] == {"out_stage": False, "microscope": False}


def test_stage_record_ignores_insert_readiness_and_rejects_unready_xy(rig):
    manager, adapter = rig
    adapter.overrides["insert"] = {"positioning_ready": False, "encoder": "unavailable"}
    manager.mutate(PAGE, "endpoint/record", {"target": "out_stage"})
    before = manager.state(PAGE)
    assert before["endpoint_record_ready"] is True
    assert before["record_ready"] is False
    adapter.overrides["top"] = {"positioning_ready": False}
    with pytest.raises(ValueError, match="Calibrate the required axes"):
        manager.mutate(PAGE, "endpoint/record", {"target": "microscope"})
    after = manager.state(PAGE)
    assert after["draft"] == before["draft"]
    assert after["selected_profile"] == before["selected_profile"]
    assert after["endpoint_record_ready"] is False


def test_saved_stage_target_outside_current_limits_cannot_move(rig):
    manager, adapter = rig
    adapter.overrides["top"] = {"upper_limit_rotations": 4}
    state = manager.state(PAGE)
    assert state["stage_reference"]["ready_targets"] == {"out_stage": False, "microscope": False}
    with pytest.raises(ValueError, match="current travel limits"):
        manager.start_stage(PAGE, "out_stage")
    assert adapter.moves == []


def test_missing_stage_profile_does_not_use_limits_or_named_positions(rig):
    manager, adapter = rig
    profile = record_position(
        new_profile("Named"), "out_stage", SOURCE, report({"x": 6, "y": 5, "insert": 8})
    )
    saved = manager.store.save(profile)
    manager.store.select(saved["id"])
    with pytest.raises(ValueError, match="Record the missing"):
        manager.start_stage(PAGE, "out_stage")
    manager.store.delete(saved["id"])
    with pytest.raises(ValueError, match="Select a saved"):
        manager.start_stage(PAGE, "microscope")
    assert adapter.moves == []


def test_default_profile_name_contains_current_date_and_marker(rig):
    from datetime import datetime

    manager, _ = rig
    expected = f"Stage Coordinate Calibration {datetime.now().date().isoformat()}"
    state = manager.state(PAGE)
    assert state["draft"]["name"] == expected
    assert state["draft_name_is_default"] is True
    state = manager.mutate(PAGE, "profile/new", {"name": "Browser date name"})
    assert state["draft"]["name"] == "Browser date name"
    assert state["draft_name_is_default"] is False
    state = manager.mutate(PAGE, "profile/new", {})
    assert state["draft"]["name"] == expected
    state = manager.mutate(PAGE, "profile/save", {})
    assert state["draft_name_is_default"] is False


def test_named_position_moves_bottom_top_insert_one_at_a_time(rig):
    manager, adapter = rig
    manager.start_position(PAGE, "Pickup")
    result = finish(manager)
    assert result["status"] == "completed"
    assert result["completed_axes"] == 3
    assert [(move["motor"], move["position"]) for move in adapter.moves] == [
        ("bottom", 5),
        ("top", 6),
        ("insert", 8),
    ]


@pytest.mark.parametrize(
    ("target", "expected"),
    [({"x": 6, "y": 5}, [("bottom", 5), ("top", 6)]), ({"insert": 8}, [("insert", 8)])],
)
def test_coordinate_targets_use_shared_sequence_without_profiles(
    rig, monkeypatch, target, expected
):
    manager, adapter = rig
    manager.store.delete(manager.store.selection())
    for motor in adapter.values:
        # X/Y targets read Insert to confirm that it is fully out.
        if motor not in dict(expected) and not (motor == "insert" and "x" in target):
            adapter.overrides[motor] = {"positioning_ready": False, "encoder": "unavailable"}

    def no_profiles(*args):
        pytest.fail("Coordinate motion must not read or create profiles.")

    monkeypatch.setattr(manager.store, "selected", no_profiles)
    monkeypatch.setattr(manager.store, "list", no_profiles)
    monkeypatch.setattr(manager, "_page", no_profiles)
    before = dict(adapter.values)
    operation = manager.start_coordinates(PAGE, target)
    assert operation["target"] == target
    assert operation["mine"] is True
    assert manager.operation["done"].wait(3)
    operation = manager.operation_state(PAGE)
    assert operation["status"] == "completed"
    assert operation["completed_axes"] == operation["total_axes"] == len(expected)
    assert not operation["settling"]
    assert [(move["motor"], move["position"]) for move in adapter.moves] == expected
    assert all(move["speed"] == 0.25 and move["stop_epoch"] == 1 for move in adapter.moves)
    assert adapter.values == before | dict(expected)
    assert manager.pages == {}
    assert manager.operation_state("other-page")["mine"] is False


def test_coordinate_targets_recheck_limits_before_the_next_axis(rig):
    manager, adapter = rig
    adapter.after_complete = lambda: adapter.overrides.update(top={"upper_limit_rotations": 5})
    manager.start_coordinates(PAGE, {"x": 6, "y": 5})
    assert finish(manager)["status"] == "failed"
    assert [move["motor"] for move in adapter.moves] == ["bottom"]
    assert adapter.values["top"] == 2


def test_coordinate_targets_preserve_saved_profiles_and_drafts(rig):
    manager, adapter = rig
    before = manager.state(PAGE)
    files = {path: path.read_bytes() for path in manager.store.root.rglob("*.json")}
    manager.start_coordinates(PAGE, {"x": 6, "y": 5})
    assert finish(manager)["status"] == "completed"
    after = manager.state(PAGE)
    assert after["draft"] == before["draft"]
    assert after["active_profile"] == before["active_profile"]
    assert after["selected_profile"] == before["selected_profile"]
    assert {path: path.read_bytes() for path in manager.store.root.rglob("*.json")} == files


def test_axis_inside_reported_tolerance_is_skipped(rig):
    manager, adapter = rig
    adapter.values["bottom"] = 5 + 1 / 4096
    manager.start_position(PAGE, "Pickup")
    assert finish(manager)["status"] == "completed"
    assert [move["motor"] for move in adapter.moves] == ["top", "insert"]


@pytest.mark.parametrize("method", ["stop", "lease", "external_stop", "stale", "fault"])
@pytest.mark.parametrize("kind", ["named", "stage", "coordinates"])
def test_interruption_never_starts_next_axis(rig, method, kind):
    manager, adapter = rig
    adapter.finish_move.clear()
    if kind == "coordinates":
        manager.start_coordinates(PAGE, {"x": 6, "y": 5})
    elif kind == "stage":
        manager.start_stage(PAGE, "out_stage")
    else:
        manager.start_position(PAGE, "Pickup")
    assert adapter.started.wait(1)
    assert len(adapter.moves) == 1
    if method == "stop":
        manager.stop()
    elif method == "lease":
        adapter.control = False
    elif method == "external_stop":
        adapter.stop()
    elif method == "stale":
        adapter.overrides["bottom"] = {"telemetry_fresh": False}
    else:
        adapter.overrides["bottom"] = {"fault": "tracking"}
    result = finish(manager)
    assert result["status"] in ("failed", "stopped")
    assert [move["motor"] for move in adapter.moves] == ["bottom"]


def test_stop_between_preflight_and_command_is_rejected_by_epoch(rig):
    manager, adapter = rig
    adapter.before_move = adapter.stop
    manager.start(PAGE, "fully_in")
    assert finish(manager)["status"] == "failed"
    assert adapter.moves == []


def test_stop_between_axes_prevents_continuation(rig):
    manager, adapter = rig
    adapter.after_complete = manager.stop
    manager.start_position(PAGE, "Pickup")
    assert finish(manager)["status"] == "stopped"
    assert len(adapter.moves) == 1


def test_epoch_change_stops_firmware_motion_before_cancelling_sequence(rig):
    manager, adapter = rig
    adapter.finish_move.clear()
    manager.start_position(PAGE, "Pickup")
    assert adapter.started.wait(1)
    # A rejected non-guarded command can change the epoch without stopping motion.
    adapter.epoch += 1
    assert adapter.moving is not None
    outcome = finish(manager)
    assert outcome["status"] == "stopped"
    assert adapter.moving is None
    assert adapter.stops == 1
    assert len(adapter.moves) == 1


@pytest.mark.parametrize("lease_lost", [False, True])
def test_cleanup_does_not_stop_new_device_lease_holder(rig, lease_lost):
    manager, adapter = rig
    adapter.finish_move.clear()
    manager.start_position(PAGE, "Pickup")
    assert adapter.started.wait(1)
    # The Console session can still appear held after the device lease changes.
    adapter.owns_current = lambda page: False
    if lease_lost:
        adapter.control = False
    else:
        adapter.epoch += 1
    outcome = finish(manager)
    assert outcome["status"] == ("failed" if lease_lost else "stopped")
    assert adapter.stops == 0
    assert len(adapter.moves) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"positioning_ready": False},
        {"telemetry_fresh": False},
        {"upper_limit_rotations": 4},
        {"encoder": "untrusted"},
        {"fault": "direction"},
        {"calibration": "upper", "mode": "moving"},
    ],
)
def test_named_motion_rejects_unready_stale_faulted_or_out_of_range_axis(rig, override):
    manager, adapter = rig
    adapter.overrides["top"] = override
    with pytest.raises(ValueError):
        manager.start_position(PAGE, "Pickup")
    assert adapter.moves == []


def test_early_idle_away_from_target_is_failed(rig):
    manager, adapter = rig

    def short_move():
        adapter.values["bottom"] = 4

    adapter.after_complete = short_move
    manager.start_position(PAGE, "Pickup")
    assert finish(manager)["status"] == "failed"
    assert len(adapter.moves) == 1
    assert adapter.stops == 1


def test_console_stop_callback_preserves_the_failure_message(rig):
    manager, adapter = rig
    stop = adapter.stop

    def console_stop():
        manager.cancel()
        return stop()

    adapter.stop = console_stop
    adapter.finish_move.clear()
    manager.start_position(PAGE, "Pickup")
    assert adapter.started.wait(1)
    adapter.overrides["bottom"] = {"fault": "tracking"}
    outcome = finish(manager)
    assert outcome["status"] == "failed"
    assert "healthy encoder" in outcome["message"]
    assert adapter.stops == 1


def test_stop_does_not_wait_for_recording_service_read(rig):
    manager, adapter = rig
    started, release = threading.Event(), threading.Event()
    original = adapter.stage_state
    failures = []

    def delayed(page):
        if threading.current_thread().name == "record":
            started.set()
            assert release.wait(2)
        return original(page)

    def record():
        try:
            manager.mutate(PAGE, "record", {"name": "Dock"})
        except Exception as exc:
            failures.append(exc)

    adapter.stage_state = delayed
    worker = threading.Thread(target=record, name="record")
    worker.start()
    assert started.wait(1)
    try:
        assert manager.stop() == {"outcome": "confirmed"}
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not failures
    assert adapter.moves == []


def test_record_uses_current_encoders_and_profiles_persist(rig):
    manager, adapter = rig
    state = manager.mutate(PAGE, "record", {"name": "Dock"})
    assert state["draft"]["positions"]["Dock"]["position_rotations"] == {
        "x": 2,
        "y": 3,
        "insert": 0,
    }
    state = manager.mutate(PAGE, "profile/save", {"name": "My positions"})
    saved = state["selected_profile"]
    restarted = SampleTransfer(TransferProfileStore(manager.store.root), adapter)
    assert restarted.state(PAGE)["active_profile"]["id"] == saved
    adapter.values["top"] = 7
    restarted.start_position(PAGE, "Dock")
    assert finish(restarted)["status"] == "completed"
    assert adapter.values == {"top": 2, "bottom": 3, "insert": 0}


def test_record_requires_fresh_calibrated_readings_and_never_uses_camera(rig):
    manager, adapter = rig
    adapter.overrides["insert"] = {"positioning_ready": False}
    with pytest.raises(ValueError, match="Calibrate"):
        manager.mutate(PAGE, "record", {"name": "Dock"})
    assert manager.state(PAGE)["draft"]["positions"] == {}
    assert manager.state(PAGE)["record_ready"] is False


def test_passive_state_requires_no_control_and_never_moves(rig):
    manager, adapter = rig
    adapter.control = False
    state = manager.state(PAGE)
    assert state["position"] == {"x": 2, "y": 3, "insert": 0}
    assert not state["has_control"]
    assert adapter.moves == []
    with pytest.raises(ServiceError):
        manager.start(PAGE, "fully_in")
    assert manager.stop() == {"outcome": "confirmed"}


def test_legacy_profile_is_preserved_but_cannot_move_or_record(rig):
    manager, adapter = rig
    profile = new_profile("Legacy")
    profile.pop("unit")
    profile.pop("stage_positions")
    profile.update(
        schema_version=2,
        source={
            "camera": "camera1",
            "device_id": "old-camera",
            "profile_id": str(uuid4()),
            "name": "Old optics",
        },
    )
    profile["positions"] = {
        "Pickup": {
            "position_mm": {"x": 1, "y": 2, "insert": 3},
            "captured_at": profile["created_at"],
            "image_sha256": "a" * 64,
        }
    }
    state = manager.mutate(PAGE, "profile/upload", {"profile": json.dumps(profile)})
    identifier = state["draft"]["id"]
    manager.mutate(PAGE, "profile/select", {"id": identifier})
    with pytest.raises(ValueError, match="Legacy"):
        manager.start_position(PAGE, "Pickup")
    with pytest.raises(ValueError, match="legacy millimetres"):
        manager.mutate(PAGE, "record", {"name": "New"})
    assert manager.store.export(identifier)["positions"]["Pickup"]["position_mm"]["insert"] == 3
    assert adapter.moves == []
    manager.mutate(PAGE, "profile/delete", {"id": identifier})
    assert manager.store.selection() is None


def test_jog_uses_fixed_small_rotations(rig):
    manager, adapter = rig
    manager.jog(PAGE, "insert", "ccw")
    assert finish(manager)["status"] == "completed"
    assert adapter.values["insert"] == pytest.approx(0.1)
    assert len(adapter.moves) == 1


def test_busy_worker_rejects_other_operations(rig):
    manager, adapter = rig
    adapter.finish_move.clear()
    manager.start(PAGE, "fully_in")
    assert adapter.started.wait(1)
    with pytest.raises((ServiceError, ValueError)):
        manager.start_position(PAGE, "Pickup")
    with pytest.raises(ServiceError):
        manager.mutate(PAGE, "profile/new", {"name": "Other"})
    manager.stop()
    assert finish(manager)["status"] == "stopped"


def test_cancel_reports_only_active_matching_operation(rig):
    manager, adapter = rig
    assert manager.cancel(PAGE) is False
    adapter.finish_move.clear()
    manager.start(PAGE, "fully_in")
    assert adapter.started.wait(1)
    assert manager.cancel("another-page") is False
    assert manager.operation["status"] == "moving"
    assert manager.cancel(PAGE) is True
    assert manager.operation["cancelled"].is_set()
    assert manager.cancel(PAGE) is False
    assert finish(manager)["status"] == "stopped"
    assert manager.cancel() is False


def test_cancel_returns_false_after_completed_operation(rig):
    manager, adapter = rig
    manager.start(PAGE, "fully_in")
    assert finish(manager)["status"] == "completed"
    assert manager.cancel(PAGE) is False
    assert manager.state(PAGE)["operation"]["status"] == "completed"


def test_routes_reject_client_coordinates_and_accept_saved_target_ids(rig):
    manager, adapter = rig
    app = Flask(__name__)
    app.config["TESTING"] = True
    blueprint = Blueprint("transfer", __name__)
    register(blueprint, SimpleNamespace(sample_transfer=manager))
    app.register_blueprint(blueprint, url_prefix="/transfer")

    @app.errorhandler(ValueError)
    def invalid(error):
        return {"error": str(error)}, 400

    client = app.test_client()
    headers = {"X-MG400-Client": PAGE}
    for action, data in (
        ("record", {"name": "Injected"}),
        ("endpoint/record", {"target": "out_stage"}),
        ("stage/move", {"target": "out_stage"}),
        ("move", {"target": "fully_out"}),
    ):
        response = client.post(
            "/transfer/" + action, headers=headers, json={**data, "position_rotations": {"x": 5}}
        )
        assert response.status_code == 400
    recorded = client.post(
        "/transfer/endpoint/record", headers=headers, json={"target": "out_stage"}
    )
    assert recorded.status_code == 200
    assert recorded.json["draft"]["stage_positions"]["out_stage"]["position_rotations"] == {
        "x": 2,
        "y": 3,
    }
    assert (
        client.post(
            "/transfer/endpoint/delete", headers=headers, json={"target": "out_stage"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/transfer/stage/move", headers=headers, json={"target": "out_stage"}
        ).status_code
        == 202
    )
    assert finish(manager)["status"] == "completed"
    assert adapter.values["insert"] == 0
    assert (
        client.post("/transfer/position/move", headers=headers, json={"name": "Pickup"}).status_code
        == 202
    )
    assert finish(manager)["status"] == "completed"
    adapter.control = False
    assert client.post("/transfer/stop", headers=headers, json={}).status_code == 200
    assert client.get("/transfer/state", headers=headers).json["position"]["insert"] == 8


@pytest.fixture
def motion_client(rig, monkeypatch):
    manager, _ = rig
    app = Flask(__name__)
    app.config["TESTING"] = True
    blueprint = Blueprint("motion", __name__)
    motion.register(blueprint, SimpleNamespace(sample_transfer=manager))
    app.register_blueprint(blueprint, url_prefix="/motion")
    app.register_error_handler(ValueError, lambda error: ({"error": str(error)}, 400))
    app.register_error_handler(ServiceError, lambda error: ({"error": error.message}, error.status))
    monkeypatch.setattr(motion, "read", lambda *args: {"available": False})
    return app.test_client()


@pytest.mark.parametrize("target", [{"x": 6, "y": 5}, {"insert": 8}])
def test_motion_position_route_starts_coordinates_and_reports_status(motion_client, rig, target):
    manager, adapter = rig
    adapter.finish_move.clear()
    response = motion_client.post(
        "/motion/stage/position", headers={"X-MG400-Client": PAGE}, json={"target": target}
    )
    assert response.status_code == 202
    assert set(response.json) == {"operation"}
    assert response.json["operation"]["status"] == "moving"
    assert response.json["operation"]["target"] == target
    assert adapter.started.wait(1)
    state = motion_client.get("/motion/state", headers={"X-MG400-Client": PAGE}).json
    assert state["stage_operation"]["id"] == response.json["operation"]["id"]
    assert state["stage_operation"]["settling"] is True
    adapter.finish_move.set()
    assert manager.operation["done"].wait(3)
    state = motion_client.get("/motion/state", headers={"X-MG400-Client": PAGE}).json
    assert state["stage_operation"]["status"] == "completed"


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"x": 6, "y": 5},
        {"target": {"x": 6, "y": 5}, "speed": 1},
        *[
            {"target": target}
            for target in (
                None,
                [],
                {},
                {"x": 6},
                {"y": 5},
                {"x": 6, "y": 5, "insert": 8},
                {"insert": 8, "z": 1},
                {"x": True, "y": 5},
                {"x": 6, "y": False},
                {"insert": True},
                {"insert": "8"},
                {"insert": float("nan")},
                {"x": float("inf"), "y": 5},
                {"x": 6, "y": float("-inf")},
            )
        ],
    ],
)
def test_motion_position_rejects_invalid_payloads_without_commands(motion_client, rig, body):
    manager, adapter = rig
    response = motion_client.post(
        "/motion/stage/position",
        headers={"X-MG400-Client": PAGE},
        data=json.dumps(body),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert manager.operation is None
    assert adapter.moves == []


@pytest.mark.parametrize(
    "override",
    [
        {"upper_limit_rotations": 5},
        {"lower_limit_rotations": 7},
        {"positioning_ready": False},
        {"telemetry_fresh": False},
        {"encoder": "untrusted"},
        {"fault": "direction"},
        {"mode": "moving"},
    ],
)
def test_motion_position_rejects_unready_targets_before_any_axis(motion_client, rig, override):
    manager, adapter = rig
    adapter.overrides["top"] = override
    response = motion_client.post(
        "/motion/stage/position",
        headers={"X-MG400-Client": PAGE},
        json={"target": {"x": 6, "y": 5}},
    )
    assert response.status_code == 400
    assert manager.operation is None
    assert adapter.moves == []


@pytest.mark.parametrize("headers", [{}, {"X-MG400-Client": "other-page"}])
def test_motion_position_requires_page_identity_and_lease(motion_client, rig, headers):
    manager, adapter = rig
    response = motion_client.post(
        "/motion/stage/position", headers=headers, json={"target": {"insert": 8}}
    )
    assert response.status_code == (409 if headers else 400)
    assert manager.operation is None
    assert adapter.moves == []


def test_live_adapter_uses_read_only_state_and_existing_guarded_commands():
    calls = []
    console = SimpleNamespace(
        _owned=lambda device, page: None,
        read=lambda *args: calls.append(args) or {"connected": False},
        command=lambda *args: calls.append(args) or {"outcome": "accepted"},
        stop=lambda *args: calls.append(args) or {"outcome": "confirmed"},
    )
    adapter = LiveTransferAdapter(SimpleNamespace(console=console))
    assert adapter.stage_state(PAGE) == {"connected": False}
    adapter.position(PAGE, motor="top", position=0.1, speed=0.25, stop_epoch=2)
    assert calls[0] == ("stage", "api/state")
    assert calls[1][2] == "move_to_position_guarded"
    assert not adapter.owns(PAGE)
    assert not adapter.owns_current(PAGE)


def test_live_cleanup_ownership_checks_the_device_and_same_console_session():
    remote = {"held": True}
    item = SimpleNamespace(
        adapter=SimpleNamespace(
            state=lambda: dict(remote),
            owned=lambda state: state["held"],
        )
    )
    console = SimpleNamespace(_owned=lambda device, page: item)
    adapter = LiveTransferAdapter(SimpleNamespace(console=console))
    assert adapter.owns_current(PAGE)
    remote["held"] = False
    assert adapter.owns(PAGE)
    assert not adapter.owns_current(PAGE)
    remote["held"] = True

    def changed_session():
        console._owned = lambda device, page: None
        return dict(remote)

    item.adapter.state = changed_session
    assert not adapter.owns_current(PAGE)
