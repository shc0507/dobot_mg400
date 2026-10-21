"""Install a host service that starts the gripper collector when the panel requests it."""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_DIR = Path("/etc/systemd/system")
PROC_ROOT = Path("/proc")
SERVICE = "mg400-gripper-collector.service"
PATH_UNIT = "mg400-gripper-collector.path"


def _path(value):
    value = str(value)
    if not Path(value).is_absolute() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Use absolute paths without control characters.")
    return value


def _quote(value, *, command=False):
    value = str(value).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    if command:
        value = value.replace("$", "$$")
    return f'"{value}"'


def render_units(repo, data_dir, python_executable="/usr/bin/python3"):
    """Return the service and path units for fixed host paths."""
    repo, data_dir = Path(_path(repo)), Path(_path(data_dir))
    python_executable = _path(python_executable)
    work = data_dir / "work"
    mounts = f"{_quote(repo)} {_quote(work)}"
    command = " ".join(_quote(value, command=True) for value in (
        python_executable, repo / "scripts/gripper_feedback.py",
        "--output", work / "gripper_olares_feedback.json", "--commands",
    ))
    request = str(work / "gripper_collector.start").replace("%", "%%")
    return {
        SERVICE: (
            "[Unit]\n"
            "Description=MG400 gripper feedback and position collector\n"
            f"RequiresMountsFor={mounts}\n"
            "StartLimitIntervalSec=0\n"
            "\n[Service]\n"
            "Type=exec\n"
            f"ExecStart={command}\n"
            "Restart=on-failure\n"
            "RestartSec=2s\n"
        ),
        PATH_UNIT: (
            "[Unit]\n"
            "Description=Start the MG400 gripper collector on a panel request\n"
            f"RequiresMountsFor={mounts}\n"
            "\n[Path]\n"
            f"PathChanged={request}\n"
            f"Unit={SERVICE}\n"
            "\n[Install]\n"
            "WantedBy=multi-user.target\n"
        ),
    }


def managed_pid():
    result = subprocess.run(
        ["systemctl", "show", "--property=LoadState", "--property=MainPID", SERVICE],
        capture_output=True, text=True, check=False,
    )
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if properties.get("LoadState") == "not-found":
        return 0
    if result.returncode or not properties.get("MainPID", "").isdigit():
        raise RuntimeError(f"Cannot inspect {SERVICE}: {result.stderr.strip()}")
    return int(properties["MainPID"])


def find_unmanaged_collectors(collector, managed_pid, proc_root=None):
    """Find this collector script outside the managed service."""
    collector = Path(collector).resolve()
    processes = PROC_ROOT if proc_root is None else Path(proc_root)
    found = []
    for process in processes.iterdir():
        if not process.name.isdecimal() or int(process.name) == managed_pid:
            continue
        try:
            arguments = [os.fsdecode(value) for value in process.joinpath("cmdline").read_bytes()
                         .split(b"\0") if value]
            if not arguments or not Path(arguments[0]).name.startswith("python"):
                continue
            for argument in arguments[1:]:
                if argument in ("-c", "-m"):
                    break
                if argument.startswith("-"):
                    continue
                if Path(argument).name != collector.name:
                    break
                candidate = Path(argument)
                if not candidate.is_absolute():
                    candidate = process.joinpath("cwd").resolve(strict=True) / candidate
                if candidate.resolve() == collector:
                    found.append(int(process.name))
                break
        except (FileNotFoundError, ProcessLookupError):
            continue
    return sorted(found)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT.parent / "mg400-data")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("Run this installer as root on the Olares Linux host.")
        data_dir = args.data_dir.expanduser().resolve()
        units = render_units(ROOT, data_dir)
        collector = ROOT / "scripts/gripper_feedback.py"
        if not collector.is_file():
            raise FileNotFoundError(f"Collector script not found: {collector}")
        unmanaged = find_unmanaged_collectors(collector, managed_pid())
        if unmanaged:
            pids = ", ".join(map(str, unmanaged))
            raise RuntimeError(
                "An independent gripper collector is running. "
                f"Check these PIDs before stopping it: {pids}"
            )
        (data_dir / "work").mkdir(parents=True, exist_ok=True)
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        for name, content in units.items():
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=UNIT_DIR, prefix=name + ".", delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    os.fchmod(stream.fileno(), 0o644)
                    stream.write(content)
                temporary.replace(UNIT_DIR / name)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", PATH_UNIT], check=True)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Collector installation failed: {error}", file=sys.stderr)
        return 1
    print("Installed the collector service and request watcher. "
          "Open the MG400 Control panel to request startup.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
