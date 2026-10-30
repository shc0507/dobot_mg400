import http.client
import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from labkit.client import ServiceClient
from labkit.errors import ServiceError


class Transport:
    def __init__(self):
        self.calls = []
        self.responses = [{"contract": 1}]

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if hasattr(response, "read"):
            return response
        return io.BytesIO(json.dumps(response).encode())


@pytest.fixture
def transport(monkeypatch):
    transport = Transport()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: transport)
    return transport


def test_all_methods_use_the_contract_paths_body_headers_and_timeout(transport):
    client = ServiceClient("http://localhost/instrument/", "t" * 24, "test-client", timeout=4)
    transport.responses += [{"value": i} for i in range(7)]
    assert client.describe() == {"contract": 1}
    results = [
        client.state(),
        client.command("move", x_mm=5),
        client.stop(15),
        client.heartbeat(),
        client.release(),
        client.events(7),
        client.state(),
    ]
    assert results == [{"value": i} for i in range(7)]
    requests = [request for request, _ in transport.calls]
    assert [request.full_url for request in requests] == [
        "http://localhost/instrument/api/" + path
        for path in (
            "describe",
            "state",
            "command",
            "stop",
            "heartbeat",
            "release",
            "events?since=7",
            "state",
        )
    ]
    assert [request.get_method() for request in requests] == [
        "GET",
        "GET",
        "POST",
        "POST",
        "POST",
        "POST",
        "GET",
        "GET",
    ]
    assert json.loads(requests[2].data) == {"action": "move", "x_mm": 5}
    assert json.loads(requests[3].data) == {"wait_s": 15}
    assert json.loads(requests[4].data) == json.loads(requests[5].data) == {}
    for request, timeout in transport.calls:
        assert timeout == 4
        assert request.get_header("Authorization") == "Bearer " + "t" * 24
        if request.data is not None:
            assert request.get_header("Content-type") == "application/json"
            assert request.get_header("X-mg400-client") == "test-client"


def test_first_operation_checks_version_before_dispatch(transport):
    client = ServiceClient("http://localhost", None, "a")
    transport.responses.append({"stop": "confirmed"})
    assert client.stop()["stop"] == "confirmed"
    assert [request.full_url for request, _ in transport.calls] == [
        "http://localhost/api/describe",
        "http://localhost/api/stop",
    ]


@pytest.mark.parametrize("contract", [None, 0, 2, "1", True])
def test_different_or_invalid_contract_prevents_mutation_without_fallback(transport, contract):
    transport.responses = [{"contract": contract}]
    client = ServiceClient("http://localhost", None, "a")
    with pytest.raises(ServiceError, match="contract version") as error:
        client.command("move")
    assert error.value.status == 409
    assert len(transport.calls) == 1
    assert transport.calls[0][0].full_url.endswith("/api/describe")


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError(
            "http://localhost/api/command",
            503,
            "link lost",
            {},
            io.BytesIO(b'{"error":"device unavailable"}'),
        ),
        urllib.error.URLError("timeout"),
        TimeoutError("read timeout"),
        http.client.BadStatusLine("broken"),
    ],
)
def test_requests_are_never_retried_and_errors_are_normalized(transport, failure):
    transport.responses.append(failure)
    client = ServiceClient("http://localhost", None, "a")
    with pytest.raises(ServiceError) as error:
        client.command("move")
    assert len(transport.calls) == 2
    if isinstance(failure, urllib.error.HTTPError):
        assert error.value.status == 503
        assert error.value.message == "device unavailable"
    else:
        assert error.value.status == 0


def test_truncated_response_is_service_error_without_retry(transport):
    class Truncated(io.BytesIO):
        def read(self, *args):
            raise http.client.IncompleteRead(b'{"command_id":', 20)

    transport.responses.append(Truncated())
    with pytest.raises(ServiceError) as error:
        ServiceClient("http://localhost", None, "a").command("move")
    assert error.value.status == 0
    assert len(transport.calls) == 2


@pytest.mark.parametrize("payload", [b"not JSON", b"[]", b"null", b"\xff"])
def test_malformed_response_is_rejected(transport, payload):
    transport.responses = [io.BytesIO(payload)]
    with pytest.raises(ServiceError) as error:
        ServiceClient("http://localhost", None, "a").describe()
    assert error.value.status == 502
    assert len(transport.calls) == 1


def test_redirect_cannot_forward_token_or_rewrite_post_without_network():
    client = ServiceClient("http://localhost", "t" * 24, "a")
    requests = []

    class OfflineHTTP(urllib.request.HTTPHandler):
        handler_order = 1

        def http_open(self, request):
            requests.append(request)
            response = io.BytesIO(b"")
            response.code = 302
            response.msg = "Found"
            response.url = request.full_url
            response.info = lambda: {"Location": "http://elsewhere.invalid/steal"}
            response.geturl = lambda: request.full_url
            return response

    client._opener.add_handler(OfflineHTTP())
    client._version_checked = True
    with pytest.raises(ServiceError) as error:
        client.command("move")
    assert error.value.status == 302
    assert len(requests) == 1
    assert requests[0].get_method() == "POST"
    assert requests[0].full_url == "http://localhost/api/command"


def test_heartbeat_helper_is_explicit_and_stops_after_first_error():
    called, failed = threading.Event(), threading.Event()
    calls = []
    error = ServiceError(409, "lease unavailable")

    class Client:
        def heartbeat(self):
            calls.append(1)
            called.set()
            raise error

    from labkit.client import Heartbeat

    helper = Heartbeat(Client(), interval=0.001, on_error=lambda exc: failed.set())
    assert calls == [] and helper._thread is None
    helper.start()
    assert called.wait(2) and failed.wait(2)
    helper.stop(2)
    assert helper.error is error
    assert calls == [1]
    assert not helper._thread.is_alive()
    with pytest.raises(RuntimeError):
        helper.start()


def test_heartbeat_context_stops_successful_renewals_without_auto_release():
    called = threading.Event()
    client = ServiceClient("http://localhost", None, "a")
    client.heartbeat = called.set
    with client.heartbeat_thread(interval=10) as helper:
        assert called.wait(2)
    assert not helper._thread.is_alive()
    assert helper.error is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost",
        "http://user:pass@localhost",
        "http://localhost/#x",
        "http://localhost/?x=1",
        "http://",
    ],
)
def test_client_rejects_ambiguous_base_urls(url):
    with pytest.raises(ValueError):
        ServiceClient(url, None, "a")


@pytest.mark.parametrize("interval", [0, -1, True, float("nan"), "1"])
def test_heartbeat_rejects_invalid_interval(interval):
    with pytest.raises(ValueError):
        ServiceClient("http://localhost", None, "a").heartbeat_thread(interval=interval)
