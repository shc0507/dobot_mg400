"""Manual turret teaching uses only simulated motors and camera frames."""

import copy
import json
import threading
from dataclasses import replace

import cv2
import pytest
from test_microscope_service import (
    demo_rig as _demo_rig,
)
from test_microscope_service import (
    dispatch,
    establish_manual_reference,
    service_post,
    wait_command,
)

from microscope.turret_calibration import new_profile

demo_rig = _demo_rig


def turret(rig, action, body=None, actor="operator"):
    return service_post(rig, "turret-calibration/" + action, body, actor)


def begin(rig, *, reference=True):
    if reference:
        establish_manual_reference(rig)
    else:
        assert service_post(rig, "manual/heartbeat").status_code == 200
    response = turret(rig, "begin", {"name": "Bench turret"})
    assert response.status_code == 200, response.json
    return response.json


def teach_all(rig):
    for ident in range(4):
        with rig.port.lock:
            rig.port.sim.objective.position = ident * 740
        response = turret(rig, "record", {"objective_id": ident})
        assert response.status_code == 200, response.json
    assert turret(rig, "finish").status_code == 200
    response = turret(rig, "save", {"name": "Saved turret"})
    assert response.status_code == 200, response.json
    return response.json["draft"]


@pytest.mark.parametrize("known", [False, True])
def test_begin_only_disables_turret_and_keeps_focus_unchanged(demo_rig, known):
    rig = demo_rig()
    if known:
        establish_manual_reference(rig)
        assert dispatch(rig, "focus_move", steps=30)["last_command"]["result"] == "ok"
    else:
        assert service_post(rig, "manual/heartbeat").status_code == 200
        rig.port.sim.focus.position = 30
    rig.service._objective_id, rig.service._objective_offset = 0, 0
    before = len(rig.port.commands)
    response = turret(rig, "begin", {"name": "Manual position"})
    assert response.status_code == 200, response.json
    commands = rig.port.commands[before:]
    assert [command for command in commands if command != "STATUS"] == ["OBJ DISABLE"]
    assert rig.port.sim.focus.position == 30
    assert response.json["draft"]["focus_position"] == (30 if known else None)
    assert response.json["session"]["focus_position"] == 30
    assert response.json["session"]["focus_zeroed"] is known
    assert response.json["alignment"] == "image_coordinates"
    assert response.json["dictionary"] == "DICT_4X4_50"
    assert "reference" not in response.json
    assert not response.json["compatible_for_switch"]
    assert rig.service.snapshot()["objective_id"] is None
    assert rig.service.snapshot()["objective_offset_steps"] is None
    assert rig.service.snapshot()["busy"]


def test_begin_requires_lease_stationary_axes_and_no_recording(demo_rig, monkeypatch):
    rig = demo_rig()
    assert turret(rig, "begin", {"name": "x"}).status_code == 409
    establish_manual_reference(rig)
    rig.service._recording = object()
    assert turret(rig, "begin", {"name": "x"}).status_code == 409
    rig.service._recording = None
    status = rig.service._motor.read_status()
    monkeypatch.setattr(
        rig.service,
        "_fresh_status",
        lambda _: replace(status, focus=replace(status.focus, moving=True)),
    )
    assert turret(rig, "begin", {"name": "x"}).status_code == 409


def test_session_blocks_jobs_and_other_clients(demo_rig):
    rig = demo_rig()
    begin(rig)
    for action, args in (("focus_move", {"steps": 1}), ("select_objective", {"id": 1})):
        assert service_post(rig, "command", {"action": action, **args}).status_code == 409
    assert service_post(rig, "manual/set_focus_zero", {"confirm": True}).status_code == 409
    assert turret(rig, "record", {"objective_id": 0}, actor="other").status_code == 409
    assert turret(rig, "upload", {"profile": "{}"}).status_code == 409


def test_record_uses_only_lens_marker_and_preserves_capture_on_error(demo_rig):
    rig = demo_rig()
    begin(rig)
    rig.service._side.open()
    frame = rig.service._side.get_frame_packet()[2]
    rig.service._side.close()
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))
    corners, ids, _ = detector.detectMarkers(frame)
    assert ids.flatten().tolist() == [0]
    before = len(rig.port.commands)
    first = turret(rig, "record", {"objective_id": 0})
    assert first.status_code == 200, first.json
    capture = copy.deepcopy(first.json["draft"]["captures"]["0"])
    assert first.json["draft"]["schema_version"] == 2
    assert first.json["draft"]["dictionary"] == "DICT_4X4_50"
    assert set(capture) == {"shape", "objective_corners", "preview_jpeg"}
    for actual, expected in zip(capture["objective_corners"], corners[0].reshape(4, 2).tolist()):
        assert actual == pytest.approx(expected, abs=1)
    failed = turret(rig, "record", {"objective_id": 1})
    assert failed.status_code == 400
    assert rig.service.turret_calibration_state()["draft"]["captures"] == {"0": capture}
    assert turret(rig, "record", {"objective_id": 0}).status_code == 200
    assert set(rig.port.commands[before:]) == {"STATUS"}
    assert not rig.service._side.is_open


def test_focus_change_and_stale_frames_preserve_draft(demo_rig, monkeypatch):
    rig = demo_rig()
    begin(rig)
    assert turret(rig, "record", {"objective_id": 0}).status_code == 200
    before = copy.deepcopy(rig.service._turret_draft)
    rig.port.sim.focus.position = 1
    assert turret(rig, "record", {"objective_id": 0}).status_code == 409
    rig.port.sim.focus.position = 0
    rig.service._side.open()
    cached = rig.service._side.get_frame_packet()
    rig.service._side.close()
    monkeypatch.setattr(rig.service._side, "get_frame_packet", lambda **_: cached)
    assert turret(rig, "record", {"objective_id": 0}).status_code == 409
    assert rig.service._turret_draft == before


@pytest.mark.parametrize("exit_action", ["finish", "cancel", "release", "expiry", "stop", "close"])
def test_all_exit_paths_end_session_without_enabling_or_moving(demo_rig, exit_action):
    rig = demo_rig()
    begin(rig)
    before = len(rig.port.commands)
    if exit_action in {"finish", "cancel"}:
        assert turret(rig, exit_action).status_code == 200
    elif exit_action == "release":
        assert service_post(rig, "manual/release").status_code == 200
    elif exit_action == "expiry":
        rig.clock.now += 4
        rig.service.lease.poll()
    elif exit_action == "stop":
        rig.service.stop("any-client", 1)
    else:
        rig.service.close()
    assert rig.service._stop_done.wait(1)
    assert rig.service._turret_session is None
    assert rig.service._turret_draft is not None
    assert not any("MOVE" in item or "ENABLE" in item for item in rig.port.commands[before:])


def test_cancel_during_capture_cannot_publish_late_capture(demo_rig, monkeypatch):
    rig = demo_rig()
    begin(rig)
    entered, release = threading.Event(), threading.Event()
    original = rig.service._side.get_frame_packet

    def blocked(**kwargs):
        if kwargs.get("wait") is not False:
            entered.set()
            assert release.wait(2)
        return original(**kwargs)

    monkeypatch.setattr(rig.service._side, "get_frame_packet", blocked)
    results = []
    thread = threading.Thread(
        target=lambda: results.append(turret(rig, "record", {"objective_id": 0}))
    )
    thread.start()
    assert entered.wait(2)
    assert turret(rig, "cancel").status_code == 200
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert results[0].status_code == 409
    assert rig.service._turret_draft["captures"] == {}
    assert not rig.service._side.is_open


def test_complete_profile_save_select_export_upload_and_delete(demo_rig):
    rig = demo_rig()
    begin(rig)
    profile = teach_all(rig)
    ident = profile["profile_id"]
    selected = turret(rig, "select", {"id": ident})
    assert selected.status_code == 200 and selected.json["selected_profile"] == ident
    assert selected.json["compatible_for_switch"]
    exported = rig.client.get("/api/turret-calibration/profile/" + ident)
    assert exported.status_code == 200 and exported.json == profile
    assert turret(rig, "upload", {"profile": exported.get_data(as_text=True)}).status_code == 200
    assert turret(rig, "delete", {"id": ident}).status_code == 200
    rejected = service_post(rig, "command", {"action": "select_objective", "id": 0})
    assert rejected.status_code == 409
    assert "Select a complete" in rejected.json["error"]


def test_unknown_recorded_focus_can_select_but_live_focus_zero_is_required(demo_rig):
    rig = demo_rig()
    begin(rig, reference=False)
    profile = teach_all(rig)
    assert profile["focus_position"] is None
    response = turret(rig, "select", {"id": profile["profile_id"]})
    assert response.status_code == 200
    assert response.json["selected_profile"] == profile["profile_id"]
    assert response.json["compatible_for_switch"]
    assert response.json["draft"]["focus_position"] is None
    before = list(rig.port.commands)
    response = service_post(rig, "command", {"action": "select_objective", "id": 0})
    assert response.status_code == 409 and "focus position unknown" in response.json["error"]
    assert rig.port.commands == before


def test_selected_profile_from_another_camera_refuses_before_motion(demo_rig):
    rig = demo_rig()
    begin(rig)
    profile = teach_all(rig)
    rig.service._turret_store.save(dict(profile, camera="another-camera"))
    assert turret(rig, "select", {"id": profile["profile_id"]}).status_code == 200
    assert not rig.service.turret_calibration_state()["compatible_for_switch"]
    before = list(rig.port.commands)
    response = service_post(rig, "command", {"action": "select_objective", "id": 0})
    assert response.status_code == 409 and "side camera" in response.json["error"]
    assert rig.port.commands == before


def test_upload_large_profile_does_not_raise_other_route_limits(demo_rig):
    rig = demo_rig()
    service_post(rig, "manual/heartbeat")
    profile = new_profile("Import", "demo", None)
    text = json.dumps(profile, indent=2) + " " * 100_000
    assert turret(rig, "upload", {"profile": text}).status_code == 200
    assert turret(rig, "begin", {"name": "x" * 100_000}).status_code == 413
    assert turret(rig, "upload", {"profile": " " * (2 * 1024 * 1024 + 1)}).status_code == 400
    assert turret(rig, "record", {"objective_id": True}).status_code == 400


def test_resume_keeps_captures_but_new_profile_clears_them(demo_rig):
    rig = demo_rig()
    begin(rig)
    assert turret(rig, "record", {"objective_id": 0}).status_code == 200
    assert turret(rig, "finish").status_code == 200
    assert turret(rig, "begin").json["draft"]["captures"]
    assert turret(rig, "finish").status_code == 200
    assert turret(rig, "begin", {"name": "New", "new": True}).json["draft"]["captures"] == {}


def test_saved_draft_loads_without_changing_active_selection(demo_rig):
    rig = demo_rig()
    begin(rig)
    assert turret(rig, "record", {"objective_id": 0}).status_code == 200
    assert turret(rig, "finish").status_code == 200
    saved = turret(rig, "save").json["draft"]
    assert turret(rig, "begin", {"name": "Another"}).status_code == 200
    assert turret(rig, "finish").status_code == 200
    loaded = turret(rig, "load", {"id": saved["profile_id"]})
    assert loaded.status_code == 200 and loaded.json["draft"] == saved
    assert loaded.json["selected_profile"] is None
    assert turret(rig, "begin").json["draft"] == saved


def test_switch_uses_selected_snapshot_and_never_legacy_locator(demo_rig, monkeypatch):
    rig = demo_rig()
    begin(rig)
    profile = teach_all(rig)
    assert turret(rig, "select", {"id": profile["profile_id"]}).status_code == 200
    entered, release = threading.Event(), threading.Event()
    observed = []

    def select(job, motor):
        entered.set()
        assert release.wait(2)
        observed.append(job.turret_profile)
        return {"objective_id": 0}

    monkeypatch.setattr(rig.service, "_select", select)
    response = service_post(rig, "command", {"action": "select_objective", "id": 0})
    assert response.status_code == 202
    assert entered.wait(2)
    changed = dict(profile, name="Changed on disk")
    rig.service._turret_store.save(changed)
    release.set()
    assert wait_command(rig, response.json["command_id"])["last_command"]["result"] == "ok"
    assert observed == [profile]


def test_selected_profile_missing_objective_refuses_before_any_move(demo_rig, monkeypatch):
    rig = demo_rig()
    begin(rig)
    profile = teach_all(rig)
    assert turret(rig, "select", {"id": profile["profile_id"]}).status_code == 200
    original = rig.service._side.get_frame_packet

    def no_objective(**kwargs):
        frame_id, captured_at, pixels = original(**kwargs)
        pixels[:] = 255
        return frame_id, captured_at, pixels

    monkeypatch.setattr(rig.service._side, "get_frame_packet", no_objective)
    monkeypatch.setattr(rig.service, "_locator", lambda: pytest.fail("Legacy locator was used"))
    before = len(rig.port.commands)
    state = dispatch(rig, "select_objective", id=0)
    assert state["last_command"]["result"] == "failed"
    assert not state["locked"] and state["focus_zero_known"]
    assert not any("MOVE" in command for command in rig.port.commands[before:])


@pytest.mark.parametrize("recorded_focus", [None, 0, 30])
def test_selected_profile_switches_with_only_lens_markers(demo_rig, monkeypatch, recorded_focus):
    rig = demo_rig()
    if recorded_focus is not None:
        establish_manual_reference(rig)
        if recorded_focus:
            assert (
                dispatch(rig, "focus_move", steps=recorded_focus)["last_command"]["result"] == "ok"
            )
        response = turret(rig, "begin", {"name": "Bench turret"})
        assert response.status_code == 200, response.json
    else:
        begin(rig, reference=False)
    profile = teach_all(rig)
    assert profile["focus_position"] == recorded_focus
    assert turret(rig, "select", {"id": profile["profile_id"]}).status_code == 200
    if recorded_focus is None:
        establish_manual_reference(rig)
    if recorded_focus != 30:
        assert dispatch(rig, "focus_move", steps=30)["last_command"]["result"] == "ok"
    monkeypatch.setattr(rig.service, "_locator", lambda: pytest.fail("Legacy locator was used"))
    before = len(rig.port.commands)
    state = dispatch(rig, "select_objective", id=2)
    assert state["last_command"]["result"] == "ok", state
    assert state["objective_id"] == 2 and state["focus_position_pulses"] == 0
    assert state["focus_zero_known"]
    assert rig.service._turret_store.selected()["focus_position"] == recorded_focus
    assert len(state["last_result"]["switches"]) == 1
    moves = [command for command in rig.port.commands[before:] if "MOVE" in command]
    assert moves == ["FOCUS MOVE -30", "FOCUS MOVE 0", "OBJ MOVE -740"]


def test_unknown_recorded_focus_still_requires_live_clearance_before_turret_movement(
    demo_rig, monkeypatch
):
    rig = demo_rig()
    begin(rig, reference=False)
    profile = teach_all(rig)
    assert turret(rig, "select", {"id": profile["profile_id"]}).status_code == 200
    establish_manual_reference(rig)
    assert dispatch(rig, "focus_move", steps=30)["last_command"]["result"] == "ok"
    wait_focus_done = rig.service._motor.wait_focus_done

    def displaced_after_focus_move(*args, **kwargs):
        result = wait_focus_done(*args, **kwargs)
        with rig.port.lock:
            rig.port.sim.focus.position = 1
        return result

    monkeypatch.setattr(rig.service._motor, "wait_focus_done", displaced_after_focus_move)
    before = len(rig.port.commands)
    state = dispatch(rig, "select_objective", id=2)
    assert state["last_command"]["result"] == "failed"
    assert "clearance" in state["last_command"]["message"]
    assert not any(command.startswith("OBJ MOVE") for command in rig.port.commands[before:])


def test_profile_identification_occurs_only_at_configured_clearance(demo_rig, monkeypatch):
    rig = demo_rig()
    begin(rig, reference=False)
    profile = teach_all(rig)
    assert turret(rig, "select", {"id": profile["profile_id"]}).status_code == 200
    establish_manual_reference(rig)
    assert dispatch(rig, "focus_move", steps=30)["last_command"]["result"] == "ok"
    factory = rig.service._profile_locator
    observed = []
    original = rig.service._side.get_frame_packet
    stale_packet = None
    stale_returned = False

    def frames(**kwargs):
        nonlocal stale_packet, stale_returned
        if kwargs.get("after_id") is not None and not stale_returned:
            stale_returned = True
            return stale_packet
        packet = original(**kwargs)
        if rig.port.sim.focus.position == 30:
            frame_id, captured_at, pixels = packet
            pixels[60:140, 60:140] = cv2.cvtColor(
                cv2.aruco.generateImageMarker(
                    cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), 0, 80
                ),
                cv2.COLOR_GRAY2BGR,
            )
            stale_packet = (frame_id, captured_at, pixels)
            return stale_packet
        return packet

    def locator(saved):
        value = factory(saved)
        locate = value.locate

        def at_clearance(*args, **kwargs):
            observed.append(rig.port.sim.focus.position)
            assert observed[-1] == 0
            return locate(*args, **kwargs)

        value.locate = at_clearance
        return value

    monkeypatch.setattr(rig.service, "_profile_locator", locator)
    monkeypatch.setattr(rig.service._side, "get_frame_packet", frames)
    before = len(rig.port.commands)
    state = dispatch(rig, "select_objective", id=3)
    assert state["last_command"]["result"] == "ok", state
    assert state["objective_id"] == 3 and observed == [0]
    assert stale_returned
    assert "FOCUS MOVE -30" in rig.port.commands[before:]
    assert not any(item.startswith("OBJ MOVE") for item in rig.port.commands[before:])


def test_select_loads_saved_profile_and_delete_clears_matching_draft(demo_rig):
    rig = demo_rig()
    begin(rig)
    profile = teach_all(rig)
    assert turret(rig, "begin", {"name": "Different draft"}).status_code == 200
    assert turret(rig, "finish").status_code == 200
    selected = turret(rig, "select", {"id": profile["profile_id"]})
    assert selected.status_code == 200 and selected.json["draft"] == profile
    deleted = turret(rig, "delete", {"id": profile["profile_id"]})
    assert deleted.status_code == 200 and deleted.json["draft"] is None
    assert turret(rig, "save").status_code == 400


@pytest.mark.parametrize("metadata", [(True, 1.0), (1, float("nan")), (1, True)])
def test_record_rejects_invalid_frame_metadata_without_changing_draft(
    demo_rig, monkeypatch, metadata
):
    rig = demo_rig()
    begin(rig)
    original = rig.service._side.get_frame_packet

    def invalid(**kwargs):
        packet = original(**kwargs)
        return packet if kwargs.get("wait") is False else (*metadata, packet[2])

    monkeypatch.setattr(rig.service._side, "get_frame_packet", invalid)
    assert turret(rig, "record", {"objective_id": 0}).status_code == 503
    assert rig.service._turret_draft["captures"] == {}
