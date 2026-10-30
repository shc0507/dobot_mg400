"""Private, atomic furnace recovery state. Construction performs no file I/O."""

import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

DEFAULT_STATE_PATH = Path("/var/lib/mg400/furnace/state.json")
MAX_STATE_BYTES = 1_000_000


class StateError(RuntimeError):
    """The recovery state cannot be read or saved safely."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate state key: {key}")
        result[key] = value
    return result


class StateStore:
    """Store one JSON object with mode 0600 in an owned 0750/0700 directory.

    Descriptor traversal refuses symlinks. Files must be private regular files
    with one link. Writes flush, replace atomically, then flush the directory.
    A persistent private .state.json.lock serializes reads, writes, and selective
    invalidation across threads and processes. Never delete this lock file.
    """

    def __init__(self, path=DEFAULT_STATE_PATH):
        self.path = Path(path).absolute()
        if ".." in self.path.parts or not self.path.name:
            raise ValueError("State path must not contain parent traversal")

    def _directory(self, *, create=False):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(self.path.anchor, flags)
        try:
            for part in self.path.parent.parts[1:]:
                created = False
                if create:
                    try:
                        os.mkdir(part, mode=0o750, dir_fd=descriptor)
                        created = True
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                if created:
                    os.fchmod(descriptor, 0o750)
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o027:
                raise StateError(
                    "State directory must be owned without group write or other access"
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _private_file(info):
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise StateError("State must be a private, owned regular file with one link")

    @contextmanager
    def _transaction(self, directory):
        descriptor = os.open(
            f".{self.path.name}.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        try:
            self._private_file(os.fstat(descriptor))
            # A paused writer must not delay the service's independent stops.
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)

    def _load(self, directory):
        try:
            descriptor = os.open(
                self.path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            self._private_file(info)
            if info.st_size > MAX_STATE_BYTES:
                raise StateError("State file is too large")
            data = stream.read(MAX_STATE_BYTES + 1)
        if len(data) > MAX_STATE_BYTES:
            raise StateError("State file is too large")
        value = json.loads(data, object_pairs_hook=_object)
        if not isinstance(value, dict):
            raise StateError("State must contain a JSON object")
        json.dumps(value, allow_nan=False)
        return value

    def load(self):
        """Return validated JSON, or None if the state file does not exist."""
        try:
            directory = self._directory()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise StateError("Cannot open the state directory safely") from error
        try:
            with self._transaction(directory):
                return self._load(directory)
        except (OSError, ValueError, RecursionError) as error:
            raise StateError("Cannot read a valid private recovery state") from error
        finally:
            os.close(directory)

    def save(self, value):
        """Atomically replace state after flushing the new file to disk."""
        if not isinstance(value, dict):
            raise StateError("State must contain a JSON object")
        try:
            payload = (json.dumps(value, allow_nan=False, sort_keys=True) + "\n").encode("utf-8")
        except (ValueError, TypeError, RecursionError) as error:
            raise StateError("State must contain finite JSON values") from error
        if len(payload) > MAX_STATE_BYTES:
            raise StateError("State file is too large")
        try:
            directory = self._directory(create=True)
        except OSError as error:
            raise StateError("Cannot create the private state directory") from error
        temporary = f".{self.path.name}.{secrets.token_hex(8)}.tmp"
        try:
            with self._transaction(directory):
                try:
                    info = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    self._private_file(info)
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=directory,
                )
                with os.fdopen(descriptor, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path.name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
        except OSError as error:
            raise StateError("Cannot save recovery state atomically") from error
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory)

    def invalidate_resumable(self):
        """Remove stale committed state after a fault save fails.

        Keep a durable fault or incomplete intent, which requires acknowledgement
        even in STOP. If storage rejects both save and invalidation, the service
        reports that limit instead of claiming that its fault is durable.
        """
        try:
            directory = self._directory()
        except FileNotFoundError:
            return False
        try:
            with self._transaction(directory):
                value = self._load(directory)
                if (
                    value is None
                    or value.get("locked")
                    or value.get("phase") in ("fault", "applying")
                ):
                    return False
                os.unlink(self.path.name, dir_fd=directory)
                os.fsync(directory)
                return True
        except (OSError, ValueError, RecursionError) as error:
            raise StateError("Cannot invalidate stale recovery state") from error
        finally:
            os.close(directory)
