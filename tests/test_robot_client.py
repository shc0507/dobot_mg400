"""Exercise the Python client through a real local PanelServer and simulator rig."""

import concurrent.futures
import json
import socket
import threading
import time

import pytest
from panel_ws import PanelServer, RealSocket, running
from sim_mg400 import wait_for

import mg400.client as client_module
from mg400.client import RobotClient, RobotClientError
from mg400.web_control import RobotPanelState
from mg400.websocket import TEXT, MessageReader, encode_frame

TOKEN = "robot-client-offline-test-token"
TARGET = {"x": 260, "y": 12, "z": 42, "r": 31}
pytestmark = pytest.mark.usefixtures("isolated_controller_locks")


class LocalPanelServer(PanelServer):
    def get_request(self):
        descriptor, address = self.socket._accept()
        return RealSocket(self.address_family, self.socket_type, fileno=descriptor), address


@pytest.fixture
def service():
    with running(None, token=TOKEN, base_path="/robot/", server_class=LocalPanelServer) as server:
        yield server


@pytest.fixture
def rig(service, request, monkeypatch):
    rig = request.getfixturevalue("make_sim_rig")()
    service.state = rig.state
    monkeypatch.setattr(RobotPanelState, "LEASE_SECONDS", 3.0)

    def local_socket(address, timeout):
        assert address == service.server_address
        sock = RealSocket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(address)
        return sock

    monkeypatch.setattr(client_module, "_connect_socket", local_socket)
    return rig


@pytest.fixture
def make_client(service, rig):
    clients = []

    def make(identity="automation", **kwargs):
        client = RobotClient(
            f"http://127.0.0.1:{service.server_address[1]}/robot", TOKEN, identity, **kwargs
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        client.close()


def started(make_client):
    client = make_client().connect()
    client.run("start_sync")
    return client


def drop_socket(service, identity="automation"):
    with service._sockets_lock:
        service._sessions[identity]._connection.abort()


def test_construct_import_and_validation_open_nothing(monkeypatch):
    monkeypatch.setattr(client_module, "_connect_socket", lambda *a: pytest.fail("Network opened"))
    before = set(threading.enumerate())
    client = RobotClient("http://localhost/panel", "", "script")
    assert client.state is None and not client.connected
    assert set(threading.enumerate()) == before
    client.close()


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/a",
        "http://u:p@localhost",
        "http://localhost/?x=1",
        "http://localhost/#a",
        "http://localhost/../a",
        "http://localhost/\r\nx",
    ],
)
def test_invalid_urls(url):
    with pytest.raises(ValueError):
        RobotClient(url, TOKEN, "script")


@pytest.mark.parametrize("client_id", ["", " ", "a\nb", "名字", "x" * 129, None])
def test_invalid_identity(client_id):
    with pytest.raises(ValueError):
        RobotClient("http://localhost", TOKEN, client_id)


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), 10**1000])
def test_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        RobotClient("http://localhost", TOKEN, "script", timeout=timeout)


@pytest.mark.parametrize("setting,code", [("token", 4401), ("protocol", 4426)])
def test_refused_hello_preserves_close_code(make_client, setting, code):
    client = make_client()
    if setting == "token":
        client.token = "wrong-token"
    else:
        client.PROTOCOL = 999
    with pytest.raises(RobotClientError) as error:
        client.connect()
    assert error.value.close_code == code
    assert client._reader_thread is None and not client.connected


def test_refuse_wrong_contract(make_client, rig, monkeypatch):
    monkeypatch.setattr(rig.state, "describe", lambda: {"contract": 2})
    client = make_client()
    with pytest.raises(RobotClientError, match="contract"):
        client.connect()
    assert not client.connected


def test_masked_frames_command_reply_latest_state_and_run(make_client, rig, monkeypatch):
    frames = []
    original = client_module.encode_frame

    def capture(opcode, payload, **kwargs):
        frame = original(opcode, payload, **kwargs)
        frames.append(frame)
        return frame

    monkeypatch.setattr(client_module, "encode_frame", capture)
    client = started(make_client)
    reply = client.command("not_a_command")
    assert reply["status"] == 400 and "Unknown action" in reply["error"]
    completed = client.run("execute", targets=TARGET)
    assert completed["last_command"]["result"] == "ok"
    assert completed["last_command"]["action"] == "execute"
    assert completed["last_command"]["client"] == "automation"
    assert not completed["busy"]
    client.wait(lambda s: s["actual"][:4] == list(TARGET.values()), 3)
    snapshot = client.state
    snapshot["actual"][0] = "changed"
    assert client.state["actual"][0] != "changed"
    assert frames and all(frame[1] & 0x80 for frame in frames)
    assert sum(command.startswith("MovL(") for command in rig.controller.sent()) == 1


def test_run_waits_for_real_completion_and_ignores_other_client_rejection(make_client, rig):
    client = started(make_client)
    other = make_client("observer").connect()
    gate = rig.controller.block("MovL(")
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(client.run, "execute", targets=TARGET)
        assert gate.entered.wait(2)
        assert not future.done()
        rejected = other.command("execute", targets=TARGET)
        assert rejected["status"] == 403
        client.wait(lambda s: (s.get("last_command") or {}).get("client") == "observer", 2)
        assert not future.done()
        gate.open()
        result = future.result(4)
    assert result["last_command"]["client"] == client.client_id
    assert result["last_command"]["action"] == "execute"


def test_run_rejects_command_refusal(make_client):
    client = make_client().connect()
    with pytest.raises(RobotClientError) as error:
        client.run("execute", targets=TARGET)
    assert error.value.status == 403


def test_run_timeout_does_not_replay_motion(make_client, rig):
    client = started(make_client)
    gate = rig.controller.block("MovL(")
    try:
        with pytest.raises(TimeoutError):
            client.run("execute", timeout=0.15, targets=TARGET)
        assert gate.entered.is_set()
        assert sum(command.startswith("MovL(") for command in rig.controller.sent()) == 1
    finally:
        gate.open()
    client.wait(lambda s: not s["busy"], 3)


def test_heartbeat_preserves_control_for_five_seconds(make_client, rig):
    client = started(make_client)
    time.sleep(5.1)
    assert client.connected
    assert rig.snapshot()["controller_id"] == "automation"
    assert not rig.snapshot()["locked"]
    assert "ResetRobot()" not in rig.controller.sent()


def test_close_stops_workers_and_lease_expiry_stops_arm(make_client, rig):
    client = started(make_client)
    threads = (client._reader_thread, client._heartbeat_thread)
    client.close()
    assert all(not worker.is_alive() for worker in threads)
    rig.wait(lambda s: s["controller_id"] is None and s["locked"] and not s["busy"], timeout=5)
    assert "ResetRobot()" in rig.controller.sent()
    assert not rig.snapshot()["stop_unconfirmed"]


def test_transport_reconnect_retains_control_and_never_repeats_command(make_client, service, rig):
    client = started(make_client)
    gate = rig.controller.block("MovL(")
    client.command("execute", targets=TARGET)
    assert gate.entered.wait(2)
    old = client._socket
    began = time.monotonic()
    drop_socket(service)
    wait_for(lambda: client.connected and client._socket is not old)
    assert time.monotonic() - began < 3
    assert rig.snapshot()["controller_id"] == "automation"
    gate.open()
    client.wait(lambda s: not s["busy"], 3)
    assert sum(command.startswith("MovL(") for command in rig.controller.sent()) == 1


def test_lost_reply_is_unknown_and_not_replayed(make_client, service, rig, monkeypatch):
    client = started(make_client)
    original = service.dispatch_live

    def drop_reply(session, kind, body):
        result = original(session, kind, body)
        if kind == "command" and body["action"] == "execute":
            session._connection.abort()
        return result

    monkeypatch.setattr(service, "dispatch_live", drop_reply)
    with pytest.raises(RobotClientError, match="unknown"):
        client.run("execute", targets=TARGET)
    client.wait(
        lambda s: not s["busy"] and (s.get("last_command") or {}).get("action") == "execute", 3
    )
    assert sum(command.startswith("MovL(") for command in rig.controller.sent()) == 1


@pytest.mark.parametrize("action", ["stop", "disable"])
def test_http_safety_fallback_when_socket_down(make_client, rig, action):
    client = started(make_client)
    client.close()
    # The HTTP path keeps the explicit token and ID and the legacy panel header.
    reply = client.command(action)
    assert reply["status"] == 200
    rig.wait(lambda s: not s["busy"])
    assert ("DisableRobot()" if action == "disable" else "ResetRobot()") in rig.controller.sent()
    with pytest.raises(RobotClientError, match="unavailable"):
        client.command("execute", targets=TARGET)


def test_context_stops_sync_and_keep_control_only_skips_cleanup(make_client, rig):
    with make_client() as client:
        client.run("start_sync")
    assert not client.connected
    assert not rig.snapshot()["syncing"]
    assert "ResetRobot()" in rig.controller.sent()


def test_context_keep_control_allows_lease_to_expire(make_client, rig):
    with make_client(keep_control=True) as client:
        client.run("start_sync")
    assert rig.snapshot()["syncing"]
    assert rig.snapshot()["controller_id"] == "automation"
    assert "ResetRobot()" not in rig.controller.sent()
    rig.wait(lambda s: s["locked"] and s["controller_id"] is None, timeout=5)


def test_wait_timeout_and_close_wakes_waiters(make_client):
    client = make_client().connect()
    with pytest.raises(TimeoutError):
        client.wait(lambda _: False, 0.01)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(client.wait, lambda _: False, 10)
        client.close()
        with pytest.raises(RobotClientError, match="closed"):
            future.result(2)


def test_replaced_identity_does_not_reconnect_and_replace_new_owner(make_client):
    older = make_client().connect()
    newer = make_client().connect()
    wait_for(lambda: not older.connected)
    time.sleep(0.6)
    assert newer.connected and not older.connected
    assert older._error.close_code == 4409


def test_server_frames_are_unmasked_and_client_rejects_masked_server_frames():
    data = encode_frame(TEXT, json.dumps({"type": "state", "state": {}}).encode(), mask=True)
    chunks = iter(data)
    reader = MessageReader(lambda n: bytes(next(chunks) for _ in range(n)), masked=False)
    with pytest.raises(client_module.ProtocolError, match="must not be masked"):
        reader.next()


def test_failed_reconnect_has_one_bounded_window_then_lease_expires(
    make_client, service, rig, monkeypatch
):
    client = started(make_client)
    attempts = []

    def unavailable(deadline):
        attempts.append(time.monotonic())
        raise ConnectionRefusedError("Offline test disconnected the listener")

    monkeypatch.setattr(client, "_open", unavailable)
    began = time.monotonic()
    drop_socket(service)
    client._reader_thread.join(3)
    assert not client._reader_thread.is_alive()
    assert attempts and attempts[-1] - began < 3
    count = len(attempts)
    rig.wait(lambda s: s["controller_id"] is None and s["locked"], timeout=3)
    assert len(attempts) == count
    assert "ResetRobot()" in rig.controller.sent()


def test_other_client_stop_never_counts_as_motion_success(make_client, rig):
    client = started(make_client)
    other = make_client("observer").connect()
    gate = rig.controller.block("MovL(")
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(client.run, "execute", targets=TARGET)
        assert gate.entered.wait(2)
        assert other.command("stop")["status"] == 200
        gate.open()
        with pytest.raises(RobotClientError):
            future.result(3)
    assert rig.snapshot()["locked"]
    assert sum(command.startswith("MovL(") for command in rig.controller.sent()) == 1


def test_stop_can_interrupt_run_on_same_client(make_client, rig):
    client = started(make_client)
    gate = rig.controller.block("MovL(")
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(client.run, "execute", targets=TARGET)
        assert gate.entered.wait(2)
        reply = client.command("stop")
        assert reply["status"] == 200
        gate.open()
        with pytest.raises(RobotClientError):
            future.result(3)


def test_http_fallback_rejects_wrong_token_without_actuation(make_client, rig):
    client = started(make_client)
    client.close()
    client.token = "wrong-token"
    with pytest.raises(RobotClientError) as error:
        client.command("stop")
    assert error.value.status == 401
    assert "ResetRobot()" not in rig.controller.sent()


def test_unconfirmed_stop_cannot_be_reported_as_success(make_client, rig):
    client = started(make_client)

    def lose_reset_reply(connection, text):
        if text == "ResetRobot()":
            raise OSError("Simulated lost stop reply")

    rig.controller.reply_hooks.append(lose_reset_reply)
    with pytest.raises(RobotClientError):
        client.run("stop")
    assert rig.snapshot()["stop_unconfirmed"]
    assert rig.snapshot()["locked"]
    assert sum(command == "ResetRobot()" for command in rig.controller.sent()) == 1
    client.close()
    rig.controller.reply_hooks.clear()
    reply = client.command("stop")
    assert reply["state"]["stop_unconfirmed"]
    rig.wait(lambda s: not s["busy"])
    assert rig.snapshot()["stop_unconfirmed"]


def test_reply_timeout_never_resends_even_after_late_reply(make_client, service, rig, monkeypatch):
    client = started(make_client)
    release = threading.Event()
    entered = threading.Event()
    original = service.dispatch_live

    def delay_reply(session, kind, body):
        result = original(session, kind, body)
        if kind == "command" and body["action"] == "execute":
            entered.set()
            release.wait(2)
        return result

    monkeypatch.setattr(service, "dispatch_live", delay_reply)
    client.timeout = 0.1
    try:
        with pytest.raises(TimeoutError, match="unknown"):
            client.command("execute", targets=TARGET)
        assert entered.is_set()
    finally:
        release.set()
    rig.wait(lambda s: not s["busy"] and s["last_command"]["action"] == "execute")
    assert sum(command.startswith("MovL(") for command in rig.controller.sent()) == 1


def test_close_during_reconnect_ends_workers(make_client, service, monkeypatch):
    client = started(make_client)
    entered = threading.Event()

    def unavailable(deadline):
        entered.set()
        raise ConnectionRefusedError("Offline test")

    monkeypatch.setattr(client, "_open", unavailable)
    drop_socket(service)
    assert entered.wait(2)
    workers = client._reader_thread, client._heartbeat_thread
    client.close()
    assert all(not worker.is_alive() for worker in workers)


def test_context_preserves_body_exception_when_cleanup_is_unavailable(make_client):
    with pytest.raises(LookupError, match="body failed"):
        with make_client() as client:
            client.run("start_sync")
            client.close()
            raise LookupError("body failed")


@pytest.mark.parametrize("worker_name", ["RobotClient-receive", "RobotClient-heartbeat"])
def test_thread_start_failure_closes_connection_and_joins_started_workers(
    make_client, monkeypatch, worker_name
):
    original = threading.Thread.start
    failed = []

    def start(worker):
        if worker.name == worker_name and not failed:
            failed.append(worker)
            raise RuntimeError("Simulated thread startup failure")
        return original(worker)

    monkeypatch.setattr(threading.Thread, "start", start)
    client = make_client()
    with pytest.raises(RuntimeError, match="startup failure"):
        client.connect()
    assert not client.connected
    assert client._reader_thread is None and client._heartbeat_thread is None
    client.close()
    assert client.connect() is client


def test_close_retains_live_worker_handle_until_it_finishes():
    class SlowWorker:
        alive = True

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            assert timeout > 0

    client = RobotClient("http://localhost/", "", "script")
    worker = SlowWorker()
    client._reader_thread = worker
    client.close()
    assert client._reader_thread is worker
    with pytest.raises(RobotClientError, match="Close"):
        client.connect()
    worker.alive = False
    client.close()
    assert client._reader_thread is None


def test_run_stop_interrupts_same_client_run_without_waiting_for_command_lock(make_client, rig):
    client = started(make_client)
    gate = rig.controller.block("MovL(")
    with concurrent.futures.ThreadPoolExecutor() as pool:
        movement = pool.submit(client.run, "execute", timeout=10, targets=TARGET)
        assert gate.entered.wait(2)
        stopping = pool.submit(client.run, "stop", timeout=3)
        try:
            wait_for(lambda: "ResetRobot()" in rig.controller.sent(), timeout=1)
        finally:
            gate.open()
        # Cancellation during an unacknowledged move can correctly leave the service locked.
        with pytest.raises(RobotClientError):
            movement.result(3)
        try:
            result = stopping.result(3)
        except RobotClientError:
            assert rig.snapshot()["stop_unconfirmed"]
        else:
            assert result["last_command"]["action"] == "stop"
            assert not result["stop_unconfirmed"]


def test_prior_same_action_completion_between_baseline_and_acceptance_is_not_reused(
    make_client, rig, monkeypatch
):
    client = started(make_client)
    original = client._command
    gate = None
    previous_id = None
    entered = threading.Event()

    def interleave(action, fields, timeout):
        nonlocal gate, previous_id
        # Finish a previously accepted same-action request just before this send.
        rig.state.command({"action": "execute", "targets": TARGET}, "automation")
        rig.wait(lambda s: not s["busy"] and s["last_command"]["action"] == "execute")
        previous_id = rig.snapshot()["last_command"]["id"]
        gate = rig.controller.block("MovL(")
        result = original(action, fields, timeout)
        entered.set()
        return result

    monkeypatch.setattr(client, "_command", interleave)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        future = pool.submit(client.run, "execute", targets={**TARGET, "x": 265})
        assert entered.wait(2) and gate.entered.wait(2)
        assert not future.done()
        gate.open()
        result = future.result(3)
    assert result["last_command"]["id"] != previous_id
    assert result["last_command"]["result"] == "ok"
    assert sum(command.startswith("MovL(") for command in rig.controller.sent()) == 2


def test_silent_time_consumes_reconnect_budget(make_client, service, monkeypatch):
    client = started(make_client)
    attempts = []

    def unavailable(deadline):
        attempts.append(deadline)
        raise ConnectionRefusedError("Simulated unavailable transport")

    monkeypatch.setattr(client, "_open", unavailable)
    # A silent link already consumed most of the lease window before EOF was detected.
    with client._changed:
        client._last_message_at = time.monotonic() - 2
        client._lost(client._socket, EOFError("Offline test dropped the silent link"))
    started_at = time.monotonic()
    client._reader_thread.join(1)
    assert not client._reader_thread.is_alive()
    assert attempts and max(attempts) - started_at < 0.6


@pytest.mark.parametrize("previously_connected", [False, True])
def test_http_fallback_checks_contract_before_sending_any_command(
    make_client, rig, monkeypatch, previously_connected
):
    client = started(make_client) if previously_connected else make_client()
    client.close()
    monkeypatch.setattr(rig.state, "describe", lambda: {"contract": 2})
    before = rig.controller.sent()
    with pytest.raises(RobotClientError, match="contract") as error:
        client.command("stop")
    assert error.value.status == 409
    assert rig.controller.sent() == before
