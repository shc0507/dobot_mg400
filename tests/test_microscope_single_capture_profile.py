"""Four manual lens captures never require or issue motor motion."""

import copy

import pytest
from test_microscope_service import demo_rig as _demo_rig
from test_microscope_service import service_post
from test_microscope_turret import image_with_markers

from microscope import turret_calibration as turret

demo_rig = _demo_rig


def focused_profile():
    profile = turret.new_single_capture_profile("One picture per lens", "side")
    for ident in range(4):
        profile["captures"][str(ident)] = turret.capture(image_with_markers(ident), ident)
    return turret.validate_profile(profile)


def test_four_manual_capture_schema_and_store_round_trip(tmp_path):
    profile = focused_profile()
    assert turret.single_capture_ready(profile)
    assert len(profile["captures"]) == 4
    assert not {
        "clearance_captures",
        "safe_objective_id",
        "focus_response",
        "focus_offsets_pulses",
    } & set(profile)
    store = turret.TurretProfileStore(tmp_path)
    store.save(profile)
    assert store.select(profile["profile_id"]) == profile
    assert store.load(profile["profile_id"]) == profile
    assert turret.ProfileLocator(profile).locate(image_with_markers(2), target_id=2)["markers"]


def test_partial_manual_profile_is_readable_but_not_ready():
    profile = focused_profile()
    del profile["captures"]["0"]
    assert turret.validate_profile(profile) == profile
    assert not turret.single_capture_ready(profile)


@pytest.mark.parametrize("field", ["focus_response", "focus_offsets_pulses", "clearance_captures"])
def test_manual_profile_rejects_old_mechanical_fields(field):
    profile = focused_profile()
    profile[field] = {}
    with pytest.raises(ValueError, match="fields"):
        turret.validate_profile(profile)


def test_legacy_two_height_profile_is_not_silently_reinterpreted():
    legacy = turret.new_visual_profile("Two heights", "side")
    before = copy.deepcopy(legacy)
    assert turret.validate_profile(legacy) == before
    assert not turret.single_capture_ready(legacy)


def test_new_profile_requires_separate_initial_5x_capture():
    old = focused_profile()
    profile = turret.new_focus_profile("X and initial", "side")
    profile["captures"] = copy.deepcopy(old["captures"])
    assert not turret.is_complete(profile)
    profile["initial_5x_capture"] = copy.deepcopy(profile["captures"]["0"])
    assert turret.visual_ready(turret.validate_profile(profile))
    assert profile["schema_version"] == 5


def test_loading_legacy_profile_copies_x_into_new_draft_without_rewriting_saved_bytes(demo_rig):
    from test_microscope_visual_focus import teach_profile

    rig = demo_rig()
    old = teach_profile(rig)
    old["schema_version"] = 4
    del old["initial_5x_capture"]
    rig.service._turret_store.save(old)
    path = rig.service._turret_store.root / (old["profile_id"] + ".json")
    original = path.read_bytes()
    service_post(rig, "heartbeat")
    loaded = service_post(rig, "turret-calibration/load", {"id": old["profile_id"]})
    assert loaded.status_code == 200
    draft = loaded.json["draft"]
    assert draft["schema_version"] == 5 and draft["profile_id"] != old["profile_id"]
    assert draft["captures"] == old["captures"] and draft["initial_5x_capture"] is None
    assert path.read_bytes() == original
    assert service_post(rig, "turret-calibration/begin").status_code == 200
    before = len(rig.port.commands)
    recorded = service_post(rig, "turret-calibration/record_initial")
    assert recorded.status_code == 200
    assert recorded.json["draft"]["captures"] == old["captures"]
    assert not any(" MOVE " in c or c.endswith(" ENABLE") for c in rig.port.commands[before:])
    assert path.read_bytes() == original


def test_initial_capture_is_separate_and_rejects_non5x_marker(demo_rig):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    service_post(rig, "turret-calibration/begin", {"new": True})
    service_post(rig, "turret-calibration/record", {"objective_id": 0})
    saved_x = copy.deepcopy(rig.service._turret_draft["captures"]["0"])
    rig.port.sim.objective.position = 740
    assert service_post(rig, "turret-calibration/record_initial").status_code == 400
    assert rig.service._turret_draft["initial_5x_capture"] is None
    rig.port.sim.objective.position = 0
    rig.port.focus_origin += 40
    response = service_post(rig, "turret-calibration/record_initial")
    assert response.status_code == 200
    assert response.json["draft"]["captures"]["0"] == saved_x
    assert response.json["draft"]["initial_5x_capture"] != saved_x


def test_human_can_record_four_focused_lenses_in_any_order_without_powering_motors(demo_rig):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    prefix = "turret-calibration/"
    assert service_post(rig, prefix + "begin", {"name": "Manual four"}).status_code == 200
    before = len(rig.port.commands)
    for ident in (2, 0, 3, 1):
        rig.port.sim.objective.position = ident * 740
        rig.port.sim.focus.position = ident * 20
        result = service_post(rig, prefix + "record", {"objective_id": ident})
        assert result.status_code == 200, result.json
        assert not rig.port.sim.objective.enabled and not rig.port.sim.focus.enabled
    rig.port.sim.objective.position = 0
    result = service_post(rig, prefix + "record_initial", {})
    assert result.status_code == 200
    profile = result.json["draft"]
    assert turret.visual_ready(profile)
    assert service_post(rig, prefix + "finish").status_code == 200
    assert service_post(rig, prefix + "save").status_code == 200
    assert not any(
        " MOVE " in line or line.endswith(" ENABLE") for line in rig.port.commands[before:]
    )


@pytest.mark.parametrize(
    "action,args",
    [
        ("measure_response", {}),
        ("focus_move", {"steps": 10}),
        ("return_clearance", {"objective_id": 0}),
    ],
)
def test_manual_calibration_does_not_expose_motor_actions(demo_rig, action, args):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    before = list(rig.port.commands)
    result = service_post(rig, "turret-calibration/" + action, args)
    assert result.status_code == 400
    assert rig.port.commands == before


def test_rerecord_one_lens_preserves_other_manual_captures(demo_rig):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    prefix = "turret-calibration/"
    service_post(rig, prefix + "begin", {"name": "Manual four"})
    for ident in (0, 1):
        rig.port.sim.objective.position = ident * 740
        assert service_post(rig, prefix + "record", {"objective_id": ident}).status_code == 200
    first = copy.deepcopy(rig.service._turret_draft["captures"]["1"])
    rig.port.sim.objective.position = 0
    rig.port.sim.focus.position = 20
    result = service_post(rig, prefix + "record", {"objective_id": 0})
    assert result.status_code == 200
    assert result.json["draft"]["captures"]["1"] == first


def test_begin_attempts_both_disables_even_if_first_acknowledgement_fails(demo_rig, monkeypatch):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    calls = []
    disable_focus = rig.service._motor.focus_disable

    def fail_objective(**_kwargs):
        calls.append("objective")
        raise RuntimeError("Missing objective acknowledgement")

    def focus(**kwargs):
        calls.append("focus")
        return disable_focus(**kwargs)

    monkeypatch.setattr(rig.service._motor, "objective_disable", fail_objective)
    monkeypatch.setattr(rig.service._motor, "focus_disable", focus)
    result = service_post(rig, "turret-calibration/begin", {"name": "Manual"})
    assert result.status_code == 503
    assert calls[:2] == ["objective", "focus"]
    assert rig.service._turret_session is None


def test_record_refuses_if_focus_driver_is_still_powered(demo_rig):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    service_post(rig, "turret-calibration/begin", {"name": "Manual"})
    rig.port.sim.focus.enabled = True
    result = service_post(rig, "turret-calibration/record", {"objective_id": 0})
    assert result.status_code == 409 and not rig.service._turret_draft["captures"]
