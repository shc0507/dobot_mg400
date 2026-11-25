"""Bind stage decisions to Console-provided reference and selected-profile values."""

import copy
import re

from labkit.errors import ServiceError

from .actions import SPECS

CHANGED = (
    "The stage calibration or selected profile changed. "
    "No command was sent. Send the instruction again to use the current values."
)
UNAVAILABLE = (
    "The Console did not provide matching stage decision context. "
    "Refresh the connection and check that the Console and portal are updated. No command was sent."
)


def protected_action(action):
    spec = SPECS.get(action, {})
    return spec.get("device") == "stage" and spec.get("route") in {"transfer", "profile"}


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _reference(value):
    if not isinstance(value, dict) or set(value) != {
        "reference_revision",
        "reference_source",
        "stop_epoch",
        "limits_digest",
    }:
        return False
    revision, source = value["reference_revision"], value["reference_source"]
    if source == "reference_revision":
        if type(revision) is not int or revision < 0:
            return False
    elif source != "limits_port_calibration" or revision is not None:
        return False
    return (
        type(value["stop_epoch"]) is int
        and value["stop_epoch"] >= 0
        and _hash(value["limits_digest"])
    )


def _preconditions(choices):
    context = choices.get("stage_context") if isinstance(choices, dict) else None
    if (
        not isinstance(context, dict)
        or type(context.get("version")) is not int
        or context["version"] != 1
        or context.get("available") is not True
    ):
        raise ServiceError(409, UNAVAILABLE)
    values = context.get("preconditions")
    if not isinstance(values, dict) or set(values) != {
        "stage_reference",
        "profile_id",
        "profile_digest",
    }:
        raise ServiceError(409, UNAVAILABLE)
    profile = values["profile_id"]
    if (
        not _reference(values["stage_reference"])
        or not _hash(values["profile_digest"])
        or (profile is not None and (not isinstance(profile, str) or not 1 <= len(profile) <= 64))
    ):
        raise ServiceError(409, UNAVAILABLE)
    return copy.deepcopy(values)


def capture(state, choices):
    """Keep missing context local to protected stage actions, not other requests."""
    try:
        values = _preconditions(choices)
        reference = (
            state.get("devices", {}).get("stage", {}).get("values", {}).get("stage_reference")
        )
        if not _reference(reference):
            raise ServiceError(409, UNAVAILABLE)
        if reference != values["stage_reference"]:
            raise ServiceError(409, CHANGED)
        return values, ""
    except ServiceError as error:
        return None, error.message


def command_preconditions(action, saved, error, choices):
    if not protected_action(action):
        return {}
    if saved is None:
        raise ServiceError(409, error or UNAVAILABLE)
    if _preconditions(choices) != saved:
        raise ServiceError(409, CHANGED)
    return copy.deepcopy(saved)
