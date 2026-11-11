"""Check the host installer in a temporary root, without systemd, uv or root rights."""

import hashlib
import io
import os
import runpy
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy/install.py"
DEVICES = [
    f"/dev/v4l/by-path/pci-0000:80:14.0-usb-0:12.1.{port}:1.0-video-index0" for port in (2, 3, 4)
]


@pytest.fixture
def module(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=AssertionError("No real commands")))
    return SimpleNamespace(**runpy.run_path(str(INSTALLER), run_name="test_deploy_install"))


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def host(module, monkeypatch, tmp_path, tmp_path_factory):
    code = tmp_path / "opt/mg400"
    (code / "deploy").mkdir(parents=True)
    shutil.copy(ROOT / "deploy/mg400.env.example", code / "deploy")
    shutil.copy(ROOT / "deploy/services.toml", code / "deploy")
    # The tests write their own asset manifests, so the copy leaves out the console model asset.
    registry = code / "deploy/services.toml"
    registry.write_text(registry.read_text().replace('assets = ["twin-model"]', "assets = []"))
    shutil.copytree(ROOT / "config/systemd", code / "config/systemd")
    shutil.copytree(ROOT / "config/proxy", code / "config/proxy")
    commands = []

    def run(command, **options):
        commands.append((command, options))
        if command[1:3] == ["-c", "import sys; print(sys.version_info >= (3, 12))"]:
            return SimpleNamespace(returncode=0, stdout="True\n", stderr="")
        if command[:2] == ["systemctl", "show"]:
            return SimpleNamespace(returncode=0, stdout="MainPID=0\n", stderr="")
        if command[:1] == ["git"]:
            return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    globals_ = module.main.__globals__
    # Model ffmpeg on the service PATH. A test removes a tool to see the notice.
    tools = tmp_path_factory.mktemp("tools")
    for name in ("ffmpeg", "ffprobe"):
        (tools / name).write_text("#!/bin/sh\nexit 1\n")
        (tools / name).chmod(0o755)
    monkeypatch.setitem(globals_, "SERVICE_PATH", str(tools))
    monkeypatch.setattr(globals_["sys"], "platform", "linux")
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    # The microscope is a default service. Model its account and device groups.
    monkeypatch.setattr(
        globals_["grp"],
        "getgrnam",
        lambda name: SimpleNamespace(gr_gid={"dialout": 20, "video": 44}[name]),
    )
    monkeypatch.setattr(globals_["pwd"], "getpwuid", lambda uid: SimpleNamespace(pw_name="lab1000"))
    monkeypatch.setattr(globals_["os"], "getgrouplist", lambda name, gid: [gid, 27])
    owners = {}

    def chown(fd, uid, gid):
        # The installer changes owners only through descriptors, never by path.
        assert isinstance(fd, int)
        owners[os.fstat(fd).st_ino] = (uid, gid)

    out = io.StringIO()

    caddy = tmp_path_factory.mktemp("caddy") / "caddy"
    caddy.write_bytes(b"offline Caddy fixture; never execute")

    def install(*argv, runner=None, caddy_binary=True):
        # The proxy is mandatory. Supply a Caddy binary unless the test gives its own.
        extra = ["--caddy", str(caddy)] if caddy_binary and "--caddy" not in argv else []
        return module.main(
            ["--uv", "/usr/bin/uv", *argv, *extra],
            host=module.Host(tmp_path, source=code),
            run=runner or run,
            chown=chown,
            out=out,
        )

    return SimpleNamespace(
        root=tmp_path,
        code=code,
        install=install,
        commands=commands,
        owner=lambda path: owners.get(path.stat().st_ino),
        out=out,
        tools=tools,
        settings=tmp_path / "etc/mg400/mg400.env",
        data=tmp_path / "var/lib/mg400",
        units=tmp_path / "etc/systemd/system",
    )


def names(commands):
    return [command for command, _ in commands]


@pytest.fixture
def toupcam_library(host, module, monkeypatch):
    library = host.root / "supplied-libtoupcam.so"
    library.write_bytes(b"synthetic SDK bytes; never load this file")
    monkeypatch.setitem(
        module.main.__globals__, "TOUPCAM_SHA256", hashlib.sha256(library.read_bytes()).hexdigest()
    )
    return library


def root_owned_directories(module, monkeypatch, *directories):
    """Model directories that root created, also when the tests do not run as root."""
    inodes = {directory.stat().st_ino for directory in directories}
    original_fstat = os.fstat

    def fstat(fd):
        info = original_fstat(fd)
        if info.st_ino not in inodes:
            return info
        values = list(info)
        values[4] = values[5] = 0
        return os.stat_result(values)

    monkeypatch.setattr(module.main.__globals__["os"], "fstat", fstat)


def test_toupcam_installs_verified_snapshot(host, module, monkeypatch, toupcam_library):
    original = toupcam_library.read_bytes()
    target = host.root / "usr/local/lib/mg400/libtoupcam.so"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old SDK")
    target.chmod(0o600)
    directories = ("usr", "usr/local", "usr/local/lib", "usr/local/lib/mg400")
    root_owned_directories(module, monkeypatch, *(host.root / name for name in directories))

    def run(command, **options):
        host.commands.append((command, options))
        if command[:2] == ["/usr/bin/uv", "sync"]:
            toupcam_library.write_bytes(b"changed after read-only checks")
        return SimpleNamespace(returncode=0, stdout="True\n", stderr="")

    assert host.install("--toupcam", str(toupcam_library), runner=run) == 0
    assert target.read_bytes() == original
    assert mode(target) == 0o755 and host.owner(target) == (0, 0)
    assert mode(target.parent) == 0o755 and host.owner(target.parent) == (0, 0)
    assert toupcam_library.read_bytes() != original
    assert not any("toupcam" in command[0] for command in names(host.commands))


@pytest.mark.parametrize("dry_run", [False, True])
def test_toupcam_refuses_unverified_sdk_before_changes(host, module, capsys, dry_run):
    library = host.root / "unverified.so"
    library.write_bytes(b"wrong SDK version")
    # The production pin must match the owner's H-12 linux/x64 artifact.
    assert module.TOUPCAM_VERSION == "59.30594.20260120"
    assert module.TOUPCAM_SHA256 == (
        "5ba51b66bb9e2901e769fa1d5d5ae88c359269ea7fb20358335462c531a3e997"
    )
    args = ["--dry-run"] if dry_run else []
    assert host.install(*args, "--toupcam", str(library)) == 1
    assert "SHA-256 mismatch" in capsys.readouterr().err
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()
    assert not (host.root / "usr").exists()


@pytest.mark.parametrize("source_kind", ["missing", "empty", "directory", "symlink", "fifo"])
def test_toupcam_refuses_invalid_sources_before_changes(host, toupcam_library, source_kind):
    original = toupcam_library.read_bytes()
    toupcam_library.unlink()
    if source_kind == "empty":
        toupcam_library.touch()
    elif source_kind == "directory":
        toupcam_library.mkdir()
    elif source_kind == "symlink":
        real_file = host.root / "real-library"
        real_file.write_bytes(original)
        toupcam_library.symlink_to(real_file)
    elif source_kind == "fifo":
        os.mkfifo(toupcam_library)
    assert host.install("--toupcam", str(toupcam_library)) == 1
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()


def test_toupcam_refuses_symlinked_source_parent(host, toupcam_library):
    alias = host.root / "source-alias"
    alias.symlink_to(host.root, target_is_directory=True)
    assert host.install("--toupcam", str(alias / toupcam_library.name)) == 1
    assert not host.settings.exists() and not host.data.exists()


@pytest.mark.parametrize("destination_kind", ["symlink", "directory", "fifo", "parent_symlink"])
def test_toupcam_refuses_unsafe_destination_before_changes(host, toupcam_library, destination_kind):
    target = host.root / "usr/local/lib/mg400/libtoupcam.so"
    outside = host.root / "untouched"
    outside.mkdir()
    marker = outside / "libtoupcam.so"
    marker.write_bytes(b"preserve")
    target.parent.parent.mkdir(parents=True)
    if destination_kind == "parent_symlink":
        target.parent.symlink_to(outside, target_is_directory=True)
    else:
        target.parent.mkdir()
        if destination_kind == "symlink":
            target.symlink_to(marker)
        elif destination_kind == "directory":
            target.mkdir()
        else:
            os.mkfifo(target)
    assert host.install("--toupcam", str(toupcam_library)) == 1
    assert marker.read_bytes() == b"preserve"
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()


def test_toupcam_dry_run_validates_and_lists_without_changes(host, toupcam_library):
    assert host.install("--dry-run", "--toupcam", str(toupcam_library)) == 0
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()
    assert not (host.root / "usr").exists()
    assert "Would install Toupcam 59.30594.20260120" in host.out.getvalue()
    assert all(command[0] in {"/usr/bin/python3", "systemctl"} for command in names(host.commands))


def test_first_install_creates_settings_data_units_and_command(module, host):
    assert host.install() == 0

    settings = module.parse_settings(host.settings.read_text())
    example = module.parse_settings((ROOT / "deploy/mg400.env.example").read_text())
    assert settings == example
    assert not any(key.endswith("_TOKEN") for key in settings)
    assert mode(host.settings) == 0o600
    assert [(path.name, mode(path)) for path in sorted(host.data.rglob("*")) if path.is_dir()] == [
        ("calibration", 0o750),
        ("cameras", 0o700),
        ("microscope", 0o750),
        ("camera", 0o700),
        ("furnace", 0o750),
        ("locks", 0o700),
        ("recordings", 0o750),
        ("robot", 0o750),
        ("runs", 0o750),
        ("stage", 0o700),
        ("work", 0o750),
        ("zones", 0o750),
    ]
    assert mode(host.data) == 0o750
    assert not (host.data / "camera/preview.key").exists()
    assert {path.name for path in host.units.iterdir()} == {
        path.name
        for path in (ROOT / "config/systemd").iterdir()
        if path.name
        not in {
            "mg400-thermal.service",
            "mg400-vision.service",
        }
    }
    for unit in host.units.iterdir():
        assert unit.read_text() == (ROOT / "config/systemd" / unit.name).read_text()
    command = host.root / "usr/local/bin/mg400ctl"
    assert command.is_symlink() and command.readlink() == host.code / "deploy/mg400ctl"
    lab_command = host.root / "usr/local/bin/labctl"
    assert lab_command.is_symlink() and lab_command.readlink() == command.readlink()
    assert not (host.root / "etc/mg400/go2rtc.yaml").exists()

    uv = [options for command, options in host.commands if command[0] == "/usr/bin/uv"]
    assert names(host.commands) == [
        ["/usr/bin/python3", "-c", "import sys; print(sys.version_info >= (3, 12))"],
        [
            "/usr/bin/uv",
            "sync",
            "--locked",
            "--no-default-groups",
            "--group",
            "furnace",
            "--group",
            "microscope",
            "--group",
            "orchestrator",
            "--group",
            "stage",
            "--group",
            "tools",
            "--python",
            "/usr/bin/python3",
        ],
        ["systemctl", "daemon-reload"],
        ["git", "-C", str(host.code), "rev-parse", "--short", "HEAD"],
    ]
    assert uv[0]["cwd"] == host.code and uv[0]["env"]["UV_LINK_MODE"] == "copy"
    assert host.owner(host.code) == host.owner(host.data) == (0, 1000)
    for name in (
        "locks",
        "work",
        "camera",
        "robot",
        "stage",
        "calibration",
        "calibration/cameras",
        "calibration/microscope",
        "furnace",
        "recordings",
        "runs",
    ):
        assert host.owner(host.data / name) == (1000, 1000), name
    assert host.owner(host.settings) == (0, 0)
    report = host.out.getvalue()
    assert "Installed commit abc1234." in report
    assert "Cameras are off" in report
    assert "The Toupcam library is not installed" in report
    assert "No service was started, stopped or restarted" in report
    # The installer never starts, stops or restarts a robot service.
    assert not [command for command in names(host.commands) if "start" in command[1:2]]


def test_repeated_install_keeps_settings(module, host):
    assert host.install() == 0
    settings = host.settings.read_text()
    assert host.install() == 0
    assert host.settings.read_text() == settings
    assert str(host.settings) not in host.out.getvalue().split("Installed")[1]


def test_existing_settings_keep_their_values_and_gain_missing_keys(module, host):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(
        "# Olares\nMG400_STAGE_BIND=100.0.0.1\nMG400_STAGE_TOKEN=x\nMG400_STAGE_SERIAL=/dev/x\n"
    )
    assert host.install() == 0
    text = host.settings.read_text()
    settings = module.parse_settings(text)
    assert text.startswith("# Olares\nMG400_STAGE_BIND=100.0.0.1\nMG400_STAGE_SERIAL=/dev/x\n")
    assert settings["MG400_STAGE_SERIAL"] == "/dev/x"
    assert "MG400_STAGE_TOKEN" not in settings
    assert settings["MG400_PROXY_PORT"] == "8080"
    assert text.count("MG400_STAGE_BIND=") == 1
    assert f"update {host.settings}: MG400_STAGE_TOKEN, MG400_ROBOT_HOST" in host.out.getvalue()


def test_merge_keeps_set_values_comments_and_quotes(module):
    example = "# Example\nA=1\nC=3\n"
    current = '# Mine\nA="custom value"\nB=keep\n'
    text, changed = module.merge_settings(current, example)
    assert text == '# Mine\nA="custom value"\nB=keep\nC=3\n'
    assert changed == ["C"]
    assert module.parse_settings(text) == {"A": "custom value", "B": "keep", "C": "3"}
    assert module.merge_settings(None, example) == (example, [])


def test_merge_removes_service_tokens_and_empty_removed_settings(module):
    example = "A=\n"
    current = (
        "A=\nMG400_PANEL_TOKEN=old\nMG400_ORCH_ROBOT_TOKEN=\nMG400_ORCH_ROBOT_URL=\n"
        "MG400_STAGE_BASE_PATH=/stage/\nMG400_GATEWAY_TOKEN=old\n"
    )
    text, changed = module.merge_settings(current, example)
    assert text == "A=\nMG400_STAGE_BASE_PATH=/stage/\n"
    assert changed == [
        "MG400_PANEL_TOKEN",
        "MG400_ORCH_ROBOT_TOKEN",
        "MG400_ORCH_ROBOT_URL",
        "MG400_GATEWAY_TOKEN",
    ]


def test_cameras_install_go2rtc_and_render_three_loopback_streams(module, host, tmp_path):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(f"MG400_CAMERA_DEVICES={','.join(DEVICES)}\n")
    binary = tmp_path / "go2rtc_linux_amd64"
    binary.write_bytes(b"\x7fELF test binary")
    assert host.install("--go2rtc", str(binary)) == 0
    installed = host.root / "usr/local/lib/mg400/go2rtc"
    assert installed.read_bytes() == binary.read_bytes() and mode(installed) == 0o755
    config = host.root / "etc/mg400/go2rtc.yaml"
    assert mode(config) == 0o640
    # The offline runner accepts the NVENC probe.
    assert config.read_text() == module.render_go2rtc(DEVICES, nvenc=True)
    assert config.read_text().startswith("api:\n  listen: 127.0.0.1:1984\n")
    assert (
        f"  camera3: v4l2:device?video={DEVICES[2]}"
        "&input_format=mjpeg&video_size=1280x720&framerate=30\n"
    ) in config.read_text()
    # A later install keeps the installed binary without --go2rtc.
    assert host.install() == 0
    assert installed.read_bytes() == binary.read_bytes()


@pytest.mark.parametrize(
    ("devices", "argv", "message"),
    [
        (DEVICES, [], "Pass the go2rtc 1.9.14 binary"),
        ([*DEVICES, *DEVICES[:2]], [], "must list 1 to 4 devices"),
        ([*DEVICES[:2], "/dev/video0&exec=x"], [], "Invalid camera device path"),
    ],
    ids=["no-binary", "five-cameras", "unsafe-path"],
)
def test_camera_errors_stop_before_any_change(host, capsys, devices, argv, message):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(f"MG400_CAMERA_DEVICES={','.join(devices)}\n")
    before = host.settings.read_text()
    assert host.install(*argv) == 1
    assert message in capsys.readouterr().err
    assert host.settings.read_text() == before
    assert not host.data.exists() and not host.units.exists()


@pytest.mark.parametrize(("platform", "uid"), [("darwin", 0), ("linux", 1000)])
def test_installer_requires_linux_root_before_any_command(module, host, monkeypatch, platform, uid):
    globals_ = module.main.__globals__
    monkeypatch.setattr(globals_["sys"], "platform", platform)
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: uid)
    assert host.install() == 1
    assert host.commands == [] and not host.settings.exists()


def test_installer_runs_only_from_the_install_checkout(module, tmp_path, monkeypatch, capsys):
    globals_ = module.main.__globals__
    monkeypatch.setattr(globals_["sys"], "platform", "linux")
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    run = Mock()
    assert module.main([], host=module.Host(tmp_path), run=run, chown=Mock()) == 1
    assert "Run the installer from the checkout at" in capsys.readouterr().err
    run.assert_not_called()


def test_old_python_or_missing_uv_stops_before_any_change(module, host, monkeypatch, capsys):
    assert (
        module.main(
            ["--python", "/usr/bin/python3"],
            host=module.Host(host.root, source=host.code),
            run=Mock(return_value=SimpleNamespace(returncode=0, stdout="False\n")),
            chown=Mock(),
        )
        == 1
    )
    assert "must be Python 3.12 or newer" in capsys.readouterr().err
    monkeypatch.setitem(module.main.__globals__, "shutil", Mock(which=Mock(return_value=None)))
    assert (
        module.main(
            [],
            host=module.Host(host.root, source=host.code),
            run=Mock(return_value=SimpleNamespace(returncode=0, stdout="True\n")),
            chown=Mock(),
        )
        == 1
    )
    assert "Install uv first" in capsys.readouterr().err
    assert not host.settings.exists() and not host.data.exists()


def test_dry_run_changes_nothing_and_runs_only_checks(host):
    assert host.install("--dry-run") == 0
    assert sorted(path.relative_to(host.root).parts[0] for path in host.root.iterdir()) == ["opt"]
    assert [command[0] for command in names(host.commands)] == ["/usr/bin/python3"]
    report = host.out.getvalue()
    assert "Would run: /usr/bin/uv sync" in report
    assert f"Would ensure {host.data / 'robot'} (0750, 1000:1000)" in report


def test_failed_dependency_sync_installs_no_units(host):
    def failing(command, **options):
        host.commands.append((command, options))
        if command[0] == "/usr/bin/uv":
            raise subprocess.CalledProcessError(1, command)
        stdout = "True\n" if command[0].endswith("python3") else "MainPID=0\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    assert host.install(runner=failing) == 1
    assert not host.units.exists()
    assert not (host.root / "usr/local/bin/mg400ctl").exists()


def test_settings_lines_parse_like_systemd(module):
    text = "A = 1\nB=\"x y\"\n; comment\n  # comment\nC='z'\nA=2\n"
    assert module.parse_settings(text) == {"A": "2", "B": "x y", "C": "z"}


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("MG400_PANEL_BIND 0.0.0.0", "is not KEY=VALUE"),
        ("1KEY=x", "is not KEY=VALUE"),
        ('KEY="a\\"b"', "remove quotes and backslashes"),
        ("KEY=a\\b", "remove quotes and backslashes"),
        ("KEY=it's", "remove quotes and backslashes"),
    ],
)
def test_settings_lines_that_systemd_could_read_differently_are_refused(module, line, message):
    with pytest.raises(ValueError, match=message):
        module.parse_settings(f"# ok\n{line}\n")


def test_merge_sees_spaced_keys(module):
    example = "MG400_ROBOT_HOST=\nB=\n"
    current = 'MG400_ROBOT_HOST = robot.test\nB=""\n'
    text, changed = module.merge_settings(current, example)
    assert text == current and changed == []


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ("MG400_PANEL_BIND 0.0.0.0\n", "is not KEY=VALUE"),
    ],
    ids=["unreadable-line"],
)
def test_unsafe_settings_stop_before_any_change(host, capsys, settings, message):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(settings)
    assert host.install() == 1
    assert message in capsys.readouterr().err
    assert host.settings.read_text() == settings
    assert not host.data.exists() and not host.units.exists()


def test_missing_go2rtc_file_stops_before_any_change(host, capsys):
    assert host.install("--go2rtc", str(host.root / "missing")) == 1
    assert "go2rtc binary does not exist" in capsys.readouterr().err
    assert not host.settings.exists() and not host.data.exists()


def test_turning_cameras_off_removes_the_camera_configuration(host, tmp_path):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(f"MG400_CAMERA_DEVICES={','.join(DEVICES)}\n")
    binary = tmp_path / "go2rtc"
    binary.write_bytes(b"binary")
    assert host.install("--go2rtc", str(binary)) == 0
    config = host.root / "etc/mg400/go2rtc.yaml"
    assert config.exists()
    host.settings.write_text(host.settings.read_text().replace(",".join(DEVICES), ""))
    assert host.install() == 0
    assert not config.exists()
    assert f"remove {config}: cameras are off" in host.out.getvalue()


def test_symbolic_link_in_the_data_directory_is_refused(host, capsys, tmp_path):
    assert host.install() == 0
    victim = tmp_path / "victim"
    victim.mkdir(mode=0o755)
    shutil.rmtree(host.data / "work")
    (host.data / "work").symlink_to(victim)
    assert host.install() == 1
    assert "is not a directory" in capsys.readouterr().err
    assert mode(victim) == 0o755 and host.owner(victim) is None


def test_settings_rights_are_repaired_on_every_run(host):
    assert host.install() == 0
    os.chmod(host.settings, 0o644)
    assert host.install() == 0
    assert mode(host.settings) == 0o600 and host.owner(host.settings) == (0, 0)


@pytest.fixture
def local_asset(host, tmp_path):
    path = tmp_path / "offline-model.dat"
    path.write_bytes(b"offline model fixture")
    (host.code / "config/assets.toml").write_text(
        '[model]\nurl = "https://assets.example/model"\n'
        f'sha256 = "{hashlib.sha256(path.read_bytes()).hexdigest()}"\n'
        f"bytes = {path.stat().st_size}\n"
        'install = "/var/lib/mg400/models/model.dat"\nmode = "0640"\n'
        'service = "web"\nsource = "test fixture"\nlicense = "AGPL-3.0"\n'
    )
    registry = host.code / "deploy/services.toml"
    registry.write_text(registry.read_text().replace("assets = []", 'assets = ["model"]', 1))
    return path


def test_offline_asset_installs_with_service_permissions(host, local_asset):
    assert host.install("--asset", f"model={local_asset}") == 0
    target = host.data / "models/model.dat"
    assert target.read_bytes() == local_asset.read_bytes()
    assert mode(target) == 0o640 and host.owner(target) == (1000, 1000)
    assert mode(target.parent) == 0o750 and host.owner(target.parent) == (1000, 1000)
    assert host.owner(host.data) == (0, 1000)
    assert "install asset model" in host.out.getvalue()


@pytest.mark.parametrize("offline", [False, True])
def test_fetch_assets_and_offline_override(module, host, local_asset, monkeypatch, offline):
    opener = Mock(return_value=io.BytesIO(local_asset.read_bytes()))
    monkeypatch.setattr(module.main.__globals__["assets"], "open_download", opener)
    argv = ["--fetch-assets"]
    if offline:
        argv += ["--asset", f"model={local_asset}"]
    assert host.install(*argv) == 0
    assert (host.data / "models/model.dat").read_bytes() == local_asset.read_bytes()
    assert opener.call_count == (0 if offline else 1)


@pytest.mark.parametrize("local", [False, True])
def test_asset_dry_run_lists_verification_without_writes_or_downloads(
    module, host, local_asset, monkeypatch, local
):
    opener = Mock(side_effect=AssertionError("Dry run must not download"))
    monkeypatch.setattr(module.main.__globals__["assets"], "open_download", opener)
    argv = ["--asset", f"model={local_asset}"] if local else ["--fetch-assets"]
    assert host.install("--dry-run", *argv) == 0
    assert not host.settings.exists() and not host.data.exists()
    assert "Would install asset model" in host.out.getvalue()
    assert "SHA-256" in host.out.getvalue()
    assert "0640, 1000:1000" in host.out.getvalue()
    opener.assert_not_called()


@pytest.mark.parametrize("specification", ["unknown=fixture", "model", "model="])
def test_bad_asset_argument_stops_before_any_host_change(host, local_asset, specification):
    assert host.install("--asset", specification) == 1
    assert not host.settings.exists() and not host.data.exists()
    assert len(host.commands) == 1


def test_duplicate_asset_stops_before_any_host_change(host, local_asset):
    value = f"model={local_asset}"
    assert host.install("--asset", value, "--asset", value) == 1
    assert not host.settings.exists() and not host.data.exists()


def test_bad_local_asset_stops_before_any_host_change(host, local_asset):
    local_asset.write_bytes(b"corrupt")
    assert host.install("--asset", f"model={local_asset}") == 1
    assert not host.settings.exists() and not host.data.exists()
    assert len(host.commands) == 1


def test_asset_destination_link_is_refused_before_host_changes(host, local_asset, tmp_path):
    target = host.data / "models/model.dat"
    target.parent.mkdir(parents=True)
    sentinel = tmp_path / "keep.dat"
    sentinel.write_bytes(b"keep")
    target.symlink_to(sentinel)
    assert host.install("--asset", f"model={local_asset}") == 1
    assert not host.settings.exists()
    assert target.is_symlink() and sentinel.read_bytes() == b"keep"
    assert len(host.commands) == 1


def add_optional_service(
    host, *, name="optional", venv=".venv-vision", groups=("vision",), asset_names=()
):
    import json

    path = host.code / "deploy/services.toml"
    with path.open("a") as stream:
        stream.write(
            f'\n[[service]]\nname = "{name}"\nunits = ["mg400-{name}.service"]\n'
            f'optional = true\nvenv = "{venv}"\ngroups = {json.dumps(list(groups))}\n'
            f"port = 8799\n"
            f"assets = {json.dumps(list(asset_names))}\n"
        )
    (host.code / "config/systemd" / f"mg400-{name}.service").write_text("[Service]\n")


def select_optional(host, value):
    example = host.code / "deploy/mg400.env.example"
    example.write_text(
        example.read_text().replace("MG400_OPTIONAL_SERVICES=", f"MG400_OPTIONAL_SERVICES={value}")
    )


@pytest.fixture
def vision_asset(host, module, monkeypatch):
    source = host.root / "offline-vision.dat"
    source.write_bytes(b"synthetic vision model; never load as a model")
    (host.code / "config/assets.toml").write_text(
        '[vision-weights]\nurl = "https://assets.example/vision"\n'
        f'sha256 = "{hashlib.sha256(source.read_bytes()).hexdigest()}"\n'
        f"bytes = {source.stat().st_size}\n"
        'install = "/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt"\n'
        'mode = "0640"\nservice = "vision"\nsource = "test fixture"\nlicense = "AGPL-3.0"\n'
    )
    opener = Mock(side_effect=lambda *_args, **_kwargs: io.BytesIO(source.read_bytes()))
    monkeypatch.setattr(module.main.__globals__["assets"], "open_download", opener)
    return source, opener


@pytest.mark.parametrize("offline", [False, True])
def test_vision_selection_installs_isolated_runtime_required_weights_and_private_data(
    module, host, vision_asset, monkeypatch, offline
):
    source, opener = vision_asset
    select_optional(host, "vision")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/unrelated/environment")
    arguments = ["--asset", f"vision-weights={source}"] if offline else []
    assert host.install(*arguments) == 0
    syncs = [(cmd, opts) for cmd, opts in host.commands if cmd[0] == "/usr/bin/uv"]
    assert len(syncs) == 2
    assert "vision-gpu" not in syncs[0][0]
    assert syncs[1][0] == [
        "/usr/bin/uv",
        "sync",
        "--locked",
        "--no-default-groups",
        "--group",
        "vision",
        "--group",
        "vision-gpu",
        "--python",
        "/usr/bin/python3",
    ]
    assert syncs[1][1]["env"]["UV_PROJECT_ENVIRONMENT"] == str(host.code / ".venv-vision")
    assert syncs[1][1]["cwd"] == host.code
    assert mode(host.data / "vision") == 0o750
    assert host.owner(host.data / "vision") == (1000, 1000)
    target = host.data / "models/chip-arm-yolo26s-seg-v1.pt"
    assert target.read_bytes() == source.read_bytes()
    assert mode(target) == 0o640 and host.owner(target) == (1000, 1000)
    assert opener.call_count == (0 if offline else 1)
    assert (host.units / "mg400-vision.service").read_bytes() == (
        ROOT / "config/systemd/mg400-vision.service"
    ).read_bytes()
    assert not any(
        cmd[:2] in (["systemctl", "start"], ["systemctl", "restart"]) for cmd, _ in host.commands
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_vision_dry_run_is_conditional_and_never_syncs_downloads_or_writes(
    host, vision_asset, enabled
):
    _, opener = vision_asset
    if enabled:
        select_optional(host, "vision")
    assert host.install("--dry-run") == 0
    report = host.out.getvalue()
    for expected in (
        ".venv-vision",
        "--group vision --group vision-gpu",
        "install asset vision-weights",
        "/var/lib/mg400/vision (0750, 1000:1000)",
        "mg400-vision.service",
    ):
        assert (expected in report) is enabled
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()
    assert len(host.commands) == 1  # Injected Python-version read-only check.
    opener.assert_not_called()


def test_disabled_vision_never_installs_model_or_gpu_group_even_with_fetch_assets(
    host, vision_asset
):
    _, opener = vision_asset
    assert host.install("--fetch-assets") == 0
    assert not (host.data / "vision").exists()
    assert not (host.data / "models").exists()
    assert not (host.units / "mg400-vision.service").exists()
    assert all("vision-gpu" not in command for command, _ in host.commands)
    opener.assert_not_called()


def test_vision_refuses_bad_local_weights_before_any_host_mutation(host, vision_asset):
    source, opener = vision_asset
    select_optional(host, "vision")
    source.write_bytes(b"corrupt")
    assert host.install("--asset", f"vision-weights={source}") == 1
    assert not host.settings.exists() and not host.data.exists()
    opener.assert_not_called()


def test_vision_reinstall_keeps_verified_weights_without_a_download(
    module, host, vision_asset, monkeypatch
):
    source, opener = vision_asset
    select_optional(host, "vision")
    assert host.install() == 0
    assert opener.call_count == 1
    target = host.data / "models/chip-arm-yolo26s-seg-v1.pt"
    # The test cannot chown to the service account: accept the test user as the owner.
    monkeypatch.setattr(module.main.__globals__["assets"], "OWNER", (os.getuid(), os.getgid()))
    assert host.install() == 0
    assert opener.call_count == 1
    assert "keep asset vision-weights" in host.out.getvalue()
    assert target.read_bytes() == source.read_bytes()
    # A changed file no longer matches the manifest: download it again.
    target.write_bytes(b"x" * len(source.read_bytes()))
    assert host.install() == 0
    assert opener.call_count == 2
    assert target.read_bytes() == source.read_bytes()


def test_vision_failed_download_preserves_existing_weights_and_installs_no_unit(host, vision_asset):
    _, opener = vision_asset
    select_optional(host, "vision")
    target = host.data / "models/chip-arm-yolo26s-seg-v1.pt"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous verified model")
    opener.side_effect = lambda *_args, **_kwargs: io.BytesIO(b"wrong content")
    assert host.install() == 1
    assert target.read_bytes() == b"previous verified model"
    assert not (host.units / "mg400-vision.service").exists()
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]


@pytest.mark.parametrize("damage", ["missing", "unregistered", "wrong-service", "symlink"])
def test_vision_asset_preflight_refuses_invalid_manifest_or_target_before_changes(
    host, vision_asset, damage
):
    _, opener = vision_asset
    select_optional(host, "vision")
    manifest = host.code / "config/assets.toml"
    if damage == "missing":
        manifest.unlink()
    elif damage == "unregistered":
        registry = host.code / "deploy/services.toml"
        registry.write_text(
            registry.read_text().replace('assets = ["vision-weights"]', "assets = []")
        )
    elif damage == "wrong-service":
        manifest.write_text(manifest.read_text().replace('service = "vision"', 'service = "web"'))
    else:
        target = host.data / "models/chip-arm-yolo26s-seg-v1.pt"
        target.parent.mkdir(parents=True)
        target.symlink_to(host.root / "never-follow-this")
    assert host.install() == 1
    assert not host.settings.exists() and not host.units.exists()
    assert len(host.commands) == 1
    opener.assert_not_called()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('name = "web"', 'name = "all"'),
        ('name = "stage"', 'name = "web"'),
        ("optional = false", 'optional = "false"'),
        ('venv = ".venv"', 'venv = "../../elsewhere"'),
        ('units = ["mg400-web-panel.service"]', 'units = ["../escape.service"]'),
        ('units = ["mg400-web-panel.service"]', "units = []"),
        ('units = ["mg400-stage-panel.service"]', 'units = ["mg400-web-panel.service"]'),
        ('groups = ["stage"]', 'groups = ["stage", "stage"]'),
        ('groups = ["tools"]', 'groups = ["--all-groups"]'),
        ("port = 8766", 'port = 8766\ntoken = "MG400_PANEL_TOKEN"'),
        ("port = 8766", "port = true"),
        ("port = 8766", "port = 65536"),
        ("port = 8766", "port = -1"),
        ("port = 8767", "port = 8766"),
        ("assets = []", 'assets = ["../model"]'),
        ("assets = []", 'assets = ["model", "model"]'),
        ("optional = false\n", ""),
    ],
)
def test_registry_loader_rejects_invalid_fields(module, tmp_path, old, new):
    path = tmp_path / "services.toml"
    path.write_text((ROOT / "deploy/services.toml").read_text().replace(old, new, 1))
    with pytest.raises(ValueError):
        module.load_services(path)


def test_registry_loader_refuses_symlink(module, tmp_path):
    link = tmp_path / "services.toml"
    link.symlink_to(ROOT / "deploy/services.toml")
    with pytest.raises(OSError):
        module.load_services(link)


@pytest.mark.parametrize(
    "selection", ["unknown", "optional,optional", "optional,", ",optional", "optional,,web"]
)
def test_bad_optional_selection_refuses_before_host_changes(host, selection):
    add_optional_service(host)
    select_optional(host, selection)
    assert host.install() == 1
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()
    assert len(host.commands) == 1


def test_mandatory_groups_are_explicit_and_ignore_external_venv(host, monkeypatch):
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/tmp/unrelated-environment")
    assert host.install() == 0
    sync, options = next(
        (command, options) for command, options in host.commands if command[0] == "/usr/bin/uv"
    )
    assert sync == [
        "/usr/bin/uv",
        "sync",
        "--locked",
        "--no-default-groups",
        "--group",
        "furnace",
        "--group",
        "microscope",
        "--group",
        "orchestrator",
        "--group",
        "stage",
        "--group",
        "tools",
        "--python",
        "/usr/bin/python3",
    ]
    assert options["env"]["UV_PROJECT_ENVIRONMENT"] == str(host.code / ".venv")


def test_enabled_optional_service_uses_its_separate_environment(host):
    add_optional_service(host)
    select_optional(host, " optional ")
    assert host.install() == 0
    syncs = [
        (command, options) for command, options in host.commands if command[0] == "/usr/bin/uv"
    ]
    assert len(syncs) == 2
    assert syncs[0][0][4:14] == [
        "--group",
        "furnace",
        "--group",
        "microscope",
        "--group",
        "orchestrator",
        "--group",
        "stage",
        "--group",
        "tools",
    ]
    assert syncs[1][0] == [
        "/usr/bin/uv",
        "sync",
        "--locked",
        "--no-default-groups",
        "--group",
        "vision",
        "--python",
        "/usr/bin/python3",
    ]
    assert syncs[1][1]["env"]["UV_PROJECT_ENVIRONMENT"] == str(host.code / ".venv-vision")
    assert (host.units / "mg400-optional.service").exists()


def test_optional_groups_in_shared_environment_are_deduplicated(host):
    add_optional_service(host, venv=".venv", groups=("stage", "thermal"))
    select_optional(host, "optional")
    assert host.install() == 0
    commands = [command for command, _ in host.commands if command[0] == "/usr/bin/uv"]
    assert commands == [
        [
            "/usr/bin/uv",
            "sync",
            "--locked",
            "--no-default-groups",
            "--group",
            "furnace",
            "--group",
            "microscope",
            "--group",
            "orchestrator",
            "--group",
            "stage",
            "--group",
            "thermal",
            "--group",
            "tools",
            "--python",
            "/usr/bin/python3",
        ]
    ]


@pytest.fixture
def optional_asset(host, local_asset):
    registry = host.code / "deploy/services.toml"
    registry.write_text(registry.read_text().replace('assets = ["model"]', "assets = []"))
    manifest = host.code / "config/assets.toml"
    manifest.write_text(manifest.read_text().replace('service = "web"', 'service = "optional"'))
    add_optional_service(host, asset_names=("model",))
    return local_asset


def test_disabled_service_installs_no_groups_assets_units_or_udev(
    module, host, optional_asset, monkeypatch
):
    templates = host.code / "config/udev"
    templates.mkdir()
    (templates / "70-mg400-optional.rules.in").write_text("invalid disabled template")
    opener = Mock(side_effect=AssertionError("Disabled asset must not download"))
    monkeypatch.setattr(module.main.__globals__["assets"], "open_download", opener)
    assert host.install("--fetch-assets") == 0
    syncs = [command for command, _ in host.commands if command[0] == "/usr/bin/uv"]
    assert len(syncs) == 1 and "vision" not in syncs[0]
    assert not (host.data / "models").exists()
    assert not (host.units / "mg400-optional.service").exists()
    assert not (host.root / "etc/udev").exists()
    assert not [command for command, _ in host.commands if command[0] == "udevadm"]
    opener.assert_not_called()


def test_explicit_disabled_asset_is_refused_before_changes(host, optional_asset):
    assert host.install("--asset", f"model={optional_asset}") == 1
    assert not host.settings.exists() and not host.data.exists()


def test_enabled_optional_asset_keeps_verification_and_permissions(host, optional_asset):
    select_optional(host, "optional")
    assert host.install("--asset", f"model={optional_asset}") == 0
    target = host.data / "models/model.dat"
    assert target.read_bytes() == optional_asset.read_bytes()
    assert mode(target) == 0o640 and host.owner(target) == (1000, 1000)


def test_registry_cannot_claim_another_services_asset(host, local_asset):
    manifest = host.code / "config/assets.toml"
    manifest.write_text(manifest.read_text().replace('service = "web"', 'service = "someone-else"'))
    assert host.install("--fetch-assets") == 1
    assert not host.settings.exists() and not host.data.exists()


def test_missing_selected_unit_is_refused_before_changes(host):
    add_optional_service(host)
    select_optional(host, "optional")
    (host.code / "config/systemd/mg400-optional.service").unlink()
    assert host.install() == 1
    assert not host.settings.exists() and not host.data.exists()


@pytest.fixture
def optional_udev(module, host, monkeypatch):
    add_optional_service(host)
    select_optional(host, "optional")
    templates = host.code / "config/udev"
    templates.mkdir()
    template = templates / "70-mg400-optional.rules.in"
    template.write_text(
        'SUBSYSTEM=="usb", ATTR{idVendor}=="1234", ATTR{idProduct}=="5678", '
        'MODE="0660", GROUP="@SERVICE_GROUP@"\n'
    )
    monkeypatch.setattr(
        module.main.__globals__["grp"],
        "getgrgid",
        lambda gid: SimpleNamespace(gr_name="mg400group"),
    )
    return template


def test_only_enabled_udev_rules_are_rendered_with_root_permissions(host, optional_udev):
    assert host.install() == 0
    target = host.root / "etc/udev/rules.d/70-mg400-optional.rules"
    assert target.read_text() == optional_udev.read_text().replace("@SERVICE_GROUP@", "mg400group")
    assert mode(target) == 0o644 and host.owner(target) == (0, 0)
    assert [command for command, _ in host.commands if command[0] == "udevadm"] == [
        ["udevadm", "control", "--reload-rules"],
        ["udevadm", "trigger", "--subsystem-match=usb"],
    ]


def test_optional_udev_dry_run_only_lists_actions(host, optional_udev):
    assert host.install("--dry-run") == 0
    assert not host.settings.exists() and not (host.root / "etc/udev").exists()
    assert not [command for command, _ in host.commands if command[0] == "udevadm"]
    assert "Would run: udevadm control --reload-rules" in host.out.getvalue()
    assert "Would run: udevadm trigger --subsystem-match=usb" in host.out.getvalue()


@pytest.mark.parametrize("where", ["source", "destination", "ancestor"])
def test_udev_symlinks_are_refused_before_changes(host, optional_udev, tmp_path, where):
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep")
    if where == "source":
        optional_udev.unlink()
        optional_udev.symlink_to(sentinel)
    elif where == "destination":
        target = host.root / "etc/udev/rules.d/70-mg400-optional.rules"
        target.parent.mkdir(parents=True)
        target.symlink_to(sentinel)
    else:
        (host.root / "etc").mkdir()
        (host.root / "etc/udev").symlink_to(tmp_path)
    assert host.install() == 1
    assert not host.settings.exists() and not host.data.exists()
    assert sentinel.read_text() == "keep"


def test_default_dry_run_lists_every_step(host):
    binary = host.root / "offline-caddy"
    binary.write_bytes(b"offline Caddy fixture; never execute")
    assert host.install("--dry-run", "--caddy", str(binary)) == 0
    assert host.out.getvalue().replace(str(host.root), "<host>").splitlines() == [
        "The Toupcam library is not installed. Pass it with --toupcam PATH.",
        "Would ensure <host>/etc/mg400 (0755, 0:0)",
        "Would create <host>/etc/mg400/mg400.env",
        "Would ensure <host>/var/lib/mg400 (0750, 0:1000)",
        "Would ensure <host>/var/lib/mg400/locks (0700, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/work (0750, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/camera (0700, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/stage (0700, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/robot (0750, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/calibration (0750, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/calibration/cameras (0700, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/furnace (0750, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/calibration/microscope (0750, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/recordings (0750, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/runs (0750, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/zones (0750, 1000:1000)",
        "Would ensure <host>/opt/mg400 (0750, 0:1000)",
        "Would prepare Python environment <host>/opt/mg400/.venv",
        "Would run: /usr/bin/uv sync --locked --no-default-groups --group furnace "
        "--group microscope --group orchestrator --group stage --group tools "
        "--python /usr/bin/python3",
        "Would ensure <host>/usr/local/lib/mg400 (0755, 0:0)",
        "Would install <host>/offline-caddy as <host>/usr/local/lib/mg400/caddy",
        "Would write <host>/etc/mg400/Caddyfile",
        "Would install <host>/etc/systemd/system/mg400-camera-capture.service",
        "Would install <host>/etc/systemd/system/mg400-camera-preview.service",
        "Would install <host>/etc/systemd/system/mg400-furnace.service",
        "Would install <host>/etc/systemd/system/mg400-microscope.service",
        "Would install <host>/etc/systemd/system/mg400-orchestrator.service",
        "Would install <host>/etc/systemd/system/mg400-proxy.service",
        "Would install <host>/etc/systemd/system/mg400-stage-panel.service",
        "Would install <host>/etc/systemd/system/mg400-web-panel.service",
        "Would run: systemctl daemon-reload",
        "Would link <host>/usr/local/bin/mg400ctl to <host>/opt/mg400/deploy/mg400ctl",
        "Would link <host>/usr/local/bin/labctl to <host>/opt/mg400/deploy/mg400ctl",
    ]


@pytest.mark.parametrize("key", ["MG400_FURNACE_GAS_MAP", "MG400_FURNACE_IDLE_GAS"])
def test_furnace_json_settings_keep_the_systemd_single_quoted_literal(module, key):
    literal = '{"0": "Operator label", "1": 0}'
    line = f"{key}='{literal}'\n"
    assert module.parse_settings(line)[key] == literal
    merged, changed = module.merge_settings(line, f"{key}=\nOTHER=1\n")
    assert merged.startswith(line) and changed == ["OTHER"]
    assert module.parse_settings(merged)[key] == literal
    for current in (None, "OTHER=1\n"):
        merged, _ = module.merge_settings(current, line)
        assert module.parse_settings(merged)[key] == literal
        repeated, changed = module.merge_settings(merged, line)
        assert repeated == merged and changed == []


@pytest.mark.parametrize(
    "value",
    [
        '{"0": 0}',
        '"{}"',
        "'[]'",
        "'{\"0\":NaN}'",
        "'{\"0\":1e999}'",
        '\'{"0":1,"0":2}\'',
        '\'{"0":"a\\nb"}\'',
        '\'{"0":"it\'s"}\'',
    ],
)
def test_furnace_json_settings_refuse_ambiguous_or_nonfinite_literals(module, value):
    with pytest.raises(ValueError):
        module.parse_settings(f"MG400_FURNACE_GAS_MAP={value}\n")


def test_furnace_json_exception_does_not_apply_to_other_settings(module):
    with pytest.raises(ValueError, match="remove quotes"):
        module.parse_settings("OTHER='{\"0\":0}'\n")


def test_furnace_installs_by_default_with_private_state_directory_and_own_group(module, host):
    example = host.code / "deploy/mg400.env.example"
    example.write_text(
        example.read_text().replace(
            "MG400_FURNACE_GAS_MAP=", 'MG400_FURNACE_GAS_MAP=\'{"0":"Operator label"}\''
        )
    )
    assert host.install() == 0
    assert mode(host.data / "furnace") == 0o750
    assert host.owner(host.data / "furnace") == (1000, 1000)
    assert (host.units / "mg400-furnace.service").exists()
    assert (
        module.parse_settings(host.settings.read_text())["MG400_FURNACE_GAS_MAP"]
        == '{"0":"Operator label"}'
    )
    sync = next(command for command, _ in host.commands if command[0] == "/usr/bin/uv")
    assert sync[sync.index("furnace") - 1] == "--group"
    assert not any(
        command[:2] in (["systemctl", "start"], ["systemctl", "restart"])
        for command, _ in host.commands
    )


def test_furnace_json_setting_rejects_del(module):
    with pytest.raises(ValueError):
        module.parse_settings('MG400_FURNACE_GAS_MAP=\'{"0":"bad\x7flabel"}\'')


def test_enabled_orchestrator_installs_private_data_and_group_without_starting_it(module, host):
    select_optional(host, "orch")
    assert host.install() == 0
    for name in ("runs", "zones"):
        directory = host.data / name
        assert mode(directory) == 0o750 and host.owner(directory) == (1000, 1000)
        assert list(directory.iterdir()) == []
    assert (host.units / "mg400-orchestrator.service").exists()
    assert not any(
        key.endswith("_TOKEN") for key in module.parse_settings(host.settings.read_text())
    )
    sync = next(command for command, _ in host.commands if command[0] == "/usr/bin/uv")
    assert sync[sync.index("orchestrator") - 1] == "--group"
    assert not any(
        command[:2] in (["systemctl", "start"], ["systemctl", "restart"])
        for command, _ in host.commands
    )


def test_selected_orchestrator_refuses_symlinked_zone_directory(host, tmp_path):
    select_optional(host, "orch")
    host.data.mkdir(parents=True)
    target = tmp_path / "unrelated-zone-data"
    target.mkdir()
    sentinel = target / "keep"
    sentinel.write_text("unchanged")
    (host.data / "zones").symlink_to(target, target_is_directory=True)
    assert host.install() == 1
    assert sentinel.read_text() == "unchanged"
    assert not (host.units / "mg400-orchestrator.service").exists()


def proxy_settings(host, **overrides):
    values = {"MG400_OPTIONAL_SERVICES": "proxy"}
    values.update(overrides)
    host.settings.parent.mkdir(parents=True, exist_ok=True)
    host.settings.write_text(
        (host.code / "deploy/mg400.env.example").read_text()
        + "\n".join(f"{key}={value}" for key, value in values.items())
        + "\n"
    )
    binary = host.root / "offline-caddy"
    binary.write_bytes(b"offline Caddy fixture; never execute")
    return binary


def test_proxy_install_is_private_and_never_starts_it(host, module):
    binary = proxy_settings(host)
    assert host.install("--caddy", str(binary)) == 0
    caddy = host.root / "usr/local/lib/mg400/caddy"
    config = host.settings.parent / "Caddyfile"
    assert caddy.read_bytes() == binary.read_bytes()
    assert mode(caddy) == 0o755 and host.owner(caddy) == (0, 0)
    assert mode(config) == 0o640 and host.owner(config) == (0, 1000)
    assert (host.units / "mg400-proxy.service").is_file()
    text = config.read_text()
    assert "http://:8080" in text and "bind 127.0.0.1" in text
    assert "@ORCH_PORT@" not in text and "/robot" not in text
    commands = names(host.commands)
    assert not any(command[0] == str(caddy) for command in commands)
    assert not any(
        command[:2] in (["systemctl", "start"], ["systemctl", "restart"]) for command in commands
    )
    sync = next(command for command in commands if command[:2] == ["/usr/bin/uv", "sync"])
    assert sync[3:] == [
        "--no-default-groups",
        "--group",
        "furnace",
        "--group",
        "microscope",
        "--group",
        "orchestrator",
        "--group",
        "stage",
        "--group",
        "tools",
        "--python",
        "/usr/bin/python3",
    ]


def test_proxy_dry_run_checks_without_any_host_mutation(host):
    binary = proxy_settings(host)
    before = host.settings.read_bytes()
    assert host.install("--dry-run", "--caddy", str(binary)) == 0
    assert host.settings.read_bytes() == before
    assert not host.data.exists()
    assert not host.units.exists()
    assert not (host.root / "usr/local/lib/mg400/caddy").exists()
    assert not (host.settings.parent / "Caddyfile").exists()
    assert all(command[0] in {"/usr/bin/python3", "systemctl"} for command in names(host.commands))
    assert "Would write" in host.out.getvalue()


@pytest.mark.parametrize(
    "settings",
    [
        {"MG400_PROXY_BIND": "0.0.0.0"},
        {"MG400_PROXY_BIND": "::"},
        {"MG400_PROXY_BIND": "224.0.0.1"},
        {"MG400_PROXY_BIND": "proxy.example"},
        {"MG400_PROXY_BIND": "127.0.0.1 { respond injected }"},
        {"MG400_PROXY_BIND": "fe80::1%eth0"},
        {"MG400_PROXY_PORT": "80"},
        {"MG400_PROXY_PORT": "8080.0"},
        {"MG400_PROXY_PORT": "8080;shutdown"},
        {"MG400_PROXY_PORT": "65536"},
        {"MG400_PROXY_PORT": "8772"},
    ],
)
def test_proxy_invalid_configuration_refuses_before_host_mutation(host, settings):
    binary = proxy_settings(host, **settings)
    before = host.settings.read_bytes()
    assert host.install("--caddy", str(binary)) == 1
    assert host.settings.read_bytes() == before
    assert not host.data.exists() and not host.units.exists()
    assert not (host.settings.parent / "Caddyfile").exists()


@pytest.mark.parametrize("source_kind", ["missing", "empty", "symlink", "fifo"])
def test_proxy_binary_requires_an_explicit_regular_nonempty_file(host, source_kind):
    binary = proxy_settings(host)
    binary.unlink()
    if source_kind == "empty":
        binary.touch()
    elif source_kind == "symlink":
        binary.symlink_to(host.settings)
    elif source_kind == "fifo":
        os.mkfifo(binary)
    assert host.install("--caddy", str(binary)) == 1
    assert not host.data.exists() and not host.units.exists()


@pytest.mark.parametrize("destination", ["usr/local/lib/mg400/caddy", "etc/mg400/Caddyfile"])
def test_proxy_refuses_replaced_destination_without_touching_link_target(host, destination):
    binary = proxy_settings(host)
    target = host.root / "untouched"
    target.write_bytes(b"preserve")
    path = host.root / destination
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    assert host.install("--caddy", str(binary)) == 1
    assert target.read_bytes() == b"preserve" and path.is_symlink()
    assert not host.data.exists()


def test_proxy_refuses_symlinked_binary_parent(host):
    binary = proxy_settings(host)
    outside = host.root / "outside"
    outside.mkdir()
    parent = host.root / "usr/local/lib"
    parent.mkdir(parents=True)
    (parent / "mg400").symlink_to(outside, target_is_directory=True)
    assert host.install("--caddy", str(binary)) == 1
    assert list(outside.iterdir()) == []
    assert not host.data.exists()


def test_proxy_requires_a_caddy_binary(host):
    assert host.install(caddy_binary=False) == 1
    assert not host.data.exists()


def test_proxy_rendering_keeps_verified_origins_and_upgrade_defaults(host, module):
    # The orchestrator upstream port is fixed. The settings cannot change it.
    proxy_settings(host, MG400_PROXY_BIND="::1", MG400_ORCH_PORT="9001")
    settings = module.parse_settings(host.settings.read_text())
    registry = module.load_services(host.code / "deploy/services.toml")
    config = module.render_proxy(
        module.Host(host.root, source=host.code),
        settings,
        module.enabled_services(registry, "proxy"),
    )
    assert "bind [::1]" in config
    assert "import instrument 127.0.0.1:8772" in config
    assert "import instrument 127.0.0.1:1985" in config
    assert "header_up Host {http.request.hostport}" in config
    assert "header_up X-Forwarded-Host {http.request.hostport}" in config
    assert "header_up X-Forwarded-For {http.request.remote.host}" in config
    assert "header_up -Forwarded" in config and "header_up -X-Real-IP" in config
    assert "Authorization" not in config and "Origin" not in config
    assert "versions 1.1" in config and "read_timeout 60s" in config
    assert "response_header_timeout 60s" in config
    assert "Upgrade" not in config and "Connection" not in config  # Caddy forwards upgrades itself.
    assert config.count("handle_path ") == 1 and "handle_path /camera/*" in config
    # Only the Lab Console, its control socket and the camera streams are proxied.
    assert config.count("handle /") == 2 and "handle /lab/*" in config
    from orchestrator.__main__ import SOCKET_PORT

    assert module.ORCH_SOCKET_PORT == SOCKET_PORT
    socket_route = config.index("handle /lab/api/console/socket {")
    assert config.index("import instrument 127.0.0.1:8774", socket_route) < config.index(
        "handle /lab/*"
    )
    assert not any(
        f"/{name}" in config for name in ("robot", "stage", "thermal", "furnace", "microscope")
    )
    assert "redir / /lab/ 302" in config
    assert "admin off" in config and "persist_config off" in config
    assert "http.log.access http.log.error" in config
    assert "lb_retries" not in config and "lb_try_duration" not in config


def installed_caddy(host, module, monkeypatch, *, owner=0, group=0, rights=0o755):
    binary = host.root / "usr/local/lib/mg400/caddy"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"existing offline Caddy fixture")
    binary.chmod(rights)
    inode = binary.stat().st_ino
    original_fstat = os.fstat

    def fstat(fd):
        info = original_fstat(fd)
        if info.st_ino != inode:
            return info
        values = list(info)
        values[4] = owner
        values[5] = group
        return os.stat_result(values)

    monkeypatch.setattr(module.main.__globals__["os"], "fstat", fstat)
    return binary


@pytest.mark.parametrize("group,rights", [(0, 0o755), (1000, 0o750)])
def test_proxy_reuses_a_safe_existing_binary_without_executing_or_replacing_it(
    host, module, monkeypatch, group, rights
):
    proxy_settings(host)
    binary = installed_caddy(host, module, monkeypatch, group=group, rights=rights)
    inode = binary.stat().st_ino
    assert host.install(caddy_binary=False) == 0
    assert binary.stat().st_ino == inode
    assert (host.settings.parent / "Caddyfile").is_file()
    assert not any(command[0] == str(binary) for command in names(host.commands))


@pytest.mark.parametrize(
    "owner,rights",
    [(1000, 0o755), (0, 0o775), (0, 0o757), (0, 0o644), (0, 0o700), (0, 0o750), (0, 0o4755)],
)
def test_proxy_refuses_an_unsafe_existing_binary_before_host_changes(
    host, module, monkeypatch, owner, rights
):
    proxy_settings(host)
    binary = installed_caddy(host, module, monkeypatch, owner=owner, rights=rights)
    assert host.install(caddy_binary=False) == 1
    assert binary.read_bytes() == b"existing offline Caddy fixture"
    assert not host.data.exists()


@pytest.mark.parametrize("rights", [0o701, 0o741])
def test_proxy_other_execute_cannot_override_matching_group_permissions(
    host, module, monkeypatch, rights
):
    proxy_settings(host)
    installed_caddy(host, module, monkeypatch, group=1000, rights=rights)
    assert host.install(caddy_binary=False) == 1
    assert not host.data.exists()


@pytest.fixture
def inventory_udev(module, host, monkeypatch):
    """Use the shipped templates with an isolated host and a fake NSS group."""
    shutil.copytree(ROOT / "config/udev", host.code / "config/udev")
    monkeypatch.setattr(
        module.main.__globals__["grp"],
        "getgrgid",
        lambda gid: SimpleNamespace(gr_name="lab1000") if gid == 1000 else None,
    )
    monkeypatch.setattr(
        module.main.__globals__["grp"],
        "getgrnam",
        lambda name: SimpleNamespace(gr_gid={"dialout": 20, "video": 44}[name]),
    )
    monkeypatch.setattr(
        module.main.__globals__["pwd"], "getpwuid", lambda uid: SimpleNamespace(pw_name="lab1000")
    )
    monkeypatch.setattr(module.main.__globals__["os"], "getgrouplist", lambda name, gid: [gid, 27])
    return host.code / "config/udev"


def test_inventory_udev_install_selects_only_registered_enabled_services(host, inventory_udev):
    assert host.install() == 0
    expected = {"camera", "stage", "furnace", "microscope"}
    rules = host.root / "etc/udev/rules.d"
    assert {p.name for p in rules.iterdir()} == {f"70-mg400-{name}.rules" for name in expected}
    for name in expected:
        target = rules / f"70-mg400-{name}.rules"
        source = inventory_udev / (target.name + ".in")
        assert target.read_text() == source.read_text().replace("@SERVICE_GROUP@", "lab1000")
        assert mode(target) == 0o644 and host.owner(target) == (0, 0)
    assert [cmd for cmd, _ in host.commands if cmd[0] == "udevadm"] == [
        ["udevadm", "control", "--reload-rules"],
        ["udevadm", "trigger", "--subsystem-match=usb"],
    ]


def test_inventory_udev_dry_run_writes_nothing_and_executes_no_udev(host, inventory_udev):
    assert host.install("--dry-run") == 0
    assert not host.settings.exists()
    assert not (host.root / "etc/udev").exists()
    assert not [cmd for cmd, _ in host.commands if cmd[0] == "udevadm"]
    output = host.out.getvalue()
    for name in ("camera", "stage", "furnace", "microscope"):
        assert f"70-mg400-{name}.rules" in output
    assert "70-mg400-thermal.rules" not in output
    assert "Would run: udevadm control --reload-rules" in output
    assert "Would run: udevadm trigger --subsystem-match=usb" in output


@pytest.mark.parametrize(
    "bad_rule",
    [
        'SUBSYSTEM=="tty", MODE="0660", GROUP="@SERVICE_GROUP@"',
        'SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", MODE="0666"',
        '# ATTR{idVendor}=="1234", ATTR{idProduct}=="5678", MODE="0660", GROUP="@SERVICE_GROUP@"',
    ],
)
def test_every_udev_permission_rule_must_identify_device_and_group(host, optional_udev, bad_rule):
    valid_rule = optional_udev.read_text()
    optional_udev.write_text(bad_rule if bad_rule.startswith("#") else valid_rule + bad_rule + "\n")
    assert host.install() == 1
    assert not host.data.exists()
    assert not (host.root / "etc/udev").exists()
    assert not [cmd for cmd, _ in host.commands if cmd[0] == "udevadm"]


def test_camera_capture_sizes_keep_defaults_and_select_microscope_stream(module):
    assert module.camera_capture_sizes("", DEVICES) == [(1280, 720)] * 3
    sizes = module.camera_capture_sizes("", DEVICES, "camera2")
    assert sizes == [(1280, 720), (3840, 2160), (1280, 720)]
    config = module.render_go2rtc(DEVICES, sizes)
    assert f"video={DEVICES[1]}&input_format=mjpeg&video_size=3840x2160" in config
    assert config.count("video_size=1280x720") == 2
    assert "api:\n  listen: 127.0.0.1:1984\n" in config
    assert module.camera_capture_sizes("800x600,3840x2160,1280x720", DEVICES) == [
        (800, 600),
        (3840, 2160),
        (1280, 720),
    ]


@pytest.mark.parametrize(
    "value,stream",
    [
        ("640x480", ""),
        ("0x480,640x480,640x480", ""),
        ("640x480&exec=bad,640x480,640x480", ""),
        ("99999x99999,640x480,640x480", ""),
        ("", "camera4"),
        ("", "http://example.invalid/frame"),
    ],
)
def test_camera_size_settings_fail_before_install_changes(host, capsys, value, stream):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(
        f"MG400_CAMERA_DEVICES={','.join(DEVICES)}\n"
        f"MG400_CAMERA_CAPTURE_SIZES={value}\nMG400_MICROSCOPE_SIDE_STREAM={stream}\n"
    )
    before = host.settings.read_bytes()
    assert host.install() == 1
    assert capsys.readouterr().err
    assert host.settings.read_bytes() == before
    assert not host.data.exists() and not host.units.exists()


def test_installer_renders_side_stream_capture_size(host, module, tmp_path):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(
        f"MG400_CAMERA_DEVICES={','.join(DEVICES)}\nMG400_MICROSCOPE_SIDE_STREAM=camera2\n"
    )
    binary = tmp_path / "go2rtc"
    binary.write_bytes(b"offline executable")
    assert host.install("--go2rtc", str(binary)) == 0
    config = (host.root / "etc/mg400/go2rtc.yaml").read_text()
    sizes = [(1280, 720), (3840, 2160), (1280, 720)]
    assert config == module.render_go2rtc(DEVICES, sizes, nvenc=True)


def test_previews_fall_back_to_libx264_without_nvenc(host, module, tmp_path):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(f"MG400_CAMERA_DEVICES={','.join(DEVICES)}\n")
    binary = tmp_path / "go2rtc"
    binary.write_bytes(b"offline executable")

    calls = []

    def fallback(command, **options):
        calls.append(command)
        if "h264_nvenc" in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="No NVENC capable devices")
        if command[1:3] == ["-c", "import sys; print(sys.version_info >= (3, 12))"]:
            return SimpleNamespace(returncode=0, stdout="True\n", stderr="")
        if command[:2] == ["systemctl", "show"]:
            return SimpleNamespace(returncode=0, stdout="MainPID=0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="abc1234\n", stderr="")

    assert host.install("--go2rtc", str(binary), runner=fallback) == 0
    config = (host.root / "etc/mg400/go2rtc.yaml").read_text()
    assert config == module.render_go2rtc(DEVICES)
    assert "-c:v libx264" in config and "bitrate=4M" in config
    assert any("h264_nvenc" in command for command in calls)
    assert "encode H.264 with libx264" in host.out.getvalue()


def test_nvenc_previews_keep_fast_start_and_use_3_mbit(module):
    rendered = module.render_go2rtc(DEVICES, nvenc=True)
    h264 = next(line for line in rendered.splitlines() if line.startswith("  h264: "))
    assert "-c:v h264_nvenc" in h264 and "-fps_mode passthrough" in h264
    assert "-forced-idr 1" in h264 and "-force_key_frames expr:gte(t,n_forced)" in h264
    assert "libx264" not in rendered
    for number in range(1, 4):
        assert f"  camera{number}_h264: ffmpeg:camera{number}#video=h264#bitrate=3M\n" in rendered
    assert "  microscope_h264: ffmpeg:microscope#video=h264#bitrate=3M\n" in rendered


def test_nvenc_adds_h265_previews_without_sei_at_2_5_mbit(module):
    modes = [(1280, 720, 30), (640, 480, 30), (1280, 720, 30)]
    rendered = module.render_go2rtc(DEVICES, modes=modes, nvenc=True)
    h265 = next(line for line in rendered.splitlines() if line.startswith("  h265: "))
    assert "-c:v hevc_nvenc" in h265 and "-bf 0" in h265 and "-fps_mode passthrough" in h265
    assert "-forced-idr 1" in h265 and "-force_key_frames expr:gte(t,n_forced)" in h265
    # go2rtc 1.9.14 breaks an RTP aggregation packet of SEI and a small frame.
    assert h265.endswith('-bsf:v filter_units=remove_types=39|40"')
    assert "  camera1_h265: ffmpeg:camera1#video=h265#bitrate=2500k\n" in rendered
    assert "  camera2_h265: ffmpeg:camera2#video=h265#bitrate=2M\n" in rendered
    assert "  camera3_h265: ffmpeg:camera3#video=h265#bitrate=2500k\n" in rendered
    assert "  microscope_h265: ffmpeg:microscope#video=h265#bitrate=2500k\n" in rendered
    assert "_h265" not in module.render_go2rtc(DEVICES, modes=modes)
    assert "hevc_nvenc" not in module.render_go2rtc(DEVICES, modes=modes)


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_camera_configuration_renders_exact_device_count(module, count):
    devices = [f"/dev/v4l/by-path/offline-{number}-video-index0" for number in range(count)]
    assert module.camera_devices(",".join(devices)) == devices
    rendered = module.render_go2rtc(devices)
    assert rendered.count("v4l2:device?video=") == count
    for number, device in enumerate(devices, 1):
        assert f"camera{number}: v4l2:device?video={device}" in rendered
        assert f"camera{number}: video\n" in rendered
        assert f"  camera{number}_h264: ffmpeg:camera{number}#video=h264#bitrate=4M\n" in rendered
    assert f"camera{count + 1}:" not in rendered
    assert f"camera{count + 1}_h264:" not in rendered
    # ffmpeg reads and returns the H.264 previews through RTSP on loopback only.
    assert "rtsp:\n  listen: 127.0.0.1:8554\n" in rendered
    assert 'webrtc:\n  listen: ""\n' in rendered
    # A view shows its first frame at once: no input rate probe, a keyframe each second,
    # and each source frame sent once.
    rtsp, h264 = (
        next(line for line in rendered.splitlines() if line.startswith(f"  {key}: "))
        for key in ("rtsp", "h264")
    )
    assert "-fpsprobesize 2 -i {input}" in rtsp
    assert "-c:v libx264" in h264 and "-fps_mode passthrough" in h264
    assert "-x264-params fps=30" in h264 and "-force_key_frames expr:gte(t,n_forced)" in h264
    assert "preload:\n" + "".join(f"  camera{n}: video\n" for n in range(1, count + 1)) in rendered
    # go2rtc reads the microscope preview only for an H.264 viewer: it is not preloaded.
    assert rendered.endswith(
        "  microscope: http://127.0.0.1:8770/api/camera/microscope/mjpeg\n"
        "  microscope_h264: ffmpeg:microscope#video=h264#bitrate=3M\n"
    )
    assert "microscope: video" not in rendered


@pytest.mark.parametrize(
    "mode",
    [
        (1920, 1080, 60),
        (1920, 1080, 30),
        (4000, 3000, 15),
        (3840, 2160, 30),
        (2592, 1944, 30),
        (2560, 1440, 30),
        (2048, 1536, 30),
        (1600, 1200, 30),
        (1280, 720, 30),
        (1024, 768, 30),
        (1024, 576, 30),
        (800, 600, 30),
        (640, 480, 30),
        (640, 360, 30),
        (320, 240, 30),
    ],
)
def test_only_descriptor_approved_mjpeg_modes_render_and_large_streams_stay_idle(module, mode):
    rendered = module.render_go2rtc(DEVICES[:1], modes=[mode])
    width, height, fps = mode
    assert f"input_format=mjpeg&video_size={width}x{height}&framerate={fps}" in rendered
    if width <= 1280 and height <= 720:
        assert "preload:\n  camera1: video\n" in rendered
        assert f"#bitrate={4 if width > 640 else 2}M\n" in rendered
    else:
        assert "preload: {}\n" in rendered


@pytest.mark.parametrize(
    "setting",
    [
        "640x480@15",
        "640x480@60",
        "1920x1080@15",
        "4000x3000@30",
        "800x480@30",
        "640x480@30.0",
        "0640x480@30",
        "640X480@30",
        "640x480@30&exec=x",
        "640x480@30,",
        "640x480@30,640x480@30",
    ],
)
def test_invalid_camera_modes_refuse_before_host_changes(host, setting):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(f"MG400_CAMERA_DEVICES={DEVICES[0]}\nMG400_CAMERA_MODES={setting}\n")
    before = host.settings.read_bytes()
    assert host.install() == 1
    assert host.settings.read_bytes() == before
    assert not host.data.exists() and not host.units.exists()


def test_camera_modes_refuse_legacy_size_conflict_before_changes(host):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(
        f"MG400_CAMERA_DEVICES={DEVICES[0]}\nMG400_CAMERA_MODES=640x480@30\n"
        "MG400_CAMERA_CAPTURE_SIZES=640x480\n"
    )
    before = host.settings.read_bytes()
    assert host.install() == 1
    assert host.settings.read_bytes() == before
    assert not host.data.exists() and not host.units.exists()


def test_four_camera_modes_preserve_side_and_tray_without_preloading_large_streams(module):
    devices = [*DEVICES, "/dev/v4l/by-path/offline-tray-video-index0"]
    settings = {
        "MG400_CAMERA_DEVICES": ",".join(devices),
        "MG400_MICROSCOPE_SIDE_STREAM": "camera3",
    }
    default = module.camera_configuration(settings)
    assert default.modes == ((1280, 720, 30), (1280, 720, 30), (3840, 2160, 30), (1280, 720, 30))
    settings["MG400_CAMERA_MODES"] = "640x480@30,640x480@30,3840x2160@30,1600x1200@30"
    configured = module.camera_configuration(settings)
    assert configured.preloaded == ("camera1", "camera2")
    rendered = module.render_go2rtc(devices, modes=configured.modes)
    assert "camera3: video\n" not in rendered and "camera4: video\n" not in rendered
    assert "camera1_h264: ffmpeg:camera1#video=h264#bitrate=2M\n" in rendered
    assert "camera2_h264: ffmpeg:camera2#video=h264#bitrate=2M\n" in rendered
    assert "camera3_h264" not in rendered and "camera4_h264" not in rendered
    assert "video_size=3840x2160&framerate=30" in rendered
    assert (
        f"camera4: v4l2:device?video={devices[3]}&input_format=mjpeg&video_size=1600x1200"
        in rendered
    )


@pytest.mark.parametrize(
    "value",
    [
        "/dev/video0",
        "/dev/serial/by-id/camera",
        "/dev/v4l/by-path/../video0",
        f"{DEVICES[0]},{DEVICES[0]}",
        f"{DEVICES[0]},",
        f",{DEVICES[0]}",
    ],
)
def test_camera_devices_require_distinct_complete_stable_paths(module, value):
    with pytest.raises(ValueError):
        module.camera_devices(value)


def test_enabled_thermal_installs_private_roi_directory_group_and_usb_rule(
    module, host, inventory_udev
):
    select_optional(host, "thermal")
    assert host.install() == 0
    directory = host.data / "thermal"
    assert mode(directory) == 0o750 and host.owner(directory) == (1000, 1000)
    assert not (directory / "rois.toml").exists()
    unit = host.units / "mg400-thermal.service"
    assert unit.read_text() == (ROOT / "config/systemd" / unit.name).read_text()
    assert mode(unit) == 0o644 and host.owner(unit) == (0, 0)
    rule = host.root / "etc/udev/rules.d/70-mg400-thermal.rules"
    expected = (inventory_udev / (rule.name + ".in")).read_text()
    assert rule.read_text() == expected.replace("@SERVICE_GROUP@", "lab1000")
    assert 'ATTR{serial}=="P1016021DG040100208*"' in rule.read_text()
    assert mode(rule) == 0o644 and host.owner(rule) == (0, 0)
    sync = next(command for command, _ in host.commands if command[0] == "/usr/bin/uv")
    assert sync[sync.index("thermal") - 1] == "--group"
    assert not any(
        command[:2] in (["systemctl", "start"], ["systemctl", "restart"])
        or "mg400-thermal.service" in command
        for command, _ in host.commands
    )


def test_disabled_thermal_has_no_unit_usb_rule_data_or_dependency_group(host, inventory_udev):
    assert host.install() == 0
    assert not (host.units / "mg400-thermal.service").exists()
    assert not (host.root / "etc/udev/rules.d/70-mg400-thermal.rules").exists()
    assert not (host.data / "thermal").exists()
    sync = next(command for command, _ in host.commands if command[0] == "/usr/bin/uv")
    assert "thermal" not in sync


def test_thermal_dry_run_lists_preparation_without_writes_or_commands(host, inventory_udev):
    select_optional(host, "thermal")
    assert host.install("--dry-run") == 0
    assert not host.settings.exists() and not host.data.exists()
    assert not (host.root / "etc/udev").exists()
    assert not (host.units / "mg400-thermal.service").exists()
    assert not [cmd for cmd, _ in host.commands if cmd[0] in {"udevadm", "/usr/bin/uv"}]
    output = host.out.getvalue()
    assert "thermal (0750, 1000:1000)" in output
    assert "70-mg400-thermal.rules" in output
    assert "mg400-thermal.service" in output
    assert "--group thermal" in output


def test_thermal_install_preserves_existing_regions(module, host, inventory_udev):
    from thermal_cam.roi import load_regions

    select_optional(host, "thermal")
    directory = host.data / "thermal"
    directory.mkdir(parents=True)
    regions = directory / "rois.toml"
    regions.write_text("[regions.crucible]\nx0=1\ny0=2\nx1=3\ny1=4\n")
    regions.chmod(0o640)
    original = regions.read_bytes()
    assert host.install() == 0
    assert regions.read_bytes() == original
    assert mode(regions) == 0o640
    assert load_regions(regions)["crucible"].bounds() == {"x0": 1, "y0": 2, "x1": 3, "y1": 4}


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("destination_exists", [False, True])
def test_legacy_stage_calibration_prints_manual_move_hint_without_mutation(
    host, dry_run, destination_exists
):
    host.data.mkdir(parents=True)
    legacy = host.data / "stage.toml"
    legacy.write_text("legacy calibration must remain unchanged\n")
    legacy.chmod(0o640)
    before = legacy.stat()
    destination = host.data / "stage/stage.toml"
    if destination_exists:
        destination.parent.mkdir(mode=0o700)
        destination.write_text("existing new calibration\n")
        destination.chmod(0o600)
    assert host.install(*(["--dry-run"] if dry_run else [])) == 0
    assert legacy.read_text() == "legacy calibration must remain unchanged\n"
    assert legacy.stat().st_ino == before.st_ino
    assert mode(legacy) == 0o640
    if destination_exists:
        assert destination.read_text() == "existing new calibration\n"
        assert mode(destination) == 0o600
    else:
        assert not destination.exists()
    report = host.out.getvalue()
    assert f"Legacy stage calibration exists at {legacy}" in report
    assert f"Move it manually to {destination}" in report
    assert "do not overwrite an existing destination" in report
    assert "No calibration file was moved" in report


def test_installed_stage_directory_supports_atomic_reference_teaching(host, monkeypatch):
    from stage_controller.config import demo_geometry, geometry_data, load_geometry, parse_geometry
    from stage_controller.reference_store import _toml, teach_pixel

    assert host.install() == 0
    directory = host.data / "stage"
    assert mode(directory) == 0o700
    assert host.owner(directory) == (1000, 1000)
    assert mode(host.data) == 0o750
    assert host.owner(host.data) == (0, 1000)
    # Installation emulates root; the reference writer runs as the local service user.
    writer_uid = directory.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: writer_uid)
    data = geometry_data(demo_geometry())
    data["motors"]["insert"]["reference"] = dict(
        stream="camera3", marker=12, pixel=[10, 10], tolerance_px=2
    )
    geometry = parse_geometry(data)
    path = directory / "stage.toml"
    path.write_text(_toml(geometry))
    path.chmod(0o600)
    before = path.stat().st_ino
    updated = teach_pixel(path, geometry, "insert", [99.5, 99.5])
    assert path.stat().st_ino != before
    assert load_geometry(path) == updated
    assert mode(path) == 0o600
    assert updated.motors["insert"].reference["pixel"] == [99.5, 99.5]


def test_microscope_installs_by_default_with_sdk_udev_private_data_and_runtime(
    host, inventory_udev, toupcam_library, module
):
    assert host.install("--toupcam", str(toupcam_library)) == 0
    for relative in ("calibration/microscope", "recordings", "runs"):
        directory = host.data / relative
        assert mode(directory) == 0o750 and host.owner(directory) == (1000, 1000)
        assert not list(directory.iterdir())
    unit = host.units / "mg400-microscope.service"
    assert unit.read_bytes() == (ROOT / "config/systemd" / unit.name).read_bytes()
    assert mode(unit) == 0o644 and host.owner(unit) == (0, 0)
    rule = host.root / "etc/udev/rules.d/70-mg400-microscope.rules"
    assert rule.read_text() == (inventory_udev / (rule.name + ".in")).read_text().replace(
        "@SERVICE_GROUP@", "lab1000"
    )
    assert mode(rule) == 0o644 and host.owner(rule) == (0, 0)
    library = host.root / "usr/local/lib/mg400/libtoupcam.so"
    assert library.read_bytes() == toupcam_library.read_bytes()
    assert mode(library) == 0o755 and host.owner(library) == (0, 0)
    settings = module.parse_settings(host.settings.read_text())
    for key in ("SERIAL", "SIDE_STREAM"):
        assert settings["MG400_MICROSCOPE_" + key] == ""
    # The focus limits, clearance and turret window are fixed in code.
    for key in ("SWITCH_FOCUS", "HOME_SWITCH", "FOCUS_LIMITS", "NUDGE_STEPS"):
        assert "MG400_MICROSCOPE_" + key not in settings
    assert settings["MG400_TOUPCAM_SERIAL"] == settings["MG400_TOUPCAM_INDEX"] == ""
    sync = next(
        command for command in names(host.commands) if command[:2] == ["/usr/bin/uv", "sync"]
    )
    assert sync[sync.index("microscope") - 1] == "--group"
    assert not any(
        command[:2] in (["systemctl", "start"], ["systemctl", "restart"])
        or "mg400-microscope.service" in command
        for command in names(host.commands)
    )


def test_microscope_dry_run_lists_without_writes_or_service_start(
    host, inventory_udev, toupcam_library
):
    assert host.install("--dry-run", "--toupcam", str(toupcam_library)) == 0
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()
    assert not (host.root / "usr").exists() and not (host.root / "etc/udev").exists()
    for item in ("mg400-microscope.service", "70-mg400-microscope.rules", "libtoupcam.so"):
        assert item in host.out.getvalue()
    assert all(command[0] in {"/usr/bin/python3", "systemctl"} for command in names(host.commands))


@pytest.mark.parametrize("dry_run", [False, True])
def test_microscope_refuses_unverified_installed_library_before_mutation(
    host, inventory_udev, toupcam_library, module, monkeypatch, capsys, dry_run
):
    args = ["--dry-run"] if dry_run else []
    target = host.root / "usr/local/lib/mg400/libtoupcam.so"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"wrong installed SDK")
    target.chmod(0o755)
    microscope_library_owner(module, monkeypatch, target)
    assert host.install(*args) == 1
    assert "SHA-256 mismatch" in capsys.readouterr().err
    assert target.read_bytes() == b"wrong installed SDK"
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()


@pytest.mark.parametrize("missing", ["user", "memberships", "groups", "dialout", "video"])
def test_microscope_requires_resolvable_service_account_and_device_groups(
    host, inventory_udev, toupcam_library, module, monkeypatch, capsys, missing
):
    if missing == "user":
        monkeypatch.setattr(module.main.__globals__["pwd"], "getpwuid", Mock(side_effect=KeyError))
    elif missing == "memberships":
        monkeypatch.setattr(
            module.main.__globals__["os"], "getgrouplist", Mock(side_effect=OSError)
        )
    elif missing == "groups":
        monkeypatch.setattr(module.main.__globals__["grp"], "getgrnam", Mock(side_effect=OSError))
    else:

        def group(name):
            if name == missing:
                raise KeyError(name)
            return SimpleNamespace(gr_gid={"dialout": 20, "video": 44}[name])

        monkeypatch.setattr(module.main.__globals__["grp"], "getgrnam", group)
    assert host.install("--toupcam", str(toupcam_library)) == 1
    message = (
        "Microscope requires"
        if missing in {"groups", "dialout", "video"}
        else "Cannot resolve microscope"
    )
    assert message in capsys.readouterr().err
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()


def test_microscope_upgrade_preserves_library_data_and_measured_settings(
    host, inventory_udev, toupcam_library, module, monkeypatch
):
    assert host.install("--toupcam", str(toupcam_library)) == 0
    library = host.root / "usr/local/lib/mg400/libtoupcam.so"
    original_inode = library.stat().st_ino
    microscope_library_owner(module, monkeypatch, library)
    values = dict(SERIAL="/dev/serial/by-id/usb-microscope", SIDE_STREAM="camera3")
    lines = host.settings.read_text().splitlines()
    for index, line in enumerate(lines):
        for key, value in values.items():
            if line.startswith(f"MG400_MICROSCOPE_{key}="):
                lines[index] = f"MG400_MICROSCOPE_{key}={value}"
    host.settings.write_text("\n".join(lines) + "\n")
    originals = []
    for relative in (
        "calibration/microscope/reference.png",
        "recordings/run.json",
        "runs/state.json",
    ):
        path = host.data / relative
        path.write_bytes(b"existing host data")
        path.chmod(0o640)
        originals.append(path)
    assert host.install() == 0
    assert library.stat().st_ino == original_inode
    assert all(
        path.read_bytes() == b"existing host data" and mode(path) == 0o640 for path in originals
    )
    settings = module.parse_settings(host.settings.read_text())
    assert {key: settings[f"MG400_MICROSCOPE_{key}"] for key in values} == values


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "directory", "fifo", "hardlink"])
def test_microscope_rejects_unsafe_installed_library_before_mutation(
    host, inventory_udev, toupcam_library, kind
):
    target = host.root / "usr/local/lib/mg400/libtoupcam.so"
    target.parent.parent.mkdir(parents=True)
    if kind == "parent_symlink":
        outside = host.root / "outside"
        outside.mkdir()
        (outside / target.name).write_bytes(toupcam_library.read_bytes())
        target.parent.symlink_to(outside, target_is_directory=True)
    else:
        target.parent.mkdir()
        if kind == "symlink":
            target.symlink_to(toupcam_library)
        elif kind == "directory":
            target.mkdir()
        elif kind == "fifo":
            os.mkfifo(target)
        else:
            os.link(toupcam_library, target)
    assert host.install() == 1
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()


def microscope_library_owner(module, monkeypatch, path, *, owner=0, group=0, directory_owners=None):
    """Model the root-owned destination chain while tests run without root."""
    identities = {parent.stat().st_ino: (0, 0) for parent in path.parents[:4]}
    identities[path.stat().st_ino] = (owner, group)
    for directory, ownership in (directory_owners or {}).items():
        identities[directory.stat().st_ino] = ownership
    original_fstat = os.fstat

    def fstat(fd):
        info = original_fstat(fd)
        if info.st_ino not in identities:
            return info
        values = list(info)
        values[4], values[5] = identities[info.st_ino]
        return os.stat_result(values)

    monkeypatch.setattr(module.main.__globals__["os"], "fstat", fstat)


@pytest.mark.parametrize(
    "group,rights", [(0, 0o644), (1000, 0o640), (20, 0o640), (44, 0o640), (27, 0o640)]
)
def test_microscope_reuses_service_readable_library_without_replacement(
    host, inventory_udev, toupcam_library, module, monkeypatch, group, rights
):
    target = host.root / "usr/local/lib/mg400/libtoupcam.so"
    target.parent.mkdir(parents=True)
    target.write_bytes(toupcam_library.read_bytes())
    target.chmod(rights)
    inode = target.stat().st_ino
    microscope_library_owner(module, monkeypatch, target, group=group)
    assert host.install() == 0
    assert target.stat().st_ino == inode and mode(target) == rights
    assert (host.units / "mg400-microscope.service").exists()
    assert not any(command[0] == str(target) for command in names(host.commands))


@pytest.mark.parametrize(
    "owner,group,rights",
    [
        (1000, 0, 0o755),
        (0, 0, 0o775),
        (0, 0, 0o757),
        (0, 0, 0o700),
        (0, 0, 0o4755),
        (0, 1000, 0o704),
        (0, 20, 0o704),
        (0, 44, 0o704),
        (0, 27, 0o704),
    ],
)
def test_microscope_rejects_unsafe_existing_library_permissions(
    host, inventory_udev, toupcam_library, module, monkeypatch, owner, group, rights
):
    target = host.root / "usr/local/lib/mg400/libtoupcam.so"
    target.parent.mkdir(parents=True)
    target.write_bytes(toupcam_library.read_bytes())
    target.chmod(rights)
    microscope_library_owner(module, monkeypatch, target, owner=owner, group=group)
    assert host.install() == 1
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
@pytest.mark.parametrize("source", [False, True])
@pytest.mark.parametrize(
    "owner,group,rights",
    [
        (1000, 0, 0o755),
        (0, 0, 0o775),
        (0, 0, 0o757),
        (0, 0, 0o700),
        (0, 1000, 0o701),
        (0, 20, 0o701),
        (0, 44, 0o701),
        (0, 27, 0o701),
    ],
)
def test_microscope_refuses_unsafe_library_ancestors_before_mutation(
    host,
    inventory_udev,
    toupcam_library,
    module,
    monkeypatch,
    capsys,
    depth,
    source,
    owner,
    group,
    rights,
):
    target = host.root / "usr/local/lib/mg400/libtoupcam.so"
    target.parent.mkdir(parents=True)
    target.write_bytes(toupcam_library.read_bytes())
    target.chmod(0o755)
    directory = target.parents[depth]
    directory.chmod(rights)
    microscope_library_owner(
        module, monkeypatch, target, directory_owners={directory: (owner, group)}
    )
    args = ["--toupcam", str(toupcam_library)] if source else []
    assert host.install(*args) == 1
    assert "destination directories" in capsys.readouterr().err
    assert mode(directory) == rights
    assert target.read_bytes() == toupcam_library.read_bytes()
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()


@pytest.mark.parametrize(
    "name,setting",
    [
        ("tool.toml", "MG400_ORCH_TOOL_FILE"),
        ("stations.toml", "MG400_ORCH_STATIONS_FILE"),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("destination_exists", [False, True])
def test_legacy_robot_data_and_explicit_paths_stay_unchanged(
    host, module, name, setting, dry_run, destination_exists
):
    host.data.mkdir(parents=True)
    legacy = host.data / name
    legacy.write_bytes(b"operator-owned legacy data\n")
    legacy.chmod(0o640)
    before = legacy.stat()
    destination = host.data / "robot" / name
    if destination_exists:
        destination.parent.mkdir(mode=0o750)
        destination.write_bytes(b"existing robot data\n")
        destination.chmod(0o600)
        destination_before = destination.stat()
    host.settings.parent.mkdir(parents=True)
    configured_path = f"/var/lib/mg400/{name}"
    host.settings.write_text(f"{setting}={configured_path}\n")
    assert host.install(*(["--dry-run"] if dry_run else [])) == 0
    assert legacy.read_bytes() == b"operator-owned legacy data\n"
    assert (legacy.stat().st_ino, legacy.stat().st_uid, legacy.stat().st_gid) == (
        before.st_ino,
        before.st_uid,
        before.st_gid,
    )
    assert mode(legacy) == 0o640
    if destination_exists:
        assert destination.read_bytes() == b"existing robot data\n"
        assert destination.stat().st_ino == destination_before.st_ino
        assert mode(destination) == 0o600
    else:
        assert not destination.exists()
    assert module.parse_settings(host.settings.read_text())[setting] == configured_path
    report = host.out.getvalue()
    assert f"Legacy robot data exists at {legacy}" in report
    assert f"Move it manually to {destination}" in report
    assert "Do not overwrite an existing destination" in report
    assert "No robot data file was moved" in report


def test_legacy_robot_symlink_is_reported_without_following_or_replacing_it(host, tmp_path):
    host.data.mkdir(parents=True)
    target = tmp_path / "unavailable-data"
    legacy = host.data / "tool.toml"
    legacy.symlink_to(target)
    before = legacy.lstat()
    assert host.install() == 0
    assert legacy.is_symlink() and legacy.readlink() == target
    assert legacy.lstat().st_ino == before.st_ino
    assert not target.exists()
    assert not (host.data / "robot/tool.toml").exists()
    assert f"Legacy robot data exists at {legacy}" in host.out.getvalue()


def test_robot_directory_link_is_refused_without_changing_its_target(host, tmp_path):
    target = tmp_path / "operator-data"
    target.mkdir(mode=0o700)
    marker = target / "keep.toml"
    marker.write_bytes(b"preserve operator data\n")
    host.data.mkdir(parents=True)
    (host.data / "robot").symlink_to(target, target_is_directory=True)
    assert host.install() == 1
    assert (host.data / "robot").is_symlink()
    assert mode(target) == 0o700
    assert marker.read_bytes() == b"preserve operator data\n"


@pytest.mark.parametrize(
    "removed,notice",
    [
        (("ffprobe",), "ffprobe is not installed."),
        (("ffmpeg", "ffprobe"), "ffmpeg and ffprobe are not installed."),
    ],
)
def test_missing_ffmpeg_gives_a_notice_and_does_not_refuse(host, removed, notice):
    for name in removed:
        (host.tools / name).unlink()
    assert host.install("--dry-run") == 0
    report = host.out.getvalue().splitlines()
    expected = f"{notice} Microscope recordings cannot finish. Install the ffmpeg package."
    assert report.count(expected) == 1


def test_cameras_without_ffmpeg_give_a_preview_notice(host, tmp_path):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(f"MG400_CAMERA_DEVICES={','.join(DEVICES)}\n")
    binary = tmp_path / "go2rtc"
    binary.write_bytes(b"offline executable")
    notice = (
        "ffmpeg is not installed. Camera previews send MJPEG, not H.264. "
        "Install the ffmpeg package."
    )
    assert host.install("--dry-run", "--go2rtc", str(binary)) == 0
    assert notice not in host.out.getvalue()
    (host.tools / "ffmpeg").unlink()
    assert host.install("--dry-run", "--go2rtc", str(binary)) == 0
    assert host.out.getvalue().splitlines().count(notice) == 1
