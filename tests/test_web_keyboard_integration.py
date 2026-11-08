"""Keyboard control through RobotPanelState with the real keyboard stack.

The real KeyboardJogSession and JointControl control the simulated MG400
of sim_mg400. No test opens a network connection.
"""

import threading

import pytest
from sim_mg400 import HOST, JOGS, POSE, RADIAL_FRAME, START, wait_for

from mg400.controller_ownership import ControllerOwnership
from mg400.joint_control import JointControl
from mg400.joint_limits import MARGIN_DEG, validate_joints
from mg400.keyboard_jog import CONTINUOUS_MARGIN_DEG_PER_PERCENT

PREPARATION = ("RobotMode(", "GetErrorID(", "GetAngle(", "SpeedFactor(")
NEXT = "Stopped. Hold a key for the next move."
STOPPED = "Robot stop is confirmed. Disable the robot before recovery."
DISABLED = "Robot disable and rest are confirmed."
FOCUS = "Keyboard off: window lost focus."

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
    assert snapshot["actual"] == [None] * 4
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


def test_continuous_hold_does_not_leave_the_pre_hold_pose_visible(rig):
    rig.connect()
    rig.wait(lambda snapshot: snapshot["actual"] == list(POSE))
    rig.arm_ready()
    rig.hold("A")
    # Block the pose query after the hold, so only the hold decides what the panel shows.
    gate = rig.controller.block("GetPose(0,0)")
    rig.keys()
    rig.wait_idle(NEXT)
    try:
        assert gate.entered.wait(2)
        assert rig.snapshot()["actual"] == [None] * 4
    finally:
        gate.open()


SUCK = ["DOExecute(1,0)", "DOExecute(2,1)"]
RELEASE = ["DOExecute(2,0)", "DOExecute(1,1)", "DOExecute(1,0)"]


def outputs(rig, mark):
    return [text for text in rig.controller.sent(mark) if text.startswith("DO")]


def test_z_sucks_and_x_releases_once_per_press_without_motion(rig):
    rig.connect()
    rig.arm_ready()
    mark = rig.controller.mark()
    rig.keys("Z")
    rig.wait_idle("Suction: suck done. Hold a key for the next move.")
    # A held key does not repeat the action.
    rig.wait_passes(worker=2, watchdog=3)
    assert outputs(rig, mark) == SUCK
    snapshot = rig.snapshot()
    assert snapshot["vacuum"] == {"suck": True, "blow": False, "held": None}
    assert snapshot["keyboard_enabled"] and not snapshot["locked"]
    rig.keys()
    mark = rig.controller.mark()
    rig.keys("X")
    rig.wait_idle("Suction: release done. Hold a key for the next move.")
    assert outputs(rig, mark) == RELEASE
    assert rig.snapshot()["vacuum"] == {"suck": False, "blow": False, "held": None}
    rig.keys()
    assert rig.controller.motion() == []
    assert rig.controller.mutations() == ["SpeedFactor(1)", *SUCK, *RELEASE]


def test_arm_keys_jog_after_a_suction_key(rig):
    rig.connect()
    rig.arm_ready()
    rig.keys("Z")
    rig.wait_idle("Suction: suck done. Hold a key for the next move.")
    rig.keys()
    rig.hold("A")
    rig.keys()
    rig.wait_idle(NEXT)
    assert rig.snapshot()["keyboard_enabled"]


def test_stop_during_the_release_blow_stops_the_robot_and_turns_keys_off(rig):
    rig.connect()
    rig.arm_ready()
    rig.keys("Z")
    rig.wait_idle("Suction: suck done. Hold a key for the next move.")
    rig.keys()
    mark = rig.controller.mark()
    rig.keys("X")
    # The blow output is on and no command is in flight.
    wait_for(lambda: "DOExecute(1,1)" in rig.controller.sent(mark), describe=rig.describe)
    rig.wait(lambda s: s["vacuum"]["blow"])
    rig.command("stop")
    rig.wait(lambda s: s["message"].startswith(STOPPED) and not s["busy"])
    snapshot = rig.snapshot()
    assert snapshot["locked"] and not snapshot["stop_unconfirmed"]
    assert not snapshot["keyboard_enabled"]
    assert rig.controller.motion() == ["ResetRobot()"]
    # The stop ends the blow pulse and sends no DO2 command.
    assert outputs(rig, mark) == ["DOExecute(2,0)", "DOExecute(1,1)", "DOExecute(1,0)"]
    rig.wait(lambda s: s["vacuum"] == {"suck": False, "blow": False, "held": None})
