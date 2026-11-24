"""Approval and handover tests never contact equipment."""

import json
import re
import secrets
import sqlite3
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from labkit.errors import ServiceError
from orchestrator.agent_connect.authority import (
    HEARTBEAT_INTERVAL_S,
    INVITATION_TIMEOUT_S,
    MAX_INVITATIONS,
    SESSION_TIMEOUT_S,
    Authority,
    Principal,
)


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def wall(self):
        return self.now + 1000


@pytest.fixture
def rig():
    clock = Clock()
    authority = Authority(clock=clock, wall_clock=clock.wall)
    yield authority, clock
    authority.close()


def agent_connect(authority, name="PC agent"):
    invitation = authority.create_invitation(authority.human)["invitation"]
    paired = authority.pair(invitation, name)
    reply = authority.connect(paired["credential"])
    return paired, reply, authority.authenticate(reply["session_token"])


def test_operator_timeout_warns_without_restoring_control_or_shutting_down(rig):
    authority, clock = rig
    _, _, agent = agent_connect(authority)
    authority.grant(authority.human, agent.id)
    clock.now += SESSION_TIMEOUT_S + 1
    report = authority.snapshot(authority.human)
    assert report["operator"] is None
    assert report["watchdog"]["policy"] == "warn_only"
    assert report["watchdog"]["shutdown_enabled"] is False
    warning = report["watchdog"]["active"]
    assert warning["sender"]["id"] == agent.id
    assert warning["reason"] == "connection_timeout"
    assert authority.snapshot()["watchdog"]["active"]["event_id"] == warning["event_id"]
    with pytest.raises(ServiceError):
        authority.acknowledge_warning(agent)
    authority.acknowledge_warning(authority.human)
    assert authority.snapshot()["watchdog"]["active"] is None


def test_observer_timeout_and_explicit_release_do_not_warn(rig):
    authority, clock = rig
    agent_connect(authority)
    clock.now += SESSION_TIMEOUT_S + 1
    assert authority.snapshot()["watchdog"]["active"] is None
    _, _, agent = agent_connect(authority)
    authority.grant(authority.human, agent.id)
    authority.release(agent, authority.control_epoch)
    clock.now += SESSION_TIMEOUT_S + 1
    assert authority.snapshot()["watchdog"]["active"] is None


def assert_error(status, action, *args):
    with pytest.raises(ServiceError) as error:
        action(*args)
    assert error.value.status == status


def test_human_can_operate_immediately_without_sign_in_or_grant(rig):
    authority, clock = rig
    human = authority.human
    state = authority.snapshot(human)
    assert state["operator"] is None
    assert state["management_available"] is True
    assert state["principal"]["kind"] == "human"
    assert state["session"] is None
    assert state["sessions"] == []
    assert "activated" not in state and "owner_authenticated" not in state
    assert state["commands_enabled"] is False
    assert state["furnace_control_enabled"] is False
    with authority.human_guard():
        pass
    epoch = authority.take_human(human)["control_epoch"]
    clock.now = 15 * 24 * 3600
    assert not authority.poll()
    assert authority.human is human
    with authority.human_guard(epoch):
        pass


def test_human_writes_wait_for_takeover_while_an_agent_holds_control(rig):
    authority, _clock = rig
    _paired, _reply, agent = agent_connect(authority)
    old_epoch = authority.control_epoch
    granted = authority.grant(authority.human, agent.id)
    with pytest.raises(ServiceError) as error:
        with authority.human_guard():
            pytest.fail("Human write ran while an agent held control.")
    assert error.value.status == 403
    with pytest.raises(ServiceError) as error:
        with authority.human_guard(old_epoch):
            pytest.fail("Old page epoch was accepted.")
    assert error.value.status == 409
    current = authority.take_human(authority.human)
    assert current["control_epoch"] != granted["control_epoch"]
    with authority.human_guard(current["control_epoch"]):
        pass


def test_human_simulation_requires_current_grant_and_epoch(rig):
    authority, _clock = rig
    human = authority.human
    assert_error(409, authority.check, human, None)
    assert_error(403, authority.check, human, authority.control_epoch)
    epoch = authority.take_human(human)["control_epoch"]
    authority.check(human, epoch)
    assert_error(409, authority.check, human, None)
    authority.release(human, epoch)
    assert_error(409, authority.check, human, epoch)
    assert_error(403, authority.check, human, authority.control_epoch)
    with pytest.raises(ServiceError):
        with authority.guard(human, authority.control_epoch):
            pytest.fail("Released human simulation continued.")
    with authority.human_guard():
        pass


@pytest.mark.parametrize("form", ["display", "lower", "spaces", "compact"])
def test_invitation_is_readable_normalized_and_single_use(rig, form):
    authority, clock = rig
    invitation = authority.create_invitation(authority.human)
    code = invitation["invitation"]
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}", code)
    assert invitation["expires_in_s"] == 300
    supplied = {
        "display": code,
        "lower": code.lower(),
        "spaces": " \t" + code.replace("-", " - ") + " ",
        "compact": code.replace("-", ""),
    }[form]
    paired = authority.pair(supplied, " Test PC ")
    assert len(paired["credential"]) == 43
    assert_error(401, authority.pair, code, "Another PC")
    assert paired["status"] == "approved"
    client = authority.snapshot(authority.human)["clients"][0]
    assert client["name"] == "Test PC"
    assert client["status"] == "approved"
    assert authority._clients[paired["client_id"]]["approved_at"] == clock.wall()
    reply = authority.connect(paired["credential"])
    agent = authority.authenticate(reply["session_token"])
    assert reply["state"]["operator"] is None
    assert_error(403, authority.check, agent, authority.control_epoch)
    epoch = authority.grant(authority.human, agent.id)["control_epoch"]
    authority.check(agent, epoch)


def test_concurrent_redemption_grants_access_to_only_one_client(rig):
    authority, _clock = rig
    code = authority.create_invitation(authority.human)["invitation"]
    ready = threading.Barrier(2)

    def redeem(name):
        ready.wait(timeout=5)
        try:
            return authority.pair(code, name)["status"]
        except ServiceError as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(redeem, ["PC A", "PC B"]))
    assert results.count("approved") == results.count(401) == 1
    state = authority.snapshot(authority.human)
    assert len(state["clients"]) == 1
    assert state["operator"] is None and state["sessions"] == []


def test_legacy_pending_client_needs_a_new_invitation(tmp_path):
    path = tmp_path / "authority.db"
    first = Authority(path)
    paired = first.pair(first.create_invitation(first.human)["invitation"], "Old pending PC")
    first.close()
    with sqlite3.connect(path) as database:
        database.execute(
            "UPDATE agent_connect_clients SET status='pending', approved_at=NULL WHERE id=?",
            (paired["client_id"],),
        )
    restored = Authority(path)
    try:
        assert restored.credential_status(paired["credential"])["status"] == "pending"
        assert_error(403, restored.connect, paired["credential"])
        current = restored.pair(
            restored.create_invitation(restored.human)["invitation"], "New invited PC"
        )
        assert current["status"] == "approved"
        assert restored.connect(current["credential"])["state"]["operator"] is None
        assert_error(403, restored.connect, paired["credential"])
    finally:
        restored.close()


def test_invitation_collision_cannot_reactivate_a_consumed_code(rig, monkeypatch):
    authority, clock = rig
    monkeypatch.setattr(secrets, "choice", lambda _alphabet: "A")
    code = authority.create_invitation(authority.human)["invitation"]
    assert code == "AAAA-AAAA"
    assert_error(503, authority.create_invitation, authority.human)
    authority.pair(code, "PC")
    assert_error(503, authority.create_invitation, authority.human)
    assert_error(401, authority.pair, code, "Other PC")
    clock.now = INVITATION_TIMEOUT_S
    assert authority.create_invitation(authority.human)["invitation"] == code


def test_credential_status_reads_own_approval_without_connecting_or_renewing(rig):
    authority, clock = rig
    paired = authority.pair(authority.create_invitation(authority.human)["invitation"], "PC")
    expected = {
        "id": paired["client_id"],
        "name": "PC",
        "status": "approved",
        "matching_code": paired["matching_code"],
    }
    assert authority.credential_status(paired["credential"]) == expected
    assert authority.snapshot(authority.human)["sessions"] == []
    reply = authority.connect(paired["credential"])
    agent = authority.authenticate(reply["session_token"])
    epoch = authority.grant(authority.human, agent.id)["control_epoch"]
    clock.now = SESSION_TIMEOUT_S - 0.01
    assert authority.credential_status(paired["credential"])["status"] == "approved"
    assert authority.control_epoch == epoch
    clock.now = SESSION_TIMEOUT_S
    authority.credential_status(paired["credential"])
    assert_error(401, authority.authenticate, reply["session_token"])
    authority.revoke(authority.human, paired["client_id"])
    assert authority.credential_status(paired["credential"]) == dict(expected, status="revoked")
    assert_error(401, authority.credential_status, "invalid")


@pytest.mark.parametrize("target", ["parent", "database", "sidecar"])
def test_authority_refuses_symlinks_before_writing(tmp_path, monkeypatch, target):
    folder = tmp_path / "private"
    folder.mkdir()
    path = folder / "authority.db"
    linked = {"parent": folder, "database": path, "sidecar": Path(str(path) + "-journal")}[target]
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda item: item == linked or original(item))
    with pytest.raises(ValueError, match="symbolic links"):
        Authority(path)


def test_expired_invitation_and_invalid_inputs_do_not_create_a_client(rig):
    authority, clock = rig
    code = authority.create_invitation(authority.human)["invitation"]
    for name in (None, "", "   ", "A\nB", "x" * 65):
        with pytest.raises(ValueError):
            authority.pair(code, name)
    for invalid in (None, [], "", "x" * 33, "0000-0000", "ＡＡＡＡ-ＡＡＡＡ"):
        assert_error(401, authority.pair, invalid, "PC")
    assert authority.snapshot(authority.human)["clients"] == []
    clock.now = INVITATION_TIMEOUT_S
    assert_error(401, authority.pair, code, "PC")


def test_management_requires_the_exact_trusted_human_identity(rig):
    authority, _clock = rig
    paired, _reply, agent = agent_connect(authority)
    for forged in (
        agent,
        replace(authority.human),
        Principal("human", [], "human", "Human"),
        {},
        None,
    ):
        for method, args in (
            (authority.create_invitation, (forged,)),
            (authority.revoke, (forged, paired["client_id"])),
            (authority.grant, (forged, agent.id)),
            (authority.take_human, (forged,)),
        ):
            assert_error(403, method, *args)
    assert_error(404, authority.revoke, authority.human, "missing")
    assert_error(404, authority.revoke, authority.human, [])
    assert_error(409, authority.grant, authority.human, authority.human.id)


def test_forged_principals_and_other_token_classes_cannot_authenticate(rig):
    authority, _clock = rig
    paired, reply, agent = agent_connect(authority)
    for token in (None, [], 1, "human", authority.human.id, paired["credential"]):
        assert_error(401, authority.authenticate, token)
    assert authority.authenticate(reply["session_token"]) is agent
    assert_error(401, authority.check, replace(agent), authority.control_epoch)


def test_one_operator_and_old_steps_fail_after_handover(rig):
    authority, _clock = rig
    _paired_a, _reply_a, a = agent_connect(authority, "A")
    _paired_b, _reply_b, b = agent_connect(authority, "B")
    before = authority.grant(authority.human, a.id)["control_epoch"]
    authority.check(a, before)
    assert_error(403, authority.check, b, before)
    after = authority.grant(authority.human, b.id)["control_epoch"]
    assert_error(409, authority.check, a, before)
    assert_error(403, authority.check, a, after)
    authority.check(b, after)
    taken = authority.take_human(authority.human)
    assert_error(409, authority.release, b, after)
    assert_error(403, authority.release, b, taken["control_epoch"])
    authority.release(authority.human, taken["control_epoch"])
    assert authority.snapshot()["operator"] is None


def test_reads_do_not_renew_connection_and_timeout_cancels_work(rig):
    authority, clock = rig
    _paired, reply, agent = agent_connect(authority)
    epoch = authority.grant(authority.human, agent.id)["control_epoch"]
    clock.now = SESSION_TIMEOUT_S - 0.01
    assert authority.authenticate(reply["session_token"]) is agent
    assert authority.snapshot(agent)["operator"]["id"] == agent.id
    clock.now = SESSION_TIMEOUT_S
    assert authority.poll()
    assert authority.snapshot(authority.human)["operator"] is None
    assert authority.control_epoch != epoch
    assert_error(401, authority.check, agent, epoch)
    assert_error(401, authority.authenticate, reply["session_token"])
    assert authority.snapshot()["last_change"]["reason"] == "connection_timeout"


def test_heartbeat_renews_but_expired_session_cannot_recover(rig):
    authority, clock = rig
    _paired, _reply, agent = agent_connect(authority)
    epoch = authority.grant(authority.human, agent.id)["control_epoch"]
    for _ in range(8):
        clock.now += HEARTBEAT_INTERVAL_S
        state = authority.heartbeat(agent)
        assert state["control_epoch"] == epoch
        assert state["sessions"][0]["last_seen_age_s"] == 0
    clock.now += SESSION_TIMEOUT_S
    assert_error(401, authority.heartbeat, agent)
    assert authority.snapshot()["operator"] is None


def test_reconnect_disconnect_and_revocation_invalidate_work(rig):
    authority, _clock = rig
    paired, reply, agent = agent_connect(authority)
    epoch = authority.grant(authority.human, agent.id)["control_epoch"]
    reconnected = authority.connect(paired["credential"])
    fresh = authority.authenticate(reconnected["session_token"])
    assert fresh.id != agent.id
    assert reconnected["state"]["operator"] is None
    assert_error(401, authority.authenticate, reply["session_token"])
    assert_error(401, authority.check, agent, epoch)
    assert_error(403, authority.check, fresh, authority.control_epoch)
    epoch = authority.grant(authority.human, fresh.id)["control_epoch"]
    authority.disconnect(fresh)
    assert_error(401, authority.check, fresh, epoch)
    reply = authority.connect(paired["credential"])
    agent = authority.authenticate(reply["session_token"])
    epoch = authority.grant(authority.human, agent.id)["control_epoch"]
    authority.revoke(authority.human, paired["client_id"])
    assert authority.snapshot()["operator"] is None
    assert_error(401, authority.check, agent, epoch)
    assert_error(401, authority.authenticate, reply["session_token"])
    assert_error(401, authority.connect, paired["credential"])
    assert authority.credential_status(paired["credential"])["status"] == "revoked"
    sender = authority.stop_sender(reply["session_token"])
    assert sender["client_id"] == paired["client_id"]
    assert sender["role"] == "expired"
    assert sender["verified"] is True


@pytest.mark.parametrize("legacy_activation", [None, "0", "1", "ignored legacy value"])
def test_restart_preserves_clients_but_never_sessions_or_grants(tmp_path, legacy_activation):
    path = tmp_path / "authority.db"
    first = Authority(path)
    old_human = first.human
    paired, reply, agent = agent_connect(first)
    first.grant(first.human, agent.id)
    initial_boot, initial_epoch = first.boot_id, first.control_epoch
    first.close()
    if legacy_activation is not None:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO agent_connect_authority_meta VALUES ('activated', ?)",
                (legacy_activation,),
            )
    second = Authority(path)
    try:
        assert second.boot_id != initial_boot
        assert second.control_epoch != initial_epoch
        assert second.snapshot()["operator"] is None
        assert second.snapshot(second.human)["sessions"] == []
        assert_error(401, second.authenticate, reply["session_token"])
        assert_error(403, second.take_human, old_human)
        connected = second.connect(paired["credential"])
        fresh = second.authenticate(connected["session_token"])
        assert_error(403, second.check, fresh, second.control_epoch)
        second.grant(second.human, fresh.id)
        assert_error(409, second.check, fresh, initial_epoch)
        second.revoke(second.human, paired["client_id"])
    finally:
        second.close()
    third = Authority(path)
    try:
        assert_error(401, third.connect, paired["credential"])
    finally:
        third.close()


def test_secrets_and_other_agent_records_are_not_exposed(tmp_path):
    path = tmp_path / "authority.db"
    authority = Authority(path)
    try:
        paired, reply, agent = agent_connect(authority)
        agent_connect(authority, "Other PC")
        status = json.dumps(authority.snapshot(authority.human))
        for secret in (paired["credential"], reply["session_token"]):
            assert secret not in status
            assert secret.encode() not in path.read_bytes()
        assert "credential_hash" not in status
        assert authority.snapshot()["clients"] == []
        assert authority.snapshot()["sessions"] == []
        own = authority.snapshot(agent)
        assert own["management_available"] is False
        assert [row["id"] for row in own["clients"]] == [paired["client_id"]]
        assert [row["id"] for row in own["sessions"]] == [agent.id]
    finally:
        authority.close()


def test_simulated_restart_rotates_human_and_clears_ephemeral_authority(rig):
    authority, _clock = rig
    human = authority.human
    paired, reply, agent = agent_connect(authority)
    previous = authority.grant(human, agent.id)
    invitation = authority.create_invitation(human)["invitation"]
    restarted = authority.restart()
    assert restarted["boot_id"] != previous["boot_id"]
    assert restarted["control_epoch"] != previous["control_epoch"]
    assert restarted["operator"] is None
    assert restarted["management_available"] is False
    assert authority.human is not human
    assert_error(403, authority.take_human, human)
    assert_error(401, authority.authenticate, reply["session_token"])
    assert_error(401, authority.pair, invitation, "New agent")
    assert authority.connect(paired["credential"])["state"]["operator"] is None


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE agent_connect_authority_meta SET value='2' WHERE key='version'",
        "DELETE FROM agent_connect_authority_meta WHERE key='version'",
    ],
)
def test_unsupported_or_missing_schema_version_is_rejected(tmp_path, statement):
    path = tmp_path / "authority.db"
    Authority(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute(statement)
    assert_error(503, Authority, path)


@pytest.mark.parametrize("kind", ["generated", "unrelated", "new_database", "linked"])
def test_cleanup_removes_only_generated_legacy_owner_code(tmp_path, monkeypatch, kind):
    path = tmp_path / "authority.db"
    authority = Authority(path)
    paired, _reply, _agent = agent_connect(authority)
    authority.close()
    if kind != "new_database":
        with sqlite3.connect(path) as connection:
            connection.execute("INSERT INTO agent_connect_authority_meta VALUES ('activated', '1')")
    code_path = tmp_path / "owner-code"
    contents = (
        "unrelated user content\n" if kind == "unrelated" else secrets.token_urlsafe(32) + "\n"
    )
    code_path.write_text(contents, encoding="ascii")
    if kind == "linked":
        original = Path.lstat
        monkeypatch.setattr(
            Path,
            "lstat",
            lambda item: (
                SimpleNamespace(st_mode=stat.S_IFLNK) if item == code_path else original(item)
            ),
        )
    reopened = Authority(path)
    try:
        assert reopened.connect(paired["credential"])["state"]["operator"] is None
        if kind == "generated":
            assert not code_path.exists()
        else:
            assert code_path.read_text(encoding="ascii") == contents
    finally:
        reopened.close()


def test_invitation_storage_is_bounded(rig):
    authority, clock = rig
    for _ in range(MAX_INVITATIONS):
        authority.create_invitation(authority.human)
    assert_error(409, authority.create_invitation, authority.human)
    clock.now += INVITATION_TIMEOUT_S
    assert authority.create_invitation(authority.human)["invitation"]


def test_atomic_guard_finishes_current_step_before_handover(rig):
    authority, _clock = rig
    _paired, _reply, agent = agent_connect(authority)
    epoch = authority.grant(authority.human, agent.id)["control_epoch"]
    entered, done = threading.Event(), threading.Event()
    events = []

    def handover():
        entered.set()
        authority.take_human(authority.human)
        events.append("handover")
        done.set()

    with authority.guard(agent, epoch):
        thread = threading.Thread(target=handover)
        thread.start()
        assert entered.wait(1)
        assert not done.is_set()
        events.append("current_step")
    assert done.wait(1)
    thread.join(1)
    assert not thread.is_alive()
    assert events == ["current_step", "handover"]
    with pytest.raises(ServiceError):
        with authority.guard(agent, epoch):
            pytest.fail("A previous operator's step was dispatched.")
