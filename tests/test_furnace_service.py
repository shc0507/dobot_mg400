"""Offline lifecycle tests through real drivers and memory-only transports."""

import os
import socket
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

import minimalmodbus
import pytest
import serial

from furnace import controller as p
from furnace.config import FurnaceConfig
from furnace.controller import Furnace, MFCFlowController
from furnace.service import FRAME_METADATA_FIELDS, FurnaceService
from furnace.sim import FurnaceSerial, MFCSerial
from furnace.state import StateError, StateStore
from labkit.errors import ServiceError

Instrument = minimalmodbus.Instrument


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def wall(self):
        return 1_000_000 + self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture(autouse=True)
def offline(monkeypatch, clock):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real devices, network, and automatic threads are forbidden")

    monkeypatch.setattr(serial, "Serial", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    monkeypatch.setattr(p, "time", SimpleNamespace(time=clock.wall, sleep=clock.advance))
    monkeypatch.setattr(
        minimalmodbus, "time", SimpleNamespace(monotonic=clock, sleep=lambda _: None)
    )
    monkeypatch.setattr(minimalmodbus, "_latest_read_times", {})
    monkeypatch.setattr(minimalmodbus, "_serialports", {})
    for name in (
        "MG400_FURNACE_SERIAL",
        "MG400_FURNACE_SLAVE",
        "MG400_MFC_SERIAL",
        "MG400_FURNACE_MAX_C",
        "MG400_FURNACE_MAX_RAMP",
        "MG400_FURNACE_GAS_MAP",
        "MG400_FURNACE_IDLE_GAS",
        "MG400_FURNACE_ORPHAN_GRACE_MIN",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def lab(tmp_path, clock):
    transport = FurnaceSerial(clock=clock, time_constant_s=1)
    mfc = MFCSerial()
    instrument = Instrument(transport, 1)
    store = StateStore(tmp_path / "furnace" / "state.json")
    factories = []

    def make(**kwargs):
        def attach(**settings):
            factories.append(("furnace", settings["port"]))
            transport.open()
            return Furnace.attach(instrument=instrument, **settings)

        def flow(**settings):
            factories.append(("flow", settings["port"]))
            mfc.open()
            return MFCFlowController(serial_connection=mfc, **settings)

        defaults = dict(
            config=FurnaceConfig(port="sim://furnace", flow_port="sim://mfc"),
            gas_map={0: "test gas A", 1: "test gas B", 2: "test gas C"},
            state_store=store,
            furnace_factory=attach,
            flow_factory=flow,
            clock=clock,
            wall_clock=clock.wall,
        )
        defaults.update(kwargs)
        return FurnaceService(**defaults)

    service = make()
    service.start(background=False)
    service.lease.acquire("operator")
    return SimpleNamespace(
        service=service,
        make=make,
        serial=transport,
        mfc=mfc,
        instrument=instrument,
        clock=clock,
        store=store,
        factories=factories,
    )


def recipe(target=30, hold=0.1, **extra):
    return [dict(temperature_c=target, ramp_rate_c_per_min=60, hold_minutes=hold, **extra)]


def enter_hold(lab):
    lab.clock.advance(10)
    result = lab.service.poll_once()
    assert result["sequence"]["phase"] == "holding", result
    return result


def writes(transport):
    return [packet for packet in transport.requests if len(packet) == 8 and packet[1] == 6]


def test_import_and_constructor_are_inert():
    code = """
import builtins, os, socket, threading, serial
from pathlib import Path
def forbidden(*a, **k):
    raise AssertionError("unexpected external action")
serial.Serial = socket.socket = threading.Thread.start = os.open = forbidden
builtins.open = Path.write_text = Path.write_bytes = forbidden
from furnace.service import FurnaceService
service = FurnaceService()
assert not service.connected
assert service._thread is None
"""
    subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True, timeout=10)


# These preserve the seven selected source service test cases, with leases,
# configurable ports, safe memory transports, and explicit readback semantics.
@pytest.mark.parametrize("channel", range(3))
def test_service_open_uses_100_percent_setpoint_for_each_channel(lab, channel):
    result = lab.service.set_gas_valve(channel, True, client="operator")
    assert result["gas_channels"][channel]["target_percent"] == 100
    assert lab.mfc.channel_state(7 + channel)["actual_percent"] == 100


@pytest.mark.parametrize("opened", ["false", 0, 1, None])
def test_service_open_rejects_non_boolean_before_sending(lab, opened):
    with pytest.raises(ValueError, match="true or false"):
        lab.service.set_gas_valve(2, opened, client="operator")
    assert not lab.mfc.requests


def test_inspection_preserves_target_when_write_only_channel_is_silent(lab):
    lab.mfc.reply_ascii = False
    result = lab.service.set_gas_flow(2, 20, client="operator")
    snapshot = result["gas_channels"][2]
    assert snapshot["target_percent"] == 20
    assert snapshot["actual_percent"] is None
    assert snapshot["readback"] == "unverified"
    assert snapshot["error"] is None
    assert not lab.service.status()["locked"]
    lab.service.inspect_gas_flow(2, client="operator")
    assert not lab.service.status()["locked"]


def test_service_sets_gas_flow_and_opens_the_valve_in_same_call(lab):
    lab.service.set_gas_flow(0, 30, client="operator")
    packets = list(lab.mfc.requests)
    assert packets[0] == b"\x0207SFR$BB8\r"
    assert packets[1][0:2] == bytes([4, 5])
    assert lab.mfc.channel_state(7)["actual_percent"] == 30
    lab.service.set_gas_flow(0, 0, client="operator")
    assert lab.mfc.channel_state(7)["actual_percent"] == 0


def test_service_rejects_percentage_above_full_scale_before_connecting(lab):
    with pytest.raises(ValueError, match="Flow percentage"):
        lab.service.set_gas_flow(0, 100.01, client="operator")
    assert lab.factories == [("furnace", "sim://furnace")]


def test_frame_metadata_includes_gas_setpoints_in_percent(lab):
    for channel, percent in enumerate([20, 50, 60]):
        lab.service.set_gas_flow(channel, percent, client="operator")
    metadata = lab.service.frame_metadata()
    assert set(metadata) == set(FRAME_METADATA_FIELDS)
    assert [metadata[f"gas_{i}_target_percent"] for i in (1, 2, 3)] == [20, 50, 60]


def test_service_connect_uses_configured_furnace_ports(lab):
    lab.service.connect(client="operator")
    lab.service.connect_flow_controller(port="sim://selected-mfc", client="operator")
    assert lab.factories == [("furnace", "sim://furnace"), ("flow", "sim://selected-mfc")]
    assert lab.service.flow_port == "sim://selected-mfc"


def test_lease_loss_mid_hold_completes_bounded_run(lab):
    lab.service.start_sequence(recipe(), client="operator")
    enter_hold(lab)
    assert lab.service.lease.holder is None
    before = len(writes(lab.serial))
    lab.clock.advance(2)
    lab.service.poll_once()
    assert len(writes(lab.serial)) == before
    lab.clock.advance(5)
    result = lab.service.poll_once()
    assert result["sequence"]["phase"] == "complete"
    assert result["run_state_code"] == 1
    assert not result["locked"]


def test_hold_timer_waits_for_step2_and_target_tolerance(lab):
    lab.service.start_sequence(recipe(target=100), client="operator")
    p.write_register(lab.instrument, p.PROGRAM_STEP_REGISTER, 2)
    result = lab.service.poll_once()
    assert result["run_state_code"] == 2
    assert result["sequence"]["phase"] == "ramping"
    assert result["sequence"]["hold_remaining_seconds"] is None


@pytest.mark.parametrize("release", [False, True])
def test_manual_client_loss_grace_stops_and_applies_idle(lab, release):
    service = lab.make(orphan_grace_min=0.1, idle_gas={0: 10})
    service.start(background=False)
    service.lease.acquire("operator")
    service.apply_setpoint(30, 60, client="operator")
    if release:
        service.lease.release("operator")
    else:
        lab.clock.advance(4)
    service.poll_once()
    assert service.status()["orphan_time"] is not None
    assert service.status()["run_state_code"] == 0
    lab.clock.advance(7)
    result = service.poll_once()
    assert result["run_state_code"] == 1
    assert result["sequence"]["phase"] == "stopped"
    assert lab.mfc.channel_state(7)["actual_percent"] == 10


def test_new_holder_clears_orphan_grace(lab):
    lab.service.apply_setpoint(30, 60, client="operator")
    lab.clock.advance(4)
    lab.service.poll_once()
    lab.service.lease.acquire("replacement")
    lab.service.poll_once()
    assert lab.service.status()["orphan_time"] is None
    assert lab.store.load()["orphan_time"] is None


@pytest.mark.parametrize(
    "command,args",
    [
        ("apply_setpoint", (30, 5)),
        ("start_sequence", (recipe(),)),
        ("set_gas_flow", (0, 20)),
        ("connect_flow_controller", ()),
    ],
)
def test_non_stop_commands_require_lease(lab, command, args):
    lab.service.lease.release("operator")
    before = (list(lab.serial.requests), list(lab.mfc.requests))
    with pytest.raises(ServiceError):
        getattr(lab.service, command)(*args, client="stranger")
    assert (list(lab.serial.requests), list(lab.mfc.requests)) == before
    assert not lab.service.stop_furnace()["locked"]


def test_stop_bypasses_failed_lease_expiry(lab):
    def failed():
        raise RuntimeError("expiry failed")

    lab.service.apply_setpoint(30, 60, client="operator")
    lab.service.lease.on_expire = failed
    lab.clock.advance(4)
    with pytest.raises(RuntimeError):
        lab.service.lease.poll()
    assert lab.service.stop_furnace()["run_state_code"] == 1


def test_step_gas_and_end_plan_follow_temperature_after_client_loss(lab):
    steps = recipe(30, 0, gas={0: 10}) + recipe(35, 0, gas={0: 20, 1: 30})
    lab.service.start_sequence(steps, end_gas={0: 5}, end_gas_until_c=26, client="operator")
    assert lab.mfc.channel_state(7)["actual_percent"] == 10
    enter_hold(lab)
    lab.service.poll_once()
    assert lab.mfc.channel_state(7)["actual_percent"] == 20
    assert lab.mfc.channel_state(8)["actual_percent"] == 30
    enter_hold(lab)
    result = lab.service.poll_once()
    assert result["sequence"]["phase"] == "cooling"
    assert result["run_state_code"] == 1
    assert lab.mfc.channel_state(7)["actual_percent"] == 5
    assert lab.mfc.channel_state(8)["actual_percent"] == 0
    lab.clock.advance(10)
    result = lab.service.poll_once()
    assert result["sequence"]["phase"] == "complete"
    assert all(lab.mfc.channel_state(unit)["actual_percent"] == 0 for unit in GAS_UNITS)


GAS_UNITS = (7, 8, 9)


@pytest.mark.parametrize("kwargs", [{}, {"end_gas": {}}, {"end_gas_until_c": 30}])
def test_gas_recipe_needs_complete_end_plan_before_io(lab, kwargs):
    before = list(lab.serial.requests)
    with pytest.raises(ValueError):
        lab.service.start_sequence(recipe(gas={0: 20}), client="operator", **kwargs)
    assert list(lab.serial.requests) == before
    assert not lab.mfc.requests


def test_unset_map_refuses_gas_but_stop_remains_available(lab):
    service = lab.make(gas_map={})
    service.start(background=False)
    service.lease.acquire("operator")
    with pytest.raises(ValueError, match="GAS_MAP"):
        service.set_gas_flow(0, 1, client="operator")
    with pytest.raises(ValueError, match="GAS_MAP"):
        service.start_sequence(
            recipe(gas={0: 0}), end_gas={}, end_gas_until_c=30, client="operator"
        )
    assert not lab.mfc.requests
    assert service.stop_furnace()["run_state_code"] == 1
    assert lab.mfc.requests


@pytest.mark.parametrize(
    "temperature,ramp", [(101, 10), (50, 11), (float("nan"), 10), (50, float("inf")), (True, 5)]
)
def test_caps_reject_entire_command_before_writes(lab, temperature, ramp):
    service = lab.make(
        config=FurnaceConfig(port="sim://furnace", flow_port="sim://mfc", max_c=100, max_ramp=10)
    )
    service.lease.acquire("operator")
    before = list(lab.serial.requests)
    with pytest.raises(ValueError):
        service.apply_setpoint(temperature, ramp, client="operator")
    with pytest.raises(ValueError):
        service.start_sequence(
            [dict(temperature_c=temperature, ramp_rate_c_per_min=ramp)], client="operator"
        )
    assert list(lab.serial.requests) == before


@pytest.mark.parametrize("phase", ["ramping", "holding", "manual"])
def test_restart_resumes_matching_state_without_output_writes(lab, phase):
    if phase == "manual":
        lab.service.apply_setpoint(30, 60, client="operator")
    else:
        lab.service.start_sequence(recipe(hold=5), client="operator")
        if phase == "holding":
            enter_hold(lab)
    before = len(writes(lab.serial))
    gas_before = list(lab.mfc.requests)
    resumed = lab.make()
    result = resumed.start(background=False)
    assert not result["locked"], result
    assert result["sequence"]["phase"] == phase
    assert len(writes(lab.serial)) == before
    assert list(lab.mfc.requests) == gas_before
    assert any(
        event["message"] == "Resumed furnace state" for event in resumed.events.since()["events"]
    )
    if phase == "ramping":
        assert resumed.furnace.pending_hold_target == 30
        lab.clock.advance(10)
        assert resumed.poll_once()["sequence"]["phase"] == "holding"


def test_restart_honors_elapsed_hold_and_persists_every_60_seconds(lab):
    lab.service.start_sequence(recipe(hold=5), client="operator")
    enter_hold(lab)
    saved = lab.store.load()
    assert saved["elapsed_s"] == 0
    lab.clock.advance(61)
    lab.service.poll_once()
    saved = lab.store.load()
    assert saved["elapsed_s"] >= 61
    lab.clock.advance(100)
    resumed = lab.make()
    state = resumed.start(background=False)
    assert state["sequence"]["hold_remaining_seconds"] == pytest.approx(139, abs=0.1)


def test_restart_does_not_reset_manual_orphan_deadline(lab):
    service = lab.make(orphan_grace_min=1)
    service.start(background=False)
    service.lease.acquire("operator")
    service.apply_setpoint(30, 60, client="operator")
    lab.clock.advance(4)
    service.poll_once()
    orphan = service.status()["orphan_time"]
    lab.clock.advance(55)
    resumed = lab.make(orphan_grace_min=1)
    resumed.start(background=False)
    assert resumed.status()["orphan_time"] == orphan
    lab.clock.advance(6)
    assert resumed.poll_once()["run_state_code"] == 1


@pytest.mark.parametrize(
    "bad",
    ["missing", "corrupt", "target", "bank", "caps", "identity", "incomplete", "intent", "future"],
)
def test_bad_recovery_stops_locks_and_requires_acknowledgement(lab, bad):
    lab.service.start_sequence(recipe(hold=5), client="operator")
    if bad == "missing":
        lab.store.path.unlink()
    elif bad == "corrupt":
        lab.store.path.write_text('{"invalid"')
    elif bad == "target":
        p.write_register(lab.instrument, p.SV_REGISTER, 600)
    elif bad == "bank":
        p.write_register(lab.instrument, p.PROGRAM_SP2_REGISTER, 600)
    else:
        saved = lab.store.load()
        if bad == "identity":
            saved["identity"]["port"] = "sim://other"
        elif bad == "incomplete":
            del saved["elapsed_s"]
        elif bad == "intent":
            saved["phase"] = "applying"
        elif bad == "future":
            saved["saved_at"] += 100
        lab.store.save(saved)
    kwargs = (
        {}
        if bad != "caps"
        else dict(config=FurnaceConfig(port="sim://furnace", flow_port="sim://mfc", max_c=29))
    )
    resumed = lab.make(**kwargs)
    state = resumed.start(background=False)
    assert state["locked"] and state["run_state_code"] == 1
    assert all(lab.mfc.channel_state(unit)["actual_percent"] == 0 for unit in GAS_UNITS)
    resumed.lease.acquire("operator")
    with pytest.raises(ServiceError):
        resumed.apply_setpoint(25, 10, client="operator")
    assert not resumed.acknowledge(client="operator")["locked"]


def test_stop_startup_does_not_resume_saved_cooling_gas(lab):
    lab.service.start_sequence(
        recipe(hold=0, gas={0: 20}), end_gas={0: 10}, end_gas_until_c=26, client="operator"
    )
    enter_hold(lab)
    lab.service.poll_once()
    assert lab.store.load()["phase"] == "cooling"
    before = (list(lab.mfc.requests), len(writes(lab.serial)))
    result = lab.make().start(background=False)
    assert result["sequence"]["phase"] == "idle"
    assert (list(lab.mfc.requests), len(writes(lab.serial))) == before


def test_fault_lock_survives_stop_and_restart(lab):
    lab.service.start_sequence(recipe(), client="operator")
    saved = lab.store.load()
    saved["phase"] = "applying"
    lab.store.save(saved)
    first = lab.make()
    assert first.start(background=False)["locked"]
    assert first.stop_furnace()["locked"]
    assert lab.make().start(background=False)["locked"]


def test_write_failure_faults_without_replay_on_later_poll(lab, monkeypatch):
    original = lab.mfc.write
    failed = False

    def write(packet):
        nonlocal failed
        if b"SFR$BB8" in packet:
            failed = True
            raise OSError("unknown gas write")
        return original(packet)

    monkeypatch.setattr(lab.mfc, "write", write)
    with pytest.raises(OSError):
        lab.service.start_sequence(
            recipe(gas={0: 30}), end_gas={}, end_gas_until_c=30, client="operator"
        )
    assert failed and lab.service.status()["locked"]
    assert lab.store.load()["phase"] == "fault"
    before = (list(lab.mfc.requests), list(lab.serial.requests))
    lab.clock.advance(100)
    lab.service.poll_once()
    assert before == (list(lab.mfc.requests), list(lab.serial.requests))


def test_shutdown_attempts_all_channels_after_heat_and_coil_failure(lab, monkeypatch):
    lab.service.set_gas_flow(0, 25, client="operator")
    original = lab.mfc.write
    seen = []

    def write(packet):
        seen.append(packet)
        if packet[:2] == bytes([4, 5]) and packet[2:6] == bytes(4):
            raise OSError("coil close failure")
        return original(packet)

    monkeypatch.setattr(lab.mfc, "write", write)
    monkeypatch.setattr(
        lab.service.furnace, "set_stop", lambda: (_ for _ in ()).throw(OSError("heat stop failed"))
    )
    result = lab.service.close()
    assert result["locked"]
    for unit in GAS_UNITS:
        assert f"\x02{unit:02X}SVC\r".encode() in seen
    assert not any(b"SVN" in packet for packet in seen)
    assert not lab.serial.is_open and not lab.mfc.is_open


def test_sigterm_close_stops_heat_and_gas(lab):
    lab.service.start_sequence(
        recipe(gas={0: 20}), end_gas={}, end_gas_until_c=30, client="operator"
    )
    result = lab.service.close()
    assert not result["locked"]
    assert lab.serial.registers[p.SRUN_REGISTER] == 1
    assert all(lab.mfc.channel_state(unit)["actual_percent"] == 0 for unit in GAS_UNITS)


def test_intent_is_durable_before_first_output(lab, monkeypatch):
    original = lab.service.furnace.set_ramp_rate

    def rate(value):
        assert lab.store.load()["phase"] == "applying"
        return original(value)

    monkeypatch.setattr(lab.service.furnace, "set_ramp_rate", rate)
    lab.service.apply_setpoint(30, 10, client="operator")
    assert lab.store.load()["phase"] == "manual"


def test_state_write_failure_before_command_never_starts_heat(lab, monkeypatch):
    def fail(value):
        raise StateError("storage unavailable")

    monkeypatch.setattr(lab.store, "save", fail)
    with pytest.raises(StateError):
        lab.service.apply_setpoint(30, 10, client="operator")
    assert lab.service.status()["locked"]
    assert lab.service.status()["persistence_error"]
    assert not any(
        packet[2:6] == bytes([0, p.SRUN_REGISTER, 0, 0]) for packet in writes(lab.serial)
    )


@pytest.mark.parametrize("mode", [0o700, 0o750])
def test_state_roundtrip_uses_installed_directory_and_private_file(tmp_path, mode):
    directory = tmp_path / "furnace"
    directory.mkdir(mode=mode)
    store = StateStore(directory / "state.json")
    store.save({"phase": "holding", "elapsed_s": 60})
    assert store.load()["elapsed_s"] == 60
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == mode


def test_state_created_directory_mode_ignores_restrictive_umask(tmp_path):
    store = StateStore(tmp_path / "furnace" / "state.json")
    old = os.umask(0o077)
    try:
        store.save({"phase": "idle"})
    finally:
        os.umask(old)
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o750


@pytest.mark.parametrize("payload", ['{"a":1,"a":2}', '{"a":NaN}', "[1]", '{"a":Infinity}', "{"])
def test_state_rejects_invalid_json(tmp_path, payload):
    store = StateStore(tmp_path / "state.json")
    tmp_path.chmod(0o750)
    store.path.write_text(payload)
    store.path.chmod(0o600)
    with pytest.raises(StateError):
        store.load()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "public", "directory"])
def test_state_refuses_unsafe_existing_file(tmp_path, kind):
    store = StateStore(tmp_path / "state.json")
    tmp_path.chmod(0o750)
    target = tmp_path / "target"
    target.write_text("keep")
    target.chmod(0o600)
    if kind == "symlink":
        store.path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, store.path)
    elif kind == "fifo":
        os.mkfifo(store.path, 0o600)
    elif kind == "directory":
        store.path.mkdir(mode=0o700)
    else:
        store.path.write_text("{}")
        store.path.chmod(0o644)
    with pytest.raises(StateError):
        store.load()
    with pytest.raises(StateError):
        store.save({"x": 1})
    assert target.read_text() == "keep"


def test_atomic_replace_failure_preserves_old_state_and_cleans_temp(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.json")
    tmp_path.chmod(0o750)
    store.save({"version": 1})

    def fail(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(StateError):
        store.save({"version": 2})
    assert store.load() == {"version": 1}
    assert {path.name for path in tmp_path.iterdir()} == {"state.json", ".state.json.lock"}


def test_state_refuses_symlink_parent_and_traversal(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir(mode=0o750)
    link = tmp_path / "link"
    link.symlink_to(directory)
    store = StateStore(link / "state.json")
    with pytest.raises(StateError):
        store.save({})
    assert not list(directory.iterdir())
    with pytest.raises(ValueError):
        StateStore(tmp_path / ".." / "escape")


def test_close_blocks_callback_that_tries_to_restart_heat(lab):
    lab.service.start_sequence(recipe(), client="operator")
    attempts = []

    def callback(reason):
        attempts.append(reason)
        with pytest.raises(ServiceError, match="closed"):
            lab.service.apply_setpoint(50, 10, client="operator")

    lab.service.on_sequence_end = callback
    result = lab.service.close()
    assert attempts == ["stopped"]
    assert result["run_state_code"] == 1
    assert lab.serial.registers[p.SRUN_REGISTER] == 1
    assert not lab.serial.is_open


@pytest.mark.parametrize("failed", [False, True])
def test_disconnect_clears_tracking_and_preserves_failed_stop(lab, monkeypatch, failed):
    lab.service.start_sequence(recipe(), client="operator")
    if failed:

        def fail():
            raise RuntimeError("STOP failed")

        monkeypatch.setattr(lab.service.furnace, "set_stop", fail)
    result = lab.service.disconnect(client="operator")
    assert not result["sequence"]["running"]
    assert result["locked"] is failed
    assert lab.store.load()["phase"] == ("fault" if failed else "stopped")
    # Retain a failed transport for explicit stop attempts instead of detaching it.
    assert lab.serial.is_open is failed


def test_reopen_primes_new_target_before_restoring_normal_valve(lab, monkeypatch):
    lab.service.set_gas_flow(0, 50, client="operator")
    controller = lab.service.flow_controller
    controller.set_valve_override(7, False)
    # Simulate a lost zero write with an external solenoid that remains open.
    assert lab.mfc.channel_state(7)["actual_percent"] == 0
    levels = []
    original = lab.mfc.write

    def write(packet):
        result = original(packet)
        levels.append(lab.mfc.channel_state(7)["actual_percent"])
        return result

    monkeypatch.setattr(lab.mfc, "write", write)
    lab.service.lease.acquire("operator")
    lab.service.set_gas_flow(0, 5, client="operator")
    assert levels and max(levels) <= 5
    assert levels[-1] == 5


@pytest.mark.parametrize("phase,locked", [("fault", True), ("applying", False), ("ramping", False)])
def test_failed_fault_save_preserves_durable_lock_and_invalidates_stale_run(
    lab, monkeypatch, phase, locked
):
    lab.service.start_sequence(recipe(), client="operator")
    saved = lab.store.load()
    saved.update(phase=phase, locked=locked)
    lab.store.save(saved)

    def fail(value):
        raise StateError("fault save failed")

    monkeypatch.setattr(lab.store, "save", fail)
    monkeypatch.setattr(
        lab.service.furnace, "set_stop", lambda: (_ for _ in ()).throw(OSError("STOP failed"))
    )
    with lab.service._operation_lock:
        lab.service._fail(RuntimeError("unknown outcome"))
    assert lab.service.status()["locked"]
    if phase == "ramping":
        assert lab.store.load() is None
    else:
        assert lab.store.load()["phase"] == phase


def test_stop_before_start_attaches_read_only_then_stops(lab):
    lab.service.apply_setpoint(30, 10, client="operator")
    unstarted = lab.make()
    assert unstarted.stop_furnace()["run_state_code"] == 1
    assert lab.serial.registers[p.SRUN_REGISTER] == 1


def test_recipe_edit_is_durable_without_current_output_change(lab):
    steps = recipe(hold=5) + recipe(target=40, gas={0: 5})
    lab.service.start_sequence(steps, end_gas={0: 1}, end_gas_until_c=30, client="operator")
    enter_hold(lab)
    lab.service.lease.acquire("operator")
    before = len(writes(lab.serial))
    steps[0]["hold_minutes"] = 10
    steps[1]["temperature_c"] = 45
    steps[1]["gas"] = {0: 15}
    lab.service.set_sequence(steps, end_gas={0: 3}, end_gas_until_c=28, client="operator")
    assert len(writes(lab.serial)) == before
    state = lab.make().start(background=False)
    assert not state["locked"]
    assert state["sequence"]["steps"][0]["hold_minutes"] == 10
    assert state["sequence"]["steps"][1]["temperature_c"] == 45
    assert state["sequence"]["steps"][1]["gas"] == {0: 15}
    assert state["sequence"]["end_gas"][0] == 3
    assert state["sequence"]["end_gas_until_c"] == 28


def test_failed_idle_plan_clears_earlier_channels(lab, monkeypatch):
    lab.service.apply_setpoint(30, 10, client="operator")
    lab.store.path.unlink()
    original = lab.mfc.write

    def write(packet):
        if packet == b"\x0208SFR$7D0\r":
            raise OSError("idle channel write failed")
        return original(packet)

    monkeypatch.setattr(lab.mfc, "write", write)
    recovered = lab.make(idle_gas={0: 10, 1: 20})
    assert recovered.start(background=False)["locked"]
    assert all(lab.mfc.channel_state(unit)["actual_percent"] == 0 for unit in GAS_UNITS)


def test_flow_stop_response_exposes_unknown_outcome(lab, monkeypatch):
    lab.service.set_gas_flow(0, 10, client="operator")
    monkeypatch.setattr(lab.mfc, "write", lambda packet: (_ for _ in ()).throw(OSError("bus lost")))
    result = lab.service.stop_gas_flow(0)
    assert result["locked"] and result["fault"]
    assert "bus lost" in result["error"]


def test_state_transaction_prevents_replacement_between_read_and_invalidation(
    tmp_path, monkeypatch
):
    tmp_path.chmod(0o750)
    store = StateStore(tmp_path / "state.json")
    other = StateStore(store.path)
    store.save({"phase": "ramping"})
    original = store._load
    attempted = []

    def load(directory):
        value = original(directory)
        # A second instance cannot replace the record during this transaction.
        with pytest.raises(StateError):
            other.save({"phase": "fault", "locked": True})
        attempted.append(True)
        return value

    monkeypatch.setattr(store, "_load", load)
    assert store.invalidate_resumable()
    assert attempted == [True]
    other.save({"phase": "fault", "locked": True})
    monkeypatch.setattr(store, "_load", original)
    assert not store.invalidate_resumable()
    assert store.load()["locked"] is True


def test_busy_state_lock_does_not_delay_output_stop(lab):
    lab.service.apply_setpoint(30, 10, client="operator")
    directory = lab.store._directory()
    try:
        with lab.store._transaction(directory):
            result = lab.service.stop_furnace()
        assert result["locked"]
        assert result["persistence_error"]
        assert lab.serial.registers[p.SRUN_REGISTER] == 1
    finally:
        os.close(directory)


@pytest.mark.parametrize("held", [False, True])
def test_recovered_manual_run_without_orphan_starts_grace(lab, held):
    lab.service.apply_setpoint(30, 60, client="operator")
    if held:
        lab.clock.advance(10)
        lab.service.lease.acquire("operator")
        assert lab.service.poll_once()["run_state_code"] == 2
    assert lab.store.load()["orphan_time"] is None
    resumed = lab.make(orphan_grace_min=0.1)
    before = lab.clock.wall()
    state = resumed.start(background=False)
    assert not state["locked"]
    assert state["lease"]["holder"] is None
    assert state["orphan_time"] == before
    assert lab.store.load()["orphan_time"] == before
    lab.clock.advance(7)
    assert resumed.poll_once()["run_state_code"] == 1


def test_expiry_persistence_fault_still_allows_explicit_acknowledgement(lab, monkeypatch):
    lab.service.apply_setpoint(30, 60, client="operator")
    original = lab.store.save
    monkeypatch.setattr(
        lab.store, "save", lambda value: (_ for _ in ()).throw(StateError("disk full"))
    )
    lab.clock.advance(4)
    assert lab.service.poll_once()["locked"]
    monkeypatch.setattr(lab.store, "save", original)
    lab.service.lease.acquire("operator")
    assert not lab.service.acknowledge(client="operator")["locked"]


def test_recovery_with_partially_configured_map_preserves_all_off_end_plan(lab):
    service = lab.make(gas_map={0: "test gas A"})
    service.start(background=False)
    service.lease.acquire("operator")
    service.start_sequence(recipe(gas={0: 20}), end_gas={}, end_gas_until_c=30, client="operator")
    restored = lab.make(gas_map={0: "test gas A"})
    assert not restored.start(background=False)["locked"]
    assert restored.status()["sequence"]["end_gas"] == {0: 0, 1: 0, 2: 0}
