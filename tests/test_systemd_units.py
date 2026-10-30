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
        ("mg400-web-panel.service", ["MG400_STAGE_TOKEN", "MG400_FURNACE_TOKEN"]),
        ("mg400-stage-panel.service", ["MG400_PANEL_TOKEN", "MG400_FURNACE_TOKEN"]),
        (
            "mg400-camera-preview.service",
            ["MG400_PANEL_TOKEN", "MG400_STAGE_TOKEN", "MG400_FURNACE_TOKEN"],
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
    }


def test_furnace_service_uses_settings_and_allows_heat_and_gas_shutdown():
    service = unit_sections("mg400-furnace.service")["Service"]
    assert command("mg400-furnace.service") == ["/opt/mg400/.venv/bin/python", "-m", "furnace"]
    assert service["RestartPreventExitStatus"] == ["2"]
    assert int(service["TimeoutStopSec"][0]) >= 120
    assert "MG400_FURNACE_TOKEN" not in service["UnsetEnvironment"][0].split()


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
