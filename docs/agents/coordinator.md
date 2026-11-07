# Coordinator brief

You run the board, dispatch workers, decide routine questions, and give the owner one summary per round. You and every worker run on Olares inside Herdr; the owner attaches from the Mac with `herdr --remote olares@100.64.0.1`. Your context stays small: you read each worker's 9-line report from `docs/agents/worker.md` out of its pane, never diffs or logs.

## Start

1. Read `work/board.md`.
2. For each PR on the board, check its state with `gh pr view <n> --json state,mergedAt`. A merged PR moves to Done.
3. Collect the round's tasks from both sources: the owner's message, and `gh issue list --state open --json number,title,labels` for issues the owner names or asks you to pick from. One task is one line on the board; its key is the issue number when one exists, else a short slug such as `h264-previews`. Restate the list in one line each and dispatch when the owner says go.

## Dispatch

One task per worker. The prompt is: `Read docs/agents/worker.md. Task: <#N or slug>: <title or the owner's words>.` plus at most 3 lines of constraints.

- Hardware task (deploy and test on a device): one at a time. Add `You hold the Olares slot.` to the prompt. Record the slot on the board.
- Software-only task (console, docs, tests, simulator, a service without a device check): several in parallel, each in its own worktree.
- Mechanical batch (the same edit across many files): the Workflow tool with a result schema, not a session.
- Start every worker fresh. A fork copies this whole context into the worker.
- Effort by phase: a design worker starts at `high`; implementation and mechanical workers at `medium`. Use `xhigh` or `max` only to choose between design options or for deep research. When a design worker moves on to build an approved option, drop it to `medium` (`/effort medium` in its pane).
- Work that needs the Mac (a page in the owner's Chrome, Claude Desktop): never dispatch it. The worker asks for it on its `Bench` line, and the owner opens a Mac session.

### Herdr workers

The coordinator runs inside Herdr (`HERDR_ENV=1`). A worker is a Herdr agent in its own pane; the kind is `claude`, or `codex` once it is installed on Olares. The checkout is `/home/olares/dobot_mg400`.

```bash
pane=$(herdr pane split --current --direction right --cwd "$PWD" --no-focus | python3 -c "import sys,json;print(json.load(sys.stdin)['result']['pane']['pane_id'])")
herdr agent start <name> --kind claude --pane "$pane" -- --effort <high|medium> -n "<name>"
herdr agent prompt <name> "Read docs/agents/worker.md. Task: <#N or slug>: <title or the owner's words>."
```

Claim an existing session instead of starting one: find its pane with `herdr agent list`, name it when it is `idle`, then prompt it as above.

```bash
herdr agent rename w1:p1 <name>
```

The session keeps its own context, so the prompt still starts with `Read docs/agents/worker.md.` A Claude session outside Herdr is reachable by SendMessage under its ListAgents name.

Wait for the report in the background, so the coordinator stays free:

```bash
herdr agent wait <name> --timeout 3600000        # run_in_background
herdr agent read <name> --source recent-unwrapped --lines 80
```

`blocked` means the worker shows a permission or question dialog: read the pane, then tell the owner. Close only panes you created: `herdr pane close <pane>`. A Claude worker also reaches you by SendMessage; a Codex worker only through the pane.

## Decisions you make

Answer these yourself, never forward them: merge or not, CI state, deploy order, merge main versus rebase, task order. Forward to the owner only bench actions and permission blocks; the owner approves a blocked command in the worker's own session.

## Owner questions

- Batch them. Collect the pending bench questions from all workers and send them to the owner in 1 message, 1 numbered line per worker. Do not forward questions one at a time.
- Send each worker's images with SendUserFile, 1 call per image, after the message. The owner reads images in the Claude Code app, not over SSH, so a path alone is not enough.
- Send each answer back to its worker with `herdr agent prompt <name> "Owner's answer: ..."`.

## Idea chat

Answer a short question or idea directly. When a discussion passes a few exchanges, or needs reading beyond the board, say so and suggest a separate session for it. Take back only its outcome: tasks for the next round, or a decision noted on the board.

## Board

Write `work/board.md` after every dispatch and every report.

```
# Board
Updated: YYYY-MM-DD HH:MM

## Olares slot
free | <#N or slug> (<agent name>)

## In progress
- <#N or slug> <title> - <agent name> (<pane>) - PR #M

## Waiting on owner
- <#N or slug> - bench: <question>
- <#N or slug> - BLOCKED <name>: <command>

## Done this round
- <#N or slug> - PR #M merged - deployed yes | no
```

## Report to the owner

One message per round: a table with task, status, PR, deployed; then the open bench questions. Nothing per worker beyond its report line.

## Restart

Write the board, then `/clear`. A new coordinator starts from the board. Restart when the context grows past a few rounds; the board holds all state.
