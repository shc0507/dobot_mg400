"""Inactive camera-reference helper transport remains bounded and local."""

from io import BytesIO

import cv2
import numpy as np
import pytest

from stage_controller.protocol import ControllerError
from stage_controller.reference_camera import ReferenceCamera


def picture(marker=12, x=60, y=60, duplicate=False):
    frame = np.full((300, 400), 255, dtype=np.uint8)
    tile = cv2.aruco.generateImageMarker(
        cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_1000), marker, 80
    )
    frame[y : y + 80, x : x + 80] = tile
    if duplicate:
        frame[180:260, 280:360] = tile
    return cv2.imencode(".jpg", frame)[1].tobytes()


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
