"""Local browser adapter with separate fixed live and mock transports."""

import copy
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from flask import Flask, jsonify, request, send_from_directory

from .client import ORIGIN, LocalTransport, NoRedirect, ScriptedClient
from .contract import FIXTURES
from .live import LiveTransport, register_live_routes
from .mock import ASSETS
from .scenarios import run_scenarios


def create_portal_app(
    transport=None,
    *,
    live_transport=None,
    control_transport=None,
    interpreter=None,
    chat_reader=None,
):
    transport = transport or LocalTransport()
    clients = {
        name: ScriptedClient(name, generation, transport)
        for name, generation in (("A", 1), ("B", 2))
    }
    lock = threading.RLock()
    results = {}
    app = Flask(__name__, static_folder=None)
    app.config["MOCK_CLIENTS"] = clients
    live_transport = live_transport if live_transport is not None else LiveTransport()
    register_live_routes(app, live_transport)
    from .control_portal import register_control_portal

    control = register_control_portal(app, control_transport, live_reader=live_transport.read)
    from .chat_routes import register_chat

    register_chat(app, control, live_transport.read, model=interpreter, reader=chat_reader)

    @app.get("/")
    def index():
        return send_from_directory(ASSETS, "portal.html")

    @app.get("/assets/<path:name>")
    def assets(name):
        return send_from_directory(ASSETS, name)

    @app.get("/api/status")
    def status():
        with lock:
            return jsonify(
                mode="MOCK",
                phase=0,
                fixtures=FIXTURES,
                clients=[client.poll() for client in clients.values()],
                results=results,
            )

    @app.get("/api/client/<name>/bundle")
    def bundle(name):
        if name not in clients:
            return jsonify(error="Unknown scripted client."), 404
        with lock:
            return jsonify(clients[name].bundle)

    @app.post("/api/control")
    def control():
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - {"action", "value", "client"}:
            return jsonify(error="Send a local mock control."), 400
        with lock:
            action = data.get("action")
            if action in {"connect", "disconnect"}:
                client = clients.get(data.get("client"))
                if client is None:
                    return jsonify(error="Choose client A or B."), 400
                client.connected = action == "connect"
                return jsonify(client.poll())
            if action not in {
                "pause",
                "resume",
                "stale",
                "reference-reset",
                "restart",
                "reset",
                "fixture",
            }:
                return jsonify(error="Unknown mock control."), 400
            if action == "fixture" and data.get("value") not in FIXTURES:
                return jsonify(error="Unknown fixture."), 400
            try:
                return jsonify(
                    transport("/mock/control", {"action": action, "value": data.get("value")})
                )
            except (OSError, ValueError) as error:
                return jsonify(error=str(error)), 503

    @app.post("/api/scenarios")
    def scenarios():
        with lock:
            results.clear()
            results.update(run_scenarios())
            return jsonify(results)

    @app.get("/api/export")
    def export():
        with lock:
            response = jsonify(
                mode="MOCK",
                phase=0,
                results=copy.deepcopy(results),
                clients=[client.report() for client in clients.values()],
                messages={name: list(client.messages) for name, client in clients.items()},
                bundles={name: list(client.cache.values()) for name, client in clients.items()},
            )
            response.headers["Content-Disposition"] = (
                'attachment; filename="agent-connect-mock-results.json"'
            )
            return response

    @app.get("/api/stream/read")
    def stream_read():
        path = request.args.get("path", "")
        parsed = urllib.parse.urlsplit(path)
        allowed = parsed.path in {
            "/lab/api/agent/discovery",
            "/lab/api/agent/v1/state",
            "/lab/api/agent/v1/samples",
        } or re.fullmatch(r"/lab/api/agent/contracts/[a-f0-9]{64}\.json", parsed.path)
        if parsed.scheme or parsed.netloc or parsed.fragment or not allowed:
            return jsonify(error="Use a local simulated data path."), 400
        return local_source(path)

    @app.post("/api/stream/sample")
    def sample_test():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Send a simulated sample action."), 400
        return local_source("/mock/samples", data)

    def local_source(path, data=None):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(
            ORIGIN + path, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with opener.open(ORIGIN + "/lab/api/agent/discovery", timeout=2) as response:
                discovery = json.loads(response.read(65536))
            if discovery.get("mode") != "MOCK":
                raise ValueError("Only the local simulated Console is supported.")
            with opener.open(req, timeout=2) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("The simulated response exceeds the size limit.")
            return jsonify(json.loads(raw))
        except urllib.error.HTTPError as error:
            return jsonify(error="The simulated Console refused the request."), error.code
        except (OSError, ValueError) as error:
            return jsonify(error=str(error)), 503

    return app
