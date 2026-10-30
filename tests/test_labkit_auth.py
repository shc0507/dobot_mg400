"""Check shared access rules without starting a service or opening a device."""

from email.message import Message

import pytest
from werkzeug.datastructures import Headers

from labkit import auth


def headers(*pairs):
    result = Message()
    for name, value in pairs:
        result[name] = value
    return result


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


def test_robot_keeps_its_case_sensitive_localhost_bind_rule():
    assert auth.loopback_name("localhost", case_sensitive_localhost=True)
    assert not auth.loopback_name("LOCALHOST", case_sensitive_localhost=True)
    assert not auth.valid_token_for_bind(None, "LOCALHOST", case_sensitive_localhost=True)


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "192.0.2.1", "panel.example"])
@pytest.mark.parametrize("token", [None, "", "x" * 23, " x" * 12, "é" * 24])
def test_non_loopback_bind_requires_a_long_browser_token(bind, token):
    assert not auth.valid_token_for_bind(token, bind)
    assert not auth.valid_token_for_bind(token, bind, require_long_token=False)


@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "panel.example", "127.0.0.1"])
def test_bind_accepts_a_token_at_the_minimum_length(bind):
    assert auth.valid_token_for_bind("x" * 24, bind)


@pytest.mark.parametrize("bind", ["127.0.0.1", "localhost", "::1"])
def test_loopback_preserves_each_services_configured_token_policy(bind):
    assert auth.valid_token_for_bind(None, bind)
    assert auth.valid_token_for_bind("", bind)
    assert not auth.valid_token_for_bind("short", bind)
    assert auth.valid_token_for_bind("short", bind, require_long_token=False)
    assert not auth.valid_token_for_bind("invalid ", bind, require_long_token=False)


@pytest.mark.parametrize(
    ("value", "default_port", "expected"),
    [
        ("LOCALHOST", 80, ("localhost", 80)),
        (" localhost:8766 ", 80, ("localhost", 8766)),
        ("[::1]:8767", 80, ("::1", 8767)),
        ("panel.example", 443, ("panel.example", 443)),
        ("localhost:0", 80, ("localhost", 80)),
        ("localhost:port", 80, None),
        ("localhost:65536", 80, None),
        ("[::1", 80, None),
        ("user@localhost", 80, None),
        ("localhost/path", 80, None),
        ("localhost?query", 80, None),
        ("localhost#fragment", 80, None),
        ("", 80, None),
    ],
)
def test_host_port_preserves_parsing_and_default_port_rules(value, default_port, expected):
    assert auth.host_port(value, default_port) == expected


@pytest.mark.parametrize("value", ["localhost:8766", "LOCALHOST", "127.8.9.10:1", "[::1]:8767"])
def test_loopback_host_accepts_loopback_headers(value):
    assert auth.loopback_host(value)


@pytest.mark.parametrize(
    "value",
    [
        "localhost.evil.test",
        "user@localhost",
        "localhost/path",
        "127.0.0.1%2eevil.test",
        "%6cocalhost",
        "localhost:bad",
        "localhost:80, evil.test",
        "192.0.2.1",
        "",
    ],
)
def test_loopback_host_refuses_ambiguous_and_remote_headers(value):
    assert not auth.loopback_host(value)


@pytest.mark.parametrize("name", auth.FORWARDED_HEADERS)
@pytest.mark.parametrize("value", ["", "127.0.0.1"])
def test_forwarded_headers_are_detected_even_when_empty(name, value):
    supplied = headers((name.lower(), value), ("Host", "localhost"))
    assert auth.has_forwarded_headers(supplied)
    assert not auth.host_allowed(supplied)
    assert auth.host_allowed(supplied, "configured-token")


def test_host_access_without_token_requires_a_loopback_host():
    assert not auth.has_forwarded_headers(headers(("Host", "localhost")))
    assert auth.host_allowed(headers(("Host", "localhost")))
    assert not auth.host_allowed(headers())
    assert not auth.host_allowed(headers(("Host", "evil.test")))
    assert auth.host_allowed(headers(("Host", "evil.test")), "configured-token")


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


@pytest.mark.parametrize("header_type", [headers, lambda *pairs: Headers(pairs)])
@pytest.mark.parametrize(
    ("pairs", "expected"),
    [
        ([("Host", "panel.example")], True),
        ([("Host", "panel.example"), ("Sec-Fetch-Site", " cross-site ")], False),
        ([("Host", "PANEL.example:80"), ("Origin", "http://panel.example")], True),
        ([("Host", "panel.example"), ("Origin", "https://panel.example:443")], True),
        ([("Host", "[::1]:8766"), ("Origin", "http://[::1]:8766")], True),
        ([("Host", "panel.example:80"), ("Origin", "https://panel.example")], False),
        ([("Host", "panel.example"), ("Origin", "http://panel.example.evil.test")], False),
        ([("Host", "panel.example"), ("Origin", "http://panel.example/path")], False),
        ([("Host", "panel.example"), ("Origin", "http://panel.example:bad")], False),
        ([("Host", "panel.example"), ("Origin", "ws://panel.example")], False),
        ([("Host", "panel.example"), ("Origin", "null")], False),
        ([("Host", "panel.example"), ("Origin", "http://[::1")], False),
        (
            [
                ("Host", "panel.example"),
                ("Origin", "http://panel.example"),
                ("Origin", "http://panel.example"),
            ],
            False,
        ),
        (
            [
                ("Host", "127.0.0.1:8766"),
                ("X-Forwarded-Host", "panel.example, proxy.internal"),
                ("Origin", "https://panel.example"),
            ],
            True,
        ),
        (
            [
                ("Host", "127.0.0.1:8766"),
                ("X-Forwarded-Host", "proxy.internal, panel.example"),
                ("Origin", "https://panel.example"),
            ],
            False,
        ),
    ],
)
def test_origin_check_preserves_browser_and_proxy_rules(header_type, pairs, expected):
    assert auth.same_origin(header_type(*pairs)) is expected
