"""The service files hold no values of one host and agree on shared paths."""

import shlex
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "config/systemd"
HOST_VALUES = ("100.64.", "/olares", "/opt/mg400-", "/root/")
with (ROOT / "deploy/services.toml").open("rb") as stream:
    REGISTRY = tomllib.load(stream)["service"]
SERVICES = tuple(unit for entry in REGISTRY for unit in entry["units"])


def unit_sections(name):
    sections = {}
    for line in (UNITS / name).read_text().splitlines():
        if line.startswith("["):
            section = sections.setdefault(line.strip("[]"), {})
        elif "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            section.setdefault(key, []).append(value)
    return sections


def command(name):
    return shlex.split(unit_sections(name)["Service"]["ExecStart"][0])


def test_the_directory_holds_exactly_the_known_units():
    names = {path.name for path in UNITS.iterdir()}
    assert names == set(SERVICES)


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_runs_as_the_panel_user_from_fixed_paths(name):
    service = unit_sections(name)["Service"]
    assert (service["User"], service["Group"]) == (["1000"], ["1000"])
    program = command(name)[0]
    if name == "mg400-vision.service":
        assert program == "/opt/mg400/.venv-vision/bin/python"
    else:
        assert program.startswith(("/opt/mg400/.venv/bin/", "/usr/local/lib/mg400/")), program
    assert service["UMask"] == ["0077"]
    assert service["Restart"] == ["on-failure"]
    # Services start only by hand or on a panel request.
    assert "Install" not in unit_sections(name)


@pytest.mark.parametrize(
    "name",
    [
        "mg400-web-panel.service",
        "mg400-camera-preview.service",
        "mg400-stage-panel.service",
        "mg400-furnace.service",
        "mg400-thermal.service",
        "mg400-vision.service",
        "mg400-microscope.service",
        "mg400-orchestrator.service",
    ],
)
def test_services_with_settings_read_the_shared_environment_file(name):
    sections = unit_sections(name)
    assert sections["Service"]["EnvironmentFile"] == ["/etc/mg400/mg400.env"]
    assert sections["Service"]["WorkingDirectory"] == ["/opt/mg400"]
    assert "network-online.target" in sections["Unit"]["Wants"]


def test_no_unit_names_a_service_token():
    for name in SERVICES:
        assert "TOKEN" not in (UNITS / name).read_text(), name


def test_fixed_defaults_come_from_the_code():
    panel = unit_sections("mg400-web-panel.service")["Service"]
    assert "Environment" not in panel
    assert "--base-path" not in command("mg400-web-panel.service")
    orchestrator = unit_sections("mg400-orchestrator.service")["Service"]
    assert "Environment" not in orchestrator
    assert orchestrator["ExecStart"] == [
        "/opt/mg400/.venv/bin/python -m orchestrator serve --base-path /lab/"
    ]


def test_stage_service_takes_every_setting_from_the_environment():
    sections = unit_sections("mg400-stage-panel.service")
    service = sections["Service"]
    # No options: the MG400_STAGE_* settings come from the environment file.
    assert service["ExecStart"] == ["/opt/mg400/.venv/bin/stage-panel"]
    assert service["SupplementaryGroups"] == ["dialout"]
    # A startup refusal exits with status 2: a configuration error must not restart forever.
    assert service["RestartPreventExitStatus"] == ["2"]


def test_web_panel_allows_the_stop_confirmation_before_systemd_kills_it():
    service = unit_sections("mg400-web-panel.service")["Service"]
    assert int(service["TimeoutStopSec"][0]) > 60
    assert service["RestartPreventExitStatus"] == ["2"]


def test_hardware_services_get_only_their_device_groups():
    groups = {
        name: unit_sections(name)["Service"].get("SupplementaryGroups", []) for name in SERVICES
    }
    assert groups == {
        "mg400-web-panel.service": [],
        "mg400-camera-capture.service": ["video"],
        "mg400-camera-preview.service": [],
        "mg400-stage-panel.service": ["dialout"],
        "mg400-furnace.service": ["dialout"],
        "mg400-thermal.service": [],
        "mg400-vision.service": [],
        "mg400-microscope.service": ["dialout video"],
        "mg400-orchestrator.service": [],
        "mg400-proxy.service": [],
    }


def test_vision_uses_the_isolated_environment():
    service = unit_sections("mg400-vision.service")["Service"]
    assert command("mg400-vision.service") == ["/opt/mg400/.venv-vision/bin/python", "-m", "vision"]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert int(service["TimeoutStopSec"][0]) >= 30
    entry = next(item for item in REGISTRY if item["name"] == "vision")
    assert entry == {
        "name": "vision",
        "units": ["mg400-vision.service"],
        "optional": True,
        "venv": ".venv-vision",
        "groups": ["vision", "vision-gpu"],
        "assets": ["vision-weights"],
    }


def test_microscope_uses_verified_sdk_path_and_is_installed_by_default():
    service = unit_sections("mg400-microscope.service")["Service"]
    assert command("mg400-microscope.service") == [
        "/opt/mg400/.venv/bin/python",
        "-m",
        "microscope",
    ]
    assert service["Environment"] == ["LD_LIBRARY_PATH=/usr/local/lib/mg400"]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert service["TimeoutStopSec"] == ["infinity"]
    assert next(item for item in REGISTRY if item["name"] == "microscope") == {
        "name": "microscope",
        "units": ["mg400-microscope.service"],
        "optional": False,
        "venv": ".venv",
        "groups": ["microscope"],
        "assets": [],
    }


def test_only_microscope_unit_sets_the_native_library_search_path():
    for name in SERVICES:
        definitions = unit_sections(name)["Service"].get("Environment", [])
        library_paths = [value for value in definitions if "LD_LIBRARY_PATH" in value]
        assert library_paths == (
            ["LD_LIBRARY_PATH=/usr/local/lib/mg400"] if name == "mg400-microscope.service" else []
        )


def test_furnace_service_uses_settings_and_allows_heat_and_gas_shutdown():
    service = unit_sections("mg400-furnace.service")["Service"]
    assert command("mg400-furnace.service") == ["/opt/mg400/.venv/bin/python", "-m", "furnace"]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert int(service["TimeoutStopSec"][0]) >= 120
    assert next(item for item in REGISTRY if item["name"] == "furnace")["optional"] is False


def test_orchestrator_drains_shutdown():
    service = unit_sections("mg400-orchestrator.service")["Service"]
    assert command("mg400-orchestrator.service") == [
        "/opt/mg400/.venv/bin/python",
        "-m",
        "orchestrator",
        "serve",
        "--base-path",
        "/lab/",
    ]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert service["TimeoutStopSec"] == ["infinity"]


def test_camera_capture_reads_the_generated_configuration():
    assert command("mg400-camera-capture.service") == [
        "/usr/local/lib/mg400/go2rtc",
        "-config",
        "/etc/mg400/go2rtc.yaml",
        "-config",
        "/var/lib/mg400/camera/still.yaml",
    ]
    service = unit_sections("mg400-camera-capture.service")["Service"]
    assert service["ExecStartPre"] == ["/bin/rm -f /var/lib/mg400/camera/still.yaml"]
    preview = unit_sections("mg400-camera-preview.service")["Unit"]
    assert preview["Requires"] == ["mg400-camera-capture.service"]


def test_service_files_and_code_hold_no_host_values():
    code = [
        *(ROOT / "src").rglob("*"),
        *(ROOT / "apps").rglob("*"),
        *(ROOT / "scripts").rglob("*"),
        *(ROOT / "deploy").rglob("*"),
        *UNITS.iterdir(),
    ]
    for path in code:
        suffixes = {".service", ".path", ".py", ".html", ".js", ".css", ".svg", ".example"}
        if path.suffix in suffixes or path.name == "labctl":
            text = path.read_text()
            assert [value for value in HOST_VALUES if value in text] == [], path


def test_proxy_runs_only_explicit_binary_without_outbound_proxy_settings():
    service = unit_sections("mg400-proxy.service")["Service"]
    assert command("mg400-proxy.service") == [
        "/usr/local/lib/mg400/caddy",
        "run",
        "--config",
        "/etc/mg400/Caddyfile",
        "--adapter",
        "caddyfile",
    ]
    assert service["EnvironmentFile"] == ["/etc/mg400/mg400.env"]
    assert service["WorkingDirectory"] == ["/opt/mg400"]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert set(service["UnsetEnvironment"][0].split()) == {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
    entry = next(item for item in REGISTRY if item["name"] == "proxy")
    assert entry["optional"] is False and entry["groups"] == []


def test_thermal_service_uses_primary_group_usb_access():
    service = unit_sections("mg400-thermal.service")["Service"]
    assert command("mg400-thermal.service") == ["/opt/mg400/.venv/bin/thermal-cam"]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert service["TimeoutStopSec"] == ["30"]
    assert not service.get("SupplementaryGroups")
    entry = next(entry for entry in REGISTRY if entry["name"] == "thermal")
    assert entry == {
        "name": "thermal",
        "units": ["mg400-thermal.service"],
        "optional": True,
        "venv": ".venv",
        "groups": ["thermal"],
        "assets": [],
    }
