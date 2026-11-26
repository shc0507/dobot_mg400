"""Collision check of a straight MG400 move against the bench twin.

The collision world is the ``twin-collision`` asset. The bench twin project builds it
(``build.py --collision``). It holds convex hulls in mm and the joint tree that moves
them, in the frame of the robot controller (User 0, Tool 0 = flange face centre):

- ``nodes``: ``name``, ``parent``, ``rest`` (4x4), ``joint``, ``type``, ``axis``, ``coef``.
  world = parent world @ rest @ (rotation or translation along ``axis`` by the value).
  The value of a ``robot`` node is the sum of ``coef[k] * Jk``. Other nodes use the
  value of their joint: ``stage_bottom``, ``stage_top`` and ``insert`` in mm.
  When a stage joint value is unknown, each obstacle that the joint moves becomes the
  box that holds it over the full ``STAGE_TRAVEL_MM`` of every unknown joint, in the frame
  of the stage. The check never uses the model state for an unknown stage joint.
- ``shapes``: ``object``, ``entity``, ``node`` (``None`` = fixed), ``body`` (``arm`` or
  ``env``), ``verts`` and ``faces`` in the node frame.

``check`` samples the line of a ``MovL`` command. An obstacle that is nearer than the
margin at the start is a near obstacle: the move must not go closer to it. The move
must keep the margin to all other obstacles. A clearance of 0 means contact or overlap.
The depth of an overlap is not known, so a move that starts in contact with an obstacle
is not checked against that obstacle. A move that starts in contact with the box of a
stage part at an unknown position is refused.
"""

import json
import math
from pathlib import Path

import numpy as np

WORLD = Path("/var/lib/mg400/twin/collision.json")  # config/assets.toml: twin-collision
MARGIN_MM = 10.0
CLOSER_MM = 0.1  # a near obstacle: a decrease below this is not a move toward it
STEP_MM = 2.0
STEP_DEG = 1.0
ARM_JOINTS = ("J1", "J2", "J3", "J4")
STAGE_JOINTS = ("stage_bottom", "stage_top", "insert")
# Model joint travel in mm: the joint ranges of the bench twin rig. Bottom stops at -34 mm,
# where the lower plate meets the robot base column (a 25 mm gap at the measured -8.45 mm).
STAGE_TRAVEL_MM = {
    "stage_bottom": (-34.0, 40.0),
    "stage_top": (-70.0, 45.0),
    "insert": (-100.0, 150.0),
}
# Box corners in index order 4i + 2j + k (x from i, y from j, z from k) and their faces.
BOX_FACES = [[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1]]
BOX_FACES += [[2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]]
# Seeds for the joint solution when the service gives no joints: J2 and J3 ranges of the
# MG400 and the range of J3 - J2, in degrees.
J2_RANGE, J3_RANGE, ELBOW_RANGE = (-25, 85), (-25, 105), (-60, 60)


class Unavailable(Exception):
    """The check cannot give an answer for this move."""


def _rotation(axis, degrees):
    x, y, z = np.asarray(axis, float) / np.linalg.norm(axis)
    c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
    matrix = np.eye(4)
    matrix[:3, :3] = [
        [c + x * x * (1 - c), x * y * (1 - c) - z * s, x * z * (1 - c) + y * s],
        [y * x * (1 - c) + z * s, c + y * y * (1 - c), y * z * (1 - c) - x * s],
        [z * x * (1 - c) - y * s, z * y * (1 - c) + x * s, c + z * z * (1 - c)],
    ]
    return matrix


def _translation(vector):
    matrix = np.eye(4)
    matrix[:3, 3] = vector
    return matrix


class World:
    def __init__(self, path=None, stage=None):
        """stage: model joint values in mm. A missing or unknown value sweeps the joint."""
        path = Path(WORLD if path is None else path)
        try:
            import fcl
        except ImportError as error:
            raise Unavailable("The python-fcl package is not installed.") from error
        try:
            document = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise Unavailable(f"Cannot read the collision world {path}: {error}") from error
        self._fcl = fcl
        nodes = {
            node["name"]: {**node, "rest": np.array(node["rest"], float)}
            for node in document["nodes"]
        }
        self._nodes = []  # parents first
        while len(self._nodes) < len(nodes):
            done = {node["name"] for node in self._nodes}
            ready = [
                node
                for node in nodes.values()
                if node["name"] not in done and (node["parent"] is None or node["parent"] in done)
            ]
            if not ready:
                raise Unavailable(f"The collision world {path} has a broken joint tree.")
            self._nodes.extend(ready)
        stage = stage if isinstance(stage, dict) else {}
        fixed = {
            name: float(stage[name])
            for name in STAGE_JOINTS
            if type(stage.get(name)) in (int, float) and math.isfinite(stage[name])
        }
        self._shape = {}  # id of an FCL geometry -> its shape
        self._sweep_frames = {}  # joint values -> frames, while unknown joints are swept
        arm, env = [], []
        for shape in document["shapes"]:
            if shape["body"] not in {"arm", "env"}:
                continue
            if shape["body"] == "env":
                shape = self._swept(shape, fixed, nodes)
            faces = np.array([[3, *face] for face in shape["faces"]], int).ravel()
            geometry = fcl.Convex(np.array(shape["verts"], float), len(shape["faces"]), faces)
            self._shape[id(geometry)] = shape
            (arm if shape["body"] == "arm" else env).append((shape, geometry))
        self._arm = arm
        self._geometry = [geometry for _, geometry in arm + env]  # FCL results refer to them
        self.env = self._objects(env, self._frames(fixed))
        self._seeds = None

    def _swept(self, shape, fixed, nodes):
        """The shape, or a fixed box that holds it over the travel of its unknown stage joints."""
        chain, name = [], shape["node"]
        while name is not None:
            chain.append(nodes[name])
            name = nodes[name]["parent"]
        unknown = [
            node for node in chain if node["joint"] in STAGE_JOINTS and node["joint"] not in fixed
        ]
        if not unknown:
            return shape
        ends = [STAGE_TRAVEL_MM[node["joint"]] for node in unknown]
        verts = np.c_[np.array(shape["verts"], float), np.ones(len(shape["verts"]))]
        # The frame of the outermost unknown joint at 0: the travel is along its axes.
        base = self._cached_frames(fixed)[unknown[-1]["name"]]
        local = []
        for corner in np.array(np.meshgrid(*ends)).reshape(len(ends), -1).T:
            values = {
                **fixed,
                **{node["joint"]: v for node, v in zip(unknown, corner, strict=True)},
            }
            frame = np.linalg.solve(base, self._cached_frames(values)[shape["node"]])
            local.append((frame @ verts.T).T[:, :3])
        local = np.vstack(local)
        lo, hi = local.min(axis=0), local.max(axis=0)
        box = [
            [(lo, hi)[i][0], (lo, hi)[j][1], (lo, hi)[k][2], 1.0]
            for i in (0, 1)
            for j in (0, 1)
            for k in (0, 1)
        ]
        world = (base @ np.array(box).T).T[:, :3]
        return {**shape, "node": None, "verts": world.tolist(), "faces": BOX_FACES, "swept": True}

    def _cached_frames(self, values):
        key = tuple(sorted(values.items()))
        if key not in self._sweep_frames:
            self._sweep_frames[key] = self._frames(values)
        return self._sweep_frames[key]

    def _frames(self, values):
        joints = [values.get(name, 0.0) for name in ARM_JOINTS]
        frames = {}
        for node in self._nodes:
            if node["joint"] == "robot":
                value = sum(c * j for c, j in zip(node["coef"], joints, strict=True))
            else:
                value = values.get(node["joint"], 0.0)
            if node["type"] == "revolute":
                move = _rotation(node["axis"], value)
            else:
                move = _translation(np.asarray(node["axis"], float) * value)
            parent = frames[node["parent"]] if node["parent"] else np.eye(4)
            frames[node["name"]] = parent @ node["rest"] @ move
        return frames

    def _objects(self, shapes, frames):
        objects = []
        for shape, geometry in shapes:
            frame = frames[shape["node"]] if shape["node"] else np.eye(4)
            transform = self._fcl.Transform(frame[:3, :3], frame[:3, 3])
            objects.append((shape, self._fcl.CollisionObject(geometry, transform)))
        return objects

    def manager(self, objects):
        manager = self._fcl.DynamicAABBTreeCollisionManager()
        manager.registerObjects([item for _, item in objects])
        manager.setup()
        return manager

    def arm(self, joints):
        """The arm hulls at these joint angles, as a collision manager."""
        return self.manager(
            self._objects(self._arm, self._frames(dict(zip(ARM_JOINTS, joints, strict=True))))
        )

    def clearance(self, arm, obstacles):
        """Return (distance in mm, arm shape, obstacle shape). Distance 0: contact or overlap."""
        data = self._fcl.DistanceData(self._fcl.DistanceRequest())
        arm.distance(obstacles, data, self._fcl.defaultDistanceCallback)
        first, second = self._shape[id(data.result.o1)], self._shape[id(data.result.o2)]
        if first["body"] != "arm":
            first, second = second, first
        return max(float(data.result.min_distance), 0.0), first, second

    def flange(self, joints):
        """Flange face centre and yaw in degrees, in the controller frame."""
        frame = self._frames(dict(zip(ARM_JOINTS, joints, strict=True)))["flange"]
        return frame[:3, 3], math.degrees(math.atan2(frame[1, 0], frame[0, 0]))

    def _plane(self, j2, j3):
        """Reach and height of the flange. They do not change with J1 and J4."""
        position, _ = self.flange((0.0, j2, j3, 0.0))
        return np.array([math.hypot(position[0], position[1]), position[2]])

    def solve(self, pose, seed=None):
        """Joint angles for a flange pose [x, y, z, r]. seed: joint angles near the answer."""
        x, y, z, r = pose
        goal = np.array([math.hypot(x, y), z])
        if seed is None:
            if self._seeds is None:
                self._seeds = [
                    ((j2, j3), self._plane(j2, j3))
                    for j2 in range(J2_RANGE[0], J2_RANGE[1] + 1, 5)
                    for j3 in range(J3_RANGE[0], J3_RANGE[1] + 1, 5)
                    if ELBOW_RANGE[0] <= j3 - j2 <= ELBOW_RANGE[1]
                ]
            angles = min(self._seeds, key=lambda item: np.linalg.norm(item[1] - goal))[0]
        else:
            angles = seed[1:3]
        angles = np.array(angles, float)
        for _ in range(30):
            point = self._plane(*angles)
            error = goal - point
            if np.linalg.norm(error) < 1e-4:
                break
            columns = [(self._plane(*(angles + step)) - point) / 1e-3 for step in np.eye(2) * 1e-3]
            try:
                angles = angles + np.clip(np.linalg.solve(np.column_stack(columns), error), -10, 10)
            except np.linalg.LinAlgError:
                break
        if not np.linalg.norm(goal - self._plane(*angles)) < 0.01:
            raise Unavailable(f"The arm has no joint solution at ({x:.1f}, {y:.1f}, {z:.1f}).")
        position, yaw = self.flange((0.0, angles[0], angles[1], 0.0))
        j1 = math.degrees(math.atan2(y, x) - math.atan2(position[1], position[0]))
        j1 = (j1 + 180.0) % 360.0 - 180.0
        return (j1, float(angles[0]), float(angles[1]), (r - j1 - yaw + 180.0) % 360.0 - 180.0)

    def line(self, start, target, joints=None):
        """Sample a straight flange move. Return [(distance along the line in mm, pose, joints)]."""
        start, target = np.array(start, float), np.array(target, float)
        if not (np.all(np.isfinite(start)) and np.all(np.isfinite(target))):
            raise Unavailable("The start pose or the target pose is not finite.")
        length = float(np.linalg.norm(target[:3] - start[:3]))
        count = max(1, math.ceil(max(length / STEP_MM, abs(target[3] - start[3]) / STEP_DEG)))
        samples = []
        for index in range(count + 1):
            pose = start + (target - start) * index / count
            joints = self.solve(pose, joints)
            samples.append((length * index / count, pose, joints))
        return samples


def _problem(clearance, arm, obstacle, along, pose, start_clearance=None):
    return {
        "obstacle": obstacle["object"],
        "group": obstacle["entity"],
        "arm_part": arm["object"],
        "clearance_mm": round(clearance, 1),
        "along_mm": round(along, 1),
        "pose": [round(float(value), 1) for value in pose],
        "start_clearance_mm": None if start_clearance is None else round(start_clearance, 1),
        "swept": bool(obstacle.get("swept")),
    }


def check(start, target, *, joints=None, stage=None, path=None, margin=MARGIN_MM):
    """Check the straight move from the flange pose ``start`` to ``target`` ([x, y, z, r]).

    joints: the joint angles at the start, when they are known. stage: model joint values.
    Return None when the move is clear. Otherwise return the first contact, or the smallest
    clearance when there is no contact: ``obstacle``, ``group``, ``arm_part``,
    ``clearance_mm``, ``along_mm``, ``pose``, ``start_clearance_mm`` (a number for a near
    obstacle) and ``swept`` (the obstacle is a stage part at an unknown position).
    Raise Unavailable when the check cannot run.
    """
    world = World(path, stage)
    if not (isinstance(joints, (list, tuple)) and len(joints) == 4):
        joints = None
    elif not all(type(value) in (int, float) and math.isfinite(value) for value in joints):
        joints = None
    samples = world.line(start, target, joints)
    # Obstacles that are inside the margin at the start, each with its start clearance.
    arm, near, far = world.arm(samples[0][2]), {}, world.env
    while far:
        clearance, part, obstacle = world.clearance(arm, world.manager(far))
        if clearance >= margin:
            break
        if clearance == 0 and obstacle.get("swept"):
            # The box is larger than the part, so contact at the start is not known to be real.
            return _problem(0.0, part, obstacle, 0.0, samples[0][1], 0.0)
        name = obstacle["object"]
        near[name] = (clearance, world.manager([item for item in far if item[0]["object"] == name]))
        far = [item for item in far if item[0]["object"] != name]
    far = world.manager(far) if far else None
    worst = None
    for along, pose, angles in samples[1:]:
        arm = world.arm(angles)
        found = []
        if far is not None:
            clearance, part, obstacle = world.clearance(arm, far)
            if clearance < margin:
                found.append(_problem(clearance, part, obstacle, along, pose))
        for start_clearance, manager in near.values():
            clearance, part, obstacle = world.clearance(arm, manager)
            if clearance < start_clearance - CLOSER_MM:
                found.append(_problem(clearance, part, obstacle, along, pose, start_clearance))
        for problem in found:
            if worst is None or problem["clearance_mm"] < worst["clearance_mm"]:
                worst = problem
        if worst is not None and worst["clearance_mm"] == 0:
            break
    return worst
