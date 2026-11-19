import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_signal", ROOT / "scripts/agent_signal.py")
sig = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sig)

REPORT = "\n".join(
    [
        "Task: suction-T4 Retire the USB gripper",
        "Branch/PR: claude/gripper-retire / #317 (merged)",
        "Changed: robot service",
        "Tests: uv run pytest -q tests/test_suction.py -> pass (12)",
        "CI: green",
        "Deployed: yes (web)",
        "Bench: none",
        "BLOCKED: none",
        "Notes: T5 must measure the cup offset again,",
        "and the vision docs still give 246 mm.",
    ]
)
PERMISSION = {
    "hook_event_name": "Notification",
    "notification_type": "permission_prompt",
    "message": "Claude needs your permission to use Bash",
}


def no_herdr(*args):
    raise FileNotFoundError("herdr")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(sig, "ROOT", tmp_path)
    monkeypatch.setattr(sig, "herdr", no_herdr)
    monkeypatch.setenv("HERDR_PANE_ID", "t1:p1")
    monkeypatch.setenv("HERDR_ENV", "0")
    return tmp_path


class MovedHerdr:
    """Herdr after `pane move w1:p11 --new-workspace`: the pane is now w7:p1.

    The terminal id stays, `pane get` still resolves the old id, and the agent
    keeps HERDR_PANE_ID=w1:p11. Pane w1:p5 is another pane, run by other processes.
    """

    def __init__(self, shell_pid):
        self.panes = {
            "w7:p1": {"pane_id": "w7:p1", "terminal_id": "term_a", "shell": shell_pid},
            "w1:p5": {"pane_id": "w1:p5", "terminal_id": "term_b", "shell": 1},
        }
        self.aliases = {"w1:p11": "w7:p1"}
        self.agents = [
            {"name": "alpha", "pane_id": "w7:p1", "terminal_id": "term_a", "agent_status": "done"}
        ]

    def __call__(self, *args):
        def out(result):
            return json.dumps({"result": result})

        if args == ("agent", "list"):
            return out({"agents": self.agents})
        if args == ("pane", "list"):
            return out({"panes": list(self.panes.values())})
        if args[:2] == ("pane", "get"):
            pane = self.panes.get(self.aliases.get(args[2], args[2]))
            return out({"pane": pane}) if pane else json.dumps({"error": {}})
        if args[:3] == ("pane", "process-info", "--pane"):
            info = {"shell_pid": self.panes[args[3]]["shell"], "foreground_processes": []}
            return out({"process_info": info})
        if args[:2] == ("pane", "read"):
            return "screen line\n"
        raise AssertionError(args)


def events(state, pane="t1:p1"):
    path = state / "events" / f"{sig.safe(pane)}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def stop(message, background=()):
    sig.hook(
        json.dumps(
            {
                "hook_event_name": "Stop",
                "last_assistant_message": message,
                "background_tasks": list(background),
            }
        )
    )


def test_report_block_takes_the_last_fenced_block():
    old = REPORT.replace("suction-T4", "old-task")
    message = f"Earlier:\n```\n{old}\n```\nMerged.\n\n```\n{REPORT}\n```"
    assert sig.report_block(message) == REPORT


def test_report_block_ignores_the_dispatch_prompt():
    prompt = "Read docs/agents/worker.md. Task: x1: Fix the panel.\nUse medium effort."
    assert sig.report_block(prompt) is None


def test_stop_during_background_work_writes_nothing(state):
    stop("CI runs. I merge when it is green.", [{"id": "b1", "type": "shell", "status": "running"}])
    assert events(state) == []


def test_stop_with_report_writes_report(state):
    stop(f"Merged.\n\n```\n{REPORT}\n```")
    [event] = events(state)
    assert (event["kind"], event["slug"], event["text"]) == ("report", "suction-T4", REPORT)


def test_stop_without_report_or_background_work_writes_idle(state, monkeypatch):
    payload = {
        "hook_event_name": "Stop",
        "last_assistant_message": "Tests pass.\nDeploy orch now?",
        "background_tasks": [],
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    sig.hook(None)
    [event] = events(state)
    assert (event["kind"], event["text"]) == ("idle", "Tests pass.\nDeploy orch now?")


@pytest.mark.parametrize(
    ("payload", "text"),
    [
        (PERMISSION, "Claude needs your permission to use Bash"),
        (
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "AskUserQuestion",
                "tool_input": {"questions": [{"question": "Is the arm clear?"}]},
            },
            "question: Is the arm clear?",
        ),
        (
            {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Bash",
                "tool_input": {"command": "sudo true"},
            },
            'Bash: {"command": "sudo true"}',
        ),
    ],
)
def test_dialogs_write_blocked(state, payload, text):
    sig.hook(json.dumps(payload))
    [event] = events(state)
    assert (event["kind"], event["text"]) == ("blocked", text)


def test_other_notifications_write_nothing(state):
    sig.hook(
        json.dumps(
            {"hook_event_name": "Notification", "notification_type": "idle_prompt", "message": "x"}
        )
    )
    assert events(state) == []


def test_same_dialog_twice_writes_one_event(state):
    sig.hook(json.dumps(PERMISSION))
    sig.hook(json.dumps(PERMISSION))
    assert len(events(state)) == 1


def test_stop_failure_writes_error(state):
    sig.hook(
        json.dumps(
            {
                "hook_event_name": "StopFailure",
                "error": "rate_limit",
                "error_details": "5-hour limit",
            }
        )
    )
    [event] = events(state)
    assert (event["kind"], event["text"]) == ("error", "rate_limit 5-hour limit")


def test_codex_notify_payload_writes_report(state):
    payload = {"type": "agent-turn-complete", "thread-id": "th1", "last-assistant-message": REPORT}
    assert sig.main(["hook", json.dumps(payload)]) == 0
    [event] = events(state)
    assert (event["kind"], event["slug"]) == ("report", "suction-T4")


def test_hook_outside_herdr_or_with_bad_input_writes_nothing(state, monkeypatch):
    assert sig.main(["hook", "not json"]) == 0
    monkeypatch.delenv("HERDR_PANE_ID")
    stop("Deploy now?")
    assert events(state) == []


def test_wait_prints_each_event_once(state, capsys):
    stop(REPORT)
    sig.wait(["t1:p1"], 0)
    out = capsys.readouterr().out
    assert out.splitlines()[:2] == [
        "report ? (t1:p1) suction-T4:",
        "  Task: suction-T4 Retire the USB gripper",
    ]
    sig.wait(["t1:p1"], 0)
    assert capsys.readouterr().out == "tick: no events in 0 min\n"


def test_wait_adds_herdr_blocked_and_exit_once(state, monkeypatch, capsys):
    monkeypatch.setenv("HERDR_ENV", "1")
    agents = {
        "term_a": {
            "name": "alpha",
            "pane_id": "w9:p1",
            "terminal_id": "term_a",
            "agent_status": "blocked",
            "state_change_seq": 7,
        }
    }
    monkeypatch.setattr(sig, "herdr_agents", lambda: agents)
    monkeypatch.setattr(sig, "screen", lambda pane: ["Do you want to proceed?", "1. Yes"])
    sig.wait(["alpha", "w9:p3"], 0)
    out = capsys.readouterr().out
    blocked = "blocked alpha (w9:p1):\n  dialog on screen (Herdr)\n"
    assert blocked + "  screen:\n    Do you want to proceed?\n" in out
    assert "exit" not in out  # One check without the agent is not yet an exit.


def test_exit_needs_two_checks_without_the_agent(state):
    seen, missing = {}, set()
    sig.backstop(["term_x"], {}, seen, missing)
    assert events(state, "term_x") == []
    sig.backstop(["term_x"], {}, seen, missing)
    sig.backstop(["term_x"], {}, seen, missing)
    [event] = events(state, "term_x")
    assert (event["kind"], event["text"]) == ("exit", "no agent in this terminal")


def test_moved_worker_reports_to_its_terminal(state, monkeypatch, capsys):
    herdr = MovedHerdr(shell_pid=sig.os.getppid())
    monkeypatch.setattr(sig, "herdr", herdr)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p11")
    stop(f"Merged.\n\n```\n{REPORT}\n```")
    [event] = events(state, "term_a")
    assert (event["kind"], event["pane"]) == ("report", "w7:p1")
    assert events(state, "w1:p11") == []

    monkeypatch.setenv("HERDR_ENV", "1")
    sig.wait(["alpha"], 0)
    out = capsys.readouterr().out
    assert out.startswith("report alpha (w7:p1) suction-T4:\n")
    assert "exit" not in out


def test_hook_skips_a_pane_that_does_not_run_it(state, monkeypatch):
    herdr = MovedHerdr(shell_pid=sig.os.getppid())
    herdr.aliases = {"w1:p11": "w1:p5"}  # The old id now names another pane.
    monkeypatch.setattr(sig, "herdr", herdr)
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p11")
    stop("Deploy now?")
    [event] = events(state, "term_a")
    assert event["pane"] == "w7:p1"


def test_wait_sees_no_exit_when_the_pane_moves(state, monkeypatch, capsys):
    monkeypatch.setenv("HERDR_ENV", "1")
    herdr = MovedHerdr(shell_pid=1)
    herdr.agents = [dict(herdr.agents[0], pane_id="w1:p11")]
    monkeypatch.setattr(sig, "herdr", herdr)
    agents = sig.herdr_agents()
    [key] = [sig.resolve("alpha", agents)]
    herdr.agents = [dict(herdr.agents[0], pane_id="w7:p1")]  # The pane moves.
    seen, missing = {}, set()
    for _ in range(3):
        sig.backstop([key], sig.herdr_agents(), seen, missing)
    assert events(state, key) == []


def test_wait_shows_context_and_usage(state, capsys):
    (state / "usage").mkdir()
    (state / "usage" / "t1_p1.json").write_text('{"five": 41.6, "week": 83.2, "ctx": 64.9}')
    stop("Deploy now?")
    sig.wait(["t1:p1"], 0)
    lines = capsys.readouterr().out.splitlines()
    assert (lines[0], lines[-1]) == ("idle ? (t1:p1), ctx 65% left:", "usage: 5h 42%, 7d 83%")


def test_project_settings_run_the_hook():
    hooks = json.loads((ROOT / ".claude/settings.json").read_text())["hooks"]
    assert set(hooks) == {"Stop", "StopFailure", "Notification", "PreToolUse"}
    for groups in hooks.values():
        [group] = groups
        [handler] = group["hooks"]
        assert handler["command"].endswith(
            'python3 "$CLAUDE_PROJECT_DIR/scripts/agent_signal.py" hook'
        )
    assert hooks["PreToolUse"][0]["matcher"] == "AskUserQuestion"
