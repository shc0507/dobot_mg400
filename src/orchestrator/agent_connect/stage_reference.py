"""Derive the same stage reference for choices, state frames, and command checks."""

import hashlib
import json


def stage_reference(state, *, allow_legacy=False):
    revision, epoch = state.get("reference_revision"), state.get("stop_epoch")
    legacy = allow_legacy and revision is None
    if (
        (not legacy and (type(revision) is not int or revision < 0))
        or type(epoch) is not int
        or epoch < 0
    ):
        raise ValueError("The stage has no current reference revision or stop epoch.")
    limits = {
        "port": state.get("port"),
        "microstep": state.get("microstep"),
        "motors": [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "lower_limit_rotations",
                    "upper_limit_rotations",
                    "tolerance",
                    "calibration",
                )
            }
            for item in state.get("motors", [])
        ],
    }
    payload = json.dumps(limits, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "reference_revision": revision,
        "reference_source": "limits_port_calibration" if legacy else "reference_revision",
        "stop_epoch": epoch,
        "limits_digest": hashlib.sha256(payload.encode()).hexdigest(),
    }
