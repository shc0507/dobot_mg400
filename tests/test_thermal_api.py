"""HTTP contract tests with simulated or disconnected sources only."""

import time

import numpy as np
import pytest
from contract_helpers import assert_error
from fastapi.testclient import TestClient

from thermal_cam.app import create_app
from thermal_cam.service import CameraService

TOKEN = "thermal-test-token-length-24"
CLIENT = {"X-MG400-Client": "lab-console"}
CONNECT = {"action": "connect"}
SHUTTER = {"action": "shutter"}
EMPTY_PARAMS = {"type": "object", "properties": {}, "additionalProperties": False}


def wait_for_frame(client, prefix=""):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(prefix + "/api/frame")
        if response.status_code == 200:
            return response.json()
        time.sleep(0.01)
    raise AssertionError("No demo frame arrived")


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Demo worker did not reach the expected state")


def command(client, data, prefix="", headers=None):
    return client.post(prefix + "/api/command", headers={**CLIENT, **(headers or {})}, json=data)


def test_disconnected_endpoints_and_input_validation():
    service = CameraService(demo=True)
    with TestClient(
        create_app(service=service, auto_connect=False), base_url="http://127.0.0.1"
    ) as client:
        state = client.get("/api/state")
        assert state.status_code == 200
        assert state.json()["state"] == "disconnected"
        assert state.json()["connected"] is False
        assert state.headers["cache-control"] == "no-store"
        assert client.get("/api/frame").status_code == 503
        assert_error(command(client, SHUTTER), 409)
        assert client.get("/api/state").json()["last_command"] is None


def test_demo_frame_content_and_lifespan_cleanup():
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
    assert service.status()["state"] == "disconnected"
    assert not service._thread.is_alive()


def test_describe_lists_connect_and_shutter_for_any_client():
    with TestClient(
        create_app(demo=True, auto_connect=False), base_url="http://127.0.0.1"
    ) as client:
        commands = client.get("/api/describe").json()["commands"]
        assert commands == {
            "connect": {"params": EMPTY_PARAMS, "any_client": True},
            "shutter": {"params": EMPTY_PARAMS, "any_client": True},
        }


def test_connect_command_after_disconnect_restarts_frames():
    service = CameraService(demo=True)
    with TestClient(create_app(service=service), base_url="http://127.0.0.1") as client:
        first = wait_for_frame(client)
        service.disconnect()
        assert client.get("/api/frame").status_code == 503
        response = command(client, CONNECT)
        assert response.status_code == 202
        body = response.json()
        assert set(body) == {"command_id", "state"}
        last = body["state"]["last_command"]
        assert last["id"] == body["command_id"]
        assert (last["action"], last["client"], last["result"]) == (
            "connect",
            "lab-console",
            "ok",
        )
        assert wait_for_frame(client)["sequence"] > first["sequence"]
        assert client.get("/api/state").json()["connected"] is True
    assert not service._thread.is_alive()


def test_connect_command_while_streaming_keeps_acquisition():
    service = CameraService(demo=True)
    with TestClient(create_app(service=service), base_url="http://127.0.0.1") as client:
        wait_for_frame(client)
        thread = service._thread
        assert command(client, CONNECT).status_code == 202
        assert service._thread is thread and thread.is_alive()


def test_shutter_command_in_demo_reports_ok():
    service = CameraService(demo=True)
    with TestClient(create_app(service=service), base_url="http://127.0.0.1") as client:
        wait_for_frame(client)
        response = command(client, SHUTTER)
        assert response.status_code == 202
        ident = response.json()["command_id"]
        last = wait_until(
            lambda: (
                (value := client.get("/api/state").json()["last_command"])
                and value["id"] == ident
                and value
            )
        )
        assert last["action"] == "shutter"
        assert last["client"] == "lab-console"
        assert last["result"] == "ok"
        assert last["message"] == ""
        assert isinstance(last["finished_at"], float)


def test_shutter_command_when_disconnected_is_conflict():
    service = CameraService(demo=True)
    with TestClient(create_app(service=service), base_url="http://127.0.0.1") as client:
        wait_for_frame(client)
        service.disconnect()
        assert_error(command(client, SHUTTER), 409)
        assert client.get("/api/state").json()["last_command"] is None


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"action": "gain"},
        {"action": "disconnect"},
        {"action": None},
        {"action": ["shutter"]},
        {"action": "connect", "force": True},
        {"action": "shutter", "gain": "high"},
    ],
)
def test_command_rejects_unknown_actions_and_params(data):
    service = CameraService(demo=True)
    with TestClient(
        create_app(service=service, auto_connect=False), base_url="http://127.0.0.1"
    ) as client:
        assert_error(command(client, data), 400)
        state = client.get("/api/state").json()
        assert state["state"] == "disconnected"
        assert state["last_command"] is None


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/"),
        ("GET", "/static/app.js"),
        ("GET", "/static/style.css"),
        ("GET", "/docs"),
        ("GET", "/redoc"),
        ("GET", "/openapi.json"),
        ("GET", "/api/status"),
        ("GET", "/api/snapshot.npz"),
        ("POST", "/api/connect"),
        ("POST", "/api/disconnect"),
        ("POST", "/api/shutter"),
        ("POST", "/api/gain"),
    ],
)
@pytest.mark.parametrize("base_path", ["/", "/thermal"])
def test_removed_routes_return_not_found(method, path, base_path):
    prefix = base_path.rstrip("/")
    service = CameraService(demo=True)
    with TestClient(
        create_app(service=service, auto_connect=False, token=TOKEN, base_path=base_path),
        base_url="http://lab.example:8080",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        assert client.request(method, prefix + path, json={}).status_code == 404
        assert service.status()["state"] == "disconnected"


def test_rejects_foreign_hosts_and_browser_control_origins():
    with TestClient(
        create_app(demo=True, auto_connect=False), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/frame", headers={"host": "untrusted.example"}).status_code == 400
        denied = command(client, CONNECT, headers={"origin": "https://untrusted.example"})
        assert_error(denied, 403)
        assert client.get("/api/state").json()["state"] == "disconnected"
        assert command(client, CONNECT, headers={"origin": "http://127.0.0.1"}).status_code == 202


@pytest.mark.parametrize(
    "header", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP"]
)
@pytest.mark.parametrize("value", ["", "127.0.0.1"])
def test_tokenless_app_rejects_proxy_headers(header, value):
    with TestClient(
        create_app(demo=True, auto_connect=False), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/frame", headers={header: value}).status_code == 400
        assert_error(command(client, CONNECT, headers={header: value}), 403)
        assert client.get("/api/state").json()["state"] == "disconnected"


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
        response = client.post("/api/command", headers=[*CLIENT.items(), *headers], json=CONNECT)
        assert_error(response, 400)
        assert client.get("/api/frame", headers=headers).status_code == 400
        assert client.get("/api/state").json()["state"] == "disconnected"


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
        response = client.post("/api/command", headers=[*CLIENT.items(), *headers], json=CONNECT)
        assert_error(response, 403)
        assert client.get("/api/state").json()["state"] == "disconnected"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/"),
        ("GET", "/static/app.js"),
        ("GET", "/api/frame"),
        ("GET", "/api/state"),
        ("GET", "/api/describe"),
        ("POST", "/api/command"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    ],
)
def test_token_guards_every_path(method, path):
    service = CameraService(demo=True)
    with TestClient(
        create_app(service=service, auto_connect=False, token=TOKEN),
        base_url="http://thermal.example",
    ) as client:
        denied = client.request(method, path, headers=CLIENT, json=CONNECT)
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
        response = client.post("/api/command", headers=[*CLIENT.items(), *headers], json=CONNECT)
        assert_error(response, 401)
        assert client.get("/api/frame", headers=headers).status_code == 401
        assert (
            client.get("/api/state", headers={"authorization": f"Bearer {TOKEN}"}).json()["state"]
            == "disconnected"
        )


def test_token_allows_remote_demo_and_keeps_origin_checks():
    service = CameraService(demo=True)
    with TestClient(
        create_app(service=service, auto_connect=False, token=TOKEN),
        base_url="http://thermal.example",
    ) as client:
        headers = {"authorization": f"Bearer {TOKEN}", "origin": "http://thermal.example"}
        assert command(client, CONNECT, headers=headers).status_code == 202
        client.headers.update(headers)
        assert wait_for_frame(client)["source"] == "demo"
        assert_error(command(client, SHUTTER, headers={"origin": "http://other.example"}), 403)
        assert (
            client.get("/api/frame", headers={"origin": "http://other.example"}).status_code == 403
        )
        assert command(client, SHUTTER).status_code == 202
    assert not service._thread.is_alive()


@pytest.mark.parametrize("token", ["x" * 23, "x" * 24 + " ", "x" * 24 + "é", "x" * 24 + "\n"])
def test_factory_rejects_invalid_tokens_before_service_creation(token):
    with pytest.raises(ValueError, match="token"):
        create_app(token=token)


@pytest.mark.parametrize("base_path", ["/thermal", "/lab/thermal/"])
def test_prefixed_contract_and_demo_lifecycle(base_path):
    prefix = base_path.rstrip("/")
    service = CameraService(demo=True)
    with TestClient(
        create_app(service=service, auto_connect=False, token=TOKEN, base_path=base_path),
        base_url="http://lab.example:8080",
    ) as client:
        for path in ("", "/", "/api/state", "/api/frame", "/docs", "/openapi.json"):
            assert client.get(prefix + path, follow_redirects=False).status_code == 401
        assert client.get("/api/state").status_code == 401
        assert client.get(prefix + "-other/api/state").status_code == 401

        client.headers.update(
            {"Authorization": f"Bearer {TOKEN}", "Origin": "http://lab.example:8080"}
        )
        for path in ("/api/state", "/api/frame", "/api/describe"):
            assert client.get(path).status_code == 404
        assert client.get(prefix + "/api/describe").json()["service"] == "thermal"
        assert client.get(prefix + "/api/state").json()["demo"] is True
        assert client.get(prefix + "/api/events").status_code == 200
        roi_path = prefix + "/api/roi?x0=0&y0=0&x1=10&y1=10"
        assert client.get(roi_path).status_code == 503
        owner = {"X-MG400-Client": "page-owner"}
        other = {"X-MG400-Client": "other-page"}
        assert client.post(prefix + "/api/heartbeat", headers=owner, json={}).status_code == 200
        assert client.post(prefix + "/api/renew", headers=owner, json={}).status_code == 200
        assert client.post(prefix + "/api/heartbeat", headers=other, json={}).status_code == 409
        assert client.post(prefix + "/api/command", headers=owner, json={}).status_code == 400
        assert client.post(prefix + "/api/stop", headers=other, json={}).status_code == 200
        assert client.post(prefix + "/api/release", headers=owner, json={}).status_code == 200

        assert command(client, CONNECT, prefix).status_code == 202
        assert wait_for_frame(client, prefix)["source"] == "demo"
        assert client.get(roi_path).status_code == 200
        assert command(client, SHUTTER, prefix).status_code == 202
    assert service.status()["state"] == "disconnected"
    assert not service._thread.is_alive()


@pytest.mark.parametrize(
    "headers,status",
    [
        ([("host", "lab.example:8080"), ("host", "other.example")], 400),
        ([("host", "user@lab.example")], 400),
        ([("origin", "http://other.example")], 403),
        ([("origin", "http://lab.example:8080"), ("origin", "http://lab.example:8080")], 403),
        ([("sec-fetch-site", "cross-site")], 403),
        ([("authorization", "Bearer wrong")], 401),
        ([("authorization", f"Bearer {TOKEN}"), ("authorization", f"Bearer {TOKEN}")], 401),
        ([("origin", "http://other.example"), ("x-forwarded-host", "other.example")], 403),
    ],
)
def test_prefixed_access_rejections_preserve_frame_and_contract_errors(headers, status):
    with TestClient(
        create_app(demo=True, auto_connect=False, token=TOKEN, base_path="/thermal"),
        base_url="http://lab.example:8080",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        frame = client.get("/thermal/api/frame", headers=headers)
        assert frame.status_code == status
        assert "detail" in frame.json()
        for path in ("/api/command", "/api/heartbeat"):
            response = client.post(
                "/thermal" + path, headers=[*CLIENT.items(), *headers], json=CONNECT
            )
            assert_error(response, status)
        assert client.get("/thermal/api/state").json()["state"] == "disconnected"


@pytest.mark.parametrize(
    "header", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP"]
)
def test_prefixed_tokenless_app_rejects_forwarding_before_commands(header):
    with TestClient(
        create_app(demo=True, auto_connect=False, base_path="/thermal"),
        base_url="http://127.0.0.1",
    ) as client:
        headers = {header: "", **CLIENT}
        frame = client.get("/thermal/api/frame", headers=headers)
        assert frame.status_code == 400 and "detail" in frame.json()
        assert_error(client.post("/thermal/api/command", headers=headers, json=CONNECT), 403)
        assert_error(client.post("/thermal/api/heartbeat", headers=headers, json={}), 403)
        assert client.get("/thermal/api/state").json()["state"] == "disconnected"


@pytest.mark.parametrize(
    "base_path",
    [
        None,
        1,
        "",
        "thermal",
        "//thermal",
        "/thermal//",
        "/./thermal",
        "/thermal/..",
        "/thermal?x",
        "/thermal#x",
        "/thermal%2fother",
        "/thermal\\other",
        '/thermal"',
        "/thermal\n",
        "/热像",
    ],
)
def test_bad_prefix_refuses_before_controller_construction(monkeypatch, base_path):
    from thermal_cam import app

    def forbidden(**kwargs):
        pytest.fail("Invalid base path constructed a camera service")

    monkeypatch.setattr(app, "CameraService", forbidden)
    with pytest.raises(ValueError, match="base path"):
        create_app(base_path=base_path)
