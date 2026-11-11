"""Check the native Console preview without a network or physical devices."""

import importlib.util
import socket
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
