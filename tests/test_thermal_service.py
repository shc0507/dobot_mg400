"""Acquisition, radiometry, lifecycle and command-queue regression tests."""

import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from thermal_cam.service import CameraService, ThermalFrame


def wait_until(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out waiting for acquisition state")


def known_frame(sequence=1):
    ir = np.full((120, 160), 100, dtype=np.uint8)
    raw = np.full((120, 160), 19000, dtype=np.uint16)
    raw[5, 7] = 18000
    raw[9, 11] = 22000
    raw[60, 80] = 20000
    return ThermalFrame.from_arrays(ir, raw, sequence)


def test_temperature_units_extrema_center_and_copy_ownership():
    frame = known_frame()
    assert frame.temperatures_c[60, 80] == pytest.approx(20000 / 64 - 273.15)
    assert frame.stats["center"] == {"celsius": pytest.approx(39.35), "x": 80, "y": 60}
    assert frame.stats["min"] == {"celsius": pytest.approx(8.10), "x": 7, "y": 5}
    assert frame.stats["max"] == {"celsius": pytest.approx(70.60), "x": 11, "y": 9}
    assert frame.stats["mean_celsius"] == pytest.approx(frame.temperatures_c.mean())
    assert frame.stats["range_celsius"] == pytest.approx(62.5)
    assert not frame.raw.flags.writeable
    assert not frame.ir.flags.writeable
    assert not frame.temperatures_c.flags.writeable
    source_ir = np.full((120, 160), 90, dtype=np.uint8)
    source_raw = np.full((120, 160), 19000, dtype=np.uint16)
    snapshot = ThermalFrame.from_arrays(source_ir, source_raw, 9)
    source_ir[:] = 0
    source_raw[:] = 0
    assert snapshot.raw[0, 0] == 19000
    assert snapshot.ir[0, 0] == 90


@pytest.mark.parametrize("value", [0, 65535])
def test_uniform_invalid_sensor_values_are_rejected(value):
    with pytest.raises(ValueError, match="invalid thermal frame"):
        ThermalFrame.from_arrays(
            np.zeros((120, 160), dtype=np.uint8), np.full((120, 160), value, dtype=np.uint16), 1
        )


def test_npz_keeps_raw_ir_temperatures_and_metadata_from_one_frame():
    frame = known_frame(123)
    with np.load(io.BytesIO(frame.npz()), allow_pickle=False) as saved:
        np.testing.assert_array_equal(saved["raw"], frame.raw)
        np.testing.assert_array_equal(saved["ir"], frame.ir)
        np.testing.assert_array_equal(saved["temperatures_c"], frame.temperatures_c)
        metadata = json.loads(str(saved["metadata"]))
        assert metadata == frame.metadata()
        assert metadata["sequence"] == 123
        assert saved["raw"].dtype == np.uint16


def test_demo_start_stop_reconnect_and_no_stale_frame():
    service = CameraService(demo=True)
    try:
        assert service.status()["state"] == "disconnected"
        service.connect()
        wait_until(lambda: service.status()["state"] == "streaming")
        frame = service.latest()
        assert frame.source == "demo"
        assert frame.raw.shape == (120, 160)
        assert service.status()["device"]["firmware"] == "synthetic"
        assert service.command("gain", "low")["ok"]
        wait_until(lambda: service.status()["camera_stats"]["gain"] == "low")
        service.disconnect()
        with pytest.raises(RuntimeError, match="No camera frame"):
            service.latest()
        service.connect()
        wait_until(lambda: service.status()["state"] == "streaming")
        assert service.latest().sequence > frame.sequence
    finally:
        service.disconnect()


def test_constructor_failure_is_reported_as_error():
    def broken_factory():
        raise RuntimeError("constructor unavailable")

    service = CameraService(camera_factory=broken_factory)
    try:
        service.connect()
        wait_until(lambda: service.status()["state"] == "error")
        assert "constructor unavailable" in service.status()["error"]
        with pytest.raises(RuntimeError, match="constructor unavailable"):
            service.latest()
    finally:
        service.disconnect()


def test_disconnect_failure_invalidates_frame_and_rejects_queued_command():
    reading = threading.Event()
    unplug = threading.Event()
    closed = threading.Event()

    class FailingCamera:
        def __init__(self):
            self.reads = 0

        def open(self):
            return {"model": "test"}

        def read(self):
            self.reads += 1
            if self.reads == 1:
                frame = known_frame()
                return frame.ir, frame.raw
            reading.set()
            unplug.wait(3)
            raise RuntimeError("USB unplugged")

        def stats(self):
            return {}

        def close(self):
            closed.set()

        def shutter(self):
            raise AssertionError("Queued shutter must not run after unplug failure")

    service = CameraService(camera_factory=FailingCamera)
    try:
        service.connect()
        assert reading.wait(3)
        assert service.latest().sequence == 1
        with ThreadPoolExecutor(max_workers=1) as executor:
            command = executor.submit(service.command, "shutter")
            wait_until(lambda: not service._commands.empty())
            unplug.set()
            with pytest.raises(RuntimeError, match="USB unplugged"):
                command.result(timeout=3)
        assert closed.wait(3)
        assert service.status()["state"] == "error"
        assert service.status()["frame_age_s"] is None
        with pytest.raises(RuntimeError, match="USB unplugged"):
            service.latest()
    finally:
        unplug.set()
        service.disconnect()


def test_camera_commands_and_reads_have_one_worker_owner():
    threads = []

    class TrackingCamera:
        def open(self):
            threads.append(threading.get_ident())
            return {}

        def read(self):
            threads.append(threading.get_ident())
            time.sleep(0.005)
            frame = known_frame()
            return frame.ir, frame.raw

        def stats(self):
            return {}

        def set_gain(self, value):
            assert value == "low"
            threads.append(threading.get_ident())

        def shutter(self):
            threads.append(threading.get_ident())

        def close(self):
            threads.append(threading.get_ident())

    service = CameraService(camera_factory=TrackingCamera)
    try:
        service.connect()
        wait_until(lambda: service.status()["state"] == "streaming")
        assert service.command("gain", "low") == {"ok": True, "command": "gain"}
        assert service.command("shutter") == {"ok": True, "command": "shutter"}
    finally:
        service.disconnect()
    assert len(set(threads)) == 1
    assert threads[0] != threading.get_ident()


def test_stale_frame_is_never_returned(monkeypatch):
    service = CameraService()
    frame = known_frame()
    service._frame, service._state = frame, "streaming"
    monkeypatch.setattr("thermal_cam.service.time.monotonic", lambda: frame.monotonic_time + 4)
    with pytest.raises(RuntimeError, match="stale"):
        service.latest()


@pytest.mark.parametrize("name,argument", [("shutter", None), ("gain", "low")])
def test_control_hides_old_frame_until_new_validated_frame(name, argument):
    control_started = threading.Event()
    finish_control = threading.Event()
    settling_read = threading.Event()
    finish_settling = threading.Event()

    class GatedCamera:
        controlled = False

        def open(self):
            return {}

        def read(self):
            if self.controlled:
                settling_read.set()
                assert finish_settling.wait(3)
            time.sleep(0.005)
            frame = known_frame()
            return frame.ir, frame.raw

        def stats(self):
            return {}

        def shutter(self):
            self.controlled = True
            control_started.set()
            assert finish_control.wait(3)

        def set_gain(self, gain):
            assert gain == "low"
            self.shutter()

        def close(self):
            pass

    service = CameraService(camera_factory=GatedCamera)
    try:
        service.connect()
        wait_until(lambda: service.status()["state"] == "streaming")
        old_sequence = service.latest().sequence
        with ThreadPoolExecutor(max_workers=1) as executor:
            command = executor.submit(service.command, name, argument)
            assert control_started.wait(3)
            with pytest.raises(RuntimeError, match="No camera frame"):
                service.latest()
            assert service.status()["fps"] == 0
            assert service.status()["frame_age_s"] is None
            finish_control.set()
            assert command.result(timeout=3)["ok"]
            assert settling_read.wait(3)
            with pytest.raises(RuntimeError, match="No camera frame"):
                service.latest()
            finish_settling.set()
            wait_until(lambda: service.status()["frame_age_s"] is not None)
            assert service.latest().sequence > old_sequence
    finally:
        finish_control.set()
        finish_settling.set()
        service.disconnect()
