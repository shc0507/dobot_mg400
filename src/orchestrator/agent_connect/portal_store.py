"""Keep one live pairing in current-user Windows protected storage."""

import base64
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from .credential_protection import WindowsCredentialProtector

MAX_PAIRING_BYTES = 32768
PAIRING_FIELDS = {"client_id", "credential", "name", "matching_code", "connect_intent"}


class PairingStorageError(ValueError):
    """Report a storage failure without including credentials."""


def _checked_path(path):
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise PairingStorageError("Pairing storage cannot use symbolic links.")
    return path


def _record(value):
    if not isinstance(value, dict) or set(value) != PAIRING_FIELDS:
        raise ValueError("Invalid pairing fields.")
    for key in PAIRING_FIELDS - {"connect_intent"}:
        text = value[key]
        if (
            not isinstance(text, str)
            or not 1 <= len(text) <= 256
            or any(
                ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in text
            )
        ):
            raise ValueError("Invalid pairing value.")
    if type(value["connect_intent"]) is not bool:
        raise ValueError("Invalid connection intent.")
    return dict(value)


class PortalPairingStore:
    """Protect pairing credentials and hold the file lock until the portal closes."""

    def __init__(self, path=None, protector=None):
        self._lock = threading.RLock()
        self._handle = None
        self._available = False
        self._message = "Secure pairing storage is unavailable."
        self._path = None
        try:
            self._protector = protector if protector is not None else WindowsCredentialProtector()
            if path is None:
                root = os.environ.get("LOCALAPPDATA", "")
                if not root or not Path(root).is_absolute():
                    raise ValueError
                path = Path(root) / "AI Chemistry Console" / "agent-portal" / "pairing.json"
            self._path = _checked_path(Path(path).absolute())
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _checked_path(self._path)
            lock_path = _checked_path(self._path.with_suffix(".lock"))
            self._handle = lock_path.open("a+b")
            self._handle.seek(0, os.SEEK_END)
            if not self._handle.tell():
                self._handle.write(b"0")
                self._handle.flush()
            self._handle.seek(0)
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self._message = "Another portal owns the saved pairing. Close that portal first."
                self._handle.close()
                self._handle = None
                return
            self._available = True
            self._message = "Pairing storage is ready."
        except Exception:
            self.close()
            self._message = "Secure pairing storage requires an available Windows user profile."

    def status(self):
        with self._lock:
            return {"storage_available": self._available, "storage_message": self._message}

    def load(self, origin):
        with self._lock:
            if not self._available:
                return {}
            try:
                path = _checked_path(self._path)
                if not path.exists():
                    return {}
                with path.open("rb") as handle:
                    raw = handle.read(MAX_PAIRING_BYTES + 1)
                if len(raw) > MAX_PAIRING_BYTES:
                    raise ValueError
                wrapper = json.loads(raw)
                if set(wrapper) != {"version", "protected"} or wrapper["version"] != 1:
                    raise ValueError
                plaintext = self._protector.unprotect(
                    base64.b64decode(wrapper["protected"], validate=True)
                )
                if not isinstance(plaintext, bytes) or len(plaintext) > MAX_PAIRING_BYTES:
                    raise ValueError
                payload = json.loads(plaintext)
                if set(payload) != {"origin", "pairing"}:
                    raise ValueError
                if payload["origin"] != origin:
                    self._message = "The saved pairing belongs to a different Console address."
                    self._available = False
                    return {}
                return _record(payload["pairing"]) if payload["pairing"] else {}
            except Exception:
                self._available = False
                self._message = "The saved pairing could not be read. Its file was not changed."
                return {}

    def save(self, origin, record):
        with self._lock:
            if not self._available:
                raise PairingStorageError(self._message)
            temporary = None
            try:
                pairing = _record(record) if record else None
                plaintext = json.dumps({"origin": origin, "pairing": pairing}).encode()
                protected = self._protector.protect(plaintext)
                if not isinstance(protected, bytes) or not protected or protected == plaintext:
                    raise ValueError
                raw = json.dumps(
                    {"version": 1, "protected": base64.b64encode(protected).decode("ascii")}
                ).encode()
                if len(raw) > MAX_PAIRING_BYTES:
                    raise ValueError
                _checked_path(self._path)
                descriptor, temporary = tempfile.mkstemp(prefix=".pairing-", dir=self._path.parent)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self._path)
            except Exception:
                self._available = False
                self._message = "The pairing could not be saved securely."
                raise PairingStorageError("The pairing could not be saved securely.") from None
            finally:
                if temporary is not None:
                    try:
                        Path(temporary).unlink(missing_ok=True)
                    except OSError:
                        pass

    def forget(self):
        """Remove stale reconnect intent if an encrypted replacement cannot be saved."""
        with self._lock:
            if self._handle is None:
                return
            try:
                _checked_path(self._path).unlink(missing_ok=True)
            except OSError:
                raise PairingStorageError("The saved pairing could not be removed.") from None

    def close(self):
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None
            self._available = False
