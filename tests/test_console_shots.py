import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("console_shots", ROOT / "scripts/console_shots.py")
shots = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shots)


def test_defaults_cover_every_page_language_and_viewport(tmp_path):
    args = shots.build_parser().parse_args(["shoot", "--out", str(tmp_path)])
    plan = shots.plan_shots(args.pages, args.langs, args.viewports, args.state, tmp_path)
    assert args.url == "http://100.64.0.1:8080/lab/"
    assert len(plan) == 4 * 2 * 2
    names = {Path(item["out"]).name for item in plan}
    assert {
        "overview-en-1440x900.png",
        "process-zh-1280x800.png",
        "cameras-zh-1440x900.png",
    } <= names
    assert {item["hash"] for item in plan} == {"overview", "motion", "cameras", "process"}


def test_a_state_adds_one_shot_with_its_script(tmp_path):
    script = tmp_path / "held.js"
    script.write_text("document.body.dataset.x = 1", encoding="utf-8")
    args = shots.build_parser().parse_args(
        [
            "shoot",
            "--out",
            str(tmp_path),
            "--pages",
            "motion",
            "--langs",
            "en",
            "--viewports",
            "1280x800",
            "--state",
            f"held={script}",
        ]
    )
    plan = shots.plan_shots(
        args.pages, args.langs, args.viewports, args.state, tmp_path, "#ws-motion"
    )
    assert [Path(item["out"]).name for item in plan] == [
        "motion-en-1280x800.png",
        "motion-en-1280x800-held.png",
    ]
    assert plan[0]["script"] is None and plan[1]["script"] == "document.body.dataset.x = 1"
    assert plan[1]["clip"] == "#ws-motion"


@pytest.mark.parametrize("argv", [["--pages", "chip"], ["--langs", "fr"], ["--viewports", "1440"]])
def test_bad_choices_are_refused(tmp_path, argv):
    with pytest.raises(SystemExit):
        shots.build_parser().parse_args(["shoot", "--out", str(tmp_path), *argv])


def test_composites_pair_names_found_on_both_sides(tmp_path):
    before, after = tmp_path / "before", tmp_path / "after"
    for side, names in ((before, ["a.png", "b.png"]), (after, ["b.png", "c.png"])):
        side.mkdir()
        for name in names:
            (side / name).write_bytes(b"")
    plan = shots.plan_composites(before, after, tmp_path)
    assert plan == [
        {
            "top": str(before / "b.png"),
            "bottom": str(after / "b.png"),
            "out": str(tmp_path / "cmp-b.png"),
        }
    ]


def test_the_browser_sends_only_get_requests():
    assert 'method() === "GET" ? route.continue() : route.abort()' in shots.DRIVER
    assert "window.WebSocket = undefined" in shots.DRIVER
