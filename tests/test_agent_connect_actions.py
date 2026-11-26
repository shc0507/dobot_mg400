"""Reviewed actions use shared handlers without physical equipment access."""

import copy
import json
import socket
import threading
from types import SimpleNamespace

import pytest

from labkit.errors import ServiceError
from orchestrator.agent_connect.action_backend import create_backend
from orchestrator.agent_connect.actions import SPECS
from orchestrator.agent_connect.mock import MockConsole
from orchestrator.transfer_profiles import new_profile, record_position

PAGE = "agent-actions-test"


class Context:
    def __init__(self):
        self.page = PAGE
        self.cancelled = threading.Event()
        self.dispatches = []
        self.updates = []
        self.check = None

    def checkpoint(self):
        if self.cancelled.is_set():
            raise ServiceError(409, "The command was cancelled.")
        if self.check:
            self.check()

    def dispatch(self, function, *args, **kwargs):
        self.checkpoint()
        self.dispatches.append((function, args, kwargs))
        return function(*args, **kwargs)

    def progress(self, **fields):
        self.updates.append(fields)


class StageConsole:
    """Deterministic telemetry for shared SampleTransfer tests."""

    def __init__(self):
        self.holder = None
        self.takes = 0
        self.moves = []
        self.stops = 0
        self.after_move = None
        self.outcome = "accepted"
        self.session = SimpleNamespace(
            adapter=SimpleNamespace(state=lambda: {}, owned=lambda state: True)
        )
        self.state = {
            "demo": True,
            "connected": True,
            "reference_revision": 3,
            "stop_epoch": 1,
            "port": "simulated-test-stage",
            "microstep": 8,
            "last_command": None,
            "motors": [
                {
                    "id": motor,
                    "position_rotations": 0.0,
                    "mode": "idle",
                    "calibration": "done",
                    "encoder": "ok",
                    "fault": "none",
                    "telemetry_fresh": True,
                    "available": True,
                    "positioning_ready": True,
                    "lower_limit_rotations": 0.0,
                    "upper_limit_rotations": 10.0,
                    "tolerance": 0.001,
                    "cooldown": 0,
                    "max_speed": 0.25,
                }
                for motor in ("bottom", "top", "insert")
            ],
        }

    def read(self, device, path):
        assert device == "stage"
        if path == "api/describe":
            return {
                "demo": True,
                "commands": {"clear_fault": {"params": SPECS["stage.clear_fault"]["params"]}},
            }
        return copy.deepcopy(self.state)

    def _owned(self, device, page):
        return self.session if self.holder == page else None

    def take(self, device, page):
        if self.holder not in (None, page):
            raise ServiceError(409, "Another page controls the stage.")
        self.takes += 1
        if self.holder is None:
            self.state["stop_epoch"] += 1
        self.holder = page

    def command(self, device, page, action, params):
        assert self.holder == page
        assert action in {"move_to_position_guarded", "move_guarded"}
        assert params["stop_epoch"] == self.state["stop_epoch"]
        self.moves.append(copy.deepcopy(params))
        if self.outcome != "accepted":
            return {"outcome": self.outcome}
        axis = next(row for row in self.state["motors"] if row["id"] == params["motor"])
        axis["position_rotations"] = params.get(
            "position",
            axis["position_rotations"]
            + params.get("rotations", 0) * (1 if params.get("direction") == "ccw" else -1),
        )
        identifier = f"move-{len(self.moves)}"
        self.state["last_command"] = {"id": identifier, "result": "ok"}
        if self.after_move:
            self.after_move()
        return {"outcome": "accepted", "command_id": identifier}

    def stop(self, device):
        self.stops += 1
        self.state["stop_epoch"] += 1
        return {"outcome": "confirmed"}


@pytest.fixture
def rig(tmp_path):
    console = StageConsole()
    service = SimpleNamespace(
        demo=True,
        temporary=SimpleNamespace(name=str(tmp_path)),
        console=console,
        dashboard_configured=lambda device: device == "stage",
    )
    backend = create_backend(service)
    yield backend, console
    backend.close()


def run(backend, action, params=None, context=None):
    context = context or Context()
    plan = backend.prepare(action, params or {}, {}, context.page)
    return backend.execute(plan, context)


def named_profile(backend):
    source = {
        "kind": "stage_encoder",
        "device_id": "simulated-test-stage",
        "name": "Stage encoders",
    }
    report = {
        "source": source,
        "received_at": "2026-11-22T12:00:00+00:00",
        "axes": {
            axis: {"value_rotations": value, "ready": True}
            for axis, value in {"x": 4, "y": 3, "insert": 2}.items()
        },
    }
    profile = record_position(new_profile("Simulation profile"), "Pickup", source, report)
    saved = backend.transfer.store.save(profile)
    backend.transfer.store.select(saved["id"])
    return saved


def test_physical_policy_blocks_every_action_and_request_override():
    class Forbidden:
        def __getattr__(self, name):
            pytest.fail("Physical mode touched a device adapter.")

    backend = create_backend(SimpleNamespace(demo=False, console=Forbidden()), mock=True)
    assert backend.observations() == {}
    assert backend.choices(PAGE)["available"] is False
    assert backend.stop("stage")["outcome"] == "disabled"
    assert backend.release_page(PAGE)["outcome"] == "disabled"
    assert backend.cancel({"action": "stage.coordinates"}, PAGE)["outcome"] == "unavailable"
    for action in backend.catalog():
        assert action["available"] is False
        with pytest.raises(ValueError):
            backend.prepare(action["id"], {"demo": True, "enabled": True}, {}, PAGE)
        with pytest.raises(ValueError):
            backend.execute({"action": action["id"], "params": {}, "simulated": True}, Context())


def test_cleanup_stays_disabled_if_simulated_service_becomes_physical(rig):
    backend, console = rig
    console.holder = PAGE
    backend.service.demo = False
    assert backend.cancel({"action": "stage.coordinates"}, PAGE)["outcome"] == "unavailable"
    assert backend.stop("stage")["outcome"] == "disabled"
    assert backend.release_page(PAGE)["outcome"] == "disabled"
    assert console.stops == 0
    assert console.holder == PAGE


def test_stop_bypasses_discovery_and_keeps_unknown_results(rig, monkeypatch):
    backend, console = rig
    monkeypatch.setattr(console, "read", lambda *_: pytest.fail("Stop requested discovery"))
    assert backend.stop("stage")["outcome"] == "confirmed"
    assert console.stops == 1
    assert backend.stop("robot")["outcome"] == "unavailable"
    monkeypatch.setattr(console, "stop", lambda *_: (_ for _ in ()).throw(TimeoutError()))
    assert backend.stop("stage")["outcome"] == "unknown"
    assert backend.stop("furnace")["outcome"] == "disabled"


def test_release_uses_only_the_old_page_identity(rig, monkeypatch):
    backend, console = rig
    pages = []
    monkeypatch.setattr(
        console,
        "release_all",
        lambda page: pages.append(page) or {"stage": "unconfirmed"},
        raising=False,
    )
    result = backend.release_page(PAGE)
    assert pages == [PAGE]
    assert result["outcome"] == "unknown"
    assert console.takes == 0 and console.stops == 0
    with pytest.raises(ValueError):
        backend.release_page(None)


def test_choices_read_saved_profiles_and_names_without_creating_a_draft(rig):
    backend, console = rig
    saved = named_profile(backend)
    assert backend.transfer.pages == {}
    choices = backend.choices(PAGE)
    assert choices["available"]
    assert choices["actions"]["transfer.profile.select"]["id"] == [
        {"value": saved["id"], "label": "Simulation profile"}
    ]
    assert choices["actions"]["transfer.position"]["name"] == [
        {"value": "Pickup", "label": "Pickup"}
    ]
    assert choices["actions"]["transfer.position.delete"]["name"] == []
    assert backend.transfer.pages == {}
    assert console.takes == 0 and console.moves == [] and console.stops == 0
    backend.transfer.pages[PAGE] = {"draft": saved}
    assert backend.choices(PAGE)["actions"]["transfer.position.delete"]["name"] == [
        {"value": "Pickup", "label": "Pickup"}
    ]


@pytest.mark.parametrize(
    "action",
    [
        "furnace.operation",
        "recipe.run",
        "composite.run",
        "microscope.record_with_sequence",
        "furnace.set_gas_flow",
        "dispatch",
    ],
)
def test_furnace_and_unreviewed_dispatch_are_always_blocked(rig, action):
    backend, console = rig
    with pytest.raises(ValueError):
        backend.prepare(action, {"action": "set_gas_flow", "device": "furnace"}, {}, PAGE)
    assert not console.moves and not console.takes


def test_prepare_is_read_only_and_metadata_is_stable(rig):
    backend, console = rig
    before = copy.deepcopy(console.state)
    profiles = backend.transfer.store.list()
    catalog = backend.catalog()
    plan = backend.prepare("stage.coordinates", {"target": {"x": 1, "y": 2}}, {}, PAGE)
    json.dumps(plan, allow_nan=False)
    assert console.takes == 0 and console.state == before
    assert backend.transfer.store.list() == profiles
    console.state["motors"][0]["position_rotations"] = 0.3
    assert backend.catalog() == catalog


@pytest.mark.parametrize(
    "params",
    [
        {"target": {"x": float("nan"), "y": 1}},
        {"target": {"x": True, "y": 1}},
        {"target": {"x": 1, "y": 1, "insert": 1}},
        {"target": {"x": 1, "y": 1}, "action": "furnace.write"},
    ],
)
def test_bad_coordinates_never_acquire_control(rig, params):
    backend, console = rig
    with pytest.raises(ValueError):
        backend.prepare("stage.coordinates", params, {}, PAGE)
    assert console.takes == 0


def test_named_motion_uses_shared_bottom_top_insert_order(rig):
    backend, console = rig
    named_profile(backend)
    context = Context()
    result = run(backend, "transfer.position", {"name": "Pickup"}, context)
    assert result["status"] == "succeeded"
    assert [item["motor"] for item in console.moves] == ["bottom", "top", "insert"]
    assert len(context.dispatches) == 3
    assert all(item["speed"] <= 0.25 for item in console.moves)
    assert all(item["stop_epoch"] == 2 for item in console.moves)


def test_inserted_named_motion_uses_bottom_top_insert_without_extraction(rig):
    backend, console = rig
    named_profile(backend)
    console.state["motors"][2]["position_rotations"] = 1
    plan = backend.prepare("transfer.position", {"name": "Pickup"}, {}, PAGE)
    assert plan["targets"] == [("bottom", 3), ("top", 4), ("insert", 2)]
    result = backend.execute(plan, Context())
    assert result["status"] == "succeeded"
    assert [(item["motor"], item["position"]) for item in console.moves] == plan["targets"]


def test_changed_reference_stops_before_later_axis(rig):
    backend, console = rig
    console.after_move = lambda: console.state.update(reference_revision=4)
    result = run(backend, "stage.coordinates", {"target": {"x": 1, "y": 2}})
    assert result["status"] == "failed"
    assert len(console.moves) == 1
    assert console.stops == 1
    assert "reference" in result["error"]


def test_operator_cancellation_stops_before_later_axis(rig):
    backend, console = rig
    context = Context()
    console.after_move = context.cancelled.set
    result = run(backend, "stage.coordinates", {"target": {"x": 1, "y": 2}}, context)
    assert result["status"] in {"stopped", "failed"}
    assert len(console.moves) == 1 and console.stops == 1


def test_unconfirmed_cleanup_stop_remains_unknown(rig):
    backend, console = rig
    console.after_move = lambda: console.state.update(reference_revision=4)
    console.stop = lambda device: {"outcome": "unconfirmed"}
    result = run(backend, "stage.coordinates", {"target": {"x": 1, "y": 2}})
    assert result["status"] == "unknown"
    assert len(console.moves) == 1


def test_ambiguous_dispatch_is_unknown_and_never_retried(rig):
    backend, console = rig
    console.outcome = "unknown"
    result = run(backend, "stage.coordinates", {"target": {"x": 1, "y": 2}})
    assert result["status"] == "unknown"
    assert len(console.moves) == 1


def test_saved_profile_change_rejects_queued_motion_before_take(rig):
    backend, console = rig
    saved = named_profile(backend)
    plan = backend.prepare("transfer.position", {"name": "Pickup"}, {}, PAGE)
    saved["positions"]["Pickup"]["position_rotations"]["x"] = 7
    changed = backend.transfer.store.save(saved)
    backend.transfer.store.select(changed["id"])
    with pytest.raises(ValueError, match="profile changed"):
        backend.execute(plan, Context())
    assert console.takes == 0 and not console.moves


@pytest.mark.parametrize("missing", ["reference_revision", "stop_epoch"])
def test_missing_stage_reference_never_becomes_valid(rig, missing):
    backend, console = rig
    console.state.pop(missing)
    with pytest.raises(ValueError, match="no current reference"):
        backend.prepare("transfer.extract", {}, {}, PAGE)
    assert console.takes == 0


@pytest.mark.parametrize("change", ["reference-reset", "restart", "stale", "fixture"])
def test_mock_source_changes_invalidate_queued_stage_motion(rig, change):
    backend, console = rig
    backend.source = MockConsole()
    plan = backend.prepare("transfer.extract", {}, {}, PAGE)
    backend.source.control(change, "compatible" if change == "fixture" else None)
    with pytest.raises(ValueError):
        backend.execute(plan, Context())
    assert console.takes == 0


def test_stage_jog_is_bounded_and_rechecks_the_target(rig):
    backend, console = rig
    result = run(backend, "stage.jog", {"motor": "insert", "direction": "ccw"})
    assert result["status"] == "succeeded"
    assert console.moves[0]["rotations"] == 0.1
    result = run(backend, "stage.jog", {"motor": "top", "direction": "ccw"})
    assert result["status"] == "succeeded"
    assert console.moves[1]["motor"] == "top" and console.moves[1]["rotations"] == 0.1
    console.state["motors"][2]["position_rotations"] = 10
    with pytest.raises(ValueError, match="travel limits"):
        backend.prepare("stage.jog", {"motor": "insert", "direction": "ccw"}, {}, PAGE)


def test_bounded_step_uses_existing_rotation_units_and_current_motor_speed(rig):
    backend, console = rig
    params = {"motor": "insert", "direction": "ccw", "rotations": 0.2, "speed": 0.2}
    result = run(backend, "stage.step", params)
    assert result["status"] == "succeeded"
    assert console.moves[0]["rotations"] == 0.2
    assert console.moves[0]["speed"] == 0.2
    with pytest.raises(ValueError, match="speed limit"):
        backend.prepare("stage.step", {**params, "speed": 1}, {}, PAGE)


def test_simulated_state_cannot_change_to_physical_between_axes(rig):
    backend, console = rig
    console.after_move = lambda: console.state.update(demo=False)
    result = run(backend, "stage.coordinates", {"target": {"x": 1, "y": 2}})
    assert result["status"] == "failed"
    assert len(console.moves) == 1
    assert "mode does not match" in result["error"]


def test_profile_actions_reuse_shared_draft_and_store(rig):
    backend, console = rig
    assert run(backend, "transfer.profile.new", {"name": "Test profile"})["status"] == "succeeded"
    assert run(backend, "transfer.record", {"name": "Origin"})["status"] == "succeeded"
    result = run(backend, "transfer.profile.save")
    selected = result["result"]["state"]["active_profile"]
    assert selected["name"] == "Test profile"
    assert selected["positions"]["Origin"]["position_rotations"] == {"x": 0, "y": 0, "insert": 0}
    assert not console.moves


def test_mock_microscope_settings_update_existing_preview_without_device_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The simulator attempted a network connection.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    backend = create_backend(mock=True)
    try:
        preview = backend.service.turret_preview
        before = preview.autofocus_settings()
        result = run(backend, "microscope.set_autofocus_settings", {"ten_x_half_range_pulses": 42})
        assert result["status"] == "succeeded" and result["result"]["simulated"] is True
        assert preview.autofocus_settings() == {**before, "ten_x_half_range_pulses": 42}
        assert (
            run(backend, "microscope.set_focus_travel_limit", {"pulses": 123})["status"]
            == "succeeded"
        )
        assert preview.travel_limit() == {"focus_travel_limit_pulses": 123}
        assert preview.store.list() == []
        preview.session = {}
        with pytest.raises(ServiceError):
            run(backend, "microscope.set_focus_travel_limit", {"pulses": 456})
        assert preview.travel_limit() == {"focus_travel_limit_pulses": 123}
    finally:
        backend.close()


def test_mock_uses_real_stage_contract_and_turret_preview_without_sockets(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("The simulator attempted a network connection.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    backend = create_backend(mock=True)
    try:
        catalog = {item["id"]: item for item in backend.catalog()}
        assert catalog["stage.calibrate"]["available"]
        assert catalog["turret.select"]["available"]
        assert not catalog["robot.move"]["available"]
        with pytest.raises(ValueError, match="Calibrate"):
            backend.prepare("transfer.insert", {}, {}, PAGE)
        result = run(backend, "stage.calibrate", {"motor": "insert", "speed": 0.25})
        assert result["status"] == "succeeded", result
        assert run(backend, "transfer.extract")["status"] == "succeeded"
        assert backend.observations()["stage"]["demo"] is True
        for action in ("begin", "record", "finish", "cancel", "save", "load", "delete", "upload"):
            assert "turret." + action not in catalog
            with pytest.raises(ValueError):
                backend.prepare("turret." + action, {}, {}, PAGE)
        for action in (
            "microscope.set_focus_zero",
            "microscope.home_focus",
            "camera.turret_frame",
        ):
            assert action not in catalog
            with pytest.raises(ValueError):
                backend.prepare(action, {}, {}, PAGE)
        assert backend.service.console._owned("stage", PAGE) is not None
        assert backend.service.turret_preview.session is None
        assert backend.release_page("agent-another-session")["devices"] == {}
        assert backend.service.console._owned("stage", PAGE) is not None
        released = backend.release_page(PAGE)
        assert released["devices"] == {"stage": "released"}
        assert released["outcome"] == "released"
        assert backend.service.console._owned("stage", PAGE) is None
        assert backend.service.turret_preview.session is None
    finally:
        backend.close()
