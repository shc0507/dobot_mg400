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
