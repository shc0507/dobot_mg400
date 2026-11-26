"""Rough 5x positioning and sharpness-verified focus operations.

Marker Y is used only to approach the initial 5x search region. It is never a
fine-focus target or a focus reference for another objective.
"""

import copy
import hashlib
import json
import math
import statistics
import time

from labkit.errors import ServiceError

from .aruco_switch import select_objective
from .autofocus import focus_score
from .focus_travel import MAX_FOCUS_TRAVEL
from .turret_calibration import ProfileLocator, capture_center, stable_capture

VISUAL_ACTIONS = (
    "select_objective",
    "focus_move",
    "objective_nudge",
    "autofocus",
    "rough_focus_5x",
    "autofocus_sequence",
)
ROUGH_TOLERANCE = 15.0


def profile_digest(profile):
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class VisualFocusMixin:
    def _visual_profile_summary(self):
        try:
            profile = self._turret_store.selected()
        except ValueError as error:
            return {
                "id": None,
                "ready": False,
                "error": str(error),
                "needs_initial_focus": True,
                "needs_height_calibration": True,
            }
        ready = bool(profile and self._turret_compatible(profile))
        return {
            "id": profile["profile_id"] if profile else None,
            "name": profile["name"] if profile else None,
            "camera": profile["camera"] if profile else None,
            "digest": profile_digest(profile) if profile else None,
            "ready": ready,
            "needs_initial_focus": not ready,
            "needs_height_calibration": not ready,
        }

    def _visual_update(self, **values):
        with self._lock:
            self._visual_focus.update(values)
            self._seq += 1

    def _autofocus_update(self, **values):
        with self._lock:
            self._autofocus_run.update(values)
            self._seq += 1

    def _visual_measure(self, job, profile, objective_id=None, *, aligned=True):
        images = [self._side_frame(job, fresh=True) for _ in range(3)]
        locator = ProfileLocator(profile, height="focus", allow_partial=True)
        detected = locator.locate(images[0], target_id=objective_id, only_target=False)
        markers = detected.get("markers", [])
        if not markers:
            raise ValueError(
                f"No usable objective marker: {detected.get('status', 'not_detected')}"
            )
        if objective_id is None and len(markers) > 1:
            ranked = sorted(abs(marker["dx_px"]) for marker in markers)
            if ranked[1] - ranked[0] < 10:
                raise ValueError("Objective identity is ambiguous; seat one lens before operation")
        marker = select_objective(markers, objective_id)
        objective_id = marker["id"]
        measured = stable_capture(images, objective_id)
        point = capture_center(measured)
        nudge_reference = getattr(self, "_visual_nudge_reference", None)
        observed_nudge = (
            nudge_reference is not None
            and nudge_reference[:2] == (profile_digest(profile), objective_id)
            and abs(marker["dx_px"] - nudge_reference[2]) <= 5
        )
        if aligned and abs(marker["dx_px"]) > 50 and not observed_nudge:
            raise ValueError(
                "Objective X does not match the profile; check seating and the side-camera angle"
            )
        job.visual_point = tuple(float(value) for value in point)
        self._visual_update(
            objective_id=objective_id,
            x_px=float(point[0]),
            x_error_px=marker["dx_px"],
            y_px=float(point[1]),
            profile_id=profile["profile_id"],
            observed_at=self._wall_clock(),
        )
        return objective_id, float(point[1])

    def _pin_focus_camera(self, job, evidence=None):
        if hasattr(job, "camera_settings"):
            return
        camera = self._camera
        settings = {"auto": bool(camera.sdk_camera.get_AutoExpoEnable())}
        for name in ("exposure", "gain"):
            getter = getattr(camera, "get_" + name, None)
            if getter is not None:
                settings[name] = getter()
        job.camera_settings = settings
        camera.set_auto_exposure(False)
        if evidence:
            for name, value in evidence["camera"].items():
                if name != "auto":
                    getattr(camera, "set_" + name)(value)
        job.focus_camera_settings = {
            name: getattr(camera, "get_" + name)()
            for name in ("exposure", "gain")
            if hasattr(camera, "get_" + name)
        }
        if not self.demo and job.cancel.wait(0.3):
            self._check_job(job)

    def _restore_focus_camera(self, job):
        settings = getattr(job, "camera_settings", None)
        if settings is None:
            return
        del job.camera_settings
        for name in ("exposure", "gain"):
            if name in settings:
                getattr(self._camera, "set_" + name)(settings[name])
        self._camera.set_auto_exposure(settings["auto"])

    def _sharpness(self, job):
        self._check_job(job)
        not_before = time.time()
        latest = self._camera.get_frame_packet(wait=False, raw=True, copy=False)
        cursor = latest[0] if latest else None
        scores = []
        deadline = time.monotonic() + 5
        while len(scores) < 3 and time.monotonic() < deadline:
            self._check_job(job)
            packet = self._camera.get_frame_packet(
                after_id=cursor, timeout=0.25, raw=True, copy=False
            )
            if packet is None:
                continue
            ident, captured_at, frame = packet
            if cursor is not None and ident <= cursor:
                continue
            cursor = ident
            if captured_at < not_before:
                continue
            value = focus_score(frame)
            if not math.isfinite(value):
                raise ValueError("The microscope image has an invalid sharpness score")
            scores.append(value)
        if len(scores) != 3:
            raise ValueError("No fresh microscope image for focus verification")
        median = statistics.median(scores)
        return median, 1.4826 * statistics.median(abs(v - median) for v in scores)

    @staticmethod
    def _require_sharpness(reference, current):
        score, noise = reference
        actual, actual_noise = current
        tolerance = max(abs(score) * 0.10, 3 * max(noise, actual_noise), 1e-9)
        if score <= 0 or actual < score - tolerance:
            raise ValueError("Focus sharpness changed; run autofocus before changing the lens")

    def _prepare_visual(self, job):
        self._check_job(job)
        try:
            self._side.open()
        except RuntimeError as error:
            self._check_job(job)
            raise ValueError(f"Side camera is unavailable: {error}") from error
        job.visual_open = True
        objective, _ = self._visual_measure(job, job.turret_profile)
        if job.action in {"rough_focus_5x", "autofocus_sequence"} and objective != 0:
            raise ValueError("Seat the 5x lens before starting this operation")
        if job.action == "autofocus":
            scan = job.autofocus_half_ranges[objective]
            job.focus_travel_limit = 16 * scan + (job.manual_focus_limit if objective == 0 else 0)
        self._visual_update(
            phase="observing",
            target_y_px=None,
            error_px=None,
            travel_used_pulses=0,
            travel_limit_pulses=job.focus_travel_limit,
        )
        self._autofocus_update(
            phase="idle",
            objective_id=objective,
            stage=None,
            score=None,
            position_pulses=None,
            verified=False,
            completed=[],
            error=None,
            sequence=job.action == "autofocus_sequence",
            search_half_range_pulses=None,
        )
        if job.action in {"autofocus", "autofocus_sequence"}:
            self._pin_focus_camera(job)
        status = self._fresh_status(job)
        if status.objective.moving or status.focus.moving or status.homing:
            raise ValueError("Stop both microscope axes before starting focus work")
        self._motor.set_microstep(4, cancel_event=job.cancel)
        self._motor.focus_enable(cancel_event=job.cancel)
        if not self.demo and job.cancel.wait(0.2):
            self._check_job(job)
        self._visual_measure(job, job.turret_profile, objective)
        self._motor.focus_zero(cancel_event=job.cancel)
        extent = min(MAX_FOCUS_TRAVEL, job.focus_travel_limit)
        limits = (-extent, extent)
        self._motor.focus_limits(*limits, cancel_event=job.cancel)
        status = self._fresh_status(job)
        if not status.zeroed or not status.limited or (status.minimum, status.maximum) != limits:
            raise RuntimeError("Firmware did not confirm the relative command travel guard")
        job.visual_limits = limits
        # Direct selection holds the operator-chosen height. Only the automatic
        # sequence requires verified focus before it advances to another lens.
        job.visual_clearance = 0 if job.action == "select_objective" else None
        job.visual_objective = objective
        with self._lock:
            self._reference = (status.session, status.reference_epoch)
            self._visual_origin, self._visual_limits = 0, limits
            if self._objective_id != objective:
                self._objective_offset = None
            self._objective_id = objective
            self._focus_verified = None

    def _rough_position_5x(self, job):
        from .service import _GuardedMotor

        if self._objective_id != 0:
            raise ValueError("Rough initial positioning is available only for the 5x lens")
        target = float(capture_center(job.turret_profile["initial_5x_capture"])[1])
        _, y = self._visual_measure(job, job.turret_profile, 0)
        self._autofocus_update(phase="rough_positioning", objective_id=0)
        self._visual_update(phase="rough_positioning", target_y_px=target, error_px=y - target)
        if abs(y - target) <= ROUGH_TOLERANCE:
            return {"rough_position": "already_near", "error_px": y - target}
        used_at_start = job.focus_travel_used
        start_y, probe, gain, stagnant = y, 0, None, 0
        for _ in range(20):
            error = y - target
            self._visual_update(error_px=error)
            if abs(error) <= ROUGH_TOLERANCE:
                return {"rough_position": "near", "error_px": error}
            remaining = job.manual_focus_limit - (job.focus_travel_used - used_at_start)
            if remaining <= 0:
                break
            step = (
                min(64 if probe == 0 else 128, remaining)
                if gain is None
                else round(-error / gain * 0.7)
            )
            if gain is not None:
                step = (1 if step >= 0 else -1) * max(1, min(200, abs(step), remaining))
            self._motor.focus_speed(min(200, self.config.focus_speed), cancel_event=job.cancel)
            _GuardedMotor(self, job).focus_move(step)
            if not self.demo and job.cancel.wait(0.15):
                self._check_job(job)
            _, next_y = self._visual_measure(job, job.turret_profile, 0)
            if gain is None:
                probe += step
                # Rough placement needs only a resolvable direction, not a 10-pixel fine gain.
                if abs(next_y - start_y) >= 3:
                    gain = (next_y - start_y) / probe
                elif probe >= min(512, job.manual_focus_limit):
                    break
            elif abs(next_y - y) >= 3 and (next_y - y) * step * gain <= 0:
                raise ValueError("5x rough focus moved in the wrong camera direction")
            stagnant = stagnant + 1 if abs(next_y - target) >= abs(error) - 0.5 else 0
            if gain is not None and stagnant >= 4:
                break
            y = next_y
        raise ValueError(
            "5x initial height was not reached; position it manually near the saved image"
        )

    def _remember_autofocus(self, job, result):
        measurement = result["measurement"]
        evidence = {
            "objective_id": self._objective_id,
            "profile": profile_digest(job.turret_profile),
            "quality": (measurement["score"], measurement.get("score_noise", 0)),
            "camera": copy.deepcopy(job.focus_camera_settings),
        }
        self._require_sharpness(evidence["quality"], self._sharpness(job))
        job.focus_evidence = evidence
        job.visual_clearance = self._guard_reference(job).focus.position
        completed = list(self._autofocus_run["completed"])
        if self._objective_id not in completed:
            completed.append(self._objective_id)
        self._autofocus_update(verified=True, completed=completed)

    def _verify_switch_focus(self, job):
        evidence = getattr(job, "focus_evidence", None)
        if evidence is None or evidence["objective_id"] != self._objective_id:
            raise ValueError("Autofocus must succeed before the next lens turn")
        self._require_sharpness(evidence["quality"], self._sharpness(job))
        job.visual_clearance = self._guard_reference(job).focus.position

    def _finish_visual(self, job, outcome):
        completed = False
        try:
            if outcome == "ok" and not job.cancel.is_set():
                evidence = getattr(job, "focus_evidence", None)
                if evidence and evidence["objective_id"] != self._objective_id:
                    evidence = None
                before = None
                if evidence:
                    self._autofocus_update(phase="verifying_release")
                    before = self._sharpness(job)
                    self._require_sharpness(evidence["quality"], before)
                self._motor.focus_disable(cancel_event=job.cancel)
                if not self.demo and job.cancel.wait(0.2):
                    self._check_job(job)
                if evidence:
                    after = self._sharpness(job)
                    self._require_sharpness(before, after)
                    self._require_sharpness(evidence["quality"], after)
                    evidence["quality"] = after
                    self._focus_verified = copy.deepcopy(evidence)
                else:
                    self._focus_verified = None
                self._fresh_status(job)
                if job.action == "objective_nudge":
                    self._visual_measure(job, job.turret_profile, self._objective_id, aligned=False)
                    self._visual_nudge_reference = (
                        profile_digest(job.turret_profile),
                        self._objective_id,
                        self._visual_focus["x_error_px"],
                    )
                self._visual_update(phase="idle_released")
                self._autofocus_update(phase="complete", verified=evidence is not None)
                completed = True
        finally:
            with self._lock:
                self._reference = None
                self._visual_origin, self._visual_limits = 0, None
            errors = []
            if getattr(job, "visual_open", False):
                job.visual_open = False
                try:
                    self._side.close()
                except Exception as error:
                    errors.append(error)
            try:
                self._restore_focus_camera(job)
            except Exception as error:
                errors.append(error)
            if outcome != "ok" or not completed or errors:
                self._focus_verified = None
                self._visual_nudge_reference = None
                self._visual_update(phase="unverified", target_y_px=None)
                self._autofocus_update(
                    phase="stopped" if outcome == "stopped" else "failed",
                    verified=False,
                    error=getattr(job, "failure_message", None),
                )
                self.stop("focus-verification-failure", 0, keep_recording=True)
            if errors and outcome == "ok":
                raise ValueError(f"Focus cleanup failed: {errors[0]}") from errors[0]

    def _require_visual_profile(self):
        try:
            profile = self._turret_store.selected()
        except ValueError as error:
            raise ServiceError(409, str(error)) from error
        if not self._turret_compatible(profile):
            raise ServiceError(409, "Select a complete X profile with a separate 5x initial image")
        return copy.deepcopy(profile)
