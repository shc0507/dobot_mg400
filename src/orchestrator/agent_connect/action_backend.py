"""Dispatch fixed actions through Console handlers and existing device leases."""

import base64
import copy
import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from labkit.errors import ServiceError
from orchestrator.devices import STALE_S
from orchestrator.transfer_motion import (
    JOG_ROTATIONS,
    SPEED,
    finite,
    fresh,
    idle,
    sequence,
    target_ready,
)
from orchestrator.transfer_profiles import compatible, validate_profile
from orchestrator.web.sample_transfer import (
    Cancelled,
    SampleTransfer,
    get_sample_transfer,
    reference,
    stage_reference,
)

from .actions import FURNACE_DISABLED, PHYSICAL_DISABLED, SPECS, descriptors, validate
from .stage_reference import stage_reference as decision_stage_reference

WAIT_SECONDS = 600


def _digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _mock_service():
    """Use the native preview's contract, lease, calibration, and image handlers."""
    from labkit.events import EventLog
    from orchestrator.devices import Devices
    from orchestrator.web.console import Console
    from orchestrator.web.stage_preview import StagePreview
    from orchestrator.web.turret_preview import TurretPreview

    temporary = tempfile.TemporaryDirectory(prefix="agent-connect-simulation-")
    stage = StagePreview()

    def unavailable(name):
        raise ServiceError(409, f"No reviewed {name} simulator is installed in this preview.")

    service = SimpleNamespace(
        demo=True,
        temporary=temporary,
        settings={},
        events=EventLog(),
        dashboard=SimpleNamespace(text=str, refresh=lambda: None),
        dashboard_configured=lambda name: name == "stage",
        console_client=stage.client,
        dashboard_client=stage.client,
        console_start_demo=unavailable,
        turret_preview=TurretPreview(Path(temporary.name) / "turret"),
    )
    service.console = Console(service)
    service.devices = Devices(service, on_events=service.console.pull_events)
    return service, stage, temporary


class _GuardedTransfer:
    """Check authority and saved references before each shared sequence command."""

    def __init__(self, backend, plan, context):
        self.backend, self.plan, self.context = backend, plan, context
        self.adapter = backend.transfer.adapter
        self.uncertain = False
        self.sent = False
        self.stop_outcome = None

    def owns(self, page):
        self.context.checkpoint()
        return self.adapter.owns(page)

    def owns_current(self, page):
        # A lost operator grant must not prevent the cleanup stop on our device lease.
        owns = getattr(self.adapter, "owns_current", self.adapter.owns)
        return owns(page)

    def stage_state(self, page):
        self.context.checkpoint()
        state = self.adapter.stage_state(page)
        self.backend._check_reference(self.plan, state, page)
        self.context.checkpoint()
        return state

    def _send(self, method, page, params):
        self.stage_state(page)
        if self.plan["action"] == "stage.step":
            params.update(
                rotations=self.plan["params"]["rotations"], speed=self.plan["params"]["speed"]
            )
        self.context.progress(motor=params["motor"], target_rotations=params.get("position"))
        self.uncertain = True
        result = self.context.dispatch(method, page, **params)
        self.uncertain = result.get("outcome") not in {"accepted", "refused"}
        self.sent = self.sent or result.get("outcome") == "accepted"
        return result

    def position(self, page, **params):
        return self._send(self.adapter.position, page, params)

    def move(self, page, **params):
        return self._send(self.adapter.move, page, params)

    def stop(self):
        self.stop_outcome = self.adapter.stop()
        return self.stop_outcome


class ActionBackend:
    """Physical execution has no request-controlled enable path."""

    def __init__(self, service=None, *, mock=False, source=None, allow_physical=False):
        self.source = source
        self.owned_resources = None
        if service is None and mock:
            service, stage, temporary = _mock_service()
            self.owned_resources = stage, temporary
        self.service = service
        # mock=True never overrides a supplied physical service.
        self.simulated = service is not None and getattr(service, "demo", False) is True
        self.physical = (
            allow_physical is True
            and service is not None
            and getattr(service, "demo", None) is False
        )
        self.transfer = get_sample_transfer(service) if self.enabled else None
        self.lock = threading.RLock()
        self.running = {}
        self.lease_epochs = {}

    @property
    def enabled(self):
        return (self.simulated or self.physical) and getattr(
            self.service, "demo", None
        ) is self.simulated

    @property
    def furnace_enabled(self):
        return self.enabled and self.physical

    @property
    def mode(self):
        return ("simulated" if self.simulated else "physical") if self.enabled else "disabled"

    def _mode(self, value):
        if not self.enabled or value.get("demo") is not self.simulated:
            raise ValueError("The device mode does not match this Console. Submit a new request.")

    def _preview(self):
        return getattr(self.service, "turret_preview", None) if self.simulated else None

    def _description(self, device, *, fresh=False):
        if not self.enabled or device not in {
            "robot",
            "stage",
            "microscope",
            "thermal",
            "furnace",
            "vision",
        }:
            return {}
        try:
            if not self.service.dashboard_configured(device):
                return {}
            cache = getattr(self.service, "devices", None)
            if not fresh and cache is not None:
                value = cache.get(device).get("describe") or {}
                if not value and self.simulated:
                    value = self.service.console.read(device, "api/describe")
            elif fresh or self.simulated:
                value = self.service.console.read(device, "api/describe")
            else:
                return {}
            return value if value.get("demo") is self.simulated else {}
        except (AttributeError, KeyError, ValueError, ServiceError, OSError):
            return {}

    def _params_schema(self, spec, description):
        schema = copy.deepcopy(spec["params"])
        if spec["device"] == "furnace":
            bounds = (
                description.get("commands", {})
                .get("apply_setpoint", {})
                .get("params", {})
                .get("properties", {})
            )
            fields = schema["properties"]
            targets = [fields]
            if "steps" in fields:
                targets.append(fields["steps"]["items"]["properties"])
            for target in targets:
                for key in ("temperature_c", "ramp_rate_c_per_min"):
                    maximum = bounds.get(key, {}).get("maximum")
                    if type(maximum) in (int, float) and key in target:
                        target[key]["maximum"] = min(target[key]["maximum"], maximum)
            if "end_gas_until_c" in fields:
                fields["end_gas_until_c"]["maximum"] = min(
                    fields["end_gas_until_c"]["maximum"],
                    bounds.get("temperature_c", {}).get(
                        "maximum", fields["end_gas_until_c"]["maximum"]
                    ),
                )
        return schema

    def _validate_params(self, spec, params, description):
        validate(params, self._params_schema(spec, description))
        if spec["id"] == "vision.observe_slots":
            from vision.tray import validate_tray

            tray = validate_tray(params["tray"])
            if set(params["targets"]) - {slot["id"] for slot in tray["slots"]}:
                raise ValueError("A target names an unknown tray slot.")
        if spec["id"] == "stage.connect" and (params["port"] == "DEMO") is not self.simulated:
            raise ValueError("The stage port must match this Console's device mode.")
        if spec["device"] == "furnace" and "sequence" in spec["command"]:
            if ("end_gas" in params) != ("end_gas_until_c" in params):
                raise ValueError("Supply both end_gas and end_gas_until_c.")
            if any("gas" in step for step in params.get("steps", [])) and "end_gas" not in params:
                raise ValueError("A sequence with gas requires end_gas and end_gas_until_c.")

    def catalog(self):
        descriptions = {}
        for item in SPECS.values():
            device = item["device"]
            if (
                not item["disabled_reason"]
                and item["route"] == "command"
                and device not in descriptions
            ):
                descriptions[device] = self._description(device)
        result = []
        for row in descriptors():
            spec = SPECS[row["id"]]
            reason = spec["disabled_reason"]
            if not reason and not self.enabled:
                reason = PHYSICAL_DISABLED
            if (
                not reason
                and (spec["device"] == "furnace" or spec["id"] == "microscope.record_with_sequence")
                and not self.furnace_enabled
            ):
                reason = FURNACE_DISABLED
            if not reason and spec["route"] in {"turret", "frame"}:
                if self._preview() is None and (
                    spec["route"] == "frame" or not descriptions.get("microscope")
                ):
                    reason = "The reviewed turret image simulator is unavailable."
            if not reason and spec["route"] == "command":
                if spec["command"] not in descriptions.get(spec["device"], {}).get("commands", {}):
                    reason = "The reviewed service does not expose this action."
            if not reason and spec["route"] == "transfer" and not descriptions.get("stage"):
                reason = "The reviewed stage service is unavailable."
            if not reason and self.physical and spec["route"] == "transfer":
                command = (
                    "move_guarded"
                    if spec["command"] in {"jog", "step"}
                    else "move_to_position_guarded"
                )
                if command not in descriptions.get("stage", {}).get("commands", {}):
                    reason = "The stage service does not expose guarded motion."
            row["params"] = self._params_schema(spec, descriptions.get(spec["device"], {}))
            if spec["id"] == "stage.connect" and self.simulated:
                row["defaults"] = {"port": "DEMO"}
            row.update(available=not bool(reason), reason=reason, simulated=self.simulated)
            result.append(row)
        return result

    def choices(self, page):
        """Read saved choices without taking control or creating a draft."""
        if not self.enabled:
            return {
                "available": False,
                "reason": PHYSICAL_DISABLED,
                "actions": {},
                "constraints": {"stage": self._stage_constraints({})},
                "stage_context": self._stage_context({}, None, PHYSICAL_DISABLED),
            }
        actions = {}
        profiles = [
            {"value": row["id"], "label": row["name"]} for row in self.transfer.store.list()
        ]
        for action in ("select", "load", "delete"):
            actions["transfer.profile." + action] = {"id": copy.deepcopy(profiles)}
        selected = self.transfer.store.selected()
        with self.transfer.lock:
            draft = copy.deepcopy(self.transfer.pages.get(page, {}).get("draft"))
        actions["transfer.position"] = {
            "name": [
                {"value": name, "label": name}
                for name in sorted((selected or {}).get("positions", {}))
            ]
        }
        actions["transfer.position.delete"] = {
            "name": [
                {"value": name, "label": name}
                for name in sorted((draft or {}).get("positions", {}))
            ]
        }
        labels = {"out_stage": "Move Out Stage", "microscope": "Move to Microscope"}
        actions["transfer.endpoint.record"] = {
            "target": [{"value": key, "label": label} for key, label in labels.items()]
        }
        actions["transfer.endpoint.delete"] = {
            "target": [
                {"value": key, "label": labels[key]}
                for key in (draft or {}).get("stage_positions", {})
                if key in labels
            ]
        }
        actions["transfer.stage"] = {
            "target": [
                {"value": key, "label": labels[key]}
                for key in (selected or {}).get("stage_positions", {})
                if key in labels
            ]
        }
        preview = self._preview()
        if preview is not None:
            profiles = [
                {"value": row["profile_id"], "label": row["name"]} for row in preview.store.list()
            ]
        else:
            # Read only a reviewed service. Never acquire its lease.
            described = self._description("microscope")
            rows = (
                self.service.console.read("microscope", "api/turret-calibration").get(
                    "profiles", []
                )
                if described
                else []
            )
            profiles = [
                {"value": row["id"], "label": row["name"]}
                for row in rows
                if isinstance(row, dict) and "id" in row and "name" in row
            ]
        for action in ("select", "load", "delete"):
            actions["turret." + action] = {"id": copy.deepcopy(profiles)}
        entry = self._stage_cache()
        constraints = {"stage": self._stage_constraints(entry)}
        return {
            "available": True,
            "reason": "",
            "actions": actions,
            "constraints": constraints,
            "stage_context": self._stage_context(
                entry.get("state"), selected, constraints["stage"]["reason"]
            ),
        }

    def _stage_cache(self):
        entry = {}
        if self.enabled:
            try:
                cache = getattr(self.service, "devices", None)
                entry = cache.get("stage") if cache is not None else {}
            except (AttributeError, KeyError, OSError, RuntimeError, ValueError):
                pass
        return entry if isinstance(entry, dict) else {}

    def _stage_context(self, state, profile, reason):
        """Identify the stage and profile snapshots used for these choices."""
        preconditions = None
        if not reason:
            try:
                preconditions = {
                    "stage_reference": self._stage_reference(state),
                    "profile_id": profile.get("id") if profile else None,
                    "profile_digest": _digest(profile),
                }
            except (AttributeError, TypeError, ValueError):
                reason = "The stage cache has no usable reference."
        return {
            "version": 1,
            "available": not bool(reason),
            "reason": reason,
            "preconditions": preconditions,
        }

    def _stage_constraints(self, entry):
        """Describe cached encoder limits without changing the command contract."""
        state = entry.get("state")
        state = state if isinstance(state, dict) else {}
        phase = entry.get("phase") if isinstance(entry.get("phase"), str) else "unavailable"
        age = entry.get("age_s")
        age = age if finite(age) and age >= 0 else None
        current = bool(state) and phase == "ready" and age is not None and age <= STALE_S
        connected = state.get("connected") if type(state.get("connected")) is bool else None
        if not self.enabled:
            reason = PHYSICAL_DISABLED
        elif not state:
            reason = "The stage cache has no device state."
        elif phase != "ready":
            reason = f"The stage cache is {phase}."
        elif age is None:
            reason = "The stage cache age is unavailable."
        elif not current:
            reason = "The stage cache is stale."
        elif state.get("demo") is not self.simulated:
            reason = "The stage cache reports a different device mode."
        elif connected is not True:
            reason = (
                "The stage is disconnected."
                if connected is False
                else "Stage connection is unknown."
            )
        else:
            reason = ""
        names = ("bottom", "top", "insert")
        values = state.get("motors")
        axes = (
            {row["id"]: row for row in values if isinstance(row, dict) and row.get("id") in names}
            if isinstance(values, list)
            else {}
        )
        checked = {**state, "motors": list(axes.values())}
        all_idle = set(axes) == set(names) and all(idle(axis) for axis in axes.values())
        rows = []
        for name in names:
            axis = axes.get(name, {})
            row = {"id": name}
            for key in (
                "position_rotations",
                "lower_limit_rotations",
                "upper_limit_rotations",
                "tolerance",
                "cooldown",
            ):
                row[key] = axis.get(key) if finite(axis.get(key)) else None
            for key in ("calibration", "encoder", "fault", "mode"):
                row[key] = axis.get(key) if isinstance(axis.get(key), str) else None
            for key in ("positioning_ready", "telemetry_fresh"):
                row[key] = axis.get(key) if type(axis.get(key)) is bool else None
            lower, upper = row["lower_limit_rotations"], row["upper_limit_rotations"]
            if reason:
                axis_reason = reason
            elif not axis:
                axis_reason = "The stage cache has no state for this axis."
            elif not fresh(checked, axis):
                axis_reason = "Fresh, healthy encoder feedback is unavailable."
            elif lower is None or upper is None:
                axis_reason = "Calibrate this axis or set both travel limits."
            elif lower > upper:
                axis_reason = "The configured travel limits are invalid."
            elif not target_ready(checked, name, lower):
                axis_reason = "The axis reference or encoder tolerance is unavailable."
            elif not all_idle:
                axis_reason = "Wait for all stage motors to stop before starting a move."
            else:
                axis_reason = ""
            rows.append({**row, "available": not bool(axis_reason), "reason": axis_reason})
        return {
            "available": not bool(reason),
            "reason": reason,
            "phase": phase,
            "age_s": age,
            "fresh": current,
            "connected": connected,
            "axes": rows,
            "xy_blocked": None if reason else False,
            "xy_reason": reason,
        }

    def _spec(self, action):
        if not isinstance(action, str) or action not in SPECS:
            raise ValueError("Choose an action from the reviewed Console catalogue.")
        spec = SPECS[action]
        if spec["disabled_reason"]:
            raise ValueError(spec["disabled_reason"])
        if not self.enabled:
            raise ValueError(PHYSICAL_DISABLED)
        if (
            spec["device"] == "furnace" or action == "microscope.record_with_sequence"
        ) and not self.furnace_enabled:
            raise ValueError(FURNACE_DISABLED)
        return spec

    def _source_reference(self):
        if self.source is None or not self.simulated:
            return None
        state = self.source.snapshot()
        if state.get("quality") != "good":
            raise ValueError("The simulated source is stale. Resume fresh observations first.")
        stage = state.get("stage", {})
        revision = stage.get("reference_revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("The simulated source has no current reference revision.")
        return {
            "boot_id": state.get("boot_id"),
            "instance": stage.get("instance"),
            "contract_id": state.get("contract_id"),
            "reference_revision": revision,
        }

    def _stage_reference(self, state):
        return decision_stage_reference(state, allow_legacy=self.physical)

    def _references(self, state, profile):
        return {
            "stage_reference": self._stage_reference(state),
            "profile_id": profile.get("id") if profile else None,
            "profile_digest": _digest(profile),
            "source_reference": self._source_reference(),
        }

    def _check_reference(self, plan, state, page):
        self._mode(state)
        expected = plan["references"]
        current = self._references(state, self.transfer.store.selected())
        if current == expected:
            return
        # Taking our existing device lease can change its stop epoch once.
        current = copy.deepcopy(current)
        old = expected["stage_reference"]["stop_epoch"]
        new = current["stage_reference"]["stop_epoch"]
        with self.lock:
            transition = self.lease_epochs.get(page)
        if transition == (old, new) and self.service.console._owned("stage", page) is not None:
            current["stage_reference"]["stop_epoch"] = old
        if current != expected:
            raise ValueError(
                "The stage reference or selected profile changed. Submit a new request."
            )

    def _targets(self, spec, params, state, profile=None):
        command = spec["command"]
        if command == "coordinates":
            value = params["target"]
            targets = (
                sequence(value, stage_only=True) if "x" in value else [("insert", value["insert"])]
            )
        elif command == "position":
            targets = [(params["motor"], params["position"])]
        elif command in {"jog", "step"}:
            axis = next(item for item in state.get("motors", []) if item["id"] == params["motor"])
            target = axis.get("position_rotations")
            if type(target) not in (int, float):
                raise ValueError("Fresh stage encoder feedback is unavailable.")
            targets = [
                (
                    params["motor"],
                    target
                    + params.get("rotations", JOG_ROTATIONS)
                    * (1 if params["direction"] == "ccw" else -1),
                )
            ]
            if command == "step" and params["speed"] > axis.get("max_speed", 0):
                raise ValueError("The step speed exceeds the current motor speed limit.")
        elif command in {"fully_in", "fully_out"}:
            targets = [("insert", reference(state)["targets_rotations"][command])]
        elif command == "stage":
            saved = stage_reference(state, profile)
            target = params["target"]
            if not saved["ready_targets"][target]:
                raise ValueError(saved["message"])
            targets = sequence(saved["targets_rotations"][target], stage_only=True)
        else:
            if not compatible(profile) or params["name"] not in profile["positions"]:
                raise ValueError("Choose a named position from the selected rotation profile.")
            targets = sequence(profile["positions"][params["name"]]["position_rotations"])
        SampleTransfer._all_idle(state)
        if not all(target_ready(state, motor, value) for motor, value in targets):
            raise ValueError(
                "Calibrate the stage and keep targets within the current travel limits."
            )
        return targets

    def prepare(self, action, params, preconditions, page):
        """Validate and capture references without a lease, command, or profile write."""
        spec = self._spec(action)
        validate(params, spec["params"])
        if not isinstance(preconditions, dict) or set(preconditions) - {
            "stage_reference",
            "profile_id",
            "profile_digest",
        }:
            raise ValueError("Send only the documented reference and profile preconditions.")
        plan = {
            "action": action,
            "params": copy.deepcopy(params),
            "page": page,
            "simulated": self.simulated,
            "revision": spec["revision"],
        }
        if spec["route"] == "command":
            description = self._description(spec["device"], fresh=True)
            described = description.get("commands", {}).get(spec["command"])
            if not isinstance(described, dict):
                raise ValueError("The reviewed service does not expose this action.")
            self._validate_params(spec, params, description)
            plan["command_schema"] = described.get("params")
            if action != "robot.jog":
                validate(params, described["params"])
        if spec["device"] == "stage":
            if self.physical and spec["route"] == "transfer":
                command = (
                    "move_guarded"
                    if spec["command"] in {"jog", "step"}
                    else "move_to_position_guarded"
                )
                if command not in self._description("stage", fresh=True).get("commands", {}):
                    raise ValueError("The stage service does not expose guarded motion.")
            state = self.transfer.adapter.stage_state(page)
            self._mode(state)
            profile = (
                self.transfer.store.selected() if spec["route"] in {"transfer", "profile"} else None
            )
            if spec["route"] == "transfer":
                plan["targets"] = self._targets(spec, params, state, profile)
                plan["references"] = self._references(state, profile)
            elif spec["route"] == "profile":
                self._validate_profile_action(spec["command"], params)
                plan["references"] = self._references(state, profile)
            else:
                # Calibration may establish an absent reference. It still freezes source identity.
                plan["source_reference"] = self._source_reference()
        elif preconditions:
            raise ValueError("Stage preconditions apply only to stage actions.")
        if "references" in plan:
            for key, value in preconditions.items():
                current = plan["references"][key]
                if key == "stage_reference":
                    if not isinstance(value, dict) or set(value) - {
                        "reference_revision",
                        "stop_epoch",
                        "limits_digest",
                        "reference_source",
                    }:
                        raise ValueError("Send supported stage reference fields.")
                    if any(current.get(field) != expected for field, expected in value.items()):
                        raise ValueError("The requested stage reference is no longer current.")
                elif value != current:
                    raise ValueError("The requested profile is no longer current.")
        elif preconditions:
            raise ValueError("This action cannot apply stage motion preconditions.")
        if (
            spec["route"] in {"turret", "frame"}
            and self._preview() is None
            and (spec["route"] == "frame" or not self._description("microscope", fresh=True))
        ):
            raise ValueError("The reviewed turret image simulator is unavailable.")
        if action == "turret.upload":
            from microscope.turret_calibration import validate_profile as validate_turret

            if len(params["profile"].encode("utf-8")) > 2097152:
                raise ValueError("The turret profile exceeds 2 MiB.")
            validate_turret(json.loads(params["profile"]))
        return plan

    def _validate_profile_action(self, action, params):
        if action in {"profile/load", "profile/select", "profile/delete"}:
            self.transfer.store.load(params["id"])
        if action == "profile/upload":
            if len(params["profile"].encode("utf-8")) > 131072:
                raise ValueError("The transfer profile exceeds 128 KiB.")
            validate_profile(json.loads(params["profile"]))

    def _take(self, device, page, context):
        context.checkpoint()
        console = self.service.console
        if self.physical and not self._description(device, fresh=True):
            raise ValueError("The reviewed service is unavailable or its mode changed.")
        owned = console._owned(device, page)
        before = (
            self.transfer.adapter.stage_state(page).get("stop_epoch") if device == "stage" else None
        )
        console.take(device, page)
        context.checkpoint()
        if device == "stage" and owned is None:
            after = self.transfer.adapter.stage_state(page).get("stop_epoch")
            if type(before) is int and after == before + 1:
                with self.lock:
                    self.lease_epochs[page] = before, after

    def execute(self, plan, context):
        spec = self._spec(plan.get("action"))
        if (
            plan.get("revision") != spec["revision"]
            or plan.get("page") != context.page
            or plan.get("simulated") is not self.simulated
        ):
            raise ValueError("The prepared action does not match this execution context.")
        validate(plan.get("params"), spec["params"])
        context.checkpoint()
        params, page = plan["params"], context.page
        if "references" in plan:
            self._check_reference(plan, self.transfer.adapter.stage_state(page), page)
        if "source_reference" in plan and plan["source_reference"] != self._source_reference():
            raise ValueError("The simulated source reference changed. Submit a new request.")
        route = spec["route"]
        if route in {"turret", "frame"}:
            preview = self._preview()
            if preview is None:
                if route == "frame" or not self._description("microscope", fresh=True):
                    raise ValueError("The reviewed turret image simulator is unavailable.")
                self._take("microscope", page, context)
                result = context.dispatch(
                    self.service.console.operator,
                    "microscope",
                    page,
                    "api/turret-calibration/" + spec["command"],
                    params,
                )
                return {
                    "status": {"accepted": "succeeded", "refused": "failed"}.get(
                        result.get("outcome"), "unknown"
                    ),
                    "result": {"simulated": self.simulated, **result},
                    "error": result.get("error"),
                }
            if route == "frame":
                result = context.dispatch(preview.frame)
                return {
                    "status": "succeeded",
                    "result": {
                        "simulated": True,
                        "mime_type": "image/jpeg",
                        "image_base64": base64.b64encode(result).decode(),
                    },
                }
            result = context.dispatch(preview.action, page, spec["command"], params)
            return {"status": "succeeded", "result": {"simulated": self.simulated, "state": result}}
        self._take(spec["device"], page, context)
        if "references" in plan:
            self._check_reference(plan, self.transfer.adapter.stage_state(page), page)
        if route == "transfer":
            return self._execute_transfer(spec, plan, context)
        if route == "profile":
            result = context.dispatch(self.transfer.mutate, page, spec["command"], params)
            return {"status": "succeeded", "result": {"simulated": self.simulated, "state": result}}
        return self._execute_command(spec, plan, context)

    def _execute_transfer(self, spec, plan, context):
        adapter = _GuardedTransfer(self, plan, context)
        manager = SampleTransfer(self.transfer.store, adapter, demo=self.simulated)
        with self.lock:
            self.running[context.page] = manager
        try:
            params, command, page = plan["params"], spec["command"], context.page
            if command == "coordinates":
                manager.start_coordinates(page, params["target"])
            elif command == "position":
                state = manager._preflight(page)
                targets = self._targets(spec, params, state)
                operation = manager._begin(page, params, state.get("stop_epoch"), 1)
                threading.Thread(
                    target=self._run_position,
                    args=(manager, adapter, operation, targets[0]),
                    daemon=True,
                ).start()
            elif command == "jog":
                self._targets(spec, params, manager._preflight(page))
                manager.jog(page, params["motor"], params["direction"])
            elif command == "step":
                state = manager._preflight(page)
                targets = self._targets(spec, params, state)
                manager._launch(page, params, state, targets, jog=params["direction"])
            elif command in {"fully_in", "fully_out"}:
                manager.start(page, command)
            elif command == "stage":
                manager.start_stage(page, params["target"])
            else:
                manager.start_position(page, params["name"])
            while not manager.operation["done"].wait(0.05):
                if context.cancelled.is_set():
                    manager.cancel(page)
                progress = manager.operation_state(page)
                context.progress(
                    **{
                        key: progress.get(key)
                        for key in ("motor", "completed_axes", "total_axes", "phase")
                    }
                )
            result = manager.operation_state(page)
            status = {"completed": "succeeded", "failed": "failed", "stopped": "stopped"}.get(
                result["status"], "unknown"
            )
            if result["status"] == "stopped" and result["message"] != "Stopped.":
                status = "unknown"
            if adapter.uncertain:
                status = "unknown"
            if (
                status == "failed"
                and adapter.sent
                and (adapter.stop_outcome or {}).get("outcome") != "confirmed"
            ):
                status = "unknown"
            return {
                "status": status,
                "result": {"simulated": self.simulated, "operation": result},
                "error": result["message"] if status != "succeeded" else None,
            }
        finally:
            with self.lock:
                self.running.pop(context.page, None)

    def _run_position(self, manager, adapter, operation, target):
        """Match the Console per-axis move without changing transfer XY checks."""
        motor, position = target
        try:
            state = manager._stage(operation)
            self._targets(SPECS["stage.position"], operation["target"], state)
            axis = next(row for row in state["motors"] if row["id"] == motor)
            with manager.lock:
                operation.update(motor=motor, target_rotations=position, message=f"Moving {motor}.")
            if abs(axis["position_rotations"] - position) > axis["tolerance"]:
                manager._check(operation)
                result = adapter.position(
                    operation["page"],
                    motor=motor,
                    position=position,
                    speed=min(SPEED, axis.get("max_speed", SPEED)),
                    stop_epoch=operation["stop_epoch"],
                )
                if result.get("outcome") != "accepted":
                    raise ValueError(result.get("error") or "The stage command was refused.")
                if not result.get("command_id"):
                    adapter.uncertain = True
                    raise ValueError("The stage returned no command completion ID.")
                manager._wait(operation, motor, position, result["command_id"])
            manager._check(operation)
            with manager.lock:
                operation.update(
                    status="completed",
                    phase="complete",
                    completed_axes=1,
                    message="Position reached.",
                )
        except Cancelled:
            with manager.lock:
                operation.update(status="stopped", message="Sequence cancelled.")
            outcome = manager._stop_owned(operation)
            with manager.lock:
                if outcome and outcome.get("outcome") == "confirmed":
                    operation["message"] = "Stopped."
                elif outcome:
                    operation["message"] = "Sequence cancelled. Stage stop is unconfirmed."
        except Exception as error:
            with manager.lock:
                if not operation["cancelled"].is_set():
                    operation.update(status="failed", message=str(error))
            manager._stop_owned(operation)
        finally:
            operation["done"].set()

    def _execute_command(self, spec, plan, context):
        device, page, params = spec["device"], context.page, copy.deepcopy(plan["params"])
        service_description = self._description(device, fresh=True)
        description = service_description.get("commands", {}).get(spec["command"])
        if not isinstance(description, dict) or description.get("params") != plan.get(
            "command_schema"
        ):
            raise ValueError("The service command schema changed. Submit a new request.")
        self._validate_params(spec, params, service_description)
        if spec["id"] == "robot.jog":
            state = self.service.console.read("robot", "api/state")
            self._mode(state)
            actual = state.get("actual")
            if (
                state.get("feedback_fresh") is not True
                or not isinstance(actual, list)
                or len(actual) < 4
            ):
                raise ValueError("Fresh robot coordinates are unavailable.")
            targets = dict(zip(("x", "y", "z", "r"), actual[:4]))
            targets[params["axis"]] += params["direction"] * params["step"]
            params = {"targets": targets, "speed": params["speed"]}
        validate(params, description["params"])
        result = context.dispatch(
            self.service.console.command, device, page, spec["command"], params
        )
        if result.get("outcome") != "accepted":
            status = "failed" if result.get("outcome") == "refused" else "unknown"
            return {"status": status, "result": result, "error": result.get("error")}
        identifier = result.get("command_id")
        if not identifier:
            return {
                "status": "unknown",
                "result": result,
                "error": "The service returned no command completion ID.",
            }
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            context.checkpoint()
            if "source_reference" in plan and plan["source_reference"] != self._source_reference():
                raise ValueError("The simulated source reference changed during the command.")
            state = self.service.console.read(device, "api/state")
            self._mode(state)
            last = state.get("last_command") or {}
            if last.get("id") == identifier and last.get("result") in {
                "ok",
                "failed",
                "rejected",
                "stopped",
            }:
                status = {
                    "ok": "succeeded",
                    "failed": "failed",
                    "rejected": "failed",
                    "stopped": "stopped",
                }[last["result"]]
                if (
                    device == "stage"
                    and status == "succeeded"
                    and any(axis.get("cooldown", 0) > 0 for axis in state.get("motors", []))
                ):
                    context.cancelled.wait(0.05)
                    continue
                return {
                    "status": status,
                    "result": {"simulated": self.simulated, "command": last},
                    "error": last.get("message") if status == "failed" else None,
                }
            context.progress(command_id=identifier, phase="waiting")
            context.cancelled.wait(0.05)
        return {
            "status": "unknown",
            "result": result,
            "error": "The service did not confirm command completion within 600 seconds.",
        }

    def cancel(self, plan, page):
        spec = SPECS.get(plan.get("action"))
        if spec is None or not self.enabled:
            return {"status": "unknown", "outcome": "unavailable"}
        if spec["device"] == "furnace":
            # Control loss preserves the furnace sequence and orphan-grace contract.
            # The caller releases this page's lease after command cleanup.
            return {
                "status": "unknown",
                "outcome": "lifecycle_preserved",
                "simulated": self.simulated,
            }
        with self.lock:
            manager = self.running.get(page)
        if manager is not None:
            manager.cancel(page)
        if spec["route"] in {"frame", "profile"} or (
            spec["route"] == "turret" and self._preview() is not None
        ):
            return {"status": "unknown", "outcome": "unconfirmed", "simulated": self.simulated}
        try:
            # Cleanup never acquires a lease or stops a different holder's operation.
            if self.service.console._owned(spec["device"], page) is None:
                return {"status": "unknown", "outcome": "lease_lost", "simulated": self.simulated}
            result = self.service.console.stop(spec["device"])
        except Exception:
            result = {"outcome": "unknown"}
        return {
            **result,
            "status": "stopped" if result.get("outcome") == "confirmed" else "unknown",
            "simulated": self.simulated,
        }

    def stop(self, target):
        """Send a fixed emergency Stop without acquiring control."""
        devices = ("stage", "robot", "microscope")
        gas = ("gas_0", "gas_1", "gas_2")
        if target not in {*devices, *gas, "all_motion", "gas_all", "furnace"}:
            raise ValueError("Choose a listed device or Stop group.")
        if not self.enabled or (
            target in {*gas, "gas_all", "furnace"} and not self.furnace_enabled
        ):
            return {"device": target, "outcome": "disabled", "simulated": False}
        if target == "all_motion":
            return {"target": target, "results": {name: self.stop(name) for name in devices}}
        if target == "gas_all":
            return {"target": target, "results": {name: self.stop(name) for name in gas}}
        # Stop must work when discovery or the command schema is unavailable.
        device = "furnace" if target in gas else target
        if not self.service.dashboard_configured(device):
            return {"device": target, "outcome": "unavailable", "simulated": self.simulated}
        if target == "stage":
            with self.lock:
                managers = tuple(self.running.values())
            for manager in managers:
                manager.cancel()
        try:
            result = (
                self._gas_stop(int(target[-1]))
                if target in gas
                else self.service.console.stop(target)
            )
        except Exception:
            result = {"device": target, "outcome": "unknown"}
        return {**result, "simulated": self.simulated}

    def _gas_stop(self, channel):
        """Use the same fixed channel route as the Console gas Stop button."""
        adapter = self.service.console_client("furnace", "console-stop-" + uuid4().hex)
        if adapter is None:
            return {"device": f"gas_{channel}", "outcome": "unavailable"}
        try:
            value = adapter.stop(gas_channel=channel)
            outcome = value.get("stop") if isinstance(value, dict) else None
            return {
                "device": f"gas_{channel}",
                "outcome": outcome
                if outcome in {"confirmed", "pending", "unconfirmed"}
                else "unknown",
            }
        finally:
            adapter.close()

    def release_page(self, page):
        """Release only this old agent page's leases after its work has ended."""
        if not self.enabled:
            return {"outcome": "disabled", "simulated": False, "devices": {}}
        if not isinstance(page, str) or not page.startswith("agent-"):
            raise ValueError("Release requires an agent page identity.")
        with self.lock:
            self.lease_epochs.pop(page, None)
        results = self.service.console.release_all(page)
        preview = self._preview()
        if preview is not None:
            with preview.lock:
                if preview.session and preview.session.get("client") == page:
                    preview.session = None
        return {
            "outcome": "released"
            if all(value == "released" for value in results.values())
            else "unknown",
            "simulated": self.simulated,
            "devices": results,
        }

    def observations(self):
        """Return simulator telemetry only. Never infer sample locations from motion."""
        if not self.simulated or getattr(self.service, "demo", False) is not True:
            return {}
        result = {}
        try:
            state = self.transfer.adapter.stage_state("agent-observation")
            if state.get("demo") is True:
                result["stage"] = state
        except Exception:
            pass
        preview = self._preview()
        if preview is not None:
            result["turret"] = preview.state("agent-observation")
        return result

    def close(self):
        with self.lock:
            running = list(self.running.values())
        for manager in running:
            manager.close()
        if self.owned_resources is not None:
            self.transfer.close()
            self.service.devices.close()
            self.service.console.close()
            stage, temporary = self.owned_resources
            stage.close()
            temporary.cleanup()
            self.owned_resources = None


def create_backend(service=None, mock=False, source=None, *, allow_physical=False):
    return ActionBackend(service, mock=mock, source=source, allow_physical=allow_physical)
