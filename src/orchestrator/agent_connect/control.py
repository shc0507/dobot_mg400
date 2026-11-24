"""Connection management, simulated commands, and operator handover."""

import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from flask import jsonify, request, send_from_directory

from labkit.errors import ServiceError

from .authority import Authority

CONTROL = "/api/agent/control"
ASSETS = Path(__file__).with_name("static")


def principal(authority):
    """Direct Console access is trusted; supplied agent credentials stay explicit."""
    header = request.headers.get("Authorization")
    if header is not None:
        if not header.startswith("Bearer ") or not header[7:]:
            raise ServiceError(401, "Send a valid agent session credential.")
        return authority.authenticate(header[7:])
    return authority.human


def fields(allowed=(), required=()):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - set(allowed) or set(required) - set(data):
        raise ServiceError(400, "Send only the documented fields for this action.")
    return data


class ControlHost:
    """Lazy authority storage and a bounded, manually stepped simulation."""

    def __init__(
        self, service=None, *, authority=None, mock=False, source=None, allow_physical=False
    ):
        self.service = service
        self._authority = authority
        self.lock = threading.RLock()
        self.simulation = None
        self.simulated_principal = None
        self.simulated = bool(mock if service is None else getattr(service, "demo", False))
        self.physical_commands_enabled = bool(
            allow_physical and service is not None and getattr(service, "demo", None) is False
        )
        self.commands_enabled = self.simulated or self.physical_commands_enabled
        self.stop_enabled = self.commands_enabled
        self.furnace_control_enabled = self.physical_commands_enabled
        self.equipment_mode = (
            "physical"
            if self.physical_commands_enabled
            else "simulated"
            if self.simulated
            else "disabled"
        )
        self._published_warning = None
        self._warning_lock = threading.Lock()
        self._monitor_stop = threading.Event()
        self._monitor = None
        self.source = source
        self._commands = None
        self._command_backend = None
        self.publish_command = None
        from .stops import StopHost

        root = getattr(service, "runs_dir", None)
        stop_path = (
            Path(root) / "agent-connect" / "stops.db" if root and not self.simulated else None
        )
        self.stops = StopHost(self._dispatch_stop, path=stop_path, publish=self._publish_stop)

    def _dispatch_stop(self, device):
        if not self.stop_enabled:
            return {
                "device": device,
                "outcome": "disabled",
                "reason": "Physical agent commands are disabled.",
            }
        if self._commands is not None:
            target = "furnace" if device.startswith("gas_") else device
            self._commands.interrupt_devices([target], "A device Stop was requested.")
        backend = self._command_backend
        if backend is not None:
            return backend.stop(device)
        if self.physical_commands_enabled:
            # This fixed emergency path must not initialize command history or wait for it.
            from .emergency import PhysicalEmergency

            return PhysicalEmergency().stop(device)
        if self.service is not None and getattr(self.service, "demo", False) is True:
            return self.service.console.stop(device)
        return {
            "device": device,
            "outcome": "unknown",
            "reason": "The simulated device is not running.",
        }

    def stop_sender(self, authorization=None):
        authority = self._authority
        if authority is None:
            return {
                "kind": "unverified",
                "name": "Unverified caller",
                "role": "unknown",
                "verified": False,
            }
        token = (
            None
            if authorization is None
            else (authorization[7:] if authorization.startswith("Bearer ") else "invalid")
        )
        return authority.stop_sender(token)

    def _publish_stop(self, *args):
        # Both hubs receive only sanitized Stop records from the Stop engine.
        payload = args[-1]
        if self.publish_command is not None:
            self.publish_command("stop", payload)
        hub = getattr(self.service, "agent_stream", None)
        if hub is not None:
            hub.publish("stop", payload)

    def authority(self):
        if self._authority is not None and (
            self._monitor is not None or self._monitor_stop.is_set()
        ):
            return self._authority
        with self.lock:
            if self._authority is None:
                root = getattr(self.service, "runs_dir", None)
                path = None
                if root is not None and not getattr(self.service, "demo", False):
                    path = Path(root) / "agent-connect" / "authority.db"
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._authority = Authority(path)
                if self.service is not None:
                    self.service.agent_authority = self._authority
            if self._monitor is None and not self._monitor_stop.is_set():
                self._monitor = threading.Thread(
                    target=self._monitor_authority, name="agent-authority-monitor", daemon=True
                )
                self._monitor.start()
            return self._authority

    def _monitor_authority(self):
        """Expire sessions and publish warnings without a browser or device request."""
        while not self._monitor_stop.wait(0.1):
            try:
                self._publish_warning()
            except Exception:
                # A closed authority or unavailable publisher must not leak a worker.
                if self._monitor_stop.is_set():
                    return

    def commands(self):
        """Open command history lazily. This never enables physical dispatch."""
        from .action_backend import create_backend
        from .commands import CommandHost

        with self.lock:
            if self._commands is None:
                self.authority()
                root = getattr(self.service, "runs_dir", None)
                path = None
                if root is not None and not self.simulated:
                    path = Path(root) / "agent-connect" / "commands.db"
                backend = create_backend(
                    self.service,
                    mock=self.simulated,
                    source=self.source,
                    allow_physical=self.physical_commands_enabled,
                )
                self._command_backend = backend
                self._commands = CommandHost(
                    self.authority, backend, path=path, publish=self._publish_command
                )
            return self._commands

    def command_observations(self):
        """Read only the local simulated state used by the command handlers."""
        if not self.simulated or self._command_backend is None:
            return {}
        from .state import CacheStateProvider

        try:
            states = self._command_backend.observations()
        except Exception:
            states = {}
        # Do not substitute Phase 0 animation for missing command feedback.
        states = {"stage": None, **states}
        now = time.time()
        cache = SimpleNamespace(
            entries=states,
            get=lambda name: {
                "state": states[name],
                "phase": "ready" if states[name] is not None else "unavailable",
                "age_s": 0.0 if states[name] is not None else None,
                "updated_at": now if states[name] is not None else None,
            },
        )
        return CacheStateProvider(cache, mode="MOCK")()["devices"]

    def _publish_command(self, kind, operation):
        if self.publish_command is not None:
            self.publish_command(kind, self._operation_summary(operation))
        hub = getattr(self.service, "agent_stream", None)
        if hub is not None:
            hub.publish(kind, self._operation_summary(operation))

    @staticmethod
    def _operation_summary(operation):
        fields = (
            "id",
            "operation_id",
            "request_id",
            "client_id",
            "action",
            "status",
            "created_at",
            "updated_at",
            "finished_at",
            "dispatch_count",
            "progress",
            "error",
            "mode",
        )
        return {key: operation[key] for key in fields if key in operation}

    def operation_summaries(self):
        commands = self._commands
        if commands is None:
            return []
        return [
            {"kind": "agent_command", **self._operation_summary(item)}
            for item in commands.list(self.authority().human)["items"][:10]
        ]

    def reconcile_commands(self):
        if self._commands is not None:
            self._commands.poll()
        self._publish_warning()

    def _publish_warning(self):
        if self._authority is not None:
            warning = self._authority.snapshot().get("watchdog", {}).get("active")
            if warning:
                with self._warning_lock:
                    if warning["event_id"] == self._published_warning:
                        return
                    self._published_warning = warning["event_id"]
                hub = getattr(self.service, "agent_stream", None)
                if hub is not None:
                    hub.publish("agent.connection_lost", warning)
                events = getattr(self.service, "events", None)
                if events is not None:
                    events.add("warning", "The controlling agent stopped responding.", **warning)

    def idle(self):
        """Require idle real work. Never stop/release hardware for a mock handover."""
        service = self.service
        if service is None or getattr(service, "demo", False):
            return
        console = getattr(service, "console", None)
        operator = self.authority()._operator
        agent_page = "agent-" + operator.id if operator and operator.kind == "agent" else None
        if console is not None:
            with console.lock:
                foreign = any(
                    agent_page is None
                    or getattr(item, "page", None) != agent_page
                    or getattr(item, "lent", True)
                    for item in console.sessions.values()
                )
                if foreign or console.reserved:
                    raise ServiceError(
                        409, "Release device control before changing the test operator."
                    )
        runner = getattr(service, "current", None)
        recovery = getattr(service, "recovery", None)
        if (runner is not None and not runner.wait_closed(0)) or (
            recovery is not None and not recovery.closed.is_set()
        ):
            raise ServiceError(409, "Finish the current run before changing the test operator.")
        job = getattr(service, "job", None)
        if job is not None and not job["done"].is_set():
            raise ServiceError(409, "Wait for the current operation to finish.")
        transfer = getattr(service, "sample_transfer", None)
        if transfer is not None:
            report = transfer.operation_state(None)
            if report.get("settling") and agent_page is None:
                raise ServiceError(409, "Finish Sample Transfer before changing the test operator.")

    def _reconcile(self):
        if not self.simulation or self.simulation["status"] != "running":
            return
        try:
            self.authority().check(self.simulated_principal, self.simulation["control_epoch"])
        except ServiceError:
            self.simulation.update(
                status="cancelled", reason="Control changed or the session ended."
            )

    def status(self, caller=None):
        # Always take the host lock before the authority lock.
        with self.lock:
            report = self.authority().snapshot(caller)
            self._reconcile()
            report.update(
                simulation=dict(self.simulation) if self.simulation else None,
                phase=8,
                control_contract_version=6,
                command_contract_id=self.commands().catalog()["contract_id"],
                commands_enabled=self.commands_enabled,
                physical_commands_enabled=self.physical_commands_enabled,
                furnace_control_enabled=self.furnace_control_enabled,
                equipment_mode=self.equipment_mode,
                stop_enabled=self.stop_enabled,
                command_boot_id=self.authority().boot_id,
                identity_scope="command_control",
                command_cleanup=self._commands.cleanup_status()
                if self._commands
                else {"pending": [], "results": {}},
            )
            return report

    def simulate(self, caller, epoch, operation_id=None):
        with self.lock, self.authority().guard(caller, epoch):
            self._reconcile()
            if operation_id is None:
                if self.simulation and self.simulation["status"] == "running":
                    raise ServiceError(409, "Finish or stop the current simulated operation.")
                self.simulated_principal = caller
                self.simulation = {
                    "id": uuid.uuid4().hex,
                    "mode": "SIMULATED",
                    "status": "running",
                    "control_epoch": epoch,
                    "completed_steps": 0,
                    "total_steps": 3,
                    "reason": None,
                    "equipment_commands_sent": 0,
                }
            elif (
                not self.simulation
                or self.simulation["id"] != operation_id
                or self.simulation["status"] != "running"
                or self.simulated_principal is not caller
            ):
                raise ServiceError(
                    409, "Start a new simulated operation with the current operator."
                )
            else:
                self.simulation["completed_steps"] += 1
                if self.simulation["completed_steps"] == self.simulation["total_steps"]:
                    self.simulation["status"] = "completed"
            return self.status(caller)

    def stop_simulation(self):
        with self.lock:
            if self.simulation and self.simulation["status"] == "running":
                self.simulation.update(status="cancelled", reason="Simulated Stop requested.")
            return self.status()

    def close(self):
        self._monitor_stop.set()
        with self.lock:
            commands, authority, monitor = self._commands, self._authority, self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join()
        if commands is not None:
            commands.close()
        self.stops.close()
        if authority is not None:
            authority.close()


def install_control(
    app,
    service=None,
    *,
    prefix="/lab",
    authority=None,
    mock=False,
    source=None,
    allow_physical=False,
):
    """Install management routes with an explicit server-side physical policy."""
    host = ControlHost(
        service, authority=authority, mock=mock, source=source, allow_physical=allow_physical
    )
    app.extensions["agent_control"] = host
    app.extensions["agent_authority"] = host.authority
    if service is not None:
        service.agent_control = host
    base = prefix + CONTROL
    from .command_routes import register_commands

    register_commands(app, host, base)
    from .stop_routes import register_stops

    register_stops(app, host, prefix, base)

    @app.get(base + "/contract")
    def control_contract():
        return jsonify(
            version=6,
            phase=8,
            commands_enabled=host.commands_enabled,
            physical_commands_enabled=host.physical_commands_enabled,
            furnace_control_enabled=host.furnace_control_enabled,
            scope="this-console-only",
            equipment_mode=host.equipment_mode,
            authentication={"agent": "Bearer session_token", "console": "trusted-network"},
            transport="console-network",
            pairing={"approval": "invitation", "operator_grant": "explicit"},
            command_catalog_url=base + "/commands/catalog",
            command_history_url=base + "/commands/history",
            command_choices_url=base + "/commands/choices",
            stop_url=base + "/stop",
            stop_history_url=base + "/stops",
            stop_enabled=host.stop_enabled,
            identity_scope="command_control",
            command_boot_id=host.authority().boot_id,
            emergency_status_url=prefix + "/api/agent/emergency/status",
            heartbeat_interval_s=2,
            timeout_s=10,
            actions={
                "pair": {"fields": ["invitation", "name"], "role": "invited"},
                "pair/status": {"fields": ["credential"], "role": "paired"},
                "connect": {"fields": ["credential"], "role": "approved"},
                "heartbeat": {"fields": [], "role": "connected"},
                "disconnect": {"fields": [], "role": "connected"},
                "release": {"fields": ["control_epoch"], "role": "operator"},
                "simulate": {"fields": ["control_epoch"], "role": "operator"},
                "simulate/step": {"fields": ["control_epoch", "operation_id"], "role": "operator"},
                "simulate/stop": {"fields": [], "role": "any", "effect": "memory-only"},
            },
            furnace={"read": True, "operate": host.furnace_control_enabled},
            watchdog={"policy": "warn_only", "shutdown_enabled": False},
        )

    @app.before_request
    def management_access():
        if not request.path.startswith(base + "/"):
            return
        if request.method != "POST":
            return
        if request.path == base + "/stop":
            return
        if request.mimetype != "application/json":
            raise ServiceError(415, "Expected Content-Type: application/json.")
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            raise ServiceError(403, "Use the Console origin for control requests.")

    @app.get(base + "/assets/<name>")
    def control_asset(name):
        if name not in {"control-owner.js", "control-owner.css", "stop-panel.js"}:
            raise ServiceError(404, "Unknown approval asset.")
        return send_from_directory(ASSETS, name)

    @app.get(base + "/status")
    def control_status():
        authority = host.authority()
        try:
            caller = principal(authority)
        except ServiceError as error:
            if error.status != 401:
                raise
            caller = None
        return jsonify(host.status(caller))

    @app.post(base + "/pair")
    def pair():
        data = fields(("invitation", "name"), ("invitation", "name"))
        return jsonify(host.authority().pair(data["invitation"], data["name"]))

    @app.post(base + "/connect")
    def connect():
        data = fields(("credential",), ("credential",))
        return jsonify(host.authority().connect(data["credential"]))

    @app.post(base + "/pair/status")
    def pairing_status():
        data = fields(("credential",), ("credential",))
        return jsonify(host.authority().credential_status(data["credential"]))

    @app.post(base + "/<action>")
    def management(action):
        authority = host.authority()
        caller = principal(authority)
        if action == "invitations":
            fields()
            return jsonify(authority.create_invitation(caller))
        if action == "revoke":
            data = fields(("client_id",), ("client_id",))
            with authority.lock:
                host.idle()
                authority.revoke(caller, data["client_id"])
        elif action == "grant":
            data = fields(("session_id",), ("session_id",))
            with authority.lock:
                host.idle()
                authority.grant(caller, data["session_id"])
        elif action == "take-human":
            fields()
            # Fence the old operator. The command worker drains its work and leases.
            with authority.lock:
                host.idle()
                authority.take_human(caller)
        elif action == "release":
            data = fields(("control_epoch",), ("control_epoch",))
            with authority.lock:
                host.idle()
                authority.release(caller, data["control_epoch"])
        elif action == "acknowledge-warning":
            fields()
            authority.acknowledge_warning(caller)
        elif action in {"heartbeat", "disconnect"}:
            fields()
            if action == "disconnect" and caller.kind != "agent":
                raise ServiceError(403, "Disconnect applies only to an agent session.")
            if caller.kind == "agent":
                getattr(authority, action)(caller)
            if action == "disconnect":
                caller = None
        elif action == "simulate":
            data = fields(("control_epoch",), ("control_epoch",))
            return jsonify(host.simulate(caller, data["control_epoch"]))
        else:
            raise ServiceError(404, "Use a documented connection or command action.")
        host.reconcile_commands()
        return jsonify(host.status(caller))

    @app.post(base + "/simulate/step")
    def simulation_step():
        data = fields(("operation_id", "control_epoch"), ("operation_id", "control_epoch"))
        caller = principal(host.authority())
        return jsonify(host.simulate(caller, data["control_epoch"], data["operation_id"]))

    @app.post(base + "/simulate/stop")
    def simulation_stop():
        fields()
        return jsonify(host.stop_simulation())

    return host
