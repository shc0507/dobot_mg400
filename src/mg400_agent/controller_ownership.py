"""Exclude competing panels for the full robot connection session."""

import fcntl
import hashlib
import os
import tempfile
import threading
from pathlib import Path


class ControllerOwnership:
    """Hold a process lock until verified cleanup or process exit.

    All panels must use the same robot address and shared lock directory.
    Keep the descriptor open after uncertain cleanup, even if this object is lost.
    """

    def __init__(self, host, *, directory=None):
        self._directory = Path(
            directory
            or os.environ.get("MG400_CONTROLLER_LOCK_DIR")
            or Path(tempfile.gettempdir()) / "mg400-controller-locks"
        )
        key = hashlib.sha256(host.strip().lower().rstrip(".").encode()).hexdigest()
        self._path = self._directory / f"{key}.lock"
        self._fd = None
        self._lock = threading.Lock()

    @property
    def held(self):
        with self._lock:
            return self._fd is not None

    def acquire(self):
        """Acquire ownership before opening any robot connection. Never wait."""
        with self._lock:
            if self._fd is not None:
                return
            if not self._directory.is_absolute():
                raise RuntimeError("The controller lock directory must be an absolute path")
            fd = None
            try:
                self._directory.mkdir(parents=True, exist_ok=True)
                owner = self._directory.stat().st_uid
                if os.geteuid() == 0 and owner != 0:
                    # A lock file that root creates is unusable for the other panels.
                    raise RuntimeError(
                        f"Run as UID {owner}, the owner of {self._directory}, not as root."
                    )
                fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(
                    "Robot controlled by another panel. Disconnect that panel before connecting."
                ) from error
            except OSError as error:
                raise RuntimeError(f"Cannot acquire robot connection ownership: {error}") from error
            else:
                self._fd = fd
            finally:
                if fd is not None and fd != self._fd:
                    os.close(fd)

    def release(self):
        """Release only after callers finish verified cleanup and drain workers."""
        with self._lock:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
        # Keep the file: unlinking it could give competing owners different inodes.
