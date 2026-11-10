"""Print the pytest targets for a set of changed files.

Pull request CI runs only the tests of the changed areas. Read changed paths
from the arguments or, without arguments, one per line from stdin. Print one
test file per line, or `tests` when the full suite must run.
"""

import sys
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FULL = "tests"
ALWAYS = ["test_doc_pairs.py", "test_repo_files.py"]

ROBOT = [
    "test_joint_*.py",
    "test_controller_*.py",
    "test_feedback.py",
    "test_keyboard_*.py",
    "test_web_*.py",
    "test_websocket.py",
    "test_control_panel*.py",
    "test_robot_*.py",
    "test_terminal_control.py",
    "test_control_mode.py",
    "test_mg400_sim.py",
    "test_suction.py",
    "test_console*.py",
    "test_orchestrator_clients.py",
    "test_orchestrator_rehearsal.py",
]
ORCHESTRATOR = [
    "test_orchestrator_*.py",
    "test_recipe_*.py",
    "test_deploy_mg400ctl.py",
    "test_console*.py",
    "test_dashboard*.py",
    "test_gateway*.py",
    "test_approvals.py",
]
MICROSCOPE = ["test_microscope_*.py"]
DEPLOY = ["test_deploy_*.py", "test_systemd_units.py", "test_udev_rules.py", "test_console.py"]

# Changed path pattern -> test file patterns in tests/. The first match wins.
# Mirror the area table in AGENTS.md, plus the packages that import each area.
AREAS = [
    # Shared code and CI configuration: run everything.
    ("src/labkit/*", [FULL]),
    ("tests/fixtures/*", [FULL]),
    ("tests/*.py", None),  # Shared helpers; test files are handled before this table.
    ("scripts/select_tests.py", [FULL]),
    # Areas.
    ("src/mg400/*", ROBOT),
    ("apps/control_panel/*", ROBOT),
    ("scripts/mg400_*.py", ROBOT),
    ("config/robot.toml", ROBOT),
    ("examples/*", ["test_joint_example.py"]),
    ("src/orchestrator/web/*", ["test_console*.py", "test_dashboard*.py"]),
    ("scripts/console_font.py", ["test_console.py"]),
    ("src/orchestrator/*", ORCHESTRATOR),
    ("recipes/*", ORCHESTRATOR),
    ("deploy/mg400ctl", ORCHESTRATOR),
    ("src/gateway/*", ["test_gateway*.py", "test_approvals.py"]),
    ("src/microscope/*", MICROSCOPE),
    ("firmware/microscope-axes/*", MICROSCOPE),
    ("src/toupcam_camera/*", ["test_toupcam_camera.py", *MICROSCOPE]),
    (
        "src/cameras/*",
        [
            "test_deploy_install.py",
            "test_camera_preview.py",
            "test_console_cameras.py",
            "test_console_twin.py",
            "test_stage_twin.py",
            "test_vision_calibration.py",
            "test_vision_drift.py",
            "test_vision_service.py",
            "test_orchestrator_rehearsal.py",
            *MICROSCOPE,
        ],
    ),
    ("scripts/camera_preview.py", ["test_camera_preview.py"]),
    (
        "src/furnace/*",
        [
            "test_furnace_*.py",
            "test_microscope_service.py",
            "test_microscope_panel.py",
            "test_orchestrator_recording.py",
            "test_orchestrator_rehearsal.py",
        ],
    ),
    ("src/stage_controller/*", ["test_stage_*.py", "test_orchestrator_rehearsal.py"]),
    ("src/thermal_cam/*", ["test_thermal_*.py", "test_orchestrator_rehearsal.py"]),
    ("src/vision/stage_twin/*", ["test_stage_twin.py", "test_console_twin.py"]),
    (
        "src/vision/*",
        [
            "test_vision_*.py",
            "test_stage_twin.py",
            "test_console_twin.py",
            "test_orchestrator_rehearsal.py",
        ],
    ),
    ("config/trays/*", ["test_vision_tray.py"]),
    ("scripts/vision_eval.py", ["test_vision_eval.py"]),
    ("scripts/console_shots.py", ["test_console_shots.py"]),
    ("scripts/agent_signal.py", ["test_agent_signal.py"]),
    (".claude/settings.json", ["test_agent_signal.py"]),
    ("deploy/*", DEPLOY),
    ("config/*", DEPLOY),
    # Documents and agent files: the doc-pair checker in ALWAYS covers them.
    ("scripts/check_doc_pairs.py", []),
    ("*.md", []),
    ("docs/*", []),
    (".agents/*", []),
    ("skills-lock.json", []),
    ("LICENSE", []),
    (".gitignore", []),
    # The JavaScript tests always run in CI.
    ("tests/*.mjs", []),
]


def select(changed, root=ROOT):
    """Return the pytest targets for the changed paths."""
    tests = root / "tests"
    patterns = set(ALWAYS)
    for path in changed:
        if fnmatch(path, "tests/test_*.py"):
            patterns.add(Path(path).name)
            continue
        for pattern, targets in AREAS:
            if fnmatch(path, pattern):
                break
        else:
            targets = None
        if targets is None or FULL in targets:
            return [FULL]
        patterns.update(targets)
    files = {f for p in patterns for f in tests.glob(p)}
    return sorted(f.relative_to(root).as_posix() for f in files)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    changed = argv or [line.strip() for line in sys.stdin if line.strip()]
    print("\n".join(select(changed)))


if __name__ == "__main__":
    main()
