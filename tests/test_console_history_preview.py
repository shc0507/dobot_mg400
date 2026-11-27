"""Preview event history accepts legacy and process-identified Console rows."""

from orchestrator.web.history_preview import PreviewHistory


def test_identified_events_survive_restarts_and_deduplicate_replays():
    history = PreviewHistory()
    fields = {"result": {"status": "ok"}}
    history.add_device_events(
        [
            ("stage", 1, 1.0, "info", "First process", fields, "stage-a"),
            ("stage", 1, 2.0, "info", "Same event", {}, "stage-a"),
            ("stage", 1, 3.0, "info", "New process", {}, "stage-b"),
            ("vision", 1, 4.0, "info", "Other service", {}, "stage-a"),
        ]
    )
    fields["result"]["status"] = "changed"
    rows = history.device_events()
    assert [row["message"] for row in rows] == ["Other service", "New process", "First process"]
    first = rows[-1]
    assert first["instance"] == "stage-a" and first["ts"] == 1.0
    assert first["fields"] == {"result": {"status": "ok"}}
    first["fields"]["result"]["status"] = "changed"
    assert history.device_events()[-1]["fields"] == {"result": {"status": "ok"}}


def test_legacy_event_deduplication_keeps_six_field_rows_compatible():
    history = PreviewHistory()
    history.add_device_events(
        [
            ("stage", 1, 1.0, "info", "Legacy", {}),
            ("stage", 1, 2.0, "info", "Legacy replay", {}, None),
            ("stage", 1, 3.0, "info", "Identified", {}, "stage-a"),
        ]
    )
    rows = history.device_events()
    assert [row["message"] for row in rows] == ["Identified", "Legacy"]
    assert [row["instance"] for row in rows] == ["stage-a", None]


def test_cursor_pages_keep_oldest_ids_before_sorting_by_nullable_timestamps():
    history = PreviewHistory()
    history.add_device_events(
        [
            ("stage", index, stamp, "info", f"event {index}", {}, "stage-a")
            for index, stamp in enumerate([100.0, 3.0, None, 9.0, 1.0, 7.0], 1)
        ]
    )
    first = history.device_events(since=1, limit=3)
    assert [row["id"] for row in first] == [4, 2, 3]
    second = history.device_events(since=max(row["id"] for row in first), limit=3)
    assert [row["id"] for row in second] == [6, 5]
    assert {row["id"] for row in first + second} == {2, 3, 4, 5, 6}
    assert history.device_events(since=6, limit=3) == []
    assert [row["id"] for row in history.device_events(limit=2)] == [1, 4]


def test_unknown_event_time_is_excluded_by_date_filters_but_survives_pruning():
    history = PreviewHistory()
    history.add_device_events(
        [
            ("stage", 1, 1.0, "info", "Old", {}),
            ("stage", 2, None, "info", "Unknown time", {}),
            ("stage", 3, 3.0, "info", "Current", {}),
        ]
    )
    assert [row["message"] for row in history.device_events(start=2)] == ["Current"]
    assert [row["message"] for row in history.device_events(end=2)] == ["Old"]
    assert [row["message"] for row in history.device_events(start=3, end=3)] == ["Current"]
    history.prune_devices(3)
    assert [row["message"] for row in history.device_events()] == ["Current", "Unknown time"]
