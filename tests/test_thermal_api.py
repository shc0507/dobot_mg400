"""HTTP contract tests with simulated or disconnected sources only."""

import io
import json
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from thermal_cam.app import create_app
from thermal_cam.service import CameraService


def wait_for_frame(client, prefix=""):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(prefix + "/api/frame")
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


@pytest.mark.parametrize("base_path", ["/thermal", "/lab/thermal/"])
def test_prefixed_page_schema_contract_and_demo_lifecycle(base_path):
    prefix = base_path.rstrip("/")
    service = CameraService(demo=True)
    with TestClient(
        create_app(service=service, auto_connect=False, token=TOKEN, base_path=base_path),
        base_url="http://lab.example:8080",
    ) as client:
        page = client.get(prefix + "/")
        assert page.status_code == 200 and TOKEN not in page.text
        assert f'data-base-path="{prefix}"' in page.text
        for link in ("/", "/static/style.css", "/static/app.js", "/docs"):
            assert f'"{prefix}{link}"' in page.text
        assert "__THERMAL_BASE_PATH__" not in page.text
        assert client.get(prefix, follow_redirects=False).status_code == 307
        for path in ("/static/style.css", "/static/app.js"):
            response = client.get(prefix + path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
        for path in ("/api/state", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            assert client.get(prefix + path).status_code == 401
        assert client.get("/static/app.js").status_code == 401
        assert client.get(prefix + "-other/static/app.js").status_code == 401

        client.headers.update(
            {"Authorization": f"Bearer {TOKEN}", "Origin": "http://lab.example:8080"}
        )
        for path in ("/", "/api/status", "/api/state", "/docs", "/static/app.js"):
            assert client.get(path).status_code == 404
        schema = client.get(prefix + "/openapi.json").json()
        assert prefix + "/api/state" in schema["paths"]
        assert all(path.startswith(prefix + "/") for path in schema["paths"])
        assert prefix + "/openapi.json" in client.get(prefix + "/docs").text
        assert prefix + "/docs/oauth2-redirect" in client.get(prefix + "/docs").text
        assert prefix + "/openapi.json" in client.get(prefix + "/redoc").text
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

        assert client.post(prefix + "/api/connect").status_code == 200
        assert wait_for_frame(client, prefix)["source"] == "demo"
        assert client.get(roi_path).status_code == 200
        assert client.get(prefix + "/api/snapshot.npz").status_code == 200
        assert client.post(prefix + "/api/shutter").json()["ok"]
        assert client.post(prefix + "/api/gain", json={"gain": "high"}).json()["ok"]
        assert client.post(prefix + "/api/disconnect").json()["state"] == "disconnected"
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
def test_prefixed_access_rejections_preserve_legacy_and_contract_errors(headers, status):
    with TestClient(
        create_app(demo=True, auto_connect=False, token=TOKEN, base_path="/thermal"),
        base_url="http://lab.example:8080",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        for path, key in (("/api/connect", "detail"), ("/api/heartbeat", "error")):
            response = client.post("/thermal" + path, headers=headers, json={})
            assert response.status_code == status
            assert key in response.json()
        assert client.get("/thermal/api/status").json()["state"] == "disconnected"


@pytest.mark.parametrize(
    "header", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP"]
)
def test_prefixed_tokenless_app_rejects_forwarding_before_commands(header):
    with TestClient(
        create_app(demo=True, auto_connect=False, base_path="/thermal"),
        base_url="http://127.0.0.1",
    ) as client:
        headers = {header: "", "X-MG400-Client": "page-owner"}
        assert client.get("/thermal/", headers=headers).status_code == 400
        assert client.post("/thermal/api/connect", headers=headers).status_code == 400
        response = client.post("/thermal/api/heartbeat", headers=headers, json={})
        assert response.status_code == 403 and "error" in response.json()
        assert client.get("/thermal/api/status").json()["state"] == "disconnected"


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


def test_browser_requests_use_rendered_prefix_without_exposing_token():
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const source = fs.readFileSync("src/thermal_cam/static/app.js", "utf8");
const start = source.indexOf("async function request(");
const end = source.indexOf("function connectionUI(", start);
const calls = [];
const token = "thermal-page-test-token-00000000";
globalThis.document = {body: {dataset: {basePath: "/lab/thermal"}}};
const fetch = async (path, options) => {
  calls.push({path, options});
  return {ok: true, json: async () => ({ok: true})};
};
const request = new Function("fetch", "$", "Headers", "AbortSignal",
  source.slice(start, end) + "return request;")(
    fetch, () => ({value: token}), Headers, AbortSignal);
(async () => {
  await request("/api/status");
  await request("/api/gain", {method: "POST", headers: {"Content-Type": "application/json"},
    body: '{"gain":"high"}'});
  assert.deepEqual(calls.map(call => call.path),
    ["/lab/thermal/api/status", "/lab/thermal/api/gain"]);
  assert(calls.every(call => !call.path.includes(token)
    && call.options.headers.get("Authorization") === `Bearer ${token}`));
  assert.equal(calls[1].options.method, "POST");
  assert.equal(calls[1].options.headers.get("Content-Type"), "application/json");
  assert.deepEqual(JSON.parse(calls[1].options.body), {gain: "high"});
  globalThis.document.body.dataset.basePath = "";
  await request("/api/status");
  assert.equal(calls[2].path, "/api/status");
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    subprocess.run(
        ["node", "-e", script], check=True, cwd=Path(__file__).resolve().parents[1], timeout=30
    )
