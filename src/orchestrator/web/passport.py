"""Read-only chip history and bounded, authenticated stored artifacts."""

import json
import math
import re

from flask import Response, jsonify, request

from labkit.errors import ServiceError

KINDS = frozenset({"runs", "steps", "samples", "artifacts", "events"})
JSON_FIELDS = {"params_json": "params", "result_json": "result", "data_json": "data"}
PRIVATE_KEYS = ("token", "password", "authorization", "secret", "credential", "path", "url")
MAX_RECORD = 256 * 1024


def _object(pairs):
    value = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("Duplicate recorded JSON key")
        value[key] = child
    return value


def _constant(value):
    raise ValueError("Nonfinite recorded JSON number")


def _query(allowed):
    if set(request.args) - allowed or any(
        len(request.args.getlist(key)) != 1 for key in request.args
    ):
        raise ValueError("Use only the documented query fields, once each.")
    chip = request.args.get("id")
    if not isinstance(chip, str) or not chip.strip():
        raise ValueError("Enter the exact nonempty chip ID.")
    return chip


def _number(name, default, maximum):
    value = request.args.get(name, str(default))
    if not re.fullmatch(r"[0-9]{1,16}", value):
        raise ValueError("Invalid page size or offset.")
    result = int(value)
    if result > maximum or name == "limit" and result == 0:
        raise ValueError("Invalid page size or offset.")
    return result


def _clean(value, dashboard, depth=0):
    if depth > 20:
        return "[Recorded nesting exceeds the display limit]"
    if isinstance(value, dict):
        return {
            _clean(key, dashboard): "[redacted]"
            if any(word in key.lower() for word in PRIVATE_KEYS)
            else _clean(child, dashboard, depth + 1)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_clean(child, dashboard, depth + 1) for child in value]
    if isinstance(value, str):
        for secret in dashboard.secrets:
            value = value.replace(secret, "[redacted]")
        return value
    if type(value) is float and not math.isfinite(value):
        return "[Invalid recorded number]"
    return value


def _record(row, dashboard):
    result = {}
    for key, value in row.items():
        if key in JSON_FIELDS:
            destination = JSON_FIELDS[key]
            if value is None:
                result[destination] = None
                continue
            try:
                if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_RECORD:
                    raise ValueError("Display limit")
                result[destination] = _clean(
                    json.loads(value, object_pairs_hook=_object, parse_constant=_constant),
                    dashboard,
                )
            except (ValueError, UnicodeError, RecursionError):
                result[destination] = {
                    "unavailable": "Recorded JSON is invalid or exceeds 256 KiB."
                }
        else:
            result[key] = _clean(value, dashboard)
    return result


def _mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def register_passport(blueprint, service):
    def store():
        current = service.store
        if current is None:
            raise ServiceError(503, "Run history is unavailable.")
        return current

    @blueprint.get("/api/chip")
    def chip_history():
        chip = _query({"id", "kind", "run_id", "limit", "offset"})
        kind = request.args.get("kind", "runs")
        run_id = request.args.get("run_id")
        if kind not in KINDS or kind == "runs" and run_id is not None:
            raise ValueError("Select a documented history category.")
        if kind != "runs" and (run_id is None or not re.fullmatch(r"[0-9a-f]{32}", run_id)):
            raise ValueError("Select a recorded run.")
        limit, offset = (
            _number("limit", 25 if kind == "runs" else 100, 100),
            _number("offset", 0, 2**53 - 1),
        )
        try:
            page = store().chip_history(chip, kind=kind, run_id=run_id, limit=limit, offset=offset)
            items = [_record(row, service.dashboard) for row in page["items"]]
        except ValueError:
            raise ServiceError(404, "No matching chip history is available.") from None
        except ServiceError:
            raise
        except Exception:
            raise ServiceError(503, "Run history is unavailable.") from None
        return jsonify(
            chip_id=chip, kind=kind, run_id=run_id, items=items, next_offset=page["next_offset"]
        )

    @blueprint.get("/api/chip/artifact/<artifact_id>")
    def chip_artifact(artifact_id):
        chip = _query({"id"})
        if not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
            raise ServiceError(404, "No matching artifact is available.")
        try:
            artifact = store().artifact_bytes(artifact_id, chip_id=chip)
        except ServiceError:
            raise
        except Exception:
            raise ServiceError(404, "Artifact is absent, unsafe, or exceeds 16 MiB.") from None
        response = Response(artifact["data"], mimetype=_mime(artifact["data"]))
        response.headers.set(
            "Content-Disposition",
            "attachment",
            filename=_clean(artifact["name"], service.dashboard),
        )
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response
