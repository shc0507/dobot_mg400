"""Turret calibration proxy keeps the Console's page-to-device lease mapping."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask import Blueprint, Flask, jsonify, request

from labkit.errors import ServiceError
from orchestrator.web.turret import MAX_PROFILE_BYTES, register

HEADERS = {"X-MG400-Client": "page-one"}
ROOT = "/lab/api/console/ws/process/turret"


@pytest.fixture
def rig():
    calls = []
    state = {
        "session": {"active": True, "client": "microscope-holder"},
        "draft": None,
        "profiles": [],
        "selected_profile": None,
    }
    outcome = {"outcome": "accepted", "result": state}

    def read(device, path):
        calls.append(("read", device, path))
        return state

    def operator(device, page, path, data):
        calls.append(("operator", device, page, path, data))
        return outcome

    console = SimpleNamespace(
        read=read,
        operator=operator,
        control=lambda page: [
            {
                "device": "microscope",
                "holder": "microscope-holder",
                "mine": page == "page-one",
                "state": "held",
            }
        ],
    )
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 65536
    bp = Blueprint("test_process", __name__, url_prefix="/lab/api/console/ws/process")
    register(bp, SimpleNamespace(console=console))
    app.register_blueprint(bp)

    @app.before_request
    def parse_like_console():
        if request.method == "POST":
            request.get_json()

    @app.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=error.message), error.status

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    return SimpleNamespace(client=app.test_client(), calls=calls, state=state, outcome=outcome)


def test_reads_map_service_holder_to_page_without_mutating_shared_state(rig):
    response = rig.client.get(ROOT, headers=HEADERS)
    assert response.status_code == 200
    assert response.json["session"]["mine"] is True
    assert response.json["has_control"] is True
    other = rig.client.get(ROOT, headers={"X-MG400-Client": "another-page"})
    assert other.json["session"]["mine"] is False
    assert other.json["has_control"] is False
    assert "mine" not in rig.state["session"]
    assert all(call[0] == "read" for call in rig.calls)


def test_record_uses_operator_lease_once_and_returns_unwrapped_state(rig):
    response = rig.client.post(ROOT + "/record", json={"objective_id": 2}, headers=HEADERS)
    assert response.status_code == 200
    assert response.json["session"]["mine"] is True
    assert rig.calls == [
        ("operator", "microscope", "page-one", "api/turret-calibration/record", {"objective_id": 2})
    ]


@pytest.mark.parametrize("outcome,code", [("refused", 409), ("unknown", 502)])
def test_failed_operator_request_is_not_retried_or_reported_successful(rig, outcome, code):
    rig.outcome.clear()
    rig.outcome.update(outcome=outcome, error="Capture unavailable")
    response = rig.client.post(ROOT + "/record", json={"objective_id": 0}, headers=HEADERS)
    assert response.status_code == code
    assert response.json["error"] == "Capture unavailable"
    assert len(rig.calls) == 1


@pytest.mark.parametrize(
    "action,payload,code",
    [("unknown", {}, 404), ("begin", {"move_focus": True}, 400), ("upload", {"profile": {}}, 400)],
)
def test_invalid_actions_do_not_reach_the_microscope(rig, action, payload, code):
    response = rig.client.post(ROOT + "/" + action, json=payload, headers=HEADERS)
    assert response.status_code == code
    assert rig.calls == []


def test_only_upload_can_exceed_normal_body_limit(rig):
    profile = " " * 70000
    response = rig.client.post(ROOT + "/upload", json={"profile": profile}, headers=HEADERS)
    assert response.status_code == 200
    assert rig.calls[-1][-1] == {"profile": profile}
    response = rig.client.post(ROOT + "/save", json={"name": profile}, headers=HEADERS)
    assert response.status_code == 413
    assert len(rig.calls) == 1


def test_profile_byte_limit_applies_after_json_decoding(rig):
    response = rig.client.post(
        ROOT + "/upload", json={"profile": "a" * (MAX_PROFILE_BYTES + 1)}, headers=HEADERS
    )
    assert response.status_code == 413
    assert rig.calls == []


def test_download_accepts_only_profile_identifiers(rig):
    identifier = uuid4().hex
    assert rig.client.get(ROOT + "/profile/" + identifier, headers=HEADERS).status_code == 200
    assert rig.calls == [("read", "microscope", "api/turret-calibration/profile/" + identifier)]
    assert rig.client.get(ROOT + "/profile/not-a-profile", headers=HEADERS).status_code == 400
    assert len(rig.calls) == 1


def test_proxy_does_not_expose_synthetic_camera_on_live_service(rig):
    assert rig.client.get(ROOT + "/frame").status_code == 404
    assert rig.calls == []
