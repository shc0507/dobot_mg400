"""Furnace contract, stop routes and the operator fault acknowledgement.

Import opens no device or file.

The adapter uses only public lifecycle and Lease methods. Manual requests and
contract acquisition share the same lease lock. Contract acquisition/renewal
marks automation ownership, including when a client reuses a manual client ID.
Stop does not acquire a lease. Camera and recording routes are not installed.
"""

import copy
import math
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import minimalmodbus
from flask import Flask, jsonify
from werkzeug.exceptions import HTTPException

from labkit.describe import command, describe, parameter
from labkit.errors import ServiceError
from labkit.flask import contract_blueprint
from labkit.lease import validate_client

from .config import FurnaceConfig
from .controller import Furnace, MFCFlowController
from .service import FurnaceService
from .sim import FurnaceSerial, MFCSerial
from .state import StateStore

STOP_RETRY_S = 0.25


class FurnaceWebService:
    """Add contract metadata without bypassing the lifecycle's output guards."""

    # api/stop {"gas_channel": N} stops 1 gas channel and leaves the furnace running.
    stop_params = ("gas_channel",)

    def __init__(self, lifecycle, *, demo=False, temporary=None, wall_clock=time.time):
        self.lifecycle = lifecycle
        self.demo = bool(demo)
        self.events = lifecycle.events
        self.lease = lifecycle.lease
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
            "acknowledge": {},
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
                    ),
                    recovery=name == "acknowledge",
                )
                for name, properties in schema.items()
            },
        )

    def status(self):
        value = self.lifecycle.status()
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

    def _finish(self, serial, action, client, result, message="", params=None):
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
            command_id=value["id"],
            action=action,
            params=params or {},
            client=client,
            result=result,
            message_detail=message,
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
            elif action == "acknowledge":
                self.lifecycle.acknowledge(client=client)
            elif action == "set_gas_valve":
                self.lifecycle.set_gas_valve(args["channel_index"], args["open"], client=client)
            else:
                self.lifecycle.inspect_gas_flow(args["channel_index"], client=client)
        except (ValueError, ServiceError) as error:
            self._finish(serial, action, client, "rejected", str(error), args)
            raise
        except Exception as error:
            self._finish(serial, action, client, "failed", str(error), args)
            raise ServiceError(503, str(error)) from error
        state = self.lifecycle.status()
        if state["locked"]:
            self._finish(serial, action, client, "failed", state["fault"]["message"], args)
            raise ServiceError(409, state["fault"]["message"])
        self._finish(serial, action, client, "ok", "", args)
        return str(serial)

    def command(self, action, args, client):
        with self.lifecycle.lease.control(client):
            return self._execute(action, args, client)

    @staticmethod
    def _stop_confirmed(status):
        flow = status["flow"]
        return (
            status["run_state_code"] == 1
            and not status["locked"]
            and all(
                channel["available"] and channel["actual_percent"] == 0
                for channel in flow["gas_channels"]
            )
            and all(valve["open"] is False and valve["verified"] for valve in flow["gas_valves"])
        )

    def _gas_stop(self, client, channel):
        serial = self._begin()
        try:
            flow = self.lifecycle.stop_gas_flow(channel)
        except ValueError:
            raise
        except Exception as error:
            self._finish(serial, "gas_stop", client, "failed", str(error))
            return "unconfirmed"
        gas = [row for row in flow["gas_channels"] if row.get("channel_index") == channel]
        valve = [row for row in flow["gas_valves"] if row.get("channel_index") == channel]
        # HTTP success alone does not confirm a physical stop: read the flow and the valve back.
        confirmed = (
            flow["connected"] is True
            and flow["locked"] is False
            and flow["fault"] is None
            and flow["error"] is None
            and len(gas) == len(valve) == 1
            and gas[0].get("available") is True
            and gas[0].get("error") is None
            and gas[0].get("actual_percent") == 0
            and valve[0].get("open") is False
            and valve[0].get("verified") is True
        )
        result, message = ("stopped", "") if confirmed else ("failed", "Gas stop is unconfirmed")
        self._finish(serial, "gas_stop", client, result, message)
        return "confirmed" if confirmed else "unconfirmed"

    def stop(self, client, wait_s=0, gas_channel=None):
        validate_client(client)
        if gas_channel is not None:
            if type(gas_channel) is not int or not 0 <= gas_channel <= 2:
                raise ValueError("gas_channel must be 0, 1 or 2")
            return self._gas_stop(client, gas_channel)
        if (
            isinstance(wait_s, bool)
            or not isinstance(wait_s, (int, float))
            or not 0 <= wait_s <= 15
            or not math.isfinite(wait_s)
        ):
            raise ValueError("wait_s must be a finite number from 0 to 15")
        deadline = time.monotonic() + wait_s
        serial = self._begin()
        while True:
            try:
                status = self.lifecycle.stop_sequence()
            except Exception as error:
                self._finish(serial, "stop", client, "failed", str(error))
                return "unconfirmed"
            confirmed = self._stop_confirmed(status)
            # Repeat the stop, which is safe, until it is confirmed or wait_s ends.
            if confirmed or status["locked"] or time.monotonic() >= deadline:
                break
            time.sleep(min(STOP_RETRY_S, max(0, deadline - time.monotonic())))
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


def create_app(service, *, base_path="/"):
    """Serve the contract at the base path."""
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    app.register_blueprint(contract_blueprint(service, base_path=base_path, lease=service.lease))

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    return app
