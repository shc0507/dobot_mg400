"""Operator-only actions that moved into the Lab Console from the old service pages."""

import pytest
from test_console import headers, no_network, post  # noqa: F401
from test_console import rig as _rig

from labkit.errors import ServiceError

rig = _rig


def take(rig, device, page="console-page-a"):
    response = post(rig, f"control/{device}/take", page=page)
    assert response.status_code == 200, response.json


def operator_calls(rig, device):
    return [call for call in rig.devices[device].calls if call[0] == "request"]


@pytest.mark.parametrize(
    "route,device,path,data",
    [
        ("process/acknowledge", "furnace", "api/furnace/acknowledge", {}),
    ],
)
def test_process_operator_action_needs_confirmation_and_control_and_is_sent_once(
    rig, route, device, path, data
):
    assert post(rig, "ws/" + route, {}).status_code == 400
    assert post(rig, "ws/" + route, {"confirmed": False}).status_code == 400
    refused = post(rig, "ws/" + route, {"confirmed": True})
    assert refused.status_code == 409 and "Take control" in refused.json["error"]
    assert operator_calls(rig, device) == []
    take(rig, device)
    assert post(rig, "ws/" + route, {"confirmed": True}, page="console-page-b").status_code == 409
    response = post(rig, "ws/" + route, {"confirmed": True})
    assert response.status_code == 202
    assert response.json == {"outcome": "accepted", "command_id": "op-1"}
    holder = rig.devices[device].holder
    assert operator_calls(rig, device) == [("request", holder, path, data)]


def test_operator_refusal_and_unknown_outcome_are_reported_without_retry(rig):
    take(rig, "furnace")
    furnace = rig.devices["furnace"]
    furnace.fail["request"] = ServiceError(409, "No fault to acknowledge")
    response = post(rig, "ws/process/acknowledge", {"confirmed": True})
    assert response.status_code == 409 and response.json["outcome"] == "refused"
    assert "No fault" in response.json["error"]
    furnace.fail["request"] = ServiceError(0, "Service request failed")
    response = post(rig, "ws/process/acknowledge", {"confirmed": True})
    assert response.status_code == 502 and response.json["outcome"] == "unknown"
    assert len(operator_calls(rig, "furnace")) == 2


def test_relay_never_forwards_acknowledge(rig):
    for device, action in (("furnace", "acknowledge"),):
        take(rig, device)
        response = post(rig, f"command/{device}", {"action": action, "params": {}})
        assert response.status_code in (400, 409)
        assert not any(
            call[0] == "command" and call[2] == action for call in rig.devices[device].calls
        )


def test_stage_ports_and_reference_are_reads_without_a_lease(rig):
    stage = rig.devices["stage"]
    stage.replies["api/ports"] = {
        "ports": [{"device": "/dev/ttyACM0", "description": "Arduino"}, {"bad": 1}, "x"]
    }
    stage.replies["api/motors/insert/reference"] = {
        "image": "data:image/jpeg;base64,AAAA",
        "offset_px": [1, -2],
        "distance_px": 2.2,
        "pixel": [5, 6],
    }
    ports = rig.client.get("/lab/api/console/ws/motion/stage/ports", headers=headers())
    assert ports.json == {"ports": [{"device": "/dev/ttyACM0", "description": "Arduino"}]}
    view = rig.client.get("/lab/api/console/ws/motion/stage/reference/insert", headers=headers())
    assert view.json == {
        "image": "data:image/jpeg;base64,AAAA",
        "offset_px": [1, -2],
        "distance_px": 2.2,
    }
    bad = rig.client.get("/lab/api/console/ws/motion/stage/reference/..%2Fx", headers=headers())
    assert bad.status_code in (400, 404)
    assert stage.holder is None
    assert all(call[3] is None for call in operator_calls(rig, "stage"))


def test_stage_connect_and_teach_need_this_pages_stage_control(rig):
    assert post(rig, "ws/motion/stage/connect", {"port": "/dev/ttyACM0"}).status_code == 409
    assert (
        post(rig, "ws/motion/stage/teach", {"motor": "insert", "confirmed": True}).status_code
        == 409
    )
    take(rig, "stage")
    assert post(rig, "ws/motion/stage/connect", {"port": ""}).status_code == 400
    assert (
        post(rig, "ws/motion/stage/teach", {"motor": "insert", "confirmed": 1}).status_code == 400
    )
    assert post(rig, "ws/motion/stage/connect", {"port": "/dev/ttyACM0"}).status_code == 200
    taught = post(rig, "ws/motion/stage/teach", {"motor": "insert", "confirmed": True})
    assert taught.status_code == 200 and taught.json["outcome"] == "accepted"
    holder = rig.devices["stage"].holder
    assert operator_calls(rig, "stage") == [
        ("request", holder, "api/connect", {"port": "/dev/ttyACM0"}),
        ("request", holder, "api/motors/insert/teach_reference", {"confirm": True}),
    ]
