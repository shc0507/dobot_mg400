"""Exercise independent Stop with local simulated hardware and process failure."""

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from orchestrator.agent_connect.command_store import TERMINAL
from orchestrator.agent_connect.fallback import create_fallback_app
from orchestrator.agent_connect.mock_devices import MockDevices

BASE = "/lab/api/agent/control"
SOURCE = Path(__file__).resolve().parents[1] / "src"
PROCESS = """
import json
import sys
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from werkzeug.serving import make_server
if sys.argv[1] == 'fallback':
    from orchestrator.agent_connect.fallback import create_fallback_app
    from orchestrator.web.stage_preview import StagePreview
    stage = StagePreview()
    app = create_fallback_app(stage)
    application = DispatcherMiddleware(app, {'/stage': stage.app})
else:
    from orchestrator.agent_connect.mock import create_mock_app
    from orchestrator.agent_connect.mock_devices import MockDevices
    devices = MockDevices(sys.argv[2])
    app = create_mock_app(command_service=devices.service)
    application = app
server = make_server('127.0.0.1', 0, application, threaded=True)
print(json.dumps({'port': server.server_port}), flush=True)
server.serve_forever()
"""


def wait_for(read, predicate, *, timeout=5):
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(0.01)
    pytest.fail(f"The local test deadline expired. Last result: {value!r}")


def finished_stop(client, identifier):
    return wait_for(
        lambda: client.get("/stops/" + identifier).get_json(),
        lambda value: value["status"] != "pending",
    )


def test_fallback_default_disables_equipment_without_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Disabled fallback attempted a device or network connection.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    app = create_fallback_app()
    try:
        client = app.test_client()
        state = client.get("/status").get_json()
        assert state["equipment_mode"] == "disabled"
        assert state["physical_commands_enabled"] is False
        assert state["furnace_control_enabled"] is False
        assert state["devices"] == {}
        response = client.post("/stop", json={"request_id": "disabled", "target": "all_motion"})
        assert response.status_code == 202
        result = finished_stop(client, response.json["id"])
        assert result["status"] == "disabled"
        assert {item["device"] for item in result["devices"]} == {"stage", "robot", "microscope"}
        assert all(item["outcome"] == "disabled" for item in result["devices"])
    finally:
        app.extensions["agent_stops"].close()


@pytest.mark.parametrize(
    "url",
    [
        "http://100.64.0.1:8082/stage/contract/",
        "http://192.168.0.2:8872/stage/contract/",
        "http://example.org/stage/contract/",
        "https://127.0.0.1/stage/contract/",
        "http://127.0.0.1/contract/",
        "http://name@127.0.0.1/stage/contract/",
        "http://127.0.0.1/stage/contract/?device=physical",
        "http://127.0.0.1/stage/contract/#physical",
    ],
)
def test_mock_devices_rejects_nonlocal_and_nonfixed_service_urls(monkeypatch, url):
    def forbidden(*args, **kwargs):
        pytest.fail("An invalid simulator URL attempted a connection.")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    with pytest.raises(ValueError):
        MockDevices(url)


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"request_id": "bad", "target": "apply_setpoint"},
        {"request_id": "bad", "target": "heat_off"},
        {"request_id": "bad", "target": "stage", "sender": {"verified": True}},
        {"request_id": "bad", "target": ["stage"]},
        {"request_id": "bad/id", "target": "stage"},
    ],
)
def test_fallback_rejects_unsupported_actions_and_sender_override(body):
    app = create_fallback_app()
    try:
        client = app.test_client()
        assert client.post("/stop", json=body).status_code == 400
        assert client.get("/stops").json["count"] == 0
        assert client.post("/commands", json={"action": "stage.move"}).status_code == 404
        assert client.post("/stage/contract/command", json={}).status_code == 404
    finally:
        app.extensions["agent_stops"].close()


def test_fallback_invalid_credentials_remain_unverified_and_do_not_block_stop():
    app = create_fallback_app()
    try:
        client = app.test_client()
        response = client.post(
            "/stop",
            json={"request_id": "anonymous-stop", "target": "stage"},
            headers={"Authorization": "Bearer not-a-session", "X-MG400-Client": "owner"},
        )
        assert response.status_code == 202
        result = finished_stop(client, response.json["id"])
        sender = result["deliveries"][0]["sender"]
        assert sender["verified"] is False
        assert sender["kind"] == "unverified"
        assert sender["role"] == "unknown"
    finally:
        app.extensions["agent_stops"].close()


def test_fallback_keeps_lost_device_replies_unknown():
    def fail():
        raise TimeoutError("The simulator did not reply.")

    stage = SimpleNamespace(client=lambda *args: SimpleNamespace(stop=fail), state=fail)
    app = create_fallback_app(stage)
    try:
        client = app.test_client()
        assert client.get("/status").json["devices"]["stage"] == {"available": False}
        response = client.post("/stop", json={"request_id": "lost-reply", "target": "all_motion"})
        result = finished_stop(client, response.json["id"])
        assert result["status"] == "unknown"
        assert all(row["outcome"] == "unknown" for row in result["devices"])
    finally:
        app.extensions["agent_stops"].close()


def test_fallback_restart_restores_audit_without_replaying_stop(tmp_path):
    calls = []

    def stop():
        calls.append("stage")
        return {"stop": "confirmed"}

    stage = SimpleNamespace(client=lambda *args: SimpleNamespace(stop=stop), state=lambda: {})
    path = tmp_path / "fallback.db"
    first = create_fallback_app(stage, path=path)
    try:
        client = first.test_client()
        response = client.post("/stop", json={"request_id": "restart-stop", "target": "stage"})
        assert finished_stop(client, response.json["id"])["status"] == "confirmed"
        wait_for(
            lambda: client.get("/stops").json, lambda value: value["audit"]["status"] == "saved"
        )
    finally:
        first.extensions["agent_stops"].close()
    assert calls == ["stage"]
    second = create_fallback_app(stage, path=path)
    try:
        client = second.test_client()
        history = wait_for(lambda: client.get("/stops").json, lambda value: value["count"] == 1)
        assert history["items"][0]["request_id"] == "restart-stop"
        assert history["items"][0]["status"] == "confirmed"
        assert calls == ["stage"]
        repeated = client.post("/stop", json={"request_id": "restart-stop", "target": "stage"})
        result = finished_stop(client, repeated.json["id"])
        assert result["delivery_count"] == 2
        assert calls == ["stage", "stage"]
    finally:
        second.extensions["agent_stops"].close()


@contextmanager
def subprocess_server(tmp_path, mode, *args):
    temporary = tmp_path / mode
    temporary.mkdir()
    environment = dict(
        os.environ,
        PYTHONPATH=str(SOURCE),
        TEMP=str(temporary),
        TMP=str(temporary),
        TMPDIR=str(temporary),
    )
    with (tmp_path / (mode + ".stderr")).open("w+", encoding="utf-8") as errors:
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", PROCESS, mode, *args],
            stdout=subprocess.PIPE,
            stderr=errors,
            text=True,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
        reader.start()
        try:
            try:
                line = lines.get(timeout=10)
                port = json.loads(line)["port"]
            except (queue.Empty, ValueError, KeyError):
                errors.seek(0)
                pytest.fail(f"The {mode} subprocess did not start: {errors.read()}")
            yield process, "http://127.0.0.1:" + str(port)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            reader.join(timeout=1)


def http(url, body=None, *, headers=None, expected=200):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(
        url, data=data, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        response = urlopen(request, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        encoded = response.read().decode()
        try:
            result = json.loads(encoded)
        except ValueError:
            pytest.fail(f"HTTP {response.status} from {url}: {encoded[:500]}")
        assert response.status == expected, result
        return result


def motor(state):
    return next(row for row in state["devices"]["stage"]["motors"] if row["id"] == "insert")


def test_fallback_stops_simulated_motion_after_console_process_is_killed(tmp_path):
    with subprocess_server(tmp_path, "fallback") as (fallback_process, fallback):
        state = http(fallback + "/status")
        assert state["equipment_mode"] == "simulated"
        assert state["physical_commands_enabled"] is False
        with subprocess_server(tmp_path, "console", fallback + "/stage/contract/") as (
            console_process,
            console,
        ):
            base = console + BASE
            invitation = http(base + "/invitations", {})
            paired = http(
                base + "/pair", {"invitation": invitation["invitation"], "name": "Test PC"}
            )
            session = http(base + "/connect", {"credential": paired["credential"]})
            grant = http(base + "/grant", {"session_id": session["session_id"]})
            headers = {"Authorization": "Bearer " + session["session_token"]}
            catalog = http(base + "/commands/catalog")

            def submit(action, params, request_id):
                return http(
                    base + "/commands",
                    {
                        "request_id": request_id,
                        "boot_id": grant["boot_id"],
                        "control_epoch": grant["control_epoch"],
                        "contract_id": catalog["contract_id"],
                        "action": action,
                        "params": params,
                    },
                    headers=headers,
                    expected=202,
                )

            submit("stage.calibrate", {"motor": "insert", "speed": 0.25}, "calibrate")

            def calibration():
                http(base + "/heartbeat", {}, headers=headers)
                return http(base + "/commands/request/calibrate", headers=headers)

            result = wait_for(calibration, lambda value: value["status"] in TERMINAL, timeout=25)
            assert result["status"] == "succeeded", result
            assert motor(http(fallback + "/status"))["calibration"] == "done"

            stop_body = {"request_id": "same-stop-across-paths", "target": "stage"}
            http(base + "/stop", stop_body, headers=headers, expected=202)
            wait_for(
                lambda: http(base + "/stops/" + stop_body["request_id"]),
                lambda value: value["status"] == "confirmed",
            )
            submit(
                "stage.step",
                {"motor": "insert", "direction": "ccw", "rotations": 0.4, "speed": 0.01},
                "long-step",
            )
            wait_for(
                lambda: motor(http(fallback + "/status")),
                lambda value: value["mode"] != "idle",
            )
            console_process.kill()
            console_process.wait(timeout=5)
            assert fallback_process.poll() is None
            assert motor(http(fallback + "/status"))["mode"] != "idle"
            response = http(fallback + "/stop", stop_body, expected=202)
            result = wait_for(
                lambda: http(fallback + "/stops/" + response["id"]),
                lambda value: value["status"] != "pending",
            )
            assert result["status"] == "confirmed", result
            assert result["request_id"] == stop_body["request_id"]
            assert result["transports"] == ["fallback"]
            assert result["deliveries"][0]["sender"]["verified"] is False
            stopped = wait_for(
                lambda: motor(http(fallback + "/status")),
                lambda value: value["mode"] == "idle",
            )
            assert stopped["position_rotations"] < 0.9
            assert http(fallback + "/stops")["count"] == 1
