"""Persist local model profiles without exposing their API keys."""

import base64
import ipaddress
import json
import os
import re
import stat
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .credential_protection import WindowsCredentialProtector

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}
MAX_PROFILES = 32
TOKEN_STATION_BASE_URL = "https://physics.bytefuture.ai/v1"
MAX_FILE_BYTES = 1024 * 1024
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:-]*)*\Z")
PROFILE_ID = re.compile(r"[0-9a-f]{32}\Z")
PROFILE_FIELDS = {"id", "name", "provider", "model", "base_url", "api_key", "route"}


class ModelProfileError(ValueError):
    """Report a fixed error message without profile credentials."""


class _PrivateProfile(dict):
    def __repr__(self):
        return repr(
            {key: "[redacted]" if key == "api_key" else value for key, value in self.items()}
        )

    __str__ = __repr__


def validate_provider(value):
    if not isinstance(value, str) or value not in DEFAULT_BASE_URLS:
        raise ModelProfileError("Choose OpenAI, Anthropic or Gemini.")
    return value


def validate_route(value):
    if value not in ("direct", "token_station"):
        raise ModelProfileError("Choose Direct provider or Token Station.")
    return value


def validate_model(value):
    if (
        not isinstance(value, str)
        or len(value) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or not MODEL_NAME.fullmatch(value.strip())
    ):
        raise ModelProfileError("Enter a valid model ID with at most 128 characters.")
    return value.strip()


def normalize_base_url(provider, value=None, *, route="direct"):
    """Require HTTPS, except for HTTP endpoints on loopback addresses."""
    validate_provider(provider)
    validate_route(route)
    if value is None or value == "":
        return TOKEN_STATION_BASE_URL if route == "token_station" else DEFAULT_BASE_URLS[provider]
    message = "Use an HTTPS endpoint or loopback HTTP without credentials, query or fragment."
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char in value for char in "\\?#")
    ):
        raise ModelProfileError(message)
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        if (
            parts.scheme not in {"https", "http"}
            or not hostname
            or parts.username is not None
            or parts.password is not None
            or "%" in parts.netloc
            or (parts.port is not None and not 1 <= parts.port <= 65535)
            or parts.netloc.endswith(":")
        ):
            raise ValueError
        if ":" in hostname and re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parts.netloc) is None:
            raise ValueError
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
            labels = hostname.removesuffix(".").split(".")
            if len(hostname) > 253 or any(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                for label in labels
            ):
                raise ValueError from None
        if parts.scheme == "http" and not (
            hostname == "localhost" or (address is not None and address.is_loopback)
        ):
            raise ValueError
        authority = f"[{hostname}]" if ":" in hostname else hostname
        if parts.port is not None:
            authority += f":{parts.port}"
        return urlunsplit((parts.scheme, authority, parts.path.rstrip("/"), "", ""))
    except (ValueError, UnicodeError):
        raise ModelProfileError(message) from None


def _name(value):
    if (
        not isinstance(value, str)
        or len(value) > 120
        or not 1 <= len(value.strip()) <= 120
        or any(
            ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value
        )
    ):
        raise ModelProfileError("Enter a profile name with 1 to 120 characters.")
    return value.strip()


def _key(value):
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or not 1 <= len(value.strip()) <= 4096
        or any(ord(char) < 32 or ord(char) > 126 for char in value)
        or any(char.isspace() for char in value.strip())
    ):
        raise ModelProfileError(
            "Enter an API key with 1 to 4096 characters and no internal spaces."
        )
    return value.strip()


def _identifier(value):
    if not isinstance(value, str) or not PROFILE_ID.fullmatch(value):
        raise ModelProfileError("Choose an existing model profile.")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field.")
        result[key] = value
    return result


def _decode(value):
    return json.loads(value, object_pairs_hook=_unique_object)


def _checked_path(path):
    for item in (path, *path.parents):
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise ModelProfileError("Profile storage cannot use symbolic links.")
    return path


class ModelProfileStore:
    """Keep one active profile and write each change before returning success."""

    def __init__(self, path=None, protector=None):
        self._lock = threading.RLock()
        self._profiles = {}
        self._active_id = None
        self._storage_available = False
        self._storage_message = "Secure profile storage is unavailable."
        self._path = None
        self._protector = None
        try:
            self._protector = protector if protector is not None else WindowsCredentialProtector()
        except Exception:
            self._storage_message = "Secure profile storage requires Windows credential protection."
            return
        try:
            if path is None:
                root = os.environ.get("LOCALAPPDATA", "")
                if not root or not Path(root).is_absolute():
                    self._storage_message = "The local application data directory is unavailable."
                    return
                path = Path(root) / "AI Chemistry Console" / "agent-portal" / "model-profiles.json"
            self._path = _checked_path(Path(path).absolute())
            self._load()
        except Exception:
            self._storage_message = (
                "Saved model profiles could not be read. The file was not changed."
            )
            return
        self._storage_available = True
        self._storage_message = "Profile storage is ready."

    def status(self):
        with self._lock:
            return {
                "profiles": [
                    {key: value for key, value in profile.items() if key != "api_key"}
                    | {"has_key": True}
                    for profile in self._profiles.values()
                ],
                "active_profile_id": self._active_id,
                "storage_available": self._storage_available,
                "storage_message": self._storage_message,
            }

    def add(self, name, provider, model, base_url, api_key, route="direct"):
        with self._lock:
            self._require_storage()
            if len(self._profiles) >= MAX_PROFILES:
                raise ModelProfileError("Delete a profile before adding another. The limit is 32.")
            profile = _PrivateProfile(
                id=uuid.uuid4().hex,
                name=_name(name),
                provider=validate_provider(provider),
                route=validate_route(route),
                model=validate_model(model),
                base_url=normalize_base_url(provider, base_url, route=route),
                api_key=_key(api_key),
            )
            profiles = self._profiles | {profile["id"]: profile}
            self._save(profiles, self._active_id)
            self._profiles = profiles
            return self.status()

    def activate(self, profile_id_or_None):
        with self._lock:
            self._require_storage()
            if profile_id_or_None is not None:
                self._require_profile(profile_id_or_None)
            self._save(self._profiles, profile_id_or_None)
            self._active_id = profile_id_or_None
            return self.status()

    def delete(self, profile_id):
        with self._lock:
            self._require_storage()
            self._require_profile(profile_id)
            profiles = {key: value for key, value in self._profiles.items() if key != profile_id}
            active_id = None if profile_id == self._active_id else self._active_id
            self._save(profiles, active_id)
            self._profiles, self._active_id = profiles, active_id
            return self.status()

    def active(self):
        """Return credentials only to the interpreter, never to a response handler."""
        with self._lock:
            profile = self._profiles.get(self._active_id)
            return _PrivateProfile(profile) if profile is not None else None

    def _require_storage(self):
        if not self._storage_available:
            raise ModelProfileError(self._storage_message)

    def _require_profile(self, profile_id):
        if _identifier(profile_id) not in self._profiles:
            raise ModelProfileError("Choose an existing model profile.")

    def _load(self):
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError
        with self._path.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError
        wrapper = _decode(raw)
        if (
            not isinstance(wrapper, dict)
            or set(wrapper) != {"version", "protected"}
            or type(wrapper["version"]) is not int
            or wrapper["version"] != 1
            or not isinstance(wrapper["protected"], str)
        ):
            raise ValueError
        encrypted = base64.b64decode(wrapper["protected"], validate=True)
        plaintext = self._protector.unprotect(encrypted)
        if not isinstance(plaintext, bytes) or len(plaintext) > MAX_FILE_BYTES:
            raise ValueError
        content = _decode(plaintext)
        if (
            not isinstance(content, dict)
            or set(content) != {"profiles", "active_profile_id"}
            or not isinstance(content["profiles"], list)
            or len(content["profiles"]) > MAX_PROFILES
        ):
            raise ValueError
        profiles = {}
        for profile in content["profiles"]:
            if not isinstance(profile, dict) or set(profile) not in (
                PROFILE_FIELDS,
                PROFILE_FIELDS - {"route"},
            ):
                raise ValueError
            profile_id = _identifier(profile["id"])
            if profile_id in profiles:
                raise ValueError
            normalized = _PrivateProfile(
                id=profile_id,
                name=_name(profile["name"]),
                provider=validate_provider(profile["provider"]),
                route=validate_route(profile.get("route", "direct")),
                model=validate_model(profile["model"]),
                base_url=normalize_base_url(
                    profile["provider"], profile["base_url"], route=profile.get("route", "direct")
                ),
                api_key=_key(profile["api_key"]),
            )
            if normalized != dict(profile, route=profile.get("route", "direct")):
                raise ValueError
            profiles[profile_id] = normalized
        active_id = content["active_profile_id"]
        if active_id is not None and _identifier(active_id) not in profiles:
            raise ValueError
        self._profiles, self._active_id = profiles, active_id

    def _save(self, profiles, active_id):
        temporary = None
        try:
            plaintext = json.dumps(
                {"profiles": list(profiles.values()), "active_profile_id": active_id},
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            encrypted = self._protector.protect(plaintext)
            if not isinstance(encrypted, bytes) or not encrypted or encrypted == plaintext:
                raise ValueError
            raw = json.dumps(
                {"version": 1, "protected": base64.b64encode(encrypted).decode("ascii")},
                separators=(",", ":"),
            ).encode("ascii")
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError
            _checked_path(self._path)
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _checked_path(self._path)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".model-profiles-", dir=self._path.parent
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except Exception:
            raise ModelProfileError(
                "Model profiles could not be saved. The change was not applied."
            ) from None
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass
