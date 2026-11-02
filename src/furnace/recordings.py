"""Private local recording directories and optional verified archive copies."""

import hashlib
import json
import math
import os
import re
import stat
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

LOCAL_RECORDINGS_DIRECTORY = Path("/var/lib/mg400/recordings")
SHORTENED_RUN_TARGET_MINUTES = 3.0


def _path(value):
    path = Path(value)
    if (
        not path.is_absolute()
        or path == Path(path.anchor)
        or ".." in path.parts
        or any(ord(char) < 32 or ord(char) == 127 for char in str(path))
    ):
        raise ValueError("Recording paths must be absolute without parent traversal or controls")
    return path


def _root(value=None):
    return _path(LOCAL_RECORDINGS_DIRECTORY if value is None else value)


def _archive_root(value):
    value = os.environ.get("MG400_FURNACE_ARCHIVE_DIR", "") if value is None else value
    return None if value == "" else _path(value)


@contextmanager
def directory_fd(path, *, root=None, create=False, exclusive=False):
    """Pin a directory after refusing symlinks and checking private data ownership."""
    path = _path(path)
    root = _root(root)
    if not path.is_relative_to(root):
        raise ValueError("Recording directory is outside its configured root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for index, component in enumerate(path.parts[1:], 1):
            if exclusive and index == len(path.parts) - 1:
                os.mkdir(component, 0o750, dir_fd=descriptor)
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o750, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if index >= len(root.parts) - 1:
                info = os.fstat(descriptor)
                if info.st_uid != os.geteuid() or info.st_mode & 0o027:
                    raise ValueError("Recording directories must be owned and private")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def private_open(path, mode="xb", *, root=None, **kwargs):
    """Read or exclusively create a private regular file, without following links."""
    path = _path(path)
    root = _root(root)
    if mode not in {"x", "xb", "r", "rb"}:
        raise ValueError("Recording files support only exclusive creation or reading")
    flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    flags |= os.O_WRONLY | os.O_CREAT | os.O_EXCL if mode.startswith("x") else os.O_RDONLY
    with directory_fd(path.parent, root=root) as parent:
        descriptor = os.open(path.name, flags, 0o640, dir_fd=parent)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o037
        ):
            raise ValueError("Recording files must be owned private regular files with one link")
        stream = os.fdopen(descriptor, mode, **kwargs)
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream


def validated_run_directory(run_directory, recordings_root=None):
    root = _root(recordings_root)
    path = _path(run_directory)
    if path.parent != root:
        raise ValueError("A recording run must be a direct child of its configured root")
    with directory_fd(path, root=root):
        return path


def recording_output_path(run_directory, filename, recordings_root=None):
    """Return an unused basename inside an owned run; never replace existing output."""
    run = validated_run_directory(run_directory, recordings_root)
    if not isinstance(filename, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", filename
    ):
        raise ValueError("Recording output requires a plain basename")
    with directory_fd(run, root=_root(recordings_root)) as parent:
        try:
            os.stat(filename, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return run / filename
    raise FileExistsError("Recording output already exists")


def create_run(run_name, run_type, details=None, recordings_root=None, *, recording_id=None):
    """Create the source date-and-name folder and return (video base, run directory)."""
    root = _root(recordings_root)
    display_name = str(run_name or "").strip() or "Untitled run"
    normalized = unicodedata.normalize("NFKD", display_name)
    safe_name = normalized.encode("ascii", "ignore").decode("ascii")
    safe_name = re.sub(r"[^A-Za-z0-9]+", "-", safe_name).strip("-").lower()
    safe_name = (safe_name or "untitled-run")[:60].rstrip("-")
    timestamp = datetime.now().strftime("%Y-%m-%d")
    if recording_id is not None and (
        not isinstance(recording_id, str) or not re.fullmatch(r"[0-9a-f]{32}", recording_id)
    ):
        raise ValueError("recording_id must contain exactly 32 lowercase hexadecimal characters")
    run_directory = root / (recording_id or f"{timestamp}_{safe_name}")
    manifest = {
        "run_name": display_name,
        "run_type": run_type,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "video_base": "video.mp4",
        "temperature_data": "video.timestamps.csv",
    }
    if recording_id is not None:
        manifest["recording_id"] = recording_id
    if details is not None:
        if not isinstance(details, dict) or (manifest.keys() | {"recording_id"}) & details.keys():
            raise ValueError("Recording details cannot replace manifest identity fields")
        manifest.update(details)
    encoded = json.dumps(manifest, indent=2, allow_nan=False)
    with directory_fd(root, root=root, create=True):
        pass
    with directory_fd(run_directory, root=root, exclusive=True):
        pass
    with private_open(run_directory / "run.json", "x", root=root, encoding="utf-8") as output:
        output.write(encoded)
    return run_directory / "video.mp4", run_directory


def _tree_files(source, root):
    directories, files = [], []
    for path in sorted(source.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            with directory_fd(path, root=root):
                directories.append(path)
        else:
            with private_open(path, "rb", root=root):
                files.append(path)
    return directories, files


def _copy_verified(source, destination, source_root, destination_root):
    digest = hashlib.sha256()
    with private_open(source, "rb", root=source_root) as original:
        before = os.fstat(original.fileno())
        with private_open(destination, "xb", root=destination_root) as copied:
            while block := original.read(1024 * 1024):
                digest.update(block)
                copied.write(block)
            copied.flush()
            os.fsync(copied.fileno())
        after = os.fstat(original.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError("Recording changed during its archive copy")
    with private_open(destination, "rb", root=destination_root) as copied:
        if hashlib.file_digest(copied, "sha256").digest() != digest.digest():
            raise RuntimeError("Recording archive verification failed")


def sync_run_to_server(run_directory, server_root=None, *, recordings_root=None):
    """Copy a completed run without replacing any archive or deleting local data."""
    destination_root = _archive_root(server_root)
    if destination_root is None:
        return None
    root = _root(recordings_root)
    source = validated_run_directory(run_directory, root)
    if destination_root.is_relative_to(source):
        raise ValueError("The recording archive cannot be inside the source run")
    directories, files = _tree_files(source, root)
    with directory_fd(destination_root, root=destination_root, create=True):
        pass
    destination = destination_root / source.name
    with directory_fd(destination, root=destination_root, exclusive=True):
        pass
    for directory in directories:
        with directory_fd(
            destination / directory.relative_to(source), root=destination_root, exclusive=True
        ):
            pass
    for path in files:
        _copy_verified(path, destination / path.relative_to(source), root, destination_root)
    return destination


def create_shortened_run_upload(
    run_directory,
    target_minutes=SHORTENED_RUN_TARGET_MINUTES,
    recordings_root=None,
    server_root=None,
    compiler=None,
):
    """Compile a local summary and optionally copy it to the private archive."""
    from .video import combine_run_to_target, speed_factor_for_target

    speed_factor_for_target(1, target_minutes)
    root = _root(recordings_root)
    source = validated_run_directory(run_directory, root)
    local_output = recording_output_path(
        source, f"{source.name}_shortened_{target_minutes:g}_min.mp4", root
    )
    (compiler or combine_run_to_target)(
        run_name=source.name,
        target_minutes=float(target_minutes),
        recordings_root=root,
        output_path=local_output,
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
    )
    with private_open(local_output, "rb", root=root):
        pass
    archive_root = _archive_root(server_root)
    if archive_root is None:
        return local_output, None
    if archive_root.is_relative_to(source):
        raise ValueError("The recording archive cannot be inside the source run")
    server_run_directory = archive_root / source.name
    with directory_fd(server_run_directory, root=archive_root, create=True):
        pass
    server_output = server_run_directory / local_output.name
    _copy_verified(local_output, server_output, root, archive_root)
    return local_output, server_output


def finalize_run_recording(
    run_directory,
    target_minutes=SHORTENED_RUN_TARGET_MINUTES,
    recordings_root=None,
    server_root=None,
    compiler=None,
):
    """Compile then verify an optional archive; always retain the local recording."""
    local_summary, _ = create_shortened_run_upload(
        run_directory,
        target_minutes=target_minutes,
        recordings_root=recordings_root,
        server_root="",
        compiler=compiler,
    )
    archived_run = sync_run_to_server(run_directory, server_root, recordings_root=recordings_root)
    archived_summary = None if archived_run is None else archived_run / local_summary.name
    return archived_run, local_summary, archived_summary


def normalize_recording_settings(data):
    """Normalize the source UI settings without importing a camera or application."""
    payload = data or {}
    mode = payload.get("mode", payload.get("recording_mode", "time_lapse"))
    mode_text = str(mode or "").strip().lower().replace("-", "_")
    mode = "time_lapse" if mode_text in {"time_lapse", "timelapse"} else "live"
    interval = payload.get("capture_interval_seconds")
    fps = payload.get("output_fps")
    minimum = payload.get("minimum_free_gb")
    if mode == "time_lapse":
        interval = 30.0 if interval is None else interval
        fps = 1.0 if fps is None else fps
    else:
        interval = None
        fps = 30.0 if fps is None else fps
        minimum = 2.0 if minimum is None else minimum
    try:
        if any(isinstance(value, bool) for value in (interval, fps, minimum)):
            raise ValueError
        interval = None if interval is None else float(interval)
        fps = float(fps)
        minimum = None if minimum is None else float(minimum)
    except (TypeError, ValueError) as error:
        raise ValueError("Recording timing and space values must be numeric") from error
    if any(
        value is not None and (not math.isfinite(value) or value <= 0) for value in (interval, fps)
    ):
        raise ValueError("Recording timing values must be finite and positive")
    if minimum is not None and (not math.isfinite(minimum) or minimum < 0):
        raise ValueError("minimum_free_gb must be finite and nonnegative")
    return {
        "mode": mode,
        "capture_interval_seconds": interval,
        "output_fps": fps,
        "minimum_free_gb": minimum,
    }
