"""Opt-in fixtures that several test modules share."""

import pytest


@pytest.fixture
def isolated_controller_locks(monkeypatch, tmp_path):
    """Keep controller lock files in the test's own directory."""
    monkeypatch.setenv("MG400_CONTROLLER_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture
def closed_clients():
    """Close the WebSocket test clients that a test leaves open."""
    from panel_ws import OPEN_CLIENTS

    yield
    while OPEN_CLIENTS:
        OPEN_CLIENTS.pop().discard()


@pytest.fixture
def make_sim_rig(monkeypatch, tmp_path):
    """Return a factory of panel rigs that control the simulated MG400 of sim_mg400."""
    from sim_mg400 import rig_factory

    yield from rig_factory(monkeypatch, tmp_path)
