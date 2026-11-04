"""Persist a taught reference without accepting links or overwriting changed calibration."""

import fcntl
import json
import os
import stat
import uuid
from dataclasses import replace
from pathlib import Path

from .config import geometry_data, load_geometry, parse_geometry


def _directory(path):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in (".", ".."):
                raise ValueError("Stage configuration must use a normalized path.")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _same_directory(path, descriptor):
    current = _directory(path)
    try:
        a, b = os.fstat(current), os.fstat(descriptor)
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise ValueError("Stage configuration directory changed during teaching.")
    finally:
        os.close(current)


def _toml(geometry):
    data = geometry_data(geometry)

    def value(item):
        # The schema accepts finite numbers, ASCII names, lists and inline tables only.
        if isinstance(item, dict):
            return (
                "{"
                + ", ".join(f"{json.dumps(key)} = {value(val)}" for key, val in item.items())
                + "}"
            )
        return json.dumps(item, allow_nan=False)

    return (
        "\n\n".join(
            f"[{group}.{json.dumps(name)}]\n"
            + "\n".join(f"{key} = {value(item)}" for key, item in fields.items())
            for group, entries in data.items()
            for name, fields in entries.items()
        )
        + "\n"
    )


def teach_pixel(path, geometry, motor, pixel):
    path = Path(path).absolute()
    directory = _directory(path.parent)
    temporary = f".stage-reference-{uuid.uuid4().hex}.tmp"
    created = False
    try:
        fcntl.flock(directory, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Teaching requires a regular stage file with no links.")
        if load_geometry(path) != geometry:
            raise ValueError("Stage calibration changed. Restart before teaching.")
        reference = {**geometry.motors[motor].reference, "pixel": list(pixel)}
        axes = {**geometry.motors, motor: replace(geometry.motors[motor], reference=reference)}
        updated = replace(geometry, motors=axes)
        # Apply the full parser before any write, including bounds on detected pixels.
        updated = parse_geometry(geometry_data(updated))
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            stat.S_IMODE(info.st_mode),
            dir_fd=directory,
        )
        created = True
        with os.fdopen(descriptor, "w") as output:
            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
            os.fchown(output.fileno(), info.st_uid, info.st_gid)
            output.write(_toml(updated))
            output.flush()
            os.fsync(output.fileno())
        _same_directory(path.parent, directory)
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size) != (
            current.st_dev,
            current.st_ino,
            current.st_mtime_ns,
            current.st_ctime_ns,
            current.st_size,
        ) or load_geometry(path) != geometry:
            raise ValueError("Stage calibration changed during teaching.")
        os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        created = False
        os.fsync(directory)
        _same_directory(path.parent, directory)
        if load_geometry(path) != updated:
            raise ValueError("Taught reference could not be confirmed.")
        return updated
    finally:
        if created:
            os.unlink(temporary, dir_fd=directory)
        os.close(directory)
