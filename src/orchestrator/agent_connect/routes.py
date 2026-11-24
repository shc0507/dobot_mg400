"""Read-only HTTP routes and a receive-only socket session."""

import threading
import time
from urllib.parse import parse_qs

from flask import jsonify, request

from mg400.websocket import Connection, ConnectionClosed, ProtocolError

from .protocol import PREFIX
from .release import metadata, running_source_bundle
from .stream import Subscription, encoded


def options(query):
    """Parse HTTP and WebSocket query fields with the same rules."""
    allowed = {
        "groups",
        "rate_hz",
        "selection",
        "chip_ids",
        "chip_id",
        "boot_id",
        "event_seq",
    }
    if set(query) - allowed or any(
        len(values) != 1 for key, values in query.items() if key != "chip_id"
    ):
        raise ValueError("Unknown or repeated stream option.")

    def get(key, default):
        return query.get(key, [default])[0]

    groups = tuple(get("groups", "devices,samples,operations").split(","))
    subscription = Subscription(
        groups=groups,
        rate_hz=int(get("rate_hz", "2")),
        selection=get("selection", "active"),
        chip_ids=tuple(query.get("chip_id", []))
        or tuple(filter(None, get("chip_ids", "").split(","))),
    )
    cursor = None
    if "boot_id" in query or "event_seq" in query:
        if "boot_id" not in query or "event_seq" not in query:
            raise ValueError("A cursor requires boot_id and event_seq.")
        cursor = {"boot_id": get("boot_id", ""), "event_seq": int(get("event_seq", "0"))}
        if not cursor["boot_id"] or cursor["event_seq"] < 0:
            raise ValueError("The replay cursor is invalid.")
    return subscription, cursor


def register_routes(app, hub, *, prefix=PREFIX):
    app.extensions["agent_stream"] = hub
    if not hasattr(hub, "source_bundle"):
        hub.source_bundle = running_source_bundle()

    @app.get(prefix + "/discovery", endpoint="agent_discovery")
    def discovery():
        value = hub.discovery(prefix)
        value["identity_scope"] = "read_stream"
        host = app.extensions.get("agent_control")
        value["release"] = metadata(hub, host, read_contract_id=value["contract_id"])
        value["limits"] = {
            "stream_rates_hz": [1, 2, 4],
            "max_subscribers": hub.max_subscribers,
            "max_frame_bytes": hub.max_frame_bytes,
            "supplement_interval_s": 1,
            "vision": "latest retained or cached result only; no capture or inference",
            "device_state": "shared Console cache; no device reads per subscriber",
            "source_process_identity": "optional; absent identity leaves continuity unknown",
            "cached_state": "bounded public fields; check complete, omitted, and data_gaps",
        }
        if host is not None:
            value["control"] = {
                "version": 6,
                "phase": 8,
                "endpoint": prefix + "/control",
                "contract_url": prefix + "/control/contract",
                "commands_enabled": host.commands_enabled,
                "physical_commands_enabled": host.physical_commands_enabled,
                "identity_scope": "command_control",
                "command_boot_id": host.authority().boot_id,
                "command_contract_id": value["release"]["command_contract_id"],
                "stop_url": prefix + "/control/stop",
                "stop_history_url": prefix + "/control/stops",
                "stop_enabled": host.stop_enabled,
                "furnace_control_enabled": host.furnace_control_enabled,
                "equipment_mode": host.equipment_mode,
                "transport": "console-network",
                "command_catalog_url": prefix + "/control/commands/catalog",
            }
        response = jsonify(value)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get(prefix + "/contracts/<digest>.json", endpoint="agent_contract")
    def contract(digest):
        value = hub.contract("sha256:" + digest)
        if value is None:
            return jsonify(
                error="The contract is not retained. Discover the current contract."
            ), 404
        response = jsonify(value)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.get(prefix + "/v1/state", endpoint="agent_state")
    def state():
        try:
            subscription, _cursor = options(request.args.to_dict(flat=False))
            response = jsonify(hub.snapshot(subscription))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get(prefix + "/v1/samples", endpoint="agent_samples")
    def samples():
        try:
            query = request.args.to_dict(flat=False)
            if set(query) - {
                "selection",
                "chip_ids",
                "chip_id",
                "limit",
                "offset",
                "expected_revision",
            } or any(len(value) != 1 for key, value in query.items() if key != "chip_id"):
                raise ValueError("Unknown or repeated samples option.")
            response = jsonify(
                hub.samples(
                    selection=request.args.get("selection", "active"),
                    chip_ids=tuple(request.args.getlist("chip_id"))
                    or tuple(filter(None, request.args.get("chip_ids", "").split(","))),
                    limit=int(request.args.get("limit", 100)),
                    offset=int(request.args.get("offset", 0)),
                    expected_revision=int(request.args["expected_revision"])
                    if "expected_revision" in request.args
                    else None,
                )
            )
        except ValueError as error:
            return jsonify(error=str(error)), 400
        response.headers["Cache-Control"] = "no-store"
        return response


def serve_socket(handler, hub, subscriber):
    """Send state and events. Reject every application message from the reader."""
    handler.connection.settimeout(60)
    connection = Connection(handler.connection, handler.rfile, handler.wfile, queue_limit=4)
    handler.server.track(connection)
    done = threading.Event()

    def receive():
        try:
            if connection.receive() is not None:
                connection.close(1008, "This stream accepts no commands.")
        except ProtocolError as error:
            connection.close(error.code, str(error))
        except (EOFError, OSError, ConnectionClosed):
            pass
        finally:
            done.set()

    reader = threading.Thread(target=receive, name="agent-stream-reader", daemon=True)
    reader.start()
    ping_at = time.monotonic() + 20
    try:
        while not done.is_set() and not hub.stopping.is_set() and not connection.aborted:
            # Do not transfer state into a transport queue while a previous frame waits.
            # The subscriber keeps only a cursor and reads the latest shared state later.
            if connection.backlog == 0:
                message = subscriber.poll()
                if message is not None:
                    connection.send_text(encoded(message))
                    continue
                if time.monotonic() >= ping_at:
                    connection.ping()
                    ping_at = time.monotonic() + 20
            with hub.changed:
                hub.changed.wait(0.05)
    except (ConnectionClosed, OSError, ValueError):
        pass
    finally:
        subscriber.close()
        connection.close()
        connection.close_written.wait(0.1)
        connection.abort()
        connection.join(1)
        reader.join(1)
        handler.server.untrack(connection)


def socket_subscription(hub, query):
    subscription, cursor = options(parse_qs(query, keep_blank_values=True))
    return hub.subscribe(subscription, cursor)
