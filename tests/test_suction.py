import pytest

from mg400 import suction

CLIENT = "browser-a"

pytestmark = pytest.mark.usefixtures("isolated_controller_locks")


def vacuum(rig, operation):
    return rig.state.command({"action": "vacuum", "operation": operation}, CLIENT)


def test_steps_never_set_both_outputs_high():
    for steps in suction.STEPS.values():
        levels = {suction.PRESSURE_DO: 0, suction.VACUUM_DO: 0}
        for step in steps:
            if step[0] != "wait":
                levels[step[0]] = step[1]
                assert not all(levels.values())
        assert levels[suction.PRESSURE_DO] == 0


@pytest.mark.parametrize(
    ("index", "status"), [(0, 1), (17, 0), (1, 2), (True, 1), ("1", 0), (1.0, 1)]
)
def test_command_text_rejects_invalid_outputs(index, status):
    with pytest.raises(ValueError):
        suction.command_text(index, status)


def test_decode_state_reads_measured_bits():
    assert suction.decode_state(None, None) == {"suck": None, "blow": None, "held": None}
    assert suction.decode_state(0b10, 0b1) == {"suck": True, "blow": False, "held": None}
    assert suction.decode_state(0b01, 0) == {"suck": False, "blow": True, "held": None}


@pytest.mark.parametrize("mode", [4, 5])
def test_vacuum_actions_send_ordered_dashboard_outputs(make_sim_rig, mode):
    rig = make_sim_rig()
    rig.controller.update(mode=mode)
    rig.command("start_sync")
    rig.wait(lambda s: s["feedback_fresh"] and s["robot_mode"] == mode)
    expected = {
        "suck": (["DOExecute(1,0)", "DOExecute(2,1)"], {"suck": True, "blow": False}),
        "release": (
            ["DOExecute(2,0)", "DOExecute(1,1)", "DOExecute(1,0)"],
            {"suck": False, "blow": False},
        ),
        "off": (["DOExecute(2,0)", "DOExecute(1,0)"], {"suck": False, "blow": False}),
    }
    for operation in ("suck", "release", "suck", "off"):
        mark = rig.controller.mark()
        vacuum(rig, operation)
        rig.wait(lambda s: not s["busy"] and s["last_command"]["action"] == "vacuum")
        assert rig.snapshot()["last_command"]["result"] == "ok", rig.describe()
        sent, state = expected[operation]
        assert [t for t in rig.controller.sent(mark) if t.startswith("DO")] == sent
        assert rig.snapshot()["vacuum"] == {**state, "held": None}
    assert rig.controller.motion() == []
    assert not rig.snapshot()["locked"]


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "vacuum"},
        {"action": "vacuum", "operation": "on"},
        {"action": "vacuum", "operation": 1},
        {"action": "vacuum", "operation": "suck", "extra": True},
    ],
)
def test_invalid_vacuum_request_sends_nothing(make_sim_rig, payload):
    rig = make_sim_rig()
    rig.connect()
    mark = rig.controller.mark()
    with pytest.raises(ValueError):
        rig.state.command(payload, CLIENT)
    assert rig.controller.sent(mark) == []


def test_vacuum_rejects_moving_robot(make_sim_rig):
    rig = make_sim_rig()
    rig.connect()
    rig.controller.update(mode=7)
    rig.wait(lambda s: s["robot_mode"] == 7)
    mark = rig.controller.mark()
    with pytest.raises(RuntimeError, match="must be idle"):
        vacuum(rig, "suck")
    assert rig.controller.sent(mark) == []


def test_vacuum_requires_the_lease_owner(make_sim_rig):
    rig = make_sim_rig()
    rig.connect()
    with pytest.raises(PermissionError):
        rig.state.command({"action": "vacuum", "operation": "suck"}, "browser-b")


def test_stop_sends_no_output_command(make_sim_rig):
    rig = make_sim_rig()
    rig.connect()
    vacuum(rig, "suck")
    rig.wait(lambda s: not s["busy"] and s["vacuum"]["suck"])
    mark = rig.controller.mark()
    rig.command("stop")
    rig.wait(lambda s: not s["busy"] and s["last_command"]["action"] == "stop")
    assert not any(t.startswith("DO") for t in rig.controller.sent(mark))
    assert rig.snapshot()["vacuum"]["suck"]


def test_describe_lists_the_vacuum_command(make_sim_rig):
    rig = make_sim_rig()
    schema = rig.state.describe()["commands"]["vacuum"]
    assert "suck" in str(schema) and "release" in str(schema)
