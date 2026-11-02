"""Confirmed manual zero and camera references, with authority rechecked after capture."""

import base64
import math
from pathlib import Path

from .config import DEFAULT_CONFIG
from .protocol import ControllerError, LinkError
from .reference_camera import ReferenceCamera, observe_marker
from .reference_store import teach_pixel


class StageReference:
    def _init_reference(self, config, frame_source):
        self._config_path = Path(config or DEFAULT_CONFIG) if not self.demo or config else None
        # Demo must never fall through to a real camera endpoint.
        self._frame_source = frame_source or (None if self.demo else ReferenceCamera())
        self._reference_views = {}

    def _home_available(self, motor):
        switches = getattr(self._transport, "home_switches", None)
        return switches is None or switches.get(motor) is True

    def _reference_snapshot(self, motor):
        reference = self._geometry.motors[motor].reference if self._geometry else None
        if reference is None:
            return None
        view = self._reference_views.get(motor)
        return {
            **reference,
            "observation": {key: value for key, value in view.items() if key != "image"}
            if view
            else None,
        }

    def _reference_idle(self, motor):
        axis = self._axis(motor)
        self._require_connection()
        self._require_no_sequence()
        if not getattr(self._transport, "zero_supported", False):
            raise ControllerError("Manual zero is unavailable with this firmware.")
        try:
            self._read_status()
        except LinkError as exc:
            self._lose_connection(exc)
            raise LinkError(self._error) from exc
        self._idle_axis(motor)
        if not self._feedback_fresh() or self._motors[motor].homed is None:
            raise ControllerError("Fresh extended stage feedback is required.")
        return axis

    def _reference_authority(self, client):
        lease = self.lease.snapshot()
        if client is None:
            if lease["holder"] is not None:
                raise ControllerError("Stage is controlled by automation")
        elif lease["holder"] != client or lease["remaining_s"] <= 0:
            raise ControllerError("The control lease expired before zero confirmation.")

    def _reference_fence(self, motor):
        state = self._motors[motor]
        return (
            self._transport,
            self._microstep,
            self._event_id,
            self.lease.epoch,
            self._geometry,
            state.position,
            state.mode,
            state.homed,
            state.soft,
        )

    def _observe_camera(self, motor, reference):
        if self._frame_source is None:
            raise ControllerError("Demo camera references require an offline frame source.")
        started = self.clock()
        try:
            pixel, jpeg = observe_marker(self._frame_source.read(reference["stream"]), reference)
            if not 0 <= self.clock() - started <= 2:
                raise ControllerError("Reference frame is stale.")
            offset = [pixel[index] - reference["pixel"][index] for index in (0, 1)]
            view = dict(
                pixel=pixel,
                offset_px=offset,
                distance_px=math.hypot(*offset),
                sampled_at=self.clock(),
                image="data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
            )
        except (ControllerError, OSError, ValueError) as exc:
            with self._lock:
                self._reference_views[motor] = dict(error=str(exc), sampled_at=self.clock())
            raise
        with self._lock:
            self._reference_views[motor] = view
        return view

    def preview_reference(self, motor):
        with self._lock:
            reference = self._axis(motor).reference
            if reference is None:
                raise ControllerError("This axis has no camera reference.")
        return self._observe_camera(motor, reference)

    def _confirmed_reference(self, action, motor, confirm, client):
        if confirm is not True:
            raise ValueError("Confirm the physical zero position before this operation.")
        with (
            self.lease.control(client or "stage-panel", require_holder=client is not None),
            self._lock,
        ):
            self._reference_authority(client)
            axis = self._reference_idle(motor)
            bounds = axis.bounds(self._microstep)
            if action == "zero" and not bounds[0] <= 0 <= bounds[1]:
                raise ControllerError("The configured soft limits must contain zero.")
            if action == "teach_reference" and axis.reference is None:
                raise ControllerError("Configure the reference stream and marker before teaching.")
            fence = self._reference_fence(motor)
        view = self._observe_camera(motor, axis.reference) if axis.reference else None
        if action == "zero" and view and view["distance_px"] > axis.reference["tolerance_px"]:
            raise ControllerError("Reference marker is outside the zero tolerance.")
        with (
            self.lease.control(client or "stage-panel", require_holder=client is not None),
            self._lock,
        ):
            self._reference_authority(client)
            self._reference_idle(motor)
            if self._reference_fence(motor) != fence:
                raise ControllerError(
                    "Stage state changed during confirmation. Check the axis again."
                )
            if view and not 0 <= self.clock() - view["sampled_at"] <= 2:
                raise ControllerError("Reference frame is stale.")
            self._references.pop(motor, None)
            try:
                if action == "teach_reference":
                    if self._config_path is None:
                        raise ControllerError(
                            "Teaching requires an explicit stage configuration file."
                        )
                    self._geometry = teach_pixel(
                        self._config_path, self._geometry, motor, view["pixel"]
                    )
                    self._reference_views.pop(motor, None)
                else:
                    self._transport.command(f"motor {motor}")
                    self._reference_authority(client)
                    if not self._feedback_fresh() or (
                        view and not 0 <= self.clock() - view["sampled_at"] <= 2
                    ):
                        raise ControllerError("Reference or board feedback is stale.")
                    self._transport.command("zero")
                    self._read_status()
                    state = self._motors[motor]
                    if (
                        state.position != 0
                        or state.mode != "idle"
                        or state.homed is not True
                        or state.soft is not False
                        or self._microstep != fence[1]
                    ):
                        raise LinkError("Arduino did not confirm the manual zero.")
                    self._reference_authority(client)
                    self._transport.command(f"soft {bounds[0]} {bounds[1]}")
                    self._read_status()
                    state = self._motors[motor]
                    self._reference_authority(client)
                    if (
                        not self._feedback_fresh()
                        or state.position != 0
                        or state.mode != "idle"
                        or state.homed is not True
                        or state.soft is not True
                        or self._microstep != fence[1]
                    ):
                        raise LinkError("Arduino did not confirm zero and soft limits.")
                    self._references[motor] = self._microstep
                self._event(f"{motor.title()} {action} confirmed by {client or 'stage-panel'}")
            except LinkError as exc:
                self._lose_connection(exc)
                raise LinkError(self._error) from exc
            except OSError as exc:
                raise ControllerError("Cannot save the stage reference.") from exc
            return self.snapshot()

    def zero(self, motor, confirm=False, *, client=None):
        return self._confirmed_reference("zero", motor, confirm, client)

    def teach_reference(self, motor, confirm=False, *, client=None):
        return self._confirmed_reference("teach_reference", motor, confirm, client)
