"""Physical-mode dispatch with native schemas and local test doubles; no device I/O."""

import copy
import json
from types import SimpleNamespace

import pytest
from test_agent_connect_actions import PAGE, Context, StageConsole, named_profile, run

from orchestrator.agent_connect.action_backend import create_backend
from orchestrator.agent_connect.actions import SPECS
from orchestrator.devices import STALE_S
from orchestrator.transfer_profiles import TransferProfileStore
from orchestrator.web.sample_transfer import LiveTransferAdapter, SampleTransfer


class NativeConsole(StageConsole):
    def __init__(self):
        super().__init__()
        self.state.update(demo=False, port="/dev/stage")
        self.state.pop("instance")
        self.state.pop("reference_revision")
        self.reads = []
        self.sent = []
        self.released = []
        self.stopped = []
        self.descriptions = {
            device: {
                "demo": False,
                "commands": {
                    spec["command"]: {"params": copy.deepcopy(spec["params"])}
                    for spec in SPECS.values()
                    if spec["device"] == device and spec["route"] == "command"
                },
            }
            for device in ("stage", "robot", "microscope", "vision", "thermal")
        }
        self.descriptions["furnace"] = {
            "demo": False,
            "commands": {
                spec["command"]: {"params": copy.deepcopy(spec["params"])}
                for spec in SPECS.values()
                if spec["device"] == "furnace" and not spec["disabled_reason"]
            },
        }
        self.descriptions["stage"]["commands"].update(
            move_guarded={"params": {}}, move_to_position_guarded={"params": {}}
        )
        commands = self.descriptions["furnace"]["commands"]
        bounds = commands["apply_setpoint"]["params"]["properties"]
        bounds["temperature_c"]["maximum"] = 450
        bounds["ramp_rate_c_per_min"]["maximum"] = 20
        # The deployed furnace describes nested sequence objects without properties.
        for name in ("set_sequence", "start_sequence"):
            fields = commands[name]["params"]["properties"]
            fields["steps"] = {"type": "array", "minItems": 1, "items": {"type": "object"}}
            fields["end_gas"] = {"type": "object"}
        self.states = {
            device: {"demo": False, "instance": device + "-process", "last_command": None}
            for device in self.descriptions
        }
        self.states["stage"] = self.state
        self.states["robot"].update(actual=[100, 0, 50, 0], feedback_fresh=True)
        self.descriptions["robot"]["commands"]["execute"]["params"] = copy.deepcopy(
            SPECS["robot.move"]["params"]
        )

    def read(self, device, path):
        self.reads.append((device, path))
        return copy.deepcopy(
            self.descriptions[device] if path == "api/describe" else self.states[device]
        )

    def take(self, device, page):
        if device == "stage":
            return super().take(device, page)
        self.holder = page
        self.takes += 1

    def command(self, device, page, action, params):
        assert self.holder == page
        if device == "stage" and action in {"move_to_position_guarded", "move_guarded"}:
            return super().command(device, page, action, params)
        self.sent.append((device, action, copy.deepcopy(params)))
        identifier = str(len(self.sent))
        self.states[device]["last_command"] = {
            "id": identifier,
            "result": "ok",
            "action": action,
            "client": page,
        }
        return {
            "outcome": "accepted",
            "command_id": identifier,
            "instance": self.states[device].get("instance"),
            "client": page,
        }

    def stop(self, device):
        self.stopped.append(device)
        return super().stop(device)

    def release_all(self, page):
        self.released.append(page)
        self.holder = None
        return {"furnace": "released"}


@pytest.fixture
def live(tmp_path):
    console = NativeConsole()
    gas = []
    service = SimpleNamespace(
        demo=False,
        console=console,
        dashboard_configured=lambda device: device in console.descriptions,
        devices=SimpleNamespace(
            get=lambda device: {"describe": copy.deepcopy(console.descriptions[device])}
        ),
        console_client=lambda *_: SimpleNamespace(
            stop=lambda **params: gas.append(params) or {"stop": "confirmed"},
            close=lambda: gas.append("closed"),
        ),
    )
    service.sample_transfer = SampleTransfer(
        TransferProfileStore(tmp_path), LiveTransferAdapter(service), demo=False
    )
    backend = create_backend(service, allow_physical=True)
    yield backend, console, gas
    backend.close()
    service.sample_transfer.close()


def test_physical_mode_requires_server_opt_in_and_cannot_be_request_selected(live):
    backend, console, _ = live
    disabled = create_backend(backend.service, mock=True)
    assert not disabled.enabled and not disabled.physical and disabled.mode == "disabled"
    assert backend.enabled and backend.physical and backend.furnace_enabled
    assert backend.mode == "physical" and not backend.simulated
    with pytest.raises(ValueError, match="disabled"):
        disabled.prepare("furnace.connect", {}, {}, PAGE)
    with pytest.raises(ValueError, match="unsupported"):
        backend.prepare("furnace.connect", {"allow_physical": True}, {}, PAGE)
    assert not console.sent and console.takes == 0


def test_live_catalog_uses_cached_contract_caps_without_device_reads(live):
    backend, console, _ = live
    catalog = {item["id"]: item for item in backend.catalog()}
    assert console.reads == []
    assert catalog["furnace.apply_setpoint"]["available"]
    assert (
        catalog["furnace.apply_setpoint"]["params"]["properties"]["temperature_c"]["maximum"] == 450
    )
    step = catalog["furnace.set_sequence"]["params"]["properties"]["steps"]["items"]
    assert step["properties"]["ramp_rate_c_per_min"]["maximum"] == 20
    assert catalog["furnace.start_sequence"]["defaults"] == {}
    assert "camera.turret_frame" not in catalog
    assert not catalog["recipe.run"]["available"]
    assert not catalog["camera.set_region"]["available"]


@pytest.mark.parametrize(
    "action",
    [
        "turret.begin",
        "turret.finish",
        "turret.cancel",
        "turret.record",
        "turret.save",
        "turret.load",
        "turret.delete",
        "turret.upload",
        "turret.measure_response",
        "turret.focus_move",
        "turret.return_clearance",
        "microscope.set_focus_zero",
        "microscope.home_focus",
        "microscope.focus_to_clearance",
        "microscope.focus_to_saved_height",
        "camera.turret_frame",
    ],
)
def test_human_microscope_calibration_cannot_enter_agent_dispatch(live, action):
    backend, console, _ = live
    assert action not in {row["id"] for row in backend.catalog()}
    with pytest.raises(ValueError):
        backend.prepare(action, {}, {}, PAGE)
    assert console.takes == 0 and not console.sent


@pytest.mark.parametrize(
    "action,params",
    [
        ("furnace.apply_setpoint", {"temperature_c": 100, "ramp_rate_c_per_min": 2}),
        ("furnace.set_gas_flow", {"channel_index": 0, "flow_percent": 5}),
        ("furnace.set_gas_valve", {"channel_index": 2, "open": False}),
        ("furnace.connect_flow", {}),
        ("furnace.start_sequence", {}),
        ("microscope.rough_focus_5x", {}),
        *[("microscope.select_objective", {"id": lens}) for lens in range(4)],
        *[("microscope.objective_nudge", {"steps": steps}) for steps in (-10, -1, 1, 10)],
        *[("microscope.focus_move", {"steps": steps}) for steps in (-100, 100)],
        ("microscope.autofocus", {"mode": "local"}),
        ("microscope.autofocus_sequence", {}),
        ("microscope.set_stabilization", {"enabled": True}),
        ("microscope.snapshot", {}),
        ("microscope.start_recording", {"recording_id": "a" * 32}),
        ("microscope.stop_recording", {"recording_id": "a" * 32}),
        ("microscope.record_with_sequence", {"recording_id": "a" * 32, "enabled": True}),
        ("camera.thermal_shutter", {}),
        ("vision.read_labels", {"camera": 1, "region": [0, 0, 10, 10]}),
        ("vision.check_calibration", {"camera": 1, "flange_mm": [100, 0, 50, 0]}),
    ],
)
def test_fixed_live_actions_use_console_command_and_confirm_native_completion(live, action, params):
    backend, console, _ = live
    console.states[SPECS[action]["device"]].setdefault("instance", "stage-process")
    assert next(row for row in backend.catalog() if row["id"] == action)["available"]
    result = run(backend, action, params)
    assert console.sent == [(SPECS[action]["device"], SPECS[action]["command"], params)]
    assert console.takes == 1
    assert result["status"] == "succeeded" and result["result"]["simulated"] is False
    assert result["result"]["command"]["result"] == "ok"


@pytest.mark.parametrize("instance", [None, "", " ", "new-process"])
def test_native_completion_rejects_reused_command_id_after_identity_loss(
    live, monkeypatch, instance
):
    backend, console, _ = live
    command = console.command

    def changed(*args):
        accepted = command(*args)
        console.states["microscope"]["instance"] = instance
        return accepted

    monkeypatch.setattr(console, "command", changed)
    result = run(backend, "microscope.snapshot", {})
    assert result["status"] == "unknown"
    assert "process" in result["error"]
    assert len(console.sent) == 1 and not console.stopped


@pytest.mark.parametrize("field", ["instance", "client", "command_id"])
def test_native_completion_requires_acceptance_identity(live, monkeypatch, field):
    backend, console, _ = live
    command = console.command

    def unidentified(*args):
        accepted = command(*args)
        accepted.pop(field)
        return accepted

    monkeypatch.setattr(console, "command", unidentified)
    result = run(backend, "microscope.snapshot", {})
    assert result["status"] == "unknown"
    assert len(console.sent) == 1 and not console.stopped


@pytest.mark.parametrize("field", ["action", "client"])
def test_native_completion_requires_accepted_action_and_client(live, monkeypatch, field):
    backend, console, _ = live
    command = console.command

    def replaced(*args):
        accepted = command(*args)
        console.states["microscope"]["last_command"][field] = "other"
        return accepted

    monkeypatch.setattr(console, "command", replaced)
    result = run(backend, "microscope.snapshot", {})
    assert result["status"] == "unknown"
    assert "action and client" in result["error"]
    assert len(console.sent) == 1 and not console.stopped


def test_live_autofocus_catalog_excludes_legacy_full_mode(live):
    backend, console, _ = live
    console.descriptions["microscope"]["commands"]["autofocus"]["params"]["properties"]["mode"][
        "enum"
    ] = ["local", "full"]
    action = next(row for row in backend.catalog() if row["id"] == "microscope.autofocus")
    assert action["params"]["properties"]["mode"]["enum"] == ["local"]
    assert action["defaults"] == {"mode": "local"}
    assert action["revision"] == 8
    with pytest.raises(ValueError):
        backend.prepare("microscope.autofocus", {"mode": "full"}, {}, PAGE)
    assert console.takes == 0 and not console.sent
    assert run(backend, "microscope.autofocus", action["defaults"])["status"] == "succeeded"
    assert console.sent == [("microscope", "autofocus", {"mode": "local"})]


@pytest.mark.parametrize(
    "action,path,params",
    [
        ("turret.select", "api/turret-calibration/select", {"id": "saved-profile"}),
        ("microscope.set_focus_travel_limit", "api/focus-travel-limit", {"pulses": 20000}),
        *[
            ("microscope.set_autofocus_settings", "api/autofocus-settings", {field: 4000})
            for field in (
                "five_x_half_range_pulses",
                "ten_x_half_range_pulses",
                "twenty_x_half_range_pulses",
                "fifty_x_half_range_pulses",
            )
        ],
    ],
)
def test_live_microscope_operator_routes_use_native_lease(live, monkeypatch, action, path, params):
    backend, console, _ = live
    selected = []

    def operator(device, page, path, params):
        assert console.holder == page == PAGE
        selected.append((device, path, params))
        return {"outcome": "accepted"}

    monkeypatch.setattr(console, "operator", operator, raising=False)
    assert next(row for row in backend.catalog() if row["id"] == action)["available"]
    result = run(backend, action, params)
    assert result["status"] == "succeeded"
    assert selected == [("microscope", path, params)]
    assert console.takes == 1 and not console.sent


@pytest.mark.parametrize(
    "action,params",
    [
        ("microscope.set_focus_travel_limit", {"pulses": value})
        for value in (0, 2147483648, True, 1.5)
    ]
    + [
        ("microscope.set_autofocus_settings", params)
        for params in (
            {},
            {"five_x_half_range_pulses": 3},
            {"ten_x_half_range_pulses": 1000001},
            {"twenty_x_half_range_pulses": True},
            {"fifty_x_half_range_pulses": 4.5},
            {"five_x_half_range_pulses": 100, "path": "api/turret-calibration/delete"},
        )
    ],
)
def test_microscope_settings_validate_before_lease_and_again_at_execution(live, action, params):
    backend, console, _ = live
    with pytest.raises(ValueError):
        backend.prepare(action, params, {}, PAGE)
    valid = (
        {"pulses": 100} if action.endswith("travel_limit") else {"five_x_half_range_pulses": 100}
    )
    plan = backend.prepare(action, valid, {}, PAGE)
    plan["params"] = params
    with pytest.raises(ValueError):
        backend.execute(plan, Context())
    assert console.takes == 0 and not console.sent


def test_microscope_settings_preserve_native_busy_rejection(live, monkeypatch):
    backend, console, _ = live
    plan = backend.prepare("microscope.set_focus_travel_limit", {"pulses": 100}, {}, PAGE)

    def operator(*args):
        return {"outcome": "refused", "error": "Stop microscope work before changing settings"}

    monkeypatch.setattr(console, "operator", operator, raising=False)
    result = backend.execute(plan, Context())
    assert result["status"] == "failed"
    assert result["error"] == "Stop microscope work before changing settings"


def test_live_microscope_stop_needs_no_lease_or_description(live, monkeypatch):
    backend, console, _ = live

    def forbidden(*args, **kwargs):
        pytest.fail("Stop must not read discovery or acquire a lease.")

    monkeypatch.setattr(console, "read", forbidden)
    monkeypatch.setattr(console, "take", forbidden)
    result = backend.stop("microscope")
    assert result["outcome"] == "confirmed"
    assert console.stopped == ["microscope"]
    assert console.takes == 0 and not console.sent


@pytest.mark.parametrize(
    "action,params",
    [
        ("furnace.apply_setpoint", {"temperature_c": 451, "ramp_rate_c_per_min": 2}),
        ("furnace.apply_setpoint", {"temperature_c": 100, "ramp_rate_c_per_min": 21}),
        ("furnace.set_sequence", {"steps": [{"temperature_c": 451, "ramp_rate_c_per_min": 2}]}),
        (
            "furnace.set_sequence",
            {"steps": [{"temperature_c": 100, "ramp_rate_c_per_min": 2, "gas": {"0": 1}}]},
        ),
        ("furnace.start_sequence", {"end_gas": {"0": 0}}),
        ("furnace.set_gas_flow", {"channel_index": 3, "flow_percent": 1}),
        ("furnace.set_gas_flow", {"channel_index": 0, "flow_percent": float("nan")}),
        (
            "furnace.start_sequence",
            {"steps": [{"temperature_c": 10, "ramp_rate_c_per_min": 1, "shell": "anything"}]},
        ),
        ("stage.connect", {"port": "DEMO"}),
    ],
)
def test_invalid_live_parameters_fail_before_a_lease_or_command(live, action, params):
    backend, console, _ = live
    with pytest.raises(ValueError):
        backend.prepare(action, params, {}, PAGE)
    assert console.takes == 0 and not console.sent and not console.moves


def test_furnace_sequence_uses_native_array_schema_and_complete_gas_plan(live):
    backend, console, _ = live
    params = {
        "steps": [
            {"temperature_c": 100, "ramp_rate_c_per_min": 2, "hold_minutes": 1, "gas": {"0": 1}}
        ],
        "end_gas": {"0": 0, "1": 0, "2": 0},
        "end_gas_until_c": 30,
    }
    assert run(backend, "furnace.set_sequence", params)["status"] == "succeeded"
    assert console.sent[0][2] == params


def test_queued_sequence_rechecks_lowered_service_caps_before_dispatch(live):
    backend, console, _ = live
    params = {"steps": [{"temperature_c": 100, "ramp_rate_c_per_min": 2}]}
    plan = backend.prepare("furnace.start_sequence", params, {}, PAGE)
    bounds = console.descriptions["furnace"]["commands"]["apply_setpoint"]["params"]["properties"]
    bounds["ramp_rate_c_per_min"]["maximum"] = 1
    with pytest.raises(ValueError, match="range"):
        backend.execute(plan, Context())
    assert not console.sent


def test_stage_motion_without_deferred_process_metadata_uses_native_epoch_and_limits(live):
    backend, console, _ = live
    plan = backend.prepare("stage.coordinates", {"target": {"x": 1, "y": 2}}, {}, PAGE)
    reference = plan["references"]["stage_reference"]
    assert reference["reference_revision"] is None
    assert reference["reference_source"] == "limits_port_calibration"
    assert reference["instance"] is None
    assert backend.execute(plan, Context())["status"] == "succeeded"
    assert [move["motor"] for move in console.moves] == ["bottom", "top"]
    assert all(move["stop_epoch"] == console.state["stop_epoch"] for move in console.moves)


@pytest.mark.parametrize("instance", ["", " ", True, 12])
def test_invalid_live_stage_identity_is_not_treated_as_legacy(live, instance):
    backend, console, _ = live
    console.state["instance"] = instance
    with pytest.raises(ValueError, match="process identity"):
        backend.prepare("stage.position", {"motor": "top", "position": 1}, {}, PAGE)
    assert not console.moves and console.takes == 0


def test_legacy_stage_reference_preconditions_keep_explicit_missing_identity(live):
    backend, console, _ = live
    reference = backend._stage_reference(console.state)
    assert reference["instance"] is None
    for expected in (
        reference,
        {key: value for key, value in reference.items() if key != "instance"},
    ):
        plan = backend.prepare(
            "stage.position", {"motor": "top", "position": 1}, {"stage_reference": expected}, PAGE
        )
        assert plan["references"]["stage_reference"]["instance"] is None
    console.state["instance"] = "stage-process-current"
    with pytest.raises(ValueError, match="no longer current"):
        backend.prepare(
            "stage.position", {"motor": "top", "position": 1}, {"stage_reference": reference}, PAGE
        )
    assert not console.moves and console.takes == 0


def test_live_stage_revision_without_instance_refuses_preparation(live):
    backend, console, _ = live
    console.state["reference_revision"] = 3
    with pytest.raises(ValueError, match="process identity"):
        backend.prepare("stage.position", {"motor": "top", "position": 1}, {}, PAGE)
    assert not console.moves and console.takes == 0


@pytest.mark.parametrize("field", ["port", "calibration", "upper_limit_rotations", "stop_epoch"])
def test_changed_legacy_stage_reference_prevents_dispatch(live, field):
    backend, console, _ = live
    plan = backend.prepare("stage.coordinates", {"target": {"x": 1, "y": 2}}, {}, PAGE)
    if field in {"port", "stop_epoch"}:
        console.state[field] = "/dev/other" if field == "port" else 999
    else:
        console.state["motors"][0][field] = "unknown" if field == "calibration" else 0.5
    with pytest.raises(ValueError, match="reference"):
        backend.execute(plan, Context())
    assert not console.moves and console.takes == 0


def test_furnace_control_loss_releases_lease_and_preserves_lifecycle(live):
    backend, console, _ = live
    plan = backend.prepare("furnace.start_sequence", {}, {}, PAGE)
    console.holder = PAGE
    assert backend.cancel(plan, PAGE)["outcome"] == "lifecycle_preserved"
    assert backend.release_page(PAGE)["outcome"] == "released"
    assert console.released == [PAGE] and not console.stopped and not console.sent


def test_explicit_furnace_and_gas_stops_bypass_discovery_and_control(live, monkeypatch):
    backend, console, gas = live
    monkeypatch.setattr(console, "read", lambda *_: pytest.fail("Stop requested discovery"))
    assert backend.stop("furnace")["outcome"] == "confirmed"
    result = backend.stop("gas_all")
    assert set(result["results"]) == {"gas_0", "gas_1", "gas_2"}
    assert gas == [
        {"gas_channel": 0},
        "closed",
        {"gas_channel": 1},
        "closed",
        {"gas_channel": 2},
        "closed",
    ]
    assert console.stopped == ["furnace"] and console.takes == 0
    backend.stop("all_motion")
    assert console.stopped == ["furnace", "stage", "robot", "microscope"]


def test_live_description_or_state_cannot_switch_to_demo(live):
    backend, console, _ = live
    console.descriptions["furnace"]["demo"] = True
    with pytest.raises(ValueError, match="does not expose"):
        backend.prepare("furnace.connect", {}, {}, PAGE)
    assert console.takes == 0
    console.state["demo"] = True
    with pytest.raises(ValueError, match="mode"):
        backend.prepare("stage.coordinates", {"target": {"x": 1, "y": 2}}, {}, PAGE)
    backend.service.demo = True
    assert not backend.enabled and backend.mode == "disabled"
    assert backend.stop("stage")["outcome"] == "disabled"


@pytest.mark.parametrize(
    "axis,direction,step",
    [("z", 1, 0.5), ("x", 1, 1.25), ("y", -1, 3.75), ("z", -1, 12.5), ("r", 1, 2.25)],
)
def test_robot_jog_reads_fresh_coordinates_and_uses_bounded_execute(live, axis, direction, step):
    backend, console, _ = live
    assert (
        run(backend, "robot.jog", {"axis": axis, "direction": direction, "step": step, "speed": 1})[
            "status"
        ]
        == "succeeded"
    )
    targets = {"x": 100, "y": 0, "z": 50, "r": 0}
    targets[axis] += direction * step
    assert console.sent == [("robot", "execute", {"targets": targets, "speed": 1})]


@pytest.mark.parametrize("step", [0, -1, float("nan"), float("inf"), True, "1.25"])
def test_robot_jog_rejects_invalid_step_before_control(live, step):
    backend, console, _ = live
    with pytest.raises(ValueError):
        backend.prepare(
            "robot.jog", {"axis": "z", "direction": 1, "step": step, "speed": 1}, {}, PAGE
        )
    assert console.takes == 0 and not console.sent


def test_unavailable_guarded_stage_handler_is_not_advertised_or_dispatched(live):
    backend, console, _ = live
    del console.descriptions["stage"]["commands"]["move_to_position_guarded"]
    catalog = {item["id"]: item for item in backend.catalog()}
    assert not catalog["stage.coordinates"]["available"]
    with pytest.raises(ValueError, match="guarded motion"):
        backend.prepare("stage.coordinates", {"target": {"x": 1, "y": 2}}, {}, PAGE)
    assert console.takes == 0 and not console.moves


@pytest.fixture
def stage_choices(live, monkeypatch):
    backend, console, _ = live
    entry = {
        "phase": "ready",
        "age_s": 0.25,
        "state": copy.deepcopy(console.state),
        "describe": copy.deepcopy(console.descriptions["stage"]),
    }
    reads = []

    def cached(device):
        reads.append(device)
        return copy.deepcopy(entry) if device == "stage" else {}

    def forbidden(*_args, **_kwargs):
        pytest.fail("Runtime stage choices must use the existing cache without device I/O.")

    monkeypatch.setattr(backend.service.devices, "get", cached)
    monkeypatch.setattr(console, "read", forbidden)
    yield backend, console, entry, reads
    assert console.takes == 0 and not console.moves and not console.sent and not console.stopped


def stage_constraints(backend):
    result = backend.choices(PAGE)["constraints"]["stage"]
    json.dumps(result, allow_nan=False)
    return result, {row["id"]: row for row in result["axes"]}


def test_stage_choices_expose_current_calibrated_limits_without_insert_restrictions(stage_choices):
    backend, _, entry, reads = stage_choices
    bottom, top, insert = entry["state"]["motors"]
    bottom.update(lower_limit_rotations=None, upper_limit_rotations=None, positioning_ready=False)
    top.update(upper_limit_rotations=2.364258)
    insert.update(position_rotations=0.012695, tolerance=0.000977, upper_limit_rotations=17.272461)
    catalog = backend.catalog()
    result, axes = stage_constraints(backend)
    assert result["available"] and result["fresh"] and result["connected"]
    assert result["phase"] == "ready" and result["age_s"] == 0.25
    assert axes["top"]["upper_limit_rotations"] == 2.364258
    assert axes["insert"]["upper_limit_rotations"] == 17.272461
    assert axes["insert"]["position_rotations"] == 0.012695
    assert axes["insert"]["tolerance"] == 0.000977
    assert axes["bottom"]["lower_limit_rotations"] is None
    assert axes["bottom"]["upper_limit_rotations"] is None
    assert not axes["bottom"]["available"] and "both travel limits" in axes["bottom"]["reason"]
    assert axes["top"]["available"] and result["xy_blocked"] is False
    assert result["xy_reason"] == ""
    assert "stage" in reads
    top["upper_limit_rotations"] = 3.0
    assert stage_constraints(backend)[1]["top"]["upper_limit_rotations"] == 3.0
    assert backend.catalog() == catalog


@pytest.mark.parametrize(
    "phase,age",
    [
        ("stale", 4),
        ("loading", 0.2),
        ("offline", 0.2),
        ("ready", STALE_S + 0.001),
        ("ready", None),
        ("ready", -1),
        ("ready", float("nan")),
    ],
)
def test_stage_choices_reject_stale_unknown_or_invalid_cache_age(stage_choices, phase, age):
    backend, _, entry, _ = stage_choices
    entry.update(phase=phase, age_s=age)
    result, axes = stage_constraints(backend)
    assert not result["available"] and not result["fresh"] and result["reason"]
    assert all(not axis["available"] and axis["reason"] for axis in axes.values())
    assert axes["top"]["upper_limit_rotations"] == 10.0
    assert result["xy_blocked"] is None and result["xy_reason"]


def test_stage_choices_keep_measured_bounds_while_an_axis_is_busy(stage_choices):
    backend, _, entry, _ = stage_choices
    entry["state"]["motors"][0]["mode"] = "moving"
    result, axes = stage_constraints(backend)
    assert result["available"] and result["fresh"]
    assert all(
        not axis["available"] and "motors to stop" in axis["reason"] for axis in axes.values()
    )
    assert axes["bottom"]["mode"] == "moving"
    assert axes["top"]["lower_limit_rotations"] == 0
    assert axes["top"]["upper_limit_rotations"] == 10


@pytest.mark.parametrize("field,value", [("connected", False), ("connected", None), ("demo", True)])
def test_stage_choices_report_disconnection_or_mode_mismatch(stage_choices, field, value):
    backend, _, entry, _ = stage_choices
    entry["state"][field] = value
    result, axes = stage_constraints(backend)
    assert not result["available"] and result["reason"]
    assert all(not axis["available"] for axis in axes.values())
    assert result["xy_blocked"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"upper_limit_rotations": None},
        {"upper_limit_rotations": float("inf")},
        {"lower_limit_rotations": 11},
        {"position_rotations": None},
        {"telemetry_fresh": False},
        {"positioning_ready": False},
        {"tolerance": 0},
        {"encoder": "unknown"},
        {"fault": "limit"},
    ],
)
def test_stage_choices_preserve_unknown_measurements_and_invalid_reference(stage_choices, changes):
    backend, _, entry, _ = stage_choices
    entry["state"]["motors"][2].update(changes)
    result, axes = stage_constraints(backend)
    assert result["available"]
    assert not axes["insert"]["available"] and axes["insert"]["reason"]
    assert result["xy_blocked"] is False and result["xy_reason"] == ""
    if (
        changes.get("upper_limit_rotations") in (None, float("inf"))
        and "upper_limit_rotations" in changes
    ):
        assert axes["insert"]["upper_limit_rotations"] is None


def test_stage_choices_do_not_restrict_xy_outside_insert_tolerance(stage_choices):
    backend, _, entry, _ = stage_choices
    entry["age_s"] = STALE_S
    insert = entry["state"]["motors"][2]
    insert.update(position_rotations=0.000977, tolerance=0.000977)
    result, _ = stage_constraints(backend)
    assert result["fresh"] and result["xy_blocked"] is False and result["xy_reason"] == ""
    insert["position_rotations"] = 0.000978
    result, _ = stage_constraints(backend)
    assert result["xy_blocked"] is False and result["xy_reason"] == ""


def test_stage_choices_missing_cache_stays_unknown_without_static_limit_fallback(stage_choices):
    backend, _, entry, _ = stage_choices
    entry.clear()
    result, axes = stage_constraints(backend)
    assert not result["available"] and not result["fresh"]
    assert result["connected"] is None and result["age_s"] is None
    for axis in axes.values():
        assert axis["position_rotations"] is None
        assert axis["lower_limit_rotations"] is None and axis["upper_limit_rotations"] is None
        assert axis["calibration"] is None and axis["telemetry_fresh"] is None


def test_simulated_choices_use_the_same_cached_stage_constraints(stage_choices):
    backend, _, entry, _ = stage_choices
    backend.service.demo = backend.simulated = True
    backend.physical = False
    backend.service.dashboard_configured = lambda device: device == "stage"
    entry["state"]["demo"] = entry["describe"]["demo"] = True
    result, axes = stage_constraints(backend)
    assert result["available"] and axes["insert"]["available"]


def test_stage_choices_measure_cache_age_after_existing_profile_reads(stage_choices, monkeypatch):
    backend, console, entry, _ = stage_choices
    monkeypatch.setattr(backend, "_description", lambda device: {"demo": False})

    def profiles(device, path):
        assert device == "microscope" and path == "api/turret-calibration"
        entry["age_s"] = STALE_S + 1
        return {"profiles": []}

    monkeypatch.setattr(console, "read", profiles)
    result, _ = stage_constraints(backend)
    assert result["age_s"] == STALE_S + 1
    assert not result["fresh"] and not result["available"]


def test_per_axis_position_matches_console_with_insert_in_and_other_limits_cleared(live):
    backend, console, _ = live
    bottom, top, insert = console.state["motors"]
    bottom.update(lower_limit_rotations=None, upper_limit_rotations=None, positioning_ready=False)
    insert.update(position_rotations=0.012695, tolerance=0.000977)
    top["upper_limit_rotations"] = 2.364258
    assert SPECS["stage.position"]["revision"] == 3
    result = run(backend, "stage.position", {"motor": "top", "position": 1})
    assert result["status"] == "succeeded", result
    assert len(console.moves) == 1
    assert console.moves[0] == {"motor": "top", "position": 1, "speed": 0.25, "stop_epoch": 2}
    assert insert["position_rotations"] == 0.012695
    assert not console.stopped


XY_ACTIONS = [
    ("stage.coordinates", {"target": {"x": 1, "y": 2}}),
    ("stage.jog", {"motor": "top", "direction": "ccw"}),
    ("stage.jog", {"motor": "bottom", "direction": "ccw"}),
    ("stage.step", {"motor": "top", "direction": "ccw", "rotations": 0.1, "speed": 0.25}),
    ("stage.step", {"motor": "bottom", "direction": "ccw", "rotations": 0.1, "speed": 0.25}),
    ("transfer.stage", {"target": "out_stage"}),
    ("transfer.stage", {"target": "microscope"}),
    ("transfer.position", {"name": "Pickup"}),
]


@pytest.fixture
def xy_live(live):
    backend, console, _ = live
    profile = named_profile(backend)
    capture = profile["positions"]["Pickup"]
    profile["stage_positions"] = {
        target: {
            "position_rotations": {
                axis: capture["position_rotations"][axis] for axis in ("x", "y")
            },
            "captured_at": capture["captured_at"],
        }
        for target in ("out_stage", "microscope")
    }
    saved = backend.transfer.store.save(profile)
    backend.transfer.store.select(saved["id"])
    console.state["motors"][2]["position_rotations"] = 1
    return backend, console


@pytest.mark.parametrize("action,params", XY_ACTIONS)
def test_xy_actions_execute_with_insert_in_without_extraction(xy_live, action, params):
    backend, console = xy_live
    context = Context()
    plan = backend.prepare(action, params, {}, PAGE)
    assert SPECS[action]["revision"] == 4
    assert not any(motor == "insert" and target == 0 for motor, target in plan["targets"])
    result = backend.execute(plan, context)
    assert result["status"] == "succeeded", result
    assert [item["motor"] for item in console.moves] == [motor for motor, _ in plan["targets"]]
    assert len(context.dispatches) == len(plan["targets"])
    assert all(item["stop_epoch"] == 2 for item in console.moves)
    assert console.state["motors"][2]["position_rotations"] == (
        2 if action == "transfer.position" else 1
    )
    assert not console.stopped


@pytest.mark.parametrize("action,params", XY_ACTIONS)
@pytest.mark.parametrize(
    "changes",
    [
        {"lower_limit_rotations": None},
        {"upper_limit_rotations": 0.05},
        {"positioning_ready": False},
        {"telemetry_fresh": False},
        {"fault": "encoder_error"},
    ],
)
def test_xy_actions_preserve_target_axis_limits_and_fault_checks(xy_live, action, params, changes):
    backend, console = xy_live
    motor = params.get("motor", "top")
    next(axis for axis in console.state["motors"] if axis["id"] == motor).update(changes)
    with pytest.raises(ValueError, match="Calibrate"):
        backend.prepare(action, params, {}, PAGE)
    assert not console.moves and console.takes == 0


@pytest.mark.parametrize("error", ["The stage axis has a fault.", "The target exceeds its limit."])
def test_xy_actions_preserve_native_command_rejection(xy_live, monkeypatch, error):
    backend, console = xy_live
    attempts = []

    def reject(device, page, action, params):
        assert device == "stage" and page == PAGE and action == "move_to_position_guarded"
        attempts.append(copy.deepcopy(params))
        return {"outcome": "refused", "error": error}

    monkeypatch.setattr(console, "command", reject)
    result = run(backend, "stage.coordinates", {"target": {"x": 1, "y": 2}})
    assert result["status"] == "failed" and result["error"] == error
    assert len(attempts) == 1 and not console.moves


def test_action_revisions_include_independent_xy_and_numeric_robot_jog():
    changed = {action for action, _ in XY_ACTIONS} | {"robot.jog"}
    microscope = {
        "turret.select",
        "microscope.objective_nudge",
        "microscope.focus_move",
        "microscope.rough_focus_5x",
    }
    autofocus = {
        "microscope.select_objective",
        "microscope.autofocus_sequence",
    }
    assert {action for action, spec in SPECS.items() if spec["revision"] == 4} == changed
    assert {action for action, spec in SPECS.items() if spec["revision"] == 6} == microscope
    assert {action for action, spec in SPECS.items() if spec["revision"] == 7} == autofocus
    assert {action for action, spec in SPECS.items() if spec["revision"] == 8} == {
        "microscope.autofocus"
    }
    assert all(
        spec["revision"] == 3
        for action, spec in SPECS.items()
        if action not in changed | microscope | autofocus | {"microscope.autofocus"}
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"lower_limit_rotations": None},
        {"upper_limit_rotations": None},
        {"upper_limit_rotations": 0.5},
        {"positioning_ready": False},
        {"telemetry_fresh": False},
        {"fault": "encoder_error"},
    ],
)
def test_per_axis_position_preserves_selected_axis_limits_and_fault_checks(live, changes):
    backend, console, _ = live
    console.state["motors"][1].update(changes)
    with pytest.raises(ValueError, match="Calibrate the stage"):
        backend.prepare("stage.position", {"motor": "top", "position": 1}, {}, PAGE)
    assert not console.moves and console.takes == 0


def test_per_axis_position_still_rejects_a_stop_after_preparation(live):
    backend, console, _ = live
    plan = backend.prepare("stage.position", {"motor": "top", "position": 1}, {}, PAGE)
    console.state["stop_epoch"] += 1
    with pytest.raises(ValueError, match="reference"):
        backend.execute(plan, Context())
    assert not console.moves and console.takes == 0


def test_per_axis_position_preserves_native_guard_when_stop_races_dispatch(live, monkeypatch):
    backend, console, _ = live
    attempted = []

    def native_guard(device, page, action, params):
        assert device == "stage" and page == PAGE and action == "move_to_position_guarded"
        attempted.append(copy.deepcopy(params))
        console.state["stop_epoch"] += 1
        assert params["stop_epoch"] != console.state["stop_epoch"]
        return {"outcome": "refused", "error": "The stage Stop epoch changed."}

    monkeypatch.setattr(console, "command", native_guard)
    result = run(backend, "stage.position", {"motor": "top", "position": 1})
    assert result["status"] == "failed" and "Stop epoch" in result["error"]
    assert len(attempted) == 1 and not console.moves


def test_per_axis_position_cancellation_stops_only_its_owned_lease(live):
    backend, console, _ = live
    context = Context()
    console.after_move = context.cancelled.set
    result = run(backend, "stage.position", {"motor": "top", "position": 1}, context)
    assert result["status"] in {"failed", "stopped"}
    assert len(console.moves) == 1 and console.stopped == ["stage"]


@pytest.mark.parametrize("failure", ["unknown_reply", "missing_id", "wrong_position"])
def test_per_axis_position_requires_confirmed_command_and_encoder_completion(
    live, monkeypatch, failure
):
    backend, console, _ = live
    if failure == "unknown_reply":
        console.outcome = "unknown"
    elif failure == "missing_id":
        command = console.command

        def missing_id(*args, **kwargs):
            result = command(*args, **kwargs)
            result.pop("command_id", None)
            return result

        monkeypatch.setattr(console, "command", missing_id)
    else:
        console.after_move = lambda: console.state["motors"][1].update(position_rotations=0.5)
    result = run(backend, "stage.position", {"motor": "top", "position": 1})
    assert result["status"] == ("failed" if failure == "wrong_position" else "unknown")
    assert len(console.moves) == 1
