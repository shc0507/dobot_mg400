"""Check the fixed read relay without contacting Olares or any lab device."""

import io
import json
import socket
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest
from websockets.exceptions import ConnectionClosedOK

from orchestrator.agent_connect import live
from orchestrator.agent_connect.portal import create_portal_app


class Source:
    def __init__(self, messages=()):
        self.reads = []
        self.queries = []
        self.messages = messages
        self.closed = False
        self.error = None

    def read(self, path):
        self.reads.append(path)
        if self.error:
            raise self.error
        return {"mode": "live", "path": path, "extra": {"unknown": [1, None, "raw"]}}

    def events(self, query):
        self.queries.append(query)
        try:
            for message in self.messages:
                yield message
            if self.error:
                raise self.error
        finally:
            self.closed = True


@pytest.fixture
def source():
    return Source()


@pytest.fixture
def http(source):
    return create_portal_app(live_transport=source).test_client()


def test_reads_preserve_discovery_contract_and_filtered_sample_data(http, source):
    discovery = http.get("/api/live/discovery")
    assert discovery.status_code == 200
    assert discovery.json["mode"] == "live"
    assert discovery.headers["Cache-Control"] == "no-store"
    paths = [
        live.PREFIX + "/contracts/" + "a" * 64 + ".json",
        live.STATE,
        live.STATE + "?groups=devices,samples,operations&rate_hz=4",
        live.SAMPLES + "?selection=all&limit=10&offset=20&expected_revision=5",
        live.SAMPLES + "?selection=ids&chip_id=chip-001&chip_id=chip-002",
    ]
    for path in paths:
        response = http.get("/api/live/read", query_string={"path": path})
        assert response.status_code == 200, response.json
        assert response.json == source.read(path)
    assert source.reads[0] == live.DISCOVERY


@pytest.mark.parametrize(
    "path",
    [
        "http://100.64.0.1:8082" + live.STATE,
        "//100.64.0.1:8082" + live.STATE,
        live.STATE + "#fragment",
        live.STATE + "#",
        " " + live.STATE,
        live.STATE + "\n",
        live.DISCOVERY,
        live.PREFIX + "/v1/stream",
        live.PREFIX + "/v1/commands",
        "/api/console/relay/stage",
        "/mock/control",
        live.PREFIX + "/contracts/" + "a" * 63 + ".json",
        live.PREFIX + "/contracts/" + "a" * 64 + ".json?extra=1",
        live.STATE + "?rate_hz=3",
        live.STATE + "?groups=commands",
        live.STATE + "?groups=devices&groups=samples",
        live.STATE + "?url=http://example.com",
        live.SAMPLES + "?selection=all&chip_id=chip-1",
        live.SAMPLES + "?selection=ids",
        live.SAMPLES + "?limit=101",
        live.SAMPLES + "?offset=-1",
        live.SAMPLES + "?expected_revision=-1",
        live.SAMPLES + "?limit=1&limit=2",
        live.SAMPLES + "?action=move",
        live.STATE + "?extra=" + "a" * live.MAX_PATH,
    ],
)
def test_read_allowlist_rejects_invalid_paths_before_transport(http, source, path):
    assert http.get("/api/live/read", query_string={"path": path}).status_code == 400
    assert source.reads == []


@pytest.mark.parametrize(
    "url",
    [
        "/api/live/discovery?url=http://example.com",
        "/api/live/read",
        "/api/live/read?path=a&path=b",
        "/api/live/read?path=a&url=http://example.com",
        "/api/live/events?rate_hz=3",
        "/api/live/events?rate_hz=1&rate_hz=2",
        "/api/live/events?rate_hz=02",
        "/api/live/events?groups=devices",
        "/api/live/events?boot_id=boot-a",
        "/api/live/events?event_seq=1",
        "/api/live/events?boot_id=boot-a&event_seq=-1",
        "/api/live/events?boot_id=boot-a&event_seq=1.0",
        "/api/live/events?boot_id=boot-a&event_seq=" + "1" * 21,
        "/api/live/events?boot_id=%0A&event_seq=1",
        "/api/live/events?url=ws://example.com",
    ],
)
def test_bad_query_fields_never_reach_upstream(http, source, url):
    assert http.get(url).status_code == 400
    assert source.reads == source.queries == []


def test_live_routes_accept_no_posts_or_commands(http, source):
    for path in ("discovery", "read", "events", "commands", "lease", "control"):
        assert http.post("/api/live/" + path, json={"action": "move"}).status_code in (404, 405)
    assert source.reads == source.queries == []


@pytest.mark.parametrize("error,status", [(OSError("offline"), 503), (ValueError("bad JSON"), 503)])
def test_read_failures_have_no_simulated_fallback(http, source, error, status):
    source.error = error
    response = http.get("/api/live/discovery")
    assert response.status_code == status
    assert response.json == {"error": str(error)}


def test_upstream_http_failure_keeps_status(http, source):
    source.error = urllib.error.HTTPError("fixed", 404, "Missing", {}, None)
    response = http.get("/api/live/read", query_string={"path": live.STATE})
    assert response.status_code == 404
    assert response.json == {"error": "Olares refused the read request."}


@pytest.mark.parametrize("rate", ["1", "2", "4"])
def test_sse_preserves_full_messages_heartbeats_and_cursor(http, source, rate):
    messages = [
        {"type": "state", "boot_id": "boot-a", "event_seq": 7, "extra": {"raw": [1, None]}},
        {"type": "event", "event_seq": 8, "data": {"value": "line\nbreak"}},
        {"type": "resync", "reason": "history_evicted"},
        None,
    ]
    source.messages = [messages[0], live.HEARTBEAT, *messages[1:]]
    response = http.get(
        "/api/live/events", query_string={"rate_hz": rate, "boot_id": "boot-a", "event_seq": "6"}
    )
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert response.headers["X-Accel-Buffering"] == "no"
    body = response.get_data(as_text=True)
    received = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert received == messages
    assert body.count("event: message\n") == len(messages)
    assert ": heartbeat\n\n" in body
    assert source.queries == [{"rate_hz": rate, "boot_id": "boot-a", "event_seq": "6"}]
    assert source.closed


@pytest.mark.parametrize(
    "error", [OSError("offline"), ValueError("invalid JSON"), ConnectionClosedOK(None, None)]
)
def test_sse_error_is_terminal_and_closes_transport(http, source, error):
    source.messages = [{"unknown": "retain this"}]
    source.error = error
    body = http.get("/api/live/events").get_data(as_text=True)
    assert body.count("event: connection-error\n") == 1
    assert body.endswith(live._event("connection-error", {"error": str(error)}))
    assert source.queries == [{"rate_hz": "2"}]
    assert source.closed


def test_browser_disconnect_closes_idle_upstream(http, source):
    source.messages = [live.HEARTBEAT] * 5
    response = http.get("/api/live/events", buffered=False)
    iterator = iter(response.response)
    assert next(iterator) == b": relay\n\n"
    assert next(iterator) == b": heartbeat\n\n"
    response.close()
    assert source.closed


def test_http_transport_uses_fixed_get_ignores_proxies_and_rejects_redirects(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://example.com:9999")
    transport = live.LiveTransport()
    calls = []

    def open_read(request, *, timeout):
        calls.append(request.full_url)
        assert request.get_method() == "GET"
        assert request.data is None
        assert timeout == live.TIMEOUT_S
        return io.BytesIO(b'{"mode":"live","unknown":[null,1]}')

    monkeypatch.setattr(transport.opener, "open", open_read)
    assert transport.read(live.DISCOVERY) == {"mode": "live", "unknown": [None, 1]}
    assert calls == [live.ORIGIN + live.DISCOVERY]
    assert not any(getattr(handler, "proxies", {}) for handler in transport.opener.handlers)
    redirect_body = io.BytesIO(b"redirect")
    with pytest.raises(ValueError, match="redirects"):
        live.NoRedirect().redirect_request(None, redirect_body, 302, "", {}, "http://example.com")
    assert redirect_body.closed
    with pytest.raises(ValueError):
        transport.read("http://example.com")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "raw",
    [b"not JSON", b'{"value":NaN}', b"\xff", b" " * (live.MAX_BYTES + 1), b"[" * 10000],
    ids=["invalid", "nan", "encoding", "oversized", "nested"],
)
def test_transport_rejects_invalid_or_oversized_http_json(monkeypatch, raw):
    transport = live.LiveTransport()
    monkeypatch.setattr(transport.opener, "open", lambda *args, **kwargs: io.BytesIO(raw))
    with pytest.raises(ValueError):
        transport.read(live.DISCOVERY)


def test_socket_transport_is_receive_only_bounded_and_closes_on_disconnect(monkeypatch):
    calls = []
    closed = []

    class Socket:
        def close(self):
            closed.append("socket")

    class Upstream:
        def __init__(self):
            self.items = iter([TimeoutError(), '{"unknown":{"raw":[null,1]}}'])

        def recv(self, *, timeout):
            assert timeout == live.HEARTBEAT_S
            item = next(self.items)
            if isinstance(item, Exception):
                raise item
            return item

    def open_socket(address, *, timeout):
        assert address == ("100.64.0.1", 8082)
        assert timeout == live.TIMEOUT_S
        return Socket()

    @contextmanager
    def connect(url, **kwargs):
        calls.append((url, kwargs))
        try:
            yield Upstream()
        finally:
            closed.append("upstream")

    monkeypatch.setattr(live.socket, "create_connection", open_socket)
    monkeypatch.setattr(live, "connect", connect)
    messages = live.LiveTransport().events({"rate_hz": "4", "boot_id": "boot-a", "event_seq": "8"})
    assert next(messages) is live.HEARTBEAT
    assert next(messages) == {"unknown": {"raw": [None, 1]}}
    messages.close()
    assert closed == ["upstream", "socket"]
    url, kwargs = calls[0]
    assert url == live.WS_ORIGIN + live.STREAM + "?rate_hz=4&boot_id=boot-a&event_seq=8"
    assert isinstance(kwargs["sock"], Socket)
    assert kwargs["proxy"] is None
    assert kwargs["max_size"] == live.MAX_BYTES and kwargs["max_queue"] == 4
    assert kwargs["origin"] == live.ORIGIN
    assert kwargs["compression"] is None


def test_failed_socket_handshake_closes_preconnected_socket(monkeypatch):
    closed = []

    class Socket:
        def close(self):
            closed.append(True)

    def refuse(*args, **kwargs):
        raise ValueError("Redirect refused.")

    monkeypatch.setattr(live.socket, "create_connection", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(live, "connect", refuse)
    with pytest.raises(ValueError, match="Redirect refused"):
        next(live.LiveTransport().events({"rate_hz": "2"}))
    assert closed == [True]


def test_websocket_library_cannot_follow_redirect_with_fixed_socket(monkeypatch):
    client_socket, server_socket = socket.socketpair()
    calls = []

    def respond():
        with server_socket:
            server_socket.settimeout(2)
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = server_socket.recv(4096)
                if not chunk:
                    return
                request += chunk
            server_socket.sendall(
                b"HTTP/1.1 302 Found\r\n"
                b"Location: ws://example.com/command\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )

    def fixed_socket(address, *, timeout):
        calls.append(address)
        assert len(calls) == 1
        return client_socket

    worker = threading.Thread(target=respond)
    worker.start()
    monkeypatch.setattr(live.socket, "create_connection", fixed_socket)
    try:
        with pytest.raises((ValueError, live.WebSocketException)):
            next(live.LiveTransport().events({"rate_hz": "2"}))
        assert calls == [("100.64.0.1", 8082)]
        assert client_socket.fileno() == -1
    finally:
        client_socket.close()
        server_socket.close()
        worker.join(3)
    assert not worker.is_alive()
