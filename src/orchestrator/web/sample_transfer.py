"""Named encoder coordinates and sequential closed-loop stage movements."""

import copy
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from labkit.errors import ServiceError
from orchestrator.transfer_motion import (
    AXES,
    JOG_ROTATIONS,
    SPEED,
    coordinates,
    finite,
    fresh,
    idle,
    motors,
    sequence,
    target_ready,
)
from orchestrator.transfer_profiles import (
    STAGE_POSITIONS,
    TransferProfileStore,
    compatible,
    delete_endpoint,
    delete_position,
    new_profile,
    position_name,
    record_endpoint,
    record_position,
)

PROFILE_ROOT = Path("/var/lib/mg400/calibration/sample-transfer")
MAX_SECONDS = 600
SAMPLE_INTERVAL = 0.05
_MANAGER_LOCK = threading.Lock()


class Cancelled(Exception):
    """The operation no longer has authority to continue."""


class LiveTransferAdapter:
    """Read shared telemetry and submit commands through the existing stage lease."""

    def __init__(self, service):
        self.service = service

    def owns(self, page):
        return self.service.console._owned("stage", page) is not None

    def owns_current(self, page):
        """Confirm the device lease before an automatic cancellation stop."""
        console = self.service.console
        item = console._owned("stage", page)
        if item is None:
            return False
        state = item.adapter.state()
        return item.adapter.owned(state) and console._owned("stage", page) is item

    def stage_state(self, page):
        return self.service.console.read("stage", "api/state")

    def position(self, page, **params):
        return self.service.console.command("stage", page, "move_to_position_guarded", params)

    def move(self, page, **params):
        return self.service.console.command("stage", page, "move_guarded", params)

    def stop(self):
        return self.service.console.stop("stage")


def source(state):
    return {
        "kind": "stage_encoder",
        "device_id": state.get("port") or "stage",
        "name": "Stage encoders",
    }


def default_profile_name():
    return f"Stage Coordinate Calibration {datetime.now().date().isoformat()}"


def reference(state):
    axis = motors(state).get("insert", {})
    targets = {"fully_out": 0.0, "fully_in": axis.get("upper_limit_rotations")}
    ready_targets = {name: target_ready(state, "insert", value) for name, value in targets.items()}
    ready = all(ready_targets.values())
    return {
        "targets_rotations": targets,
        "ready_targets": ready_targets,
        "ready": ready,
        "message": ""
        if ready
        else "Calibrate Insert in Motion and keep 0 within its travel limits.",
    }


def stage_reference(state, profile=None):
    positions = profile.get("stage_positions", {}) if compatible(profile) else {}
    targets = {name: positions.get(name, {}).get("position_rotations") for name in STAGE_POSITIONS}
    ready_targets = {
        name: bool(position)
        and all(
            target_ready(state, motor, value)
            for motor, value in sequence(position, stage_only=True)
        )
        for name, position in targets.items()
    }
    ready = all(ready_targets.values())
    if not compatible(profile):
        message = "Select a saved stage coordinate profile in Calibration."
    elif not all(value is not None for value in targets.values()):
        message = "Record the missing Out stage or Microscope position, then save the profile."
    elif not ready:
        message = (
            "Calibrate X/Y in Motion and keep saved targets within their current travel limits."
        )
    else:
        message = ""
    return {
        "source": profile["source"] if compatible(profile) else None,
        "profile_id": profile["id"] if compatible(profile) else None,
        "profile_name": profile["name"] if compatible(profile) else None,
        "targets_rotations": targets,
        "ready_targets": ready_targets,
        "ready": ready,
        "message": message,
    }


class SampleTransfer:
    """One cancellable sequence, with page-local drafts and persistent saved profiles."""

    def __init__(self, store, adapter, *, demo=False):
        self.store, self.adapter, self.demo = store, adapter, demo
        self.lock = threading.RLock()
        self.pages = {}
        self.operation = None

    def _page(self, page):
        if page not in self.pages:
            self.pages[page] = {
                "draft": new_profile(default_profile_name()),
                "draft_name_is_default": True,
            }
        return self.pages[page]

    def operation_state(self, page):
        """Read sequence status without reading stage telemetry or saved profiles."""
        with self.lock:
            current = self.operation
            if current is None:
                return {"status": "idle", "mine": False, "message": "", "settling": False}
            operation = {
                key: copy.deepcopy(current.get(key))
                for key in (
                    "id",
                    "status",
                    "target",
                    "target_rotations",
                    "message",
                    "motor",
                    "phase",
                    "completed_axes",
                    "total_axes",
                )
            }
            operation.update(mine=current["page"] == page, settling=not current["done"].is_set())
            return operation

    def state(self, page):
        try:
            stage = self.adapter.stage_state(page)
            error = ""
        except Exception as exc:
            stage, error = {}, str(exc)
        position = coordinates(stage)
        axes = motors(stage)
        with self.lock:
            data = copy.deepcopy(self._page(page))
            operation = self.operation_state(page)
        active = self.store.selected()
        targets_ready = bool(
            active
            and compatible(active)
            and active["positions"]
            and all(
                target_ready(stage, motor, target)
                for capture in active["positions"].values()
                for motor, target in sequence(capture["position_rotations"])
            )
        )
        record_ready = all(
            target_ready(stage, motor, position[axis]) for motor, axis in AXES.items()
        )
        return {
            **data,
            "demo": self.demo,
            "has_control": self.adapter.owns(page),
            "position": position,
            "position_source": source(stage) if stage.get("connected") else None,
            "position_available": any(value is not None for value in position.values()),
            "record_ready": record_ready,
            "endpoint_record_ready": all(
                target_ready(stage, motor, position[axis])
                for motor, axis in AXES.items()
                if motor != "insert"
            ),
            "named_motion_ready": targets_ready,
            "profiles": self.store.list(),
            "selected_profile": active["id"] if active else None,
            "active_profile": active,
            "operation": operation,
            "tolerance_rotations": axes.get("insert", {}).get("tolerance"),
            "transfer_reference": reference(stage),
            "stage_reference": stage_reference(stage, active),
            "error": error,
        }

    def _begin(self, page, target, epoch, total, *, stage=None):
        if type(epoch) is not int:
            raise ValueError("Update the stage service to support guarded encoder moves.")
        stage = stage or {}
        instance, revision = stage.get("instance"), stage.get("reference_revision")
        if instance is not None and (not isinstance(instance, str) or not instance.strip()):
            raise ValueError("The stage has no current process identity.")
        if revision is not None and (type(revision) is not int or revision < 0 or instance is None):
            raise ValueError("The stage has no current process identity or reference revision.")
        with self.lock:
            if self.operation and not self.operation["done"].is_set():
                raise ServiceError(409, "Wait for the current stage operation to stop.")
            operation = {
                "id": uuid4().hex,
                "page": page,
                "status": "moving",
                "target": target,
                "target_rotations": None,
                "motor": None,
                "phase": "moving",
                "message": "",
                "started": time.monotonic(),
                "stop_epoch": epoch,
                "instance": instance,
                "reference_revision": revision,
                "cancelled": threading.Event(),
                "done": threading.Event(),
                "completed_axes": 0,
                "total_axes": total,
            }
            self.operation = operation
            return operation

    def cancel(self, page=None):
        with self.lock:
            current = self.operation
            if (
                current
                and not current["done"].is_set()
                and current["status"] == "moving"
                and (page is None or current["page"] == page)
            ):
                current["cancelled"].set()
                current.update(status="stopped", message="Stopped.")
                return True
            return False

    def stop(self):
        self.cancel()
        return self.adapter.stop()

    def close(self):
        """Cancel continuation before the application closes its stage connection."""
        self.cancel()
        with self.lock:
            operation = self.operation
        if operation and not operation["done"].is_set():
            try:
                self.adapter.stop()
            finally:
                operation["done"].wait(2)

    def _check(self, operation):
        if operation["cancelled"].is_set():
            raise Cancelled()
        if not self.adapter.owns(operation["page"]):
            raise ServiceError(409, "The stage control lease was lost.")
        if time.monotonic() - operation["started"] > MAX_SECONDS:
            raise ValueError("Stage motion completion was not confirmed within 600 seconds.")

    def _stage(self, operation):
        self._check(operation)
        stage = self.adapter.stage_state(operation["page"])
        self._check(operation)
        if (
            stage.get("instance") != operation["instance"]
            or stage.get("reference_revision") != operation["reference_revision"]
            or (
                operation["reference_revision"] is not None
                and type(stage.get("reference_revision")) is not int
            )
        ):
            raise ValueError("The stage process or reference changed. Start a new move.")
        if stage.get("stop_epoch") != operation["stop_epoch"]:
            raise Cancelled()
        if not stage.get("connected"):
            raise ValueError("Fresh stage encoder feedback is unavailable.")
        return stage

    @staticmethod
    def _all_idle(stage):
        axes = motors(stage)
        if set(axes) != set(AXES) or not all(idle(item) for item in axes.values()):
            raise ValueError("Wait for all stage motors to stop before starting a move.")

    def _preflight(self, page):
        if not self.adapter.owns(page):
            raise ServiceError(409, "Take stage control first.")
        stage = self.adapter.stage_state(page)
        self._all_idle(stage)
        return stage

    def _wait(self, operation, motor, target, command_id):
        if not isinstance(command_id, str) or not command_id.strip():
            raise ValueError("The stage returned no command completion ID.")
        while True:
            stage = self._stage(operation)
            axes = motors(stage)
            axis = axes.get(motor, {})
            if not fresh(stage, axis):
                raise ValueError("Fresh, healthy encoder feedback is unavailable.")
            if any(not idle(item) for name, item in axes.items() if name != motor):
                raise ValueError("Another stage axis moved during the move.")
            last = stage.get("last_command") or {}
            if last.get("id") == command_id:
                if last.get("result") == "stopped":
                    raise Cancelled()
                if last.get("result") not in (None, "ok", "pending", "running"):
                    raise ValueError(last.get("message") or "The stage command failed.")
            if idle(axis) and axis.get("cooldown", 0) <= 0:
                # The command result must be known before accepting even a near target.
                if last.get("id") == command_id and last.get("result") == "ok":
                    if (
                        not finite(axis.get("tolerance"))
                        or abs(axis["position_rotations"] - target) > axis["tolerance"]
                    ):
                        raise ValueError("The motor stopped before reaching its encoder target.")
                    return
            operation["cancelled"].wait(SAMPLE_INTERVAL)

    def _run(self, operation, targets, jog=None):
        try:
            for motor, target in targets:
                stage = self._stage(operation)
                self._all_idle(stage)
                axis = motors(stage).get(motor, {})
                if jog is None and not target_ready(stage, motor, target):
                    raise ValueError(
                        "Auto calibrate the stage and keep targets within its current limits."
                    )
                if jog is not None and (not fresh(stage, axis) or not axis.get("available")):
                    raise ValueError("Wait for fresh, idle encoder feedback before jogging.")
                with self.lock:
                    operation.update(
                        motor=motor, target_rotations=target, message=f"Moving {motor}."
                    )
                if abs(axis["position_rotations"] - target) > axis["tolerance"]:
                    self._check(operation)
                    params = {
                        "motor": motor,
                        "speed": min(SPEED, axis.get("max_speed", SPEED)),
                        "stop_epoch": operation["stop_epoch"],
                    }
                    if (
                        operation["instance"] is not None
                        and operation["reference_revision"] is not None
                    ):
                        params.update(
                            expected_instance=operation["instance"],
                            expected_reference_revision=operation["reference_revision"],
                        )
                    if jog is None:
                        result = self.adapter.position(operation["page"], position=target, **params)
                    else:
                        result = self.adapter.move(
                            operation["page"], direction=jog, rotations=JOG_ROTATIONS, **params
                        )
                    if result.get("outcome") != "accepted":
                        raise ValueError(result.get("error") or "The stage command was refused.")
                    self._wait(operation, motor, target, result.get("command_id"))
                self._check(operation)
                with self.lock:
                    operation["completed_axes"] += 1
            self._stage(operation)
            with self.lock:
                self._check(operation)
                operation.update(status="completed", phase="complete", message="Position reached.")
        except Cancelled:
            with self.lock:
                operation.update(status="stopped", message="Sequence cancelled.")
            outcome = self._stop_owned(operation)
            with self.lock:
                if outcome and outcome.get("outcome") == "confirmed":
                    operation["message"] = "Stopped."
                elif outcome:
                    operation["message"] = "Sequence cancelled. Stage stop is unconfirmed."
        except Exception as exc:
            with self.lock:
                if not operation["cancelled"].is_set():
                    operation.update(status="failed", message=str(exc))
            self._stop_owned(operation)
        finally:
            operation["done"].set()

    def _stop_owned(self, operation):
        try:
            owns = getattr(self.adapter, "owns_current", self.adapter.owns)
            if owns(operation["page"]):
                return self.adapter.stop()
        except Exception:
            return {"outcome": "unknown"}
        return None

    def _launch(self, page, label, stage, targets, jog=None):
        operation = self._begin(page, label, stage.get("stop_epoch"), len(targets), stage=stage)
        threading.Thread(target=self._run, args=(operation, targets, jog), daemon=True).start()
        return self.operation_state(page)

    def start_coordinates(self, page, target):
        """Move X/Y or Insert to rotations without creating or selecting a profile."""
        if not isinstance(target, dict) or set(target) not in ({"x", "y"}, {"insert"}):
            raise ValueError("Send target with x and y, or insert only.")
        if not all(finite(value) for value in target.values()):
            raise ValueError("Target coordinates must be finite rotations.")
        target = dict(target)
        targets = (
            sequence(target, stage_only=True) if "x" in target else [("insert", target["insert"])]
        )
        stage = self._preflight(page)
        if not all(target_ready(stage, motor, value) for motor, value in targets):
            raise ValueError("Auto calibrate the stage and keep targets within its current limits.")
        return self._launch(page, target, stage, targets)

    def start(self, page, target):
        if target not in ("fully_out", "fully_in"):
            raise ValueError("Choose Fully out or Fully in.")
        stage = self._preflight(page)
        limits = reference(stage)
        if not limits["ready_targets"][target]:
            raise ValueError(limits["message"])
        self._launch(page, target, stage, [("insert", limits["targets_rotations"][target])])
        return self.state(page)

    def start_stage(self, page, target):
        if target not in STAGE_POSITIONS:
            raise ValueError("Choose Out stage or Microscope.")
        stage = self._preflight(page)
        saved = stage_reference(stage, self.store.selected())
        if not saved["ready_targets"][target]:
            raise ValueError(saved["message"])
        targets = sequence(saved["targets_rotations"][target], stage_only=True)
        self._launch(page, target, stage, targets)
        return self.state(page)

    def start_position(self, page, name):
        name = position_name(name)
        profile = self.store.selected()
        if not compatible(profile):
            raise ValueError(
                "Select a saved rotation profile. Legacy millimetre profiles cannot move the stage."
            )
        capture = profile["positions"].get(name)
        if capture is None:
            raise ValueError("Choose a position from the selected saved profile.")
        stage = self._preflight(page)
        targets = sequence(capture["position_rotations"])
        if not all(target_ready(stage, motor, value) for motor, value in targets):
            raise ValueError(
                "Auto calibrate all axes and keep the saved position within their current limits."
            )
        self._launch(page, name, stage, targets)
        return self.state(page)

    def jog(self, page, motor, direction):
        if motor not in AXES or direction not in ("cw", "ccw"):
            raise ValueError("Choose a stage motor and cw or ccw direction.")
        stage = self._preflight(page)
        axis = motors(stage)[motor]
        if not fresh(stage, axis) or not axis.get("available"):
            raise ValueError("Wait for fresh, idle encoder feedback before jogging.")
        target = axis["position_rotations"] + JOG_ROTATIONS * (1 if direction == "ccw" else -1)
        self._launch(page, None, stage, [(motor, target)], jog=direction)
        return self.state(page)

    def measure(self, page):
        return self.state(page)

    def _record(self, page, name, *, endpoint=False):
        with self.lock:
            draft = copy.deepcopy(self._page(page)["draft"])
        # A service read can block. Keep Stop independent of that read.
        stage = self._preflight(page)
        position = coordinates(stage)
        binding = source(stage)
        report = {
            "source": binding,
            "received_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "axes": {
                axis: {
                    "value_rotations": position[axis],
                    "ready": target_ready(stage, motor, position[axis]),
                }
                for motor, axis in AXES.items()
            },
        }
        recorder = record_endpoint if endpoint else record_position
        recorded = recorder(draft, name, binding, report)
        with self.lock:
            if self.operation and not self.operation["done"].is_set():
                raise ServiceError(409, "Wait for the current stage operation to stop.")
            if self._page(page)["draft"] != draft:
                raise ValueError("The draft changed while reading encoders. Record again.")
            if not self.adapter.owns(page):
                raise ServiceError(409, "The stage control lease was lost.")
            self._page(page)["draft"] = recorded
        return self.state(page)

    def mutate(self, page, action, data):
        if action == "record":
            return self._record(page, data.get("name"))
        if action == "endpoint/record":
            return self._record(page, data.get("target"), endpoint=True)
        with self.lock:
            if self.operation and not self.operation["done"].is_set():
                raise ServiceError(409, "Wait for the current stage operation to stop.")
            if action == "endpoint/delete":
                self._page(page)["draft"] = delete_endpoint(
                    self._page(page)["draft"], data.get("target")
                )
            elif action == "position/delete":
                self._page(page)["draft"] = delete_position(
                    self._page(page)["draft"], data.get("name")
                )
            elif action == "profile/new":
                self._page(page).update(
                    draft=new_profile(data.get("name", default_profile_name())),
                    draft_name_is_default="name" not in data,
                )
            elif action == "profile/save":
                draft = copy.deepcopy(self._page(page)["draft"])
                draft["name"] = data.get("name", draft["name"])
                editing_selected = draft["id"] == self.store.selection()
                saved = self.store.save(draft)
                if saved["positions"] or saved.get("stage_positions") or editing_selected:
                    self.store.select(saved["id"])
                self._page(page).update(draft=saved, draft_name_is_default=False)
            elif action in ("profile/select", "profile/load"):
                load = self.store.select if action == "profile/select" else self.store.load
                self._page(page).update(draft=load(data.get("id")), draft_name_is_default=False)
            elif action == "profile/delete":
                self.store.delete(data.get("id"))
            elif action == "profile/upload":
                value = data.get("profile")
                if not isinstance(value, str):
                    raise ValueError("Upload a profile JSON file.")
                self._page(page).update(
                    draft=self.store.upload(value.encode("utf-8")), draft_name_is_default=False
                )
            else:
                raise ValueError("Unknown Sample Transfer action.")
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
