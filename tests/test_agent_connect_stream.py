"""Read-only stream bounds, replay, subscriptions, and source-time semantics."""

import copy
import json

import pytest
from flask import Flask
from websockets.sync.client import connect

from orchestrator.agent_connect.contract import digest
from orchestrator.agent_connect.protocol import bundle
from orchestrator.agent_connect.routes import options, register_routes
from orchestrator.agent_connect.state import CacheStateProvider
from orchestrator.agent_connect.stream import BroadcastHub, Subscription
from orchestrator.web.control_socket import ControlSocket


class Clock:
    now = 0

    def __call__(self):
        return self.now


class Provider:
    def __init__(self):
        self.reads = self.sample_reads = 0

    def __call__(self):
        self.reads += 1
        return {
            "mode": "MOCK",
            "devices": {"stage": {"reading": self.reads}},
            "operations": {"items": [], "count": 0, "complete": True},
        }

    def samples(self, *, selection, chip_ids, limit, offset):
        self.sample_reads += 1
        ids = list(chip_ids) if selection == "ids" else [f"chip-{i}" for i in range(120)]
        rows = [{"chip_id": chip_id} for chip_id in ids[offset : offset + limit]]
        return {
            "selection": selection,
            "items": rows,
            "count": len(rows),
            "total": len(ids),
            "complete": offset == 0 and len(rows) == len(ids),
            "next_offset": offset + len(rows) if offset + len(rows) < len(ids) else None,
        }


@pytest.fixture
def setup():
    clock, provider = Clock(), Provider()
    hub = BroadcastHub(provider, clock=clock, wall_clock=clock, boot_id="boot-a")
    yield hub, provider, clock
    hub.close()


def test_shared_producer_and_slow_reader_coalesce_without_blocking_a(setup):
    hub, provider, clock = setup
    a, b = hub.subscribe(), hub.subscribe()
    first = a.poll()
    assert b.poll() == first
    for index in range(1, 101):
        clock.now = index / 2
        hub.tick()
        assert a.poll()["seq"] == index + 1
    assert provider.reads == 101
    assert b.poll()["seq"] == 101
    assert not hasattr(b, "queue")
    assert b.poll() is None


@pytest.mark.parametrize("rate", [1, 2, 4])
def test_rate_selects_shared_frames(setup, rate):
    hub, _, clock = setup
    reader = hub.subscribe(Subscription(rate_hz=rate))
    reader.poll()
    count = 0
    for index in range(1, 17):
        clock.now = index / 4
        hub.tick()
        count += reader.poll() is not None
    assert count == rate * 4


def test_snapshot_then_replay_preserves_old_contract_and_binding(setup):
    hub, _, _ = setup
    old = hub.contract_id
    hub.publish("sample.changed", {"chip_id": "one"})
    changed = bundle()
    changed["version"] = "1.1.0"
    current = hub.change_contract(changed)
    reader = hub.subscribe(cursor={"boot_id": hub.boot_id, "event_seq": 0})
    assert reader.poll()["contract_id"] == current
    historical = reader.poll()
    assert historical["contract_id"] == old
    assert historical["kind"] == "sample.changed"
    assert digest(hub.contract(old)) == old
    notice = reader.poll()
    assert notice["contract_id"] == current
    assert notice["kind"] == "contract.changed"
    assert hub.snapshot()["contract_id"] == hub.discovery()["contract_id"] == current
    assert hub.snapshot()["event_seq"] == 2


@pytest.mark.parametrize(
    "cursor,reason",
    [
        ({"boot_id": "old", "event_seq": 0}, "boot_changed"),
        ({"boot_id": "boot-a", "event_seq": 999}, "cursor_ahead"),
    ],
)
def test_resume_reports_restart_and_ahead(setup, cursor, reason):
    hub, _, _ = setup
    reader = hub.subscribe(cursor=cursor)
    assert reader.poll()["type"] == "state"
    assert reader.poll()["reason"] == reason


def test_count_and_byte_bounds_report_gaps():
    hub = BroadcastHub(Provider(), max_events=3, max_event_bytes=700)
    reader = hub.subscribe(cursor={"boot_id": hub.boot_id, "event_seq": 0})
    reader.poll()
    for i in range(20):
        hub.publish("fault", {"number": i, "message": "x" * 50})
    assert len(hub.events) <= 3
    assert hub.event_bytes <= 700
    assert reader.poll()["reason"] == "history_evicted"
    with pytest.raises(ValueError, match="byte limit"):
        hub.publish("fault", {"message": "x" * 701})
    hub.close()


def test_bundle_eviction_removes_undecodable_history():
    hub = BroadcastHub(Provider(), max_contracts=2)
    original = hub.contract_id
    hub.publish("old", {})
    for index in range(3):
        value = bundle()
        value["version"] = f"1.{index + 1}.0"
        hub.change_contract(value)
    assert len(hub.contracts) == 2
    assert hub.contract(original) is None
    assert all(event["contract_id"] in hub.contracts for event, _ in hub.events)
    assert hub.gap_reason(hub.boot_id, 0) == "history_evicted"
    hub.close()


def test_subscriber_limit_and_restart(setup):
    hub, _, _ = setup
    hub.max_subscribers = 1
    reader = hub.subscribe()
    reader.poll()
    with pytest.raises(ValueError, match="slot"):
        hub.subscribe()
    old = hub.boot_id
    hub.restart()
    assert hub.boot_id != old
    assert reader.poll()["reason"] == "boot_changed"
    reader.close()
    hub.subscribe().close()


def test_selection_pages_are_explicit_and_cached_once(setup):
    hub, provider, _ = setup
    page = hub.samples(selection="all", limit=50)
    assert (page["count"], page["total"], page["complete"], page["next_offset"]) == (
        50,
        120,
        False,
        50,
    )
    hub.samples(selection="all", limit=50)
    assert provider.sample_reads == 1
    selected = Subscription(groups=("samples",), selection="ids", chip_ids=("a,b", "two"))
    snapshot = hub.snapshot(selected)
    assert "devices" not in snapshot
    assert [item["chip_id"] for item in snapshot["samples"]["items"]] == ["a,b", "two"]
    for index in range(70):
        hub.samples(selection="all", offset=index)
    assert len(hub.sample_cache) <= hub.max_subscribers


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rate_hz": 3},
        {"rate_hz": True},
        {"groups": ("camera",)},
        {"selection": "all"},
        {"selection": "ids"},
        {"chip_ids": ("one",)},
        {"selection": "ids", "chip_ids": tuple(map(str, range(51)))},
    ],
)
def test_invalid_subscriptions_are_rejected(kwargs):
    with pytest.raises(ValueError):
        Subscription(**kwargs)


def test_cache_provider_preserves_sensor_age_and_unknown_capture_time():
    class Cache:
        entries = {"stage": None, "robot": None}

        def get(self, name):
            return {
                "phase": "ready",
                "age_s": 2,
                "updated_at": 100,
                "state": {
                    "ts_unix": 99,
                    "instance": "source",
                    "feedback_fresh": False,
                    "motors": [
                        {
                            "id": "insert",
                            "position_rotations": 2.3,
                            "reference_valid": False,
                            "telemetry_fresh": False,
                            "telemetry_age_ms": 500,
                            "encoder_age_ms": None,
                        }
                    ],
                },
            }

    value = CacheStateProvider(Cache())()
    row = value["devices"]["stage"]
    assert row["sampled_at"] is None
    assert row["state_at"] == 99
    assert row["observed_at"] == 100
    axis = row["values"]["axes"][0]
    assert axis["quality"] == "uncalibrated"
    assert axis["telemetry_age_s"] == 2.5
    assert axis["encoder_age_s"] is None
    assert value["devices"]["robot"]["quality"] == "stale"


def test_http_routes_and_exact_id_query(setup):
    hub, _, _ = setup
    app = Flask(__name__)
    register_routes(app, hub)
    client = app.test_client()
    discovery = client.get("/lab/api/agent/discovery").json
    assert discovery["mode"] == "MOCK"
    contract = client.get(discovery["contract_url"])
    assert digest(contract.json) == discovery["contract_id"]
    assert "immutable" in contract.headers["Cache-Control"]
    state = client.get(
        "/lab/api/agent/v1/state?groups=samples&selection=ids&chip_id=a%2Cb&chip_id=c"
    )
    assert [item["chip_id"] for item in state.json["samples"]["items"]] == ["a,b", "c"]
    assert client.get("/lab/api/agent/v1/state?rate_hz=0").status_code == 400
    assert client.get("/lab/api/agent/v1/samples?selection=all&limit=1").json["complete"] is False
    assert client.post("/lab/api/agent/v1/commands", json={}).status_code == 404
    assert client.get("/lab/api/agent/contracts/missing.json").status_code == 404
    with pytest.raises(ValueError):
        options({"rate_hz": ["1", "4"]})


def test_real_socket_snapshot_event_and_rejects_commands(setup):
    hub, _, _ = setup
    app = Flask(__name__)
    app.config["MOCK_SOURCE"] = object()
    register_routes(app, hub)
    server = ControlSocket(app, host="127.0.0.1", port=0, base_path="/lab").start()
    try:
        with connect(
            f"ws://127.0.0.1:{server.port}/lab/api/agent/v1/stream?groups=devices",
            origin="http://127.0.0.1:8792",
            proxy=None,
        ) as ws:
            first = json.loads(ws.recv(timeout=2))
            assert first["type"] == "state"
            assert "samples" not in first
            hub.publish("stop", {"confirmed": True})
            event = json.loads(ws.recv(timeout=2))
            assert event["kind"] == "stop"
            ws.send('{"action":"move"}')
            from websockets.exceptions import ConnectionClosedError

            with pytest.raises(ConnectionClosedError):
                ws.recv(timeout=2)
    finally:
        server.close()


def test_contract_copy_is_immutable(setup):
    hub, _, _ = setup
    original = copy.deepcopy(hub.contract(hub.contract_id))
    changed = hub.contract(hub.contract_id)
    changed["version"] = "99"
    assert hub.contract(hub.contract_id) == original


def test_existing_control_socket_still_requires_client_and_dispatches_post(setup):
    from flask import jsonify, request
    from websockets.exceptions import InvalidStatus

    hub, _, _ = setup
    app = Flask(__name__)
    register_routes(app, hub)

    @app.post("/lab/api/console/stop/stage")
    def stop():
        return jsonify(client=request.headers["X-MG400-Client"], stop="confirmed")

    server = ControlSocket(app, host="127.0.0.1", port=0, base_path="/lab").start()
    url = f"ws://127.0.0.1:{server.port}/lab/api/console/socket"
    try:
        with pytest.raises(InvalidStatus) as error:
            connect(url, proxy=None)
        assert error.value.response.status_code == 400
        with connect(url + "?client=existing-page", proxy=None) as ws:
            ws.send(json.dumps({"seq": 1, "path": "/api/console/stop/stage", "body": {}}))
            reply = json.loads(ws.recv(timeout=2))
            assert reply["status"] == 200
            assert reply["body"] == {"client": "existing-page", "stop": "confirmed"}
        with pytest.raises(InvalidStatus) as error:
            connect(
                f"ws://127.0.0.1:{server.port}/lab/api/agent/v1/stream",
                origin="http://127.0.0.1:8792",
                proxy=None,
            )
        assert error.value.response.status_code == 403
    finally:
        server.close()


def test_revision_bound_sample_pages_reject_changes(setup):
    hub, provider, _ = setup
    original = provider.samples

    def checked(**kwargs):
        expected = kwargs.pop("expected_revision", None)
        if expected is not None and expected != 5:
            raise ValueError("Sample revision changed. Restart pagination.")
        return {**original(**kwargs), "revision": 5}

    provider.samples = checked
    assert hub.samples(selection="all", expected_revision=5)["revision"] == 5
    with pytest.raises(ValueError, match="revision changed"):
        hub.samples(selection="all", expected_revision=4)


def test_production_schema_validates_cache_frames_and_sample_rows():
    from orchestrator.agent_connect.state import CacheStateProvider

    validator = pytest.importorskip("jsonschema").Draft202012Validator

    class Cache:
        entries = {"stage": None}

        def get(self, name):
            return {
                "phase": "ready",
                "age_s": 0.2,
                "updated_at": 5,
                "state": {
                    "ts_unix": 4.8,
                    "instance": "stage-instance",
                    "motors": [
                        {
                            "id": "insert",
                            "position_rotations": 2,
                            "reference_valid": True,
                            "telemetry_fresh": True,
                            "telemetry_age_ms": 50,
                            "encoder_age_ms": 50,
                        }
                    ],
                },
            }

    hub = BroadcastHub(CacheStateProvider(Cache()))
    contract = hub.contract(hub.contract_id)
    validator.check_schema(contract["schemas"]["state"])
    validator(contract["schemas"]["state"]).validate(hub.snapshot())
    event = hub.publish("fault", {"device": "stage"})
    validator(contract["schemas"]["event"]).validate(event)
    hub.close()


def test_tick_emits_cached_fault_and_operation_changes_once():
    class Devices:
        reads = 0
        row = {
            "instance": "service-a",
            "quality": "valid",
            "state_at": 50,
            "observed_at": 50.2,
            "values": {"fault": None, "last_command": None},
        }

        def __call__(self):
            self.reads += 1
            return {"devices": {"stage": copy.deepcopy(self.row)}}

    provider = Devices()
    hub = BroadcastHub(provider)
    assert hub.event_seq == 0
    provider.row["values"] = {
        "fault": {"code": "limit", "message": "Travel limit reached."},
        "last_command": {"id": "command-1", "result": "failed"},
    }
    hub.tick()
    events = [event for event, _ in hub.events]
    assert [event["kind"] for event in events] == ["device.fault.changed", "operation.result"]
    assert all(event["data"]["instance"] == "service-a" for event in events)
    assert all(event["data"]["device"] == "stage" for event in events)
    assert events[1]["data"]["last_command"]["result"] == "failed"
    assert provider.reads == 2
    hub.tick()
    assert hub.event_seq == 2
    assert provider.reads == 3
    provider.row["values"]["fault"] = None
    hub.tick()
    assert hub.events[-1][0]["kind"] == "device.fault.changed"
    assert hub.events[-1][0]["data"]["fault"] is None
    provider.row["instance"] = "service-b"
    hub.tick()
    assert hub.events[-1][0]["data"]["instance"] == "service-b"
    assert hub.events[-1][0]["data"]["previous_instance"] == "service-a"
    count = hub.event_seq
    provider.row["values"] = {}
    hub.tick()
    assert hub.event_seq == count
    hub.restart()
    assert hub.event_seq == 0
    hub.close()


def test_expire_history_preserves_cursor_and_reports_gap(setup):
    hub, _, _ = setup
    lagging = hub.subscribe(cursor={"boot_id": hub.boot_id, "event_seq": 0})
    lagging.poll()
    hub.publish("fault", {"code": "limit"})
    seq, boot_id = hub.event_seq, hub.boot_id
    hub.expire_history()
    assert hub.event_seq == seq and hub.boot_id == boot_id
    assert not hub.events and hub.event_bytes == 0
    assert lagging.poll()["reason"] == "history_evicted"
    current = hub.subscribe(cursor={"boot_id": boot_id, "event_seq": seq})
    assert current.poll()["type"] == "state"
    assert current.poll() is None
    assert hub.publish("stop", {})["event_seq"] == seq + 1
