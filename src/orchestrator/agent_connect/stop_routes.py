"""Stop routes and passive attribution for existing human emergency routes."""

import re
import uuid

from flask import g, jsonify, request

from labkit.errors import ServiceError

REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def legacy_target(path):
    if path.startswith("/api/console/stop/"):
        device = path.rsplit("/", 1)[-1]
        return (
            device
            if device in {"robot", "stage", "microscope", "furnace", "thermal", "vision"}
            else None
        )
    return {
        "/api/dashboard/motion": "all_motion",
        "/api/dashboard/heat": "furnace",
        "/api/dashboard/gas": "furnace",
        "/api/console/ws/process/gas-stop": "furnace",
        "/api/console/ws/sample-transfer/stop": "stage",
    }.get(path)


def register_stops(app, host, prefix, base):
    @app.post(base + "/stop")
    def agent_stop():
        request.max_content_length = 4096
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ServiceError(400, "Send a Stop request ID and target as JSON.")
        sender = host.stop_sender(request.headers.get("Authorization"))
        transport = request.environ.get("mg400.transport", "http")
        return jsonify(host.stops.submit(body, sender, transport)), 202

    @app.get(base + "/stops")
    def stop_history():
        return jsonify(host.stops.history())

    @app.get(base + "/stops/<request_id>")
    def stop_record(request_id):
        return jsonify(host.stops.get(request_id))

    @app.before_request
    def observe_legacy_start():
        if request.method != "POST" or not request.path.startswith(prefix + "/"):
            return
        target = legacy_target(request.path[len(prefix) :])
        if target is None:
            return
        commands = host._commands
        if commands is not None:
            # Fence queued work before the human route sends its existing Stop.
            # This changes only engine memory and never sends another device request.
            commands.interrupt_devices(target, "A human Stop was requested.")
        identifier = request.headers.get("X-MG400-Stop-ID", "")
        if not REQUEST_ID.fullmatch(identifier):
            identifier = uuid.uuid4().hex
        g.agent_stop_observation = (
            {"request_id": identifier, "target": target},
            host.stop_sender(request.headers.get("Authorization")),
            request.environ.get("mg400.transport", "http"),
        )

    @app.after_request
    def observe_legacy_finish(response):
        context = getattr(g, "agent_stop_observation", None)
        if context is None:
            return response
        body, sender, transport = context
        try:
            payload = response.get_json(silent=True)
            payload = payload if isinstance(payload, dict) else {}
            outcome = payload.get("outcome", payload.get("stop", "unknown"))
            operation = payload.get("operation")
            operation_id = payload.get("operation_id")
            if not isinstance(operation_id, str):
                operation_id = operation.get("id") if isinstance(operation, dict) else operation
            if operation or operation_id:
                outcome = "pending"
            if response.status_code >= 400:
                outcome = "unknown"
            targets = (
                ("stage", "robot", "microscope")
                if body["target"] == "all_motion"
                else (body["target"],)
            )
            outcomes = [
                {"device": device, "outcome": outcome, "operation_id": operation_id}
                for device in targets
            ]
            host.stops.observe(body, sender, transport, outcomes)
        except Exception:
            # Stop already ran. A record failure cannot change its response.
            pass
        response.headers["X-MG400-Stop-ID"] = body["request_id"]
        return response
