"""Check mg400ctl commands without systemd, root rights or a service user."""

import hashlib
import io
import os
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


@pytest.mark.parametrize("command", ["run", "rehearse", "runs"])
def test_recipe_dispatch_precedes_admin_registry_and_drops_root(ctl, monkeypatch, command):
    settings = (
        SETTINGS
        + """
MG400_ORCH_RUNS_DIR=/var/lib/mg400/custom-runs
MG400_ORCH_ROBOT_URL=http://robot.invalid
MG400_ORCH_ROBOT_TOKEN=orchestrator-token-0123456789
MG400_ORCH_TIMEOUT_S=2
MG400_ORCH_UNRECOGNIZED_TOKEN=never-forward
MG400_PANEL_TOKEN=never-forward-panel
"""
    )
    ctl.host.settings.write_text(settings)
    monkeypatch.setitem(
        ctl.module.main.__globals__,
        "load_services",
        Mock(side_effect=AssertionError("admin parser")),
    )
    for key in (
        "PYTHONPATH",
        "LD_PRELOAD",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "MG400_ORCH_STAGE_TOKEN",
    ):
        monkeypatch.setenv(key, "ambient-secret")
    if command == "rehearse":
        ctl.host.settings.unlink()
    assert ctl.call(command, "recipe-or-run-id", "--runs-dir", "/tmp/run-records") == 0
    program, argv, environment = ctl.execvpe.call_args.args
    assert program == "/usr/sbin/runuser"
    assert argv == [
        "runuser",
        "--pty",
        "-u",
        "mg400user",
        "-g",
        "mg400group",
        "--",
        str(ctl.host.code / ".venv/bin/python"),
        "-I",
        "-m",
        "orchestrator",
        command,
        "recipe-or-run-id",
        "--runs-dir",
        "/tmp/run-records",
    ]
    settings = {key: value for key, value in environment.items() if key.startswith("MG400_")}
    expected = (
        {} if command == "rehearse" else {"MG400_ORCH_RUNS_DIR": "/var/lib/mg400/custom-runs"}
    )
    if command == "run":
        expected |= {
            "MG400_ORCH_ROBOT_URL": "http://robot.invalid",
            "MG400_ORCH_ROBOT_TOKEN": "orchestrator-token-0123456789",
            "MG400_ORCH_TIMEOUT_S": "2",
        }
    assert settings == expected
    assert not set(environment) & {
        "PYTHONPATH",
        "LD_PRELOAD",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
    }
    ctl.getpwuid.assert_called_with(1000)
    ctl.run.assert_not_called()
    ctl.chdir.assert_not_called()  # Preserve the caller's relative recipe paths.


@pytest.mark.parametrize("command", ["run", "rehearse", "runs"])
def test_nonroot_recipe_uses_only_allowed_explicit_environment(ctl, monkeypatch, command):
    for method in ("getuid", "geteuid", "getgid", "getegid"):
        monkeypatch.setattr(ctl.module.os, method, lambda: 1000)
    ctl.host.settings.unlink()
    monkeypatch.setenv("MG400_ORCH_RUNS_DIR", "/tmp/own-runs")
    monkeypatch.setenv("MG400_ORCH_ROBOT_URL", "http://robot.invalid")
    monkeypatch.setenv("MG400_ORCH_ROBOT_TOKEN", "explicit-token")
    monkeypatch.setenv("MG400_ORCH_UNKNOWN", "unknown")
    monkeypatch.setenv("MG400_PANEL_TOKEN", "panel")
    monkeypatch.setenv("HTTP_PROXY", "proxy")
    assert ctl.call(command) == 0
    program, argv, environment = ctl.execvpe.call_args.args
    assert program == str(ctl.host.code / ".venv/bin/python")
    assert argv == [program, "-I", "-m", "orchestrator", command]
    assert environment["MG400_ORCH_RUNS_DIR"] == "/tmp/own-runs"
    assert ("MG400_ORCH_ROBOT_TOKEN" in environment) is (command == "run")
    assert ("MG400_ORCH_ROBOT_URL" in environment) is (command == "run")
    assert not set(environment) & {"MG400_ORCH_UNKNOWN", "MG400_PANEL_TOKEN", "HTTP_PROXY"}
    ctl.getpwuid.assert_not_called()


@pytest.mark.parametrize("method", ["getuid", "geteuid", "getgid", "getegid"])
def test_nonroot_recipe_refuses_wrong_real_or_effective_identity(ctl, monkeypatch, method):
    for identity in ("getuid", "geteuid", "getgid", "getegid"):
        monkeypatch.setattr(ctl.module.os, identity, lambda: 1000)
    monkeypatch.setattr(ctl.module.os, method, lambda: 1001)
    assert ctl.call("rehearse", "recipe.toml") == 1
    ctl.execvpe.assert_not_called()
    ctl.run.assert_not_called()


@pytest.mark.parametrize("command", ["run-extra", "rehears", "runs-extra", "-m"])
def test_recipe_dispatch_accepts_only_exact_commands(ctl, command):
    with pytest.raises(SystemExit):
        ctl.call(command)
    ctl.execvpe.assert_not_called()


def test_root_recipe_dispatch_refuses_missing_runuser(ctl, monkeypatch):
    monkeypatch.setattr(ctl.module.shutil, "which", lambda *args, **kwargs: None)
    assert ctl.call("rehearse", "recipe.toml") == 1
    ctl.execvpe.assert_not_called()


@pytest.mark.parametrize("root", [True, False])
@pytest.mark.parametrize("command", ["run", "rehearse", "runs"])
def test_transfer_data_is_forwarded_without_rehearsal_device_credentials(
    ctl, monkeypatch, root, command
):
    geometry = {key: "explicit-data" for key in ctl.module.TRANSFER_SETTINGS}
    settings = {
        **geometry,
        "MG400_ORCH_ROBOT_URL": "http://production.invalid",
        "MG400_ORCH_ROBOT_TOKEN": "production-token",
        "MG400_GRIPPER_FEEDBACK": "/production/collector.json",
        "MG400_ORCH_UNKNOWN": "forbidden",
        "HTTP_PROXY": "http://proxy.invalid",
        "PYTHONPATH": "/untrusted/python",
    }
    if root and command != "rehearse":
        ctl.host.settings.write_text("\n".join(f"{key}={value}" for key, value in settings.items()))
    else:
        ctl.host.settings.unlink()
        for key, value in settings.items():
            monkeypatch.setenv(key, value)
    if not root:
        for name in ("getuid", "geteuid", "getgid", "getegid"):
            monkeypatch.setattr(ctl.module.os, name, lambda: 1000)
    assert ctl.call(command, "recipe-or-run-id") == 0
    program, argv, environment = ctl.execvpe.call_args.args
    assert {key: environment[key] for key in geometry if key in environment} == (
        {} if command == "runs" else geometry
    )
    assert ("MG400_ORCH_ROBOT_URL" in environment) is (command == "run")
    assert ("MG400_ORCH_ROBOT_TOKEN" in environment) is (command == "run")
    assert not set(environment) & {
        "MG400_GRIPPER_FEEDBACK",
        "MG400_ORCH_UNKNOWN",
        "HTTP_PROXY",
        "PYTHONPATH",
    }
    python = str(ctl.host.code / ".venv/bin/python")
    assert argv[-6:] == [python, "-I", "-m", "orchestrator", command, "recipe-or-run-id"]
    assert program == ("/usr/sbin/runuser" if root else python)
    if root:
        assert argv[:7] == ["runuser", "--pty", "-u", "mg400user", "-g", "mg400group", "--"]
    ctl.run.assert_not_called()


@pytest.mark.parametrize("root", [True, False])
@pytest.mark.parametrize("action", ["save", "list", "show", "rm"])
def test_station_dispatch_only_forwards_necessary_paths_bounds_and_read_credentials(
    ctl, monkeypatch, root, action
):
    station_settings = {key: "12" for key in ctl.module.STATION_SETTINGS}
    station_settings["MG400_ORCH_STATIONS_FILE"] = "/var/lib/mg400/stations.toml"
    extras = {
        "MG400_ORCH_TOOL_FILE": "/var/lib/mg400/tool.toml",
        "MG400_ORCH_ROBOT_URL": "http://robot.invalid",
        "MG400_ORCH_ROBOT_TOKEN": "robot-read-token-0123456789",
        "MG400_ORCH_TIMEOUT_S": "2",
        "MG400_ORCH_STAGE_TOKEN": "not-needed",
        "MG400_ORCH_STAGE_URL": "http://stage.invalid",
        "MG400_PANEL_TOKEN": "not-needed",
        "MG400_ORCH_RUNS_DIR": "/var/lib/mg400/runs",
        "HTTP_PROXY": "not-needed",
        "PYTHONPATH": "not-needed",
    }
    values = {**station_settings, **extras}
    if root:
        ctl.host.settings.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    else:
        for method in ("getuid", "geteuid", "getgid", "getegid"):
            monkeypatch.setattr(ctl.module.os, method, lambda: 1000)
        ctl.host.settings.unlink()
        for key, value in values.items():
            monkeypatch.setenv(key, value)
    assert ctl.call("station", action, *([] if action == "list" else ["tray"])) == 0
    program, argv, environment = ctl.execvpe.call_args.args
    expected = dict(station_settings)
    if action == "save":
        expected |= {
            key: extras[key]
            for key in (
                "MG400_ORCH_TOOL_FILE",
                "MG400_ORCH_ROBOT_URL",
                "MG400_ORCH_ROBOT_TOKEN",
                "MG400_ORCH_TIMEOUT_S",
            )
        }
    assert {
        key: value for key, value in environment.items() if key.startswith("MG400_")
    } == expected
    assert "HTTP_PROXY" not in environment and "PYTHONPATH" not in environment
    command = [
        str(ctl.host.code / ".venv/bin/python"),
        "-I",
        "-m",
        "orchestrator",
        "station",
        action,
        *([] if action == "list" else ["tray"]),
    ]
    assert (
        argv
        == (["runuser", "--pty", "-u", "mg400user", "-g", "mg400group", "--"] if root else [])
        + command
    )
    assert program == ("/usr/sbin/runuser" if root else command[0])
    ctl.run.assert_not_called()


@pytest.mark.parametrize("action", ["", "save-extra", "remove", "list-extra", "--unknown"])
def test_station_dispatch_requires_exact_subcommand(ctl, action):
    assert ctl.call("station", *([action] if action else [])) == 2
    ctl.execvpe.assert_not_called()


@pytest.mark.parametrize("identity", ["getuid", "geteuid", "getgid", "getegid"])
def test_station_dispatch_preserves_real_and_effective_identity_gate(ctl, monkeypatch, identity):
    for method in ("getuid", "geteuid", "getgid", "getegid"):
        monkeypatch.setattr(ctl.module.os, method, lambda: 1000)
    monkeypatch.setattr(ctl.module.os, identity, lambda: 1001)
    assert ctl.call("station", "save", "tray") == 1
    ctl.execvpe.assert_not_called()


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


def test_assets_reports_verified_missing_and_mismatched_without_commands(
    ctl, tmp_path, monkeypatch
):
    content = b"asset status fixture"
    config = tmp_path / "assets.toml"
    config.write_text(
        "\n".join(
            f'[{name}]\nurl = "https://assets.example/{name}"\n'
            f'sha256 = "{hashlib.sha256(content).hexdigest()}"\nbytes = {len(content)}\n'
            f'install = "/var/lib/mg400/models/{name}.dat"\nmode = "0640"\n'
            'service = "vision"\nsource = "test fixture"\nlicense = "AGPL-3.0"\n'
            for name in ("good", "missing", "wrong")
        )
    )
    ctl.host.asset_manifest = config
    target = ctl.host.data / "models/good.dat"
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    target.chmod(0o640)
    wrong = target.with_name("wrong.dat")
    wrong.write_bytes(b"wrong")
    wrong.chmod(0o640)
    assets = ctl.module.main.__globals__["assets"]
    monkeypatch.setattr(assets, "OWNER", (os.getuid(), os.getgid()))
    monkeypatch.setattr(assets, "open_download", Mock(side_effect=AssertionError("No download")))
    ctl.host.settings.unlink()  # Asset inspection does not require service settings.
    assert ctl.call("assets") == 1
    assert "good  present" in ctl.out.getvalue()
    assert "missing  missing" in ctl.out.getvalue()
    assert "wrong  mismatch" in ctl.out.getvalue()
    ctl.run.assert_not_called()
    ctl.execvpe.assert_not_called()
    wrong.write_bytes(content)
    missing = target.with_name("missing.dat")
    missing.write_bytes(content)
    missing.chmod(0o640)
    assert ctl.call("assets") == 0


def test_assets_reports_invalid_manifest_without_commands(ctl, tmp_path, capsys):
    ctl.host.asset_manifest = tmp_path / "bad.toml"
    ctl.host.asset_manifest.write_text('[broken]\nurl = "file:///tmp/model"\n')
    assert ctl.call("assets") == 1
    assert "Cannot read assets:" in capsys.readouterr().err
    ctl.run.assert_not_called()


@pytest.fixture
def optional_ctl(ctl, tmp_path):
    registry = tmp_path / "services.toml"
    registry.write_text(
        (ROOT / "deploy/services.toml").read_text()
        + '\n[[service]]\nname = "optional"\nunits = ["mg400-optional.service"]\n'
        'optional = true\nvenv = ".venv-vision"\ngroups = ["vision"]\n'
        'token = "MG400_OPTIONAL_TOKEN"\nport = 8771\nassets = []\n'
    )
    ctl.host.registry = registry
    return ctl


@pytest.mark.parametrize("action", ["start", "restart"])
def test_disabled_optional_service_cannot_start(optional_ctl, action):
    assert optional_ctl.call(action, "optional") == 1
    optional_ctl.run.assert_not_called()
    optional_ctl.execvpe.assert_not_called()


def test_start_all_selects_only_enabled_services(optional_ctl):
    assert optional_ctl.call("start", "all") == 0
    assert "mg400-optional.service" not in systemctl(optional_ctl)[0]
    optional_ctl.run.reset_mock()
    optional_ctl.host.settings.write_text(SETTINGS + "MG400_OPTIONAL_SERVICES=optional\n")
    assert optional_ctl.call("start", "all") == 0
    assert systemctl(optional_ctl) == [
        [
            "systemctl",
            "start",
            "mg400-web-panel.service",
            "mg400-stage-panel.service",
            "mg400-camera-preview.service",
            "mg400-gripper-collector.service",
            "mg400-optional.service",
        ]
    ]


@pytest.mark.parametrize("target", ["optional", "all"])
def test_stop_remains_available_after_optional_service_is_disabled(optional_ctl, target):
    optional_ctl.host.settings.write_text(SETTINGS + "MG400_OPTIONAL_SERVICES=unknown,unknown\n")
    assert optional_ctl.call("stop", target) == 0
    assert "mg400-optional.service" in systemctl(optional_ctl)[0]


def test_logs_remain_available_for_disabled_registered_services(optional_ctl):
    assert optional_ctl.call("logs", "optional") == 0
    assert systemctl(optional_ctl) == [
        ["journalctl", "--no-pager", "-n", "50", "-u", "mg400-optional.service"]
    ]


def test_status_shows_only_enabled_services(optional_ctl):
    assert optional_ctl.call("status") == 0
    assert "mg400-optional.service" not in optional_ctl.out.getvalue()
    optional_ctl.host.settings.write_text(SETTINGS + "MG400_OPTIONAL_SERVICES=optional\n")
    assert optional_ctl.call("status") == 0
    assert "mg400-optional.service" in optional_ctl.out.getvalue()


@pytest.mark.parametrize("selection", ["unknown", "optional,optional", "optional,", "web"])
def test_invalid_optional_selection_refuses_start_before_commands(optional_ctl, selection):
    optional_ctl.host.settings.write_text(SETTINGS + f"MG400_OPTIONAL_SERVICES={selection}\n")
    assert optional_ctl.call("start", "all") == 1
    optional_ctl.run.assert_not_called()
    optional_ctl.execvpe.assert_not_called()


def test_optional_token_comes_from_registry_setting(optional_ctl):
    optional_ctl.host.settings.write_text(SETTINGS + "MG400_OPTIONAL_TOKEN=test-token-value\n")
    assert optional_ctl.call("token", "optional") == 0
    assert optional_ctl.out.getvalue() == "test-token-value\n"
    optional_ctl.run.assert_not_called()


def test_invalid_registry_fails_before_commands(optional_ctl, capsys):
    optional_ctl.host.registry.write_text("[invalid]\n")
    assert optional_ctl.call("start", "all") == 1
    assert "Cannot read service registry:" in capsys.readouterr().err
    optional_ctl.run.assert_not_called()


def test_help_names_both_command_links(ctl, capsys):
    with pytest.raises(SystemExit) as exit_:
        ctl.call("--help")
    assert exit_.value.code == 0
    help_text = capsys.readouterr().out
    assert "mg400ctl or labctl" in help_text
    ctl.run.assert_not_called()


@pytest.fixture
def backup_ctl(ctl, monkeypatch):
    # The fake host belongs to the test user; production requires UID 0.
    monkeypatch.setitem(ctl.module.backup.__globals__, "BACKUP_OWNER", (os.getuid(), os.getgid()))
    ctl.host.settings.chmod(0o600)
    ctl.host.data.mkdir(parents=True)
    ctl.host.data.chmod(0o750)
    directory = ctl.host.root / "backups"
    directory.mkdir(mode=0o700)
    ctl.archive = directory / "host.tar"
    return ctl


def test_backup_preserves_host_data_owners_modes_and_persistent_command_locks(backup_ctl):
    import stat
    import tarfile

    ctl = backup_ctl
    files = {
        "stations.toml": b"[station]\n",
        "tool.toml": b"offset = 1\n",
        "stage.toml": b"steps_per_mm = 1\n",
        "calibration/cameras/arm.toml": b"homography = []\n",
        "calibration/microscope/reference.json": b"{}\n",
        "thermal/rois.toml": b"[crucible]\n",
        "furnace/state.json": b'{"phase":"HOLD"}\n',
        "runs/index.sqlite": b"fake database fixture",
        "recordings/run.mp4": b"fake video fixture",
        "models/model.pt": b"fake model fixture",
        "camera/preview.key": b"private-preview-key",
        "work/gripper_feedback.json.command-lock.json": b'{"outcome":"unknown"}',
        "work/locks/persistent.json": b"do not omit nested lock directories",
        "locks/process.lock": b"exclude only this process-lock tree",
    }
    for name, content in files.items():
        path = ctl.host.data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600 if name.endswith(("key", ".json")) else 0o640)
    old_umask = os.umask(0)
    try:
        assert ctl.call("backup", str(ctl.archive)) == 0
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(ctl.archive.stat().st_mode) == 0o600
    assert (ctl.archive.stat().st_uid, ctl.archive.stat().st_gid) == (os.getuid(), os.getgid())
    with tarfile.open(ctl.archive) as archive:
        names = archive.getnames()
        assert "var/lib/mg400" in names
        assert "etc/mg400/mg400.env" in names
        assert archive.extractfile("etc/mg400/mg400.env").read() == ctl.host.settings.read_bytes()
        for member in archive:
            assert not member.name.startswith("/")
            assert ".." not in Path(member.name).parts
            assert member.isfile() or member.isdir()
            path = ctl.host.root / member.name
            info = path.stat()
            assert (member.uid, member.gid) == (info.st_uid, info.st_gid)
            assert member.mode == stat.S_IMODE(info.st_mode)
            assert not member.uname and not member.gname
        for name, content in files.items():
            if name.startswith("locks/"):
                assert f"var/lib/mg400/{name}" not in names
            else:
                assert archive.extractfile(f"var/lib/mg400/{name}").read() == content
            assert (ctl.host.data / name).read_bytes() == content
        assert "var/lib/mg400/locks" not in names
    assert list(ctl.archive.parent.iterdir()) == [ctl.archive]
    assert SETTINGS not in ctl.out.getvalue()
    ctl.run.assert_not_called()
    ctl.execvpe.assert_not_called()


@pytest.mark.parametrize("kind", ["file", "directory", "symlink", "dangling-symlink"])
def test_backup_never_overwrites_an_existing_destination(backup_ctl, kind):
    ctl = backup_ctl
    target = ctl.archive.parent / "existing"
    target.write_bytes(b"keep this content")
    if kind == "file":
        ctl.archive.write_bytes(b"keep this archive")
    elif kind == "directory":
        ctl.archive.mkdir()
    elif kind == "symlink":
        ctl.archive.symlink_to(target)
    else:
        ctl.archive.symlink_to(ctl.archive.parent / "missing")
    before = ctl.archive.lstat()
    assert ctl.call("backup", str(ctl.archive)) == 1
    assert ctl.archive.lstat().st_ino == before.st_ino
    assert target.read_bytes() == b"keep this content"
    if kind == "file":
        assert ctl.archive.read_bytes() == b"keep this archive"
    assert not list(ctl.archive.parent.glob(".mg400-backup-*"))
    ctl.run.assert_not_called()


def test_backup_publication_refuses_a_destination_created_during_backup(backup_ctl, monkeypatch):
    ctl = backup_ctl
    link = os.link

    def collision(*args, **kwargs):
        ctl.archive.write_bytes(b"created by another writer")
        return link(*args, **kwargs)

    monkeypatch.setattr(ctl.module.backup.__globals__["os"], "link", collision)
    assert ctl.call("backup", str(ctl.archive)) == 1
    assert ctl.archive.read_bytes() == b"created by another writer"
    assert not list(ctl.archive.parent.glob(".mg400-backup-*"))


@pytest.mark.parametrize(
    "location", ["settings", "data", "child", "fifo", "settings-directory", "data-file"]
)
def test_backup_rejects_symlink_and_special_sources(backup_ctl, location):
    ctl = backup_ctl
    external = ctl.host.root / "unrelated-secret"
    external.write_bytes(b"never follow this path")
    if location == "settings":
        ctl.host.settings.unlink()
        ctl.host.settings.symlink_to(external)
    elif location == "data":
        ctl.host.data.rmdir()
        ctl.host.data.symlink_to(external.parent)
    elif location == "child":
        (ctl.host.data / "outside").symlink_to(external)
    elif location == "fifo":
        os.mkfifo(ctl.host.data / "pipe")
    elif location == "settings-directory":
        ctl.host.settings.unlink()
        ctl.host.settings.mkdir()
    else:
        ctl.host.data.rmdir()
        ctl.host.data.write_bytes(b"not a directory")
    assert ctl.call("backup", str(ctl.archive)) == 1
    assert not ctl.archive.exists()
    assert not list(ctl.archive.parent.glob(".mg400-backup-*"))
    assert external.read_bytes() == b"never follow this path"
    ctl.run.assert_not_called()


@pytest.mark.parametrize(
    "kind", ["inside", "inside-locks", "parent", "symlink-parent", "unsafe-mode", "wrong-owner"]
)
def test_backup_rejects_unsafe_destinations(backup_ctl, monkeypatch, kind):
    ctl = backup_ctl
    destination = ctl.archive
    if kind == "inside":
        destination = ctl.host.data / "host.tar"
    elif kind == "inside-locks":
        destination = ctl.host.data / "locks/host.tar"
    elif kind == "parent":
        destination = ctl.archive.parent / "../host.tar"
    elif kind == "symlink-parent":
        alias = ctl.host.root / "alias"
        alias.symlink_to(ctl.archive.parent, target_is_directory=True)
        destination = alias / "host.tar"
    elif kind == "unsafe-mode":
        ctl.archive.parent.chmod(0o770)
    else:
        monkeypatch.setitem(
            ctl.module.backup.__globals__, "BACKUP_OWNER", (os.getuid() + 1, os.getgid())
        )
    assert ctl.call("backup", str(destination)) == 1
    assert not destination.exists()
    assert not list(ctl.archive.parent.glob(".mg400-backup-*"))
    ctl.run.assert_not_called()


@pytest.mark.parametrize("failure", ["write", "changed", "fsync", "ownership"])
def test_backup_failure_removes_private_staging_without_publishing(
    backup_ctl, monkeypatch, failure
):
    import stat
    import tarfile

    ctl = backup_ctl
    source = ctl.host.data / "stations.toml"
    source.write_bytes(b"original station data")
    addfile = tarfile.TarFile.addfile

    def interrupted(archive, member, stream=None):
        if member.name == "var/lib/mg400/stations.toml":
            temporary = next(ctl.archive.parent.glob(".mg400-backup-*"))
            assert stat.S_IMODE(temporary.stat().st_mode) == 0o700
            assert stat.S_IMODE((temporary / "archive.tar").stat().st_mode) == 0o600
            if failure == "write":
                raise OSError("injected archive failure")
        result = addfile(archive, member, stream)
        if failure == "changed" and member.name == "var/lib/mg400/stations.toml":
            source.write_bytes(b"changed during read")
        return result

    monkeypatch.setattr(tarfile.TarFile, "addfile", interrupted)
    if failure == "fsync":
        monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError("injected sync failure")))
    elif failure == "ownership":
        monkeypatch.setattr(os, "fchown", Mock(side_effect=OSError("injected ownership failure")))
    assert ctl.call("backup", str(ctl.archive)) == 1
    assert not ctl.archive.exists()
    assert not list(ctl.archive.parent.glob(".mg400-backup-*"))
    assert ctl.host.settings.read_text() == SETTINGS


def test_backup_archives_settings_without_parsing_or_printing_their_contents(backup_ctl, capsys):
    import tarfile

    ctl = backup_ctl
    content = b"private settings with an invalid line\x00\n"
    ctl.host.settings.write_bytes(content)
    assert ctl.call("backup", str(ctl.archive)) == 0
    with tarfile.open(ctl.archive) as archive:
        assert archive.extractfile("etc/mg400/mg400.env").read() == content
    assert "private settings" not in ctl.out.getvalue() + capsys.readouterr().err
    ctl.run.assert_not_called()


def test_backup_refuses_non_root_and_missing_data(backup_ctl, monkeypatch):
    ctl = backup_ctl
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert ctl.call("backup", str(ctl.archive)) == 1
    assert not ctl.archive.exists()
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    ctl.host.data.rmdir()
    assert ctl.call("backup", str(ctl.archive)) == 1
    assert not ctl.archive.exists()
    assert not list(ctl.archive.parent.glob(".mg400-backup-*"))


def test_backup_does_not_follow_a_source_replaced_after_it_is_pinned(backup_ctl, monkeypatch):
    ctl = backup_ctl
    source = ctl.host.data / "stations.toml"
    source.write_bytes(b"original stations")
    external = ctl.host.root / "external-secret"
    external.write_bytes(b"must never be read")
    outside_inode = external.stat().st_ino
    open_ = os.open
    swapped = False

    def race(path, flags, *args, **kwargs):
        nonlocal swapped
        if str(path).startswith("/proc/self/fd/"):
            pinned = int(str(path).rsplit("/", 1)[-1])
            assert os.fstat(pinned).st_ino != outside_inode
        fd = open_(path, flags, *args, **kwargs)
        if path == "stations.toml" and flags & os.O_PATH and not swapped:
            swapped = True
            source.unlink()
            source.symlink_to(external)
        return fd

    monkeypatch.setattr(os, "open", race)
    assert ctl.call("backup", str(ctl.archive)) == 1
    assert swapped
    assert not ctl.archive.exists()
    assert external.read_bytes() == b"must never be read"
    assert not list(ctl.archive.parent.glob(".mg400-backup-*"))
