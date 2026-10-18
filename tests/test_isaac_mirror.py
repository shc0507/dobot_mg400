"""Exercise the live mirror's actual USD poses and passive reconnect worker."""

import math
import runpy
import threading
import time
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MODULE = runpy.run_path(str(ROOT / "examples/isaac_sim_mirror_mg400.py"), run_name="test_mirror")
UsdPoseMirror = MODULE["UsdPoseMirror"]
LiveMirror = MODULE["LiveMirror"]


@pytest.mark.parametrize("scene", ("scene.usda", "grasp_scene.usda"))
@pytest.mark.parametrize("joints", ((0, 0, 0, 0), (20, 15, 35, 25), (-90, 45, 60, -30)))
def test_authored_live_pose_matches_independent_cad_fk_without_editing_source(scene, joints):
    stage = Usd.Stage.Open(
        Sdf.Layer.FindOrOpen(str(ROOT / "isaacsim" / scene)), Sdf.Layer.CreateAnonymous()
    )
    before = {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetUsedLayers()
        if layer != stage.GetSessionLayer()
    }
    edit_target = stage.GetEditTarget()
    mirror = UsdPoseMirror(stage)
    mirror.update(joints)
    mirror.update(joints)  # Rerun uses the existing transform op, never appends a second one.
    j1, j2, j3, j4 = map(math.radians, joints)
    radial = (
        0.1095
        - 0.0010512570171516 * math.cos(j2)
        + 0.175001164344716 * math.sin(j2)
        + 0.175 * math.cos(j3)
        + 0.00325 * math.sin(j3)
    )
    lateral = -0.0000001243781102
    height = (
        0.175
        + 0.0010512570171516 * math.sin(j2)
        + 0.175001164344716 * math.cos(j2)
        - 0.175 * math.sin(j3)
        + 0.00325 * math.cos(j3)
    )
    expected = (
        -0.005 + radial * math.cos(j1) - lateral * math.sin(j1),
        radial * math.sin(j1) + lateral * math.cos(j1),
        height,
    )
    flange = stage.GetPrimAtPath("/World/MG400/mg400_end_effector_flange")
    pose = UsdGeom.XformCache().GetLocalToWorldTransform(flange)
    assert pose.ExtractTranslation() == pytest.approx(expected, abs=3e-8)
    rotation = Gf.Matrix3d().SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), math.degrees(j1 + j4)))
    assert np.array(pose.ExtractRotationMatrix()) == pytest.approx(np.array(rotation), abs=3e-7)
    assert stage.GetEditTarget() == edit_target
    assert before == {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetUsedLayers()
        if layer != stage.GetSessionLayer()
    }
    assert len(UsdGeom.Xformable(flange).GetOrderedXformOps()) == 1


def test_live_arm_changes_hold_measured_jaw_opening():
    stage = Usd.Stage.Open(
        Sdf.Layer.FindOrOpen(str(ROOT / "isaacsim/grasp_scene.usda")), Sdf.Layer.CreateAnonymous()
    )
    mirror = UsdPoseMirror(stage, {"pgea_left_slide": 0.001, "pgea_right_slide": 0.003})
    for joints in ((0, 0, 0, 0), (60, 30, 40, 20), (-30, 10, 60, -50)):
        mirror.update(joints)
        cache = UsdGeom.XformCache()
        housing = cache.GetLocalToWorldTransform(stage.GetPrimAtPath("/World/MG400/pgea_housing"))
        centers = []
        for side in ("left", "right"):
            fingertip = stage.GetPrimAtPath(f"/World/MG400/pgea_{side}_jaw/pgea_fingertip_{side}")
            world = cache.GetLocalToWorldTransform(fingertip)
            centers.append(np.array(world.ExtractTranslation()))
        y_axis = np.array(housing.ExtractRotationMatrix())[1]
        gap = np.dot(centers[1] - centers[0], y_axis) - 0.009
        assert gap == pytest.approx(0.0119 - 0.001 - 0.003, abs=3e-8)
    restarted = UsdPoseMirror(stage)
    assert restarted.jaws == pytest.approx(mirror.jaws, abs=3e-8)


def test_worker_reconnects_on_invalid_frame_and_receives_without_sending(monkeypatch):
    mirror = LiveMirror()
    received = threading.Event()
    attempts = []
    feedback = object()

    class Connection:
        def __init__(self, host, timeout_s):
            attempts.append((host, timeout_s))

        def __enter__(self):
            if len(attempts) == 1:
                raise ValueError("Bad feedback magic")
            return self

        def __exit__(self, *args):
            pass

        def read_latest(self):
            mirror.stop_event.set()
            received.set()
            return feedback

    monkeypatch.setitem(LiveMirror.receive.__globals__, "FeedbackConnection", Connection)
    began = time.monotonic()
    worker = threading.Thread(target=mirror.receive)
    worker.start()
    assert received.wait(3)
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert attempts == [("192.168.1.6", 1.0)] * 2
    assert mirror.received == 1
    assert mirror.latest[0] is feedback
    assert began <= mirror.latest[1] <= time.monotonic()
    assert mirror.network_error is None
