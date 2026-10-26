"""Offline MG400 joint playback or dynamics in MuJoCo; no robot connection."""

import argparse
import json
import math
import os
import sys
import sysconfig
import time
from contextlib import nullcontext
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from .config import finite_number
from .kinematics import JOINT_NAMES, joint_angles


@dataclass(frozen=True)
class Keyframe:
    time_s: float
    joints_deg: tuple[float, float, float, float]


def load_trajectory(path: str | Path) -> list[Keyframe]:
    """Read timestamped controller joint angles; validate before opening a viewer."""
    with Path(path).open(encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError("Trajectory must contain at least two keyframes")
    frames = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"time_s", "joints_deg"}:
            raise ValueError("Each keyframe must have time_s and joints_deg only")
        timestamp = finite_number(item["time_s"])
        angles = item["joints_deg"]
        if not isinstance(angles, list) or len(angles) != 4:
            raise ValueError("joints_deg must contain four numbers in degrees")
        angles = tuple(finite_number(angle) for angle in angles)
        if (not frames and timestamp != 0) or (frames and timestamp <= frames[-1].time_s):
            raise ValueError("Trajectory times must start at zero and strictly increase")
        frames.append(Keyframe(timestamp, angles))
    return frames


def trajectory_samples(frames: list[Keyframe], rate: float):
    """Interpolate joint angles linearly, including the final keyframe exactly."""
    end = frames[-1].time_s
    segment = 0
    for index in range(math.ceil(end * rate)):
        timestamp = index / rate
        if timestamp >= end:
            break
        while segment < len(frames) - 2 and frames[segment + 1].time_s < timestamp:
            segment += 1
        start, finish = frames[segment : segment + 2]
        fraction = (timestamp - start.time_s) / (finish.time_s - start.time_s)
        angles = tuple(
            a + fraction * (b - a) for a, b in zip(start.joints_deg, finish.joints_deg, strict=True)
        )
        yield Keyframe(timestamp, angles)
    yield frames[-1]


def load_model(scene_name: str):
    try:
        import mujoco
    except ImportError as error:
        raise RuntimeError("Install MuJoCo support: uv sync --extra mujoco") from error
    scene = files("mg400_agent").joinpath("assets/mg400", scene_name)
    model = mujoco.MjModel.from_xml_path(str(scene))
    return mujoco, model, mujoco.MjData(model)


def flange_state(data) -> dict:
    flange = data.site("mg400_flange")

    def position_in(frame: str) -> list[float]:
        site = data.site(frame)
        local = site.xmat.reshape(3, 3).T @ (flange.xpos - site.xpos)
        return (local * 1000).tolist()

    rotation = flange.xmat.reshape(3, 3)
    return {
        "flange_in_base_mm": position_in("mg400_base"),
        "flange_in_urdf_origin_mm": position_in("mg400_controller_origin"),
        "flange_yaw_deg": math.degrees(math.atan2(rotation[1, 0], rotation[0, 0])),
        "tool_offset_mm": [0, 0, 0],
    }


class KinematicModel:
    """Write the eight coupled model angles and evaluate FK without stepping physics."""

    def __init__(self):
        self.mujoco, self.model, self.data = load_model("scene.xml")
        self._qpos = [int(self.model.joint("mg400_" + name).qposadr[0]) for name in JOINT_NAMES]
        self.set_joints((0, 0, 0, 0))

    def set_joints(self, joints_deg: tuple[float, float, float, float]) -> None:
        angles = tuple(finite_number(angle) for angle in joints_deg)
        coupled = joint_angles(angles)
        self.data.qpos[self._qpos] = coupled
        self.data.qvel[:] = 0
        self.mujoco.mj_forward(self.model, self.data)
        self.joints_deg = angles

    def state(self) -> dict:
        return {"joints_deg": self.joints_deg, **flange_state(self.data)}


def wait_for_viewer(viewer, deadline: float) -> bool:
    remaining = deadline - time.monotonic()
    while viewer.is_running() and remaining > 0:
        time.sleep(min(0.02, remaining))
        remaining = deadline - time.monotonic()
    return viewer.is_running()


def replay(model: KinematicModel, frames: list[Keyframe], rate: float, viewer=None) -> dict:
    start = time.monotonic()
    count = 0
    completed = True
    for frame in trajectory_samples(frames, rate):
        if viewer is not None:
            if not wait_for_viewer(viewer, start + frame.time_s):
                completed = False
                break
        with viewer.lock() if viewer is not None else nullcontext():
            model.set_joints(frame.joints_deg)
            model.data.time = frame.time_s
        if viewer is not None:
            viewer.sync()
        count += 1
    return {
        "mode": "offline_kinematic_playback",
        "completed": completed,
        "samples": count,
        "trajectory_time_s": float(model.data.time),
        **model.state(),
    }


def positive_rate(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Rate must be positive and finite")
    return number


def launch_macos_viewer(argv: list[str], module: str = "mg400_agent.mujoco_replay") -> None:
    """Use MuJoCo's Cocoa launcher, including uv Python's actual library directory."""
    launcher = Path(sysconfig.get_path("scripts")) / "mjpython"
    if not launcher.is_file():
        raise RuntimeError("Install MuJoCo support: uv sync --extra mujoco")
    environment = os.environ.copy()
    library_dir = sysconfig.get_config_var("LIBDIR")
    if library_dir:
        fallback = environment.get("DYLD_FALLBACK_LIBRARY_PATH", "/usr/local/lib:/usr/lib")
        environment["DYLD_FALLBACK_LIBRARY_PATH"] = os.pathsep.join(
            path for path in (library_dir, fallback) if path
        )
    os.execve(
        sys.executable,
        [sys.executable, str(launcher), "-m", module, *argv],
        environment,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trajectory", type=Path, required=True, help="JSON keyframe file")
    parser.add_argument("--viewer", action="store_true", help="Display playback in real time")
    parser.add_argument(
        "--rate",
        type=positive_rate,
        default=30,
        help="Playback samples/s or dynamics viewer updates/s",
    )
    parser.add_argument(
        "--dynamics", action="store_true", help="Track joints using physics/actuators"
    )
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    try:
        frames = load_trajectory(args.trajectory)
        if args.dynamics:
            from .mujoco_dynamics import DynamicModel, run_dynamics, validate_trajectory

            validate_trajectory(frames)
        if args.viewer and sys.platform == "darwin" and not os.environ.get("MJPYTHON_BIN"):
            launch_macos_viewer(argv)
        if args.dynamics:
            model = DynamicModel()
            model.reset(frames[0].joints_deg)
            run = run_dynamics
        else:
            model = KinematicModel()
            model.set_joints(frames[0].joints_deg)
            run = replay
        if args.viewer:
            import mujoco.viewer

            with mujoco.viewer.launch_passive(model.model, model.data) as viewer:
                with viewer.lock():
                    viewer.cam.type = mujoco.mjtCamera.mjCAMERA_FIXED
                    viewer.cam.fixedcamid = model.model.camera("overview").id
                result = run(model, frames, args.rate, viewer)
        else:
            result = run(model, frames, args.rate)
        print(json.dumps(result, indent=2))
        return 0 if result["completed"] else 130
    except KeyboardInterrupt:
        print("MuJoCo playback interrupted.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"MuJoCo playback failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
