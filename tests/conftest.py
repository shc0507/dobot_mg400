"""Shared fixtures and private orchestrator configuration for offline tests."""

import pytest


@pytest.fixture(autouse=True)
def private_orchestrator_zones(request):
    """Never let orchestrator tests inherit an installed host's zone files."""
    if not request.path.name.startswith("test_orchestrator_"):
        return
    from orchestrator import zones

    tmp_path_factory = request.getfixturevalue("tmp_path_factory")
    monkeypatch = request.getfixturevalue("monkeypatch")
    directory = tmp_path_factory.mktemp("orchestrator-zones")
    directory.chmod(0o700)
    config = directory / "config.toml"
    config.write_text("[zones]\n")
    config.chmod(0o600)
    monkeypatch.setattr(zones, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(zones, "DEFAULT_STATE", directory / "occupancy.json")
    monkeypatch.setenv("MG400_ORCH_ZONES_FILE", str(config))
    monkeypatch.setenv("MG400_ORCH_ZONE_STATE_FILE", str(directory / "occupancy.json"))
    return directory


@pytest.fixture
def isolated_controller_locks(monkeypatch, tmp_path):
    """Keep controller lock files in the test's own directory."""
    monkeypatch.setenv("MG400_CONTROLLER_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture
def closed_clients():
    """Close the WebSocket test clients that a test leaves open."""
    from panel_ws import OPEN_CLIENTS

    yield
    while OPEN_CLIENTS:
        OPEN_CLIENTS.pop().discard()


@pytest.fixture
def make_sim_rig(monkeypatch, tmp_path):
    """Return a factory of panel rigs that control the simulated MG400 of sim_mg400."""
    from sim_mg400 import rig_factory

    yield from rig_factory(monkeypatch, tmp_path)


@pytest.fixture
def stage_rig():
    """Return a stage controller connected to a recording demo board, the board and its clock."""
    from stage_helpers import Clock, RecordingTransport

    from stage_controller.controller import MotorController

    clock = Clock()
    transport = RecordingTransport(clock)
    controller = MotorController(demo=True, transport_factory=lambda _port: transport, clock=clock)
    controller.connect("DEMO")
    yield controller, transport, clock
    controller.close()


@pytest.fixture
def stage_ports(monkeypatch):
    """Make pyserial list the returned ports instead of this machine's serial ports."""
    from serial.tools import list_ports

    ports = []
    monkeypatch.setattr(list_ports, "comports", lambda: list(ports))
    return ports


@pytest.fixture
def stage_usb(stage_ports, tmp_path):
    """List two Arduino boards and the gripper converter as device files in tmp_path.

    Return their /dev/serial/by-id style links by name: "arduino", "other" and "gripper".
    """
    from stage_helpers import serial_port

    from stage_controller.controller import GRIPPER_CONVERTER

    by_id = tmp_path / "by-id"
    by_id.mkdir()
    links = {}
    for name, device, identity, description, link in (
        ("arduino", "ttyACM0", (0x2341, 0x0043, "ARDUINO1"), "Arduino Uno", "Arduino_Uno"),
        ("other", "ttyACM1", (0x2341, 0x0042, "ARDUINO2"), "Arduino Mega", "Arduino_Mega"),
        ("gripper", "ttyUSB0", GRIPPER_CONVERTER, "FT232R USB UART", "FTDI_FT232R_USB_UART"),
    ):
        (tmp_path / device).touch()
        links[name] = by_id / f"usb-{link}_{identity[2]}-if00"
        links[name].symlink_to(tmp_path / device)
        stage_ports.append(serial_port(str(tmp_path / device), *identity, description=description))
    return {name: str(link) for name, link in links.items()}
