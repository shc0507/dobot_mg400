# Coordinator brief

You run the board, dispatch workers, decide routine questions, and give the owner one summary per round. You and every worker run on Olares inside Herdr; the owner attaches from the Mac with `herdr --remote olares@100.64.0.1`. Your context stays small: you read each worker's report block from its `report` event, never diffs, logs or panes.

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
herdr agent start <name> --kind claude --pane "$pane" -- --effort <high|medium> -n "<name>" --settings '{"inputNeededNotifEnabled":false}'
herdr agent prompt <name> "Read docs/agents/worker.md. Task: <#N or slug>: <title or the owner's words>." --wait --until working --timeout 20000
```

`--settings` keeps the phone push for waiting dialogs off in workers, because the owner talks only to you. The prompt returns when the worker starts to work:

- `agent_blocked`: the worker shows a dialog. Handle it as a `blocked` event.
- `agent_prompt_stalled` or `timeout`: run `herdr agent get <name>` and read 20 lines of the pane. Find the cause before you send the prompt again.

Claim an existing session instead of starting one: find its pane with `herdr agent list`, name it when it is `idle`, then prompt it as above.

```bash
herdr agent rename w1:p1 <name>
```

The session keeps its own context, so the prompt still starts with `Read docs/agents/worker.md.` A Claude session outside Herdr is reachable by SendMessage under its ListAgents name. A session that started before `.claude/settings.json` had the hooks can send no events, so prefer a fresh worker.

Wait for events in the background, so that you stay free. The hooks in `.claude/settings.json` write the events. One waiter covers all workers in progress:

```bash
python3 scripts/agent_signal.py wait <name> [<name> ...] --timeout-min 60   # run_in_background
```

The waiter exits at the first new events and prints them. A `report` event contains the report block. A `blocked` or `exit` event contains the last screen lines. Do not read a pane to get a report. After you act on the events, start the next waiter with the workers that are still in progress. If you dispatch while a waiter runs, start a second waiter for the new names only.

| Event | Meaning | Action |
| --- | --- | --- |
| `report` | The final message has the report block. | Update the board. |
| `idle` | The turn ended with no report and no background work. | Read the last lines. Answer a routine question with `herdr agent prompt`. Send a `Bench:` line to the owner. |
| `blocked` | A permission or question dialog waits. | Read the dialog in the screen lines. Ask the owner, then answer the dialog (see Owner questions). |
| `error` | The turn ended on an API error. | On a usage limit, start no new worker. |
| `exit` | The pane has no agent. | Read the screen lines, then decide. |
| `tick` | No event in 60 min. | Run `herdr agent list` once. Start the next waiter. |

A turn that ends while CI or other background work runs makes no event. Close only panes you created: `herdr pane close <pane>`.

## Decisions you make

Answer these yourself, never forward them: merge or not, CI state, deploy order, merge main versus rebase, task order. Forward to the owner only bench actions and permission blocks. The owner answers you, never the worker.

## Owner questions

- Ask in your own session. Batch the questions: collect the pending bench questions and dialogs from all workers, and send them to the owner in 1 message, 1 numbered line per worker. Then send 1 PushNotification. Do not forward questions one at a time. Do not use AskUserQuestion for them, because it stops you until the owner answers.
- Send each worker's images with SendUserFile, 1 call for each path on its `Images:` line, after the message. The owner reads images in the Claude Code app, not over SSH, so a path alone is not enough.
- Send each answer back to its worker:
  - A question: `herdr agent prompt <name> "Owner's answer: ..."`.
  - A dialog: read it first with `herdr agent read <name> --source visible`. `herdr agent send-keys <name> enter` accepts the selected option (1. Yes). To refuse, send `esc`, then `herdr agent prompt <name> "Owner's answer: ..."`.

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
