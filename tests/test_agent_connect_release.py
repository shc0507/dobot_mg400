"""Release identity follows source and contracts, without Git or repeated file reads."""

from types import SimpleNamespace

from flask import Flask

from orchestrator.agent_connect import release
from orchestrator.agent_connect.routes import register_routes
from orchestrator.agent_connect.stream import BroadcastHub


def test_source_changes_and_rollback_have_reproducible_release_identity(tmp_path):
    code = tmp_path / "runtime.py"
    code.write_text("value = 1\n")
    initial = release.source_bundle(tmp_path)
    code.write_text("value = 2\n")
    changed = release.source_bundle(tmp_path)
    code.write_text("value = 1\n")
    restored = release.source_bundle(tmp_path)
    assert initial["digest"] != changed["digest"]
    assert restored == initial
    assert initial["complete"] and initial["file_count"] == 1
    (tmp_path / "runtime-settings.json").write_text('{"secret": "private"}')
    assert release.source_bundle(tmp_path) == initial


def test_source_scan_reports_limits_and_never_claims_complete(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("a" * 20)
    (tmp_path / "b.py").write_text("b")
    monkeypatch.setattr(release, "MAX_FILE_BYTES", 10)
    bounded = release.source_bundle(tmp_path)
    assert not bounded["complete"] and bounded["file_count"] == 1
    monkeypatch.setattr(release, "MAX_FILES", 1)
    assert not release.source_bundle(tmp_path)["complete"]
    assert release.source_bundle(tmp_path / "missing")["digest"] is None


def test_running_source_is_computed_once_and_keeps_startup_identity(monkeypatch):
    calls = []
    release.running_source_bundle.cache_clear()
    try:
        monkeypatch.setattr(
            release, "source_bundle", lambda root: calls.append(root) or {"digest": "startup"}
        )
        assert release.running_source_bundle() == {"digest": "startup"}
        assert release.running_source_bundle() == {"digest": "startup"}
        assert len(calls) == 1
    finally:
        release.running_source_bundle.cache_clear()


def test_release_binds_read_and_command_contracts_and_reports_missing_source():
    catalog = {"contract_id": "sha256:commands-1"}
    host = SimpleNamespace(commands=lambda: SimpleNamespace(catalog=lambda: catalog))
    hub = SimpleNamespace(
        contract_id="sha256:read-1", source_bundle={"digest": "sha256:source", "complete": True}
    )
    initial = release.metadata(hub, host)
    catalog["contract_id"] = "sha256:commands-2"
    assert release.metadata(hub, host)["release_id"] != initial["release_id"]
    catalog["contract_id"] = "sha256:commands-1"
    assert release.metadata(hub, host) == initial
    hub.contract_id = "sha256:read-2"
    assert release.metadata(hub, host)["release_id"] != initial["release_id"]
    hub.source_bundle["complete"] = False
    assert not release.metadata(hub, host)["complete"]


def test_discovery_exposes_host_policy_and_current_release_contracts():
    app = Flask(__name__)
    hub = BroadcastHub(lambda: {"mode": "live", "devices": {}})
    host = SimpleNamespace(
        commands_enabled=True,
        physical_commands_enabled=True,
        furnace_control_enabled=True,
        stop_enabled=True,
        equipment_mode="physical",
        authority=lambda: SimpleNamespace(boot_id="command-boot"),
        commands=lambda: SimpleNamespace(catalog=lambda: {"contract_id": "sha256:command"}),
    )
    app.extensions["agent_control"] = host
    register_routes(app, hub)
    value = app.test_client().get("/lab/api/agent/discovery").get_json()
    assert value["control"]["version"] == 6 and value["control"]["phase"] == 8
    assert value["control"]["physical_commands_enabled"]
    assert value["control"]["furnace_control_enabled"]
    assert value["release"]["read_contract_id"] == hub.contract_id
    assert value["release"]["command_contract_id"] == "sha256:command"
    assert value["release"]["source"]["complete"]
    assert value["limits"]["supplement_interval_s"] == 1
    assert not {"api/detect", "api/labels"} & set(value["endpoints"].values())
    hub.close()
