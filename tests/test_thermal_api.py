"""HTTP contract tests with simulated or disconnected sources only."""

import io
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

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
    with TestClient(
        create_app(service=service, auto_connect=False), base_url="http://127.0.0.1"
    ) as client:
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
    with TestClient(create_app(service=service), base_url="http://127.0.0.1") as client:
        payload = wait_for_frame(client)
        assert payload["source"] == "demo"
        assert payload["temperature_unit"] == "celsius"
        assert (payload["width"], payload["height"]) == (160, 120)
        assert len(payload["raw"]) == len(payload["temperatures_c"]) == 120
        assert len(payload["raw"][0]) == 160
        assert payload["stats"]["center"]["x"] == 80
        assert payload["stats"]["center"]["y"] == 60
        np.testing.assert_allclose(
            np.array(payload["raw"]) / 64 - 273.15, payload["temperatures_c"]
        )
        response = client.get("/api/snapshot.npz")
        assert response.status_code == 200
        assert ".npz" in response.headers["content-disposition"]
        with np.load(io.BytesIO(response.content), allow_pickle=False) as archive:
            metadata = json.loads(str(archive["metadata"]))
            assert metadata["source"] == "demo"
            np.testing.assert_allclose(
                archive["raw"].astype(float) / 64 - 273.15, archive["temperatures_c"]
            )
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
    with TestClient(
        create_app(demo=True, auto_connect=False), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/status", headers={"host": "untrusted.example"}).status_code == 400
        denied = client.post("/api/connect", headers={"origin": "https://untrusted.example"})
        assert denied.status_code == 403
        assert client.get("/api/status").json()["state"] == "disconnected"
        assert (
            client.post("/api/connect", headers={"origin": "http://127.0.0.1"}).status_code == 200
        )


@pytest.mark.parametrize(
    "header", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP"]
)
@pytest.mark.parametrize("value", ["", "127.0.0.1"])
def test_tokenless_app_rejects_proxy_headers(header, value):
    with TestClient(
        create_app(demo=True, auto_connect=False), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/status", headers={header: value}).status_code == 400
        assert client.post("/api/connect", headers={header: value}).status_code == 400
        assert client.get("/api/status").json()["state"] == "disconnected"


@pytest.mark.parametrize(
    "headers",
    [
        [("host", "127.0.0.1"), ("host", "untrusted.example")],
        [("host", "untrusted.example"), ("host", "127.0.0.1")],
        [("host", "[malformed")],
        [("host", "127.0.0.1:invalid")],
        [("host", "user@127.0.0.1")],
    ],
)
def test_app_rejects_ambiguous_or_malformed_hosts(headers):
    with TestClient(
        create_app(demo=True, auto_connect=False), base_url="http://127.0.0.1"
    ) as client:
        assert client.post("/api/connect", headers=headers).status_code == 400
        assert client.get("/api/status").json()["state"] == "disconnected"


@pytest.mark.parametrize(
    "headers",
    [
        [("origin", "http://127.0.0.1"), ("origin", "http://other.example")],
        [("origin", "http://other.example"), ("origin", "http://127.0.0.1")],
        [("origin", "http://[malformed")],
        [("origin", "null")],
        [("origin", "https://127.0.0.1:443")],
        [("origin", "http://127.0.0.1/path")],
        [("sec-fetch-site", "cross-site")],
    ],
)
def test_app_rejects_foreign_or_malformed_origins(headers):
    with TestClient(
        create_app(demo=True, auto_connect=False), base_url="http://127.0.0.1"
    ) as client:
        assert client.post("/api/connect", headers=headers).status_code == 403
        assert client.get("/api/status").json()["state"] == "disconnected"


TOKEN = "thermal-test-token-length-24"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/status"),
        ("GET", "/api/frame"),
        ("GET", "/api/snapshot.npz"),
        ("POST", "/api/connect"),
        ("POST", "/api/disconnect"),
        ("POST", "/api/shutter"),
        ("POST", "/api/gain"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    ],
)
def test_token_guards_all_camera_and_schema_routes(method, path):
    service = CameraService(demo=True)
    with TestClient(
        create_app(service=service, auto_connect=False, token=TOKEN),
        base_url="http://thermal.example",
    ) as client:
        denied = client.request(method, path)
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == "Bearer"
        assert denied.headers["cache-control"] == "no-store"
        assert service.status()["state"] == "disconnected"


@pytest.mark.parametrize(
    "headers",
    [
        [("authorization", "Bearer wrong")],
        [("authorization", f"bearer {TOKEN}")],
        [("authorization", f"Bearer {TOKEN} ")],
        [("authorization", f"Bearer {TOKEN}"), ("authorization", "Bearer wrong")],
        [("authorization", "Bearer wrong"), ("authorization", f"Bearer {TOKEN}")],
    ],
)
def test_token_must_match_exactly_once(headers):
    with TestClient(
        create_app(demo=True, auto_connect=False, token=TOKEN), base_url="http://thermal.example"
    ) as client:
        assert client.post("/api/connect", headers=headers).status_code == 401
        assert (
            client.get("/api/status", headers={"authorization": f"Bearer {TOKEN}"}).json()["state"]
            == "disconnected"
        )


def test_token_allows_remote_demo_and_keeps_origin_checks():
    service = CameraService(demo=True)
    with TestClient(
        create_app(service=service, auto_connect=False, token=TOKEN),
        base_url="http://thermal.example",
    ) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert TOKEN not in page.text
        assert 'type="password"' in page.text
        assert client.get("/static/app.js").status_code == 200
        headers = {"authorization": f"Bearer {TOKEN}", "origin": "http://thermal.example"}
        assert client.post("/api/connect", headers=headers).status_code == 200
        client.headers.update(headers)
        assert wait_for_frame(client)["source"] == "demo"
        assert (
            client.post("/api/disconnect", headers={"origin": "http://other.example"}).status_code
            == 403
        )
        assert client.get("/api/snapshot.npz").status_code == 200
        assert client.post("/api/disconnect").json()["state"] == "disconnected"
    assert not service._thread.is_alive()


@pytest.mark.parametrize("token", ["x" * 23, "x" * 24 + " ", "x" * 24 + "é", "x" * 24 + "\n"])
def test_factory_rejects_invalid_tokens_before_service_creation(token):
    with pytest.raises(ValueError, match="token"):
        create_app(token=token)
