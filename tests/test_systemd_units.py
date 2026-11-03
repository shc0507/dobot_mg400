"""The service files hold no values of one host and agree on shared paths."""

import shlex
import tomllib
from pathlib import Path

import pytest

from mg400.web_gripper import COLLECTOR_REQUEST

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "config/systemd"
HOST_VALUES = ("100.64.", "/olares", "/opt/mg400-", "/root/")
with (ROOT / "deploy/services.toml").open("rb") as stream:
    REGISTRY = tomllib.load(stream)["service"]
SERVICES = tuple(unit for entry in REGISTRY for unit in entry["units"])
FEEDBACK = "/var/lib/mg400/work/gripper_feedback.json"
ORCHESTRATOR_TOKENS = [
    "MG400_ORCH_TOKEN",
    *[
        f"MG400_ORCH_{name}_TOKEN"
        for name in ("ROBOT", "STAGE", "THERMAL", "VISION", "FURNACE", "MICROSCOPE")
    ],
]


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
    assert names == {*SERVICES, "mg400-gripper-collector.path"}


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
        "mg400-orchestrator.service",
        "mg400-gateway.service",
    ],
)
def test_services_with_settings_read_the_shared_environment_file(name):
    sections = unit_sections(name)
    assert sections["Service"]["EnvironmentFile"] == ["/etc/mg400/mg400.env"]
    assert sections["Service"]["WorkingDirectory"] == ["/opt/mg400"]
    assert "network-online.target" in sections["Unit"]["Wants"]


@pytest.mark.parametrize(
    ("name", "unset"),
    [
        (
            "mg400-web-panel.service",
            [
                "MG400_STAGE_TOKEN",
                "MG400_FURNACE_TOKEN",
                "MG400_THERMAL_TOKEN",
                *ORCHESTRATOR_TOKENS,
                "MG400_GATEWAY_TOKEN",
            ],
        ),
        (
            "mg400-stage-panel.service",
            [
                "MG400_PANEL_TOKEN",
                "MG400_FURNACE_TOKEN",
                "MG400_THERMAL_TOKEN",
                *ORCHESTRATOR_TOKENS,
                "MG400_GATEWAY_TOKEN",
            ],
        ),
        (
            "mg400-camera-preview.service",
            [
                "MG400_PANEL_TOKEN",
                "MG400_STAGE_TOKEN",
                "MG400_FURNACE_TOKEN",
                "MG400_THERMAL_TOKEN",
                *ORCHESTRATOR_TOKENS,
                "MG400_GATEWAY_TOKEN",
            ],
        ),
        (
            "mg400-furnace.service",
            [
                "MG400_PANEL_TOKEN",
                "MG400_STAGE_TOKEN",
                "MG400_THERMAL_TOKEN",
                "MG400_VISION_TOKEN",
                "MG400_MICROSCOPE_TOKEN",
                "MG400_ORCH_TOKEN",
                "MG400_GATEWAY_TOKEN",
                "MG400_ORCH_ROBOT_TOKEN",
                "MG400_ORCH_STAGE_TOKEN",
                "MG400_ORCH_THERMAL_TOKEN",
                "MG400_ORCH_VISION_TOKEN",
                "MG400_ORCH_FURNACE_TOKEN",
                "MG400_ORCH_MICROSCOPE_TOKEN",
            ],
        ),
    ],
)
def test_services_drop_the_tokens_they_do_not_check(name, unset):
    service = unit_sections(name)["Service"]
    assert service["UnsetEnvironment"][0].split() == unset


def test_camera_preview_retains_vision_token_for_the_loopback_proxy():
    service = unit_sections("mg400-camera-preview.service")["Service"]
    assert service["EnvironmentFile"] == ["/etc/mg400/mg400.env"]
    unset = {value for line in service.get("UnsetEnvironment", []) for value in line.split()}
    assert "MG400_VISION_TOKEN" not in unset
    assert {"MG400_PANEL_TOKEN", "MG400_STAGE_TOKEN"} <= unset


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


def test_panel_collector_and_request_watcher_share_one_work_directory():
    panel = command("mg400-web-panel.service")
    collector = command("mg400-gripper-collector.service")
    assert panel[panel.index("--gripper-feedback") + 1] == FEEDBACK
    assert collector[collector.index("--output") + 1] == FEEDBACK
    assert "--commands" in collector
    path = unit_sections("mg400-gripper-collector.path")
    # WebGripper writes the request file next to the feedback file.
    assert path["Path"]["PathChanged"] == [str(Path(FEEDBACK).with_name(COLLECTOR_REQUEST))]
    assert path["Path"]["Unit"] == ["mg400-gripper-collector.service"]
    assert path["Install"]["WantedBy"] == ["multi-user.target"]


def test_hardware_services_get_only_their_device_groups():
    groups = {
        name: unit_sections(name)["Service"].get("SupplementaryGroups", []) for name in SERVICES
    }
    assert groups == {
        "mg400-web-panel.service": [],
        "mg400-gripper-collector.service": ["dialout"],
        "mg400-camera-capture.service": ["video"],
        "mg400-camera-preview.service": [],
        "mg400-stage-panel.service": ["dialout"],
        "mg400-furnace.service": ["dialout"],
        "mg400-thermal.service": [],
        "mg400-vision.service": [],
        "mg400-orchestrator.service": [],
        "mg400-proxy.service": [],
        "mg400-gateway.service": [],
    }


def test_vision_uses_the_isolated_environment_and_keeps_only_its_token():
    service = unit_sections("mg400-vision.service")["Service"]
    assert command("mg400-vision.service") == ["/opt/mg400/.venv-vision/bin/python", "-m", "vision"]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert int(service["TimeoutStopSec"][0]) >= 30
    excluded = set(service["UnsetEnvironment"][0].split())
    assert excluded == {
        *(
            f"MG400_{name}_TOKEN"
            for name in ("PANEL", "STAGE", "THERMAL", "FURNACE", "MICROSCOPE", "GATEWAY")
        ),
        *ORCHESTRATOR_TOKENS,
    }
    entry = next(item for item in REGISTRY if item["name"] == "vision")
    assert entry == {
        "name": "vision",
        "units": ["mg400-vision.service"],
        "optional": True,
        "venv": ".venv-vision",
        "groups": ["vision", "vision-gpu"],
        "token": "MG400_VISION_TOKEN",
        "port": 8771,
        "assets": ["vision-weights"],
    }


def test_furnace_service_uses_settings_and_allows_heat_and_gas_shutdown():
    service = unit_sections("mg400-furnace.service")["Service"]
    assert command("mg400-furnace.service") == ["/opt/mg400/.venv/bin/python", "-m", "furnace"]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert int(service["TimeoutStopSec"][0]) >= 120
    assert "MG400_FURNACE_TOKEN" not in service["UnsetEnvironment"][0].split()


def test_orchestrator_keeps_operator_proposal_and_alias_tokens_and_drains_shutdown():
    service = unit_sections("mg400-orchestrator.service")["Service"]
    assert command("mg400-orchestrator.service") == [
        "/opt/mg400/.venv/bin/python",
        "-m",
        "orchestrator",
        "serve",
    ]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert service["TimeoutStopSec"] == ["infinity"]
    assert set(service["UnsetEnvironment"][0].split()) == {
        f"MG400_{name}_TOKEN"
        for name in ("PANEL", "STAGE", "THERMAL", "VISION", "FURNACE", "MICROSCOPE")
    }
    assert not set(ORCHESTRATOR_TOKENS) & set(service["UnsetEnvironment"][0].split())
    assert "MG400_GATEWAY_TOKEN" not in service["UnsetEnvironment"][0].split()


def test_gateway_has_no_device_groups_and_never_retains_operator_credential():
    service = unit_sections("mg400-gateway.service")["Service"]
    assert command("mg400-gateway.service") == ["/opt/mg400/.venv/bin/python", "-m", "gateway"]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert not service.get("SupplementaryGroups")
    assert set(service["UnsetEnvironment"][0].split()) == {
        f"MG400_{name}_TOKEN"
        for name in ("PANEL", "STAGE", "THERMAL", "VISION", "FURNACE", "MICROSCOPE", "ORCH")
    }
    assert "MG400_GATEWAY_TOKEN" not in service["UnsetEnvironment"][0].split()


def test_camera_capture_reads_the_generated_configuration():
    assert command("mg400-camera-capture.service") == [
        "/usr/local/lib/mg400/go2rtc",
        "-config",
        "/etc/mg400/go2rtc.yaml",
    ]
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
        if path.suffix in suffixes or path.name == "mg400ctl":
            text = path.read_text()
            assert [value for value in HOST_VALUES if value in text] == [], path


def test_proxy_runs_only_explicit_binary_and_never_receives_service_credentials():
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
    excluded = set(service["UnsetEnvironment"][0].split())
    expected = {entry["token"] for entry in REGISTRY if entry["token"]}
    assert expected | set(ORCHESTRATOR_TOKENS) <= excluded
    assert {"MG400_THERMAL_TOKEN", "MG400_VISION_TOKEN", "MG400_MICROSCOPE_TOKEN"} <= excluded
    assert {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    } <= excluded
    entry = next(item for item in REGISTRY if item["name"] == "proxy")
    assert entry["optional"] is True and entry["token"] == "" and entry["groups"] == []


def test_installed_robot_panel_uses_existing_base_path_option():
    arguments = command("mg400-web-panel.service")
    assert arguments[arguments.index("--base-path") + 1] == "${MG400_PANEL_BASE_PATH}"


def test_thermal_service_keeps_only_its_token_and_uses_primary_group_usb_access():
    service = unit_sections("mg400-thermal.service")["Service"]
    assert command("mg400-thermal.service") == ["/opt/mg400/.venv/bin/thermal-cam"]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert service["TimeoutStopSec"] == ["30"]
    assert not service.get("SupplementaryGroups")
    assert set(service["UnsetEnvironment"][0].split()) == {
        f"MG400_{name}_TOKEN"
        for name in ("PANEL", "STAGE", "VISION", "FURNACE", "MICROSCOPE", "GATEWAY")
    } | set(ORCHESTRATOR_TOKENS)
    assert "MG400_THERMAL_TOKEN" not in service["UnsetEnvironment"][0].split()
    entry = next(entry for entry in REGISTRY if entry["name"] == "thermal")
    assert entry == {
        "name": "thermal",
        "units": ["mg400-thermal.service"],
        "optional": True,
        "venv": ".venv",
        "groups": ["thermal"],
        "token": "MG400_THERMAL_TOKEN",
        "port": 8768,
        "assets": [],
    }
