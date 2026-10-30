"""Protocol and cleanup tests; never open real USB hardware."""

from types import SimpleNamespace
import struct

import numpy as np
import pytest
import usb.core

from thermal_cam import camera


def packet(counter=7, cnt3=280, value=19000):
    config = camera.get_model_config("p1")
    pixels = np.full((config.frame_rows, config.sensor_w), value, dtype="<u2")
    pixels[:120, :] = 85
    return (
        struct.pack("<BBIIH", 12, 0x8D, counter, 0, cnt3)
        + pixels.tobytes()
        + struct.pack("<BBIIH", 12, 0x8F, counter, 0, cnt3)
    )


def test_split_markers_and_p1_pixel_planes():
    assembler = camera.FrameAssembler(camera.get_model_config("p1").frame_size)
    data = packet()
    assembler.feed(b"discarded\x0c")
    assembler.feed(data[1:15])
    assert assembler.pop() is None
    assembler.feed(data[15:-3])
    assert assembler.pop() is None
    assembler.feed(data[-3:])
    frame, count, cnt3 = assembler.pop()
    assert (count, cnt3) == (7, 280)
    ir, raw = camera.extract_both(frame, camera.get_model_config("p1"))
    assert ir.shape == raw.shape == (120, 160)
    assert ir.dtype == np.uint8
    assert raw.dtype == np.uint16
    assert np.all(ir == 85)
    assert np.all(raw == 19000)
    assert assembler.pop() is None


@pytest.mark.parametrize("corrupt_offset", [-12, -11, -10])
def test_rejects_bad_end_markers_then_resynchronizes(corrupt_offset):
    assembler = camera.FrameAssembler(camera.get_model_config("p1").frame_size)
    broken = bytearray(packet())
    broken[corrupt_offset] ^= 1
    assembler.feed(broken + packet(counter=8))
    frame, count, _ = assembler.pop()
    assert count == 8
    assert assembler.marker_mismatches == 1
    assert len(frame) == camera.get_model_config("p1").frame_size + 12


def test_corrupt_stream_has_bounded_memory():
    assembler = camera.FrameAssembler(camera.get_model_config("p1").frame_size)
    assembler.feed(bytes(assembler.packet_size * 10))
    assert len(assembler.buffer) <= 3 * assembler.packet_size
    assert assembler.pop() is None
    assert len(assembler.buffer) <= 1
    assembler.feed(packet())
    assert assembler.pop()[1] == 7


class FakeDevice:
    def __init__(self):
        self.alts = []

    def get_active_configuration(self):
        return [SimpleNamespace(bInterfaceNumber=0), SimpleNamespace(bInterfaceNumber=1)]

    def set_interface_altsetting(self, **kwargs):
        self.alts.append(kwargs)

    def is_kernel_driver_active(self, interface):
        return False


def test_failed_second_claim_releases_first_interface(monkeypatch):
    device = FakeDevice()
    released = []
    disposed = []
    monkeypatch.setattr(camera, "usb_backend", lambda: object())
    monkeypatch.setattr(camera.usb.core, "find", lambda **_: device)

    def claim(_device, interface):
        if interface == 1:
            raise usb.core.USBError("claim failed")

    monkeypatch.setattr(camera.usb.util, "claim_interface", claim)
    monkeypatch.setattr(camera.usb.util, "release_interface", lambda d, i: released.append(i))
    monkeypatch.setattr(camera.usb.util, "dispose_resources", disposed.append)
    driver = camera._P1USB(1)
    with pytest.raises(usb.core.USBError):
        driver.connect()
    assert released == [0]
    assert disposed == [device]
    assert driver.dev is None
    driver.disconnect()
    assert disposed == [device]


def test_read_deadline_on_usb_timeouts():
    class TimeoutDevice:
        def read(self, *_args, **_kwargs):
            raise usb.core.USBTimeoutError("timeout")

    driver = camera._P1USB(0.002)
    driver.dev = TimeoutDevice()
    driver.streaming = True
    with pytest.raises(camera.CameraTimeout):
        driver.read_frame()


def test_shutter_transition_frames_are_not_returned():
    class FrameDevice:
        def __init__(self):
            self.chunks = [packet(counter=index, cnt3=index * 40) for index in range(4)]

        def read(self, *_args, **_kwargs):
            return self.chunks.pop(0)

    driver = camera._P1USB(1)
    driver.dev = FrameDevice()
    driver.streaming = True
    driver._settling_frames = 3
    frame = driver.read_frame()
    assert struct.unpack_from("<I", frame, 2)[0] == 3
    assert driver.stats.frames_read == 1


def test_invalid_gain_and_timeout_fail_before_usb_access():
    with pytest.raises(ValueError):
        camera.P1Camera(frame_timeout=0)
    with pytest.raises(ValueError):
        camera.P1Camera().set_gain("auto")


def test_low_gain_rejected_before_any_device_command(monkeypatch):
    adapter = camera.P1Camera()
    commands = []
    monkeypatch.setattr(adapter._camera, "set_gain_mode", commands.append)
    with pytest.raises(ValueError, match="Low gain is disabled: temperature conversion has not been validated on the P1"):
        adapter.set_gain("low")
    assert commands == []


def test_reopening_same_camera_resets_parser_and_settling(monkeypatch):
    adapter = camera.P1Camera()
    driver = adapter._camera
    monkeypatch.setattr(driver, "connect", lambda: setattr(driver, "dev", object()))
    monkeypatch.setattr(driver, "init", lambda: ("P1 test", "test firmware"))
    monkeypatch.setattr(driver, "set_gain_mode", lambda _mode: None)
    monkeypatch.setattr(camera.P3Camera, "start_streaming", lambda self: setattr(self, "streaming", True))
    adapter.open()
    driver._settling_frames = 3
    driver.assembler.marker_mismatches = 6
    driver.assembler.discarded_bytes = 42
    driver.assembler.feed(b"partial frame")
    adapter.close()
    try:
        adapter.open()
        assert driver._settling_frames == 0
        assert driver.assembler.marker_mismatches == 0
        assert driver.assembler.discarded_bytes == 0
        assert not driver.assembler.buffer
    finally:
        adapter.close()


def test_device_metadata_respects_nul_termination(monkeypatch):
    adapter = camera.P1Camera()
    driver = adapter._camera
    calls = []

    def connect():
        calls.append("connect")
        driver.dev = object()

    def init():
        calls.append("init")
        return "P1\x0000.00.04", "00.00.00.04\x00old response"

    monkeypatch.setattr(driver, "connect", connect)
    monkeypatch.setattr(driver, "init", init)
    monkeypatch.setattr(driver, "set_gain_mode", lambda mode: calls.append(("gain", mode)))
    monkeypatch.setattr(driver, "start_streaming", lambda: calls.append("start"))
    monkeypatch.setattr(driver, "disconnect", lambda: calls.append("close"))
    try:
        info = adapter.open()
        assert info["name"] == "P1"
        assert info["firmware"] == "00.00.00.04"
        assert calls == ["connect", "init", ("gain", camera.GainMode.HIGH), "start"]
        assert adapter.open() == info
        assert len(calls) == 4
    finally:
        adapter.close()
    assert calls[-1] == "close"
