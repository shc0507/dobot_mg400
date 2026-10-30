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
    assert log.since(0) == {"events": entries[1:], "next": 3}
    assert log.since(2) == {"events": entries[2:], "next": 3}
    assert log.since(3) == {"events": [], "next": 3}
    assert log.since(99) == {"events": [], "next": 3}
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
