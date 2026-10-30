"""HTTP contract tests with simulated or disconnected sources only."""

import io
import json
import time

from fastapi.testclient import TestClient
import numpy as np

from thermal_cam.app import create_app
from thermal_cam.service import CameraService


def wait_for_frame(client):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get("/api/frame")
        if response.status_code == 200:
            return response.json()
        time.sleep(0.01)
    raise AssertionError("No demo frame arrived")


def test_disconnected_endpoints_and_input_validation():
    service = CameraService(demo=True)
    with TestClient(create_app(service=service, auto_connect=False)) as client:
        status = client.get("/api/status")
        assert status.status_code == 200
        assert status.json()["state"] == "disconnected"
        assert status.headers["cache-control"] == "no-store"
        assert client.get("/api/frame").status_code == 503
        assert client.get("/api/snapshot.npz").status_code == 503
        assert client.post("/api/shutter").status_code == 409
        assert client.post("/api/gain", json={"gain": "auto"}).status_code == 422
        assert client.post("/api/gain", json={"gain": "low"}).status_code == 422
        assert client.post("/api/gain", json={}).status_code == 422
        assert client.post("/api/gain", json={"gain": "high"}).status_code == 409


def test_demo_frame_snapshot_controls_and_lifespan_cleanup():
    service = CameraService(demo=True)
    with TestClient(create_app(service=service)) as client:
        payload = wait_for_frame(client)
        assert payload["source"] == "demo"
        assert payload["temperature_unit"] == "celsius"
        assert (payload["width"], payload["height"]) == (160, 120)
        assert len(payload["raw"]) == len(payload["temperatures_c"]) == 120
        assert len(payload["raw"][0]) == 160
        assert payload["stats"]["center"]["x"] == 80
        assert payload["stats"]["center"]["y"] == 60
        np.testing.assert_allclose(np.array(payload["raw"]) / 64 - 273.15,
                                   payload["temperatures_c"])
        response = client.get("/api/snapshot.npz")
        assert response.status_code == 200
        assert ".npz" in response.headers["content-disposition"]
        with np.load(io.BytesIO(response.content), allow_pickle=False) as archive:
            metadata = json.loads(str(archive["metadata"]))
            assert metadata["source"] == "demo"
            np.testing.assert_allclose(archive["raw"].astype(float) / 64 - 273.15,
                                       archive["temperatures_c"])
            assert metadata["stats"]["center"]["celsius"] == archive["temperatures_c"][60, 80]
        assert client.post("/api/gain", json={"gain": "low"}).status_code == 422
        assert client.post("/api/gain", json={"gain": "high"}).json()["ok"]
        assert client.post("/api/shutter").json()["ok"]
        assert client.post("/api/disconnect").json()["state"] == "disconnected"
        assert client.get("/api/frame").status_code == 503
        assert client.post("/api/connect").status_code == 200
        assert wait_for_frame(client)["sequence"] > payload["sequence"]
    assert service.status()["state"] == "disconnected"
    assert not service._thread.is_alive()


def test_rejects_foreign_hosts_and_browser_control_origins():
    with TestClient(create_app(demo=True, auto_connect=False)) as client:
        assert client.get("/api/status", headers={"host": "untrusted.example"}).status_code == 400
        denied = client.post("/api/connect", headers={"origin": "https://untrusted.example"})
        assert denied.status_code == 403
        assert client.get("/api/status").json()["state"] == "disconnected"
        assert client.post("/api/connect", headers={"origin": "http://testserver"}).status_code == 200
