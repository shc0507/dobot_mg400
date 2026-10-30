"""Verify and install external assets without following symbolic links."""

import hashlib
import os
import re
import secrets
import stat
import tomllib
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

OWNER = (1000, 1000)
DIRECTORY_MODE = 0o750
FIELDS = {"url", "sha256", "bytes", "install", "mode", "service", "source", "license"}
NAME = re.compile(r"[a-z][a-z0-9-]*")
CHUNK = 1024 * 1024


class Asset(NamedTuple):
    name: str
    url: str
    sha256: str
    bytes: int
    install: PurePosixPath
    mode: int
    service: str
    source: str
    license: str


def _https(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        raise ValueError("Asset URL must use HTTPS without credentials or a fragment.")


class HTTPSRedirectHandler(HTTPRedirectHandler):
    """Refuse an insecure redirect before urllib sends another request."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_download(url, *, timeout):
    _https(url)
    return build_opener(HTTPSRedirectHandler()).open(url, timeout=timeout)


def load_manifest(path):
    """Read one table per asset. Refuse unknown fields and unsafe paths or modes."""
    with _local(path) as stream:
        tables = tomllib.load(stream)
    assets = {}
    destinations = set()
    for name, table in tables.items():
        if not NAME.fullmatch(name) or not isinstance(table, dict) or set(table) != FIELDS:
            raise ValueError(f"Invalid asset table: {name}")
        for field in FIELDS - {"bytes"}:
            if not isinstance(table[field], str) or not table[field].strip():
                raise ValueError(f"Asset {name}: {field} must be a nonempty string.")
        _https(table["url"])
        if not re.fullmatch(r"[a-f0-9]{64}", table["sha256"]):
            raise ValueError(f"Asset {name}: invalid SHA-256.")
        if type(table["bytes"]) is not int or table["bytes"] <= 0:
            raise ValueError(f"Asset {name}: bytes must be a positive integer.")
        path = PurePosixPath(table["install"])
        if (
            path.anchor != "/"
            or len(path.parts) < 2
            or ".." in path.parts
            or str(path) != table["install"]
            or any(ord(character) < 32 for character in str(path))
        ):
            raise ValueError(f"Asset {name}: install must be a canonical absolute file path.")
        if path in destinations:
            raise ValueError(f"Asset {name}: duplicate install path.")
        destinations.add(path)
        if not re.fullmatch(r"0[0-7]{3}", table["mode"]):
            raise ValueError(f"Asset {name}: mode must be a four-digit octal string.")
        mode = int(table["mode"], 8)
        if mode not in (0o600, 0o640):
            raise ValueError(f"Asset {name}: mode must be 0600 or 0640.")
        if not NAME.fullmatch(table["service"]):
            raise ValueError(f"Asset {name}: invalid service name.")
        assets[name] = Asset(name, **{**table, "install": path, "mode": mode})
    return assets


def destination(asset, root="/"):
    """Map an absolute manifest destination below the host root used by tests."""
    return Path(root).absolute() / asset.install.relative_to("/")


@contextmanager
def _directory(path, *, create=False, owner=OWNER, chown=os.fchown):
    """Walk directory descriptors, refusing symbolic links in every component."""
    path = Path(path).absolute()
    if ".." in path.parts:
        raise ValueError(f"Parent traversal is not allowed in asset paths: {path}")
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            created = False
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, DIRECTORY_MODE, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                created = True
            os.close(fd)
            fd = child
            if created:
                chown(fd, *owner)
                os.fchmod(fd, DIRECTORY_MODE)
        yield fd
    finally:
        os.close(fd)


def _regular(path, directory):
    """Refuse a symbolic link or special file, but allow a missing destination."""
    try:
        info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Asset path is not a regular file: {path}")


def check_destination(asset, root="/"):
    """Check existing destination components without creating or changing them."""
    path = destination(asset, root)
    try:
        with _directory(path.parent) as directory:
            _regular(path, directory)
    except FileNotFoundError:
        pass


@contextmanager
def _local(path):
    path = Path(path).absolute()
    with _directory(path.parent) as directory:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Asset source is not a regular file: {path}")
        yield stream


def _verify(asset, stream, target=None):
    digest = hashlib.sha256()
    count = 0
    while block := stream.read(min(CHUNK, asset.bytes - count + 1)):
        count += len(block)
        if count > asset.bytes:
            raise ValueError(f"Asset {asset.name}: size mismatch (expected {asset.bytes} bytes).")
        digest.update(block)
        if target is not None:
            target.write(block)
    if count != asset.bytes:
        raise ValueError(f"Asset {asset.name}: size mismatch (expected {asset.bytes} bytes).")
    if digest.hexdigest() != asset.sha256:
        raise ValueError(f"Asset {asset.name}: SHA-256 mismatch.")


def validate_source(asset, source):
    """Check a local file before the installer changes the host."""
    with _local(source) as stream:
        _verify(asset, stream)


def install_asset(asset, *, root="/", source=None, dry_run=False, chown=os.fchown, opener=None):
    """Verify content and metadata before atomic replacement. Keep old files on failure."""
    check_destination(asset, root)
    if source is not None:
        validate_source(asset, source)
    path = destination(asset, root)
    if dry_run:
        return path
    with _directory(path.parent, create=True, chown=chown) as directory:
        # Host data belongs to the service account, with no access for other users.
        chown(directory, *OWNER)
        os.fchmod(directory, DIRECTORY_MODE)
        temporary = f".{path.name}.{secrets.token_hex(16)}.tmp"
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        try:
            with os.fdopen(fd, "wb") as target:
                incoming = (
                    _local(source)
                    if source is not None
                    else (opener or open_download)(asset.url, timeout=30)
                )
                with incoming as stream:
                    if source is None and hasattr(stream, "geturl"):
                        _https(stream.geturl())
                    _verify(asset, stream, target)
                target.flush()
                chown(target.fileno(), *OWNER)
                os.fchmod(target.fileno(), asset.mode)
                os.fsync(target.fileno())
            _regular(path, directory)
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    return path


def asset_status(asset, root="/"):
    """Return present, missing, or mismatch, including ownership and mode checks."""
    path = destination(asset, root)
    try:
        with _local(path) as stream:
            info = os.fstat(stream.fileno())
            if (info.st_uid, info.st_gid) != OWNER or stat.S_IMODE(info.st_mode) != asset.mode:
                return "mismatch"
            _verify(asset, stream)
    except FileNotFoundError:
        # A dangling symlink must report mismatch, not missing.
        return "mismatch" if path.is_symlink() else "missing"
    except (OSError, ValueError):
        return "mismatch"
    return "present"
