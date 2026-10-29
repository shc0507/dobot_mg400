import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys

import pytest
from helpers import wait_for
from stage_helpers import Clock, RecordingTransport, serial_port

import stage_controller.__main__ as stage_main
from stage_controller.app import create_app
from stage_controller.controller import MotorController

TOKEN = "stage-panel-test-token-0123456789"
HOST = "100.64.0.1:8767"
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
SETTINGS = ("MG400_STAGE_BIND", "MG400_STAGE_PORT", "MG400_STAGE_TOKEN", "MG400_STAGE_SERIAL")
SHORT_TOKEN = (
    "Set MG400_STAGE_TOKEN to at least 24 characters, or leave it unset and bind to localhost"
)


@pytest.fixture(autouse=True)
def no_settings(monkeypatch):
    """Start each test without the stage panel settings of this machine."""
    for name in SETTINGS:
        monkeypatch.delenv(name, raising=False)


def panel(controller, token=None, host="localhost"):
    """Return a test client of the panel and the command token header of its page."""
    app = create_app(controller, token)
    app.config["TESTING"] = True
    client = app.test_client()
    html = client.get("/", headers={"Host": host}).text
    control = re.search(r'name="control-token" content="([^"]+)"', html)[1]
    return client, {"X-Control-Token": control}


@pytest.fixture
def web(stage_rig):
    controller, _, _ = stage_rig
    return panel(controller)


@pytest.fixture
def secured(stage_rig):
    """A panel with an access token, reached through the VPN address."""
    controller, _, _ = stage_rig
    client, headers = panel(controller, TOKEN, HOST)
    return client, {**headers, "Host": HOST, "Authorization": f"Bearer {TOKEN}"}


def test_dashboard_assets_ports_and_state_are_served(web):
    client, _ = web
    assert "Motion control" in client.get("/").text
    for path in ("/static/app.js", "/static/app.css"):
        with client.get(path) as response:
            assert response.status_code == 200
    assert client.get("/api/ports").json["ports"][0]["device"] == "DEMO"
    assert len(client.get("/api/state").json["motors"]) == 3


def test_api_move_and_cross_tab_busy_rejection(web):
    client, headers = web
    request = {"direction": "ccw", "speed": 250, "steps": 50}
    response = client.post("/api/motors/top/move", json=request, headers=headers)
    assert response.status_code == 200
    assert response.json["motors"][1]["mode"] == "moving"
    assert client.post("/api/motors/top/move", json=request, headers=headers).status_code == 409
    assert client.post("/api/stop", json={}, headers=headers).status_code == 200
    assert client.post("/api/disconnect", json={}, headers=headers).json["connected"] is False
    assert client.post("/api/connect", json={"port": "DEMO"}, headers=headers).status_code == 200


@pytest.mark.parametrize("data", [[], None, {}, {"direction": "cw", "speed": 0, "steps": 3}])
def test_api_rejects_invalid_payloads(web, data):
    client, headers = web
    response = client.post("/api/motors/bottom/move", json=data, headers=headers)
    assert response.status_code in (400, 415)


def test_cross_site_commands_and_untrusted_hosts_are_rejected(web):
    client, headers = web
    assert client.post("/api/stop", json={}).status_code == 403
    for control in ("bad", "caf\xe9"):
        response = client.post("/api/stop", json={}, headers={"X-Control-Token": control})
        assert response.status_code == 403
    plain = client.post("/api/stop", data="{}", content_type="text/plain", headers=headers)
    assert plain.status_code == 415
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 403
    response = client.get("/")
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


def test_unplug_returns_disconnected_status_in_api(web, stage_rig, monkeypatch):
    from stage_controller.protocol import LinkError

    client, headers = web
    controller, transport, _ = stage_rig

    def unplugged():
        raise LinkError("Port removed")

    monkeypatch.setattr(transport, "status", unplugged)
    response = client.post(
        "/api/motors/bottom/move",
        headers=headers,
        json={"direction": "cw", "speed": 200, "steps": 200},
    )
    assert response.status_code == 503
    assert not client.get("/api/state").json["connected"]


def test_hold_api_runs_and_releases_one_motor(web, stage_rig):
    client, headers = web
    _, _, clock = stage_rig
    payload = {
        "direction": "ccw",
        "speed": 50,
        "hold_id": "test-api-hold-0001",
        "sampled_at": client.get("/api/state").json["sampled_at"],
    }
    response = client.post("/api/motors/top/hold", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json["motors"][1]["mode"] == "continuous"
    clock.advance(0.5)
    response = client.post("/api/motors/top/hold/keepalive", json=payload, headers=headers)
    assert response.status_code == 200
    response = client.post("/api/motors/top/hold/stop", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json["motors"][1]["mode"] == "idle"
    assert response.json["motors"][1]["position"] == -25


@pytest.mark.parametrize("suffix", ["hold", "hold/keepalive", "hold/stop"])
def test_hold_api_requires_control_token_and_valid_identifier(web, suffix):
    client, headers = web
    path = f"/api/motors/bottom/{suffix}"
    assert client.post(path, json={}).status_code == 403
    assert client.post(path, json={}, headers=headers).status_code == 400
    assert client.post(path, json=[], headers=headers).status_code == 400


def test_global_microstep_api_and_dashboard(web):
    client, headers = web
    html = client.get("/").text
    assert 'aria-labelledby="microstep-title"' in html
    assert html.index('id="microstep-title"') < html.index('id="motor-bottom"')
    response = client.post("/api/microstep", json={"microstep": 16}, headers=headers)
    assert response.status_code == 200
    assert response.json["microstep"] == 16
    assert client.get("/api/state").json["microstep"] == 16
    assert [item["value"] for item in response.json["microstep_modes"]] == [1, 2, 4, 8, 16]


def test_microstep_api_rejects_busy_and_untrusted_requests(web):
    client, headers = web
    assert client.post("/api/microstep", json={"microstep": 16}).status_code == 403
    assert client.post("/api/microstep", json={"microstep": 3}, headers=headers).status_code == 400
    assert client.post("/api/microstep", json=[], headers=headers).status_code == 400
    response = client.post(
        "/api/motors/insert/move",
        json={"direction": "cw", "speed": 50, "steps": 500},
        headers=headers,
    )
    assert response.status_code == 200
    response = client.post("/api/microstep", json={"microstep": 16}, headers=headers)
    assert response.status_code == 409
    assert client.get("/api/state").json["microstep"] == 1


@pytest.mark.parametrize("port", ["/dev/ttyUSB0", "/dev/serial/by-id/usb-FTDI", "demo", 5])
def test_connect_api_refuses_an_unlisted_port(web, port):
    client, headers = web
    response = client.post("/api/connect", json={"port": port}, headers=headers)
    assert response.status_code == 400


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8767", "LOCALHOST", "127.0.0.1:8767", "[::1]:8767"]
)
def test_without_token_loopback_hosts_are_served(web, host):
    client, headers = web
    assert client.get("/", headers={"Host": host}).status_code == 200
    assert client.get("/api/state", headers={"Host": host}).status_code == 200
    assert client.post("/api/stop", json={}, headers={**headers, "Host": host}).status_code == 200


@pytest.mark.parametrize(
    "host",
    [HOST, "attacker.example", "localhost.attacker.example", "user@localhost", "localhost/x", ""],
)
def test_without_token_other_hosts_are_refused(web, host):
    client, headers = web
    for response in (
        client.get("/", headers={"Host": host}),
        client.get("/static/app.js", headers={"Host": host}),
        client.get("/api/state", headers={"Host": host}),
        client.post("/api/stop", json={}, headers={**headers, "Host": host}),
    ):
        assert response.status_code == 403
        assert response.json == {"error": "Open the panel at 127.0.0.1 or localhost"}


@pytest.mark.parametrize("name", ["Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP"])
def test_without_token_proxied_requests_are_refused(web, name):
    client, headers = web
    assert client.get("/", headers={name: "127.0.0.1"}).status_code == 403
    assert client.get("/api/state", headers={name: "127.0.0.1"}).status_code == 403
    proxied = {**headers, name: "127.0.0.1"}
    assert client.post("/api/stop", json={}, headers=proxied).status_code == 403


@pytest.mark.parametrize(
    "authorization",
    [None, "", TOKEN, f"Basic {TOKEN}", f"Bearer {TOKEN}x", f"Bearer {TOKEN}\xe9", "Bearer wrong"],
)
def test_with_token_every_api_request_needs_the_bearer_token(secured, authorization):
    client, headers = secured
    headers = {name: value for name, value in headers.items() if name != "Authorization"}
    if authorization is not None:
        headers["Authorization"] = authorization
    for response in (
        client.get("/api/state", headers=headers),
        client.get("/api/ports", headers=headers),
        client.post("/api/stop", json={}, headers=headers),
        client.post("/api/connect", json={"port": "DEMO"}, headers=headers),
        client.post("/api/motors/bottom/move", json={}, headers=headers),
    ):
        assert response.status_code == 401
        assert response.json == {"error": "Enter the panel access token"}


def test_with_token_any_host_reaches_the_api_with_the_bearer_token(secured):
    client, headers = secured
    assert client.get("/api/state", headers=headers).json["connected"]
    assert client.get("/api/ports", headers=headers).json["ports"][0]["device"] == "DEMO"
    move = {"direction": "cw", "speed": 50, "steps": 50}
    response = client.post("/api/motors/bottom/move", json=move, headers=headers)
    assert response.status_code == 200
    assert response.json["motors"][0]["mode"] == "moving"
    # The bearer token does not replace the page's command token.
    without_command_token = {**headers, "X-Control-Token": "bad"}
    assert client.post("/api/stop", json={}, headers=without_command_token).status_code == 403
    proxied = {**headers, "Host": "stage.example", "X-Forwarded-For": "100.64.0.2"}
    assert client.get("/api/state", headers=proxied).status_code == 200


def test_with_token_page_and_assets_load_without_the_bearer_token(secured):
    client, _ = secured
    response = client.get("/", headers={"Host": HOST})
    assert response.status_code == 200
    assert 'name="control-token"' in response.text
    assert 'aria-label="Panel access token"' in response.text
    with client.get("/static/app.js", headers={"Host": HOST}) as asset:
        assert asset.status_code == 200


def test_page_script_avoids_apis_that_plain_http_removes(secured):
    # Over the VPN the page is http://100.64.0.1:8767, not a secure context.
    # There crypto.randomUUID is undefined, and every hold failed without a message.
    client, _ = secured
    with client.get("/static/app.js", headers={"Host": HOST}) as asset:
        assert "randomUUID" not in asset.text


@pytest.mark.parametrize("control", ["bad", "caf\xe9", "\xff\xfe"])
@pytest.mark.parametrize("path", ["/", "/static/app.js", "/api/stop"])
def test_with_token_a_wrong_command_token_is_refused(secured, path, control):
    client, headers = secured
    response = client.post(path, json={}, headers={**headers, "X-Control-Token": control})
    assert response.status_code == 403
    assert response.json == {"error": "Reload this page before sending another command."}


def test_page_without_token_shows_no_ports_or_session_data(stage_ports):
    stage_ports.append(serial_port("/dev/ttyACM7", 0x2341, 0x0043, "ARDUINO7", "Arduino Uno"))
    clock = Clock()
    controller = MotorController(transport_factory=lambda _port: RecordingTransport(clock))
    controller.connect("/dev/ttyACM7")
    controller.move("bottom", "cw", 50, 50)
    try:
        client, _ = panel(controller, TOKEN, HOST)
        html = client.get("/", headers={"Host": HOST}).text
    finally:
        controller.close()
    assert controller.snapshot()["events"]
    for live in ("ttyACM7", "Arduino Uno", "Connected to", "pulses at"):
        assert live not in html
    assert 'id="motor-insert"' in html


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
    assert client.get("/api/state", headers={"Host": "100.64.0.1:8770"}).status_code == 401
    authorized = {"Host": "100.64.0.1:8770", "Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/state", headers=authorized).status_code == 200


def test_settings_come_from_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("MG400_STAGE_BIND", "100.64.0.1")
    monkeypatch.setenv("MG400_STAGE_PORT", "8770")
    monkeypatch.setenv("MG400_STAGE_TOKEN", TOKEN)
    started = run_main(monkeypatch, ["--demo"])
    assert (started["host"], started["port"]) == ("100.64.0.1", 8770)
    client = started["app"].test_client()
    assert client.get("/api/state", headers={"Host": "100.64.0.1:8770"}).status_code == 401
    authorized = {"Host": "100.64.0.1:8770", "Authorization": f"Bearer {TOKEN}"}
    assert client.get("/api/state", headers=authorized).status_code == 200


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
    assert client.get("/api/ports").json["ports"] == [
        {"device": stage_usb["arduino"], "description": "Arduino Uno"}
    ]
    assert f"Serial port: {stage_usb['arduino']}" in capsys.readouterr().out
    # The option overrides the environment.
    client = run_main(monkeypatch, ["--serial", stage_usb["other"]])["app"].test_client()
    assert client.get("/api/ports").json["ports"] == [
        {"device": stage_usb["other"], "description": "Arduino Mega"}
    ]


def test_without_serial_setting_every_port_but_the_gripper_is_listed(
    monkeypatch, stage_usb, tmp_path
):
    client = run_main(monkeypatch, [])["app"].test_client()
    devices = [port["device"] for port in client.get("/api/ports").json["ports"]]
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
    assert client.get("/api/ports").json["ports"][0]["device"] == "DEMO"


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
                http_request(port, "GET", "/static/favicon.svg")
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
    assert http_request(port, "GET", "/api/state")[0] == 200
    assert http_request(port, "GET", "/api/state", headers={name: "127.0.0.1"})[0] == 403


def test_service_with_token_accepts_the_vpn_host_with_the_bearer_token(service):
    _, port = service(TOKEN)
    vpn = {"Host": f"100.64.0.1:{port}"}
    assert http_request(port, "GET", "/", headers=vpn)[0] == 200
    assert http_request(port, "GET", "/api/state", headers=vpn)[0] == 401
    authorized = {**vpn, "Authorization": f"Bearer {TOKEN}", "X-Forwarded-For": "100.64.0.2"}
    assert http_request(port, "GET", "/api/state", headers=authorized)[0] == 200


def test_service_refuses_a_large_body_before_it_reads_the_body(service):
    _, port = service(TOKEN)
    # A body of 4096 bytes reaches the access check, which refuses the request without the token.
    status, _ = http_request(port, "POST", "/api/stop", b"{" + b" " * 4094 + b"}")
    assert status == 401
    # A larger body is refused before the access check. Send only the headers:
    # waitress must answer without waiting for the body.
    status, body = http_request(port, "POST", "/api/stop", headers={"Content-Length": "4097"})
    assert status == 413
    assert "Request Entity Too Large" in body


@pytest.mark.parametrize("signum", SIGNALS, ids=["SIGINT", "SIGTERM", "SIGHUP"])
def test_service_signal_stops_the_motors_before_exit(service, signum):
    process, port = service()
    _, html = http_request(port, "GET", "/")
    control = re.search(r'name="control-token" content="([^"]+)"', html)[1]
    headers = {"Content-Type": "application/json", "X-Control-Token": control}
    connect = json.dumps({"port": "DEMO"})
    status, body = http_request(port, "POST", "/api/connect", connect, headers)
    assert (status, json.loads(body)["connected"]) == (200, True)
    move = json.dumps({"direction": "cw", "speed": 10, "steps": 1000})
    assert http_request(port, "POST", "/api/motors/bottom/move", move, headers)[0] == 200
    process.send_signal(signum)
    output, _ = process.communicate(timeout=20)
    assert process.returncode == 0, output
    assert f"Stage Controller: http://127.0.0.1:{port}" in output
    assert "All motors stopped · disconnected" in output
