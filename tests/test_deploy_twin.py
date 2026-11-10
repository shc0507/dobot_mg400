"""Exercise incremental Twin deployments without SSH, systemd, or root rights."""

import importlib.util
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = "src/orchestrator/web/twin.py"
STATIC = "src/orchestrator/web/static/console/twin.js"
CAMERA = "scripts/camera_preview.py"


@pytest.fixture
def twin(monkeypatch):
    spec = importlib.util.spec_from_file_location("deploy_twin", ROOT / "deploy/twin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.os, "chown", Mock(), raising=False)
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=AssertionError("Real command")))
    monkeypatch.setattr(
        module.subprocess, "check_output", Mock(side_effect=AssertionError("Real command"))
    )
    monkeypatch.setattr(
        module.urllib.request, "urlopen", Mock(side_effect=AssertionError("Real HTTP request"))
    )
    return module


@pytest.fixture
def host(twin, monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    data = tmp_path / "data"
    data.mkdir()
    contents = {
        BACKEND: b"VALUE = 1\n",
        STATIC: b"const value = 1;\n",
        CAMERA: b"VALUE = 1\n",
        "pyproject.toml": b"[project]\nname = 'twin'\n",
        "uv.lock": b"version = 1\n",
    }
    for relative, content in contents.items():
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    venv = checkout / ".venv"
    venv.mkdir()
    (venv / "package.py").write_bytes(b"old dependency\n")
    manifest = {
        "branch": "twin-test",
        "base_commit": "abc123",
        "files": {path: twin.digest(content) for path, content in contents.items()},
    }
    manifest_path = data / "deployment-manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(twin, "CHECKOUT", checkout)
    monkeypatch.setattr(twin, "DATA", data)
    monkeypatch.setattr(twin, "MANIFEST", manifest_path)
    monkeypatch.setattr(twin, "CAMERA_DROP_IN", tmp_path / "twin-sampling.conf")
    state = SimpleNamespace(
        contents=contents,
        before=manifest,
        active=True,
        camera_active=True,
        commands=[],
        services=[],
        camera_services=[],
        operations=[],
        camera_start=None,
        camera_exec=None,
        sync=None,
    )

    def run(command, **options):
        state.commands.append(command)
        if command == ["systemctl", "is-active", "--quiet", twin.SERVICE]:
            return SimpleNamespace(returncode=0 if state.active else 3)
        if command == ["systemctl", "is-active", "--quiet", twin.CAMERA_SERVICE]:
            return SimpleNamespace(returncode=0 if state.camera_active else 3)
        if command == ["systemctl", "show", "--property=ExecStart", "--value", twin.CAMERA_SERVICE]:
            return SimpleNamespace(returncode=0, stdout=state.camera_exec)
        if command[1:2] == ["sync"]:
            if state.sync:
                state.sync(command)
        elif command[0] not in {"chown", "chmod"}:
            raise AssertionError(f"Unexpected command: {command}")
        return SimpleNamespace(returncode=0)

    def service(action, name=twin.SERVICE):
        assert name in {twin.SERVICE, twin.CAMERA_SERVICE}
        state.operations.append((action, name))
        if name == twin.CAMERA_SERVICE:
            state.camera_services.append(action)
            if action == "start" and state.camera_start:
                state.camera_start()
            state.camera_active = action == "start"
        else:
            state.services.append(action)
            state.active = action == "start"

    monkeypatch.setattr(twin.subprocess, "run", run)
    monkeypatch.setattr(twin, "service", service)
    monkeypatch.setattr(twin, "ready", Mock())
    return state


def enable_camera(twin, host):
    python = twin.CHECKOUT / ".venv/bin/python"
    command = f"{python} {twin.CHECKOUT / CAMERA}"
    twin.CAMERA_DROP_IN.write_text(f"[Service]\nExecStart=\nExecStart={command}\n")
    host.camera_exec = f"{{ path={python} ; argv[]={command} ; ignore_errors=no ; }}"


def bundle(twin, host, tmp_path, updates=None, removed=(), *, corrupt=False, before=None):
    updates = updates or {}
    files = host.before["files"] | {
        relative: twin.digest(content) for relative, content in updates.items()
    }
    for relative in removed:
        del files[relative]
    after = {**host.before, "files": files}
    payload = {
        "before": before or host.before,
        "after": after,
        "modes": {relative: 0o644 for relative in updates},
    }
    entries = {"manifest.json": json.dumps(payload).encode()}
    entries.update({"files/" + path: content for path, content in updates.items()})
    if corrupt:
        entries["files/" + next(iter(updates))] = b"corrupt"
    archive = tmp_path / "patch.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name, content in entries.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            stream.addfile(member, io.BytesIO(content))
    return archive, after


def assert_original(twin, host):
    assert json.loads(twin.MANIFEST.read_text()) == host.before
    for relative, content in host.contents.items():
        assert (twin.CHECKOUT / relative).read_bytes() == content
    assert (twin.CHECKOUT / ".venv/package.py").read_bytes() == b"old dependency\n"


def test_changes_include_additions_edits_and_deletions(twin):
    assert twin.changes(
        {"same": "a", "edit": "b", "gone": "c"}, {"same": "a", "edit": "d", "new": "e"}
    ) == (
        ["edit", "new"],
        ["gone"],
    )


@pytest.mark.parametrize(
    ("path", "restart"),
    [
        (STATIC, False),
        ("docs/deploy.md", False),
        (BACKEND, True),
        ("src/orchestrator/web/templates/dashboard.html", True),
        ("config/trays/example.json", True),
        ("recipes/test.toml", True),
        ("uv.lock", True),
        ("pyproject.toml", True),
    ],
)
def test_restart_classification(twin, path, restart):
    assert twin.needs_restart([path]) is restart


@pytest.mark.parametrize(
    ("path", "restart"),
    [
        (CAMERA, True),
        ("src/cameras/config.py", True),
        ("src/cameras/jpeg.py", True),
        ("uv.lock", True),
        ("pyproject.toml", True),
        (STATIC, False),
        (BACKEND, False),
        ("scripts/console_shots.py", False),
    ],
)
def test_camera_restart_classification(twin, path, restart):
    assert twin.needs_camera_restart([path]) is restart


@pytest.mark.parametrize("response", [{"top": 30.0}, []])
def test_camera_readiness_uses_preview_status_without_capturing(twin, monkeypatch, response):
    request = Mock(return_value=io.BytesIO(json.dumps(response).encode()))
    monkeypatch.setattr(twin.urllib.request, "urlopen", request)
    monkeypatch.setattr(twin.time, "monotonic", Mock(side_effect=[0, 16]))
    if isinstance(response, dict):
        twin.ready(camera=True)
    else:
        with pytest.raises(RuntimeError, match="camera preview did not become ready"):
            twin.ready(camera=True)
    assert request.call_args.args[0].full_url == "http://127.0.0.1:1985/fps.json"


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "/absolute.py",
        "C:/outside.py",
        ".git/config",
        ".venv/file",
        "work/file",
        "a\\b",
    ],
)
def test_source_path_rejects_unsafe_names(twin, tmp_path, path):
    with pytest.raises(ValueError, match="Invalid deployment path"):
        twin.source_path(tmp_path, path)


@pytest.mark.parametrize("kind", ["path", "checksum", "stale"])
def test_invalid_archive_leaves_running_instance_unchanged(twin, host, tmp_path, kind):
    updates = {"../outside.py" if kind == "path" else BACKEND: b"VALUE = 2\n"}
    before = {**host.before, "base_commit": "stale"} if kind == "stale" else None
    archive, _ = bundle(twin, host, tmp_path, updates, corrupt=kind == "checksum", before=before)
    with pytest.raises((ValueError, RuntimeError)):
        twin.apply(archive)
    assert_original(twin, host)
    assert host.services == []
    assert host.commands == []
    assert not (twin.DATA / "update").exists()


@pytest.mark.parametrize("kind", ["link", "duplicate"])
def test_archive_rejects_links_and_duplicate_entries(twin, host, tmp_path, kind):
    archive = tmp_path / "invalid.tar"
    with tarfile.open(archive, "w") as stream:
        member = tarfile.TarInfo("manifest.json")
        if kind == "link":
            member.type = tarfile.SYMTYPE
            member.linkname = "../../outside"
            stream.addfile(member)
        else:
            stream.addfile(member, io.BytesIO())
            stream.addfile(member, io.BytesIO())
    with pytest.raises(ValueError, match="regular files|duplicate paths"):
        twin.apply(archive)
    assert_original(twin, host)
    assert host.services == []
    assert host.commands == []


def test_local_noop_does_not_upload_or_restart(twin, host, monkeypatch, capsys):
    monkeypatch.setattr(twin, "__file__", str(twin.CHECKOUT / "deploy/twin.py"))
    commands = []

    def output(command, **options):
        commands.append(command)
        if command[0] == "ssh":
            return json.dumps(host.before).encode()
        if command[1:3] == ["ls-files", "--stage"]:
            return b""
        if command[1] == "ls-files":
            return ("\0".join(host.contents) + "\0").encode()
        if command[1] == "branch":
            return host.before["branch"]
        if command[1] == "rev-parse":
            return host.before["base_commit"]
        raise AssertionError(command)

    monkeypatch.setattr(twin.subprocess, "check_output", output)
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(twin.urllib.request, "urlopen", Mock(return_value=response))
    twin.local(check=False)
    assert sum(command[0] == "ssh" for command in commands) == 1
    assert host.commands == []
    assert host.services == []
    assert "already match" in capsys.readouterr().out


@pytest.mark.parametrize("path", [STATIC, BACKEND])
def test_success_commits_files_and_manifest(twin, host, tmp_path, path):
    content = b"VALUE = 2\n"
    archive, after = bundle(twin, host, tmp_path, {path: content})
    twin.apply(archive)
    assert (twin.CHECKOUT / path).read_bytes() == content
    assert json.loads(twin.MANIFEST.read_text()) == after
    assert host.services == (["stop", "start"] if path == BACKEND else [])
    assert len(host.commands) == 1
    assert host.active
    assert not (twin.DATA / "update").exists()


@pytest.mark.parametrize(
    "override",
    ["missing", "unrelated", "wrong-section", "no-reset", "production", "not-loaded", "multiple"],
)
def test_camera_update_requires_the_loaded_test_override(twin, host, tmp_path, override):
    enable_camera(twin, host)
    if override == "missing":
        twin.CAMERA_DROP_IN.unlink()
    elif override == "unrelated":
        twin.CAMERA_DROP_IN.write_text("[Service]\nEnvironment=OTHER=1\n")
    elif override == "wrong-section":
        twin.CAMERA_DROP_IN.write_text(
            twin.CAMERA_DROP_IN.read_text().replace("[Service]", "[Unit]")
        )
    elif override == "no-reset":
        twin.CAMERA_DROP_IN.write_text(twin.CAMERA_DROP_IN.read_text().replace("ExecStart=\n", ""))
    elif override == "production":
        twin.CAMERA_DROP_IN.write_text(
            "[Service]\nExecStart=\n"
            "ExecStart=/opt/mg400/.venv/bin/python scripts/camera_preview.py\n"
        )
    elif override == "not-loaded":
        host.camera_exec = "{ path=/opt/mg400/.venv/bin/python ; argv[]=production ; }"
    elif override == "multiple":
        host.camera_exec += " { path=/bin/true ; argv[]=/bin/true ; }"
    archive, after = bundle(twin, host, tmp_path, {CAMERA: b"VALUE = 2\n"})
    twin.apply(archive)
    assert json.loads(twin.MANIFEST.read_text()) == after
    assert host.camera_services == []
    assert host.camera_active
    assert ["systemctl", "is-active", "--quiet", twin.CAMERA_SERVICE] not in host.commands


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("path", [CAMERA, "src/cameras/config.py", "uv.lock"])
def test_camera_update_preserves_state_and_stops_before_writes(
    twin, host, tmp_path, monkeypatch, active, path
):
    enable_camera(twin, host)
    host.camera_active = active
    write = twin.atomic_write

    def write_while_stopped(target, content, *args):
        if target == twin.CHECKOUT / path:
            assert not host.camera_active
            plan = json.loads((twin.DATA / "update/plan.json").read_text())
            assert plan["camera_restart"]
            assert plan["camera_was_active"] is active
            assert plan["was_active"]
        return write(target, content, *args)

    monkeypatch.setattr(twin, "atomic_write", write_while_stopped)
    archive, after = bundle(twin, host, tmp_path, {path: b"VALUE = 2\n"})
    twin.apply(archive)
    assert json.loads(twin.MANIFEST.read_text()) == after
    assert host.camera_services == ["stop"] + (["start"] if active else [])
    assert host.camera_active is active
    assert host.active
    assert host.services == ([] if path == CAMERA else ["stop", "start"])


def test_unrelated_update_does_not_touch_enabled_camera(twin, host, tmp_path):
    enable_camera(twin, host)
    archive, _ = bundle(twin, host, tmp_path, {BACKEND: b"VALUE = 2\n"})
    twin.apply(archive)
    assert host.camera_services == []
    assert all(twin.CAMERA_SERVICE not in command for command in host.commands)


@pytest.mark.parametrize("console_active", [False, True])
@pytest.mark.parametrize("camera_active", [False, True])
def test_camera_rollback_restores_both_service_states(
    twin, host, tmp_path, console_active, camera_active
):
    enable_camera(twin, host)
    host.active = console_active
    host.camera_active = camera_active
    archive, _ = bundle(twin, host, tmp_path, {CAMERA: b"VALUE = 2\n", BACKEND: b"VALUE = 2\n"})
    twin.ready.side_effect = ([None] if camera_active else []) + [
        RuntimeError("startup failed"),
        None,
        None,
    ]
    with pytest.raises(RuntimeError, match="startup failed"):
        twin.apply(archive)
    assert_original(twin, host)
    assert host.active is console_active
    assert host.camera_active is camera_active
    camera_actions = ["stop"] + (["start"] if camera_active else [])
    assert host.camera_services == camera_actions * 2
    assert host.services == ["stop", "start", "stop"] + (["start"] if console_active else [])
    assert not (twin.DATA / "update").exists()


def test_camera_health_failure_rolls_back_source_and_services(twin, host, tmp_path):
    enable_camera(twin, host)
    archive, _ = bundle(twin, host, tmp_path, {CAMERA: b"VALUE = 2\n", BACKEND: b"VALUE = 2\n"})
    twin.ready.side_effect = [RuntimeError("camera not ready"), None, None]
    with pytest.raises(RuntimeError, match="camera not ready"):
        twin.apply(archive)
    assert_original(twin, host)
    assert host.camera_active and host.active
    assert host.camera_services == ["stop", "start", "stop", "start"]
    assert host.services == ["stop", "stop", "start"]
    assert twin.ready.call_args_list[0].kwargs == {"camera": True}
    assert not (twin.DATA / "update").exists()


def test_camera_start_failure_rolls_back_source_and_services(twin, host, tmp_path):
    enable_camera(twin, host)
    archive, _ = bundle(twin, host, tmp_path, {CAMERA: b"VALUE = 2\n", BACKEND: b"VALUE = 2\n"})
    host.camera_start = Mock(side_effect=[RuntimeError("camera failed"), None])
    with pytest.raises(RuntimeError, match="camera failed"):
        twin.apply(archive)
    assert_original(twin, host)
    assert host.camera_services == ["stop", "start", "stop", "start"]
    assert host.services == ["stop", "stop", "start"]
    assert host.camera_active and host.active
    assert not (twin.DATA / "update").exists()


@pytest.mark.parametrize("active", [True, False])
def test_startup_failure_restores_files_and_initial_service_state(twin, host, tmp_path, active):
    host.active = active
    addition = "src/orchestrator/web/added.py"
    archive, _ = bundle(
        twin,
        host,
        tmp_path,
        {BACKEND: b"VALUE = 2\n", addition: b"NEW = True\n"},
        removed=(STATIC,),
    )
    twin.ready.side_effect = [RuntimeError("startup failed"), None]
    with pytest.raises(RuntimeError, match="startup failed"):
        twin.apply(archive)
    assert_original(twin, host)
    assert not (twin.CHECKOUT / addition).exists()
    assert host.active is active
    assert host.services == ["stop", "start", "stop"] + (["start"] if active else [])
    assert not (twin.DATA / "update").exists()


def test_static_failure_returns_initially_inactive_service_to_stopped(twin, host, tmp_path):
    host.active = False
    archive, _ = bundle(twin, host, tmp_path, {STATIC: b"const value = 2;\n"})
    twin.ready.side_effect = RuntimeError("startup failed")
    with pytest.raises(RuntimeError, match="startup failed"):
        twin.apply(archive)
    assert_original(twin, host)
    assert host.services == ["start", "stop"]
    assert not host.active


def test_file_write_failure_restores_previous_source(twin, host, tmp_path, monkeypatch):
    archive, _ = bundle(twin, host, tmp_path, {BACKEND: b"VALUE = 2\n"})
    write = twin.atomic_write

    def fail_write(path, content, *args):
        if path == twin.CHECKOUT / BACKEND and content == b"VALUE = 2\n":
            raise OSError("disk error")
        return write(path, content, *args)

    monkeypatch.setattr(twin, "atomic_write", fail_write)
    with pytest.raises(OSError, match="disk error"):
        twin.apply(archive)
    assert_original(twin, host)
    assert host.services == ["stop", "stop", "start"]
    assert host.active


def test_failed_rollback_retains_transaction_for_recovery(twin, host, tmp_path):
    archive, _ = bundle(twin, host, tmp_path, {BACKEND: b"VALUE = 2\n"})
    twin.ready.side_effect = RuntimeError("not ready")
    with pytest.raises(RuntimeError, match="not ready"):
        twin.apply(archive)
    assert (twin.DATA / "update/plan.json").exists()
    assert_original(twin, host)
    twin.ready.side_effect = None
    twin.recover()
    assert host.active
    assert not (twin.DATA / "update").exists()


@pytest.mark.parametrize("camera_enabled", [False, True])
def test_dependency_sync_failure_restores_environment(twin, host, tmp_path, camera_enabled):
    if camera_enabled:
        enable_camera(twin, host)
    archive, _ = bundle(twin, host, tmp_path, {"uv.lock": b"version = 2\n"})

    def fail_sync(command):
        if camera_enabled:
            assert not host.camera_active
        (twin.CHECKOUT / ".venv/package.py").write_bytes(b"partial dependency update\n")
        (twin.CHECKOUT / ".venv/added.py").write_bytes(b"new dependency\n")
        raise subprocess.CalledProcessError(1, command)

    host.sync = fail_sync
    with pytest.raises(subprocess.CalledProcessError):
        twin.apply(archive)
    assert_original(twin, host)
    assert not (twin.CHECKOUT / ".venv/added.py").exists()
    assert host.active
    assert host.services == ["stop", "stop", "start"]
    assert host.camera_services == (["stop", "stop", "start"] if camera_enabled else [])
    assert host.camera_active
    assert not (twin.DATA / "update").exists()


@pytest.mark.parametrize("committed", [False, True])
def test_recover_interrupted_transaction(twin, host, committed):
    transaction = twin.DATA / "update"
    backup = transaction / "files" / BACKEND
    backup.parent.mkdir(parents=True)
    shutil.copy2(twin.CHECKOUT / BACKEND, backup)
    content = b"VALUE = 2\n"
    (twin.CHECKOUT / BACKEND).write_bytes(content)
    after = {**host.before, "files": host.before["files"] | {BACKEND: twin.digest(content)}}
    plan = dict(before=host.before, after=after, paths=[BACKEND], restart=True, was_active=True)
    (transaction / "plan.json").write_text(json.dumps(plan))
    if committed:
        twin.MANIFEST.write_text(json.dumps(after))
    twin.recover()
    if committed:
        assert (twin.CHECKOUT / BACKEND).read_bytes() == content
        assert json.loads(twin.MANIFEST.read_text()) == after
        assert host.services == []
    else:
        assert_original(twin, host)
        assert host.services == ["stop", "start"]
    assert not transaction.exists()


@pytest.mark.parametrize("console_active", [False, True])
@pytest.mark.parametrize("camera_active", [False, True])
def test_camera_recovery_uses_journal_service_states(twin, host, console_active, camera_active):
    enable_camera(twin, host)
    transaction = twin.DATA / "update"
    backup = transaction / "files" / CAMERA
    backup.parent.mkdir(parents=True)
    shutil.copy2(twin.CHECKOUT / CAMERA, backup)
    content = b"VALUE = 2\n"
    (twin.CHECKOUT / CAMERA).write_bytes(content)
    after = {**host.before, "files": host.before["files"] | {CAMERA: twin.digest(content)}}
    plan = dict(
        before=host.before,
        after=after,
        paths=[CAMERA],
        restart=True,
        was_active=console_active,
        camera_restart=True,
        camera_was_active=camera_active,
    )
    (transaction / "plan.json").write_text(json.dumps(plan))
    host.active = host.camera_active = False
    twin.CAMERA_DROP_IN.unlink()
    with pytest.raises(RuntimeError, match="Twin camera override"):
        twin.recover()
    assert transaction.exists()
    assert host.operations == []
    assert (twin.CHECKOUT / CAMERA).read_bytes() == content
    enable_camera(twin, host)
    twin.recover()
    assert_original(twin, host)
    assert host.active is console_active
    assert host.camera_active is camera_active
    assert host.camera_services == ["stop"] + (["start"] if camera_active else [])
    assert not transaction.exists()
