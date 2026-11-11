"""Offline recording receipts, revoked authority and chip-linked manifest storage."""

import copy
import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from orchestrator.recipe import validate
from orchestrator.runner import Context, Revoked, Runner, _Authority
from orchestrator.skills import default_registry
from orchestrator.skills.recording import MAX_MANIFEST, record
from orchestrator.store import RunStore
from orchestrator.zones import ZonePlan, Zones


class Microscope:
    timeout = 0.02
    identity = "owned-recording-client"

    def __init__(self):
        self.lock = threading.RLock()
        self.calls = []
        self.lifecycle = []
        self.before_command = self.after_command = self.on_read = None
        self.value = {
            "connected": True,
            "busy": False,
            "locked": False,
            "fault": None,
            "lease": {"holder": self.identity, "remaining_s": 3},
            "last_command": None,
            "last_result": None,
            "recording": {"id": None, "status": "idle"},
            "recording_sequence": {"armed": False},
        }

    def describe(self):
        return {"contract": 1, "lease_seconds": 3}

    def acquire(self):
        self.lifecycle.append("acquire")

    def heartbeat(self):
        self.lifecycle.append("renew")

    def release(self):
        self.lifecycle.append("release")

    def close(self):
        pass

    def owned(self, state):
        return state.get("lease", {}).get("holder") == self.identity

    def healthy(self, state):
        return state.get("connected") is True

    def state(self):
        with self.lock:
            if self.on_read:
                self.on_read(self)
            return copy.deepcopy(self.value)

    def command(self, action, **params):
        with self.lock:
            if self.before_command:
                self.before_command(self, action, params)
            self.calls.append((action, params))
            command_id = f"command-{len(self.calls)}"
            if action == "start_recording":
                mode = params["mode"]
                self.value["recording"] = dict(
                    id=params["recording_id"],
                    status="recording",
                    client=self.identity,
                    start_command_id=command_id,
                    stop_command_id=None,
                    run_name=params.get("run_name", "Untitled run"),
                    mode=mode,
                    capture_interval_seconds=30.0 if mode == "time_lapse" else None,
                    output_fps=1.0 if mode == "time_lapse" else 30.0,
                    frame_count=1,
                    started_at=123.0,
                    finished_at=None,
                    segments=["video_0001.mp4"],
                    timestamps="video.timestamps.csv",
                    summary=None,
                    archived=False,
                    error=None,
                )
            elif action == "stop_recording":
                assert params["recording_id"] == self.value["recording"]["id"]
                self.value["recording"].update(
                    status="completed",
                    stop_command_id=command_id,
                    finished_at=124.0,
                    frame_count=8,
                    summary=f"{params['recording_id']}_shortened_3_min.mp4",
                )
            else:
                raise AssertionError("Only recording commands belong in this skill")
            self.value["last_command"] = dict(
                id=command_id, action=action, client=self.identity, result="ok"
            )
            self.value["last_result"] = {"recording": copy.deepcopy(self.value["recording"])}
            if self.after_command:
                self.after_command(self, action)
            return {"command_id": command_id}

    def stop(self):
        with self.lock:
            self.lifecycle.append("stop")
            if self.value["recording"]["status"] == "recording":
                self.value["recording"]["status"] = "stopped"
            return {"stop": "confirmed"}


def prepared(**overrides):
    params = {"chip_id": "chip-001", "seconds": 0.01, **overrides}
    registry = default_registry()
    recipe = validate(
        {
            "name": "offline recording",
            "version": 1,
            "params": {},
            "steps": [
                {
                    "skill": "microscope.record",
                    "params": params,
                    "timeout_s": 3,
                    "on_fail": "abort",
                }
            ],
        },
        registry,
    )
    return registry, recipe


@contextmanager
def harness(tmp_path, *, duration=2):
    service = Microscope()
    registry, recipe = prepared()
    with RunStore(tmp_path / "runs") as store:
        run_id = store.create(recipe)
        authority = _Authority(time.monotonic() + duration)
        plan = ZonePlan({}, ("microscope",), tmp_path / "zones.json", "offline")
        zones = Zones(plan, {"microscope": service}, holder=run_id, audit=lambda *args: None)
        ctx = Context(authority, {"microscope": service}, store, run_id, 0, "chip-001", 1, zones)
        yield SimpleNamespace(
            service=service, store=store, run_id=run_id, authority=authority, ctx=ctx
        )


def manifests(h):
    rows = h.store.chip_history("chip-001", kind="artifacts")["items"]
    return [
        json.loads(h.store.artifact_bytes(row["id"], chip_id="chip-001")["data"]) for row in rows
    ]


@pytest.mark.parametrize("mode,fps,interval", [("time_lapse", 1, 30), ("live", 30, None)])
def test_recording_writes_intent_before_start_and_completed_metadata_only(
    tmp_path, mode, fps, interval
):
    with harness(tmp_path) as h:

        def before(_service, action, _params):
            if action == "start_recording":
                assert [value["status"] for value in manifests(h)] == ["requested"]

        h.service.before_command = before
        result = record(h.ctx, "chip-001", 0.01, "Anneal batch", mode)
        values = manifests(h)
        assert [value["status"] for value in values] == ["requested", "completed"]
        intent, final = values
        assert final["recording_id"] == intent["recording_id"] == result["recording_id"]
        assert len(final["recording_id"]) == 32
        assert final["chip_id"] == "chip-001"
        assert final["frame_count"] == 8
        assert final["output_fps"] == fps
        assert final["capture_interval_seconds"] == interval
        assert final["segments"] == ["video_0001.mp4"]
        assert h.service.calls == [
            (
                "start_recording",
                dict(recording_id=final["recording_id"], mode=mode, run_name="Anneal batch"),
            ),
            ("stop_recording", dict(recording_id=final["recording_id"])),
        ]
        assert h.service.lifecycle == []
        for row in h.store.get(h.run_id)["artifacts"]:
            assert row["chip_id"] == "chip-001" and row["step"] == 0
            blob = h.store.artifact_bytes(row["id"], chip_id="chip-001")["data"]
            assert len(blob) < MAX_MANIFEST and b"/var/lib" not in blob
            with pytest.raises(ValueError, match="Unknown chip artifact"):
                h.store.artifact_bytes(row["id"], chip_id="another-chip")


def test_registered_skill_runs_through_real_runner_and_preserves_chip_association(tmp_path):
    registry, recipe = prepared()
    assert recipe.services == ("microscope",)
    service = Microscope()
    plan = ZonePlan({}, ("microscope",), tmp_path / "zones.json", "offline")
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(recipe, registry, {"microscope": service}, store, zone_plan=plan)
        runner.start()
        assert runner.wait(3) == "completed"
        assert service.lifecycle.count("acquire") == 1
        assert service.lifecycle[-1] == "release"
        assert len(store.chip_history("chip-001", kind="artifacts")["items"]) == 2


@pytest.mark.parametrize(
    "options",
    [
        {"seconds": 0},
        {"seconds": -1},
        {"seconds": float("inf")},
        {"seconds": float("nan")},
        {"seconds": True},
        {"seconds": 10},
        {"mode": "unknown"},
        {"mode": []},
        {"chip_id": ""},
        {"chip_id": "x" * 257},
        {"run_name": "x" * 81},
        {"run_name": "line\nbreak"},
    ],
)
def test_invalid_parameters_never_publish_or_dispatch(tmp_path, options):
    with harness(tmp_path) as h:
        params = {"chip_id": "chip-001", "seconds": 0.01, **options}
        with pytest.raises(ValueError):
            record(h.ctx, **params)
        assert manifests(h) == [] and h.service.calls == []


def test_intent_audit_failure_prevents_the_first_command(tmp_path, monkeypatch):
    with harness(tmp_path) as h:
        monkeypatch.setattr(
            h.store, "artifact", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
        )
        with pytest.raises(OSError, match="disk full"):
            record(h.ctx, "chip-001", 0.01)
        assert h.service.calls == [] and manifests(h) == []


@pytest.mark.parametrize("action", ["start_recording", "stop_recording"])
def test_lost_command_response_is_never_retried_or_marked_completed(tmp_path, action):
    with harness(tmp_path) as h:

        def lose(_service, sent):
            if sent == action:
                raise OSError("response lost")

        h.service.after_command = lose
        with pytest.raises(OSError, match="response lost"):
            record(h.ctx, "chip-001", 0.01)
        assert [name for name, _ in h.service.calls].count(action) == 1
        assert [value["status"] for value in manifests(h)] == ["requested"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state["last_command"].update(id="another-command"),
        lambda state: state["last_command"].update(action="snapshot"),
        lambda state: state["last_command"].update(client="other-client"),
        lambda state: state["last_command"].update(result="failed"),
        lambda state: state["last_result"]["recording"].update(id="f" * 32),
        lambda state: state["last_result"]["recording"].update(stop_command_id="unrelated"),
        lambda state: state["last_result"]["recording"].update(start_command_id="unrelated"),
        lambda state: state["last_result"]["recording"].update(status="finalizing"),
        lambda state: state["recording"].update(id="f" * 32),
        lambda state: state["lease"].update(holder="new-owner"),
    ],
)
def test_mismatched_stop_receipts_never_publish_completion(tmp_path, mutation):
    with harness(tmp_path) as h:
        h.service.after_command = lambda service, action: (
            mutation(service.value) if action == "stop_recording" else None
        )
        with pytest.raises(RuntimeError):
            record(h.ctx, "chip-001", 0.01)
        assert len(h.service.calls) == 2
        assert [value["status"] for value in manifests(h)] == ["requested"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("segments", ["../../video.mp4"]),
        ("segments", ["/tmp/video.mp4"]),
        ("segments", ["video.mp4"] * 257),
        ("segments", ["video.mp4", "video.mp4"]),
        ("timestamps", "other.csv"),
        ("timestamps", None),
        ("timestamps", []),
        ("summary", None),
        ("summary", "unexpected.mp4"),
        ("segments", []),
        ("segments", ["video_0002.mp4"]),
        ("segments", ["notes.mp4"]),
        ("frame_count", 0),
        ("summary", "../summary.mp4"),
        ("frame_count", True),
        ("frame_count", -1),
        ("started_at", float("nan")),
        ("finished_at", 1),
        ("output_fps", 120),
        ("capture_interval_seconds", 1),
        ("archived", "yes"),
        ("run_name", "changed"),
    ],
)
def test_unsafe_or_inconsistent_final_metadata_is_not_stored(tmp_path, field, value):
    with harness(tmp_path) as h:

        def change(service, action):
            if action == "stop_recording":
                service.value["last_result"]["recording"][field] = value

        h.service.after_command = change
        with pytest.raises(ValueError):
            record(h.ctx, "chip-001", 0.01, "expected name")
        assert [value["status"] for value in manifests(h)] == ["requested"]


def test_arbitrary_service_keys_and_paths_are_not_copied_into_manifest(tmp_path):
    with harness(tmp_path) as h:

        def change(service, action):
            service.value["last_result"]["recording"]["private_path"] = "/secret/video.mp4"
            service.value["last_result"]["recording"]["token"] = "private-credential"

        h.service.after_command = change
        record(h.ctx, "chip-001", 0.01)
        assert "private" not in json.dumps(manifests(h))


@pytest.mark.parametrize(
    "boundary", ["before-start", "after-start", "capture", "after-stop", "before-final-store"]
)
def test_revocation_never_dispatches_or_publishes_after_authority_is_lost(
    tmp_path, monkeypatch, boundary
):
    with harness(tmp_path) as h:
        artifact = h.store.artifact
        if boundary == "before-start":

            def revoke_after_intent(*args, **kwargs):
                result = artifact(*args, **kwargs)
                h.authority.revoke()
                return result

            monkeypatch.setattr(h.store, "artifact", revoke_after_intent)
        elif boundary in {"after-start", "after-stop"}:
            target = "start_recording" if boundary == "after-start" else "stop_recording"
            h.service.after_command = lambda _service, action: (
                h.authority.revoke() if action == target else None
            )
        elif boundary == "capture":
            monkeypatch.setattr(
                h.ctx, "sleep", lambda _seconds: (h.authority.revoke(), h.ctx.check())
            )
        else:
            original = h.service.owned

            def revoke_before_final(state):
                result = original(state)
                if (state.get("last_command") or {}).get("action") == "stop_recording":
                    h.authority.revoke()
                return result

            monkeypatch.setattr(h.service, "owned", revoke_before_final)
        with pytest.raises(Revoked):
            record(h.ctx, "chip-001", 0.01)
        expected = (
            0 if boundary == "before-start" else 1 if boundary in {"after-start", "capture"} else 2
        )
        assert len(h.service.calls) == expected
        assert [value["status"] for value in manifests(h)] == ["requested"]


def test_capture_detects_replaced_recording_before_stop_dispatch(tmp_path, monkeypatch):
    with harness(tmp_path) as h:

        def replace(_seconds):
            h.service.value["recording"]["id"] = "f" * 32

        monkeypatch.setattr(h.ctx, "sleep", replace)
        with pytest.raises(RuntimeError, match="expected owned recording"):
            record(h.ctx, "chip-001", 0.01)
        assert [name for name, _ in h.service.calls] == ["start_recording"]
        assert [value["status"] for value in manifests(h)] == ["requested"]


def test_completion_audit_failure_keeps_only_intent_and_does_not_retry(tmp_path, monkeypatch):
    with harness(tmp_path) as h:
        original = h.store.artifact

        def fail_final(run_id, step, name, data, **kwargs):
            if name.endswith("-completed.json"):
                raise OSError("full disk")
            return original(run_id, step, name, data, **kwargs)

        monkeypatch.setattr(h.store, "artifact", fail_final)
        with pytest.raises(OSError, match="full disk"):
            record(h.ctx, "chip-001", 0.01)
        assert len(h.service.calls) == 2
        assert [value["status"] for value in manifests(h)] == ["requested"]


@pytest.mark.parametrize(
    "change",
    [
        lambda state: state.pop("fault"),
        lambda state: state.pop("connected"),
        lambda state: state.pop("locked"),
        lambda state: state["lease"].update(holder="foreign"),
        lambda state: state.update(busy=True),
        lambda state: state.update(recording_sequence={"armed": True}),
        lambda state: state.update(recording_sequence={}),
        lambda state: state.pop("recording_sequence"),
        lambda state: state["recording"].update(status="finalizing"),
    ],
)
def test_unknown_health_or_armed_recording_cannot_publish_or_start(tmp_path, change):
    with harness(tmp_path) as h:
        change(h.service.value)
        with pytest.raises(RuntimeError):
            record(h.ctx, "chip-001", 0.01)
        assert h.service.calls == [] and manifests(h) == []


def test_pending_command_waits_for_its_exact_receipt_without_redispatch(tmp_path):
    with harness(tmp_path) as h:
        saved = {}

        def delay(service, _action):
            saved.update(state=copy.deepcopy(service.value), reads=0)
            service.value.update(busy=True, last_command=None, last_result=None)

        def publish(service):
            if not saved:
                return
            saved["reads"] += 1
            if saved["reads"] >= 2:
                service.value = saved.pop("state")
                saved.clear()

        h.service.after_command = delay
        h.service.on_read = publish
        record(h.ctx, "chip-001", 0.01)
        assert [action for action, _ in h.service.calls] == ["start_recording", "stop_recording"]
        assert [value["status"] for value in manifests(h)] == ["requested", "completed"]


def test_expired_pending_receipt_keeps_intent_without_stop_or_retry(tmp_path):
    with harness(tmp_path, duration=0.08) as h:

        def never_publish(service, _action):
            service.value.update(busy=True, last_command=None, last_result=None)

        h.service.after_command = never_publish
        with pytest.raises(Revoked):
            record(h.ctx, "chip-001", 0.01)
        assert [action for action, _ in h.service.calls] == ["start_recording"]
        assert [value["status"] for value in manifests(h)] == ["requested"]


def test_runner_abort_drains_and_stops_owned_recording_without_completed_manifest(tmp_path):
    registry, recipe = prepared(seconds=1)
    service, started = Microscope(), threading.Event()
    service.after_command = lambda _service, action: (
        started.set() if action == "start_recording" else None
    )
    plan = ZonePlan({}, ("microscope",), tmp_path / "zones.json", "offline")
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(recipe, registry, {"microscope": service}, store, zone_plan=plan)
        runner.start()
        try:
            assert started.wait(2)
            runner.abort()
            assert runner.wait(3) == "aborted"
        finally:
            runner.abort()
            runner.wait(3)
        assert [action for action, _ in service.calls] == ["start_recording"]
        assert service.value["recording"]["status"] == "stopped"
        assert service.lifecycle.count("acquire") == 1
        assert "stop" in service.lifecycle
        assert service.lifecycle[-1] == "release"
        values = store.chip_history("chip-001", kind="artifacts")["items"]
        assert len(values) == 1
        value = json.loads(store.artifact_bytes(values[0]["id"], chip_id="chip-001")["data"])
        assert value["status"] == "requested"
