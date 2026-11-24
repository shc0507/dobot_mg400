"""Check local profile persistence without contacting a model provider."""

import base64
import json
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor

import pytest

from orchestrator.agent_connect import credential_protection, model_profiles
from orchestrator.agent_connect.credential_protection import (
    CredentialProtectionError,
    WindowsCredentialProtector,
)
from orchestrator.agent_connect.model_profiles import (
    DEFAULT_BASE_URLS,
    ModelProfileError,
    ModelProfileStore,
    normalize_base_url,
)

TEST_KEY = "test-profile-secret-which-must-not-be-public"


class FakeProtector:
    """Use a reversible test transform. This is not credential protection."""

    def protect(self, plaintext):
        return b"test:" + bytes(byte ^ 0xA5 for byte in plaintext)

    def unprotect(self, ciphertext):
        if not ciphertext.startswith(b"test:"):
            raise RuntimeError("unreadable " + TEST_KEY)
        return bytes(byte ^ 0xA5 for byte in ciphertext[5:])


@pytest.fixture
def saved_store(tmp_path):
    path = tmp_path / "private" / "model-profiles.json"
    return ModelProfileStore(path, FakeProtector()), path


def add_profile(store, **overrides):
    return store.add(
        **{
            "name": "Personal OpenAI",
            "provider": "openai",
            "model": "gpt-test-model",
            "base_url": "",
            "api_key": TEST_KEY,
            **overrides,
        }
    )


def rewrite_payload(path, mutate):
    wrapper = json.loads(path.read_bytes())
    content = json.loads(FakeProtector().unprotect(base64.b64decode(wrapper["protected"])))
    mutate(content)
    wrapper["protected"] = base64.b64encode(
        FakeProtector().protect(json.dumps(content).encode())
    ).decode()
    path.write_text(json.dumps(wrapper), encoding="utf-8")


def test_profiles_need_explicit_activation_and_survive_restart(saved_store):
    store, path = saved_store
    assert store.status()["storage_available"] is True
    assert not path.parent.exists()
    assert store.active() is None
    first = add_profile(store)["profiles"][0]
    assert first == {
        "id": first["id"],
        "name": "Personal OpenAI",
        "provider": "openai",
        "route": "direct",
        "model": "gpt-test-model",
        "base_url": DEFAULT_BASE_URLS["openai"],
        "has_key": True,
    }
    assert store.active() is None
    assert ModelProfileStore(path, FakeProtector()).active() is None
    store.activate(first["id"])
    second = add_profile(store, name="Anthropic", provider="anthropic", model="claude-test")[
        "profiles"
    ][1]
    assert store.status()["active_profile_id"] == first["id"]
    restarted = ModelProfileStore(path, FakeProtector())
    assert restarted.status() == store.status()
    assert restarted.active()["api_key"] == TEST_KEY
    restarted.activate(second["id"])
    assert restarted.active()["provider"] == "anthropic"
    assert ModelProfileStore(path, FakeProtector()).active()["id"] == second["id"]
    restarted.delete(first["id"])
    assert restarted.active()["id"] == second["id"]
    restarted.delete(second["id"])
    assert restarted.status()["profiles"] == []
    assert restarted.active() is None
    assert ModelProfileStore(path, FakeProtector()).active() is None


def test_disable_profile_persists_without_deleting_it(saved_store):
    store, path = saved_store
    profile_id = add_profile(store)["profiles"][0]["id"]
    store.activate(profile_id)
    status = store.activate(None)
    assert status["active_profile_id"] is None
    assert len(status["profiles"]) == 1
    assert ModelProfileStore(path, FakeProtector()).active() is None


def test_public_status_repr_and_file_do_not_expose_credentials(saved_store):
    store, path = saved_store
    status = add_profile(store)
    store.activate(status["profiles"][0]["id"])
    ciphertext = json.loads(path.read_bytes())["protected"]
    for public in (
        status,
        store.status(),
        repr(store),
        repr(store._profiles),
        repr(store.active()),
    ):
        assert TEST_KEY not in str(public)
        assert ciphertext not in str(public)
    raw = path.read_text(encoding="utf-8")
    assert TEST_KEY not in raw
    assert "Personal OpenAI" not in raw
    assert DEFAULT_BASE_URLS["openai"] not in raw
    assert "api_key" not in json.dumps(store.status())


def test_returned_profiles_cannot_mutate_saved_state(saved_store):
    store, _path = saved_store
    public = add_profile(store)
    profile_id = public["profiles"][0]["id"]
    store.activate(profile_id)
    public["profiles"][0]["name"] = "Changed"
    private = store.active()
    private["api_key"] = "changed-key"
    private["base_url"] = "https://changed.example"
    assert store.active()["api_key"] == TEST_KEY
    assert store.status()["profiles"][0]["name"] == "Personal OpenAI"


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini"])
def test_provider_defaults_and_custom_endpoint_normalization(provider):
    assert normalize_base_url(provider, "") == DEFAULT_BASE_URLS[provider]
    assert normalize_base_url(provider, None) == DEFAULT_BASE_URLS[provider]
    assert normalize_base_url(provider, "HTTPS://Proxy.Example:8443/v1///") == (
        "https://proxy.example:8443/v1"
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:9000/v1/",
        "http://127.0.0.1:9000/",
        "http://127.2.3.4/v1",
        "http://[::1]:9000/v1/",
        "https://example.com/",
        "https://[2001:db8::1]/v1/",
    ],
)
def test_accepts_https_or_loopback_http(endpoint):
    assert normalize_base_url("openai", endpoint) == endpoint.rstrip("/")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com/v1",
        "http://192.168.1.10/v1",
        "http://0.0.0.0/v1",
        "http://127.1/v1",
        "http://localhost.example/v1",
        "http://[::]/v1",
        "ftp://example.com/v1",
        "example.com/v1",
        "//example.com/v1",
        "https:///v1",
        "https://user:secret@example.com/v1",
        "https://user@example.com/v1",
        "https://example.com/v1?key=secret",
        "https://example.com/v1?",
        "https://example.com/v1#fragment",
        "https://example.com/v1#",
        "https://example.com\\@localhost/v1",
        "https://exa mple.com/v1",
        "https://example.com/v1\n",
        "https://example.com:0/v1",
        "https://example.com:65536/v1",
        "https://example.com:/v1",
        "https://[::1%25zone]/v1",
        "https://%65xample.com/v1",
        "https://-example.com/v1",
        "https://[invalid]/v1",
        "https://[::1]junk/v1",
        "https://example.com/" + "a" * 2048,
        42,
    ],
)
def test_rejects_unsafe_endpoints_without_echoing_input(endpoint):
    with pytest.raises(ModelProfileError) as caught:
        normalize_base_url("openai", endpoint)
    assert "secret" not in str(caught.value)
    assert "example.com" not in str(caught.value)


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"name": "x" * 121},
        {"name": "bad\nname"},
        {"name": 42},
        {"provider": "unknown"},
        {"provider": ["openai"]},
        {"model": ""},
        {"model": "models//gemini"},
        {"model": "openai/"},
        {"model": "/openai/model"},
        {"route": "unknown"},
        {"route": None},
        {"model": "gpt-test\n"},
        {"model": "x" * 129},
        {"model": []},
        {"api_key": ""},
        {"api_key": " "},
        {"api_key": "x" * 4097},
        {"api_key": "bad\nkey"},
        {"api_key": "bad key"},
        {"api_key": "non-ascii-\u00e9"},
        {"api_key": None},
    ],
)
def test_invalid_profile_never_creates_storage(saved_store, change):
    store, path = saved_store
    with pytest.raises(ModelProfileError):
        add_profile(store, **change)
    assert store.status()["profiles"] == []
    assert not path.parent.exists()


def test_token_station_route_and_qualified_model_survive_restart(saved_store):
    store, path = saved_store
    profile = add_profile(store, route="token_station", model="openai/gpt-6.1-sol")["profiles"][0]
    assert profile["route"] == "token_station"
    assert profile["base_url"] == "https://physics.bytefuture.ai/v1"
    store.activate(profile["id"])
    restored = ModelProfileStore(path, FakeProtector())
    assert restored.active()["model"] == "openai/gpt-6.1-sol"
    assert restored.active()["route"] == "token_station"
    assert TEST_KEY not in json.dumps(restored.status())


def test_legacy_encrypted_profiles_keep_direct_route_without_eager_rewrite(saved_store):
    store, path = saved_store
    profile = add_profile(store)["profiles"][0]
    store.activate(profile["id"])
    rewrite_payload(path, lambda content: content["profiles"][0].pop("route"))
    original = path.read_bytes()
    restored = ModelProfileStore(path, FakeProtector())
    assert restored.status()["storage_available"]
    assert restored.active()["route"] == "direct"
    assert restored.active()["base_url"] == DEFAULT_BASE_URLS["openai"]
    assert path.read_bytes() == original
    restored.activate(profile["id"])
    assert ModelProfileStore(path, FakeProtector()).active()["route"] == "direct"


@pytest.mark.parametrize("operation", ["add", "activate", "delete", "disable"])
def test_failed_atomic_replace_preserves_original_file_and_memory(
    saved_store, monkeypatch, operation
):
    store, path = saved_store
    profile_id = add_profile(store)["profiles"][0]["id"]
    store.activate(profile_id)
    old_status = store.status()
    old_file = path.read_bytes()

    def fail_replace(*_args):
        raise OSError("write error " + TEST_KEY)

    monkeypatch.setattr(model_profiles.os, "replace", fail_replace)
    with pytest.raises(ModelProfileError) as caught:
        if operation == "add":
            add_profile(store, name="Second")
        elif operation == "activate":
            store.activate(profile_id)
        elif operation == "disable":
            store.activate(None)
        else:
            store.delete(profile_id)
    assert TEST_KEY not in "".join(traceback.format_exception(caught.value))
    assert path.read_bytes() == old_file
    assert store.status() == old_status
    assert store.active()["api_key"] == TEST_KEY
    assert list(path.parent.iterdir()) == [path]


def test_encryption_failure_creates_no_plaintext_or_directory(saved_store, monkeypatch):
    store, path = saved_store

    def fail_protect(_plaintext):
        raise RuntimeError(TEST_KEY)

    monkeypatch.setattr(store._protector, "protect", fail_protect)
    with pytest.raises(ModelProfileError) as caught:
        add_profile(store)
    assert TEST_KEY not in "".join(traceback.format_exception(caught.value))
    assert not path.parent.exists()
    assert store.status()["profiles"] == []


def test_plaintext_protector_cannot_write_fallback(saved_store, monkeypatch):
    store, path = saved_store
    monkeypatch.setattr(store._protector, "protect", lambda plaintext: plaintext)
    with pytest.raises(ModelProfileError):
        add_profile(store)
    assert not path.exists()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{broken-json",
        b'{"version":1,"protected":"invalid"}',
        b'{"version":1,"protected":"bm90LWVuY3J5cHRlZA=="}',
        b'{"version":1,"version":1,"protected":""}',
        b'{"version":2,"protected":""}',
        b'{"version":true,"protected":""}',
        b'{"version":1,"profiles":[],"api_key":"secret"}',
    ],
)
def test_corrupt_load_is_visible_and_never_overwrites_file(tmp_path, raw):
    path = tmp_path / "model-profiles.json"
    path.write_bytes(raw)
    store = ModelProfileStore(path, FakeProtector())
    status = store.status()
    assert status["storage_available"] is False
    assert status["storage_message"]
    assert status["profiles"] == []
    assert store.active() is None
    assert "secret" not in json.dumps(status)
    assert TEST_KEY not in json.dumps(status)
    for mutation in (
        lambda: add_profile(store),
        lambda: store.activate(None),
        lambda: store.delete("x"),
    ):
        with pytest.raises(ModelProfileError):
            mutation()
        assert path.read_bytes() == raw


@pytest.mark.parametrize("corruption", ["active", "duplicate", "endpoint", "key", "extra"])
def test_rejects_invalid_decrypted_records_as_a_complete_store(saved_store, corruption):
    store, path = saved_store
    add_profile(store)

    def corrupt(content):
        if corruption == "active":
            content["active_profile_id"] = "f" * 32
        elif corruption == "duplicate":
            content["profiles"].append(content["profiles"][0].copy())
        elif corruption == "endpoint":
            content["profiles"][0]["base_url"] = "http://external.example"
        elif corruption == "key":
            content["profiles"][0]["api_key"] = ""
        else:
            content["profiles"][0]["extra"] = TEST_KEY

    rewrite_payload(path, corrupt)
    original = path.read_bytes()
    restarted = ModelProfileStore(path, FakeProtector())
    assert restarted.status()["storage_available"] is False
    assert restarted.status()["profiles"] == []
    assert restarted.active() is None
    assert path.read_bytes() == original


def test_default_path_uses_local_app_data_without_eager_creation(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    store = ModelProfileStore(protector=FakeProtector())
    assert store.status()["storage_available"] is True
    path = tmp_path / "AI Chemistry Console" / "agent-portal" / "model-profiles.json"
    assert not path.parent.exists()
    add_profile(store)
    assert path.is_file()


def test_missing_default_directory_has_no_working_directory_fallback(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    store = ModelProfileStore(protector=FakeProtector())
    assert store.status()["storage_available"] is False
    with pytest.raises(ModelProfileError):
        add_profile(store)


def test_unsupported_os_does_not_save_plaintext(tmp_path, monkeypatch):
    monkeypatch.setattr(credential_protection.sys, "platform", "linux")
    path = tmp_path / "profiles.json"
    store = ModelProfileStore(path)
    assert store.status()["storage_available"] is False
    with pytest.raises(ModelProfileError):
        add_profile(store)
    assert not path.exists()
    with pytest.raises(CredentialProtectionError):
        WindowsCredentialProtector()


@pytest.mark.parametrize("profile_id", ["unknown", "0" * 32, [], 123])
def test_unknown_profile_changes_nothing(saved_store, profile_id):
    store, path = saved_store
    add_profile(store)
    before = path.read_bytes()
    for mutation in (store.activate, store.delete):
        with pytest.raises(ModelProfileError):
            mutation(profile_id)
        assert path.read_bytes() == before


def test_concurrent_adds_keep_all_profiles(saved_store):
    store, path = saved_store
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda index: add_profile(store, name=f"Profile {index}"), range(16)))
    status = store.status()
    assert len(status["profiles"]) == 16
    assert len({profile["id"] for profile in status["profiles"]}) == 16
    assert ModelProfileStore(path, FakeProtector()).status() == status


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI is required.")
def test_windows_dpapi_roundtrip_and_restart(tmp_path):
    protector = WindowsCredentialProtector()
    plaintext = TEST_KEY.encode()
    ciphertext = protector.protect(plaintext)
    assert plaintext not in ciphertext
    assert protector.unprotect(ciphertext) == plaintext
    with pytest.raises(CredentialProtectionError) as caught:
        protector.unprotect(b"not-a-DPAPI-payload")
    assert TEST_KEY not in str(caught.value)
    path = tmp_path / "profiles.json"
    store = ModelProfileStore(path)
    profile_id = add_profile(store)["profiles"][0]["id"]
    store.activate(profile_id)
    restarted = ModelProfileStore(path)
    assert restarted.status()["storage_available"] is True
    assert restarted.active()["api_key"] == TEST_KEY
