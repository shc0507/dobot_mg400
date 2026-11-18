"""Check the native Console preview without a network or physical devices."""

import importlib.util
import json
import socket
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "console_preview", ROOT / "scripts/console_preview.py"
)
preview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preview)
HEADERS = {"X-MG400-Client": "preview-test"}
TWIN = "/lab/api/console/ws/twin"
TURRET = "/lab/api/console/ws/process/turret"
TRANSFER = "/lab/api/console/ws/sample-transfer"


@pytest.fixture
def client(tmp_path, monkeypatch):
    def network_forbidden(*args, **kwargs):
        pytest.fail("The local preview attempted a network connection.")

    monkeypatch.setattr(socket.socket, "connect", network_forbidden)
    monkeypatch.setenv("MG400_CAMERA_DEVICES", "/dev/video0")
    monkeypatch.setenv("MG400_ORCH_ROBOT_URL", "http://hardware.invalid/")
    return preview.create_app(tmp_path).test_client()


def test_real_shell_and_workspace_polls_stay_offline(client):
    shell = client.get("/lab/").get_data(as_text=True)
    assert "AI Chemistry Console" in shell and "Position Finder" in shell
    assert 'data-title-en="Furnace"' in shell and "Furnace + microscope" not in shell
    assert 'id="ws-sample-transfer"' in shell and 'data-title-en="Sample Transfer"' in shell
    assert 'src="/lab/console-static/sample-transfer.js"' in shell
    assert 'href="/lab/console-static/sample-transfer.css"' in shell
    assert "/lab/console-static/console-logo.png" in shell
    logo = client.get("/lab/console-static/console-logo.png")
    assert logo.status_code == 200 and logo.mimetype == "image/png"
    for name in ("overview", "motion", "cameras", "twin", "process"):
        assert f'id="ws-{name}"' in shell
        assert client.get(f"/lab/console-static/{name}.js").status_code == 200
    for path in (
        "/lab/api/console/state",
        "/lab/api/console/events",
        "/lab/api/console/ws/motion/state",
        "/lab/api/console/ws/cameras/status",
        "/lab/api/console/ws/process/status",
        TWIN + "/state",
        TWIN + "/frame",
        TRANSFER + "/state",
    ):
        assert client.get(path, headers=HEADERS).status_code == 200
    state = client.get("/lab/api/console/state").get_json()
    assert all(row["connected"] is False for row in state["services"])
    cameras = client.get("/lab/api/console/ws/cameras/status").get_json()
    assert all(row["configured"] is False for row in cameras["views"])
    for path in ("thermal/frame", "thermal/regions", "snapshot/microscope"):
        assert client.get("/lab/api/console/ws/cameras/" + path).status_code == 502


def test_device_commands_are_unavailable_and_stops_need_no_control(client):
    for name in preview.SERVICES:
        assert (
            client.post(
                f"/lab/api/console/control/{name}/take", json={}, headers=HEADERS
            ).status_code
            == 503
        )
        assert (
            client.post(f"/lab/api/console/stop/{name}", json={}, headers=HEADERS).get_json()[
                "outcome"
            ]
            == "unavailable"
        )
    for action in ("motion", "heat", "gas"):
        assert client.post(f"/lab/api/dashboard/{action}", json={}).status_code == 202
    state = client.get("/lab/api/console/state").get_json()
    assert len(state["operations"]) == 3
    assert all(
        row["status"] == "unavailable"
        for operation in state["operations"]
        for row in operation["results"].values()
    )


def test_mock_calibration_and_saved_edits_survive_restart(client, tmp_path):
    state = client.get(TWIN + "/state", headers=HEADERS).get_json()
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
    state = restarted.get(TWIN + "/state", headers=HEADERS).get_json()
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
        if not state["operation"].get("settling"):
            return state
        time.sleep(0.01)
    pytest.fail("The transfer preview did not finish.")


def test_transfer_teaching_uses_moving_camera_and_persists_profiles(client, tmp_path):
    transfer_action(client, "profile/new", {"name": "Preview transfer"})
    out = transfer_action(client, "record", {"name": "Pickup"})
    out_mm = out["draft"]["positions"]["Pickup"]["position_mm"]
    transfer_action(client, "jog", {"motor": "insert", "direction": "ccw"})
    assert transfer_idle(client)["operation"]["status"] == "completed"
    inside = transfer_action(client, "record", {"name": "Inspection"})
    in_mm = inside["draft"]["positions"]["Inspection"]["position_mm"]
    # Ten pulses can be smaller than one pixel in the synthetic camera frame.
    assert in_mm["insert"] >= out_mm["insert"]
    assert in_mm["x"] == pytest.approx(out_mm["x"], abs=0.05)
    assert in_mm["y"] == pytest.approx(out_mm["y"], abs=0.05)
    saved = transfer_action(client, "profile/save", {"name": "Preview transfer"})
    identifier = saved["selected_profile"]
    assert identifier == saved["draft"]["id"]
    transfer_action(client, "move", {"target": "fully_out"})
    extracted = transfer_idle(client)
    assert extracted["operation"]["status"] == "completed", extracted["operation"]
    assert extracted["operation"]["target_mm"] == 0
    assert extracted["position"]["insert"] == pytest.approx(0, abs=0.5)
    transfer_action(client, "move", {"target": "fully_in"})
    inserted = transfer_idle(client)
    assert inserted["operation"]["status"] == "completed", inserted["operation"]
    assert inserted["operation"]["target_mm"] == 150
    assert inserted["position"]["insert"] == pytest.approx(150, abs=0.5)
    optical = client.post(TWIN + "/measure", json={}, headers=HEADERS).get_json()
    assert optical["axes"]["insert"]["value_mm"] == inserted["position"]["insert"]
    restarted = preview.create_app(tmp_path).test_client()
    assert (
        restarted.get(TRANSFER + "/state", headers=HEADERS).get_json()["selected_profile"]
        == identifier
    )
    exported = client.get(TRANSFER + "/profile/" + identifier, headers=HEADERS)
    assert exported.status_code == 200 and exported.mimetype == "application/json"
    imported = transfer_action(
        client, "profile/upload", {"profile": exported.get_data(as_text=True)}
    )
    assert len(imported["profiles"]) == 2 and imported["selected_profile"] == identifier
    invalid = client.post(TRANSFER + "/profile/upload", json={"profile": "{}"}, headers=HEADERS)
    assert invalid.status_code == 400
    deleted = transfer_action(client, "profile/delete", {"id": identifier})
    assert deleted["selected_profile"] is None and len(deleted["profiles"]) == 1


def test_stop_all_cancels_preview_insert_between_measurements(client):
    state = transfer_action(client, "measure")
    assert state["profiles"] == [] and state["selected_profile"] is None
    start = state["position"]["insert"]
    target = state["transfer_reference"]["targets_mm"]["fully_out"]
    transfer_action(client, "move", {"target": "fully_out"})
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = client.get(TRANSFER + "/state", headers=HEADERS).get_json()
        position = state["operation"]["position_mm"]
        if position is not None and target + 1 < position < start - 1:
            break
        time.sleep(0.01)
    else:
        pytest.fail("The preview did not move toward the endpoint.")
    response = client.post("/lab/api/dashboard/motion", json={}, headers=HEADERS)
    assert response.status_code == 202
    assert transfer_idle(client)["operation"]["status"] == "stopped"
    first = transfer_action(client, "measure")["position"]["insert"]
    time.sleep(0.3)
    second = transfer_action(client, "measure")["position"]["insert"]
    assert second == first and target < second < start
