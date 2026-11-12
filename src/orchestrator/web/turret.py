"""Console routes for operator-guided microscope turret calibration."""

import copy
import re

from flask import Response, jsonify, request

from labkit.errors import ServiceError
from labkit.lease import validate_client

MAX_PROFILE_BYTES = 2 * 1024 * 1024
FIELDS = {
    "begin": {"name", "new"},
    "record": {"objective_id"},
    "finish": set(),
    "cancel": set(),
    "save": {"name"},
    "select": {"id"},
    "load": {"id"},
    "delete": {"id"},
    "upload": {"profile"},
}


def register(blueprint, service):
    """Proxy each request once, using the existing microscope control lease."""

    @blueprint.before_app_request
    def turret_upload_limit():
        if (
            request.endpoint == blueprint.name + ".turret_action"
            and (request.view_args or {}).get("action") == "upload"
        ):
            request.max_content_length = 6 * MAX_PROFILE_BYTES + 1024

    def page():
        return validate_client(request.headers.get("X-MG400-Client"))

    def preview():
        return getattr(service, "turret_preview", None)

    def decorate(value, client):
        value = copy.deepcopy(value)
        session = value.get("session")
        if preview() is not None:
            value["demo"] = True
            value["frame_url"] = request.path.split("/turret", 1)[0] + "/turret/frame"
            value.setdefault("camera", {})["frame_url"] = value["frame_url"]
            if isinstance(session, dict):
                session["mine"] = session.get("client") == client
            return value
        control = service.console.control(client)
        mine = next(
            (
                item
                for item in control
                if item.get("device") == "microscope"
                and item.get("mine")
                and item.get("state") == "held"
            ),
            None,
        )
        value["has_control"] = mine is not None
        if isinstance(session, dict):
            session["mine"] = mine is not None and mine.get("holder") == session.get("client")
        return value

    @blueprint.get("/turret")
    def turret_state():
        client = page()
        value = (
            preview().state(client)
            if preview() is not None
            else service.console.read("microscope", "api/turret-calibration")
        )
        return jsonify(decorate(value, client))

    @blueprint.post("/turret/<action>")
    def turret_action(action):
        if action not in FIELDS:
            raise ServiceError(404, "Unknown turret calibration action.")
        client = page()
        data = request.get_json()
        if not isinstance(data, dict) or set(data) - FIELDS[action]:
            raise ValueError("Send only the fields for this turret calibration action.")
        if action == "upload":
            profile = data.get("profile")
            if not isinstance(profile, str):
                raise ValueError("Upload a calibration profile JSON file.")
            try:
                size = len(profile.encode("utf-8"))
            except UnicodeError as error:
                raise ValueError("The profile must contain valid UTF-8 text.") from error
            if size > MAX_PROFILE_BYTES:
                raise ServiceError(413, "The calibration profile exceeds 2 MiB.")
        if preview() is not None:
            value = preview().action(client, action, data)
        else:
            result = service.console.operator(
                "microscope", client, "api/turret-calibration/" + action, data
            )
            if result.get("outcome") != "accepted":
                return jsonify(result), 409 if result.get("outcome") == "refused" else 502
            value = result.get("result")
            if not isinstance(value, dict):
                raise ServiceError(502, "The microscope returned an invalid calibration state.")
        return jsonify(decorate(value, client))

    @blueprint.get("/turret/profile/<identifier>")
    def turret_profile(identifier):
        page()
        if re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
            raise ValueError("Choose a saved turret calibration profile.")
        value = (
            preview().profile(identifier)
            if preview() is not None
            else service.console.read("microscope", "api/turret-calibration/profile/" + identifier)
        )
        return jsonify(value)

    @blueprint.get("/turret/frame")
    def turret_preview_frame():
        if preview() is None:
            raise ServiceError(404, "The synthetic turret camera is unavailable.")
        return Response(
            preview().frame(), mimetype="image/jpeg", headers={"Cache-Control": "no-store"}
        )
