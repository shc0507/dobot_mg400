"""Manual reference tests use an in-memory board and generated, offline JPEGs."""

import os
from io import BytesIO

import cv2
import numpy as np
import pytest

from labkit.errors import ServiceError
from stage_controller.app import create_app
from stage_controller.config import geometry_data, load_geometry, parse_geometry
from stage_controller.protocol import ControllerError, LinkError
from stage_controller.reference_camera import ReferenceCamera
from stage_controller.reference_store import _toml


def picture(marker=12, x=60, y=60, duplicate=False):
    frame = np.full((300, 400), 255, dtype=np.uint8)
    tile = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_1000), marker, 80
    )
    frame[y : y + 80, x : x + 80] = tile
    if duplicate:
        frame[180:260, 280:360] = tile
    return cv2.imencode(".jpg", frame)[1].tobytes()


class Frames:
    def __init__(self, data=None, callback=None):
        self.data = picture() if data is None else data
        self.calls = []
        self.callback = callback

    def read(self, stream):
        self.calls.append(stream)
        if self.callback:
            self.callback()
        return self.data


def reference(controller, *, pixel=(99.5, 99.5), tolerance=2, frames=None):
    data = geometry_data(controller._geometry)
    data["motors"]["insert"]["reference"] = dict(
        stream="camera3", marker=12, pixel=list(pixel), tolerance_px=tolerance
    )
    controller._geometry = parse_geometry(data)
    controller._frame_source = frames or Frames()
    return controller._geometry.motors["insert"].reference


def test_zero_confirms_both_stages_and_audits_client(stage_rig):
    controller, transport, _ = stage_rig
    transport.motors["insert"].position = 123
    controller.lease.acquire("operator")
    identifier = controller.command("zero", {"motor": "insert", "confirm": True}, "operator")
    assert transport.commands == ["motor insert", "zero", "soft -1000 1000"]
    assert controller._ready("insert")
    assert controller.snapshot()["last_command"]["id"] == identifier
    assert any(
        "zero confirmed by operator" in event["message"]
        for event in controller.snapshot()["events"]
    )


@pytest.mark.parametrize("confirm", [None, False, 1, "true", {}, []])
def test_zero_needs_literal_confirmation_before_any_write(stage_rig, confirm):
    controller, transport, _ = stage_rig
    with pytest.raises(ValueError, match="Confirm"):
        controller.zero("insert", confirm)
    assert not transport.commands


@pytest.mark.parametrize("condition", ["moving", "cooldown", "unsupported", "locked", "lease"])
def test_zero_refuses_unsafe_state(stage_rig, condition):
    controller, transport, clock = stage_rig
    if condition == "moving":
        transport.motors["insert"].mode = "moving"
    elif condition == "cooldown":
        controller._cooldowns["insert"] = clock() + 1
    elif condition == "unsupported":
        transport.zero_supported = False
    elif condition == "locked":
        controller._contract_fault = "unknown outcome"
    else:
        controller.lease.acquire("orchestrator")
    with pytest.raises(ControllerError):
        controller.zero("insert", True)
    assert not transport.commands


@pytest.mark.parametrize("unconfirmed", ["zero", "soft", "microstep"])
def test_zero_never_grants_reference_from_acknowledgement_alone(stage_rig, unconfirmed):
    controller, transport, _ = stage_rig
    original = transport.command

    def command(value):
        if value == unconfirmed or (unconfirmed == "soft" and value.startswith("soft ")):
            return
        original(value)
        if unconfirmed == "microstep" and value == "zero":
            transport.microstep = 2

    transport.command = command
    with pytest.raises(LinkError):
        controller.zero("insert", True)
    assert not controller._ready("insert")
    assert controller.snapshot()["locked"]


def test_zero_references_clear_on_reset_and_microstep_change(stage_rig):
    controller, transport, _ = stage_rig
    controller.zero("insert", True)
    controller.set_microstep(2)
    assert not controller._ready("insert")
    controller.zero("insert", True)
    transport.motors["insert"].homed = False
    controller.poll()
    assert not controller._ready("insert")


def test_camera_matches_exact_marker_and_tolerance(stage_rig):
    controller, transport, _ = stage_rig
    reference(controller)
    controller.zero("insert", True)
    assert controller._ready("insert")
    assert controller._frame_source.calls == ["camera3"]
    view = controller.snapshot()["motors"][2]["reference"]["observation"]
    assert view["distance_px"] < 1
    assert "image" not in view
    assert transport.commands == ["motor insert", "zero", "soft -1000 1000"]


@pytest.mark.parametrize(
    "frames,pixel",
    [
        (Frames(picture(marker=13)), (99.5, 99.5)),
        (Frames(picture(duplicate=True)), (99.5, 99.5)),
        (Frames(), (120, 120)),
        (Frames(b"not a jpeg"), (99.5, 99.5)),
    ],
)
def test_camera_failure_never_changes_zero(stage_rig, frames, pixel):
    controller, transport, _ = stage_rig
    reference(controller, frames=frames, pixel=pixel)
    with pytest.raises(ControllerError):
        controller.zero("insert", True)
    assert not transport.commands
    assert not controller._ready("insert")


@pytest.mark.parametrize(
    "change", ["lease_expiry", "release_reacquire", "position", "microstep", "event", "stale"]
)
def test_camera_fetch_cannot_carry_authority_across_state_change(stage_rig, change):
    controller, transport, clock = stage_rig
    controller.lease.acquire("operator")

    def callback():
        if change == "lease_expiry":
            clock.advance(4)
        elif change == "release_reacquire":
            controller.lease.release("operator")
            controller.lease.acquire("operator")
        elif change == "position":
            transport.motors["insert"].position += 1
        elif change == "microstep":
            transport.microstep = 2
        elif change == "event":
            controller._event("Concurrent operation")
        else:
            clock.advance(2.1)

    reference(controller, frames=Frames(callback=callback))
    with pytest.raises((ServiceError, ControllerError)):
        controller.command("zero", {"motor": "insert", "confirm": True}, "operator")
    assert "zero" not in transport.commands
    assert not controller._ready("insert")


def test_stop_during_camera_fetch_remains_available_and_cancels_zero(stage_rig):
    controller, transport, _ = stage_rig
    reference(controller, frames=Frames(callback=lambda: controller.stop_all()))
    with pytest.raises(ControllerError, match="changed"):
        controller.zero("insert", True)
    assert transport.commands == ["stop all"]


def test_manual_zero_demo_features_and_no_switch_home(stage_rig):
    controller, transport, _ = stage_rig
    transport.home_switches = dict.fromkeys(transport.motors, False)
    assert all(
        not axis["homing_available"] and axis["zero_available"]
        for axis in controller.snapshot()["motors"]
    )
    with pytest.raises(ControllerError, match="minimum limit not configured"):
        controller.home("insert")
    assert transport.motors["insert"].mode == "idle"
    controller.zero("insert", True)
    assert controller._ready("insert")


@pytest.mark.parametrize(
    "key,value",
    [
        ("stream", "http://bad/"),
        ("stream", "camera1&src=other"),
        ("marker", True),
        ("marker", 1000),
        ("pixel", [float("nan"), 0]),
        ("pixel", [-1, 0]),
        ("pixel", [1]),
        ("tolerance_px", 0),
        ("tolerance_px", True),
        ("tolerance_px", float("inf")),
    ],
)
def test_reference_configuration_fails_closed(stage_rig, key, value):
    controller, _, _ = stage_rig
    reference(controller)
    data = geometry_data(controller._geometry)
    data["motors"]["insert"]["reference"][key] = value
    with pytest.raises(ValueError):
        parse_geometry(data)


def config_file(tmp_path, geometry):
    tmp_path.chmod(0o700)
    path = tmp_path / "stage.toml"
    path.write_text(_toml(geometry))
    path.chmod(0o640)
    return path


def test_teach_persists_reference_atomically_and_invalidates_prior_zero(stage_rig, tmp_path):
    controller, transport, _ = stage_rig
    reference(controller, pixel=(10, 10))
    path = config_file(tmp_path, controller._geometry)
    controller._config_path = path
    before = path.stat().st_ino
    controller.teach_reference("insert", True)
    assert path.stat().st_ino != before
    assert path.stat().st_mode & 0o777 == 0o640
    assert load_geometry(path) == controller._geometry
    assert controller._geometry.motors["insert"].reference["pixel"] == pytest.approx(
        [99.5, 99.5], abs=1
    )
    assert not transport.commands
    assert not controller._ready("insert")
    controller.zero("insert", True)
    assert controller._ready("insert")


@pytest.mark.parametrize("unsafe", ["changed", "symlink", "hardlink"])
def test_teach_never_overwrites_changed_or_unsafe_configuration(stage_rig, tmp_path, unsafe):
    controller, transport, _ = stage_rig
    reference(controller)
    path = config_file(tmp_path, controller._geometry)
    controller._config_path = path
    original = path.read_bytes()
    if unsafe == "changed":
        data = geometry_data(controller._geometry)
        data["motors"]["top"]["steps_per_mm"] = 20
        path.write_text(_toml(parse_geometry(data)))
    elif unsafe == "symlink":
        real = tmp_path / "real"
        path.rename(real)
        path.symlink_to(real)
    else:
        os.link(path, tmp_path / "link")
    unchanged = path.read_bytes()
    with pytest.raises((ValueError, ControllerError)):
        controller.teach_reference("insert", True)
    assert path.read_bytes() == unchanged
    assert not transport.commands
    assert original


class Response(BytesIO):
    status = 200

    def geturl(self):
        return "http://127.0.0.1:1984/api/frame.jpeg?src=camera3"


class Opener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, url, timeout):
        self.calls.append((url, timeout))
        return self.response


def test_camera_transport_is_fixed_loopback_bounded_and_closed():
    response = Response(picture())
    opener = Opener(response)
    assert ReferenceCamera(opener=opener).read("camera3").startswith(b"\xff\xd8")
    assert opener.calls == [("http://127.0.0.1:1984/api/frame.jpeg?src=camera3", 2)]
    assert response.closed


@pytest.mark.parametrize("failure", ["redirect", "status", "size"])
def test_camera_transport_refuses_bad_response(failure):
    response = Response(b"x" * (4 * 1024 * 1024 + 1) if failure == "size" else picture())
    if failure == "redirect":
        response.geturl = lambda: "http://elsewhere/"
    elif failure == "status":
        response.status = 302
    with pytest.raises(ControllerError):
        ReferenceCamera(opener=Opener(response)).read("camera3")
    assert response.closed


def test_preview_route_and_contract_zero_keep_lease_checks(stage_rig):
    controller, transport, _ = stage_rig
    reference(controller)
    client = create_app(controller).test_client()
    access = {}
    holder = {**access, "X-MG400-Client": "runner"}
    other = {**access, "X-MG400-Client": "operator"}
    preview = "/api/motors/insert/reference"

    def zero(confirm, headers):
        body = {"action": "zero", "motor": "insert", "confirm": confirm}
        return client.post("/api/command", json=body, headers=headers)

    assert zero(True, access).status_code == 400
    controller.lease.acquire("runner")
    assert zero(1, holder).status_code == 400
    assert zero(True, other).status_code == 409
    assert not transport.commands
    assert client.get(preview, headers=access).json["image"].startswith("data:image/jpeg;base64,")


def test_initial_link_loss_invalidates_reference_and_connection(stage_rig, monkeypatch):
    controller, transport, _ = stage_rig
    controller.zero("insert", True)

    def lost():
        raise LinkError("unplugged")

    monkeypatch.setattr(transport, "status", lost)
    with pytest.raises(LinkError):
        controller.zero("insert", True)
    assert not controller._ready("insert")
    assert not controller.snapshot()["connected"]
    assert controller.snapshot()["locked"]


def test_stale_feedback_after_motor_select_cannot_authorize_zero(stage_rig):
    controller, transport, clock = stage_rig
    reference(controller)
    original = transport.command

    def delayed(command):
        original(command)
        if command == "motor insert":
            clock.advance(2.1)

    transport.command = delayed
    with pytest.raises(ControllerError, match="stale"):
        controller.zero("insert", True)
    assert transport.commands == ["motor insert"]
    assert not controller._ready("insert")


def test_camera_slow_body_checks_budget_between_single_buffer_reads():
    from stage_helpers import Clock

    clock = Clock()

    class SlowResponse(Response):
        calls = 0

        def read(self, _size=-1):
            raise AssertionError("An exact-size read can wait across multiple receives")

        def read1(self, _size=-1):
            self.calls += 1
            clock.advance(0.7)
            return b"a"

    response = SlowResponse()
    with pytest.raises(ControllerError, match="timed out"):
        ReferenceCamera(opener=Opener(response), clock=clock).read("camera3")
    assert response.calls == 3
    assert response.closed


def test_teach_reference_command_needs_the_lease_holder(stage_rig, tmp_path):
    controller, transport, _ = stage_rig
    reference(controller, pixel=(10, 10))
    controller._config_path = config_file(tmp_path, controller._geometry)
    before = controller._config_path.read_bytes()
    client = create_app(controller).test_client()
    access = {}
    operator = {**access, "X-MG400-Client": "stage-operator"}
    endpoint = "/api/command"
    confirm = {"action": "teach_reference", "motor": "insert", "confirm": True}
    assert client.post(endpoint, json=confirm, headers=access).status_code == 400
    # Nobody holds the lease: a client that does not hold it is refused.
    assert client.post(endpoint, json=confirm, headers=operator).status_code == 409
    controller.lease.acquire("automation")
    assert client.post(endpoint, json=confirm, headers=operator).status_code == 409
    assert controller._config_path.read_bytes() == before
    controller.lease.release("automation")
    controller.lease.acquire("stage-operator")
    unconfirmed = {**confirm, "confirm": 1}
    assert client.post(endpoint, json=unconfirmed, headers=operator).status_code == 400
    assert controller._config_path.read_bytes() == before
    response = client.post(endpoint, json=confirm, headers=operator)
    assert response.status_code == 202
    assert response.json["state"]["lease"]["holder"] == "stage-operator"
    assert controller._geometry.motors["insert"].reference["pixel"] == pytest.approx(
        [99.5, 99.5], abs=1
    )
    assert not transport.commands
