"""Connection warnings have a server-owned clock and never dispatch device actions."""

import threading
from types import SimpleNamespace

import pytest

from labkit.errors import ServiceError
from orchestrator.agent_connect.authority import SESSION_TIMEOUT_S, Authority
from orchestrator.agent_connect.control import ControlHost


class Clock:
    now = 0

    def __call__(self):
        return self.now


class Publications:
    def __init__(self):
        self.condition = threading.Condition()
        self.stream, self.journal = [], []

    def publish(self, kind, value):
        with self.condition:
            self.stream.append((kind, value))
            self.condition.notify_all()

    def add(self, level, message, **fields):
        with self.condition:
            self.journal.append((level, message, fields))
            self.condition.notify_all()

    def wait(self, count=1):
        with self.condition:
            return self.condition.wait_for(lambda: len(self.journal) >= count, timeout=2)


@pytest.fixture
def host():
    clock, publications = Clock(), Publications()
    authority = Authority(clock=clock, wall_clock=clock)

    def prohibited(*args, **kwargs):
        raise AssertionError("The monitor must not contact devices or initialize commands.")

    service = SimpleNamespace(
        demo=False,
        events=publications,
        agent_stream=publications,
        console=SimpleNamespace(stop=prohibited, take=prohibited, release_all=prohibited),
    )
    value = ControlHost(service, authority=authority)
    value.commands = prohibited
    assert value._monitor is None
    assert value.authority() is authority
    yield value, authority, clock, publications
    value.close()
    assert value._monitor_stop.is_set() and not value._monitor.is_alive()


def connect(authority):
    invitation = authority.create_invitation(authority.human)["invitation"]
    paired = authority.pair(invitation, "Test agent")
    reply = authority.connect(paired["credential"])
    return paired, authority.authenticate(reply["session_token"])


def test_grant_without_commands_publishes_timeout_once_without_http(host):
    owner, authority, clock, publications = host
    _, agent = connect(authority)
    authority.grant(authority.human, agent.id)
    assert not publications.stream and not publications.journal
    clock.now = SESSION_TIMEOUT_S + 1
    assert publications.wait()
    assert owner._commands is None
    assert publications.stream[0][0] == "agent.connection_lost"
    assert publications.journal[0][0] == "warning"
    warning = publications.stream[0][1]
    assert warning["sender"]["id"] == agent.id
    assert warning["reason"] == "connection_timeout"
    assert authority.snapshot()["operator"] is None
    assert not authority.snapshot()["watchdog"]["shutdown_enabled"]
    owner.reconcile_commands()
    owner._publish_warning()
    assert len(publications.stream) == len(publications.journal) == 1


def test_human_acknowledges_warning_and_a_new_timeout_publishes_again(host):
    owner, authority, clock, publications = host
    paired, agent = connect(authority)
    authority.grant(authority.human, agent.id)
    clock.now = SESSION_TIMEOUT_S + 1
    assert publications.wait()
    with pytest.raises(ServiceError):
        authority.acknowledge_warning(agent)
    authority.acknowledge_warning(authority.human)
    assert authority.snapshot()["watchdog"]["active"] is None
    owner._publish_warning()
    assert len(publications.stream) == 1
    reply = authority.connect(paired["credential"])
    agent = authority.authenticate(reply["session_token"])
    authority.grant(authority.human, agent.id)
    clock.now += SESSION_TIMEOUT_S + 1
    assert publications.wait(2)
    assert publications.stream[0][1]["event_id"] != publications.stream[1][1]["event_id"]
    assert owner._commands is None


def test_observer_timeout_does_not_warn_and_close_joins_the_monitor(host):
    owner, authority, clock, publications = host
    connect(authority)
    clock.now = SESSION_TIMEOUT_S + 1
    owner._publish_warning()
    assert authority.snapshot(authority.human)["sessions"] == []
    assert publications.stream == publications.journal == []
    owner.close()
    assert not owner._monitor.is_alive()
    # Repeated lifecycle calls must not restart the monitor.
    assert owner.authority() is authority
    assert not owner._monitor.is_alive()
