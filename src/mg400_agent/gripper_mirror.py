"""Display measured gripper positions in USD without physics or robot commands."""

import math
from numbers import Real

from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics

ROOT = Sdf.Path("/World/MG400")
TRAVEL_M = 0.005


def _frame(joint, index):
    matrix = Gf.Matrix4d().SetRotate(
        Gf.Rotation(joint.GetAttribute(f"physics:localRot{index}").Get())
    )
    matrix.SetTranslateOnly(Gf.Vec3d(joint.GetAttribute(f"physics:localPos{index}").Get()))
    return matrix


class JawMirror:
    """Update only the two jaw bodies. Position 1000 is open. Position 0 is closed."""

    def __init__(self, stage):
        if stage is None or not math.isclose(UsdGeom.GetStageMetersPerUnit(stage), 1):
            raise ValueError("The gripper stage must use metres.")
        self.stage = stage
        self.housing = stage.GetPrimAtPath(ROOT.AppendChild("pgea_housing"))
        if not self.housing or not self.housing.IsActive() or not self.housing.IsA(UsdGeom.Xform):
            raise ValueError("The MG400 gripper housing is absent.")
        self.jaws = []
        for side, direction in (("left", 1), ("right", -1)):
            path = ROOT.AppendChild(f"pgea_{side}_jaw")
            jaw = stage.GetPrimAtPath(path)
            joint = UsdPhysics.PrismaticJoint(
                stage.GetPrimAtPath(ROOT.AppendPath(f"joints/pgea_{side}_slide"))
            )
            fingertip = UsdGeom.Cube(
                stage.GetPrimAtPath(path.AppendChild(f"pgea_fingertip_{side}"))
            )
            if (
                not jaw
                or not jaw.IsActive()
                or not jaw.IsA(UsdGeom.Xform)
                or not joint
                or not joint.GetPrim().IsActive()
                or not fingertip
                or not fingertip.GetPrim().IsActive()
                or joint.GetBody0Rel().GetTargets() != [self.housing.GetPath()]
                or joint.GetBody1Rel().GetTargets() != [path]
                or joint.GetAxisAttr().Get() != "Y"
                or joint.GetLowerLimitAttr().Get() != 0
                or not math.isclose(joint.GetUpperLimitAttr().Get(), TRAVEL_M, abs_tol=1e-9)
            ):
                raise ValueError(f"The {side} jaw structure does not match the MG400 model.")
            local0, local1 = _frame(joint.GetPrim(), 0), _frame(joint.GetPrim(), 1)
            expected = Gf.Matrix4d(1)
            if direction == -1:
                expected.SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), 180))
            if any(
                abs(frame[row][column] - expected[row][column]) > 1e-6
                for frame in (local0, local1)
                for row in range(4)
                for column in range(4)
            ):
                raise ValueError(f"The {side} jaw frames do not match the MG400 model.")
            self.jaws.append((jaw, local0, local1.GetInverse(), fingertip, direction))

    def apply(self, position_0_1000):
        """Apply a measured position. Return each jaw's displacement and the measured model gap."""
        if (
            isinstance(position_0_1000, bool)
            or not isinstance(position_0_1000, Real)
            or not math.isfinite(position_0_1000)
            or not 0 <= position_0_1000 <= 1000
        ):
            raise ValueError(
                "The measured gripper position must be a finite number from 0 to 1000."
            )
        q = TRAVEL_M * (1 - position_0_1000 / 1000)
        housing_pose = UsdGeom.Xformable(self.housing).GetLocalTransformation()
        motion = Gf.Matrix4d().SetTranslate(Gf.Vec3d(0, q, 0))
        with Usd.EditContext(self.stage, self.stage.GetSessionLayer()):
            for jaw, local0, inverse1, _, _ in self.jaws:
                xform = UsdGeom.Xformable(jaw)
                attribute = jaw.GetAttribute("xformOp:transform:mg400Mirror")
                op = (
                    UsdGeom.XformOp(attribute)
                    if attribute
                    else xform.AddTransformOp(opSuffix="mg400Mirror")
                )
                op.Set(inverse1 * motion * local0 * housing_pose)
                xform.SetXformOpOrder([op])
        cache = UsdGeom.XformCache()
        inverse_housing = cache.GetLocalToWorldTransform(self.housing).GetInverse()
        inner_faces = []
        for _, _, _, fingertip, direction in self.jaws:
            point = Gf.Vec3d(0, direction * fingertip.GetSizeAttr().Get() / 2, 0)
            world = cache.GetLocalToWorldTransform(fingertip.GetPrim()).Transform(point)
            inner_faces.append(inverse_housing.Transform(world)[1])
        return {"q_m": q, "gap_m": inner_faces[1] - inner_faces[0]}
