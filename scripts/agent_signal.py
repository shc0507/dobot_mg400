#!/usr/bin/env python3
"""Worker-to-coordinator signals for coding agents in Herdr panes.

Agent hooks run `hook`. It writes each event as one JSON line to a file for
the Herdr pane of the agent. The coordinator runs `wait` in the background.
`wait` exits at the first new events of the given workers and prints them,
with the report text or the screen lines inline.

    agent_signal.py hook [JSON]      # JSON on stdin (Claude Code and Codex hooks),
                                     # or as the last argument (Codex notify)
    agent_signal.py wait NAME|PANE ... [--timeout-min 60]

Events:
    report   the final message contains the report block
    idle     the turn ended with no report and no background work
             (a question, or the worker stopped)
    blocked  a permission or question dialog waits for an answer
    error    the turn ended on an API error, for example a usage limit
    exit     the pane has no agent (found by `wait`)

A turn that ends while background work runs (a CI watch, a Monitor) makes no
event. The agent starts a new turn when that work completes.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("AGENT_SIGNAL_DIR") or Path.home() / ".local/state/agents")
DIALOGS = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog"}
SCREEN_LINES = 20


def safe(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:64] or "_"


def report_block(text):
    """Return the last report block in a message, or None."""
    lines = [line.strip() for line in text.splitlines()]
    starts = [
        i
        for i, line in enumerate(lines)
        if line.startswith("Task:")
        and any(x.startswith("Branch/PR:") for x in lines[i + 1 : i + 3])
    ]
    if not starts:
        return None
    block = []
    for line in lines[starts[-1] :]:
        if line.startswith("```"):
            break
        block.append(line)
    return "\n".join(block).strip()


def events_file(pane):
    return ROOT / "events" / f"{safe(pane)}.jsonl"


def emit(pane, kind, **fields):
    path = events_file(pane)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": time.time(),
        "kind": kind,
        "pane": pane,
    }
    with open(path, "a", encoding="utf-8") as out:
        out.write(json.dumps(record | fields) + "\n")


def blocked(pane, message, src="hook"):
    """Write a blocked event, unless one for the same dialog came in the last 60 s."""
    path = events_file(pane)
    last = path.read_text(encoding="utf-8").splitlines()[-1:] if path.exists() else []
    if last:
        previous = json.loads(last[0])
        if previous["kind"] == "blocked" and time.time() - previous.get("ts", 0) < 60:
            return
    emit(pane, "blocked", text=message[:300], src=src)


def hook(arg):
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return  # Not in a Herdr pane.
    data = json.loads(arg if arg is not None else (sys.stdin.read() or "{}"))
    event = data.get("hook_event_name") or data.get("type", "")
    if event in ("Stop", "agent-turn-complete"):
        text = data.get("last_assistant_message") or data.get("last-assistant-message") or ""
        block = report_block(text)
        if block:
            words = block.split()
            emit(pane, "report", slug=words[1] if len(words) > 1 else "?", text=block)
        elif not data.get("background_tasks"):
            emit(pane, "idle", text="\n".join(text.strip().splitlines()[-6:]))
    elif event == "Notification" and data.get("notification_type") in DIALOGS:
        blocked(pane, data.get("message", ""))
    elif event == "PermissionRequest":
        blocked(pane, f"{data.get('tool_name')}: {json.dumps(data.get('tool_input'))[:200]}")
    elif event == "PreToolUse" and data.get("tool_name") == "AskUserQuestion":
        questions = (data.get("tool_input") or {}).get("questions") or []
        blocked(pane, "question: " + " | ".join(q.get("question", "") for q in questions))
    elif event == "StopFailure":
        emit(pane, "error", text=f"{data.get('error')} {data.get('error_details') or ''}"[:300])


def herdr(*args):
    return subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=5).stdout


def herdr_agents():
    """Return the live Herdr agents by pane, or None when Herdr does not answer."""
    try:
        return {a["pane_id"]: a for a in json.loads(herdr("agent", "list"))["result"]["agents"]}
    except Exception:
        return None


def screen(pane):
    """Return the last non-empty lines of the visible pane."""
    try:
        lines = [
            line
            for line in herdr("pane", "read", pane, "--source", "visible").splitlines()
            if line.strip()
        ]
    except Exception:
        return []
    return lines[-SCREEN_LINES:]


def backstop(panes, agents, seen):
    """Add the events that hooks cannot see: Herdr's blocked state and exits."""
    for pane in panes:
        agent = agents.get(pane)
        if agent is None:
            if seen.get(pane) != "exit":
                seen[pane] = "exit"
                emit(pane, "exit", text="no agent in this pane", src="herdr")
        elif agent.get("agent_status") == "blocked" and seen.get(pane) != agent.get(
            "state_change_seq"
        ):
            seen[pane] = agent.get("state_change_seq")
            blocked(pane, "dialog on screen (Herdr)", src="herdr")


def wait(targets, timeout_min):
    in_herdr = os.environ.get("HERDR_ENV") == "1"
    agents = (herdr_agents() if in_herdr else None) or {}
    by_name = {a["name"]: pane for pane, a in agents.items() if a.get("name")}
    panes = [by_name.get(t, t) for t in targets]
    names = {pane: name for name, pane in by_name.items()}
    cursors = ROOT / "cursor"
    cursors.mkdir(parents=True, exist_ok=True)
    seen_file = ROOT / "herdr-seen.json"
    seen = json.loads(seen_file.read_text()) if seen_file.exists() else {}
    deadline = time.time() + timeout_min * 60
    checked = 0.0
    while True:
        if agents and time.time() - checked > 5:
            checked = time.time()
            agents = herdr_agents() or agents
            backstop(panes, agents, seen)
            seen_file.write_text(json.dumps(seen))
        new, ends = [], {}
        for pane in panes:
            cursor = cursors / safe(pane)
            done = int(cursor.read_text() or 0) if cursor.exists() else 0
            path = events_file(pane)
            lines = path.read_text(encoding="utf-8").split("\n")[:-1] if path.exists() else []
            new += [json.loads(line) for line in lines[done:]]
            ends[cursor] = len(lines)
        if new or time.time() >= deadline:
            for event in new:
                show(event, names, in_herdr)
            if not new:
                print(f"tick: no events in {timeout_min:g} min")
            five, week = usage().get("five"), usage().get("week")
            if isinstance(five, (int, float)) and isinstance(week, (int, float)):
                print(f"usage: 5h {five:.0f}%, 7d {week:.0f}%")
            for cursor, end in ends.items():
                cursor.write_text(str(end))
            return
        time.sleep(1)


def show(event, names, in_herdr):
    pane = event["pane"]
    head = f"{event['kind']} {names.get(pane, '?')} ({pane})"
    if event.get("slug"):
        head += f" {event['slug']}"
    ctx = usage(pane).get("ctx")
    if isinstance(ctx, (int, float)):
        head += f", ctx {ctx:.0f}% left"
    print(head + ":")
    for line in event.get("text", "").splitlines():
        print("  " + line)
    if in_herdr and event["kind"] in ("blocked", "exit"):
        print("  screen:")
        for line in screen(pane):
            print("    " + line)


def usage(pane=None):
    """Return what the status line wrote: {"five": %, "week": %, "ctx": % left}."""
    folder = ROOT / "usage"
    files = (
        sorted(folder.glob("*.json"), key=lambda f: f.stat().st_mtime) if folder.exists() else []
    )
    if pane is not None:
        files = [f for f in files if f.stem == safe(pane)]
    try:
        return json.loads(files[-1].read_text()) if files else {}
    except Exception:
        return {}


def main(argv):
    if argv[:1] == ["hook"]:
        try:
            hook(argv[1] if len(argv) > 1 else None)
        except Exception:
            pass  # A hook must never stop the agent.
        return 0
    if argv[:1] == ["wait"] and len(argv) > 1:
        timeout = 60.0
        if "--timeout-min" in argv:
            i = argv.index("--timeout-min")
            timeout = float(argv[i + 1])
            argv = argv[:i] + argv[i + 2 :]
        wait(argv[1:], timeout)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
