"""Lab Console routes."""

import re

from flask import Blueprint, abort, jsonify, render_template, request, send_from_directory

from labkit.errors import ServiceError
from labkit.lease import validate_client

from ..clients import SERVICES
from .workspaces import discover

_ASSET = re.compile(r"[a-z][a-z0-9-]{0,31}\.(js|css|woff2|glb)")


def _page():
    """The browser page identity; console sessions are bound to it."""
    value = request.headers.get("X-MG400-Client")
    if value is None:
        raise ServiceError(400, "Send the page identity in X-MG400-Client.")
    return validate_client(value)


def _device(name):
    if name not in SERVICES:
        raise ServiceError(404, "Unknown console device.")
    return name


def _empty():
    if request.get_json() != {}:
        raise ValueError("Expected an empty object.")


def console_blueprints(service, prefix):
    workspaces = discover()

    def console():
        # Resolved per request: some service stubs have no console.
        return service.console

    blueprint = Blueprint("console", __name__, template_folder="templates", url_prefix=prefix)
    static = blueprint.root_path + "/static/console"

    @blueprint.get("/")
    def index():
        return render_template(
            "console.html",
            base_path=prefix,
            commit=getattr(getattr(service, "console", None), "commit", "unknown"),
            workspaces=workspaces,
        )

    @blueprint.get("/console-static/<name>")
    def asset(name):
        if not _ASSET.fullmatch(name):
            abort(404)
        return send_from_directory(static, name)

    @blueprint.get("/api/console/state")
    def state():
        page = request.headers.get("X-MG400-Client")
        page = validate_client(page) if page else None
        service.dashboard.refresh()
        console().refresh_events()
        report = service.dashboard.snapshot()
        report.update(
            console={"commit": console().commit, "demo": service.demo},
            control=console().control(page),
            workspaces=[{"id": item.id, "title": item.title} for item in workspaces],
        )
        return jsonify(report)

    @blueprint.get("/api/console/state/<name>")
    def device_state(name):
        page = request.headers.get("X-MG400-Client")
        page = validate_client(page) if page else None
        return jsonify(console().device_state(_device(name), page))

    @blueprint.get("/api/console/events")
    def events():
        console().refresh_events()
        return jsonify(console().event_report())

    @blueprint.get("/api/console/describe/<name>")
    def describe(name):
        return jsonify(console().describe(_device(name)))

    @blueprint.post("/api/console/heartbeat")
    def heartbeat():
        _empty()
        return jsonify(control=console().heartbeat(_page()))

    @blueprint.post("/api/console/control/<name>/take")
    def take(name):
        _empty()
        return jsonify(control=console().take(_device(name), _page()))

    @blueprint.post("/api/console/control/<name>/release")
    def release(name):
        _empty()
        return jsonify(console().release(_device(name), _page()))

    @blueprint.post("/api/console/release-page")
    def release_page():
        _empty()
        return jsonify(results=console().release_all(_page()))

    @blueprint.post("/api/console/release-all")
    def release_all():
        _empty()
        _page()
        return jsonify(results=console().release_all())

    @blueprint.post("/api/console/command/<name>")
    def command(name):
        data = request.get_json()
        if set(data) - {"action", "params"} or "action" not in data:
            raise ValueError("Send action and optional params only.")
        result = console().command(_device(name), _page(), data["action"], data.get("params", {}))
        status = {"accepted": 202, "refused": 409, "unknown": 502}[result["outcome"]]
        return jsonify(result), status

    @blueprint.post("/api/console/stop/<name>")
    def stop(name):
        _empty()
        _page()
        return jsonify(console().stop(_device(name)))

    result = [blueprint]
    for item in workspaces:
        if item.register is None:
            continue
        child = Blueprint(
            "console_ws_" + item.id.replace("-", "_"),
            __name__,
            url_prefix=f"{prefix}/api/console/ws/{item.id}",
        )
        item.register(child, service)
        result.append(child)
    return result
