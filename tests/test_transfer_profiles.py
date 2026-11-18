"""Named positions retain their optical source and reject invalid imports."""

import copy
import json
from uuid import uuid4

import pytest

from orchestrator.transfer_profiles import (
    TransferProfileStore,
    is_complete,
    new_profile,
    record_position,
    validate_profile,
)


def source():
    return {
        "camera": "camera1",
        "device_id": "demo:camera1",
        "profile_id": str(uuid4()),
        "name": "Mock stage",
    }


def measurement(binding, insert=20):
    return {
        "camera": binding["camera"],
        "profile_id": binding["profile_id"],
        "received_at": "2026-11-12T12:00:00+00:00",
        "image_sha256": "a" * 64,
        "axes": {
            axis: {"value_mm": value, "status": "provisional_interpolation"}
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
    assert recorded["positions"]["Loading"]["position_mm"] == {"x": 12, "y": 15, "insert": 20}
    for axis in ("x", "y", "insert"):
        for status in ("unknown", "extrapolated"):
            invalid = copy.deepcopy(report)
            invalid["axes"][axis]["status"] = status
            with pytest.raises(ValueError, match="visible"):
                record_position(recorded, "Loading", binding, invalid)
    different = source()
    with pytest.raises(ValueError, match="same Position Finder"):
        record_position(recorded, "Pickup", different, measurement(different, 130))
    report["profile_id"] = str(uuid4())
    with pytest.raises(ValueError, match="does not match"):
        record_position(profile, "Loading", binding, report)


@pytest.mark.parametrize("value", [True, None, "20", float("nan"), float("inf"), -float("inf")])
def test_invalid_coordinates_are_rejected(value):
    profile = complete_profile()
    profile["positions"]["fully_in"]["position_mm"]["insert"] = value
    with pytest.raises(ValueError, match="finite"):
        validate_profile(profile)


def test_position_shape_and_source_are_checked():
    profile = complete_profile()
    for mutate in (
        lambda p: p.update(source=None),
        lambda p: p.update(schema_version=True),
        lambda p: p.update(id="../../elsewhere"),
        lambda p: p.update(extra="unsupported"),
        lambda p: p["positions"]["fully_in"].update(image_sha256="bad"),
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
    profile = complete_profile()
    profile["schema_version"] = 1
    profile["endpoints"] = profile.pop("positions")
    profile["endpoints"]["fully_out"]["position_mm"]["x"] = None
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
    assert updated["positions"]["0"]["position_mm"]["insert"] == 30
    removed = delete_position(updated, "0")
    assert "0" not in removed["positions"]
    assert "0" in updated["positions"]
