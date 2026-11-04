"""Cached dashboard reads and independently drained safety requests."""

import copy
import threading
import time
import uuid
from urllib.parse import urlsplit

from labkit.errors import ServiceError

from ..clients import SERVICES

LABELS = {
    "robot": "Robot",
    "stage": "Stage",
    "thermal": "Thermal camera",
    "vision": "Vision",
    "furnace": "Furnace",
    "microscope": "Microscope",
}
CAMERAS = {"camera": "Camera preview", "thermal": "Thermal camera", "microscope": "Microscope"}


def gas_confirmed(value, channel):
    """HTTP success alone does not confirm a gas channel's physical stop."""
    state = value.get("status") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("ok") is not True
        or not isinstance(state, dict)
        or state.get("connected") is not True
        or state.get("locked") is not False
        or "fault" not in state
        or state["fault"] is not None
        or state.get("error") is not None
    ):
        return False
    selected = []
    for key in ("gas_channels", "gas_valves"):
        rows = state.get(key)
        if not isinstance(rows, list):
            return False
        matches = [
            item
            for item in rows
            if isinstance(item, dict)
            and type(item.get("channel_index")) is int
            and item["channel_index"] == channel
        ]
        if len(matches) != 1:
            return False
        selected.append(matches[0])
    flow, valve = selected
    return (
        flow.get("available") is True
        and flow.get("error") is None
        and type(flow.get("actual_percent")) in {int, float}
        and flow["actual_percent"] == 0
        and valve.get("open") is False
        and valve.get("verified") is True
    )


class Dashboard:
    """Own a short admission gate; never wait on the orchestrator under this lock."""

    def __init__(self, service, *, clock=time.monotonic):
        self.service, self.clock = service, clock
        self.lock = threading.RLock()
        self.threads = set()
        self.reading = set()
        self.operations = []
        self.closing = False
        self.approval_generation = 0
        self.cards = {name: self._empty(name) for name in SERVICES}
        self.run = None
        self.orchestrator = {
            "demo": service.demo,
            "connected": False,
            "busy": False,
            "locked": True,
            "stale": True,
            "lease": {"holder": None, "remaining_s": 0},
        }
        self.summary_at = None
        self.secrets = tuple(
            sorted(
                {
                    value
                    for key, value in service.settings.items()
                    if (
                        key.endswith("_TOKEN")
                        or key.startswith("MG400_ORCH_")
                        and key.endswith("_URL")
                    )
                    and isinstance(value, str)
                    and value
                },
                key=len,
                reverse=True,
            )
        )
        self.cameras = []
        for name, label in CAMERAS.items():
            value = service.settings.get(f"MG400_DASH_{name.upper()}_URL", "")
            url = None
            if value and not service.demo:
                if not isinstance(value, str):
                    raise ValueError("Camera page URLs must be strings.")
                parsed = urlsplit(value)
                if (
                    len(value) > 2048
                    or any(ord(char) < 33 or ord(char) == 127 for char in value)
                    or parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.query
                    or parsed.fragment
                    or "\\" in value
                    or any(secret in value for secret in self.secrets)
                ):
                    raise ValueError(
                        "Camera page URLs must be public HTTP(S) URLs without credentials."
                    )
                _ = parsed.port
                url = value
            self.cameras.append({"id": name, "label": label, "url": url})

    def text(self, value, limit=240):
        if not isinstance(value, str):
            return None
        for secret in self.secrets:
            value = value.replace(secret, "[redacted]")
        return "".join(char for char in value if char.isprintable())[:limit]

    def _empty(self, name):
        return {
            "id": name,
            "label": LABELS[name],
            "configured": False,
            "phase": "unconfigured",
            "connected": None,
            "busy": None,
            "locked": None,
            "fault": None,
            "lease_holder": None,
            "demo": None,
            "updated_at": None,
            "error": None,
            "observed": None,
        }

    def _configured(self, name):
        try:
            return bool(self.service.dashboard_configured(name))
        except Exception:
            # Unknown: show loading until the read settles the phase.
            return True

    def _spawn(self, function, name):
        def work():
            try:
                function()
            finally:
                with self.lock:
                    self.threads.discard(threading.current_thread())

        worker = threading.Thread(target=work, name="dashboard-" + name, daemon=True)
        with self.lock:
            self.threads.add(worker)
            try:
                worker.start()
            except Exception:
                self.threads.discard(worker)
                raise
        return worker

    def _read(self, name):
        adapter = None
        card = self._empty(name)
        try:
            card["configured"] = self.service.dashboard_configured(name)
            adapter = self.service.dashboard_client(name, "dashboard-read-" + uuid.uuid4().hex)
            if adapter is None:
                return
            card["configured"] = True
            description, state = adapter.describe(), adapter.state()
            if not isinstance(description, dict) or not isinstance(state, dict):
                raise ValueError("Invalid service state")
            card.update(phase="ready", updated_at=time.time(), observed=self.clock())
            for field in ("connected", "busy", "locked"):
                card[field] = state.get(field) if type(state.get(field)) is bool else None
            demo = state.get("demo", description.get("demo"))
            card["demo"] = demo if type(demo) is bool else None
            lease = state.get("lease")
            holder = lease.get("holder", False) if isinstance(lease, dict) else False
            card["lease_holder"] = None if holder is None else self.text(holder, 128) or "Unknown"
            if name == "robot":
                flags = [state.get("syncing"), state.get("feedback_fresh")]
                card["connected"] = (
                    all(flags) if all(type(flag) is bool for flag in flags) else None
                )
                holder = state.get("controller_id", False)
                card["lease_holder"] = (
                    None if holder is None else self.text(holder, 128) or "Unknown"
                )
                card["fault"] = (
                    "Robot error or stop confirmation is unknown."
                    if state.get("robot_error") is not False
                    or state.get("stop_unconfirmed") is not False
                    else None
                )
            else:
                card["fault"] = (
                    None
                    if "fault" in state and state["fault"] is None
                    else "Service reports a fault or unknown fault state."
                )
        except Exception:
            card.update(phase="unavailable", error="Service state is unavailable.")
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    card.update(phase="unavailable", error="Service read cleanup is incomplete.")
            with self.lock:
                self.cards[name] = card
                self.reading.discard(name)

    def _summary(self):
        try:
            observed = self.clock()
            state, run = self.service.dashboard_summary()
            lease = state.get("lease") or {}
            remaining = lease.get("remaining_s", 0)
            public = {
                "demo": self.service.demo,
                "connected": state.get("connected") is True,
                "busy": state.get("busy") is True,
                "locked": state.get("locked") is True,
                "stale": False,
                "lease": {
                    "holder": self.text(lease.get("holder"), 128),
                    "remaining_s": remaining
                    if type(remaining) in {int, float} and 0 <= remaining <= 60
                    else 0,
                },
            }
            result = None
            if run is not None:
                live = run.get("live") or {}
                position = live.get("step")
                step = next(
                    (item for item in run.get("steps", []) if item.get("position") == position),
                    None,
                )
                resume_kind = None
                if isinstance(step, dict) and live.get("status") == "held":
                    if step.get("status") == "pending" and step.get("started_at") is None:
                        resume_kind = "pending"
                    elif step.get("started_at") is not None:
                        resume_kind = "started"
                result = {
                    **{key: self.text(run.get(key)) for key in ("id", "name", "status", "detail")},
                    **{key: live.get(key) is True for key in ("closed", "settled", "draining")},
                    "step_position": position if type(position) is int else None,
                    "resume_kind": resume_kind,
                    "steps": [
                        {
                            "position": step["position"],
                            **{
                                key: self.text(step.get(key))
                                for key in ("skill", "status", "error")
                            },
                        }
                        for step in run.get("steps", [])
                    ],
                }
            with self.lock:
                self.orchestrator, self.run, self.summary_at = public, result, observed
        except Exception:
            with self.lock:
                self.orchestrator["stale"] = True
        finally:
            with self.lock:
                self.reading.discard("summary")

    def refresh(self):
        with self.lock:
            if self.closing:
                return
            for name in (*SERVICES, "summary"):
                if name in self.reading:
                    continue
                self.reading.add(name)
                if (
                    name != "summary"
                    and self.cards[name]["observed"] is None
                    and self._configured(name)
                ):
                    # An unconfigured service keeps "unconfigured" between reads.
                    self.cards[name]["phase"] = "loading"
                try:
                    self._spawn(
                        self._summary if name == "summary" else lambda name=name: self._read(name),
                        name,
                    )
                except Exception:
                    self.reading.discard(name)
                    if name != "summary":
                        self.cards[name].update(
                            phase="unavailable", error="State refresh could not start."
                        )
        self.poll()

    def busy(self):
        with self.lock:
            return any(not operation["done"].is_set() for operation in self.operations)

    def reserve(self, action):
        with self.lock:
            if self.closing or not self.service.started or self.service.closing:
                raise ServiceError(409, "The dashboard is shutting down or has not started.")
            for operation in self.operations:
                if operation["action"] == action and not operation["done"].is_set():
                    return operation, False
            targets = {
                "motion": (("robot", None), ("stage", None), ("microscope", None)),
                "heat": (("furnace", None),),
                "gas": tuple(("furnace", index) for index in range(3)),
                "hold": (),
            }[action]
            operation = {
                "id": uuid.uuid4().hex,
                "action": action,
                "targets": targets,
                "phase": "stopping",
                "hold": "not-required" if action == "gas" else "pending",
                "results": {},
                "done": threading.Event(),
                "coordinator": None,
                "initial": [],
                "dispatched": False,
            }
            self.operations = [item for item in self.operations if not item["done"].is_set()] + [
                item for item in self.operations if item["done"].is_set()
            ][-19:]
            if action != "gas":
                self.approval_generation += 1
            self.operations.append(operation)
            return operation, True

    def _round(self, operation):
        events = []
        for name, channel in operation["targets"]:
            target = name if channel is None else f"gas-{channel}"
            finished = threading.Event()
            events.append(finished)
            with self.lock:
                operation["results"][target] = {"status": "pending", "message": "Stop requested."}

            def attempt(name=name, channel=channel, target=target, finished=finished):
                adapter = None
                result = {
                    "status": "failed",
                    "message": "Stop failed; physical outcome is unknown.",
                }
                try:
                    adapter = self.service.dashboard_client(
                        name, "dashboard-stop-" + uuid.uuid4().hex
                    )
                    if adapter is None:
                        result = {
                            "status": "unavailable",
                            "message": "No configured service or running owned demo is available.",
                        }
                    else:
                        value = adapter.stop() if channel is None else adapter.gas_stop(channel)
                        confirmed = (
                            isinstance(value, dict) and value.get("stop") == "confirmed"
                            if channel is None
                            else gas_confirmed(value, channel)
                        )
                        result = {
                            "status": "confirmed" if confirmed else "unconfirmed",
                            "message": "Stop confirmed by service feedback."
                            if confirmed
                            else "Stop requested; physical stop is not confirmed.",
                        }
                except Exception:
                    pass
                finally:
                    if adapter is not None:
                        try:
                            adapter.close()
                        except Exception:
                            result = {
                                "status": "unconfirmed",
                                "message": "Stop client cleanup failed; outcome is uncertain.",
                            }
                    with self.lock:
                        operation["results"][target] = result
                    finished.set()

            try:
                self._spawn(attempt, target + "-stop")
            except Exception:
                with self.lock:
                    operation["results"][target] = {
                        "status": "failed",
                        "message": "The stop request could not start.",
                    }
                finished.set()
        return events

    def dispatch(self, operation):
        events = self._round(operation)
        with self.lock:
            operation["initial"] = events
            operation["dispatched"] = True
        self.poll()

    def _coordinate(self, operation):
        context = {}
        if operation["action"] != "gas":
            with self.lock:
                operation["phase"] = "holding"
            while True:
                try:
                    proof = self.service.dashboard_drain(context)
                    if proof["settled"]:
                        break
                except Exception:
                    with self.lock:
                        operation["hold"] = "unconfirmed"
                time.sleep(0.05)
            with self.lock:
                operation["hold"] = "settled" if proof["confirmed"] else "unconfirmed"
        for event in operation["initial"]:
            event.wait()
        if operation["action"] != "gas":
            with self.lock:
                operation["phase"] = "final-stop"
            for event in self._round(operation):
                event.wait()
        with self.lock:
            operation["phase"] = "done"
            operation["done"].set()

    def poll(self):
        # Retrying an unstarted cleanup coordinator never repeats a motion command.
        with self.lock:
            for operation in self.operations:
                if (
                    operation["dispatched"]
                    and operation["coordinator"] is None
                    and not operation["done"].is_set()
                ):
                    try:
                        operation["coordinator"] = self._spawn(
                            lambda operation=operation: self._coordinate(operation), "stop-drain"
                        )
                    except Exception:
                        operation["hold"] = "unconfirmed"

    def snapshot(self):
        with self.lock:
            now = self.clock()
            cards = []
            for saved in self.cards.values():
                card = copy.deepcopy(saved)
                observed = card.pop("observed")
                if card["phase"] == "ready" and observed is not None and now - observed > 3:
                    card["phase"] = "stale"
                cards.append(card)
            orchestrator = copy.deepcopy(self.orchestrator)
            age = max(0, now - self.summary_at) if self.summary_at is not None else 0
            orchestrator["lease"]["remaining_s"] = max(
                0, orchestrator["lease"]["remaining_s"] - age
            )
            if self.summary_at is None or now - self.summary_at > 3:
                orchestrator["stale"] = True
            return {
                "services": cards,
                "cameras": copy.deepcopy(self.cameras),
                "orchestrator": orchestrator,
                "run": copy.deepcopy(self.run),
                "operations": [
                    {
                        **{key: operation[key] for key in ("id", "action", "phase", "hold")},
                        "done": operation["done"].is_set(),
                        "draining": not operation["done"].is_set(),
                        "results": copy.deepcopy(operation["results"]),
                    }
                    for operation in self.operations
                ],
            }

    def begin_close(self):
        with self.lock:
            self.closing = True

    def close(self):
        self.begin_close()
        while True:
            self.poll()
            with self.lock:
                workers = list(self.threads)
            if not workers and not self.busy():
                return
            for worker in workers:
                worker.join(0.05)
