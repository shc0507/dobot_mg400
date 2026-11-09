"""Furnace + microscope workspace: the status route and the relay with both demos."""

import threading
from types import SimpleNamespace

import pytest
from test_console import Clock, Device, headers, no_network, post  # noqa: F401

from labkit.client import ServiceClient
from labkit.errors import ServiceError
from orchestrator.app import OrchestratorService, create_app
from orchestrator.clients import SERVICES, ServiceAdapter
from orchestrator.web.workspaces import process

ROI = {"name": "crucible", "min_c": 24.0, "mean_c": 25.5, "max_c": 27.25, "frame_age_s": 0.2}


class _FlaskClient(ServiceClient):
    """The contract client over a Flask test client: no socket."""

    app = None

    def _request(self, path, data=None):
        headers = {"Accept": "application/json"}
        if data is None:
            response = self.app.get("/" + path, headers=headers)
        else:
            headers["X-MG400-Client"] = self.client_id
            response = self.app.post("/" + path, json=data, headers=headers)
        body = response.get_json()
        if response.status_code >= 400:
            raise ServiceError(response.status_code, (body or {}).get("error", "error"))
        return body


def flask_adapter(test_client, identity):
    client_type = type("Client", (_FlaskClient,), {"app": test_client})
    adapter_type = type("Adapter", (ServiceAdapter,), {"_client_type": client_type})
    return adapter_type("http://demo.invalid", identity)


def furnace_adapter(test_client, identity):
    """The furnace demo adapter with the lease-free gas stop of FurnaceAdapter."""
    adapter = flask_adapter(test_client, identity)
    adapter.gas_stop = lambda channel: adapter.request(
        "api/flow/gas/stop", {"channel_index": channel}
    )
    return adapter


def build(tmp_path, adapters):
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    devices = {name: Device(name) for name in SERVICES}

    def factory(required, identity):
        return {
            name: adapters[name](identity) if name in adapters else devices[name].adapter(identity)
            for name in required
        }

    service = OrchestratorService(
        settings={"MG400_CONSOLE_COMMIT": "abc1234"},
        recipes_dir=recipes,
        demo=True,
        client_factory=factory,
        clock=Clock(),
    ).start(background=False)
    app = create_app(service, base_path="/lab")
    return SimpleNamespace(service=service, devices=devices, client=app.test_client())


@pytest.fixture
def rig(tmp_path):
    def thermal(identity):
        adapter = devices["thermal"].adapter(identity)
        adapter.roi = lambda name: {**ROI} if name == "crucible" else None
        return adapter

    devices = {}
    value = build(tmp_path, {"thermal": thermal})
    devices.update(value.devices)
    value.devices["furnace"].state.update(
        pv=24.5,
        sv=0.0,
        control_mode="STOPPED",
        sequence={"running": False, "phase": "idle", "steps": []},
        flow={"connected": True, "gas_channels": [{"channel_index": 0, "gas_name": "Gas 1"}]},
        lease_secret="not-for-the-page",
    )
    value.devices["microscope"].state.update(
        objective_id=1,
        objective_offset_steps=-3,
        objective_offset_limit_steps=20,
        focus_position_pulses=1200,
        focus_zero_known=True,
    )
    yield value
    value.service.close()


def status(rig):
    response = rig.client.get("/lab/api/console/ws/process/status", headers=headers())
    assert response.status_code == 200
    return response.get_json()


def test_status_reports_page_fields_and_crucible(rig):
    rig.service.console._collect()
    body = status(rig)
    assert body["furnace"]["pv"] == 24.5 and body["furnace"]["control_mode"] == "STOPPED"
    assert "lease" not in body["furnace"] and "lease_secret" not in body["furnace"]
    assert body["microscope"]["focus_position_pulses"] == 1200
    assert body["microscope"]["objective_offset_steps"] == -3
    assert body["microscope"]["objective_offset_limit_steps"] == 20
    assert body["crucible"] == {
        key: ROI[key] for key in ("min_c", "mean_c", "max_c", "frame_age_s")
    }
    assert "gas_ready" not in body and "cutoff_confirmed" not in body
    assert len(body["history"]) == 1 and body["history"][0][1:] == [24.5, 0.0]


def test_history_keeps_60_minutes_at_the_sample_interval(rig):
    now = [1000.0]
    reader = process.ProcessStatus(rig.service, wall=lambda: now[0])
    rig.service.console._collect()
    for step in range(800):
        now[0] = 1000.0 + step * 2.5
        reader.report()
    history = reader.report()["history"]
    assert all(b[0] - a[0] >= process.SAMPLE_S for a, b in zip(history, history[1:]))
    assert now[0] - history[0][0] <= process.HISTORY_S
    assert len(history) <= process.HISTORY_S / process.SAMPLE_S + 1


def test_status_without_service_data_is_empty_not_an_error(tmp_path):
    rig = build(tmp_path, {})
    try:
        body = process.ProcessStatus(rig.service).report()
        assert body["furnace"] is None and body["microscope"] is None
        assert body["crucible"] is None and body["history"] == []
    finally:
        rig.service.close()


# The furnace and microscope demos ---------------------------------------------


@pytest.fixture
def demos(tmp_path, monkeypatch):
    from furnace.app import create_app as furnace_app
    from furnace.app import make_service
    from microscope.app import create_app as microscope_app
    from microscope.service import MicroscopeService

    monkeypatch.setattr(threading.Thread, "start", _no_microscope_threads(threading.Thread.start))
    furnace = make_service(demo=True)
    furnace.start(background=False)
    microscope = MicroscopeService(demo=True)
    furnace_client = furnace_app(furnace).test_client()
    microscope_client = microscope_app(microscope).test_client()
    rig = build(
        tmp_path,
        {
            "furnace": lambda identity: furnace_adapter(furnace_client, identity),
            "microscope": lambda identity: flask_adapter(microscope_client, identity),
        },
    )
    yield SimpleNamespace(**vars(rig), furnace=furnace, microscope=microscope)
    rig.service.close()
    furnace.close()
    microscope.close()


def _no_microscope_threads(start):
    def guarded(thread):
        if thread.name.startswith("microscope"):
            raise AssertionError("The microscope demo started a thread")
        return start(thread)

    return guarded


def test_furnace_demo_heats_and_sends_gas_only_through_the_relay(demos):
    described = demos.client.get("/lab/api/console/describe/furnace", headers=headers()).get_json()
    assert {"apply_setpoint", "start_sequence", "set_gas_flow"} <= set(described["commands"])
    refused = post(demos, "command/furnace", {"action": "apply_setpoint", "params": {}})
    assert refused.status_code == 409
    assert post(demos, "control/furnace/take").status_code == 200
    params = {"temperature_c": 120, "ramp_rate_c_per_min": 5}
    sent = post(demos, "command/furnace", {"action": "apply_setpoint", "params": params})
    assert sent.status_code == 202, sent.get_json()
    assert demos.furnace.status()["sv"] == 120
    gas = post(
        demos,
        "command/furnace",
        {"action": "set_gas_flow", "params": {"channel_index": 0, "flow_percent": 10}},
    )
    assert gas.status_code == 202, gas.get_json()
    demos.service.console._collect()
    body = status(demos)
    assert body["furnace"]["sv"] == 120
    assert body["furnace"]["flow"]["gas_channels"][0]["gas_name"] == "Demo gas 1"
    stopped = post(demos, "stop/furnace", page="console-page-b")
    assert stopped.get_json()["outcome"] == "confirmed"
    assert demos.furnace.status()["control_mode"] == "STOPPED"


def test_gas_stop_closes_1_channel_without_control_and_reports_the_valves(demos):
    assert post(demos, "control/furnace/take").status_code == 200
    params = {"channel_index": 1, "flow_percent": 10}
    gas = post(demos, "command/furnace", {"action": "set_gas_flow", "params": params})
    assert gas.status_code == 202, gas.get_json()
    demos.service.console._collect()
    valves = status(demos)["furnace"]["flow"]["gas_valves"]
    assert {"channel_index": 1, "open": True, "verified": True} in valves
    for bad in ({"channel_index": 3}, {"channel_index": True}, {"channel_index": 1, "x": 1}, {}):
        assert post(demos, "ws/process/gas-stop", bad).status_code == 400
    # Another page without furnace control closes the channel.
    response = post(demos, "ws/process/gas-stop", {"channel_index": 1}, page="console-page-b")
    assert response.status_code == 200
    assert response.get_json() == {"channel_index": 1, "outcome": "confirmed"}
    demos.service.console._collect()
    valves = status(demos)["furnace"]["flow"]["gas_valves"]
    assert {"channel_index": 1, "open": False, "verified": True} in valves


def test_furnace_demo_sequence_round_trip(demos):
    assert post(demos, "control/furnace/take").status_code == 200
    steps = [{"temperature_c": 80, "ramp_rate_c_per_min": 5, "hold_minutes": 1}]
    saved = post(demos, "command/furnace", {"action": "set_sequence", "params": {"steps": steps}})
    assert saved.status_code == 202, saved.get_json()
    demos.service.console._collect()
    sequence = status(demos)["furnace"]["sequence"]
    assert sequence["steps"][0]["temperature_c"] == 80 and sequence["running"] is False


def test_microscope_demo_relays_focus_zero(demos):
    described = demos.client.get("/lab/api/console/describe/microscope", headers=headers())
    commands = described.get_json()["commands"]
    assert {"select_objective", "autofocus", "focus_move", "snapshot"} <= set(commands)
    assert commands["set_focus_zero"]["params"]["properties"] == {}
    assert post(demos, "control/microscope/take").status_code == 200
    zero = post(demos, "command/microscope", {"action": "set_focus_zero", "params": {}})
    assert zero.status_code == 502 and zero.get_json()["outcome"] == "unknown"
    move = post(demos, "command/microscope", {"action": "focus_move", "params": {"steps": 10}})
    # The unstarted demo answers 503: the relay reports unknown and does not retry.
    assert move.status_code == 502 and move.get_json()["outcome"] == "unknown"
    demos.service.console._collect()
    scope = status(demos)["microscope"]
    assert scope["focus_zero_known"] is False and scope["focus_position_pulses"] is None
