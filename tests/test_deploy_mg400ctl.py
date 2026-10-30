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
    getpwuid = Mock(return_value=SimpleNamespace(pw_name="mg400user"))
    monkeypatch.setattr(globals_["pwd"], "getpwuid", getpwuid)
    monkeypatch.setattr(
        globals_["grp"], "getgrgid", Mock(return_value=SimpleNamespace(gr_name="mg400group"))
    )
    monkeypatch.setattr(globals_["shutil"], "which", Mock(return_value="/usr/sbin/runuser"))
    host = module.Host(tmp_path)
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(SETTINGS)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
    execvpe = Mock()
    out = io.StringIO()

    def call(*argv):
        return module.main(list(argv), host=host, run=run, execvpe=execvpe, out=out)

    return SimpleNamespace(
        module=module,
        host=host,
        run=run,
        execvpe=execvpe,
        out=out,
        chdir=chdir,
        getpwuid=getpwuid,
        call=call,
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


def test_terminal_runs_as_the_service_user_on_its_own_terminal(ctl, monkeypatch):
    for name in ("COLORTERM", "LANG", "LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("SECRET_OF_ROOT", "hidden")
    assert ctl.call("terminal") == 0
    ctl.chdir.assert_called_once_with(ctl.host.code)
    ctl.getpwuid.assert_called_with(1000)
    (program, argv, environment), _ = ctl.execvpe.call_args
    # PATH of the service user does not contain /usr/sbin: use the absolute path.
    assert program == "/usr/sbin/runuser"
    assert argv == [
        "runuser",
        "--pty",
        "-u",
        "mg400user",
        "-g",
        "mg400group",
        "--",
        str(ctl.host.code / ".venv/bin/mg400-robot"),
        "--gripper-feedback",
        str(ctl.host.data / "work/gripper_feedback.json"),
    ]
    # No token and nothing else from the root environment reaches the service user.
    assert environment == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "MG400_ROBOT_HOST": "robot.test",
        "MG400_CONTROLLER_LOCK_DIR": "/var/lib/mg400/locks",
        "TERM": "xterm-256color",
    }
    ctl.run.assert_not_called()


@pytest.mark.parametrize("switch", [False, True])
def test_mode_runs_the_switch_script_as_the_service_user(ctl, switch):
    assert ctl.call("mode", *["--switch-to-tcp"] * switch) == 0
    argv = ctl.execvpe.call_args.args[1]
    assert argv[:7] == ["runuser", "--pty", "-u", "mg400user", "-g", "mg400group", "--"]
    assert argv[7:] == [
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


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ("MG400_CONTROLLER_LOCK_DIR=\n", "MG400_CONTROLLER_LOCK_DIR to an absolute path"),
        ("MG400_ROBOT_HOST robot.test\n", "is not KEY=VALUE"),
    ],
    ids=["empty-lock-directory", "unreadable-line"],
)
@pytest.mark.parametrize("command", ["terminal", "mode"])
def test_unsafe_settings_never_start_a_robot_program(ctl, capsys, settings, message, command):
    ctl.host.settings.write_text(settings)
    assert ctl.call(command) == 1
    assert message in capsys.readouterr().err
    ctl.execvpe.assert_not_called()


def test_missing_service_user_is_reported(ctl, capsys):
    ctl.getpwuid.side_effect = KeyError(1000)
    assert ctl.call("terminal") == 1
    assert "Create a user and a group with ID 1000 first" in capsys.readouterr().err
    ctl.execvpe.assert_not_called()


CAMERAS = [
    f"/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.{port}:1.0-video-index0" for port in (2, 3, 4)
]


def plug(ctl, devices):
    """Create each device link below the test root, as udev does for a connected camera."""
    (ctl.host.dev / "v4l/by-path").mkdir(parents=True, exist_ok=True)
    for number, device in enumerate(devices):
        node = ctl.host.dev / f"video{number * 2}"
        node.touch()
        (ctl.host.dev / device.removeprefix("/dev/")).symlink_to(node)


def answer(states, preload="camera1 camera2 camera3"):
    def run(command, **_):
        if command[0] == "curl":
            return SimpleNamespace(returncode=0, stdout=preload)
        blocks = [f"Id={unit}\nActiveState={state}\nSubState=x" for unit, state in states.items()]
        return SimpleNamespace(returncode=0, stdout="\n\n".join(blocks))

    return run


ACTIVE = {"mg400-camera-preview.service": "active", "mg400-camera-capture.service": "active"}


@pytest.fixture
def cameras(ctl):
    ctl.host.settings.write_text(SETTINGS + f"MG400_CAMERA_DEVICES={','.join(CAMERAS)}\n")
    return ctl


def test_camera_check_passes_when_devices_services_and_streams_work(cameras):
    plug(cameras, CAMERAS)
    cameras.run.side_effect = answer(ACTIVE)
    assert cameras.call("camera-check") == 0
    output = cameras.out.getvalue()
    assert output.count(" ok ") == 3
    assert f"{CAMERAS[1]} -> /dev/video2" in output
    assert "streams  all loaded" in output


@pytest.mark.parametrize(
    ("plugged", "hint"),
    [
        ([], "Check the USB hub"),
        (CAMERAS[:2], "Check the cable of each missing camera"),
        ([*CAMERAS[:2], CAMERAS[2].replace("12.1.4", "12.1.1")], "on another USB port"),
    ],
    ids=["hub", "cable", "moved"],
)
def test_missing_camera_is_reported_before_the_services(cameras, plugged, hint):
    plug(cameras, plugged)
    assert cameras.call("camera-check") == 1
    output = cameras.out.getvalue()
    assert f"camera3  missing  {CAMERAS[2]}" in output
    assert hint in output
    cameras.run.assert_not_called()


def test_camera_check_reports_stopped_services_and_missing_streams(cameras):
    plug(cameras, CAMERAS)
    cameras.run.side_effect = answer({**ACTIVE, "mg400-camera-capture.service": "inactive"})
    assert cameras.call("camera-check") == 1
    assert "mg400ctl start camera" in cameras.out.getvalue()
    cameras.run.side_effect = answer(ACTIVE, preload='{"camera1": {}, "camera3": {}}')
    assert cameras.call("camera-check") == 1
    assert "not loaded: camera2" in cameras.out.getvalue()


def test_camera_check_without_cameras(ctl):
    assert ctl.call("camera-check") == 0
    assert "Cameras are off" in ctl.out.getvalue()
    ctl.run.assert_not_called()
