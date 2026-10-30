"""Exercise CLI control flow without devices, sockets or an installed host."""

import io
import json
from unittest.mock import Mock

import pytest

from orchestrator import cli


@pytest.fixture
def recipe(tmp_path):
    path = tmp_path / "recipe.toml"
    path.write_text("""name = "CLI test"
version = 1
[params.chip_id]
type = "string"
default = "chip-001"
[[steps]]
skill = "sample.record"
params = { chip_id = "${chip_id}", note = "test" }
timeout_s = 1
""")
    return path


class FakeRunner:
    def __init__(self, states, *, abort_waits=()):
        self.states = iter(states)
        self.abort_waits = iter(abort_waits)
        self.status = "created"
        self.aborting = False
        self.started = False
        self.resolutions = []
        self.reports = []

    def start(self):
        self.started = True
        self.status = "running"

    def wait(self, timeout):
        if self.aborting:
            result = next(self.abort_waits, "aborted")
        else:
            result = next(self.states)
        if isinstance(result, BaseException):
            raise result
        self.status = result
        return result

    def snapshot(self):
        result = {
            "run_id": "run-1",
            "status": self.status,
            "detail": "step failed",
            "persistence_error": "disk full",
            "stops": {"robot": {"stop": "unconfirmed"}},
            "unconfirmed_services": ["robot"],
            "release_errors": {"robot": "lease release failed"},
        }
        self.reports.append(result)
        return result

    def abort(self):
        self.aborting = True

    def resume(self, **kwargs):
        self.resolutions.append(kwargs)


def test_hold_requires_exact_manual_completion_and_nonempty_note():
    runner = FakeRunner(["held"] * 4 + ["completed"])
    replies = iter(["resume", "", "manually-completed", " ", "manually-completed", "Placed safely"])
    output = io.StringIO()
    assert cli._drive(runner, output, lambda prompt: next(replies)) == 0
    assert runner.resolutions == [{"manually_completed": True, "note": "Placed safely"}]
    assert not runner.aborting
    assert "will not be retried" in output.getvalue()


def test_resume_refusal_keeps_hold_until_explicit_abort():
    runner = FakeRunner(["held", "held"])
    runner.resume = Mock(side_effect=RuntimeError("old worker still active"))
    replies = iter(["manually-completed", "Checked position", "abort"])
    output = io.StringIO()
    assert cli._drive(runner, output, lambda prompt: next(replies)) == 1
    assert runner.status == "aborted"
    assert "old worker still active" in output.getvalue()


@pytest.mark.parametrize("failure", [EOFError(), KeyboardInterrupt(), OSError("input failed")])
def test_prompt_failure_aborts_and_drains_despite_repeated_interrupts(failure):
    runner = FakeRunner(
        ["held"], abort_waits=[KeyboardInterrupt(), EOFError(), TimeoutError(), "aborted"]
    )
    output = io.StringIO()
    result = cli._drive(runner, output, Mock(side_effect=failure))
    assert result == (130 if isinstance(failure, KeyboardInterrupt) else 1)
    assert runner.status == "aborted"
    report = runner.reports[-1]
    assert report["unconfirmed_services"] == ["robot"]
    assert report["persistence_error"] == "disk full"
    assert report["release_errors"]
    assert '"unconfirmed_services": ["robot"]' in output.getvalue()


def test_broken_output_still_drains_active_runner():
    runner = FakeRunner([], abort_waits=[KeyboardInterrupt(), TimeoutError(), "aborted"])
    output = Mock()
    output.write.side_effect = BrokenPipeError()
    assert cli._drive(runner, output, Mock()) == 1
    assert runner.status == "aborted"


def test_snapshot_failure_still_drains_active_runner():
    runner = FakeRunner([], abort_waits=[TimeoutError(), "aborted"])
    real_snapshot = runner.snapshot
    runner.snapshot = Mock(side_effect=[RuntimeError("snapshot failed"), real_snapshot()])
    assert cli._drive(runner, io.StringIO(), Mock()) == 1
    assert runner.status == "aborted"


@pytest.mark.parametrize("command", ["run", "rehearse"])
@pytest.mark.parametrize(
    "change,arguments",
    [
        ('skill = "unknown"', []),
        (None, ["--param", "chip_id=1"]),
        (None, ["--param", 'unknown="x"']),
        (None, ["--param", 'chip_id="x"', "--param", 'chip_id="y"']),
    ],
)
def test_complete_validation_precedes_store_clients_and_demos(
    recipe, monkeypatch, command, change, arguments
):
    if change:
        recipe.write_text(recipe.read_text().replace('skill = "sample.record"', change))
    factories = [Mock(side_effect=AssertionError("must validate first")) for _ in range(3)]
    for name, factory in zip(("RunStore", "from_settings", "Rehearsal"), factories):
        monkeypatch.setattr(cli, name, factory)
    assert cli.main([command, str(recipe), *arguments], out=io.StringIO()) == 2
    for factory in factories:
        factory.assert_not_called()


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_invalid_rehearsal_scale_precedes_demo_start(recipe, monkeypatch, value):
    demo = Mock(side_effect=AssertionError("must validate first"))
    monkeypatch.setattr(cli, "Rehearsal", demo)
    assert cli.main(["rehearse", str(recipe), "--time-scale", value]) == 2
    demo.assert_not_called()


def test_runs_only_reads_store_and_cannot_restart_held_run(monkeypatch, tmp_path):
    row = {"id": "held-id", "status": "held"}
    store = Mock()
    store.get.return_value = row
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=store)
    factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(cli, "RunStore", factory)
    clients = Mock(side_effect=AssertionError("history cannot create clients"))
    monkeypatch.setattr(cli, "from_settings", clients)
    monkeypatch.setenv("MG400_ORCH_RUNS_DIR", str(tmp_path))
    output = io.StringIO()
    assert cli.main(["runs", "held-id"], out=output) == 0
    factory.assert_called_once_with(tmp_path, readonly=True)
    assert json.loads(output.getvalue()) == row
    clients.assert_not_called()


def test_rehearsal_strips_environment_and_closes_only_after_abort_drain(
    recipe, monkeypatch, tmp_path
):
    runner = FakeRunner(["held"], abort_waits=[KeyboardInterrupt(), "aborted"])
    monkeypatch.setattr(cli, "Runner", Mock(return_value=runner))
    monkeypatch.setattr(
        cli, "from_settings", Mock(side_effect=AssertionError("production clients"))
    )
    for key in (
        "MG400_ORCH_ROBOT_URL",
        "MG400_ORCH_ROBOT_TOKEN",
        "MG400_ROBOT_HOST",
        "HTTP_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "PYTHONPATH",
        "SECRET_TOKEN",
    ):
        monkeypatch.setenv(key, "ambient")
    seen = []

    class Demo:
        def __init__(self, directory, required, *, identity):
            self.clients = {}
            seen.append((directory, required, identity, dict(cli.os.environ)))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            assert runner.status == "aborted"

    monkeypatch.setattr(cli, "Rehearsal", Demo)
    result = cli.main(
        ["rehearse", str(recipe), "--runs-dir", str(tmp_path / "runs")],
        out=io.StringIO(),
        read=Mock(side_effect=EOFError()),
    )
    assert result == 1
    assert seen[0][2].startswith("orch-")
    assert "MG400_ORCH_ROBOT_URL" not in seen[0][3]
    assert "HTTP_PROXY" not in seen[0][3]
    assert "PYTHONPATH" not in seen[0][3]
    assert "SECRET_TOKEN" not in seen[0][3]
    assert cli.os.environ["MG400_ROBOT_HOST"] == "ambient"


def test_real_wait_only_cli_run_and_readonly_history(recipe, tmp_path):
    directory = tmp_path / "runs"
    output = io.StringIO()
    assert cli.main(["run", str(recipe), "--runs-dir", str(directory)], out=output) == 0
    snapshot = json.loads(output.getvalue().splitlines()[-1])
    assert snapshot["status"] == "completed"
    output = io.StringIO()
    assert cli.main(["runs", snapshot["run_id"], "--runs-dir", str(directory)], out=output) == 0
    run = json.loads(output.getvalue())
    assert run["samples"][0]["chip_id"] == "chip-001"
    assert run["steps"][0]["status"] == "completed"
