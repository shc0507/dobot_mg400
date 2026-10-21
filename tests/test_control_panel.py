"""Check the independent browser panel without robot connections."""

import concurrent.futures
import http.client
import json
import runpy
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

PANEL = runpy.run_path(str(Path(__file__).resolve().parents[1] / "apps/control_panel/server.py"))
PanelState = PANEL["PanelState"]
PanelServer = PANEL["PanelServer"]


def targets(**changes):
    return {"x": 250, "y": 0, "z": 100, "r": 0, "grip": 500, **changes}


def test_demo_requires_enable_and_applies_all_five_targets_atomically():
    panel = PanelState()
    with pytest.raises(RuntimeError, match="Enable"):
        panel.command({"action": "execute", "targets": targets()})
    panel.command({"action": "enable"})
    result = panel.command({"action": "execute", "targets": targets(x=300, grip=250)})
    assert result["actual"] == [300, 0, 100, 0, 250]
    before = result["actual"]
    with pytest.raises(ValueError):
        panel.command({"action": "execute", "targets": targets(grip=1001)})
    assert panel.snapshot()["actual"] == before
    assert panel.command({"action": "home"})["actual"] == [250, 0, 100, 0, 250]
    panel.command({"action": "disable"})
    with pytest.raises(RuntimeError, match="Enable"):
        panel.command({"action": "home"})


@pytest.mark.parametrize("changes", [
    {"x": float("nan")}, {"r": float("inf")}, {"y": True}, {"z": "100"},
    {"x": 99.9}, {"grip": -1}, {"grip": 1.5}, {"r": 91}, {"x": 10**1000},
])
def test_invalid_targets_do_not_change_state(changes):
    panel = PanelState()
    panel.command({"action": "enable"})
    before = panel.snapshot()["actual"]
    with pytest.raises(ValueError):
        panel.command({"action": "execute", "targets": targets(**changes)})
    assert panel.snapshot()["actual"] == before


def test_read_only_mode_is_inert_until_sync_and_has_no_hardware_commands():
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(args)
        raise OSError("test offline")

    panel = PanelState("read-only", "test-host", connection_factory=forbidden)
    assert panel.snapshot()["actual"] == [None] * 5
    assert panel.snapshot()["limits"] is None
    assert panel.snapshot()["home"] is None
    assert calls == []
    for action in ("enable", "disable", "home", "execute"):
        command = {"action": action}
        if action == "execute":
            command["targets"] = targets()
        with pytest.raises(PermissionError):
            panel.command(command)
    panel.command({"action": "start_sync"})
    panel._reader.join(timeout=1)
    assert calls == [("test-host",)]
    assert not panel.snapshot()["syncing"]
    assert "test offline" in panel.snapshot()["message"]
    panel.close()


def test_concurrent_demo_commands_never_mix_target_rows():
    panel = PanelState()
    panel.command({"action": "enable"})

    def execute(i):
        return panel.command({"action": "execute", "targets": targets(x=200+i, grip=i)})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(execute, range(40)))
    assert all(result["actual"][0] - 200 == result["actual"][4] for result in results)


def test_passive_feedback_stops_without_accepting_a_late_sample():
    waiting = threading.Event()
    release = threading.Event()

    class Connection:
        count = 0

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read_latest(self):
            self.count += 1
            if self.count > 1:
                waiting.set()
                release.wait(timeout=2)
            return SimpleNamespace(joints_deg=(self.count, 2, 3, 4), mode=4, error_status=False)

    panel = PanelState("read-only", "test-host", connection_factory=Connection)
    try:
        panel.command({"action": "start_sync"})
        assert waiting.wait(timeout=1)
        assert panel.snapshot()["joints"] == [1, 2, 3, 4]
        panel.command({"action": "stop_sync"})
        assert not panel.snapshot()["feedback_fresh"]
        release.set()
        panel._reader.join(timeout=1)
        assert panel.snapshot()["joints"] == [1, 2, 3, 4]
        assert panel.snapshot()["actual"] == [None] * 5
    finally:
        release.set()
        panel.close()


@pytest.fixture
def server():
    instance = PanelServer(("127.0.0.1", 0), PanelState(), token="test-token")
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    instance.state.close()
    thread.join(timeout=2)


def request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    connection.request(method, path, body, headers or {})
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    return status, data


def test_http_authentication_and_command_guard(server):
    assert request(server, "GET", "/")[0] == 200
    assert request(server, "GET", "/api/state")[0] == 401
    auth = {"Authorization": "Bearer test-token"}
    assert request(server, "GET", "/api/state", headers=auth)[0] == 200
    body = json.dumps({"action": "enable"})
    assert request(server, "POST", "/api/command", body, auth)[0] == 403
    headers = {**auth, "Content-Type": "application/json", "X-MG400-Panel": "1"}
    assert request(server, "POST", "/api/command", body, headers)[0] == 200
    assert request(server, "POST", "/api/command", "{", headers)[0] == 400
    for invalid in ([], {}, None, True):
        assert request(server, "POST", "/api/command",
                       json.dumps({"action": invalid}), headers)[0] == 400
    assert request(server, "POST", "/api/command", body,
                   {**headers, "Sec-Fetch-Site": "cross-site"})[0] == 403
    assert request(server, "GET", "/../config/robot.toml")[0] == 404


def test_remote_bind_requires_access_token(monkeypatch):
    monkeypatch.delenv("MG400_PANEL_TOKEN", raising=False)
    with pytest.raises(SystemExit) as error:
        PANEL["main"](["--bind", "0.0.0.0"])
    assert error.value.code == 2


def test_reverse_proxy_base_path(server):
    server.base_path = "/mg400-panel/"
    auth = {"Authorization": "Bearer test-token"}
    assert request(server, "GET", "/mg400-panel")[0] == 308
    assert request(server, "GET", "/mg400-panel/")[0] == 200
    assert request(server, "GET", "/mg400-panel/api/state", headers=auth)[0] == 200
    assert request(server, "GET", "/api/state", headers=auth)[0] == 404
    headers = {**auth, "Content-Type": "application/json", "X-MG400-Panel": "1"}
    assert request(server, "POST", "/mg400-panel/api/command",
                   json.dumps({"action": "enable"}), headers)[0] == 200
