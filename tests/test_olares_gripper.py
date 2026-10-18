import runpy
import socket
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

ENTRY = Path(__file__).resolve().parents[1] / "apps/isaacsim/olares_gripper.py"


@pytest.fixture
def entry_environment(monkeypatch, tmp_path):
    real_run_path = runpy.run_path
    panel = Mock(return_value={})
    monkeypatch.setattr(runpy, "run_path", panel)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("MG400_DATA_DIR", str(tmp_path / "server-data"))
    monkeypatch.setattr(socket, "create_connection", Mock(side_effect=AssertionError("No network")))
    return real_run_path, panel


def test_import_does_not_start_panel_or_read_feedback(entry_environment, tmp_path):
    run, panel = entry_environment
    run(str(ENTRY), run_name="test_olares_gripper_import")
    panel.assert_not_called()
    socket.create_connection.assert_not_called()
    assert not (tmp_path / "server-data").exists()


@pytest.mark.parametrize("run_name", ("__main__", "<run_path>"))
def test_explicit_execution_uses_shared_usb_feedback_path(entry_environment, tmp_path, run_name):
    run, panel = entry_environment
    run(str(ENTRY), run_name=run_name)
    feedback = tmp_path / "server-data/work/gripper_olares_feedback.json"
    panel.assert_called_once_with(
        str(ENTRY.with_name("gripper_control.py")),
        init_globals={"FEEDBACK_PATH": str(feedback)},
    )
    assert feedback.parent.is_dir()
    socket.create_connection.assert_not_called()
