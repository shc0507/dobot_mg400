#!/usr/bin/env python3
"""Install or update the MG400 services on this Linux host.

Run as root with the system Python, from the checkout at /opt/mg400:

    sudo /usr/bin/python3 /opt/mg400/deploy/install.py

The installer never starts, stops or restarts a robot service.
"""

import argparse
import errno
import grp
import hashlib
import ipaddress
import json
import math
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import assets  # noqa: E402

from cameras.config import camera_configuration  # noqa: E402
from cameras.config import camera_devices as camera_devices  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1]
TOUPCAM_VERSION = "59.30594.20260120"
TOUPCAM_SHA256 = "5ba51b66bb9e2901e769fa1d5d5ae88c359269ea7fb20358335462c531a3e997"
SERVICE_UID = SERVICE_GID = 1000
COLLECTOR = "mg400-gripper-collector.service"
REQUEST_WATCHER = "mg400-gripper-collector.path"
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
        self.caddy = root / "usr/local/lib/mg400/caddy"
        self.toupcam = root / "usr/local/lib/mg400/libtoupcam.so"
        self.proxy_config = self.etc / "Caddyfile"
        self.units = root / "etc/systemd/system"
        self.command = root / "usr/local/bin/mg400ctl"
        self.lab_command = root / "usr/local/bin/labctl"
        self.udev = root / "etc/udev/rules.d"
        self.proc = root / "proc"
        self.dev = root / "dev"


SERVICE_FIELDS = {"name", "units", "optional", "venv", "groups", "port", "assets"}
# The default PATH of systemd services. The microscope finds ffmpeg and ffprobe there.
SERVICE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
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
    used_units, used_assets, used_ports = set(), set(), set()
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
        port = entry["port"]
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError(f"Service {name}: port must be 0 or a valid TCP port.")
        if (
            used_units.intersection(entry["units"])
            or used_assets.intersection(entry["assets"])
            or (port and port in used_ports)
        ):
            raise ValueError(f"Service {name}: duplicate unit, asset, or port.")
        used_units.update(entry["units"])
        used_assets.update(entry["assets"])
        used_ports.add(port)
        result[name] = entry
    return result


def enabled_services(registry, value):
    """Select all mandatory services and the explicitly named optional services.

    A mandatory name in the list is accepted and has no effect.
    """
    names = [] if not value.strip() else [part.strip() for part in value.split(",")]
    if len(names) != len(set(names)):
        raise ValueError("MG400_OPTIONAL_SERVICES contains duplicate names.")
    for name in names:
        if name not in registry:
            raise ValueError(f"MG400_OPTIONAL_SERVICES has an unknown name: {name!r}")
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
        rules = [line.strip() for line in text.splitlines() if line.strip()]
        rules = [line for line in rules if not line.startswith("#")]
        if not rules or any(
            'GROUP="@SERVICE_GROUP@"' not in rule
            or 'MODE="0660"' not in rule
            or not re.search(r'ATTRS?\{idVendor\}=="[0-9a-fA-F]{4}"', rule)
            or not re.search(r'ATTRS?\{idProduct\}=="[0-9a-fA-F]{4}"', rule)
            for rule in rules
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


# Fixed settings for programs that mg400ctl and labctl start. Their units set the same values.
FIXED_SETTINGS = {
    "MG400_CONTROLLER_LOCK_DIR": "/var/lib/mg400/locks",
    "MG400_ORCH_ROBOT_URL": "http://127.0.0.1:8766/",
    "MG400_ORCH_STAGE_URL": "http://127.0.0.1:8767/contract/",
    "MG400_ORCH_THERMAL_URL": "http://127.0.0.1:8768/",
    "MG400_ORCH_FURNACE_URL": "http://127.0.0.1:8769/",
    "MG400_ORCH_MICROSCOPE_URL": "http://127.0.0.1:8770/",
    "MG400_ORCH_VISION_URL": "http://127.0.0.1:8771/",
}


def obsolete(key, value):
    """Removed settings: service tokens, and empty settings that are not in the example."""
    return (key.endswith("_TOKEN") and key != "MG400_GATEWAY_TOKEN") or not value


def merge_settings(current, example):
    """Return the new settings text and the changed keys.

    Add missing example settings. Keep every nonempty value, except removed service
    tokens. Remove empty settings that the example does not contain, so that the
    fixed defaults of the services apply.
    """
    if current is None:
        return example if example.endswith("\n") else example + "\n", []
    known = {
        pair[0] for n, line in enumerate(example.splitlines(), 1) if (pair := parse_line(line, n))
    }
    lines, present, changed = [], set(), []
    for number, line in enumerate(current.splitlines(), 1):
        pair = parse_line(line, number)
        if pair is not None and pair[0] not in known and obsolete(*pair):
            if pair[0] not in changed:
                changed.append(pair[0])
            continue
        if pair is not None:
            present.add(pair[0])
        lines.append(line)
    for number, line in enumerate(example.splitlines(), 1):
        pair = parse_line(line, number)
        if pair is not None and pair[0] not in present:
            key, value = pair
            if key in JSON_SETTINGS and value:
                value = f"'{value}'"
            lines.append(f"{key}={value}")
            present.add(key)
            changed.append(key)
    return "\n".join(lines) + "\n", changed


def camera_capture_sizes(value, devices, side_stream=""):
    """Keep the legacy size API with the shared mode and side-stream policy."""
    configured = camera_configuration(
        {
            "MG400_CAMERA_DEVICES": ",".join(devices),
            "MG400_CAMERA_CAPTURE_SIZES": value,
            "MG400_MICROSCOPE_SIDE_STREAM": side_stream,
        }
    )
    return [mode[:2] for mode in configured.modes]


def render_go2rtc(devices, sizes=None, *, modes=None):
    """Return per-camera MJPEG capture settings and on-demand H.264 previews.

    The API and RTSP listen on loopback only. ffmpeg reads and returns the H.264
    previews through RTSP. The microscope preview comes from the microscope service.
    """
    if sizes is not None and modes is not None:
        raise ValueError("Use camera sizes or modes, not both.")
    settings = {"MG400_CAMERA_DEVICES": ",".join(devices)}
    if modes is not None:
        if any(len(mode) != 3 or any(type(value) is not int for value in mode) for mode in modes):
            raise ValueError("Invalid per-camera capture mode.")
        settings["MG400_CAMERA_MODES"] = ",".join(f"{w}x{h}@{fps}" for w, h, fps in modes)
    elif sizes is not None:
        if any(len(size) != 2 or any(type(value) is not int for value in size) for size in sizes):
            raise ValueError("Invalid per-camera capture dimensions.")
        settings["MG400_CAMERA_CAPTURE_SIZES"] = ",".join(f"{w}x{h}" for w, h in sizes)
    configured = camera_configuration(settings)
    if (modes is not None and len(modes) != configured.count) or (
        sizes is not None and len(sizes) != configured.count
    ):
        raise ValueError("Supply one capture mode per camera device.")
    streams = "".join(
        f"  camera{number}: v4l2:device?video={device}"
        f"&input_format=mjpeg&video_size={mode[0]}x{mode[1]}&framerate={mode[2]}\n"
        for number, (device, mode) in enumerate(zip(configured.devices, configured.modes), 1)
    )
    # go2rtc starts each ffmpeg encoder for its first viewer and stops it after the last.
    # Sensor noise can raise a 640x480 stream to about 9 Mbit/s. A 1280x720 preview gets
    # 4 Mbit/s and a smaller one 2 Mbit/s.
    streams += "".join(
        f"  {name}_h264: ffmpeg:{name}#video=h264#bitrate={4 if width > 640 else 2}M\n"
        for name, (width, _, _) in zip(configured.streams, configured.modes)
        if name in configured.preloaded
    )
    # go2rtc reads the microscope MJPEG preview (1296x968) only for an H.264 viewer.
    # With its 3 Mbit/s cap, the microscope and 4 previews at 1280x720 need about
    # 19 Mbit/s of the 22 Mbit/s VPN link.
    streams += (
        "  microscope: http://127.0.0.1:8770/api/camera/microscope/mjpeg\n"
        "  microscope_h264: ffmpeg:microscope#video=h264#bitrate=3M\n"
    )
    preload = "".join(f"  {name}: video\n" for name in configured.preloaded)
    preload = f"preload:\n{preload}" if preload else "preload: {}\n"
    return (
        "api:\n  listen: 127.0.0.1:1984\n"
        "rtsp:\n  listen: 127.0.0.1:8554\n"
        'webrtc:\n  listen: ""\n'
        f"{preload}"
        f"streams:\n{streams}"
    )


# The proxy serves only the Lab Console (/lab/) and the camera streams (port 1985).
PROXY_UPSTREAMS = {"ORCH": 8772}
# Fixed: the console control socket of the orchestrator (SOCKET_PORT in orchestrator/__main__.py).
ORCH_SOCKET_PORT = 8774


def render_proxy(host, settings, selected):
    """Render fixed loopback routes after validating the proxy and its installed peers."""

    def port(key, default, minimum=1):
        value = settings.get(key) or str(default)
        if not re.fullmatch(r"[0-9]{1,5}", value) or not minimum <= int(value) <= 65535:
            raise ValueError(f"{key} must be an integer from {minimum} to 65535.")
        return str(int(value))

    bind = settings.get("MG400_PROXY_BIND") or "127.0.0.1"
    try:
        address = ipaddress.ip_address(bind)
    except ValueError as error:
        raise ValueError("MG400_PROXY_BIND must be one numeric loopback or VPN address.") from error
    if "%" in bind or address.is_unspecified or address.is_multicast:
        raise ValueError("MG400_PROXY_BIND must be one numeric loopback or VPN address.")
    values = {
        "PROXY_BIND": f"[{address}]" if address.version == 6 else str(address),
        "PROXY_PORT": port("MG400_PROXY_PORT", 8080, 1024),
    }
    ports = {"1985", values["PROXY_PORT"]}
    if len(ports) != 2:
        raise ValueError("Proxy and upstream ports must be distinct.")
    for prefix, default in PROXY_UPSTREAMS.items():
        value = port(f"MG400_{prefix}_PORT", default)
        if value in ports:
            raise ValueError("Proxy and upstream ports must be distinct.")
        ports.add(value)
        values[prefix + "_PORT"] = value
    if str(ORCH_SOCKET_PORT) in ports:
        raise ValueError("Proxy and upstream ports must be distinct.")
    values["ORCH_SOCKET_PORT"] = str(ORCH_SOCKET_PORT)
    with assets._local(host.source / "config/proxy/Caddyfile") as stream:
        template = stream.read().decode("utf-8")
    for key, value in values.items():
        template = template.replace(f"@{key}@", value)
    if re.search(r"@[A-Z][A-Z0-9_]*@", template):
        raise ValueError("Proxy template contains an unresolved placeholder.")
    return template


def prepare_proxy(args, host, settings, selected):
    """Check the proxy without starting its binary or changing files."""
    if "proxy" not in selected:
        if args.caddy is not None:
            raise ValueError("Select proxy in MG400_OPTIONAL_SERVICES before using --caddy.")
        return None, None
    config = render_proxy(host, settings, selected)
    for destination in (host.caddy, host.proxy_config):
        try:
            with assets._directory(destination.parent) as directory:
                assets._regular(destination, directory)
        except FileNotFoundError:
            pass
    source = args.caddy if args.caddy is not None else host.caddy
    try:
        with assets._local(source) as stream:
            info = os.fstat(stream.fileno())
            # POSIX uses matching group bits before "other" permissions.
            execute_bit = 0o010 if info.st_gid == SERVICE_GID else 0o001
            service_executable = bool(info.st_mode & execute_bit)
            if args.caddy is None and (
                info.st_uid != 0 or info.st_mode & 0o7022 or not service_executable
            ):
                raise ValueError(
                    "The installed Caddy binary must be root-owned and executable by the service, "
                    "without special mode bits or group or other write access."
                )
            binary = stream.read()
    except FileNotFoundError as error:
        raise RuntimeError("Proxy is selected. Pass a Caddy 2 binary with --caddy PATH.") from error
    if not binary:
        raise ValueError("The Caddy binary is empty.")
    return config, binary if args.caddy is not None else None


def microscope_groups():
    """Resolve the numeric unit user's NSS and explicit supplementary groups."""
    groups = {SERVICE_GID}
    try:
        name = pwd.getpwuid(SERVICE_UID).pw_name
        groups.update(os.getgrouplist(name, SERVICE_GID))
    except (KeyError, OSError) as error:
        raise ValueError(
            "Cannot resolve microscope service UID 1000 and its supplementary groups."
        ) from error
    for name in ("dialout", "video"):
        try:
            groups.add(grp.getgrnam(name).gr_gid)
        except (KeyError, OSError) as error:
            raise ValueError(f"Microscope requires the {name} device group.") from error
    return groups


def check_toupcam_directories(host, groups):
    """Require a trusted destination chain, without changing existing directories."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with assets._directory(host.root) as anchor:
        parent = os.dup(anchor)
        try:
            for component in host.toupcam.parent.relative_to(host.root).parts:
                try:
                    child = os.open(component, flags, dir_fd=parent)
                except FileNotFoundError:
                    return  # Installation creates the missing suffix as root.
                os.close(parent)
                parent = child
                info = os.fstat(parent)
                execute_bit = 0o010 if info.st_gid in groups else 0o001
                if info.st_uid != 0 or info.st_mode & 0o7022 or not info.st_mode & execute_bit:
                    raise ValueError(
                        "Toupcam destination directories must be root-owned and traversable "
                        "by the service, "
                        "without special mode bits or group or other write access."
                    )
        finally:
            os.close(parent)


def prepare_toupcam(source, host, *, required=False):
    """Keep verified SDK bytes without loading the library or changing the host."""
    if source is None and not required:
        return None
    if required:
        groups = microscope_groups()
        check_toupcam_directories(host, groups)
    try:
        with assets._directory(host.toupcam.parent) as directory:
            assets._regular(host.toupcam, directory)
    except FileNotFoundError:
        pass
    try:
        with assets._local(host.toupcam if source is None else source) as stream:
            if source is None:
                info = os.fstat(stream.fileno())
                read_bit = 0o040 if info.st_gid in groups else 0o004
                if (
                    info.st_uid != 0
                    or info.st_nlink != 1
                    or info.st_mode & 0o7022
                    or not info.st_mode & read_bit
                ):
                    raise ValueError(
                        "The installed Toupcam library must be root-owned and service-readable, "
                        "with one link, no special mode bits, and no group or other write access."
                    )
            binary = stream.read()
    except FileNotFoundError as error:
        if source is None:
            raise RuntimeError(
                "Microscope is selected. Pass the verified Toupcam library with --toupcam PATH."
            ) from error
        raise
    if hashlib.sha256(binary).hexdigest() != TOUPCAM_SHA256:
        raise ValueError(
            f"Toupcam {TOUPCAM_VERSION} linux/x64/libtoupcam.so: SHA-256 mismatch "
            f"(expected {TOUPCAM_SHA256})."
        )
    # A previously installed verified library is not rewritten during an upgrade.
    return binary if source is not None else None


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
    registry = load_services(host.registry)
    selected = enabled_services(registry, settings.get("MG400_OPTIONAL_SERVICES", ""))
    selected_units = {unit for entry in selected.values() for unit in entry["units"]}
    unit_contents = {}
    for name in sorted(selected_units | {REQUEST_WATCHER}):
        with assets._local(host.source / "config/systemd" / name) as stream:
            unit_contents[name] = stream.read().decode("utf-8")
    rendered_rules = udev_rules(host, selected)
    camera_config = camera_configuration(settings)
    devices = list(camera_config.devices)
    if args.go2rtc is not None and not args.go2rtc.is_file():
        raise RuntimeError(f"The go2rtc binary does not exist: {args.go2rtc}")
    if devices and args.go2rtc is None and not host.go2rtc.exists():
        raise RuntimeError("Cameras are configured. Pass the go2rtc 1.9.14 binary with --go2rtc.")
    proxy_config, caddy_binary = prepare_proxy(args, host, settings, selected)
    # Without a library, the microscope service installs and runs without its camera.
    toupcam_missing = args.toupcam is None and not os.path.lexists(host.toupcam)
    toupcam_binary = prepare_toupcam(
        args.toupcam, host, required="microscope" in selected and not toupcam_missing
    )
    if "microscope" in selected and toupcam_missing:
        print(
            "The Toupcam library is not installed. Pass it with --toupcam PATH.",
            file=out,
            flush=True,
        )
    # Without ffmpeg, the microscope runs, but its recordings cannot finish.
    # The camera previews then send MJPEG, which needs more VPN bandwidth than H.264.
    tools = [
        name for name in ("ffmpeg", "ffprobe") if shutil.which(name, path=SERVICE_PATH) is None
    ]
    if "microscope" in selected and tools:
        verb = "is" if len(tools) == 1 else "are"
        print(
            f"{' and '.join(tools)} {verb} not installed. "
            "Microscope recordings cannot finish. Install the ffmpeg package.",
            file=out,
            flush=True,
        )
    if devices and "ffmpeg" in tools:
        print(
            "ffmpeg is not installed. Camera previews send MJPEG, not H.264. "
            "Install the ffmpeg package.",
            file=out,
            flush=True,
        )

    # Vision requires the released model even without the optional bulk-fetch flag.
    required_assets = {"vision-weights"} if "vision" in selected else set()
    load_assets = bool(required_assets or args.asset or args.fetch_assets)
    manifest = assets.load_manifest(host.asset_manifest) if load_assets else {}
    available_assets = service_assets(selected, manifest) if load_assets else {}
    if not required_assets <= available_assets.keys():
        raise ValueError("The vision service must register its vision-weights asset.")
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
        else {
            name: available_assets[name] for name in sorted(required_assets | local_assets.keys())
        }
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
            step(f"update {host.settings}: {', '.join(changed)}")
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
    directory(host.data / "stage", 0o700, service)
    directory(host.data / "robot", 0o750, service)
    directory(host.data / "calibration", 0o750, service)
    directory(host.data / "calibration/cameras", 0o700, service)
    legacy_stage = host.data / "stage.toml"
    if os.path.lexists(legacy_stage):
        print(
            f"Legacy stage calibration exists at {legacy_stage}. "
            f"Move it manually to {host.data / 'stage/stage.toml'} before reference teaching; "
            "do not overwrite an existing destination. No calibration file was moved.",
            file=out,
            flush=True,
        )
    for name in ("tool.toml", "stations.toml"):
        legacy = host.data / name
        if os.path.lexists(legacy):
            print(
                f"Legacy robot data exists at {legacy}. "
                f"Move it manually to {host.data / 'robot' / name}. "
                "Do not overwrite an existing destination. "
                "No robot data file was moved.",
                file=out,
                flush=True,
            )
    if "thermal" in selected:
        directory(host.data / "thermal", 0o750, service)
    if "vision" in selected:
        directory(host.data / "vision", 0o750, service)
    if "furnace" in selected:
        directory(host.data / "furnace", 0o750, service)
    if "microscope" in selected:
        directory(host.data / "calibration/microscope", 0o750, service)
        directory(host.data / "recordings", 0o750, service)
    if {"orch", "gateway", "microscope"} & selected.keys():
        directory(host.data / "runs", 0o750, service)
    if "orch" in selected:
        directory(host.data / "zones", 0o750, service)
    directory(host.code, 0o750, (0, SERVICE_GID))
    # The system Python keeps the environment readable for the service user.
    for venv, groups in service_environments(selected).items():
        step(f"prepare Python environment {host.code / venv}")
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
        if source is None and assets.asset_status(asset, host.root) == "present":
            # The installed file matches its manifest size, SHA-256, owner and mode.
            step(f"keep asset {name} at {assets.destination(asset, host.root)} (verified)")
            continue
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
            write_file(
                host.go2rtc_config,
                render_go2rtc(devices, modes=camera_config.modes),
                0o640,
                (0, SERVICE_GID),
                chown,
            )
    elif host.go2rtc_config.exists():
        step(f"remove {host.go2rtc_config}: cameras are off")
        if not args.dry_run:
            host.go2rtc_config.unlink()
    if proxy_config is not None:
        if caddy_binary is not None:
            directory(host.caddy.parent, 0o755, (0, 0))
            step(f"install {args.caddy} as {host.caddy}")
            if not args.dry_run:
                write_file(host.caddy, caddy_binary, 0o755, (0, 0), chown)
        step(f"write {host.proxy_config}")
        if not args.dry_run:
            write_file(host.proxy_config, proxy_config, 0o640, (0, SERVICE_GID), chown)
    if toupcam_binary is not None:
        directory(host.toupcam.parent, 0o755, (0, 0))
        step(f"install Toupcam {TOUPCAM_VERSION} from {args.toupcam} as {host.toupcam}")
        if not args.dry_run:
            write_file(host.toupcam, toupcam_binary, 0o755, (0, 0), chown)
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
    parser.add_argument("--caddy", type=Path, help="Caddy 2 binary for the proxy")
    parser.add_argument(
        "--toupcam", type=Path, help=f"Verified Toupcam {TOUPCAM_VERSION} Linux x64 library"
    )
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
