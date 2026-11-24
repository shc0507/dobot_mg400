"""Stop identity and HTTP/socket routing must not depend on command permission."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify

from labkit.errors import ServiceError
from orchestrator.agent_connect.authority import Authority
from orchestrator.agent_connect.commands import CommandHost
from orchestrator.agent_connect.control import install_control
from orchestrator.web.control_socket import _Server

BASE = "/lab/api/agent/control"


def wait_for(check):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("The Stop did not reach its expected state.")


@pytest.fixture
def rig():
    app = Flask(__name__)
    authority = Authority()
    host = install_control(app, authority=authority, mock=True)
    sent = []

    def stop(device):
        sent.append(device)
        return {"device": device, "outcome": "confirmed"}

    host._command_backend = SimpleNamespace(stop=stop)

    @app.errorhandler(ServiceError)
    def error(exc):
        return jsonify(error=exc.message), exc.status

    @app.post("/lab/api/console/stop/stage")
    def legacy_stop():
        return jsonify(stop("stage"))

    yield app, authority, host, sent
    host.close()


def agent(authority):
    invitation = authority.create_invitation(authority.human)["invitation"]
    paired = authority.pair(invitation, "Test agent")
    connection = authority.connect(paired["credential"])
    return connection, {"Authorization": "Bearer " + connection["session_token"]}


def test_observer_stop_and_unknown_credentials_do_not_grant_control(rig):
    app, authority, host, sent = rig
    connection, headers = agent(authority)
    http = app.test_client()
    response = http.post(
        BASE + "/stop", json={"request_id": "observer:1", "target": "stage"}, headers=headers
    )
    assert response.status_code == 202
    wait_for(lambda: len(sent) == 1)
    record = host.stops.get("observer:1")
    assert record["sender"]["name"] == "Test agent"
    assert record["sender"]["role"] == "observer"
    assert record["sender"]["verified"] is True
    assert authority.snapshot()["operator"] is None
    bad = http.post(
        BASE + "/stop",
        json={"request_id": "unverified:1", "target": "stage"},
        headers={"Authorization": "bad"},
    )
    assert bad.status_code == 202
    assert host.stops.get("unverified:1")["sender"]["verified"] is False
    authority.disconnect(authority.authenticate(connection["session_token"]))
    expired = http.post(
        BASE + "/stop", json={"request_id": "expired:1", "target": "stage"}, headers=headers
    )
    assert expired.status_code == 202
    assert host.stops.get("expired:1")["sender"]["role"] == "expired"


def test_stop_does_not_wait_for_authority_lock_or_command_contract(rig):
    app, authority, host, sent = rig
    entered, release = threading.Event(), threading.Event()

    def block():
        with authority.lock:
            entered.set()
            release.wait(3)

    thread = threading.Thread(target=block)
    thread.start()
    assert entered.wait(1)
    try:
        result = app.test_client().post(
            BASE + "/stop",
            json={"request_id": "locked", "target": "stage"},
            headers={"Authorization": "bad", "X-MG400-Control-Epoch": "obsolete"},
        )
        assert result.status_code == 202
        wait_for(lambda: sent)
        assert host.stops.get("locked")["sender"]["verified"] is False
    finally:
        release.set()
        thread.join(3)


def test_http_socket_stop_copies_share_request_and_preserve_both_outcomes(rig):
    app, _, host, sent = rig
    body = {"request_id": "same-press", "target": "stage"}
    first = app.test_client().post(BASE + "/stop", json=body)
    server = object.__new__(_Server)
    server.app, server.prefix = app, "/lab"
    code, second = server.call("page", "/api/agent/control/stop", body, "127.0.0.1")
    assert first.status_code == code == 202
    assert first.json["id"] == second["id"]
    wait_for(lambda: len(sent) == 2)
    record = host.stops.get("same-press")
    assert record["delivery_count"] == 2
    assert {row["transport"] for row in record["deliveries"]} == {"http", "websocket"}


def test_legacy_human_stop_is_only_observed_without_second_dispatch(rig):
    app, _, host, sent = rig
    assert host._commands is None
    headers = {"X-MG400-Stop-ID": "legacy-press"}
    response = app.test_client().post("/lab/api/console/stop/stage", json={}, headers=headers)
    assert response.status_code == 200
    assert sent == ["stage"]
    record = host.stops.get("legacy-press")
    assert record["sender"]["kind"] == "human"
    assert record["status"] == "confirmed"
    assert record["deliveries"][0]["outcomes"] == [{"device": "stage", "outcome": "confirmed"}]
    assert response.headers["X-MG400-Stop-ID"] == "legacy-press"
    assert host._commands is None


@pytest.mark.parametrize("pause", ["admission", "prepare"])
@pytest.mark.parametrize(
    "device,path,target",
    [
        ("robot", "/lab/api/console/stop/robot", "robot"),
        ("robot", "/lab/api/dashboard/motion", "all_motion"),
        ("furnace", "/lab/api/console/stop/furnace", "furnace"),
        ("furnace", "/lab/api/console/ws/process/gas-stop", "furnace"),
    ],
)
def test_legacy_stop_fences_admitted_commands_before_device_dispatch(
    rig, pause, device, path, target
):
    app, authority, host, sent = rig
    connection, _ = agent(authority)
    caller = authority.authenticate(connection["session_token"])
    authority.grant(authority.human, caller.id)
    entered, release, block = threading.Event(), threading.Event(), threading.Event()
    writes = []

    def catalog():
        if pause == "admission" and block.is_set() and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return [{"id": device + ".move", "device": device, "available": True, "params": {}}]

    def prepare(*args):
        if pause == "prepare":
            entered.set()
            assert release.wait(3)
        return {"action": device + ".move"}

    def execute(plan, context):
        context.dispatch(writes.append, device)
        return {"status": "succeeded"}

    backend = SimpleNamespace(
        catalog=catalog,
        prepare=prepare,
        execute=execute,
        furnace_enabled=True,
        cancel=lambda *args: {"status": "stopped"},
    )
    commands = host._commands = CommandHost(host.authority, backend)
    body = {
        "request_id": "before-human-stop",
        "boot_id": authority.boot_id,
        "control_epoch": authority.control_epoch,
        "contract_id": commands.catalog()["contract_id"],
        "action": device + ".move",
        "params": {},
    }

    @app.post(path)
    def human_stop():
        sent.append(target)
        return jsonify(outcome="confirmed")

    block.set()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            admission = executor.submit(commands.submit, caller, body)
            try:
                assert entered.wait(1)
                response = app.test_client().post(path, json={})
                assert response.status_code == 200
                assert sent == [target]
            finally:
                release.set()
            admission.result(timeout=2)
        wait_for(lambda: commands.get(authority.human, body["request_id"])["status"] == "stopped")
        assert writes == []
        assert sent == [target]
    finally:
        release.set()


def test_legacy_pending_operation_retains_identifier_without_claiming_completion(rig):
    app, _, host, sent = rig

    @app.post("/lab/api/dashboard/motion")
    def legacy_motion():
        sent.append("all_motion")
        return jsonify(operation_id="dashboard-stop-1"), 202

    response = app.test_client().post(
        "/lab/api/dashboard/motion", json={}, headers={"X-MG400-Stop-ID": "motion-press"}
    )
    assert response.status_code == 202
    assert sent == ["all_motion"]
    record = host.stops.get("motion-press")
    assert record["status"] == "pending"
    assert record["deliveries"][0]["outcomes"] == [
        {"device": device, "outcome": "pending", "operation_id": "dashboard-stop-1"}
        for device in ("stage", "robot", "microscope")
    ]


@pytest.mark.parametrize("payload", [["invalid"], {"outcome": []}])
def test_legacy_malformed_result_cannot_change_stop_response(rig, payload):
    app, _, host, sent = rig

    @app.post("/lab/api/console/stop/robot")
    def legacy_robot():
        sent.append("robot")
        return jsonify(payload)

    response = app.test_client().post(
        "/lab/api/console/stop/robot", json={}, headers={"X-MG400-Stop-ID": "malformed"}
    )
    assert response.status_code == 200
    assert response.json == payload
    assert sent == ["robot"]
    assert host.stops.get("malformed")["status"] == "unknown"


def test_stop_body_cannot_claim_sender_or_arbitrary_dispatch(rig):
    app, _, _, sent = rig
    for body in (
        {"request_id": "spoof", "target": "stage", "sender": {"name": "Owner"}},
        {"request_id": "heat", "target": "apply_setpoint"},
    ):
        assert app.test_client().post(BASE + "/stop", json=body).status_code == 400
    assert sent == []


def test_physical_stop_policy_never_contacts_shared_services():
    def forbidden(*args, **kwargs):
        pytest.fail("Physical Phase 6 Stop contacted a shared service.")

    app = Flask(__name__)
    host = install_control(
        app, SimpleNamespace(demo=False, console=SimpleNamespace(stop=forbidden))
    )
    try:
        http = app.test_client()
        response = http.post(
            BASE + "/stop", json={"request_id": "physical", "target": "all_motion"}
        )
        assert response.status_code == 202
        record = wait_for(
            lambda: row if (row := host.stops.get("physical"))["status"] != "pending" else None
        )
        assert record["status"] == "disabled"
        contract = http.get(BASE + "/contract").json
        assert contract["phase"] == 8 and contract["version"] == 6
        assert contract["stop_enabled"] is False
        assert contract["physical_commands_enabled"] is False
    finally:
        host.close()
