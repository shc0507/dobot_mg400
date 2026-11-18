"""Optical Insert positioning without hardware or network access."""

import copy
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask import Blueprint, Flask

from labkit.errors import ServiceError
from orchestrator.transfer_motion import PulseScale, move_seconds, movement
from orchestrator.transfer_profiles import TransferProfileStore, new_profile, record_position
from orchestrator.web.backend import Dashboard
from orchestrator.web.console import Console, Session
from orchestrator.web.sample_transfer import SampleTransfer
from orchestrator.web.workspaces.sample_transfer import register

PAGE = "sample-transfer-test"
SOURCE = {
    "camera": "camera1",
    "device_id": "test-camera",
    "profile_id": str(uuid4()),
    "name": "Optical stage calibration",
}


def report(position):
    return {
        "axes": {
            axis: {"value_mm": value, "status": "provisional_interpolation"}
            for axis, value in {"x": 2, "y": 3, "insert": position}.items()
        },
        "camera": SOURCE["camera"],
        "profile_id": SOURCE["profile_id"],
        "received_at": "2026-11-12T12:00:00+00:00",
        "image_sha256": "a" * 64,
        "sample_requested_at": time.monotonic(),
    }


class Adapter:
    def __init__(self):
        self.control = True
        self.epoch = 1
        self.position = 4.0
        self.gain = 1 / 640
        self.microstep = 2
        self.preparations = []
        self.port = "test-stage"
        self.moves = []
        self.sample_sources = []
        self.stops = 0
        self.last = None
        self.sample_started = threading.Event()
        self.resume_sample = threading.Event()
        self.resume_sample.set()
        self.before_move = None
        self.invalid = False
        self.reference = {
            "source": copy.deepcopy(SOURCE),
            "targets_mm": {"fully_out": 0.0, "fully_in": 10.0},
            "ready": True,
            "message": "",
        }

    def owns(self, page):
        return self.control and page == PAGE

    def stage_state(self, page):
        return {
            "connected": True,
            "feedback_fresh": True,
            "locked": False,
            "busy": False,
            "stop_epoch": self.epoch,
            "microstep": self.microstep,
            "port": self.port,
            "last_command": self.last,
            "motors": [
                {"id": motor, "mode": "idle", "cooldown": 0, "available": True, "acceleration": 50}
                for motor in ("top", "bottom", "insert")
            ],
        }

    def transfer_reference(self, page):
        return copy.deepcopy(self.reference)

    def sample(self, page, source=None):
        self.sample_sources.append(copy.deepcopy(source))
        self.sample_started.set()
        assert self.resume_sample.wait(3)
        measured = report(self.position)
        if self.invalid:
            measured["axes"]["insert"] = {"status": "unknown", "value_mm": None}
        return copy.deepcopy(SOURCE), measured

    def move(self, page, **params):
        if self.before_move is not None:
            self.before_move()
        if params["stop_epoch"] != self.epoch:
            return {"outcome": "refused", "error": "Stage motion was cancelled."}
        self.moves.append(params)
        self.position += params["steps"] * self.gain * (1 if params["direction"] == "ccw" else -1)
        identifier = str(uuid4())
        self.last = {"id": identifier, "result": "ok"}
        return {"outcome": "accepted", "command_id": identifier}

    def prepare(self, page, stop_epoch):
        if stop_epoch != self.epoch:
            return {"outcome": "refused", "error": "Stage motion was cancelled."}
        self.microstep = 8
        self.preparations.append(stop_epoch)
        identifier = str(uuid4())
        self.last = {"id": identifier, "result": "ok"}
        return {"outcome": "accepted", "command_id": identifier}

    def stop(self):
        self.epoch += 1
        self.stops += 1
        return {"outcome": "confirmed"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.web.sample_transfer.SETTLE_SECONDS", 0)
    monkeypatch.setattr("orchestrator.web.sample_transfer.SAMPLE_INTERVAL", 0)
    store, adapter = TransferProfileStore(tmp_path), Adapter()
    profile = new_profile("Transfer endpoints")
    profile = record_position(profile, "fully_out", SOURCE, report(0))
    profile = record_position(profile, "fully_in", SOURCE, report(10))
    saved = store.save(profile)
    store.select(saved["id"])
    manager = SampleTransfer(store, adapter, demo=True)
    yield manager, adapter
    manager.cancel()
    adapter.resume_sample.set()
    if manager.operation is not None:
        assert manager.operation["done"].wait(3)


def finish(manager):
    assert manager.operation["done"].wait(3)
    return manager.state(PAGE)["operation"]


@pytest.mark.parametrize(
    ("target", "direction", "position"), [("fully_out", "cw", 0), ("fully_in", "ccw", 10)]
)
def test_transfer_moves_only_insert_and_uses_fresh_optical_feedback(
    rig, target, direction, position
):
    manager, adapter = rig
    manager.start(PAGE, target)
    assert finish(manager)["status"] == "completed"
    assert abs(adapter.position - position) <= 0.5
    assert {move["motor"] for move in adapter.moves} == {"insert"}
    assert {move["direction"] for move in adapter.moves} == {direction}
    assert adapter.preparations == [1]
    assert adapter.microstep == 8
    assert adapter.moves[0]["steps"] == 1280
    assert all(move["steps"] <= 8000 and move["speed"] <= 1000 for move in adapter.moves)


def test_stop_during_camera_read_returns_without_waiting_and_never_moves(rig):
    manager, adapter = rig
    adapter.resume_sample.clear()
    manager.start(PAGE, "fully_in")
    assert adapter.sample_started.wait(1)
    assert manager.stop() == {"outcome": "confirmed"}
    assert manager.state(PAGE)["operation"]["status"] == "stopped"
    adapter.resume_sample.set()
    assert finish(manager)["status"] == "stopped"
    assert adapter.moves == []


def test_direct_stage_stop_during_measurement_invalidates_continuation(rig):
    manager, adapter = rig
    adapter.resume_sample.clear()
    manager.start(PAGE, "fully_in")
    assert adapter.sample_started.wait(1)
    adapter.stop()
    adapter.resume_sample.set()
    assert finish(manager)["status"] == "stopped"
    assert adapter.moves == []


@pytest.mark.parametrize("action", ["transfer", "jog"])
def test_direct_stage_stop_before_worker_entry_cancels_the_accepted_motion(rig, action):
    manager, adapter = rig
    entered, resume = threading.Event(), threading.Event()
    run = manager._run

    def delayed(*args, **kwargs):
        entered.set()
        assert resume.wait(3)
        return run(*args, **kwargs)

    manager._run = delayed
    try:
        if action == "transfer":
            manager.start(PAGE, "fully_in")
        else:
            manager.jog(PAGE, "insert", "ccw")
        assert entered.wait(1)
        adapter.stop()
    finally:
        resume.set()
    assert finish(manager)["status"] == "stopped"
    assert adapter.moves == []


def test_atomic_guard_rejects_stop_between_measurement_and_move(rig):
    manager, adapter = rig
    adapter.before_move = adapter.stop
    manager.start(PAGE, "fully_in")
    assert finish(manager)["status"] == "stopped"
    assert adapter.moves == []


def test_lost_control_during_measurement_never_reacquires(rig):
    manager, adapter = rig
    adapter.resume_sample.clear()
    manager.start(PAGE, "fully_in")
    assert adapter.sample_started.wait(1)
    adapter.control = False
    adapter.resume_sample.set()
    assert finish(manager)["status"] == "failed"
    assert adapter.moves == []
    assert adapter.stops == 1


def test_no_progress_stops_after_a_measurable_verification_move(rig):
    manager, adapter = rig
    adapter.gain = 0
    manager.start(PAGE, "fully_in")
    outcome = finish(manager)
    assert outcome["status"] == "failed"
    assert "pulse conversion" in outcome["message"]
    assert len(adapter.moves) == 1
    assert adapter.stops == 1


@pytest.mark.parametrize("invalid", [True, False])
def test_unknown_position_or_wrong_direction_stops(rig, invalid):
    manager, adapter = rig
    adapter.invalid, adapter.gain = invalid, -0.1
    manager.start(PAGE, "fully_in")
    assert finish(manager)["status"] == "failed"
    assert len(adapter.moves) == (0 if invalid else 1)
    assert adapter.stops == 1


def test_operation_pins_finder_reference_and_ignores_named_profiles_and_draft(rig):
    manager, adapter = rig
    adapter.resume_sample.clear()
    selected = manager.store.selected()
    manager.start(PAGE, "fully_in")
    assert adapter.sample_started.wait(1)
    assert manager.state(PAGE)["operation"]["target_mm"] == 10
    adapter.reference["targets_mm"]["fully_in"] = 50
    adapter.reference["source"]["profile_id"] = str(uuid4())
    selected["positions"]["fully_in"]["position_mm"]["insert"] = 30
    changed = manager.store.save(selected)
    manager.store.select(changed["id"])
    manager.mutate(PAGE, "profile/new", {"name": "An unsaved draft"})
    adapter.resume_sample.set()
    assert finish(manager)["status"] == "completed"
    assert abs(adapter.position - 10) <= 0.5
    assert all(source == SOURCE for source in adapter.sample_sources)
    state = manager.state(PAGE)
    assert state["active_profile"]["id"] == changed["id"]
    assert state["draft"]["id"] != changed["id"]


def test_record_reads_server_position_and_load_does_not_select(rig):
    manager, adapter = rig
    result = manager.mutate(PAGE, "record", {"name": "fully_out"})
    assert result["draft"]["positions"]["fully_out"]["position_mm"]["insert"] == 4
    result = manager.mutate(PAGE, "profile/save", {"name": "Partial"})
    partial = result["draft"]
    original = partial["id"]
    manager.mutate(PAGE, "profile/new", {"name": "Other"})
    loaded = manager.mutate(PAGE, "profile/load", {"id": partial["id"]})
    assert loaded["draft"] == partial
    assert manager.store.selection() == original
    uploaded = manager.mutate(
        PAGE, "profile/upload", {"profile": json.dumps(manager.store.export(partial["id"]))}
    )
    assert uploaded["draft"]["id"] != partial["id"]
    assert uploaded["draft"]["positions"] == partial["positions"]
    assert manager.store.selection() == original


def test_save_selects_positions_profile_and_empty_save_preserves_selection(rig):
    manager, adapter = rig
    original = manager.store.selection()
    manager.mutate(PAGE, "profile/new", {"name": "Named positions"})
    empty = manager.mutate(PAGE, "profile/save", {"name": "Empty draft"})
    assert empty["selected_profile"] == original
    manager.mutate(PAGE, "record", {"name": "Pickup"})
    saved = manager.mutate(PAGE, "profile/save", {"name": "Named positions"})
    assert saved["selected_profile"] == saved["draft"]["id"]
    assert saved["active_profile"] == saved["draft"]
    assert saved["profiles"][0]["position_count"] == 1


def test_select_loads_complete_profile_for_editing_without_changing_saved_snapshot(rig):
    manager, adapter = rig
    original = manager.store.selected()
    manager.mutate(PAGE, "profile/new", {"name": "Unrelated draft"})
    selected = manager.mutate(PAGE, "profile/select", {"id": original["id"]})
    assert selected["draft"] == original
    adapter.position = 1
    manager.mutate(PAGE, "record", {"name": "fully_out"})
    edited = manager.mutate(PAGE, "profile/save", {"name": "Edited endpoints"})
    assert edited["draft"]["id"] != original["id"]
    assert edited["draft"]["positions"]["fully_out"]["position_mm"]["insert"] == 1
    assert edited["selected_profile"] == edited["draft"]["id"]
    assert manager.store.load(original["id"]) == original
    assert edited["draft"]["source"] == original["source"]


def test_operation_reports_page_ownership_and_rejects_second_operation(rig):
    manager, adapter = rig
    adapter.resume_sample.clear()
    manager.start(PAGE, "fully_in")
    assert adapter.sample_started.wait(1)
    assert manager.state(PAGE)["operation"]["mine"] is True
    assert manager.state("another-page")["operation"]["mine"] is False
    with pytest.raises(ServiceError, match="previous operation"):
        manager.start(PAGE, "fully_out")


def test_jog_is_bounded_and_requires_existing_control(rig):
    manager, adapter = rig
    adapter.control = False
    with pytest.raises(ServiceError, match="Take stage"):
        manager.jog(PAGE, "insert", "cw")
    adapter.control = True
    manager.measure(PAGE)
    assert manager.state(PAGE)["position"]["insert"] == 4
    manager.jog(PAGE, "insert", "ccw")
    assert finish(manager)["status"] == "completed"
    assert len(adapter.moves) == 1
    assert adapter.moves[0]["steps"] == 10
    assert manager.state(PAGE)["position"] == {"x": 2, "y": 3, "insert": 4 + 10 / 640}


def test_jog_reports_failed_measurement_and_stops_after_the_bounded_move(rig):
    manager, adapter = rig
    adapter.invalid = True
    manager.jog(PAGE, "insert", "ccw")
    assert finish(manager)["status"] == "failed"
    assert len(adapter.moves) == 1
    assert adapter.stops == 1
    assert manager.state(PAGE)["position"] == {"x": None, "y": None, "insert": None}


def test_routes_reject_client_coordinates_and_stop_needs_no_control(rig):
    manager, adapter = rig
    app = Flask(__name__)
    blueprint = Blueprint("sample_transfer", __name__, url_prefix="/transfer")
    register(blueprint, SimpleNamespace(sample_transfer=manager))
    app.register_blueprint(blueprint)
    app.register_error_handler(ValueError, lambda exc: ({"error": str(exc)}, 400))
    web = app.test_client()
    headers = {"X-MG400-Client": PAGE}
    response = web.post(
        "/transfer/record", json={"name": "fully_out", "insert": 12}, headers=headers
    )
    assert response.status_code == 400
    adapter.control = False
    response = web.post("/transfer/stop", json={}, headers=headers)
    assert response.status_code == 200
    assert response.json["stop"]["outcome"] == "confirmed"
    assert adapter.stops == 1


@pytest.mark.parametrize("action", ["stop", "release", "all"])
def test_existing_console_stop_and_release_paths_cancel_transfer(rig, action):
    manager, adapter = rig
    adapter.resume_sample.clear()
    manager.start(PAGE, "fully_in")
    assert adapter.sample_started.wait(1)
    calls = []
    device = SimpleNamespace(
        stop=lambda: calls.append("stop") or {"stop": "confirmed"},
        release=lambda: calls.append("release"),
        close=lambda: None,
    )
    service = SimpleNamespace(
        sample_transfer=manager,
        settings={},
        events=SimpleNamespace(add=lambda *args, **kwargs: None),
        demo=True,
        started=True,
        closing=False,
    )
    console = Console(service)
    console._client = lambda *args: (None, device)
    item = Session("stage", PAGE, "owned-identity", device, {}, 0)
    console.sessions["stage"] = item
    if action == "stop":
        console.stop("stage")
        assert calls == ["stop"]
    elif action == "release":
        console.release("stage", PAGE)
        assert calls == ["stop", "release"]
    else:
        dashboard = Dashboard(service)
        operation, created = dashboard.reserve("motion")
        assert created is True
        operation["done"].set()
    adapter.resume_sample.set()
    assert finish(manager)["status"] == "stopped"
    assert adapter.moves == []


def test_transfer_needs_no_sample_transfer_profile(rig):
    manager, adapter = rig
    manager.store.delete(manager.store.selection())
    assert manager.state(PAGE)["selected_profile"] is None
    manager.start(PAGE, "fully_in")
    assert finish(manager)["status"] == "completed"
    assert abs(adapter.position - 10) <= 0.5


def test_uncalibrated_finder_disables_transfer_even_with_named_profile(rig):
    manager, adapter = rig
    adapter.reference.update(ready=False, message="Calibrate Insert first.")
    assert not manager.state(PAGE)["transfer_reference"]["ready"]
    with pytest.raises(ValueError, match="Calibrate Insert"):
        manager.start(PAGE, "fully_in")
    assert adapter.moves == []


def test_record_delete_and_save_named_positions_do_not_change_transfer_targets(rig):
    manager, adapter = rig
    manager.mutate(PAGE, "record", {"name": "Loading"})
    before = manager.state(PAGE)["transfer_reference"]
    state = manager.mutate(PAGE, "profile/save", {"name": "Test positions"})
    saved_id = state["draft"]["id"]
    state = manager.mutate(PAGE, "position/delete", {"name": "Loading"})
    assert state["draft"]["positions"] == {}
    assert state["transfer_reference"] == before
    assert "Loading" in manager.store.load(saved_id)["positions"]


@pytest.mark.parametrize(
    ("target", "measured", "expected"),
    [
        ("fully_out", -0.02, "completed"),
        ("fully_in", 10.02, "completed"),
        ("fully_out", -0.51, "failed"),
        ("fully_in", 10.51, "failed"),
    ],
)
def test_endpoint_noise_uses_only_motion_tolerance(rig, target, measured, expected):
    manager, adapter = rig
    adapter.position = measured
    original = adapter.sample

    def sample(*args):
        source, measurement = original(*args)
        measurement["axes"]["insert"]["status"] = "extrapolated"
        return source, measurement

    adapter.sample = sample
    manager.start(PAGE, target)
    assert finish(manager)["status"] == expected
    assert adapter.moves == []
    with pytest.raises(ValueError, match="calibrated travel"):
        manager.mutate(PAGE, "record", {"name": "Outside"})


@pytest.fixture
def finder_adapter(monkeypatch):
    from orchestrator.web import twin
    from orchestrator.web.sample_transfer import LiveTransferAdapter

    saved = {
        "id": SOURCE["profile_id"],
        "name": SOURCE["name"],
        "camera": {"device_id": SOURCE["device_id"]},
        "axes": {"insert": {"min": 0.0, "max": 150.0}},
        "axis_status": {"insert": {"status": "calibrated"}},
        "endpoints": {"insert_out": {}, "insert_in": {}},
    }
    current = SimpleNamespace(
        selected=saved["id"],
        camera="camera1",
        lock=threading.RLock(),
        profile={"travel_mm": {"insert": 999}},
        active_profile={"travel_mm": {"insert": 888}},
    )
    loads = []

    def load(identifier):
        loads.append(identifier)
        assert identifier == saved["id"]
        return copy.deepcopy(saved)

    manager = SimpleNamespace(
        page=lambda client: current,
        sync_selection=lambda page: None,
        store=SimpleNamespace(load=load),
        frames=SimpleNamespace(identity=lambda camera: SOURCE["device_id"]),
    )
    monkeypatch.setattr(twin, "get_twin_console", lambda service: manager)
    return LiveTransferAdapter(None), manager, current, saved, loads


def test_live_reference_loads_saved_finder_endpoints_not_editing_draft(finder_adapter):
    adapter, _, current, saved, loads = finder_adapter
    reference = adapter.transfer_reference(PAGE)
    assert reference["targets_mm"] == {"fully_out": 0, "fully_in": 150}
    assert reference["source"] == SOURCE
    assert loads == [saved["id"]]
    current.selected = None
    with pytest.raises(ValueError, match="Select a saved"):
        adapter.transfer_reference(PAGE)


@pytest.mark.parametrize("invalid", ["axis", "endpoints", "status", "camera", "range"])
def test_live_reference_rejects_incomplete_or_mismatched_finder(finder_adapter, invalid):
    adapter, _, _, saved, _ = finder_adapter
    if invalid == "axis":
        saved["axes"] = {}
    elif invalid == "endpoints":
        saved["endpoints"].pop("insert_in")
    elif invalid == "status":
        saved["axis_status"]["insert"]["status"] = "pending"
    elif invalid == "camera":
        saved["camera"]["device_id"] = "different camera"
    else:
        saved["axes"]["insert"]["max"] = float("inf")
    with pytest.raises(ValueError):
        adapter.transfer_reference(PAGE)


@pytest.mark.parametrize("all_missing", [False, True])
def test_passive_measure_preserves_partial_coordinates_without_enabling_motion(rig, all_missing):
    manager, adapter = rig

    def sample(page, source=None):
        measured = report(151.36)
        measured["axes"]["x"]["value_mm"] = 19.97
        measured["axes"]["y"] = {"status": "unknown", "value_mm": None}
        measured["axes"]["insert"]["status"] = "extrapolated"
        if all_missing:
            measured["axes"] = {}
        return copy.deepcopy(SOURCE), measured

    adapter.sample = sample
    state = manager.measure(PAGE)
    assert state["operation"]["status"] == "completed"
    assert state["position_source"] == SOURCE
    assert "Y unavailable" in state["operation"]["message"]
    if all_missing:
        assert state["position"] == {"x": None, "y": None, "insert": None}
        assert "X unavailable" in state["operation"]["message"]
        assert "Insert unavailable" in state["operation"]["message"]
    else:
        assert state["position"] == {"x": 19.97, "y": None, "insert": 151.36}
        assert "Insert outside calibrated travel" in state["operation"]["message"]
    assert adapter.moves == []
    assert adapter.stops == 0
    with pytest.raises(ValueError, match="calibrated travel"):
        manager.mutate(PAGE, "record", {"name": "Unavailable position"})
    manager.start(PAGE, "fully_in")
    assert finish(manager)["status"] == "failed"
    assert adapter.moves == []


def test_full_stroke_uses_geometry_and_learns_persisted_direction_scale(rig):
    manager, adapter = rig
    adapter.position = 0
    adapter.reference["targets_mm"]["fully_in"] = 150
    adapter.gain = 1 / 660
    manager.start(PAGE, "fully_in")
    outcome = finish(manager)
    assert outcome["status"] == "completed", outcome
    assert abs(adapter.position - 150) <= 0.5
    assert sum(item["steps"] for item in adapter.moves) > 95000
    assert max(item["steps"] for item in adapter.moves) > 5000
    assert adapter.moves[0]["speed"] < max(item["speed"] for item in adapter.moves)
    assert adapter.moves[-1]["speed"] == 200
    key = manager.scales.key(SOURCE, adapter.port, "ccw")
    assert manager.scales.get(key) == pytest.approx(660)
    restarted = SampleTransfer(manager.store, adapter, demo=True)
    assert restarted.scales.get(key) == pytest.approx(660)
    assert restarted.scales.get(manager.scales.key(SOURCE, "other-stage", "ccw")) is None
    assert restarted.scales.get(manager.scales.key(SOURCE, adapter.port, "cw")) is None
    changed = {**SOURCE, "profile_id": str(uuid4())}
    assert restarted.scales.get(manager.scales.key(changed, adapter.port, "ccw")) is None


def test_acceleration_controls_completion_deadline_and_batch_distance():
    # A 4000-pulse move at 50 pulses/s² is triangular, despite a 1000 pulses/s cap.
    assert move_seconds(4000, 1000, 50) == pytest.approx(2 * (4000 / 50) ** 0.5 + 5)
    assert move_seconds(10000, 500, 50) == pytest.approx(35)
    scale = PulseScale()
    assert movement(150, scale) == (1280, 400)
    scale.observe(1280, 2)
    assert movement(150, scale) == (6400, 1000)
    assert movement(1, scale) == (416, 200)


def test_bad_pulse_scale_is_not_reused_or_persisted(rig):
    manager, adapter = rig
    key = manager.scales.key(SOURCE, adapter.port, "ccw")
    manager.scales.path.write_text('{"schema_version":1,"scales":{"' + key + '":99999}}')
    assert manager.scales.get(key) is None
    with pytest.raises(ValueError, match="pulse scale"):
        manager.scales.save(key, 99999)
    adapter.gain = 1 / 320
    manager.start(PAGE, "fully_in")
    assert finish(manager)["status"] == "failed"
    assert len(adapter.moves) == 1
    assert manager.scales.get(key) is None


def test_stop_during_mode_preparation_never_refreshes_authority(rig):
    manager, adapter = rig
    prepare = adapter.prepare

    def interrupted(page, epoch):
        result = prepare(page, epoch)
        adapter.stop()
        return result

    adapter.prepare = interrupted
    manager.start(PAGE, "fully_in")
    assert finish(manager)["status"] == "stopped"
    assert adapter.preparations == [1]
    assert adapter.moves == []


def test_external_microstep_change_cancels_before_next_batch(rig):
    manager, adapter = rig
    sample = adapter.sample

    def changed(*args):
        result = sample(*args)
        if adapter.moves:
            adapter.microstep = 4
        return result

    adapter.sample = changed
    manager.start(PAGE, "fully_in")
    outcome = finish(manager)
    assert outcome["status"] == "failed"
    assert "stepping mode changed" in outcome["message"]
    assert len(adapter.moves) == 1


def test_configured_soft_limits_must_be_ready_after_microstep_preparation(rig):
    manager, adapter = rig
    state = adapter.stage_state

    def configured(page):
        value = state(page)
        value["automation"] = {"configured": True}
        value["motors"][2]["automation_ready"] = False
        return value

    adapter.stage_state = configured
    manager.start(PAGE, "fully_in")
    outcome = finish(manager)
    assert outcome["status"] == "failed"
    assert "soft limits" in outcome["message"]
    assert adapter.moves == []


def test_cached_scale_still_requires_initial_geometry_verification(rig):
    manager, adapter = rig
    key = manager.scales.key(SOURCE, adapter.port, "ccw")
    manager.scales.save(key, 700)
    manager.start(PAGE, "fully_in")
    outcome = finish(manager)
    assert outcome["status"] == "completed"
    assert adapter.moves[0]["steps"] == 1280
    assert adapter.moves[0]["speed"] == 400


def test_stop_on_final_confirmation_does_not_report_completed(rig):
    manager, adapter = rig
    adapter.position = 10
    sample = adapter.sample
    count = 0

    def stopped(*args):
        nonlocal count
        count += 1
        result = sample(*args)
        if count == 6:
            adapter.stop()
        return result

    adapter.sample = stopped
    manager.start(PAGE, "fully_in")
    assert finish(manager)["status"] == "stopped"
    assert adapter.moves == []


def test_stale_camera_request_cannot_drive_motion(rig):
    manager, adapter = rig
    sample = adapter.sample

    def stale(*args):
        source, value = sample(*args)
        value["sample_requested_at"] = 0
        return source, value

    adapter.sample = stale
    manager.start(PAGE, "fully_in")
    outcome = finish(manager)
    assert outcome["status"] == "failed"
    assert "predates" in outcome["message"]
    assert adapter.moves == []


def test_missing_frame_is_retried_at_rest_and_endpoint_needs_consistent_reads(rig):
    manager, adapter = rig
    sample = adapter.sample
    samples, final = [], []

    def intermittent(*args):
        source, value = sample(*args)
        samples.append(adapter.position)
        if len(samples) == 1:
            value["axes"]["insert"] = {"status": "unknown", "value_mm": None}
        elif abs(adapter.position - 10) <= 0.5:
            final.append(adapter.position)
        return source, value

    adapter.sample = intermittent
    manager.start(PAGE, "fully_in")
    assert finish(manager)["status"] == "completed"
    assert len(final) >= 3
    assert samples[:4] == [4, 4, 4, 4]


def test_unstable_camera_does_not_move_or_store_scale(rig):
    manager, adapter = rig
    sample = adapter.sample
    count = 0

    def noisy(*args):
        nonlocal count
        count += 1
        source, value = sample(*args)
        value["axes"]["insert"]["value_mm"] += 1 if count % 2 else -1
        return source, value

    adapter.sample = noisy
    manager.start(PAGE, "fully_in")
    outcome = finish(manager)
    assert outcome["status"] == "failed"
    assert "not stable" in outcome["message"]
    assert adapter.moves == []
    assert not manager.scales.path.exists()
