import pytest

from mg400.controller_protocol import ControllerCommandError, parse_controller_response

CALC_USER = "CalcUser(9,1,{0,0,0,0})"


@pytest.mark.parametrize(
    "packet, command, payload",
    [
        (b"0,{},EnableRobot();", "EnableRobot()", ""),
        (b"0,{1,2,3,4,0,0},GetAngle();", "GetAngle()", "1,2,3,4,0,0"),
        (
            bytearray(b" \n0,{ [[], [], []] \t},GetErrorID();\r\n"),
            "GetErrorID()",
            "[[], [], []]",
        ),
        (
            b"0,{},JointMovJ( 1,\n2, 3, 4, SpeedJ = 5 );",
            " JointMovJ(1,2,3,4,SpeedJ=5)\t",
            "",
        ),
        (b"0,{},1,2,3,4,CalcUser( 9, 1, {0, 0, 0, 0} );", CALC_USER, "1,2,3,4"),
    ],
)
def test_success_returns_payload_with_existing_whitespace_rules(packet, command, payload):
    assert parse_controller_response(packet, command) == payload


@pytest.mark.parametrize(
    "packet, command",
    [
        (b"0,{},EnableRobot()", "EnableRobot()"),
        (b"0,{},DisableRobot();", "EnableRobot()"),
        (b"0,{},enablerobot();", "EnableRobot()"),
        (b"0,{},EnableRobot();trailing", "EnableRobot()"),
        (b"bad,{},EnableRobot();", "EnableRobot()"),
        (b"-1,{},DisableRobot();", "EnableRobot()"),
        (b"0,{},1,2,3,4,GetPose(0,0);", "GetPose(0,0)"),
    ],
)
def test_invalid_reply_or_echo_preserves_error_text(packet, command):
    with pytest.raises(RuntimeError) as error:
        parse_controller_response(packet, command)
    assert str(error.value) == f"Unexpected controller response: {packet.decode('ascii')}"
    assert not isinstance(error.value, ControllerCommandError)


@pytest.mark.parametrize(
    "packet, command, code",
    [
        (b"-1,{},EnableRobot();", "EnableRobot()", -1),
        (b" 1,{failed},EnableRobot();\n", "EnableRobot()", 1),
        (
            b"-1,{0,0,0,0},InverseSolution(406.817825,9.647012,112.928917,1.446726,0,0);",
            "InverseSolution(406.817825,9.647012,112.928917,1.446726,0,0)",
            -1,
        ),
        (f"-1,{{}},{CALC_USER};".encode(), CALC_USER, -1),
    ],
)
def test_controller_rejection_preserves_error_text_and_command_details(packet, command, code):
    with pytest.raises(ControllerCommandError) as error:
        parse_controller_response(packet, command)
    assert isinstance(error.value, RuntimeError)
    assert str(error.value) == packet.decode("ascii").strip()
    assert error.value.response == packet.decode("ascii").strip()
    assert error.value.command == command
    assert error.value.code == code


def test_non_ascii_reply_preserves_decode_error():
    with pytest.raises(UnicodeDecodeError):
        parse_controller_response(b"0,{\xff},EnableRobot();", "EnableRobot()")


@pytest.mark.parametrize(
    "body,payload",
    [
        (
            "{},249.668793,-415.842590,107.388802,69.243301",
            "249.668793,-415.842590,107.388802,69.243301",
        ),
        (
            "{249.668793,-415.842590,107.388802,69.243301}",
            "249.668793,-415.842590,107.388802,69.243301",
        ),
        ("{}, +1e2, -2.5, .3, 4. ", "+1e2, -2.5, .3, 4."),
        ("{ +1e2, -2.5, .3, 4. }", "+1e2, -2.5, .3, 4."),
    ],
)
def test_calc_user_accepts_four_finite_coordinates_in_both_firmware_formats(body, payload):
    packet = f"0,{body},{CALC_USER};".encode("ascii")
    assert parse_controller_response(packet, CALC_USER) == payload


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize(
    "values",
    ["", "1,2,3", "1,2,3,4,5", "1,2,3,", "1,2,3,nan", "1,2,3,inf", "1,2,3,1e999", "1,2,3,1_0"],
)
def test_calc_user_rejects_missing_extra_or_nonfinite_coordinates(external, values):
    body = "{}," + values if external else "{" + values + "}"
    with pytest.raises(RuntimeError):
        parse_controller_response(f"0,{body},{CALC_USER};".encode("ascii"), CALC_USER)


@pytest.mark.parametrize(
    "packet",
    [
        f"0,{{1,2,3,4}},5,6,7,8,{CALC_USER};",
        "0,{},1,2,3,4,CalcUser(8,1,{0,0,0,0});",
        "0,{},1,2,3,4,CalcUser(9,0,{0,0,0,0});",
        "0,{},1,2,3,4,CalcUser(9,1,{0,0,0,1});",
        "0,{},1,2,3,4,GetPose(9,1);",
        f"0,{{}},Other();0,{{}},1,2,3,4,{CALC_USER};",
        f"0,{{}},1,2,3,4,{CALC_USER};extra",
        f"0,{{}},1,2,3,4,{CALC_USER}",
    ],
)
def test_calc_user_rejects_mixed_payloads_wrong_echoes_and_extra_frames(packet):
    with pytest.raises(RuntimeError) as error:
        parse_controller_response(packet.encode("ascii"), CALC_USER)
    assert not isinstance(error.value, ControllerCommandError)
