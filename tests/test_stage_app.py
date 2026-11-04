import http.client
import json
import os
import signal
import socket
import subprocess
import sys

import pytest
from helpers import wait_for
from stage_helpers import home_stage

import stage_controller.__main__ as stage_main
from stage_controller.app import create_app

TOKEN = "stage-panel-test-token-0123456789"
HOST = "100.64.0.1:8767"
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
SETTINGS = (
    "MG400_STAGE_BIND",
    "MG400_STAGE_PORT",
    "MG400_STAGE_TOKEN",
    "MG400_STAGE_SERIAL",
    "MG400_STAGE_BASE_PATH",
)
SHORT_TOKEN = (
    "Set MG400_STAGE_TOKEN to at least 24 characters, or leave it unset and bind to localhost"
)


@pytest.fixture(autouse=True)
def no_settings(monkeypatch):
    """Start each test without the stage panel settings of this machine."""
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)


OPERATOR = "stage-operator"
CLIENT = {"X-MG400-Client": OPERATOR}
OPEN = "Use a loopback Host without forwarded headers."
ACCESS = "Invalid service access token."
REMOVED = ("/", "/api/state", "/api/ports", "/api/motors/top/move", "/static/app.js")


def panel(controller, token=None):
    """Return a test client of the stage service."""
    app = create_app(controller, token)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def web(stage_rig):
    controller, _, _ = stage_rig
    return panel(controller), dict(CLIENT)


@pytest.fixture
def secured(stage_rig):
    """A service with an access token, reached through the VPN address."""
    controller, _, _ = stage_rig
    return panel(controller, TOKEN), {**CLIENT, "Host": HOST, "Authorization": f"Bearer {TOKEN}"}


def command(client, action, headers=CLIENT, **params):
    return client.post("/contract/api/command", json={"action": action, **params}, headers=headers)


def test_ports_are_served_and_the_page_routes_are_removed(web):
    client, _ = web
    assert client.get("/contract/api/ports").json["ports"][0]["device"] == "DEMO"
    for path in REMOVED:
        assert client.get(path).status_code == 404


def test_with_token_removed_routes_return_not_found(secured):
    client, headers = secured
    for path in REMOVED:
        assert client.get(path, headers=headers).status_code == 404
    for path in (
        "/api/stop",
        "/api/connect",
        "/api/disconnect",
        "/api/microstep",
        "/api/motors/top/move",
        "/api/motors/insert/teach_reference",
    ):
        assert client.post(path, json={}, headers=headers).status_code == 404


def test_connect_needs_the_lease_holder(web, stage_rig):
    client, headers = web
    controller, transport, _ = stage_rig
    controller.disconnect()
    response = client.post("/contract/api/connect", json={"port": "DEMO"}, headers=headers)
    assert response.status_code == 409
    assert "lease" in response.json["error"]
    controller.lease.acquire("automation-a")
    response = client.post("/contract/api/connect", json={"port": "DEMO"}, headers=headers)
    assert response.status_code == 409
    assert not controller.snapshot()["connected"]
    controller.lease.release("automation-a")
    assert client.post("/contract/api/heartbeat", json={}, headers=headers).status_code == 200
    response = client.post("/contract/api/connect", json={"port": "DEMO"}, headers=headers)
    assert response.status_code == 200
    assert response.json["connected"] is True
    assert response.json["lease"]["holder"] == OPERATOR


def test_contract_move_and_busy_rejection(web, stage_rig):
    client, headers = web
    controller, _, clock = stage_rig
    home_stage(controller, clock)
    assert client.post("/contract/api/heartbeat", json={}, headers=headers).status_code == 200
    request = {"motor": "top", "direction": "cw", "speed": 250, "steps": 50}
    response = command(client, "move", **request)
    assert response.status_code == 202
    assert controller.snapshot()["motors"][1]["mode"] == "moving"
    assert command(client, "move", **request).status_code == 409
    response = client.post("/contract/api/stop", json={}, headers={"X-MG400-Client": "other"})
    assert response.status_code == 200
    assert controller.snapshot()["motors"][1]["mode"] == "idle"


@pytest.mark.parametrize(
    "path,data",
    [
        ("/contract/api/connect", []),
        ("/contract/api/connect", None),
        ("/contract/api/connect", {}),
        ("/contract/api/connect", {"port": "DEMO", "extra": 1}),
        ("/contract/api/motors/insert/teach_reference", []),
        ("/contract/api/motors/insert/teach_reference", {}),
        ("/contract/api/motors/insert/teach_reference", {"confirm": True, "extra": 1}),
    ],
)
def test_operator_routes_reject_invalid_payloads(web, stage_rig, path, data):
    client, headers = web
    controller, transport, _ = stage_rig
    controller.lease.acquire(OPERATOR)
    transport.commands.clear()
    response = client.post(path, json=data, headers=headers)
    assert response.status_code in (400, 415)
    assert not transport.commands


def test_cross_site_commands_and_untrusted_hosts_are_rejected(web):
    client, headers = web
    for invalid in ({}, {"X-MG400-Client": ""}, {"X-MG400-Client": "caf\xe9"}):
        response = client.post("/contract/api/connect", json={"port": "DEMO"}, headers=invalid)
        assert response.status_code == 400
    plain = client.post(
        "/contract/api/connect", data="{}", content_type="text/plain", headers=headers
    )
    assert plain.status_code == 415
    assert (
        client.get("/contract/api/ports", headers={"Host": "attacker.example"}).status_code == 403
    )
    response = client.get("/contract/api/ports")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_unplug_returns_a_link_fault_in_the_contract(web, stage_rig, monkeypatch):
    from stage_controller.protocol import LinkError

    client, headers = web
    controller, transport, _ = stage_rig

    def unplugged():
        raise LinkError("Port removed")

    monkeypatch.setattr(transport, "status", unplugged)
    assert client.post("/contract/api/heartbeat", json={}, headers=headers).status_code == 200
    response = command(client, "move", motor="bottom", direction="cw", speed=200, steps=200)
    assert response.status_code == 503
    assert not controller.snapshot()["connected"]
    assert client.get("/contract/api/state").json["fault"]["code"] == "device_link_lost"


def test_contract_hold_runs_and_releases_one_motor(web, stage_rig):
    client, headers = web
    controller, _, clock = stage_rig
    home_stage(controller, clock)
    assert client.post("/contract/api/heartbeat", json={}, headers=headers).status_code == 200
    hold = {"motor": "top", "hold_id": "test-api-hold-0001"}
    response = command(client, "hold", direction="cw", speed=50, sampled_at=clock(), **hold)
    assert response.status_code == 202
    assert controller.snapshot()["motors"][1]["mode"] == "continuous"
    clock.advance(0.5)
    assert command(client, "keepalive", **hold).status_code == 202
    assert command(client, "release_hold", **hold).status_code == 202
    motor = controller.snapshot()["motors"][1]
    assert motor["mode"] == "idle"
    assert motor["position"] == 25


@pytest.mark.parametrize("action", ["hold", "keepalive", "release_hold"])
def test_contract_hold_needs_a_valid_identifier(web, stage_rig, action):
    client, headers = web
    controller, transport, clock = stage_rig
    home_stage(controller, clock)
    transport.commands.clear()
    assert client.post("/contract/api/heartbeat", json={}, headers=headers).status_code == 200
    params = {"motor": "bottom", "hold_id": "short"}
    if action == "hold":
        params.update(direction="cw", speed=50, sampled_at=clock())
    assert command(client, action, **params).status_code == 400
    assert command(client, action, headers={}, **params).status_code == 400
    assert not any(item.startswith(("hold", "keepalive")) for item in transport.commands)


def test_contract_microstep_rejects_invalid_and_busy_requests(web, stage_rig):
    client, headers = web
    controller, _, clock = stage_rig
    home_stage(controller, clock)
    assert command(client, "set_microstep", microstep=16).status_code == 409
    assert client.post("/contract/api/heartbeat", json={}, headers=headers).status_code == 200
    assert command(client, "set_microstep", microstep=3).status_code == 400
    response = command(client, "move", motor="insert", direction="cw", speed=50, steps=500)
    assert response.status_code == 202
    assert command(client, "set_microstep", microstep=16).status_code == 409
    assert controller.snapshot()["microstep"] == 1
    assert client.post("/contract/api/stop", json={}, headers=headers).status_code == 200
    assert command(client, "set_microstep", microstep=16).status_code == 202
    state = controller.snapshot()
    assert state["microstep"] == 16
    assert [item["value"] for item in state["microstep_modes"]] == [1, 2, 4, 8, 16]


@pytest.mark.parametrize("port", ["/dev/ttyUSB0", "/dev/serial/by-id/usb-FTDI", "demo", 5])
def test_connect_api_refuses_an_unlisted_port(web, stage_rig, port):
    client, headers = web
    controller, _, _ = stage_rig
    controller.disconnect()
    controller.lease.acquire(OPERATOR)
    response = client.post("/contract/api/connect", json={"port": port}, headers=headers)
    assert response.status_code == 400
    assert not controller.snapshot()["connected"]


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8767", "LOCALHOST", "127.0.0.1:8767", "[::1]:8767"]
)
def test_without_token_loopback_hosts_are_served(web, stage_rig, host):
    client, headers = web
    controller, _, _ = stage_rig
    controller.disconnect()
    controller.lease.acquire(OPERATOR)
    assert client.get("/contract/api/ports", headers={"Host": host}).status_code == 200
    response = client.post(
        "/contract/api/connect", json={"port": "DEMO"}, headers={**headers, "Host": host}
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "host",
    [HOST, "attacker.example", "localhost.attacker.example", "user@localhost", "localhost/x", ""],
)
def test_without_token_other_hosts_are_refused(web, host):
    client, headers = web
    for response in (
        client.get("/", headers={"Host": host}),
        client.get("/contract/api/ports", headers={"Host": host}),
        client.get("/contract/api/motors/insert/reference", headers={"Host": host}),
        client.post(
            "/contract/api/connect", json={"port": "DEMO"}, headers={**headers, "Host": host}
        ),
    ):
        assert response.status_code == 403
        assert response.json == {"error": OPEN}


@pytest.mark.parametrize("name", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP"])
def test_without_token_proxied_requests_are_refused(web, name):
    client, headers = web
    assert client.get("/contract/api/ports", headers={name: "127.0.0.1"}).status_code == 403
    proxied = {**headers, name: "127.0.0.1"}
    assert (
        client.post("/contract/api/connect", json={"port": "DEMO"}, headers=proxied).status_code
        == 403
    )


@pytest.mark.parametrize(
    "authorization",
    [None, "", TOKEN, f"Basic {TOKEN}", f"Bearer {TOKEN}x", f"Bearer {TOKEN}\xe9", "Bearer wrong"],
)
def test_with_token_every_request_needs_the_bearer_token(secured, stage_rig, authorization):
    client, headers = secured
    controller, transport, _ = stage_rig
    controller.disconnect()
    controller.lease.acquire(OPERATOR)
    transport.commands.clear()
    headers = {name: value for name, value in headers.items() if name != "Authorization"}
    if authorization is not None:
        headers["Authorization"] = authorization
    for response in (
        client.get("/", headers=headers),
        client.get("/api/state", headers=headers),
        client.get("/contract/api/ports", headers=headers),
        client.get("/contract/api/motors/insert/reference", headers=headers),
        client.post("/contract/api/connect", json={"port": "DEMO"}, headers=headers),
        client.post(
            "/contract/api/motors/insert/teach_reference", json={"confirm": True}, headers=headers
        ),
    ):
        assert response.status_code == 401
        assert response.json == {"error": ACCESS}
    assert not controller.snapshot()["connected"]
    assert not transport.commands


def test_with_token_any_host_reaches_the_api_with_the_bearer_token(secured, stage_rig):
    client, headers = secured
    controller, _, _ = stage_rig
    assert client.get("/contract/api/ports", headers=headers).json["ports"][0]["device"] == "DEMO"
    proxied = {**headers, "Host": "stage.example", "X-Forwarded-For": "100.64.0.2"}
    assert client.get("/contract/api/ports", headers=proxied).status_code == 200
    controller.disconnect()
    assert client.post("/contract/api/heartbeat", json={}, headers=proxied).status_code == 200
    response = client.post("/contract/api/connect", json={"port": "DEMO"}, headers=proxied)
    assert response.status_code == 200
    assert response.json["connected"] is True


def run_main(monkeypatch, argv, server=None):
    """Run main() with a fake waitress server. Return the create_server options."""
    started = {}

    class Server:
        effective_host, effective_port = "127.0.0.1", 8767

        def run(self):
            pass

    def create_server(app, **options):
        started.update(options, app=app)
        return server or Server()

    monkeypatch.setattr(stage_main, "create_server", create_server)
    stage_main.main(argv)
    return started


@pytest.mark.parametrize("bind", ["0.0.0.0", "100.64.0.1", "::", "stage.example"])
@pytest.mark.parametrize("token", [None, "", "x" * 23])
def test_bind_outside_localhost_needs_a_long_access_token(monkeypatch, capsys, bind, token):
    if token is not None:
        monkeypatch.setenv("MG400_STAGE_TOKEN", token)
    with pytest.raises(SystemExit) as exit_info:
        run_main(monkeypatch, ["--demo", "--bind", bind])
    assert exit_info.value.code == 2
    assert SHORT_TOKEN in capsys.readouterr().err


@pytest.mark.parametrize("bind", ["127.0.0.1", "localhost", "::1"])
def test_loopback_bind_refuses_a_short_access_token(monkeypatch, capsys, bind):
    # With an access token, the panel accepts any host name, also on a loopback bind.
    monkeypatch.setenv("MG400_STAGE_TOKEN", "x" * 23)
    with pytest.raises(SystemExit) as exit_info:
        run_main(monkeypatch, ["--demo", "--bind", bind])
    assert exit_info.value.code == 2
    assert SHORT_TOKEN in capsys.readouterr().err


@pytest.mark.parametrize("token", [" " + TOKEN, TOKEN + " ", TOKEN + "\n", TOKEN + "é"])
def test_access_token_must_be_sendable_by_a_browser(monkeypatch, capsys, token):
    monkeypatch.setenv("MG400_STAGE_TOKEN", token)
    with pytest.raises(SystemExit):
        run_main(monkeypatch, ["--demo"])
    assert "MG400_STAGE_TOKEN must contain only printable ASCII characters" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize("port", ["0", "65536"])
def test_port_must_be_a_tcp_port(monkeypatch, capsys, port):
    with pytest.raises(SystemExit):
        run_main(monkeypatch, ["--demo", "--port", port])
    assert "--port must be between 1 and 65535" in capsys.readouterr().err


@pytest.mark.parametrize("bind", ["127.0.0.1", "localhost", "LOCALHOST", "::1"])
def test_loopback_bind_starts_without_token(monkeypatch, capsys, bind):
    started = run_main(monkeypatch, ["--demo", "--bind", bind])
    options = {key: value for key, value in started.items() if key != "app"}
    # waitress must pass proxy headers on: without a token, the app refuses them.
    # waitress must refuse a large body before it reads the body.
    assert options == {
        "host": bind,
        "port": 8767,
        "threads": 6,
        "clear_untrusted_proxy_headers": False,
        "max_request_body_size": 4097,
    }
    assert "Stage Controller: http://127.0.0.1:8767" in capsys.readouterr().out


def test_vpn_bind_with_token_serves_the_api_only_with_the_bearer_token(monkeypatch, capsys):
    monkeypatch.setenv("MG400_STAGE_TOKEN", TOKEN)

    class Server:
        effective_host, effective_port = "100.64.0.1", 8770

        def run(self):
            pass

    started = run_main(monkeypatch, ["--demo", "--bind", "100.64.0.1", "--port", "8770"], Server())
    assert (started["host"], started["port"]) == ("100.64.0.1", 8770)
    assert "Stage Controller: http://100.64.0.1:8770" in capsys.readouterr().out
    client = started["app"].test_client()
    assert client.get("/contract/api/ports", headers={"Host": "100.64.0.1:8770"}).status_code == 401
    authorized = {"Host": "100.64.0.1:8770", "Authorization": f"Bearer {TOKEN}"}
    assert client.get("/contract/api/ports", headers=authorized).status_code == 200


def test_settings_come_from_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("MG400_STAGE_BIND", "100.64.0.1")
    monkeypatch.setenv("MG400_STAGE_PORT", "8770")
    monkeypatch.setenv("MG400_STAGE_TOKEN", TOKEN)
    started = run_main(monkeypatch, ["--demo"])
    assert (started["host"], started["port"]) == ("100.64.0.1", 8770)
    client = started["app"].test_client()
    assert client.get("/contract/api/ports", headers={"Host": "100.64.0.1:8770"}).status_code == 401
    authorized = {"Host": "100.64.0.1:8770", "Authorization": f"Bearer {TOKEN}"}
    assert client.get("/contract/api/ports", headers=authorized).status_code == 200


@pytest.mark.parametrize("port", ["8770", "not-a-port"])
def test_options_override_the_environment(monkeypatch, port):
    monkeypatch.setenv("MG400_STAGE_BIND", "100.64.0.1")
    monkeypatch.setenv("MG400_STAGE_PORT", port)
    started = run_main(monkeypatch, ["--demo", "--bind", "127.0.0.1", "--port", "8771"])
    assert (started["host"], started["port"]) == ("127.0.0.1", 8771)


def test_empty_settings_use_the_defaults(monkeypatch):
    for name in SETTINGS:
        monkeypatch.setenv(name, "")
    started = run_main(monkeypatch, ["--demo"])
    assert (started["host"], started["port"]) == ("127.0.0.1", 8767)
    assert started["app"].extensions["motor_controller"].serial is None


@pytest.mark.parametrize(
    "port", ["0", "65536", "99999", "-1", "+8767", " 8767", "8767x", "87.5", "8_767", "８７６７"]
)
def test_environment_port_must_be_a_tcp_port(monkeypatch, capsys, port):
    monkeypatch.setenv("MG400_STAGE_PORT", port)
    with pytest.raises(SystemExit) as exit_info:
        run_main(monkeypatch, ["--demo"])
    assert exit_info.value.code == 2
    message = "MG400_STAGE_PORT must be a whole number between 1 and 65535"
    assert message in capsys.readouterr().err


def test_serial_setting_limits_the_port_list(monkeypatch, capsys, stage_usb):
    monkeypatch.setenv("MG400_STAGE_SERIAL", stage_usb["arduino"])
    client = run_main(monkeypatch, [])["app"].test_client()
    assert client.get("/contract/api/ports").json["ports"] == [
        {"device": stage_usb["arduino"], "description": "Arduino Uno"}
    ]
    assert f"Serial port: {stage_usb['arduino']}" in capsys.readouterr().out
    # The option overrides the environment.
    client = run_main(monkeypatch, ["--serial", stage_usb["other"]])["app"].test_client()
    assert client.get("/contract/api/ports").json["ports"] == [
        {"device": stage_usb["other"], "description": "Arduino Mega"}
    ]


def test_without_serial_setting_every_port_but_the_gripper_is_listed(
    monkeypatch, stage_usb, tmp_path
):
    client = run_main(monkeypatch, [])["app"].test_client()
    devices = [port["device"] for port in client.get("/contract/api/ports").json["ports"]]
    assert devices == [str(tmp_path / "ttyACM0"), str(tmp_path / "ttyACM1")]


@pytest.mark.parametrize("option", [False, True])
def test_serial_setting_must_not_be_the_gripper_converter(
    monkeypatch, capsys, stage_usb, tmp_path, option
):
    alias = tmp_path / "stage-arduino"
    alias.symlink_to(tmp_path / "ttyUSB0")
    unplugged = "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_BG02Q5TN-if00-port0"
    name = "--serial" if option else "MG400_STAGE_SERIAL"
    for serial in (stage_usb["gripper"], str(alias), unplugged):
        if option:
            argv = ["--serial", serial]
        else:
            monkeypatch.setenv("MG400_STAGE_SERIAL", serial)
            argv = []
        with pytest.raises(SystemExit) as exit_info:
            run_main(monkeypatch, argv)
        assert exit_info.value.code == 2
        message = f"{name} points to the gripper USB converter (BG02Q5TN)"
        assert message in capsys.readouterr().err


def test_demo_ignores_the_serial_setting(monkeypatch, stage_usb):
    monkeypatch.setenv("MG400_STAGE_SERIAL", stage_usb["gripper"])
    client = run_main(monkeypatch, ["--demo"])["app"].test_client()
    assert client.get("/contract/api/ports").json["ports"][0]["device"] == "DEMO"


def test_printed_urls_name_every_bound_socket():
    class Server:
        effective_listen = [("::1", 8767), ("127.0.0.1", 8767)]

    assert stage_main.listen_urls(Server()) == ["http://[::1]:8767", "http://127.0.0.1:8767"]


@pytest.fixture
def unreplaced_signals():
    """Give the panel signals a handler that main() must replace. Restore them after the test.

    main() would leave a signal that the test process ignores alone.
    """

    def unreplaced(signum, frame):
        raise AssertionError("main() must install its own signal handler")

    previous = {number: signal.signal(number, unreplaced) for number in SIGNALS}
    yield
    for number, handler in previous.items():
        signal.signal(number, handler)


def signalled_server(signum, before_signal=lambda: None):
    """Return a fake waitress server whose run() receives one signal."""

    class Server:
        effective_host, effective_port = "127.0.0.1", 8767

        def run(self):
            before_signal()
            try:
                signal.getsignal(signum)(signum, None)
            except KeyboardInterrupt:
                # waitress then waits for its workers. A second signal must not stop that wait.
                assert {signal.getsignal(number) for number in SIGNALS} == {signal.SIG_IGN}
                return  # Like waitress, return from run() after an interrupt.
            raise AssertionError("The signal handler must interrupt the server")

    return Server()


@pytest.mark.parametrize("signum", SIGNALS, ids=["SIGINT", "SIGTERM", "SIGHUP"])
def test_signals_stop_the_motors_before_main_returns(
    monkeypatch, capsys, stage_rig, unreplaced_signals, signum
):
    controller, transport, _ = stage_rig
    controller.move("bottom", "cw", 50, 500)
    during_close = []
    original_close = transport.close

    def close():
        during_close.append({number: signal.getsignal(number) for number in SIGNALS})
        original_close()

    monkeypatch.setattr(transport, "close", close)
    monkeypatch.setattr(stage_main, "MotorController", lambda **_: controller)
    before = {number: signal.getsignal(number) for number in SIGNALS}
    run_main(monkeypatch, ["--demo"], signalled_server(signum))
    assert transport.commands[-1] == "stop all"
    assert transport.closed
    assert not controller.snapshot()["connected"]
    # Repeated signals cannot interrupt the stop, and main() restores the handlers.
    assert during_close == [dict.fromkeys(SIGNALS, signal.SIG_IGN)]
    assert {number: signal.getsignal(number) for number in SIGNALS} == before
    assert "All motors stopped · disconnected" in capsys.readouterr().out


def test_hung_up_terminal_does_not_prevent_the_stop(monkeypatch, stage_rig, unreplaced_signals):
    controller, transport, _ = stage_rig
    controller.move("bottom", "cw", 50, 500)
    monkeypatch.setattr(stage_main, "MotorController", lambda **_: controller)

    class HungUp:
        def write(self, text):
            raise OSError(5, "Input/output error")

        def flush(self):
            raise OSError(5, "Input/output error")

    def hang_up():
        monkeypatch.setattr(sys, "stdout", HungUp())

    run_main(monkeypatch, ["--demo"], signalled_server(signal.SIGHUP, hang_up))
    assert transport.commands[-1] == "stop all"
    assert transport.closed


def test_an_ignored_signal_stays_ignored(monkeypatch, stage_rig, unreplaced_signals):
    # Like nohup: a closed terminal must not stop the panel.
    controller, _, _ = stage_rig
    monkeypatch.setattr(stage_main, "MotorController", lambda **_: controller)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    during_run = []

    class Server:
        effective_host, effective_port = "127.0.0.1", 8767

        def run(self):
            during_run.append(signal.getsignal(signal.SIGHUP))

    run_main(monkeypatch, ["--demo"], Server())
    assert during_run == [signal.SIG_IGN]
    assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN


def http_request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        return response.status, response.read().decode()
    finally:
        connection.close()


def default_signals():
    # Like systemd, start the service with default signal handling.
    for number in SIGNALS:
        signal.signal(number, signal.SIG_DFL)


@pytest.fixture
def service():
    """Start the stage panel as a process on a free loopback port. Return it and its port."""
    processes = []

    def start(token=None):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        # Like the systemd unit, pass the settings in the environment.
        env = {key: value for key, value in os.environ.items() if key not in SETTINGS}
        env["MG400_STAGE_PORT"] = str(port)
        if token:
            env["MG400_STAGE_TOKEN"] = token
        process = subprocess.Popen(
            [sys.executable, "-m", "stage_controller", "--demo"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            preexec_fn=default_signals,
        )
        processes.append(process)

        def listening():
            try:
                http_request(port, "GET", "/contract/api/ports")
            except OSError:
                return process.poll() is not None
            return True

        wait_for(listening, timeout=20)
        assert process.poll() is None, process.communicate()[0]
        return process, port

    yield start
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.communicate()


@pytest.mark.parametrize("name", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP"])
def test_service_refuses_proxied_requests_without_token(service, name):
    _, port = service()
    assert http_request(port, "GET", "/contract/api/ports")[0] == 200
    assert http_request(port, "GET", "/contract/api/ports", headers={name: "127.0.0.1"})[0] == 403


def test_service_with_token_accepts_the_vpn_host_with_the_bearer_token(service):
    _, port = service(TOKEN)
    vpn = {"Host": f"100.64.0.1:{port}"}
    assert http_request(port, "GET", "/", headers=vpn)[0] == 401
    assert http_request(port, "GET", "/contract/api/ports", headers=vpn)[0] == 401
    authorized = {**vpn, "Authorization": f"Bearer {TOKEN}", "X-Forwarded-For": "100.64.0.2"}
    assert http_request(port, "GET", "/contract/api/ports", headers=authorized)[0] == 200
    assert http_request(port, "GET", "/", headers=authorized)[0] == 404


def test_service_refuses_a_large_body_before_it_reads_the_body(service):
    _, port = service(TOKEN)
    # A body of 4096 bytes reaches the access check, which refuses the request without the token.
    status, _ = http_request(port, "POST", "/contract/api/connect", b"{" + b" " * 4094 + b"}")
    assert status == 401
    # A larger body is refused before the access check. Send only the headers:
    # waitress must answer without waiting for the body.
    status, body = http_request(
        port, "POST", "/contract/api/connect", headers={"Content-Length": "4097"}
    )
    assert status == 413
    assert "Request Entity Too Large" in body


@pytest.mark.parametrize("signum", SIGNALS, ids=["SIGINT", "SIGTERM", "SIGHUP"])
def test_service_signal_stops_the_motors_before_exit(service, signum):
    process, port = service()
    headers = {"Content-Type": "application/json", "X-MG400-Client": OPERATOR}

    def post(path, body):
        status, text = http_request(port, "POST", path, json.dumps(body), headers)
        return status, json.loads(text)

    assert post("/contract/api/heartbeat", {})[0] == 200
    status, body = post("/contract/api/connect", {"port": "DEMO"})
    assert (status, body["connected"]) == (200, True)
    assert post("/contract/api/command", {"action": "home", "motor": "bottom"})[0] == 202

    def homed():
        _, text = http_request(port, "GET", "/contract/api/state")
        last = json.loads(text)["last_command"]
        return last is not None and last["action"] == "home" and last["result"] == "ok"

    wait_for(homed, timeout=10)
    move = {"action": "move", "motor": "bottom", "direction": "cw", "speed": 10, "steps": 1000}
    # A refused move sends nothing. The board refuses moves during the cooldown after homing.
    wait_for(lambda: post("/contract/api/command", move)[0] == 202, timeout=10)
    process.send_signal(signum)
    output, _ = process.communicate(timeout=20)
    assert process.returncode == 0, output
    assert f"Stage Controller: http://127.0.0.1:{port}" in output
    assert "All motors stopped · disconnected" in output


@pytest.mark.parametrize("prefix", ["/stage", "/lab/stage/"])
def test_prefixed_stage_operator_routes_need_the_bearer_token(stage_rig, prefix):
    controller, _, _ = stage_rig
    base = prefix.rstrip("/")
    web = create_app(controller, TOKEN, base_path=prefix).test_client()
    proxy = {"Host": "panel.example:8080", "X-Forwarded-Host": "panel.example:8080"}
    assert web.get(base + "/contract/api/ports", headers=proxy).status_code == 401
    access = {**proxy, "Authorization": f"Bearer {TOKEN}", **CLIENT}
    assert web.get(base + "/contract/api/ports", headers=access).status_code == 200
    assert web.get(base + "/", headers=access).status_code == 404
    assert web.get("/contract/api/ports", headers=access).status_code == 404
    controller.disconnect()
    assert (
        web.post(base + "/contract/api/connect", json={"port": "DEMO"}, headers=access).status_code
        == 409
    )
    assert web.post(base + "/contract/api/heartbeat", json={}, headers=access).status_code == 200
    assert (
        web.post(base + "/contract/api/connect", json={"port": "DEMO"}, headers=access).status_code
        == 200
    )


@pytest.mark.parametrize(
    "prefix",
    [
        None,
        "",
        "stage",
        "//stage",
        "/stage//",
        "/a/../stage",
        "/a/./stage",
        "/%73tage",
        "/stage?x",
        "/stage#x",
        "/stage\\x",
        "/sta ge",
        "/stage\n",
    ],
)
def test_invalid_stage_base_path_precedes_controller_construction(monkeypatch, prefix):
    import stage_controller.app as stage_app

    def forbidden():
        pytest.fail("Invalid prefix constructed a controller")

    monkeypatch.setattr(stage_app, "MotorController", forbidden)
    with pytest.raises(ValueError, match="Base path"):
        stage_app.create_app(base_path=prefix)


def test_stage_base_path_cli_overrides_environment_without_device_access(monkeypatch, capsys):
    monkeypatch.setenv("MG400_STAGE_BASE_PATH", "/from-env/")
    configured = run_main(monkeypatch, ["--demo"])
    assert configured["app"].test_client().get("/from-env/contract/api/ports").status_code == 200
    assert "http://127.0.0.1:8767/from-env/" in capsys.readouterr().out
    configured = run_main(monkeypatch, ["--demo", "--base-path", "/from-cli"])
    web = configured["app"].test_client()
    assert web.get("/from-cli/contract/api/ports").status_code == 200
    assert web.get("/from-env/contract/api/ports").status_code == 404
    monkeypatch.setenv("MG400_STAGE_BASE_PATH", "/../invalid")
    monkeypatch.setattr(
        stage_main, "MotorController", lambda **kwargs: pytest.fail("controller started")
    )
    with pytest.raises(SystemExit) as error:
        stage_main.main(["--demo"])
    assert error.value.code == 2
