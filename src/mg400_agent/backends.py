"""Blocking hardware contracts and an in-memory simulator (no physics)."""

from dataclasses import dataclass, field
from typing import Protocol

from .config import Pose


class Arm(Protocol):
    def get_pose(self) -> Pose:
        """Return fresh measured TCP pose; raise on stale feedback or controller fault."""
        ...

    def move_linear(self, target: Pose, timeout_s: float) -> None:
        """Return only after confirmed arrival. Raise on timeout, alarm, or lost feedback."""
        ...

    def stop(self) -> None:
        """Request a controlled stop. This is not a hardware emergency stop."""
        ...


class Gripper(Protocol):
    def release(self, timeout_s: float) -> None:
        """Open the electric gripper and confirm completion; raise on fault/timeout."""
        ...

    def grasp(self, timeout_s: float) -> bool:
        """Close and return object-detected feedback, NOT just command acceptance."""
        ...


@dataclass
class SimArm:
    pose: Pose
    stopped: bool = False
    history: list[Pose] = field(default_factory=list)

    def get_pose(self) -> Pose:
        return self.pose

    def move_linear(self, target: Pose, timeout_s: float) -> None:
        if self.stopped:
            raise RuntimeError("Simulation stopped; create a new session to reset")
        self.pose = target
        self.history.append(target)

    def stop(self) -> None:
        self.stopped = True


@dataclass
class SimGripper:
    """Idealized grip feedback; a test double can simulate misses and timeouts."""

    holding: bool = False

    def release(self, timeout_s: float) -> None:
        self.holding = False

    def grasp(self, timeout_s: float) -> bool:
        self.holding = True
        return True
