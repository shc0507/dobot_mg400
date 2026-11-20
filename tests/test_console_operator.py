"""Console reads of service routes outside the contract."""

from test_console import headers, no_network, post  # noqa: F401
from test_console import rig as _rig

rig = _rig


def test_stage_reference_is_a_read_without_a_lease(rig):
    stage = rig.devices["stage"]
    stage.replies["api/motors/insert/reference"] = {
        "image": "data:image/jpeg;base64,AAAA",
        "offset_px": [1, -2],
        "distance_px": 2.2,
        "pixel": [5, 6],
    }
    view = rig.client.get("/lab/api/console/ws/motion/stage/reference/insert", headers=headers())
    assert view.json == {
        "image": "data:image/jpeg;base64,AAAA",
        "offset_px": [1, -2],
        "distance_px": 2.2,
    }
    bad = rig.client.get("/lab/api/console/ws/motion/stage/reference/..%2Fx", headers=headers())
    assert bad.status_code in (400, 404)
    assert stage.holder is None
    requests = [call for call in stage.calls if call[0] == "request"]
    assert all(call[3] is None for call in requests)


TURRET = "ws/process/turret/"


def test_turret_action_uses_the_page_lease_once_on_the_real_console(rig):
    microscope = rig.devices["microscope"]
    state = {"session": {"active": True}, "draft": None, "profiles": []}
    microscope.replies["api/turret-calibration/begin"] = state
    assert post(rig, TURRET + "begin", {"name": "x"}).status_code == 409
    assert not [call for call in microscope.calls if call[0] == "request"]
    assert post(rig, "control/microscope/take").status_code == 200
    holder = microscope.holder
    response = post(rig, TURRET + "begin", {"name": "x", "new": True})
    assert response.status_code == 200
    assert response.json["session"]["active"] is True
    assert response.json["has_control"] is True
    requests = [call for call in microscope.calls if call[0] == "request"]
    assert requests == [
        ("request", holder, "api/turret-calibration/begin", {"name": "x", "new": True})
    ]
    other = post(rig, TURRET + "cancel", page="console-page-b")
    assert other.status_code == 409


def test_turret_action_reports_refused_and_unknown_without_retry(rig):
    from labkit.errors import ServiceError

    microscope = rig.devices["microscope"]
    assert post(rig, "control/microscope/take").status_code == 200
    microscope.fail["request"] = ServiceError(409, "Capture unavailable")
    refused = post(rig, TURRET + "record", {"objective_id": 1})
    assert refused.status_code == 409
    assert refused.json["outcome"] == "refused"
    assert "Capture unavailable" in refused.json["error"]
    microscope.fail["request"] = TimeoutError()
    unknown = post(rig, TURRET + "select", {"id": "a" * 32})
    assert unknown.status_code == 502
    assert unknown.json["outcome"] == "unknown"
    assert len([call for call in microscope.calls if call[0] == "request"]) == 2
