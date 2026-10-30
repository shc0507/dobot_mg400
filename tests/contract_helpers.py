"""Offline conformance checks shared by service tests in demo mode.

Supply an in-process HTTP test client, a fake-clock advance function, and an
expiry_count function. The helper opens no network connection or device.
"""


def response_json(response):
    getter = getattr(response, "get_json", None)
    return getter() if getter else response.json()


def assert_error(response, status):
    assert response.status_code == status
    body = response_json(response)
    assert set(body) == {"error"}
    assert isinstance(body["error"], str) and body["error"]


def assert_description(value):
    required = {
        "service",
        "version",
        "contract",
        "demo",
        "zones",
        "lease_seconds",
        "on_lease_expiry",
        "commands",
    }
    assert required <= value.keys()
    assert type(value["contract"]) is int and value["contract"] == 1
    assert value["demo"] is True
    assert isinstance(value["service"], str) and value["service"]
    assert isinstance(value["version"], str) and value["version"]
    assert isinstance(value["zones"], list)
    assert value["lease_seconds"] > 0
    assert value["on_lease_expiry"]
    for spec in value["commands"].values():
        assert spec["params"]["type"] == "object"
        assert isinstance(spec["any_client"], bool)


def assert_state(value):
    required = {
        "service",
        "seq",
        "ts_unix",
        "demo",
        "connected",
        "busy",
        "locked",
        "fault",
        "lease",
        "last_command",
    }
    assert required <= value.keys()
    assert value["demo"] is True
    assert isinstance(value["seq"], int) and value["seq"] >= 0
    for field in ("connected", "busy", "locked"):
        assert isinstance(value[field], bool)
    assert value["fault"] is None or {"code", "message"} <= value["fault"].keys()
    last = value["last_command"]
    if last is not None:
        assert {"id", "action", "client", "result", "message", "finished_at"} <= last.keys()
        assert last["result"] in {"ok", "failed", "rejected", "stopped"}


def check_contract(client, *, advance, expiry_count, base_path="/", headers=None):
    """Check discovery, state, any-client stop, lease conflicts, expiry, and errors."""
    base = base_path.rstrip("/")
    common = dict(headers or {})

    def post(route, actor="conformance-a", data=None, **kwargs):
        request_headers = {**common, "X-MG400-Client": actor}
        return client.post(
            base + "/api/" + route,
            headers=request_headers,
            json={} if data is None else data,
            **kwargs,
        )

    description = client.get(base + "/api/describe", headers=common)
    assert description.status_code == 200
    metadata = response_json(description)
    assert_description(metadata)
    state = client.get(base + "/api/state", headers=common)
    assert state.status_code == 200
    assert_state(response_json(state))
    assert post("heartbeat").status_code == 200
    assert_error(post("heartbeat", "conformance-b"), 409)
    assert_error(post("release", "conformance-b"), 409)
    for _ in range(2):
        reply = post("stop", "conformance-b", {"wait_s": 0})
        assert reply.status_code == 200
        value = response_json(reply)
        assert value["stop"] in {"confirmed", "pending", "unconfirmed"}
        assert_state(value["state"])
    before = expiry_count()
    advance(metadata["lease_seconds"])
    assert client.get(base + "/api/state", headers=common).status_code == 200
    assert expiry_count() == before + 1
    assert client.get(base + "/api/state", headers=common).status_code == 200
    assert expiry_count() == before + 1
    assert post("heartbeat", "conformance-b").status_code == 200
    assert post("release", "conformance-b").status_code == 200
    assert_error(post("stop", data={"wait_s": -1}), 400)
    assert_error(post("heartbeat", data=[]), 400)
    assert_error(
        client.post(
            base + "/api/heartbeat",
            data="{}",
            headers={**common, "X-MG400-Client": "conformance-a", "Content-Type": "text/plain"},
        ),
        415,
    )
    assert_error(client.get(base + "/api/state", headers={**common, "Origin": "null"}), 403)
    if "Authorization" in common:
        assert_error(client.get(base + "/api/state"), 401)
