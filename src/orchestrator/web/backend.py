"""Cached dashboard reads and independently drained safety requests."""

import copy
import threading
import time
import uuid

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


class Dashboard:
    """Own a short admission gate; never wait on the orchestrator under this lock."""

    def __init__(self, service, *, clock=time.monotonic):
        self.service, self.clock = service, clock
        self.lock = threading.RLock()
        self.threads = set()
        self.reading = set()
        self.operations = []
        self.closing = False
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
        # The console Cameras workspace shows every view; there are no camera page links.
        self.cameras = [
            {"id": name, "label": label, "url": None} for name, label in CAMERAS.items()
        ]

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
        }

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

    def _card(self, name):
        """One Overview card from the device cache."""
        card = self._empty(name)
        entry = self.service.devices.get(name)
        phase = entry["phase"]
        if phase == "unconfigured":
            return card
        card["configured"] = True
        if phase == "offline":
            return card
        if phase not in {"ready", "stale"}:
            card.update(
                phase=phase,
                error="Service state is unavailable." if phase == "unavailable" else None,
            )
            return card
        holder = entry["holder"]
        card.update(
            phase=phase,
            updated_at=entry["updated_at"],
            lease_holder=None if holder is None else self.text(holder, 128) or "Unknown",
            **{key: entry[key] for key in ("connected", "busy", "locked", "fault", "demo")},
        )
        return card

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
            self.service.devices.start()
            if "summary" not in self.reading:
                self.reading.add("summary")
                try:
                    self._spawn(self._summary, "summary")
                except Exception:
                    self.reading.discard("summary")
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
                        value = adapter.stop(
                            **({} if channel is None else {"gas_channel": channel})
                        )
                        confirmed = isinstance(value, dict) and value.get("stop") == "confirmed"
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
            cards = [self._card(name) for name in SERVICES]
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
