"""One PC agent connection. Credentials remain in this process, outside the page."""

import atexit
import ipaddress
import json
import re
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from flask import jsonify, request

from labkit.errors import ServiceError

from .live import NoRedirect
from .portal_runtime import PortalSession

ORIGINS = {"live": "http://100.64.0.1:8082", "mock": "http://127.0.0.1:8790"}
PREFIX = "/lab/api/agent/control"
MAX_RESPONSE = 262144
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
STOP_TARGETS = {
    "stage",
    "robot",
    "microscope",
    "all_motion",
    "furnace",
    "gas_all",
    "gas_0",
    "gas_1",
    "gas_2",
}
FALLBACKS = {
    "mock": "http://127.0.0.1:8794",
    "live": "http://100.64.0.1:8082/lab/api/agent/emergency",
}


def command_path(action):
    return action in {
        "commands",
        "commands/cancel",
        "commands/catalog",
        "commands/history",
        "commands/choices",
    } or (
        action.startswith("commands/request/")
        and bool(REQUEST_ID.fullmatch(action.removeprefix("commands/request/")))
    )


def stop_read_path(action):
    return action == "stops" or (
        action.startswith("stops/") and bool(REQUEST_ID.fullmatch(action.removeprefix("stops/")))
    )


def merge_stop_records(records):
    """Keep each transport's record, including its server-resolved sender."""
    grouped = {}
    for origin, record in records:
        key = (record.get("request_id"), record.get("target"))
        if not all(key):
            continue
        previous = grouped.get(key, {})
        sources = {**previous.get("relay_sources", {}), origin: record}
        grouped[key] = {
            **previous,
            **record,
            "updated_at": max(item.get("updated_at", 0) for item in sources.values()),
            "relay_sources": sources,
        }
    return sorted(grouped.values(), key=lambda item: item.get("requested_at", 0), reverse=True)


class ControlTransport:
    """Use the same VPN Console address for control and the public read stream."""

    def __call__(self, source, action, data=None, *, token=None):
        if source not in ORIGINS:
            raise ValueError("Choose live or mock.")
        if (
            action
            not in {
                "status",
                "pair",
                "pair/status",
                "connect",
                "disconnect",
                "heartbeat",
                "release",
                "simulate",
                "simulate/step",
                "simulate/stop",
                "stop",
            }
            and not command_path(action)
            and not stop_read_path(action)
        ):
            raise ValueError("Use a documented agent control action.")
        return self._request(ORIGINS[source] + PREFIX + "/" + action, data, token, source)

    def fallback(self, source, action, data=None):
        if source not in FALLBACKS:
            raise ValueError("Choose live or mock.")
        if action not in {"stop", "status"} and not stop_read_path(action):
            raise ValueError("Use a documented Stop-only fallback route.")
        result = self._request(FALLBACKS[source] + "/" + action, data, None, source)
        return {**result, "relay_source": "fallback"}

    def _request(self, url, data, token, source):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        headers = {"Accept": "application/json", "X-MG400-Client": "agent-portal"}
        if token:
            headers["Authorization"] = "Bearer " + token
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data, allow_nan=False).encode()
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with opener.open(req, timeout=3) as response:
                raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise ValueError("The connection response exceeds the size limit.")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("The connection response is invalid.")
            return result
        except urllib.error.HTTPError as error:
            # Do not include request bodies, credentials, or arbitrary response text.
            messages = {
                401: (
                    "The session ended or the invitation is invalid or expired. "
                    "Reconnect or use a new invitation."
                ),
                403: (
                    "The Console refused this action. Check connection access and operator control."
                ),
                409: "Control changed or the test Console is busy. Refresh its status.",
            }
            raise ServiceError(
                error.code, messages.get(error.code, "The Console refused the request.")
            ) from None
        except (OSError, ValueError):
            hint = (
                "Connect LarePass and check the Olares test Console on port 8082."
                if source == "live"
                else "Start the local simulated Console on port 8790."
            )
            raise ServiceError(503, hint) from None


class PortalControl:
    def __init__(self, transport=None, *, live_reader=None):
        self.transport = transport or ControlTransport()
        self.live_reader = live_reader
        self.lock = threading.RLock()
        self.records = {source: {} for source in ORIGINS}
        self.sessions = {}
        self.closed = False

    def _source(self, source):
        if not isinstance(source, str) or source not in self.records:
            raise ServiceError(400, "Choose live or mock.")
        return self.records[source]

    def _record(self, source):
        record = self._source(source)
        session = self.sessions.get(source)
        if session is not None and session.stopped.is_set():
            record.pop("session_token", None)
        return record

    def local_status(self, source, *, include_state=False):
        with self.lock:
            self._record(source)
            session = self.sessions.get(source)
            return (
                session.snapshot(include_state=include_state)
                if session is not None
                else {"running": False, "heartbeat_owner": "process", "read_collection": None}
            )

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            sessions = list(self.sessions.values())
            tokens = {
                source: record.pop("session_token", None) for source, record in self.records.items()
            }
            for session in sessions:
                session.stop()
        for source, token in tokens.items():
            if token:
                try:
                    self.transport(source, "disconnect", {}, token=token)
                except (ServiceError, OSError, ValueError):
                    pass
        for session in sessions:
            session.join()

    def status(self, source):
        with self.lock:
            record = self._record(source)
            token = record.get("session_token")
            state = self.transport(source, "status", token=token)
            phase = state.get("phase")
            enabled = state.get("commands_enabled")
            legacy = (phase == 4 and enabled is False) or (
                phase in {5, 6}
                and enabled is (source == "mock")
                and state.get("physical_commands_enabled") is False
                and state.get("equipment_mode") == ("simulated" if source == "mock" else "disabled")
            )
            compatible = legacy and state.get("furnace_control_enabled") is False
            if phase in {8, 9}:
                enabled_source = (
                    enabled is True
                    and state.get("physical_commands_enabled") is (source == "live")
                    and state.get("equipment_mode")
                    == ("physical" if source == "live" else "simulated")
                    and isinstance(state.get("furnace_control_enabled"), bool)
                )
                disabled_live = (
                    source == "live"
                    and enabled is False
                    and state.get("physical_commands_enabled") is False
                    and state.get("equipment_mode") == "disabled"
                    and state.get("furnace_control_enabled") is False
                )
                compatible = enabled_source or disabled_live
            if not compatible:
                if source in self.sessions:
                    self.sessions[source].stop(
                        "The Console control contract changed. Connect again."
                    )
                    record.pop("session_token", None)
                raise ServiceError(409, "The Console control contract does not match this source.")
            current = state.get("principal")
            connected = bool(token and current and current.get("kind") == "agent")
            session = self.sessions.get(source)
            if session is not None and (
                session.stopped.is_set()
                or (session.boot_id and state.get("boot_id") != session.boot_id)
                or (
                    token and session.session_id and (current or {}).get("id") != session.session_id
                )
            ):
                connected = False
            if token and not connected:
                record.pop("session_token", None)
                if session is not None:
                    session.stop("The Console session ended. Connect again.")
            client_status = None
            if record.get("credential"):
                client_status = self.transport(
                    source, "pair/status", {"credential": record["credential"]}
                ).get("status")
            return {
                **state,
                "source": source,
                "paired": bool(record.get("credential")),
                "connected": connected,
                "client_id": record.get("client_id"),
                "name": record.get("name"),
                "pairing_code": record.get("matching_code"),
                "client_status": client_status,
                "console_url": ORIGINS[source] + "/lab/#agent-connect",
                "credential_storage": "memory",
                "local_agent": self.local_status(source),
            }

    def read_commands(self, source, action):
        with self.lock:
            record = self._record(source)
            if self.closed:
                raise ServiceError(503, "The local agent process is stopping.")
            if not command_path(action) or action in {"commands", "commands/cancel"}:
                raise ServiceError(404, "Choose a documented command read route.")
            token = record.get("session_token")
            if action != "commands/catalog" and not token:
                raise ServiceError(401, "Connect the agent first.")
            return self.transport(source, action, token=token)

    def command(self, source, action, data, *, before_dispatch=None):
        record = self._source(source)
        if action == "commands/cancel":
            expected = {"request_id"}
        else:
            expected = {
                "request_id",
                "boot_id",
                "contract_id",
                "control_epoch",
                "action",
                "params",
                "preconditions",
            }
        if (
            set(data) != expected
            or not isinstance(data.get("request_id"), str)
            or not REQUEST_ID.fullmatch(data["request_id"])
        ):
            raise ServiceError(400, "Send the documented command fields and a valid request ID.")
        if not record.get("session_token"):
            raise ServiceError(401, "Connect the agent first.")
        state = self.status(source)
        if not state["connected"]:
            raise ServiceError(401, "Connect the agent first.")
        if state.get("phase") not in {5, 6, 8, 9} or state.get("commands_enabled") is not True:
            raise ServiceError(403, "Commands are unavailable for this source.")
        if action == "commands":
            principal = state.get("principal") or {}
            operator = state.get("operator") or {}
            if (
                not state["connected"]
                or not principal.get("id")
                or principal.get("id") != operator.get("id")
            ):
                raise ServiceError(403, "Grant operator control from the Console first.")
            if not isinstance(data["params"], dict) or not isinstance(data["preconditions"], dict):
                raise ServiceError(
                    400, "Send command parameters and preconditions as JSON objects."
                )
            if state.get("phase") in {8, 9} and (
                not state.get("command_contract_id")
                or data["boot_id"] != state.get("boot_id")
                or data["control_epoch"] != state.get("control_epoch")
                or data["contract_id"] != state.get("command_contract_id")
            ):
                raise ServiceError(409, "Control changed. Refresh before sending a command.")
        if before_dispatch is not None:
            before_dispatch()
        return self.transport(source, action, data, token=record["session_token"])

    def stop(self, source, data):
        # Stop must not wait for a normal request, status check, or authority lock.
        record = self._source(source)
        if (
            set(data) != {"request_id", "target"}
            or not isinstance(data.get("request_id"), str)
            or not REQUEST_ID.fullmatch(data["request_id"])
            or not isinstance(data.get("target"), str)
            or data["target"] not in STOP_TARGETS
        ):
            raise ServiceError(400, "Send a valid Stop request ID and device target.")
        try:
            return self.transport(source, "stop", data, token=record.get("session_token"))
        except ServiceError as error:
            fallback = getattr(self.transport, "fallback", None)
            if error.status != 503 or fallback is None:
                raise
            return fallback(source, "stop", data)

    def read_stops(self, source, action):
        record = self._source(source)
        if not stop_read_path(action):
            raise ServiceError(404, "Choose a documented Stop history route.")
        fallback = getattr(self.transport, "fallback", None)
        if fallback is None:
            return self.transport(source, action, token=record.get("session_token"))
        results, failures = {}, {}
        for origin in ("console", "fallback"):
            try:
                results[origin] = (
                    self.transport(source, action, token=record.get("session_token"))
                    if origin == "console"
                    else fallback(source, action)
                )
            except ServiceError as error:
                failures[origin] = error
        if not results:
            raise next(
                (error for error in failures.values() if error.status != 404),
                next(iter(failures.values())),
            )
        if action != "stops":
            records = merge_stop_records(results.items())
            if not records:
                raise ServiceError(502, "The Stop detail response is invalid.")
            return records[0]
        items = merge_stop_records(
            (origin, item) for origin, result in results.items() for item in result.get("items", [])
        )[:50]
        source_status = {
            origin: {"available": True, "audit": result.get("audit")}
            for origin, result in results.items()
        }
        source_status.update(
            {
                origin: {"available": False, "error": error.message}
                for origin, error in failures.items()
            }
        )
        return {"items": items, "count": len(items), "source_status": source_status}

    def action(self, source, action, data):
        if action == "stop":
            return self.stop(source, data)
        with self.lock:
            record = self._record(source)
            if self.closed:
                raise ServiceError(503, "The local agent process is stopping.")
            if action in {"commands", "commands/cancel"}:
                return self.command(source, action, data)
            if action == "pair":
                if set(data) != {"invitation", "name"}:
                    raise ServiceError(400, "Send an invitation and an agent name.")
                if record.get("session_token"):
                    raise ServiceError(409, "Disconnect before pairing another agent.")
                result = self.transport(source, "pair", data)
                if not isinstance(result.get("credential"), str) or not result.get("client_id"):
                    raise ServiceError(502, "The Console returned an invalid pairing result.")
                record.clear()
                record.update(result)
                record["name"] = data["name"]
                previous = self.sessions.pop(source, None)
                if previous is not None:
                    previous.stop()
            elif action == "connect":
                if data or not record.get("credential"):
                    raise ServiceError(400, "Pair this portal before connecting.")
                if record.get("session_token"):
                    raise ServiceError(409, "Disconnect before starting another connection.")
                result = self.transport(source, "connect", {"credential": record["credential"]})
                if not isinstance(result.get("session_token"), str):
                    raise ServiceError(502, "The Console returned an invalid session.")
                record["session_token"] = result["session_token"]
                session = PortalSession(
                    source, result, self.transport, self.live_reader if source == "live" else None
                )
                self.sessions[source] = session
                session.start()
            elif action in {
                "disconnect",
                "heartbeat",
                "release",
                "simulate",
                "simulate/step",
                "simulate/stop",
            }:
                allowed = {
                    "release": {"control_epoch"},
                    "simulate": {"control_epoch"},
                    "simulate/step": {"control_epoch", "operation_id"},
                }.get(action, set())
                if set(data) != allowed:
                    raise ServiceError(400, "Send the documented control fields.")
                if action != "simulate/stop" and not record.get("session_token"):
                    raise ServiceError(401, "Connect the approved agent first.")
                if action == "disconnect" and source in self.sessions:
                    self.sessions[source].stop()
                try:
                    self.transport(source, action, data, token=record.get("session_token"))
                finally:
                    if action == "disconnect":
                        record.pop("session_token", None)
            else:
                raise ServiceError(404, "Equipment commands are not available in this portal.")
            return self.status(source)


def register_control_portal(app, transport=None, *, live_reader=None):
    control = PortalControl(transport, live_reader=live_reader)
    app.extensions["agent_portal_control"] = control
    atexit.register(control.close)

    @app.errorhandler(ServiceError)
    def control_error(error):
        return jsonify(error=error.message), error.status

    @app.before_request
    def control_access():
        if not request.path.startswith(("/api/agent-control/", "/api/agent-chat/")):
            return
        # This server holds agent credentials in memory and binds only to this PC.
        host = urlsplit(request.host_url).hostname
        try:
            local = ipaddress.ip_address(request.remote_addr or "").is_loopback and (
                host == "localhost" or ipaddress.ip_address(host or "").is_loopback
            )
        except ValueError:
            local = False
        if not local:
            raise ServiceError(403, "Open the agent portal on this PC.")
        origin = request.headers.get("Origin")
        if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
            return jsonify(error="Use the local portal origin."), 403

    @app.get("/api/agent-control/status", endpoint="agent_portal_control_status")
    def status():
        try:
            return jsonify(control.status(request.args.get("source", "live")))
        except ServiceError as error:
            return jsonify(error=error.message), error.status

    @app.post("/api/agent-control/<path:action>", endpoint="agent_portal_control_action")
    def action(action):
        data = request.get_json(silent=True)
        if request.mimetype != "application/json" or not isinstance(data, dict):
            return jsonify(error="Send a JSON control request."), 400
        data = dict(data)
        source = data.pop("source", "live")
        try:
            return jsonify(control.action(source, action, data))
        except ServiceError as error:
            return jsonify(error=error.message), error.status

    @app.get("/api/agent-control/latest", endpoint="agent_portal_latest")
    def latest():
        if set(request.args) - {"source"}:
            raise ServiceError(400, "Send only the source query field.")
        response = jsonify(
            control.local_status(request.args.get("source", "live"), include_state=True)
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/agent-control/commands/<path:action>", endpoint="agent_portal_command_read")
    def command_read(action):
        if set(request.args) - {"source"}:
            raise ServiceError(400, "Send only the source query field.")
        return jsonify(
            control.read_commands(request.args.get("source", "live"), "commands/" + action)
        )

    @app.get("/api/agent-control/stops", endpoint="agent_portal_stop_history")
    @app.get("/api/agent-control/stops/<request_id>", endpoint="agent_portal_stop_detail")
    def stop_read(request_id=None):
        if set(request.args) - {"source"}:
            raise ServiceError(400, "Send only the source query field.")
        action = "stops" if request_id is None else "stops/" + request_id
        return jsonify(control.read_stops(request.args.get("source", "live"), action))

    return control
