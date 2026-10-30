import json
from uuid import UUID

import pytest

from mg400.gripper_feedback import FeedbackState, FileFeedback

SESSION = "a51c687e-95f5-4fa5-a73d-6088dba073f1"
RESTART = "ea97d1be-5302-44a1-8dfb-83d88defc935"


def sample(**changes):
    return {
        "session": SESSION,
        "seq": 0,
        "sent_at": 1000.0,
        "init": 1,
        "state": 1,
        "position": 1000,
        **changes,
    }


@pytest.fixture
def clocks(monkeypatch):
    values = {"wall": 1000.0, "monotonic": 10.0}
    monkeypatch.setattr("mg400.gripper_feedback.time.time", lambda: values["wall"])
    monkeypatch.setattr("mg400.gripper_feedback.time.monotonic", lambda: values["monotonic"])
    return values


def test_unchanged_file_does_not_refresh_or_invalidate_sample(tmp_path, clocks):
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(sample()))
    state = FeedbackState()
    source = FileFeedback(path, state)
    source.poll()
    clocks["monotonic"] += 0.1
    source.poll()
    assert state.get() == sample()
    clocks["monotonic"] += 0.7
    source.poll()
    assert state.get() is None
    path.write_text(json.dumps(sample(seq=1, position=0)))
    source.poll()
    assert state.get()["position"] == 0


@pytest.mark.parametrize("failure", ["missing", "json", "uninitialized"])
def test_file_failure_invalidates_and_new_sample_recovers(tmp_path, clocks, failure):
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(sample()))
    state = FeedbackState()
    source = FileFeedback(path, state)
    source.poll()
    if failure == "missing":
        path.unlink()
    elif failure == "json":
        path.write_text("{")
    else:
        path.write_text(json.dumps(sample(seq=1, init=0)))
    source.poll()
    assert state.get() is None
    path.write_text(json.dumps(sample(seq=2)))
    source.poll()
    assert state.get() == sample(seq=2)


def test_freshness_uses_monotonic_time_and_copies_samples(clocks):
    feedback = FeedbackState()
    assert feedback.get() is None
    payload = sample()
    assert feedback.receive(payload)
    payload["position"] = 0
    result = feedback.get()
    assert result == sample()
    result["position"] = 500
    assert feedback.get()["position"] == 1000
    clocks["wall"] += 100
    clocks["monotonic"] = 10.75
    assert feedback.get() == sample()
    assert feedback.get(now=9.99) is None
    clocks["monotonic"] = 10.751
    assert feedback.get() is None


@pytest.mark.parametrize("sequence", [1, 2])
def test_repeated_or_older_sequence_preserves_sample_without_refreshing_age(clocks, sequence):
    feedback = FeedbackState()
    assert feedback.receive(sample(seq=2))
    clocks["monotonic"] = 10.5
    assert not feedback.receive(sample(seq=sequence, position=0))
    assert feedback.get() == sample(seq=2)
    clocks["monotonic"] = 10.751
    assert not feedback.receive(sample(seq=2))
    assert feedback.get() is None
    assert feedback.receive(sample(seq=3, position=500))
    assert feedback.get()["position"] == 500


def test_new_session_restarts_sequence_and_rejects_retired_session(clocks):
    feedback = FeedbackState()
    assert feedback.receive(sample(seq=20))
    assert feedback.receive(sample(session=RESTART, seq=0, position=0))
    assert feedback.get()["position"] == 0
    clocks["monotonic"] = 10.5
    assert not feedback.receive(sample(session=SESSION.upper(), seq=21))
    assert feedback.get() == sample(session=RESTART, seq=0, position=0)
    clocks["monotonic"] = 10.751
    assert not feedback.receive(sample(session=SESSION, seq=22))
    assert feedback.get() is None
    assert feedback.receive(sample(session=RESTART, seq=1))
    assert not feedback.receive(sample(session=UUID(RESTART).hex, seq=1))
    assert feedback.get() == sample(session=RESTART, seq=1)


@pytest.mark.parametrize(
    "changes",
    [
        {"sent_at": 998.0},
        {"sent_at": 1002.0},
        {"state": 0, "position": 0},
        {"state": 1, "position": 1000},
        {"state": 2, "position": 500},
        {"state": 3, "position": 1000},
    ],
    ids=["sent_at_min", "sent_at_max", "state0", "state1", "state2", "state3"],
)
def test_valid_samples_are_accepted(clocks, changes):
    feedback = FeedbackState()
    payload = sample(**changes)
    assert feedback.receive(payload)
    assert feedback.get() == payload


INVALID_VALUES = [
    ("session", "not-a-uuid"),
    ("session", UUID(SESSION)),
    ("seq", -1),
    ("seq", True),
    ("seq", 1.0),
    ("sent_at", True),
    ("sent_at", "1000"),
    ("sent_at", float("nan")),
    ("sent_at", float("inf")),
    ("sent_at", -float("inf")),
    ("sent_at", 10**1000),
    ("sent_at", 997.999),
    ("sent_at", 1002.001),
    ("init", 0),
    ("init", 2),
    ("init", True),
    ("init", 1.0),
    ("state", -1),
    ("state", 4),
    ("state", True),
    ("state", 1.0),
    ("position", -1),
    ("position", 1001),
    ("position", True),
    ("position", 0.0),
    ("position", float("nan")),
]


def without(field):
    payload = sample(seq=1)
    del payload[field]
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        *(
            pytest.param(sample(**{"seq": 1, field: value}), id=f"{field}={value!r}"[:40])
            for field, value in INVALID_VALUES
        ),
        *(pytest.param(without(field), id=f"missing_{field}") for field in sample()),
        *(pytest.param(value, id=f"non_object={value!r}") for value in (None, [], "", False)),
    ],
)
def test_invalid_sample_stops_updates_until_new_valid_sample(clocks, payload):
    feedback = FeedbackState()
    assert feedback.receive(sample())
    assert not feedback.receive(payload)
    assert feedback.get() is None
    assert feedback.receive(sample(seq=2))


def control_sample(**changes):
    return dict(
        protocol=1,
        command_id=None,
        target=None,
        ack=False,
        locked=False,
        error=None,
        io_mode=None,
        **changes,
    )


@pytest.mark.parametrize(
    "initialized,message",
    [
        (0, "The gripper is not initialized."),
        (2, "Gripper initialization is in progress."),
    ],
)
def test_initialization_diagnostic_expires_without_exposing_position(clocks, initialized, message):
    feedback = FeedbackState()
    assert not feedback.receive(sample(init=initialized, state=65535, position=1070))
    assert feedback.get() is None
    assert feedback.diagnostic() == message
    clocks["wall"] += 100
    assert feedback.diagnostic(now=10.75) == message
    assert feedback.diagnostic(now=9.99) is None
    assert feedback.diagnostic(now=10.751) is None
    clocks["wall"] = 1000.0
    assert feedback.receive(sample(seq=1))
    assert feedback.diagnostic() is None
    assert feedback.get() == sample(seq=1)


def test_initialization_diagnostic_enforces_sequence_and_session_order(clocks):
    feedback = FeedbackState()
    assert feedback.receive(sample(seq=20))
    assert not feedback.receive(sample(seq=21, init=0))
    assert feedback.diagnostic() is not None
    assert not feedback.receive(sample(seq=21, init=0))
    assert feedback.diagnostic() == "The gripper is not initialized."
    assert not feedback.receive(sample(seq=20))
    assert feedback.get() is None
    assert not feedback.receive(sample(session=RESTART, seq=0, init=2))
    assert feedback.diagnostic() == "Gripper initialization is in progress."
    assert not feedback.receive(sample(seq=22, init=0))
    assert feedback.diagnostic() == "Gripper initialization is in progress."
    assert not feedback.receive(sample(seq=22))
    assert feedback.get() is None
    assert feedback.receive(sample(session=RESTART, seq=1))


@pytest.mark.parametrize(
    "changes",
    [
        {"session": "bad"},
        {"seq": True},
        {"sent_at": 997.999},
        {"sent_at": 1002.001},
        {"init": 3},
        {"init": False},
        {"state": None},
        {"state": 65536},
        {"position": None},
        {"position": -1},
        {"position": 65536},
        {"control": {**control_sample(), "locked": 0}},
        {"control": {}},
    ],
)
def test_invalid_packet_clears_status_without_retiring_or_registering_session(clocks, changes):
    feedback = FeedbackState()
    assert not feedback.receive(sample(seq=20, init=0))
    assert feedback.diagnostic() is not None
    assert feedback.get_status() is not None
    payload = sample(session=RESTART, seq=0, init=2, state=65535, position=1070)
    payload.update(changes)
    assert not feedback.receive(payload)
    assert feedback.get() is None
    assert feedback.get_status() is None
    assert feedback.diagnostic() is None
    assert feedback.receive(sample(seq=21))
    assert feedback.get_status() == sample(seq=21)
    assert feedback.receive(sample(session=RESTART))


def test_unchanged_file_does_not_refresh_status_or_diagnostic(tmp_path, clocks):
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(sample(init=0, state=65535, position=1070)))
    feedback = FeedbackState()
    source = FileFeedback(path, feedback)
    source.poll()
    assert feedback.diagnostic() == "The gripper is not initialized."
    assert feedback.get_status()["init"] == 0
    clocks["monotonic"] += 0.751
    source.poll()
    assert feedback.diagnostic() is None
    assert feedback.get_status() is None
    path.write_text(json.dumps(sample(seq=1, init=2)))
    source.poll()
    assert feedback.diagnostic() == "Gripper initialization is in progress."
    assert feedback.get_status()["init"] == 2
    path.write_text("{")
    source.poll()
    assert feedback.diagnostic() is None
    assert feedback.get_status() is None


@pytest.mark.parametrize(
    "change",
    [
        {"protocol": True},
        {"protocol": 0},
        {"protocol": 4},
        {"ack": 1},
        {"locked": 0},
        {"ack": True},
        {"target": 500},
        {"io_mode": True},
        {"io_mode": 2},
        {"command_id": "bad", "target": 500},
        {"command_id": SESSION, "target": 1001},
        {"error": []},
    ],
)
def test_invalid_control_feedback_clears_freshness(clocks, change):
    feedback = FeedbackState()
    assert feedback.receive(sample())
    assert not feedback.receive(sample(seq=1, control={**control_sample(), **change}))
    assert feedback.get() is None


@pytest.mark.parametrize("initialized", [0, 1, 2])
def test_raw_status_is_copied_and_expires_with_monotonic_time(clocks, initialized):
    feedback = FeedbackState()
    assert feedback.get_status() is None
    control = {
        **control_sample(),
        "protocol": 3,
        "operation": "initialize",
        "command_id": SESSION,
        "ack": initialized == 1,
        "detail": {"attempt": 1},
    }
    payload = sample(init=initialized, control=control)
    if initialized != 1:
        payload.update(state=65535, position=65535)
    assert feedback.receive(payload) is (initialized == 1)
    expected = json.loads(json.dumps(payload))
    payload["position"] = 0
    control["detail"]["attempt"] = 2
    assert feedback.get_status() == expected
    result = feedback.get_status()
    result["position"] = 0
    result["control"]["detail"]["attempt"] = 3
    assert feedback.get_status() == expected
    if initialized == 1:
        measurement = feedback.get()
        measurement["control"]["detail"]["attempt"] = 4
        assert feedback.get() == expected
    else:
        assert feedback.get() is None
    clocks["wall"] += 100
    assert feedback.get_status(now=10.75) == expected
    assert feedback.get_status(now=9.99) is None
    clocks["monotonic"] = 10.751
    assert feedback.get_status() is None


@pytest.mark.parametrize(
    "older",
    [
        {"session": RESTART, "seq": 0},
        {"session": RESTART, "seq": 1},
        {"session": SESSION, "seq": 21},
    ],
)
def test_old_packets_preserve_raw_status_without_refreshing_age(clocks, older):
    feedback = FeedbackState()
    assert feedback.receive(sample(seq=20))
    payload = sample(session=RESTART, seq=1, init=2, state=65535, position=1070)
    assert not feedback.receive(payload)
    clocks["monotonic"] = 10.5
    assert not feedback.receive(sample(**older))
    assert feedback.get_status() == payload
    assert feedback.diagnostic() == "Gripper initialization is in progress."
    clocks["monotonic"] = 10.751
    assert not feedback.receive(sample(**older))
    assert feedback.get_status() is None
    assert feedback.diagnostic() is None


@pytest.mark.parametrize(
    "protocol,operation,command_id,target,ack",
    [
        (1, None, None, None, False),
        (2, None, None, None, False),
        (3, None, None, None, False),
        (3, "position", SESSION, 0, False),
        (3, "position", SESSION, 1000, True),
        (3, "initialize", SESSION, None, False),
        (3, "initialize", SESSION, None, True),
    ],
)
def test_valid_control_feedback_is_accepted(clocks, protocol, operation, command_id, target, ack):
    feedback = FeedbackState()
    control = {
        **control_sample(),
        "protocol": protocol,
        "command_id": command_id,
        "target": target,
        "ack": ack,
    }
    if protocol == 3:
        control["operation"] = operation
    payload = sample(control=control)
    assert feedback.receive(payload)
    assert feedback.get() == payload
    assert feedback.get_status() == payload


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": None},
        {"operation": "unknown"},
        {"operation": True},
        {"operation": []},
        {"command_id": None},
        {"command_id": "bad"},
        {"target": 0},
        {"target": False},
        {"target": "0"},
        {"operation": "position"},
        {"operation": "position", "target": -1},
        {"operation": "position", "target": 1001},
        {"operation": "position", "target": True},
        {"operation": None, "command_id": None, "ack": True},
        {"operation": None, "command_id": None, "target": 0},
    ],
)
def test_invalid_protocol_three_operation_clears_status(clocks, changes):
    feedback = FeedbackState()
    assert not feedback.receive(sample(init=2))
    control = {
        **control_sample(),
        "protocol": 3,
        "operation": "initialize",
        "command_id": SESSION,
        **changes,
    }
    assert not feedback.receive(sample(seq=1, init=2, control=control))
    assert feedback.get_status() is None
    assert feedback.get() is None
    assert feedback.diagnostic() is None
