"""The migration task graph and the task cards agree, and the graph has no cycles."""

import re
import tomllib
from pathlib import Path

PLAN = Path(__file__).resolve().parents[1] / "docs/agents/migration"
OWNERS = {"agent", "human", "agent+human"}
SIZES = {"S", "M", "L"}
REVIEWS = {"agent", "human"}
HEADING = re.compile(r"^### ([A-Z]{1,3}-\d{2}) · (.+)$")


def tasks():
    with (PLAN / "tasks.toml").open("rb") as stream:
        return tomllib.load(stream)["task"]


def cards():
    """Return {id: (title, first non-empty line after the heading)}."""
    lines = (PLAN / "tasks.md").read_text(encoding="utf-8").splitlines()
    found = {}
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if match:
            following = next((text for text in lines[index + 1 :] if text.strip()), "")
            assert match.group(1) not in found, f"Two cards for {match.group(1)}"
            found[match.group(1)] = (match.group(2), following)
    return found


def test_tasks_have_valid_fields():
    ids = [task["id"] for task in tasks()]
    assert len(ids) == len(set(ids))
    for task in tasks():
        assert re.fullmatch(r"[A-Z]{1,3}-\d{2}", task["id"]), task["id"]
        assert task["owner"] in OWNERS, task["id"]
        assert task["size"] in SIZES, task["id"]
        assert task["review"] in REVIEWS, task["id"]
        assert task["phase"] in range(7), task["id"]
        assert task["title"].strip() and task["track"].strip(), task["id"]
        for pattern in task["writes"]:
            assert not pattern.startswith("/") and ".." not in pattern, (task["id"], pattern)
        if task["owner"] == "human":
            assert task["writes"] == [], task["id"]


def test_dependencies_exist_and_form_no_cycle():
    graph = {task["id"]: task["deps"] for task in tasks()}
    for name, deps in graph.items():
        assert name not in deps, name
        assert set(deps) <= set(graph), (name, set(deps) - set(graph))
    remaining = dict(graph)
    while remaining:
        ready = [name for name, deps in remaining.items() if not set(deps) & set(remaining)]
        assert ready, f"Dependency cycle among {sorted(remaining)}"
        for name in ready:
            del remaining[name]


def test_every_task_has_one_matching_card():
    found = cards()
    assert set(found) == {task["id"] for task in tasks()}
    for task in tasks():
        title, meta = found[task["id"]]
        assert title == task["title"], task["id"]
        deps = ", ".join(task["deps"]) or "none"
        expected = (
            f"Owner: {task['owner']} · Size: {task['size']} · "
            f"Review: {task['review']} · Deps: {deps}"
        )
        assert meta == expected, task["id"]
