"""Asset verification uses small local files and fake downloads, never the network."""

import hashlib
import io
import os
import runpy
import stat
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTENT = b"small model fixture\n"


@pytest.fixture
def module():
    return SimpleNamespace(**runpy.run_path(str(ROOT / "deploy/assets.py")))


@pytest.fixture
def asset(module):
    return module.Asset(
        "model",
        "https://assets.example/model",
        hashlib.sha256(CONTENT).hexdigest(),
        len(CONTENT),
        PurePosixPath("/var/lib/mg400/models/model.dat"),
        0o640,
        "vision",
        "test fixture",
        "AGPL-3.0",
    )


@pytest.fixture
def owners():
    changed = {}

    def chown(fd, uid, gid):
        assert isinstance(fd, int)
        changed[os.fstat(fd).st_ino] = (uid, gid)

    return chown, changed


def test_pinned_manifest(module):
    asset = module.load_manifest(ROOT / "config/assets.toml")["vision-weights"]
    assert asset.bytes == 23393117
    assert asset.sha256 == "0b684c470191540b0cfc8802b0d13149b47d7be76b901ad1dd59882297407a07"
    assert asset.url == (
        "https://github.com/shc0507/dobot_mg400/releases/download/vision-model-v1/"
        "chip-arm-yolo26s-seg-v1.pt"
    )
    assert str(asset.install) == "/var/lib/mg400/models/chip-arm-yolo26s-seg-v1.pt"
    assert asset.mode == 0o640 and asset.service == "vision" and asset.license == "AGPL-3.0"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", '"http://assets.example/model"'),
        ("url", '"https://user:secret@assets.example/model"'),
        ("url", '"file:///tmp/model"'),
        ("sha256", '"invalid"'),
        ("bytes", "true"),
        ("bytes", "0"),
        ("bytes", "-1"),
        ("install", '"relative/model"'),
        ("install", '"/tmp/../model"'),
        ("install", '"/tmp//model"'),
        ("install", '"/"'),
        ("install", '"/etc/model.pt"'),
        ("install", '"/var/lib/mg400"'),
        ("install", '"/var/lib/other/model.pt"'),
        ("mode", '"0666"'),
        ("mode", '"0644"'),
        ("mode", '"4600"'),
        ("mode", "640"),
        ("service", '"../vision"'),
        ("source", '""'),
        ("license", '""'),
    ],
)
def test_invalid_manifest_is_refused(module, tmp_path, field, value):
    lines = (ROOT / "config/assets.toml").read_text().splitlines()
    path = tmp_path / "assets.toml"
    path.write_text(
        "\n".join(f"{field} = {value}" if line.startswith(field + " =") else line for line in lines)
    )
    with pytest.raises(ValueError):
        module.load_manifest(path)


@pytest.mark.parametrize("change", ["unknown", "missing", "duplicate"])
def test_manifest_schema_and_unique_destinations(module, tmp_path, change):
    text = (ROOT / "config/assets.toml").read_text()
    if change == "unknown":
        text += 'extra = "unsupported"\n'
    elif change == "missing":
        text = text.replace('mode = "0640"\n', "")
    else:
        text += text.replace("[vision-weights]", "[another-model]")
    path = tmp_path / "assets.toml"
    path.write_text(text)
    with pytest.raises(ValueError):
        module.load_manifest(path)


@pytest.mark.parametrize("local", [False, True])
def test_install_verifies_then_replaces_with_private_service_metadata(
    module, asset, owners, tmp_path, local
):
    chown, changed = owners
    target = module.destination(asset, tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous model")
    old_inode = target.stat().st_ino
    source = tmp_path / "offline.dat"
    source.write_bytes(CONTENT)
    opener = Mock(return_value=io.BytesIO(CONTENT))
    assert (
        module.install_asset(
            asset, root=tmp_path, source=source if local else None, chown=chown, opener=opener
        )
        == target
    )
    assert target.read_bytes() == CONTENT and target.stat().st_ino != old_inode
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o750
    assert changed[target.stat().st_ino] == changed[target.parent.stat().st_ino] == (1000, 1000)
    assert list(target.parent.iterdir()) == [target]
    if local:
        opener.assert_not_called()
    else:
        opener.assert_called_once_with(asset.url, timeout=30)


@pytest.mark.parametrize("content", [CONTENT[:-1], CONTENT + b"x", b"x" * len(CONTENT)])
@pytest.mark.parametrize("local", [False, True])
def test_bad_content_keeps_original_file_and_cleans_temporary(
    module, asset, owners, tmp_path, content, local
):
    target = module.destination(asset, tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous model")
    target.chmod(0o600)
    old = target.stat()
    source = tmp_path / "offline.dat"
    source.write_bytes(content)
    with pytest.raises(ValueError, match="size mismatch|SHA-256 mismatch"):
        module.install_asset(
            asset,
            root=tmp_path,
            source=source if local else None,
            chown=owners[0],
            opener=lambda *a, **k: io.BytesIO(content),
        )
    assert target.read_bytes() == b"previous model"
    assert target.stat().st_ino == old.st_ino
    assert target.stat().st_mode == old.st_mode
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize("failure", ["download", "chown", "fsync", "replace"])
def test_io_failure_preserves_original(module, asset, owners, tmp_path, monkeypatch, failure):
    target = module.destination(asset, tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous model")
    original = target.stat().st_ino
    opener = Mock(return_value=io.BytesIO(CONTENT))
    chown = owners[0]
    if failure == "download":
        opener.side_effect = OSError("download failed")
    elif failure == "chown":

        def chown(fd, uid, gid):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise PermissionError("chown failed")
    else:
        monkeypatch.setattr(os, failure, Mock(side_effect=OSError(f"{failure} failed")))
    with pytest.raises(OSError):
        module.install_asset(asset, root=tmp_path, chown=chown, opener=opener)
    assert target.read_bytes() == b"previous model"
    assert target.stat().st_ino == original
    assert list(target.parent.iterdir()) == [target]


@pytest.mark.parametrize(
    "where", ["destination", "directory", "ancestor", "source", "source-parent"]
)
def test_symlinks_are_refused_without_changing_their_targets(
    module, asset, owners, tmp_path, where
):
    target = module.destination(asset, tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    sentinel = elsewhere / "sentinel"
    sentinel.write_bytes(CONTENT)
    source = sentinel
    if where == "destination":
        target.parent.mkdir(parents=True)
        target.symlink_to(sentinel)
    elif where in ("directory", "ancestor"):
        link = target.parent if where == "directory" else tmp_path / "var"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(elsewhere, target_is_directory=True)
    elif where == "source":
        source = tmp_path / "input-link"
        source.symlink_to(sentinel)
    else:
        (tmp_path / "linked-input").symlink_to(elsewhere, target_is_directory=True)
        source = tmp_path / "linked-input/sentinel"
    with pytest.raises((ValueError, OSError)):
        module.install_asset(asset, root=tmp_path, source=source, chown=owners[0])
    assert sentinel.read_bytes() == CONTENT
    assert list(elsewhere.iterdir()) == [sentinel]


def test_special_file_source_is_refused_without_blocking(module, asset, tmp_path):
    source = tmp_path / "fifo"
    os.mkfifo(source)
    with pytest.raises(ValueError, match="not a regular file"):
        module.validate_source(asset, source)


def test_dry_run_creates_nothing_and_never_downloads(module, asset, tmp_path):
    opener = Mock(side_effect=AssertionError("No download"))
    chown = Mock(side_effect=AssertionError("No metadata changes"))
    target = module.install_asset(asset, root=tmp_path, dry_run=True, opener=opener, chown=chown)
    assert not target.exists() and list(tmp_path.iterdir()) == []
    opener.assert_not_called()
    chown.assert_not_called()


def test_status_checks_content_permissions_owner_and_symlinks(module, asset, tmp_path, monkeypatch):
    target = module.destination(asset, tmp_path)
    assert module.asset_status(asset, tmp_path) == "missing"
    target.parent.mkdir(parents=True)
    target.write_bytes(CONTENT)
    target.chmod(asset.mode)
    monkeypatch.setitem(module.asset_status.__globals__, "OWNER", (os.getuid(), os.getgid()))
    assert module.asset_status(asset, tmp_path) == "present"
    target.chmod(0o666)
    assert module.asset_status(asset, tmp_path) == "mismatch"
    target.chmod(asset.mode)
    monkeypatch.setitem(module.asset_status.__globals__, "OWNER", (os.getuid() + 1, os.getgid()))
    assert module.asset_status(asset, tmp_path) == "mismatch"
    monkeypatch.setitem(module.asset_status.__globals__, "OWNER", (os.getuid(), os.getgid()))
    target.write_bytes(b"x" * len(CONTENT))
    assert module.asset_status(asset, tmp_path) == "mismatch"
    target.unlink()
    target.symlink_to(tmp_path / "missing")
    assert module.asset_status(asset, tmp_path) == "mismatch"


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_redirect_checks_scheme_before_the_next_request(
    module, asset, owners, tmp_path, monkeypatch, scheme
):
    from email.message import Message
    from urllib.request import HTTPHandler, HTTPSHandler, build_opener
    from urllib.response import addinfourl

    requested = []
    final = f"{scheme}://cdn.example/model"

    class FakeHTTPS(HTTPSHandler):
        def https_open(self, request):
            requested.append(request.full_url)
            headers = Message()
            if request.full_url == asset.url:
                headers["Location"] = final
                response = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
            else:
                response = addinfourl(io.BytesIO(CONTENT), headers, request.full_url, 200)
                response.msg = "OK"
            return response

    class RefuseHTTP(HTTPHandler):
        def http_open(self, request):
            raise AssertionError("An HTTP redirect must never be requested")

    monkeypatch.setitem(
        module.open_download.__globals__,
        "build_opener",
        lambda handler: build_opener(handler, FakeHTTPS(), RefuseHTTP()),
    )
    if scheme == "http":
        with pytest.raises(ValueError, match="HTTPS"):
            module.install_asset(asset, root=tmp_path, chown=owners[0])
        assert requested == [asset.url]
        assert not module.destination(asset, tmp_path).exists()
    else:
        module.install_asset(asset, root=tmp_path, chown=owners[0])
        assert requested == [asset.url, final]
        assert module.destination(asset, tmp_path).read_bytes() == CONTENT
