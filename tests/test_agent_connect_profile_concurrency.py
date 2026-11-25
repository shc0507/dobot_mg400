"""Check shared profile files with separate portal stores and processes."""

import multiprocessing
import time
import traceback

import pytest
from test_agent_connect_model_profiles import TEST_KEY, FakeProtector, add_profile

from orchestrator.agent_connect import profile_lock
from orchestrator.agent_connect.model_profiles import ModelProfileError, ModelProfileStore


def _write_profiles(path, start, ready, result, prefix):
    store = ModelProfileStore(path, FakeProtector())
    ready.put(prefix)
    if not start.wait(15):
        result.put("start timeout")
        return
    try:
        for index in range(8):
            add_profile(store, name=f"{prefix}-{index}")
        result.put("done")
    except ModelProfileError as error:
        result.put(str(error))


def _hold_profile_lock(path, ready, release):
    with profile_lock.profile_storage_lock(path):
        ready.put("locked")
        release.wait(15)


def test_separate_stores_merge_writes_and_refresh_selection(tmp_path):
    path = tmp_path / "profiles.json"
    first = ModelProfileStore(path, FakeProtector())
    second = ModelProfileStore(path, FakeProtector())
    first_id = add_profile(first, name="First")["profiles"][0]["id"]
    first.activate(first_id)
    second_id = add_profile(second, name="Second")["profiles"][1]["id"]
    assert second.active()["id"] == first_id
    second.activate(second_id)
    assert first.active()["id"] == second_id
    first.delete(first_id)
    assert [item["id"] for item in second.status()["profiles"]] == [second_id]
    second.activate(None)
    assert first.active() is None
    assert first.status() == second.status()


def test_concurrent_processes_preserve_every_saved_profile(tmp_path):
    path = tmp_path / "profiles.json"
    context = multiprocessing.get_context("spawn")
    start, ready, result = context.Event(), context.Queue(), context.Queue()
    processes = [
        context.Process(target=_write_profiles, args=(path, start, ready, result, name))
        for name in ("First", "Second")
    ]
    try:
        for process in processes:
            process.start()
        assert {ready.get(timeout=15), ready.get(timeout=15)} == {"First", "Second"}
        start.set()
        assert [result.get(timeout=15), result.get(timeout=15)] == ["done", "done"]
        for process in processes:
            process.join(15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
    status = ModelProfileStore(path, FakeProtector()).status()
    assert status["storage_available"] is True
    assert {item["name"] for item in status["profiles"]} == {
        f"{name}-{index}" for name in ("First", "Second") for index in range(8)
    }
    assert TEST_KEY.encode() not in path.read_bytes()


@pytest.mark.parametrize("crash", [False, True])
def test_lock_timeout_clears_credentials_and_recovers(tmp_path, monkeypatch, crash):
    path = tmp_path / "profiles.json"
    store = ModelProfileStore(path, FakeProtector())
    profile_id = add_profile(store)["profiles"][0]["id"]
    store.activate(profile_id)
    original = path.read_bytes()
    context = multiprocessing.get_context("spawn")
    ready, release = context.Queue(), context.Event()
    holder = context.Process(target=_hold_profile_lock, args=(path, ready, release))
    holder.start()
    try:
        assert ready.get(timeout=15) == "locked"
        monkeypatch.setattr(profile_lock, "LOCK_TIMEOUT_SECONDS", 0.05)
        started = time.monotonic()
        status = store.status()
        assert status["storage_available"] is False
        assert "busy" in status["storage_message"]
        assert status["profiles"] == []
        assert store.active() is None
        with pytest.raises(ModelProfileError, match="busy"):
            add_profile(store, name="Must not be saved")
        assert time.monotonic() - started < 2
        assert path.read_bytes() == original
        if crash:
            holder.terminate()
        else:
            release.set()
        holder.join(10)
        assert not holder.is_alive()
    finally:
        if holder.is_alive():
            holder.terminate()
        holder.join(5)
    assert store.status()["storage_available"] is True
    assert store.active()["id"] == profile_id
    add_profile(store, name="After release")
    assert len(store.status()["profiles"]) == 2


def test_external_corruption_cannot_restore_cached_profiles(tmp_path):
    path = tmp_path / "profiles.json"
    store = ModelProfileStore(path, FakeProtector())
    profile_id = add_profile(store)["profiles"][0]["id"]
    store.activate(profile_id)
    original = path.read_bytes()
    corrupt = b"broken file with " + TEST_KEY.encode()
    path.write_bytes(corrupt)
    assert store.active() is None
    status = store.status()
    assert status["profiles"] == [] and not status["storage_available"]
    assert TEST_KEY not in str(status)
    with pytest.raises(ModelProfileError) as caught:
        add_profile(store)
    assert TEST_KEY not in "".join(traceback.format_exception(caught.value))
    assert path.read_bytes() == corrupt
    path.write_bytes(original)
    assert store.active()["id"] == profile_id


def test_external_deletion_cannot_restore_cached_profiles(tmp_path):
    path = tmp_path / "profiles.json"
    store = ModelProfileStore(path, FakeProtector())
    profile_id = add_profile(store)["profiles"][0]["id"]
    store.activate(profile_id)
    path.unlink()
    assert store.active() is None
    assert store.status()["profiles"] == []
    status = add_profile(store, name="New file")
    assert [item["name"] for item in status["profiles"]] == ["New file"]


def test_lock_error_is_visible_without_private_details(tmp_path, monkeypatch):
    path = tmp_path / "profiles.json"
    store = ModelProfileStore(path, FakeProtector())
    add_profile(store)
    original = path.read_bytes()

    def fail(*_args):
        raise OSError(TEST_KEY)

    monkeypatch.setattr(profile_lock, "_windows_lock", fail)
    monkeypatch.setattr(profile_lock, "_posix_lock", fail)
    status = store.status()
    assert not status["storage_available"] and status["profiles"] == []
    assert "lock is unavailable" in status["storage_message"]
    assert store.active() is None
    with pytest.raises(ModelProfileError) as caught:
        add_profile(store)
    assert TEST_KEY not in "".join(traceback.format_exception(caught.value))
    assert path.read_bytes() == original
