"""Decision references use the same cached state and selected profile as choices."""

import copy
import json
from types import SimpleNamespace

import pytest
from test_agent_connect_actions import PAGE, Context, named_profile
from test_agent_connect_actions import rig as rig

from orchestrator.agent_connect.action_backend import _digest
from orchestrator.agent_connect.protocol import bundle
from orchestrator.agent_connect.state import CacheStateProvider
from orchestrator.devices import STALE_S


@pytest.fixture
def cached(rig):
    backend, console = rig
    entry = {"phase": "ready", "age_s": 0.25, "state": copy.deepcopy(console.state)}
    reads = []

    def get(device):
        reads.append(device)
        assert device == "stage"
        return copy.deepcopy(entry)

    backend.service.devices = SimpleNamespace(get=get, entries={"stage": entry})
    return backend, console, entry, reads


def test_choices_share_one_stage_and_profile_snapshot_without_device_io(cached, monkeypatch):
    backend, console, entry, reads = cached
    selected = named_profile(backend)
    changed = copy.deepcopy(selected)
    changed["positions"]["Elsewhere"] = changed["positions"].pop("Pickup")
    changed = backend.transfer.store.save(changed)
    select = backend.transfer.store.select
    original = backend.transfer.store.selected
    selections = []

    def selected_then_changed():
        selections.append(True)
        value = original()
        select(changed["id"])
        return value

    def forbidden(*args, **kwargs):
        pytest.fail("Choices must not read the stage, write profiles, or acquire control.")

    monkeypatch.setattr(backend.transfer.store, "selected", selected_then_changed)
    monkeypatch.setattr(console, "read", forbidden)
    monkeypatch.setattr(console, "take", forbidden)
    monkeypatch.setattr(console, "command", forbidden)
    monkeypatch.setattr(console, "stop", forbidden)
    before = copy.deepcopy(entry)
    choices = backend.choices(PAGE)
    assert selections == [True] and reads == ["stage"]
    assert choices["actions"]["transfer.position"]["name"] == [
        {"value": "Pickup", "label": "Pickup"}
    ]
    context = choices["stage_context"]
    assert context == {
        "version": 1,
        "available": True,
        "reason": "",
        "preconditions": {
            "stage_reference": backend._stage_reference(before["state"]),
            "profile_id": selected["id"],
            "profile_digest": _digest(selected),
        },
    }
    assert choices["constraints"]["stage"]["age_s"] == before["age_s"]
    assert entry == before and PAGE not in backend.transfer.pages
    json.dumps(choices, allow_nan=False)

    monkeypatch.setattr(backend.transfer.store, "selected", original)
    monkeypatch.setattr(backend.transfer.store, "_write", forbidden)
    backend.choices(PAGE)


@pytest.mark.parametrize(
    "change,reason",
    [
        (lambda entry: entry.clear(), "no device state"),
        (lambda entry: entry.update(phase="stale"), "cache is stale"),
        (lambda entry: entry.update(age_s=STALE_S + 0.1), "cache is stale"),
        (lambda entry: entry.update(age_s=None), "age is unavailable"),
        (lambda entry: entry["state"].update(connected=False), "disconnected"),
        (lambda entry: entry["state"].update(demo=False), "different device mode"),
        (lambda entry: entry["state"].pop("instance"), "no usable reference"),
        (lambda entry: entry["state"].update(instance=""), "no usable reference"),
        (lambda entry: entry["state"].pop("reference_revision"), "no usable reference"),
        (lambda entry: entry["state"].pop("stop_epoch"), "no usable reference"),
        (lambda entry: entry["state"].update(stop_epoch=True), "no usable reference"),
        (
            lambda entry: entry["state"]["motors"][0].update(tolerance=float("nan")),
            "no usable reference",
        ),
    ],
)
def test_unavailable_stage_context_has_no_preconditions(cached, change, reason):
    backend, console, entry, _ = cached
    change(entry)
    context = backend.choices(PAGE)["stage_context"]
    assert context["version"] == 1 and context["available"] is False
    assert context["preconditions"] is None and reason in context["reason"]
    assert console.takes == console.stops == 0 and not console.moves


def test_context_without_selected_profile_keeps_available_axis_choices(cached):
    backend, _, entry, _ = cached
    entry["state"]["motors"][0].update(
        lower_limit_rotations=None, upper_limit_rotations=None, positioning_ready=False
    )
    choices = backend.choices(PAGE)
    assert choices["stage_context"]["available"]
    preconditions = choices["stage_context"]["preconditions"]
    assert preconditions["profile_id"] is None
    assert preconditions["profile_digest"] == _digest(None)
    axes = choices["constraints"]["stage"]["axes"]
    assert not axes[0]["available"] and axes[1]["available"]


@pytest.mark.parametrize("missing", [("reference_revision",), ("instance", "reference_revision")])
def test_physical_context_keeps_legacy_reference_fallback(cached, missing):
    backend, _, entry, _ = cached
    backend.service.demo = backend.simulated = False
    backend.physical = True
    entry["state"]["demo"] = False
    for key in missing:
        entry["state"].pop(key)
    context = backend.choices(PAGE)["stage_context"]
    assert context["available"]
    reference = context["preconditions"]["stage_reference"]
    assert reference["instance"] == entry["state"].get("instance")
    assert reference["reference_revision"] == entry["state"].get("reference_revision")
    assert reference["reference_source"] == (
        "limits_port_calibration" if "reference_revision" in missing else "reference_revision"
    )


def test_physical_revision_without_instance_has_no_decision_context(cached):
    backend, _, entry, _ = cached
    backend.service.demo = backend.simulated = False
    backend.physical = True
    entry["state"]["demo"] = False
    entry["state"].pop("instance")
    context = backend.choices(PAGE)["stage_context"]
    assert context["available"] is False and context["preconditions"] is None
    state = CacheStateProvider(backend.service.devices, mode="live")
    assert state()["devices"]["stage"]["values"]["stage_reference"] is None


@pytest.mark.parametrize(
    "change", ["profile", "contents", "limits", "revision", "stop", "instance"]
)
def test_decision_changes_reject_prepare_before_control(cached, monkeypatch, change):
    backend, console, _, _ = cached
    selected = named_profile(backend)
    preconditions = backend.choices(PAGE)["stage_context"]["preconditions"]
    if change in {"profile", "contents"}:
        selected["positions"]["Pickup"]["position_rotations"]["x"] = 6
        if change == "profile":
            changed = backend.transfer.store.save(selected)
            backend.transfer.store.select(changed["id"])
        else:
            monkeypatch.setattr(backend.transfer.store, "selected", lambda: selected)
    elif change == "limits":
        console.state["motors"][0]["upper_limit_rotations"] = 9
    elif change == "revision":
        console.state["reference_revision"] += 1
    elif change == "instance":
        console.state["instance"] = "stage-process-b"
    else:
        console.state["stop_epoch"] += 1
    with pytest.raises(ValueError, match="no longer current"):
        backend.prepare("transfer.position", {"name": "Pickup"}, preconditions, PAGE)
    assert console.takes == console.stops == 0 and not console.moves


@pytest.mark.parametrize("instance", [None, "missing"])
def test_old_reference_without_instance_cannot_bind_current_service(cached, instance):
    backend, console, _, _ = cached
    preconditions = backend.choices(PAGE)["stage_context"]["preconditions"]
    reference = preconditions["stage_reference"]
    if instance == "missing":
        reference.pop("instance")
    else:
        reference["instance"] = instance
    with pytest.raises(ValueError, match="no longer current"):
        backend.prepare("stage.position", {"motor": "top", "position": 1}, preconditions, PAGE)
    assert console.takes == console.stops == 0 and not console.moves


@pytest.mark.parametrize("during_take", [False, True])
def test_stage_restart_rejects_queued_decision_even_with_matching_epoch(
    cached, monkeypatch, during_take
):
    backend, console, _, _ = cached
    plan = backend.prepare("stage.position", {"motor": "top", "position": 1}, {}, PAGE)
    if during_take:
        take = console.take

        def restart_and_take(device, page):
            console.state["instance"] = "stage-process-b"
            return take(device, page)

        monkeypatch.setattr(console, "take", restart_and_take)
    else:
        console.state["instance"] = "stage-process-b"
    with pytest.raises(ValueError, match="reference.*changed"):
        backend.execute(plan, Context())
    assert console.takes == int(during_take) and not console.moves


@pytest.mark.parametrize("action", ["transfer.position", "transfer.stage"])
def test_prepare_keeps_targets_and_references_from_one_profile(cached, monkeypatch, action):
    backend, console, _, _ = cached
    selected = named_profile(backend)
    if action == "transfer.stage":
        capture = copy.deepcopy(selected["positions"]["Pickup"])
        capture["position_rotations"].pop("insert")
        selected["stage_positions"]["microscope"] = capture
        selected = backend.transfer.store.save(selected)
        backend.transfer.store.select(selected["id"])
    preconditions = backend.choices(PAGE)["stage_context"]["preconditions"]
    changed = copy.deepcopy(selected)
    changed["positions"]["Pickup"]["position_rotations"]["x"] = 6
    if action == "transfer.stage":
        changed["stage_positions"]["microscope"]["position_rotations"]["x"] = 6
    changed = backend.transfer.store.save(changed)
    original = backend.transfer.store.selected
    selections = []

    def selected_then_changed():
        selections.append(True)
        value = original()
        backend.transfer.store.select(changed["id"])
        return value

    monkeypatch.setattr(backend.transfer.store, "selected", selected_then_changed)
    params = {"target": "microscope"} if action == "transfer.stage" else {"name": "Pickup"}
    plan = backend.prepare(action, params, preconditions, PAGE)
    assert selections == [True]
    assert plan["references"]["profile_id"] == selected["id"]
    assert plan["references"]["profile_digest"] == _digest(selected)
    assert plan["targets"][:2] == [("bottom", 3), ("top", 4)]
    with pytest.raises(ValueError, match="profile changed"):
        backend.execute(plan, Context())
    assert console.takes == console.stops == 0 and not console.moves


def test_valid_decision_context_and_legacy_empty_preconditions_are_accepted(cached):
    backend, _, _, _ = cached
    named_profile(backend)
    preconditions = backend.choices(PAGE)["stage_context"]["preconditions"]
    params = {"name": "Pickup"}
    prepared = backend.prepare("transfer.position", params, preconditions, PAGE)
    legacy = backend.prepare("transfer.position", params, {}, PAGE)
    assert prepared == legacy


@pytest.mark.parametrize("change", ["profile", "limits", "stop", "instance"])
def test_decision_references_are_checked_before_later_axes(cached, change):
    backend, console, _, _ = cached
    selected = named_profile(backend)
    preconditions = backend.choices(PAGE)["stage_context"]["preconditions"]
    plan = backend.prepare("transfer.position", {"name": "Pickup"}, preconditions, PAGE)

    def changed():
        if change == "profile":
            selected["positions"]["Pickup"]["position_rotations"]["x"] = 6
            saved = backend.transfer.store.save(selected)
            backend.transfer.store.select(saved["id"])
        elif change == "limits":
            console.state["motors"][1]["upper_limit_rotations"] = 9
        elif change == "instance":
            console.state["instance"] = "stage-process-b"
        else:
            console.state["stop_epoch"] += 1

    console.after_move = changed
    result = backend.execute(plan, Context())
    assert result["status"] == "failed"
    assert len(console.moves) == 1 and console.stops == 1


@pytest.mark.parametrize("change", ["reference", "instance"])
def test_state_frame_reference_matches_choices_and_retains_its_snapshot(cached, change):
    backend, _, entry, _ = cached
    state = CacheStateProvider(backend.service.devices, mode="MOCK")
    first = state()["devices"]["stage"]
    original = backend.choices(PAGE)["stage_context"]["preconditions"]["stage_reference"]
    assert first["values"]["stage_reference"] == original
    if change == "reference":
        entry["state"]["reference_revision"] += 1
        entry["state"]["motors"][1]["upper_limit_rotations"] = 9
    else:
        entry["state"]["instance"] = "stage-process-b"
    second = state()["devices"]["stage"]
    current = backend.choices(PAGE)["stage_context"]["preconditions"]["stage_reference"]
    assert second["values"]["stage_reference"] == current != original
    assert first["values"]["stage_reference"] == original
    schema = bundle()["schemas"]["state"]["properties"]["devices"]["additionalProperties"]
    validator = pytest.importorskip("jsonschema").Draft202012Validator(schema)
    validator.validate(first)
    validator.validate(second)
    second["values"]["stage_reference"]["stop_epoch"] = -1
    assert not validator.is_valid(second)


@pytest.mark.parametrize(
    "mode,demo,legacy",
    [("live", False, True), ("live", True, False), ("live", None, False), ("MOCK", False, False)],
)
def test_state_frame_legacy_reference_requires_live_physical_mode(cached, mode, demo, legacy):
    backend, _, entry, _ = cached
    entry["state"].pop("reference_revision")
    entry["state"]["demo"] = demo
    row = CacheStateProvider(backend.service.devices, mode=mode)()["devices"]["stage"]
    reference = row["values"]["stage_reference"]
    if legacy:
        assert reference["reference_source"] == "limits_port_calibration"
    else:
        assert reference is None
    schema = bundle()["schemas"]["state"]["properties"]["devices"]["additionalProperties"]
    pytest.importorskip("jsonschema").Draft202012Validator(schema).validate(row)


def test_state_frame_without_stop_epoch_has_no_stage_reference(cached):
    backend, _, entry, _ = cached
    entry["state"].pop("stop_epoch")
    state = CacheStateProvider(backend.service.devices, mode="MOCK")
    assert state()["devices"]["stage"]["values"]["stage_reference"] is None
