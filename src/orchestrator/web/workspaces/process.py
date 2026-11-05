"""Console workspace: Furnace + microscope.

``GET status`` returns the furnace and microscope fields that the page shows,
the thermal ``crucible`` region reading, a 60-minute PV and SV history and the
cutoff setting. Every described device command goes through the console relay.

Two operator actions are not in the service descriptions, so the relay and the
runner never reach them. Each needs this page's control of the device and an
in-page confirmation, and is sent once:

- ``POST focus-zero`` ``{"confirmed": true}``: microscope focus zero at the
  current focus position.
- ``POST acknowledge`` ``{"confirmed": true}``: acknowledge a furnace fault.

``POST gas-stop`` ``{"channel_index": 0|1|2}`` closes 1 furnace gas channel
through the furnace gas-stop route. Like a stop, it needs no control, it is sent
once with a fresh identity and the reply is confirmed, unconfirmed or unknown.

- The furnace and microscope fields come from the console's last service read.
- The crucible reading is one ``api/roi?name=crucible`` call at most every
  ``CRUCIBLE_S`` seconds, without a lease.
- The history is sampled while a console page reads this route. It starts
  empty when the console process starts.
"""

import copy
import math
import threading
import time
import uuid
from collections import deque

from flask import jsonify, request

from labkit.errors import ServiceError
from labkit.lease import validate_client

from ..backend import gas_confirmed
from . import Workspace

HISTORY_S = 3600
SAMPLE_S = 5.0
CRUCIBLE_S = 2.0

FURNACE_KEYS = (
    "connected",
    "locked",
    "fault",
    "error",
    "pv",
    "sv",
    "ramp",
    "run_state",
    "control_mode",
    "controller_program_step",
    "waiting_for_program_step_2",
    "orphan_time",
)
SEQUENCE_KEYS = (
    "running",
    "steps",
    "current_index",
    "phase",
    "hold_remaining_seconds",
    "end_gas",
    "end_gas_until_c",
)
CHANNEL_KEYS = ("channel_index", "gas_name", "target_percent", "actual_percent", "available")
VALVE_KEYS = ("channel_index", "open", "verified")
MICROSCOPE_KEYS = (
    "connected",
    "busy",
    "locked",
    "fault",
    "objective_id",
    "objective_offset_steps",
    "objective_offset_limit_steps",
    "focus_position_pulses",
    "focus_zero_known",
    "focus_limits_pulses",
    "cameras",
    "last_command",
    "recording",
    "recording_sequence",
    "stabilization",
)
RECORDING_KEYS = ("id", "status", "client", "run_name", "mode", "frame_count", "error")


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) else None


def _pick(source, keys):
    return {key: copy.deepcopy(source.get(key)) for key in keys} if isinstance(source, dict) else {}


def furnace_view(raw):
    if not isinstance(raw, dict):
        return None
    value = _pick(raw, FURNACE_KEYS)
    value["sequence"] = _pick(raw.get("sequence"), SEQUENCE_KEYS)
    flow = raw.get("flow") if isinstance(raw.get("flow"), dict) else {}
    channels = flow.get("gas_channels") if isinstance(flow.get("gas_channels"), list) else []
    valves = flow.get("gas_valves") if isinstance(flow.get("gas_valves"), list) else []
    value["flow"] = {
        "connected": flow.get("connected") is True,
        "error": flow.get("error") if isinstance(flow.get("error"), str) else None,
        "gas_channels": [_pick(item, CHANNEL_KEYS) for item in channels[:3]],
        "gas_valves": [_pick(item, VALVE_KEYS) for item in valves[:3]],
    }
    return value


def microscope_view(raw):
    if not isinstance(raw, dict):
        return None
    value = _pick(raw, MICROSCOPE_KEYS)
    value["recording"] = _pick(raw.get("recording"), RECORDING_KEYS)
    value["recording_sequence"] = _pick(raw.get("recording_sequence"), ("armed", "id", "client"))
    return value


class ProcessStatus:
    def __init__(self, service, *, wall=time.time, clock=time.monotonic):
        self.service, self.wall, self.clock = service, wall, clock
        self.lock = threading.Lock()
        self.history = deque(maxlen=int(HISTORY_S / SAMPLE_S) + 2)
        self.crucible = None
        self.crucible_at = None
        self.crucible_busy = False

    def _raw(self):
        console = self.service.console
        with console.lock:
            raw = copy.deepcopy(console.raw)
            read_at = console.events_at
        age = None if read_at is None else round(max(0.0, console.clock() - read_at), 1)
        return raw, age

    def _sample(self, furnace, now):
        pv = _number((furnace or {}).get("pv"))
        sv = _number((furnace or {}).get("sv"))
        with self.lock:
            while self.history and now - self.history[0][0] > HISTORY_S:
                self.history.popleft()
            if pv is None and sv is None:
                return
            if self.history and now - self.history[-1][0] < SAMPLE_S:
                return
            self.history.append((now, pv, sv))

    def _read_crucible(self):
        with self.lock:
            due = self.crucible_at is None or self.clock() - self.crucible_at >= CRUCIBLE_S
            if self.crucible_busy or not due:
                return copy.deepcopy(self.crucible)
            self.crucible_busy = True
        value = None
        adapter = None
        try:
            if self.service.dashboard_configured("thermal"):
                adapter = self.service.console_client("thermal", f"console-read-{uuid.uuid4().hex}")
            if adapter is not None and hasattr(adapter, "roi"):
                reading = adapter.roi("crucible")
                if isinstance(reading, dict):
                    value = {
                        key: _number(reading.get(key))
                        for key in ("min_c", "mean_c", "max_c", "frame_age_s")
                    }
                    if value["max_c"] is None:
                        value = None
        except Exception:
            value = None
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass
            with self.lock:
                self.crucible, self.crucible_at = value, self.clock()
                self.crucible_busy = False
        return copy.deepcopy(value)

    def report(self):
        raw, age = self._raw()
        furnace = furnace_view(raw.get("furnace"))
        now = self.wall()
        self._sample(furnace, now)
        with self.lock:
            history = [list(item) for item in self.history]
        return {
            "now": now,
            "age_s": age,
            "furnace": furnace,
            "microscope": microscope_view(raw.get("microscope")),
            "crucible": self._read_crucible(),
            "history": history,
        }


def register(blueprint, service):
    status = ProcessStatus(service)

    @blueprint.get("/status")
    def read_status():
        service.console.refresh_events()
        return jsonify(status.report())

    def operator(device, path, data):
        if request.get_json() != {"confirmed": True}:
            raise ValueError("Confirm the action inside the page.")
        page = request.headers.get("X-MG400-Client")
        if page is None:
            raise ServiceError(400, "Send the page identity in X-MG400-Client.")
        result = service.console.operator(device, validate_client(page), path, data)
        result.pop("result", None)
        status_code = {"accepted": 202, "refused": 409, "unknown": 502}[result["outcome"]]
        return jsonify(result), status_code

    @blueprint.post("/focus-zero")
    def focus_zero():
        return operator("microscope", "api/manual/set_focus_zero", {"confirm": True})

    @blueprint.post("/acknowledge")
    def acknowledge():
        return operator("furnace", "api/furnace/acknowledge", {})

    @blueprint.post("/gas-stop")
    def gas_stop():
        # The app accepts only a JSON object in a POST body.
        data = request.get_json()
        channel = data.get("channel_index")
        if set(data) != {"channel_index"} or type(channel) is not int or not 0 <= channel <= 2:
            raise ValueError("Send channel_index 0, 1 or 2 only.")
        validate_client(request.headers.get("X-MG400-Client"))
        adapter = service.console_client("furnace", f"console-stop-{uuid.uuid4().hex}")
        if adapter is None:
            raise ServiceError(409, "The furnace is not configured or its demo is not running.")
        try:
            confirmed = gas_confirmed(adapter.gas_stop(channel), channel)
            outcome = "confirmed" if confirmed else "unconfirmed"
        except Exception:
            outcome = "unknown"
        finally:
            try:
                adapter.close()
            except Exception:
                pass
        return jsonify(channel_index=channel, outcome=outcome)


WORKSPACE = Workspace(
    id="process",
    order=40,
    title={"en": "Furnace + microscope", "zh": "炉子与显微镜"},
    register=register,
)
