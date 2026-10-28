"""The stage panel service file and code hold no values of one host."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "config/stage_panel/mg400-stage-panel.service"
HOST_VALUES = ("100.64.", "/olares", "/opt/mg400-")


def unit_sections():
    sections = {}
    for line in UNIT.read_text().splitlines():
        if line.startswith("["):
            section = sections.setdefault(line.strip("[]"), {})
        elif "=" in line:
            key, value = line.split("=", 1)
            section.setdefault(key, []).append(value)
    return sections


def test_service_reads_its_settings_from_the_shared_environment_file():
    sections = unit_sections()
    service = sections["Service"]
    assert service["EnvironmentFile"] == ["/etc/mg400/mg400.env"]
    assert service["WorkingDirectory"] == ["/opt/mg400"]
    # No options: the MG400_STAGE_* settings come from the environment file.
    assert service["ExecStart"] == ["/opt/mg400/.venv/bin/stage-panel"]
    assert (service["User"], service["Group"]) == (["1000"], ["1000"])
    assert service["SupplementaryGroups"] == ["dialout"]
    assert sections["Unit"]["Wants"] == sections["Unit"]["After"] == ["network-online.target"]
    # The service starts only by hand.
    assert "Install" not in sections


def test_service_file_and_code_hold_no_host_values():
    code = (ROOT / "src/stage_controller").rglob("*")
    for path in [UNIT, *code]:
        if path.suffix in {".service", ".py", ".html", ".js", ".css", ".svg"}:
            text = path.read_text()
            assert [value for value in HOST_VALUES if value in text] == [], path
