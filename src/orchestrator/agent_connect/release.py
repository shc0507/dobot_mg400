"""Identify the running Console source and its advertised contracts without Git."""

import hashlib
import json
from functools import lru_cache
from itertools import islice
from pathlib import Path

MAX_FILES = 512
MAX_FILE_BYTES = 1_000_000
MAX_TOTAL_BYTES = 16_000_000
SOURCE_SUFFIXES = {".py", ".js", ".css", ".html"}


def source_bundle(root):
    """Hash a bounded source bundle once at startup. Never read runtime settings."""
    root = Path(root)
    paths = list(
        islice(
            (path for path in root.rglob("*") if path.suffix in SOURCE_SUFFIXES and path.is_file()),
            MAX_FILES + 1,
        )
    )
    complete = len(paths) <= MAX_FILES
    fingerprint = hashlib.sha256()
    count = total = 0
    for path in sorted(paths[:MAX_FILES]):
        if path.is_symlink():
            complete = False
            continue
        try:
            with path.open("rb") as stream:
                content = stream.read(min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total) + 1)
            if len(content) > MAX_FILE_BYTES or total + len(content) > MAX_TOTAL_BYTES:
                complete = False
                continue
        except OSError:
            complete = False
            continue
        name = path.relative_to(root).as_posix().encode()
        fingerprint.update(len(name).to_bytes(4, "big") + name)
        fingerprint.update(len(content).to_bytes(8, "big") + content)
        count += 1
        total += len(content)
    return {
        "digest": "sha256:" + fingerprint.hexdigest() if count else None,
        "scope": "orchestrator Python, JavaScript, CSS, and HTML source",
        "complete": complete and count > 0,
        "file_count": count,
        "bytes_hashed": total,
    }


@lru_cache(maxsize=1)
def running_source_bundle():
    # A running process keeps its startup identity even if files change on disk.
    return source_bundle(Path(__file__).resolve().parents[1])


def metadata(hub, host=None, *, read_contract_id=None):
    """Combine source identity and current interfaces, without refreshing devices."""
    command_id = None
    if host is not None:
        try:
            command_id = host.commands().catalog()["contract_id"]
        except Exception:
            pass
    source = hub.source_bundle
    definition = {
        "source_bundle_id": source["digest"],
        "read_contract_id": read_contract_id or hub.contract_id,
        "command_contract_id": command_id,
    }
    encoded = json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()
    return {
        **definition,
        "release_id": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "source": dict(source),
        "complete": source["complete"] and (host is None or command_id is not None),
        "scope": "this-console",
    }
