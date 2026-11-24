"""Dispatch Stops independently of operator grants, normal work and audit storage.

The caller supplies a fixed device dispatcher and a server-resolved identity.
Every delivery dispatches Stop. A shared request ID groups transport copies only;
it never suppresses another Stop or replays one after a restart.
"""

import copy
import re
import threading
import time
import uuid
from collections import OrderedDict

from labkit.errors import ServiceError

from .stop_store import HISTORY_LIMIT, StopStore, canonical

DEVICES = ("stage", "robot", "microscope")
TARGETS = (*DEVICES, "all_motion", "furnace", "gas_all", "gas_0", "gas_1", "gas_2")
OBSERVE_TARGETS = (*TARGETS, "thermal", "vision")
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
DELIVERY_LIMIT = 32
STATUSES = frozenset({"pending", "confirmed", "unconfirmed", "unknown", "disabled"})


def stop_devices(target):
    if target == "all_motion":
        return DEVICES
    if target == "gas_all":
        return ("gas_0", "gas_1", "gas_2")
    return (target,)


def _text(value, limit=256):
    return value[:limit] if isinstance(value, str) else None


def _sender(value):
    value = value if isinstance(value, dict) else {}
    verified = value.get("verified") is True
    kind = value.get("kind")
    if kind not in {"agent", "human", "system"} or not verified:
        return {
            "kind": "unverified",
            "name": "Unverified sender",
            "role": "unknown",
            "verified": False,
        }
    result = {"kind": kind, "verified": True}
    for key in ("id", "client_id", "session_id", "name", "role"):
        text = _text(value.get(key), 120)
        if text:
            result[key] = text
    result.setdefault("name", {"agent": "Agent", "human": "Console user", "system": "System"}[kind])
    result.setdefault("role", "unknown")
    return result


def _outcome(device, value):
    value = value if isinstance(value, dict) else {}
    outcome = value.get("outcome", "unknown")
    if not isinstance(outcome, str) or outcome not in STATUSES:
        outcome = "unknown"
    result = {"device": device, "outcome": outcome}
    if value.get("simulated") is True:
        result["simulated"] = True
    error = _text(value.get("error")) or _text(value.get("reason"))
    if error:
        result["error"] = error
    operation_id = _text(value.get("operation_id"), 128)
    if operation_id:
        result["operation_id"] = operation_id
    return result


def _status(values):
    values = set(values)
    if not values or "unknown" in values:
        return "unknown"
    if "unconfirmed" in values:
        return "unconfirmed"
    if "pending" in values:
        return "pending"
    if values == {"confirmed"}:
        return "confirmed"
    if values == {"disabled"}:
        return "disabled"
    return "unconfirmed"


class StopHost:
    def __init__(self, dispatch, path=None, publish=None):
        self.dispatch, self.path, self.publish = dispatch, path, publish
        self.lock = threading.RLock()
        self.records = OrderedDict()
        self.groups = {}
        self.dirty = set()
        self.saved = {}
        self.ready = False
        self.audit_error = None
        self.audit_errors = {}
        self.dropped = 0
        self.closed = False
        self._audit_stop = False
        self._wake = threading.Event()
        self._worker_lock = threading.Lock()
        self._threads = set()
        self._audit = threading.Thread(
            target=self._audit_loop, name="agent-stop-audit", daemon=True
        )
        self._audit.start()

    @staticmethod
    def _body(body, *, observed=False):
        if (
            not isinstance(body, dict)
            or set(body) != {"request_id", "target"}
            or not isinstance(body["request_id"], str)
            or not REQUEST_ID.fullmatch(body["request_id"])
            or body["target"] not in (OBSERVE_TARGETS if observed else TARGETS)
        ):
            raise ServiceError(400, "Send a request_id and a supported Stop target.")
        return body["request_id"], body["target"]

    def _audit_status(self, record=None):
        pending = sum(
            self.saved.get(item["id"]) != item["revision"] for item in self.records.values()
        )
        if record is not None and self.saved.get(record["id"]) == record["revision"]:
            status = "saved"
        elif self.audit_error:
            status = "failed"
        elif not self.ready:
            status = "starting"
        else:
            status = "pending" if record is not None or pending else "saved"
        return {
            "status": status,
            "pending": pending,
            "error": self.audit_error,
            "dropped_records": self.dropped,
        }

    def _public(self, record, summary=False):
        result = copy.deepcopy(
            {
                key: value
                for key, value in record.items()
                if not key.startswith("_") and not (summary and key == "deliveries")
            }
        )
        result["audit"] = self._audit_status(record)
        result["deliveries_truncated"] = record["delivery_count"] > len(record["deliveries"])
        if summary:
            result.pop("deliveries", None)
        return result

    def _refresh(self, record):
        values = {device: [] for device in stop_devices(record["target"])}
        for result in record["_archived"]:
            values[result["device"]].append(result["outcome"])
        for delivery in record["deliveries"]:
            for result in delivery["outcomes"]:
                values[result["device"]].append(result["outcome"])
            delivery["status"] = _status(item["outcome"] for item in delivery["outcomes"])
        record["devices"] = [
            {"device": device, "outcome": _status(outcomes)} for device, outcomes in values.items()
        ]
        record["status"] = _status(result["outcome"] for result in record["devices"])
        record["transports"] = sorted({item["transport"] for item in record["deliveries"]})

    def _remember(self, record):
        self.records[record["id"]] = record
        self.records.move_to_end(record["id"])
        while len(self.records) > HISTORY_LIMIT:
            identifier, oldest = self.records.popitem(last=False)
            group = (oldest["request_id"], oldest["target"])
            if self.groups.get(group) == identifier:
                self.groups.pop(group, None)
            if self.saved.get(identifier) != oldest["revision"]:
                self.dropped += 1
            self.dirty.discard(identifier)
            self.saved.pop(identifier, None)
            self.audit_errors.pop(identifier, None)

    @staticmethod
    def _archive(record, outcomes):
        archived = {item["device"]: item["outcome"] for item in record["_archived"]}
        for item in outcomes:
            outcome = "unknown" if item["outcome"] == "pending" else item["outcome"]
            archived[item["device"]] = _status([archived.get(item["device"], outcome), outcome])
        record["_archived"] = [
            {"device": device, "outcome": outcome} for device, outcome in archived.items()
        ]

    def _trim_deliveries(self, record):
        while len(record["deliveries"]) > DELIVERY_LIMIT:
            removed = record["deliveries"].pop(0)
            self._archive(record, removed["outcomes"])

    def _delivery(self, body, sender, transport, *, observed=False):
        request_id, target = self._body(body, observed=observed)
        sender = _sender(sender)
        transport = _text(transport, 40) or "unknown"
        devices = stop_devices(target)
        now = time.time()
        delivery = {
            "id": uuid.uuid4().hex,
            "transport": transport,
            "sender": sender,
            "requested_at": now,
            "finished_at": None,
            "status": "pending",
            "outcomes": [{"device": device, "outcome": "pending"} for device in devices],
        }
        with self.lock:
            if self.closed:
                raise ServiceError(503, "This Stop host has closed. Use the fallback Stop path.")
            group = (request_id, target)
            record = self.records.get(self.groups.get(group))
            if record is None:
                record = {
                    "id": uuid.uuid4().hex,
                    "request_id": request_id,
                    "target": target,
                    "sender": sender,
                    "requested_at": now,
                    "updated_at": now,
                    "revision": 0,
                    "delivery_count": 0,
                    "deliveries": [],
                    "_archived": [],
                }
                self.groups[group] = record["id"]
            elif record["sender"] != sender:
                record["sender"] = {
                    "kind": "mixed",
                    "name": "Multiple senders",
                    "role": "mixed",
                    "verified": False,
                }
            record["deliveries"].append(delivery)
            record["delivery_count"] += 1
            self._trim_deliveries(record)
            record["revision"] += 1
            record["updated_at"] = now
            self._refresh(record)
            self._remember(record)
            return record, delivery

    def submit(self, body, sender=None, transport="http"):
        _request_id, target = self._body(body)
        if self.closed:
            raise ServiceError(503, "This Stop host has closed. Use the fallback Stop path.")
        ready, context = threading.Event(), []
        for device in stop_devices(target):
            worker = threading.Thread(
                target=self._dispatch,
                args=(device, ready, context),
                name="agent-device-stop",
                daemon=True,
            )
            with self._worker_lock:
                self._threads = {thread for thread in self._threads if thread.is_alive()}
                self._threads.add(worker)
            worker.start()
        try:
            record, delivery = self._delivery(body, sender, transport)
            context.extend((record, delivery))
            with self.lock:
                return self._public(record)
        finally:
            ready.set()

    def _dispatch(self, device, ready, context):
        try:
            result = _outcome(device, self.dispatch(device))
        except Exception:
            result = {
                "device": device,
                "outcome": "unknown",
                "error": "Device Stop was not confirmed.",
            }
        # Even a busy history reader cannot postpone the device call above.
        ready.wait()
        if context:
            self._complete(*context, device, result)

    def _complete(self, record, delivery, device, result):
        with self.lock:
            delivery["outcomes"] = [
                result if item["device"] == device else item for item in delivery["outcomes"]
            ]
            if all(item["outcome"] != "pending" for item in delivery["outcomes"]):
                delivery["finished_at"] = time.time()
            if delivery not in record["deliveries"]:
                self._archive(record, [result])
            record["revision"] += 1
            record["updated_at"] = time.time()
            self._refresh(record)
            if record["id"] in self.records:
                self.dirty.add(record["id"])
        # Audit work begins only after the device call returns. It cannot delay dispatch.
        self._wake.set()

    def observe(self, body, sender=None, transport="http", outcomes=None):
        """Record an existing Stop after dispatch. Do not send another device call."""
        record, delivery = self._delivery(body, sender, transport, observed=True)
        if isinstance(outcomes, dict) and "device" in outcomes:
            outcomes = [outcomes]
        if not isinstance(outcomes, list):
            outcomes = []
        for item in tuple(delivery["outcomes"]):
            device = item["device"]
            result = next(
                (
                    value
                    for value in outcomes
                    if isinstance(value, dict) and value.get("device") == device
                ),
                {},
            )
            self._complete(record, delivery, device, _outcome(device, result))
        with self.lock:
            return self._public(record)

    def history(self):
        with self.lock:
            return {
                "items": [
                    self._public(record, summary=True) for record in reversed(self.records.values())
                ],
                "count": len(self.records),
                "audit": self._audit_status(),
            }

    def get(self, identifier):
        with self.lock:
            record = self.records.get(identifier)
            if record is None:
                record = next(
                    (
                        item
                        for item in self.records.values()
                        if identifier in item.get("previous_ids", [])
                    ),
                    None,
                )
            if record is None:
                matches = [
                    item for item in self.records.values() if item["request_id"] == identifier
                ]
                if len(matches) > 1:
                    raise ServiceError(409, "Use the Stop record ID to select this target.")
                record = matches[0] if matches else None
            if record is None:
                raise ServiceError(404, "This Stop is not in the retained history.")
            return self._public(record)

    def _restore(self, records):
        with self.lock:
            for record in reversed(records):
                if (
                    record.get("target") not in OBSERVE_TARGETS
                    or record.get("status") not in STATUSES
                ):
                    raise ValueError("A saved Stop record is invalid.")
                if record["id"] in self.records:
                    continue
                record.setdefault("_archived", [])
                changed = False
                for delivery in record["deliveries"]:
                    for result in delivery["outcomes"]:
                        if result["outcome"] == "pending":
                            result["outcome"] = "unknown"
                            result["error"] = "The Console restarted before Stop confirmation."
                            changed = True
                    if changed:
                        delivery["status"] = _status(
                            item["outcome"] for item in delivery["outcomes"]
                        )
                if changed:
                    record["revision"] += 1
                    self._refresh(record)
                group = (record["request_id"], record["target"])
                current = self.records.get(self.groups.get(group))
                if current is not None:
                    # A Stop can arrive while the audit worker is still opening storage.
                    # Merge its earlier transport copies without waiting or resending.
                    current["previous_ids"] = [
                        *record.get("previous_ids", []),
                        record["id"],
                        *current.get("previous_ids", []),
                    ][-32:]
                    current["requested_at"] = min(current["requested_at"], record["requested_at"])
                    current["delivery_count"] += record["delivery_count"]
                    current["deliveries"] = [*record["deliveries"], *current["deliveries"]]
                    self._archive(current, record["_archived"])
                    self._trim_deliveries(current)
                    if current["sender"] != record["sender"]:
                        current["sender"] = {
                            "kind": "mixed",
                            "name": "Multiple senders",
                            "role": "mixed",
                            "verified": False,
                        }
                    current["revision"] = max(current["revision"], record["revision"]) + 1
                    self._refresh(current)
                    self.dirty.add(current["id"])
                    continue
                if changed:
                    self.dirty.add(record["id"])
                else:
                    self.saved[record["id"]] = record["revision"]
                self.records[record["id"]] = record
                self.groups[group] = record["id"]
            self.records = OrderedDict(
                sorted(self.records.items(), key=lambda item: item[1]["updated_at"])
            )
            while len(self.records) > HISTORY_LIMIT:
                identifier, record = self.records.popitem(last=False)
                group = (record["request_id"], record["target"])
                if self.groups.get(group) == identifier:
                    self.groups.pop(group, None)
                self.dirty.discard(identifier)
                self.saved.pop(identifier, None)
                self.audit_errors.pop(identifier, None)
            self.ready = True
            self.audit_error = next(iter(self.audit_errors.values()), None)

    def _audit_loop(self):
        store = None
        try:
            while True:
                self._wake.wait(0.1)
                self._wake.clear()
                if store is None:
                    try:
                        store = StopStore(self.path)
                        self._restore(store.load())
                    except Exception as error:
                        if store is not None:
                            store.close()
                            store = None
                        with self.lock:
                            self.audit_error = _text(str(error)) or "Stop history is unavailable."
                with self.lock:
                    identifiers = tuple(self.dirty)
                    closed = self._audit_stop
                if store is not None:
                    for identifier in identifiers:
                        with self.lock:
                            record = copy.deepcopy(self.records.get(identifier))
                        if record is None:
                            continue
                        try:
                            canonical(record)
                            store.save(record)
                        except Exception as error:
                            with self.lock:
                                self.audit_error = (
                                    _text(str(error)) or "The Stop audit could not be saved."
                                )
                                self.audit_errors[identifier] = self.audit_error
                            continue
                        with self.lock:
                            current = self.records.get(identifier)
                            if current is not None:
                                self.saved[identifier] = record["revision"]
                            if current and current["revision"] == record["revision"]:
                                self.dirty.discard(identifier)
                            self.audit_errors.pop(identifier, None)
                            self.audit_error = next(iter(self.audit_errors.values()), None)
                            report = self._public(current, summary=True) if current else None
                        if self.publish is not None and report is not None:
                            try:
                                self.publish("stop", report)
                            except Exception:
                                pass
                if closed:
                    return
        finally:
            if store is not None:
                store.close()

    def close(self):
        deadline = time.monotonic() + 2
        with self.lock:
            self.closed = True
        with self._worker_lock:
            workers = tuple(self._threads)
        for worker in workers:
            worker.join(max(0, deadline - time.monotonic()))
        self._audit_stop = True
        self._wake.set()
        self._audit.join(max(0, deadline - time.monotonic()))
