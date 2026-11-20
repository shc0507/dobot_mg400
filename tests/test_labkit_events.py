import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from labkit.events import EventLog


def test_bounded_replay_matches_journal_and_owns_its_data(caplog):
    log = EventLog(2, clock=lambda: 123.5)
    fields = {"axes": [1, 2]}
    with caplog.at_level(logging.DEBUG, logger="labkit.events"):
        assert log.add("info", "connected", **fields) == 1
        fields["axes"].append(3)
        log.add("warning", "lease expired", client="a")
        log.add("error", "locked", code="unknown_outcome")
    entries = [json.loads(record.message) for record in caplog.records]
    assert entries[0]["fields"] == {"axes": [1, 2]}
    assert entries[0]["ts_unix"] == 123.5
    assert log.since(0)["events"] == entries[1:]
    assert log.since(0)["gap_reason"] == "history_evicted"
    assert log.since(0)["oldest"] == 2
    assert log.since(2)["events"] == entries[2:]
    assert not log.since(2)["gap"]
    assert log.since(3)["events"] == []
    assert log.since(99)["events"] == []
    assert log.since(99)["gap_reason"] == "cursor_ahead"
    assert log.since(99)["next"] == 3
    reply = log.since(0)
    reply["events"][0]["fields"]["client"] = "changed"
    assert log.since(1)["events"][0]["fields"]["client"] == "a"


def test_concurrent_events_have_unique_ordered_sequences():
    log = EventLog(100)
    with ThreadPoolExecutor(max_workers=8) as executor:
        sequences = list(executor.map(lambda i: log.add("info", "sample", value=i), range(100)))
    assert sorted(sequences) == list(range(1, 101))
    replay = log.since(0)
    assert [entry["seq"] for entry in replay["events"]] == list(range(1, 101))
    assert replay["next"] == 100


@pytest.mark.parametrize("maxlen", [0, -1, 1.5, True])
def test_invalid_capacities(maxlen):
    with pytest.raises(ValueError):
        EventLog(maxlen)


@pytest.mark.parametrize("seq", [-1, True, "0", 1.5])
def test_invalid_cursors(seq):
    with pytest.raises(ValueError):
        EventLog().since(seq)


def test_bad_event_does_not_consume_a_sequence():
    log = EventLog()
    with pytest.raises(ValueError):
        log.add("unknown", "bad")
    with pytest.raises(ValueError):
        log.add("info", "bad", value=float("nan"))
    assert log.add("info", "valid") == 1


def test_process_identity_detects_restart_even_after_counter_overtakes_cursor():
    old = EventLog(instance="first")
    old.add("info", "old")
    new = EventLog(instance="second")
    for index in range(4):
        new.add("info", "new", index=index)
    reply = new.since(1, instance=old.instance)
    assert reply["instance"] == "second"
    assert reply["restarted"] and reply["gap_reason"] == "instance_changed"
    assert [event["seq"] for event in reply["events"]] == [1, 2, 3, 4]
    assert all(event["instance"] == "second" for event in reply["events"])
    assert new.since(4, instance="second")["events"] == []
    assert not new.since(4, instance="second")["gap"]


def test_empty_log_has_identity_and_restart_metadata():
    log = EventLog()
    assert log.instance != EventLog().instance
    reply = log.since(0)
    assert reply["oldest"] == 1 and reply["next"] == 0 and not reply["gap"]
    assert log.since(0, instance="previous")["restarted"]


@pytest.mark.parametrize("instance", ["", "a\n", "é", "a" * 129, 3])
def test_invalid_instance_is_rejected(instance):
    with pytest.raises(ValueError):
        EventLog(instance=instance)
    with pytest.raises(ValueError):
        EventLog().since(instance=instance)
