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
MG400_CAMERA_DEVICES=
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
    state = Mock(
        return_value={
            "service": "furnace",
            "connected": True,
            "locked": False,
            "fault": None,
            "sequence": {"running": False, "phase": "idle"},
        }
    )

    def call(*argv):
        return module.main(
            list(argv), host=host, run=run, execvpe=execvpe, out=out, read_furnace_state=state
        )

    return SimpleNamespace(
        module=module,
        host=host,
        run=run,
        execvpe=execvpe,
        out=out,
        chdir=chdir,
        getpwuid=getpwuid,
        call=call,
        state=state,
    )


def systemctl(ctl):
    return [args[0] for args, _ in ctl.run.call_args_list]


def mutations(ctl):
    """Return the systemctl commands that can change a unit; show only reads."""
    return [command for command in systemctl(ctl) if command[:2] != ["systemctl", "show"]]


def systemd_units(ctl, missing=(), running=()):
    """Answer systemctl show like systemd for not-found or loaded units."""

    def run(command, **kwargs):
        if command[:2] == ["systemctl", "show"]:
            blocks = [
                f"Id={unit}\nLoadState={'not-found' if unit in missing else 'loaded'}\n"
                f"ActiveState={'active' if unit in running else 'inactive'}"
                for unit in command[3:]
            ]
            return SimpleNamespace(returncode=0, stdout="\n\n".join(blocks))
        return SimpleNamespace(returncode=0, stdout="")

    ctl.run.side_effect = run


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
                "mg400-furnace.service",
                "mg400-microscope.service",
                "mg400-orchestrator.service",
                "mg400-stage-panel.service",
                "mg400-camera-preview.service",
                "mg400-camera-capture.service",
                "mg400-gripper-collector.service",
                "mg400-proxy.service",
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


@pytest.mark.parametrize("command", ["run", "rehearse", "runs", "station"])
def test_recipe_dispatch_precedes_admin_registry_and_drops_root(ctl, monkeypatch, command):
    settings = SETTINGS + "MG400_ORCH_RUNS_DIR=/var/lib/mg400/custom-runs\nMG400_EMPTY=\n"
    ctl.host.settings.write_text(settings)
    monkeypatch.setitem(
        ctl.module.main.__globals__,
        "load_services",
        Mock(side_effect=AssertionError("admin parser")),
    )
    for key in ("PYTHONPATH", "LD_PRELOAD", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(key, "ambient")
    monkeypatch.setenv("MG400_ORCH_ZONES_FILE", "/tmp/demo-zones.toml")
    if command == "rehearse":
        # Rehearsal never reads the host settings.
        ctl.host.settings.unlink()
    arguments = ["list"] if command == "station" else ["recipe-or-run-id", "--runs-dir", "/tmp/r"]
    assert ctl.call(command, *arguments) == 0
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
        *arguments,
    ]
    forwarded = {key: value for key, value in environment.items() if key.startswith("MG400_")}
    if command == "rehearse":
        assert forwarded == {"MG400_ORCH_ZONES_FILE": "/tmp/demo-zones.toml"}
    else:
        file_settings = {
            key: value
            for key, value in ctl.module.parse_settings(settings).items()
            if key.startswith("MG400_") and value
        }
        assert forwarded == {**ctl.module.FIXED_SETTINGS, **file_settings}
    assert not set(environment) & {"PYTHONPATH", "LD_PRELOAD", "HTTP_PROXY", "HTTPS_PROXY"}
    ctl.getpwuid.assert_called_with(1000)
    ctl.run.assert_not_called()
    ctl.chdir.assert_not_called()  # Preserve the caller's relative recipe paths.


@pytest.mark.parametrize("command", ["run", "rehearse", "runs"])
def test_nonroot_recipe_forwards_every_caller_setting(ctl, monkeypatch, command):
    for method in ("getuid", "geteuid", "getgid", "getegid"):
        monkeypatch.setattr(ctl.module.os, method, lambda: 1000)
    ctl.host.settings.unlink()
    monkeypatch.setenv("MG400_ORCH_RUNS_DIR", "/tmp/own-runs")
    monkeypatch.setenv("MG400_ORCH_ROBOT_URL", "http://robot.invalid")
    monkeypatch.setenv("HTTP_PROXY", "proxy")
    assert ctl.call(command) == 0
    program, argv, environment = ctl.execvpe.call_args.args
    assert program == str(ctl.host.code / ".venv/bin/python")
    assert argv == [program, "-I", "-m", "orchestrator", command]
    assert environment["MG400_ORCH_RUNS_DIR"] == "/tmp/own-runs"
    assert environment["MG400_ORCH_ROBOT_URL"] == "http://robot.invalid"
    assert ("MG400_ORCH_STAGE_URL" in environment) is (command != "rehearse")
    assert "HTTP_PROXY" not in environment
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
    assert len(command) == 3 + 10
    lines = ctl.out.getvalue().splitlines()
    assert len(lines) == 10
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
    # Nothing from the root environment except the terminal settings reaches the service user.
    assert environment == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "MG400_CONTROLLER_LOCK_DIR": "/var/lib/mg400/locks",
        "MG400_ROBOT_HOST": "robot.test",
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


def test_gpu_uses_a_fixed_read_only_query_as_uid_1000_without_service_secrets(ctl, monkeypatch):
    lookup = Mock(
        side_effect=lambda name, **_: {
            "nvidia-smi": "/usr/bin/nvidia-smi",
            "runuser": "/usr/sbin/runuser",
        }[name]
    )
    monkeypatch.setattr(ctl.module.main.__globals__["shutil"], "which", lookup)
    monkeypatch.setenv("LD_PRELOAD", "/untrusted/library.so")
    monkeypatch.setenv("MG400_VISION_TOKEN", "root-only-secret")
    # GPU status does not require loading service settings or their credentials.
    ctl.host.settings.unlink()
    assert ctl.call("gpu") == 0
    ctl.getpwuid.assert_called_once_with(1000)
    ctl.module.main.__globals__["grp"].getgrgid.assert_called_once_with(1000)
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
        "/usr/bin/nvidia-smi",
        "--query-gpu=name,memory.used,memory.total",
        "--format=csv",
    ]
    lookup.assert_any_call("nvidia-smi", path="/usr/bin:/bin")
    assert not any(key.startswith("MG400_") for key in environment)
    assert "LD_PRELOAD" not in environment
    ctl.run.assert_not_called()


@pytest.mark.parametrize("missing", ["nvidia-smi", "runuser", "user", "group"])
def test_gpu_refuses_missing_program_or_service_identity(ctl, monkeypatch, capsys, missing):
    globals_ = ctl.module.main.__globals__
    monkeypatch.setattr(
        globals_["shutil"],
        "which",
        lambda name, **_: None if name == missing else f"/usr/bin/{name}",
    )
    if missing == "user":
        globals_["pwd"].getpwuid.side_effect = KeyError(1000)
    elif missing == "group":
        globals_["grp"].getgrgid.side_effect = KeyError(1000)
    assert ctl.call("gpu") == 1
    assert "Cannot read GPU status as UID 1000" in capsys.readouterr().err
    ctl.execvpe.assert_not_called()
    ctl.run.assert_not_called()


def test_gpu_requires_root_and_accepts_no_extra_query_or_write_arguments(ctl, monkeypatch):
    with pytest.raises(SystemExit):
        ctl.call("gpu", "--reset-gpu-clocks")
    monkeypatch.setattr(ctl.module.main.__globals__["os"], "geteuid", lambda: 1000)
    assert ctl.call("gpu") == 1
    ctl.execvpe.assert_not_called()
    ctl.run.assert_not_called()


def test_camera_url_lists_each_camera_through_the_proxy(ctl):
    ctl.host.settings.write_text(SETTINGS + "MG400_CAMERA_DEVICES=" + ",".join(CAMERAS[:2]) + "\n")
    assert ctl.call("camera-url") == 0
    assert ctl.out.getvalue() == (
        "http://127.0.0.1:8080/camera/camera1.mjpeg\nhttp://127.0.0.1:8080/camera/camera2.mjpeg\n"
    )


def test_camera_url_uses_the_proxy_when_it_is_enabled(ctl):
    devices = "MG400_CAMERA_DEVICES=" + CAMERAS[0] + "\n"
    ctl.host.settings.write_text(
        SETTINGS + devices + "MG400_OPTIONAL_SERVICES=proxy\nMG400_PROXY_BIND=100.64.0.1\n"
    )
    assert ctl.call("camera-url") == 0
    assert ctl.out.getvalue() == "http://100.64.0.1:8080/camera/camera1.mjpeg\n"
    ctl.out.truncate(0)
    ctl.out.seek(0)
    ctl.host.settings.write_text(
        SETTINGS
        + devices
        + "MG400_OPTIONAL_SERVICES=proxy\nMG400_PROXY_BIND=fd00::1\nMG400_PROXY_PORT=9443\n"
    )
    assert ctl.call("camera-url") == 0
    assert ctl.out.getvalue() == "http://[fd00::1]:9443/camera/camera1.mjpeg\n"


def test_camera_url_without_cameras(ctl):
    assert ctl.call("camera-url") == 0
    assert ctl.out.getvalue() == "Cameras are off: MG400_CAMERA_DEVICES is empty.\n"


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
        ("MG400_ROBOT_HOST robot.test\n", "is not KEY=VALUE"),
    ],
    ids=["unreadable-line"],
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
    assert "streams  configured preloads loaded" in output


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


def test_camera_check_expects_only_low_resolution_preloads(cameras):
    plug(cameras, CAMERAS)
    with cameras.host.settings.open("a") as settings:
        settings.write("MG400_CAMERA_MODES=640x480@30,1920x1080@30,3840x2160@30\n")
    cameras.run.side_effect = answer(ACTIVE, preload='{"camera1": {}}')
    assert cameras.call("camera-check") == 0
    assert "camera2 starts on demand" in cameras.out.getvalue()
    assert "camera3 starts on demand" in cameras.out.getvalue()
    assert sum(command[0] == "curl" for command in systemctl(cameras)) == 1


def test_camera_check_all_large_streams_does_not_open_them(cameras):
    plug(cameras, CAMERAS)
    with cameras.host.settings.open("a") as settings:
        settings.write("MG400_CAMERA_MODES=1600x1200@30,1920x1080@60,3840x2160@30\n")
    cameras.run.side_effect = answer(ACTIVE, preload="")
    assert cameras.call("camera-check") == 0
    assert "no continuous preloads" in cameras.out.getvalue()
    assert not any(command[0] == "curl" for command in systemctl(cameras))


def test_camera_check_rejects_conflicting_modes_before_device_or_service_checks(cameras, capsys):
    with cameras.host.settings.open("a") as settings:
        settings.write(
            "MG400_CAMERA_MODES=640x480@30,640x480@30,640x480@30\n"
            "MG400_CAMERA_CAPTURE_SIZES=640x480,640x480,640x480\n"
        )
    assert cameras.call("camera-check") == 1
    assert "MG400_CAMERA" in capsys.readouterr().err
    cameras.run.assert_not_called()


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
        "port = 8799\nassets = []\n"
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
            "mg400-furnace.service",
            "mg400-microscope.service",
            "mg400-orchestrator.service",
            "mg400-stage-panel.service",
            "mg400-camera-preview.service",
            "mg400-gripper-collector.service",
            "mg400-proxy.service",
            "mg400-optional.service",
        ]
    ]


@pytest.mark.parametrize("target", ["optional", "all"])
def test_stop_remains_available_after_optional_service_is_disabled(optional_ctl, target):
    optional_ctl.host.settings.write_text(SETTINGS + "MG400_OPTIONAL_SERVICES=unknown,unknown\n")
    assert optional_ctl.call("stop", target) == 0
    assert "mg400-optional.service" in mutations(optional_ctl)[0]


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


@pytest.mark.parametrize("selection", ["unknown", "optional,optional", "optional,"])
def test_invalid_optional_selection_refuses_start_before_commands(optional_ctl, selection):
    optional_ctl.host.settings.write_text(SETTINGS + f"MG400_OPTIONAL_SERVICES={selection}\n")
    assert optional_ctl.call("start", "all") == 1
    optional_ctl.run.assert_not_called()
    optional_ctl.execvpe.assert_not_called()


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
        "robot/stations.toml": b"[stations]\n",
        "robot/tool.toml": b"offset_mm = [0, 0, 146]\n",
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


@pytest.mark.parametrize("action", ["stop", "restart"])
@pytest.mark.parametrize("targets", [("furnace",), ("web", "furnace"), ("all",)])
@pytest.mark.parametrize(
    "phase,running", [("ramping", True), ("holding", True), ("cooling", False)]
)
def test_furnace_shutdown_guard_precedes_all_systemctl_mutations(
    ctl, capsys, action, targets, phase, running
):
    ctl.host.settings.write_text(SETTINGS + "MG400_OPTIONAL_SERVICES=furnace\n")
    ctl.state.return_value["sequence"] = {"phase": phase, "running": running}
    assert ctl.call(action, *targets) == 1
    assert mutations(ctl) == []
    assert "--force" in capsys.readouterr().err
    assert ctl.call(action, *targets, "--force") == 0
    assert "mg400-furnace.service" in mutations(ctl)[0]
    assert ctl.state.call_count == 1


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        {"service": "other"},
        {"service": "furnace", "connected": False, "locked": False, "fault": None},
        {"service": "furnace", "connected": True, "locked": True, "fault": {"message": "unknown"}},
        {
            "service": "furnace",
            "connected": True,
            "locked": False,
            "fault": None,
            "sequence": {"running": "false", "phase": "idle"},
        },
        {
            "service": "furnace",
            "connected": True,
            "locked": False,
            "fault": None,
            "sequence": {"running": False, "phase": "ramping"},
        },
    ],
)
def test_furnace_shutdown_refuses_unknown_state_even_after_optional_disable(ctl, state):
    ctl.state.return_value = state
    assert ctl.call("stop", "all") == 1
    assert mutations(ctl) == []
    assert ctl.call("stop", "furnace", "--force") == 0


def test_furnace_shutdown_refuses_unavailable_status(ctl):
    ctl.state.side_effect = OSError("offline")
    assert ctl.call("stop", "furnace") == 1
    assert ctl.call("restart", "all") == 1
    assert mutations(ctl) == []
    assert ctl.state.call_count == 2
    assert ctl.call("restart", "all", "--force") == 0
    assert "mg400-furnace.service" in mutations(ctl)[0]


def registered_units(ctl, optional):
    registry = ctl.module.load_services(ctl.host.registry)
    return {
        unit
        for entry in registry.values()
        if entry["optional"] is optional
        for unit in entry["units"]
    }


def test_stop_all_skips_uninstalled_units_and_their_furnace_check(ctl):
    furnace = {"mg400-furnace.service"}
    systemd_units(ctl, missing=registered_units(ctl, True) | furnace)
    ctl.state.side_effect = OSError("offline")
    assert ctl.call("stop", "all") == 0
    ctl.state.assert_not_called()
    (stop,) = mutations(ctl)
    assert stop[:2] == ["systemctl", "stop"]
    assert set(stop[2:]) == registered_units(ctl, False) - furnace
    assert "Skipping mg400-furnace.service: it is not installed" in ctl.out.getvalue()


@pytest.mark.parametrize(
    ("missing", "running"),
    [((), ()), (("mg400-furnace.service",), ("mg400-furnace.service",))],
    ids=["installed-and-disabled", "unit-file-removed-while-running"],
)
def test_stop_all_keeps_the_furnace_check_while_its_unit_exists_or_runs(ctl, missing, running):
    systemd_units(ctl, missing=missing, running=running)
    ctl.state.side_effect = OSError("offline")
    assert ctl.call("stop", "all") == 1
    assert ctl.state.call_count == 1
    assert mutations(ctl) == []


@pytest.mark.parametrize("returncode", [1, 0], ids=["systemd-error", "no-answer"])
def test_unreadable_systemd_state_keeps_the_furnace_check(ctl, returncode):
    answer = "Id=mg400-furnace.service\nLoadState=not-found\nActiveState=inactive"
    ctl.run.return_value = SimpleNamespace(
        returncode=returncode, stdout=answer if returncode else ""
    )
    ctl.state.side_effect = OSError("offline")
    assert ctl.call("stop", "all") == 1
    assert mutations(ctl) == []


def test_named_stop_of_an_uninstalled_furnace_skips_only_the_check(ctl):
    systemd_units(ctl, missing={"mg400-furnace.service"})
    ctl.state.side_effect = OSError("offline")
    assert ctl.call("stop", "furnace") == 0
    ctl.state.assert_not_called()
    assert mutations(ctl) == [["systemctl", "stop", "mg400-furnace.service"]]


def test_stop_all_with_no_installed_unit_runs_no_stop(ctl):
    systemd_units(ctl, missing=registered_units(ctl, True) | registered_units(ctl, False))
    assert ctl.call("stop", "all") == 0
    assert mutations(ctl) == []
    assert "No installed unit to stop." in ctl.out.getvalue()


class FurnaceHTTP:
    def __init__(
        self, payload=b'{"service":"furnace"}', status=200, content_type="application/json"
    ):
        self.payload, self.status, self.content_type = payload, status, content_type
        self.closed = False
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))

    def getresponse(self):
        return self

    def getheader(self, name, default=""):
        return self.content_type

    def read1(self, size):
        chunk, self.payload = self.payload[:size], self.payload[size:]
        return chunk

    def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "bind,host",
    [
        ("127.0.0.1", "127.0.0.1"),
        ("0.0.0.0", "127.0.0.1"),
        ("::", "::1"),
        ("::1", "::1"),
        ("192.0.2.1", "192.0.2.1"),
        ("localhost", "localhost"),
    ],
)
def test_furnace_guard_uses_configured_bind_without_proxy(ctl, bind, host, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid")
    response = FurnaceHTTP()
    factory = Mock(return_value=response)
    value = ctl.module.furnace_state(
        {
            "MG400_FURNACE_BIND": bind,
            "MG400_FURNACE_PORT": "9000",
            "MG400_FURNACE_BASE_PATH": "/heat/",
        },
        connection_factory=factory,
    )
    assert value == {"service": "furnace"}
    factory.assert_called_once_with(host, 9000, timeout=2)
    args, options = response.requests[0]
    assert args == ("GET", "/heat/api/state")
    assert "Authorization" not in options["headers"] and response.closed


@pytest.mark.parametrize(
    "settings",
    [
        {"MG400_FURNACE_BIND": "user@host"},
        {"MG400_FURNACE_BIND": "127.0.0.1/evil"},
        {"MG400_FURNACE_BIND": "remote.example"},
        {"MG400_FURNACE_BIND": "fe80::1%eth0"},
        {"MG400_FURNACE_PORT": "0"},
        {"MG400_FURNACE_BASE_PATH": "//evil"},
        {"MG400_FURNACE_BASE_PATH": "/a?x=1"},
        {"MG400_FURNACE_BASE_PATH": "/../"},
    ],
)
def test_furnace_guard_invalid_destination_never_constructs_connection(ctl, settings):
    factory = Mock(side_effect=AssertionError("network"))
    with pytest.raises(ValueError):
        ctl.module.furnace_state(
            settings,
            connection_factory=factory,
        )
    factory.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        FurnaceHTTP(status=302),
        FurnaceHTTP(content_type="text/html"),
        FurnaceHTTP(b"x" * 262145),
        FurnaceHTTP(b'{"x":1,"x":2}'),
        FurnaceHTTP(b'{"x":NaN}'),
        FurnaceHTTP(b'{"x":1e999}'),
    ],
)
def test_furnace_guard_rejects_redirects_and_malformed_status(ctl, response):
    factory = Mock(return_value=response)
    with pytest.raises(ValueError):
        ctl.module.furnace_state({}, connection_factory=factory)
    factory.assert_called_once()
    assert response.closed


def test_furnace_guard_closes_dripping_body_at_monotonic_deadline(ctl):
    response = FurnaceHTTP()
    response.read1 = Mock(return_value=b" ")
    times = iter([10, 10, 11, 12])
    with pytest.raises(ValueError, match="deadline"):
        ctl.module.furnace_state(
            {},
            connection_factory=Mock(return_value=response),
            clock=lambda: next(times),
        )
    assert response.read1.call_count == 2 and response.closed


def test_proxy_is_a_mandatory_service_and_stop_remains_available(ctl):
    assert ctl.call("start", "proxy") == 0
    assert systemctl(ctl) == [["systemctl", "start", "mg400-proxy.service"]]
    ctl.run.reset_mock()
    ctl.host.settings.write_text(SETTINGS)
    assert ctl.call("stop", "proxy") == 0
    assert systemctl(ctl) == [["systemctl", "stop", "mg400-proxy.service"]]


def test_thermal_is_optional_and_starts_only_when_selected(ctl):
    assert ctl.call("start", "thermal") == 1
    ctl.run.assert_not_called()
    ctl.host.settings.write_text(SETTINGS + "MG400_OPTIONAL_SERVICES=thermal\n")
    assert ctl.call("start", "thermal") == 0
    assert systemctl(ctl) == [["systemctl", "start", "mg400-thermal.service"]]
    ctl.run.reset_mock()
    ctl.host.settings.write_text(SETTINGS)
    assert ctl.call("stop", "thermal") == 0
    assert systemctl(ctl) == [["systemctl", "stop", "mg400-thermal.service"]]


def test_thermal_logs_use_registered_names_without_device_access(ctl):
    assert ctl.call("logs", "thermal") == 0
    assert systemctl(ctl) == [
        ["journalctl", "--no-pager", "-n", "50", "-u", "mg400-thermal.service"]
    ]


@pytest.mark.parametrize("selection", ["", "microscope,furnace"])
def test_microscope_and_furnace_start_without_selection(ctl, selection):
    ctl.host.settings.write_text(SETTINGS + f"MG400_OPTIONAL_SERVICES={selection}\n")
    assert ctl.call("start", "microscope", "furnace") == 0
    assert systemctl(ctl) == [
        ["systemctl", "start", "mg400-microscope.service", "mg400-furnace.service"]
    ]
    ctl.run.reset_mock()
    assert ctl.call("start", "microscope") == 0
    assert systemctl(ctl) == [["systemctl", "start", "mg400-microscope.service"]]
    ctl.run.reset_mock()
    ctl.host.settings.write_text(SETTINGS)
    assert ctl.call("stop", "microscope") == 0
    assert systemctl(ctl) == [["systemctl", "stop", "mg400-microscope.service"]]


def test_microscope_logs_do_not_contact_devices(ctl):
    assert ctl.call("logs", "microscope") == 0
    assert systemctl(ctl) == [
        ["journalctl", "--no-pager", "-n", "50", "-u", "mg400-microscope.service"]
    ]


@pytest.fixture
def calibration_ctl(ctl, monkeypatch):
    globals_ = ctl.module.import_microscope_calibration.__globals__
    assert globals_["CALIBRATION_OWNER"] == (1000, 1000)
    owner = (os.getuid(), os.getgid())
    monkeypatch.setitem(globals_, "CALIBRATION_OWNER", owner)
    monkeypatch.setitem(globals_, "BACKUP_OWNER", owner)
    ctl.source = ctl.host.root / "incoming"
    ctl.source.mkdir()
    ctl.destination = ctl.host.data / "calibration/microscope"
    return ctl


def import_calibration(ctl, source=None):
    return ctl.call("calibration", "import", "microscope", str(source or ctl.source))


def test_microscope_import_copies_only_allowlisted_data_with_private_modes(calibration_ctl):
    import stat

    ctl = calibration_ctl
    files = {
        "objective_views/nested/view.png": b"synthetic objective image",
        "side_camera_images/frame.jpeg": b"synthetic side image",
        "objective_reference.png": b"synthetic reference image",
        "objective_calibration.csv": b"id,value\n1,2\n",
        "trials.csv": b"id,score\n1,3\n",
    }
    for name, content in files.items():
        path = ctl.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for name in ("current_objective.txt", "unrelated.py", "upper.CSV"):
        (ctl.source / name).write_bytes(b"not calibration data")
    old_umask = os.umask(0)
    try:
        assert import_calibration(ctl) == 0
    finally:
        os.umask(old_umask)
    assert {
        str(path.relative_to(ctl.destination))
        for path in ctl.destination.rglob("*")
        if path.is_file()
    } == set(files)
    for path in [ctl.destination, *ctl.destination.rglob("*")]:
        assert (path.stat().st_uid, path.stat().st_gid) == (os.getuid(), os.getgid())
        assert stat.S_IMODE(path.stat().st_mode) == (0o750 if path.is_dir() else 0o640)
    for name, content in files.items():
        assert (ctl.destination / name).read_bytes() == content
        assert f"Copied {ctl.source / name} -> {ctl.destination / name}" in ctl.out.getvalue()
    assert "Imported 5 microscope calibration files." in ctl.out.getvalue()
    assert "current_objective" not in ctl.out.getvalue()
    assert not list(ctl.destination.rglob(".mg400-calibration-*"))
    ctl.run.assert_not_called()
    ctl.execvpe.assert_not_called()


def test_microscope_import_uses_host_setting_under_fake_root_not_environment(
    calibration_ctl, monkeypatch
):
    ctl = calibration_ctl
    (ctl.source / "one.csv").write_bytes(b"data")
    ctl.host.settings.write_text(
        SETTINGS + "MG400_MICROSCOPE_CALIBRATION_DIR=/custom/calibration\n"
    )
    monkeypatch.setenv("MG400_MICROSCOPE_CALIBRATION_DIR", "/ignored/environment")
    assert import_calibration(ctl) == 0
    assert (ctl.host.root / "custom/calibration/one.csv").read_bytes() == b"data"
    assert not ctl.destination.exists()
    assert not (ctl.host.root / "ignored").exists()


@pytest.mark.parametrize("setting", ["/", "relative/path", "/var/lib/../outside"])
def test_microscope_import_rejects_unsafe_configured_path_before_writes(calibration_ctl, setting):
    ctl = calibration_ctl
    (ctl.source / "one.csv").write_bytes(b"data")
    ctl.host.settings.write_text(SETTINGS + f"MG400_MICROSCOPE_CALIBRATION_DIR={setting}\n")
    assert import_calibration(ctl) == 1
    assert not ctl.destination.exists()


@pytest.mark.parametrize(
    "kind",
    [
        "source-root",
        "source-parent",
        "source-file",
        "source-tree",
        "nested",
        "fifo",
        "hardlink",
        "csv-directory",
    ],
)
def test_microscope_import_refuses_unsafe_selected_sources(calibration_ctl, kind):
    ctl = calibration_ctl
    outside = ctl.host.root / "outside"
    outside.mkdir()
    secret = outside / "private.txt"
    secret.write_bytes(b"private synthetic content")
    source = ctl.source
    if kind == "source-root":
        alias = ctl.host.root / "alias"
        alias.symlink_to(source, target_is_directory=True)
        source = alias
        (ctl.source / "one.csv").write_bytes(b"data")
    elif kind == "source-parent":
        alias = ctl.host.root / "alias"
        alias.symlink_to(ctl.host.root, target_is_directory=True)
        source = alias / "incoming"
        (ctl.source / "one.csv").write_bytes(b"data")
    elif kind == "source-file":
        (source / "one.csv").symlink_to(secret)
    elif kind == "source-tree":
        (source / "objective_views").symlink_to(outside, target_is_directory=True)
    elif kind == "nested":
        (source / "objective_views").mkdir()
        (source / "objective_views/view.png").symlink_to(secret)
    elif kind == "fifo":
        os.mkfifo(source / "one.csv")
    elif kind == "hardlink":
        os.link(secret, source / "one.csv")
    else:
        (source / "one.csv").mkdir()
    assert import_calibration(ctl, source) == 1
    assert not ctl.destination.exists()
    assert secret.read_bytes() == b"private synthetic content"
    ctl.run.assert_not_called()


@pytest.mark.parametrize(
    "kind", ["collision", "symlink-file", "symlink-directory", "unsafe-mode", "wrong-owner"]
)
def test_microscope_import_preflights_all_destinations_before_copy(
    calibration_ctl, monkeypatch, kind
):
    ctl = calibration_ctl
    (ctl.source / "a.csv").write_bytes(b"first")
    (ctl.source / "z.csv").write_bytes(b"last")
    ctl.destination.mkdir(parents=True, mode=0o750)
    outside = ctl.host.root / "outside"
    outside.mkdir()
    preserved = outside / "preserved"
    preserved.write_bytes(b"keep")
    if kind == "collision":
        (ctl.destination / "z.csv").write_bytes(b"old")
    elif kind == "symlink-file":
        (ctl.destination / "z.csv").symlink_to(preserved)
    elif kind == "symlink-directory":
        ctl.destination.rmdir()
        ctl.destination.symlink_to(outside, target_is_directory=True)
    elif kind == "unsafe-mode":
        ctl.destination.chmod(0o777)
    else:
        monkeypatch.setitem(
            ctl.module.import_microscope_calibration.__globals__,
            "CALIBRATION_OWNER",
            (99999, 99999),
        )
    assert import_calibration(ctl) == 1
    assert not (ctl.destination / "a.csv").exists()
    assert preserved.read_bytes() == b"keep"
    assert not list(ctl.destination.rglob(".mg400-calibration-*"))


@pytest.mark.parametrize(
    "limit",
    [
        "CALIBRATION_MAX_FILES",
        "CALIBRATION_MAX_ENTRIES",
        "CALIBRATION_MAX_FILE_BYTES",
        "CALIBRATION_MAX_BYTES",
        "CALIBRATION_MAX_DEPTH",
    ],
)
def test_microscope_import_bounds_preflight_before_writes(calibration_ctl, monkeypatch, limit):
    ctl = calibration_ctl
    (ctl.source / "one.csv").write_bytes(b"data")
    monkeypatch.setitem(ctl.module.import_microscope_calibration.__globals__, limit, 0)
    assert import_calibration(ctl) == 1
    assert not ctl.destination.exists()


def test_microscope_import_refuses_empty_overlap_and_parent_traversal(calibration_ctl):
    ctl = calibration_ctl
    assert import_calibration(ctl) == 1
    (ctl.source / "one.csv").write_bytes(b"data")
    assert import_calibration(ctl, ctl.source / ".." / "incoming") == 1
    ctl.host.settings.write_text(SETTINGS + "MG400_MICROSCOPE_CALIBRATION_DIR=/incoming/nested\n")
    assert import_calibration(ctl) == 1
    assert not ctl.destination.exists()


def test_microscope_import_requires_root(calibration_ctl, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    assert import_calibration(calibration_ctl) == 1
    assert not calibration_ctl.destination.exists()


@pytest.mark.parametrize("failure", ["source-swapped", "fsync", "ownership", "destination-race"])
def test_microscope_import_failures_preserve_targets_and_remove_staging(
    calibration_ctl, monkeypatch, capsys, failure
):
    ctl = calibration_ctl
    original = ctl.source / "one.csv"
    original.write_bytes(b"data")
    outside = ctl.host.root / "outside"
    outside.write_bytes(b"keep")
    globals_ = ctl.module.import_microscope_calibration.__globals__
    if failure == "source-swapped":
        open_source = globals_["_calibration_source"]

        def swapped(*args):
            original.unlink()
            original.symlink_to(outside)
            return open_source(*args)

        monkeypatch.setitem(globals_, "_calibration_source", swapped)
    elif failure == "fsync":
        monkeypatch.setattr(os, "fsync", Mock(side_effect=OSError("injected sync failure")))
    elif failure == "ownership":
        monkeypatch.setattr(os, "fchown", Mock(side_effect=OSError("injected ownership failure")))
    else:
        link = os.link

        def collide(*args, **kwargs):
            (ctl.destination / "one.csv").write_bytes(b"concurrent calibration")
            return link(*args, **kwargs)

        monkeypatch.setattr(os, "link", collide)
    assert import_calibration(ctl) == 1
    assert "Imported" not in ctl.out.getvalue()
    assert "Inspect the destination before retrying" in capsys.readouterr().err
    if failure == "destination-race":
        assert (ctl.destination / "one.csv").read_bytes() == b"concurrent calibration"
    else:
        assert not (ctl.destination / "one.csv").exists()
    assert outside.read_bytes() == b"keep"
    assert not list(ctl.host.root.rglob(".mg400-calibration-*"))
