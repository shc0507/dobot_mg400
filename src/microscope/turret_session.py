"""Manual turret teaching under the microscope service's existing lease."""

import copy
import math
import threading
import time
import uuid
from dataclasses import dataclass, field

from labkit.errors import ServiceError

from .side_capture import SideCaptureCancelled
from .turret_calibration import (
    ProfileLocator,
    TurretProfileStore,
    is_complete,
    new_profile,
    stable_capture,
    validate_profile,
)


@dataclass
class _Session:
    client: str
    cancel: object
    focus_position: int
    reference: tuple
    focus_zeroed: bool


@dataclass
class _Operation:
    action: str
    client: str
    cancel: object
    ident: str = field(default_factory=lambda: uuid.uuid4().hex)
    done: threading.Event = field(default_factory=threading.Event)


class TurretCalibrationMixin:
    def _init_turret_calibration(self, store):
        self._turret_store = store or TurretProfileStore()
        self._turret_session = None
        self._turret_draft = None
        self._turret_draft_context = None

    def _cancel_turret_session_locked(self):
        session = self._turret_session
        if session is not None:
            session.cancel.set()
            self._turret_session = None
            self._objective_id = self._objective_offset = None

    def _turret_compatible(self, profile):
        return bool(
            profile and is_complete(profile) and profile["camera"] == self.config.side_stream
        )

    def turret_calibration_state(self):
        self.lease.poll()
        with self._lock:
            draft = copy.deepcopy(self._turret_draft)
            session = self._turret_session
            dimensions = list(self.config.side_mode[:2]) if self.config.side_mode else None
            if draft and draft["captures"]:
                dimensions = list(next(iter(draft["captures"].values()))["shape"])[::-1]
            selection_error = None
            try:
                selection = self._turret_store.selection_status()
            except ValueError as error:
                selection = {"profile_id": None, "ever_selected": True}
                selection_error = str(error)
            return {
                "session": {
                    "active": session is not None,
                    "client": session.client if session else None,
                    "focus_position": session.focus_position if session else None,
                    "focus_zeroed": session.focus_zeroed if session else False,
                },
                "draft": draft,
                "profiles": [
                    dict(item, id=item["profile_id"]) for item in self._turret_store.list()
                ],
                "selected_profile": selection["profile_id"],
                "selection_error": selection_error,
                "camera": {"stream": self.config.side_stream, "dimensions": dimensions},
                "alignment": "image_coordinates",
                "dictionary": "DICT_4X4_50",
                "switch_focus": self.config.switch_focus,
                "compatible_for_switch": self._turret_compatible(draft),
            }

    def turret_calibration_profile(self, ident):
        try:
            with self._lock:
                return self._turret_store.export(ident)
        except FileNotFoundError as error:
            raise ServiceError(404, "Turret profile does not exist") from error

    @staticmethod
    def _turret_arguments(action, args):
        allowed = {
            "begin": {"name", "new"},
            "record": {"objective_id"},
            "finish": set(),
            "cancel": set(),
            "save": {"name"},
            "load": {"id"},
            "select": {"id"},
            "delete": {"id"},
            "upload": {"profile"},
        }
        if not isinstance(args, dict) or action not in allowed or set(args) - allowed[action]:
            raise ValueError("Unknown turret calibration action or parameters")
        if (
            action in {"record", "load", "select", "delete", "upload"}
            and set(args) != allowed[action]
        ):
            raise ValueError("Missing turret calibration parameters")
        if action == "record" and (
            type(args["objective_id"]) is not int or args["objective_id"] not in (0, 1, 2, 3)
        ):
            raise ValueError("Objective ID must be 0, 1, 2 or 3")
        if "name" in args and (
            not isinstance(args["name"], str) or not args["name"].strip() or len(args["name"]) > 120
        ):
            raise ValueError("Profile name must contain 1..120 characters")
        if "new" in args and type(args["new"]) is not bool:
            raise ValueError("new must be a boolean")
        if action == "upload" and not isinstance(args["profile"], str):
            raise ValueError("profile must be a JSON string")

    def _reserve_turret_operation(self, action, args, client):
        from .service import _AuthorityCancel

        with self._lease.control(client):
            deadline = self._lease_deadline()
            with self._lock:
                if not self._started or self._closed:
                    raise ServiceError(503, "Microscope is not connected")
                if self._fault:
                    raise ServiceError(409, "Microscope is locked; only stop is allowed")
                session = self._turret_session
                if session and session.client != client:
                    raise ServiceError(409, "Another client owns turret calibration")
                if self._job is not None or self._stopping or self._recording is not None:
                    raise ServiceError(409, "Microscope is busy")
                if action in {"record", "finish", "cancel"} and session is None:
                    raise ServiceError(409, "Begin turret calibration first")
                if session and action not in {"record", "finish", "cancel"}:
                    raise ServiceError(409, "Finish turret calibration first")
                token = session.cancel if session else _AuthorityCancel(self._clock, deadline)
                operation = _Operation("turret_" + action, client, token)
                self._job = operation
                self._seq += 1
                return operation

    def turret_calibration_action(self, action, args, client):
        self._turret_arguments(action, args)
        if action == "cancel":
            with self._lease.control(client):
                with self._lock:
                    session = self._turret_session
                    if session is None or session.client != client:
                        raise ServiceError(409, "No turret calibration belongs to this client")
                    self._cancel_turret_session_locked()
                    self._seq += 1
            return self.turret_calibration_state()
        operation = self._reserve_turret_operation(action, args, client)
        try:
            if action == "begin":
                self._begin_turret(operation, args)
            elif action == "record":
                self._record_turret(operation, args["objective_id"])
            else:
                with self._lease.control(client):
                    with self._lock:
                        self._check_job_locked(operation)
                        if action in {"finish", "cancel"}:
                            self._turret_session = None
                        elif action == "save":
                            if self._turret_draft is None:
                                raise ValueError("Record a turret profile first")
                            profile = copy.deepcopy(self._turret_draft)
                            profile["name"] = args.get("name", profile["name"])
                            self._turret_draft = self._turret_store.save(profile)
                        elif action == "select":
                            self._turret_draft = self._turret_store.select(args["id"])
                            self._turret_draft_context = None
                        elif action == "load":
                            self._turret_draft = self._turret_store.load(args["id"])
                            self._turret_draft_context = None
                        elif action == "delete":
                            self._turret_store.delete(args["id"])
                            if (
                                self._turret_draft is not None
                                and self._turret_draft["profile_id"] == args["id"]
                            ):
                                self._turret_draft = None
                                self._turret_draft_context = None
                        elif action == "upload":
                            self._turret_draft = self._turret_store.upload(
                                args["profile"].encode("utf-8")
                            )
                            self._turret_draft_context = None
            return self.turret_calibration_state()
        except SideCaptureCancelled as error:
            raise ServiceError(409, str(error)) from error
        except FileNotFoundError as error:
            raise ServiceError(404, "Turret profile does not exist") from error
        except RuntimeError as error:
            self._sync_motor_fault()
            raise ServiceError(503, str(error)) from error
        finally:
            with self._lock:
                if operation is self._job:
                    self._job = None
                    self._seq += 1
                operation.done.set()

    def _turret_focus(self, status):
        known = bool(
            self._reference == (status.session, status.reference_epoch)
            and status.zeroed
            and status.limited
            and status.microstep == 4
            and status.focus.enabled
            and (status.minimum, status.maximum) == self.config.focus_limits
            and status.minimum <= status.focus.position <= status.maximum
        )
        return status.focus.position if known else None

    def _begin_turret(self, operation, args):
        status = self._fresh_status(operation)
        if status.objective.moving or status.focus.moving or status.homing:
            raise ServiceError(409, "Stop both axes before turret calibration")
        with self._lock:
            focus = self._turret_focus(status)
            context = (status.session, status.reference_epoch, status.focus.position)
            resume = self._turret_draft is not None and not args.get("new") and "name" not in args
            if resume:
                draft = copy.deepcopy(self._turret_draft)
                if draft["camera"] != self.config.side_stream or draft["focus_position"] != focus:
                    raise ServiceError(
                        409, "Draft camera or focus position changed; create a new profile"
                    )
                if focus is None and draft["captures"] and self._turret_draft_context != context:
                    raise ServiceError(
                        409, "Draft focus reference is unknown; create a new profile"
                    )
            else:
                draft = new_profile(
                    args.get("name", "Turret calibration"), self.config.side_stream, focus
                )
            self._objective_id = self._objective_offset = None
        self._motor.objective_disable(cancel_event=operation.cancel)
        self._check_job(operation)
        status = self._fresh_status(operation)
        if (
            status.objective.enabled
            or status.objective.moving
            or status.focus.moving
            or status.homing
        ):
            raise ServiceError(409, "The turret must be disabled and both axes stationary")
        if self._turret_focus(status) != focus or context != (
            status.session,
            status.reference_epoch,
            status.focus.position,
        ):
            raise ServiceError(409, "Focus reference changed during turret calibration setup")
        with self._lease.control(operation.client):
            with self._lock:
                self._check_job_locked(operation)
                self._turret_session = _Session(
                    operation.client,
                    operation.cancel,
                    status.focus.position,
                    (status.session, status.reference_epoch),
                    focus is not None,
                )
                self._turret_draft = draft
                self._turret_draft_context = context
                self._objective_id = self._objective_offset = None

    def _check_turret_focus(self, operation):
        status = self._fresh_status(operation)
        with self._lock:
            session = self._turret_session
            if session is None or session.client != operation.client:
                raise SideCaptureCancelled("Turret calibration was stopped")
            if (
                status.objective.enabled
                or status.objective.moving
                or status.focus.moving
                or status.homing
                or status.focus.position != session.focus_position
                or (status.session, status.reference_epoch) != session.reference
                or (self._turret_focus(status) is not None) != session.focus_zeroed
            ):
                raise ServiceError(
                    409, "Keep focus fixed and the turret disabled throughout calibration"
                )

    def _record_turret(self, operation, objective_id):
        self._check_turret_focus(operation)
        try:
            self._side.open()
            self._check_job(operation)
            latest = self._side.get_frame_packet(wait=False, copy=False)
            cursor = latest[0] if latest is not None else None
            if cursor is not None and (type(cursor) is not int or cursor < 0):
                raise ServiceError(503, "Invalid side-camera frame ID")
            not_before = time.monotonic()
            deadline = not_before + 2.0
            images = []
            while len(images) < 3 and time.monotonic() < deadline:
                self._check_job(operation)
                packet = self._side.get_frame_packet(after_id=cursor, timeout=0.2, copy=True)
                if packet is None:
                    continue
                frame_id, captured_at, pixels = packet
                if (
                    type(frame_id) is not int
                    or frame_id < 0
                    or isinstance(captured_at, bool)
                    or not isinstance(captured_at, (int, float))
                    or not math.isfinite(captured_at)
                ):
                    raise ServiceError(503, "Invalid side-camera frame metadata")
                if (cursor is not None and frame_id <= cursor) or captured_at < not_before:
                    continue
                cursor = frame_id
                images.append(pixels)
            self._check_job(operation)
            if len(images) != 3:
                raise ServiceError(409, "No fresh side-camera burst; try Record again")
            captured = stable_capture(images, objective_id)
        finally:
            self._side.close()
        self._check_turret_focus(operation)
        with self._lease.control(operation.client):
            with self._lock:
                self._check_job_locked(operation)
                draft = copy.deepcopy(self._turret_draft)
                draft["captures"][str(objective_id)] = captured
                self._turret_draft = validate_profile(draft)

    def _turret_profile_for_switch(self):
        try:
            selected = self._turret_store.selected()
            selection = self._turret_store.selection_status()
            if selected is None:
                if selection["ever_selected"]:
                    raise ValueError("Select a complete turret profile before switching")
                return None
            if not self._turret_compatible(selected):
                raise ValueError("Select a complete turret profile for the configured side camera")
            return selected
        except ValueError as error:
            raise ServiceError(409, str(error)) from error

    @staticmethod
    def _profile_locator(profile):
        return ProfileLocator(profile)
