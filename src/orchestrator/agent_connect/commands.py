"""Run durable agent operations with bounded concurrency and explicit uncertainty.

Persist each dispatch intent before the device call. A lost reply can leave an
unknown outcome. Neither a duplicate request nor a restart replays that call.
"""

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

from labkit.errors import ServiceError

from .command_store import TERMINAL, CommandStore, canonical

REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
MAX_BODY_BYTES = 6 * 1024 * 1024 + 1024
FIELDS = frozenset(
    {"request_id", "boot_id", "contract_id", "control_epoch", "action", "params", "preconditions"}
)


def _copy(value):
    return json.loads(canonical(value))


def _public(record):
    return _copy({key: value for key, value in record.items() if not key.startswith("_")})


def _summary(record):
    hidden = {"params", "preconditions", "progress", "result", "stop_result"}
    summary = _copy(
        {
            key: value[:512] if isinstance(value, str) else value
            for key, value in record.items()
            if not key.startswith("_") and key not in hidden
        }
    )
    progress = {}
    for key, value in record.get("progress", {}).items():
        if type(value) not in {str, int, float, bool, type(None)}:
            continue
        candidate = {**progress, key[:64]: value[:256] if isinstance(value, str) else value}
        if len(canonical(candidate).encode()) <= 2048:
            progress = candidate
    summary["progress"] = progress
    return summary


@dataclass
class _Operation:
    caller: object
    record: dict
    cancelled: threading.Event = field(default_factory=threading.Event)
    plan: dict | None = None
    worker: threading.Thread | None = None
    cancel_worker: threading.Thread | None = None
    execution_done: bool = False
    cancel_done: bool = False
    outcome: dict | None = None
    stop_outcome: dict | None = None


@dataclass
class _Page:
    caller: object
    epoch: str
    invalid: bool = False
    releasing: bool = False


class CommandInterrupted(ServiceError):
    """The host refused the next step before its device call."""


class CommandContext:
    """Supply cancellation checks and a durable boundary for each device write."""

    def __init__(self, host, operation):
        self._host, self._operation = host, operation
        self.page = operation.record["page"]
        self.cancelled = operation.cancelled

    def checkpoint(self):
        try:
            self._host._checkpoint(self._operation)
        except ServiceError as error:
            raise CommandInterrupted(error.status, error.message) from error

    def progress(self, **fields):
        fields = _copy(fields)
        with self._host.lock:
            self._operation.record["progress"].update(fields)
            self._host._save(self._operation)
        self._host._publish(self._operation)

    def dispatch(self, function, *args, **kwargs):
        self.checkpoint()
        with self._host.lock:
            if self.cancelled.is_set() or self._host._closed:
                raise CommandInterrupted(409, "The operation was cancelled before dispatch.")
            record = self._operation.record
            record["dispatch_count"] += 1
            record["last_dispatch_at"] = time.time()
            self._host._save(self._operation)
        # Never hold an engine or authority lock across device I/O.
        self.checkpoint()
        return function(*args, **kwargs)


class CommandHost:
    def __init__(self, authority_getter, backend, path=None, publish=None):
        self.authority_getter, self.backend, self.publish = authority_getter, backend, publish
        self.lock = threading.RLock()
        self._active = None
        self._closed = False
        self._store_error = None
        self._store = None
        self._threads = set()
        self._pages = {}
        self._cleanup_results = {}
        self._stop_revisions = {
            device: object()
            for device in ("robot", "stage", "microscope", "furnace", "thermal", "vision")
        }
        self._wake = threading.Event()
        try:
            self._store = CommandStore(path)
        except Exception as error:
            self._store_error = error
        self._poller = threading.Thread(target=self._watch, name="agent-command-poll", daemon=True)
        self._poller.start()

    def _storage(self, method, *args):
        if self._store_error is not None or self._store is None:
            raise ServiceError(503, "The command history is unavailable. No new commands can run.")
        try:
            return getattr(self._store, method)(*args)
        except Exception as error:
            self._store_error = error
            raise ServiceError(
                503, "The command history is unavailable. No new commands can run."
            ) from error

    def _authenticate(self, caller):
        if caller is None:
            raise ServiceError(401, "Connect with a current approved session.")
        authority = self.authority_getter()
        authority.snapshot(caller)
        return authority

    def catalog(self):
        try:
            actions = _copy(self.backend.catalog())
        except Exception as error:
            raise ServiceError(503, "The command catalog is unavailable.") from error
        if not isinstance(actions, list):
            raise ServiceError(503, "The command catalog is invalid.")
        seen = set()
        for descriptor in actions:
            name = descriptor.get("id") if isinstance(descriptor, dict) else None
            if not isinstance(name, str) or not name or name in seen:
                raise ServiceError(503, "The command catalog is invalid.")
            seen.add(name)
            if self._furnace(descriptor) and not getattr(self.backend, "furnace_enabled", False):
                descriptor.update(available=False, reason="Agent furnace operation is unavailable.")
        actions.sort(key=lambda row: row["id"])
        contract = {"version": 1, "actions": actions}
        definition = {
            "version": 1,
            "actions": [
                {key: value for key, value in row.items() if key not in {"available", "reason"}}
                for row in actions
            ],
        }
        digest = hashlib.sha256(canonical(definition).encode()).hexdigest()
        return {**contract, "contract_id": "sha256:" + digest}

    @staticmethod
    def _body(body):
        if (
            not isinstance(body, dict)
            or set(body) - FIELDS
            or FIELDS - {"preconditions"} - set(body)
        ):
            raise ServiceError(400, "Send only the documented command fields.")
        for key in ("request_id", "boot_id", "contract_id", "control_epoch", "action"):
            if not isinstance(body[key], str) or not body[key] or len(body[key]) > 256:
                raise ServiceError(400, f"Send a valid {key}.")
        if not REQUEST_ID.fullmatch(body["request_id"]):
            raise ServiceError(400, "Use a request_id with 1 to 128 letters, digits, or . _ : -.")
        if not isinstance(body["params"], dict) or not isinstance(
            body.get("preconditions", {}), dict
        ):
            raise ServiceError(400, "Send params and preconditions as JSON objects.")
        try:
            encoded = canonical(body)
            if len(encoded.encode("utf-8")) > MAX_BODY_BYTES:
                raise ServiceError(413, "The command body exceeds the supported size.")
            return json.loads(encoded)
        except (ValueError, TypeError, RecursionError) as error:
            raise ServiceError(400, "Send finite JSON values for this command.") from error

    @staticmethod
    def _furnace(descriptor):
        return descriptor.get("device") == "furnace" or "furnace" in re.split(
            r"[./:_-]", descriptor["id"].lower()
        )

    def _action(self, catalog, action):
        descriptor = next((row for row in catalog["actions"] if row["id"] == action), None)
        if descriptor is None:
            raise ServiceError(404, "The action is not in the current command catalog.")
        if self._furnace(descriptor) and not getattr(self.backend, "furnace_enabled", False):
            raise ServiceError(403, "Agent furnace operation is unavailable.")
        if descriptor.get("available") is not True:
            raise ServiceError(409, descriptor.get("reason") or "This action is unavailable.")
        return descriptor

    def _validate(self, caller, body, catalog=None):
        authority = self.authority_getter()
        if body["boot_id"] != authority.boot_id:
            raise ServiceError(409, "The Console restarted. Read the current command contract.")
        authority.check(caller, body["control_epoch"])
        catalog = self.catalog() if catalog is None else catalog
        if body["contract_id"] != catalog["contract_id"]:
            raise ServiceError(409, "The command contract changed. Read the current catalog.")
        return self._action(catalog, body["action"])

    def _existing(self, caller, body):
        saved = self._storage("get", caller.client_id, body["request_id"])
        if saved is None:
            return None
        if saved[0] != canonical(body):
            raise ServiceError(409, "This request_id already identifies a different command.")
        return _public(saved[1])

    def submit(self, caller, body):
        stop_revisions = self._stop_revisions.copy()
        self._authenticate(caller)
        body = self._body(body)
        with self.lock:
            if self._closed:
                raise ServiceError(503, "The command host is closed.")
            existing = self._existing(caller, body)
            if existing is not None:
                return existing
        # A duplicate remains readable after handover or a catalog change.
        descriptor = self._validate(caller, body)
        with self.lock:
            existing = self._existing(caller, body)
            if existing is not None:
                return existing
            if self._closed:
                raise ServiceError(503, "The command host is closed.")
            if self._active is not None:
                raise ServiceError(409, "Wait for the current operation to finish or cancel it.")
            page = "agent-" + caller.id
            tracked = self._pages.get(page)
            if tracked is not None and (tracked.invalid or tracked.epoch != body["control_epoch"]):
                tracked.invalid = True
                self._release_ready_pages()
                raise ServiceError(409, "Wait for the previous device control to be released.")
            now = time.time()
            record = {
                "id": uuid.uuid4().hex,
                **body,
                "preconditions": body.get("preconditions", {}),
                "client_id": caller.client_id,
                "session_id": caller.id,
                "page": page,
                "device": descriptor.get("device"),
                "status": "accepted",
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
                "dispatch_count": 0,
                "last_dispatch_at": None,
                "progress": {},
                "result": None,
                "error": None,
                "cancellation_reason": None,
            }
            self._storage("insert", body, record)
            self._pages[page] = _Page(caller, body["control_epoch"])
            operation = _Operation(caller, record)
            self._active = operation
            device = record["device"]
            if self._stop_revisions.get(device) is not stop_revisions.get(device):
                record["cancellation_reason"] = "A device Stop arrived during command admission."
                operation.cancelled.set()
            response = _public(record)
            operation.worker = self._start(self._run, operation, "agent-command-run")
        self._publish(operation)
        return response

    def _find(self, caller, request_id):
        if caller is self.authority_getter().human:
            matches = [row for row in self._storage("items") if row["request_id"] == request_id]
            if len(matches) > 1:
                raise ServiceError(409, "This request_id belongs to multiple clients.")
            record = matches[0] if matches else None
        else:
            saved = self._storage("get", caller.client_id, request_id)
            record = saved[1] if saved else None
        if record is None:
            raise ServiceError(404, "The operation does not exist for this client.")
        return record

    def get(self, caller, request_id):
        self._authenticate(caller)
        with self.lock:
            return _public(self._find(caller, request_id))

    def list(self, caller):
        authority = self._authenticate(caller)
        client_id = None if caller is authority.human else caller.client_id
        with self.lock:
            records = self._storage("items", client_id)
        return {"items": [_summary(row) for row in reversed(records[-50:])], "count": len(records)}

    def _save(self, operation):
        operation.record["updated_at"] = time.time()
        self._storage("save", operation.record)

    def _publish(self, operation):
        if self.publish is not None:
            try:
                with self.lock:
                    report = _public(operation.record)
                self.publish("operation", report)
            except Exception:
                pass

    def _start(self, target, operation, name):
        thread = threading.Thread(target=target, args=(operation,), name=name, daemon=True)
        self._threads = {item for item in self._threads if item.is_alive()}
        self._threads.add(thread)
        thread.start()
        return thread

    def _checkpoint(self, operation):
        if self._closed or operation.cancelled.is_set():
            raise ServiceError(409, "The operation was cancelled.")
        if self._store_error is not None:
            raise ServiceError(503, "The command history is unavailable.")
        self._validate(operation.caller, operation.record)
        if operation.cancelled.is_set():
            raise ServiceError(409, "The operation was cancelled.")

    def _request_cancel(self, operation, reason):
        # Set the event before any database work. Stop does not depend on storage.
        operation.cancelled.set()
        operation.record["status"] = "cancelling"
        operation.record["cancellation_reason"] = reason
        if operation.plan is not None and operation.cancel_worker is None:
            operation.cancel_worker = self._start(self._stop, operation, "agent-command-stop")
        try:
            self._save(operation)
        except ServiceError:
            pass

    def interrupt_devices(self, targets, reason="A device Stop was requested."):
        """Prevent later steps without waiting for a journal or authority lock.

        The independent Stop dispatcher still sends the device Stop. The command
        worker and watcher reconcile this event through their normal cleanup.
        """
        devices = {targets} if isinstance(targets, str) else set(targets)
        if "all_motion" in devices:
            devices |= {"robot", "stage", "microscope"}
        for device in devices & self._stop_revisions.keys():
            self._stop_revisions[device] = object()
        operation = self._active
        if operation is None or operation.record.get("device") not in devices:
            return {"interrupted": []}
        operation.record["cancellation_reason"] = reason
        operation.cancelled.set()
        return {"interrupted": [operation.record["request_id"]]}

    def cancel(self, caller, request_id):
        authority = self._authenticate(caller)
        human = caller is authority.human
        with self.lock:
            if human and self._store_error is None:
                try:
                    self._find(caller, request_id)
                except ServiceError as error:
                    if error.status != 503:
                        raise
            operation = self._active
            if (
                operation is not None
                and operation.record["request_id"] == request_id
                and (human or caller.client_id == operation.record["client_id"])
            ):
                self._request_cancel(operation, "Cancellation requested.")
                report = _public(operation.record)
            else:
                report = _public(self._find(caller, request_id))
        if operation is not None:
            self._publish(operation)
        return report

    def _run(self, operation):
        context = CommandContext(self, operation)
        outcome = None
        try:
            context.checkpoint()
            record = operation.record
            plan = _copy(
                self.backend.prepare(
                    record["action"],
                    _copy(record["params"]),
                    _copy(record["preconditions"]),
                    context.page,
                )
            )
            if not isinstance(plan, dict):
                raise ValueError("The backend did not return an operation plan.")
            with self.lock:
                operation.plan = plan
                record["_plan"] = plan
                self._save(operation)
                if operation.cancelled.is_set():
                    self._request_cancel(operation, record["cancellation_reason"])
            context.checkpoint()
            with self.lock:
                record.update(status="running", started_at=time.time())
                self._save(operation)
            self._publish(operation)
            outcome = _copy(self.backend.execute(plan, context))
            if not isinstance(outcome, dict):
                raise ValueError("The backend did not return an operation outcome.")
            outcome["status"] = {"completed": "succeeded", "cancelled": "stopped"}.get(
                outcome.get("status"), outcome.get("status")
            )
            if outcome["status"] not in TERMINAL:
                raise ValueError("The backend did not return a terminal operation outcome.")
            if outcome["status"] == "unknown" and operation.record["dispatch_count"]:
                with self.lock:
                    self._request_cancel(
                        operation, outcome.get("error") or "The device outcome is unknown."
                    )
        except Exception as error:
            outcome = {
                "status": "unknown" if operation.record["dispatch_count"] else "rejected",
                "error": str(error),
                "result": None,
                "_uncertain": not isinstance(error, CommandInterrupted),
            }
            with self.lock:
                if operation.record["dispatch_count"]:
                    self._request_cancel(operation, str(error))
        finally:
            with self.lock:
                operation.outcome = outcome
                operation.execution_done = True
                if operation.cancelled.is_set() and operation.plan is None:
                    operation.cancel_done = True
                self._finish(operation)
            self._publish(operation)

    def _stop(self, operation):
        try:
            result = _copy(self.backend.cancel(operation.plan, operation.record["page"]))
            if not isinstance(result, dict):
                raise ValueError("The backend did not return a Stop outcome.")
        except Exception as error:
            result = {"status": "unknown", "error": str(error)}
        with self.lock:
            operation.stop_outcome = result
            operation.cancel_done = True
            self._finish(operation)
        self._publish(operation)

    def _finish(self, operation):
        if not operation.execution_done or (
            operation.cancelled.is_set() and not operation.cancel_done
        ):
            return
        outcome = operation.outcome
        status = outcome["status"]
        if operation.cancelled.is_set():
            stop_status = (operation.stop_outcome or {}).get("status")
            if not operation.record["dispatch_count"]:
                status = "stopped"
            elif status == "unknown" and outcome.get("_uncertain", True):
                status = "unknown"
            elif status not in {"succeeded", "stopped"}:
                status = "stopped" if stop_status in {"stopped", "cancelled"} else "unknown"
        operation.record.update(
            status=status,
            result=outcome.get("result"),
            error=outcome.get("error"),
            finished_at=time.time(),
        )
        if operation.cancelled.is_set():
            operation.record["stop_result"] = operation.stop_outcome
        try:
            self._save(operation)
        except ServiceError:
            operation.record["status"] = "unknown"
        if self._active is operation:
            self._active = None
        self._release_ready_pages()

    def _release_ready_pages(self):
        """Schedule page cleanup under the engine lock, but do no device I/O."""
        active_page = self._active.record["page"] if self._active else None
        for page, tracked in tuple(self._pages.items()):
            if not tracked.invalid or tracked.releasing or page == active_page:
                continue
            tracked.releasing = True
            self._start(lambda item: self._release_page(*item), (page, tracked), "agent-release")

    def _release_page(self, page, tracked):
        try:
            release = getattr(self.backend, "release_page", None)
            result = release(page) if release is not None else {"outcome": "not-held"}
            result = _copy(result)
        except Exception as error:
            result = {"outcome": "unknown", "error": str(error)}
        with self.lock:
            # Do not remove a newer grant if the same page was replaced.
            if self._pages.get(page) is tracked:
                self._pages.pop(page)
            self._cleanup_results[page] = result
            while len(self._cleanup_results) > 50:
                self._cleanup_results.pop(next(iter(self._cleanup_results)))

    def cleanup_status(self):
        with self.lock:
            return {
                "pending": [page for page, tracked in self._pages.items() if tracked.invalid],
                "results": _copy(self._cleanup_results),
            }

    def poll(self):
        with self.lock:
            operation = self._active
            pages = tuple(self._pages.items())
        for page, tracked in pages:
            if tracked.invalid:
                continue
            try:
                self.authority_getter().check(tracked.caller, tracked.epoch)
            except Exception:
                with self.lock:
                    if self._pages.get(page) is tracked:
                        tracked.invalid = True
        if operation is not None:
            try:
                self._checkpoint(operation)
            except Exception as error:
                with self.lock:
                    if self._active is operation:
                        self._request_cancel(
                            operation, operation.record.get("cancellation_reason") or str(error)
                        )
                self._publish(operation)
        with self.lock:
            self._release_ready_pages()

    def _watch(self):
        while not self._wake.wait(0.1):
            self.poll()

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            self._wake.set()
            for tracked in self._pages.values():
                tracked.invalid = True
            if self._active is not None:
                self._request_cancel(self._active, "The Console command host closed.")
            self._release_ready_pages()
        self._poller.join()
        # Backend I/O must have finite timeouts. No engine lock is held here.
        while True:
            with self.lock:
                threads = [thread for thread in self._threads if thread.is_alive()]
            if not threads:
                break
            for thread in threads:
                thread.join()
        try:
            close = getattr(self.backend, "close", None)
            if close is not None:
                close()
        finally:
            if self._store is not None:
                self._store.close()
