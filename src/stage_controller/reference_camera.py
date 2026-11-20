"""One bounded JPEG read through the camera owner; never open a camera device."""

import math
import time

from cameras import go2rtc

from .protocol import ControllerError

MAX_JPEG_BYTES = 4 * 1024 * 1024


class ReferenceCamera:
    def __init__(self, *, opener=None, clock=time.monotonic):
        self.opener = opener
        self.clock = clock

    def read(self, stream):
        from .config import NAME

        if not isinstance(stream, str) or not NAME.fullmatch(stream):
            raise ControllerError("Invalid reference stream name.")
        try:
            return go2rtc.read_jpeg(
                stream, timeout=2, max_bytes=MAX_JPEG_BYTES, opener=self.opener, clock=self.clock
            )
        except go2rtc.FrameError as exc:
            raise ControllerError(f"Reference camera: {exc}") from exc
        except (OSError, ValueError) as exc:
            raise ControllerError("Reference camera request failed.") from exc


def observe_marker(jpeg, reference):
    """Return the sole configured marker centre and a sanitized preview JPEG."""
    import cv2
    import numpy as np

    if (
        not isinstance(jpeg, bytes)
        or len(jpeg) > MAX_JPEG_BYTES
        or not jpeg.startswith(b"\xff\xd8")
    ):
        raise ControllerError("Reference camera must supply a bounded JPEG image.")
    # Inspect the JPEG dimensions before allowing the decoder to allocate an image.
    offset = 2
    dimensions = None
    while offset + 4 <= len(jpeg):
        if jpeg[offset] != 0xFF:
            break
        while offset < len(jpeg) and jpeg[offset] == 0xFF:
            offset += 1
        if offset >= len(jpeg):
            break
        marker = jpeg[offset]
        offset += 1
        if marker in (0xD9, 0xDA):
            break
        length = int.from_bytes(jpeg[offset : offset + 2], "big")
        if length < 2 or offset + length > len(jpeg):
            break
        if marker in (0xC0, 0xC1, 0xC2):
            if length < 8:
                break
            dimensions = (
                int.from_bytes(jpeg[offset + 3 : offset + 5], "big"),
                int.from_bytes(jpeg[offset + 5 : offset + 7], "big"),
            )
            break
        offset += length
    if (
        dimensions is None
        or any(not 1 <= size <= 8192 for size in dimensions)
        or math.prod(dimensions) > 16_000_000
    ):
        raise ControllerError("Reference JPEG dimensions are invalid or too large.")
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or tuple(image.shape[:2]) != dimensions:
        raise ControllerError("Cannot decode reference JPEG.")
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_1000))
    corners, ids, _ = detector.detectMarkers(image)
    matches = (
        []
        if ids is None
        else [
            corner
            for corner, identifier in zip(corners, ids.flatten())
            if int(identifier) == reference["marker"]
        ]
    )
    if len(matches) != 1:
        raise ControllerError("Reference marker is missing or duplicated.")
    pixel = [float(value) for value in matches[0].reshape(4, 2).mean(axis=0)]
    if not all(math.isfinite(value) for value in pixel):
        raise ControllerError("Reference marker has invalid coordinates.")
    ok, preview = cv2.imencode(".jpg", image)
    if not ok or len(preview) > MAX_JPEG_BYTES:
        raise ControllerError("Cannot encode reference preview.")
    return pixel, preview.tobytes()
