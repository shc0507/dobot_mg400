"""Named rotation profiles retain source identity and reject invalid imports."""

import copy
import json
from uuid import uuid4

import pytest

from orchestrator.transfer_profiles import (
    TransferProfileStore,
    delete_endpoint,
    is_complete,
    new_profile,
    record_endpoint,
    record_position,
    validate_profile,
)


def source():
    return {"kind": "stage_encoder", "device_id": "test-stage", "name": "Stage encoders"}


def measurement(binding, insert=20):
    return {
        "source": binding,
        "received_at": "2026-11-12T12:00:00+00:00",
        "axes": {
            axis: {"value_rotations": value, "ready": True}
            for axis, value in (("x", 12.0), ("y", 15.0), ("insert", insert))
        },
    }


def complete_profile():
    binding = source()
    profile = new_profile("Transfer")
    profile = record_position(profile, "fully_out", binding, measurement(binding, 20))
    return record_position(profile, "fully_in", binding, measurement(binding, 130))


def test_record_requires_fresh_valid_xyz_and_same_source():
    binding = source()
    profile = new_profile("Positions")
    report = measurement(binding)
    recorded = record_position(profile, "Loading", binding, report)
    assert profile["positions"] == {}
    assert recorded["positions"]["Loading"]["position_rotations"] == {
        "x": 12,
        "y": 15,
        "insert": 20,
    }
    for axis in ("x", "y", "insert"):
        for status in (False, None):
            invalid = copy.deepcopy(report)
            invalid["axes"][axis]["ready"] = status
            with pytest.raises(ValueError, match="Calibrate"):
                record_position(recorded, "Loading", binding, invalid)
    different = {**source(), "device_id": "another-stage"}
    with pytest.raises(ValueError, match="same stage encoder"):
        record_position(recorded, "Pickup", different, measurement(different, 130))
    report["source"] = different
    with pytest.raises(ValueError, match="does not match"):
        record_position(profile, "Loading", binding, report)


@pytest.mark.parametrize("value", [True, None, "20", float("nan"), float("inf"), -float("inf")])
def test_invalid_coordinates_are_rejected(value):
    profile = complete_profile()
    profile["positions"]["fully_in"]["position_rotations"]["insert"] = value
    with pytest.raises(ValueError, match="finite"):
        validate_profile(profile)


def test_position_shape_and_source_are_checked():
    profile = complete_profile()
    for mutate in (
        lambda p: p.update(source=None),
        lambda p: p.update(schema_version=True),
        lambda p: p.update(id="../../elsewhere"),
        lambda p: p.update(extra="unsupported"),
        lambda p: p["positions"]["fully_in"].update(captured_at="bad"),
    ):
        invalid = copy.deepcopy(profile)
        mutate(invalid)
        with pytest.raises(ValueError):
            validate_profile(invalid)


def test_store_selection_upload_delete_and_restart(tmp_path):
    store = TransferProfileStore(tmp_path)
    profile = complete_profile()
    saved = store.save(profile)
    assert saved["id"] != profile["id"]
    assert store.selection() is None
    store.select(saved["id"])
    restarted = TransferProfileStore(tmp_path)
    assert restarted.selected() == saved
    imported = restarted.upload(json.dumps(saved).encode())
    assert imported["id"] != saved["id"]
    assert imported["positions"] == saved["positions"]
    assert restarted.selection() == saved["id"]
    before = restarted.list()
    for invalid in (b"{}", b"\xff", b"[", b" " * (128 * 1024 + 1)):
        with pytest.raises(ValueError):
            restarted.upload(invalid)
        assert restarted.list() == before
        assert restarted.selected() == saved
    restarted.delete(saved["id"])
    assert restarted.selection() is None
    assert restarted.export(imported["id"]) == imported
    assert is_complete(imported)


def test_empty_profile_can_be_saved_and_selected_for_editing(tmp_path):
    store = TransferProfileStore(tmp_path)
    saved = store.save(new_profile("Not finished"))
    assert not store.list()[0]["complete"]
    assert store.select(saved["id"]) == saved
    assert store.list()[0]["position_count"] == 0
    assert store.load(saved["id"]) == saved


def test_legacy_profiles_normalize_without_rewriting_or_inventing_xy(tmp_path):
    profile = new_profile("Legacy")
    profile.pop("unit")
    profile.pop("stage_positions")
    profile["schema_version"] = 1
    profile.pop("positions")
    profile["source"] = {
        "camera": "camera1",
        "device_id": "test-camera",
        "profile_id": str(uuid4()),
        "name": "Optical",
    }
    profile["endpoints"] = {
        name: {
            "position_mm": {"x": None, "y": 2, "insert": value},
            "captured_at": profile["created_at"],
            "image_sha256": "a" * 64,
        }
        for name, value in (("fully_out", 0), ("fully_in", 100))
    }
    path = tmp_path / "profiles" / (profile["id"] + ".json")
    path.parent.mkdir()
    payload = json.dumps(profile).encode()
    path.write_bytes(payload)
    store = TransferProfileStore(tmp_path)
    loaded = store.load(profile["id"])
    assert loaded["schema_version"] == 2
    assert set(loaded["positions"]) == {"Fully out", "Fully in"}
    assert loaded["positions"]["Fully out"]["position_mm"]["x"] is None
    assert path.read_bytes() == payload
    assert store.list()[0]["position_count"] == 2
    assert store.list()[0]["compatible"] is False
    assert store.list()[0]["unit"] == "mm"
    with pytest.raises(ValueError, match="legacy millimetres"):
        record_position(loaded, "New", source(), measurement(source()))
    assert not store.list()[0]["complete"]
    assert store.select(profile["id"]) == loaded
    assert store.save(loaded)["positions"] == loaded["positions"]


@pytest.mark.parametrize("name", ["", " ", "a" * 121, "a\nb", None, 1])
def test_position_names_are_validated(name):
    binding = source()
    with pytest.raises(ValueError, match="Position name"):
        record_position(new_profile("Positions"), name, binding, measurement(binding))


def test_position_limit_and_delete():
    from orchestrator.transfer_profiles import delete_position

    binding = source()
    profile = new_profile("Positions")
    for index in range(64):
        profile = record_position(profile, str(index), binding, measurement(binding))
    with pytest.raises(ValueError, match="64"):
        record_position(profile, "extra", binding, measurement(binding))
    updated = record_position(profile, "0", binding, measurement(binding, 30))
    assert updated["positions"]["0"]["position_rotations"]["insert"] == 30
    removed = delete_position(updated, "0")
    assert "0" not in removed["positions"]
    assert "0" in updated["positions"]


def test_stage_positions_persist_with_named_positions_and_selection(tmp_path):
    binding = source()
    profile = complete_profile()
    profile = record_endpoint(profile, "out_stage", binding, measurement(binding, 4))
    profile = record_endpoint(profile, "microscope", binding, measurement(binding, 90))
    assert profile["schema_version"] == 5
    assert len(profile["positions"]) == 2
    assert profile["stage_positions"]["out_stage"]["position_rotations"] == {"x": 12, "y": 15}
    store = TransferProfileStore(tmp_path)
    saved = store.save(profile)
    store.select(saved["id"])
    restarted = TransferProfileStore(tmp_path)
    assert restarted.selected()["stage_positions"] == profile["stage_positions"]
    imported = restarted.upload(json.dumps(restarted.export(saved["id"])).encode())
    assert imported["stage_positions"] == profile["stage_positions"]
    assert restarted.list()[0]["stage_position_count"] == 2
    removed = delete_endpoint(imported, "out_stage")
    assert set(removed["stage_positions"]) == {"microscope"}
    assert len(imported["stage_positions"]) == 2
    assert is_complete(
        record_endpoint(new_profile("XY"), "out_stage", binding, measurement(binding))
    )


@pytest.mark.parametrize("value", [True, None, "20", float("nan"), float("inf")])
def test_invalid_stage_coordinates_are_rejected_before_save(value):
    profile = record_endpoint(new_profile("Stage"), "out_stage", source(), measurement(source()))
    profile["stage_positions"]["out_stage"]["position_rotations"]["x"] = value
    with pytest.raises(ValueError, match="finite"):
        validate_profile(profile)


def test_stage_position_shape_and_source_are_validated():
    profile = record_endpoint(new_profile("Stage"), "out_stage", source(), measurement(source()))
    for mutate in (
        lambda p: p.update(source=None),
        lambda p: p["stage_positions"].update(other=p["stage_positions"]["out_stage"]),
        lambda p: p["stage_positions"]["out_stage"].update(captured_at="bad"),
        lambda p: p["stage_positions"]["out_stage"]["position_rotations"].pop("x"),
        lambda p: p["stage_positions"]["out_stage"]["position_rotations"].update(insert=1),
        lambda p: p["stage_positions"]["out_stage"].update(extra=1),
    ):
        invalid = copy.deepcopy(profile)
        mutate(invalid)
        with pytest.raises(ValueError):
            validate_profile(invalid)
    for target in ("other", "fully_out", "fully_in"):
        with pytest.raises(ValueError, match="Out stage or Microscope"):
            record_endpoint(profile, target, source(), measurement(source()))
    assert set(profile["stage_positions"]) == {"out_stage"}


def test_stage_record_requires_only_xy_feedback():
    binding = source()
    report = measurement(binding)
    report["axes"].pop("insert")
    profile = record_endpoint(new_profile("XY"), "microscope", binding, report)
    assert profile["stage_positions"]["microscope"]["position_rotations"] == {"x": 12, "y": 15}
    for axis in ("x", "y"):
        invalid = copy.deepcopy(report)
        invalid["axes"][axis]["ready"] = False
        with pytest.raises(ValueError, match="Calibrate"):
            record_endpoint(profile, "out_stage", binding, invalid)


def test_schema_three_loads_without_inventing_stage_targets_or_rewriting(tmp_path):
    profile = complete_profile()
    profile["schema_version"] = 3
    profile.pop("stage_positions")
    path = tmp_path / "profiles" / (profile["id"] + ".json")
    path.parent.mkdir()
    payload = json.dumps(profile).encode()
    path.write_bytes(payload)
    store = TransferProfileStore(tmp_path)
    loaded = store.load(profile["id"])
    assert loaded["schema_version"] == 5
    assert loaded["stage_positions"] == {}
    assert loaded["positions"] == profile["positions"]
    assert path.read_bytes() == payload


def test_schema_four_preserves_endpoints_as_collision_safe_named_records(tmp_path):
    profile = complete_profile()
    capture = copy.deepcopy(profile["positions"]["fully_in"])
    profile["schema_version"] = 4
    profile.pop("stage_positions")
    profile["positions"] = {str(index): copy.deepcopy(capture) for index in range(62)}
    profile["positions"].update(
        {"Legacy Extract": copy.deepcopy(capture), "Legacy Extract (2)": copy.deepcopy(capture)}
    )
    profile["transfer_endpoints"] = {
        "fully_out": copy.deepcopy(capture),
        "fully_in": copy.deepcopy(capture),
    }
    profile["transfer_endpoints"]["fully_out"]["position_rotations"]["insert"] = 4
    path = tmp_path / "profiles" / (profile["id"] + ".json")
    path.parent.mkdir()
    payload = json.dumps(profile).encode()
    path.write_bytes(payload)
    store = TransferProfileStore(tmp_path)
    loaded = store.load(profile["id"])
    assert loaded["schema_version"] == 5
    assert loaded["stage_positions"] == {}
    assert len(loaded["positions"]) == 66
    assert loaded["positions"]["Legacy Extract (3)"] == profile["transfer_endpoints"]["fully_out"]
    assert loaded["positions"]["Legacy Insert"] == profile["transfer_endpoints"]["fully_in"]
    assert all(loaded["positions"][name] == value for name, value in profile["positions"].items())
    assert path.read_bytes() == payload
    assert store.save(loaded)["positions"] == loaded["positions"]
