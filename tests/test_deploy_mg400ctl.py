"""Check mg400ctl commands without systemd, root rights or a service user."""

import io
import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = """# Test host
MG400_ROBOT_HOST=robot.test
MG400_CONTROLLER_LOCK_DIR=/var/lib/mg400/locks
MG400_PANEL_TOKEN=panel-token-0123456789abcdef
MG400_CAMERA_BIND=
MG400_STAGE_TOKEN=
"""


@pytest.fixture
def ctl(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=AssertionError("No real commands")))
    module = SimpleNamespace(**runpy.run_path(str(ROOT / "deploy/mg400ctl"), run_name="test_ctl"))
    globals_ = module.main.__globals__
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    chdir = Mock()
    monkeypatch.setattr(globals_["os"], "chdir", chdir)
    host = module.Host(tmp_path)
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(SETTINGS)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
    execvpe = Mock()
    out = io.StringIO()

    def call(*argv):
        return module.main(list(argv), host=host, run=run, execvpe=execvpe, out=out)

    return SimpleNamespace(
        module=module, host=host, run=run, execvpe=execvpe, out=out, chdir=chdir, call=call
    )


def systemctl(ctl):
    return [args[0] for args, _ in ctl.run.call_args_list]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["start", "web", "stage"], ["mg400-web-panel.service", "mg400-stage-panel.service"]),
        (["start", "camera"], ["mg400-camera-preview.service"]),
        (["stop", "camera"], ["mg400-camera-preview.service", "mg400-camera-capture.service"]),
        (["start", "collector"], ["mg400-gripper-collector.service"]),
        (
            ["restart", "all", "web"],
            [
                "mg400-web-panel.service",
                "mg400-stage-panel.service",
                "mg400-camera-preview.service",
                "mg400-camera-capture.service",
                "mg400-gripper-collector.service",
            ],
        ),
    ],
    ids=["start-two", "start-camera", "stop-camera", "collector", "restart-all"],
)
def test_service_actions_map_short_names_to_units(ctl, argv, expected):
    assert ctl.call(*argv) == 0
    assert systemctl(ctl) == [["systemctl", argv[0], *expected]]
    notice = "60 seconds" in ctl.out.getvalue()
    assert notice is (argv[0] != "start" and ("web" in argv or "all" in argv))


def test_unknown_service_name_is_refused(ctl, capsys):
    with pytest.raises(SystemExit):
        ctl.call("stop", "robot")
    ctl.run.assert_not_called()


def test_status_lists_every_service_and_the_request_watcher(ctl):
    blocks = [
        "Id=mg400-web-panel.service\nActiveState=active\nSubState=running",
        "Id=mg400-gripper-collector.path\nActiveState=active\nSubState=waiting",
    ]
    ctl.run.return_value = SimpleNamespace(returncode=0, stdout="\n\n".join(blocks) + "\n")
    assert ctl.call("status") == 0
    command = systemctl(ctl)[0]
    assert command[:3] == ["systemctl", "show", "--property=Id,ActiveState,SubState"]
    assert len(command) == 3 + 6
    lines = ctl.out.getvalue().splitlines()
    assert len(lines) == 6
    assert lines[0].split() == ["web", "mg400-web-panel.service", "active", "(running)"]
    assert lines[-1].split() == ["collector", "mg400-gripper-collector.path", "active", "(waiting)"]
    assert lines[1].split()[-1] == "unknown"


def test_logs_follow_both_camera_units(ctl):
    assert ctl.call("logs", "camera", "-n", "20", "-f") == 0
    assert systemctl(ctl) == [
        [
            "journalctl",
            "--no-pager",
            "-n",
            "20",
            "-u",
            "mg400-camera-preview.service",
            "-u",
            "mg400-camera-capture.service",
            "-f",
        ]
    ]


def test_terminal_runs_as_the_service_user_with_the_settings(ctl, monkeypatch):
    for name in ("COLORTERM", "LANG", "LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("SECRET_OF_ROOT", "hidden")
    assert ctl.call("terminal") == 0
    ctl.chdir.assert_called_once_with(ctl.host.code)
    (program, argv, environment), _ = ctl.execvpe.call_args
    assert program == "setpriv"
    assert argv == [
        "setpriv",
        "--reuid=1000",
        "--regid=1000",
        "--clear-groups",
        "--",
        str(ctl.host.code / ".venv/bin/mg400-robot"),
        "--gripper-feedback",
        str(ctl.host.data / "work/gripper_feedback.json"),
    ]
    assert environment == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(ctl.host.data),
        "MG400_ROBOT_HOST": "robot.test",
        "MG400_CONTROLLER_LOCK_DIR": "/var/lib/mg400/locks",
        "MG400_PANEL_TOKEN": "panel-token-0123456789abcdef",
        "TERM": "xterm-256color",
    }
    ctl.run.assert_not_called()


@pytest.mark.parametrize("switch", [False, True])
def test_mode_runs_the_switch_script_as_the_service_user(ctl, switch):
    assert ctl.call("mode", *["--switch-to-tcp"] * switch) == 0
    argv = ctl.execvpe.call_args.args[1]
    assert argv[5:] == [
        str(ctl.host.code / ".venv/bin/python"),
        str(ctl.host.code / "scripts/mg400_control_mode.py"),
        "--backup-dir",
        str(ctl.host.data / "work/control-mode"),
        *["--switch-to-tcp"] * switch,
    ]


def test_camera_url_uses_the_key_and_default_bind(ctl):
    key = ctl.host.data / "camera/preview.key"
    key.parent.mkdir(parents=True)
    key.write_text("k" * 43 + "\n")
    assert ctl.call("camera-url") == 0
    assert ctl.out.getvalue() == f"http://127.0.0.1:1985/{'k' * 43}/\n"


def test_token_prints_only_a_set_token(ctl, capsys):
    assert ctl.call("token", "web") == 0
    assert ctl.out.getvalue() == "panel-token-0123456789abcdef\n"
    assert ctl.call("token", "stage") == 1
    assert "No token is set" in capsys.readouterr().err


def test_non_root_and_missing_settings_are_refused(ctl, monkeypatch, capsys):
    monkeypatch.setattr(ctl.module.main.__globals__["os"], "geteuid", lambda: 1000)
    assert ctl.call("status") == 1
    assert "as root" in capsys.readouterr().err
    monkeypatch.setattr(ctl.module.main.__globals__["os"], "geteuid", lambda: 0)
    ctl.host.settings.unlink()
    assert ctl.call("terminal") == 1
    assert "Run deploy/install.py first" in capsys.readouterr().err
    ctl.run.assert_not_called()
    ctl.execvpe.assert_not_called()
