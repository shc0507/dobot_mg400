#!/usr/bin/env python3
"""Install or update the MG400 services on this Linux host.

Run as root with the system Python, from the checkout at /opt/mg400:

    sudo /usr/bin/python3 /opt/mg400/deploy/install.py

The installer never starts, stops or restarts a robot service.
"""

import argparse
import errno
import grp
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assets  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1]
SERVICE_UID = SERVICE_GID = 1000
COLLECTOR = "mg400-gripper-collector.service"
REQUEST_WATCHER = "mg400-gripper-collector.path"
CAMERA_COUNT = 3
DEVICE = re.compile(r"/dev/[A-Za-z0-9/._:-]+")
KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
JSON_SETTINGS = {"MG400_FURNACE_GAS_MAP", "MG400_FURNACE_IDLE_GAS"}


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def finite_json_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Nonfinite JSON number")
    return number


class Host:
    """Fixed install locations, below a root that tests can replace."""

    def __init__(self, root="/", source=SOURCE):
        root = Path(root)
        self.root = root
        self.source = Path(source)
        self.asset_manifest = self.source / "config/assets.toml"
        self.registry = self.source / "deploy/services.toml"
        self.code = root / "opt/mg400"
        self.etc = root / "etc/mg400"
        self.settings = self.etc / "mg400.env"
        self.go2rtc_config = self.etc / "go2rtc.yaml"
        self.data = root / "var/lib/mg400"
        self.go2rtc = root / "usr/local/lib/mg400/go2rtc"
        self.units = root / "etc/systemd/system"
        self.command = root / "usr/local/bin/mg400ctl"
        self.lab_command = root / "usr/local/bin/labctl"
        self.udev = root / "etc/udev/rules.d"
        self.proc = root / "proc"
        self.dev = root / "dev"


SERVICE_FIELDS = {"name", "units", "optional", "venv", "groups", "token", "port", "assets"}
SERVICE_NAME = re.compile(r"[a-z][a-z0-9-]*")
UNIT_NAME = re.compile(r"mg400-[a-z0-9-]+\.service")


def load_services(path):
    """Read the registry without starting a service or touching host settings.

    The first unit starts its companion units through systemd dependencies.
    An enabled service can supply config/udev/70-mg400-<name>.rules.in.
    """
    with assets._local(path) as stream:
        data = tomllib.load(stream)
    if set(data) != {"service"} or not isinstance(data["service"], list) or not data["service"]:
        raise ValueError("Service registry must contain nonempty [[service]] entries.")
    result = {}
    used_units, used_assets, used_tokens, used_ports = set(), set(), set(), set()
    for entry in data["service"]:
        if not isinstance(entry, dict) or set(entry) != SERVICE_FIELDS:
            raise ValueError("Service registry entry has missing or unknown fields.")
        name = entry["name"]
        if not isinstance(name, str) or not SERVICE_NAME.fullmatch(name) or name == "all":
            raise ValueError("Invalid service name.")
        if name in result:
            raise ValueError(f"Duplicate service name: {name}")
        if type(entry["optional"]) is not bool or entry["venv"] not in (".venv", ".venv-vision"):
            raise ValueError(f"Service {name}: invalid optional flag or environment.")
        for field, pattern in (
            ("units", UNIT_NAME),
            ("groups", SERVICE_NAME),
            ("assets", SERVICE_NAME),
        ):
            values = entry[field]
            if (
                not isinstance(values, list)
                or any(
                    not isinstance(value, str) or not pattern.fullmatch(value) for value in values
                )
                or len(values) != len(set(values))
                or (field == "units" and not values)
            ):
                raise ValueError(f"Service {name}: invalid or duplicate {field}.")
        token, port = entry["token"], entry["port"]
        if not isinstance(token, str) or (
            token and not re.fullmatch(r"MG400_[A-Z0-9_]+_TOKEN", token)
        ):
            raise ValueError(f"Service {name}: invalid token setting.")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError(f"Service {name}: port must be 0 or a valid TCP port.")
        if (
            used_units.intersection(entry["units"])
            or used_assets.intersection(entry["assets"])
            or (token and token in used_tokens)
            or (port and port in used_ports)
        ):
            raise ValueError(f"Service {name}: duplicate unit, asset, token, or port.")
        used_units.update(entry["units"])
        used_assets.update(entry["assets"])
        used_tokens.add(token)
        used_ports.add(port)
        result[name] = entry
    return result


def enabled_services(registry, value):
    """Select all mandatory services and the explicitly named optional services."""
    names = [] if not value.strip() else [part.strip() for part in value.split(",")]
    if len(names) != len(set(names)):
        raise ValueError("MG400_OPTIONAL_SERVICES contains duplicate names.")
    for name in names:
        if name not in registry or not registry[name]["optional"]:
            raise ValueError(
                f"MG400_OPTIONAL_SERVICES has an unknown or non-optional name: {name!r}"
            )
    return {
        name: entry for name, entry in registry.items() if not entry["optional"] or name in names
    }


def service_environments(selected):
    """Return explicit dependency groups for each selected Python environment."""
    environments = {}
    for entry in selected.values():
        environments.setdefault(entry["venv"], set()).update(entry["groups"])
    return {venv: sorted(groups) for venv, groups in sorted(environments.items())}


def service_assets(selected, manifest):
    """Use both registry membership and manifest ownership to select assets."""
    chosen = {}
    for service, entry in selected.items():
        for name in entry["assets"]:
            if name not in manifest or manifest[name].service != service:
                raise ValueError(f"Service {service}: missing or incorrectly owned asset {name}.")
            chosen[name] = manifest[name]
    return chosen


def udev_rules(host, selected):
    """Render only selected services' templates, before any host changes."""
    rendered = {}
    for name in selected:
        filename = f"70-mg400-{name}.rules"
        source = host.source / "config/udev" / (filename + ".in")
        if not os.path.lexists(source):
            continue
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"udev template must be a regular file: {source}")
        with assets._local(source) as stream:
            text = stream.read().decode("utf-8")
        if (
            "@SERVICE_GROUP@" not in text
            or 'MODE="0660"' not in text
            or not re.search(r'ATTRS?\{idVendor\}=="[0-9a-fA-F]{4}"', text)
            or not re.search(r'ATTRS?\{idProduct\}=="[0-9a-fA-F]{4}"', text)
        ):
            raise ValueError(f"udev template lacks VID, PID, mode, or group: {source}")
        try:
            group = grp.getgrgid(SERVICE_GID).gr_name
        except KeyError as error:
            raise ValueError(f"Create the service group with ID {SERVICE_GID} first.") from error
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]*\$?", group):
            raise ValueError("The service group name cannot be used in a udev rule.")
        text = text.replace("@SERVICE_GROUP@", group)
        if "@" in text:
            raise ValueError(f"udev template has an unresolved placeholder: {source}")
        try:
            with assets._directory(host.udev) as directory:
                assets._regular(host.udev / filename, directory)
        except FileNotFoundError:
            pass
        rendered[filename] = text
    return rendered


def parse_line(line, number):
    """Return (key, value), or None for a blank or comment line.

    Read the line as systemd reads EnvironmentFile=: spaces around the key and the
    value do not count, and one pair of quotes around the value is removed. Refuse
    the forms where systemd could read another value: no "=", escapes, inner quotes.
    Only the two furnace JSON settings permit a literal single-quoted object.
    """
    stripped = line.strip()
    if not stripped or stripped[0] in "#;":
        return None
    key, equals, value = stripped.partition("=")
    key, value = key.strip(), value.strip()
    if not equals or not KEY.fullmatch(key):
        raise ValueError(f"Settings line {number} is not KEY=VALUE.")
    if key in JSON_SETTINGS and value:
        # EnvironmentFile single quotes preserve all interior characters.
        # Keep this exception restricted to the two furnace JSON settings.
        if len(value) < 2 or value[0] != "'" or value[-1] != "'":
            raise ValueError(f"Settings line {number}: enclose {key} JSON in single quotes.")
        literal = value[1:-1]
        if any(
            character in "\\'" or ord(character) < 32 or ord(character) == 127
            for character in literal
        ):
            raise ValueError(f"Settings line {number}: invalid literal JSON in {key}.")
        parsed = json.loads(
            literal,
            object_pairs_hook=unique_json_object,
            parse_constant=finite_json_number,
            parse_float=finite_json_number,
        )
        if not isinstance(parsed, dict):
            raise ValueError(f"Settings line {number}: {key} must be a JSON object.")
        return key, literal
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    if any(character in value for character in "\\'\""):
        raise ValueError(f"Settings line {number}: remove quotes and backslashes from {key}.")
    return key, value


def parse_settings(text):
    """Return the settings. A later line for the same key wins, as in systemd."""
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        if (pair := parse_line(line, number)) is not None:
            values[pair[0]] = pair[1]
    return values


def check_settings(settings):
    """Refuse settings that would split the robot lock between programs."""
    if not os.path.isabs(settings.get("MG400_CONTROLLER_LOCK_DIR", "")):
        raise ValueError("Set MG400_CONTROLLER_LOCK_DIR to an absolute path.")


def merge_settings(current, example, token=lambda: secrets.token_urlsafe(32)):
    """Return the new settings text and the changed keys. Keep every nonempty value."""
    lines = (example if current is None else current).splitlines()
    present, changed = set(), []
    for number, line in enumerate(lines, 1):
        if (pair := parse_line(line, number)) is None:
            continue
        present.add(pair[0])
        if pair[0].endswith("_TOKEN") and not pair[1]:
            lines[number - 1] = f"{pair[0]}={token()}"
            changed.append(pair[0])
    if current is not None:
        for number, line in enumerate(example.splitlines(), 1):
            pair = parse_line(line, number)
            if pair is not None and pair[0] not in present:
                key, value = pair
                if key in JSON_SETTINGS and value:
                    value = f"'{value}'"
                lines.append(f"{key}={token() if key.endswith('_TOKEN') and not value else value}")
                present.add(key)
                changed.append(key)
    return "\n".join(lines) + "\n", changed


def camera_devices(value):
    """Return the camera devices from MG400_CAMERA_DEVICES. An empty value means no cameras."""
    devices = [item.strip() for item in value.split(",") if item.strip()]
    if not devices:
        return []
    if len(devices) != CAMERA_COUNT:
        raise ValueError(f"MG400_CAMERA_DEVICES must list {CAMERA_COUNT} devices or none.")
    for device in devices:
        if not DEVICE.fullmatch(device):
            raise ValueError(f"Invalid camera device path: {device!r}")
    return devices


def render_go2rtc(devices):
    """Return the go2rtc configuration: one MJPEG stream per camera, API on loopback only."""
    streams = "".join(
        f"  camera{number}: v4l2:device?video={device}"
        "&input_format=mjpeg&video_size=640x480&framerate=30\n"
        for number, device in enumerate(devices, 1)
    )
    preload = "".join(f"  camera{number}: video\n" for number in range(1, len(devices) + 1))
    return (
        "api:\n  listen: 127.0.0.1:1984\n"
        'rtsp:\n  listen: ""\n'
        'webrtc:\n  listen: ""\n'
        f"preload:\n{preload}"
        f"streams:\n{streams}"
    )


def find_collectors(proc_root, managed_pid):
    """Return PIDs of Python processes that run any gripper_feedback.py, except the service.

    Only one collector can own the gripper serial port.
    """
    found = []
    for process in Path(proc_root).iterdir():
        if not process.name.isdecimal() or int(process.name) == managed_pid:
            continue
        try:
            arguments = [
                os.fsdecode(value)
                for value in process.joinpath("cmdline").read_bytes().split(b"\0")
                if value
            ]
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if not arguments or not Path(arguments[0]).name.startswith("python"):
            continue
        for argument in arguments[1:]:
            if argument in ("-c", "-m"):
                break
            if not argument.startswith("-"):
                if Path(argument).name == "gripper_feedback.py":
                    found.append(int(process.name))
                break
    return sorted(found)


def open_directory(path):
    """Open a directory without following a symbolic link in its last part."""
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise RuntimeError(f"{path} is not a directory. Check who replaced it.") from error
        raise


def secure_directory(path, mode, owner, chown):
    """Create a directory or repair its mode and owner. Refuse a symbolic link."""
    try:
        Path(path).mkdir(parents=True)
    except FileExistsError:
        pass
    fd = open_directory(path)
    try:
        os.fchmod(fd, mode)
        chown(fd, *owner)
    finally:
        os.close(fd)


def write_file(path, content, mode, owner, chown):
    """Replace a file in a root-owned directory atomically, with this mode and owner."""
    path = Path(path)
    data = content.encode() if isinstance(content, str) else content
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as out:
        temporary = Path(out.name)
        try:
            out.write(data)
            out.flush()
            os.fchmod(out.fileno(), mode)
            chown(out.fileno(), *owner)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def create_key(directory, name, owner, chown):
    """Create a new secret file. Keep any existing entry, even a symbolic link."""
    fd = open_directory(directory)
    try:
        key = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    except FileExistsError:
        return False
    finally:
        os.close(fd)
    try:
        os.write(key, (secrets.token_urlsafe(32) + "\n").encode())
        os.fchmod(key, 0o600)
        chown(key, *owner)
    finally:
        os.close(key)
    return True


def install(args, host, run, chown, out):
    def step(text):
        print(("Would " if args.dry_run else "") + text, file=out, flush=True)

    def system(command, **options):
        step("run: " + " ".join(map(str, command)))
        if not args.dry_run:
            run(command, check=True, **options)

    def directory(path, mode, owner):
        step(f"ensure {path} ({mode:04o}, {owner[0]}:{owner[1]})")
        if not args.dry_run:
            secure_directory(path, mode, owner, chown)

    # Read-only checks first: a refusal must leave the host unchanged.
    version = run(
        [args.python, "-c", "import sys; print(sys.version_info >= (3, 12))"],
        capture_output=True,
        text=True,
        check=False,
    )
    if version.returncode or version.stdout.strip() != "True":
        raise RuntimeError(f"{args.python} must be Python 3.12 or newer.")
    uv = args.uv or shutil.which("uv")
    if not uv:
        raise RuntimeError("Install uv first, or pass its path with --uv.")
    shown = run(
        ["systemctl", "show", "--property=MainPID", COLLECTOR],
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"MainPID=(\d+)", shown.stdout)
    others = find_collectors(host.proc, int(match[1]) if match else 0)
    if others:
        raise RuntimeError(
            "A gripper collector runs outside the service. "
            f"Check these PIDs before you stop them: {', '.join(map(str, others))}"
        )
    example = (host.source / "deploy/mg400.env.example").read_text()
    current = host.settings.read_text() if host.settings.exists() else None
    merged, changed = merge_settings(current, example)
    settings = parse_settings(merged)
    check_settings(settings)
    registry = load_services(host.registry)
    selected = enabled_services(registry, settings.get("MG400_OPTIONAL_SERVICES", ""))
    selected_units = {unit for entry in selected.values() for unit in entry["units"]}
    unit_contents = {}
    for name in sorted(selected_units | {REQUEST_WATCHER}):
        with assets._local(host.source / "config/systemd" / name) as stream:
            unit_contents[name] = stream.read().decode("utf-8")
    rendered_rules = udev_rules(host, selected)
    devices = camera_devices(settings.get("MG400_CAMERA_DEVICES", ""))
    if args.go2rtc is not None and not args.go2rtc.is_file():
        raise RuntimeError(f"The go2rtc binary does not exist: {args.go2rtc}")
    if devices and args.go2rtc is None and not host.go2rtc.exists():
        raise RuntimeError("Cameras are configured. Pass the go2rtc 1.9.14 binary with --go2rtc.")

    manifest = assets.load_manifest(host.asset_manifest) if args.asset or args.fetch_assets else {}
    available_assets = service_assets(selected, manifest) if args.asset or args.fetch_assets else {}
    local_assets = {}
    for specification in args.asset:
        name, equals, path = specification.partition("=")
        if not equals or not path or name not in manifest:
            raise ValueError(f"Use --asset NAME=PATH with a name from {host.asset_manifest}.")
        if name not in available_assets:
            raise ValueError(f"Asset {name} does not belong to an enabled registered service.")
        if name in local_assets:
            raise ValueError(f"Asset {name} was specified more than once.")
        local_assets[name] = Path(path)
    selected_assets = (
        available_assets
        if args.fetch_assets
        else {name: available_assets[name] for name in local_assets}
    )
    for name, asset in selected_assets.items():
        assets.check_destination(asset, host.root)
        if name not in available_assets:
            raise ValueError(f"Asset {name} does not belong to an enabled registered service.")
        if name in local_assets:
            assets.validate_source(asset, local_assets[name])

    service = (SERVICE_UID, SERVICE_GID)
    directory(host.etc, 0o755, (0, 0))
    if current is None or changed:
        if current is None:
            step(f"create {host.settings}")
        else:
            step(f"add to {host.settings}: {', '.join(changed)}")
        if not args.dry_run:
            write_file(host.settings, merged, 0o600, (0, 0), chown)
    elif not args.dry_run:
        # A copied settings file can arrive with other rights. Only root may read it.
        fd = os.open(host.settings, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fchmod(fd, 0o600)
            chown(fd, 0, 0)
        finally:
            os.close(fd)
    # A root-owned data directory stops the service user from replacing its subdirectories.
    directory(host.data, 0o750, (0, SERVICE_GID))
    directory(host.data / "locks", 0o700, service)
    directory(host.data / "work", 0o750, service)
    directory(host.data / "camera", 0o700, service)
    if "furnace" in selected:
        directory(host.data / "furnace", 0o750, service)
    key = host.data / "camera/preview.key"
    if not os.path.lexists(key):
        step(f"create {key}")
        if not args.dry_run:
            create_key(key.parent, key.name, service, chown)
    directory(host.code, 0o750, (0, SERVICE_GID))
    # The system Python keeps the environment readable for the service user.
    for venv, groups in service_environments(selected).items():
        system(
            [
                uv,
                "sync",
                "--locked",
                "--no-default-groups",
                *[part for group in groups for part in ("--group", group)],
                "--python",
                args.python,
            ],
            cwd=host.code,
            env={
                **os.environ,
                "UV_LINK_MODE": "copy",
                "UV_PROJECT_ENVIRONMENT": str(host.code / venv),
            },
        )
    for name, asset in selected_assets.items():
        source = local_assets.get(name)
        step(
            f"install asset {name} from {source or asset.url} as "
            f"{assets.destination(asset, host.root)} "
            f"({asset.bytes} bytes, SHA-256 {asset.sha256}, {asset.mode:04o}, "
            f"{SERVICE_UID}:{SERVICE_GID})"
        )
        if not args.dry_run:
            assets.install_asset(asset, root=host.root, source=source, chown=chown)
    if devices:
        if args.go2rtc is not None:
            step(f"install {args.go2rtc} as {host.go2rtc}")
            if not args.dry_run:
                write_file(host.go2rtc, Path(args.go2rtc).read_bytes(), 0o755, (0, 0), chown)
        step(f"write {host.go2rtc_config}")
        if not args.dry_run:
            write_file(host.go2rtc_config, render_go2rtc(devices), 0o640, (0, SERVICE_GID), chown)
    elif host.go2rtc_config.exists():
        step(f"remove {host.go2rtc_config}: cameras are off")
        if not args.dry_run:
            host.go2rtc_config.unlink()
    if rendered_rules:
        directory(host.udev, 0o755, (0, 0))
        for name, text in rendered_rules.items():
            step(f"install {host.udev / name}")
            if not args.dry_run:
                write_file(host.udev / name, text, 0o644, (0, 0), chown)
        system(["udevadm", "control", "--reload-rules"])
        system(["udevadm", "trigger", "--subsystem-match=usb"])
    for name, content in unit_contents.items():
        step(f"install {host.units / name}")
        if not args.dry_run:
            write_file(host.units / name, content, 0o644, (0, 0), chown)
    system(["systemctl", "daemon-reload"])
    system(["systemctl", "enable", "--now", REQUEST_WATCHER])
    for command in (host.command, host.lab_command):
        step(f"link {command} to {host.code / 'deploy/mg400ctl'}")
        if not args.dry_run:
            command.parent.mkdir(parents=True, exist_ok=True)
            command.unlink(missing_ok=True)
            command.symlink_to(host.code / "deploy/mg400ctl")
    return settings, devices


def main(argv=None, *, host=None, run=subprocess.run, chown=os.fchown, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="Print the steps. Change nothing.")
    parser.add_argument("--go2rtc", type=Path, help="go2rtc 1.9.14 binary to install")
    parser.add_argument(
        "--asset",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Install a verified local asset",
    )
    parser.add_argument("--fetch-assets", action="store_true", help="Download manifest assets")
    parser.add_argument("--python", default="/usr/bin/python3", help="Python for the services")
    parser.add_argument("--uv", help="uv executable (default: uv on PATH)")
    args = parser.parse_args(argv)
    host = host or Host()
    try:
        if sys.platform != "linux" or os.geteuid() != 0:
            raise RuntimeError("Run the installer as root on Linux.")
        if host.code.resolve() != host.source.resolve():
            raise RuntimeError(f"Run the installer from the checkout at {host.code}.")
        settings, devices = install(args, host, run, chown, out)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Installation failed: {error}", file=sys.stderr)
        return 1
    if args.dry_run:
        return 0
    commit = run(
        ["git", "-C", str(host.code), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    print(f"Installed commit {commit.stdout.strip() or 'unknown'}.", file=out)
    if not devices:
        print("Cameras are off: MG400_CAMERA_DEVICES is empty.", file=out)
    if not settings.get("MG400_STAGE_SERIAL"):
        print("The stage panel offers every serial port: MG400_STAGE_SERIAL is empty.", file=out)
    print("No service was started, stopped or restarted. Use: mg400ctl start NAME", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
