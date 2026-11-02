"""Audited, single-use proposals. No instrument is contacted by this module."""

import copy
import hashlib
import json
import math
import threading
import time
import uuid
from dataclasses import asdict, replace

from labkit.errors import ServiceError
from labkit.lease import validate_client

from .recipe import validate
from .stations import Workspace
from .zones import prepare_zones

EXPIRY_S = 1800
MAX_PENDING = 3
MAX_HISTORY = 100
ALLOWED = frozenset(
    {
        "wait",
        "fail",
        "robot.enable",
        "robot.move",
        "robot.stop",
        "sample.record",
        "robot.pick",
        "robot.place",
        "robot.transfer",
        "crucible.load",
        "crucible.unload",
    }
)
HOST_LIMITS = {"load_max_c", "unload_max_c", "awake_s", "max_age_s"}


def _number(value, label):
    try:
        valid = type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label} must be finite.")
    return float(value)


def _digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)
    if len(raw) > 400000:
        raise ValueError("The proposal definition exceeds the audit size limit.")
    return hashlib.sha256(raw.encode()).hexdigest()


def _public(service, value):
    serialized = json.dumps(value, ensure_ascii=True, allow_nan=False)
    secrets = [
        value
        for key, value in service.settings.items()
        if key.endswith(("_TOKEN", "_URL")) and isinstance(value, str) and value
    ]
    secrets.extend(service.approvals.private_tokens)
    if any(json.dumps(secret, ensure_ascii=True)[1:-1] in serialized for secret in secrets):
        raise ValueError("A proposal must not contain private credentials or service URLs.")


def prepare(service, name, overrides):
    """Resolve the catalog once, then enforce policy on every effective step."""
    if not isinstance(overrides, dict):
        raise ValueError("Recipe params must be an object.")
    definition = service.catalog.read(name)
    recipe = validate(definition, service.registry, overrides)
    # A differently named recipe parameter must not bypass a step's policy.
    defaults = {
        key: spec["default"] for key, spec in definition["params"].items() if "default" in spec
    }
    steps = []
    for entry, step in zip(definition["steps"], recipe.steps, strict=True):
        if step.skill not in ALLOWED:
            raise ValueError("This skill is not supported by the approval policy.")
        values = copy.deepcopy(step.params)
        for key, value in values.items():
            if "speed" in key.lower():
                original = entry["params"].get(key)
                if isinstance(original, str) and original.startswith("${"):
                    original = defaults.get(original[2:-1])
                if original is None:
                    original = service.registry[step.skill].signature.parameters[key].default
                if _number(value, key) > _number(original, "Recipe default speed"):
                    raise ValueError("A proposal cannot exceed the recipe default speed.")
        if step.skill == "robot.move":
            bounds = Workspace.from_settings(service.settings).bounds
            if any(not low <= values[axis] <= high for axis, (low, high) in bounds.items()):
                raise ValueError("Robot coordinates exceed the configured workspace bounds.")
        if step.skill.startswith("crucible."):
            for key in HOST_LIMITS & values.keys():
                # Reject explicit limits, including aliases through arbitrary recipe params.
                if key in entry["params"] or key in overrides:
                    raise ValueError("Crucible safety limits must come from host settings.")
                setting = "MG400_ORCH_CRUCIBLE_" + key.upper()
                try:
                    value = float(service.settings[setting])
                except (KeyError, TypeError, ValueError, OverflowError):
                    raise ValueError(f"Set an explicit finite value for {setting}.") from None
                value = _number(value, setting)
                if (
                    (key.endswith("max_c") and value < 0)
                    or (key == "awake_s" and not 0 < value <= 60)
                    or (key == "max_age_s" and value <= 0)
                ):
                    raise ValueError("The configured crucible safety limit is invalid.")
                values[key] = value
        steps.append(replace(step, params=values))
    recipe = replace(recipe, steps=tuple(steps))
    plan = prepare_zones(recipe, service.registry, service.settings)
    visible = {"definition": definition, "effective": asdict(recipe)}
    _public(service, visible)
    return (recipe, plan), visible, _digest(visible)


class Approvals:
    """Pending authority exists only in this process; audit records survive restart."""

    def __init__(self, service, *, clock=time.monotonic, wall=time.time):
        self.service, self.clock, self.wall = service, clock, wall
        self.lock = threading.RLock()
        self.records = {}
        self.deadlines = {}
        self.private_tokens = ()

    def _audit(self, caller, event, record):
        # A failed audit prevents publication and execution. Never use an event log
        # as a substitute for the durable transaction in the run database.
        try:
            self.service.store.audit_call(caller, "approval." + event, record)
        except Exception:
            raise ServiceError(
                503, "Approval audit is unavailable. Check the proposal and run state."
            ) from None

    def _expire(self):
        for identifier, record in self.records.items():
            if record["decision"] == "pending" and self.clock() >= self.deadlines[identifier]:
                expired = {**record, "decision": "expired", "decided_at": self.wall()}
                self._audit("approval-service", "expired", expired)
                self.records[identifier] = expired

    def propose(self, data, requester):
        validate_client(requester)
        if not isinstance(data, dict) or set(data) != {"recipe", "params", "reason"}:
            raise ValueError("Supply recipe, params and reason only.")
        reason = data["reason"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("Supply a reason of 1 to 2000 characters.")
        # No proposal is stored before the complete policy check succeeds.
        _, visible, fingerprint = prepare(self.service, data["recipe"], data["params"])
        with self.lock:
            self.service._ready()
            self._expire()
            if sum(row["decision"] == "pending" for row in self.records.values()) >= MAX_PENDING:
                raise ServiceError(409, "At most 3 proposals may be pending.")
            now, deadline = self.wall(), self.clock() + EXPIRY_S
            record = {
                "id": uuid.uuid4().hex,
                "recipe": data["recipe"],
                "params": copy.deepcopy(data["params"]),
                "requester": requester,
                "reason": reason,
                "created_at": now,
                "expires_at": now + EXPIRY_S,
                "decision": "pending",
                "decider": None,
                "decided_at": None,
                "command_id": None,
                "fingerprint": fingerprint,
                **visible,
                "presence_required": True,
            }
            _public(self.service, record)
            self._audit(requester, "proposed", record)
            self.records[record["id"]] = copy.deepcopy(record)
            self.deadlines[record["id"]] = deadline
            self._expire()
            while len(self.records) > MAX_HISTORY:
                oldest = next(
                    key for key, row in self.records.items() if row["decision"] != "pending"
                )
                del self.records[oldest], self.deadlines[oldest]
            return copy.deepcopy(self.records[record["id"]])

    def get(self, identifier):
        with self.lock:
            self._expire()
            if identifier not in self.records:
                raise ServiceError(404, "Proposal unavailable or invalidated by service restart.")
            return copy.deepcopy(self.records[identifier])

    def list(self):
        with self.lock:
            self._expire()
            return copy.deepcopy(list(reversed(self.records.values())))

    def decide(self, identifier, data, decider):
        validate_client(decider)
        if (
            not isinstance(data, dict)
            or set(data) != {"decision", "present", "fingerprint"}
            or not isinstance(data["decision"], str)
            or data["decision"] not in {"approve", "reject"}
            or type(data["present"]) is not bool
        ):
            raise ValueError("Supply decision, present and the displayed fingerprint.")
        with self.lock:
            self.service._ready()
            record = self.get(identifier)
            if record["decision"] != "pending":
                raise ServiceError(409, "This proposal no longer permits a decision.")
            if decider == record["requester"]:
                raise ServiceError(403, "A requester cannot decide its own proposal.")
            if data["fingerprint"] != record["fingerprint"]:
                raise ServiceError(409, "Review the complete current proposal before deciding.")
            approved = data["decision"] == "approve"
            if approved and data["present"] is not True:
                raise ServiceError(
                    400, "Confirm a person is in the lab and can reach a stop control."
                )
            if approved:
                ticket = self.service.approval_ticket(decider)
                prepared, _, fingerprint = prepare(self.service, record["recipe"], record["params"])
                if fingerprint != record["fingerprint"]:
                    raise ServiceError(
                        409, "Recipe or host policy changed. Reject and propose again."
                    )
            record.update(
                decision="approved" if approved else "rejected",
                decider=decider,
                decided_at=self.wall(),
                present=data["present"],
            )
            self._audit(decider, record["decision"], record)
            # Consume before dispatch. A busy service, lost lease or failed reply
            # never leaves reusable authority, even when dispatch outcome is unknown.
            self.records[identifier] = copy.deepcopy(record)
            if approved:
                if self.clock() >= self.deadlines[identifier]:
                    record.update(decision="expired", dispatch="not-started")
                    self.records[identifier] = copy.deepcopy(record)
                    self._audit(decider, "expired", record)
                    raise ServiceError(409, "The proposal expired before dispatch.")
                try:
                    record["command_id"] = self.service.run(
                        prepared, decider, approval_ticket=ticket
                    )
                except Exception:
                    record["dispatch"] = "not-confirmed"
                    self.records[identifier] = copy.deepcopy(record)
                    self._audit(decider, "dispatch-not-confirmed", record)
                    raise
                record["dispatch"] = "accepted"
                self.records[identifier] = copy.deepcopy(record)
                self._audit(decider, "dispatch-accepted", record)
            return copy.deepcopy(record)
