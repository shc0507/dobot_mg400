"""Print the pytest targets for a set of changed files.

Pull request CI runs only the tests of the changed areas. Read changed paths
from the arguments or, without arguments, one per line from stdin. Print one
test file per line, or `tests` when the full suite must run.
"""

import ast
import re
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
    "test_control_mode.py",
    "test_mg400_sim.py",
    "test_suction.py",
    "test_pressure.py",
    "test_console*.py",
    "test_orchestrator_clients.py",
    "test_orchestrator_rehearsal.py",
]
ORCHESTRATOR = [
    "test_orchestrator_*.py",
    "test_agent_cli*.py",
    "test_recipe_*.py",
    "test_deploy_labctl.py",
    "test_console*.py",
    "test_dashboard*.py",
    "test_transfer_profiles.py",
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
    # A push to main runs the full suite, so a selection error shows there.
    ("scripts/select_tests.py", ["test_select_tests.py"]),
    # Areas.
    ("src/mg400/*", ROBOT),
    ("apps/control_panel/*", ROBOT),
    ("config/robot.toml", ROBOT),
    ("examples/*", ["test_joint_example.py"]),
    (
        "src/orchestrator/transfer_profiles.py",
        ["test_transfer_profiles.py", "test_console_sample_transfer.py", "test_console_preview.py"],
    ),
    (
        "src/orchestrator/transfer_motion.py",
        ["test_console_sample_transfer.py", "test_console_preview.py"],
    ),
    ("src/orchestrator/web/*", ["test_console*.py", "test_dashboard*.py", "test_agent_cli*.py"]),
    ("src/orchestrator/agent*.py", ["test_agent_cli*.py", "test_orchestrator_cli.py"]),
    ("scripts/console_font.py", ["test_console.py"]),
    (
        "scripts/console_preview.py",
        [
            "test_console_preview.py",
            "test_console_twin.py",
            "test_console_position_finder.py",
            "test_console_turret.py",
        ],
    ),
    ("src/orchestrator/*", ORCHESTRATOR),
    ("recipes/*", ORCHESTRATOR),
    (
        "src/microscope/turret_calibration.py",
        [*MICROSCOPE, "test_console_preview.py", "test_console_turret.py"],
    ),
    ("deploy/labctl", ORCHESTRATOR),
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
            "test_console_position_finder.py",
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
    # Host tests cannot detect firmware bugs; the stage-firmware CI job runs the native checks.
    ("firmware/stage-controller-v3/*", []),
    # Host tests cannot detect firmware bugs; nothing tests this sketch.
    ("firmware/pressure-sensor/*", []),
    ("src/thermal_cam/*", ["test_thermal_*.py", "test_orchestrator_rehearsal.py"]),
    ("src/vision/stage_twin/*", ["test_stage_twin.py", "test_console_position_finder.py"]),
    (
        "src/vision/*",
        [
            "test_vision_*.py",
            "test_stage_twin.py",
            "test_console_twin.py",
            "test_console_position_finder.py",
            "test_orchestrator_rehearsal.py",
        ],
    ),
    ("config/trays/*", ["test_vision_tray.py"]),
    ("config/cameras/*", ["test_vision_wrist_camera.py"]),
    ("scripts/wrist_camera_calibrate.py", ["test_vision_wrist_camera.py"]),
    ("scripts/wrist_chip_dataset.py", ["test_vision_wrist_dataset.py"]),
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
    # .claude/skills is a symlink to .agents/skills, so git reports the bare path.
    (".claude/skills", []),
    (".claude/skills/*", []),
    ("skills-lock.json", []),
    ("LICENSE", []),
    (".gitignore", []),
    # The JavaScript tests always run in CI.
    ("tests/*.mjs", []),
]


IMPORT = re.compile(r"""^\s*(?:from|import)\s+(\w+)|import_module\(\s*["'](\w+)""", re.MULTILINE)
# A test or helper can load a repository file by path, for example with runpy.
LOADED = re.compile(r"""["'/]((?:apps|deploy|examples|scripts)/[\w./-]+)""")


def _imports(path, root):
    """Return the top-level module names that `path` imports, also through files it loads."""
    text = path.read_text(encoding="utf-8")
    names = {a or b for a, b in IMPORT.findall(text)}
    for relative in LOADED.findall(text):
        loaded = root / relative
        if loaded.is_file() and loaded != path:
            names |= {a or b for a, b in IMPORT.findall(loaded.read_text(encoding="utf-8"))}
    return names


def _fixtures(conftest):
    """Return the fixture names of `conftest` and the module names that each one imports."""
    fixtures = {}
    for node in ast.parse(conftest.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef):
            names = set()
            for item in ast.walk(node):
                if isinstance(item, ast.ImportFrom) and item.module:
                    names.add(item.module.split(".")[0])
                elif isinstance(item, ast.Import):
                    names |= {alias.name.split(".")[0] for alias in item.names}
            # A fixture that requests another fixture also depends on its imports.
            fixtures[node.name] = names | {arg.arg for arg in node.args.args}
    return fixtures


def importers(packages, tests, root=None):
    """Return the test files that import one of `packages`, directly or indirectly.

    Follow imports through test helpers, files that a test loads by path, and the
    fixtures of tests/conftest.py. The area table lists the packages that import an area.
    """
    root = tests.parent if root is None else root
    deps = {}
    for helper in tests.glob("*.py"):
        if not helper.name.startswith("test_") and helper.name != "conftest.py":
            deps[helper.stem] = _imports(helper, root)
    conftest = tests / "conftest.py"
    fixtures = _fixtures(conftest) if conftest.is_file() else {}
    deps.update(fixtures)
    reached = set(packages)
    while True:
        more = {name for name, used in deps.items() if used & reached} - reached
        if not more:
            break
        reached |= more
    used = {name for name in fixtures if name in reached}
    found = set()
    for test in tests.glob("test_*.py"):
        text = test.read_text(encoding="utf-8")
        if _imports(test, root) & reached or any(re.search(rf"\b{n}\b", text) for n in used):
            found.add(test.name)
    return found


def select(changed, root=ROOT):
    """Return the pytest targets for the changed paths."""
    tests = root / "tests"
    patterns = set(ALWAYS)
    # The area table can miss a module that imports a changed package.
    # Only Python files can be imported; static web files and data use the area table.
    parts = [Path(path).parts for path in changed if path.endswith(".py")]
    packages = {p[1] for p in parts if len(p) > 2 and p[0] == "src"}
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
    if packages:
        patterns.update(importers(packages, tests, root))
    files = {f for p in patterns for f in tests.glob(p)}
    return sorted(f.relative_to(root).as_posix() for f in files)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    changed = argv or [line.strip() for line in sys.stdin if line.strip()]
    print("\n".join(select(changed)))


if __name__ == "__main__":
    main()
