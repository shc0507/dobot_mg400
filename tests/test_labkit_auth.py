"""Check the gateway token helpers without starting a service or opening a device."""

import pytest

from labkit import auth


@pytest.mark.parametrize("value", ["", "x", "!~", "lab control token", "x" * 24])
def test_valid_token_accepts_browser_header_characters(value):
    assert auth.valid_token(value)


@pytest.mark.parametrize("value", [" x", "x ", " ", "x\t", "x\n", "x\r", "é", "\x7f"])
def test_valid_token_refuses_characters_a_browser_changes(value):
    assert not auth.valid_token(value)


@pytest.mark.parametrize("name", ["127.0.0.1", "127.8.9.10", "::1", "localhost", "LOCALHOST"])
def test_loopback_name_accepts_only_local_names_and_addresses(name):
    assert auth.loopback_name(name)


@pytest.mark.parametrize(
    "name",
    ["0.0.0.0", "::", "192.0.2.1", "localhost.evil.test", "127.0.0.1.evil.test", "[::1]", ""],
)
def test_loopback_name_refuses_remote_and_malformed_names(name):
    assert not auth.loopback_name(name)


def test_loopback_name_keeps_its_case_sensitive_localhost_option():
    assert auth.loopback_name("localhost", case_sensitive_localhost=True)
    assert not auth.loopback_name("LOCALHOST", case_sensitive_localhost=True)


@pytest.mark.parametrize(
    "value",
    ["", "token", "Basic token", "bearer token", "Bearer  token", "Bearer token ", "Bearer tokén"],
)
def test_bearer_check_does_not_normalize_or_strip_the_header(value):
    assert not auth.bearer_matches(value, "token")


def test_bearer_check_uses_constant_time_byte_comparison(monkeypatch):
    calls = []
    compare = auth.hmac.compare_digest

    def tracked(actual, expected):
        calls.append((actual, expected))
        return compare(actual, expected)

    monkeypatch.setattr(auth.hmac, "compare_digest", tracked)
    assert auth.bearer_matches("Bearer lab token", "lab token")
    assert calls == [(b"Bearer lab token", b"Bearer lab token")]
