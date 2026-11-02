"""One authority-bound recording and small chip-linked JSON manifests.

The microscope owns the camera and every media file. This skill only sends
contract commands and stores metadata; it never reads a service-supplied path.
"""

import json
import math
import uuid

MAX_MANIFEST = 64 * 1024
MAX_SEGMENTS = 128


def _text(value, label, maximum, *, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value.strip())
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"Invalid bounded {label}.")
    return value


def _number(value, label, low=0, high=None):
    try:
        valid = type(value) in {int, float} and math.isfinite(value) and value >= low
        valid = valid and (high is None or value <= high)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"Invalid finite {label}.")
    return value


def _manifest(value):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True).encode("utf-8")
    if len(data) > MAX_MANIFEST:
        raise ValueError("Recording manifest exceeds 64 KiB.")
    return data


def _state(ctx):
    state = ctx.clients["microscope"].state()
    if (
        not isinstance(state, dict)
        or ctx.clients["microscope"].owned(state) is not True
        or state.get("connected") is not True
        or state.get("locked") is not False
        or "fault" not in state
        or state["fault"] is not None
        or not isinstance(state.get("lease"), dict)
    ):
        raise RuntimeError("Recording requires healthy microscope control owned by this run.")
    return state


def _active(state, recording_id, status):
    value = state.get("recording")
    if (
        not isinstance(value, dict)
        or value.get("id") != recording_id
        or value.get("status") != status
        or value.get("client") != state["lease"].get("holder")
        or value.get("error") is not None
    ):
        raise RuntimeError("The expected owned recording is not active in the service state.")
    return value


def _receipt(ctx, action, recording_id, status, **params):
    response = ctx.clients["microscope"].command(action, recording_id=recording_id, **params)
    if not isinstance(response, dict):
        raise RuntimeError("The recording command returned no identifiable receipt.")
    command_id = _text(response.get("command_id"), "command ID", 128)
    while True:
        state = _state(ctx)
        receipt = state.get("last_command")
        if isinstance(receipt, dict) and receipt.get("id") == command_id:
            if (
                receipt.get("action") != action
                or receipt.get("result") != "ok"
                or receipt.get("client") != state["lease"].get("holder")
                or state.get("busy") is not False
            ):
                raise RuntimeError(
                    "The recording command did not finish with an exact success receipt."
                )
            result = state.get("last_result")
            value = result.get("recording") if isinstance(result, dict) else None
            field = "start_command_id" if action == "start_recording" else "stop_command_id"
            current = _active(state, recording_id, status)
            if (
                not isinstance(value, dict)
                or value.get("id") != recording_id
                or value.get("status") != status
                or value.get("client") != state["lease"].get("holder")
                or value.get(field) != command_id
                or current.get(field) != command_id
                or value.get("error") is not None
            ):
                raise RuntimeError("Recording finalization does not match the accepted command.")
            return command_id, value
        if state.get("busy") is not True:
            raise RuntimeError("The recording command outcome is unknown; it will not be retried.")
        ctx.sleep(0.05)


def _summary(value, *, recording_id, mode, run_name, completed):
    """Copy a fixed metadata schema, excluding paths, arbitrary keys and media."""
    if value.get("id") != recording_id or value.get("mode") != mode:
        raise ValueError("Recording identity or mode changed.")
    resolved_name = _text(value.get("run_name"), "run name", 80, empty=True)
    if run_name and resolved_name != run_name:
        raise ValueError("Recording run name changed.")
    fps = _number(value.get("output_fps"), "output FPS", 0.1, 120)
    if fps != (1 if mode == "time_lapse" else 30):
        raise ValueError("Recording frame-rate defaults changed.")
    interval = value.get("capture_interval_seconds")
    if mode == "time_lapse":
        if _number(interval, "capture interval", 0.1, 3600) != 30:
            raise ValueError("Recording interval default changed.")
    elif interval is not None:
        raise ValueError("Live recording must not have a capture interval.")
    count = value.get("frame_count")
    if type(count) is not int or not 0 <= count < 2**63:
        raise ValueError("Invalid recording frame count.")
    started = _number(value.get("started_at"), "recording start time")
    finished = value.get("finished_at")
    if completed:
        _number(finished, "recording finish time", started)
    elif finished is not None:
        raise ValueError("An active recording already has a finish time.")
    segments = value.get("segments")
    if not isinstance(segments, list) or len(segments) > MAX_SEGMENTS:
        raise ValueError("Recording segment metadata exceeds its bounded list.")
    if segments != [f"video_{number:04d}.mp4" for number in range(1, len(segments) + 1)]:
        raise ValueError("Recording segments must be consecutive known video basenames.")
    timestamps, summary = value.get("timestamps"), value.get("summary")
    if timestamps is not None and timestamps != "video.timestamps.csv":
        raise ValueError("Unexpected recording timestamps basename.")
    if completed:
        if (
            not segments
            or count <= 0
            or timestamps != "video.timestamps.csv"
            or summary != f"{recording_id}_shortened_3_min.mp4"
        ):
            raise ValueError("Completed recording lacks the expected finalized media metadata.")
    elif summary is not None:
        raise ValueError("An active recording already has a compiled summary.")
    if type(value.get("archived")) is not bool:
        raise ValueError("Invalid recording archive status.")
    return {
        "recording_id": recording_id,
        "run_name": resolved_name,
        "mode": mode,
        "capture_interval_seconds": interval,
        "output_fps": fps,
        "frame_count": count,
        "started_at": started,
        "finished_at": finished,
        "segments": segments,
        "timestamps": timestamps,
        "summary": summary,
        "archived": value["archived"],
    }


def record(ctx, chip_id: str, seconds: float, run_name: str = "", mode: str = "time_lapse"):
    """Record for a finite interval using the microscope's default capture rates."""
    _text(chip_id, "chip ID", 256)
    _text(run_name, "run name", 80, empty=True)
    _number(seconds, "recording duration")
    if seconds <= 0 or seconds >= ctx.remaining_s():
        raise ValueError("Recording duration must be positive and shorter than the step deadline.")
    if not isinstance(mode, str) or mode not in {"time_lapse", "live"}:
        raise ValueError("Recording mode must be time_lapse or live.")
    state = _state(ctx)
    existing = state.get("recording")
    sequence = state.get("recording_sequence")
    if (
        state.get("busy") is not False
        or not isinstance(existing, dict)
        or not isinstance(existing.get("status"), str)
        or existing["status"] not in {"idle", "completed", "stopped", "failed"}
        or not isinstance(sequence, dict)
        or sequence.get("armed") is not False
        or state.get("manual_client") is not None
    ):
        raise RuntimeError("A new recording requires an idle microscope and no active recorder.")
    recording_id = uuid.uuid4().hex
    intent = {
        "schema": 1,
        "service": "microscope",
        "chip_id": chip_id,
        "recording_id": recording_id,
        "status": "requested",
        "seconds": seconds,
        "run_name": run_name,
        "mode": mode,
    }
    ctx.store.artifact(f"recording-{recording_id}-intent.json", _manifest(intent))
    options = {"mode": mode}
    if run_name:
        options["run_name"] = run_name
    start_id, started = _receipt(ctx, "start_recording", recording_id, "recording", **options)
    start_metadata = _summary(
        started, recording_id=recording_id, mode=mode, run_name=run_name, completed=False
    )
    remaining = seconds
    while remaining > 0:
        _active(_state(ctx), recording_id, "recording")
        interval = min(remaining, 0.25)
        ctx.sleep(interval)
        remaining -= interval
    _active(_state(ctx), recording_id, "recording")
    stop_id, finished = _receipt(ctx, "stop_recording", recording_id, "completed")
    if finished.get("start_command_id") != start_id:
        raise RuntimeError("The completed recording does not match its start command.")
    metadata = _summary(
        finished, recording_id=recording_id, mode=mode, run_name=run_name, completed=True
    )
    if (
        metadata["started_at"] != start_metadata["started_at"]
        or metadata["run_name"] != start_metadata["run_name"]
        or metadata["frame_count"] < start_metadata["frame_count"]
        or len(metadata["segments"]) < len(start_metadata["segments"])
    ):
        raise RuntimeError("Completed metadata does not match the recording start.")
    completed = {
        **intent,
        **metadata,
        "status": "completed",
        "start_command_id": start_id,
        "stop_command_id": stop_id,
    }
    artifact_id = ctx.store.artifact(
        f"recording-{recording_id}-completed.json", _manifest(completed)
    )
    return {"recording_id": recording_id, "artifact_id": artifact_id, "status": "completed"}
