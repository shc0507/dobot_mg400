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
def host(module, monkeypatch, tmp_path):
    code = tmp_path / "opt/mg400"
    (code / "deploy").mkdir(parents=True)
    shutil.copy(ROOT / "deploy/mg400.env.example", code / "deploy")
    shutil.copy(ROOT / "deploy/services.toml", code / "deploy")
    shutil.copytree(ROOT / "config/systemd", code / "config/systemd")
    (tmp_path / "proc").mkdir()
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
    monkeypatch.setattr(globals_["sys"], "platform", "linux")
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    owners = {}

    def chown(fd, uid, gid):
        # The installer changes owners only through descriptors, never by path.
        assert isinstance(fd, int)
        owners[os.fstat(fd).st_ino] = (uid, gid)

    out = io.StringIO()

    def install(*argv, runner=None):
        return module.main(
            ["--uv", "/usr/bin/uv", *argv],
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
        settings=tmp_path / "etc/mg400/mg400.env",
        data=tmp_path / "var/lib/mg400",
        units=tmp_path / "etc/systemd/system",
    )


def names(commands):
    return [command for command, _ in commands]


def test_first_install_creates_settings_data_units_and_command(module, host):
    assert host.install() == 0

    settings = module.parse_settings(host.settings.read_text())
    example = module.parse_settings((ROOT / "deploy/mg400.env.example").read_text())
    assert settings.keys() == example.keys()
    tokens = [settings["MG400_PANEL_TOKEN"], settings["MG400_STAGE_TOKEN"]]
    assert all(len(token) >= 24 for token in tokens) and tokens[0] != tokens[1]
    assert mode(host.settings) == 0o600
    assert {key: value for key, value in settings.items() if not key.endswith("_TOKEN")} == {
        key: value for key, value in example.items() if not key.endswith("_TOKEN")
    }
    assert [(path.name, mode(path)) for path in sorted(host.data.rglob("*")) if path.is_dir()] == [
        ("camera", 0o700),
        ("locks", 0o700),
        ("work", 0o750),
    ]
    assert mode(host.data) == 0o750
    assert len((host.data / "camera/preview.key").read_text().strip()) >= 32
    assert mode(host.data / "camera/preview.key") == 0o600
    assert {path.name for path in host.units.iterdir()} == {
        path.name
        for path in (ROOT / "config/systemd").iterdir()
        if path.name != "mg400-furnace.service"
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
        ["systemctl", "show", "--property=MainPID", "mg400-gripper-collector.service"],
        [
            "/usr/bin/uv",
            "sync",
            "--locked",
            "--no-default-groups",
            "--group",
            "stage",
            "--group",
            "tools",
            "--python",
            "/usr/bin/python3",
        ],
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "mg400-gripper-collector.path"],
        ["git", "-C", str(host.code), "rev-parse", "--short", "HEAD"],
    ]
    assert uv[0]["cwd"] == host.code and uv[0]["env"]["UV_LINK_MODE"] == "copy"
    assert host.owner(host.code) == host.owner(host.data) == (0, 1000)
    for name in ("locks", "work", "camera", "camera/preview.key"):
        assert host.owner(host.data / name) == (1000, 1000), name
    assert host.owner(host.settings) == (0, 0)
    report = host.out.getvalue()
    assert "Installed commit abc1234." in report
    assert "Cameras are off" in report
    assert "No service was started, stopped or restarted" in report
    # The installer never starts, stops or restarts a robot service.
    assert not [command for command in names(host.commands) if "start" in command[1:2]]


def test_repeated_install_keeps_tokens_keys_and_settings(module, host):
    assert host.install() == 0
    settings = host.settings.read_text()
    key = (host.data / "camera/preview.key").read_text()
    assert host.install() == 0
    assert host.settings.read_text() == settings
    assert (host.data / "camera/preview.key").read_text() == key
    assert str(host.settings) not in host.out.getvalue().split("Installed")[1]


def test_existing_settings_keep_their_values_and_gain_missing_keys(module, host):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(
        "# Olares\nMG400_STAGE_BIND=100.0.0.1\nMG400_STAGE_TOKEN=\nMG400_STAGE_SERIAL=/dev/x\n"
    )
    assert host.install() == 0
    text = host.settings.read_text()
    settings = module.parse_settings(text)
    assert text.startswith("# Olares\nMG400_STAGE_BIND=100.0.0.1\nMG400_STAGE_TOKEN=")
    assert settings["MG400_STAGE_SERIAL"] == "/dev/x"
    assert len(settings["MG400_STAGE_TOKEN"]) >= 24
    assert settings["MG400_PANEL_BIND"] == "127.0.0.1"
    assert len(settings["MG400_PANEL_TOKEN"]) >= 24
    assert text.count("MG400_STAGE_BIND=") == 1
    assert f"add to {host.settings}: MG400_STAGE_TOKEN, MG400_ROBOT_HOST" in host.out.getvalue()


def test_merge_keeps_set_values_comments_and_quotes(module):
    example = "# Example\nA=1\nB_TOKEN=\nC=3\n"
    current = '# Mine\nA="custom value"\nB_TOKEN=keep\n'
    text, changed = module.merge_settings(current, example, token=lambda: "new")
    assert text == '# Mine\nA="custom value"\nB_TOKEN=keep\nC=3\n'
    assert changed == ["C"]
    assert module.parse_settings(text) == {"A": "custom value", "B_TOKEN": "keep", "C": "3"}
    text, changed = module.merge_settings(None, example, token=lambda: "new")
    assert (text, changed) == ("# Example\nA=1\nB_TOKEN=new\nC=3\n", ["B_TOKEN"])


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
    assert config.read_text() == module.render_go2rtc(DEVICES)
    assert config.read_text().startswith("api:\n  listen: 127.0.0.1:1984\n")
    assert (
        f"  camera3: v4l2:device?video={DEVICES[2]}"
        "&input_format=mjpeg&video_size=640x480&framerate=30\n"
    ) in config.read_text()
    # A later install keeps the installed binary without --go2rtc.
    assert host.install() == 0
    assert installed.read_bytes() == binary.read_bytes()


@pytest.mark.parametrize(
    ("devices", "argv", "message"),
    [
        (DEVICES, [], "Pass the go2rtc 1.9.14 binary"),
        (DEVICES[:2], [], "must list 3 devices"),
        ([*DEVICES[:2], "/dev/video0&exec=x"], [], "Invalid camera device path"),
    ],
    ids=["no-binary", "two-cameras", "unsafe-path"],
)
def test_camera_errors_stop_before_any_change(host, capsys, devices, argv, message):
    host.settings.parent.mkdir(parents=True)
    host.settings.write_text(f"MG400_CAMERA_DEVICES={','.join(devices)}\n")
    before = host.settings.read_text()
    assert host.install(*argv) == 1
    assert message in capsys.readouterr().err
    assert host.settings.read_text() == before
    assert not host.data.exists() and not host.units.exists()


def process(proc_root, pid, arguments):
    directory = proc_root / str(pid)
    directory.mkdir(parents=True)
    (directory / "cmdline").write_bytes(b"\0".join(item.encode() for item in arguments) + b"\0")


def test_process_scan_finds_every_collector_except_the_service(module, tmp_path):
    proc = tmp_path / "proc"
    process(proc, 101, ["/usr/bin/python3", "/opt/mg400/scripts/gripper_feedback.py"])
    process(proc, 102, ["/usr/bin/python3", "/opt/mg400/scripts/gripper_feedback.py"])
    process(proc, 103, ["python3", "-u", "scripts/gripper_feedback.py", "--commands"])
    process(proc, 104, ["python3.12", "/old/checkout/scripts/gripper_feedback.py"])
    process(proc, 105, ["grep", "gripper_feedback.py"])
    process(proc, 106, ["python3", "-c", "print()", "gripper_feedback.py"])
    process(proc, 107, ["python3", "other.py", "gripper_feedback.py"])
    process(proc, 108, [])
    (proc / "self").mkdir()
    assert module.find_collectors(proc, 102) == [101, 103, 104]


def test_independent_collector_blocks_installation_before_any_writes(host, capsys):
    process(host.root / "proc", 123, ["python3", "scripts/gripper_feedback.py", "--commands"])
    assert host.install() == 1
    assert "123" in capsys.readouterr().err
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()


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
    assert sorted(path.relative_to(host.root).parts[0] for path in host.root.iterdir()) == [
        "opt",
        "proc",
    ]
    assert [command[0] for command in names(host.commands)] == ["/usr/bin/python3", "systemctl"]
    report = host.out.getvalue()
    assert "Would run: /usr/bin/uv sync" in report
    assert "Would run: systemctl enable --now mg400-gripper-collector.path" in report


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


def test_merge_sees_spaced_keys_and_fills_a_quoted_empty_token(module):
    example = "MG400_ROBOT_HOST=\nB_TOKEN=\n"
    current = 'MG400_ROBOT_HOST = robot.test\nB_TOKEN=""\n'
    text, changed = module.merge_settings(current, example, token=lambda: "new")
    assert text == "MG400_ROBOT_HOST = robot.test\nB_TOKEN=new\n"
    assert changed == ["B_TOKEN"]


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ("MG400_PANEL_BIND 0.0.0.0\n", "is not KEY=VALUE"),
        ("MG400_CONTROLLER_LOCK_DIR=\n", "MG400_CONTROLLER_LOCK_DIR to an absolute path"),
        ("MG400_CONTROLLER_LOCK_DIR=locks\n", "MG400_CONTROLLER_LOCK_DIR to an absolute path"),
    ],
    ids=["unreadable-line", "empty-lock-directory", "relative-lock-directory"],
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


def test_planted_key_link_is_kept_and_never_followed(host, tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    (host.data / "camera").mkdir(parents=True)
    (host.data / "camera/preview.key").symlink_to(victim)
    assert host.install() == 0
    assert victim.read_text() == "keep\n" and host.owner(victim) is None
    assert (host.data / "camera/preview.key").is_symlink()


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
    assert len(host.commands) == 2


def test_duplicate_asset_stops_before_any_host_change(host, local_asset):
    value = f"model={local_asset}"
    assert host.install("--asset", value, "--asset", value) == 1
    assert not host.settings.exists() and not host.data.exists()


def test_bad_local_asset_stops_before_any_host_change(host, local_asset):
    local_asset.write_bytes(b"corrupt")
    assert host.install("--asset", f"model={local_asset}") == 1
    assert not host.settings.exists() and not host.data.exists()
    assert len(host.commands) == 2


def test_asset_destination_link_is_refused_before_host_changes(host, local_asset, tmp_path):
    target = host.data / "models/model.dat"
    target.parent.mkdir(parents=True)
    sentinel = tmp_path / "keep.dat"
    sentinel.write_bytes(b"keep")
    target.symlink_to(sentinel)
    assert host.install("--asset", f"model={local_asset}") == 1
    assert not host.settings.exists()
    assert target.is_symlink() and sentinel.read_bytes() == b"keep"
    assert len(host.commands) == 2


def add_optional_service(
    host, *, name="optional", venv=".venv-vision", groups=("vision",), asset_names=()
):
    import json

    path = host.code / "deploy/services.toml"
    with path.open("a") as stream:
        stream.write(
            f'\n[[service]]\nname = "{name}"\nunits = ["mg400-{name}.service"]\n'
            f'optional = true\nvenv = "{venv}"\ngroups = {json.dumps(list(groups))}\n'
            f'token = "MG400_{name.upper()}_TOKEN"\nport = 8771\n'
            f"assets = {json.dumps(list(asset_names))}\n"
        )
    (host.code / "config/systemd" / f"mg400-{name}.service").write_text("[Service]\n")


def select_optional(host, value):
    example = host.code / "deploy/mg400.env.example"
    example.write_text(
        example.read_text().replace("MG400_OPTIONAL_SERVICES=", f"MG400_OPTIONAL_SERVICES={value}")
    )


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
        ('token = "MG400_PANEL_TOKEN"', 'token = "OTHER_TOKEN"'),
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
    "selection", ["unknown", "optional,optional", "optional,", ",optional", "web", "optional,,web"]
)
def test_bad_optional_selection_refuses_before_host_changes(host, selection):
    add_optional_service(host)
    select_optional(host, selection)
    assert host.install() == 1
    assert not host.settings.exists() and not host.data.exists() and not host.units.exists()
    assert len(host.commands) == 2


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
    assert syncs[0][0][4:8] == ["--group", "stage", "--group", "tools"]
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


def test_default_dry_run_preserves_existing_steps_and_adds_only_labctl(host):
    assert host.install("--dry-run") == 0
    assert host.out.getvalue().replace(str(host.root), "<host>").splitlines() == [
        "Would ensure <host>/etc/mg400 (0755, 0:0)",
        "Would create <host>/etc/mg400/mg400.env",
        "Would ensure <host>/var/lib/mg400 (0750, 0:1000)",
        "Would ensure <host>/var/lib/mg400/locks (0700, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/work (0750, 1000:1000)",
        "Would ensure <host>/var/lib/mg400/camera (0700, 1000:1000)",
        "Would create <host>/var/lib/mg400/camera/preview.key",
        "Would ensure <host>/opt/mg400 (0750, 0:1000)",
        "Would run: /usr/bin/uv sync --locked --no-default-groups "
        "--group stage --group tools --python /usr/bin/python3",
        "Would install <host>/etc/systemd/system/mg400-camera-capture.service",
        "Would install <host>/etc/systemd/system/mg400-camera-preview.service",
        "Would install <host>/etc/systemd/system/mg400-gripper-collector.path",
        "Would install <host>/etc/systemd/system/mg400-gripper-collector.service",
        "Would install <host>/etc/systemd/system/mg400-stage-panel.service",
        "Would install <host>/etc/systemd/system/mg400-web-panel.service",
        "Would run: systemctl daemon-reload",
        "Would run: systemctl enable --now mg400-gripper-collector.path",
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


def test_enabled_furnace_installs_private_state_directory_and_own_group(module, host):
    select_optional(host, "furnace")
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
    assert len(module.parse_settings(host.settings.read_text())["MG400_FURNACE_TOKEN"]) >= 24
    sync = next(command for command, _ in host.commands if command[0] == "/usr/bin/uv")
    assert sync[sync.index("furnace") - 1] == "--group"
    assert not any(
        command[:2] in (["systemctl", "start"], ["systemctl", "restart"])
        for command, _ in host.commands
    )


def test_furnace_json_setting_rejects_del(module):
    with pytest.raises(ValueError):
        module.parse_settings('MG400_FURNACE_GAS_MAP=\'{"0":"bad\x7flabel"}\'')
