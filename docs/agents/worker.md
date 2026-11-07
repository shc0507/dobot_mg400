# Worker brief

You are one worker. The coordinator dispatched you with one task: an issue number or a slug, with the title or the owner's words. Decide every routine question with the rules below. Send only bench questions outward.

## Setup

- You run on Olares. Work in your own worktree, so workers never share a checkout and `/home/olares/dobot_mg400` stays on `main`: `git worktree add ~/worktrees/<slug> -b claude/<slug> origin/main`, then `cd` there. Remove it after the PR merges: `git worktree remove ~/worktrees/<slug>`, then `git branch -d claude/<slug>`.
- Hardware tasks (deploy and test on a device) hold the Olares slot; the coordinator assigns the slot.
- Find the area in the table in `AGENTS.md` ("Reading the codebase"). Read only that area's code, tests and doc.

## Rules

- Tests: run the area's tests locally. CI runs the full suite on push.
- Before push: merge `origin/main` into the branch. Force-push is blocked, so merge instead of rebasing.
- PR: `gh pr create`; body states what changed, why, tests run, and the device check if any.
- CI: right after push, start `gh pr checks <n> --watch --interval 20` in the background (`run_in_background`) and do the docs, screenshots and cleanup while it runs. Merge yourself when CI is green and the device check passed: `gh pr merge <n> --merge`.
- Deploy: follow `docs/deploy.md`. Keep `web` running while the owner may use the arm. Poll readiness with an until-loop on `curl` or `systemctl`, never a fixed sleep.
- Look at the console yourself with `scripts/console_shots.py` (one call for pages, EN and 中文, 2 viewports; it sends only GET requests). Read only the PNGs you need.
  - Live: `uv run python scripts/console_shots.py shoot --url http://100.64.0.1:8080/lab/ --pages motion --out <dir>`. `LAB_CONSOLE_URL` can replace `--url`.
  - Your branch: add `--demo`; the script starts and stops a local demo server.
  - Before/after (before on top): `compare http://100.64.0.1:8080/lab/ --demo --out <dir>`, or `compare <before-dir> <after-dir> --out <dir>`.
  - A state (held key, open dialog): `--state NAME=FILE.js`, a script that changes the page after load. `--clip <selector>` captures 1 element.
  - Camera stills are at `/camera/<view>.jpg` on the same host. Ask the owner only for pages behind the Olares login.
- Docs: change instruction docs in place as `AGENTS.md` "Documenting" says. Chinese and English pairs change together.
- Commit message: one line plus a brief description of what was done.
- Permission block: when the classifier blocks a command, write `BLOCKED <name>: <command>` in the report and continue with the rest of the task. A worker in an interactive session asks the owner once, with one question that covers the whole path (merge main, push, merge).
- Routine question (merge, CI, deploy order, what next): decide by these rules. If a rule is missing, end your turn with the question as your last line; the coordinator reads your pane. Never ask the owner.
- Bench question (watch motion, focus zero, cables, a safety call, or a look at a page in the owner's browser): batch everything into one question. Ask the owner once in your own pane, and put the question under `Bench` in the report.

## Report

End your final message with this block. The coordinator reads it from your pane. Nothing else after it: no diffs, no logs.

```
Task: <#N or slug> <title>
Branch/PR: claude/<slug> / #M (merged | open | none)
Changed: <areas or files, one line>
Tests: <command> -> pass | fail (<count>)
CI: green | red | not run
Deployed: no | yes (<services restarted>)
Bench: none | <one question>
BLOCKED: none | <name>: <command>
Notes: <at most 2 lines: a limitation or a follow-up>
```
