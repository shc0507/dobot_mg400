"""Human focus settings persist independently of manual lens profiles."""

import json

import pytest
from test_microscope_service import demo_rig as _demo_rig
from test_microscope_service import dispatch, install_visual_profile, service_post

demo_rig = _demo_rig


def test_travel_cap_persists_across_service_restart_without_device_motion(demo_rig):
    rig = demo_rig()
    before = list(rig.port.commands)
    assert rig.client.get("/api/focus-travel-limit").json == {"focus_travel_limit_pulses": 20000}
    assert service_post(rig, "focus-travel-limit", {"pulses": 2500}).status_code == 409
    service_post(rig, "heartbeat")
    saved = service_post(rig, "focus-travel-limit", {"pulses": 2500})
    assert saved.status_code == 200 and saved.json == {"focus_travel_limit_pulses": 2500}
    assert rig.port.commands == before
    replacement = demo_rig()
    assert replacement.client.get("/api/focus-travel-limit").json == saved.json
    assert replacement.service.snapshot()["focus_reference"] == "relative_command"


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1000", None, 2**31])
def test_invalid_travel_cap_is_rejected_without_overwriting(value, demo_rig):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    assert service_post(rig, "focus-travel-limit", {"pulses": value}).status_code == 400
    assert rig.service.focus_travel_limit() == {"focus_travel_limit_pulses": 20000}
    assert not (rig.service._turret_store.root.parent / "focus-travel-limit.json").exists()


@pytest.mark.parametrize("value", [1, 2**31 - 1])
def test_firmware_integer_boundaries_can_be_saved_without_motion(value, demo_rig):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    before = list(rig.port.commands)
    assert service_post(rig, "focus-travel-limit", {"pulses": value}).status_code == 200
    assert rig.port.commands == before


def test_running_command_keeps_its_frozen_cap(demo_rig, monkeypatch):
    rig = demo_rig()
    install_visual_profile(rig)
    service_post(rig, "heartbeat")
    service_post(rig, "focus-travel-limit", {"pulses": 123})
    original = rig.service._prepare_visual
    observed = []

    def during(job):
        observed.append(service_post(rig, "focus-travel-limit", {"pulses": 5000}).status_code)
        assert job.focus_travel_limit == 123
        return original(job)

    monkeypatch.setattr(rig.service, "_prepare_visual", during)
    result = dispatch(rig, "focus_move", steps=10)
    assert result["last_command"]["result"] == "ok"
    assert observed == [409]
    assert result["visual_focus"]["travel_limit_pulses"] == 123
    assert result["visual_focus"]["travel_used_pulses"] == 10


@pytest.mark.parametrize("encoded", ['{"pulses":0}', '{"pulses":true}', "{}", "x" * 513])
def test_invalid_stored_cap_does_not_silently_increase_travel(encoded, demo_rig):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    assert service_post(rig, "focus-travel-limit", {"pulses": 100}).status_code == 200
    path = rig.service._turret_store.root.parent / "focus-travel-limit.json"
    path.write_text(encoded)
    with pytest.raises(ValueError):
        rig.service._load_focus_travel()
    assert rig.service.focus_travel_limit() == {"focus_travel_limit_pulses": 100}


def test_separate_5x_range_persists_without_changing_manual_cap(demo_rig):
    rig = demo_rig()
    settings = rig.client.get("/api/autofocus-settings").json
    assert settings["five_x_half_range_pulses"] == 5000
    assert settings["ten_x_half_range_pulses"] == 5000
    assert settings["twenty_x_half_range_pulses"] == 5000
    assert settings["fifty_x_half_range_pulses"] == 3000
    assert "other_half_range_pulses" not in settings
    assert (
        service_post(rig, "autofocus-settings", {"five_x_half_range_pulses": 7000}).status_code
        == 409
    )
    service_post(rig, "heartbeat")
    before = list(rig.port.commands)
    saved = service_post(rig, "autofocus-settings", {"five_x_half_range_pulses": 7000})
    assert saved.status_code == 200
    assert rig.port.commands == before
    assert rig.service.focus_travel_limit()["focus_travel_limit_pulses"] == 20000
    assert demo_rig().client.get("/api/autofocus-settings").json == saved.json


@pytest.mark.parametrize("value", [0, 3, True, 5.5, "5000", None, 1000001])
@pytest.mark.parametrize("lens", ["five_x", "ten_x", "twenty_x", "fifty_x"])
def test_invalid_autofocus_range_is_not_saved(demo_rig, value, lens):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    response = service_post(rig, "autofocus-settings", {lens + "_half_range_pulses": value})
    assert response.status_code == 400
    assert rig.service.autofocus_settings()["five_x_half_range_pulses"] == 5000


def test_per_lens_partial_updates_merge_and_restart_preserves_all_ranges(demo_rig):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    before = list(rig.port.commands)
    first = service_post(rig, "autofocus-settings", {"ten_x_half_range_pulses": 1200})
    assert first.status_code == 200
    changed = {"twenty_x_half_range_pulses": 800, "fifty_x_half_range_pulses": 400}
    saved = service_post(rig, "autofocus-settings", changed)
    assert saved.status_code == 200
    expected = {
        "five_x_half_range_pulses": 5000,
        "ten_x_half_range_pulses": 1200,
        "twenty_x_half_range_pulses": 800,
        "fifty_x_half_range_pulses": 400,
    }
    assert {key: saved.json[key] for key in expected} == expected
    path = rig.service._turret_store.root.parent / "autofocus-settings.json"
    assert json.loads(path.read_text(encoding="utf-8")) == expected
    assert demo_rig().client.get("/api/autofocus-settings").json == saved.json
    assert rig.port.commands == before


def test_legacy_five_x_setting_loads_without_rewrite_and_next_save_merges_defaults(demo_rig):
    rig = demo_rig()
    path = rig.service._turret_store.root.parent / "autofocus-settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"five_x_half_range_pulses": 6500}\n'
    path.write_bytes(original)
    replacement = demo_rig()
    settings = replacement.client.get("/api/autofocus-settings").json
    assert settings["five_x_half_range_pulses"] == 6500
    assert settings["ten_x_half_range_pulses"] == 5000
    assert settings["twenty_x_half_range_pulses"] == 5000
    assert settings["fifty_x_half_range_pulses"] == 3000
    assert path.read_bytes() == original
    service_post(replacement, "heartbeat")
    saved = service_post(replacement, "autofocus-settings", {"ten_x_half_range_pulses": 1000})
    assert saved.status_code == 200
    assert saved.json["five_x_half_range_pulses"] == 6500
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 4


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"other_half_range_pulses": 1000},
        {"unknown": 10},
        {"ten_x_half_range_pulses": 1000, "fifty_x_half_range_pulses": 0},
    ],
)
def test_invalid_autofocus_subset_does_not_replace_saved_values(demo_rig, payload):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    valid = {"ten_x_half_range_pulses": 700}
    assert service_post(rig, "autofocus-settings", valid).status_code == 200
    path = rig.service._turret_store.root.parent / "autofocus-settings.json"
    original = path.read_bytes()
    assert service_post(rig, "autofocus-settings", payload).status_code == 400
    assert path.read_bytes() == original


def test_failed_atomic_settings_save_preserves_file_and_current_ranges(demo_rig, monkeypatch):
    rig = demo_rig()
    service_post(rig, "heartbeat")
    saved = service_post(rig, "autofocus-settings", {"ten_x_half_range_pulses": 700})
    assert saved.status_code == 200
    path = rig.service._turret_store.root.parent / "autofocus-settings.json"
    original = path.read_bytes()

    def unavailable(*_args):
        raise OSError("Storage unavailable")

    monkeypatch.setattr("microscope.focus_travel.os.replace", unavailable)
    with pytest.raises(OSError, match="Storage unavailable"):
        rig.service.set_autofocus_settings({"twenty_x_half_range_pulses": 1200}, "operator")
    assert rig.service.autofocus_settings() == saved.json
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".autofocus-*"))


def test_saved_legacy_defaults_survive_restart_without_rewrite(demo_rig):
    rig = demo_rig()
    parent = rig.service._turret_store.root.parent
    parent.mkdir(parents=True, exist_ok=True)
    files = {
        "focus-travel-limit.json": b'{"pulses":1000}\n',
        "autofocus-settings.json": (
            b'{"five_x_half_range_pulses":5000,"ten_x_half_range_pulses":300,'
            b'"twenty_x_half_range_pulses":300,"fifty_x_half_range_pulses":300}\n'
        ),
    }
    for name, encoded in files.items():
        (parent / name).write_bytes(encoded)
    replacement = demo_rig()
    assert replacement.service.focus_travel_limit() == {"focus_travel_limit_pulses": 1000}
    settings = replacement.service.autofocus_settings()
    assert settings["five_x_half_range_pulses"] == 5000
    assert settings["ten_x_half_range_pulses"] == 300
    assert settings["twenty_x_half_range_pulses"] == 300
    assert settings["fifty_x_half_range_pulses"] == 300
    for name, encoded in files.items():
        assert (parent / name).read_bytes() == encoded
