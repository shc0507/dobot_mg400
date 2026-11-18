"""Record named optical positions and move Insert between Position Finder endpoints."""

import copy
import math
import threading
import time
from pathlib import Path
from statistics import median
from uuid import uuid4

from labkit.errors import ServiceError
from orchestrator.transfer_motion import (
    MICROSTEP,
    MotionScaleStore,
    PulseScale,
    move_seconds,
    movement,
)
from orchestrator.transfer_profiles import (
    TransferProfileStore,
    delete_position,
    new_profile,
    position_name,
    record_position,
)

PROFILE_ROOT = Path("/var/lib/mg400/calibration/sample-transfer")
TOLERANCE_MM = 0.5
SPEED = 50
INITIAL_STEPS = 10
MAX_SECONDS = 600
SETTLE_SECONDS = 0.15
SAMPLE_INTERVAL = 0.05
STABLE_SPREAD_MM = 0.4
ACTIVE = {"moving", "measuring"}
_MANAGER_LOCK = threading.Lock()


class Cancelled(Exception):
    """The operation no longer has authority to continue."""


def coordinates(report):
    values = {}
    for axis in ("x", "y", "insert"):
        value = report.get("axes", {}).get(axis, {}).get("value_mm")
        values[axis] = value if type(value) in (int, float) and math.isfinite(value) else None
    return values


class LiveTransferAdapter:
    """Use the existing Console lease and Position Finder calibration store."""

    def __init__(self, service):
        self.service = service

    def owns(self, page):
        return self.service.console._owned("stage", page) is not None

    def stage_state(self, page):
        item = self.service.console._owned("stage", page)
        if item is None:
            raise ServiceError(409, "Take stage control first.")
        state = item.adapter.state()
        if not item.adapter.owned(state):
            raise ServiceError(409, "The stage control lease was lost.")
        return state

    def move(self, page, **params):
        return self.service.console.command("stage", page, "move_guarded", params)

    def prepare(self, page, stop_epoch):
        return self.service.console.command(
            "stage",
            page,
            "set_microstep_guarded",
            {"microstep": MICROSTEP, "stop_epoch": stop_epoch},
        )

    def stop(self):
        return self.service.console.stop("stage")

    def _profile(self, page, source=None):
        from .twin import get_twin_console

        twin = get_twin_console(self.service)
        if source is None:
            current = twin.page(page)
            with current.lock:
                twin.sync_selection(current)
                if current.selected is None or current.camera is None:
                    raise ValueError("Select a saved Position Finder calibration first.")
                profile = twin.store.load(current.selected)
                camera = current.camera
        else:
            profile = twin.store.load(source["profile_id"])
            camera = source["camera"]
        actual = {
            "camera": camera,
            "device_id": twin.frames.identity(camera),
            "profile_id": profile["id"],
            "name": profile["name"],
        }
        if profile["camera"]["device_id"] != actual["device_id"] or (
            source is not None and source != actual
        ):
            raise ValueError("The Position Finder calibration does not match this profile.")
        return twin, profile, actual

    def transfer_reference(self, page):
        """Use the saved Position Finder Insert endpoints, never its editing draft."""
        _, profile, source = self._profile(page)
        axis = profile.get("axes", {}).get("insert")
        endpoints = profile.get("endpoints", {})
        if (
            not axis
            or profile.get("axis_status", {}).get("insert", {}).get("status") != "calibrated"
            or not all(name in endpoints for name in ("insert_out", "insert_in"))
        ):
            raise ValueError("Calibrate Insert fully out and fully in in Position Finder first.")
        minimum, maximum = axis.get("min"), axis.get("max")
        if (
            type(minimum) not in (int, float)
            or type(maximum) not in (int, float)
            or not math.isfinite(minimum)
            or not math.isfinite(maximum)
            or minimum != 0
            or maximum <= minimum
        ):
            raise ValueError("The Position Finder Insert travel is invalid.")
        return {
            "source": source,
            "targets_mm": {"fully_out": minimum, "fully_in": maximum},
            "ready": True,
            "message": "",
        }

    def sample(self, page, source=None):
        from vision.stage_twin import StageConverter

        twin, profile, actual = self._profile(page, source)
        camera = actual["camera"]
        with twin.processing():
            requested_at = time.monotonic()
            frame, received_at = twin.read(camera, profile)
            report = StageConverter.from_profile(profile).measure(frame)
        return actual, {
            **report,
            "received_at": received_at,
            "camera": camera,
            "profile_id": profile["id"],
            "sample_requested_at": requested_at,
        }


class SampleTransfer:
    """One stage operation at a time, with page-local drafts and shared profiles.

    The adapter supplies owns, sample, transfer_reference, stage_state, move and stop.
    Sample returns
    (source, report). Guarded moves require the stop_epoch from stage_state.
    No network request or camera read runs with this manager's lock held.
    """

    def __init__(self, store, adapter, *, demo=False):
        self.store, self.adapter, self.demo = store, adapter, demo
        self.lock = threading.RLock()
        self.pages = {}
        self.operation = None
        self.scales = MotionScaleStore(store)

    def _page(self, page):
        if page not in self.pages:
            self.pages[page] = {
                "draft": new_profile("New position profile"),
                "position": dict.fromkeys(("x", "y", "insert")),
                "position_source": None,
            }
        return self.pages[page]

    def state(self, page):
        try:
            reference = self.adapter.transfer_reference(page)
        except (ValueError, OSError) as error:
            reference = {"source": None, "targets_mm": {}, "ready": False, "message": str(error)}
        with self.lock:
            data = copy.deepcopy(self._page(page))
            operation = self.operation
            report = {"id": None, "status": "idle", "target": None, "message": "", "mine": False}
            if operation is not None:
                report = {
                    key: copy.deepcopy(operation[key])
                    for key in (
                        "id",
                        "status",
                        "target",
                        "target_mm",
                        "message",
                        "position_mm",
                        "phase",
                        "microstep",
                        "pulses_per_mm",
                        "scale_source",
                    )
                }
                report["mine"] = operation["page"] == page
                report["settling"] = not operation["done"].is_set()
        return {
            **data,
            "demo": self.demo,
            "has_control": self.adapter.owns(page),
            "profiles": self.store.list(),
            "selected_profile": self.store.selection(),
            "active_profile": self.store.selected(),
            "operation": report,
            "tolerance_mm": TOLERANCE_MM,
            "transfer_reference": reference,
        }

    def _begin(self, page, status, target=None, *, stop_epoch=None, target_mm=None):
        if status == "moving" and type(stop_epoch) is not int:
            raise ValueError("Update the stage service to support guarded moves.")
        with self.lock:
            if self.operation is not None and not self.operation["done"].is_set():
                raise ServiceError(409, "The previous operation is still finishing.")
            self.operation = {
                "id": str(uuid4()),
                "page": page,
                "status": status,
                "target": target,
                "target_mm": target_mm,
                "message": "Reading Position Finder." if status == "measuring" else "Moving.",
                "position_mm": None,
                "phase": "preparing" if target else "measuring",
                "microstep": MICROSTEP if target else None,
                "pulses_per_mm": None,
                "scale_source": None,
                "cancelled": threading.Event(),
                "done": threading.Event(),
                "started": time.monotonic(),
                "stop_epoch": stop_epoch,
            }
            return self.operation

    def _finish(self, operation, status, message):
        with self.lock:
            if not operation["cancelled"].is_set():
                operation.update(status=status, message=message)

    def cancel(self, page=None):
        """Remove continuation authority immediately. The caller sends the stop."""
        with self.lock:
            operation = self.operation
            if (
                operation is None
                or operation["status"] not in ACTIVE
                or (page is not None and operation["page"] != page)
            ):
                return False
            operation["cancelled"].set()
            operation.update(status="stopped", message="Motion cancelled.")
            return True

    def stop(self):
        self.cancel()
        result = self.adapter.stop()
        with self.lock:
            if self.operation is not None and self.operation["status"] == "stopped":
                self.operation["message"] = (
                    "Stopped." if result.get("outcome") == "confirmed" else "Stop is unconfirmed."
                )
        return result

    def _check(self, operation):
        if operation["cancelled"].is_set():
            raise Cancelled()
        if operation["status"] == "moving" and not self.adapter.owns(operation["page"]):
            raise ServiceError(409, "The stage control lease was lost.")
        if time.monotonic() - operation["started"] > MAX_SECONDS:
            raise ValueError("The target was not reached within 600 seconds.")

    def _sample(self, operation, source=None, limits=None, *, require_insert=True):
        self._check(operation)
        with self.lock:
            self._page(operation["page"])["position"] = dict.fromkeys(("x", "y", "insert"))
        actual, report = self.adapter.sample(operation["page"], source)
        self._check(operation)
        if source is not None and actual != source:
            raise ValueError("The Position Finder calibration changed.")
        position = coordinates(report)
        reading = report.get("axes", {}).get("insert", {})
        insert = position["insert"]
        near_endpoint = (
            limits is not None
            and insert is not None
            and reading.get("status") == "extrapolated"
            and limits["fully_out"] - TOLERANCE_MM <= insert <= limits["fully_in"] + TOLERANCE_MM
        )
        if require_insert and (
            insert is None
            or (reading.get("status") != "provisional_interpolation" and not near_endpoint)
        ):
            raise ValueError("Position Finder cannot locate Insert within its calibrated travel.")
        with self.lock:
            self._check(operation)
            current = self._page(operation["page"])
            current.update(position=position, position_source=copy.deepcopy(actual))
            operation["position_mm"] = position["insert"]
        return actual, report

    def measure(self, page):
        operation = self._begin(page, "measuring")
        try:
            _, report = self._sample(operation, require_insert=False)
            warnings = []
            position = coordinates(report)
            for axis, label in (("x", "X"), ("y", "Y"), ("insert", "Insert")):
                reading = report.get("axes", {}).get(axis, {})
                if position[axis] is None:
                    warnings.append(f"{label} unavailable")
                elif reading.get("status") == "extrapolated":
                    warnings.append(f"{label} outside calibrated travel")
                elif reading.get("status") != "provisional_interpolation":
                    warnings.append(f"{label} reading is unconfirmed")
            message = "Position measured."
            if warnings:
                message += " " + ". ".join(warnings) + "."
            self._finish(operation, "completed", message)
        except Cancelled:
            pass
        except Exception as exc:
            self._finish(operation, "failed", str(exc))
            raise
        finally:
            operation["done"].set()
        return self.state(page)

    def mutate(self, page, action, data):
        if action == "record":
            name = position_name(data.get("name"))
            operation = self._begin(page, "measuring")
            try:
                with self.lock:
                    draft = copy.deepcopy(self._page(page)["draft"])
                source, report = self._sample(operation)
                recorded = record_position(draft, name, source, report)
                with self.lock:
                    self._check(operation)
                    if self._page(page)["draft"] != draft:
                        raise ValueError("The draft changed during measurement. Record again.")
                    self._page(page)["draft"] = recorded
                self._finish(operation, "completed", "Position recorded.")
            except Cancelled:
                pass
            except Exception as exc:
                self._finish(operation, "failed", str(exc))
                raise
            finally:
                operation["done"].set()
        elif action == "position/delete":
            with self.lock:
                current = self._page(page)
                current["draft"] = delete_position(current["draft"], data.get("name"))
        elif action == "profile/new":
            draft = new_profile(data.get("name", "New position profile"))
            with self.lock:
                self._page(page)["draft"] = draft
        elif action == "profile/save":
            with self.lock:
                draft = copy.deepcopy(self._page(page)["draft"])
            draft["name"] = data.get("name", draft["name"])
            saved = self.store.save(draft)
            if saved["positions"]:
                self.store.select(saved["id"])
            with self.lock:
                self._page(page)["draft"] = saved
        elif action == "profile/select":
            draft = self.store.select(data.get("id"))
            with self.lock:
                self._page(page)["draft"] = draft
        elif action == "profile/load":
            draft = self.store.load(data.get("id"))
            with self.lock:
                self._page(page)["draft"] = draft
        elif action == "profile/delete":
            self.store.delete(data.get("id"))
        elif action == "profile/upload":
            value = data.get("profile")
            if not isinstance(value, str):
                raise ValueError("Upload a profile JSON file.")
            draft = self.store.upload(value.encode("utf-8"))
            with self.lock:
                self._page(page)["draft"] = draft
        else:
            raise ValueError("Unknown Sample Transfer action.")
        return self.state(page)

    def _stage(self, operation, epoch=None):
        self._check(operation)
        state = self.adapter.stage_state(operation["page"])
        self._check(operation)
        if not state.get("connected") or not state.get("feedback_fresh") or state.get("locked"):
            raise ValueError("Fresh stage feedback is unavailable.")
        if type(state.get("stop_epoch")) is not int:
            raise ValueError("Update the stage service to support guarded moves.")
        if epoch is not None and state["stop_epoch"] != epoch:
            raise Cancelled()
        if operation.get("prepared") and state.get("microstep") != MICROSTEP:
            raise ValueError("The stage stepping mode changed. Restart transfer at 1/8 step.")
        if (
            operation.get("prepared")
            and state.get("automation", {}).get("configured")
            and self._motor(state, "insert").get("automation_ready") is not True
        ):
            raise ValueError(
                "Reconfirm the Insert zero/reference and soft limits at 1/8 step in Motion."
            )
        return state

    @staticmethod
    def _motor(state, motor):
        return next(item for item in state["motors"] if item["id"] == motor)

    def _wait(self, operation, motor, epoch, command_id=None, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            state = self._stage(operation, epoch)
            axis = self._motor(state, motor)
            result = state.get("last_command") or {}
            if command_id is not None and result.get("id") == command_id:
                if result.get("result") == "stopped":
                    raise Cancelled()
                if result.get("result") != "ok":
                    raise ValueError(result.get("message") or "The stage move failed.")
                command_id = None
            if command_id is None and axis["mode"] == "idle" and axis.get("cooldown", 0) <= 0:
                return state
            if time.monotonic() >= deadline:
                raise ValueError("Stage motion completion was not confirmed.")
            operation["cancelled"].wait(0.05)

    def _move(self, operation, motor, direction, steps, epoch, speed=SPEED):
        self._check(operation)
        state = self._stage(operation, epoch)
        timeout = move_seconds(steps, speed, self._motor(state, motor).get("acceleration"))
        with self.lock:
            self._page(operation["page"])["position"] = dict.fromkeys(("x", "y", "insert"))
        result = self.adapter.move(
            operation["page"],
            motor=motor,
            direction=direction,
            speed=speed,
            steps=steps,
            stop_epoch=epoch,
        )
        self._check(operation)
        if result.get("outcome") != "accepted" or not isinstance(result.get("command_id"), str):
            if result.get("outcome") == "refused":
                self._stage(operation, epoch)
            raise ValueError(result.get("error") or "The stage move outcome is unknown.")
        self._wait(operation, motor, epoch, result["command_id"], timeout)

    def _phase(self, operation, phase, message, scale=None):
        with self.lock:
            self._check(operation)
            operation.update(phase=phase, message=message)
            if scale is not None:
                operation.update(pulses_per_mm=scale.value, scale_source=scale.source)

    def _observe(self, operation, reference):
        """Read after stopped motion; retry missing or unstable markers at rest."""
        if operation["cancelled"].wait(SETTLE_SECONDS):
            raise Cancelled()
        after = time.monotonic()
        readings, last_error = [], None
        for _ in range(7):
            self._stage(operation, operation["stop_epoch"])
            try:
                _, report = self._sample(operation, reference["source"], reference["targets_mm"])
                requested = report.get("sample_requested_at")
                if (
                    type(requested) not in (int, float)
                    or not math.isfinite(requested)
                    or requested < after
                ):
                    raise ValueError("The camera measurement predates the stopped movement.")
                value = coordinates(report)["insert"]
                readings.append(value)
                readings = readings[-3:]
                if len(readings) == 3 and max(readings) - min(readings) <= STABLE_SPREAD_MM:
                    self._stage(operation, operation["stop_epoch"])
                    position = median(readings)
                    with self.lock:
                        self._check(operation)
                        self._page(operation["page"])["position"]["insert"] = position
                        operation["position_mm"] = position
                    return position, readings
                last_error = ValueError(
                    "Position Finder readings are not stable while Insert is stopped."
                )
            except ValueError as error:
                readings = []
                last_error = error
            if operation["cancelled"].wait(SAMPLE_INTERVAL):
                raise Cancelled()
        raise last_error

    def _hybrid(self, operation, reference, state):
        epoch = operation["stop_epoch"]
        self._phase(operation, "preparing", "Setting the stage to 1/8 step.")
        result = self.adapter.prepare(operation["page"], epoch)
        self._stage(operation, epoch)
        if result.get("outcome") != "accepted" or not isinstance(result.get("command_id"), str):
            raise ValueError(result.get("error") or "The stage did not accept 1/8 stepping.")
        state = self._wait(operation, "insert", epoch, result["command_id"])
        if state.get("microstep") != MICROSTEP:
            raise ValueError("The stage did not confirm 1/8 stepping.")
        operation["prepared"] = True
        source, target = reference["source"], operation["target_mm"]
        self._phase(operation, "measuring", "Reading the current Insert position.")
        position, readings = self._observe(operation, reference)
        direction = "ccw" if target > position else "cw"
        key = self.scales.key(source, state.get("port"), direction)
        scale = PulseScale(self.scales.get(key))
        anchor, pulses, still, movements = position, 0, 0, 0
        self._phase(operation, "learning", "Checking Insert pulse-to-distance conversion.", scale)
        while True:
            self._stage(operation, epoch)
            error = target - position
            if all(abs(target - reading) <= TOLERANCE_MM for reading in readings):
                self._phase(operation, "verifying", "Confirming the Insert endpoint.", scale)
                position, readings = self._observe(operation, reference)
                if all(abs(target - reading) <= TOLERANCE_MM for reading in readings):
                    self._finish(operation, "completed", "Target confirmed by 3 camera readings.")
                    return
                continue
            if movements >= 400:
                raise ValueError("The target was not reached within 400 movements.")
            next_direction = "ccw" if error > 0 else "cw"
            if next_direction != direction:
                direction = next_direction
                key = self.scales.key(source, state.get("port"), direction)
                scale = PulseScale(self.scales.get(key))
                anchor, pulses, still = position, 0, 0
            steps, speed = movement(error, scale)
            phase = "moving" if scale.verified else "learning"
            self._phase(
                operation,
                phase,
                "Moving Insert."
                if scale.verified
                else "Checking Insert pulse-to-distance conversion.",
                scale,
            )
            self._move(operation, "insert", direction, steps, epoch, speed)
            movements += 1
            measured, readings = self._observe(operation, reference)
            sign = 1 if direction == "ccw" else -1
            progress = (measured - position) * sign
            if progress < -STABLE_SPREAD_MM:
                raise ValueError("Insert moved away from the target. Check its direction.")
            expected = steps / scale.value
            if expected >= 1 and abs(progress - expected) > max(0.8, expected * 0.4):
                raise ValueError(
                    "Measured Insert travel differs from its pulse conversion. "
                    "Check the mechanism and calibration."
                )
            still = still + 1 if progress < 0.02 else 0
            if still >= 3:
                raise ValueError("Position Finder detected no reliable Insert progress.")
            pulses += steps
            displacement = (measured - anchor) * sign
            if displacement >= 2:
                if scale.observe(pulses, displacement):
                    self.scales.save(key, scale.value)
                anchor, pulses = measured, 0
            position = measured

    def _run(self, operation, reference=None, jog=None):
        try:
            epoch = operation["stop_epoch"]
            state = self._stage(operation, epoch)
            if state.get("busy"):
                raise ValueError("Wait for the current stage motion to finish.")
            motor = jog[0] if jog else "insert"
            self._wait(operation, motor, epoch)
            if jog:
                self._move(operation, motor, jog[1], INITIAL_STEPS, epoch)
                self._sample(operation)
                self._finish(operation, "completed", "Jog completed.")
                return
            self._hybrid(operation, reference, state)
        except Cancelled:
            self.cancel(operation["page"])
        except Exception as exc:
            self._finish(operation, "failed", str(exc))
            try:
                result = self.adapter.stop()
                if result.get("outcome") != "confirmed":
                    with self.lock:
                        operation["message"] += " Stop is unconfirmed."
            except Exception:
                with self.lock:
                    operation["message"] += " Stop is unconfirmed."
        finally:
            operation["done"].set()

    def start(self, page, target):
        if target not in ("fully_out", "fully_in"):
            raise ValueError("Choose fully_out or fully_in.")
        if not self.adapter.owns(page):
            raise ServiceError(409, "Take stage control first.")
        epoch = self.adapter.stage_state(page).get("stop_epoch")
        reference = copy.deepcopy(self.adapter.transfer_reference(page))
        if not reference["ready"]:
            raise ValueError(reference["message"] or "Select a calibrated Position Finder profile.")
        operation = self._begin(
            page, "moving", target, stop_epoch=epoch, target_mm=reference["targets_mm"][target]
        )
        threading.Thread(target=self._run, args=(operation, reference), daemon=True).start()
        return self.state(page)

    def jog(self, page, motor, direction):
        if motor not in ("top", "bottom", "insert") or direction not in ("cw", "ccw"):
            raise ValueError("Choose a stage motor and cw or ccw direction.")
        if not self.adapter.owns(page):
            raise ServiceError(409, "Take stage control first.")
        epoch = self.adapter.stage_state(page).get("stop_epoch")
        operation = self._begin(page, "moving", stop_epoch=epoch)
        threading.Thread(
            target=self._run, args=(operation, None, (motor, direction)), daemon=True
        ).start()
        return self.state(page)


def get_sample_transfer(service):
    with _MANAGER_LOCK:
        manager = getattr(service, "sample_transfer", None)
        if manager is None:
            demo = bool(getattr(service, "demo", False))
            temporary = getattr(service, "temporary", None)
            if demo and temporary is None:
                raise RuntimeError("The demo service has not started.")
            root = (
                Path(temporary.name) / "calibration" / "sample-transfer" if demo else PROFILE_ROOT
            )
            adapter = getattr(service, "sample_transfer_adapter", None) or LiveTransferAdapter(
                service
            )
            manager = SampleTransfer(TransferProfileStore(root), adapter, demo=demo)
            service.sample_transfer = manager
        return manager
