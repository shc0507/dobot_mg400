"""Read-only thermal contract and native-coordinate regions, without USB or sockets."""

import json
import subprocess
import sys
import time
from dataclasses import replace

import numpy as np
import pytest
from contract_helpers import assert_error, check_contract
from fastapi.testclient import TestClient

from thermal_cam.app import CONTRACT_PATHS, create_app
from thermal_cam.roi import Region, load_regions
from thermal_cam.service import CameraService, ThermalFrame

TOKEN = "thermal-test-token-length-24"
COORDS = {"x0": 2, "y0": 5, "x1": 5, "y1": 7}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def region_file(tmp_path, content="[regions.crucible]\nx0=2\ny0=5\nx1=5\ny1=7\n"):
    path = tmp_path / "rois.toml"
    path.write_text(content)
    return path


def service_with_frame(tmp_path):
    clock = Clock()
    service = CameraService(demo=True, roi_path=region_file(tmp_path), clock=clock)
    raw = np.arange(120 * 160, dtype=np.uint16).reshape(120, 160) + 19200
    frame = ThermalFrame.from_arrays(np.zeros((120, 160), dtype=np.uint8), raw, 17, "demo")
    service._frame = replace(frame, monotonic_time=clock())
    service._state = "streaming"
    return service, clock


def client_for(service, **kwargs):
    return TestClient(
        create_app(service=service, auto_connect=False, **kwargs), base_url="http://127.0.0.1"
    )


def post(client, route, *, actor="reader", data=None, **kwargs):
    return client.post(
        "/api/" + route,
        headers={"X-MG400-Client": actor},
        json={} if data is None else data,
        **kwargs,
    )


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Demo worker did not reach the expected state")


def test_contract_conformance_and_read_only_metadata(tmp_path):
    service, clock = service_with_frame(tmp_path)

    def expiries():
        return sum(
            "lease expired" in event["message"] for event in service.events.since()["events"]
        )

    with client_for(service) as client:
        check_contract(client, advance=clock.advance, expiry_count=expiries)
        description = client.get("/api/describe").json()
        assert description["commands"] == {}
        assert description["on_lease_expiry"] == "continue_acquisition"
        assert description["regions"] == {"crucible": COORDS}
        assert description["max_frame_age_s"] == 3
        assert_error(post(client, "command", data={"action": "shutter"}), 400)
        state = client.get("/api/state").json()
        assert state["connected"] and not state["busy"] and not state["locked"]
        assert state["last_command"] is None
        assert state["frame_sequence"] == 17


def test_named_and_explicit_regions_use_same_native_coherent_frame(tmp_path):
    service, clock = service_with_frame(tmp_path)
    frame = service._frame
    clock.advance(1.25)
    with client_for(service) as client:
        named = client.get("/api/roi", params={"name": "crucible"}).json()
        explicit = client.get("/api/roi", params=COORDS).json()
        expected = frame.temperatures_c[5:7, 2:5]
        assert named == {**explicit, "name": "crucible"}
        assert named["min_c"] == float(expected.min())
        assert named["mean_c"] == float(expected.mean())
        assert named["max_c"] == float(expected.max())
        assert named["frame_sequence"] == 17
        assert named["timestamp"] == frame.timestamp
        assert named["frame_age_s"] == 1.25
        assert named["source"] == "demo" and named["demo"] is True
        assert all(named[key] == value for key, value in COORDS.items())
        assert client.get("/api/frame").json()["sequence"] == named["frame_sequence"]


@pytest.mark.parametrize(
    "bounds",
    [
        {"x0": 159, "y0": 119, "x1": 160, "y1": 120},
        {"x0": 0, "y0": 0, "x1": 160, "y1": 120},
    ],
)
def test_region_sensor_edges_and_full_frame_are_valid(tmp_path, bounds):
    service, _ = service_with_frame(tmp_path)
    values = service._frame.temperatures_c[bounds["y0"] : bounds["y1"], bounds["x0"] : bounds["x1"]]
    with client_for(service) as client:
        response = client.get("/api/roi", params=bounds)
        assert response.status_code == 200
        assert response.json()["min_c"] == float(values.min())
        assert response.json()["max_c"] == float(values.max())
        assert response.json()["mean_c"] == float(values.mean())


@pytest.mark.parametrize(
    "query",
    [
        {},
        {"name": "missing"},
        {"name": ""},
        {"name": "crucible", **COORDS},
        {"x0": 1},
        {**COORDS, "x0": -1},
        {**COORDS, "x0": 5},
        {**COORDS, "x1": 161},
        {**COORDS, "y1": 121},
        {**COORDS, "y1": 5},
        {**COORDS, "x0": "true"},
        {**COORDS, "x0": "2.0"},
        {**COORDS, "x0": "²"},
        {**COORDS, "x0": "1e0"},
        {**COORDS, "x0": "9" * 5000},
        {**COORDS, "rotation": 90},
        [("name", "crucible"), ("name", "crucible")],
        [*COORDS.items(), ("x0", 3)],
    ],
)
def test_invalid_region_is_rejected_before_frame_access(tmp_path, monkeypatch, query):
    service, _ = service_with_frame(tmp_path)

    def forbidden():
        raise AssertionError("An invalid selector read the frame")

    monkeypatch.setattr(service, "latest", forbidden)
    with client_for(service) as client:
        assert_error(client.get("/api/roi", params=query), 400)


@pytest.mark.parametrize("age,status", [(3, 200), (3.001, 503)])
def test_region_uses_existing_three_second_freshness_boundary(tmp_path, age, status):
    service, clock = service_with_frame(tmp_path)
    clock.advance(age)
    with client_for(service) as client:
        response = client.get("/api/roi", params=COORDS)
        assert response.status_code == status
        if status == 503:
            assert_error(response, 503)
            assert "stale" in response.json()["error"]
            assert client.get("/api/state").json()["fault"]["code"] == "frame_stale"
        else:
            assert response.json()["frame_age_s"] == 3


@pytest.mark.parametrize(
    "state,error",
    [
        ("disconnected", None),
        ("connecting", None),
        ("error", "Camera link lost"),
    ],
)
def test_region_never_returns_retained_frame_after_link_loss(tmp_path, state, error):
    service, _ = service_with_frame(tmp_path)
    service._state, service._error = state, error
    with client_for(service) as client:
        assert_error(client.get("/api/roi", params=COORDS), 503)
        current = client.get("/api/state").json()
        assert not current["connected"]
        if error:
            assert current["fault"] == {"code": "camera_error", "message": error}


def test_region_has_no_reading_during_sensor_settling(tmp_path):
    service, _ = service_with_frame(tmp_path)
    service._frame = None
    with client_for(service) as client:
        assert_error(client.get("/api/roi", params=COORDS), 503)
        assert client.get("/api/state").json()["fault"]["code"] == "frame_unavailable"


def test_missing_region_file_allows_coordinates_but_not_names(tmp_path):
    service, _ = service_with_frame(tmp_path)
    service.regions = load_regions(tmp_path / "absent.toml")
    with client_for(service) as client:
        assert_error(client.get("/api/roi", params={"name": "crucible"}), 400)
        assert client.get("/api/roi", params=COORDS).status_code == 200


@pytest.mark.parametrize(
    "content",
    [
        "[regions",
        "unexpected=1",
        "regions=[]",
        "[regions.bad]\nx0=1\n",
        "[regions.'bad name']\nx0=1\ny0=1\nx1=2\ny1=2\n",
        *[f"[regions.bad]\nx0={x0}\ny0=0\nx1=2\ny1=2\n" for x0 in ("true", "1.5", "-1", "2")],
        "[regions.bad]\nx0=1\ny0=1\nx1=161\ny1=2\n",
        "[regions.bad]\nx0=1\ny0=1\nx1=2\ny1=121\n",
        "[regions.bad]\nx0=1\ny0=1\nx1=2\ny1=2\nrotation=90\n",
    ],
)
def test_invalid_config_fails_before_camera_factory(tmp_path, content):
    def forbidden():
        raise AssertionError("Invalid configuration opened a camera")

    with pytest.raises(ValueError):
        CameraService(demo=True, camera_factory=forbidden, roi_path=region_file(tmp_path, content))


def test_regions_loaded_once_and_immutable(tmp_path):
    path = region_file(tmp_path)
    service = CameraService(demo=True, roi_path=path)
    path.write_text("[regions]\n")
    assert service.describe()["regions"] == {"crucible": COORDS}
    with pytest.raises(ValueError):
        Region(True, 0, 2, 2)


def test_demo_noop_stop_and_lease_expiry_keep_acquiring_without_http(tmp_path):
    clock = Clock()
    service = CameraService(demo=True, roi_path=tmp_path / "absent", clock=clock)
    with TestClient(create_app(service=service), base_url="http://127.0.0.1") as client:
        wait_until(lambda: service.status()["state"] == "streaming")
        assert service.latest().monotonic_time == clock()
        assert service.status()["frame_age_s"] == 0
        assert post(client, "heartbeat").status_code == 200
        before = service.latest().sequence
        response = post(client, "stop", actor="other", data={"wait_s": 15})
        assert response.status_code == 200 and response.json()["stop"] == "confirmed"
        assert response.json()["state"]["lease"]["holder"] == "reader"
        clock.advance(3)
        wait_until(lambda: service.lease.snapshot()["holder"] is None)
        wait_until(lambda: service.latest().sequence > before)
        assert service.latest().monotonic_time == clock()
        assert service.status()["state"] == "streaming"
        result = client.get("/api/roi", params=COORDS)
        assert result.status_code == 200 and result.json()["demo"]
        events = client.get("/api/events").json()
        messages = [event["message"] for event in events["events"]]
        assert sum("lease expired" in message for message in messages) == 1
        assert sum("Camera is streaming" in message for message in messages) == 1
        assert client.get("/api/events", params={"since": events["next"]}).json() == {
            "events": [],
            "next": events["next"],
        }


def test_standard_uvicorn_logging_writes_each_replay_event_to_journal(tmp_path):
    script = """
import json
import logging.config
import sys
from uvicorn.config import LOGGING_CONFIG
from thermal_cam.service import CameraService

logging.config.dictConfig(LOGGING_CONFIG)
now = [1000.0]
service = CameraService(demo=True, roi_path=sys.argv[1], clock=lambda: now[0])
service.lease.acquire("reader")
service.stop("other", 0)
now[0] += 3
service.snapshot()
service.snapshot()
print(json.dumps(service.events.since()))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "absent")],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    replay = json.loads(result.stdout)["events"]
    journal = [json.loads(line[line.index("{") :]) for line in result.stderr.splitlines()]
    assert journal == replay
    assert len(replay) == 2
    assert "stop is a no-op" in replay[0]["message"]
    assert "lease expired" in replay[1]["message"]


@pytest.mark.parametrize("path", sorted(CONTRACT_PATHS))
def test_new_routes_preserve_bearer_host_origin_and_proxy_checks(tmp_path, path):
    service, _ = service_with_frame(tmp_path)
    method = (
        "POST" if path.rsplit("/", 1)[-1] in {"heartbeat", "release", "command", "stop"} else "GET"
    )
    with client_for(service, token=TOKEN) as client:
        assert_error(client.request(method, path), 401)
        assert_error(
            client.request(
                method, path, headers={"Authorization": f"Bearer {TOKEN}", "Origin": "null"}
            ),
            403,
        )
        assert_error(
            client.request(
                method,
                path,
                headers=[
                    ("Authorization", f"Bearer {TOKEN}"),
                    ("Authorization", f"Bearer {TOKEN}"),
                ],
            ),
            401,
        )
    with client_for(service) as client:
        assert_error(client.request(method, path, headers={"Host": "remote.example"}), 403)
        assert_error(client.request(method, path, headers={"Forwarded": ""}), 403)
        assert_error(
            client.request(method, path, headers=[("Host", "127.0.0.1"), ("Host", "127.0.0.1")]),
            400,
        )


@pytest.mark.parametrize("wait_s", [-1, 16, True, "0", None, 10**400, 1e300])
def test_stop_rejects_invalid_wait_without_effects(tmp_path, wait_s):
    service, _ = service_with_frame(tmp_path)
    with client_for(service) as client:
        assert_error(post(client, "stop", data={"wait_s": wait_s}), 400)
        assert service.events.since()["events"] == []


@pytest.mark.parametrize(
    "body", ["null", "[]", "{", '{"wait_s":NaN}', '{"wait_s":Infinity}', "{}" + " " * 4096]
)
def test_contract_invalid_json_and_size_limit(tmp_path, body):
    service, _ = service_with_frame(tmp_path)
    with client_for(service) as client:
        assert_error(
            client.post(
                "/api/stop",
                content=body,
                headers={"Content-Type": "application/json", "X-MG400-Client": "reader"},
            ),
            400,
        )


@pytest.mark.parametrize("actor", ["", "x" * 129, "control\x7f"])
def test_contract_rejects_invalid_clients(tmp_path, actor):
    service, _ = service_with_frame(tmp_path)
    with client_for(service) as client:
        assert_error(post(client, "heartbeat", actor=actor), 400)


def test_contract_rejects_missing_duplicate_clients_and_wrong_media_type(tmp_path):
    service, _ = service_with_frame(tmp_path)
    with client_for(service) as client:
        assert_error(client.post("/api/stop", json={}), 400)
        assert_error(
            client.post(
                "/api/stop", json={}, headers=[("X-MG400-Client", "a"), ("X-MG400-Client", "b")]
            ),
            400,
        )
        assert_error(
            client.post(
                "/api/stop",
                content=json.dumps({}),
                headers={"X-MG400-Client": "a", "Content-Type": "text/plain"},
            ),
            415,
        )


@pytest.mark.parametrize("since", ["-1", "1.0", "²", "", "9" * 5000])
def test_events_reject_invalid_cursors(tmp_path, since):
    service, _ = service_with_frame(tmp_path)
    with client_for(service) as client:
        assert_error(client.get("/api/events", params={"since": since}), 400)
