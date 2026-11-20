"""Sample evidence acceptance checks with portable SQLite."""

import pytest

from orchestrator.agent_connect.samples import SampleTracker


def test_pending_handoff_and_confirmed_rejection():
    tracker = SampleTracker.memory()
    tracker.manual_report(
        "A", actor="human", location={"holder": "tray_slot", "tray_id": "T1", "slot_id": "A1"}
    )
    state = tracker.record_event(
        "A", "accepted", operation_id="pick", destination={"holder": "robot"}
    )
    assert state["location"]["holder"] == "unknown"
    assert state["last_known_location"]["location"]["slot_id"] == "A1"
    assert tracker.samples()["count"] == 1
    state = tracker.record_event("A", "rejected", operation_id="pick", no_motion_started=True)
    assert state["location"]["slot_id"] == "A1"
    assert state["pending_operation"] is None


def test_inferred_then_observed_and_entry_uncertainty():
    tracker = SampleTracker.memory()
    tracker.record_event("A", "accepted", operation_id="place", recorded_at=9)
    state = tracker.record_event(
        "A",
        "completed",
        operation_id="place",
        recorded_at=10,
        location={"holder": "stage_insert"},
        workflow_supports_location=True,
    )
    assert state["basis"] == "inferred"
    state = tracker.record_event(
        "A",
        "observation",
        recorded_at=12,
        location={"holder": "stage_insert"},
        observation={
            "observed_at": 11,
            "chip_id": "A",
            "identity_source": "assigned_slot_continuity",
            "source_assignment": "A",
            "source_empty": True,
            "target_present": True,
            "artifact_id": "image",
        },
    )
    assert state["basis"] == "observed"
    state = tracker.record_event("A", "entry", recorded_at=12, inside_furnace=True)
    assert state["location"]["inside_furnace"] is None
    state = tracker.record_event(
        "A",
        "entry",
        recorded_at=14,
        observed_at=13,
        mapping_valid=True,
        reference_valid=True,
        aligned=True,
        inside_furnace=True,
    )
    assert state["location"]["inside_furnace"] is True
    state = tracker.record_event("A", "partial", scope="entry")
    assert state["location"]["holder"] == "stage_insert"
    assert state["location"]["inside_furnace"] is None


def test_old_unknown_duplicate_and_conflicting_evidence():
    tracker = SampleTracker.memory()
    tracker.record_event(
        "A", "reported", actor="operator", recorded_at=20, location={"holder": "robot"}
    )
    for stamp in (10, None):
        state = tracker.record_event(
            "A",
            "observation",
            location={"holder": "tray_slot"},
            observation={"observed_at": stamp, "identity_source": "exact_id", "chip_id": "A"},
        )
        assert state["location"]["holder"] == "robot"
    payload = dict(actor="operator", location={"holder": "stage_insert"})
    first = tracker.record_event("A", "reported", event_id="once", **payload)
    again = tracker.record_event("A", "reported", event_id="once", **payload)
    assert first == again
    with pytest.raises(ValueError):
        tracker.record_event("B", "reported", event_id="once", **payload)
    state = tracker.record_event(
        "A",
        "observation",
        observation={"observed_at": 30, "chip_id": "B"},
        location={"holder": "tray_slot"},
    )
    assert state["location"]["holder"] == "unknown"
    assert state["conflict"]
    assert state["last_known_location"]["location"]["holder"] == "stage_insert"
    assert state["updated_by"] == "operator"


def test_history_exact_chip_last_started_work_and_page():
    tracker = SampleTracker.memory()
    db = tracker.store._db
    with db:
        db.executemany(
            "INSERT INTO runs VALUES(?,?,?)",
            [("old", "A", "completed"), ("new", "A", "created"), ("mixed", "A", "running")],
        )
        db.executemany(
            "INSERT INTO steps VALUES(?,?,?,?,?,?)",
            [
                ("old", 0, "place", "A", "completed", 10),
                ("new", 0, "pick", "A", "pending", None),
                ("mixed", 0, "pick", "B", "running", 20),
            ],
        )
        db.execute("INSERT INTO artifacts VALUES('A-extra')")
    page = tracker.samples(selection="all", limit=1)
    assert page["total"] == 3 and not page["complete"]
    state = tracker.samples(selection="ids", chip_ids=["A"])["items"][0]
    assert state["last_run"]["id"] == "old"
    assert state["last_step"] == {
        "run_id": "old",
        "index": 0,
        "name": "place",
        "status": "completed",
    }
    assert state["location"]["holder"] == "unknown"
    assert [item["chip_id"] for item in tracker.samples()["items"]] == ["B"]
    unknown = tracker.samples(selection="ids", chip_ids=["absent"])["items"][0]
    assert unknown["last_step"] is None
    revision = tracker.samples(selection="all")["revision"]
    assert tracker.samples(selection="all")["revision"] == revision
    with pytest.raises(ValueError):
        tracker.samples(selection="ids", chip_ids=["A"] * 51)


def test_same_capture_conflict_and_unidentified_detection():
    tracker = SampleTracker.memory()
    state = tracker.record_event(
        "A", "observation", location={"holder": "robot"}, observation={"observed_at": 10}
    )
    assert state["basis"] == "unknown"
    tracker.record_event(
        "A",
        "observation",
        location={"holder": "robot"},
        observation={"observed_at": 20, "identity_source": "exact_id", "chip_id": "A"},
    )
    state = tracker.record_event(
        "A",
        "observation",
        location={"holder": "stage_insert"},
        observation={"observed_at": 20, "identity_source": "exact_id", "chip_id": "A"},
    )
    assert state["basis"] == "unknown"
    assert state["conflict"]


def test_invalid_event_rolls_back_both_rows_and_manual_basis_cannot_override():
    tracker = SampleTracker.memory()
    with pytest.raises(ValueError):
        tracker.record_event("A", "reported", location={"holder": "robot"})
    assert tracker.store._db.execute("SELECT count(*) FROM chip_events").fetchone()[0] == 0
    assert tracker.samples(selection="all")["revision"] == 0
    with pytest.raises(TypeError):
        tracker.manual_report("A", actor="agent", location={"holder": "robot"}, basis="observed")
    result = tracker.manual_report("A", actor={"kind": "agent", "id": "a"}, unknown=True)
    assert result["basis"] == "unknown"
    assert result["updated_by"]["id"] == "a"


def test_reads_do_not_write_and_reject_invalid_inputs():
    tracker = SampleTracker.memory()
    with tracker.store._db:
        tracker.store._db.execute("INSERT INTO samples VALUES('legacy')")
    before = tracker.store._db.total_changes
    tracker.samples(selection="all")
    tracker.samples(selection="ids", chip_ids=["legacy"])
    assert tracker.store._db.total_changes == before
    for stamp in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            tracker.record_event("A", "observation", observation={"observed_at": stamp})
    for actor in ("", {}, {"kind": "human"}, {"id": " "}):
        with pytest.raises(ValueError):
            tracker.manual_report("A", actor=actor, unknown=True)
    with pytest.raises(ValueError):
        tracker.samples(selection="ids", chip_ids=["a" * 257])
    with pytest.raises(ValueError):
        tracker.record_event("a" * 257, "unknown")
    assert (
        tracker.samples(selection="ids", chip_ids=["a/b?#"])["items"][0]["passport_url"]
        == "/lab/chip?id=a%2Fb%3F%23"
    )


def test_continuity_needs_source_assignment_and_both_observations():
    tracker = SampleTracker.memory()
    state = tracker.record_event(
        "A",
        "observation",
        location={"holder": "robot"},
        observation={"observed_at": 10, "identity_source": "assigned_slot_continuity"},
    )
    assert state["basis"] == "unknown"
    tracker.record_event("A", "accepted", operation_id="place")
    state = tracker.record_event(
        "A", "completed", operation_id="place", location={"holder": "stage_insert"}
    )
    assert state["basis"] == "unknown"


def test_page_materialization_is_bounded_and_revision_change_rejects_cursor(monkeypatch):
    tracker = SampleTracker.memory()
    with tracker.store._db:
        tracker.store._db.executemany(
            "INSERT INTO samples VALUES(?)", [(f"chip-{i:05}",) for i in range(1500)]
        )
    calls = []
    original = tracker._history_state

    def read(chip_id, tables):
        calls.append(chip_id)
        return original(chip_id, tables)

    monkeypatch.setattr(tracker, "_history_state", read)
    exact = tracker.samples(selection="ids", chip_ids=["chip-00042"])
    assert exact["total"] == 1
    assert calls == ["chip-00042"]
    calls.clear()
    first = tracker.samples(selection="all", limit=7)
    assert first["total"] == 1500
    assert len(calls) == 7
    second = tracker.samples(
        selection="all", limit=7, offset=7, expected_revision=first["revision"]
    )
    assert second["items"][0]["chip_id"] == "chip-00007"
    tracker.manual_report("new", actor="human", unknown=True)
    with pytest.raises(ValueError, match="revision changed"):
        tracker.samples(selection="all", offset=14, expected_revision=first["revision"])
    with pytest.raises(ValueError):
        tracker.samples(limit=101)
    assert not tracker.store._db.in_transaction


def test_active_query_excludes_older_running_run_and_includes_pending():
    tracker = SampleTracker.memory()
    db = tracker.store._db
    with db:
        db.executemany(
            "INSERT INTO runs VALUES(?,?,?)",
            [("older", "A", "running"), ("newer", "A", "completed")],
        )
        db.executemany(
            "INSERT INTO steps VALUES(?,?,?,?,?,?)",
            [("older", 0, "place", "A", "running", 10), ("newer", 0, "pick", "A", "completed", 20)],
        )
    assert tracker.samples()["total"] == 0
    tracker.record_event("A", "accepted", operation_id="pending")
    assert tracker.samples()["total"] == 1


def test_conflict_capture_blocks_older_agreeing_frame():
    tracker = SampleTracker.memory()
    tracker.record_event(
        "A", "reported", actor="human", recorded_at=20, location={"holder": "robot"}
    )
    tracker.record_event(
        "A",
        "observation",
        recorded_at=35,
        location={"holder": "stage_insert"},
        observation={"chip_id": "B", "observed_at": 30, "identity_source": "exact_id"},
    )
    state = tracker.record_event(
        "A",
        "observation",
        recorded_at=36,
        location={"holder": "robot"},
        observation={"chip_id": "A", "observed_at": 25, "identity_source": "exact_id"},
    )
    assert state["location"]["holder"] == "unknown"
    assert state["conflict"]


def test_entry_requires_new_capture_after_partial_entry():
    tracker = SampleTracker.memory()
    tracker.record_event(
        "A", "reported", actor="human", recorded_at=10, location={"holder": "stage_insert"}
    )
    flags = dict(mapping_valid=True, reference_valid=True, aligned=True, inside_furnace=True)
    tracker.record_event("A", "entry", recorded_at=20, observed_at=19, **flags)
    tracker.record_event("A", "partial", scope="entry", recorded_at=30)
    for capture in (None, 25):
        state = tracker.record_event("A", "entry", recorded_at=40, observed_at=capture, **flags)
        assert state["location"]["inside_furnace"] is None
        assert state["location"]["holder"] == "stage_insert"
    state = tracker.record_event("A", "entry", recorded_at=42, observed_at=41, **flags)
    assert state["location"]["inside_furnace"] is True


def test_completion_requires_current_operation_and_capture_orders_evidence():
    tracker = SampleTracker.memory()
    with pytest.raises(ValueError, match="pending operation"):
        tracker.record_event("A", "completed", location={"holder": "robot"})
    tracker.record_event("A", "accepted", operation_id="pick", recorded_at=10)
    with pytest.raises(ValueError, match="pending operation"):
        tracker.record_event("A", "completed", operation_id="other")
    tracker.record_event(
        "A",
        "completed",
        operation_id="pick",
        recorded_at=40,
        location={"holder": "robot"},
        observation={"chip_id": "A", "observed_at": 20, "identity_source": "exact_id"},
    )
    state = tracker.record_event(
        "A",
        "observation",
        recorded_at=41,
        location={"holder": "stage_insert"},
        observation={"chip_id": "A", "observed_at": 30, "identity_source": "exact_id"},
    )
    assert state["location"]["holder"] == "stage_insert"
    with pytest.raises(ValueError, match="pending operation"):
        tracker.record_event("A", "completed", operation_id="pick")
