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
  - Camera stills are at `/camera/<view>.jpg` on the same host. For a page behind the Olares login, put a request under `Bench`.
- Docs: change instruction docs in place as `AGENTS.md` "Documenting" says. Chinese and English pairs change together.
- Commit message: one line plus a brief description of what was done.
- Permission block: when the classifier blocks a command, write `BLOCKED <name>: <command>` in the report and continue with the rest of the task. A permission dialog goes to the coordinator through the hook, and the coordinator answers it.
- Routine question (merge, CI, deploy order, what next): decide by these rules. If a rule is missing, end your turn with the question as your last line, when no background work runs. The Stop hook sends it to the coordinator.
- Bench question (watch motion, focus zero, cables, a safety call, or a look at a page in the owner's browser): batch everything into one question. If you can finish without the answer, put it under `Bench` in the report. If not, end your turn with `Bench: <question>` as the last line. The coordinator asks the owner and sends you the answer.
- Never ask the owner, and do not use AskUserQuestion. The owner talks only to the coordinator.

## Report

End your final message with this block. A Stop hook copies it to the coordinator. The block means that the task is done. Write it only when the task is complete, after CI, merge and deploy if the task has them. While CI or other background work runs, end your turn without it. Nothing else after it: no diffs, no logs. `Images:` lists the images for the owner, and the coordinator sends them.

```
Task: <#N or slug> <title>
Branch/PR: claude/<slug> / #M (merged | open | none)
Changed: <areas or files, one line>
Tests: <command> -> pass | fail (<count>)
CI: green | red | not run
Deployed: no | yes (<services restarted>)
Bench: none | <one question>
Images: none | <path> [<path> ...]
BLOCKED: none | <name>: <command>
Notes: <at most 2 lines: a limitation or a follow-up>
```
