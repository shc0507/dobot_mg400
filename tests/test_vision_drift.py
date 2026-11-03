"""Independent synthetic camera/box proofs, without cameras or services."""

import copy
import json
import math
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
from PIL import Image
from test_deploy_mg400ctl import ctl as ctl
from test_orchestrator_runner import FakeService, until
from test_orchestrator_transfer import Robot
from test_orchestrator_transfer import geometry as geometry

from labkit.errors import ServiceError
from orchestrator.recipe import validate
from orchestrator.runner import Context, Runner, _Authority
from orchestrator.skills import Registry
from orchestrator.skills.vision import DRIFT_SETTING, check_calibration
from orchestrator.store import RunStore
from orchestrator.zones import ZonePlan, Zones
from vision.calibration import VALIDATION_INDICES, drift_inputs, drift_margin, drift_proof
from vision.service import VisionService, create_app

DEVICE = "/dev/v4l/by-path/platform-offline-drift-video-index0"
FLANGE = [0, 0, 146, 0]
TOOL = [0, 0, 146]


def model():
    training = [index for index in range(24) if index not in VALIDATION_INDICES]
    return {
        "schema": 1,
        "camera_device": DEVICE,
        "image_size": [320, 240],
        "camera_matrix": [[300, 0, 160], [0, 280, 120], [0, 0, 1]],
        "distortion": [0, 0, 0, 0, 0],
        "rotation": [0, 0, 0],
        "translation": [0, 0, 500],
        "tray_height_mm": 0,
        "distance_mm": 246,
        "nominal_tag_height_mm": 100,
        "nominal_distance_mm": 246,
        "rms_limit_mm": 1,
        "created_at": 1234,
        "rms_mm": {"held_out": 0.1, "tray": 0.2},
        "errors_mm": {"held_out": [0.1] * 6, "tray": [0.2] * 3},
        "points": {
            "tag_flange_mm": [[0, 0, 0]] * 24,
            "tag_pixels": [[160, 120]] * 24,
            "tray_tip_xy_flange_z_mm": [[0, 0, 0]] * 3,
            "tray_pixels": [[160, 120]] * 3,
            "training_indices": training,
            "validation_indices": list(VALIDATION_INDICES),
            "inlier_indices": training,
        },
    }


def arm(box=(0, 0, 320, 240), label="Robotic Arm"):
    return {"label": label, "xyxy": list(box)}


def proof(detections=None, **kwargs):
    return drift_proof(
        model(),
        FLANGE,
        TOOL,
        [arm()] if detections is None else detections,
        [320, 240],
        device=DEVICE,
        **kwargs,
    )


@pytest.mark.parametrize("label", ["Robotic Arm", "roboticarm", "ROBOTIC_ARM", "robotic_arm"])
def test_one_box_proof_is_explicit_and_json_safe(label):
    result = proof([{"label": "Chip"}, arm(label=label)])
    assert result == {
        "passed": True,
        "reason": None,
        "projected_tip_px": [160, 120],
        "arm_box_px": [0, 0, 320, 240],
        "margin_px": 0,
        "camera_device": DEVICE,
        "image_size": {"width": 320, "height": 240},
        "model_created_at": 1234,
    }
    json.dumps(result, allow_nan=False)


def test_r_rotates_nonzero_tool_xy_and_model_z_does_not_use_physical_tool_dz():
    calibration = model()
    result = drift_proof(
        calibration, [20, 30, 146, 90], [2, 3, -999], [arm()], [320, 240], device=DEVICE
    )
    assert result["passed"]
    assert result["projected_tip_px"] == pytest.approx([160 + 300 * 17 / 500, 120 + 280 * 32 / 500])
    wrapped = drift_proof(
        calibration, [20, 30, 146, 450], [2, 3, 999], [arm()], [320, 240], device=DEVICE
    )
    assert wrapped["projected_tip_px"] == pytest.approx(result["projected_tip_px"])


def test_radial_projection_matches_independent_pinhole_formula():
    calibration = model()
    calibration["distortion"] = [0.2, -0.1, 0, 0, 0]
    result = drift_proof(
        calibration, [100, 80, 146, 45], [20, 30, 146], [arm()], [320, 240], device=DEVICE
    )
    x, y = (100 - 10 / math.sqrt(2)) / 500, (80 + 50 / math.sqrt(2)) / 500
    radial = 1 + 0.2 * (x * x + y * y) - 0.1 * (x * x + y * y) ** 2
    assert result["passed"]
    assert result["projected_tip_px"] == pytest.approx(
        [160 + 300 * x * radial, 120 + 280 * y * radial]
    )


def test_nominal_height_gauge_preserves_projection():
    original = model()
    shifted = copy.deepcopy(original)
    shifted["nominal_tag_height_mm"] += 7
    shifted["tray_height_mm"] += 7
    shifted["translation"][2] -= 7
    before = drift_proof(
        original, [20, 30, 146, 90], [2, 3, 146], [arm()], [320, 240], device=DEVICE
    )
    after = drift_proof(shifted, [20, 30, 146, 90], [2, 3, 146], [arm()], [320, 240], device=DEVICE)
    assert before == after
    assert original == model()


@pytest.mark.parametrize("z,reason", [(-500, "tip_behind_camera"), (0, "tip_behind_camera")])
def test_nonpositive_camera_depth_never_passes(z, reason):
    calibration = model()
    calibration["translation"][2] = z
    result = drift_proof(calibration, FLANGE, TOOL, [arm()], [320, 240], device=DEVICE)
    assert result["passed"] is False and result["reason"] == reason
    assert result["projected_tip_px"] is None


def test_finite_offimage_projection_cannot_be_rescued_by_bad_box():
    result = drift_proof(
        model(), [1000, 0, 146, 0], TOOL, [arm([-1e6, -1e6, 1e6, 1e6])], [320, 240], device=DEVICE
    )
    assert result["passed"] is False
    assert result["reason"] == "tip_outside_image"
    assert result["projected_tip_px"][0] > 320


@pytest.mark.parametrize(
    "detections,reason",
    [
        ([], "arm_detection_missing"),
        ([{"label": "Chip"}], "arm_detection_missing"),
        ([arm(), arm([159, 119, 161, 121])], "arm_detection_ambiguous"),
        ([arm([0, 0, 100, 100]), arm([200, 140, 320, 240])], "arm_detection_ambiguous"),
        ([arm(label="robotic-arm")], "arm_detection_missing"),
        ([None], "arm_detection_malformed"),
        ([{}], "arm_detection_malformed"),
        ([{"label": True}], "arm_detection_malformed"),
        ([arm()] * 1025, "arm_detection_malformed"),
        ({"detections": [arm()]}, "arm_detection_malformed"),
    ],
)
def test_absent_malformed_and_multiple_arm_evidence_is_negative(detections, reason):
    result = proof(detections)
    assert result["passed"] is False and result["reason"] == reason
    assert result["arm_box_px"] is None


@pytest.mark.parametrize(
    "box",
    [
        [160, 100, 150, 140],
        [160, 100, 160, 140],
        [-1, 0, 320, 240],
        [0, 0, 321, 240],
        [0, 0, 320, 241],
        [0, 0, float("nan"), 240],
        [0, 0, float("inf"), 240],
        [True, 0, 320, 240],
        [0, 0, 320],
        "bad",
        None,
        [0, 0, 10**400, 240],
    ],
)
def test_arm_box_must_be_finite_ordered_and_inside_frame(box):
    result = proof([{"label": "Robotic Arm", "xyxy": box}])
    assert result["passed"] is False and result["reason"] == "arm_detection_malformed"


def test_margin_is_an_inset_and_collapsed_region_cannot_pass():
    box = [160, 100, 200, 140]
    assert proof([arm(box)], margin_px=0)["passed"]
    result = proof([arm(box)], margin_px=1)
    assert result["passed"] is False and result["reason"] == "tip_outside_arm_box"
    assert proof([arm(box)], margin_px=20)["reason"] == "margin_excludes_box"
    assert proof([arm()], margin_px=4096)["reason"] == "margin_excludes_box"
    assert proof([arm([150, 110, 170, 130])], margin_px=9.5)["passed"]


@pytest.mark.parametrize(
    "value", [True, -1, 4096.001, float("nan"), float("inf"), 10**400, "1", None]
)
def test_margin_rejects_invalid_command_values(value):
    with pytest.raises(ValueError):
        drift_margin(value)
    with pytest.raises(ValueError):
        proof(margin_px=value)


@pytest.mark.parametrize(
    "flange,tool",
    [
        ([True, 0, 146, 0], TOOL),
        ([0, 0, 146], TOOL),
        ([0, 0, 146, float("nan")], TOOL),
        ([0, 0, 146, 10**400], TOOL),
        (FLANGE, [0, False, 146]),
        (FLANGE, [0, 0, np.inf]),
        (FLANGE, [0, 0, 1_000_001]),
    ],
)
def test_command_vectors_reject_mixed_bool_nonfinite_or_unbounded_values(flange, tool):
    with pytest.raises(ValueError):
        drift_inputs(flange, tool)
    with pytest.raises(ValueError):
        drift_proof(model(), flange, tool, [arm()], [320, 240], device=DEVICE)


def test_wrong_port_size_or_model_is_a_configuration_error():
    with pytest.raises(ValueError, match="port"):
        drift_proof(model(), FLANGE, TOOL, [arm()], [320, 240], device=DEVICE + "-other")
    with pytest.raises(ValueError, match="image size"):
        drift_proof(model(), FLANGE, TOOL, [arm()], [640, 480], device=DEVICE)
    calibration = model()
    calibration["translation"][0] = float("nan")
    with pytest.raises(ValueError):
        drift_proof(calibration, FLANGE, TOOL, [arm()], [320, 240], device=DEVICE)


@pytest.mark.parametrize("device", [None, "", "/dev/video0", "/dev/v4l/by-path/..", True])
def test_drift_requires_explicit_stable_caller_identity(device):
    with pytest.raises(ValueError, match="stable"):
        drift_proof(model(), FLANGE, TOOL, [arm()], [320, 240], device=device)


@pytest.fixture
def drift_service(tmp_path):
    image = Image.new("RGB", (320, 240), "white")
    frames = SimpleNamespace(
        camera_count=1,
        configuration=SimpleNamespace(devices=(DEVICE,)),
        read=Mock(return_value=image),
    )

    def predict(current):
        return current, {
            "image_size": {"width": current.width, "height": current.height},
            "coordinate_system": "oriented_original_image_pixels",
            "detections": [arm()],
        }

    predictor = SimpleNamespace(predict=Mock(side_effect=predict))
    service = VisionService(
        demo=True,
        frames=frames,
        localizer_factory=lambda: predictor,
        calibration_directory=tmp_path,
    )
    service.calibrations.save(DEVICE, model())
    service.start()
    predictor.predict.reset_mock()
    service.calibrations.load = Mock(wraps=service.calibrations.load)
    service.calibrations.save = Mock(
        side_effect=AssertionError("Drift must not rewrite calibration")
    )
    client = create_app(service, token="d" * 24).test_client()
    headers = {"Authorization": "Bearer " + "d" * 24, "X-MG400-Client": "drift-test"}
    yield SimpleNamespace(
        service=service, frames=frames, predictor=predictor, client=client, headers=headers
    )
    service.close(wait_s=1)


def drift_command(**changes):
    return {
        "action": "check_calibration",
        "camera": 1,
        "flange_mm": FLANGE,
        "tool_offset_mm": TOOL,
        **changes,
    }


def submit(rig, **changes):
    assert rig.client.post("/api/heartbeat", headers=rig.headers, json={}).status_code == 200
    response = rig.client.post("/api/command", headers=rig.headers, json=drift_command(**changes))
    assert response.status_code == 202, response.json
    return response.json["command_id"]


def receipt(rig, identity):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = rig.service.snapshot()
        command = state["last_command"]
        if command is not None and command["id"] == identity:
            return command
        threading.Event().wait(0.001)
    pytest.fail("Drift command did not drain")


def test_service_checks_one_owned_frame_model_and_prediction_without_tray_mapping(
    drift_service, monkeypatch
):
    rig = drift_service
    monkeypatch.setattr("vision.service.robot_xy", Mock(side_effect=AssertionError("No tray map")))
    result = receipt(rig, submit(rig))
    assert result["result"] == "ok" and result["output"]["passed"] is True
    assert result["output"]["camera"] == 1 and result["output"]["calibrated"] is True
    assert result["output"]["camera_device"] == DEVICE
    rig.frames.read.assert_called_once_with(1)
    rig.service.calibrations.load.assert_called_once_with(DEVICE)
    rig.predictor.predict.assert_called_once_with(rig.frames.read.return_value)
    rig.service.calibrations.save.assert_not_called()


@pytest.mark.parametrize("change", ["missing-box", "ambiguous", "wrong-size", "wrong-coordinate"])
def test_negative_visual_proof_is_explicit_success_not_positive_evidence(drift_service, change):
    rig = drift_service
    original = rig.predictor.predict.side_effect

    def predict(image):
        current, data = original(image)
        if change == "missing-box":
            data["detections"] = []
        elif change == "ambiguous":
            data["detections"] *= 2
        elif change == "wrong-size":
            data["image_size"]["width"] += 1
        else:
            data["coordinate_system"] = "model_input_pixels"
        return current, data

    rig.predictor.predict.side_effect = predict
    result = receipt(rig, submit(rig))
    assert result["result"] == "ok"
    assert result["output"]["passed"] is False and result["output"]["reason"]
    rig.service.calibrations.save.assert_not_called()


@pytest.mark.parametrize("failure", ["missing-model", "wrong-size"])
def test_missing_or_mismatched_model_fails_before_inference(drift_service, failure):
    rig = drift_service
    if failure == "missing-model":
        rig.service.calibrations.load.side_effect = lambda _: None
    else:
        rig.frames.read.return_value = Image.new("RGB", (640, 480))
    result = receipt(rig, submit(rig))
    assert result["result"] == "failed" and result["output"] is None
    assert "recalibrate" in result["message"]
    rig.predictor.predict.assert_not_called()


@pytest.mark.parametrize(
    "bad",
    [
        dict(margin_px=True),
        dict(margin_px=-1),
        dict(margin_px="1"),
        dict(flange_mm=[0, 0, 1]),
        dict(tool_offset_mm=[0, False, 0]),
        dict(camera=True),
        dict(extra="not-allowed"),
    ],
)
def test_invalid_drift_command_never_reads_a_frame(drift_service, bad):
    rig = drift_service
    rig.client.post("/api/heartbeat", headers=rig.headers, json={})
    response = rig.client.post("/api/command", headers=rig.headers, json=drift_command(**bad))
    assert response.status_code == 400
    rig.frames.read.assert_not_called()


@pytest.mark.parametrize("device", [None, "", "/dev/video0"])
def test_invalid_configured_identity_refuses_before_frame_acquisition(drift_service, device):
    rig = drift_service
    rig.service._devices = (device,)
    rig.client.post("/api/heartbeat", headers=rig.headers, json={})
    response = rig.client.post("/api/command", headers=rig.headers, json=drift_command())
    assert response.status_code == 400
    rig.frames.read.assert_not_called()


def test_drift_keeps_bearer_lease_and_declared_parameter_guards(drift_service):
    rig = drift_service
    assert rig.client.post("/api/command", json=drift_command()).status_code in (400, 401)
    assert (
        rig.client.post("/api/command", headers=rig.headers, json=drift_command()).status_code
        == 409
    )
    params = rig.service.describe()["commands"]["check_calibration"]["params"]
    assert set(params["required"]) == {"camera", "flange_mm", "tool_offset_mm"}
    assert params["properties"]["margin_px"]["default"] == 0
    rig.frames.read.assert_not_called()


@pytest.mark.parametrize("cancel", ["stop", "release-reacquire", "expiry-reacquire", "close"])
@pytest.mark.parametrize("positive", [True, False])
def test_interrupted_inference_never_publishes_or_revives_a_drift_proof(
    drift_service, cancel, positive
):
    rig = drift_service
    entered, finish = threading.Event(), threading.Event()
    original = rig.predictor.predict.side_effect

    def predict(image):
        entered.set()
        assert finish.wait(2)
        image, data = original(image)
        if not positive:
            data["detections"] = []
        return image, data

    rig.predictor.predict.side_effect = predict
    identity = submit(rig)
    try:
        assert entered.wait(2)
        if cancel == "stop":
            assert rig.service.stop("another-authorized-client") == "pending"
        elif cancel == "close":
            assert rig.service.close(wait_s=0) == "pending"
        else:
            if cancel == "release-reacquire":
                rig.service.lease.release("drift-test")
            else:
                rig.service.lease.clock = lambda: time.monotonic() + 10
                rig.service.lease.poll()
            rig.service.lease.acquire("drift-test")
        assert rig.service.snapshot()["busy"]
        with pytest.raises(ServiceError):
            rig.service.command(
                "check_calibration",
                {k: v for k, v in drift_command().items() if k != "action"},
                "drift-test",
            )
    finally:
        finish.set()
    result = receipt(rig, identity)
    assert result["result"] == "stopped" and result["output"] is None
    rig.frames.read.assert_called_once_with(1)
    rig.predictor.predict.assert_called_once()
    rig.service.calibrations.save.assert_not_called()


@pytest.mark.parametrize("positive", [True, False])
def test_publication_checks_expiry_even_when_background_poll_has_not_run(
    drift_service, monkeypatch, positive
):
    rig = drift_service
    # Stop only the test instance's poll loop, leaving service admission unchanged.
    rig.service._quit.set()
    rig.service._poller.join(timeout=1)
    assert not rig.service._poller.is_alive()
    original = rig.service._process

    def processed(job):
        result, image = original(job)
        if not positive:
            result.update(passed=False, reason="arm_detection_missing", arm_box_px=None)
        rig.service.lease.clock = lambda: time.monotonic() + 10
        assert not job["cancel"].is_set()  # The publication fence must discover expiry itself.
        return result, image

    monkeypatch.setattr(rig.service, "_process", processed)
    result = receipt(rig, submit(rig))
    assert result["result"] == "stopped" and result["output"] is None
    assert rig.service.expiry_count == 1


class DriftVision:
    def __init__(self):
        self.calls = []
        self.after = None
        self.output = {**proof(), "camera": 1, "calibrated": True}
        self.value = dict(
            connected=True,
            busy=False,
            locked=False,
            fault=None,
            lease=dict(holder="owned-run", remaining_s=2),
            last_command=None,
        )

    def state(self):
        return copy.deepcopy(self.value)

    def owned(self, state):
        return state.get("lease", {}).get("holder") == "owned-run"

    def command(self, action, **params):
        self.calls.append((action, params))
        self.value["last_command"] = dict(
            id="drift-1",
            action=action,
            client="owned-run",
            result="ok",
            output=copy.deepcopy(self.output),
        )
        if self.after:
            self.after()
        return {"command_id": "drift-1"}


@pytest.fixture
def drift_rig(geometry, monkeypatch, tmp_path):
    monkeypatch.delenv(DRIFT_SETTING, raising=False)
    robot, vision = Robot(), DriftVision()
    plan = ZonePlan({}, ("robot", "vision"), tmp_path / "occupancy.json", "d" * 64)
    zones = Zones(plan, dict(robot=robot, vision=vision), holder="owned-run", audit=lambda *a: True)
    authority = _Authority(time.monotonic() + 3)
    storage = SimpleNamespace(event=Mock())
    ctx = Context(
        authority, dict(robot=robot, vision=vision), storage, "owned-run", 1, "chip", 0, zones
    )
    return SimpleNamespace(ctx=ctx, robot=robot, vision=vision, authority=authority, zones=zones)


def test_guarded_skill_checks_without_robot_commands_or_zone_clearance(drift_rig):
    rig = drift_rig
    result = check_calibration(rig.ctx, 1)
    assert result == rig.vision.output and result["passed"] is True
    assert rig.vision.calls == [
        (
            "check_calibration",
            dict(camera=1, flange_mm=[200, 0, 50, 0], tool_offset_mm=[3, 4, 5], margin_px=0),
        )
    ]
    assert rig.robot.reads >= 3 and rig.robot.calls == []
    assert rig.zones.snapshot() == {}
    assert rig.zones.error is None


@pytest.mark.parametrize(
    "changes",
    [
        {"passed": False, "reason": "tip_outside_arm_box"},
        {"passed": 1},
        {"calibrated": 1},
        {"camera": True},
        {"camera": 2},
        {"margin_px": 1},
        {"margin_px": float("nan")},
        {"reason": "unexpected"},
        {"model_created_at": 0},
        {"model_created_at": 10**400},
        {"camera_device": None},
        {"camera_device": "/dev/video0"},
        {"image_size": {"width": True, "height": 240}},
        {"image_size": {"width": 320, "height": 240, "extra": 1}},
        {"projected_tip_px": [320, 120]},
        {"projected_tip_px": [float("nan"), 120]},
        {"arm_box_px": [170, 100, 200, 140]},
        {"arm_box_px": [-1, 0, 320, 240]},
        {"arm_box_px": [0, 0, 10**400, 240]},
        {"unknown": "extra-field"},
    ],
)
def test_negative_or_malformed_proof_raises_drift_failure_without_motion(drift_rig, changes):
    rig = drift_rig
    rig.vision.output.update(changes)
    with pytest.raises(RuntimeError, match="^camera moved, recalibrate$"):
        check_calibration(rig.ctx, 1)
    assert len(rig.vision.calls) == 1 and rig.robot.calls == []


@pytest.mark.parametrize("field", ["camera_device", "arm_box_px", "model_created_at"])
def test_missing_proof_evidence_is_not_success(drift_rig, field):
    del drift_rig.vision.output[field]
    with pytest.raises(RuntimeError, match="^camera moved, recalibrate$"):
        check_calibration(drift_rig.ctx, 1)


@pytest.mark.parametrize("mutation", ["x", "r", "owner", "instance", "stale", "disabled"])
def test_robot_guard_preserves_motion_or_identity_failure_before_proof(drift_rig, mutation):
    rig = drift_rig
    rig.vision.output.update(passed=False, reason="arm_detection_missing")

    def change():
        if mutation in {"x", "r"}:
            rig.robot.value["actual"][0 if mutation == "x" else 3] += 1
        elif mutation == "owner":
            rig.robot.value["controller_id"] = "foreign-client"
        elif mutation == "instance":
            rig.robot.value["instance"] = "restarted"
        else:
            rig.robot.value["feedback_fresh" if mutation == "stale" else "enabled"] = False

    rig.vision.after = change
    with pytest.raises(RuntimeError) as caught:
        check_calibration(rig.ctx, 1)
    assert str(caught.value) != "camera moved, recalibrate"
    assert len(rig.vision.calls) == 1 and rig.robot.calls == []


def test_prior_cancellation_cause_is_not_replaced_with_drift(drift_rig):
    rig = drift_rig
    cause = RuntimeError("operator Stop won first")
    rig.vision.after = lambda: rig.authority.revoke(cause)
    rig.vision.output.update(passed=False, reason="arm_detection_missing")
    with pytest.raises(RuntimeError, match="operator Stop won first") as caught:
        check_calibration(rig.ctx, 1)
    assert str(caught.value) == str(cause)
    assert rig.authority.revocation_error is cause and rig.robot.calls == []


@pytest.mark.parametrize(
    "field,value",
    [("id", "other"), ("action", "detect"), ("client", "foreign"), ("result", "failed")],
)
def test_skill_requires_exact_action_client_and_success_receipt(drift_rig, field, value):
    rig = drift_rig
    rig.vision.after = lambda: rig.vision.value["last_command"].update({field: value})
    with pytest.raises(RuntimeError, match="Vision"):
        check_calibration(rig.ctx, 1)
    assert len(rig.vision.calls) == 1 and rig.robot.calls == []


@pytest.mark.parametrize("value", ["", "nan", "inf", "-1", "4097", "not-a-number"])
def test_bad_margin_setting_fails_before_service_or_robot_reads(drift_rig, monkeypatch, value):
    monkeypatch.setenv(DRIFT_SETTING, value)
    with pytest.raises(ValueError, match=DRIFT_SETTING):
        check_calibration(drift_rig.ctx, 1)
    assert drift_rig.robot.reads == 0 and drift_rig.vision.calls == []


def test_supplied_margin_is_forwarded_and_must_match_the_receipt(drift_rig, monkeypatch):
    monkeypatch.setenv(DRIFT_SETTING, "2.5")
    drift_rig.vision.output["margin_px"] = 2.5
    assert check_calibration(drift_rig.ctx, 1)["margin_px"] == 2.5
    assert drift_rig.vision.calls[0][1]["margin_px"] == 2.5


def test_registry_exposes_only_the_typed_camera_scalar():
    from orchestrator.skills import default_registry

    skill = default_registry()["vision.check_calibration"]
    assert skill.services == ("robot", "vision")
    assert skill.validate({"camera": 1}) == {"camera": 1}
    for params in ({}, {"camera": True}, {"camera": 1, "margin_px": 1}):
        with pytest.raises(ValueError):
            skill.validate(params)


@pytest.mark.parametrize("present", [False, True])
def test_owned_demo_filters_preserve_only_supplied_margin(monkeypatch, present):
    import os

    from orchestrator.app import demo_environment
    from orchestrator.cli import _demo_environment

    poisoned = {
        "MG400_VISION_TOKEN": "private",
        "MG400_CAMERA_DEVICES": "/dev/not-owned",
        "MG400_VISION_CALIBRATION_DIR": "/host/not-owned",
        "HTTP_PROXY": "bad",
        "PYTHONPATH": "/host/python",
        "MG400_CAMERA_CALIBRATION_REFERENCES": "/host/ref",
    }
    for key, value in poisoned.items():
        monkeypatch.setenv(key, value)
    if present:
        monkeypatch.setenv(DRIFT_SETTING, "2.5")
    else:
        monkeypatch.delenv(DRIFT_SETTING, raising=False)
    original = dict(os.environ)
    filtered = demo_environment(original)
    assert filtered.get(DRIFT_SETTING) == ("2.5" if present else None)
    assert not poisoned.keys() & filtered.keys()
    with _demo_environment():
        assert os.environ.get(DRIFT_SETTING) == ("2.5" if present else None)
        assert not poisoned.keys() & os.environ.keys()
    assert dict(os.environ) == original


@pytest.mark.parametrize("root", [True, False])
@pytest.mark.parametrize("command", ["run", "rehearse", "runs", "station"])
def test_installed_margin_filter_preserves_execution_identity_and_secret_boundaries(
    ctl, monkeypatch, root, command
):
    monkeypatch.setenv(DRIFT_SETTING, "2.5")
    monkeypatch.setenv("MG400_VISION_TOKEN", "must-not-pass")
    monkeypatch.setenv("MG400_CAMERA_DEVICES", "/dev/must-not-pass")
    monkeypatch.setenv("MG400_VISION_CALIBRATION_DIR", "/host/must-not-pass")
    if root and command != "rehearse":
        ctl.host.settings.write_text(f"{DRIFT_SETTING}=3.5\nMG400_VISION_TOKEN=must-not-pass\n")
    else:
        ctl.host.settings.unlink()
    if not root:
        for name in ("getuid", "geteuid", "getgid", "getegid"):
            monkeypatch.setattr(ctl.module.os, name, lambda: 1000)
    argument = "list" if command == "station" else "synthetic-recipe"
    assert ctl.call(command, argument) == 0
    program, argv, environment = ctl.execvpe.call_args.args
    expected = "3.5" if root and command == "run" else "2.5"
    assert environment.get(DRIFT_SETTING) == (expected if command in {"run", "rehearse"} else None)
    assert (
        not {"MG400_VISION_TOKEN", "MG400_CAMERA_DEVICES", "MG400_VISION_CALIBRATION_DIR"}
        & environment.keys()
    )
    python = str(ctl.host.code / ".venv/bin/python")
    assert argv[-6:] == [python, "-I", "-m", "orchestrator", command, argument]
    assert program == ("/usr/sbin/runuser" if root else python)
    ctl.run.assert_not_called()


def runner_proof():
    return {
        "camera": 1,
        "calibrated": True,
        "passed": True,
        "reason": None,
        "projected_tip_px": [160, 120],
        "arm_box_px": [0, 0, 320, 240],
        "margin_px": 0,
        "camera_device": "/dev/v4l/by-path/platform-offline-drift-video-index0",
        "image_size": {"width": 320, "height": 240},
        "model_created_at": 1234,
    }


class RunnerDriftVision(FakeService):
    def __init__(self, result):
        super().__init__("vision")
        self.value["lease"] = {"holder": self.identity}
        self.result = result
        self.entered = threading.Event()
        self.finish = threading.Event()

    def command(self, action, **params):
        assert action == "check_calibration"
        assert params == {
            "camera": 1,
            "flange_mm": [200, 0, 50, 0],
            "tool_offset_mm": [3, 4, 5],
            "margin_px": 0,
        }
        self.events.append(action)
        self.value["busy"] = True
        self.entered.set()
        assert self.finish.wait(4), "The test must release the accepted fake command"
        self.value.update(
            busy=False,
            last_command={
                "id": "drift-command-1",
                "action": action,
                "client": self.identity,
                "result": "ok",
                "output": copy.deepcopy(self.result),
            },
        )
        return {"command_id": "drift-command-1"}


@contextmanager
def drift_runner(tmp_path, monkeypatch, result):
    monkeypatch.setenv("MG400_ORCH_DRIFT_MARGIN_PX", "0")
    robot, vision, registry = FakeService(), RunnerDriftVision(result), Registry()
    robot.value.update(
        enabled=True,
        robot_mode=5,
        gripper_available=True,
        gripper_busy=False,
        keyboard_enabled=False,
        actual=[200, 0, 50, 0, 1000],
        gripper={"outcome": None},
    )

    def occupy(ctx):
        ctx.check()

    def later_move(ctx):
        ctx.clients["robot"].command("later-move")

    registry.register("occupy", occupy, services=("robot",))
    registry.register("vision.check_calibration", check_calibration, services=("robot", "vision"))
    registry.register("later.move", later_move, services=("robot",))
    recipe = validate(
        {
            "name": "Drift before further movement",
            "version": 1,
            "params": {},
            "steps": [
                {"skill": "occupy", "params": {}, "timeout_s": 5, "zone": "tray"},
                {
                    "skill": "vision.check_calibration",
                    "params": {"camera": 1},
                    "timeout_s": 5,
                },
                {"skill": "later.move", "params": {}, "timeout_s": 5},
            ],
        },
        registry,
    )
    directory = tmp_path / "zones"
    directory.mkdir(mode=0o750)
    plan = ZonePlan(
        {"tray": {"owner": "robot", "conditions": ()}},
        ("robot", "vision"),
        directory / "occupancy.json",
        "d" * 64,
    )
    with RunStore(tmp_path / "runs") as store:
        runner = Runner(
            recipe, registry, {"robot": robot, "vision": vision}, store, zone_plan=plan
        ).start()
        try:
            assert vision.entered.wait(3), runner.snapshot()
            claim = runner.snapshot()["zones"]["tray"]
            assert claim["status"] == "occupied"
            assert claim["holder"] == runner.run_id
            assert store.get(runner.run_id)["steps"][0]["status"] == "completed"
            yield runner, robot, vision, store, claim
        finally:
            runner.abort()
            vision.finish.set()
            assert runner.wait_closed(5), runner.snapshot()


def assert_drift_held(runner, robot, vision, store, claim, reason):
    until(lambda: runner.snapshot()["settled"])
    state = runner.snapshot()
    assert state["status"] == "held"
    assert state["detail"] == reason
    assert "later-move" not in robot.events
    assert vision.events.count("check_calibration") == 1
    for service in (robot, vision):
        assert "stop" in service.events
        assert service.events.count("acquire") == 1
        assert "release" not in service.events
    retained = state["zones"]["tray"]
    assert retained["status"] == "unknown"
    for field in ("holder", "device", "generation", "config"):
        assert retained[field] == claim[field]
    steps = store.get(runner.run_id)["steps"]
    assert steps[0]["status"] == "completed"
    assert steps[1]["status"] == "failed"
    assert steps[2]["status"] == "pending"


@pytest.mark.parametrize("kind", ["negative", "malformed", "malformed-envelope"])
def test_drift_proof_holds_before_next_move_and_retains_prior_zone(
    geometry, tmp_path, monkeypatch, kind
):
    result = runner_proof()
    reason = "camera moved, recalibrate"
    if kind == "negative":
        result.update(passed=False, reason="tip_outside_arm_box")
    elif kind == "malformed":
        del result["model_created_at"]
    else:
        result = None
        reason = "Vision calibration failed: "
    with drift_runner(tmp_path, monkeypatch, result) as (runner, robot, vision, store, claim):
        vision.finish.set()
        assert_drift_held(runner, robot, vision, store, claim, reason)


def test_drift_valid_proof_can_reach_the_later_fake_move(geometry, tmp_path, monkeypatch):
    with drift_runner(tmp_path, monkeypatch, runner_proof()) as (runner, robot, vision, store, _):
        vision.finish.set()
        assert runner.wait(3) == "completed"
        assert robot.events.count("later-move") == 1
        assert vision.events.count("check_calibration") == 1
        assert [step["status"] for step in store.get(runner.run_id)["steps"]] == [
            "completed",
            "completed",
            "completed",
        ]


def test_drift_late_bad_proof_preserves_earlier_operator_hold(geometry, tmp_path, monkeypatch):
    with drift_runner(tmp_path, monkeypatch, {}) as (runner, robot, vision, store, claim):
        reason = "Operator Stop preceded the drift result"
        assert runner.hold(reason)
        until(lambda: runner.status == "held" and all("stop" in s.events for s in (robot, vision)))
        assert not runner.snapshot()["settled"]
        assert all("release" not in s.events for s in (robot, vision))
        vision.finish.set()
        assert_drift_held(runner, robot, vision, store, claim, reason)


def test_drift_late_bad_proof_preserves_first_instance_revocation(geometry, tmp_path, monkeypatch):
    with drift_runner(tmp_path, monkeypatch, {}) as (runner, robot, vision, store, claim):
        robot.value["instance"] = "replacement-robot-instance"
        until(lambda: runner.status == "held" and all("stop" in s.events for s in (robot, vision)))
        reason = runner.snapshot()["detail"]
        assert reason in {
            "A zone service instance changed.",
            "Robot ownership or instance changed during transfer.",
        }
        assert not runner.snapshot()["settled"]
        assert all("release" not in s.events for s in (robot, vision))
        vision.finish.set()
        assert_drift_held(runner, robot, vision, store, claim, reason)


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("interrupt", [False, True])
def test_failed_worker_bootstrap_cannot_admit_inference_or_hide_an_active_job(
    drift_service, monkeypatch, late, interrupt
):
    rig = drift_service
    original = threading.Thread.start
    workers = []

    def failed_start(worker):
        if worker.name != "vision-inference":
            return original(worker)
        if late:
            original(worker)
            workers.append(worker)
        if interrupt:
            raise KeyboardInterrupt("injected startup interruption")
        raise RuntimeError("injected startup failure")

    rig.service.lease.acquire("drift-test")
    args = {key: value for key, value in drift_command().items() if key != "action"}
    monkeypatch.setattr(threading.Thread, "start", failed_start)
    try:
        with pytest.raises(KeyboardInterrupt if interrupt else ServiceError):
            rig.service.command("check_calibration", args, "drift-test")
    finally:
        for worker in workers:
            worker.join(timeout=1)
            assert not worker.is_alive()
    rig.frames.read.assert_not_called()
    rig.predictor.predict.assert_not_called()
    state = rig.service.snapshot()
    assert not state["busy"] and state["last_command"]["result"] == "failed"
    assert state["last_command"]["output"] is None
    assert rig.service.stop("other-authorized-client") == "confirmed"


@pytest.mark.parametrize("cancel", ["stop", "close"])
def test_stop_during_successful_worker_bootstrap_drains_without_frame_access(
    drift_service, monkeypatch, cancel
):
    rig = drift_service
    original = threading.Thread.start
    workers = []

    def stopped_start(worker):
        original(worker)
        if worker.name == "vision-inference":
            workers.append(worker)
            outcome = rig.service.stop("other") if cancel == "stop" else rig.service.close(0)
            assert outcome == "pending"
            assert rig.service.snapshot()["busy"]

    monkeypatch.setattr(threading.Thread, "start", stopped_start)
    identity = submit(rig)
    result = receipt(rig, identity)
    for worker in workers:
        worker.join(timeout=1)
        assert not worker.is_alive()
    assert result["result"] == "stopped" and result["output"] is None
    rig.frames.read.assert_not_called()
    rig.predictor.predict.assert_not_called()


def test_denied_bootstrap_cannot_clobber_a_later_successful_job(drift_service, monkeypatch):
    rig = drift_service
    original = threading.Thread.start
    denied = []

    def failed_start(worker):
        if worker.name == "vision-inference":
            denied.append(worker)
            raise RuntimeError("start denied before bootstrap")
        return original(worker)

    rig.client.post("/api/heartbeat", headers=rig.headers, json={})
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", failed_start)
        response = rig.client.post("/api/command", headers=rig.headers, json=drift_command())
        assert response.status_code == 503
    successful = receipt(rig, submit(rig))
    assert successful["result"] == "ok" and successful["output"]["passed"]
    assert len(denied) == 1
    original(denied[0])
    denied[0].join(timeout=1)
    assert not denied[0].is_alive()
    assert rig.service.snapshot()["last_command"] == successful
    rig.frames.read.assert_called_once_with(1)
    rig.predictor.predict.assert_called_once()
