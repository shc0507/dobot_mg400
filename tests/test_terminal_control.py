import curses
from collections import deque
from unittest.mock import Mock

import pytest
from helpers import Feed, PanelControl, block_network, wait_for

from mg400_agent import terminal_control
from mg400_agent.terminal_control import TerminalSession, parse_command, run_terminal, status_lines
from mg400_agent.web_control import RobotPanelState


class State:
    def __init__(self):
        self.commands = []
        self.heartbeats = []
        self.closed = False
        self.values = {
            "robot_host": "robot.test",
            "feedback_fresh": False,
            "syncing": False,
            "robot_mode": None,
            "robot_error": None,
            "busy": False,
            "locked": False,
            "stop_unconfirmed": False,
            "joints": None,
            "actual": [None] * 5,
            "message": "Disconnected",
            "gripper_message": "Gripper is not configured.",
        }

    def snapshot(self):
        return self.values.copy()

    def heartbeat(self, client_id):
        self.heartbeats.append(client_id)
        return self.snapshot()

    def command(self, payload, client_id):
        self.commands.append((payload, client_id))

    def close(self):
        self.closed = True


class Screen:
    def __init__(self, keys, *, before_key=None):
        self.keys = deque(keys)
        self.before_key = before_key
        self.rows = {}
        self.frames = []

    def keypad(self, enabled):
        pass

    def timeout(self, milliseconds):
        pass

    def getmaxyx(self):
        return 24, 100

    def erase(self):
        self.rows = {}

    def addnstr(self, row, column, text, length):
        self.rows[row] = text[:length]

    def move(self, row, column):
        pass

    def refresh(self):
        self.frames.append(self.rows.copy())

    def get_wch(self):
        assert self.keys, "The terminal did not exit after the supplied input"
        if self.before_key is not None:
            self.before_key()
        return self.keys.popleft()


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch, isolated_controller_locks):
    block_network(monkeypatch)
    monkeypatch.setattr(curses, "raw", lambda: None)


@pytest.mark.parametrize(
    ("line", "command"),
    [
        ("", None),
        ("  ", None),
        ("connect", {"action": "start_sync"}),
        ("disconnect", {"action": "stop_sync"}),
        ("stop", {"action": "stop"}),
        ("disable", {"action": "disable"}),
        ("enable", {"action": "enable"}),
        ("home", {"action": "home"}),
        ("grip-init", {"action": "initialize_gripper"}),
        ("grip 0", {"action": "grip", "position": 0}),
        ("grip 1000", {"action": "grip", "position": 1000}),
        ("help", "help"),
        ("status", "status"),
        ("quit", "quit"),
        ("quit --force", "force_quit"),
        (
            "move 250.5 -2 3e1 -90",
            {"action": "execute", "targets": {"x": 250.5, "y": -2.0, "z": 30.0, "r": -90.0}},
        ),
    ],
)
def test_parse_command(line, command):
    assert parse_command(line) == command
    if isinstance(command, dict):
        state = State()
        session = TerminalSession(state)
        session.submit(line)
        assert state.commands == [(command, session.client_id)]


@pytest.mark.parametrize(
    "line",
    [
        "move 1 2 3",
        "move 1 2 3 4 5",
        "move nan 2 3 4",
        "move 1 inf 3 4",
        "move 1 2 -inf 4",
        "move 1 2 3 1e10000",
        "move yes 2 3 4",
        "move '1 2 3 4'",
        "move '1 2 3 4",
        "grip",
        "grip 1 2",
        "grip -1",
        "grip 1001",
        "grip 0.5",
        "grip nan",
        "grip inf",
        "grip 1e10000",
        "grip true",
        "connect now",
        "stop now",
        "quit force",
        "quit --force extra",
        "clear-error",
    ],
)
def test_invalid_command_never_reaches_state(line):
    state = State()
    session = TerminalSession(state)
    with pytest.raises(ValueError):
        parse_command(line)
    session.submit(line)
    assert state.commands == []
    assert session.messages[-1].startswith("Error:")


def test_session_startup_and_status_do_not_connect_or_send_commands():
    state = State()
    session = TerminalSession(state)
    session.submit("status")
    session.submit("help")
    assert state.commands == []
    assert not session.finished
    first = session.tick()
    session.tick()
    assert first == state.snapshot()
    assert state.heartbeats == [session.client_id, session.client_id]
    assert list(session.messages).count("Disconnected") == 1


def test_stale_status_hides_measurements_and_robot_mode():
    state = State()
    state.values.update(joints=[1, 2, 3, 4], actual=[250, 0, 30, 90, 1000], robot_mode=5)
    lines = status_lines(state.snapshot())
    assert "Mode=--" in lines[1]
    assert lines[2] == "Joints (deg): J1=--  J2=--  J3=--  J4=--"
    assert lines[3] == "Flange (mm/deg): X=--  Y=--  Z=--  R=--"
    assert lines[4] == "Gripper (0..1000): position=--"


def test_stop_dispatch_remains_available_while_quitting():
    state = State()
    state.values.update(syncing=True, busy=True)
    session = TerminalSession(state)
    session.submit("quit")
    session.submit("move 250 0 30 0")
    session.submit("stop")
    session.submit("disable")
    assert [payload["action"] for payload, _ in state.commands] == [
        "stop_sync",
        "stop",
        "disable",
    ]
    assert not session.finished


def test_quit_waits_for_disconnection_and_pending_operation():
    state = State()
    state.values.update(syncing=True, busy=True)
    session = TerminalSession(state)
    session.submit("quit")
    assert state.commands == [({"action": "stop_sync"}, session.client_id)]
    session.tick()
    assert session.quitting and not session.finished
    state.values["syncing"] = False
    session.tick()
    assert session.quitting and not session.finished
    state.values["busy"] = False
    session.tick()
    assert session.finished and session.exit_code == 0


def test_failed_stop_keeps_terminal_open_and_reports_failure():
    state = State()
    state.values.update(syncing=True, busy=True)
    session = TerminalSession(state)
    session.submit("quit")
    state.values.update(busy=False, locked=True, message="Robot stop is unconfirmed")
    session.tick()
    assert not session.finished
    assert not session.quitting
    assert "physical emergency stop" in " ".join(session.messages)
    session.submit("disable")
    assert state.commands[-1][0] == {"action": "disable"}


def test_quit_with_unconfirmed_stop_and_no_feedback_keeps_terminal_open():
    state = State()
    state.values.update(stop_unconfirmed=True, locked=True)
    session = TerminalSession(state)
    session.submit("quit")
    session.tick()
    assert not session.finished
    assert state.commands == []
    assert "physical emergency stop" in " ".join(session.messages)


def test_ui_preserves_input_and_handles_ctrl_c_while_operation_is_busy():
    state = State()
    session = TerminalSession(state)

    def receive(payload, client_id):
        state.commands.append((payload, client_id))
        if payload["action"] == "execute":
            state.values.update(syncing=True, busy=True)
        elif payload["action"] == "stop":
            state.values["locked"] = True

    state.command = receive
    screen = Screen(list("move 250 0 30 0\nstatus\n") + ["\x03"] + list("quit --force\n"))
    run_terminal(screen, session)
    assert [payload["action"] for payload, _ in state.commands] == ["execute", "stop"]
    assert any(frame[23] == "mg400> stat" for frame in screen.frames)
    assert any("Busy=True" in message for message in session.messages)
    assert len(state.heartbeats) >= len(screen.frames)
    assert session.finished and session.exit_code == 1


def test_ui_ctrl_d_requests_disconnect_and_waits_for_result():
    state = State()
    state.values.update(syncing=True)
    session = TerminalSession(state)

    def receive(payload, client_id):
        state.commands.append((payload, client_id))
        state.values["busy"] = True

    def complete_stop():
        if state.commands:
            state.values.update(syncing=False, busy=False)

    state.command = receive
    screen = Screen(["\x04", curses.KEY_RESIZE, curses.KEY_RESIZE], before_key=complete_stop)
    run_terminal(screen, session)
    assert state.commands == [({"action": "stop_sync"}, session.client_id)]
    assert session.finished
    assert session.exit_code == 0


def test_terminal_stop_interrupts_waiting_robot_move():
    feed = Feed()
    controls = []

    def control_factory(*args, **kwargs):
        control = PanelControl(*args, **kwargs)
        controls.append(control)
        return control

    state = RobotPanelState("robot.test", connection_factory=feed, control_factory=control_factory)
    session = TerminalSession(state)
    control = controls[0]
    try:
        session.submit("connect")
        wait_for(lambda: session.tick()["feedback_fresh"])
        control.release_move.clear()
        session.submit("move 250 0 30 0")
        assert control.move_entered.wait(1)
        assert session.tick()["busy"]
        session.submit("stop")
        assert control.stop_entered.wait(1)
        assert control.stopped
        control.release_move.set()
        wait_for(lambda: not session.tick()["busy"])
        assert state.snapshot()["locked"]
        session.submit("quit")
        wait_for(lambda: session.tick() and session.finished)
    finally:
        control.release_move.set()
        state.close()


@pytest.mark.parametrize("stream", ["stdin", "stdout"])
def test_non_tty_fails_before_loading_config_or_creating_state(monkeypatch, capsys, stream):
    monkeypatch.setattr(terminal_control.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(terminal_control.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(getattr(terminal_control.sys, stream), "isatty", lambda: False)
    constructor = Mock(side_effect=AssertionError("State must remain unconstructed"))
    monkeypatch.setattr(terminal_control, "RobotPanelState", constructor)
    monkeypatch.setattr(
        terminal_control, "load_robot_host", Mock(side_effect=AssertionError("Config"))
    )
    assert terminal_control.main([]) == 2
    constructor.assert_not_called()
    assert "interactive terminal" in capsys.readouterr().err


def test_help_does_not_construct_state_or_require_tty(monkeypatch, capsys):
    constructor = Mock(side_effect=AssertionError("State must remain unconstructed"))
    monkeypatch.setattr(terminal_control, "RobotPanelState", constructor)
    with pytest.raises(SystemExit) as error:
        terminal_control.main(["--help"])
    assert error.value.code == 0
    constructor.assert_not_called()
    assert "--robot-host" in capsys.readouterr().out


def tty_state(monkeypatch):
    """Patch the TTY checks and the state factory in the test call phase, after capsys starts."""
    state = State()
    monkeypatch.setattr(terminal_control.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(terminal_control.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(terminal_control, "RobotPanelState", lambda host, gripper: state)
    return state


@pytest.mark.parametrize("failure", [None, KeyboardInterrupt(), RuntimeError("Terminal failed")])
def test_main_constructs_inert_session_and_closes_it_on_exit(monkeypatch, failure):
    state = tty_state(monkeypatch)
    monkeypatch.setattr(terminal_control.signal, "signal", Mock())

    def wrapper(function, session):
        assert state.commands == []
        assert session.state is state
        if failure is not None:
            raise failure
        session.submit("quit")

    monkeypatch.setattr(terminal_control.curses, "wrapper", wrapper)
    code = terminal_control.main(["--robot-host", "robot.test"])
    assert code == (130 if isinstance(failure, KeyboardInterrupt) else 1 if failure else 0)
    assert state.closed
    assert state.commands == []


@pytest.mark.parametrize("failure", ["exception", "stop_unconfirmed", "busy"])
def test_main_reports_failed_cleanup_and_restores_signal_handlers(monkeypatch, capsys, failure):
    state = tty_state(monkeypatch)
    monkeypatch.setattr(terminal_control.curses, "wrapper", lambda function, session: None)
    signals = terminal_control.signal
    original = {signum: object() for signum in (signals.SIGINT, signals.SIGTERM, signals.SIGHUP)}
    handlers = original.copy()

    def signal_handler(signum, handler):
        previous = handlers[signum]
        handlers[signum] = handler
        return previous

    def close():
        assert all(handler == signals.SIG_IGN for handler in handlers.values())
        state.closed = True
        if failure == "exception":
            raise RuntimeError("Stop transport failed")
        state.values[failure] = True

    monkeypatch.setattr(signals, "signal", signal_handler)
    state.close = close
    assert terminal_control.main(["--robot-host", "robot.test"]) == 1
    assert state.closed
    assert handlers == original
    assert "physical emergency stop" in capsys.readouterr().err
