"""Read-only HTTP and WebSocket access to the fixed Olares Agent Connect service."""

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing

from flask import Response, jsonify, request
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from .protocol import PREFIX
from .routes import options
from .stream import Subscription

ORIGIN = "http://100.64.0.1:8082"
WS_ORIGIN = "ws://100.64.0.1:8082"
DISCOVERY = PREFIX + "/discovery"
STATE = PREFIX + "/v1/state"
SAMPLES = PREFIX + "/v1/samples"
STREAM = PREFIX + "/v1/stream"
MAX_BYTES = 2 * 1024 * 1024
MAX_PATH = 16384
TIMEOUT_S = 3
HEARTBEAT_S = 1
HEARTBEAT = object()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise ValueError("Olares redirects are disabled.")


def read_path(path, *, discovery=False):
    """Accept only known GET routes and validated query fields."""
    if not isinstance(path, str) or not path.startswith("/") or len(path) > MAX_PATH:
        raise ValueError("Use an Agent Connect read path.")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("The read path contains a control character.")
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or "#" in path:
        raise ValueError("Use an Agent Connect read path.")
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True, max_num_fields=60)
    if re.fullmatch(re.escape(PREFIX) + r"/contracts/[a-f0-9]{64}\.json", parsed.path):
        if parsed.query:
            raise ValueError("The contract path accepts no query fields.")
    elif discovery and path == DISCOVERY:
        pass
    elif parsed.path == STATE:
        options(query)
    elif parsed.path == SAMPLES:
        _sample_options(query)
    else:
        raise ValueError("Use an immutable contract, state, or samples path.")
    return path


def _sample_options(query):
    allowed = {"selection", "chip_ids", "chip_id", "limit", "offset", "expected_revision"}
    if set(query) - allowed or any(
        len(values) != 1 for key, values in query.items() if key != "chip_id"
    ):
        raise ValueError("Unknown or repeated samples option.")

    def get(key, default):
        return query.get(key, [default])[0]

    selection = get("selection", "active")
    chip_ids = tuple(query.get("chip_id", [])) or tuple(
        filter(None, get("chip_ids", "").split(","))
    )
    if selection == "all":
        if chip_ids:
            raise ValueError("Chip IDs require selection=ids.")
    else:
        Subscription(selection=selection, chip_ids=chip_ids)
    if not 1 <= int(get("limit", "100")) <= 100 or int(get("offset", "0")) < 0:
        raise ValueError("Use a limit from 1 to 100 and a nonnegative offset.")
    if "expected_revision" in query and int(get("expected_revision", "0")) < 0:
        raise ValueError("Use a nonnegative sample revision.")


def event_options(query):
    """Keep the full default subscription. Accept a rate and a replay cursor."""
    if set(query) - {"rate_hz", "boot_id", "event_seq"} or any(
        len(values) != 1 for values in query.values()
    ):
        raise ValueError("Unknown or repeated live stream option.")
    rate = query.get("rate_hz", ["2"])[0]
    if rate not in {"1", "2", "4"}:
        raise ValueError("Select a rate of 1, 2, or 4 Hz.")
    result = {"rate_hz": rate}
    if "boot_id" in query or "event_seq" in query:
        boot = query.get("boot_id", [""])[0]
        sequence = query.get("event_seq", [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,256}", boot) or not re.fullmatch(
            r"[0-9]{1,20}", sequence
        ):
            raise ValueError("A cursor requires boot_id and a nonnegative event_seq.")
        result.update(boot_id=boot, event_seq=sequence)
    return result


def _json(raw):
    if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_BYTES:
        raise ValueError("The Olares response exceeds the size limit.")

    def invalid_constant(value):
        raise ValueError("The Olares response contains an invalid JSON number.")

    try:
        return json.loads(raw, parse_constant=invalid_constant)
    except RecursionError as error:
        raise ValueError("The Olares JSON nesting exceeds the limit.") from error


class LiveTransport:
    """Ignore proxies, reject redirects, and send no application messages."""

    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def read(self, path):
        path = read_path(path, discovery=True)
        req = urllib.request.Request(ORIGIN + path, headers={"Accept": "application/json"})
        with self.opener.open(req, timeout=TIMEOUT_S) as response:
            return _json(response.read(MAX_BYTES + 1))

    def events(self, query):
        query = event_options({key: [value] for key, value in query.items()})
        url = WS_ORIGIN + STREAM + "?" + urllib.parse.urlencode(query)
        # A supplied socket prevents websockets from following HTTP redirects.
        with closing(socket.create_connection(("100.64.0.1", 8082), timeout=TIMEOUT_S)) as sock:
            with connect(
                url,
                sock=sock,
                origin=ORIGIN,
                proxy=None,
                compression=None,
                open_timeout=TIMEOUT_S,
                close_timeout=1,
                max_size=MAX_BYTES,
                max_queue=4,
            ) as upstream:
                while True:
                    try:
                        raw = upstream.recv(timeout=HEARTBEAT_S)
                    except TimeoutError:
                        yield HEARTBEAT
                    else:
                        yield _json(raw)


def _event(name, value):
    return "event: " + name + "\ndata: " + json.dumps(value, allow_nan=False) + "\n\n"


def register_live_routes(app, transport=None):
    transport = transport if transport is not None else LiveTransport()

    def read(path):
        try:
            response = jsonify(transport.read(path))
        except urllib.error.HTTPError as error:
            return jsonify(error="Olares refused the read request."), error.code
        except (OSError, ValueError) as error:
            return jsonify(error=str(error)), 503
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/live/discovery")
    def live_discovery():
        if request.args:
            return jsonify(error="Discovery accepts no query fields."), 400
        return read(DISCOVERY)

    @app.get("/api/live/read")
    def live_read():
        if set(request.args) != {"path"} or len(request.args.getlist("path")) != 1:
            return jsonify(error="Send 1 Agent Connect read path."), 400
        try:
            path = read_path(request.args["path"])
        except ValueError as error:
            return jsonify(error=str(error)), 400
        return read(path)

    @app.get("/api/live/events")
    def live_events():
        try:
            query = event_options(request.args.to_dict(flat=False))
        except ValueError as error:
            return jsonify(error=str(error)), 400

        def relay():
            yield ": relay\n\n"
            try:
                with closing(transport.events(query)) as messages:
                    for message in messages:
                        yield (
                            ": heartbeat\n\n"
                            if message is HEARTBEAT
                            else _event("message", message)
                        )
            except (OSError, ValueError, WebSocketException) as error:
                yield _event("connection-error", {"error": str(error)})

        return Response(
            relay(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
