"""Read-only passport HTTP, storage and browser tests without network or devices."""

import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import orchestrator.store as store_module
from orchestrator.app import OrchestratorService, create_app
from orchestrator.recipe import Recipe, Step
from orchestrator.store import MAX_ARTIFACT_BYTES, RunStore

PRIVATE = "offline-private-instrument-token"
CHIP = " QR / # ? 芯片 "


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Passport tests cannot open devices or network connections")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def saved_recipe(chip, *, step_chip=None, name="Recorded recipe"):
    return Recipe(
        name,
        1,
        {"chip_id": chip},
        (
            Step(
                "robot.transfer",
                {"chip_id": step_chip or chip, "from_station": "A", "to_station": "B"},
                1,
                "hold",
            ),
        ),
        (),
    )


@pytest.fixture
def passport(tmp_path):
    def no_instruments(*args, **kwargs):
        raise AssertionError("Reading a passport cannot construct an instrument client")

    recipes = tmp_path / "recipes"
    recipes.mkdir()
    service = OrchestratorService(
        settings={
            "MG400_ORCH_ROBOT_TOKEN": PRIVATE,
            "MG400_ORCH_ROBOT_URL": "http://private.invalid:9999",
        },
        recipes_dir=recipes,
        runs_dir=tmp_path / "runs",
        client_factory=no_instruments,
    ).start(background=False)
    app = create_app(service, base_path="/lab.v1")
    fixture = SimpleNamespace(
        service=service, store=service.store, app=app, client=app.test_client()
    )
    yield fixture
    service.close()


def history(passport, chip=CHIP, **args):
    return passport.client.get(
        "/lab.v1/api/chip",
        query_string={"id": chip, **args},
    )


def artifact(passport, identity, chip=CHIP):
    return passport.client.get(
        "/lab.v1/api/chip/artifact/" + identity,
        query_string={"id": chip},
    )


def test_public_shell_and_fixed_assets_never_read_stored_chip_data(passport, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A public shell read stored chip data")

    monkeypatch.setattr(passport.store, "chip_history", forbidden)
    for path in (
        "/chip",
        "/chip/ordinary-chip",
        "/dashboard-static/passport.js",
        "/dashboard-static/passport.css",
    ):
        for method in ("get", "head"):
            response = getattr(passport.client, method)("/lab.v1" + path)
            assert response.status_code == 200
            assert PRIVATE not in response.get_data(as_text=True)
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert passport.client.get("/lab.v1/dashboard-static/../passport.py").status_code == 404


@pytest.mark.parametrize("method", ["get", "head"])
@pytest.mark.parametrize("path", ["/api/chip?id=known", "/chip"])
def test_history_reads_need_no_token(passport, method, path):
    call = getattr(passport.client, method)
    headers = {"Host": "foreign.invalid", "Origin": "https://foreign.invalid"}
    assert call("/lab.v1" + path, headers=headers).status_code == 200


@pytest.mark.parametrize(
    "chip",
    [
        CHIP,
        "/leading//slash",
        "MixedCASE",
        " space ",
        "../..",
        "x%2Fy",
        "x?y#z",
        "芯片",
        pytest.param("A" * 4296, id="long-qr-text"),
    ],
)
def test_qr_lookup_preserves_exact_identity(passport, chip):
    run = passport.store.create(saved_recipe(chip))
    result = history(passport, chip).json
    assert result["chip_id"] == chip and [item["id"] for item in result["items"]] == [run]
    assert history(passport, chip + " ").json["items"] == []
    shell = passport.client.get("/lab.v1/chip", query_string={"id": chip})
    assert shell.status_code == 200 and shell.location is None


def test_matching_mixed_run_does_not_include_other_chip_records(passport):
    run = passport.store.create(saved_recipe("other-chip", step_chip=CHIP))
    passport.store.sample(run, 0, "other-chip", {"pv_c": 999})
    passport.store.sample(run, 0, CHIP, {"pv_c": 40, "sample_time": 1000})
    passport.store.event(run, "transfer", {"from": "A", "to": "B"}, chip_id=CHIP, step=0)
    passport.store.event(run, "private-other-event", {}, chip_id="other-chip", step=0)
    passport.store.artifact(run, 0, "other.txt", b"unrelated", chip_id="other-chip")
    image = passport.store.artifact(run, 0, "frame.png", b"\x89PNG\r\n\x1a\nfixture", chip_id=CHIP)
    summary = history(passport).json["items"]
    assert (
        summary[0]["id"] == run and "chip_id" not in summary[0] and "recipe_json" not in summary[0]
    )
    for kind in ("steps", "samples", "events", "artifacts"):
        page = history(passport, kind=kind, run_id=run).json
        assert len(page["items"]) == 1 and page["next_offset"] is None
        assert page["items"][0]["chip_id"] == CHIP
        assert "other-chip" not in json.dumps(page)
    assert history(passport, kind="samples", run_id=run).json["items"][0]["data"] == {
        "pv_c": 40,
        "sample_time": 1000,
    }
    assert artifact(passport, image).data == b"\x89PNG\r\n\x1a\nfixture"
    assert artifact(passport, image, "other-chip").status_code == 404


def test_all_matching_records_are_reachable_by_pagination(passport):
    run = passport.store.create(saved_recipe(CHIP))
    for index in range(107):
        passport.store.sample(run, 0, "other", {"value": -index})
    for index in range(7):
        passport.store.sample(run, 0, CHIP, {"pv_c": index})
    offset, observed = 0, []
    while offset is not None:
        page = history(passport, kind="samples", run_id=run, limit=3, offset=offset).json
        observed.extend(item["data"]["pv_c"] for item in page["items"])
        offset = page["next_offset"]
    assert observed == list(range(7))


def test_search_and_download_never_modify_history_or_acquire_control(passport):
    run = passport.store.create(saved_recipe(CHIP))
    file_id = passport.store.artifact(run, 0, "result.txt", b"recorded", chip_id=CHIP)
    before = hashlib.sha256(passport.store.path.read_bytes()).digest()
    assert history(passport).status_code == 200
    assert artifact(passport, file_id).status_code == 200
    assert hashlib.sha256(passport.store.path.read_bytes()).digest() == before
    assert passport.service.lease.snapshot()["holder"] is None
    assert passport.service.current is passport.service.recovery is passport.service.job is None


def test_history_accepts_its_cursor_beyond_one_million_rows(passport, monkeypatch):
    observed = []

    def page(chip, *, kind, run_id, limit, offset):
        observed.append(offset)
        return {"items": [], "next_offset": 1000100 if offset == 1000000 else None}

    monkeypatch.setattr(passport.store, "chip_history", page)
    first = history(passport, offset=1000000).json
    following = history(passport, offset=first["next_offset"])
    assert following.status_code == 200 and following.json["next_offset"] is None
    assert history(passport, offset=2**53 - 1).status_code == 200
    assert history(passport, offset=2**53).status_code == 400
    assert observed == [1000000, 1000100, 2**53 - 1]


def test_history_redacts_credentials_endpoints_and_path_fields_without_inventing_values(passport):
    run = passport.store.create(saved_recipe(CHIP))
    passport.store.sample(
        run,
        0,
        CHIP,
        {
            "note": "<script>evil</script>\n" + PRIVATE,
            "endpoint": "http://private.invalid:9999",
            "file_path": "/host/private",
            "password": "hidden",
            "thermal": {"mean_c": 21.5},
            "missing": None,
        },
    )
    response = history(passport, kind="samples", run_id=run)
    data = response.json["items"][0]["data"]
    assert PRIVATE not in response.get_data(as_text=True)
    assert "private.invalid" not in response.get_data(as_text=True)
    assert data["file_path"] == data["password"] == "[redacted]"
    assert data["thermal"] == {"mean_c": 21.5} and data["missing"] is None
    assert data["note"] == "<script>evil</script>\n[redacted]"


@pytest.mark.parametrize(
    "payload",
    [
        "{invalid",
        '{"pv_c":NaN}',
        '{"pv_c":20,"pv_c":999}',
        '{"large":"' + "x" * (256 * 1024) + '"}',
    ],
)
def test_unreadable_recorded_json_is_explicit_not_a_measurement(passport, payload):
    run = passport.store.create(saved_recipe(CHIP))
    passport.store.sample(run, 0, CHIP, {})
    with passport.store._db:
        passport.store._db.execute("UPDATE samples SET data_json=? WHERE run_id=?", (payload, run))
    data = history(passport, kind="samples", run_id=run).json["items"][0]["data"]
    assert set(data) == {"unavailable"} and "256 KiB" in data["unavailable"]


@pytest.mark.parametrize(
    "content,mime",
    [
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF89a", "image/gif"),
        (b"RIFF1234WEBP", "image/webp"),
        (b"<svg onload='evil()'/>", "application/octet-stream"),
        (b"<html>active</html>", "application/octet-stream"),
    ],
)
def test_artifact_preview_types_use_bytes_and_all_downloads_remain_private(passport, content, mime):
    run = passport.store.create(saved_recipe(CHIP))
    identity = passport.store.artifact(run, 0, "misleading.png", content, chip_id=CHIP)
    response = artifact(passport, identity)
    assert response.status_code == 200 and response.data == content
    assert response.mimetype == mime
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert "sandbox" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"
    assert str(passport.store.directory) not in str(response.headers)


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"id": " "},
        {"id": "x", "kind": "sql"},
        {"id": "x", "kind": "steps"},
        {"id": "x", "run_id": "a" * 32},
        {"id": "x", "limit": "0"},
        {"id": "x", "limit": "101"},
        {"id": "x", "offset": "-1"},
        {"id": "x", "path": "../../runs.db"},
        [("id", "x"), ("id", "y")],
    ],
)
def test_invalid_history_queries_are_rejected_before_store_reads(passport, monkeypatch, query):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid query reached storage")

    monkeypatch.setattr(passport.store, "chip_history", forbidden)
    response = passport.client.get("/lab.v1/api/chip", query_string=query)
    assert response.status_code == 400


def test_history_errors_do_not_expose_storage_paths_or_credentials(passport, monkeypatch):
    def failure(*args, **kwargs):
        raise OSError(f"{passport.store.path}: {PRIVATE}")

    monkeypatch.setattr(passport.store, "chip_history", failure)
    response = history(passport)
    assert response.status_code == 503
    assert PRIVATE not in response.get_data(as_text=True) and str(
        passport.store.path
    ) not in response.get_data(as_text=True)


PASSPORT_CHIP = " target / %_?雪 "


def passport_recipe(chips=(PASSPORT_CHIP,), *, default="other"):
    return Recipe(
        "mixed chip fixture",
        1,
        {"chip_id": default},
        tuple(Step("sample.record", {"chip_id": chip}, 1, "hold") for chip in chips),
        (),
    )


@pytest.fixture
def passport_artifact(tmp_path):
    parent = tmp_path / "parent"
    parent.mkdir(mode=0o750)
    with RunStore(parent / "runs") as store:
        run_id = store.create(passport_recipe())
        artifact_id = store.artifact(run_id, 0, "image.png", b"safe fixture", chip_id=PASSPORT_CHIP)
        path = store.directory / run_id / f"{artifact_id}-image.png"
        yield store, run_id, artifact_id, path


def passport_pages(store, chip_id, kind, run_id=None):
    items, offset = [], 0
    while True:
        page = store.chip_history(chip_id, kind=kind, run_id=run_id, limit=1, offset=offset)
        assert set(page) == {"items", "next_offset"}
        assert len(page["items"]) <= 1
        items.extend(page["items"])
        if page["next_offset"] is None:
            return items
        assert page["next_offset"] == offset + 1
        offset = page["next_offset"]
        assert offset < 100


def test_passport_filters_each_kind_before_paging_and_uses_exact_chip(tmp_path):
    with RunStore(tmp_path / "runs") as store:
        run_id = store.create(passport_recipe(("other", PASSPORT_CHIP, "other", PASSPORT_CHIP)))
        unrelated = store.create(passport_recipe(("other",)))
        for index, chip in enumerate(("other", PASSPORT_CHIP, "other", PASSPORT_CHIP)):
            store.sample(run_id, index, chip, {"index": index})
            store.event(run_id, "reading", {"index": index}, chip_id=chip, step=index)
            store.artifact(run_id, index, f"image{index}.png", b"fixture", chip_id=chip)
        summaries = passport_pages(store, PASSPORT_CHIP, "runs")
        assert [row["id"] for row in summaries] == [run_id]
        assert set(summaries[0]) == {"id", "name", "status", "created_at", "updated_at", "detail"}
        for kind in ("steps", "samples", "events", "artifacts"):
            rows = passport_pages(store, PASSPORT_CHIP, kind, run_id)
            assert len(rows) == 2 and all(row["chip_id"] == PASSPORT_CHIP for row in rows)
            field = "position" if kind == "steps" else "step"
            assert [row[field] for row in rows] == [1, 3]
            if kind == "artifacts":
                assert all("path" not in row for row in rows)
                assert [row["name"] for row in rows] == ["image1.png", "image3.png"]
            with pytest.raises(ValueError, match="Unknown chip run"):
                store.chip_history(PASSPORT_CHIP, kind=kind, run_id=unrelated)
        for different in (PASSPORT_CHIP.strip(), PASSPORT_CHIP.upper(), "%", "target"):
            assert store.chip_history(different) == {"items": [], "next_offset": None}


def test_passport_runs_include_each_explicit_record_kind_and_default_identity(tmp_path):
    with RunStore(tmp_path / "runs") as store:
        run_ids = []
        for kind in ("runs", "steps", "samples", "events", "artifacts"):
            recipe = passport_recipe(
                (PASSPORT_CHIP,) if kind == "steps" else ("other",),
                default=PASSPORT_CHIP if kind == "runs" else "other",
            )
            run_id = store.create(recipe)
            run_ids.append(run_id)
            if kind == "samples":
                store.sample(run_id, 0, PASSPORT_CHIP, {})
            elif kind == "events":
                store.event(run_id, "reading", {}, chip_id=PASSPORT_CHIP)
            elif kind == "artifacts":
                store.artifact(run_id, 0, "sample.txt", b"fixture", chip_id=PASSPORT_CHIP)
        assert {row["id"] for row in passport_pages(store, PASSPORT_CHIP, "runs")} == set(run_ids)


def test_passport_readonly_reads_do_not_mutate_history(passport_artifact):
    store, run_id, artifact_id, _ = passport_artifact
    before = store.path.read_bytes()
    before_rows = store.get(run_id)
    with RunStore(store.directory, readonly=True) as reader:
        assert reader.chip_history(PASSPORT_CHIP)["items"][0]["id"] == run_id
        result = reader.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP)
        assert result["data"] == b"safe fixture"
        assert result["name"] == "image.png" and "path" not in result
    assert store.path.read_bytes() == before
    assert store.get(run_id) == before_rows


@pytest.mark.parametrize(
    "options",
    [
        {"kind": "gateway_calls"},
        {"kind": ["runs"]},
        {"limit": None},
        {"limit": True},
        {"limit": 0},
        {"limit": 1001},
        {"offset": True},
        {"offset": -1},
        {"offset": 2**64},
        {"run_id": "../outside"},
    ],
)
def test_passport_rejects_invalid_page_parameters(passport_artifact, options):
    store, _, _, _ = passport_artifact
    with pytest.raises(ValueError):
        store.chip_history(PASSPORT_CHIP, **options)


@pytest.mark.parametrize("chip", ["other", PASSPORT_CHIP.strip(), PASSPORT_CHIP.upper()])
def test_passport_artifact_requires_exact_chip_before_filesystem_access(
    passport_artifact, monkeypatch, chip
):
    store, _, artifact_id, _ = passport_artifact

    def refuse(*args, **kwargs):
        raise AssertionError("An unrelated chip must never open artifact paths")

    monkeypatch.setattr(store_module.os, "open", refuse)
    with pytest.raises(ValueError, match="Unknown chip artifact"):
        store.artifact_bytes(artifact_id, chip_id=chip)


@pytest.mark.parametrize("bad_id", ["../outside", "0" * 31, "A" * 32, True])
def test_passport_artifact_rejects_nonopaque_ids(passport_artifact, bad_id):
    store, _, _, _ = passport_artifact
    with pytest.raises(ValueError, match="identity"):
        store.artifact_bytes(bad_id, chip_id=PASSPORT_CHIP)


@pytest.mark.parametrize("limit", [True, 0, -1, MAX_ARTIFACT_BYTES + 1, None])
def test_passport_artifact_read_limit_cannot_be_disabled(passport_artifact, limit):
    store, _, artifact_id, _ = passport_artifact
    with pytest.raises(ValueError, match="read limit"):
        store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP, max_bytes=limit)


def test_passport_oversize_rejected_before_read_and_exact_limit_accepted(
    passport_artifact, monkeypatch
):
    store, _, artifact_id, path = passport_artifact
    original_read = os.read
    requests = []

    def read(fd, count):
        requests.append(count)
        return original_read(fd, count)

    monkeypatch.setattr(store_module.os, "read", read)
    with pytest.raises(ValueError, match="read limit"):
        store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP, max_bytes=3)
    assert requests == []
    data = store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP, max_bytes=path.stat().st_size)
    assert data["data"] == b"safe fixture" and requests[-1] == 1


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_passport_rejects_unsafe_file_before_any_readable_open(
    passport_artifact, tmp_path, monkeypatch, kind
):
    store, _, artifact_id, path = passport_artifact
    other = tmp_path / "outside"
    other.write_bytes(b"not an artifact")
    other.chmod(0o600)
    path.unlink()
    if kind == "symlink":
        path.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, path)
    else:
        os.mkfifo(path, 0o600)
    original_open = os.open
    opened = []

    def open_checked(name, flags, *args, **kwargs):
        opened.append((str(name), flags))
        assert flags & os.O_PATH, "Unsafe artifact must never get a readable descriptor"
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(store_module.os, "open", open_checked)
    with pytest.raises(ValueError, match="unsafe"):
        store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP)
    assert opened


@pytest.mark.parametrize("which", ["file", "run", "root"])
def test_passport_rejects_unsafe_modes(passport_artifact, which):
    store, _, artifact_id, path = passport_artifact
    selected = {"file": path, "run": path.parent, "root": store.directory}[which]
    selected.chmod(0o666 if which == "file" else 0o777)
    with pytest.raises(ValueError, match="unsafe"):
        store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP)


def test_passport_rejects_wrong_file_owner(passport_artifact, monkeypatch):
    store, _, artifact_id, path = passport_artifact
    inode = path.stat().st_ino
    original_fstat = os.fstat

    def fstat(fd):
        info = original_fstat(fd)
        if info.st_ino != inode:
            return info
        values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
        return SimpleNamespace(**{**values, "st_uid": info.st_uid + 1})

    monkeypatch.setattr(store_module.os, "fstat", fstat)
    with pytest.raises(ValueError, match="ownership"):
        store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP)


def test_passport_group_readonly_file_and_private_directory_are_supported(passport_artifact):
    store, _, artifact_id, path = passport_artifact
    path.chmod(0o640)
    path.parent.chmod(0o700)
    assert store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP)["data"] == b"safe fixture"


def test_passport_swap_to_fifo_after_path_open_never_opens_fifo_for_read(
    passport_artifact, monkeypatch
):
    store, _, artifact_id, path = passport_artifact
    original_open = os.open
    swapped = False

    def open_with_swap(name, flags, *args, **kwargs):
        nonlocal swapped
        descriptor = original_open(name, flags, *args, **kwargs)
        if str(name) == path.name:
            assert flags & os.O_PATH
            path.unlink()
            os.mkfifo(path, 0o600)
            swapped = True
        return descriptor

    monkeypatch.setattr(store_module.os, "open", open_with_swap)
    with pytest.raises(ValueError):
        store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP)
    assert swapped


@pytest.mark.parametrize("which", ["run", "root", "ancestor"])
def test_passport_refuses_symlinked_directories_after_store_open(passport_artifact, which):
    store, _, artifact_id, path = passport_artifact
    original = {"run": path.parent, "root": store.directory, "ancestor": store.directory.parent}[
        which
    ]
    moved = original.with_name(original.name + "-moved")
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="Cannot read artifact safely"):
        store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP)


def test_passport_refuses_replaced_run_root(passport_artifact):
    store, _, artifact_id, _ = passport_artifact
    root = store.directory
    root.rename(root.with_name("old-runs"))
    root.mkdir(mode=0o750)
    root.chmod(0o750)
    with pytest.raises(ValueError, match="Run directory was replaced"):
        store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP)


@pytest.mark.parametrize("mutation", ["replace", "modify", "link", "mode", "run_replace"])
def test_passport_rejects_concurrent_artifact_mutations(
    passport_artifact, tmp_path, monkeypatch, mutation
):
    store, _, artifact_id, path = passport_artifact
    original_read = os.read
    changed = False

    def read(fd, count):
        nonlocal changed
        block = original_read(fd, count)
        if not changed:
            changed = True
            if mutation == "replace":
                replacement = path.with_name("replacement")
                replacement.write_bytes(b"different")
                replacement.chmod(0o600)
                replacement.replace(path)
            elif mutation == "modify":
                path.write_bytes(b"different")
            elif mutation == "link":
                os.link(path, tmp_path / "extra-link")
            elif mutation == "mode":
                path.chmod(0o644)
            else:
                path.parent.rename(path.parent.with_name("old-run"))
                path.parent.mkdir(mode=0o750)
                path.parent.chmod(0o750)
        return block

    monkeypatch.setattr(store_module.os, "read", read)
    with pytest.raises(ValueError):
        store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP)
    assert changed


def test_passport_read_error_closes_every_open_descriptor(passport_artifact, monkeypatch):
    store, _, artifact_id, _ = passport_artifact
    original_open, original_fstat = os.open, os.fstat
    opened = []

    def open_recorded(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def read(*args):
        raise OSError("offline read failure")

    monkeypatch.setattr(store_module.os, "open", open_recorded)
    monkeypatch.setattr(store_module.os, "read", read)
    with pytest.raises(ValueError, match="Cannot read artifact safely"):
        store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP)
    assert opened
    for fd in opened:
        with pytest.raises(OSError):
            original_fstat(fd)


@pytest.mark.parametrize("path", ["../outside", "/absolute", "other/file", "{run}/{id}-../x"])
def test_passport_corrupt_artifact_path_is_not_exposed_or_opened(passport_artifact, path):
    store, run_id, artifact_id, _ = passport_artifact
    with store._db:
        store._db.execute(
            "UPDATE artifacts SET path=? WHERE id=?",
            (path.format(run=run_id, id=artifact_id), artifact_id),
        )
    with pytest.raises(ValueError):
        store.chip_history(PASSPORT_CHIP, kind="artifacts")
    with pytest.raises(ValueError):
        store.artifact_bytes(artifact_id, chip_id=PASSPORT_CHIP)


# The browser harness stays in the carded test file and runs without a server.
PASSPORT_DOM_SCRIPT = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const script = readFileSync('src/orchestrator/web/static/passport.js', 'utf8');
const html = readFileSync('src/orchestrator/web/templates/passport.html', 'utf8');
const drain = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => {
    resolve = a; reject = b; }); return { promise, resolve, reject }; };
class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = [];
      this.listeners = new Map(); this.attributes = {}; this.value = '';
      this.selectionStart = this.selectionEnd = 0; this.disabled = this.hidden = false;
      this.ownText = ''; }
  set value(value) { this.inputValue = this.tagName === 'TEXTAREA' ? String(value).replace(/\r\n?/g,
      '\n') : String(value); }
  get value() { return this.inputValue; }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() { return this.ownText + this.children.map(item => item.textContent).join(''); }
  set innerHTML(value) { throw new Error('Untrusted HTML insertion'); }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.ownText = ''; this.children = [...items]; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(event, callback) { this.listeners.set(event, callback); }
  emit(event, extra = {}) { if (event === 'click' && this.disabled) return;
      this.listeners.get(event)?.({ preventDefault() {}, ...extra }); }
}
const walk = element => [element, ...element.children.flatMap(walk)];
function browser(href = 'http://localhost:8772/lab/chip') {
  const nodes = new Map(), requests = [], timers = new Map(), windowEvents = new Map(),
      locations = [], revoked = [], blobURLs = [];
  let serial = 0;
  for (const match of html.matchAll(/<(\w+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
      const element = new Element(match[1]); element.hidden = /\shidden(?:\s|>)/.test(match[0]);
      nodes.set(match[2], element); }
  const document = { body: { dataset: { basePath: '/lab' } }, getElementById(id) {
      assert.ok(nodes.has(id), id); return nodes.get(id); }, createElement: tag => new Element(tag),
      createElementNS: (_namespace, tag) => new Element(tag) };
  class BrowserURL extends URL {
    static createObjectURL(blob) { const url = 'blob:passport-' + (blobURLs.length + 1);
        blobURLs.push({ url, blob }); return url; }
    static revokeObjectURL(url) { revoked.push(url); }
  }
  vm.runInNewContext(script, {
    document, window: { addEventListener: (event, callback) => windowEvents.set(event, callback) },
    location: { href }, history: { replaceState: (_state, _unused, url) => locations.push(url) },
    URL: BrowserURL, URLSearchParams, AbortController, Date, Blob,
    setTimeout: (callback, delay) => { const id = ++serial; timers.set(id, { callback, delay });
        return id; }, clearTimeout: id => timers.delete(id),
    localStorage: new Proxy({}, { get() { throw new Error('Persistent token'); } }), sessionStorage:
        new Proxy({}, { get() { throw new Error('Persistent token'); } }),
    fetch(url, options) { const request = { ...deferred(), url, options };
        options.signal.addEventListener('abort', () => request.reject(new Error('Aborted')));
        requests.push(request); return request.promise; },
  });
  return {
    requests, locations, revoked, blobURLs, nodes,
    get: id => nodes.get(id),
    async search(chip = 'chip-A') { nodes.get('chip-id').value = chip;
        nodes.get('chip-id').emit('input'); nodes.get('passport-form').emit('submit');
        await drain(); },
    async respond(index, data, status = 200) { requests[index].resolve({ ok:
        status >= 200 && status < 300, status, json: async () => data, blob: async () => data });
        await drain(); },
    async click(element) { (typeof element === 'string' ? nodes.get(element) :
        element).emit('click'); await drain(); },
    find(label) {
      const found = walk(nodes.get('passport-runs')).find(
        element => element.tagName === 'BUTTON' && element.textContent === label
      );
      assert.ok(found, 'button ' + label);
      return found;
    },
    async hide() { windowEvents.get('pagehide')(); await drain(); },
  };
}
const runId = 'a'.repeat(32), artifactId = 'b'.repeat(32);
const run = (id = runId) => ({ id, name: 'Recorded recipe', status: 'completed', detail:
    'Recorded result', created_at: 1700000000, updated_at: 1700000010 });
const pageData = (kind, items, next_offset = null, chip_id = 'chip-A',
    run_id = kind === 'runs' ? null : runId) => ({ chip_id, kind, run_id, items, next_offset });
async function open(page) {
  await page.search(); await page.respond(0, pageData('runs', [run()]));
      await page.click(page.find('Read this run'));
  assert.deepEqual(page.requests.slice(1).map(item => new URL(item.url,
      'http://localhost').searchParams.get('kind')), ['steps', 'samples', 'artifacts', 'events']);
}
const cases = {
  async exact_search_and_secret() {
    const page = browser(); assert.equal(page.requests.length, 0);
    const chip = ' /Chip?#\n ';
    await page.search(chip);
    const request = page.requests[0], url = new URL(request.url, 'http://localhost');
    assert.equal(url.searchParams.get('id'), chip);
    assert.equal(request.options.method, 'GET');
    assert.equal(request.options.headers.Authorization, undefined);
    assert.equal(request.options.redirect, 'error'); assert.equal(request.options.credentials,
        'omit'); assert.equal(request.options.cache, 'no-store');
    assert.equal(new URL(page.locations[0], 'http://localhost').searchParams.get('id'), chip);
    await page.respond(0, pageData('runs', [], null, chip));
    assert.match(page.get('passport-status').textContent,
        /No recorded runs match this exact chip ID/);
    assert.equal(page.get('selected-chip').textContent, chip);
    assert.doesNotMatch(html, /type="password"/); assert.doesNotMatch(script,
        /innerHTML|insertAdjacentHTML|document\.write/);
  },
  async initial_id_decoded_once() {
    const query = browser('http://localhost/lab/chip/wrong?id=%20%2FChip%3F%23%20');
    assert.equal(new URL(query.requests[0].url, 'http://localhost').searchParams.get('id'),
        ' /Chip?# ');
    const path = browser('http://localhost/lab/chip/%252FKeepCase');
    assert.equal(new URL(path.requests[0].url, 'http://localhost').searchParams.get('id'),
        '%2FKeepCase');
  },
  async raw_url_id_survives_textarea_changes() {
    const page = browser('http://localhost/lab/chip?id=Chip%0D%0AA');
    assert.equal(page.get('chip-id').value, 'Chip\nA');
    assert.equal(new URL(page.requests[0].url, 'http://localhost').searchParams.get('id'),
        'Chip\r\nA');
    await page.respond(0, { error: 'Unavailable' }, 503);
    page.get('passport-form').emit('submit'); await drain();
    assert.equal(new URL(page.requests[1].url, 'http://localhost').searchParams.get('id'),
        'Chip\r\nA');
    page.get('chip-id').value = 'Edited\nA'; page.get('chip-id').emit('input');
    page.get('passport-form').emit('submit'); await drain();
    assert.equal(new URL(page.requests[2].url, 'http://localhost').searchParams.get('id'),
        'Edited\nA');
  },
  async exact_clipboard_and_selection() {
    const page = browser();
    const field = page.get('chip-id');
    field.emit('paste', { clipboardData: { getData: () => 'QR\r\nA\rB' } });
    assert.equal(field.value, 'QR\nA\nB');
    page.get('passport-form').emit('submit'); await drain();
    assert.equal(new URL(page.requests[0].url, 'http://localhost').searchParams.get('id'),
        'QR\r\nA\rB');
    field.setSelectionRange(3, 4);
    field.emit('paste', { clipboardData: { getData: () => 'X\r\nY' } });
    page.get('passport-form').emit('submit'); await drain();
    assert.equal(new URL(page.requests[1].url, 'http://localhost').searchParams.get('id'),
        'QR\r\nX\r\nY\rB');
  },
  async independent_pagination() {
    const page = browser(); await page.search();
    await page.respond(0, pageData('runs', [run()], 25));
    assert.equal(page.get('more-runs').hidden, false);
    await page.click('more-runs');
    assert.equal(new URL(page.requests[1].url, 'http://localhost').searchParams.get('offset'),
        '25');
    await page.respond(1, pageData('runs', [run('c'.repeat(32))]));
    assert.match(page.get('run-count').textContent, /2 runs loaded.*End/);
    await page.click(page.find('Read this run'));
    await page.respond(2, pageData('steps', [{ position: 0, skill: 'robot.transfer', status:
        'completed', params: { from: 'tray' }, result: { recorded: true }, error: null, resolution:
        null }], 100));
    await page.respond(3, pageData('samples', [])); await page.respond(4, pageData('artifacts', [
        ])); await page.respond(5, pageData('events', []));
    await page.click(page.find('Load more steps'));
    const query = new URL(page.requests[6].url, 'http://localhost').searchParams;
    assert.equal(query.get('kind'), 'steps'); assert.equal(query.get('offset'), '100');
        assert.equal(query.get('run_id'), runId);
    await page.respond(6, pageData('steps', [{ position: 100, skill: 'sample.record', status:
        'completed' }]));
    assert.match(page.get('passport-runs').textContent, /2 records loaded\. End of records/);
    assert.ok(page.requests.every(item => item.options.method === 'GET'));
  },
  async explicit_pv_only() {
    const page = browser(); await open(page);
    await page.respond(2, pageData('samples', [
      { id: 1, step: 0, created_at: 1700000000, data: { temperature_c: 500 } },
      { id: 2, step: 1, created_at: 1700000001, data: { pv_c: 20 } },
      { id: 3, step: 1, created_at: 1700000002, data: { furnace: { pv_c: 21, sample_time:
          1700000010 }, sample_time: 1700000020 } },
      { id: 4, step: 1, created_at: 1700000003, data: { pv_c: true } },
    ]));
    await page.respond(4, pageData('events', [{ id: 5, step: 1, kind: 'reading', created_at:
        1700000004, data: { pv_c: 22 } }]));
    const tree = walk(page.get('passport-runs'));
    assert.equal(tree.filter(element => element.tagName === 'CIRCLE').length, 3);
    assert.match(page.get('passport-runs').textContent, /3 recorded points loaded/);
    assert.match(page.get('passport-runs').textContent, /Record time/);
        assert.match(page.get('passport-runs').textContent, /Sample time/);
    assert.match(page.get('passport-runs').textContent, /temperature_c/);
        // still available as recorded JSON, not misclassified PV
    assert.equal(tree.filter(element => element.tagName === 'SVG').length, 1);
    const rows = tree.filter(element => element.tagName === 'TR')
      .map(element => element.textContent);
    assert.ok(rows.some(row =>
      row.includes('2023-11-14T22:13:30.000Z') && row.includes('furnace.pv_c')
    ));
    assert.ok(!rows.some(row =>
      row.includes('2023-11-14T22:13:40.000Z') && row.includes('furnace.pv_c')
    ));
  },
  async text_only_and_unavailable_data() {
    const page = browser(); await open(page);
    const hostile = '<img src=x onerror=alert(1)>';
    await page.respond(1, pageData('steps', [{ position: 0, skill: hostile, status: 'failed',
        params: { input: hostile }, result: { unavailable: 'malformed recorded data' }, error:
        hostile, resolution: hostile }]));
    await page.respond(2, pageData('samples', [{ id: 1, step: 0, created_at: 1, data: { unavailable:
        'Oversized record', temperature_c: 100 } }]));
    assert.match(page.get('passport-runs').textContent, /<img src=x/);
    assert.match(page.get('passport-runs').textContent, /Oversized record/);
    assert.match(page.get('passport-runs').textContent, /No explicit furnace PV points/);
    const images = walk(page.get('passport-runs')).filter(element => element.tagName === 'IMG');
    assert.equal(images.length, 0);
  },
  async protected_artifact_and_revocation() {
    const page = browser(); await open(page);
    await page.respond(3, pageData('artifacts', [{ id: artifactId, name: 'image.png', step: 0,
        created_at: 1 }]));
    await page.click(page.find('Read artifact'));
    const request = page.requests[5];
    assert.equal(new URL(request.url, 'http://localhost').pathname,
        '/lab/api/chip/artifact/' + artifactId);
    assert.equal(request.options.headers.Authorization, undefined);
    await page.respond(5, new Blob(['fake image bytes'], { type: 'image/png' }));
    const image = walk(page.get('passport-runs')).find(element => element.tagName === 'IMG');
    assert.equal(image.src, 'blob:passport-1'); assert.doesNotMatch(image.src, /api|token/);
    const link = walk(page.get('passport-runs')).find(element => element.tagName === 'A');
    assert.equal(link.download, 'image.png'); assert.equal(link.href, image.src);
    await page.hide();
    assert.deepEqual(page.revoked, ['blob:passport-1']);
    assert.equal(page.get('passport-runs').children.length, 0);
  },
  async non_raster_never_embedded() {
    const page = browser(); await open(page);
    await page.respond(3, pageData('artifacts', [{ id: artifactId, name: '../unsafe.svg', step: 0,
        created_at: 1 }]));
    await page.click(page.find('Read artifact'));
    await page.respond(5, new Blob(['<svg onload="alert(1)"></svg>'], { type: 'image/svg+xml' }));
    const tree = walk(page.get('passport-runs'));
    assert.equal(tree.filter(element => element.tagName === 'IMG').length, 0);
    assert.match(page.get('passport-runs').textContent, /no safe raster preview/);
    assert.equal(tree.find(element => element.tagName === 'A').download, '.._unsafe.svg');
    assert.equal(page.blobURLs[0].blob.type, 'application/octet-stream');
  },
  async changed_search_ignores_retired_reply() {
    const page = browser(); await page.search('old-chip'); await page.search('new-chip');
    await page.respond(0, pageData('runs', [run()], null, 'old-chip'));
    assert.equal(page.get('passport-runs').children.length, 0);
    await page.respond(1, pageData('runs', [], null, 'new-chip'));
    assert.equal(page.get('selected-chip').textContent, 'new-chip');
    assert.match(page.get('passport-status').textContent, /No recorded runs/);
    await page.hide(); assert.equal(page.get('selected-chip').textContent, '');
    assert.equal(page.requests.length, 2);
  },
  async invalid_pages_and_errors() {
    const page = browser(); await page.search();
    await page.respond(0, pageData('runs', [run()], 0));
    assert.match(page.get('passport-status').textContent, /invalid continuation/);
    assert.equal(page.get('passport-runs').children.length, 0);
    await page.click('more-runs');
    await page.respond(1, { error: 'Refused by the service' }, 503);
    assert.match(page.get('passport-status').textContent, /Refused by the service/);
    assert.ok(page.requests.every(item => item.options.method === 'GET'));
  },
  async language_switch() {
    const page = browser();
    assert.equal(page.get('pp-console').textContent, 'Lab Console');
    await page.click('pp-lang');
    assert.equal(page.get('pp-title').textContent, '芯片履历');
    assert.equal(page.get('pp-lang').textContent, 'English');
    assert.equal(page.get('passport-status').textContent, '输入芯片 ID，读取其历史记录。');
    await page.search();
    assert.equal(page.get('passport-status').textContent, '正在读取运行记录…');
    await page.respond(0, pageData('runs', [run()]));
    assert.ok(walk(page.get('passport-runs')).some(element => element.textContent === '读取此次运行'));
    // Switching back reads the chip again in English. Storage stays unused when it is blocked.
    await page.click('pp-lang');
    assert.equal(page.requests.length, 2);
    await page.respond(1, pageData('runs', [run()]));
    assert.ok(walk(page.get('passport-runs')).some(element => element.textContent === 'Read this run'));
  },
  async rejected_artifact_retains_no_blob() {
    const page = browser(); await open(page);
    await page.respond(3, pageData('artifacts', [{ id: artifactId, name: 'image.png', step: 0,
        created_at: 1 }]));
    await page.click(page.find('Read artifact'));
    const pending = deferred();
    page.requests[5].resolve({ ok: true, blob: () => pending.promise }); await drain();
    await page.hide(); pending.resolve(new Blob(['bytes'], { type:
        'image/png' })); await drain();
    assert.equal(page.blobURLs.length, 0); assert.equal(page.get('passport-runs').children.length,
        0);
  },
};
const selected = process.argv[2];
if (selected) { assert.ok(Object.hasOwn(cases, selected), 'Known case'); await cases[selected]();
    console.log(selected + ' passed'); }
else { for (const [name, check] of Object.entries(cases)) { await check();
    console.log(name + ' passed'); } }
"""


@pytest.mark.parametrize(
    "case",
    [
        "exact_search_and_secret",
        "initial_id_decoded_once",
        "raw_url_id_survives_textarea_changes",
        "exact_clipboard_and_selection",
        "independent_pagination",
        "explicit_pv_only",
        "text_only_and_unavailable_data",
        "protected_artifact_and_revocation",
        "non_raster_never_embedded",
        "changed_search_ignores_retired_reply",
        "invalid_pages_and_errors",
        "rejected_artifact_retains_no_blob",
        "language_switch",
    ],
)
def test_passport_browser_behaviors(case):
    result = subprocess.run(
        ["node", "--input-type=module", "-", case],
        input=PASSPORT_DOM_SCRIPT,
        text=True,
        capture_output=True,
        cwd=Path(__file__).resolve().parents[1],
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert case + " passed" in result.stdout
