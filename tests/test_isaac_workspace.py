"""Check the saved workbench and current-stage controller without Isaac or hardware."""

import asyncio
import builtins
import math
import runpy
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pxr")
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics, UsdShade  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ROBOT = "/World/MG400"
ARM = ["mg400_" + name for name in ("j1", "j2_1", "j2_2", "j3_1", "j3_2", "j4_1", "j4_2", "j5")]
SLIDES = ["pgea_left_slide", "pgea_right_slide"]


@pytest.fixture
def stage():
    return Usd.Stage.Open(
        Sdf.Layer.FindOrOpen(str(ROOT / "00_OPEN_MG400.usda")), Sdf.Layer.CreateAnonymous()
    )


def cad_flange(joints):
    j1, j2, j3, _ = map(math.radians, joints)
    radius = (
        0.1095 - 0.0010512570171516 * math.cos(j2) + 0.175001164344716 * math.sin(j2)
        + 0.175 * math.cos(j3) + 0.00325 * math.sin(j3)
    )
    lateral = -0.0000001243781102
    return np.array([
        -0.005 + radius * math.cos(j1) - lateral * math.sin(j1),
        radius * math.sin(j1) + lateral * math.cos(j1),
        0.175 + 0.0010512570171516 * math.sin(j2) + 0.175001164344716 * math.cos(j2)
        - 0.175 * math.sin(j3) + 0.00325 * math.cos(j3),
    ])


def test_main_entry_opens_complete_workbench_without_python(stage):
    assert stage.GetSessionLayer().empty
    assert stage.GetDefaultPrim().GetPath() == Sdf.Path("/World")
    assert UsdGeom.GetStageMetersPerUnit(stage) == 1
    assert UsdGeom.GetStageUpAxis(stage) == "Z"
    assert stage.GetPrimAtPath(ROBOT).HasAPI(UsdPhysics.ArticulationRootAPI)
    assert sum(p.IsA(UsdPhysics.RevoluteJoint) for p in stage.Traverse()) == 8
    assert sum(p.IsA(UsdPhysics.PrismaticJoint) for p in stage.Traverse()) == 2
    assert sum(p.HasAPI(UsdPhysics.RigidBodyAPI) for p in stage.Traverse()) == 15
    assert stage.GetPrimAtPath("/World/Ground").HasAPI(UsdPhysics.CollisionAPI)
    for path, kind in (("DomeLight", UsdLux.DomeLight), ("KeyLight", UsdLux.DistantLight)):
        light = stage.GetPrimAtPath("/World/" + path)
        assert light.IsA(kind)
        assert light.GetAttribute("inputs:intensity").Get() > 0
    camera_path = stage.GetRootLayer().customLayerData["cameraSettings"]["boundCamera"]
    assert stage.GetPrimAtPath(camera_path).IsA(UsdGeom.Camera)
    physics = stage.GetPrimAtPath("/World/physicsScene")
    assert physics.GetAttribute("physxScene:timeStepsPerSecond").Get() == 120
    assert physics.GetAttribute("physxScene:solverType").Get() == "TGS"
    assert physics.GetAttribute("physxScene:enableGPUDynamics").Get() is False
    assert UsdPhysics.Scene(physics).GetGravityMagnitudeAttr().Get() == pytest.approx(9.81)


def test_saved_object_is_free_and_aligned_with_pickup_support(stage):
    cube = stage.GetPrimAtPath("/World/GraspObject")
    support = stage.GetPrimAtPath("/World/GraspSupport")
    assert cube.HasAPI(UsdPhysics.RigidBodyAPI)
    assert not UsdPhysics.RigidBodyAPI(cube).GetKinematicEnabledAttr().Get()
    assert cube.GetAttribute("physxRigidBody:disableGravity").Get() in (None, False)
    assert UsdPhysics.MassAPI(cube).GetMassAttr().Get() == pytest.approx(0.005)
    assert cube.HasAPI(UsdPhysics.CollisionAPI)
    assert support.HasAPI(UsdPhysics.CollisionAPI)
    assert not support.HasAPI(UsdPhysics.RigidBodyAPI)
    assert tuple(cube.GetAttribute("xformOp:scale").Get()) == pytest.approx((0.008, 0.008, 0.02))
    pickup = stage.GetPrimAtPath(ROBOT).GetAttribute("mg400:pickupJointsDeg").Get()
    expected = cad_flange(pickup) - (0, 0, 0.096)
    center = cube.GetAttribute("xformOp:translate").Get()
    assert center == pytest.approx(expected, abs=3e-8)
    support_center = support.GetAttribute("xformOp:translate").Get()
    support_size = support.GetAttribute("xformOp:scale").Get()
    assert center[2] - 0.01 - support_center[2] - support_size[2] / 2 == pytest.approx(
        0.0005, abs=1e-8
    )
    material, _ = UsdShade.MaterialBindingAPI(cube).ComputeBoundMaterial(materialPurpose="physics")
    contact = UsdPhysics.MaterialAPI(material.GetPrim())
    assert contact.GetStaticFrictionAttr().Get() == contact.GetDynamicFrictionAttr().Get() == 1
    assert contact.GetRestitutionAttr().Get() == 0
    for prim in stage.Traverse():
        if prim.IsA(UsdPhysics.Joint):
            joint = UsdPhysics.Joint(prim)
            assert cube.GetPath() not in joint.GetBody0Rel().GetTargets()
            assert cube.GetPath() not in joint.GetBody1Rel().GetTargets()


def test_saved_pose_matches_drives_and_independent_cad_fk(stage):
    root = stage.GetPrimAtPath(ROBOT)
    ready = root.GetAttribute("mg400:readyJointsDeg").Get()
    pickup = root.GetAttribute("mg400:pickupJointsDeg").Get()
    lift = root.GetAttribute("mg400:liftJointsDeg").Get()
    assert cad_flange(ready) - cad_flange(pickup) == pytest.approx((0, 0, 0.05), abs=1e-8)
    assert cad_flange(lift) - cad_flange(pickup) == pytest.approx((0, 0, 0.08), abs=1e-8)
    cache = UsdGeom.XformCache()
    flange = cache.GetLocalToWorldTransform(
        stage.GetPrimAtPath(ROBOT + "/mg400_end_effector_flange")
    )
    assert flange.ExtractTranslation() == pytest.approx(cad_flange(ready), abs=3e-8)
    q1, q2, q3, q4 = ready
    angles = (q1, q2, q2, q3 - q2, -q2, -q3, q3, q4)
    for name, angle in zip(ARM + SLIDES, (*angles, 0, 0), strict=True):
        prim = stage.GetPrimAtPath(f"{ROBOT}/joints/{name}")
        kind = "linear" if name in SLIDES else "angular"
        drive = UsdPhysics.DriveAPI(prim, kind)
        assert drive.GetTargetPositionAttr().Get() == pytest.approx(angle, abs=2e-6)
        joint = UsdPhysics.Joint(prim)
        frames = []
        for side in (0, 1):
            body = getattr(joint, f"GetBody{side}Rel")().GetTargets()[0]
            rotation = getattr(joint, f"GetLocalRot{side}Attr")().Get()
            local = Gf.Matrix4d().SetRotate(Gf.Rotation(rotation))
            local.SetTranslateOnly(Gf.Vec3d(getattr(joint, f"GetLocalPos{side}Attr")().Get()))
            frames.append(local * cache.GetLocalToWorldTransform(stage.GetPrimAtPath(body)))
        actual_motion = frames[1] * frames[0].GetInverse()
        axis = Gf.Vec3d(0)
        axis["XYZ".index(prim.GetAttribute("physics:axis").Get())] = 1
        expected_motion = Gf.Matrix4d().SetRotate(Gf.Rotation(axis, angle))
        assert np.array(actual_motion) == pytest.approx(np.array(expected_motion), abs=3e-7)


class Events:
    def __init__(self):
        self.callback = None

    def create_subscription_to_pop(self, callback):
        self.callback = callback
        return object()

    def emit(self, kind):
        if self.callback:
            self.callback(SimpleNamespace(type=kind))


class Tensor:
    device = "cpu"

    def __init__(self, data):
        self.data = np.array(data)

    def numpy(self):
        return self.data


class Widget:
    def __init__(self, text="", **kwargs):
        self.text = text
        self.frame = self
        self.destroyed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def destroy(self):
        self.destroyed = True


@pytest.fixture
def controls(monkeypatch, stage):
    state = SimpleNamespace(
        stage=stage, playing=False, pending_play=False, steps=0, pauses=0, plays=0, views=[],
        initial_targets=np.zeros((1, 10)),
    )
    timeline_events, stage_events = Events(), Events()

    class Robot:
        count = 1

        def __init__(self):
            self.targets = state.initial_targets.copy()
            self.writes = []

        def get_metatype(self, index):
            return SimpleNamespace(dof_names=ARM + SLIDES, fixed_base=True)

        def get_dof_positions(self):
            return Tensor(np.zeros((1, 10)))

        def get_dof_position_targets(self):
            return Tensor(self.targets.copy())

        def set_dof_position_targets(self, targets, indices):
            assert state.view.is_valid
            self.targets = targets.numpy().copy()
            self.writes.append(self.targets.copy())

    def play(**kwargs):
        state.plays += 1
        state.pending_play = True
        if not getattr(state, "view", None) or not state.view.is_valid:
            robot = Robot()
            state.view = SimpleNamespace(is_valid=True, create_articulation_view=lambda path: robot)
            state.views.append(robot)

    def pause():
        state.playing = False
        state.pending_play = False
        state.pauses += 1

    def stop():
        state.playing = False
        state.pending_play = False
        state.view.is_valid = False
        timeline_events.emit(1)

    async def update():
        await asyncio.sleep(0)
        if state.pending_play:
            state.playing = True
            state.pending_play = False
        state.steps += 30

    modules = {}
    for name in ("omni", "omni.kit", "omni.kit.app", "omni.timeline", "omni.ui", "omni.usd",
                 "warp", "isaacsim", "isaacsim.core", "isaacsim.core.simulation_manager"):
        module = modules[name] = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, module)
        if "." in name:
            parent, attr = name.rsplit(".", 1)
            setattr(modules[parent], attr, module)
    modules["omni.kit.app"].get_app = lambda: SimpleNamespace(next_update_async=update)
    modules["omni.timeline"].get_timeline_interface = lambda: SimpleNamespace(
        play=play, pause=pause, is_playing=lambda: state.playing,
        get_timeline_event_stream=lambda: timeline_events,
    )
    modules["omni.timeline"].TimelineEventType = SimpleNamespace(STOP=1)
    modules["omni.usd"].get_context = lambda: SimpleNamespace(
        get_stage=lambda: state.stage, get_stage_event_stream=lambda: stage_events,
    )
    modules["omni.usd"].StageEventType = SimpleNamespace(OPENING=2, CLOSING=3)
    for name in ("Window", "VStack", "HStack", "Label", "Button"):
        setattr(modules["omni.ui"], name, Widget)
    modules["warp"].array = lambda data, **kwargs: Tensor(data)
    modules["warp"].int32 = modules["warp"].float32 = object()
    modules["isaacsim.core.simulation_manager"].SimulationManager = SimpleNamespace(
        get_active_physics_engine=lambda: "physx",
        get_physics_simulation_view=lambda: state.view,
        get_physics_scenes=lambda: [SimpleNamespace(get_dt=lambda: 1 / 120)],
        get_num_physics_steps=lambda: state.steps,
    )
    monkeypatch.setattr(builtins, "_mg400_actions_controller", None, raising=False)
    monkeypatch.setattr(sys, "path", list(sys.path))
    namespace = runpy.run_path(
        str(ROOT / "examples/isaac_sim_actions_mg400.py"), run_name="test_workspace_controls"
    )
    return SimpleNamespace(**namespace, state=state, stop=stop, stage_events=stage_events)


def test_panel_and_attachment_preserve_usd_and_do_not_send_motion(controls, stage):
    before = {layer.identifier: layer.ExportToString() for layer in stage.GetUsedLayers()}
    panel = controls.show_controls()
    assert not controls.state.playing
    assert controls.state.views == []
    asyncio.run(panel.attach())
    assert panel.stage is stage
    assert controls.state.views[0].writes == []
    assert before == {layer.identifier: layer.ExportToString() for layer in stage.GetUsedLayers()}
    panel.close()


async def start_until_first_target(panel, state):
    panel.start("close")
    for _ in range(20):
        await asyncio.sleep(0)
        if state.views and state.views[-1].writes:
            return
    pytest.fail("Action did not send its first drive target")


def test_stop_cancels_old_handles_and_next_action_reattaches(controls):
    async def exercise():
        panel = controls.show_controls()
        await start_until_first_target(panel, controls.state)
        previous = controls.state.views[0]
        writes = len(previous.writes)
        controls.stop()
        await panel.task
        assert len(previous.writes) == writes
        panel.start("open")
        await panel.task
        assert len(controls.state.views) == 2
        assert controls.state.views[1].writes
        assert controls.state.views[1].targets[0, -2:] == pytest.approx((0, 0))
        panel.close()

    asyncio.run(exercise())


def test_replacement_stage_is_not_paused_or_commanded(controls):
    async def exercise():
        panel = controls.show_controls()
        await start_until_first_target(panel, controls.state)
        previous = controls.state.views[0]
        writes = len(previous.writes)
        pauses = controls.state.pauses
        controls.state.stage = Usd.Stage.CreateInMemory()
        controls.stage_events.emit(2)
        await panel.task
        assert len(previous.writes) == writes
        assert controls.state.pauses == pauses
        assert controls.state.playing
        panel.close()

    asyncio.run(exercise())


def test_pause_resume_reuses_view_and_preserves_uncommanded_arm_targets(controls):
    async def exercise():
        panel = controls.show_controls()
        await panel.attach()
        robot = controls.state.views[0]
        robot.targets[0, :8] = np.arange(8) / 10
        await panel.run("close")
        assert robot.targets[0, -2:] == pytest.approx((0.005, 0.005))
        assert robot.targets[0, :8] == pytest.approx(np.arange(8) / 10)
        assert not controls.state.playing
        await panel.run("open")
        assert len(controls.state.views) == 1
        assert robot.targets[0, -2:] == pytest.approx((0, 0))
        assert robot.targets[0, :8] == pytest.approx(np.arange(8) / 10)
        panel.close()

    asyncio.run(exercise())


def test_rerun_cancels_old_action_without_pausing_new_controller(controls):
    async def exercise():
        old = controls.show_controls()
        await start_until_first_target(old, controls.state)
        new = controls.show_controls()
        assert old.window.destroyed
        assert builtins._mg400_actions_controller is new
        new.start("close")
        await old.task
        await new.task
        assert controls.state.views[0].targets[0, -2:] == pytest.approx((0.005, 0.005))
        assert new.status.text.startswith("Done")
        new.close()

    asyncio.run(exercise())


def block_poses(stage, names):
    """Remove composed values in this test's anonymous session, never the saved USD."""
    source_before = {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetUsedLayers() if layer != stage.GetSessionLayer()
    }
    assert stage.GetSessionLayer().anonymous
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        for name in names:
            stage.GetPrimAtPath(ROBOT).GetAttribute(f"mg400:{name}JointsDeg").Block()
    assert source_before == {
        layer.identifier: layer.ExportToString()
        for layer in stage.GetUsedLayers() if layer != stage.GetSessionLayer()
    }
    for name in names:
        assert stage.GetPrimAtPath(ROBOT).GetAttribute(f"mg400:{name}JointsDeg").Get() is None


def test_jaws_work_without_pose_presets_and_preserve_every_arm_target(controls, stage):
    block_poses(stage, ("ready", "pickup", "lift"))
    controls.state.initial_targets[0, :8] = np.arange(8) / 10
    controls.state.initial_targets[0, -2:] = 0.003

    async def exercise():
        panel = controls.show_controls()
        for action, expected in (("open", 0), ("close", 0.005)):
            await panel.run(action)
            assert panel.status.text.startswith("Done"), panel.status.text
            robot = controls.state.views[0]
            assert robot.targets[0, -2:] == pytest.approx((expected, expected))
            assert robot.writes
            for targets in robot.writes:
                assert targets[0, :8] == pytest.approx(np.arange(8) / 10)
        panel.close()

    asyncio.run(exercise())


def test_pickup_needs_only_pickup_preset(controls, stage):
    block_poses(stage, ("ready", "lift"))

    async def exercise():
        panel = controls.show_controls()
        await panel.run("pickup")
        assert panel.status.text.startswith("Done"), panel.status.text
        robot = controls.state.views[0]
        assert robot.targets[0, :8] == pytest.approx(
            np.radians((0, 25, 25, 20, -25, -45, 45, 0))
        )
        assert robot.targets[0, -2:] == pytest.approx((0, 0))
        panel.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("playing", (False, True))
@pytest.mark.parametrize("action,missing", (
    ("pickup", "pickup"), ("lift", "lift"), ("demo", "pickup"), ("demo", "lift"),
))
def test_missing_required_preset_fails_before_play_without_disturbing_timeline(
    controls, stage, action, missing, playing,
):
    block_poses(stage, (missing,))

    async def exercise():
        panel = controls.show_controls()
        controls.state.playing = playing
        await panel.run(action)
        assert controls.state.plays == 0
        assert controls.state.views == []
        assert controls.state.playing is playing
        assert controls.state.pauses == 0
        assert stage.GetRootLayer().identifier in panel.status.text
        assert f"mg400:{missing}JointsDeg" in panel.status.text
        controls.state.playing = False
        panel.close()

    asyncio.run(exercise())
