"""Local API profile routes never expose keys or initiate model or equipment calls."""

import json

import pytest
from test_agent_connect_model_profiles import FakeProtector

from orchestrator.agent_connect.chat import ChatJob
from orchestrator.agent_connect.chat_model import OpenAIInterpreter
from orchestrator.agent_connect.model_profiles import ModelProfileStore
from orchestrator.agent_connect.portal import create_portal_app

KEY = "test-only-profile-credential"


@pytest.fixture
def profile_app(tmp_path):
    path = tmp_path / "profiles.json"
    store = ModelProfileStore(path, FakeProtector())
    calls = []
    model = OpenAIInterpreter(transport=lambda *args, **kwargs: calls.append("model"), api_key="")
    app = create_portal_app(interpreter=model, model_profiles=store)
    chat = app.extensions["agent_portal_chat"]
    yield app.test_client(), chat, store, path, calls
    if chat.current:
        chat.current.done.set()
    chat.close()
    app.extensions["agent_portal_control"].close()


def add(http, provider="openai"):
    response = http.post(
        "/api/agent-chat/profiles",
        json={
            "name": provider.title(),
            "provider": provider,
            "model": "test-model",
            "base_url": "",
            "api_key": KEY,
        },
    )
    assert response.status_code == 201
    assert KEY not in response.get_data(as_text=True)
    return response.get_json()["profiles"][-1]["id"]


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_save_activate_deactivate_delete_never_sends_a_request(profile_app, provider):
    http, chat, store, path, calls = profile_app
    identifier = add(http, provider)
    assert not chat.model.status()["configured"]
    response = http.post("/api/agent-chat/profiles/activate", json={"profile_id": identifier})
    assert response.status_code == 200
    assert chat.model.status()["provider"] == provider
    assert chat.model.status()["configured"]
    for endpoint in ("profiles", "status"):
        response = http.get("/api/agent-chat/" + endpoint)
        assert response.headers["Cache-Control"] == "no-store"
        assert KEY not in response.get_data(as_text=True)
    assert KEY.encode() not in path.read_bytes()
    assert not calls
    assert (
        http.post("/api/agent-chat/profiles/activate", json={"profile_id": None}).status_code == 200
    )
    assert not chat.model.status()["configured"]
    http.post("/api/agent-chat/profiles/activate", json={"profile_id": identifier})
    assert (
        http.post("/api/agent-chat/profiles/delete", json={"profile_id": identifier}).status_code
        == 200
    )
    assert store.status()["profiles"] == []
    assert not chat.model.status()["configured"] and not calls


def test_restart_loads_selected_profile_without_connection_or_model_call(profile_app):
    http, _, _, path, calls = profile_app
    identifier = add(http, "gemini")
    http.post("/api/agent-chat/profiles/activate", json={"profile_id": identifier})
    model = OpenAIInterpreter(transport=lambda *args, **kwargs: calls.append("restart"), api_key="")
    app = create_portal_app(
        interpreter=model, model_profiles=ModelProfileStore(path, FakeProtector())
    )
    try:
        status = app.test_client().get("/api/agent-chat/status").get_json()
        assert status["model"]["profile_id"] == identifier
        assert status["model"]["configured"] and not status["busy"]
        assert status["adaptation"]["status"] == "ready"
        assert not calls
    finally:
        app.extensions["agent_portal_chat"].close()
        app.extensions["agent_portal_control"].close()


def test_profile_mutations_wait_for_inflight_instruction(profile_app):
    http, chat, store, path, calls = profile_app
    identifier = add(http)
    original = path.read_bytes()
    chat.current = ChatJob("live", "pending", "Move stage")
    for route in ("activate", "delete"):
        response = http.post("/api/agent-chat/profiles/" + route, json={"profile_id": identifier})
        assert response.status_code == 409
    assert path.read_bytes() == original and not calls
    assert store.status()["active_profile_id"] is None


def test_profile_routes_retain_loopback_origin_and_exact_body_checks(profile_app):
    http, _, _, path, _ = profile_app
    response = http.post(
        "/api/agent-chat/profiles", json={}, headers={"Origin": "https://unrelated.invalid"}
    )
    assert response.status_code == 403
    assert http.post("/api/agent-chat/profiles", json={"api_key": KEY}).status_code == 400
    assert not path.exists()


def test_bad_key_and_corrupt_storage_are_reported_without_credentials(profile_app):
    http, _, _, _, _ = profile_app
    response = http.post(
        "/api/agent-chat/profiles",
        json={
            "name": "bad",
            "provider": "openai",
            "model": "test",
            "base_url": "",
            "api_key": KEY + "\n",
        },
    )
    assert response.status_code == 400
    assert KEY not in json.dumps(response.get_json())


def test_token_station_route_is_saved_and_activated_without_api_call(profile_app):
    http, chat, store, path, calls = profile_app
    response = http.post(
        "/api/agent-chat/profiles",
        json={
            "name": "Token Station",
            "route": "token_station",
            "provider": "openai",
            "model": "openai/gpt-6.1-sol",
            "base_url": "",
            "api_key": KEY,
        },
    )
    assert response.status_code == 201
    profile = response.get_json()["profiles"][0]
    assert profile["base_url"] == "https://physics.bytefuture.ai/v1"
    assert profile["route"] == "token_station"
    assert not chat.model.status()["configured"]
    response = http.post("/api/agent-chat/profiles/activate", json={"profile_id": profile["id"]})
    assert response.status_code == 200
    assert chat.model.status()["route"] == "token_station"
    assert chat.model.status()["model"] == "openai/gpt-6.1-sol"
    assert not calls and KEY.encode() not in path.read_bytes()
    assert ModelProfileStore(path, FakeProtector()).active()["route"] == "token_station"
