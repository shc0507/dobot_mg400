"""Furnace contract and operator page. Import opens no device or file.

The adapter uses only public lifecycle and Lease methods. Manual requests and
contract acquisition share the same lease lock. Contract acquisition/renewal
marks automation ownership, including when a client reuses a manual client ID.
Stop does not acquire a lease. Camera and recording routes are not installed.
"""

import copy
import math
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import minimalmodbus
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from labkit.auth import bearer_matches, host_allowed, same_origin, valid_token_for_bind
from labkit.describe import command, describe, parameter
from labkit.errors import ServiceError
from labkit.flask import contract_blueprint
from labkit.lease import validate_client

from .config import FurnaceConfig
from .controller import Furnace, MFCFlowController
from .service import FurnaceService
from .sim import FurnaceSerial, MFCSerial
from .state import StateStore


class _AutomationLease:
    """Classify contract ownership atomically through the public lease API."""

    def __init__(self, owner):
        self.owner = owner
        self.lease = owner.lifecycle.lease

    @property
    def on_expire(self):
        return self.lease.on_expire

    @on_expire.setter
    def on_expire(self, value):
        self.lease.on_expire = value

    def acquire(self, client):
        with self.lease.control(client, require_holder=False):
            result = self.lease.acquire(client)
            self.owner.manual_holder = None
            return result

    def renew(self, client):
        with self.lease.control(client):
            result = self.lease.renew(client)
            self.owner.manual_holder = None
            return result

    def release(self, client):
        with self.lease.control(client, require_holder=False):
            result = self.lease.release(client)
            if self.owner.manual_holder == client:
                self.owner.manual_holder = None
            return result

    def snapshot(self):
        return self.lease.snapshot()

    def poll(self):
        return self.lease.poll()

    def control(self, client, *, require_holder=True):
        return self.lease.control(client, require_holder=require_holder)


class FurnaceWebService:
    """Add contract metadata without bypassing the lifecycle's output guards."""

    def __init__(self, lifecycle, *, demo=False, temporary=None, wall_clock=time.time):
        self.lifecycle = lifecycle
        self.demo = bool(demo)
        self.events = lifecycle.events
        self.manual_holder = None
        self.lease = _AutomationLease(self)
        self._temporary = temporary
        self._wall_clock = wall_clock
        self._metadata_lock = threading.Lock()
        self._serial = self._seq = 0
        self._last_command = None
        self._startup_error = None
        self._background = True
        self._started = False
        self._start_attempted = False

    def start(self, *, background=True):
        self._background = background
        # Bad port settings are configuration errors, detected before serving.
        if not self.lifecycle.port:
            raise ValueError("Set MG400_FURNACE_SERIAL before starting the service")
        self._start_attempted = True
        try:
            self.lifecycle.start(background=background)
            self._started = True
            self._startup_error = None
        except (OSError, RuntimeError) as error:
            # The page can report a known disconnected link and accept an
            # explicit Connect request. There is no automatic connection retry.
            self._startup_error = str(error)
            self.events.add("error", "Furnace connection failed", error=str(error))

    def close(self):
        try:
            if self._start_attempted or self.lifecycle.connected or self.lifecycle.flow_connected:
                return self.lifecycle.close()
        finally:
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None

    def on_lease_expired(self):
        return self.lifecycle.on_lease_expired()

    def describe(self):
        def number(**kw):
            return parameter("number", **kw)

        steps = parameter("array", minItems=1, items=parameter("object"))
        schema = {
            "connect": {},
            "connect_flow": {},
            "apply_setpoint": {
                "temperature_c": number(minimum=0, maximum=self.lifecycle.config.max_c),
                "ramp_rate_c_per_min": number(
                    exclusiveMinimum=0, maximum=self.lifecycle.config.max_ramp
                ),
            },
            "set_sequence": {
                "steps": steps,
                "end_gas": parameter("object"),
                "end_gas_until_c": number(minimum=0),
            },
            "start_sequence": {
                "steps": steps,
                "end_gas": parameter("object"),
                "end_gas_until_c": number(minimum=0),
            },
            "set_gas_flow": {
                "channel_index": parameter("integer", minimum=0, maximum=2),
                "flow_percent": number(minimum=0, maximum=100),
            },
            "set_gas_valve": {
                "channel_index": parameter("integer", minimum=0, maximum=2),
                "open": parameter("boolean"),
            },
            "inspect_gas_flow": {"channel_index": parameter("integer", minimum=0, maximum=2)},
        }
        required = {
            "apply_setpoint": ["temperature_c", "ramp_rate_c_per_min"],
            "set_sequence": ["steps"],
            "set_gas_flow": ["channel_index", "flow_percent"],
            "set_gas_valve": ["channel_index", "open"],
            "inspect_gas_flow": ["channel_index"],
        }
        return describe(
            service="furnace",
            version="1",
            demo=self.demo,
            zones=["furnace-tube"],
            lease_seconds=self.lifecycle.lease.seconds,
            on_lease_expiry=(
                "Continue bounded sequences and cooling gas. "
                "Start orphan grace for an open-ended setpoint."
            ),
            commands={
                name: command(
                    parameter(
                        "object",
                        properties=properties,
                        required=required.get(name, []),
                        additionalProperties=False,
                    )
                )
                for name, properties in schema.items()
            },
        )

    def status(self):
        value = self.lifecycle.status()
        holder = value["lease"]["holder"]
        value["manual_client"] = self.manual_holder if holder == self.manual_holder else None
        value["demo"] = self.demo
        if self._startup_error and not value["connected"]:
            value["error"] = self._startup_error
        return value

    def snapshot(self):
        status = self.status()
        with self._metadata_lock:
            self._seq += 1
            metadata = dict(seq=self._seq, last_command=copy.deepcopy(self._last_command))
        return dict(
            service="furnace",
            ts_unix=self._wall_clock(),
            busy=status["sequence"]["running"] or status["sequence"]["phase"] == "cooling",
            pv_c=status["pv"],
            target_c=status["sv"],
            ramp_c_per_min=status["ramp"],
            flow_pct=[item["actual_percent"] for item in status["flow"]["gas_channels"]],
            **status,
            **metadata,
        )

    def _begin(self):
        with self._metadata_lock:
            self._serial += 1
            return self._serial

    def _finish(self, serial, action, client, result, message=""):
        value = dict(
            id=str(serial),
            action=action,
            client=client,
            result=result,
            message=message,
            finished_at=self._wall_clock(),
        )
        with self._metadata_lock:
            if serial == self._serial:
                self._last_command = value
        self.events.add(
            "info" if result in ("ok", "stopped") else "warning",
            "Furnace command finished",
            command=value,
        )

    def _execute(self, action, args, client):
        if not isinstance(args, dict):
            raise ValueError("Command parameters must be an object")
        properties = self.describe()["commands"].get(action, {}).get("params", {}).get("properties")
        if properties is None or set(args) - set(properties):
            raise ValueError("Unknown command or parameters")
        required = self.describe()["commands"][action]["params"]["required"]
        if any(name not in args for name in required):
            raise ValueError("Missing command parameters")
        serial = self._begin()
        try:
            if action == "connect":
                self._start_attempted = True
                self.lifecycle.connect(client=client)
                self.lifecycle.start(background=self._background)
                self._started = True
                self._startup_error = None
            elif action == "connect_flow":
                self.lifecycle.connect_flow_controller(client=client)
            elif action == "apply_setpoint":
                self.lifecycle.apply_setpoint(
                    args["temperature_c"], args["ramp_rate_c_per_min"], client=client
                )
            elif action in ("set_sequence", "start_sequence"):
                getattr(self.lifecycle, action)(
                    args.get("steps"),
                    end_gas=args.get("end_gas"),
                    end_gas_until_c=args.get("end_gas_until_c"),
                    client=client,
                )
            elif action == "set_gas_flow":
                self.lifecycle.set_gas_flow(
                    args["channel_index"], args["flow_percent"], client=client
                )
            elif action == "set_gas_valve":
                self.lifecycle.set_gas_valve(args["channel_index"], args["open"], client=client)
            else:
                self.lifecycle.inspect_gas_flow(args["channel_index"], client=client)
        except (ValueError, ServiceError) as error:
            self._finish(serial, action, client, "rejected", str(error))
            raise
        except Exception as error:
            self._finish(serial, action, client, "failed", str(error))
            raise ServiceError(503, str(error)) from error
        state = self.lifecycle.status()
        if state["locked"]:
            self._finish(serial, action, client, "failed", state["fault"]["message"])
            raise ServiceError(409, state["fault"]["message"])
        self._finish(serial, action, client, "ok")
        return str(serial)

    def command(self, action, args, client):
        with self.lifecycle.lease.control(client):
            self.manual_holder = None
            return self._execute(action, args, client)

    @contextmanager
    def _manual_control(self, client, *, renew_only=False):
        lease = self.lifecycle.lease
        with lease.control(client, require_holder=False):
            holder = lease.snapshot()["holder"]
            if holder is not None and (holder != client or self.manual_holder != client):
                raise ServiceError(
                    409, "Another client controls the furnace. Stop remains available."
                )
            if renew_only:
                if holder != client or self.manual_holder != client:
                    raise ServiceError(
                        409, "Manual control expired. Take control again explicitly."
                    )
                lease.renew(client)
            else:
                lease.acquire(client)
                self.manual_holder = client
            yield

    def manual_heartbeat(self, client, *, renew_only=False):
        with self._manual_control(client, renew_only=renew_only):
            return self.lifecycle.lease.snapshot()

    def manual_release(self, client):
        with self.lifecycle.lease.control(client, require_holder=False):
            holder = self.lifecycle.lease.snapshot()["holder"]
            if holder is not None and (holder != client or self.manual_holder != client):
                raise ServiceError(409, "Another client controls the furnace")
            result = self.lifecycle.lease.release(client)
            self.manual_holder = None
            return result

    def manual_command(self, action, args, client):
        with self._manual_control(client):
            if action == "acknowledge":
                if args:
                    raise ValueError("Acknowledgement takes no parameters")
                self.lifecycle.acknowledge(client=client)
                return None
            return self._execute(action, args, client)

    def stop(self, client, wait_s=0):
        validate_client(client)
        if (
            isinstance(wait_s, bool)
            or not isinstance(wait_s, (int, float))
            or not 0 <= wait_s <= 15
            or not math.isfinite(wait_s)
        ):
            raise ValueError("wait_s must be a finite number from 0 to 15")
        serial = self._begin()
        try:
            status = self.lifecycle.stop_sequence()
        except Exception as error:
            self._finish(serial, "stop", client, "failed", str(error))
            return "unconfirmed"
        flow = status["flow"]
        confirmed = (
            status["run_state_code"] == 1
            and not status["locked"]
            and all(
                channel["available"] and channel["actual_percent"] == 0
                for channel in flow["gas_channels"]
            )
            and all(valve["open"] is False and valve["verified"] for valve in flow["gas_valves"])
        )
        self._finish(
            serial,
            "stop",
            client,
            "stopped" if confirmed else "failed",
            "" if confirmed else "Output stop is unconfirmed",
        )
        return "confirmed" if confirmed else "unconfirmed"


def make_service(
    *,
    demo=False,
    config=None,
    gas_map=None,
    idle_gas=None,
    orphan_grace_min=None,
    clock=time.monotonic,
    wall_clock=time.time,
):
    """Construct live settings or isolated demo transports without connecting."""
    if not demo:
        lifecycle = FurnaceService(
            config=config,
            gas_map=gas_map,
            idle_gas=idle_gas,
            orphan_grace_min=orphan_grace_min,
            clock=clock,
            wall_clock=wall_clock,
        )
        return FurnaceWebService(lifecycle, wall_clock=wall_clock)
    temporary = TemporaryDirectory(prefix="mg400-furnace-demo-")
    furnace_serial = FurnaceSerial(clock=clock)
    flow_serial = MFCSerial()

    def furnace_factory(**settings):
        furnace_serial.open()
        instrument = minimalmodbus.Instrument(furnace_serial, settings["slave_address"])
        return Furnace.attach(instrument=instrument, **settings)

    def flow_factory(**settings):
        flow_serial.open()
        return MFCFlowController(serial_connection=flow_serial, **settings)

    try:
        lifecycle = FurnaceService(
            config=FurnaceConfig(port="sim://furnace", flow_port="sim://mfc"),
            gas_map={0: "Demo gas 1", 1: "Demo gas 2", 2: "Demo gas 3"},
            idle_gas={},
            orphan_grace_min=15,
            state_store=StateStore(Path(temporary.name) / "furnace/state.json"),
            furnace_factory=furnace_factory,
            flow_factory=flow_factory,
            clock=clock,
            wall_clock=wall_clock,
        )
        return FurnaceWebService(lifecycle, demo=True, temporary=temporary, wall_clock=wall_clock)
    except BaseException:
        temporary.cleanup()
        raise


def create_app(service, *, token=None, base_path="/"):
    """Serve an inert public shell and authenticated contract/legacy APIs."""
    if not valid_token_for_bind(token, "localhost"):
        raise ValueError("A configured token needs at least 24 printable ASCII characters")
    blueprint = contract_blueprint(service, token=token, base_path=base_path, lease=service.lease)
    prefix = base_path.rstrip("/")
    app = Flask(__name__, static_url_path=prefix + "/static")
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.register_blueprint(blueprint)

    @app.before_request
    def access():
        if not host_allowed(request.headers, token) or not same_origin(request.headers):
            raise ServiceError(403, "The request host or origin is not allowed")
        if request.endpoint not in ("page", "static"):
            if token and not bearer_matches(request.headers.get("Authorization", ""), token):
                raise ServiceError(401, "Invalid service access token")
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if request.mimetype != "application/json":
                raise ServiceError(415, "Expected Content-Type: application/json")
            validate_client(request.headers.get("X-MG400-Client"))

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(ValueError)
    def bad_request(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    @app.get(prefix + "/")
    def page():
        return render_template(
            "index.html",
            base_path=prefix,
            demo=service.demo,
            max_c=service.lifecycle.config.max_c,
            max_ramp=service.lifecycle.config.max_ramp,
        )

    @app.get(prefix + "/api/status")
    def status():
        service.lease.poll()
        return jsonify(furnace=service.status())

    @app.post(prefix + "/api/manual/<operation>")
    def manual_lease(operation):
        if body():
            raise ValueError("Manual lease requests take an empty object")
        client = request.headers["X-MG400-Client"]
        if operation == "heartbeat":
            value = service.manual_heartbeat(client)
        elif operation == "renew":
            value = service.manual_heartbeat(client, renew_only=True)
        elif operation == "release":
            value = service.manual_release(client)
        else:
            raise ValueError("Unknown manual lease operation")
        return jsonify(lease=value)

    routes = {
        "/api/furnace/connect": "connect",
        "/api/furnace/apply": "apply_setpoint",
        "/api/furnace/acknowledge": "acknowledge",
        "/api/flow/connect": "connect_flow",
        "/api/flow/gas/set": "set_gas_flow",
        "/api/flow/gas/valve": "set_gas_valve",
        "/api/flow/gas/inspect": "inspect_gas_flow",
        "/api/sequence": "set_sequence",
        "/api/sequence/start": "start_sequence",
    }

    def manual_route(action):
        def dispatch():
            args = body()
            client = request.headers["X-MG400-Client"]
            if action == "set_gas_valve" and args.get("open") is False:
                if set(args) != {"channel_index", "open"}:
                    raise ValueError("Expected channel_index and open")
                result = service.lifecycle.stop_gas_flow(args["channel_index"])
                return jsonify(ok=not result["locked"], status=result)
            service.manual_command(action, args, client)
            result = (
                service.lifecycle.flow_status()
                if action in ("connect_flow", "set_gas_flow", "set_gas_valve", "inspect_gas_flow")
                else service.status()
            )
            return jsonify(ok=True, status=result)

        return dispatch

    for index, (route, action) in enumerate(routes.items()):
        app.add_url_rule(
            prefix + route,
            f"manual_{index}",
            manual_route(action),
            methods=["PUT" if route == "/api/sequence" else "POST"],
        )

    @app.post(prefix + "/api/furnace/stop")
    @app.post(prefix + "/api/sequence/stop")
    def stop_furnace():
        if body():
            raise ValueError("Stop takes an empty object")
        outcome = service.stop(request.headers["X-MG400-Client"])
        return jsonify(ok=outcome == "confirmed", stop=outcome, status=service.status())

    @app.post(prefix + "/api/flow/gas/stop")
    def stop_gas():
        args = body()
        if set(args) != {"channel_index"}:
            raise ValueError("Expected channel_index")
        result = service.lifecycle.stop_gas_flow(args["channel_index"])
        return jsonify(ok=not result["locked"], status=result)

    @app.post(prefix + "/api/flow/stop-all")
    def stop_flow():
        if body():
            raise ValueError("Stop takes an empty object")
        result = service.lifecycle.stop_all_flow()
        return jsonify(ok=not result["locked"], status=result)

    return app
