"""One local language request, validated against the live Console contract."""

import copy
import hashlib
import inspect
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from labkit.errors import ServiceError

from .actions import validate
from .chat_adaptation import ConsoleAdaptation
from .chat_payload import model_context
from .chat_preconditions import capture, command_preconditions, protected_action
from .command_store import TERMINAL, canonical
from .control_portal import REQUEST_ID, STOP_TARGETS


@dataclass
class ChatJob:
    source: str
    request_id: str
    message: str
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    command: dict | None = None
    dispatched: bool = False
    worker: threading.Thread | None = None
    stage_preconditions: dict | None = None
    stage_context_error: str = ""


def checked_catalog(value):
    """Verify the catalog definition separately from changing availability."""
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ServiceError(409, "The command catalog needs an agent update.")
    actions = value.get("actions")
    if not isinstance(actions, list) or not actions or len(actions) > 256:
        raise ServiceError(502, "The Console returned an invalid action list.")
    names = set()
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get("id"), str):
            raise ServiceError(502, "The Console returned an invalid action.")
        if action["id"] in names or not isinstance(action.get("params"), dict):
            raise ServiceError(502, "The Console action definition is invalid.")
        names.add(action["id"])
    definition = {
        "version": 1,
        "actions": [
            {k: v for k, v in action.items() if k not in {"available", "reason"}}
            for action in actions
        ],
    }
    identity = "sha256:" + hashlib.sha256(canonical(definition).encode()).hexdigest()
    if value.get("contract_id") != identity:
        raise ServiceError(409, "The command catalog failed its integrity check.")
    return value


def command_identity(status):
    return tuple(status.get(key) for key in ("boot_id", "control_epoch", "command_contract_id")) + (
        (status.get("principal") or {}).get("id"),
    )


class ChatAgent:
    def __init__(self, control, reader, model, *, profiles=None, adaptation=None, history=None):
        self.control, self.reader, self.model = control, reader, model
        self.profiles = profiles
        self.history = history
        self._profile_value = None
        self._managed_profile = bool(model.status().get("profile_id"))
        self.adaptation = adaptation or ConsoleAdaptation()
        self.adaptation_errors = {source: None for source in ("live", "mock")}
        self.lock = threading.RLock()
        self.messages = {source: deque(maxlen=60) for source in ("live", "mock")}
        self.activity = {source: deque(maxlen=120) for source in ("live", "mock")}
        self.jobs = OrderedDict()
        self.current = None
        self.closed = False
        self.usage = {source: deque(maxlen=100) for source in ("live", "mock")}
        self._restore_history()

    def _restore_history(self):
        if self.history is None or not self.history.available:
            return
        try:
            self.messages["live"].extend(self.history.rows("message"))
            self.activity["live"].extend(self.history.rows("activity"))
            self.usage["live"].extend(self.history.rows("usage"))
            for row in self.history.rows("request"):
                job = ChatJob("live", row["request_id"], row["message"])
                job.command = row.get("command")
                job.dispatched = row.get("dispatched", False)
                job.done.set()
                self.jobs["live", job.request_id] = job
        except (ServiceError, KeyError, TypeError):
            self.messages["live"].append(
                {
                    "id": uuid.uuid4().hex,
                    "role": "system",
                    "text": "Saved history could not be restored.",
                }
            )

    def _persist(self, source, kind, key, value, *, required=False, **kwargs):
        if self.history is not None and source == "live":
            try:
                return self.history.put(kind, key, value, **kwargs)
            except ServiceError:
                if required:
                    raise
        return None

    def _save_job(self, job, *, required=False):
        self._persist(
            job.source,
            "request",
            job.request_id,
            {
                "request_id": job.request_id,
                "message": job.message,
                "dispatched": job.dispatched,
                "command": job.command,
            },
            required=required,
        )

    def _sync_profile(self):
        if self.profiles is None or (self.current and not self.current.done.is_set()):
            return
        active = self.profiles.active()
        if active != self._profile_value or (active is None and self._managed_profile):
            if active is not None or self._managed_profile:
                self.model.configure_profile(active)
            self._profile_value = copy.deepcopy(active)
            self._managed_profile = active is not None

    @staticmethod
    def source(source):
        if not isinstance(source, str) or source not in {"live", "mock"}:
            raise ServiceError(400, "Choose live or mock.")
        return source

    def _message(self, source, role, text, **fields):
        with self.lock:
            value = {"id": uuid.uuid4().hex, "role": role, "text": text, **copy.deepcopy(fields)}
            self._persist(source, "message", value["id"], value)
            self.messages[source].append(value)

    def status(self, source="live"):
        self.source(source)
        with self.lock:
            self._sync_profile()
            job = self.current
            active = job if job and job.source == source and not job.done.is_set() else None
            adaptation = self.adaptation.public()
            if self.adaptation_errors[source]:
                adaptation.update(status="update_required", message=self.adaptation_errors[source])
            return {
                "model": self.model.status(),
                "adaptation": adaptation,
                "busy": bool(job and not job.done.is_set()),
                "messages": list(copy.deepcopy(self.messages[source])),
                "activity": list(copy.deepcopy(self.activity[source])),
                "active": copy.deepcopy(active.command) if active else None,
                "usage": list(copy.deepcopy(self.usage[source])),
                "history": self.history.status()
                if self.history is not None and source == "live"
                else {"available": False, "message": "This conversation is stored in memory only."},
                "recovery_pending": [
                    job.request_id
                    for (saved_source, _), job in self.jobs.items()
                    if saved_source == source
                    and job.done.is_set()
                    and job.dispatched
                    and job.command
                    and job.command.get("status") not in TERMINAL - {"unknown"}
                ],
            }

    def _activity(self, job, phase, text, **fields):
        with self.lock:
            value = {
                "id": uuid.uuid4().hex,
                "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "request_id": job.request_id,
                "phase": phase,
                "text": text,
                **copy.deepcopy(fields),
            }
            self._persist(job.source, "activity", value["id"], value)
            self.activity[job.source].append(value)

    def _usage(self, job, value):
        value = copy.deepcopy(value) | {
            "id": uuid.uuid4().hex,
            "request_id": job.request_id,
            "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }
        with self.lock:
            self._persist(job.source, "usage", value["id"], value)
            self.usage[job.source].append(value)

    def reconcile(self, source):
        """Read saved command IDs. Do not infer, submit, cancel, or acquire control."""
        pending = self.status(source)["recovery_pending"]
        for identifier in pending:
            job = self.jobs[source, identifier]
            try:
                result = self.control.read_commands(source, "commands/request/" + identifier)
                if result.get("request_id") != identifier:
                    raise ServiceError(502, "The saved result has a different request ID.")
            except ServiceError:
                result = self._unknown(job.command)
            with self.lock:
                job.command = copy.deepcopy(result)
                self._save_job(job)
            self._activity(
                job,
                "reconciled",
                "Read the saved command result. No command was repeated.",
                result=result,
            )
        return self.status(source)

    def configure(self, api_key, model):
        with self.lock:
            if self.current and not self.current.done.is_set():
                raise ServiceError(409, "Wait for the current request before changing the model.")
            if self.profiles and self.profiles.status()["active_profile_id"]:
                raise ServiceError(
                    409, "Deactivate the saved profile before using temporary settings."
                )
            self.model.configure(api_key=api_key, model=model)
        return self.status()

    def profile_status(self):
        if self.profiles is None:
            raise ServiceError(503, "Saved API profiles are unavailable in this portal.")
        return self.profiles.status()

    def change_profile(self, action, data):
        from .model_profiles import ModelProfileError

        with self.lock:
            self.profile_status()
            if self.current and not self.current.done.is_set():
                raise ServiceError(
                    409, "Wait for the current request before changing API profiles."
                )
            previous = self.profiles.status()["active_profile_id"]
            try:
                if action == "add":
                    result = self.profiles.add(**data)
                elif action == "activate":
                    result = self.profiles.activate(data["profile_id"])
                else:
                    result = self.profiles.delete(data["profile_id"])
                if action == "activate" or result["active_profile_id"] != previous:
                    self.model.configure_profile(self.profiles.active())
                    self._managed_profile = bool(result["active_profile_id"])
                    self._profile_value = None
            except ModelProfileError as error:
                raise ServiceError(400, str(error)) from None
            return result

    def submit(self, source, message, request_id):
        self.source(source)
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise ServiceError(400, "Enter an instruction with 1 to 4000 characters.")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            raise ServiceError(400, "Send a valid request ID.")
        stop = {
            "stop": "all_motion",
            "stop all": "all_motion",
            "stop all motion": "all_motion",
            "stop robot": "robot",
            "stop stage": "stage",
            "stop microscope": "microscope",
            "stop furnace": "furnace",
            "stop gas": "gas_all",
        }.get(message.strip().lower().rstrip(".!"))
        if stop:
            self.invalidate(source)
            self._stop(ChatJob(source, request_id, message), {"target": stop}, record_user=True)
            return self.status(source)
        with self.lock:
            old = self.jobs.get((source, request_id))
            if old:
                if old.message != message:
                    raise ServiceError(
                        409, "This request ID already identifies another instruction."
                    )
                return self.status(source)
            if self.closed:
                raise ServiceError(503, "The local agent is shutting down.")
            self._sync_profile()
            if not self.model.status()["configured"]:
                raise ServiceError(400, "Add and activate an API profile in Model APIs first.")
            if self.current and not self.current.done.is_set():
                raise ServiceError(409, "Wait for the current request or cancel it.")
            job = ChatJob(source, request_id, message)
            old = self._persist(
                source,
                "request",
                request_id,
                {
                    "request_id": request_id,
                    "message": message,
                    "dispatched": False,
                    "command": None,
                },
                once=True,
                required=True,
            )
            if old is not None:
                if old.get("message") != message:
                    raise ServiceError(
                        409, "This request ID already identifies another instruction."
                    )
                return self.status(source)
            self.current = job
            self.jobs[source, request_id] = job
            while len(self.jobs) > 100:
                self.jobs.popitem(last=False)
            self._message(source, "user", message)
            job.worker = threading.Thread(target=self._run, args=(job,), daemon=True)
            job.worker.start()
        return self.status(source)

    @staticmethod
    def _check_cancelled(job):
        if job.cancelled.is_set():
            raise ServiceError(409, "The request was cancelled. No further action will be sent.")

    def _context(self, job):
        status = self.control.status(job.source)
        catalog = checked_catalog(self.control.read_commands(job.source, "commands/catalog"))
        try:
            knowledge = self.adaptation.context(catalog)
        except ServiceError as error:
            self.adaptation_errors[job.source] = error.message
            raise
        self.adaptation_errors[job.source] = None
        context = self.reader.read(job.source)
        command_boot = (context["discovery"].get("control") or {}).get("command_boot_id")
        if command_boot and command_boot != status.get("boot_id"):
            raise ServiceError(409, "The Console restarted while reading its state. Try again.")
        choices = (
            self.control.read_commands(job.source, "commands/choices")
            if status.get("connected")
            else {}
        )
        job.stage_preconditions, job.stage_context_error = capture(context["state"], choices)
        with self.lock:
            conversation = [
                {"role": item["role"], "text": item["text"]}
                for item in list(self.messages[job.source])[-12:-1]
            ]
        return (
            status,
            catalog,
            {
                "source": job.source,
                "user_message": job.message,
                "conversation": conversation,
                "state": context["state"],
                "units": context["units"],
                "read_contract_id": context["contract_id"],
                "commands": catalog["actions"],
                "choices": choices,
                "project_knowledge": knowledge,
            },
        )

    def _run(self, job):
        try:
            self._activity(job, "reading", "Reading current Console state, units and commands.")
            before, catalog, context = self._context(job)
            self._check_cancelled(job)
            self._activity(
                job, "interpreting", "Interpreting your instruction with the active model."
            )
            options = {}
            parameters = inspect.signature(self.model.interpret).parameters
            if "cancelled" in parameters:
                options["cancelled"] = job.cancelled
            if "on_usage" in parameters:
                options["on_usage"] = lambda value: self._usage(job, value)
            decision = self.model.interpret(model_context(context), **options)
            self._check_cancelled(job)
            kind = decision["kind"]
            if kind in {"answer", "clarify"}:
                self._message(job.source, "assistant", decision["message"], kind=kind)
                self._activity(
                    job,
                    "completed",
                    "Clarification needed. No command sent."
                    if kind == "clarify"
                    else "Answer ready. No command sent.",
                )
            elif kind == "stop":
                self._stop(job, decision["params"])
            elif kind == "command":
                self._execute(job, decision, before, catalog, context)
            else:
                raise ServiceError(502, "The model did not return a supported decision.")
        except ServiceError as error:
            self._report_error(job, error.message)
        except Exception:
            self._report_error(job, "The request could not be verified. No retry was sent.")
        finally:
            job.done.set()

    def _report_error(self, job, message):
        if job.dispatched:
            record = self._unknown(job.command or {"request_id": job.request_id})
            self._message(job.source, "assistant", record["error"], kind="result", command=record)
            self._activity(job, "failed", record["error"], result=record)
        else:
            self._message(job.source, "assistant", message, kind="error")
            self._activity(job, "cancelled" if job.cancelled.is_set() else "failed", message)

    def _stop(self, job, params, *, record_user=False):
        if not isinstance(params, dict) or set(params) != {"target"}:
            raise ServiceError(400, "Specify the device to stop.")
        if params["target"] not in STOP_TARGETS:
            raise ServiceError(400, "Choose a supported Stop target.")
        envelope = {"request_id": job.request_id, "target": params["target"]}
        try:
            result = self.control.stop(job.source, envelope)
        except ServiceError as error:
            self._activity(job, "failed", error.message)
            raise
        # Stop reaches its independent transport before any history lock or disk write.
        if record_user:
            self._message(job.source, "user", job.message)
        self._activity(job, "sending", "Sent independent Stop request.", command=envelope)
        self._activity(job, "completed", "Stop response received.", result=result)
        self._message(
            job.source,
            "assistant",
            "Stop requested. See the returned device status.",
            kind="result",
            action="stop",
            params=params,
            command=result,
        )

    def _execute(self, job, decision, before, catalog, context):
        action, params = decision["action"], decision["params"]
        self._activity(job, "validating", "Checking parameters, current contract and control.")
        descriptor = next((row for row in catalog["actions"] if row["id"] == action), None)
        if descriptor is None or descriptor.get("available") is not True:
            raise ServiceError(409, "That action is not available in the current Console catalog.")
        try:
            validate(params, descriptor["params"])
        except ValueError as error:
            raise ServiceError(400, "The selected action has invalid parameters: " + str(error))
        latest = self.reader.read(job.source)
        if latest["contract_id"] != context["read_contract_id"]:
            raise ServiceError(409, "The state format changed. Send the instruction again.")
        current = checked_catalog(self.control.read_commands(job.source, "commands/catalog"))
        status = self.control.status(job.source)
        if current["contract_id"] != catalog["contract_id"] or command_identity(
            status
        ) != command_identity(before):
            raise ServiceError(
                409, "The Console contract or control changed. Send the instruction again."
            )
        if not any(
            row["id"] == action and row.get("available") is True for row in current["actions"]
        ):
            raise ServiceError(409, "The selected action is no longer available.")
        preconditions = command_preconditions(
            action,
            job.stage_preconditions,
            job.stage_context_error,
            self.control.read_commands(job.source, "commands/choices")
            if protected_action(action)
            else {},
        )
        envelope = {
            "request_id": job.request_id,
            "boot_id": status.get("boot_id"),
            "control_epoch": status.get("control_epoch"),
            "contract_id": catalog["contract_id"],
            "action": action,
            "params": params,
            "preconditions": preconditions,
        }
        self._check_cancelled(job)
        with self.lock:
            job.command = {**envelope, "status": "submitting"}
        self._message(
            job.source,
            "assistant",
            "Interpretation: " + decision["message"],
            kind="command",
            action=action,
            params=params,
        )
        try:
            with self.control.lock:
                result = self.control.command(
                    job.source,
                    "commands",
                    envelope,
                    before_dispatch=lambda: self._admit(job, envelope),
                )
        except ServiceError as error:
            if error.status < 500:
                job.dispatched = False
                raise
            # Look up the same ID after an uncertain response. Never resubmit movement.
            try:
                result = self.control.read_commands(
                    job.source, "commands/request/" + job.request_id
                )
            except ServiceError:
                result = self._unknown(envelope)
        self._follow(job, result)

    def _admit(self, job, envelope):
        self._check_cancelled(job)
        job.dispatched = True
        self._save_job(job, required=True)
        self._activity(
            job,
            "sending",
            "Sending POST /lab/api/agent/control/commands.",
            command=envelope,
        )
        self._check_cancelled(job)

    @staticmethod
    def _unknown(record):
        return {
            **record,
            "status": "unknown",
            "error": "The command outcome is unconfirmed. Inspect its request ID in history. "
            "Do not repeat the movement without checking the equipment.",
        }

    def _follow(self, job, result):
        deadline, cancelled = time.monotonic() + 300, False
        previous_status = None
        while True:
            with self.lock:
                job.command = copy.deepcopy(result)
            if result.get("status") != previous_status:
                self._save_job(job)
            if result.get("status") in TERMINAL:
                text = "Command status: " + result["status"] + "."
                if result.get("error"):
                    text += " " + str(result["error"])
                if str(result.get("action", "")).startswith("furnace."):
                    text += " This does not mean a heating sequence has finished."
                self._message(job.source, "assistant", text, kind="result", command=result)
                phase = {"succeeded": "completed", "cancelled": "cancelled"}.get(
                    result["status"], "failed"
                )
                self._activity(job, phase, text, result=result)
                return
            if result.get("status") != previous_status:
                previous_status = result.get("status")
                self._activity(
                    job,
                    "executing",
                    "Console command status: " + str(previous_status) + ".",
                    result=result,
                )
            if job.cancelled.is_set() and not cancelled:
                self.control.action(job.source, "commands/cancel", {"request_id": job.request_id})
                cancelled = True
            if time.monotonic() >= deadline:
                result = self._unknown(result)
                continue
            time.sleep(0.25)
            try:
                result = self.control.read_commands(
                    job.source, "commands/request/" + job.request_id
                )
            except ServiceError:
                result = self._unknown(result)

    def invalidate(self, source):
        """Signal without waiting for inference, device requests, or Stop."""
        job = self.current
        if job and job.source == source and not job.done.is_set():
            job.cancelled.set()
        return job

    def cancel(self, source):
        self.source(source)
        job = self.invalidate(source)
        if job and job.source == source and not job.done.is_set():
            self._activity(
                job, "cancelling", "Cancellation requested. No further action will be sent."
            )
        if job and job.source == source and job.dispatched and not job.done.is_set():
            try:
                self.control.action(source, "commands/cancel", {"request_id": job.request_id})
            except ServiceError as error:
                self._message(source, "assistant", error.message, kind="error")
        return self.status(source)

    def close(self):
        self.closed = True
        for source in self.messages:
            self.invalidate(source)
