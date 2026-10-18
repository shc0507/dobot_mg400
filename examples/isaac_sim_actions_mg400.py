"""Buttons that move the MG400 in the USD scene already open in Isaac Sim.

Open 00_OPEN_MG400.usda, then run these two lines in Window > Script Editor:
    import runpy
    runpy.run_path('/root/Documents/mg400-isaac/examples/isaac_sim_actions_mg400.py')

Loading this script only shows the panel. Click a button to start physics and move.
The scene owns geometry, lights, physics settings and named poses; this script
only reads that scene and sends joint drive targets. It never loads another USD,
teleports the arm or object, saves a scene, or communicates with a real robot.
Stop resets the USD scene; Pause keeps the current pose. A new stage or Stop
invalidates the attachment; the next button attaches again. Rerunning is safe.
"""

import asyncio
import builtins
import sys
import time
from pathlib import Path

import numpy as np
import omni.kit.app
import omni.timeline
import omni.ui as ui
import omni.usd
import warp as wp
from isaacsim.core.simulation_manager import SimulationManager

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
from mg400_agent.joint_limits import validate_joints  # noqa: E402
from mg400_agent.kinematics import JOINT_NAMES, joint_angles  # noqa: E402

ROBOT = "/World/MG400"
ARM_NAMES = [f"mg400_{name}" for name in JOINT_NAMES]
SLIDES = [f"pgea_{side}_slide" for side in ("left", "right")]


class MG400Actions:
    def __init__(self):
        self.app = omni.kit.app.get_app()
        self.context = omni.usd.get_context()
        self.timeline = omni.timeline.get_timeline_interface()
        self.robot = self.stage = self.task = None
        self.closed = False
        self.play_requested = False
        self.window = ui.Window("MG400 Actions - current scene", width=360, height=220)
        with self.window.frame:
            with ui.VStack(spacing=8):
                ui.Label("USD: scene / Python: joint drive targets")
                self.status = ui.Label("Ready. Choose an action.", word_wrap=True)
                for choices in (
                    (("Open jaws", "open"), ("Close jaws", "close")),
                    (("Pickup pose", "pickup"), ("Lift pose", "lift")),
                    (("Grab / lift / release", "demo"), ("Pause", "pause")),
                ):
                    with ui.HStack(spacing=8):
                        for label, action in choices:
                            ui.Button(label, clicked_fn=lambda action=action: self.start(action))
        timeline_events = self.timeline.get_timeline_event_stream()
        self.stop_subscription = timeline_events.create_subscription_to_pop(self.on_timeline_event)
        self.stage_subscription = self.context.get_stage_event_stream().create_subscription_to_pop(
            self.on_stage_event
        )

    def invalidate(self):
        self.pause()
        self.robot = self.stage = None
        self.status.text = "Scene changed or stopped. Choose an action to attach again."

    def on_timeline_event(self, event):
        if event.type == int(omni.timeline.TimelineEventType.STOP):
            self.invalidate()

    def on_stage_event(self, event):
        stage_events = omni.usd.StageEventType
        if event.type in (int(stage_events.OPENING), int(stage_events.CLOSING)):
            self.invalidate()

    def pause(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.context.get_stage() == self.stage and self.timeline.is_playing():
            self.timeline.pause()
        self.status.text = "Paused."

    def close(self):
        self.closed = True
        self.pause()
        self.stop_subscription = self.stage_subscription = None
        self.window.destroy()

    def start(self, action):
        if action == "pause":
            self.pause()
        elif self.task is None or self.task.done():
            self.task = asyncio.ensure_future(self.run(action))

    def pose(self, name):
        prim = self.stage.GetPrimAtPath(ROBOT)
        attribute = f"mg400:{name}JointsDeg"
        value = prim.GetAttribute(attribute).Get() if prim else None
        if value is None:
            scene = self.stage.GetRootLayer().identifier
            raise RuntimeError(
                f"Missing {attribute} on {ROBOT}. Current scene: {scene}. "
                "Open /root/Documents/mg400-isaac/00_OPEN_MG400.usda for preset arm motions. "
                "Jaw controls do not need pose presets."
            )
        return joint_angles(validate_joints(tuple(value)))

    async def attach(self, required_poses=()):
        stage = self.context.get_stage()
        if stage != self.stage:
            self.robot = None
        self.stage = stage
        if self.stage is None:
            raise RuntimeError("Open 00_OPEN_MG400.usda first.")
        for name in required_poses:
            self.pose(name)
        if SimulationManager.get_active_physics_engine() != "physx":
            raise RuntimeError("Select the PhysX physics engine before using these controls.")
        self.play_requested = True
        self.timeline.play(looping=False)
        await self.app.next_update_async()
        if self.context.get_stage() != self.stage or not self.timeline.is_playing():
            raise RuntimeError("Scene or timeline changed while attaching. Choose an action again.")
        if self.robot is not None:
            return
        simulation = SimulationManager.get_physics_simulation_view()
        if simulation is None:
            raise RuntimeError("Physics is not ready. Check the Isaac Sim console and try again.")
        robot = simulation.create_articulation_view(ROBOT)
        if robot.count != 1:
            raise RuntimeError("The current scene must contain one MG400 articulation.")
        meta = robot.get_metatype(0)
        self.names = list(meta.dof_names)
        if set(self.names) != set(ARM_NAMES + SLIDES) or not meta.fixed_base:
            raise RuntimeError("Expected a fixed-base MG400 with eight arm and two jaw joints.")
        self.device = robot.get_dof_positions().device
        self.indices = wp.array([0], dtype=wp.int32, device=self.device)
        self.dt = SimulationManager.get_physics_scenes()[0].get_dt()
        self.robot = robot

    async def move(self, label, duration, pose=None, close=None):
        self.status.text = label
        start = self.robot.get_dof_position_targets().numpy().copy()
        target = start.copy()
        if pose is not None:
            target[0, [self.names.index(name) for name in ARM_NAMES]] = self.pose(pose)
        if close is not None:
            target[0, [self.names.index(name) for name in SLIDES]] = close
        if not np.isfinite(target).all():
            raise RuntimeError("The current joint targets are not finite. Stop and try again.")
        began = SimulationManager.get_num_physics_steps()
        deadline = time.monotonic() + 60
        final_sent = False
        while True:
            if self.context.get_stage() != self.stage or not self.timeline.is_playing():
                raise RuntimeError("Scene or timeline changed; action cancelled.")
            if final_sent:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Physics timed out. Stop and try again.")
            elapsed = (SimulationManager.get_num_physics_steps() - began) * self.dt
            fraction = min(1.0, elapsed / duration)
            blend = fraction * fraction * (3 - 2 * fraction)
            targets = start + blend * (target - start)
            self.robot.set_dof_position_targets(
                wp.array(targets, dtype=wp.float32, device=self.device), self.indices
            )
            final_sent = fraction == 1.0
            await self.app.next_update_async()

    async def run(self, action):
        self.play_requested = False
        try:
            required_poses = (
                ("pickup", "lift")
                if action == "demo"
                else ((action,) if action in ("pickup", "lift") else ())
            )
            await self.attach(required_poses)
            if action in ("open", "close"):
                await self.move(action, 1.5, close=0.0 if action == "open" else 0.005)
            elif action in ("pickup", "lift"):
                await self.move(action, 3.0, pose=action)
            elif action == "demo":
                await self.move("Open", 1.0, close=0.0)
                await self.move("Approach", 3.0, pose="pickup")
                await self.move("Grip", 1.5, close=0.005)
                await self.move("Lift", 3.0, pose="lift")
                await self.move("Hold", 1.0)
                await self.move("Release", 1.0, close=0.0)
                await self.move("Settle", 2.0)
            self.status.text = "Done; physics paused. Choose another action."
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self.robot = None
            self.status.text = str(error)
            print(f"MG400 Actions: {error}")
        finally:
            if (
                not self.closed
                and self.play_requested
                and self.stage is not None
                and self.context.get_stage() == self.stage
                and self.timeline.is_playing()
            ):
                self.timeline.pause()


def show_controls():
    previous = getattr(builtins, "_mg400_actions_controller", None)
    if previous is not None:
        previous.close()
    controller = MG400Actions()
    builtins._mg400_actions_controller = controller
    return controller


if __name__ in ("__main__", "<run_path>"):
    MG400_ACTIONS = show_controls()
