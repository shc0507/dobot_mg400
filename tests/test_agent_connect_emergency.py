"""Exercise the physical Stop wiring with recording adapters and no equipment."""

import time

import pytest

from orchestrator.agent_connect.emergency import PhysicalEmergency
from orchestrator.agent_connect.fallback import create_fallback_app
from orchestrator.agent_connect.fallback_deploy import service_unit


class Adapter:
    calls = []

    def __init__(self, url, identity, timeout):
        self.url = url
        assert identity == "agent-emergency" and timeout == 2

    def stop(self, **params):
        self.calls.append((self.url, params))
        return {"stop": "confirmed"}

    def state(self):
        return {"demo": False, "connected": True, "secret": "must not be exposed"}

    def close(self):
        pass


@pytest.fixture
def gateway():
    Adapter.calls = []
    emergency = PhysicalEmergency(Adapter)
    app = create_fallback_app(physical=emergency)
    yield app, emergency
    app.extensions["agent_stops"].close()


def test_fixed_stop_routes_have_no_normal_dispatch_or_lease(gateway):
    _, emergency = gateway
    assert emergency.stop("stage")["outcome"] == "confirmed"
    assert Adapter.calls == [("http://127.0.0.1:8767/contract/", {})]
    assert emergency.stop("furnace")["outcome"] == "confirmed"
    assert Adapter.calls[-1] == ("http://127.0.0.1:8769/", {})
    with pytest.raises(ValueError):
        emergency.stop("apply_setpoint")


def test_all_gas_stop_records_each_channel_and_does_not_send_heat_stop(gateway):
    app, _ = gateway
    client = app.test_client()
    result = client.post("/stop", json={"target": "gas_all", "request_id": "gas-stop"})
    assert result.status_code == 202
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = client.get("/stops/gas-stop").json
        if record["status"] != "pending":
            break
        time.sleep(0.005)
    assert record["status"] == "confirmed"
    assert {row["device"] for row in record["devices"]} == {"gas_0", "gas_1", "gas_2"}
    assert sorted(params["gas_channel"] for _, params in Adapter.calls) == [0, 1, 2]
    assert all(url == "http://127.0.0.1:8769/" for url, _ in Adapter.calls)
    assert record["sender"]["verified"] is False


def test_independent_status_is_read_only_and_no_arbitrary_fields(gateway):
    app, _ = gateway
    report = app.test_client().get("/status").json
    assert report["equipment_mode"] == "physical" and report["stop_enabled"]
    assert not report["physical_commands_enabled"]
    assert set(report["devices"]) == {
        "stage",
        "robot",
        "microscope",
        "thermal",
        "furnace",
        "vision",
    }
    assert all("secret" not in state for state in report["devices"].values())
    assert Adapter.calls == []
    assert app.test_client().post("/command", json={"action": "enable"}).status_code == 404


def test_gateway_opt_in_is_a_server_unit_argument():
    assert "--physical" not in service_unit("/opt/mg400-twin-test")
    assert "--physical" in service_unit("/opt/mg400-twin-test", physical=True)
    assert "WantedBy=multi-user.target" in service_unit("/opt/mg400-twin-test", physical=True)
