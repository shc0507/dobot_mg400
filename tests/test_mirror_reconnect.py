import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call

import pytest

from mg400_agent import mirror
from mg400_agent.feedback import Feedback


class ZMQError(Exception):
    pass


def session(*read_results):
    feedback = MagicMock()
    feedback.__enter__.return_value = feedback
    feedback.read_latest.side_effect = read_results
    return SimpleNamespace(
        client=SimpleNamespace(socket=Mock(), context=Mock(), require=Mock()),
        feedback=feedback,
        mirror=Mock(update=Mock(return_value=0)),
    )


@pytest.fixture
def environment(monkeypatch):
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        assert 0 <= seconds <= 1
        clock.sleeps.append(seconds)
        assert len(clock.sleeps) <= 20, "Mirror kept retrying beyond its deadline"
        clock.now += seconds

    monkeypatch.setattr(mirror.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(mirror.time, "sleep", sleep)
    monkeypatch.setitem(
        sys.modules,
        "zmq",
        SimpleNamespace(RCVTIMEO=1, SNDTIMEO=2, LINGER=3, ZMQError=ZMQError),
    )

    def install(*sessions):
        clients = Mock(side_effect=[s.client for s in sessions])
        feedback = Mock(side_effect=[s.feedback for s in sessions])
        mirrors = Mock(side_effect=[s.mirror for s in sessions])
        monkeypatch.setitem(
            sys.modules,
            "coppeliasim_zmqremoteapi_client",
            SimpleNamespace(RemoteAPIClient=clients),
        )
        monkeypatch.setattr(mirror, "FeedbackConnection", feedback)
        monkeypatch.setattr(mirror, "CoppeliaMirror", mirrors)
        return SimpleNamespace(clients=clients, feedback=feedback, mirrors=mirrors, clock=clock)

    return install


def assert_closed(connection):
    connection.feedback.__exit__.assert_called_once()
    connection.mirror.close.assert_called_once()
    connection.client.socket.close.assert_called_once_with(linger=0)
    connection.client.context.term.assert_called_once()


def run_for(seconds):
    return mirror.main(
        [
            "--host",
            "robot.test",
            "--source-host",
            "192.168.1.10",
            "--rate",
            "10",
            "--duration",
            str(seconds),
        ]
    )


def test_timeout_reconnects_all_resources_and_only_applies_new_feedback(environment, capsys):
    old = Feedback((10, 20, 30, 40), 5, 1, False)
    fresh = Feedback((11, 21, 31, 41), 5, 2, False)
    first = session(old, TimeoutError("No complete feedback frame"))
    second = session(fresh)
    env = environment(first, second)

    def reconnect(*args, **kwargs):
        if env.clients.call_count == 2:
            assert_closed(first)
            return second.client
        return first.client

    env.clients.side_effect = reconnect
    assert run_for(1.15) == 0

    assert env.clients.call_count == env.feedback.call_count == env.mirrors.call_count == 2
    assert env.feedback.call_args_list == [call("robot.test", "192.168.1.10", 2)] * 2
    first.mirror.update.assert_called_once_with(old.joints_deg)
    second.mirror.update.assert_called_once_with(fresh.joints_deg)
    assert 1 in env.clock.sleeps
    assert_closed(second)
    output = capsys.readouterr()
    assert "reconnecting" in output.err.lower()
    assert "no longer live" in output.err.lower()
    assert "live" in output.out.lower()


def test_simulator_transport_failure_reconnects_without_replaying_failed_pose(environment):
    old = Feedback((10, 20, 30, 40), 5, 1, False)
    fresh = Feedback((11, 21, 31, 41), 5, 2, False)
    first, second = session(old), session(fresh)
    first.mirror.update.side_effect = ZMQError("Simulator restarted")
    env = environment(first, second)

    assert run_for(1.05) == 0

    assert env.clients.call_count == 2
    second.mirror.update.assert_called_once_with(fresh.joints_deg)
    assert_closed(first)
    assert_closed(second)


def test_lost_scene_binding_rebuilds_mirror_for_fresh_sample(environment):
    sample = Feedback((10, 20, 30, 40), 5, 1, False)
    first, second = session(sample), session(sample)
    first.mirror.update.side_effect = mirror.MirrorBindingLost("MG400 model was reloaded")
    env = environment(first, second)

    assert run_for(1.05) == 0

    assert env.mirrors.call_count == 2
    second.mirror.update.assert_called_once_with(sample.joints_deg)
    assert_closed(first)
    assert_closed(second)


def test_retries_connection_failures_until_wall_time_deadline(environment, capsys):
    first, second = session(), session()
    first.feedback.__enter__.side_effect = OSError("Robot unreachable")
    second.feedback.__enter__.side_effect = ConnectionError("Robot disconnected")
    env = environment(first, second)

    assert run_for(1.5) == 1

    assert env.clients.call_count == env.feedback.call_count == 2
    assert env.clock.now == pytest.approx(1.5)
    for connection in (first, second):
        connection.mirror.update.assert_not_called()
        connection.mirror.close.assert_called_once()
        connection.client.socket.close.assert_called_once_with(linger=0)
        connection.client.context.term.assert_called_once()
    assert "no longer live" in capsys.readouterr().err.lower()


def test_duration_ends_disconnected_after_a_previous_live_sample(environment, capsys):
    sample = Feedback((10, 20, 30, 40), 5, 1, False)
    connection = session(sample, TimeoutError("Feedback stopped"))
    env = environment(connection)

    assert run_for(0.25) == 1

    env.clients.assert_called_once()
    assert env.clock.now == pytest.approx(0.25)
    connection.mirror.update.assert_called_once_with(sample.joints_deg)
    assert_closed(connection)
    assert "no longer live" in capsys.readouterr().err.lower()


def test_keyboard_interrupt_cleans_up_without_retry(environment):
    connection = session(KeyboardInterrupt())
    env = environment(connection)

    assert run_for(10) == 130

    env.clients.assert_called_once()
    assert env.clock.sleeps == []
    connection.mirror.update.assert_not_called()
    assert_closed(connection)


@pytest.mark.parametrize(
    "error", [ValueError("Malformed feedback"), RuntimeError("Joint readback mismatch")]
)
def test_invalid_data_and_readback_failures_are_fatal_without_retry(environment, error):
    sample = Feedback((10, 20, 30, 40), 5, 1, False)
    connection = session(error if isinstance(error, ValueError) else sample)
    if isinstance(error, RuntimeError):
        connection.mirror.update.side_effect = error
    env = environment(connection)

    assert run_for(10) == 1

    env.clients.assert_called_once()
    assert env.clock.sleeps == []
    assert_closed(connection)
