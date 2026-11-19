"""Pull request CI selects tests by the changed areas."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("select_tests", ROOT / "scripts/select_tests.py")
select_tests = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(select_tests)
select = select_tests.select


@pytest.mark.parametrize(
    "path",
    [
        "src/labkit/lease.py",
        "tests/conftest.py",
        "tests/helpers.py",
        "tests/fixtures/doc_pairs/a.md",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/ci.yml",
        "scripts/select_tests.py",
        "unknown/file.txt",
    ],
)
def test_shared_or_unmapped_file_runs_full_suite(path):
    assert select(["src/vision/qr.py", path]) == ["tests"]


def test_area_change_runs_area_tests_and_repo_checks():
    selected = select(["src/thermal_cam/camera.py"])
    assert "tests/test_thermal_camera.py" in selected
    assert "tests/test_orchestrator_rehearsal.py" in selected
    assert "tests/test_doc_pairs.py" in selected
    assert "tests/test_repo_files.py" in selected
    assert "tests/test_joint_control.py" not in selected


def test_changed_test_file_runs_itself():
    assert "tests/test_vision_qr.py" in select(["tests/test_vision_qr.py"])


def test_package_change_runs_tests_that_import_it(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_direct.py").write_text("from thermal_cam.camera import Camera\n")
    (tests / "helper_sim.py").write_text("import thermal_cam\n")
    (tests / "test_indirect.py").write_text("from helper_sim import run\n")
    (tests / "test_other.py").write_text("import thermal_camera_extra\n")
    assert select(["src/thermal_cam/camera.py"], root=tmp_path) == [
        "tests/test_direct.py",
        "tests/test_indirect.py",
    ]


def test_docs_only_change_runs_doc_checks():
    assert select(["docs/vision.md", "README.en.md", ".claude/skills/x/SKILL.md"]) == [
        "tests/test_doc_pairs.py",
        "tests/test_repo_files.py",
    ]


def test_every_area_pattern_matches_a_test_file():
    tests = ROOT / "tests"
    for _, targets in select_tests.AREAS:
        for pattern in targets or []:
            if pattern != "tests":
                assert list(tests.glob(pattern)), pattern


def test_main_reads_paths_from_arguments(capsys):
    select_tests.main(["docs/vision.md"])
    assert capsys.readouterr().out.split() == [
        "tests/test_doc_pairs.py",
        "tests/test_repo_files.py",
    ]
