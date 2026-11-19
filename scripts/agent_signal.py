#!/usr/bin/env python3
"""Worker-to-coordinator signals for coding agents in Herdr panes.

Agent hooks run `hook`. It writes each event as one JSON line to a file for
the Herdr terminal of the agent. The terminal id stays the same when the pane
moves; the pane id and the HERDR_PANE_ID of the agent do not. The
coordinator runs `wait` in the background.
`wait` exits at the first new events of the given workers and prints them,
with the report text or the screen lines inline.

    agent_signal.py hook [JSON]      # JSON on stdin (Claude Code and Codex hooks),
                                     # or as the last argument (Codex notify)
    agent_signal.py wait NAME|PANE|TERMINAL ... [--timeout-min 60]

Events:
    report   the final message contains the report block
    idle     the turn ended with no report and no background work
             (a question, or the worker stopped)
    blocked  a permission or question dialog waits for an answer
    error    the turn ended on an API error, for example a usage limit
    exit     no agent runs in the terminal (found by `wait`)

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


def events_file(key):
    return ROOT / "events" / f"{safe(key)}.jsonl"


def emit(key, kind, pane=None, **fields):
    """Append an event to the file of `key`, a terminal id (or a pane id as fallback)."""
    path = events_file(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "t": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": time.time(),
        "kind": kind,
        "key": key,
        "pane": pane or key,
    }
    with open(path, "a", encoding="utf-8") as out:
        out.write(json.dumps(record | fields) + "\n")


def blocked(key, message, src="hook", pane=None):
    """Write a blocked event, unless one for the same dialog came in the last 60 s."""
    path = events_file(key)
    last = path.read_text(encoding="utf-8").splitlines()[-1:] if path.exists() else []
    if last:
        previous = json.loads(last[0])
        if previous["kind"] == "blocked" and time.time() - previous.get("ts", 0) < 60:
            return
    emit(key, "blocked", pane, text=message[:300], src=src)


def ancestors():
    """Return the pids of this process and of its parents."""
    pids, pid = set(), os.getpid()
    while pid > 1 and pid not in pids:
        pids.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except Exception:
            break
    return pids


def herdr_result(*args):
    return json.loads(herdr(*args))["result"]


def own_pane(env_pane):
    """Return the Herdr pane that runs this process, or None.

    HERDR_PANE_ID keeps the id from the pane start, and a move to another
    workspace changes the pane id. Herdr still resolves the old id, and the
    pane process info confirms it: the pane shell is a parent of this process.
    """
    mine = ancestors()

    def runs_here(pane):
        info = herdr_result("pane", "process-info", "--pane", pane["pane_id"])["process_info"]
        pids = {info.get("shell_pid")} | {
            p.get("pid") for p in info.get("foreground_processes", [])
        }
        return bool(pids & mine)

    try:
        pane = herdr_result("pane", "get", env_pane)["pane"]
        if runs_here(pane):
            return pane
    except Exception:
        pass
    try:
        return next((p for p in herdr_result("pane", "list")["panes"] if runs_here(p)), None)
    except Exception:
        return None


def hook(arg):
    env_pane = os.environ.get("HERDR_PANE_ID")
    if not env_pane:
        return  # Not in a Herdr pane.
    data = json.loads(arg if arg is not None else (sys.stdin.read() or "{}"))
    found = own_pane(env_pane) or {}
    pane = found.get("pane_id") or env_pane
    key = found.get("terminal_id") or pane

    def emit_(kind, **fields):
        emit(key, kind, pane, **fields)

    def blocked_(message):
        blocked(key, message, pane=pane)

    event = data.get("hook_event_name") or data.get("type", "")
    if event in ("Stop", "agent-turn-complete"):
        text = data.get("last_assistant_message") or data.get("last-assistant-message") or ""
        block = report_block(text)
        if block:
            words = block.split()
            emit_("report", slug=words[1] if len(words) > 1 else "?", text=block)
        elif not data.get("background_tasks"):
            emit_("idle", text="\n".join(text.strip().splitlines()[-6:]))
    elif event == "Notification" and data.get("notification_type") in DIALOGS:
        blocked_(data.get("message", ""))
    elif event == "PermissionRequest":
        blocked_(f"{data.get('tool_name')}: {json.dumps(data.get('tool_input'))[:200]}")
    elif event == "PreToolUse" and data.get("tool_name") == "AskUserQuestion":
        questions = (data.get("tool_input") or {}).get("questions") or []
        blocked_("question: " + " | ".join(q.get("question", "") for q in questions))
    elif event == "StopFailure":
        emit_("error", text=f"{data.get('error')} {data.get('error_details') or ''}"[:300])


def herdr(*args):
    return subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=5).stdout


def herdr_agents():
    """Return the live Herdr agents by terminal id, or None when Herdr does not answer."""
    try:
        agents = json.loads(herdr("agent", "list"))["result"]["agents"]
        return {a.get("terminal_id") or a["pane_id"]: a for a in agents}
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


def resolve(target, agents):
    """Return the terminal id for an agent name, a pane id or a terminal id."""
    for key, agent in agents.items():
        if target in (key, agent.get("name"), agent.get("pane_id")):
            return key
    return target


def backstop(keys, agents, seen, missing):
    """Add the events that hooks cannot see: Herdr's blocked state and exits."""
    for key in keys:
        agent = agents.get(key)
        if agent is None:
            # Two checks in a row, so that a pane move or a short Herdr gap is no exit.
            if key in missing and seen.get(key) != "exit":
                seen[key] = "exit"
                emit(key, "exit", text="no agent in this terminal", src="herdr")
            missing.add(key)
            continue
        missing.discard(key)
        if seen.get(key) == "exit":
            del seen[key]
        if agent.get("agent_status") == "blocked" and seen.get(key) != agent.get(
            "state_change_seq"
        ):
            seen[key] = agent.get("state_change_seq")
            blocked(key, "dialog on screen (Herdr)", src="herdr", pane=agent.get("pane_id"))


def wait(targets, timeout_min):
    in_herdr = os.environ.get("HERDR_ENV") == "1"
    agents = (herdr_agents() if in_herdr else None) or {}
    keys = [resolve(t, agents) for t in targets]
    cursors = ROOT / "cursor"
    cursors.mkdir(parents=True, exist_ok=True)
    seen_file = ROOT / "herdr-seen.json"
    seen = json.loads(seen_file.read_text()) if seen_file.exists() else {}
    deadline = time.time() + timeout_min * 60
    checked, missing = 0.0, set()
    while True:
        if agents and time.time() - checked > 5:
            checked = time.time()
            agents = herdr_agents() or agents
            backstop(keys, agents, seen, missing)
            seen_file.write_text(json.dumps(seen))
        # The terminal file, and the pane file of a hook that could not find its terminal.
        files = dict.fromkeys(keys)
        files |= dict.fromkeys(a["pane_id"] for k, a in agents.items() if k in keys)
        new, ends = [], {}
        for key in files:
            cursor = cursors / safe(key)
            done = int(cursor.read_text() or 0) if cursor.exists() else 0
            path = events_file(key)
            lines = path.read_text(encoding="utf-8").split("\n")[:-1] if path.exists() else []
            new += [json.loads(line) for line in lines[done:]]
            ends[cursor] = len(lines)
        if new or time.time() >= deadline:
            for event in sorted(new, key=lambda e: e.get("ts", 0)):
                show(event, agents, in_herdr)
            if not new:
                print(f"tick: no events in {timeout_min:g} min")
            five, week = usage().get("five"), usage().get("week")
            if isinstance(five, (int, float)) and isinstance(week, (int, float)):
                print(f"usage: 5h {five:.0f}%, 7d {week:.0f}%")
            for cursor, end in ends.items():
                cursor.write_text(str(end))
            return
        time.sleep(1)


def show(event, agents, in_herdr):
    key = event.get("key") or event["pane"]
    agent = agents.get(key) or next(
        (a for a in agents.values() if a["pane_id"] == event["pane"]), {}
    )
    pane = agent.get("pane_id") or event["pane"]
    head = f"{event['kind']} {agent.get('name', '?')} ({pane})"
    if event.get("slug"):
        head += f" {event['slug']}"
    ctx = (usage(key) or usage(pane)).get("ctx")
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
