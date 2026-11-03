"""Offline panel routes, owned-camera lifecycle, and real demo lease transitions."""

import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from test_microscope_service import demo_rig as _demo_rig
from test_microscope_service import service_post, wait_command

from labkit.errors import ServiceError
from labkit.events import EventLog
from labkit.lease import Lease
from microscope.app import create_app
from microscope.service import MicroscopeService, PreviewFrame

demo_rig = _demo_rig


class Camera:
    def __init__(self, packet=None):
        self.packet = packet
        self.is_open = True
        self.calls = []
        self.entered = threading.Event()
        self.release = None
        self.closed = False

    def get_frame_packet(self, **kwargs):
        self.calls.append(kwargs)
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(3)
        if isinstance(self.packet, BaseException):
            raise self.packet
        return self.packet

    def close(self):
        self.closed = True
        self.is_open = False


def frame_service(packet=None):
    main, side = Camera(packet), Camera(packet)
    service = MicroscopeService(demo=True, camera=main, side_camera=side)
    # Public preview's camera ownership is tested without any startup transport.
    service._started = True
    return service, main, side


def launch(call):
    outcomes = []

    def run():
        try:
            outcomes.append(call())
        except BaseException as error:
            outcomes.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcomes


def finish(thread):
    thread.join(3)
    assert not thread.is_alive()


@pytest.mark.parametrize("name,timestamp", [("microscope", 1730000000.5), ("side", 5.25)])
def test_owned_preview_preserves_ids_and_each_camera_timestamp_domain(name, timestamp):
    pixels = np.full((20, 30, 3), 127, dtype=np.uint8)
    service, main, side = frame_service((7, timestamp, pixels))
    result = service.read_frame(name, after_id=6, timeout=0.25)
    assert result.frame_id == 7 and result.captured_at == timestamp
    image = cv2.imdecode(np.frombuffer(result.jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert image.shape == pixels.shape
    selected, other = (main, side) if name == "microscope" else (side, main)
    assert selected.calls == [dict(after_id=6, wait=True, timeout=0.25, copy=True)]
    assert other.calls == []
    assert service.read_frame(name, after_id=7, timeout=0) is None
    assert selected.calls[-1]["wait"] is False
    assert service._frame_reads == 0 and service.lease.snapshot()["holder"] is None
    service.close()


@pytest.mark.parametrize(
    "args",
    [
        {"camera": "other"},
        {"camera": []},
        {"after_id": -1},
        {"after_id": True},
        {"after_id": 1.0},
        {"after_id": 2**63},
        {"timeout": -1},
        {"timeout": 0.6},
        {"timeout": float("nan")},
        {"timeout": True},
        {"timeout": "0"},
    ],
)
def test_invalid_preview_requests_never_reach_camera(args):
    service, main, side = frame_service()
    with pytest.raises(ValueError):
        service.read_frame(**{"camera": "microscope", **args})
    assert not main.calls and not side.calls and service._frame_reads == 0


@pytest.mark.parametrize(
    "packet",
    [
        (True, 1.0, np.zeros((2, 2), np.uint8)),
        (1, float("inf"), np.zeros((2, 2), np.uint8)),
        (1, 1.0, np.zeros((2, 2), np.float32)),
        (1, 1.0, np.zeros((2, 2, 2), np.uint8)),
        (1, 1.0, np.zeros((0, 2), np.uint8)),
        (1, 1.0, np.broadcast_to(np.uint8(0), (4001, 4000))),
        (1, 1.0, None),
        RuntimeError("fake read error"),
    ],
)
def test_invalid_camera_packets_release_read_admission(packet):
    service, _, _ = frame_service(packet)
    with pytest.raises(ServiceError):
        service.read_frame("microscope")
    assert service._frame_reads == 0
    assert service._fault is None


def test_empty_or_oversized_encoded_frame_is_unavailable(monkeypatch):
    service, main, _ = frame_service()
    assert service.read_frame("microscope") is None
    main.packet = (1, 2.0, np.zeros((2, 2), np.uint8))
    for ok, encoded in [(False, None), (True, SimpleNamespace(nbytes=16_777_217))]:
        monkeypatch.setattr("microscope.service.cv2.imencode", lambda *_a, **_k: (ok, encoded))
        with pytest.raises(ServiceError):
            service.read_frame("microscope")
        assert service._frame_reads == 0
    main.is_open = False
    with pytest.raises(ServiceError, match="unavailable"):
        service.read_frame("microscope")


def test_close_drains_accepted_reads_before_closing_owners_and_discards_late_packet(monkeypatch):
    service, main, side = frame_service((1, 2.0, np.zeros((2, 2), np.uint8)))
    main.release = threading.Event()
    reader, outcomes = launch(lambda: service.read_frame("microscope"))
    assert main.entered.wait(1)
    stopping = threading.Event()
    monkeypatch.setattr(service, "stop", lambda *_: stopping.set() or "confirmed")
    closer, closed = launch(service.close)
    assert stopping.wait(1)
    assert not main.closed and not side.closed
    with pytest.raises(ServiceError, match="closed"):
        service.read_frame("side")
    main.release.set()
    finish(reader)
    finish(closer)
    assert len(outcomes) == 1 and isinstance(outcomes[0], ServiceError)
    assert closed == [None] and main.closed and side.closed
    assert service._frame_reads == 0


def test_drain_timeout_retains_owners_and_retry_can_finish(monkeypatch):
    service, main, side = frame_service()
    main.release = threading.Event()
    reader, outcomes = launch(lambda: service.read_frame("microscope"))
    assert main.entered.wait(1)
    with monkeypatch.context() as patch:
        patch.setattr(service._frame_condition, "wait_for", lambda *_a, **_k: False)
        with pytest.raises(RuntimeError, match="reads are still stopping"):
            service.close()
    assert not main.closed and not side.closed and not service._shutdown_complete
    with pytest.raises(ServiceError, match="closed"):
        service.read_frame("side")
    main.release.set()
    finish(reader)
    assert isinstance(outcomes[0], ServiceError)
    service.close()
    assert main.closed and side.closed and service._shutdown_complete


def test_frame_admission_caps_simultaneous_reads_without_touching_lease():
    service, main, _ = frame_service()
    main.release = threading.Event()
    entered = threading.Barrier(5)
    original = main.get_frame_packet

    def blocked(**kwargs):
        entered.wait(timeout=2)
        return original(**kwargs)

    main.get_frame_packet = blocked
    readers = [launch(lambda: service.read_frame("microscope")) for _ in range(4)]
    entered.wait(timeout=2)
    with pytest.raises(ServiceError, match="busy"):
        service.read_frame("side")
    assert service.lease.snapshot()["holder"] is None
    main.release.set()
    for thread, result in readers:
        finish(thread)
        assert result == [None]
    assert service._frame_reads == 0
    service.close()


def test_panel_metadata_uses_calibrated_ids_once_and_returns_independent_values(monkeypatch):
    service, _, _ = frame_service()
    calls = []
    monkeypatch.setattr(
        service,
        "_locator",
        lambda: calls.append(1) or SimpleNamespace(references={1: 0, 3: 0, 999: 0}),
    )
    metadata = service.panel_info()
    assert metadata == dict(
        objective_ids=[1, 3], calibration_available=True, calibration_error=None, home_switch=False
    )
    metadata["objective_ids"].clear()
    assert service.panel_info()["objective_ids"] == [1, 3] and calls == [1]
    assert service.lease.snapshot()["holder"] is None
    service.close()
    with pytest.raises(ServiceError, match="closed"):
        service.panel_info()


def test_missing_and_invalid_real_calibration_never_falls_back_to_objective_ids(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("MG400_MICROSCOPE_CALIBRATION_DIR", str(tmp_path / "calibration"))
    service, _, _ = frame_service()
    service.demo = False
    monkeypatch.setattr(
        service, "_locator", lambda: pytest.fail("Must reject missing references before decode")
    )
    metadata = service.panel_info()
    assert not metadata["calibration_available"] and metadata["objective_ids"] == []
    assert metadata["calibration_error"]
    assert not (tmp_path / "calibration").exists()
    service.close()


@pytest.mark.parametrize("bad_reference", [False, True])
def test_real_metadata_uses_supplied_marker_references_or_reports_invalid_data(
    monkeypatch, tmp_path, bad_reference
):
    from microscope.aruco_locator import REFERENCES

    root = tmp_path / "calibration"
    images = root / "side_camera_images"
    images.mkdir(parents=True)
    monkeypatch.setenv("MG400_MICROSCOPE_CALIBRATION_DIR", str(root))
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    for ident, filename in REFERENCES.items():
        pixels = np.full((200, 200), 255, dtype=np.uint8)
        pixels[60:140, 60:140] = cv2.aruco.generateImageMarker(dictionary, ident, 80)
        assert cv2.imwrite(str(images / filename), pixels)
    if bad_reference:
        (images / REFERENCES[0]).write_bytes(b"invalid PNG reference")
    service, _, _ = frame_service()
    service.demo = False
    metadata = service.panel_info()
    assert metadata["calibration_available"] is not bad_reference
    assert metadata["objective_ids"] == ([] if bad_reference else [0, 1, 2, 3])
    assert bool(metadata["calibration_error"]) is bad_reference
    service.close()


def test_frame_and_metadata_reads_do_not_clear_fault_or_renew_existing_lease(monkeypatch):
    service, main, _ = frame_service((1, 2.0, np.zeros((2, 2), np.uint8)))
    service._fault = {"code": "existing", "message": "unknown prior command"}
    service._lease.acquire("other-client")
    expiry = service._lease.snapshot()["remaining_s"]
    monkeypatch.setattr(service, "_locator", lambda: SimpleNamespace(references={1: {}}))
    assert service.read_frame("microscope").frame_id == 1
    assert service.panel_info()["objective_ids"] == [1]
    assert service._fault == {"code": "existing", "message": "unknown prior command"}
    assert service._lease.snapshot()["remaining_s"] <= expiry
    assert service._lease.snapshot()["holder"] == "other-client"
    assert len(main.calls) == 1
    service.close()


def test_metadata_loading_does_not_hold_stop_lock_or_publish_after_close(monkeypatch):
    service, _, _ = frame_service()
    entered, release = threading.Event(), threading.Event()

    def locator():
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(references={1: 0})

    monkeypatch.setattr(service, "_locator", locator)
    loader, results = launch(service.panel_info)
    assert entered.wait(1)
    with pytest.raises(ServiceError, match="loading"):
        service.panel_info()
    assert service.stop("operator", 0) == "confirmed"
    service.close()
    release.set()
    finish(loader)
    assert isinstance(results[0], ServiceError) and service._panel_info_cache is None


class PanelService:
    """Read-only HTTP fake; motion policy is exercised separately with the demo."""

    def __init__(self):
        self.lease = Lease(on_expire=lambda: None)
        self.events = EventLog()
        self.calls = []
        self.frame = PreviewFrame(7, 10.5, b"fake-jpeg")

    def describe(self):
        return {"commands": {}, "service": "microscope"}

    def snapshot(self):
        return {"busy": False, "locked": False}

    def panel_info(self):
        return {
            "objective_ids": [1, 3],
            "calibration_available": True,
            "calibration_error": None,
            "home_switch": False,
        }

    def read_frame(self, camera, **kwargs):
        self.calls.append((camera, kwargs))
        if isinstance(self.frame, BaseException):
            raise self.frame
        return self.frame

    def stop(self, client, wait_s=0):
        self.calls.append(("stop", client, wait_s))
        return "confirmed"


def test_public_shell_and_fixed_assets_contain_no_service_data_or_token():
    service = PanelService()
    app = create_app(service, token="secret-token-for-tests-only", base_path="/scope")
    client = app.test_client()
    for path in ("/scope/", "/scope/static/app.js", "/scope/static/styles.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert b"secret-token-for-tests-only" not in response.data
        assert response.headers["Cache-Control"] == "no-store"
        assert "object-src 'none'" in response.headers["Content-Security-Policy"]
    assert service.calls == []
    assert b"/scope/static/app.js" in client.get("/scope/").data
    assert client.get("/scope/static/../service.py").status_code == 401
    assert client.get("/scope/api/panel-info").status_code == 401
    assert (
        client.get("/scope/api/camera/side/mjpeg?token=secret-token-for-tests-only").status_code
        == 401
    )


@pytest.mark.parametrize(
    "path",
    ["/", "/static/app.js", "/static/styles.css", "/api/panel-info", "/api/camera/side/frame.jpg"],
)
@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "outside.invalid"},
        {"Forwarded": ""},
        {"Origin": "https://outside.invalid"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_shell_and_data_retain_host_origin_checks_without_token(path, headers):
    service = PanelService()
    assert create_app(service).test_client().get(path, headers=headers).status_code == 403
    assert service.calls == []


@pytest.mark.parametrize(
    "route", ["panel-info", "state", "events", "camera/side/frame.jpg", "camera/side/mjpeg"]
)
def test_data_routes_require_header_token_and_allow_authorized_head(route):
    service = PanelService()
    client = create_app(service, token="t" * 24).test_client()
    assert client.get("/api/" + route).status_code == 401
    assert client.get("/api/" + route, headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = client.head("/api/" + route, headers={"Authorization": "Bearer " + "t" * 24})
    assert response.status_code == 200
    assert not response.data and service.calls == []


@pytest.mark.parametrize(
    "suffix",
    [
        "other/frame.jpg",
        "side/frame.jpg?after_id=-1",
        "side/frame.jpg?after_id=1.5",
        "side/frame.jpg?after_id=9223372036854775808",
        "side/frame.jpg?after_id=1&after_id=2",
        "side/mjpeg?token=nope",
    ],
)
def test_camera_route_rejects_unknown_names_and_unbounded_or_ambiguous_cursors(suffix):
    service = PanelService()
    response = create_app(service).test_client().get("/api/camera/" + suffix)
    assert response.status_code == 400 and service.calls == []


def test_single_frame_cursor_and_multipart_headers():
    service = PanelService()
    app = create_app(service)
    client = app.test_client()
    response = client.get("/api/camera/microscope/frame.jpg?after_id=6")
    assert response.data == b"fake-jpeg" and response.mimetype == "image/jpeg"
    assert response.headers["X-Frame-Id"] == "7" and response.headers["X-Captured-At"] == "10.5"
    assert service.calls[-1] == ("microscope", {"after_id": 6, "timeout": 0.5})
    response = client.get("/api/camera/side/mjpeg", buffered=False)
    part = next(response.response)
    assert (
        part == b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 9\r\n"
        b"X-Frame-Id: 7\r\nX-Captured-At: 10.5\r\n\r\nfake-jpeg\r\n"
    )
    service.frame = ServiceError(503, "camera closed")
    assert list(response.response) == []
    response.close()
    service.frame = None
    assert client.get("/api/camera/side/frame.jpg").status_code == 204


def unopened_stream(app):
    # Flask test_client starts the iterable. Direct dispatch tests the earlier
    # close window where a WSGI server has not entered the generator at all.
    with app.test_request_context("/api/camera/side/mjpeg"):
        return app.full_dispatch_request()


def test_four_unstarted_streams_bound_admission_and_close_releases_exactly_once():
    service = PanelService()
    app = create_app(service)
    streams = [unopened_stream(app) for _ in range(4)]
    assert all(response.status_code == 200 for response in streams)
    assert unopened_stream(app).status_code == 503
    control = app.test_client().post(
        "/api/stop", json={"wait_s": 0}, headers={"X-MG400-Client": "operator"}
    )
    assert control.status_code == 200 and control.json["stop"] == "confirmed"
    streams[0].close()
    streams[0].close()
    replacement = unopened_stream(app)
    assert replacement.status_code == 200 and unopened_stream(app).status_code == 503
    for response in [*streams[1:], replacement]:
        response.close()
    another = [unopened_stream(app) for _ in range(4)]
    assert all(response.status_code == 200 for response in another)
    assert unopened_stream(app).status_code == 503
    for response in another:
        response.close()


@pytest.mark.parametrize("failure", [None, ServiceError(503, "fake unavailable")])
def test_first_frame_failure_returns_stream_slot(failure):
    service = PanelService()
    app = create_app(service)
    service.frame = failure
    for _ in range(5):
        assert unopened_stream(app).status_code == 503
    service.frame = PreviewFrame(7, 1.0, b"jpeg")
    streams = [unopened_stream(app) for _ in range(4)]
    assert all(response.status_code == 200 for response in streams)
    for response in streams:
        response.close()


def test_stream_disconnect_releases_slot_after_generator_started():
    app = create_app(PanelService())
    streams = [unopened_stream(app) for _ in range(4)]
    assert next(iter(streams[0].response)).startswith(b"--frame")
    streams[0].close()
    replacement = unopened_stream(app)
    assert replacement.status_code == 200
    for response in [*streams[1:], replacement]:
        response.close()


def test_actual_demo_panel_zero_handoff_and_ordinary_controls(demo_rig):
    rig = demo_rig()
    assert service_post(rig, "heartbeat").status_code == 200
    assert service_post(rig, "manual/heartbeat").status_code == 409
    assert service_post(rig, "release").status_code == 200
    assert rig.service._stop_done.wait(1)
    assert service_post(rig, "manual/heartbeat").status_code == 200
    assert service_post(rig, "manual/set_focus_zero", {"confirm": False}).status_code == 400
    zero = service_post(rig, "manual/set_focus_zero", {"confirm": True})
    assert zero.status_code == 202
    assert service_post(rig, "manual/renew").status_code == 200
    state = wait_command(rig, zero.json["command_id"])
    assert state["last_command"]["result"] == "ok" and state["focus_zero_known"]
    assert service_post(rig, "manual/release").status_code == 200
    assert service_post(rig, "heartbeat").status_code == 200
    assert service_post(rig, "renew").status_code == 200
    assert rig.service.snapshot()["manual_client"] is None
    for action, args in [
        ("focus_move", {"steps": 10}),
        ("autofocus", {"mode": "local"}),
        ("snapshot", {}),
    ]:
        response = service_post(rig, "command", {"action": action, **args})
        assert response.status_code == 202, response.json
        state = wait_command(rig, response.json["command_id"])
        assert state["last_command"]["result"] == "ok", state
    assert rig.client.get("/api/panel-info").json["objective_ids"] == [0, 1, 2, 3]
    for camera in ("microscope", "side"):
        assert rig.client.get(f"/api/camera/{camera}/frame.jpg").status_code == 200
    stop = service_post(rig, "stop", {"wait_s": 1}, actor="other-operator")
    assert stop.status_code == 200 and stop.json["stop"] == "confirmed"
    assert not rig.service.snapshot()["focus_zero_known"]


def test_panel_cannot_claim_manual_control_from_automation(demo_rig):
    rig = demo_rig()
    assert service_post(rig, "heartbeat", actor="automation").status_code == 200
    for actor in ("operator", "automation"):
        assert service_post(rig, "manual/heartbeat", actor=actor).status_code == 409
        assert (
            service_post(rig, "manual/set_focus_zero", {"confirm": True}, actor=actor).status_code
            == 409
        )
    assert rig.service.snapshot()["manual_client"] is None
    assert not any(command == "FOCUS ZERO" for command in rig.port.commands)


# Recording uses fake capture/metadata/finalization; no SDK, socket or encoder.
class RecordingReadings:
    def __init__(self):
        self.value = dict(
            available=True,
            error=None,
            metadata={},
            sequence=dict(running=False, started_at=None, phase="idle", healthy=True),
        )
        self.started = self.closed = False

    def snapshot(self):
        import copy

        return copy.deepcopy(self.value)

    def start(self):
        self.started = True

    def close(self):
        self.closed = True


class RecordingCamera(Camera):
    def __init__(self):
        super().__init__((1, 1.0, np.zeros((20, 30, 3), np.uint8)))
        self.is_recording = False
        self.recording_error = None
        self.recording_segments = []
        self.stabilization_enabled = False
        self.start_gate = self.stop_gate = None
        self.start_calls = self.stop_calls = self.stabilization_calls = 0
        self.metadata = []

    def open(self):
        self.is_open = True

    def set_stabilization(self, enabled, *, cancel=None):
        if cancel is not None and cancel.is_set():
            raise RuntimeError("Camera admission cancelled")
        self.stabilization_calls += 1
        self.stabilization_enabled = enabled

    def start_recording(self, path, **options):
        from furnace.recordings import private_open

        if options.get("cancel") is not None and options["cancel"].is_set():
            raise RuntimeError("Camera admission cancelled")
        self.start_calls += 1
        self.entered.set()
        if self.start_gate:
            assert self.start_gate.wait(3)
        assert options["private_output"] is True
        self.is_recording = True
        segment = path.with_name("video_0001.mp4")
        for output in (segment, options["timestamps_path"]):
            with private_open(output, "xb", root=path.parent.parent) as stream:
                stream.write(b"fake-media")
        self.recording_segments = [segment]
        self.metadata.append(options["metadata_provider"]())

    def stop_recording(self):
        self.stop_calls += 1
        if self.stop_gate:
            assert self.stop_gate.wait(3)
        self.is_recording = False


@pytest.fixture
def recording_rig(tmp_path):
    import time

    from furnace.recordings import private_open
    from microscope.service import MicroscopeConfig

    camera, readings = RecordingCamera(), RecordingReadings()
    clock = [10.0]
    calls = []

    def finalize(directory, *, recordings_root, check_cancel):
        check_cancel()
        calls.append(directory.name)
        output = directory / f"{directory.name}_shortened_3_min.mp4"
        with private_open(output, "xb", root=recordings_root) as stream:
            stream.write(b"fake-summary")
        check_cancel()
        return None, output, None

    service = MicroscopeService(
        MicroscopeConfig(lease_seconds=30),
        demo=True,
        camera=camera,
        readings=readings,
        recordings_root=tmp_path / "recordings",
        recording_finalizer=finalize,
        clock=lambda: clock[0],
    )
    service.start(background=False)
    service.lease.acquire("operator")
    rig = SimpleNamespace(
        service=service,
        camera=camera,
        readings=readings,
        clock=clock,
        calls=calls,
        root=tmp_path / "recordings",
    )

    def command(action, args):
        ident = service.command(action, args, "operator")
        until = time.monotonic() + 3
        while time.monotonic() < until:
            state = service.snapshot()
            if state["last_command"] and state["last_command"]["id"] == ident:
                return state
            time.sleep(0.005)
        pytest.fail("Fake recording command did not finish")

    rig.command = command
    yield rig
    for gate in (camera.start_gate, camera.stop_gate):
        if gate:
            gate.set()
    service.close()


def wait_recording(rig, status):
    import time

    until = time.monotonic() + 3
    while time.monotonic() < until:
        state = rig.service.snapshot()
        if state["recording"]["status"] == status:
            return state
        time.sleep(0.005)
    pytest.fail(f"Recording did not reach {status}")


def test_recording_exact_id_start_finish_and_private_retained_outputs(recording_rig):
    rig = recording_rig
    ident = "a" * 32
    state = rig.command("start_recording", {"recording_id": ident})
    assert state["last_command"]["result"] == "ok" and not state["busy"]
    record = state["recording"]
    assert record == state["last_result"]["recording"]
    assert record["id"] == ident and record["status"] == "recording"
    assert record["run_name"] == "Untitled run" and record["frame_count"] == 1
    state = rig.command("stop_recording", {"recording_id": ident})
    assert state["last_command"]["result"] == "ok"
    assert state["recording"] == state["last_result"]["recording"]
    assert state["recording"]["status"] == "completed"
    assert state["recording"]["summary"] == f"{ident}_shortened_3_min.mp4"
    assert state["recording"]["stop_command_id"] == state["last_command"]["id"]
    assert state["recording"]["segments"] == ["video_0001.mp4"]
    assert rig.calls == [ident]
    assert all(path.stat().st_mode & 0o007 == 0 for path in (rig.root / ident).iterdir())
    # A repeated ID never replaces the original run, even under a different label.
    before = (rig.root / ident / "run.json").read_bytes()
    state = rig.command("start_recording", {"recording_id": ident, "run_name": "new"})
    assert state["last_command"]["result"] == "failed"
    assert (rig.root / ident / "run.json").read_bytes() == before


@pytest.mark.parametrize(
    "action,args",
    [
        ("start_recording", {}),
        ("start_recording", {"recording_id": "A" * 32}),
        ("start_recording", {"recording_id": "a" * 32, "run_name": "a\n"}),
        ("start_recording", {"recording_id": "a" * 32, "output_fps": True}),
        ("start_recording", {"recording_id": "a" * 32, "output_fps": float("nan")}),
        ("start_recording", {"recording_id": "a" * 32, "output_fps": 121}),
        (
            "start_recording",
            {"recording_id": "a" * 32, "mode": "live", "capture_interval_seconds": 1},
        ),
        ("stop_recording", {"recording_id": "a" * 32, "mode": "live"}),
        ("record_with_sequence", {"recording_id": "a" * 32}),
        ("record_with_sequence", {"recording_id": "a" * 32, "enabled": False, "mode": "live"}),
        ("set_stabilization", {"enabled": 1}),
    ],
)
def test_recording_arguments_fail_before_dispatch(recording_rig, action, args):
    with pytest.raises(ValueError):
        recording_rig.service.command(action, args, "operator")
    assert recording_rig.camera.start_calls == 0 and not recording_rig.root.exists()


@pytest.mark.parametrize("revoke", ["stop", "release", "expiry", "same-id"])
def test_active_recording_authority_is_irreversible(recording_rig, revoke):
    rig = recording_rig
    rig.command("start_recording", {"recording_id": "b" * 32, "mode": "live"})
    if revoke == "stop":
        rig.service.stop("another-client", 0)
    elif revoke == "release":
        rig.service.lease.release("operator")
    else:
        rig.clock[0] += 31
        if revoke == "same-id":
            rig.service.lease.acquire("operator")
        else:
            rig.service.lease.poll()
    state = wait_recording(rig, "stopped")
    assert not state["recording_sequence"]["armed"]
    assert not rig.calls and rig.camera.stop_calls >= 1
    assert state["recording"]["summary"] is None


@pytest.mark.parametrize("action", ["start_recording", "set_stabilization"])
@pytest.mark.parametrize("revoke", ["stop", "release", "expiry", "same-id"])
def test_camera_admission_rechecks_revoked_service_authority(
    recording_rig, monkeypatch, action, revoke
):
    rig = recording_rig
    entered, resume = threading.Event(), threading.Event()
    captured = []
    original = getattr(rig.camera, action)

    def before_admission(*args, **options):
        # The service's last guard has passed, but the camera has not yet
        # admitted any writer or stabilization change.
        captured.append(options.get("cancel"))
        entered.set()
        assert resume.wait(3)
        return original(*args, **options)

    monkeypatch.setattr(rig.camera, action, before_admission)
    recording_id = "d" * 32
    args = {"recording_id": recording_id} if action == "start_recording" else {"enabled": True}
    ident = rig.service.command(action, args, "operator")
    assert entered.wait(1)
    job, recording = rig.service._job, rig.service._recording
    token = recording.cancel if recording is not None else job.cancel
    try:
        assert captured == [token] and not token.is_set()
        assert rig.camera.start_calls == rig.camera.stabilization_calls == 0
        if revoke == "stop":
            rig.service.stop("emergency", 0)
        elif revoke == "release":
            rig.service.lease.release("operator")
        else:
            rig.clock[0] += 31
            if revoke == "same-id":
                rig.service.lease.acquire("operator")
            else:
                rig.service.lease.poll()
        assert token.is_set()
        assert rig.service._stop_done.wait(1)
        assert not rig.camera.is_recording and not rig.camera.stabilization_enabled
    finally:
        resume.set()
    assert job.done.wait(2)
    if recording is not None:
        assert recording.done.wait(2)
    state = rig.service.snapshot()
    assert state["last_command"]["id"] == ident
    assert state["last_command"]["result"] == "stopped" and state["last_result"] is None
    assert rig.camera.start_calls == rig.camera.stabilization_calls == 0
    assert not rig.camera.is_recording and not rig.camera.stabilization_enabled
    assert not rig.calls
    if recording is not None:
        assert state["recording"]["status"] == "stopped"
        assert state["recording"]["summary"] is None
        # Reserving the persistent attempt ID is allowed; camera output is not.
        assert {path.name for path in (rig.root / recording_id).iterdir()} == {"run.json"}


def test_recording_heartbeat_extends_only_current_live_authority(recording_rig):
    rig = recording_rig
    rig.command("start_recording", {"recording_id": "c" * 32})
    rig.clock[0] += 25
    rig.service.lease.renew("operator")
    rig.clock[0] += 10
    assert not rig.service._recording.cancel.is_set()
    state = rig.command("stop_recording", {"recording_id": "c" * 32})
    assert state["recording"]["status"] == "completed"


def test_stop_does_not_wait_for_blocked_accepted_camera_start(recording_rig):
    rig = recording_rig
    rig.camera.start_gate = threading.Event()
    command_id = rig.service.command("start_recording", {"recording_id": "d" * 32}, "operator")
    assert rig.camera.entered.wait(1)
    assert rig.service.stop("emergency", 0) == "pending"
    assert rig.service._stop_done.wait(1)
    assert rig.service.snapshot()["busy"]
    rig.camera.start_gate.set()
    state = wait_recording(rig, "stopped")
    assert not rig.calls and not rig.camera.is_recording
    assert state["recording"]["start_command_id"] == command_id


def test_stop_during_finalization_cannot_publish_completed(recording_rig):
    rig = recording_rig
    entered, release = threading.Event(), threading.Event()

    def finalizer(directory, **options):
        entered.set()
        assert release.wait(3)
        options["check_cancel"]()
        pytest.fail("Cancelled finalizer cannot publish")

    rig.service._recording_finalizer = finalizer
    rig.command("start_recording", {"recording_id": "e" * 32})
    rig.service.command("stop_recording", {"recording_id": "e" * 32}, "operator")
    assert entered.wait(1)
    try:
        assert rig.service.stop("emergency", 0) == "pending"
        assert rig.service._stop_done.wait(1)
        assert rig.service.snapshot()["busy"]
    finally:
        release.set()
    state = wait_recording(rig, "stopped")
    assert state["recording"]["summary"] is None and not state["recording"]["archived"]


@pytest.mark.parametrize(
    "phase,healthy,identity,expected",
    [
        ("complete", True, 20.0, "completed"),
        ("cooling", True, 20.0, "completed"),
        ("stopped", True, 20.0, "failed"),
        ("fault", False, 20.0, "failed"),
        ("complete", True, 21.0, "failed"),
        ("complete", True, None, "failed"),
    ],
)
def test_sequence_arm_start_and_exact_normal_end(recording_rig, phase, healthy, identity, expected):
    rig = recording_rig
    state = rig.command("record_with_sequence", {"recording_id": "f" * 32, "enabled": True})
    receipt = state["last_result"]["recording_sequence"]
    assert receipt["armed"] and receipt["id"] == "f" * 32
    assert not rig.camera.start_calls and not state["busy"]
    rig.readings.value["sequence"] = dict(
        running=True, started_at=20.0, phase="holding", healthy=True
    )
    wait_recording(rig, "recording")
    assert rig.service.snapshot()["recording"]["start_command_id"] == state["last_command"]["id"]
    # Missing samples do not finish, disarm, or infer a successful end.
    rig.readings.value["sequence"] = None
    assert rig.service._recording is not None
    rig.readings.value["sequence"] = dict(
        running=False, started_at=identity, phase=phase, healthy=healthy
    )
    state = wait_recording(rig, expected)
    assert bool(rig.calls) is (expected == "completed")
    assert receipt["armed"]  # accepted receipt is immutable


def test_disarm_preserves_exact_accepted_identity_and_never_starts(recording_rig):
    rig = recording_rig
    ident = "1" * 32
    rig.command("record_with_sequence", {"recording_id": ident, "enabled": True})
    state = rig.command("record_with_sequence", {"recording_id": ident, "enabled": False})
    accepted = state["last_result"]["recording_sequence"]
    assert accepted == dict(armed=False, id=ident, client="operator", sequence_started_at=None)
    wait_recording(rig, "stopped")
    assert not rig.camera.start_calls and not rig.calls


def test_recording_missing_furnace_metadata_still_captures(recording_rig):
    from furnace.service import FRAME_METADATA_FIELDS

    rig = recording_rig
    rig.readings.value = dict(
        available=False,
        error="unavailable",
        sequence=None,
        metadata=dict.fromkeys(FRAME_METADATA_FIELDS),
    )
    state = rig.command("start_recording", {"recording_id": "2" * 32})
    assert state["recording"]["status"] == "recording"
    assert rig.camera.metadata == [dict.fromkeys(FRAME_METADATA_FIELDS)]
    assert (
        rig.command("stop_recording", {"recording_id": "2" * 32})["recording"]["status"]
        == "completed"
    )


def furnace_reading_state():
    return dict(
        service="furnace",
        connected=True,
        locked=False,
        fault=None,
        sequence=dict(running=False, phase="idle", current_index=None, started_at=None),
        sample_time=100.0,
        pv=22.5,
        sv=30.0,
        ramp=2.0,
        run_state="STOP",
        flow=dict(gas_channels=[dict(channel_index=0, target_percent=12.0)]),
    )


def test_metadata_provider_preserves_columns_blanks_failure_and_bounds_events():
    from furnace.service import FRAME_METADATA_FIELDS
    from microscope.service import FurnaceReadings

    state, now = furnace_reading_state(), [0.0]
    events = EventLog()
    provider = FurnaceReadings(SimpleNamespace(state=lambda: state), events, clock=lambda: now[0])
    assert not provider.snapshot()["available"]
    provider.poll_once()
    value = provider.snapshot()
    assert value["available"]
    assert tuple(value["metadata"]) == FRAME_METADATA_FIELDS
    assert value["metadata"]["temperature_c"] == 22.5
    assert value["metadata"]["gas_1_target_percent"] == 12.0
    assert value["metadata"]["gas_2_target_percent"] is None
    now[0] += 3
    assert provider.snapshot()["metadata"] == dict.fromkeys(FRAME_METADATA_FIELDS)
    state["connected"] = False
    provider.poll_once()
    provider.poll_once()
    assert len(events.since()["events"]) == 2  # available, then one failure transition
    state["connected"] = True
    state["locked"], state["fault"] = True, {"message": "fault"}
    provider.poll_once()
    value = provider.snapshot()
    assert not value["available"] and not value["sequence"]["healthy"]
    assert value["metadata"] == dict.fromkeys(FRAME_METADATA_FIELDS)
    provider.close()
    state["fault"], state["locked"] = None, False
    provider.poll_once()
    assert not provider.snapshot()["available"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("flow", None),
        ("flow", {"gas_channels": [None]}),
        ("flow", {"gas_channels": [{"channel_index": 0, "target_percent": float("nan")}]}),
        ("flow", {"gas_channels": [{"channel_index": 0, "target_percent": 10}] * 2}),
        ("pv", float("inf")),
        ("locked", None),
        ("sequence", None),
        ("sequence", dict(running=True, phase="holding", started_at=None)),
    ],
)
def test_metadata_provider_refuses_malformed_shapes(key, value):
    from microscope.service import FurnaceReadings

    state = furnace_reading_state()
    state[key] = value
    with pytest.raises(ValueError):
        FurnaceReadings._validate(state)


def test_metadata_provider_worker_start_failure_rolls_back(monkeypatch):
    from microscope.service import FurnaceReadings

    provider = FurnaceReadings(SimpleNamespace(state=furnace_reading_state), EventLog())

    def fail(_thread):
        raise RuntimeError("fake start failure")

    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="fake start"):
        provider.start()
    assert provider._thread is None
    provider.close()


def test_readings_client_allows_only_bounded_get_with_header_token():
    import io
    import json

    from microscope.service import FurnaceReadingsClient

    captured = []

    class Opener:
        def open(self, request, **kwargs):
            captured.append((request, kwargs))
            return io.BytesIO(json.dumps({"service": "furnace"}).encode())

    client = FurnaceReadingsClient("http://127.0.0.1:8769/", "x" * 32)
    client._opener = Opener()
    assert client._request("api/state") == {"service": "furnace"}
    request, options = captured[0]
    assert request.get_method() == "GET" and request.data is None
    assert request.get_header("Authorization") == "Bearer " + "x" * 32
    assert "x" * 32 not in request.full_url and options == {"timeout": 0.5}
    for path, data in (
        ("api/command", {}),
        ("api/state", {}),
        ("api/lease", None),
        ("../api/state", None),
    ):
        with pytest.raises(ServiceError):
            client._request(path, data)
    assert len(captured) == 1
    with pytest.raises(ServiceError, match="size limit"):
        client._body(io.BytesIO(b"x" * (256 * 1024 + 1)))


def test_readings_client_rejects_trickling_body_and_redirect(monkeypatch):
    import io

    import microscope.service as module

    client = module.FurnaceReadingsClient("http://127.0.0.1:8769/")
    ticks = iter((0.0, 0.2, 1.1))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    with pytest.raises(ServiceError, match="deadline"):
        client._body(io.BytesIO(b"{}"))
    assert (
        module._NoMetadataRedirect().redirect_request(
            None, None, 302, "", {}, "http://outside.invalid/"
        )
        is None
    )


def test_wrong_recording_id_and_duplicate_start_do_not_cancel_current(recording_rig):
    rig = recording_rig
    rig.command("start_recording", {"recording_id": "3" * 32})
    for action, args in (
        ("stop_recording", {"recording_id": "4" * 32}),
        ("start_recording", {"recording_id": "5" * 32}),
    ):
        with pytest.raises(ServiceError):
            rig.service.command(action, args, "operator")
    assert not rig.service._recording.cancel.is_set() and rig.camera.is_recording
    assert (
        rig.command("stop_recording", {"recording_id": "3" * 32})["recording"]["status"]
        == "completed"
    )


def test_recording_worker_start_failure_is_terminal_and_retains_no_live_authority(
    recording_rig, monkeypatch
):
    rig = recording_rig
    original = threading.Thread.start

    def start(thread):
        if thread.name == "microscope-recording":
            raise RuntimeError("fake recorder start failure")
        return original(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    state = rig.command("start_recording", {"recording_id": "6" * 32})
    assert state["last_command"]["result"] == "failed"
    assert state["recording"]["status"] == "failed" and rig.service._recording is None
    assert not rig.camera.start_calls


def test_camera_error_cannot_finalize_partial_capture(recording_rig):
    rig = recording_rig
    rig.command("start_recording", {"recording_id": "7" * 32})
    rig.camera.recording_error = "synthetic writer failure"
    state = wait_recording(rig, "failed")
    assert state["recording"]["summary"] is None and not rig.calls


def test_stabilization_command_does_not_start_recording_or_change_lease(recording_rig):
    rig = recording_rig
    before = rig.service.lease.snapshot()
    state = rig.command("set_stabilization", {"enabled": True})
    assert state["last_result"] == {"stabilization": True}
    assert state["stabilization"] and not rig.camera.start_calls
    assert rig.service.lease.snapshot() == before


def test_close_retains_all_owners_until_accepted_camera_command_drains(recording_rig, monkeypatch):
    rig = recording_rig
    entered, release, stopped = (threading.Event() for _ in range(3))
    closes = []
    stabilize = rig.camera.set_stabilization
    stop = rig.service._motor.safe_stop_disable

    def blocked_stabilization(enabled, *, cancel=None):
        # Admission precedes the blocked native call: close must retain its
        # owners until the accepted operation returns, even after cancellation.
        stabilize(enabled, cancel=cancel)
        entered.set()
        assert release.wait(3)

    def observed_stop():
        stopped.set()
        return stop()

    for name, owner, method in (
        ("readings", rig.readings, "close"),
        ("side", rig.service._side, "close"),
        ("camera", rig.camera, "close"),
        ("motor", rig.service._motor, "disconnect"),
    ):
        original = getattr(owner, method)

        def close_owner(original=original, name=name):
            closes.append(name)
            return original()

        monkeypatch.setattr(owner, method, close_owner)
    monkeypatch.setattr(rig.camera, "set_stabilization", blocked_stabilization)
    monkeypatch.setattr(rig.service._motor, "safe_stop_disable", observed_stop)
    ident = rig.service.command("set_stabilization", {"enabled": True}, "operator")
    assert entered.wait(1)
    job = rig.service._job
    try:
        # Simulate the bounded drain deadline while the accepted call is still
        # blocked; the production timeout and Stop path remain unchanged.
        with monkeypatch.context() as patch:
            patch.setattr(job.done, "wait", lambda _timeout: False)
            with pytest.raises(RuntimeError, match="command is still stopping"):
                rig.service.close()
        assert stopped.is_set() and job.cancel.is_set() and not job.done.is_set()
        assert closes == []
        assert rig.camera.is_open and rig.service._side.is_open and rig.service._motor.connected
        assert not rig.service._shutdown_complete
        with pytest.raises(ServiceError) as denied:
            rig.service.command("set_stabilization", {"enabled": False}, "operator")
        assert denied.value.status == 503
    finally:
        release.set()
    assert job.done.wait(2)
    state = rig.service.snapshot()
    assert state["last_command"]["id"] == ident
    assert state["last_command"]["result"] == "stopped" and state["last_result"] is None
    rig.service.close()
    rig.service.close()
    assert rig.service._shutdown_complete
    assert closes == ["readings", "side", "camera", "motor"]
    assert not rig.camera.is_open and not rig.service._motor.connected


@pytest.mark.parametrize("terminal", ["failed", "stopped"])
def test_terminal_recording_cleanup_blocks_mutations_until_slot_clears(
    recording_rig, monkeypatch, terminal
):
    rig = recording_rig
    rig.command("start_recording", {"recording_id": "2" * 32})
    recording = rig.service._recording
    cleanup_entered = threading.Event()
    rig.camera.stop_gate = threading.Event()
    stop_recording = rig.camera.stop_recording

    def blocked_cleanup():
        cleanup_entered.set()
        stop_recording()

    monkeypatch.setattr(rig.camera, "stop_recording", blocked_cleanup)
    if terminal == "failed":
        rig.camera.recording_error = "synthetic asynchronous recording failure"
    else:
        rig.service.stop("operator-stop", 0)
    try:
        assert cleanup_entered.wait(2)
        assert rig.service._stop_done.wait(2)
        state = rig.service.snapshot()
        assert state["recording"]["status"] == terminal
        assert not recording.done.is_set() and rig.service._recording is recording
        assert not rig.service._stopping and rig.service._job is None
        assert state["busy"] and rig.camera.is_recording
        assert state["recording"]["summary"] is None and not rig.calls
        for action, args in (
            ("set_stabilization", {"enabled": True}),
            ("focus_move", {"steps": 1}),
            ("start_recording", {"recording_id": "3" * 32}),
        ):
            with pytest.raises(ServiceError, match="busy") as denied:
                rig.service.command(action, args, "operator")
            assert denied.value.status == 409
        assert not rig.camera.stabilization_enabled and rig.camera.start_calls == 1
        assert rig.service.stop("emergency", 0) == "pending"
    finally:
        rig.camera.stop_gate.set()
    assert recording.done.wait(2) and rig.service._stop_done.wait(2)
    state = rig.service.snapshot()
    assert rig.service._recording is None and not state["busy"]
    assert state["recording"]["status"] == terminal and state["recording"]["summary"] is None
    assert not rig.camera.is_recording and not rig.calls
    state = rig.command("set_stabilization", {"enabled": True})
    assert state["last_command"]["result"] == "ok" and state["stabilization"]


@pytest.mark.parametrize("armed", [False, True])
def test_live_or_armed_recording_keeps_ordinary_camera_commands_available(recording_rig, armed):
    rig = recording_rig
    action = "record_with_sequence" if armed else "start_recording"
    args = {"recording_id": "4" * 32, **({"enabled": True} if armed else {})}
    state = rig.command(action, args)
    recording = rig.service._recording
    assert not state["busy"]
    state = rig.command("set_stabilization", {"enabled": True})
    assert state["last_command"]["result"] == "ok" and state["stabilization"]
    assert rig.service._recording is recording and not recording.cancel.is_set()
    assert state["recording_sequence"]["armed"] is armed
    assert rig.camera.start_calls == (0 if armed else 1)


@pytest.mark.parametrize(
    "args",
    [
        {"recording_id": 11111111111111111111111111111111},
        {"recording_id": "a" * 32, "output_fps": 10**1000},
        {"recording_id": "a" * 32, "run_name": "control\u0085"},
    ],
)
def test_recording_strict_types_remain_client_errors(recording_rig, args):
    rig = recording_rig
    client = create_app(rig.service).test_client()
    reply = client.post(
        "/api/command",
        json={"action": "start_recording", "args": args},
        headers={"X-MG400-Client": "operator"},
    )
    assert reply.status_code == 400 and not rig.camera.start_calls


@pytest.mark.parametrize("enabled", [True, False, 1, None])
def test_stop_recording_rejects_enabled_field_before_dispatch(recording_rig, enabled):
    with pytest.raises(ValueError):
        recording_rig.service.command(
            "stop_recording", {"recording_id": "a" * 32, "enabled": enabled}, "operator"
        )


def test_recording_columns_match_existing_furnace_source_schema():
    from furnace.recordings import FRAME_METADATA_FIELDS
    from furnace.service import FRAME_METADATA_FIELDS as SOURCE_FIELDS

    assert FRAME_METADATA_FIELDS == SOURCE_FIELDS


def test_furnace_provider_settings_reuse_existing_secret_without_public_disclosure():
    from microscope.service import MicroscopeConfig

    token = "test-secret-only" * 2
    config = MicroscopeConfig.from_env(
        {
            "MG400_MICROSCOPE_FURNACE_URL": "http://127.0.0.1:8769/heat/",
            "MG400_FURNACE_TOKEN": token,
        }
    )
    assert config.furnace_url.endswith("/heat/") and config.furnace_token == token
    assert token not in repr(config)
    service = MicroscopeService(config)
    assert token not in str(service.snapshot()) and token not in str(service.describe())
    service.close()


def test_recording_cleanup_failure_keeps_stop_unconfirmed(recording_rig):
    rig = recording_rig
    rig.command("start_recording", {"recording_id": "8" * 32})
    original = rig.camera.stop_recording

    def fail():
        raise RuntimeError("synthetic close failure")

    rig.camera.stop_recording = fail
    try:
        rig.service.stop("emergency", 0)
        state = wait_recording(rig, "failed")
        assert state["locked"] and "shutdown is unconfirmed" in state["fault"]["message"]
        assert rig.service.stop("emergency", 1) == "unconfirmed"
    finally:
        rig.camera.stop_recording = original
        # The fixture must observe, not conceal, this intentionally latched failure.
        with pytest.raises(RuntimeError, match="shutdown is unconfirmed"):
            rig.service.close()


@pytest.mark.parametrize("revoke", ["release", "expiry", "same-id"])
def test_armed_sequence_cannot_start_after_authority_is_lost(recording_rig, revoke):
    import time

    rig = recording_rig
    rig.command("record_with_sequence", {"recording_id": "9" * 32, "enabled": True})
    recording = rig.service._recording
    if revoke == "release":
        rig.service.lease.release("operator")
    else:
        rig.clock[0] += 31
        if revoke == "same-id":
            rig.service.lease.acquire("operator")
        else:
            rig.service.lease.poll()
    assert recording.done.wait(1)
    rig.readings.value["sequence"] = dict(
        running=True, started_at=100, phase="holding", healthy=True
    )
    time.sleep(0.08)
    assert recording.cancel.is_set() and not rig.camera.start_calls
    assert not rig.service.snapshot()["recording_sequence"]["armed"]


def test_provider_close_discards_late_read_without_publishing_or_rearming():
    from microscope.service import FurnaceReadings

    entered, release = threading.Event(), threading.Event()

    def read():
        entered.set()
        assert release.wait(3)
        return furnace_reading_state()

    events = EventLog()
    provider = FurnaceReadings(SimpleNamespace(state=read), events)
    worker, outcomes = launch(provider.poll_once)
    assert entered.wait(1)
    provider.close()
    release.set()
    finish(worker)
    assert outcomes == [None] and not provider.snapshot()["available"]
    assert events.since()["events"] == []


def test_readings_http_error_does_not_echo_remote_details_or_leave_response_open():
    import io
    import urllib.error

    from microscope.service import FurnaceReadingsClient

    body = io.BytesIO(b"secret-remote-error")
    error = urllib.error.HTTPError("http://127.0.0.1/", 401, "secret-remote-error", {}, body)

    class Opener:
        def open(self, *_args, **_kwargs):
            raise error

    client = FurnaceReadingsClient("http://127.0.0.1/")
    client._opener = Opener()
    with pytest.raises(ServiceError, match="request was refused") as caught:
        client._request("api/state")
    assert "secret" not in str(caught.value) and body.closed


def test_stop_can_drain_old_recording_while_same_client_acquires_new_lease(recording_rig):
    rig = recording_rig
    rig.camera.stop_gate = threading.Event()
    rig.command("start_recording", {"recording_id": "a1" * 16})
    old = rig.service._recording
    rig.service.lease.release("operator")
    rig.service.lease.acquire("operator")
    assert old.cancel.is_set()
    with pytest.raises(ServiceError, match="busy"):
        rig.service.command("start_recording", {"recording_id": "b1" * 16}, "operator")
    rig.camera.stop_gate.set()
    assert old.done.wait(1)
    assert old.data["status"] == "stopped" and not rig.calls


def test_actual_app_recording_contract_uses_flat_arguments_and_exact_stop_receipt(recording_rig):
    import time

    rig = recording_rig
    client = create_app(rig.service, base_path="/scope", token="s" * 32).test_client()
    headers = {"Authorization": "Bearer " + "s" * 32, "X-MG400-Client": "operator"}

    def issue(action):
        response = client.post(
            "/scope/api/command",
            json={"action": action, "recording_id": "c1" * 16},
            headers=headers,
        )
        assert response.status_code == 202
        command_id = response.json["command_id"]
        until = time.monotonic() + 3
        while time.monotonic() < until:
            state = client.get("/scope/api/state", headers=headers).json
            if state["last_command"] and state["last_command"]["id"] == command_id:
                return state
            time.sleep(0.005)
        pytest.fail("HTTP command did not settle")

    assert client.get("/scope/api/state").status_code == 401
    assert issue("start_recording")["recording"]["status"] == "recording"
    state = issue("stop_recording")
    assert state["last_result"]["recording"] == state["recording"]
    assert state["recording"]["status"] == "completed"


@pytest.mark.parametrize("mode", ["time_lapse", "live"])
def test_typed_recording_skill_consumes_actual_service_receipts(recording_rig, mode):
    import json
    import time

    from orchestrator.skills.recording import record

    rig = recording_rig
    api = create_app(rig.service).test_client()
    headers = {"X-MG400-Client": "operator"}
    artifacts = []

    class Client:
        def state(self):
            return api.get("/api/state").json

        def owned(self, state):
            return state["lease"]["holder"] == "operator"

        def command(self, action, **args):
            response = api.post("/api/command", json={"action": action, **args}, headers=headers)
            assert response.status_code == 202
            return response.json

    def artifact(name, data):
        artifacts.append((name, json.loads(data)))
        return len(artifacts)

    context = SimpleNamespace(
        clients={"microscope": Client()},
        remaining_s=lambda: 10,
        sleep=time.sleep,
        store=SimpleNamespace(artifact=artifact),
    )
    result = record(context, "chip-offline", 0.01, mode=mode)
    assert result["status"] == "completed" and len(artifacts) == 2
    intent, completed = artifacts[0][1], artifacts[1][1]
    assert intent["recording_id"] == completed["recording_id"] == result["recording_id"]
    assert completed["chip_id"] == "chip-offline" and completed["mode"] == mode
    assert completed["segments"] == ["video_0001.mp4"]
    assert completed["summary"] == f"{result['recording_id']}_shortened_3_min.mp4"
    assert str(rig.root) not in json.dumps(artifacts)


@pytest.mark.parametrize("outcome", ["confirmed", "pending", "camera-failure", "motor-failure"])
def test_stop_waits_for_current_recording_with_remaining_motor_budget(
    recording_rig, monkeypatch, outcome
):
    import time

    import microscope.service as module

    rig = recording_rig
    rig.command("start_recording", {"recording_id": "d1" * 16})
    recording = rig.service._recording
    recording_entered, release_recording = threading.Event(), threading.Event()
    now, motor_waits, recording_waits, results = [0.0], [], [], []
    caller = None
    original_stop = rig.camera.stop_recording

    def close_capture():
        recording_entered.set()
        assert release_recording.wait(5)
        if outcome == "camera-failure":
            raise RuntimeError("Offline capture shutdown failed")
        original_stop()

    class Wait:
        def __init__(self, event, *, motor):
            self.event, self.motor = event, motor

        def set(self):
            self.event.set()

        def clear(self):
            self.event.clear()

        def is_set(self):
            return self.event.is_set()

        def wait(self, timeout=None):
            if threading.current_thread() is not caller:
                return self.event.wait(timeout)
            if self.motor:
                motor_waits.append(timeout)
                assert self.event.wait(5)
                now[0] = 0.25
                return True
            recording_waits.append(timeout)
            if outcome == "pending":
                now[0] = 1.0
                return False
            release_recording.set()
            assert self.event.wait(5)
            now[0] = 0.5
            return True

    monkeypatch.setattr(rig.camera, "stop_recording", close_capture)
    monkeypatch.setattr(
        module, "time", SimpleNamespace(monotonic=lambda: now[0], time=time.time, sleep=time.sleep)
    )
    rig.service._stop_done = Wait(rig.service._stop_done, motor=True)
    recording.done = Wait(recording.done, motor=False)
    if outcome == "motor-failure":

        def fail_motor():
            raise RuntimeError("Offline motor shutdown failed")

        monkeypatch.setattr(rig.service._motor, "safe_stop_disable", fail_motor)
    caller = threading.Thread(target=lambda: results.append(rig.service.stop("operator", 1)))
    caller.start()
    try:
        assert recording_entered.wait(5)
        caller.join(5)
        assert not caller.is_alive()
        expected = "unconfirmed" if outcome.endswith("failure") else outcome
        assert results == [expected]
        assert motor_waits == [1.0]
        assert recording_waits == ([] if outcome == "motor-failure" else [0.75])
        if outcome in {"pending", "motor-failure"}:
            assert not recording.done.is_set() and rig.camera.is_recording
            assert rig.service.snapshot()["busy"]
        else:
            assert recording.done.is_set()
        assert recording.data["status"] != "completed"
    finally:
        release_recording.set()
        caller.join(5)
        assert recording.done.wait(5)
        if outcome.endswith("failure"):
            monkeypatch.setattr(rig.camera, "stop_recording", original_stop)
            with pytest.raises(RuntimeError, match="shutdown is unconfirmed"):
                rig.service.close()
