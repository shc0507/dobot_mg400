"""Serialize profile access across local portal processes."""

import ctypes
import errno
import hashlib
import os
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

LOCK_TIMEOUT_SECONDS = 2.0


class ProfileLockError(RuntimeError):
    """Report a lock failure without file contents or credentials."""


@contextmanager
def profile_storage_lock(path):
    identity = hashlib.sha256(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()
    lock = _windows_lock if sys.platform == "win32" else _posix_lock
    with lock(identity):
        yield


@contextmanager
def _windows_lock(identity):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateMutexW(None, False, "Global\\AIChemistryModelProfiles-" + identity)
    if not handle:
        raise ProfileLockError("The model profile storage lock is unavailable.")
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, int(LOCK_TIMEOUT_SECONDS * 1000))
        if result == 0x102:
            raise ProfileLockError("Model profile storage is busy. Retry the operation.")
        if result not in (0, 0x80):  # An abandoned mutex now belongs to this thread.
            raise ProfileLockError("The model profile storage lock is unavailable.")
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


@contextmanager
def _posix_lock(identity):
    # This supports injected test protectors. Production credentials still require DPAPI.
    import fcntl

    path = Path(tempfile.gettempdir()) / f".ai-chemistry-profiles-{os.getuid()}-{identity}.lock"
    descriptor = None
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OSError
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise ProfileLockError(
                        "Model profile storage is busy. Retry the operation."
                    ) from None
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
    except OSError:
        if descriptor is not None:
            os.close(descriptor)
        raise ProfileLockError("The model profile storage lock is unavailable.") from None
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    try:
        yield
    finally:
        os.close(descriptor)
