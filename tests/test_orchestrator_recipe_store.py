"""Recipe validation and private local run storage, without service access."""

import json
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.recipe import load_recipe, validate
from orchestrator.skills import Registry, default_registry
from orchestrator.store import RunStore


def recipe_data():
    return {
        "name": "offline",
        "version": 1,
        "params": {"chip_id": {"type": "string", "default": "chip-001"}},
        "steps": [
            {
                "skill": "sample.record",
                "params": {"chip_id": "${chip_id}", "note": "sample"},
                "timeout_s": 1,
            }
        ],
    }


def test_recipe_resolves_typed_parameters_and_required_services_before_a_run():
    data = recipe_data()
    data["steps"].append(
        {
            "skill": "robot.move",
            "params": {"x_mm": 200, "y_mm": 0, "z_mm": 100, "r_deg": 0},
            "timeout_s": 2,
        }
    )
    recipe = validate(data, default_registry(), {"chip_id": "chip-002"})
    assert recipe.params == {"chip_id": "chip-002"}
    assert recipe.steps[0].params["chip_id"] == "chip-002"
    assert recipe.steps[0].on_fail == "hold" and recipe.services == ("robot",)
    assert data["steps"][0]["params"]["chip_id"] == "${chip_id}"


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(version=True),
        lambda d: d.update(name=" "),
        lambda d: d.update(extra=True),
        lambda d: d.update(steps=[]),
        lambda d: d["params"]["chip_id"].update(type=[]),
        lambda d: d["params"]["chip_id"].pop("default"),
        lambda d: d["steps"][0].update(skill="missing"),
        lambda d: d["steps"][0].update(params={}),
        lambda d: d["steps"][0]["params"].update(note=1),
        lambda d: d["steps"][0]["params"].update(note="${missing}"),
        lambda d: d["steps"][0].update(timeout_s=True),
        lambda d: d["steps"][0].update(timeout_s=float("nan")),
        lambda d: d["steps"][0].update(timeout_s=10**400),
        lambda d: d["steps"][0].update(on_fail="retry"),
        lambda d: d["steps"][0].update(on_fail={}),
    ],
)
def test_invalid_recipe_is_rejected(change):
    data = recipe_data()
    change(data)
    with pytest.raises(ValueError):
        validate(data, default_registry())


def test_unknown_override_missing_integer_and_boolean_type(tmp_path):
    with pytest.raises(ValueError):
        validate(recipe_data(), default_registry(), {"unknown": "x"})
    registry = Registry()

    def scalar(ctx, count: int, enabled: bool):
        pass

    registry.register("scalar", scalar)
    data = recipe_data()
    data["steps"] = [
        {"skill": "scalar", "params": {"count": True, "enabled": True}, "timeout_s": 1}
    ]
    with pytest.raises(ValueError):
        validate(data, registry)
    data["steps"][0]["params"] = {"count": 1, "enabled": 1}
    with pytest.raises(ValueError):
        validate(data, registry)
    path = tmp_path / "bad.toml"
    path.write_text("not valid [ toml")
    with pytest.raises(ValueError):
        load_recipe(path, registry)


def test_store_records_sample_identity_and_private_artifacts(tmp_path):
    with RunStore(tmp_path / "runs") as store:
        recipe = validate(recipe_data(), default_registry())
        run_id = store.create(recipe)
        store.step(run_id, 0, "running")
        store.sample(run_id, 0, "chip-001", {"temperature_c": 20})
        store.event(run_id, "measurement", {"value": 20}, chip_id="chip-001", step=0)
        store.artifact(run_id, 0, "frame.txt", b"fixture", chip_id="chip-001")
        store.step(run_id, 0, "completed", result={"ok": True})
        row = store.get(run_id)
        assert row["chip_id"] == "chip-001" and len(run_id) == 32
        for table in ("steps", "samples", "artifacts", "events"):
            assert row[table][0]["chip_id"] == "chip-001"
        artifact = store.directory / row["artifacts"][0]["path"]
        assert artifact.read_bytes() == b"fixture" and artifact.stat().st_mode & 0o777 == 0o600
        assert artifact.parent.stat().st_mode & 0o777 == 0o750
        assert store.path.stat().st_mode & 0o777 == 0o600
        assert json.loads(row["samples"][0]["data_json"]) == {"temperature_c": 20}
        assert store.create(recipe) != run_id
    with RunStore(tmp_path / "runs", readonly=True) as store:
        assert len(store.runs()) == 2


@pytest.mark.parametrize("table", ["runs", "steps"])
def test_failed_create_rolls_back_rows_and_its_empty_run_directory(tmp_path, table):
    with RunStore(tmp_path / "runs") as store:
        store._db.execute(
            f"CREATE TRIGGER fail_insert BEFORE INSERT ON {table} "
            "BEGIN SELECT RAISE(FAIL, 'full'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.create(validate(recipe_data(), default_registry()))
        assert store.runs() == []
        assert list(store.directory.glob("*/")) == []


@pytest.mark.parametrize("failure", ["bytes", "fsync", "insert"])
def test_failed_artifact_removes_unindexed_file(tmp_path, monkeypatch, failure):
    with RunStore(tmp_path / "runs") as store:
        run_id = store.create(validate(recipe_data(), default_registry()))
        if failure == "fsync":
            monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("disk full")))
        if failure == "insert":
            store._db.execute(
                "CREATE TRIGGER fail_artifact BEFORE INSERT ON artifacts "
                "BEGIN SELECT RAISE(FAIL, 'full'); END"
            )
        with pytest.raises((ValueError, OSError, sqlite3.IntegrityError)):
            store.artifact(
                run_id,
                0,
                "result.txt",
                "not bytes" if failure == "bytes" else b"result",
                chip_id="chip-001",
            )
        assert store.get(run_id)["artifacts"] == []
        assert list((store.directory / run_id).iterdir()) == []


@pytest.mark.parametrize("location", ["directory", "database", "journal", "parent"])
def test_store_refuses_symlinks_without_modifying_targets(tmp_path, location):
    target = tmp_path / "target"
    target.mkdir(mode=0o750)
    directory = tmp_path / "runs"
    if location == "directory":
        directory.symlink_to(target, target_is_directory=True)
    elif location == "parent":
        link = tmp_path / "link"
        link.symlink_to(target, target_is_directory=True)
        directory = link / "runs"
    else:
        directory.mkdir(mode=0o750)
        db = directory / "runs.db"
        db.touch(mode=0o600)
        if location == "database":
            db.unlink()
            db.symlink_to(target / "secret")
        else:
            Path(str(db) + "-journal").symlink_to(target / "secret")
    with pytest.raises((ValueError, FileNotFoundError)):
        RunStore(directory)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_store_refuses_public_existing_database_or_sidecar(tmp_path, suffix):
    directory = tmp_path / "runs"
    directory.mkdir(mode=0o750)
    (directory / "runs.db").touch(mode=0o600)
    unsafe = directory / ("runs.db" + suffix)
    unsafe.touch(exist_ok=True)
    unsafe.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        RunStore(directory)
    assert unsafe.stat().st_mode & 0o777 == 0o644


def test_readonly_missing_store_makes_no_directory_and_chip_id_is_required(tmp_path):
    directory = tmp_path / "absent"
    with pytest.raises(FileNotFoundError):
        RunStore(directory, readonly=True)
    assert not directory.exists()
    with RunStore(directory) as store:
        run_id = store.create(validate(recipe_data(), default_registry()))
        with pytest.raises(ValueError):
            store.sample(run_id, 0, "", {})
        with pytest.raises(ValueError):
            store.artifact(run_id, 0, "../outside", b"x")


def test_partial_artifact_write_is_removed(tmp_path, monkeypatch):
    real_fdopen = os.fdopen

    class PartialWrite:
        def __init__(self, fd, mode):
            self.stream = real_fdopen(fd, mode)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stream.close()

        def write(self, data):
            self.stream.write(data[:1])
            self.stream.flush()
            raise OSError("disk full after first byte")

    with RunStore(tmp_path / "runs") as store:
        run_id = store.create(validate(recipe_data(), default_registry()))
        monkeypatch.setattr(os, "fdopen", PartialWrite)
        with pytest.raises(OSError, match="first byte"):
            store.artifact(run_id, 0, "result.txt", b"partial result")
        assert store.get(run_id)["artifacts"] == []
        assert list((store.directory / run_id).iterdir()) == []


def test_store_refuses_database_with_another_owner(tmp_path, monkeypatch):
    directory = tmp_path / "runs"
    with RunStore(directory):
        pass
    real_lstat = Path.lstat

    def lstat(path):
        info = real_lstat(path)
        if path == directory / "runs.db":
            return SimpleNamespace(st_uid=info.st_uid + 1, st_gid=info.st_gid, st_mode=info.st_mode)
        return info

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="ownership"):
        RunStore(directory)
