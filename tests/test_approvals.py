"""In-process approval policy and HTTP tests. No instruments or sockets are opened."""

import copy
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from labkit.errors import ServiceError
from orchestrator.app import OrchestratorService, create_app
from orchestrator.approvals import EXPIRY_S, MAX_HISTORY, prepare
from orchestrator.skills import Registry

OPERATOR = "operator-approval-test-credential"
GATEWAY = "gateway-approval-test-credential"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Approval test attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    catalog = tmp_path / "recipes"
    catalog.mkdir()
    (catalog / "wait.toml").write_text(
        'name="Wait"\nversion=1\n[params]\n[[steps]]\nskill="wait"\n'
        "params={seconds=0.0}\ntimeout_s=1\n"
    )
    clock = [100.0]
    settings = {"MG400_GATEWAY_TOKEN": GATEWAY, "MG400_GATEWAY_CALLER": "test-agent"}
    service = OrchestratorService(
        settings=settings,
        recipes_dir=catalog,
        demo=True,
        client_factory=lambda required, identity: {},
        clock=lambda: clock[0],
    ).start(background=False)
    service.approvals.wall = lambda: 1000 + clock[0]
    app = create_app(service, token=OPERATOR)
    yield service, app.test_client(), clock
    service.close()


def headers(token=OPERATOR, actor="person"):
    return {"Authorization": "Bearer " + token, "X-MG400-Client": actor}


def proposal(client, **changes):
    return client.post(
        "/api/proposals",
        headers=headers(GATEWAY, "spoofed-header"),
        json={"recipe": "wait", "params": {}, "reason": "Review this run", **changes},
    )


def decision(client, record, action="approve", **changes):
    return client.post(
        "/api/approvals/" + record["id"] + "/decision",
        headers=headers(),
        json={"decision": action, "present": True, "fingerprint": record["fingerprint"], **changes},
    )


def settle(service):
    end = time.monotonic() + 3
    while (
        service.current is None
        or not service.job["done"].is_set()
        or not service.current.wait_closed(0)
    ):
        if time.monotonic() >= end:
            pytest.fail("Fake run did not settle")
        time.sleep(0.005)


def test_propose_review_approve_runs_once_through_existing_control(rig):
    service, client, _ = rig
    response = proposal(client)
    assert response.status_code == 201
    record = response.json
    assert record["requester"] == "test-agent"
    assert record["decision"] == "pending" and record["decider"] is None
    assert record["expires_at"] - record["created_at"] == EXPIRY_S
    assert record["effective"]["steps"][0]["skill"] == "wait"
    assert service.current is None and service.store.runs() == []
    assert client.get("/api/approvals", headers=headers()).json["approvals"] == [record]
    assert client.post("/api/heartbeat", headers=headers(), json={}).status_code == 200
    accepted = decision(client, record)
    assert accepted.status_code == 202
    assert accepted.json["decider"] == "person"
    assert accepted.json["command_id"]
    settle(service)
    assert len(service.store.runs()) == 1
    assert decision(client, record).status_code == 409
    audit = list(reversed(service.store.audit_calls()))
    assert [row["tool"] for row in audit] == [
        "approval.proposed",
        "approval.approved",
        "approval.dispatch-accepted",
    ]
    assert json.loads(audit[1]["arguments_json"])["decider"] == "person"


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/heartbeat", "POST"),
        ("/api/renew", "POST"),
        ("/api/release", "POST"),
        ("/api/command", "POST"),
        ("/api/stop", "POST"),
        ("/api/runs", "POST"),
        ("/api/runs/id/resume", "POST"),
        ("/api/runs/id/abort", "POST"),
        ("/api/runs/id/recovery", "POST"),
        ("/api/runs/id/recovery/confirm", "POST"),
        ("/api/dashboard/motion", "POST"),
        ("/api/dashboard/gas", "POST"),
        ("/api/dashboard/heat", "POST"),
        ("/api/approvals/id/decision", "POST"),
        ("/api/approvals", "GET"),
        ("/api/approvals", "HEAD"),
        ("/api/approvals/id/decision", "OPTIONS"),
        ("/api/%72uns", "POST"),
    ],
)
def test_gateway_credential_has_no_operator_capability(rig, path, method):
    service, client, _ = rig
    response = client.open(path, method=method, headers=headers(GATEWAY), json={})
    assert response.status_code == 401
    assert service.job is None and service.lease.snapshot()["holder"] is None


@pytest.mark.parametrize(
    "change,status",
    [
        ({"Authorization": "Bearer " + OPERATOR}, 401),
        ({"Host": "remote.invalid"}, 403),
        ({"Origin": "http://other.invalid"}, 403),
        ({"X-Forwarded-Host": "localhost"}, 403),
        ({"Content-Type": "text/plain"}, 415),
        ({"X-MG400-Client": ""}, 400),
    ],
)
def test_proposal_routes_preserve_access_rules(rig, change, status):
    _, client, _ = rig
    response = client.post(
        "/api/proposals",
        headers={**headers(GATEWAY), **change},
        data='{"recipe":"wait","params":{},"reason":"check"}',
        content_type="application/json" if "Content-Type" not in change else None,
    )
    assert response.status_code == status


def test_strict_json_and_bad_request_cannot_store_proposal(rig):
    service, client, _ = rig
    for raw in [
        '{"recipe":"wait","recipe":"wait"}',
        '{"recipe":"wait","params":{"x":NaN},"reason":"x"}',
    ]:
        assert (
            client.post(
                "/api/proposals",
                headers=headers(GATEWAY),
                data=raw,
                content_type="application/json",
            ).status_code
            == 400
        )
    for changes in (
        {"reason": ""},
        {"params": []},
        {"recipe": "../wait"},
        {"params": {"x": 1}},
        {"caller": "person"},
    ):
        assert proposal(client, **changes).status_code == 400
    assert service.approvals.list() == []


def test_reject_expire_and_no_presence_never_start(rig):
    service, client, clock = rig
    first = proposal(client).json
    assert decision(client, first, present=False).status_code == 400
    assert decision(client, first, "reject", present=False).json["decision"] == "rejected"
    assert decision(client, first).status_code == 409
    expired = proposal(client).json
    clock[0] += EXPIRY_S
    assert decision(client, expired).status_code == 409
    assert service.approvals.get(expired["id"])["decision"] == "expired"
    assert service.job is None


def test_identity_and_display_fingerprint_are_required(rig):
    _, client, _ = rig
    record = proposal(client).json
    url = "/api/approvals/" + record["id"] + "/decision"
    data = {"decision": "approve", "present": True, "fingerprint": record["fingerprint"]}
    assert client.post(url, headers=headers(actor="test-agent"), json=data).status_code == 403
    assert decision(client, record, fingerprint="changed").status_code == 409
    assert decision(client, record, present=1).status_code == 400


def test_catalog_and_host_policy_drift_refuse_dispatch(rig):
    service, client, _ = rig
    record = proposal(client).json
    client.post("/api/heartbeat", headers=headers(), json={})
    path = service.catalog.directory / "wait.toml"
    path.write_text(path.read_text().replace("seconds=0.0", "seconds=0.1"))
    assert decision(client, record).status_code == 409
    assert service.approvals.get(record["id"])["decision"] == "pending"
    assert service.job is None


def test_missing_lease_refuses_before_a_decision_is_recorded(rig):
    service, client, _ = rig
    record = proposal(client).json
    assert decision(client, record).status_code == 409
    saved = service.approvals.get(record["id"])
    assert saved["decision"] == "pending" and saved["decider"] is None
    assert service.job is None
    assert client.post("/api/heartbeat", headers=headers(), json={}).status_code == 200
    assert decision(client, record).status_code == 202
    settle(service)


def test_concurrent_proposals_and_decisions_are_bounded_and_one_shot(rig):
    service, client, _ = rig

    def propose_one(_):
        with client.application.test_client() as separate:
            return proposal(separate).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(propose_one, range(8)))
    assert statuses.count(201) == 3 and statuses.count(409) == 5
    record = service.approvals.list()[0]
    client.post("/api/heartbeat", headers=headers(), json={})

    def approve_one(_):
        with client.application.test_client() as separate:
            return decision(separate, record).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(approve_one, range(4)))
    assert statuses.count(202) == 1 and statuses.count(409) == 3
    settle(service)
    assert len(service.store.runs()) == 1


def test_audit_failure_prevents_storage_or_execution(rig, monkeypatch):
    service, client, _ = rig
    original = service.store.audit_call

    def fail(*args):
        raise OSError("private-secret-path")

    monkeypatch.setattr(service.store, "audit_call", fail)
    result = proposal(client)
    assert result.status_code == 503 and "private-secret-path" not in result.text
    assert service.approvals.records == {}
    monkeypatch.setattr(service.store, "audit_call", original)
    record = proposal(client).json
    client.post("/api/heartbeat", headers=headers(), json={})
    monkeypatch.setattr(service.store, "audit_call", fail)
    assert decision(client, record).status_code == 503
    assert service.approvals.records[record["id"]]["decision"] == "pending"
    assert service.job is None


@pytest.mark.parametrize("boundary", ["prepare", "audit"])
def test_expiry_during_validation_or_audit_cannot_dispatch(rig, monkeypatch, boundary):
    service, client, clock = rig
    record = proposal(client).json
    clock[0] += EXPIRY_S - 1
    client.post("/api/heartbeat", headers=headers(), json={})
    if boundary == "prepare":
        original = service.catalog.read

        def slow(name):
            clock[0] += 2
            return original(name)

        monkeypatch.setattr(service.catalog, "read", slow)
    else:
        original = service.store.audit_call

        def slow(*args):
            result = original(*args)
            clock[0] += 2
            return result

        monkeypatch.setattr(service.store, "audit_call", slow)
    assert decision(client, record).status_code == 409
    assert service.job is None
    assert service.approvals.get(record["id"])["decision"] == "expired"


def test_public_records_cannot_mutate_authorized_definition_and_history_is_bounded(rig):
    service, client, _ = rig
    first = proposal(client).json
    saved = service.approvals.get(first["id"])
    saved["definition"]["steps"][0]["skill"] = "robot.move"
    saved["effective"]["steps"][0]["params"]["seconds"] = 100
    assert service.approvals.get(first["id"])["definition"]["steps"][0]["skill"] == "wait"
    for _ in range(MAX_HISTORY + 1):
        record = service.approvals.propose(
            {"recipe": "wait", "params": {}, "reason": "bounded"}, "agent"
        )
        service.approvals.decide(
            record["id"],
            {"decision": "reject", "present": False, "fingerprint": record["fingerprint"]},
            "person",
        )
    assert len(service.approvals.records) == MAX_HISTORY
    assert service.approvals.get(first["id"])["decision"] == "pending"


def test_restart_does_not_reconstruct_pending_authority(rig):
    from orchestrator.approvals import Approvals

    service, client, _ = rig
    record = proposal(client).json
    restored = Approvals(service)
    with pytest.raises(ServiceError, match="invalidated"):
        restored.get(record["id"])
    assert any(row["tool"] == "approval.proposed" for row in service.store.audit_calls())


def catalog_step(service, monkeypatch, skill, params, schemas=None):
    data = {
        "name": "Policy",
        "version": 1,
        "params": schemas or {},
        "steps": [{"skill": skill, "params": params, "timeout_s": 10}],
    }
    monkeypatch.setattr(service.catalog, "read", lambda name: copy.deepcopy(data))
    return data


def test_free_robot_coordinates_require_all_finite_workspace_bounds(rig, monkeypatch):
    service, _, _ = rig
    catalog_step(
        service, monkeypatch, "robot.move", {"x_mm": 0.0, "y_mm": 0.0, "z_mm": 0.0, "r_deg": 0.0}
    )
    with pytest.raises(ValueError, match="workspace"):
        prepare(service, "robot", {})
    for axis in "XYZR":
        unit = "DEG" if axis == "R" else "MM"
        service.settings[f"MG400_ORCH_{axis}_MIN_{unit}"] = "-10"
        service.settings[f"MG400_ORCH_{axis}_MAX_{unit}"] = "10"
    assert prepare(service, "robot", {})[1]["effective"]["steps"][0]["skill"] == "robot.move"
    service.settings["MG400_ORCH_X_MIN_MM"] = "1"
    with pytest.raises(ValueError, match="bounds"):
        prepare(service, "robot", {})
    service.settings["MG400_ORCH_X_MIN_MM"] = "nan"
    with pytest.raises(ValueError):
        prepare(service, "robot", {})


@pytest.mark.parametrize(
    "skill", ["stage.move", "stage.micro", "firmware.command", "custom.actuate", "furnace.setpoint"]
)
def test_unrecognized_skills_fail_closed(rig, monkeypatch, skill):
    service, _, _ = rig
    service.registry.register(skill, lambda ctx: None)
    catalog_step(service, monkeypatch, skill, {})
    with pytest.raises(ValueError, match="not supported"):
        prepare(service, "custom", {})


def test_speed_cannot_exceed_recipe_default_even_with_renamed_parameter(rig, monkeypatch):
    service, _, _ = rig
    registry = Registry()

    def pick(ctx, speed: float = 2):
        pass

    registry.register("robot.pick", pick)
    service.registry = registry
    catalog_step(
        service,
        monkeypatch,
        "robot.pick",
        {"speed": "${velocity}"},
        {"velocity": {"type": "number", "default": 1.0}},
    )
    assert prepare(service, "pick", {"velocity": 0.5})
    with pytest.raises(ValueError, match="default speed"):
        prepare(service, "pick", {"velocity": 2.0})


def test_crucible_limits_are_pinned_from_host_and_cannot_be_proposed(rig, monkeypatch):
    service, _, _ = rig
    # Isolate policy resolution from the independent zone configuration checks.
    monkeypatch.setattr("orchestrator.approvals.prepare_zones", lambda *args: object())
    data = catalog_step(service, monkeypatch, "crucible.load", {"from_slot": "tray-1"})
    for key, value in {"LOAD_MAX_C": "40", "AWAKE_S": "30", "MAX_AGE_S": "1"}.items():
        service.settings["MG400_ORCH_CRUCIBLE_" + key] = value
    prepared, visible, digest = prepare(service, "load", {})
    assert prepared[0].steps[0].params["load_max_c"] == 40
    assert visible["effective"]["steps"][0]["params"]["awake_s"] == 30
    service.settings["MG400_ORCH_CRUCIBLE_LOAD_MAX_C"] = "35"
    assert prepare(service, "load", {})[2] != digest
    data["steps"][0]["params"]["load_max_c"] = "${harmless}"
    data["params"] = {"harmless": {"type": "number", "default": 35.0}}
    with pytest.raises(ValueError, match="host settings"):
        prepare(service, "load", {})


def test_credentials_cannot_be_equal_or_leak_through_recipe(rig, monkeypatch):
    service, client, _ = rig
    with pytest.raises(ValueError, match="distinct"):
        create_app(service, token=GATEWAY)
    service.settings["MG400_ORCH_ROBOT_TOKEN"] = GATEWAY
    with pytest.raises(ValueError, match="distinct"):
        create_app(service, token=OPERATOR)
    del service.settings["MG400_ORCH_ROBOT_TOKEN"]
    catalog_step(service, monkeypatch, "sample.record", {"chip_id": "chip", "note": OPERATOR})
    service.settings["MG400_ORCH_TOKEN"] = OPERATOR
    response = proposal(client)
    assert response.status_code == 400 and OPERATOR not in response.text


def test_creation_audit_cannot_extend_the_displayed_expiry(rig, monkeypatch):
    service, client, clock = rig
    original = service.store.audit_call

    def slow(caller, event, record):
        result = original(caller, event, record)
        if event == "approval.proposed":
            clock[0] += EXPIRY_S + 1
        return result

    monkeypatch.setattr(service.store, "audit_call", slow)
    response = proposal(client)
    assert response.status_code == 201
    record = response.json
    assert record["decision"] == "expired"
    assert service.approvals.wall() > record["expires_at"]
    assert decision(client, record).status_code == 409
    assert service.job is None


def test_approval_keeps_existing_admission_and_lease_revocation_fences(rig):
    service, client, _ = rig
    entered, release = threading.Event(), threading.Event()

    def delayed_clients(required, identity):
        entered.set()
        assert release.wait(3)
        return {}

    service.client_factory = delayed_clients
    first, second = proposal(client).json, proposal(client).json
    client.post("/api/heartbeat", headers=headers(), json={})
    try:
        assert decision(client, first).status_code == 202
        assert entered.wait(1)
        assert decision(client, second).status_code == 409
        assert service.approvals.get(second["id"])["dispatch"] == "not-confirmed"
        assert client.post("/api/release", headers=headers(), json={}).status_code == 200
        assert service.job["cancelled"].is_set()
    finally:
        release.set()
    assert service.job["done"].wait(2)
    assert service.store.runs() == []
    assert decision(client, first).status_code == 409


def test_approval_ui_enforces_presence_control_and_one_shot_decisions():
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    harness = (root / "tests/test_dashboard.mjs").read_text().split('\ntest("', 1)[0]
    script = (
        harness
        + r"""
const proposal = overrides => ({
  id: "a".repeat(32), recipe: "wait", requester: "agent", reason: "<img src=x>",
  decision: "pending", decider: null, expires_at: 9999999999,
  fingerprint: "f".repeat(64), definition: {steps: [{skill: "wait"}]},
  params: {chip: "<script>alert(1)</script>"},
  effective: {steps: [{skill: "wait", params: {seconds: 0}}]}, ...overrides,
});
const approvalButton = (page, text) => descendants(page.get("approvals")).find(
  element => element.tagName === "BUTTON" && element.textContent === text);
const presence = page => descendants(page.get("approvals")).find(
  element => element.tagName === "INPUT");
{
  const page = browser(), row = proposal();
  await page.respond(0, state({approvals: [row]}));
  assert.ok(approvalButton(page, "Approve and run").disabled);
  assert.ok(!approvalButton(page, "Reject").disabled);
  assert.ok(page.get("approvals").textContent.includes("<script>alert(1)</script>"));
  await page.click("acquire-control");
  await page.respond(1, {lease: {holder: client, remaining_s: 3}});
  await page.respond(2, owned({approvals: [row]}));
  assert.ok(approvalButton(page, "Approve and run").disabled);
  presence(page).checked = true;
  presence(page).emit("change");
  assert.ok(!approvalButton(page, "Approve and run").disabled);
  approvalButton(page, "Approve and run").emit("click");
  await drain();
  const sent = page.requests.at(-1);
  assert.equal(sent.url, "/lab/api/approvals/" + row.id + "/decision");
  assert.deepEqual(JSON.parse(sent.options.body),
    {decision: "approve", present: true, fingerprint: row.fingerprint});
  assert.ok(approvalButton(page, "Approve and run").disabled);
  sent.reject(new Error("Unknown result"));
  await drain();
  approvalButton(page, "Approve and run").emit("click");
  await drain();
  assert.equal(page.requests.filter(item => item.url.endsWith("/decision")).length, 1);
  assert.match(page.get("notice").textContent, /No automatic retry/);
}
{
  const page = browser(), row = proposal();
  await page.respond(0, state({approvals: [row]}));
  approvalButton(page, "Reject").emit("click");
  await drain();
  const sent = page.requests.at(-1);
  assert.equal(JSON.parse(sent.options.body).decision, "reject");
  await page.click("forget-token");
  sent.resolve({ok: true, status: 200, json: async () => ({decision: "rejected"})});
  await drain();
  assert.doesNotMatch(page.get("notice").textContent, /Proposal rejected/);
  assert.equal(posts(page).filter(item => item.url.endsWith("/heartbeat")).length, 0);
}
{
  const page = browser(), row = proposal();
  await page.respond(0, state({approvals: [row]}));
  await page.advance(4000);
  assert.ok(approvalButton(page, "Approve and run").disabled);
  // A stale poll cannot imply authority or trigger an approval.
  assert.equal(posts(page).length, 0);
}
"""
    )
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        text=True,
        cwd=root / "tests",
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("secret", [GATEWAY, OPERATOR])
def test_known_tokens_are_refused_in_reason_before_publication(rig, secret):
    service, client, _ = rig
    reply = proposal(client, reason="private: " + secret)
    assert reply.status_code == 400
    assert secret not in reply.text
    assert service.approvals.list() == []


@pytest.mark.parametrize("malformed", [[], {}, None, 1, True])
def test_malformed_decision_is_rejected_before_state_change(rig, malformed):
    service, client, _ = rig
    record = proposal(client).json
    response = decision(client, record, action=malformed)
    assert response.status_code == 400
    assert service.approvals.get(record["id"])["decision"] == "pending"
    assert service.job is None


@pytest.mark.parametrize("loss", ["release", "expiry", "stop", "dashboard-reserve"])
def test_slow_decision_audit_cannot_outlive_its_original_control(rig, monkeypatch, loss):
    service, client, clock = rig
    record = proposal(client).json
    client.post("/api/heartbeat", headers=headers(), json={})
    entered, release = threading.Event(), threading.Event()
    original = service.store.audit_call
    created = []

    def no_clients(required, identity):
        created.append(identity)
        return {}

    service.client_factory = no_clients

    def delayed(caller, event, data):
        if event == "approval.approved":
            entered.set()
            assert release.wait(3)
        return original(caller, event, data)

    monkeypatch.setattr(service.store, "audit_call", delayed)
    replies = []

    def decide_in_thread():
        with client.application.test_client() as separate:
            replies.append(decision(separate, record))

    worker = threading.Thread(target=decide_in_thread)
    worker.start()
    try:
        assert entered.wait(1)
        if loss == "release":
            assert client.post("/api/release", headers=headers(), json={}).status_code == 200
            assert client.post("/api/heartbeat", headers=headers(), json={}).status_code == 200
        elif loss == "expiry":
            clock[0] += 4
            assert client.post("/api/heartbeat", headers=headers(), json={}).status_code == 200
        elif loss == "stop":
            assert client.post("/api/stop", headers=headers(), json={}).status_code == 200
        else:
            operation, fresh = service.dashboard.reserve("motion")
            assert fresh
            # Exercise initial acceptance with no worker publication. Even a
            # subsequently cleared gate must not restore the earlier ticket.
            operation["done"].set()
            assert not service.dashboard.busy()
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert len(replies) == 1 and replies[0].status_code == 409
    assert service.approvals.get(record["id"])["dispatch"] == "not-confirmed"
    assert service.job is None and service.current is None and created == []
    assert decision(client, record).status_code == 409
