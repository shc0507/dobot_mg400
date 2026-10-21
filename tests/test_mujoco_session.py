from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mg400_agent.agent import run_agent
from mg400_agent.mujoco_session import _run_commands, run_session, wait_with_viewer


def test_viewer_remains_responsive_during_console_input(monkeypatch):
    entered, release = Event(), Event()
    main_thread = get_ident()
    sync_calls = []

    def console_input(prompt):
        assert get_ident() != main_thread
        entered.set()
        assert release.wait(timeout=2)
        return "quit"

    def sync():
        assert entered.wait(timeout=2)
        sync_calls.append(get_ident())
        if len(sync_calls) == 3:
            release.set()

    viewer = SimpleNamespace(is_running=lambda: True, sync=sync)
    executor = Mock(stopped=False, state=Mock(return_value={}))
    agent = Mock(side_effect=AssertionError("Quitting must not invoke the API"))
    monkeypatch.setattr("builtins.input", console_input)
    monkeypatch.setattr("mg400_agent.mujoco_session.run_agent", agent)

    assert _run_commands(SimpleNamespace(command="chat", model="test"), executor, viewer) == 0

    assert len(sync_calls) >= 3
    assert set(sync_calls) == {main_thread}
    executor.execute.assert_not_called()
    agent.assert_not_called()


def test_api_wait_keeps_syncing_and_returns_worker_result():
    release = Event()
    sync_calls = []

    def request():
        assert release.wait(timeout=2)
        return "API response"

    def sync():
        sync_calls.append(True)
        if len(sync_calls) == 3:
            release.set()

    viewer = SimpleNamespace(is_running=lambda: True, sync=sync)

    assert wait_with_viewer(request, viewer) == "API response"
    assert len(sync_calls) >= 3


@pytest.mark.parametrize("error", [ConnectionError("API failed"), EOFError(), KeyboardInterrupt()])
def test_worker_errors_propagate_to_session(error):
    def fail():
        raise error

    viewer = SimpleNamespace(is_running=lambda: True, sync=lambda: None)
    with pytest.raises(type(error)) as caught:
        wait_with_viewer(fail, viewer)
    assert caught.value is error


def test_window_close_discards_late_api_tool_call_without_execution():
    entered, release, finished = Event(), Event(), Event()
    running = True

    def request(**kwargs):
        entered.set()
        assert release.wait(timeout=2)
        result = SimpleNamespace(
            status="completed",
            error=None,
            output=[
                SimpleNamespace(
                    type="function_call", name="home", arguments="{}", call_id="late_home"
                )
            ],
        )
        finished.set()
        return result

    def sync():
        nonlocal running
        assert entered.wait(timeout=2)
        running = False

    client = SimpleNamespace(responses=SimpleNamespace(create=request))
    executor = Mock(tool_schemas=Mock(return_value=[]), stop=Mock(return_value={"ok": True}))
    viewer = SimpleNamespace(is_running=lambda: running, sync=sync)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_agent(
                "home",
                executor,
                model="test",
                client=client,
                request_runner=lambda callback: wait_with_viewer(callback, viewer),
            )
    finally:
        release.set()
        assert finished.wait(timeout=2)

    executor.stop.assert_not_called()
    executor.execute.assert_not_called()


def test_already_closed_viewer_does_not_start_callback():
    callback = Mock()
    viewer = SimpleNamespace(is_running=lambda: False)
    with pytest.raises(KeyboardInterrupt):
        wait_with_viewer(callback, viewer)
    callback.assert_not_called()


def test_chat_planning_failure_allows_next_input(monkeypatch, capsys):
    inputs = iter(["first request", "corrected request", "quit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    executor = Mock(stopped=False, state=Mock(return_value={}))
    agent = Mock(side_effect=[ConnectionError("API unavailable"), "Completed."])
    monkeypatch.setattr("mg400_agent.mujoco_session.run_agent", agent)
    args = SimpleNamespace(command="chat", model="test", api_mode="responses")
    assert _run_commands(args, executor, None) == 0
    assert agent.call_count == 2
    executor.stop.assert_not_called()
    output = capsys.readouterr()
    assert "API unavailable" in output.err
    assert "Completed." in output.out


def test_chat_device_failure_ends_session_without_next_input(monkeypatch):
    read = Mock(return_value="move")
    monkeypatch.setattr("builtins.input", read)
    executor = Mock(stopped=False)

    def fail(*args, **kwargs):
        executor.stopped = True
        raise RuntimeError("Device motion failed")

    monkeypatch.setattr("mg400_agent.mujoco_session.run_agent", fail)
    args = SimpleNamespace(command="chat", model="test", api_mode="responses")
    with pytest.raises(RuntimeError, match="Device motion failed"):
        _run_commands(args, executor, None)
    read.assert_called_once()


@pytest.mark.parametrize("error", [ConnectionError("API unavailable"), KeyboardInterrupt()])
def test_session_always_stops_executor_on_error(monkeypatch, error):
    executor = Mock()
    monkeypatch.setattr("mg400_agent.mujoco_session.MuJoCoJointExecutor", lambda: executor)
    monkeypatch.setattr("mg400_agent.mujoco_session.run_agent", Mock(side_effect=error))
    args = SimpleNamespace(
        command="ask", task="home", model="test", viewer=False, api_mode="responses"
    )

    with pytest.raises(type(error)):
        run_session(args, [])

    executor.stop.assert_called_once()


def test_macos_session_relaunches_agent_module_before_constructing_model(monkeypatch):
    launcher = Mock(side_effect=SystemExit(0))
    factory = Mock()
    monkeypatch.setattr("mg400_agent.mujoco_session.sys.platform", "darwin")
    monkeypatch.delenv("MJPYTHON_BIN", raising=False)
    monkeypatch.setattr("mg400_agent.mujoco_session.launch_macos_viewer", launcher)
    monkeypatch.setattr("mg400_agent.mujoco_session.MuJoCoJointExecutor", factory)
    argv = ["--backend", "mujoco", "--viewer", "chat", "--model", "test"]

    with pytest.raises(SystemExit):
        run_session(SimpleNamespace(viewer=True), argv)

    launcher.assert_called_once_with(argv, module="mg400_agent")
    factory.assert_not_called()
