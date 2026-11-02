"""Microscope contract lifecycle with explicit reference and motion authority.

Construction is inert. Real devices open only in start(), after measured settings
have been validated. Demo transports use the MIC-01 simulator and generated images.
"""

import copy
import math
import os
import queue
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from types import SimpleNamespace

import cv2
import numpy as np

from labkit.describe import command, describe, parameter
from labkit.errors import ServiceError
from labkit.events import EventLog
from labkit.lease import Lease

from .aruco_locator import REFERENCES, Locator
from .aruco_switch import OBJECTIVE_ORDER, ArucoObjectiveSwitcher, SwitchSettings, select_objective
from .autofocus import AutofocusCancelled, AutoFocuser
from .motors import CommandRejected, MotorController
from .paths import calibration_path, private_open, run_path
from .side_capture import SideCaptureCancelled
from .sim import MicroscopeSimulator


@dataclass(frozen=True)
class MicroscopeConfig:
    serial: str = ""
    side_stream: str = ""
    focus_limits: tuple[int, int] | None = None
    switch_focus: int | None = None
    home_switch: bool | None = None
    lease_seconds: float = 3.0
    focus_speed: int = 800

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        limits = env.get("MG400_MICROSCOPE_FOCUS_LIMITS", "")
        clearance = env.get("MG400_MICROSCOPE_SWITCH_FOCUS", "")
        switch = env.get("MG400_MICROSCOPE_HOME_SWITCH", "")
        if limits and not re.fullmatch(r"-?\d+,-?\d+", limits):
            raise ValueError("MG400_MICROSCOPE_FOCUS_LIMITS must be MIN,MAX integer pulses")
        if clearance and not re.fullmatch(r"-?\d+", clearance):
            raise ValueError("MG400_MICROSCOPE_SWITCH_FOCUS must be integer pulses")
        if switch not in ("", "0", "1"):
            raise ValueError("MG400_MICROSCOPE_HOME_SWITCH must be 0 or 1")
        return cls(
            serial=env.get("MG400_MICROSCOPE_SERIAL", ""),
            side_stream=env.get("MG400_MICROSCOPE_SIDE_STREAM", ""),
            focus_limits=tuple(map(int, limits.split(","))) if limits else None,
            switch_focus=int(clearance) if clearance else None,
            home_switch=None if not switch else switch == "1",
        )

    def validate(self):
        if (
            self.focus_limits is None
            or len(self.focus_limits) != 2
            or any(
                type(value) is not int or not -(2**31) <= value < 2**31
                for value in self.focus_limits
            )
            or not self.focus_limits[0] <= 0 <= self.focus_limits[1]
            or self.focus_limits[0] == self.focus_limits[1]
        ):
            raise ValueError("Measured focus limits must include zero and have nonzero travel")
        if (
            type(self.switch_focus) is not int
            or not self.focus_limits[0] <= self.switch_focus <= self.focus_limits[1]
            or type(self.home_switch) is not bool
        ):
            raise ValueError("Set measured clearance and whether a focus home switch exists")
        if not self.home_switch and self.switch_focus != 0:
            raise ValueError("Manual focus zero is at clearance; SWITCH_FOCUS must be 0")
        if not math.isfinite(self.lease_seconds) or self.lease_seconds <= 0:
            raise ValueError("Invalid control lease duration")
        if type(self.focus_speed) is not int or not 1 <= self.focus_speed <= 10000:
            raise ValueError("Invalid focus speed")
        return self


class _ContractLease:
    """Contract acquisition revokes manual classification under the same lease lock."""

    def __init__(self, service):
        self.service = service
        self.raw = service._lease

    @property
    def on_expire(self):
        return self.raw.on_expire

    @on_expire.setter
    def on_expire(self, value):
        self.raw.on_expire = value

    def acquire(self, client):
        with self.raw.control(client, require_holder=False):
            self.service._expire_handoff_locked()
            value = self.raw.acquire(client)
            self.service._expire_handoff_locked()
            self.service._renew_job_authority(client)
            with self.service._lock:
                self.service._handoff_deadline = None
                self.service._manual_holder = None
            return value

    def renew(self, client):
        with self.raw.control(client):
            value = self.raw.renew(client)
            self.service._renew_job_authority(client)
            self.service._manual_holder = None
            return value

    def release(self, client):
        with self.raw.control(client, require_holder=False):
            self.service._expire_handoff_locked()
            sampled_at = self.service._clock()
            lease_state = self.raw.snapshot()
            if lease_state["holder"] == client:
                self.service._prepare_idle_handoff(sampled_at + lease_state["remaining_s"])
            value = self.raw.release(client)
            self.service._manual_holder = None
            return value

    def control(self, client, *, require_holder=True):
        return self.raw.control(client, require_holder=require_holder)

    def poll(self):
        expired = self.raw.poll()
        with self.raw.control("reference-handoff", require_holder=False):
            return self.service._expire_handoff_locked() or expired

    def snapshot(self):
        return self.raw.snapshot()


class _AuthorityCancel:
    """An irreversible cancellation flag that also expires at the job lease deadline.

    Motor dispatch checks this token without acquiring service or lease locks.
    A heartbeat can extend a live token; an expired or stopped token stays revoked.
    """

    def __init__(self, clock, deadline):
        self._clock, self._deadline = clock, deadline
        self._event = threading.Event()
        self._lock = threading.Lock()

    def set(self):
        self._event.set()

    def is_set(self):
        with self._lock:
            if self._clock() >= self._deadline:
                self._event.set()
            return self._event.is_set()

    def renew(self, deadline):
        with self._lock:
            if self._clock() >= self._deadline:
                self._event.set()
            if not self._event.is_set():
                self._deadline = deadline

    def wait(self, timeout=None):
        end = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if end is None else end - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self._event.wait(0.05 if remaining is None else min(0.05, remaining))
        return True


@dataclass
class _Job:
    ident: str
    action: str
    client: str
    args: dict
    cancel: _AuthorityCancel
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class _DemoSerial:
    """In-memory firmware transport. Its reader advances only synthetic time."""

    def __init__(self, *, home_switch=False):
        self.sim = MicroscopeSimulator(
            home_switch=home_switch,
            home_direction=-1 if home_switch else 0,
            home_max_steps=100 if home_switch else 0,
        )
        self.is_open = True
        self.lock = threading.RLock()
        self.replies = queue.Queue()
        self.commands = []

    def emit(self, lines):
        for line in lines:
            self.replies.put((line + "\n").encode("ascii"))

    def write(self, payload):
        with self.lock:
            text = payload.decode("ascii").strip()
            self.commands.append(text)
            self.emit(self.sim.command(text))
        return len(payload)

    def readline(self, size=513):
        try:
            return self.replies.get(timeout=0.002)
        except queue.Empty:
            with self.lock:
                if self.sim.homing:
                    self.sim.set_home_switch(True)
                self.emit(self.sim.advance(0.025))
            return b""

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False
        self.replies.put(b"")


class _DemoCamera:
    def __init__(self, port, *, side=False):
        self.port, self.side = port, side
        self.is_open = False
        self.stabilization_enabled = False
        self._serial = 0
        self._texture = None
        self.sdk_camera = SimpleNamespace(get_AutoExpoEnable=lambda: False)

    def open(self):
        self._texture = np.random.default_rng(8).integers(0, 256, (200, 200), dtype=np.uint8)
        self.is_open = True
        return self

    def close(self):
        self.is_open = False

    def set_stabilization(self, value):
        self.stabilization_enabled = bool(value)

    def set_auto_exposure(self, _value):
        pass

    def get_frame_packet(self, after_id=None, wait=True, timeout=2.0, copy=True, raw=False):
        if not self.is_open:
            raise RuntimeError("Demo camera is closed")
        with self.port.lock:
            position = (
                self.port.sim.objective.position if self.side else self.port.sim.focus.position
            )
        if self.side:
            ident = round(position / 740) % 4
            offset = round((position - round(position / 740) * 740) / 8)
            frame = np.full((200, 200), 255, np.uint8)
            marker = cv2.aruco.generateImageMarker(
                cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), ident, 80
            )
            x = max(1, min(119, 60 - offset))
            frame[60:140, x : x + 80] = marker
            captured_at = time.monotonic()
        else:
            sigma = min(8.0, abs(position - 12) / 12)
            frame = (
                self._texture.copy()
                if sigma < 0.1
                else cv2.GaussianBlur(self._texture, (31, 31), sigma)
            )
            captured_at = time.time()
        self._serial += 1
        return self._serial, captured_at, cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)


@dataclass(frozen=True)
class PreviewFrame:
    frame_id: int
    captured_at: float
    jpeg: bytes


class MicroscopeService:
    def __init__(
        self,
        config=None,
        *,
        demo=False,
        motor=None,
        camera=None,
        side_camera=None,
        clock=time.monotonic,
        wall_clock=time.time,
        autofocus_factory=AutoFocuser,
        switcher_factory=ArucoObjectiveSwitcher,
    ):
        config = config or MicroscopeConfig()
        self.demo = bool(demo)
        self.config = (
            replace(
                config,
                serial="/dev/serial/by-id/offline-microscope",
                side_stream="demo",
                focus_limits=(-200, 200),
                switch_focus=0,
                home_switch=bool(config.home_switch),
            )
            if demo
            else config
        )
        self._motor, self._camera, self._side = motor, camera, side_camera
        self._demo_port = None
        self._autofocus_factory, self._switcher_factory = autofocus_factory, switcher_factory
        self._clock, self._wall_clock = clock, wall_clock
        self._lock = threading.RLock()
        self._lifecycle = threading.RLock()
        self._frame_condition = threading.Condition(self._lock)
        self._frame_reads = 0
        self._panel_info_lock = threading.Lock()
        self._panel_info_cache = None
        self._lease = Lease(self.config.lease_seconds, clock=clock, on_expire=self.on_lease_expired)
        self.lease = _ContractLease(self)
        self._manual_holder = None
        self._handoff_deadline = None
        self.events = EventLog()
        self._quit = threading.Event()
        self._started = self._closed = self._starting = False
        self._shutdown_complete = False
        self._poller = None
        self._job = None
        self._stopping = False
        self._stop_done = threading.Event()
        self._stop_done.set()
        self._stop_outcome = "confirmed"
        self._stop_thread = None
        self._seq = 0
        self._fault = None
        self._reference = None
        self._status = None
        self._objective_id = None
        self._last_command = self._last_result = None
        self.expiry_count = 0

    def describe(self):
        specifications = {
            "select_objective": {"id": parameter("integer", enum=list(OBJECTIVE_ORDER))},
            "autofocus": {"mode": parameter("string", enum=["full", "local"])},
            "focus_move": {"steps": parameter("integer", minimum=-(2**31), maximum=2**31 - 1)},
            "home_focus": {},
            "snapshot": {"path": parameter("string", minLength=1)},
        }
        return describe(
            service="microscope",
            version="1",
            demo=self.demo,
            zones=["furnace-window"],
            lease_seconds=self.config.lease_seconds,
            on_lease_expiry="Cancel work, STOP ALL, disable both motors, and lose focus zero.",
            commands={
                name: command(
                    parameter(
                        "object",
                        properties=properties,
                        required=[] if name == "snapshot" else list(properties),
                        additionalProperties=False,
                    )
                )
                for name, properties in specifications.items()
            },
        )

    def snapshot(self):
        self._sync_motor_fault()
        lease_state = self._lease.snapshot()
        with self._lock:
            status = self._status
            if (
                self._reference is not None
                and self._motor is not None
                and self._reference != (self._motor.session, self._motor.reference_epoch)
            ):
                self._reference = None
            known = self._reference is not None
            return dict(
                service="microscope",
                seq=self._seq,
                ts_unix=self._wall_clock(),
                demo=self.demo,
                connected=bool(self._started and self._motor and self._motor.connected),
                busy=self._job is not None or self._stopping,
                locked=self._fault is not None,
                fault=copy.deepcopy(self._fault),
                lease=lease_state,
                last_command=copy.deepcopy(self._last_command),
                objective_id=self._objective_id,
                focus_position_pulses=status.focus.position if known and status else None,
                focus_zero_known=known,
                focus_limits_pulses=list(self.config.focus_limits or ()),
                cameras={
                    "microscope": bool(self._camera and self._camera.is_open),
                    "side": bool(self._side and self._side.is_open),
                },
                last_result=copy.deepcopy(self._last_result),
                manual_client=self._manual_holder,
            )

    def panel_info(self):
        """Load fixed calibration metadata once, without opening a camera or moving."""
        with self._lock:
            if not self._started or self._closed:
                raise ServiceError(503, "Microscope service is closed")
            if self._panel_info_cache is not None:
                return copy.deepcopy(self._panel_info_cache)
        if not self._panel_info_lock.acquire(blocking=False):
            raise ServiceError(503, "Microscope calibration metadata is loading")
        try:
            with self._lock:
                if not self._started or self._closed:
                    raise ServiceError(503, "Microscope service is closed")
                if self._panel_info_cache is not None:
                    return copy.deepcopy(self._panel_info_cache)
            try:
                if not self.demo:
                    # The pinned locator consumes four PNG references. Bound their
                    # declared dimensions before OpenCV allocates decoded images.
                    for name in REFERENCES.values():
                        path = calibration_path("side_camera_images") / name
                        if (
                            path.is_symlink()
                            or not path.is_file()
                            or path.stat().st_size > 64 * 1024 * 1024
                        ):
                            raise ValueError("A calibration reference is missing or invalid")
                        with path.open("rb") as stream:
                            header = stream.read(24)
                        if (
                            len(header) != 24
                            or header[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                        ):
                            raise ValueError("Calibration references must be bounded PNG images")
                        width, height = (
                            int.from_bytes(header[16:20], "big"),
                            int.from_bytes(header[20:24], "big"),
                        )
                        if not width or not height or width * height > 16_000_000:
                            raise ValueError(
                                "Calibration reference dimensions exceed the image limit"
                            )
                identifiers = sorted(set(self._locator().references) & set(OBJECTIVE_ORDER))
                if not identifiers:
                    raise ValueError("No calibrated objectives are available")
                error = None
            except Exception as problem:
                identifiers, error = [], str(problem)
            value = dict(
                objective_ids=identifiers,
                calibration_available=bool(identifiers),
                calibration_error=error,
                home_switch=self.config.home_switch,
            )
            with self._lock:
                if self._closed or not self._started:
                    raise ServiceError(503, "Microscope service closed during metadata loading")
                self._panel_info_cache = value
                return copy.deepcopy(value)
        finally:
            self._panel_info_lock.release()

    def read_frame(self, camera, *, after_id=None, timeout=0.5):
        """Read and encode one bounded copy from an already owned camera.

        Frame IDs and timestamp domains are unchanged: the main camera uses its
        capture wall time and the side camera uses its monotonic receive time.
        """
        if not isinstance(camera, str) or camera not in ("microscope", "side"):
            raise ValueError("Unknown microscope camera")
        if after_id is not None and (type(after_id) is not int or not 0 <= after_id < 2**63):
            raise ValueError("after_id must be a nonnegative integer")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 <= timeout <= 0.5
        ):
            raise ValueError("Frame timeout must be within 0..0.5 seconds")
        with self._frame_condition:
            if self._closed or not self._started:
                raise ServiceError(503, "Microscope service is closed")
            owner = self._camera if camera == "microscope" else self._side
            if owner is None or not owner.is_open:
                raise ServiceError(503, "Microscope camera is unavailable")
            if self._frame_reads >= 4:
                raise ServiceError(503, "Microscope frame readers are busy")
            self._frame_reads += 1
        try:
            packet = owner.get_frame_packet(
                after_id=after_id, wait=timeout > 0, timeout=timeout, copy=True
            )
            with self._frame_condition:
                if self._closed or not self._started:
                    raise ServiceError(503, "Microscope service closed during frame read")
            if packet is None:
                return None
            frame_id, captured_at, pixels = packet
            if (
                type(frame_id) is not int
                or frame_id < 0
                or isinstance(captured_at, bool)
                or not isinstance(captured_at, (int, float))
                or not math.isfinite(captured_at)
                or not isinstance(pixels, np.ndarray)
                or pixels.dtype != np.uint8
                or pixels.ndim not in (2, 3)
                or not pixels.shape[0]
                or not pixels.shape[1]
                or pixels.shape[0] * pixels.shape[1] > 16_000_000
                or (pixels.ndim == 3 and pixels.shape[2] not in (1, 3, 4))
            ):
                raise ServiceError(503, "Microscope camera returned an invalid frame")
            if after_id is not None and frame_id <= after_id:
                return None
            ok, encoded = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok or encoded.nbytes > 16_777_216:
                raise ServiceError(503, "Microscope preview exceeds the frame limit")
            jpeg = encoded.tobytes()
            with self._frame_condition:
                if self._closed or not self._started:
                    raise ServiceError(503, "Microscope service closed during frame read")
                return PreviewFrame(frame_id, float(captured_at), jpeg)
        except ServiceError:
            raise
        except Exception as error:
            raise ServiceError(503, "Microscope camera read failed") from error
        finally:
            with self._frame_condition:
                self._frame_reads -= 1
                self._frame_condition.notify_all()

    def start(self, *, background=True):
        with self._lifecycle:
            with self._lock:
                if self._closed:
                    raise RuntimeError("Microscope service is closed")
                if self._started:
                    return self
                self._starting = True
            try:
                self.config.validate()
                if self.demo and self._motor is None:
                    self._demo_port = _DemoSerial(home_switch=self.config.home_switch)
                    self._motor = MotorController(serial_factory=lambda **_: self._demo_port)
                    self._camera = self._camera or _DemoCamera(self._demo_port)
                    self._side = self._side or _DemoCamera(self._demo_port, side=True)
                if self._motor is None:
                    self._motor = MotorController()
                if self._camera is None:
                    from toupcam_camera import MicroscopeCamera

                    self._camera = MicroscopeCamera()
                if self._side is None:
                    from cameras.uvc import USBCamera

                    self._side = USBCamera(self.config.side_stream)
                self._motor.connect(self.config.serial, reset_seconds=0 if self.demo else 1.8)
                if not self._motor.focus_completion:
                    raise RuntimeError("Microscope service requires reference-aware firmware")
                self._motor.safe_stop_disable()
                self._motor.set_microstep(4)
                self._status = self._motor.read_status()
                self._camera.open()
                self._side.open()
                with self._lock:
                    if self._closed or self._quit.is_set():
                        raise RuntimeError("Microscope startup was cancelled")
                    self._started = True
                    self._seq += 1
                if background:
                    thread = threading.Thread(
                        target=self._poll, name="microscope-lease", daemon=True
                    )
                    thread.start()
                    self._poller = thread
            except BaseException:
                self.close()
                raise
            finally:
                self._starting = False
        return self

    def _poll(self):
        while not self._quit.wait(0.05):
            try:
                self.lease.poll()
                if self._motor is not None and self._motor.fault:
                    if self._latch_fault("motor", self._motor.fault):
                        self.stop("motor-monitor", 0)
            except Exception as error:
                if self._latch_fault("monitor", str(error)):
                    self.stop("motor-monitor", 0)

    def _sync_motor_fault(self):
        if self._motor is not None and self._motor.fault:
            if self._latch_fault("motor", self._motor.fault):
                self.stop("motor-monitor", 0)

    def _latch_fault(self, code, message):
        with self._lock:
            first = self._fault is None
            self._fault = self._fault or {"code": code, "message": str(message)}
            self._reference = None
            self._seq += 1
            if self._job is not None:
                self._job.cancel.set()
            return first

    @contextmanager
    def _manual_control(self, client, *, renew_only=False):
        with self._lease.control(client, require_holder=False):
            self._expire_handoff_locked()
            holder = self._lease.snapshot()["holder"]
            if holder is not None and (holder != client or self._manual_holder != client):
                raise ServiceError(409, "Automation or another operator holds control")
            if renew_only:
                if holder != client or self._manual_holder != client:
                    raise ServiceError(409, "Manual control expired; acquire it explicitly")
                self._lease.renew(client)
            else:
                self._lease.acquire(client)
                self._expire_handoff_locked()
                self._manual_holder = client
            self._renew_job_authority(client)
            with self._lock:
                self._handoff_deadline = None
            yield

    def _lease_deadline(self):
        sampled_at = self._clock()
        return sampled_at + self._lease.snapshot()["remaining_s"]

    def _renew_job_authority(self, client):
        deadline = self._lease_deadline()
        with self._lock:
            if self._job is not None and self._job.client == client:
                self._job.cancel.renew(deadline)

    def _expire_handoff_locked(self):
        # The caller holds the raw lease lock, so acquisition cannot revive a
        # reference after its original deadline even before the poller runs.
        with self._lock:
            if self._handoff_deadline is None or self._clock() < self._handoff_deadline:
                return False
            self._handoff_deadline = None
        self.on_lease_expired()
        return True

    def _prepare_idle_handoff(self, deadline):
        # The caller anchors this absolute deadline before sampling the lease.
        # Neither scheduling delay nor a status read can extend its authority.
        with self._lock:
            candidate = self._job is None and not self._stopping and not self._fault
            reference = self._reference
        if candidate and reference is not None:
            try:
                status = self._motor.read_status(timeout=1.0)
                with self._lock:
                    valid = (
                        self._clock() < deadline
                        and self._job is None
                        and not self._stopping
                        and not self._fault
                        and self._reference == reference
                        and reference == (status.session, status.reference_epoch)
                        and status.microstep == 4
                        and status.zeroed
                        and status.limited
                        and (status.minimum, status.maximum) == self.config.focus_limits
                        and not status.homing
                        and not status.objective.moving
                        and not status.focus.moving
                        and status.focus.enabled
                        and status.minimum <= status.focus.position <= status.maximum
                    )
                    if valid:
                        self._status = status
                        self._handoff_deadline = deadline
                        return
            except Exception as error:
                self._latch_fault("reference", str(error))
        self.stop("lease-released", 0)

    def manual_heartbeat(self, client, *, renew_only=False):
        with self._manual_control(client, renew_only=renew_only):
            return self._lease.snapshot()

    def manual_release(self, client):
        with self._lease.control(client, require_holder=False):
            self._expire_handoff_locked()
            holder = self._lease.snapshot()["holder"]
            if holder is not None and (holder != client or self._manual_holder != client):
                raise ServiceError(409, "Automation or another operator holds control")
            if holder == client:
                sampled_at = self._clock()
                lease_state = self._lease.snapshot()
                self._prepare_idle_handoff(sampled_at + lease_state["remaining_s"])
            value = self._lease.release(client)
            self._manual_holder = None
            return value

    def manual_command(self, action, args, client):
        if (
            action != "set_focus_zero"
            or not isinstance(args, dict)
            or set(args) != {"confirm"}
            or args["confirm"] is not True
        ):
            raise ValueError("Only confirmed manual focus zero is supported")
        with self._manual_control(client, renew_only=True):
            return self._dispatch(action, {}, client, manual=True)

    def command(self, action, args, client):
        with self._lease.control(client):
            self._manual_holder = None
            if action == "set_focus_zero":
                raise ServiceError(403, "Only the operator panel can establish manual focus zero")
            return self._dispatch(action, args, client)

    def _arguments(self, action, args):
        if not isinstance(args, dict) or (
            action not in self.describe()["commands"] and action != "set_focus_zero"
        ):
            raise ValueError("Unknown microscope command or parameters")
        required = {"select_objective": {"id"}, "autofocus": {"mode"}, "focus_move": {"steps"}}
        allowed = required.get(action, {"path"} if action == "snapshot" else set())
        if set(args) - allowed or not required.get(action, set()) <= set(args):
            raise ValueError("Unexpected or missing microscope command parameters")
        if action == "select_objective" and (
            type(args["id"]) is not int or args["id"] not in OBJECTIVE_ORDER
        ):
            raise ValueError("Unknown objective ID")
        if action == "autofocus" and args["mode"] not in ("full", "local"):
            raise ValueError("Autofocus mode must be full or local")
        if action == "focus_move" and (
            type(args["steps"]) is not int or not -(2**31) <= args["steps"] < 2**31
        ):
            raise ValueError("Focus steps must be a signed 32-bit integer")
        if action == "snapshot" and "path" in args:
            if not isinstance(args["path"], str) or not args["path"] or len(args["path"]) > 4096:
                raise ValueError("Snapshot path must be a nonempty bounded string")
            run_path(args["path"])

    def _dispatch(self, action, args, client, *, manual=False):
        self._arguments(action, args)
        self._sync_motor_fault()
        deadline = self._lease_deadline()
        with self._lock:
            if not self._started or self._closed:
                raise ServiceError(503, "Microscope is not connected")
            if self._fault:
                raise ServiceError(409, "Microscope is locked; only stop is allowed")
            if self._job is not None or self._stopping:
                raise ServiceError(409, "Microscope is busy")
            if (
                action in ("focus_move", "select_objective", "autofocus")
                and self._reference is None
            ):
                raise ServiceError(409, "focus position unknown")
            if action == "home_focus" and not self.config.home_switch:
                raise ServiceError(409, "No configured focus home switch")
            if action == "set_focus_zero" and (not manual or self.config.home_switch):
                raise ServiceError(403, "Manual zero requires an operator and no home switch")
            job = _Job(
                uuid.uuid4().hex,
                action,
                client,
                copy.deepcopy(args),
                _AuthorityCancel(self._clock, deadline),
            )
            self._job = job
            self._seq += 1
            thread = threading.Thread(
                target=self._run_job, args=(job,), name="microscope-command", daemon=True
            )
            job.thread = thread
            try:
                thread.start()
            except BaseException as error:
                job.cancel.set()
                self._job = None
                self._seq += 1
                job.done.set()
                self._latch_fault("worker", "Microscope command worker could not start")
                self.stop("worker-start-failed", 0)
                if isinstance(error, Exception):
                    raise ServiceError(503, "Microscope command worker could not start") from error
                raise
            return job.ident

    def _check_job_locked(self, job):
        if job is not self._job or job.cancel.is_set() or self._closed or self._stopping:
            raise SideCaptureCancelled("Microscope command was stopped")
        if self._fault:
            raise RuntimeError(self._fault["message"])

    def _check_job(self, job):
        try:
            with self._lease.control(job.client):
                with self._lock:
                    self._check_job_locked(job)
        except ServiceError as error:
            job.cancel.set()
            raise SideCaptureCancelled("Microscope control lease was revoked") from error

    def _fresh_status(self, job=None):
        if job is not None:
            self._check_job(job)
        status = self._motor.read_status(cancel_event=job.cancel if job is not None else None)
        if job is not None:
            self._check_job(job)
        with self._lock:
            self._status = status
            self._seq += 1
        return status

    def _guard_reference(self, job, *, clearance=False):
        status = self._fresh_status(job)
        with self._lock:
            valid = (
                self._reference == (status.session, status.reference_epoch)
                and status.microstep == 4
                and status.zeroed
                and status.limited
                and (status.minimum, status.maximum) == self.config.focus_limits
                and not status.homing
                and status.focus.enabled
                and status.minimum <= status.focus.position <= status.maximum
            )
            if not valid:
                self._reference = None
                raise RuntimeError("focus position unknown or reference changed")
            if clearance and (
                status.focus.moving or status.focus.position != self.config.switch_focus
            ):
                raise RuntimeError("Focus is not stationary at the verified clearance position")
        return status

    def _reference_focus(self, job):
        self._check_job(job)
        status = self._fresh_status(job)
        if status.objective.moving or status.focus.moving:
            raise RuntimeError("Stop both axes before establishing focus zero")
        self._motor.set_microstep(4, cancel_event=job.cancel)
        self._motor.focus_enable(cancel_event=job.cancel)
        if job.action == "home_focus":
            self._motor.focus_home(cancel_event=job.cancel)
        else:
            self._motor.focus_zero(cancel_event=job.cancel)
        self._motor.focus_limits(*self.config.focus_limits, cancel_event=job.cancel)
        status = self._fresh_status(job)
        if not (
            status.microstep == 4
            and status.zeroed
            and status.limited
            and not status.homing
            and status.focus.position == 0
            and not status.focus.moving
            and (status.minimum, status.maximum) == self.config.focus_limits
        ):
            raise RuntimeError("Firmware did not confirm focus zero and limits")
        with self._lease.control(job.client):
            with self._lock:
                self._check_job_locked(job)
                self._reference = (status.session, status.reference_epoch)
        return {"focus_position_pulses": 0, "focus_zero_known": True}

    def _locator(self):
        if not self.demo:
            return Locator()
        locator = object.__new__(Locator)
        locator.near_px = 50.0
        locator.references = {
            ident: {"center": [99.5, 99.5], "shape": [200, 200]} for ident in OBJECTIVE_ORDER
        }
        return locator

    def _select(self, job, motor):
        locator = self._locator()
        packet = self._side.get_frame_packet(timeout=2.0)
        self._check_job(job)
        if packet is None:
            raise RuntimeError("No fresh side-camera frame")
        measurement = locator.locate(packet[2], only_target=False)
        current = select_objective(measurement["markers"])["id"]
        target = job.args["id"]
        completed = []
        for _ in range(3):
            if current == target:
                break
            position = self._guard_reference(job).focus.position
            motor.focus_move(self.config.switch_focus - position)
            self._guard_reference(job, clearance=True)
            forward = (OBJECTIVE_ORDER.index(target) - OBJECTIVE_ORDER.index(current)) % len(
                OBJECTIVE_ORDER
            )
            direction = 1 if forward <= 2 else -1
            settings = SwitchSettings()
            if self.demo:
                settings = replace(
                    settings,
                    enable_settle_seconds=0,
                    release_settle_seconds=0,
                    move_settle_seconds=0,
                )
            switcher = self._switcher_factory(
                self._side,
                motor,
                locator=locator,
                settings=settings,
                cancel_event=job.cancel,
                before_move=lambda: self._guard_reference(job, clearance=True),
            )
            result = switcher.run(
                direction, f"microscope/{job.ident}-switch-{len(completed)}.csv", source_id=current
            )
            self._check_job(job)
            expected = OBJECTIVE_ORDER[
                (OBJECTIVE_ORDER.index(current) + direction) % len(OBJECTIVE_ORDER)
            ]
            if result.target_id != expected:
                raise RuntimeError("Objective switch returned an unexpected identity")
            current = result.target_id
            item = asdict(result)
            item["log_path"] = str(item["log_path"])
            completed.append(item)
        if current != target:
            raise RuntimeError("Requested objective was not confirmed")
        with self._lease.control(job.client):
            with self._lock:
                self._check_job_locked(job)
                self._objective_id = current
        return {"objective_id": current, "switches": completed}

    def _autofocus(self, job, motor):
        status = self._guard_reference(job)
        half_range = (status.maximum - status.minimum) // 2
        if job.args["mode"] == "local":
            half_range = min(half_range, 1500)
        if half_range < 1:
            raise ValueError("No two-sided autofocus window fits inside the focus limits")
        center = status.minimum + half_range
        if job.args["mode"] == "local":
            center = max(center, min(status.focus.position, status.maximum - half_range))
        # The preserved algorithm scans symmetrically around its starting point.
        # Place that window inside measured bounds, including clearance at an end.
        if center != status.focus.position:
            motor.focus_move(center - status.focus.position)
        self._guard_reference(job)
        options = dict(speed=self.config.focus_speed, cancel_event=job.cancel)
        if self.demo:
            options.update(
                settle_seconds=0,
                precision_settle_seconds=0,
                frames_per_position=1,
                precision_frames_per_position=1,
                backlash_steps=min(200, 2 * half_range),
            )
        focuser = self._autofocus_factory(self._camera, motor, **options)
        if job.args["mode"] == "local" or half_range < 3:
            result = focuser.run_local(
                half_range=half_range, step=min(100, half_range), lock_exposure=not self.demo
            )
        else:
            fine = min(100, max(2, half_range // 4))
            result = focuser.run(
                half_range=half_range,
                coarse_step=max(fine + 1, min(1000, half_range)),
                fine_step=fine,
                ultrafine_step=min(10, fine - 1),
                lock_exposure=not self.demo,
            )
        self._check_job(job)
        status = self._guard_reference(job)
        return {"measurement": asdict(result), "focus_position_pulses": status.focus.position}

    def _save_snapshot(self, job):
        first = self._camera.get_frame_packet(wait=False)
        packet = self._camera.get_frame_packet(after_id=first[0] if first else None, timeout=2.0)
        self._check_job(job)
        if packet is None:
            raise RuntimeError("No fresh microscope frame")
        destination = run_path(job.args.get("path", f"microscope/{job.ident}.png"))
        ok, encoded = cv2.imencode(".png", packet[2])
        if not ok:
            raise RuntimeError("Microscope snapshot could not be encoded")
        self._check_job(job)
        with private_open(destination, "xb") as output:
            output.write(encoded.tobytes())
        self._check_job(job)
        return {"path": str(destination), "frame_id": packet[0], "captured_at": packet[1]}

    def _run_job(self, job):
        result, outcome, message = None, "ok", ""
        try:
            motor = _GuardedMotor(self, job)
            if job.action in ("set_focus_zero", "home_focus"):
                result = self._reference_focus(job)
            elif job.action == "focus_move":
                motor.focus_move(job.args["steps"])
                result = {"focus_position_pulses": self._guard_reference(job).focus.position}
            elif job.action == "select_objective":
                result = self._select(job, motor)
            elif job.action == "autofocus":
                result = self._autofocus(job, motor)
            elif job.action == "snapshot":
                result = self._save_snapshot(job)
            self._check_job(job)
        except (SideCaptureCancelled, AutofocusCancelled) as error:
            outcome, message = "stopped", str(error)
            self.stop("command-cancel", 0)
        except (ValueError, CommandRejected) as error:
            outcome, message = "failed", str(error)
            self.stop("command-refused", 0)
        except Exception as error:
            outcome, message = "failed", str(error)
            if not job.cancel.is_set():
                self._latch_fault("command", message)
            else:
                outcome = "stopped"
            self.stop("command-failure", 0)
        finally:
            with self._lock:
                if job.cancel.is_set() and outcome == "ok":
                    self.stop("completion-authority-expired", 0)
                    outcome, message, result = "stopped", "Microscope command was stopped", None
                if job is self._job:
                    self._last_command = dict(
                        id=job.ident,
                        action=job.action,
                        client=job.client,
                        result=outcome,
                        message=message,
                        finished_at=self._wall_clock(),
                    )
                    self._last_result = result if outcome == "ok" else None
                    self._job = None
                    self._seq += 1
                job.done.set()
            self.events.add(
                "info" if outcome == "ok" else "warning",
                "Microscope command finished",
                command_id=job.ident,
                action=job.action,
                result=outcome,
                message_detail=message,
            )

    def stop(self, client, wait_s=0):
        if (
            isinstance(wait_s, bool)
            or not isinstance(wait_s, (int, float))
            or not 0 <= wait_s <= 15
        ):
            raise ValueError("Stop wait must be within 0..15 seconds")
        with self._lock:
            if self._job is not None:
                self._job.cancel.set()
            self._reference = None
            self._handoff_deadline = None
            self._seq += 1
            if not self._stopping:
                if self._motor is None:
                    return "confirmed"
                self._stopping = True
                self._stop_done.clear()
                self._stop_outcome = "pending"
                thread = threading.Thread(
                    target=self._finish_stop, name="microscope-stop", daemon=True
                )
                self._stop_thread = thread
                try:
                    thread.start()
                except BaseException as error:
                    self._stopping = False
                    self._stop_outcome = "unconfirmed"
                    self._stop_done.set()
                    self._latch_fault("stop", str(error))
            done = self._stop_done
        done.wait(wait_s)
        with self._lock:
            return self._stop_outcome if done.is_set() else "pending"

    def _finish_stop(self):
        outcome = "confirmed"
        try:
            self._motor.safe_stop_disable()
        except Exception as error:
            outcome = "unconfirmed"
            self._latch_fault("stop", str(error))
        finally:
            with self._lock:
                self._reference = None
                self._stopping = False
                self._stop_outcome = outcome
                self._stop_done.set()
                self._seq += 1

    def on_lease_expired(self):
        self.expiry_count += 1
        self._manual_holder = None
        self.stop("lease-expired", 0)

    def close(self):
        with self._lifecycle:
            with self._lock:
                if self._shutdown_complete:
                    return
                self._closed = True
                self._quit.set()
                job = self._job
            outcome = self.stop("shutdown", 15)
            failures = [] if outcome == "confirmed" else ["Motor shutdown is unconfirmed"]
            if job is not None and not job.done.wait(3):
                failures.append("Microscope command did not stop")
            with self._frame_condition:
                if not self._frame_condition.wait_for(lambda: self._frame_reads == 0, timeout=2.0):
                    # Keep both owners intact until the accepted read returns.
                    # A later explicit close can finish cleanup; no new read is admitted.
                    raise RuntimeError("Microscope frame reads are still stopping")
            for device in (self._side, self._camera):
                if device is not None:
                    try:
                        device.close()
                    except Exception as error:
                        failures.append(str(error))
            if self._motor is not None:
                try:
                    self._motor.disconnect()
                except Exception as error:
                    failures.append(str(error))
            if self._poller is not None and self._poller is not threading.current_thread():
                self._poller.join(1)
                if self._poller.is_alive():
                    failures.append("Microscope monitor did not stop")
            with self._lock:
                self._started = False
                self._reference = None
                self._shutdown_complete = True
            if failures:
                raise RuntimeError("Microscope shutdown failed: " + "; ".join(failures))


class _GuardedMotor:
    """An algorithm gets only job-bound operations, never unrestricted motor access."""

    def __init__(self, service, job):
        self.service, self.job = service, job

    @property
    def connected(self):
        return self.service._motor.connected

    def _call(self, name, *args):
        self.service._guard_reference(self.job)
        return getattr(self.service._motor, name)(*args, cancel_event=self.job.cancel)

    def focus_enable(self):
        return self._call("focus_enable")

    def focus_speed(self, speed):
        return self._call("focus_speed", speed)

    def objective_stop(self):
        return self.send_and_wait("STOP OBJ", "OK STOP OBJ")

    def objective_disable(self):
        return self._call("objective_disable")

    def focus_stop(self):
        self.service._check_job(self.job)
        return self.service._motor.send_and_wait(
            "STOP FOCUS", "OK STOP FOCUS", cancel_event=self.job.cancel
        )

    def focus_move_async(self, steps, *, cancel_event=None):
        status = self.service._guard_reference(self.job)
        if (
            type(steps) is not int
            or not status.minimum <= status.focus.position + steps <= status.maximum
        ):
            raise ValueError("Focus move exceeds the configured limits")
        return self.service._motor.focus_move_async(steps, cancel_event=self.job.cancel)

    def wait_focus_done(self, ticket, *, cancel_event=None):
        self.service._check_job(self.job)
        return self.service._motor.wait_focus_done(ticket, cancel_event=self.job.cancel)

    def focus_move(self, steps, *, cancel_event=None):
        return self.wait_focus_done(self.focus_move_async(steps))

    def send_and_wait(self, command, response_prefix, *, timeout=5.0, cancel_event=None):
        self.service._check_job(self.job)
        if command.startswith("OBJ MOVE "):
            self.service._guard_reference(self.job, clearance=True)
        elif command not in (
            "STOP OBJ",
            "OBJ DISABLE",
            "OBJ ENABLE",
            "MICRO 4",
        ) and not re.fullmatch(r"OBJ SPEED [1-9]\d{0,4}", command):
            raise ValueError("The algorithm requested an unsupported motor operation")
        else:
            self.service._guard_reference(self.job)
        typed = {"OBJ ENABLE": "objective_enable", "OBJ DISABLE": "objective_disable"}
        if command in typed:
            return getattr(self.service._motor, typed[command])(cancel_event=self.job.cancel)
        if command == "MICRO 4":
            return self.service._motor.set_microstep(4, cancel_event=self.job.cancel)
        if command.startswith("OBJ SPEED "):
            return self.service._motor.objective_speed(
                int(command.split()[-1]), cancel_event=self.job.cancel
            )
        return self.service._motor.send_and_wait(
            command, response_prefix, timeout=timeout, cancel_event=self.job.cancel
        )
