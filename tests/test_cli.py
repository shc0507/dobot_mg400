import json
import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mg400_agent.cli import main


@pytest.fixture(autouse=True)
def isolated_api_environment(monkeypatch):
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_MODE"):
        monkeypatch.delenv(name, raising=False)


def test_demo_completes_without_api_key(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(["demo"]) == 0
    output = capsys.readouterr().out
    assert '"backend": "sim"' in output
    assert '"holding": null' in output
    assert '"occupied_locations": [' in output


def test_invalid_destination_rejected_before_pick(capsys):
    assert main(["demo", "--destination", "missing"]) == 1
    result = capsys.readouterr()
    assert result.out == ""
    assert "Unknown object or destination" in result.err


def test_hardware_backend_fails_explicitly_without_connecting(capsys):
    assert main(["--backend", "mg400", "--host", "192.0.2.1", "demo"]) == 1
    assert "not implemented" in capsys.readouterr().err


def test_ask_requires_explicit_model(monkeypatch, capsys):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert main(["ask", "抓取积木"]) == 1
    assert "OPENAI_MODEL" in capsys.readouterr().err


@pytest.fixture
def offline_mujoco(monkeypatch):
    pytest.importorskip("mujoco")

    def forbidden(*args, **kwargs):
        pytest.fail("MuJoCo CLI must not connect to hardware, network, or a headless viewer")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("mg400_agent.cli.connect_mg400", forbidden)
    monkeypatch.setitem(sys.modules, "mujoco.viewer", None)


def model_reply(name=None, arguments=None, call_id="call", text=""):
    output = (
        [
            SimpleNamespace(
                type="function_call", name=name, arguments=json.dumps(arguments), call_id=call_id
            )
        ]
        if name
        else [SimpleNamespace(type="message", role="assistant", content=text)]
    )
    return SimpleNamespace(output=output, output_text=text, status="completed", error=None)


def install_model(monkeypatch, replies, api_mode="responses"):
    if api_mode == "chat_completions":
        converted = []
        for reply in replies:
            tools = [
                SimpleNamespace(
                    id=item.call_id,
                    type="function",
                    function=SimpleNamespace(name=item.name, arguments=item.arguments),
                )
                for item in reply.output
                if item.type == "function_call"
            ]
            converted.append(
                SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            finish_reason="tool_calls" if tools else "stop",
                            message=SimpleNamespace(
                                content=reply.output_text or None, tool_calls=tools
                            ),
                        )
                    ]
                )
            )
        replies = converted
    create = Mock(side_effect=replies)
    client = SimpleNamespace(
        responses=SimpleNamespace(create=create),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=Mock(return_value=client)))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-sent")
    monkeypatch.setenv("OPENAI_API_MODE", api_mode)
    return client


def feedback(client, call_id):
    for request in client.responses.create.call_args_list:
        for item in request.kwargs.get("input", request.kwargs.get("messages", [])):
            if isinstance(item, dict):
                if item.get("call_id") == call_id:
                    return json.loads(item["output"])
                if item.get("tool_call_id") == call_id:
                    return json.loads(item["content"])
    pytest.fail(f"Tool feedback for {call_id} was never returned to the model")


def test_mujoco_demo_runs_actual_joint_motion_and_home_without_api(
    offline_mujoco, monkeypatch, capsys
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "openai", None)

    assert main(["--backend", "mujoco", "demo"]) == 0

    lines = capsys.readouterr().out.splitlines()
    move, home = map(json.loads, lines[:2])
    assert move["tool"] == "move_joint" and move["ok"]
    assert move["motion"]["physics_steps"] > 1
    assert move["state"]["joints_deg"][0] == pytest.approx(20, abs=0.25)
    assert home["tool"] == "home" and home["ok"]
    assert home["state"]["joints_deg"] == pytest.approx([0] * 4, abs=0.25)
    assert home["state"]["simulation_time_s"] > move["state"]["simulation_time_s"]
    result = json.loads("\n".join(lines[2:]))
    assert result["backend"] == "mujoco"
    assert result["state"]["target_joints_deg"] == [0] * 4


def test_mujoco_state_does_not_require_pick_place_config(offline_mujoco, tmp_path, capsys):
    assert main(["--backend", "mujoco", "--config", str(tmp_path / "missing.toml"), "state"]) == 0
    state = json.loads(capsys.readouterr().out)["state"]
    assert state["joints_deg"] == [0] * 4
    assert state["simulation_time_s"] == 0
    assert state["gripper"]["fixed_visual_only"]


@pytest.mark.parametrize("api_mode", ["responses", "chat_completions"])
def test_mujoco_ask_executes_model_tools_against_actual_physics(
    offline_mujoco, monkeypatch, capsys, api_mode
):
    client = install_model(
        monkeypatch,
        [
            model_reply("get_robot_state", {}, "initial"),
            model_reply("move_joint", {"joint": "J1", "angle_deg": 20, "relative": False}, "move"),
            model_reply("home", {}, "home"),
            model_reply(text="第一轴已转动并回到初始姿态。"),
        ],
        api_mode,
    )

    assert (
        main(["--backend", "mujoco", "ask", "第一轴转到20度，再回初始姿态", "--model", "test"]) == 0
    )

    assert feedback(client, "initial")["state"]["simulation_time_s"] == 0
    moved = feedback(client, "move")["state"]
    home = feedback(client, "home")["state"]
    assert moved["joints_deg"][0] == pytest.approx(20, abs=0.25)
    assert home["joints_deg"] == pytest.approx([0] * 4, abs=0.25)
    assert home["simulation_time_s"] > moved["simulation_time_s"]
    assert "第一轴已转动" in capsys.readouterr().out


@pytest.mark.parametrize("api_mode", ["responses", "chat_completions"])
def test_mujoco_chat_retains_robot_state_and_model_history_between_turns(
    offline_mujoco, monkeypatch, capsys, api_mode
):
    client = install_model(
        monkeypatch,
        [
            model_reply("move_joint", {"joint": "J1", "angle_deg": 20, "relative": False}, "move"),
            model_reply(text="第一轴已转到20度。"),
            model_reply("get_robot_state", {}, "second_turn_state"),
            model_reply("home", {}, "home"),
            model_reply(text="已回到初始姿态。"),
        ],
        api_mode,
    )
    tasks = iter(["第一轴转到20度", "回到初始姿态", "quit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(tasks))

    assert main(["--backend", "mujoco", "chat", "--model", "test"]) == 0

    moved = feedback(client, "move")["state"]
    second = feedback(client, "second_turn_state")["state"]
    assert second["joints_deg"] == moved["joints_deg"]
    assert second["simulation_time_s"] == moved["simulation_time_s"]
    assert feedback(client, "home")["state"]["joints_deg"] == pytest.approx([0] * 4, abs=0.25)
    request = client.responses.create.call_args_list[2].kwargs
    second_turn_input = request.get("input", request.get("messages", [])[1:])
    assert second_turn_input[0] == {"role": "user", "content": "第一轴转到20度"}
    if api_mode == "responses":
        assert second_turn_input[-2].type == "message"
    else:
        assert second_turn_input[-2]["role"] == "assistant"
    assert second_turn_input[-1] == {"role": "user", "content": "回到初始姿态"}
    assert client.responses.create.call_count == 5
    assert "已回到初始姿态" in capsys.readouterr().out


@pytest.mark.parametrize("exit_input", ["quit", "EXIT", "退出", EOFError()])
def test_mujoco_chat_exit_and_eof_do_not_call_api(offline_mujoco, monkeypatch, exit_input):
    client = install_model(monkeypatch, [])
    monkeypatch.setattr("builtins.input", Mock(side_effect=["  ", exit_input]))

    assert main(["--backend", "mujoco", "chat", "--model", "test"]) == 0

    client.responses.create.assert_not_called()


@pytest.mark.parametrize(
    "options,error",
    [
        (["--backend", "sim", "--viewer", "state"], "--viewer requires"),
        (["chat", "--model", "test"], "chat requires"),
        (["--backend", "mujoco", "--viewer", "ask", "home"], "OPENAI_MODEL"),
        (["--backend", "mujoco", "--viewer", "chat", "--model", "test"], "OPENAI_API_KEY"),
        (["--backend", "mujoco", "--viewer", "demo", "--object", "custom"], "joint/home"),
    ],
)
def test_invalid_agent_options_are_rejected_before_starting_viewer(
    monkeypatch, capsys, options, error
):
    session = Mock()
    monkeypatch.setitem(sys.modules, "mg400_agent.mujoco_session", session)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)

    assert main(options) == 1

    session.run_session.assert_not_called()
    assert error in capsys.readouterr().err


@pytest.mark.parametrize(
    "api_mode,base_url",
    [
        ("responses", None),
        ("responses", "http://lab.example.invalid/v1"),
        ("chat_completions", None),
        ("chat_completions", ""),
    ],
)
def test_missing_key_requires_explicit_custom_chat_endpoint_before_viewer(
    monkeypatch, capsys, api_mode, base_url
):
    session = Mock()
    monkeypatch.setitem(sys.modules, "mg400_agent.mujoco_session", session)
    if base_url is not None:
        monkeypatch.setenv("OPENAI_BASE_URL", base_url)

    assert (
        main(["--backend", "mujoco", "--viewer", "chat", "--model", "test", "--api-mode", api_mode])
        == 1
    )

    session.run_session.assert_not_called()
    output = capsys.readouterr()
    assert "OPENAI_API_KEY" in output.err
    assert output.out == ""


@pytest.mark.parametrize("command", [["ask", "Go home"], ["chat"]])
def test_custom_chat_endpoint_passes_viewer_validation_without_key(monkeypatch, capsys, command):
    session = SimpleNamespace(run_session=Mock(return_value=0))
    monkeypatch.setitem(sys.modules, "mg400_agent.mujoco_session", session)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://lab.example.invalid/v1")

    assert (
        main(
            [
                "--backend",
                "mujoco",
                "--viewer",
                *command,
                "--model",
                "test",
                "--api-mode",
                "chat_completions",
            ]
        )
        == 0
    )

    args, _ = session.run_session.call_args.args
    assert args.api_mode == "chat_completions" and args.viewer
    output = capsys.readouterr()
    assert output.out == output.err == ""
