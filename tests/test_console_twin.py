"""Twin workspace: the state route and the joint and signal mapping."""

import json
from types import SimpleNamespace

import pytest

from orchestrator.app import OrchestratorService, create_app
from orchestrator.web.workspaces import discover, twin

PRIVATE = "private-robot-service-token-0123456789"


class Clock:
    def __call__(self):
        return 1000.0


class FakeAdapter:
    def __init__(self, state):
        self._state = state

    def state(self):
        return self._state

    def close(self):
        pass


ROBOT = {
    "joints": [10.0, 20.0, 30.0, 40.0],
    "actual": [284.6, -0.6, 121.2, 14.5],
    "enabled": False,
    "busy": False,
    "robot_error": False,
    "alarms": None,
    "feedback_fresh": True,
    "robot_host": "192.168.1.6",
    "token": PRIVATE,
    "lease": {"holder": None},
}
STAGE = {
    "connected": True,
    "busy": False,
    "fault": None,
    "feedback_fresh": True,
    "microstep": 2,
    "port": "/dev/ttyACM0",
    "motors": [
        {"id": "bottom", "position": 1193, "mode": "idle"},
        {"id": "top", "position": 258, "mode": "idle"},
        {"id": "insert", "position": 3334, "mode": "idle"},
    ],
    "lease": {"holder": "someone-else"},
}
MICROSCOPE = {
    "connected": True,
    "busy": False,
    "fault": None,
    "objective_id": 3,
    "focus_position_pulses": None,
    "focus_zero_known": False,
    "lease": {"holder": None},
}
FURNACE = {
    "connected": True,
    "busy": False,
    "fault": None,
    "pv_c": 650.0,
    "target_c": 700.0,
    "control_mode": "RUN",
    "run_state": "RUN",
    "flow": {
        "gas_valves": [{"open": True}, {"open": None}],
        "gas_channels": [{"actual_percent": 12.5}, {"actual_percent": None}],
    },
    "lease": {"holder": None},
}


@pytest.fixture
def rig(tmp_path):
    states = {"robot": ROBOT, "stage": STAGE, "microscope": MICROSCOPE, "furnace": FURNACE}

    def factory(required, identity):
        return {name: FakeAdapter(states[name]) for name in required}

    service = OrchestratorService(
        settings={}, recipes_dir=tmp_path, demo=True, client_factory=factory, clock=Clock()
    ).start(background=False)
    app = create_app(service, base_path="/lab")
    yield SimpleNamespace(service=service, client=app.test_client(), states=states)
    service.close()


def state(rig):
    response = rig.client.get(
        "/lab/api/console/ws/twin/state", headers={"X-MG400-Client": "console-page-a"}
    )
    assert response.status_code == 200
    return response.get_json()


def test_twin_workspace_is_found_with_both_titles():
    item = next(item for item in discover() if item.id == "twin")
    assert item.register is not None and item.title == {"en": "Twin", "zh": "实时台面"}


def test_model_is_served_from_the_asset_path(rig, tmp_path, monkeypatch):
    path = tmp_path / "twin.glb"
    monkeypatch.setattr(twin, "MODEL", path)
    assert rig.client.get("/lab/api/console/ws/twin/model").status_code == 404
    path.write_bytes(b"glTF" + bytes(16))
    response = rig.client.get("/lab/api/console/ws/twin/model")
    assert response.status_code == 200 and response.data[:4] == b"glTF"
    assert response.mimetype == "model/gltf-binary"


def test_state_maps_joints_and_signals_and_hides_identities(rig):
    body = state(rig)
    text = json.dumps(body)
    assert (
        PRIVATE not in text
        and "192.168.1.6" not in text
        and "/dev/ttyACM0" not in text
        and "someone-else" not in text
    )
    j = body["joints"]
    assert [j["J1"], j["J2"], j["J3"], j["J4"]] == [10.0, 20.0, 30.0, 40.0]
    assert j["stage_bottom"] == round(-1193 / 80 + 11.01, 2)
    assert j["stage_top"] == round(-258 / 80 + 2.58, 2) and j["insert"] == round(
        -3334 / 80 + 39.04, 2
    )
    assert j["turret"] == 144.0 and j["focus"] is None
    s = body["signals"]
    assert s["furnace_c"] == 650.0 and s["furnace_hot"] and s["furnace_running"] and s["gas_on"]
    assert (
        not s["robot_alarm"]
        and not s["robot_stale"]
        and s["stage_calibrated"]
        and not s["stage_moving"]
    )
    assert body["devices"]["stage"]["holder"] == "other"


def test_changed_microstep_or_missing_data_gives_unknown_joints():
    rows = {
        "robot": {"available": False},
        "stage": {"available": True, "microstep": 4, "motors": {"bottom": {"position": 5}}},
        "microscope": {"available": True, "objective_id": None},
    }
    j = twin.joints(rows)
    assert all(
        j[k] is None
        for k in ("J1", "J2", "J3", "J4", "stage_bottom", "stage_top", "insert", "turret", "focus")
    )
    s = twin.signals(rows)
    assert (
        s["robot_stale"]
        and not s["stage_calibrated"]
        and s["furnace_c"] is None
        and not s["furnace_hot"]
    )


def test_alarm_and_motion_signals():
    rows = {
        "robot": {"available": True, "feedback_fresh": True, "alarms": 2, "busy": True},
        "stage": {
            "available": True,
            "feedback_fresh": True,
            "microstep": 2,
            "motors": {"top": {"mode": "move"}},
        },
    }
    s = twin.signals(rows)
    assert s["robot_alarm"] and s["robot_moving"] and s["stage_moving"]


def test_unreachable_devices_are_reported_without_an_error(rig, monkeypatch):
    def broken(name, identity):
        raise OSError("down")

    monkeypatch.setattr(rig.service, "console_client", broken)
    body = state(rig)
    assert all(
        row == {"available": False, "reason": "unreachable"} for row in body["devices"].values()
    )
    assert body["signals"]["robot_stale"] and body["signals"]["stage_stale"]
