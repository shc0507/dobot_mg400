"""Read MG400 feedback and display it in CoppeliaSim; never command the robot."""

import argparse
import json
import math
import sys
import time
import uuid

from .feedback import FeedbackConnection

JOINT_NAMES = ("j1", "j2_1", "j2_2", "j3_1", "j3_2", "j4_1", "j4_2", "j5")

# One remote call updates all coupled joints without rendering intermediate poses.
UPDATE_SCRIPT = """
function sysCall_init()
    sim = require('sim')
end
function applyFeedback_TOKEN(scriptUid, rootUid, jointUids, angles)
    assert(sim.getSimulationState() == sim.simulation_stopped,
           'Stop simulation before mirroring real robot feedback')
    assert(sim.getObjectUid(sim.getObject('.')) == scriptUid,
           'MG400_MIRROR_BINDING_LOST: helper replaced')
    assert(sim.getObjectFromUid(rootUid, {noError=true}) >= 0,
           'MG400_MIRROR_BINDING_LOST: model replaced')
    local joints = {}
    for i, uid in ipairs(jointUids) do
        local joint = sim.getObjectFromUid(uid, {noError=true})
        assert(joint >= 0, 'MG400_MIRROR_BINDING_LOST: joint replaced')
        assert(sim.getJointMode(joint) == sim.jointmode_kinematic,
               'The MG400 model must use kinematic joints')
        joints[i] = joint
    end
    local actual = {}
    for i, joint in ipairs(joints) do
        sim.setJointPosition(joint, angles[i])
        actual[i] = sim.getJointPosition(joint)
    end
    return actual
end
function closeFeedback_TOKEN(scriptUid)
    local sim = require('sim')
    local self = sim.getObject('.')
    if sim.getObjectUid(self) == scriptUid then
        sim.removeObjects({self}, true)
    end
end
"""


def joint_angles(joints_deg: tuple[float, float, float, float]) -> tuple[float, ...]:
    if len(joints_deg) != 4 or not all(math.isfinite(q) for q in joints_deg):
        raise ValueError("Expected four finite joint angles in degrees")
    q1, q2, q3, q4 = map(math.radians, joints_deg)
    return q1, q2, q2, q3 - q2, -q2, -q3, q3, q4


class MirrorBindingLost(RuntimeError):
    """The scene objects owned by this mirror no longer exist."""


class CoppeliaMirror:
    def __init__(self, sim, model_path: str = "/MG400"):
        self.sim = sim
        if sim.getSimulationState() != sim.simulation_stopped:
            raise RuntimeError("Stop CoppeliaSim simulation before starting the mirror")
        root = sim.getObject(model_path)
        self.root_uid = sim.getObjectUid(root)
        by_name = {}
        for joint in sim.getObjectsInTree(root, sim.sceneobject_joint, 0):
            name = sim.getObjectAlias(joint)
            if name in by_name:
                raise ValueError(f"Duplicate model joint: {name}")
            by_name[name] = joint
        missing = ["mg400_" + name for name in JOINT_NAMES if "mg400_" + name not in by_name]
        if missing:
            raise ValueError(f"Missing model joints: {', '.join(missing)}")
        self.joints = [by_name["mg400_" + name] for name in JOINT_NAMES]
        if any(sim.getJointMode(j)[0] != sim.jointmode_kinematic for j in self.joints):
            raise ValueError("The MG400 model must use kinematic joints")
        self.joint_uids = [sim.getObjectUid(joint) for joint in self.joints]
        token = uuid.uuid4().hex
        self.update_function = "applyFeedback_" + token
        self.close_function = "closeFeedback_" + token
        self.script_uid = None
        self.script = sim.createScript(
            sim.scripttype_customization, UPDATE_SCRIPT.replace("TOKEN", token), 0, "lua"
        )
        try:
            self.script_uid = sim.getObjectUid(self.script)
            sim.initScript(self.script)
        except Exception:
            try:
                self.close()
            except Exception:
                pass  # Never delete an unverified handle when initialization failed.
            raise

    def update(self, joints_deg: tuple[float, float, float, float]) -> float:
        expected = joint_angles(joints_deg)
        script = self.sim.getObjectFromUid(self.script_uid, {"noError": True})
        if script < 0:
            raise MirrorBindingLost("CoppeliaSim mirror helper was removed or replaced")
        try:
            actual = self.sim.callScriptFunction(
                self.update_function,
                script,
                self.script_uid,
                self.root_uid,
                self.joint_uids,
                list(expected),
            )
        except Exception as error:
            if "MG400_MIRROR_BINDING_LOST" in str(error) or "script does not exist" in str(error):
                raise MirrorBindingLost(str(error)) from error
            raise
        if len(actual) != len(expected) or not all(math.isfinite(q) for q in actual):
            raise RuntimeError("Invalid joint readback from CoppeliaSim")
        error = max(
            abs(math.remainder(a - b, 2 * math.pi)) for a, b in zip(actual, expected, strict=True)
        )
        if error > 1e-6:
            raise RuntimeError(f"CoppeliaSim joint readback mismatch: {error:.6g} rad")
        return error

    def close(self) -> None:
        if self.script_uid is None:
            return
        uid, self.script_uid = self.script_uid, None
        script = self.sim.getObjectFromUid(uid, {"noError": True})
        if script >= 0:
            self.sim.callScriptFunction(self.close_function, script, uid)


def positive_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Must be a positive finite number")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="MG400 IP; receive-only port 30004")
    parser.add_argument("--source-host", help="Local robot-network interface IP, if needed")
    parser.add_argument("--sim-host", default="127.0.0.1")
    parser.add_argument("--sim-port", type=int, default=23000)
    parser.add_argument("--model", default="/MG400", help="Loaded CoppeliaSim model path")
    parser.add_argument("--rate", type=positive_number, default=30, help="Maximum updates/s")
    parser.add_argument("--timeout", type=positive_number, default=2, help="I/O timeout in seconds")
    parser.add_argument("--duration", type=positive_number, help="Stop after this many seconds")
    args = parser.parse_args(argv)
    try:
        try:
            import zmq
            from coppeliasim_zmqremoteapi_client import RemoteAPIClient
        except ImportError as error:
            raise RuntimeError(
                "Install the CoppeliaSim extra: uv sync --extra coppeliasim"
            ) from error
        start = last_log = time.monotonic()
        end = start + args.duration if args.duration is not None else math.inf
        count = 0
        last_error = None
        while time.monotonic() < end:
            client = mirror = None
            session_live = False
            try:
                client = RemoteAPIClient(host=args.sim_host, port=args.sim_port)
                client.socket.setsockopt(zmq.RCVTIMEO, max(1, int(args.timeout * 1000)))
                client.socket.setsockopt(zmq.SNDTIMEO, max(1, int(args.timeout * 1000)))
                client.socket.setsockopt(zmq.LINGER, 0)
                mirror = CoppeliaMirror(client.require("sim"), args.model)
                with FeedbackConnection(args.host, args.source_host, args.timeout) as feedback:
                    while time.monotonic() < end:
                        tick = time.monotonic()
                        state = feedback.read_latest()
                        error = mirror.update(state.joints_deg)
                        count += 1
                        last_error = None
                        now = time.monotonic()
                        if not session_live or now - last_log >= 1:
                            print(
                                json.dumps(
                                    {
                                        "status": "live",
                                        "frames": count,
                                        "joints_deg": state.joints_deg,
                                        "mode": state.mode,
                                        "robot_error": state.error_status,
                                        "max_model_error_rad": error,
                                    }
                                ),
                                flush=True,
                            )
                            last_log = now
                        session_live = True
                        time.sleep(max(0, min(end - now, 1 / args.rate - (now - tick))))
            except (OSError, zmq.ZMQError, MirrorBindingLost) as error:
                last_error = error
                print(
                    f"Mirror reconnecting: {error}. Displayed pose is no longer live; "
                    "waiting for fresh feedback.",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                if mirror is not None:
                    try:
                        mirror.close()
                    except Exception:
                        pass  # Simulator transport may already be disconnected.
                if client is not None:
                    client.socket.close(linger=0)
                    client.context.term()
            if last_error is not None:
                time.sleep(max(0, min(1.0, end - time.monotonic())))
        print(
            f"Mirror finished: {count} updates in {time.monotonic() - start:.2f}s; "
            "displayed pose is no longer live.",
            flush=True,
        )
        return 0 if count and last_error is None else 1
    except KeyboardInterrupt:
        print("Mirror stopped; displayed pose is no longer live.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Mirror stopped: {error}. Displayed pose is no longer live.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
