import pytest

from mg400_agent.controller_protocol import parse_controller_response


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
    ],
)
def test_success_returns_payload_with_existing_whitespace_rules(packet, command, payload):
    assert parse_controller_response(packet, command) == payload


@pytest.mark.parametrize(
    "packet",
    [
        b"0,{},EnableRobot()",
        b"0,{},DisableRobot();",
        b"0,{},enablerobot();",
        b"0,{},EnableRobot();trailing",
        b"bad,{},EnableRobot();",
        b"-1,{},DisableRobot();",
    ],
)
def test_invalid_reply_or_echo_preserves_error_text(packet):
    with pytest.raises(RuntimeError) as error:
        parse_controller_response(packet, "EnableRobot()")
    assert str(error.value) == f"Unexpected controller response: {packet.decode('ascii')}"


@pytest.mark.parametrize("packet", [b"-1,{},EnableRobot();", b" 1,{failed},EnableRobot();\n"])
def test_controller_rejection_preserves_error_text(packet):
    with pytest.raises(RuntimeError) as error:
        parse_controller_response(packet, "EnableRobot()")
    assert str(error.value) == packet.decode("ascii").strip()


def test_non_ascii_reply_preserves_decode_error():
    with pytest.raises(UnicodeDecodeError):
        parse_controller_response(b"0,{\xff},EnableRobot();", "EnableRobot()")
