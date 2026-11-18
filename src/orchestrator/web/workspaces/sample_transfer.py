"""Sample Transfer routes use the existing Console page identity."""

import json

from flask import Response, jsonify, request

from labkit.lease import validate_client
from orchestrator.transfer_profiles import MAX_PROFILE_BYTES
from orchestrator.web.sample_transfer import get_sample_transfer

from . import Workspace


def register(blueprint, service):
    def page():
        return validate_client(request.headers.get("X-MG400-Client"))

    @blueprint.before_app_request
    def upload_limit():
        if request.endpoint == blueprint.name + ".profile_upload":
            request.max_content_length = 6 * MAX_PROFILE_BYTES + 1024

    @blueprint.get("/state")
    def state():
        return jsonify(get_sample_transfer(service).state(page()))

    @blueprint.get("/profile/<identifier>")
    def download(identifier):
        page()
        payload = json.dumps(
            get_sample_transfer(service).store.export(identifier), ensure_ascii=False
        )
        return Response(
            payload,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="sample-transfer-{identifier}.json"',
                "Cache-Control": "no-store",
            },
        )

    def action(name, allowed):
        data = request.get_json()
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("Send the supported Sample Transfer fields as a JSON object.")
        current = page()
        manager = get_sample_transfer(service)
        if name == "stop":
            outcome = manager.stop()
            return jsonify({**manager.state(current), "stop": outcome})
        if name == "move":
            return jsonify(manager.start(current, data.get("target"))), 202
        if name == "jog":
            return jsonify(manager.jog(current, data.get("motor"), data.get("direction"))), 202
        if name == "measure":
            return jsonify(manager.measure(current))
        return jsonify(manager.mutate(current, name, data))

    fields = {
        "measure": set(),
        "profile/new": {"name"},
        "record": {"name"},
        "position/delete": {"name"},
        "profile/save": {"name"},
        "profile/select": {"id"},
        "profile/load": {"id"},
        "profile/delete": {"id"},
        "profile/upload": {"profile"},
        "move": {"target"},
        "jog": {"motor", "direction"},
        "stop": set(),
    }
    for name, allowed in fields.items():
        blueprint.add_url_rule(
            "/" + name,
            endpoint=name.replace("/", "_"),
            view_func=lambda name=name, allowed=allowed: action(name, allowed),
            methods=["POST"],
        )


WORKSPACE = Workspace(
    id="sample-transfer",
    order=50,
    title={"en": "Sample Transfer", "zh": "样品转移"},
    register=register,
)
