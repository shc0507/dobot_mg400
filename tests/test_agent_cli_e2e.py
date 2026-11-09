"""labctl agent groups against a real demo orchestrator (`serve --demo`) on loopback."""

import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

from orchestrator import agent_cli


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def console(tmp_path_factory):
    port = _free_port()
    log = tmp_path_factory.mktemp("demo") / "serve.log"
    env = {key: value for key, value in os.environ.items() if not key.startswith("MG400_")}
    with open(log, "wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "orchestrator", "serve", "--demo", "--port", str(port)]
            + ["--base-path", "/lab/"],
            stdout=output,
            stderr=subprocess.STDOUT,
            env=env,
        )
    url = f"http://127.0.0.1:{port}/lab/"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 60
    try:
        while True:
            try:
                opener.open(url + "api/console/state", timeout=2).close()
                break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise AssertionError(log.read_text()) from None
                time.sleep(0.2)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(15)
        except subprocess.TimeoutExpired:
            process.kill()


def labctl(console, *argv, session="e2e"):
    out = io.StringIO()
    code = agent_cli.main([*argv, "--url", console, "--session", session], out=out)
    return code, json.loads(out.getvalue())


def test_robot_take_enable_move_vacuum_and_release(console):
    assert labctl(console, "robot", "take")[0] == 0
    code, result = labctl(console, "robot", "enable")
    assert code == 0 and result["state"]["enabled"] is True
    start = result["state"]["actual"]
    code, result = labctl(console, "robot", "move", "0", "0", "-10", "0", "--relative")
    assert code == 0, result
    assert result["state"]["actual"][2] == pytest.approx(start[2] - 10)
    code, result = labctl(console, "robot", "move", *map(str, start), "--speed", "999")
    assert code == 1 and result["outcome"] == "refused"
    assert labctl(console, "robot", "vacuum", "suck")[0] == 0
    assert labctl(console, "robot", "vacuum", "off")[0] == 0
    code, result = labctl(console, "robot", "release")
    assert code == 0 and result["result"] == "released"
    code, result = labctl(console, "robot", "state")
    assert code == 0 and result["state"]["enabled"] is False and result["mine"] is False


def test_commands_need_control_and_one_controller_per_device(console):
    # The demo starts the stage service at its first take.
    assert labctl(console, "stage", "take", session="other")[0] == 0
    code, result = labctl(console, "stage", "home", "top")
    assert code == 1 and result["hint"] == "labctl stage take"
    code, result = labctl(console, "stage", "take")
    assert code == 1 and result["error"]["code"] == "held_by_other"
    # Stops need no control. A robot service without feedback reports its stop unconfirmed.
    code, result = labctl(console, "all", "stop")
    assert result["results"]["stage"] == "confirmed"
    assert code == (0 if result["results"]["robot"] == "confirmed" else 3)
    assert labctl(console, "all", "release", session="other")[0] == 0


def test_furnace_caps_stay_in_the_service(console):
    assert labctl(console, "furnace", "take")[0] == 0
    code, result = labctl(console, "furnace", "setpoint", "5000", "--ramp", "5")
    assert code == 1 and "at most" in result["error"]["message"]
    code, result = labctl(console, "furnace", "stop")
    assert code == 0 and result["results"] == {"furnace": "confirmed"}
    code, result = labctl(console, "all", "session")
    assert [item["device"] for item in result["control"]] == ["furnace"]
    assert labctl(console, "all", "release")[0] == 0
