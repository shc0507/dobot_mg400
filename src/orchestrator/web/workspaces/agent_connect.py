"""Agent Connect connection status and observations from the existing cache."""

from flask import jsonify

from ...clients import SERVICES
from . import Workspace

OBSERVATION_FIELDS = (
    "instance",
    "reference_revision",
    "reference_change_reason",
    "reference_valid",
    "frame_sequence",
    "frame_age_s",
    "feedback_fresh",
    "pv_c",
    "target_c",
    "connected",
    "busy",
    "job_id",
)


def status(service):
    rows = []
    for name in SERVICES:
        entry = service.devices.get(name)
        state = entry.get("state") or {}
        observation = {key: state[key] for key in OBSERVATION_FIELDS if key in state}
        if name == "stage":
            observation["motors"] = [
                {
                    key: motor[key]
                    for key in ("id", "position_rotations", "reference_valid", "encoder_age_ms")
                    if key in motor
                }
                for motor in state.get("motors", [])
                if isinstance(motor, dict)
            ]
        rows.append(
            {
                "service": name,
                "phase": entry.get("phase"),
                "age_s": entry.get("age_s"),
                "instance_present": isinstance(state.get("instance"), str)
                and bool(state["instance"]),
                "observation": observation,
            }
        )
    hub = getattr(service, "agent_stream", None)
    stream = hub.discovery() if hub else None
    if hub:
        with hub.lock:
            stream.update(
                subscribers=len(hub.subscribers),
                state_seq=hub.seq,
                retained_events=len(hub.events),
                event_bytes=hub.event_bytes,
            )
    authority = getattr(service, "agent_authority", None)
    return {
        "mode": "connection-and-control" if authority else "read-only",
        "source": "shared device cache",
        "normal_commands": bool(
            getattr(getattr(service, "agent_control", None), "commands_enabled", False)
        ),
        "phases": [
            {"phase": 0, "status": "local mock portal", "available": True},
            {"phase": 1, "status": "service observations", "available": True},
            {"phase": 2, "status": "sample evidence", "available": True},
            {"phase": 3, "status": "read-only stream", "available": hub is not None},
            {"phase": 4, "status": "connection and control", "available": authority is not None},
            {"phase": 5, "status": "simulated commands", "available": authority is not None},
            {
                "phase": 6,
                "status": "simulated Stop and recovery",
                "available": authority is not None,
            },
            {"phase": 7, "status": "live data and release identity", "available": hub is not None},
            {
                "phase": 8,
                "status": "live commands after operator grant",
                "available": authority is not None,
            },
            {
                "phase": 9,
                "status": "connection-loss warning only",
                "available": authority is not None,
            },
        ],
        "services": rows,
        "stream": stream,
    }


def register(blueprint, service):
    @blueprint.get("/status")
    def observations():
        return jsonify(status(service))


WORKSPACE = Workspace(
    id="agent-connect",
    order=90,
    title={"en": "Agent Connect", "zh": "智能体连接"},
    register=register,
)
