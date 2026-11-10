"""Update the existing Olares Digital Twin test instance from this checkout.

Run ``python deploy/twin.py`` on the development PC. Use ``--check`` to list
changes. SSH uses the existing Olares key and prompts for its passphrase.
"""

import argparse
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath
from uuid import uuid4

CHECKOUT = Path("/opt/mg400-twin-test")
DATA = Path("/var/lib/mg400/twin-test")
MANIFEST = DATA / "deployment-manifest.json"
SERVICE = "mg400-twin-test.service"
CAMERA_SERVICE = "mg400-camera-preview.service"
CAMERA_DROP_IN = Path("/etc/systemd/system/mg400-camera-preview.service.d/twin-sampling.conf")
URL = "http://100.64.0.1:8082/lab/"
DEPENDENCIES = {"pyproject.toml", "uv.lock"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def changes(before, after):
    changed = sorted(path for path, value in after.items() if before.get(path) != value)
    removed = sorted(before.keys() - after.keys())
    return changed, removed


def needs_restart(paths):
    return any(
        path in DEPENDENCIES
        or path.startswith(("recipes/", "config/"))
        or (path.startswith("src/") and not path.startswith("src/orchestrator/web/static/"))
        for path in paths
    )


def needs_camera_restart(paths):
    return any(
        path in DEPENDENCIES
        or path == "scripts/camera_preview.py"
        or path.startswith("src/cameras/")
        for path in paths
    )


def camera_test_enabled():
    if not CAMERA_DROP_IN.is_file():
        return False
    section, commands = "", []
    for line in CAMERA_DROP_IN.read_text().splitlines():
        line = line.strip()
        if line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "Service" and "=" in line:
            key, value = line.split("=", 1)
            if key.strip() == "ExecStart":
                commands.append(value.strip())
    python = CHECKOUT / ".venv/bin/python"
    command = f"{python} {CHECKOUT / 'scripts/camera_preview.py'}"
    if commands != ["", command]:
        return False
    loaded = subprocess.run(
        ["systemctl", "show", "--property=ExecStart", "--value", CAMERA_SERVICE],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return loaded.count("argv[]=") == 1 and f"path={python} ; argv[]={command} ;" in loaded


def source_path(root, relative):
    path = PurePosixPath(relative)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in relative
        or ":" in relative
        or path.as_posix() != relative
        or path.parts[0] in {".git", ".venv", "work"}
    ):
        raise ValueError(f"Invalid deployment path {relative!r}.")
    target = root / path
    if any(part.is_symlink() for part in (target, *target.parents)):
        raise ValueError(f"Deployment path contains a symbolic link {relative!r}.")
    return target


def atomic_write(path, data, mode=0o640):
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    for parent in missing:
        os.chown(parent, 0, 1000)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(mode)
            os.chown(temporary, 0, 1000)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    if path.suffix == ".py":
        for cache in (path.parent / "__pycache__").glob(path.stem + ".*.pyc"):
            cache.unlink()


def service(action, name=SERVICE):
    subprocess.run(["systemctl", action, name], check=True)


def is_active(name):
    return subprocess.run(["systemctl", "is-active", "--quiet", name], check=False).returncode == 0


def ready(*, camera=False):
    name = "camera preview" if camera else "test Console"
    url = "http://127.0.0.1:1985/fps.json" if camera else URL + "api/console/ws/twin/state"
    deadline = time.monotonic() + 15
    while True:
        try:
            request = urllib.request.Request(url, headers={"X-MG400-Client": "deploy-check"})
            with urllib.request.urlopen(request, timeout=2) as response:
                state = json.load(response)
            if not isinstance(state, dict) or (not camera and "cameras" not in state):
                raise ValueError(f"The {name} returned an invalid state.")
            return
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"The {name} did not become ready.") from None
            time.sleep(0.25)


def verify_files(manifest):
    for relative, expected in manifest["files"].items():
        path = source_path(CHECKOUT, relative)
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise RuntimeError(f"Olares source changed outside this deployment at {relative}.")


def restore(transaction, plan):
    camera_restart = plan.get("camera_restart", False)
    if camera_restart and not camera_test_enabled():
        raise RuntimeError("Restore the Twin camera override before recovery.")
    if plan["restart"] or not plan["was_active"]:
        service("stop")
    if camera_restart:
        service("stop", CAMERA_SERVICE)
    for relative in plan["paths"]:
        target = source_path(CHECKOUT, relative)
        backup = transaction / "files" / relative
        if backup.exists():
            atomic_write(target, backup.read_bytes(), backup.stat().st_mode & 0o777)
        else:
            target.unlink(missing_ok=True)
    if (transaction / "venv").exists():
        if (CHECKOUT / ".venv").exists():
            shutil.rmtree(CHECKOUT / ".venv")
        shutil.copytree(transaction / "venv", CHECKOUT / ".venv", symlinks=True)
        subprocess.run(["chown", "-R", "root:1000", str(CHECKOUT / ".venv")], check=True)
    atomic_write(MANIFEST, json.dumps(plan["before"], indent=2).encode())
    if camera_restart and plan["camera_was_active"]:
        service("start", CAMERA_SERVICE)
        ready(camera=True)
    if plan["restart"] and plan["was_active"]:
        service("start")
        ready()


def recover():
    transaction = DATA / "update"
    journal = transaction / "plan.json"
    if journal.exists():
        plan = json.loads(journal.read_text())
        if json.loads(MANIFEST.read_text()) != plan["after"]:
            restore(transaction, plan)
    if transaction.exists():
        shutil.rmtree(transaction)


def apply(archive):
    before = json.loads(MANIFEST.read_text())
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        if any(not member.isfile() for member in members):
            raise ValueError("The deployment archive must contain only regular files.")
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise ValueError("The deployment archive contains duplicate paths.")
        payload = json.load(bundle.extractfile("manifest.json"))
        if before != payload["before"]:
            raise RuntimeError("Another deployment completed. Run this command again.")
        after = payload["after"]
        changed, removed = changes(before["files"], after["files"])
        if set(names) != {"manifest.json", *("files/" + path for path in changed)}:
            raise ValueError("The deployment archive does not match its manifest.")
        contents = {}
        for relative in changed:
            source_path(CHECKOUT, relative)
            content = bundle.extractfile("files/" + relative).read()
            if digest(content) != after["files"][relative]:
                raise ValueError(f"Invalid uploaded checksum for {relative}.")
            if relative.endswith(".py"):
                compile(content, relative, "exec")
            contents[relative] = content

    verify_files(before)
    paths = changed + removed
    restart = needs_restart(paths)
    dependency_change = bool(DEPENDENCIES.intersection(paths))
    camera_restart = needs_camera_restart(paths) and camera_test_enabled()
    was_active = is_active(SERVICE)
    camera_was_active = is_active(CAMERA_SERVICE) if camera_restart else None
    plan = dict(
        before=before,
        after=after,
        paths=paths,
        restart=restart,
        was_active=was_active,
        camera_restart=camera_restart,
        camera_was_active=camera_was_active,
    )
    transaction = DATA / "update"
    transaction.mkdir()
    for relative in paths:
        path = source_path(CHECKOUT, relative)
        if relative not in before["files"] and path.exists():
            raise RuntimeError(f"The new deployment would overwrite an unmanaged file {relative}.")
        if path.exists():
            backup = transaction / "files" / relative
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup)
    if dependency_change:
        shutil.copytree(CHECKOUT / ".venv", transaction / "venv", symlinks=True)
    atomic_write(transaction / "plan.json", json.dumps(plan).encode())
    try:
        if restart:
            service("stop")
        if camera_restart:
            service("stop", CAMERA_SERVICE)
        for relative, content in contents.items():
            path = source_path(CHECKOUT, relative)
            atomic_write(path, content, payload["modes"][relative])
        for relative in removed:
            source_path(CHECKOUT, relative).unlink()
        if dependency_change:
            subprocess.run(
                [
                    "/home/olares/.local/bin/uv",
                    "sync",
                    "--project",
                    str(CHECKOUT),
                    "--locked",
                    "--no-default-groups",
                    "--group",
                    "orchestrator",
                    "--group",
                    "dev",
                    "--link-mode",
                    "copy",
                ],
                check=True,
            )
            subprocess.run(["chown", "-R", "root:1000", str(CHECKOUT / ".venv")], check=True)
            subprocess.run(["chmod", "-R", "g+rX", str(CHECKOUT / ".venv")], check=True)
        if camera_restart and camera_was_active:
            service("start", CAMERA_SERVICE)
            ready(camera=True)
        if restart or not was_active:
            service("start")
        ready()
        atomic_write(MANIFEST, json.dumps(after, indent=2).encode())
    except BaseException:
        restore(transaction, plan)
        shutil.rmtree(transaction)
        raise
    shutil.rmtree(transaction)
    print(f"Updated {len(changed)} files and removed {len(removed)} files.")
    print("Test Console restarted." if restart else "Applied without a test Console restart.")
    if camera_restart and camera_was_active:
        print("Shared camera preview restarted with the Twin override.")


def remote(archive):
    import fcntl

    os.umask(0o027)
    with (DATA / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        recover()
        if archive:
            # Recovery must remain available if the uploaded session is interrupted.
            atomic_write(DATA / "deployer.py", Path(__file__).read_bytes())
            apply(archive)
        else:
            manifest = json.loads(MANIFEST.read_text())
            verify_files(manifest)
            print(json.dumps(manifest))


def local(check):
    root = Path(__file__).resolve().parents[1]
    key = Path.home() / ".ssh" / "olares_ed25519"
    known_hosts = root / "work" / "olares-known_hosts"
    options = [
        "-i",
        str(key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
    ]
    host = "olares@100.64.0.1"
    status = (
        "if sudo -n test -f /var/lib/mg400/twin-test/deployer.py; then "
        "sudo -n /usr/bin/python3 /var/lib/mg400/twin-test/deployer.py --status; "
        "else sudo -n cat /var/lib/mg400/twin-test/deployment-manifest.json; fi"
    )
    before = json.loads(subprocess.check_output(["ssh", *options, host, status]))
    names = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
        )
        .decode()
        .split("\0")
    )
    files, modes = {}, {}
    index = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=root)
    for entry in index.decode().split("\0"):
        if entry:
            metadata, relative = entry.split("\t", 1)
            modes[relative] = 0o755 if metadata.startswith("100755 ") else 0o644
    for relative in sorted(set(names) - {""}):
        path = source_path(root, relative)
        if path.is_file():
            files[relative] = path.read_bytes()
            modes.setdefault(relative, 0o644)
    after = dict(
        branch=subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=root, text=True
        ).strip(),
        base_commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        files={relative: digest(content) for relative, content in files.items()},
    )
    changed, removed = changes(before["files"], after["files"])
    if not changed and not removed:
        with urllib.request.urlopen(URL, timeout=5) as response:
            if response.status != 200:
                raise RuntimeError("The deployed Console is unavailable.")
        print("The deployed files already match this checkout.")
        return
    print(f"{len(changed)} changed files, {len(removed)} removed files.", flush=True)
    if check:
        print(
            "\n".join([*("Update " + path for path in changed), *("Remove " + p for p in removed)])
        )
        return
    with tempfile.TemporaryDirectory(prefix="mg400-twin-") as temporary:
        name = "twin-update-" + uuid4().hex
        archive = Path(temporary) / (name + ".tar.gz")
        helper = Path(temporary) / (name + ".py")
        shutil.copyfile(__file__, helper)
        with tarfile.open(archive, "w:gz") as bundle:
            payload = json.dumps(dict(before=before, after=after, modes=modes)).encode()
            entries = {"manifest.json": payload, **{"files/" + p: files[p] for p in changed}}
            for name, content in entries.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                bundle.addfile(info, io.BytesIO(content))
        remote_archive = "/home/olares/" + archive.name
        remote_helper = "/home/olares/" + helper.name
        subprocess.run(
            ["scp", *options, str(archive), str(helper), f"{host}:/home/olares/"], check=True
        )
        command = shlex.join(
            ["sudo", "-n", "/usr/bin/python3", remote_helper, "--apply", remote_archive]
        )
        cleanup = shlex.join(["rm", "-f", remote_archive, remote_helper])
        command += "; twin_result=$?; " + cleanup + '; exit "$twin_result"'
        subprocess.run(["ssh", *options, host, command], check=True)
    print(URL + "#twin")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="List the files that need an update.")
    parser.add_argument("--status", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--apply", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    started = time.monotonic()
    try:
        if args.status or args.apply:
            remote(args.apply)
        else:
            local(args.check)
            print(f"Completed in {time.monotonic() - started:.1f} seconds.")
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Deployment failed. {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
