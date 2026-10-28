import pytest

from mg400_agent.controller_alarms import describe_alarms, parse_alarm_ids


def test_parse_reads_one_tuple_per_list():
    assert parse_alarm_ids("[\n\t[72, 85],\n\t[],\n\t[],\n\t[],\n\t[],\n\t[]\n]") == (
        (72, 85),
        (),
        (),
        (),
        (),
        (),
    )


@pytest.mark.parametrize(
    "payload",
    ["", "72", "[[72]]", "[[1.5],[],[],[],[]]", '[["72"],[],[],[],[]]', "[1,[],[],[],[]]"],
)
def test_parse_rejects_malformed_payloads(payload):
    with pytest.raises(RuntimeError, match="Invalid GetErrorID"):
        parse_alarm_ids(payload)


@pytest.mark.parametrize("groups", [6, 7])
def test_describe_maps_the_last_four_lists_to_the_servos(groups):
    ids = [[] for _ in range(groups)]
    ids[0] = [72, -2, 999999]
    ids[-1] = [8]
    alarms = describe_alarms(ids)
    assert [(alarm["id"], alarm["source"]) for alarm in alarms] == [
        (72, "controller"),
        (-2, "controller"),
        (999999, "controller"),
        (8, "J4"),
    ]
    assert alarms[0]["description"] == "Parallelogram positive limit"
    assert alarms[0]["solution"] == "Reverse jog out of limit"
    assert alarms[1]["description"] == "Collision detected"
    assert alarms[2]["description"] == "Unknown alarm"
