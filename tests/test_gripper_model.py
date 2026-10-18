"""Validate vendor CAD scale and rigid attachment through the arm's motion."""

from pathlib import Path

import pytest

from mg400_agent.mirror import JOINT_NAMES, joint_angles
from mg400_agent.mujoco_replay import load_model

mujoco = pytest.importorskip("mujoco")
np = pytest.importorskip("numpy")


def test_cad_dimensions_are_metres_and_photo_side_exit_cable_is_preserved():
    meshes = Path(__file__).parents[1] / "src/mg400_agent/assets/mg400/meshes"
    record = np.dtype([("normal", "<f4", (3,)), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
    parts = {}
    for part in ("body", "front", "jaw_left", "jaw_right", "cable"):
        raw = (meshes / f"pgea_{part}.stl").read_bytes()
        parts[part] = np.frombuffer(raw, dtype=record, offset=84)["vertices"].reshape(-1, 3)
    rigid = np.concatenate([parts[p] for p in ("body", "front", "jaw_left", "jaw_right")])
    # O-S drawing: 89 mm length and 18 mm thickness. Source CAD side-cover
    # screws reach Y=18.6 mm; the opposite jaw screws reach Y=-16 mm.
    assert np.ptp(rigid, axis=0) == pytest.approx((0.018, 0.0346, 0.089), abs=1e-6)
    assert rigid[:, 2].max() == pytest.approx(0, abs=1e-6)
    assert parts["cable"][:, 2].max() == pytest.approx(-0.007, abs=1e-6)
    assert parts["cable"][:, 1].max() == pytest.approx(0.050, abs=1e-6)


@pytest.mark.parametrize("scene", ["scene.xml", "dynamics.xml"])
def test_gripper_stays_attached_without_duplicate_flange_or_new_joints(scene):
    _, model, data = load_model(scene)
    assert model.njnt == 8
    assert model.body("pgea_gripper").parentid[0] == model.body("mg400_end_effector_flange").id
    assert sum(model.geom(i).name == "visual_flange_0" for i in range(model.ngeom)) == 1
    assert not any(model.geom(i).name.startswith("pgea_adapter_") for i in range(model.ngeom))
    for side in ("left", "right"):
        finger = model.geom(f"pgea_fingertip_{side}")
        assert finger.pos[2] + finger.size[2] == pytest.approx(-0.089)
        assert finger.pos[2] - finger.size[2] == pytest.approx(-0.104)
    qpos = [int(model.joint("mg400_" + name).qposadr[0]) for name in JOINT_NAMES]
    for angles in ((0, 0, 0, 0), (90, 30, 70, -45), (-80, -15, -20, 55)):
        data.qpos[qpos] = joint_angles(angles)
        mujoco.mj_forward(model, data)
        flange = data.site("mg400_flange")
        local_tip = flange.xmat.reshape(3, 3).T @ (data.site("pgea_jaw_center").xpos - flange.xpos)
        assert local_tip == pytest.approx((0, 0, -0.089), abs=1e-10)
        tip = flange.xmat.reshape(3, 3).T @ (data.site("pgea_tip").xpos - flange.xpos)
        assert tip == pytest.approx((0, 0, -0.104), abs=1e-10)
        # Cable exits toward the base (-X in the flange frame) at J4=0.
        rotation = flange.xmat.reshape(3, 3).T @ data.body("pgea_gripper").xmat.reshape(3, 3)
        assert rotation[:, 1] == pytest.approx((-1, 0, 0), abs=1e-10)
        assert rotation[:, 2] == pytest.approx((0, 0, 1), abs=1e-10)
