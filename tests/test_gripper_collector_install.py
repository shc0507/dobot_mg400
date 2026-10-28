"""Check collector installation without systemd, USB, or privileged writes."""

import runpy
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts/install_gripper_collector.py"


@pytest.fixture
def module(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", Mock(side_effect=AssertionError("No real systemctl commands"))
    )
    namespace = runpy.run_path(str(INSTALLER), run_name="test_collector_install")
    return SimpleNamespace(**namespace)


def unit_value(text, key):
    values = [line.partition("=")[2] for line in text.splitlines() if line.startswith(key + "=")]
    assert len(values) == 1
    return values[0]


@pytest.mark.parametrize(
    "component", ("", 'space "quote" \\path %name $HOME'), ids=("plain", "special")
)
def test_units_watch_one_request_and_start_one_command_capable_collector(
    module, tmp_path, component
):
    prefix = component + " " if component else ""
    repo, data = tmp_path / (prefix + "repo"), tmp_path / (prefix + "data")
    if component:
        python = str(tmp_path / (component + " python"))
        units = module.render_units(repo, data, python)
    else:
        python = "/usr/bin/python3"
        units = module.render_units(repo, data)
    assert set(units) == {module.SERVICE, module.PATH_UNIT}
    service, watch = units[module.SERVICE], units[module.PATH_UNIT]
    assert unit_value(service, "Type") == "exec"
    assert unit_value(service, "Restart") == "on-failure"
    assert unit_value(service, "RestartSec") == "2s"
    assert unit_value(service, "StartLimitIntervalSec") == "0"
    command = [
        value.replace("%%", "%").replace("$$", "$")
        for value in shlex.split(unit_value(service, "ExecStart"))
    ]
    assert command[0] == python
    assert str(repo / "scripts/gripper_feedback.py") in command
    assert "--commands" in command
    assert command[command.index("--output") + 1] == str(data / "work/gripper_olares_feedback.json")
    assert unit_value(watch, "PathChanged") == str(data / "work/gripper_collector.start").replace(
        "%", "%%"
    )
    assert unit_value(watch, "Unit") == module.SERVICE
    for text in units.values():
        mounts = [
            value.replace("%%", "%") for value in shlex.split(unit_value(text, "RequiresMountsFor"))
        ]
        assert str(repo) in mounts
        assert str(data / "work") in mounts
    subprocess.run.assert_not_called()


CONTROL_CHARACTERS = ("\0", "\n", "\r", "\t", "\x1f", "\x7f")


@pytest.mark.parametrize(
    "field,value",
    (
        ("repo", Path("relative")),
        ("data", Path("relative")),
        ("python", "relative"),
        *(("repo", "repo" + character) for character in CONTROL_CHARACTERS),
    ),
    ids=(
        "relative_repo",
        "relative_data",
        "relative_python",
        *(f"repo_control_{ord(character):#04x}" for character in CONTROL_CHARACTERS),
    ),
)
def test_unit_rendering_rejects_unsafe_paths(module, tmp_path, field, value):
    values = {"repo": tmp_path / "repo", "data": tmp_path / "data", "python": "/usr/bin/python3"}
    values[field] = value if field == "python" or isinstance(value, Path) else tmp_path / value
    with pytest.raises(ValueError):
        module.render_units(values["repo"], values["data"], values["python"])


def process(proc_root, pid, arguments, cwd):
    directory = proc_root / str(pid)
    directory.mkdir(parents=True)
    (directory / "cmdline").write_bytes(
        b"\0".join(argument.encode() for argument in arguments) + b"\0"
    )
    (directory / "cwd").symlink_to(cwd, target_is_directory=True)


def test_process_scan_finds_exact_absolute_and_relative_script_but_excludes_managed_pid(
    module, tmp_path
):
    repo, other = tmp_path / "repo", tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    proc = tmp_path / "proc"
    collector = repo / "scripts/gripper_feedback.py"
    process(proc, 101, ["/usr/bin/python3", str(collector), "--commands"], repo)
    process(proc, 102, ["/usr/bin/python3", str(collector), "--commands"], repo)
    process(proc, 103, ["python3", "scripts/gripper_feedback.py", "--commands"], repo)
    process(proc, 104, ["python3", "scripts/gripper_feedback.py", "--commands"], other)
    process(proc, 105, ["python3", str(other / "gripper_feedback.py")], repo)
    process(proc, 106, [], repo)
    (proc / "107").mkdir()
    process(proc, 108, ["grep", str(collector)], repo)
    process(proc, 109, ["python3", "-c", "print('example')", str(collector)], repo)
    process(proc, 110, ["python3", "-m", "example", str(collector)], repo)
    (proc / "not-a-pid").mkdir()
    assert sorted(module.find_unmanaged_collectors(collector, 102, proc)) == [101, 103]
    subprocess.run.assert_not_called()


@pytest.fixture
def installation(module, monkeypatch, tmp_path):
    repo, units, proc = tmp_path / "repo", tmp_path / "units", tmp_path / "proc"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/gripper_feedback.py").write_text("# Test collector, never executed.\n")
    proc.mkdir()
    for name, value in (("ROOT", repo), ("UNIT_DIR", units), ("PROC_ROOT", proc)):
        monkeypatch.setitem(module.main.__globals__, name, value)
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout="LoadState=not-found\nMainPID=0\n", stderr=""
        )
    )
    monkeypatch.setattr(subprocess, "run", run)
    return SimpleNamespace(module=module, repo=repo, units=units, proc=proc, run=run)


def test_installation_is_repeatable_and_enables_only_the_request_watcher(installation):
    setup = installation
    data = setup.repo.parent / "mg400-data"
    expected = setup.module.render_units(setup.repo, data)
    for _ in range(2):
        assert setup.module.main([]) == 0
        assert {path.name: path.read_text() for path in setup.units.iterdir()} == expected
    assert list((data / "work").iterdir()) == []
    assert (
        setup.run.call_args_list
        == [
            call(
                [
                    "systemctl",
                    "show",
                    "--property=LoadState",
                    "--property=MainPID",
                    setup.module.SERVICE,
                ],
                capture_output=True,
                text=True,
                check=False,
            ),
            call(["systemctl", "daemon-reload"], check=True),
            call(["systemctl", "enable", "--now", setup.module.PATH_UNIT], check=True),
        ]
        * 2
    )


def test_independent_collector_blocks_installation_before_any_writes(installation, capsys):
    setup = installation
    process(setup.proc, 123, ["python3", "scripts/gripper_feedback.py", "--commands"], setup.repo)
    data = setup.repo.parent / "custom data"
    assert setup.module.main(["--data-dir", str(data)]) == 1
    assert "123" in capsys.readouterr().err
    assert not data.exists()
    assert not setup.units.exists()
    assert setup.run.call_count == 1
    assert setup.run.call_args.args[0][1] == "show"


@pytest.mark.parametrize("platform,uid", (("darwin", 0), ("linux", 1000)))
def test_installer_requires_linux_root_before_system_calls(
    installation, monkeypatch, platform, uid
):
    setup = installation
    monkeypatch.setattr(setup.module.sys, "platform", platform)
    monkeypatch.setattr(setup.module.os, "geteuid", lambda: uid)
    assert setup.module.main([]) == 1
    setup.run.assert_not_called()
    assert not setup.units.exists()


def test_daemon_reload_failure_does_not_enable_watcher(installation, capsys):
    setup = installation
    setup.run.side_effect = [
        setup.run.return_value,
        subprocess.CalledProcessError(1, ["systemctl", "daemon-reload"]),
    ]
    assert setup.module.main([]) == 1
    assert "Collector installation failed" in capsys.readouterr().err
    assert setup.run.call_count == 2
    assert setup.run.call_args.args[0] == ["systemctl", "daemon-reload"]
