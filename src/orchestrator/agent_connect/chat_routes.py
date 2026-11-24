"""Local chat endpoints; all equipment commands use the existing agent connection."""

import atexit
import json
import urllib.request

from flask import jsonify, request

from labkit.errors import ServiceError

from .chat import ChatAgent
from .chat_context import ChatContextReader
from .chat_model import OpenAIInterpreter
from .live import NoRedirect, read_path


def mock_read(path):
    path = read_path(path, discovery=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open("http://127.0.0.1:8790" + path, timeout=3) as response:
        raw = response.read(262145)
    if len(raw) > 262144:
        raise ServiceError(502, "The simulated state exceeds the supported size.")
    return json.loads(raw)


def register_chat(app, control, live_reader, *, model=None, reader=None, profiles=None):
    if profiles is None and model is None:
        from .model_profiles import ModelProfileStore

        profiles = ModelProfileStore()
    model = model or OpenAIInterpreter(api_key="" if profiles is not None else None)
    if profiles is not None and profiles.status()["storage_available"]:
        active = profiles.active()
        if active is not None:
            model.configure_profile(active)
    chat = ChatAgent(
        control,
        reader or ChatContextReader({"live": live_reader, "mock": mock_read}),
        model,
        profiles=profiles,
    )
    app.extensions["agent_portal_chat"] = chat
    atexit.register(chat.close)
    prefix = "/api/agent-chat"

    def body(fields, optional=()):
        if request.content_length is not None and request.content_length > 16384:
            raise ServiceError(413, "The local chat request is too large.")
        value = request.get_json(silent=True)
        if request.mimetype != "application/json" or not isinstance(value, dict):
            raise ServiceError(400, "Send a JSON object.")
        if not set(fields) <= set(value) or set(value) - set(fields) - set(optional):
            raise ServiceError(400, "Send only the documented chat fields.")
        return value

    @app.before_request
    def cancel_pending_on_manual_control():
        if request.method != "POST" or request.path not in {
            "/api/agent-control/stop",
            "/api/agent-control/disconnect",
            "/api/agent-control/release",
            "/api/agent-control/connect",
            "/api/agent-control/pair",
            "/api/agent-control/commands",
            "/api/agent-control/commands/cancel",
        }:
            return
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            source = data.get("source", "live")
            if isinstance(source, str) and source in {"live", "mock"}:
                chat.invalidate(source)

    @app.get(prefix + "/status")
    def chat_status():
        if set(request.args) - {"source"}:
            raise ServiceError(400, "Send only the source query field.")
        response = jsonify(chat.status(request.args.get("source", "live")))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(prefix + "/settings")
    def settings():
        data = body({"api_key", "model"})
        return jsonify(chat.configure(data["api_key"], data["model"]))

    @app.get(prefix + "/profiles")
    def profiles_status():
        response = jsonify(chat.profile_status())
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(prefix + "/profiles")
    def add_profile():
        data = body({"name", "provider", "model", "base_url", "api_key"}, optional={"route"})
        return jsonify(chat.change_profile("add", data)), 201

    @app.post(prefix + "/profiles/activate")
    def activate_profile():
        return jsonify(chat.change_profile("activate", body({"profile_id"})))

    @app.post(prefix + "/profiles/delete")
    def delete_profile():
        return jsonify(chat.change_profile("delete", body({"profile_id"})))

    @app.post(prefix + "/message")
    def message():
        data = body({"source", "message", "request_id"})
        return jsonify(chat.submit(**data)), 202

    @app.post(prefix + "/cancel")
    def cancel():
        data = body({"source"})
        return jsonify(chat.cancel(data["source"]))

    return chat
