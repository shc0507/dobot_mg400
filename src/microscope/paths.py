"""Host calibration and run paths; resolving settings performs no writes."""

import os
import stat
from pathlib import Path


def calibration_root():
    return _root("MG400_MICROSCOPE_CALIBRATION_DIR", "/var/lib/mg400/calibration/microscope")


def runs_root():
    return _root("MG400_ORCH_RUNS_DIR", "/var/lib/mg400/runs")


def _root(setting, default):
    path = Path(os.environ.get(setting, default))
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise ValueError(f"{setting} must be an absolute path without parent traversal")
    return path


def _beneath(root, value):
    path = Path(value)
    if ".." in path.parts:
        raise ValueError("Host data paths must not contain parent traversal")
    path = path if path.is_absolute() else root / path
    if not path.is_relative_to(root):
        raise ValueError("Output path must be inside the configured host data directory")
    return path


def calibration_path(value):
    return _beneath(calibration_root(), value)


def run_path(value):
    return _beneath(runs_root(), value)


def _data_root(path):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Host output paths must be absolute without parent traversal")
    matches = [root for root in (calibration_root(), runs_root()) if path.is_relative_to(root)]
    if not matches:
        raise ValueError("Path is outside the configured host data directories")
    return max(matches, key=lambda root: len(root.parts))


def _open_directory(path, *, exclusive=False):
    root = _data_root(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for index, component in enumerate(path.parts[1:], 1):
            if exclusive and index == len(path.parts) - 1:
                os.mkdir(component, mode=0o750, dir_fd=descriptor)
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode=0o750, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            if index >= len(root.parts) - 1:
                info = os.fstat(descriptor)
                if info.st_uid != os.geteuid() or info.st_mode & 0o027:
                    raise ValueError("Host data directories must be owned and private")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def directory(path, *, exclusive=False):
    """Walk without symlinks and validate each directory before creating children."""
    path = Path(path)
    descriptor = _open_directory(path, exclusive=exclusive)
    os.close(descriptor)
    return path


def private_open(path, mode="w", **kwargs):
    """Open private outputs relative to a validated, pinned directory descriptor."""
    path = Path(path)
    _data_root(path)
    if mode not in ("w", "x", "wb", "xb"):
        raise ValueError("Output mode must be w or x, optionally binary")
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    if mode.startswith("x"):
        flags |= os.O_EXCL
    parent = _open_directory(path.parent)
    try:
        descriptor = os.open(path.name, flags, 0o640, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError("Host output must be an owned regular file with one link")
        if info.st_mode & 0o037:
            raise ValueError("Host output file permissions are not private")
        if mode.startswith("w"):
            os.ftruncate(descriptor, 0)
        return os.fdopen(descriptor, mode, **kwargs)
    except BaseException:
        os.close(descriptor)
        raise
