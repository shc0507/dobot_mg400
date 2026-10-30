"""Keyboard control through RobotPanelState with the real keyboard stack.

The real KeyboardJogSession, JointControl and WebGripper control the simulated MG400
and USB collector of sim_mg400. No test opens a network connection.
"""

import threading
from pathlib import Path

import pytest
from sim_mg400 import HOST, JOGS, POSE, RADIAL_FRAME, START, held_until, wait_for

from mg400.controller_ownership import ControllerOwnership
from mg400.joint_control import JointControl
from mg400.joint_limits import MARGIN_DEG, validate_joints
from mg400.keyboard_jog import CONTINUOUS_MARGIN_DEG_PER_PERCENT
from mg400.web_control import RobotPanelState
from mg400.web_gripper import WebGripper

PREPARATION = ("RobotMode(", "GetErrorID(", "GetAngle(", "SpeedFactor(")
NEXT = "Stopped. Hold a key for the next move."
STOPPED = "Robot stop is confirmed. Disable the robot before recovery."
DISABLED = "Robot disable and rest are confirmed."
FOCUS = "Keyboard off: window lost focus."
CONTACT = (
    "The gripper reports object contact before the target. Release the key before pressing again."
)
HOLD_LIMIT = "Hold limit reached. Release the key before pressing again."

pytestmark = pytest.mark.usefixtures("isolated_controller_locks")


def before_command(monkeypatch, prefix, action):
    """Run action once on the calling worker, just before a guarded command is registered."""
    original = JointControl._guarded_command
    done = threading.Event()

    def guarded(control, text, **kwargs):
        if text.startswith(prefix) and not done.is_set():
            done.set()
            action()
        return original(control, text, **kwargs)

    monkeypatch.setattr(JointControl, "_guarded_command", guarded)
    return done


def before_speed_factor_send(rig, action):
    """Run action once when the registered SpeedFactor opens its connection, before the send."""
    captured = {}

    def opened(port):
        pending = rig.state._control._pending
        if (
            port == 29999
            and not captured
            and pending is not None
            and pending["command"].startswith("SpeedFactor(")
            and not pending["done"].is_set()
        ):
            captured.update(pending=pending, sent=pending["sent"])
            action()

    rig.controller.connect_hooks.append(opened)
    return captured


@pytest.fixture
def rig(make_sim_rig):
    return make_sim_rig()


@pytest.fixture
def gripper_rig(make_sim_rig):
    rig = make_sim_rig(gripper=True)
    rig.connect()
    rig.arm_ready()
    rig.wait(lambda snapshot: snapshot["actual"][4] == 500)
    return rig


def test_released_continuous_hold_sends_one_jog_and_a_verified_jog_stop(rig):
    rig.connect()
    rig.arm_ready(speed=25)
    assert rig.controller.mutations() == ["SpeedFactor(25)"]
    assert rig.snapshot()["speed_factor"] == 25
    rig.hold("A")
    # Mode 11 lasts several worker and watchdog passes. Neither one stops the hold.
    rig.wait_passes(worker=2, watchdog=3)
    snapshot = rig.snapshot()
    assert snapshot["robot_mode"] == 11 and snapshot["keyboard_active_key"] == "A"
    assert snapshot["busy"] and not snapshot["locked"]
    assert snapshot["actual"][:4] == [None] * 4
    rig.controller.slow_jog_stop = True
    rig.keys()
    wait_for(lambda: rig.controller.acked("MoveJog()"), describe=rig.describe)
    # The jog still slows down. Mode 11 is accepted while the session verifies its stop.
    rig.wait_passes(watchdog=3)
    snapshot = rig.snapshot()
    assert snapshot["robot_mode"] == 11 and snapshot["busy"] and not snapshot["locked"]
    rig.controller.rest()
    rig.wait_idle(NEXT)
    assert rig.controller.mutations() == ["SpeedFactor(25)", "MoveJog(J1+)", "MoveJog()"]
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert snapshot["robot_mode"] == 5 and not snapshot["stop_unconfirmed"]
    assert not rig.state._control.stopped
    assert rig.state._control.feedback_retention_reason() is None


@pytest.mark.parametrize(
    ("key", "action"),
    [("A", "stop"), ("W", "stop"), ("A", "disable")],
    ids=["A-stop", "W-stop", "A-disable"],
)
def test_core_action_during_continuous_hold_waits_for_the_verified_jog_stop(rig, key, action):
    core = "ResetRobot()" if action == "stop" else "DisableRobot()"
    rig.connect()
    rig.arm_ready(speed=25)
    rig.hold(key)
    held_control = rig.state._control
    rig.controller.slow_jog_stop = True
    rig.command(action)
    snapshot = rig.snapshot()
    assert not snapshot["keyboard_enabled"]
    if action == "stop":
        assert snapshot["locked"]
    # The session sends its own jog stop at once.
    wait_for(lambda: rig.controller.acked("MoveJog()"), describe=rig.describe)
    # The core command waits while the jog slows down and the session verifies its stop.
    rig.wait_passes(watchdog=3)
    assert core not in rig.controller.sent()
    rig.controller.rest()
    jog = JOGS[key]
    if action == "stop":
        rig.wait_idle(STOPPED)
        frame = [RADIAL_FRAME] if key == "W" else []
        expected = ["SpeedFactor(25)", *frame, jog, "MoveJog()", "ResetRobot()"]
    else:
        rig.wait(lambda s: s["message"] == DISABLED and not s["locked"] and not s["busy"])
        expected = ["SpeedFactor(25)", "MoveJog(J1+)", "MoveJog()", "DisableRobot()"]
    assert rig.controller.mutations() == expected
    assert rig.controller.position("note", "rest") < rig.controller.position("send", core)
    # The session confirmed its stop, so the parent keeps no unconfirmed operation stop.
    assert held_control.feedback_retention_reason() is None
    if action == "disable":
        assert rig.state._control is not held_control
        snapshot = rig.snapshot()
        assert snapshot["robot_mode"] == 4 and not snapshot["stop_unconfirmed"]
        assert not snapshot["keyboard_enabled"]
        return
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"] and snapshot["robot_mode"] == 5

    rig.command("disable")
    rig.wait(lambda s: s["message"] == DISABLED and not s["locked"] and not s["busy"])
    assert rig.controller.motion() == [jog, "MoveJog()", "ResetRobot()", "DisableRobot()"]
    snapshot = rig.snapshot()
    assert snapshot["robot_mode"] == 4 and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"]


def test_close_during_continuous_hold_stops_the_jog_before_the_core_stop(rig):
    rig.connect()
    rig.arm_ready(speed=25)
    rig.hold("A")
    rig.controller.slow_jog_stop = True
    contender = ControllerOwnership(HOST)
    closer = threading.Thread(target=rig.state.close, name="test-close", daemon=True)
    closer.start()
    try:
        wait_for(lambda: rig.controller.acked("MoveJog()"), describe=rig.describe)
        # The panel loops end at close. Feedback still arrives while the jog slows down.
        rig.wait_reads(30)
        assert "ResetRobot()" not in rig.controller.sent()
        assert closer.is_alive()
        # The panel keeps the controller until the stop is verified.
        with pytest.raises(RuntimeError, match="Robot controlled by another panel"):
            contender.acquire()
        rig.controller.rest()
    finally:
        contender.release()
        rig.controller.slow_jog_stop = False
        if rig.controller.sample.mode == 11:
            rig.controller.update(mode=5)
        closer.join(5)
    assert not closer.is_alive()
    assert rig.controller.mutations() == [
        "SpeedFactor(25)",
        "MoveJog(J1+)",
        "MoveJog()",
        "ResetRobot()",
    ]
    assert rig.controller.position("note", "rest") < rig.controller.position("send", "ResetRobot()")
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["syncing"] and not snapshot["busy"] and not snapshot["keyboard_enabled"]
    # The confirmed stop lets close() release the controller for another panel.
    contender.acquire()
    contender.release()


def test_unconfirmed_speed_factor_becomes_a_verified_stop_that_disable_recovers(rig):
    rig.connect()
    rig.controller.ignore_speed_factor = True
    rig.arm(speed=25)
    # The session faults and cancels the parent. The panel turns this into a verified stop.
    rig.wait_idle(STOPPED)
    assert rig.controller.mutations() == ["SpeedFactor(25)", "ResetRobot()"]
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"] and not snapshot["keyboard_arming"]
    assert snapshot["speed_factor"] == 100
    assert rig.state._control.feedback_retention_reason() is None
    rig.command("disable")
    rig.wait(lambda s: s["message"] == DISABLED and not s["locked"] and not s["busy"])
    # The new control enables the robot, and keyboard control works again.
    rig.controller.ignore_speed_factor = False
    rig.command("enable")
    rig.wait(
        lambda s: (
            s["message"] == "Robot operation completed." and s["robot_mode"] == 5 and not s["busy"]
        )
    )
    rig.arm_ready(speed=25)
    assert rig.controller.mutations() == [
        "SpeedFactor(25)",
        "ResetRobot()",
        "DisableRobot()",
        "EnableRobot()",
        "SpeedFactor(25)",
    ]


def test_joint_limit_keeps_keys_on_and_a_new_direction_jogs_away_at_once(rig):
    # J1 starts just outside the 1% stop band and turns toward the limit while A is held.
    rig.controller.update(joints_deg=(158.8, *START[1:]))
    rig.controller.jog_rate = 0.005
    rig.connect()
    rig.arm_ready()
    rig.keys("A")
    rig.wait(
        lambda s: (
            s["message"].endswith("Release the key and choose another direction.") and not s["busy"]
        )
    )
    snapshot = rig.snapshot()
    assert snapshot["message"].startswith("J1=")
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    limit = rig.controller.sample.joints_deg[0]
    assert 160 - (MARGIN_DEG + CONTINUOUS_MARGIN_DEG_PER_PERCENT) < limit < 160
    assert rig.controller.mutations() == ["SpeedFactor(1)", "MoveJog(J1+)", "MoveJog()"]
    # The closing check stops outside the 1.05-degree band, so the other direction
    # starts one continuous jog without a finite inward step.
    assert validate_joints(rig.controller.sample.joints_deg)
    rig.keys()
    rig.keys("D")
    rig.wait(lambda s: s["robot_mode"] == 11)
    rig.keys()
    rig.wait_idle(NEXT)
    assert rig.controller.mutations() == [
        "SpeedFactor(1)",
        "MoveJog(J1+)",
        "MoveJog()",
        "MoveJog(J1-)",
        "MoveJog()",
    ]
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert not rig.state._control.stopped


@pytest.mark.parametrize(
    "phase", ["pose_poll", "preflight", "registration", "send", "acknowledgement"]
)
def test_disarm_during_preparation_sends_no_speed_factor_and_leaves_the_parent_clean(
    rig, monkeypatch, phase
):
    rig.connect()
    gate = speed_factor = None
    if phase == "pose_poll":
        gate = rig.controller.block("GetPose(0,0)")
        assert gate.entered.wait(2)
    elif phase == "preflight":
        gate = rig.controller.block("GetAngle()")
    elif phase == "registration":
        before_command(monkeypatch, "SpeedFactor(", lambda: rig.disarm("focus"))
    elif phase == "send":
        speed_factor = before_speed_factor_send(rig, lambda: rig.disarm("focus"))
    else:
        gate = rig.controller.block("SpeedFactor(")
    armed = rig.controller.mark()
    rig.arm(speed=25)
    if phase == "pose_poll":
        rig.disarm("focus")
        # The preparation ends at once. It does not wait for the pose query.
        rig.wait(lambda s: not s["busy"] and not s["keyboard_arming"])
        assert not gate.passed.is_set()
        gate.open()
        assert not any(text.startswith(PREPARATION) for text in rig.controller.sent(armed))
    elif phase == "preflight":
        assert gate.entered.wait(2)
        rig.disarm("focus")
        # The disarm cuts the preflight short. A later preparation query would stay here.
        pose = rig.controller.block("GetPose(0,0)")
        gate.open()
        rig.wait(lambda s: not s["busy"] and not s["keyboard_arming"])
        pose.open()
    elif phase == "acknowledgement":
        # A SpeedFactor that is already sent finishes without uncertainty.
        assert gate.entered.wait(2)
        rig.disarm("focus")
        gate.open()
    rig.wait(lambda s: not s["busy"] and not s["keyboard_arming"])
    if speed_factor is not None:
        # The disarm came after the registration: the send-time guard refused it.
        pending = speed_factor["pending"]
        assert pending["done"].is_set() and not pending["sent"] and not pending["ok"]
    if phase == "acknowledgement":
        assert rig.controller.mutations() == ["SpeedFactor(25)"]
        assert rig.snapshot()["speed_factor"] == 25
    else:
        assert not any(text.startswith("SpeedFactor(") for text in rig.controller.sent())
    assert rig.controller.motion() == []
    control = rig.state._control
    assert not control.stopped and control.feedback_retention_reason() is None
    snapshot = rig.snapshot()
    assert snapshot["message"] == FOCUS
    assert not snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert not snapshot["stop_unconfirmed"]
    # The parent control stays usable: the next arming prepares and confirms the speed.
    rig.arm_ready(speed=25)
    expected = 2 if phase == "acknowledgement" else 1
    assert rig.controller.mutations() == ["SpeedFactor(25)"] * expected


@pytest.mark.parametrize(
    "phase", ["pose_poll", "preflight", "registration", "send", "acknowledgement"]
)
def test_stop_during_preparation_sends_no_speed_factor_after_the_lock(rig, monkeypatch, phase):
    rig.connect()
    locked_at = []

    def stop():
        locked_at.append(rig.controller.mark())
        rig.command("stop")

    gate = speed_factor = None
    if phase == "registration":
        before_command(monkeypatch, "SpeedFactor(", stop)
    elif phase == "send":
        speed_factor = before_speed_factor_send(rig, stop)
    else:
        prefix = {
            "pose_poll": "GetPose(0,0)",
            "preflight": "GetAngle()",
            "acknowledgement": "SpeedFactor(",
        }[phase]
        gate = rig.controller.block(prefix)
        if phase == "pose_poll":
            assert gate.entered.wait(2)
    armed = rig.controller.mark()
    rig.arm(speed=25)
    if gate is not None:
        assert gate.entered.wait(2)
        stop()
        snapshot = rig.snapshot()
        assert snapshot["locked"] and not snapshot["keyboard_arming"]
        if phase != "pose_poll":
            # The stop waits for the query or acknowledgement in flight.
            gate.open()
    rig.wait_idle(STOPPED)
    if gate is not None:
        gate.open()
    assert not any(text.startswith("SpeedFactor(") for text in rig.controller.sent(locked_at[0]))
    assert rig.controller.motion() == ["ResetRobot()"]
    if phase == "acknowledgement":
        assert rig.controller.mutations() == ["SpeedFactor(25)", "ResetRobot()"]
        assert rig.controller.position("ack", "SpeedFactor(25)") < rig.controller.position(
            "send", "ResetRobot()"
        )
    else:
        assert rig.controller.mutations() == ["ResetRobot()"]
    if phase == "pose_poll":
        # The preparation never started, and the stop did not wait for the pose query.
        assert not any(text.startswith(PREPARATION) for text in rig.controller.sent(armed))
        assert not gate.passed.is_set()
    if phase == "preflight":
        assert rig.controller.position("ack", "GetAngle()") < rig.controller.position(
            "send", "ResetRobot()"
        )
        # The stop cut the preflight short: no pose query followed the lock.
        assert "GetPose(0,0)" not in rig.controller.sent(locked_at[0])
    if speed_factor is not None:
        pending = speed_factor["pending"]
        assert pending["done"].is_set() and not pending["sent"] and not pending["ok"]
    assert rig.state._control.feedback_retention_reason() is None
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"] and not snapshot["keyboard_arming"]


@pytest.mark.parametrize("point", ["enqueue", "send"])
@pytest.mark.parametrize("revocation", ["stop", "lease"])
def test_revocation_before_a_jog_is_sent_sends_nothing(rig, monkeypatch, point, revocation):
    rig.connect()
    rig.arm_ready()
    captured = {}

    def revoke():
        if revocation == "stop":
            rig.command("stop")
        else:
            # The browser lease lapses between two watchdog passes.
            with rig.state._lock:
                rig.state._heartbeat -= rig.state.LEASE_SECONDS + 1

    def opened(port):
        if port != 30003 or captured:
            return
        # The jog is registered and its connection opens. Nothing was sent yet.
        pending = rig.state._control._pending
        captured.update(pending=pending, command=pending["command"], sent=pending["sent"])
        revoke()

    if point == "enqueue":
        before_command(monkeypatch, "MoveJog(J1+)", revoke)
    else:
        rig.controller.connect_hooks.append(opened)
    rig.keys("A")
    rig.wait_idle(STOPPED)
    if point == "enqueue":
        # The guards refused the registration, so the motion connection never opened.
        assert 30003 not in rig.controller.ports()
    else:
        assert captured["command"] == "MoveJog(J1+)" and captured["sent"] is False
        pending = captured["pending"]
        assert pending["done"].is_set() and not pending["sent"] and not pending["ok"]
    assert not any(text.startswith("MoveJog(") for text in rig.controller.sent())
    motion = rig.controller.motion()
    assert motion and set(motion) == {"ResetRobot()"}
    assert rig.state._control.feedback_retention_reason() is None
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"]


def test_z_steps_open_by_25_from_each_measured_position_until_release(gripper_rig):
    rig, collector = gripper_rig, gripper_rig.collector
    third = threading.Event()

    def answer(request):
        if collector.answered_count() == 2 and not third.is_set():
            return None
        # Arrive 2 / 1000 short of the target, inside the arrival tolerance.
        return {"state": 1, "position": request["position"] - 2}

    collector.answer = answer
    rig.keys("Z")
    wait_for(lambda: len(collector.targets("published")) == 3, describe=rig.describe)
    rig.keys()
    third.set()
    rig.wait_idle(NEXT)
    assert collector.targets("published") == [525, 548, 571]
    assert collector.targets("answered") == [525, 548, 571]
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert not snapshot["gripper_busy"]
    rig.wait(lambda s: s["actual"][4] == 569)
    assert not rig.gripper.failed and rig.gripper.last_outcome == "arrived"
    assert rig.controller.mutations() == ["SpeedFactor(1)"]


def test_x_at_the_closed_end_sends_nothing_and_z_from_it_opens(make_sim_rig):
    rig = make_sim_rig(gripper=True)
    rig.collector.set(position=0)
    rig.connect()
    rig.arm_ready()
    rig.wait(lambda snapshot: snapshot["actual"][4] == 0)
    rig.keys("X")
    rig.wait_idle(NEXT)
    assert rig.collector.targets("published") == []
    assert not rig.collector.command_path.exists()
    assert rig.snapshot()["keyboard_enabled"]
    rig.keys()
    allow = threading.Event()
    rig.collector.answer = held_until(allow)
    rig.keys("Z")
    wait_for(lambda: rig.collector.targets("published") == [25], describe=rig.describe)
    rig.keys()
    allow.set()
    wait_for(lambda: rig.collector.targets("answered") == [25], describe=rig.describe)
    rig.wait_idle(NEXT)
    assert rig.collector.targets("published") == [25]
    assert rig.snapshot()["keyboard_enabled"]
    rig.wait(lambda s: s["actual"][4] == 25)
    assert not rig.gripper.failed
    assert rig.controller.mutations() == ["SpeedFactor(1)"]


@pytest.mark.parametrize("cause", ["mode", "stop"])
def test_publication_guard_refusal_publishes_nothing_and_leaves_the_gripper_unlocked(
    gripper_rig, monkeypatch, cause
):
    rig, collector, controller = gripper_rig, gripper_rig.collector, gripper_rig.controller
    write_text, unlink = Path.write_text, Path.unlink
    prepared = threading.Event()

    def prepare_request(path, *args, **kwargs):
        result = write_text(path, *args, **kwargs)
        if path.name.endswith(".command.json.tmp") and not prepared.is_set():
            prepared.set()
            if cause == "mode":
                # The robot reports motion while the step waits for its publication.
                controller.update(mode=7)
                wait_for(lambda: rig.snapshot()["robot_mode"] == 7)
            else:
                rig.command("stop")
        return result

    def remove_request(path, *args, **kwargs):
        if path.name.endswith(".command.json.tmp") and controller.sample.mode == 7:
            # The robot is idle again before the step reports its result.
            controller.update(mode=5)
            wait_for(lambda: rig.snapshot()["robot_mode"] == 5)
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", prepare_request)
    monkeypatch.setattr(Path, "unlink", remove_request)
    rig.keys("Z")
    if cause == "mode":
        rig.wait(lambda s: s["message"].startswith("Gripper unavailable:") and not s["busy"])
    else:
        rig.wait(lambda s: s["message"].startswith(STOPPED) and not s["busy"])
    assert prepared.is_set()
    assert collector.targets("published") == []
    assert not collector.command_path.exists()
    assert not rig.gripper.failed and not rig.gripper.snapshot()["locked"]
    snapshot = rig.snapshot()
    assert not snapshot["stop_unconfirmed"]
    if cause == "stop":
        # The stop turns keys off and does not wait for the gripper step.
        assert not snapshot["keyboard_enabled"] and snapshot["locked"]
        assert "Gripper unavailable" not in snapshot["message"]
        assert controller.motion() == ["ResetRobot()"]
        return
    assert snapshot["message"].startswith("Gripper unavailable: No command sent.")
    assert "idle" in snapshot["message"]
    assert snapshot["message"].endswith("Arm keys remain ready.")
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert controller.mutations() == ["SpeedFactor(1)"]
    # The keys still work: a new press publishes one step.
    allow = threading.Event()
    collector.answer = held_until(allow)
    rig.keys()
    rig.keys("Z")
    wait_for(lambda: collector.targets("published") == [525], describe=rig.describe)
    rig.keys()
    allow.set()
    wait_for(lambda: collector.targets("answered") == [525], describe=rig.describe)
    rig.wait_idle(NEXT)
    assert rig.snapshot()["keyboard_enabled"] and not rig.gripper.failed


def test_replace_failure_latches_the_gripper_and_turns_keys_off_without_stopping_the_arm(
    gripper_rig, monkeypatch
):
    rig = gripper_rig
    replace_path = Path.replace

    def fail_publication(path, target):
        if path.name.endswith(".command.json.tmp"):
            raise OSError("Disk failure.")
        return replace_path(path, target)

    monkeypatch.setattr(Path, "replace", fail_publication)
    rig.keys("X")
    rig.wait(lambda s: s["message"].startswith("ERROR:") and not s["busy"])
    snapshot = rig.snapshot()
    assert snapshot["message"] == "ERROR: Control is locked. Check the gripper. Disk failure."
    assert not snapshot["keyboard_enabled"]
    assert rig.gripper.failed and rig.gripper.snapshot()["locked"]
    # The gripper latch shows as locked. The arm itself is not stopped or locked.
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert snapshot["robot_mode"] == 5
    assert not rig.state._locked and not rig.state._control.stopped
    rig.wait_passes(worker=1, watchdog=2)
    assert rig.controller.motion() == []
    assert rig.controller.mutations() == ["SpeedFactor(1)"]
    assert rig.collector.targets("published") == []
    # The latched gripper blocks keyboard arming.
    with pytest.raises(RuntimeError, match="Gripper control is locked"):
        rig.arm()


def test_collector_lock_during_a_step_keeps_the_keyboard_off_reason(gripper_rig, monkeypatch):
    rig, collector = gripper_rig, gripper_rig.collector
    write_text = Path.write_text
    prepared = threading.Event()

    def prepare_request(path, *args, **kwargs):
        result = write_text(path, *args, **kwargs)
        if path.name.endswith(".command.json.tmp") and not prepared.is_set():
            prepared.set()
            # The collector locks while the step prepares its request. Keys turn off.
            collector.lock("Write reply lost.")
            wait_for(lambda: not rig.snapshot()["keyboard_enabled"])
        return result

    monkeypatch.setattr(Path, "write_text", prepare_request)
    rig.keys("Z")
    wait_for(prepared.is_set, describe=rig.describe)
    rig.wait(lambda s: not s["busy"] and not s["gripper_busy"])
    snapshot = rig.snapshot()
    # The adapter refused the locked collector before publication: no command, no latch.
    assert collector.targets("published") == [] and not collector.command_path.exists()
    assert not rig.gripper.failed
    assert not snapshot["keyboard_enabled"] and not snapshot["stop_unconfirmed"]
    assert rig.controller.motion() == []
    # Keys were off before the step ended, so the step result sets no message (spec §8).
    assert snapshot["keyboard_disarm_reason"] == "gripper control is locked."
    assert snapshot["message"] == "Keyboard off: gripper control is locked."
    assert snapshot["locked"]
    # The collector lock does not stop the arm, and it refuses a new arming.
    rig.wait_passes(worker=1, watchdog=2)
    assert not rig.state._locked and not rig.state._control.stopped
    assert rig.controller.motion() == []
    with pytest.raises(RuntimeError, match="Gripper control is locked"):
        rig.arm()


def test_contact_keeps_keys_on_and_needs_a_new_press_for_the_next_step(gripper_rig):
    rig, collector = gripper_rig, gripper_rig.collector
    collector.answer = lambda request: {"state": 2, "position": 490}
    rig.keys("X")
    rig.wait_idle(CONTACT)
    assert collector.targets("published") == [475]
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert rig.gripper.last_outcome == "contact" and not rig.gripper.failed
    # A renewal of the held key starts nothing, also in later worker passes.
    rig.keys("X")
    rig.wait_passes(worker=2)
    assert collector.targets("published") == [475]
    snapshot = rig.snapshot()
    assert not snapshot["busy"] and snapshot["message"] == CONTACT
    # Release and press again: one new step from the measured contact position.
    allow = threading.Event()
    collector.answer = held_until(allow)
    rig.keys()
    rig.keys("X")
    wait_for(lambda: collector.targets("published") == [475, 465], describe=rig.describe)
    rig.keys()
    allow.set()
    wait_for(lambda: collector.targets("answered") == [475, 465], describe=rig.describe)
    rig.wait_idle(NEXT)
    assert rig.snapshot()["keyboard_enabled"] and not rig.gripper.failed


def test_gripper_hold_limit_keeps_keys_on_and_needs_a_new_press(gripper_rig, monkeypatch):
    rig, collector = gripper_rig, gripper_rig.collector
    monkeypatch.setattr(RobotPanelState, "GRIPPER_HOLD_SECONDS", 0.15)
    rig.keys("Z")
    rig.wait_idle(HOLD_LIMIT)
    targets = collector.targets("published")
    assert targets == [525 + 25 * index for index in range(len(targets))]
    assert targets and collector.targets("answered") == targets
    assert rig.snapshot()["keyboard_enabled"] and not rig.gripper.failed
    rig.keys("Z")
    rig.wait_passes(worker=2)
    assert collector.targets("published") == targets
    assert rig.snapshot()["message"] == HOLD_LIMIT


def test_stop_during_gripper_step_does_not_wait_for_the_step(gripper_rig):
    rig, collector = gripper_rig, gripper_rig.collector
    allow = threading.Event()
    collector.answer = held_until(allow)
    rig.keys("Z")
    wait_for(lambda: collector.targets("published") == [525], describe=rig.describe)
    try:
        rig.command("stop")
        wait_for(lambda: "ResetRobot()" in rig.controller.sent(), describe=rig.describe)
        # The core stop went out while the gripper step still waits for its result.
        assert collector.targets("answered") == []
        assert rig.snapshot()["gripper_busy"]
    finally:
        allow.set()
    rig.wait(lambda s: s["message"].startswith(STOPPED) and not s["busy"])
    assert collector.targets("answered") == [525]
    assert not rig.gripper.failed and not rig.gripper.snapshot()["locked"]
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"]
    assert rig.controller.motion() == ["ResetRobot()"]


def test_continuous_hold_does_not_leave_the_pre_hold_pose_visible(rig):
    rig.connect()
    rig.wait(lambda snapshot: snapshot["actual"][:4] == list(POSE))
    rig.arm_ready()
    rig.hold("A")
    # Block the pose query after the hold, so only the hold decides what the panel shows.
    gate = rig.controller.block("GetPose(0,0)")
    rig.keys()
    rig.wait_idle(NEXT)
    try:
        assert gate.entered.wait(2)
        assert rig.snapshot()["actual"][:4] == [None] * 4
    finally:
        gate.open()


def test_release_before_publication_sends_no_gripper_step(gripper_rig, monkeypatch):
    rig, collector = gripper_rig, gripper_rig.collector
    move = WebGripper.move
    released = []

    def release_then_move(gripper, position):
        # The key-up arrives after the step checked its release and before it publishes.
        if not released:
            released.append(rig.keys())
        return move(gripper, position)

    monkeypatch.setattr(WebGripper, "move", release_then_move)
    rig.keys("X")
    rig.wait_idle(NEXT)
    assert released
    assert collector.targets("published") == []
    assert not collector.command_path.exists()
    assert not rig.gripper.failed
    snapshot = rig.snapshot()
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    assert snapshot["actual"][4] == 500
