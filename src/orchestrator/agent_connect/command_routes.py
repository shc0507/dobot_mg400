"""Phase 5 requests use an explicit action catalog, not arbitrary device routes."""

from flask import jsonify, request

from .control import fields, principal

MAX_REQUEST_BYTES = 6 * 1024 * 1024 + 1024
SUBMIT_FIELDS = (
    "request_id",
    "boot_id",
    "contract_id",
    "control_epoch",
    "action",
    "params",
    "preconditions",
)


def register_commands(app, host, base):
    @app.before_request
    def command_size_limit():
        if request.path == base + "/commands":
            request.max_content_length = MAX_REQUEST_BYTES

    @app.get(base + "/commands/catalog")
    def agent_command_catalog():
        return jsonify(host.commands().catalog())

    @app.get(base + "/commands/history")
    def agent_command_history():
        caller = principal(host.authority())
        return jsonify(host.commands().list(caller))

    @app.get(base + "/commands/choices")
    def agent_command_choices():
        caller = principal(host.authority())
        commands = host.commands()
        return jsonify(commands.backend.choices("agent-" + caller.id))

    @app.get(base + "/commands/request/<request_id>")
    def agent_command_lookup(request_id):
        caller = principal(host.authority())
        return jsonify(host.commands().get(caller, request_id))

    @app.post(base + "/commands")
    def agent_command_submit():
        caller = principal(host.authority())
        data = fields(SUBMIT_FIELDS, SUBMIT_FIELDS[:-1])
        return jsonify(host.commands().submit(caller, data)), 202

    @app.post(base + "/commands/cancel")
    def agent_command_cancel():
        caller = principal(host.authority())
        data = fields(("request_id",), ("request_id",))
        return jsonify(host.commands().cancel(caller, data["request_id"]))
