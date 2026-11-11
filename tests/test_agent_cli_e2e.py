"""labctl agent groups against a real demo orchestrator (`serve --demo`) on loopback."""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from orchestrator import agent_cli

ROOT = Path(__file__).resolve().parents[1]
RECIPES = {
    "robot-fail.toml": """
[[steps]]
skill = "robot.enable"
params = {}
timeout_s = 5

[[steps]]
skill = "fail"
params = { message = "synthetic failure" }
timeout_s = 5
""",
    "robot-wait.toml": """
[[steps]]
skill = "robot.enable"
params = {}
timeout_s = 5

[[steps]]
skill = "wait"
params = { seconds = 30.0 }
timeout_s = 60
""",
}


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve(tmp_path_factory):
    port = _free_port()
    log = tmp_path_factory.mktemp("demo") / "serve.log"
    env = {key: value for key, value in os.environ.items() if not key.startswith("MG400_")}
    recipes = tmp_path_factory.mktemp("recipes")
    shutil.copy(ROOT / "recipes/example-rehearsal.toml", recipes)
    for name, steps in RECIPES.items():
        (recipes / name).write_text(f'name = "{name}"\nversion = 1\nparams = {{}}\n{steps}')
    with open(log, "wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "orchestrator", "serve", "--demo", "--port", str(port)]
            + ["--base-path", "/lab/", "--recipes-dir", str(recipes)],
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


@pytest.fixture(scope="module")
def console(tmp_path_factory):
    yield from _serve(tmp_path_factory)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """A separate demo for runs: a STOP ALL in other tests leaves the console draining."""
    yield from _serve(tmp_path_factory)


def labctl(console, *argv, session="e2e"):
    out = io.StringIO()
    code = agent_cli.main([*argv, "--url", console, "--session", session], out=out)
    return code, json.loads(out.getvalue())


def pose(console):
    """The pose is unknown until the first feedback sample after a move."""
    deadline = time.monotonic() + 5
    while True:
        actual = labctl(console, "robot", "state")[1]["state"]["actual"]
        if None not in actual or time.monotonic() > deadline:
            return actual
        time.sleep(0.1)


def test_robot_take_enable_move_vacuum_and_release(console):
    assert labctl(console, "robot", "take")[0] == 0
    code, result = labctl(console, "robot", "enable")
    assert code == 0 and result["state"]["enabled"] is True
    start = pose(console)
    code, result = labctl(console, "robot", "move", "0", "0", "-10", "0", "--relative")
    assert code == 0, result
    assert pose(console)[2] == pytest.approx(start[2] - 10)
    code, result = labctl(console, "robot", "move", *map(str, start), "--speed", "999")
    assert code == 1 and result["outcome"] == "refused"
    assert labctl(console, "robot", "vacuum", "suck")[0] == 0
    assert labctl(console, "robot", "vacuum", "off")[0] == 0
    code, result = labctl(console, "robot", "release")
    assert code == 0 and result["result"] == "released"
    code, result = labctl(console, "robot", "state")
    assert code == 0 and result["state"]["enabled"] is False and result["mine"] is False
    code, result = labctl(console, "all", "history", "--device", "robot")
    assert code == 0 and {row["device"] for row in result["states"]} == {"robot"}
    code, result = labctl(console, "all", "events", "--device", "runs")
    assert code == 0 and result["events"], result
    newest = result["events"][0]["id"]
    code, result = labctl(console, "all", "events", "--device", "runs", "--since", str(newest))
    assert code == 0 and result["events"] == []


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


def orchestrator_state(console):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(console + "api/state", timeout=5) as response:
        return json.loads(response.read())


def test_do_borrows_held_devices_and_returns_them_after_the_run(runs):
    code, result = labctl(runs, "do", "example-rehearsal", session="run")
    assert code == 1 and result["hint"] == "labctl robot take"
    assert labctl(runs, "robot", "take", session="run")[0] == 0
    code, result = labctl(runs, "do", "example-rehearsal", session="run")
    assert code == 0 and result["status"] == "completed", result
    assert [step["status"] for step in result["steps"]] == ["completed"] * 4
    assert result["devices"] == ["robot"]
    code, state = labctl(runs, "robot", "state", session="run")
    assert state["mine"] is True and state["state"]["enabled"] is True
    # The device is commandable again after the run.
    assert labctl(runs, "robot", "home", session="run")[0] in {0, 1}


def test_do_aborts_a_held_run_and_keeps_control(runs):
    labctl(runs, "robot", "take", session="run")
    code, result = labctl(runs, "do", "robot-fail", session="run")
    assert code == 1 and result.get("status") == "aborted", json.dumps(result)[:600]
    assert result["steps"][1]["status"] != "completed", result["steps"]
    assert result["devices"] == ["robot"]
    assert labctl(runs, "all", "release", session="run")[0] == 0


def test_killed_cli_during_do_holds_the_run(runs):
    assert labctl(runs, "robot", "take", session="kill")[0] == 0
    process = subprocess.Popen(
        [sys.executable, "-m", "orchestrator", "do", "robot-wait"]
        + ["--url", runs, "--session", "kill"],
        stdout=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    # The previous test's closed run also reports step 1, so wait for an open run.
    while (active := orchestrator_state(runs).get("active_run") or {}).get("closed", True) or (
        active.get("step") != 1
    ):
        assert time.monotonic() < deadline, (active, process.poll())
        time.sleep(0.1)
    # A lent device refuses commands from its page; stops still work.
    code, result = labctl(runs, "robot", "home", session="kill")
    assert code == 1 and result["error"]["code"] == "busy"
    process.kill()
    process.wait(5)
    deadline = time.monotonic() + 20
    while orchestrator_state(runs)["active_run"]["status"] != "held":
        assert time.monotonic() < deadline
        time.sleep(0.1)
    run_id = orchestrator_state(runs)["active_run"]["run_id"]
    # The owner aborts the held run; the device returns to the session.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Content-Type": "application/json", "X-MG400-Client": "owner"}
    for path in ("api/heartbeat", f"api/runs/{run_id}/abort"):
        request = urllib.request.Request(runs + path, b"{}", headers)
        opener.open(request, timeout=5).close()
    deadline = time.monotonic() + 20
    while not orchestrator_state(runs)["active_run"]["closed"]:
        assert time.monotonic() < deadline
        time.sleep(0.1)
    code, result = labctl(runs, "all", "session", session="kill")
    assert [item["lent"] for item in result["control"]] == [False]
    assert labctl(runs, "all", "release", session="kill")[0] == 0
