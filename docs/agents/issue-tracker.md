# Issue tracker: GitHub

Issues and specs live in GitHub Issues for `shc0507/dobot_mg400`. Use the `gh` CLI from this repository.

## Issue operations

For a multiline body, write the exact text to a temporary file. Pass that file with `--body-file`.

- Create: `gh issue create --title "..." --body-file <path>`.
- Read: `gh issue view <number> --json number,title,body,labels,comments`.
- List: `gh issue list --state open --json number,title,body,labels,comments`. Apply `--label` and `--state` filters as needed.
- Comment: `gh issue comment <number> --body-file <path>`.
- Add a label: `gh issue edit <number> --add-label "..."`.
- Remove a label: `gh issue edit <number> --remove-label "..."`.
- Close: `gh issue close <number> --comment "..."`.

When a skill says "publish to the issue tracker", create a GitHub issue.
When a skill says "fetch the relevant ticket", read that issue.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub issues and pull requests share a number sequence. For an ambiguous reference, try `gh pr view <number>`, then `gh issue view <number>`.

## Parent issues and dependencies

Use native GitHub sub-issues to link a child issue to its parent.
Read an issue's numeric database ID with `gh api repos/{owner}/{repo}/issues/<number> --jq .id`.
The database ID differs from the issue number and `node_id`.

- Add a child: `gh api --method POST repos/{owner}/{repo}/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>`.
- Add a blocker: `gh api --method POST repos/{owner}/{repo}/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`.

If sub-issues are unavailable, add the child to a task list in the parent body.
Put `Part of #<parent>` at the top of the child body.
If native dependencies are unavailable, put `Blocked by: #<number>, #<number>` at the top of the child body.
A ticket is unblocked when every blocker is closed.

## Wayfinding operations

A map is a GitHub issue with child issues as decision tickets.

- Map: use the `wayfinder:map` label and the Notes / Decisions-so-far / Fog sections.
- Child ticket: link it to the map. Use the applicable label: `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`.
- Ready tickets: list the map's open children. Exclude assigned tickets and tickets with open blockers. Select the first remaining ticket in map order.
- Native blocker check: `gh api repos/{owner}/{repo}/issues/<number> --jq .issue_dependencies_summary.blocked_by` returns the open blocker count. For fallback references, read each blocker's state.
- Claim: run `gh issue edit <number> --add-assignee @me` as the session's first write.
- Resolve: comment with the answer, then close the ticket. Add a summary and ticket link to the map's Decisions-so-far section.
