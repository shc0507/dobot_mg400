"""Check the native Console preview without a network or physical devices."""

import importlib.util
import json
import socket
import time
from pathlib import Path

import pytest

import stage_controller.controller
import stage_controller.encoder
from stage_controller.protocol import DemoTransport

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "console_preview", ROOT / "scripts/console_preview.py"
)
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)
HEADERS = {"X-MG400-Client": "preview-test"}
TWIN = "/lab/api/console/ws/position-finder"
TURRET = "/lab/api/console/ws/process/turret"
TRANSFER = "/lab/api/console/ws/sample-transfer"
MOTION = "/lab/api/console/ws/motion"


@pytest.fixture
def client(tmp_path, monkeypatch):
    def network_forbidden(*args, **kwargs):
        pytest.fail("The local preview attempted a network connection.")

    monkeypatch.setattr(socket.socket, "connect", network_forbidden)
    monkeypatch.setenv("MG400_CAMERA_DEVICES", "/dev/video0")
    monkeypatch.setenv("MG400_ORCH_ROBOT_URL", "http://hardware.invalid/")
    apps = []
    create = preview.create_app

    def tracked_create(*args, **kwargs):
        app = create(*args, **kwargs)
        apps.append(app)
        return app

    monkeypatch.setattr(preview, "create_app", tracked_create)
    yield preview.create_app(tmp_path).test_client()
    for app in reversed(apps):
        app.extensions["preview_close"]()


class ScaledClock:
    """Run simulated stage motion `rate` times faster than real time."""

    def __init__(self, rate):
        self._real = self._now = time.monotonic()
        self.rate = rate

    def __call__(self):
        real = time.monotonic()
        self._now += (real - self._real) * self.rate
        self._real = real
        return self._now


@pytest.fixture
def fast_stage(monkeypatch):
    """Speed up the simulated encoder board; the host keeps real time and a short cooldown."""
    clock = ScaledClock(10)
    monkeypatch.setattr(
        stage_controller.controller, "DemoTransport", lambda _: DemoTransport(clock)
    )
    monkeypatch.setattr(stage_controller.encoder, "COOLDOWN_SECONDS", 0.05)
    return clock


def test_real_shell_and_workspace_polls_stay_offline(client):
    shell = client.get("/lab/").get_data(as_text=True)
    assert "AI Chemistry Console" in shell and "Position Finder (legacy)" in shell
    assert 'data-title-en="Furnace"' in shell and "Furnace + microscope" not in shell
    assert 'id="ws-sample-transfer"' in shell and 'data-title-en="Sample Transfer"' in shell
    assert 'src="/lab/console-static/sample-transfer.js"' in shell
    assert 'href="/lab/console-static/sample-transfer.css"' in shell
    assert "/lab/console-static/console-logo.webp" in shell
    logo = client.get("/lab/console-static/console-logo.webp")
    assert logo.status_code == 200 and logo.mimetype == "image/webp"
    for name in ("overview", "motion", "cameras", "twin", "position-finder", "process"):
        assert f'id="ws-{name}"' in shell
        assert client.get(f"/lab/console-static/{name}.js").status_code == 200
    for path in (
        "/lab/api/console/state",
        "/lab/api/console/events",
        "/lab/api/console/history",
        "/lab/api/console/ws/motion/state",
        "/lab/api/console/ws/cameras/status",
        "/lab/api/console/ws/process/status",
        TWIN + "/state",
        TRANSFER + "/state",
    ):
        assert client.get(path, headers=HEADERS).status_code == 200
    state = client.get("/lab/api/console/state").get_json()
    assert all(row["connected"] is (row["id"] == "stage") for row in state["services"])
    cameras = client.get("/lab/api/console/ws/cameras/status").get_json()
    assert all(row["configured"] is False for row in cameras["views"])
    for path in ("thermal/frame", "thermal/regions"):
        assert client.get("/lab/api/console/ws/cameras/" + path).status_code == 502
    # The microscope snapshot now uses the host camera proxy, absent in the mock.
    assert client.get("/lab/api/console/ws/cameras/snapshot/microscope").status_code == 404


def test_legacy_stays_asleep_until_explicit_activation(client, tmp_path):
    assert client.get("/").headers["Location"].endswith("/lab/#motion")
    assert client.get(TWIN + "/state", headers=HEADERS).get_json() == {"enabled": False}
    assert client.get(TWIN + "/frame", headers=HEADERS).status_code == 409
    assert client.post(TWIN + "/measure", json={}, headers=HEADERS).status_code == 409
    assert not (tmp_path / "calibration" / "twin").exists()
    for path in ("/lab/api/console/ws/motion/state", TRANSFER + "/state"):
        assert client.get(path, headers=HEADERS).status_code == 200
    assert not (tmp_path / "calibration" / "twin").exists()
    enabled = client.post(TWIN + "/enabled", json={"enabled": True}, headers=HEADERS)
    assert enabled.status_code == 200 and enabled.get_json()["enabled"] is True
    assert (tmp_path / "calibration" / "twin" / "profiles").is_dir()
    assert client.get(TWIN + "/frame", headers=HEADERS).status_code == 200
    disabled = client.post(TWIN + "/enabled", json={"enabled": False}, headers=HEADERS)
    assert disabled.get_json() == {"enabled": False}
    assert client.get(TWIN + "/frame", headers=HEADERS).status_code == 409
    assert client.get(TWIN + "/state", headers=HEADERS).get_json() == {"enabled": False}


def test_only_simulated_stage_is_available_and_stops_need_no_control(client):
    for name in preview.SERVICES:
        if name == "stage":
            continue
        assert (
            client.post(
                f"/lab/api/console/control/{name}/take", json={}, headers=HEADERS
            ).status_code
            == 502
        )
        assert (
            client.post(f"/lab/api/console/stop/{name}", json={}, headers=HEADERS).status_code
            == 409
        )
    assert (
        client.post("/lab/api/console/stop/stage", json={}, headers=HEADERS).get_json()["outcome"]
        == "confirmed"
    )
    for action in ("motion", "heat", "gas"):
        assert client.post(f"/lab/api/dashboard/{action}", json={}).status_code == 202
    state = client.get("/lab/api/console/state").get_json()
    assert len(state["operations"]) == 3
    assert state["operations"][0]["results"]["stage"]["status"] == "confirmed"


def test_mock_calibration_and_saved_edits_survive_restart(client, tmp_path):
    state = client.post(TWIN + "/enabled", json={"enabled": True}, headers=HEADERS).get_json()
    profile = state["calibration"]["profile"]
    assert profile["name"] == "Mock stage"
    assert all(axis["status"] == "calibrated" for axis in profile["axis_status"].values())
    measure = client.post(TWIN + "/measure", json={"automatic": False}, headers=HEADERS)
    assert measure.status_code == 200
    assert measure.get_json()["profile_id"] == profile["id"]
    for axis, expected in {"x": 22.5, "y": 20.0, "insert": 75.0}.items():
        assert measure.get_json()["axes"][axis]["value_mm"] == pytest.approx(expected, abs=0.2)
    saved = client.post(TWIN + "/profile/save", json={"name": "My preview"}, headers=HEADERS)
    assert saved.status_code == 200
    restarted = preview.create_app(tmp_path).test_client()
    assert restarted.get(TWIN + "/state", headers=HEADERS).get_json() == {"enabled": False}
    state = restarted.post(TWIN + "/enabled", json={"enabled": True}, headers=HEADERS).get_json()
    assert state["calibration"]["profile"]["name"] == "My preview"
    assert len(state["profiles"]) == 2


def test_turret_profile_workflow_uses_synthetic_frames_and_persists(client, tmp_path):
    import cv2
    import numpy as np

    def action(name, data=None):
        response = client.post(TURRET + "/" + name, json=data or {}, headers=HEADERS)
        assert response.status_code == 200, response.get_json()
        return response.get_json()

    state = action("begin", {"name": "Preview lenses", "new": True})
    assert state["demo"] and state["session"]["mine"]
    assert state["alignment"] == "image_coordinates"
    assert state["dictionary"] == "DICT_4X4_50"
    assert "reference" not in state
    assert state["draft"]["schema_version"] == 2
    frame = client.get(state["camera"]["frame_url"])
    assert frame.status_code == 200 and frame.mimetype == "image/jpeg"
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))
    for objective in range(4):
        state = action("record", {"objective_id": objective})
        capture = state["draft"]["captures"][str(objective)]
        assert capture["preview_jpeg"]
        assert set(capture) == {"shape", "objective_corners", "preview_jpeg"}
        frame = client.get(state["camera"]["frame_url"])
        image = cv2.imdecode(np.frombuffer(frame.data, dtype=np.uint8), cv2.IMREAD_COLOR)
        _, ids, _ = detector.detectMarkers(image)
        assert ids.flatten().tolist() == [objective]
    assert action("finish")["session"] is None
    state = action("save", {"name": "All lenses"})
    identifier = state["draft"]["profile_id"]
    assert state["profiles"][0]["complete"]
    assert action("select", {"id": identifier})["selected_profile"] == identifier
    restarted = preview.create_app(tmp_path).test_client()
    assert restarted.get(TURRET, headers=HEADERS).get_json()["selected_profile"] == identifier
    profile = client.get(TURRET + "/profile/" + identifier, headers=HEADERS).get_json()
    state = action("upload", {"profile": json.dumps(profile)})
    assert len(state["profiles"]) == 2
    assert state["draft"]["profile_id"] != identifier
    invalid = client.post(TURRET + "/upload", json={"profile": "{}"}, headers=HEADERS)
    assert invalid.status_code == 400
    assert len(client.get(TURRET, headers=HEADERS).get_json()["profiles"]) == 2
    state = action("delete", {"id": identifier})
    assert len(state["profiles"]) == 1 and state["selected_profile"] is None


def test_four_lens_coordinates_can_be_used_without_saved_focus_zero(client):
    def action(name, data=None):
        response = client.post(TURRET + "/" + name, json=data or {}, headers=HEADERS)
        assert response.status_code == 200, response.get_json()
        return response.get_json()

    action("begin", {"name": "Lens references", "new": True})
    for objective in range(4):
        state = action("record", {"objective_id": objective})
    profile = state["draft"]
    action("finish")
    profile["focus_position"] = None
    uploaded = action("upload", {"profile": json.dumps(profile)})
    assert uploaded["profiles"][0]["complete"]
    identifier = uploaded["draft"]["profile_id"]
    selected = action("select", {"id": identifier})
    assert selected["selected_profile"] == identifier
    assert selected["compatible_for_switch"]
    assert selected["draft"]["focus_position"] is None
    assert selected["draft"]["captures"] == profile["captures"]


def test_turret_draft_resume_and_session_owner(client):
    def action(name, data=None, headers=HEADERS):
        return client.post(TURRET + "/" + name, json=data or {}, headers=headers)

    action("begin", {"name": "Partial", "new": True})
    assert action("record", {"objective_id": 0}).status_code == 200
    other = {"X-MG400-Client": "another-page"}
    assert action("record", {"objective_id": 1}, headers=other).status_code == 409
    assert action("begin", headers=other).status_code == 409
    assert action("save").status_code == 409
    action("finish")
    saved = action("save").get_json()
    identifier = saved["draft"]["profile_id"]
    assert not saved["profiles"][0]["complete"]
    assert action("select", {"id": identifier}).status_code == 400
    assert action("load", {"id": identifier}).status_code == 200
    resumed = action("begin").get_json()
    assert resumed["draft"]["profile_id"] == identifier
    assert list(resumed["draft"]["captures"]) == ["0"]
    assert action("cancel").status_code == 200


def transfer_action(client, action, data=None):
    response = client.post(TRANSFER + "/" + action, json=data or {}, headers=HEADERS)
    assert response.status_code in (200, 202), response.get_json()
    return response.get_json()


def transfer_idle(client):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        state = client.get(TRANSFER + "/state", headers=HEADERS).get_json()
        client.post("/lab/api/console/heartbeat", json={}, headers=HEADERS)
        if not state["operation"].get("settling"):
            return state
        time.sleep(0.01)
    pytest.fail("The transfer preview did not finish.")


def test_legacy_transfer_profiles_remain_manageable_but_cannot_move(client, tmp_path):
    state = transfer_action(client, "profile/new", {"name": "Preview transfer"})
    assert state["draft"]["source"] is None and state["draft"]["positions"] == {}
    saved = transfer_action(client, "profile/save", {"name": "Preview transfer"})
    empty_id = saved["draft"]["id"]
    assert saved["selected_profile"] is None
    # Import an existing optical record without connecting its old source.
    profile = client.get(TRANSFER + "/profile/" + empty_id, headers=HEADERS).get_json()
    profile["schema_version"] = 2
    profile.pop("unit")
    profile.pop("stage_positions")
    profile["source"] = {
        "camera": "camera1",
        "device_id": "demo:camera1",
        "profile_id": "00000000-0000-0000-0000-000000000001",
        "name": "Earlier calibration",
    }
    profile["positions"] = {
        "Pickup": {
            "position_mm": {"x": 10, "y": 20, "insert": 30},
            "captured_at": profile["created_at"],
            "image_sha256": "a" * 64,
        }
    }
    imported = transfer_action(client, "profile/upload", {"profile": json.dumps(profile)})
    identifier = imported["draft"]["id"]
    assert len(imported["profiles"]) == 2 and imported["selected_profile"] is None
    selected = transfer_action(client, "profile/select", {"id": identifier})
    assert selected["selected_profile"] == identifier
    assert selected["named_motion_ready"] is False
    blocked = client.post(TRANSFER + "/position/move", json={"name": "Pickup"}, headers=HEADERS)
    assert blocked.status_code == 400
    restarted = preview.create_app(tmp_path).test_client()
    assert (
        restarted.get(TRANSFER + "/state", headers=HEADERS).get_json()["selected_profile"]
        == identifier
    )
    exported = client.get(TRANSFER + "/profile/" + identifier, headers=HEADERS)
    assert exported.status_code == 200 and exported.mimetype == "application/json"
    assert exported.get_json()["positions"] == profile["positions"]
    loaded = transfer_action(client, "profile/load", {"id": identifier})
    assert loaded["draft"]["positions"] == profile["positions"]
    assert (
        transfer_action(client, "position/delete", {"name": "Pickup"})["draft"]["positions"] == {}
    )
    invalid = client.post(TRANSFER + "/profile/upload", json={"profile": "{}"}, headers=HEADERS)
    assert invalid.status_code == 400
    deleted = transfer_action(client, "profile/delete", {"id": identifier})
    assert deleted["selected_profile"] is None and len(deleted["profiles"]) == 1
    assert not (tmp_path / "calibration" / "twin").exists()


@pytest.mark.parametrize("legacy_enabled", [False, True])
def test_transfer_never_uses_legacy_finder_as_a_position_source(client, legacy_enabled):
    if legacy_enabled:
        assert (
            client.post(TWIN + "/enabled", json={"enabled": True}, headers=HEADERS).status_code
            == 200
        )
        assert client.post(TWIN + "/measure", json={}, headers=HEADERS).status_code == 200
    wait_stage(client, lambda stage: all(m["telemetry_fresh"] for m in stage["motors"]))
    state = client.get(TRANSFER + "/state", headers=HEADERS).get_json()
    assert state["position_available"] is True
    assert state["position"] == {"x": 0, "y": 0, "insert": 0}
    assert state["position_source"]["kind"] == "stage_encoder"
    assert state["transfer_reference"]["ready"] is False
    assert "stage_position" not in client.get("/lab/api/console/ws/motion/state").get_json()


def test_manual_jog_uses_encoder_feedback_without_legacy_calibration(client, tmp_path):
    take_stage(client)
    wait_stage(client, lambda stage: all(m["telemetry_fresh"] for m in stage["motors"]))
    transfer_action(client, "jog", {"motor": "insert", "direction": "ccw"})
    state = transfer_idle(client)
    assert state["operation"]["status"] == "completed", state["operation"]
    assert state["position_available"] is True
    assert state["position"]["insert"] == pytest.approx(0.1, abs=0.001)
    assert not (tmp_path / "calibration" / "twin").exists()


@pytest.mark.parametrize(
    "path,identity",
    [
        (TRANSFER + "/stop", "another-page"),
        ("/lab/api/dashboard/motion", "another-page"),
        ("/lab/api/console/control/stage/release", "preview-test"),
    ],
)
def test_stops_cancel_simulated_encoder_move_without_control(client, path, identity):
    take_stage(client)
    wait_stage(client, lambda stage: all(m["telemetry_fresh"] for m in stage["motors"]))
    transfer_action(client, "jog", {"motor": "insert", "direction": "ccw"})
    wait_stage(client, lambda stage: stage["motors"][2]["mode"] != "idle")
    response = client.post(path, json={}, headers={"X-MG400-Client": identity})
    assert response.status_code in (200, 202)
    assert transfer_idle(client)["operation"]["status"] == "stopped"
    stopped = wait_stage(client, lambda stage: all(m["mode"] == "idle" for m in stage["motors"]))
    assert stopped["motors"][2]["position_rotations"] < 0.1


def test_motion_coordinates_use_live_stage_and_all_stop_routes_cancel_sequence(
    fast_stage, client, tmp_path
):
    service = client.application.extensions["preview_service"]
    assert (
        client.get(MOTION + "/state", headers=HEADERS).json["stage_operation"]["status"] == "idle"
    )
    assert not hasattr(service, "sample_transfer")
    take_stage(client)
    for motor in ("bottom", "top", "insert"):
        response = client.post(
            "/lab/api/console/command/stage",
            json={"action": "calibrate", "params": {"motor": motor, "speed": 0.25}},
            headers=HEADERS,
        )
        assert response.status_code == 202, response.json
        wait_stage(
            client,
            lambda state: next(m for m in state["motors"] if m["id"] == motor)["positioning_ready"],
        )

    seen = []

    def settled(stage):
        moving = [m["id"] for m in stage["motors"] if m["mode"] != "idle"]
        assert len(moving) <= 1
        if moving and (not seen or seen[-1] != moving[0]):
            seen.append(moving[0])
        return not client.get(MOTION + "/state", headers=HEADERS).json["stage_operation"][
            "settling"
        ]

    def position(target):
        response = client.post(MOTION + "/stage/position", json={"target": target}, headers=HEADERS)
        assert response.status_code == 202, response.json
        return response.json["operation"]

    def values(stage):
        return {motor["id"]: motor["position_rotations"] for motor in stage["motors"]}

    position({"x": 0.7, "y": 0.65})
    reached = wait_stage(client, settled)
    assert seen == ["bottom", "top"]
    assert values(reached) == pytest.approx({"top": 0.7, "bottom": 0.65, "insert": 0.5}, abs=0.002)
    seen.clear()
    position({"insert": 0.3})
    reached = wait_stage(client, settled)
    assert seen == ["insert"]
    assert values(reached) == pytest.approx({"top": 0.7, "bottom": 0.65, "insert": 0.3}, abs=0.002)
    assert service.sample_transfer.pages == {}

    # Each stop must arrive while Bottom moves.
    fast_stage.rate = 2
    for path, identity in (
        ("/lab/api/console/stop/stage", "another-page"),
        ("/lab/api/dashboard/motion", "another-page"),
        (TRANSFER + "/stop", "another-page"),
        ("/lab/api/console/control/stage/release", "preview-test"),
        ("/lab/api/console/release-page", "preview-test"),
        ("/lab/api/console/release-all", "another-page"),
    ):
        take_stage(client)
        wait_stage(client, lambda stage: all(m["positioning_ready"] for m in stage["motors"]))
        seen.clear()
        position({"x": 0.9, "y": 0.9})
        wait_stage(
            client,
            lambda stage: next(m for m in stage["motors"] if m["id"] == "bottom")["mode"] != "idle",
        )
        before = client.get(MOTION + "/state", headers=HEADERS).json["stage"]["stop_epoch"]
        response = client.post(path, json={}, headers={"X-MG400-Client": identity})
        assert response.status_code in (200, 202), response.json
        stopped = wait_stage(client, settled)
        wait_stage(client, lambda stage: stage["stop_epoch"] > before)
        service.devices.fresh("stage")
        state = client.get(MOTION + "/state", headers=HEADERS).json
        assert state["stage_operation"]["status"] == "stopped"
        assert state["stage"]["stop_epoch"] > before
        assert values(stopped)["top"] == pytest.approx(0.7, abs=0.002)
        assert values(stopped)["insert"] == pytest.approx(0.3, abs=0.002)
    assert "preview-test" not in service.sample_transfer.pages
    assert not (tmp_path / "calibration" / "sample-transfer").exists()


def take_stage(client):
    result = client.post("/lab/api/console/control/stage/take", json={}, headers=HEADERS)
    assert result.status_code == 200, result.get_json()


def test_rejected_manual_move_cancels_and_stops_active_transfer(client):
    take_stage(client)
    wait_stage(client, lambda stage: all(m["telemetry_fresh"] for m in stage["motors"]))
    transfer_action(client, "jog", {"motor": "insert", "direction": "ccw"})
    wait_stage(client, lambda stage: stage["motors"][2]["mode"] != "idle")
    rejected = client.post(
        "/lab/api/console/command/stage",
        json={
            "action": "move",
            "params": {"motor": "top", "direction": "ccw", "speed": 0.25, "rotations": 0.1},
        },
        headers=HEADERS,
    )
    assert rejected.status_code == 409
    assert transfer_idle(client)["operation"]["status"] == "stopped"
    stage = wait_stage(client, lambda state: all(m["mode"] == "idle" for m in state["motors"]))
    assert stage["motors"][2]["position_rotations"] < 0.1


def wait_stage(client, condition, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.application.extensions["preview_stage"].state()
        if condition(state):
            return state
        client.post("/lab/api/console/heartbeat", json={}, headers=HEADERS)
        time.sleep(0.05)
    pytest.fail(f"Stage did not settle: {state}")


def test_simulated_calibration_saved_target_and_sequential_move(fast_stage, client, tmp_path):
    take_stage(client)
    for motor in ("bottom", "top", "insert"):
        result = client.post(
            "/lab/api/console/command/stage",
            json={"action": "calibrate", "params": {"motor": motor, "speed": 0.25}},
            headers=HEADERS,
        )
        assert result.status_code == 202, result.get_json()
        wait_stage(
            client,
            lambda state: next(m for m in state["motors"] if m["id"] == motor)["positioning_ready"],
        )
    saved = transfer_action(client, "record", {"name": "Center"})
    capture = saved["draft"]["positions"]["Center"]["position_rotations"]
    assert all(value == pytest.approx(0.5, abs=0.002) for value in capture.values())
    saved = transfer_action(client, "profile/save", {"name": "Encoder positions"})
    transfer_action(client, "profile/select", {"id": saved["draft"]["id"]})
    # Change all axes, then require the recorded Bottom -> Top -> Insert sequence.
    for motor in ("bottom", "top", "insert"):
        transfer_action(client, "jog", {"motor": motor, "direction": "ccw"})
        assert transfer_idle(client)["operation"]["status"] == "completed"
    transfer_action(client, "position/move", {"name": "Center"})
    seen = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        stage = client.application.extensions["preview_stage"].state()
        moving = [m["id"] for m in stage["motors"] if m["mode"] != "idle"]
        assert len(moving) <= 1
        if moving and (not seen or seen[-1] != moving[0]):
            seen.append(moving[0])
        state = client.get(TRANSFER + "/state", headers=HEADERS).get_json()
        if not state["operation"]["settling"]:
            break
        client.post("/lab/api/console/heartbeat", json={}, headers=HEADERS)
        time.sleep(0.03)
    assert state["operation"]["status"] == "completed", state["operation"]
    assert seen == ["bottom", "top", "insert"]
    assert state["position"] == pytest.approx(capture, abs=0.002)
    # Transfer uses the live Insert bounds before any XY stage target is recorded.
    reference = state["transfer_reference"]["targets_rotations"]
    assert reference["fully_out"] == 0
    assert reference["fully_in"] == pytest.approx(1, abs=0.002)
    for endpoint, target in reference.items():
        transfer_action(client, "move", {"target": endpoint})
        reached = transfer_idle(client)
        assert reached["operation"]["status"] == "completed", reached["operation"]
        assert reached["position"] == pytest.approx(
            {"x": capture["x"], "y": capture["y"], "insert": target}, abs=0.002
        )
    insert = reached["position"]["insert"]
    for endpoint, target in (("out_stage", 0.3), ("microscope", 0.7)):
        for motor in ("bottom", "top"):
            result = client.post(
                "/lab/api/console/command/stage",
                json={
                    "action": "move_to_position",
                    "params": {"motor": motor, "position": target, "speed": 0.25},
                },
                headers=HEADERS,
            )
            assert result.status_code == 202, result.get_json()
            wait_stage(
                client,
                lambda stage: all(m["positioning_ready"] for m in stage["motors"]),
            )
        transfer_action(client, "endpoint/record", {"target": endpoint})
    saved = transfer_action(client, "profile/save", {"name": "Stage coordinates"})
    identifier = saved["draft"]["id"]
    positions = saved["draft"]["stage_positions"]
    assert all(set(capture["position_rotations"]) == {"x", "y"} for capture in positions.values())
    for endpoint, target in (("out_stage", 0.3), ("microscope", 0.7)):
        transfer_action(client, "stage/move", {"target": endpoint})
        reached = transfer_idle(client)
        assert reached["operation"]["status"] == "completed", reached["operation"]
        assert reached["position"] == pytest.approx(
            {"x": target, "y": target, "insert": insert}, abs=0.002
        )
    restarted = preview.create_app(tmp_path).test_client()
    restored = restarted.get(TRANSFER + "/state", headers=HEADERS).get_json()
    assert restored["selected_profile"] == identifier
    assert restored["active_profile"]["stage_positions"] == positions
    assert not (tmp_path / "calibration" / "twin").exists()
