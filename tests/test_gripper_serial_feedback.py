"""Test serial telemetry without opening hardware devices."""

import contextlib
import copy
import json
import runpy
import struct
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def module(monkeypatch, tmp_path):
    namespace = runpy.run_path(str(ROOT / "scripts/gripper_feedback.py"), run_name="test_feedback")
    monkeypatch.setattr(namespace["os"], "open", Mock(side_effect=AssertionError("No hardware")))
    return SimpleNamespace(**namespace)


def packet(module, data):
    return data + module.crc(data)


def test_crc_known_modbus_vector(module):
    assert module.crc(b"123456789") == b"\x37\x4b"


def test_decode_three_registers(module):
    response = packet(module, b"\x01\x03\x06" + struct.pack(">3H", 1, 2, 501))
    assert module.decode(response) == (1, 2, 501)


@pytest.mark.parametrize("fault", ["crc", "truncated", "slave", "function", "count", "exception"])
def test_decode_rejects_invalid_response(module, fault):
    payload = bytearray(b"\x01\x03\x06" + struct.pack(">3H", 1, 1, 1000))
    if fault == "slave":
        payload[0] = 2
    elif fault == "function":
        payload[1] = 6
    elif fault == "count":
        payload[2] = 4
    elif fault == "exception":
        payload = b"\x01\x83\x02"
    response = packet(module, payload)
    if fault == "crc":
        response = response[:-1] + bytes([response[-1] ^ 1])
    elif fault == "truncated":
        response = response[:-1]
    with pytest.raises(ValueError):
        module.decode(response)


def test_read_sends_only_fixed_read_request_and_accepts_fragments(module, monkeypatch):
    response = packet(module, b"\x01\x03\x06" + struct.pack(">3H", 1, 1, 1000))
    write = Mock(return_value=8)
    monkeypatch.setattr(module.os, "write", write)
    monkeypatch.setattr(module.os, "read", Mock(side_effect=[response[:4], response[4:]]))
    monkeypatch.setattr(module.select, "select", Mock(return_value=([42], [], [])))
    assert module.read(42) == (1, 1, 1000)
    write.assert_called_once_with(42, packet(module, b"\x01\x03\x02\x00\x00\x03"))


def test_partial_request_is_not_retried(module, monkeypatch):
    write = Mock(return_value=4)
    monkeypatch.setattr(module.os, "write", write)
    with pytest.raises(RuntimeError, match="incomplete"):
        module.read(42)
    write.assert_called_once()


def test_truncated_reply_times_out_without_retry(module, monkeypatch):
    now = [0.0]
    write = Mock(return_value=8)
    monkeypatch.setattr(module.os, "write", write)
    monkeypatch.setattr(module.os, "read", Mock(return_value=b"\x01\x03\x06"))
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])

    def select(*args):
        now[0] += 0.6
        return [42], [], []

    monkeypatch.setattr(module.select, "select", select)
    with pytest.raises(TimeoutError):
        module.read(42)
    write.assert_called_once()


def test_serial_settings_are_exclusive_and_restored_on_error(module, monkeypatch):
    saved = [11, 22, 33, 44, 55, 66, [0] * 32]
    monkeypatch.setattr(module.os, "open", Mock(return_value=42))
    close = Mock()
    ioctl = Mock()
    lock = Mock()
    settings = Mock()
    monkeypatch.setattr(module.os, "close", close)
    monkeypatch.setattr(module.fcntl, "ioctl", ioctl)
    monkeypatch.setattr(module.fcntl, "flock", lock)
    monkeypatch.setattr(module.termios, "tcgetattr", lambda fd: copy.deepcopy(saved))
    monkeypatch.setattr(module.termios, "tcsetattr", settings)
    monkeypatch.setattr(module.termios, "tcflush", Mock())
    monkeypatch.setitem(module.connect.__wrapped__.__globals__, "resolve_port", lambda: module.PORT)
    with pytest.raises(RuntimeError, match="Read failed"):
        with module.connect() as fd:
            assert fd == 42
            raise RuntimeError("Read failed")
    ioctl.assert_called_once_with(42, module.termios.TIOCEXCL)
    lock.assert_called_once_with(42, module.fcntl.LOCK_EX | module.fcntl.LOCK_NB)
    configured = settings.call_args_list[0].args[2]
    assert configured[2] == module.termios.CLOCAL | module.termios.CREAD | module.termios.CS8
    assert configured[4:6] == [module.termios.B115200] * 2
    assert settings.call_args_list[-1].args[2] == saved
    close.assert_called_once_with(42)


def test_duplicate_reader_is_rejected_before_serial_settings_change(module, monkeypatch):
    monkeypatch.setattr(module.os, "open", Mock(return_value=42))
    close = Mock()
    settings = Mock()
    monkeypatch.setattr(module.os, "close", close)
    monkeypatch.setattr(module.termios, "tcgetattr", settings)
    monkeypatch.setattr(module.fcntl, "flock", Mock(side_effect=BlockingIOError("Already in use")))
    monkeypatch.setitem(module.connect.__wrapped__.__globals__, "resolve_port", lambda: module.PORT)
    with pytest.raises(BlockingIOError, match="Already in use"):
        with module.connect():
            pytest.fail("The second reader must not connect.")
    settings.assert_not_called()
    close.assert_called_once_with(42)


def test_json_sequence_and_nonzero_exit_on_read_error(module, monkeypatch, capsys):
    closed = []

    @contextlib.contextmanager
    def connect():
        try:
            yield 42
        finally:
            closed.append(True)

    monkeypatch.setitem(module.run.__globals__, "connect", connect)
    monkeypatch.setitem(
        module.run.__globals__,
        "read",
        Mock(side_effect=[(1, 0, 600), (1, 1, 1000), TimeoutError("Read failed")]),
    )
    monkeypatch.setattr(module.time, "sleep", Mock())
    assert module.main([]) == 1
    output = capsys.readouterr()
    rows = [json.loads(line) for line in output.out.splitlines()]
    assert len(rows) == 2
    assert str(uuid.UUID(rows[0]["session"])) == rows[1]["session"]
    assert [row["seq"] for row in rows] == [1, 2]
    assert [row["position"] for row in rows] == [600, 1000]
    assert all(
        set(row) == {"session", "seq", "sent_at", "init", "state", "position"}
        and isinstance(row["sent_at"], float)
        for row in rows
    )
    assert "Read failed" in output.err
    assert closed == [True]


def linux_device(module, monkeypatch, tmp_path, name, identity):
    sys_tty = tmp_path / "sys/class/tty"
    dev = tmp_path / "dev"
    usb = tmp_path / "devices" / name
    interface = usb / "interface"
    interface.mkdir(parents=True)
    tty = sys_tty / name
    tty.mkdir(parents=True)
    (tty / "device").symlink_to(interface)
    dev.mkdir(exist_ok=True)
    (dev / name).write_text("")
    for key, value in zip(("idVendor", "idProduct", "serial"), identity, strict=True):
        (usb / key).write_text(value + "\n")
    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setitem(module.resolve_port.__globals__, "SYS_TTY", sys_tty)
    monkeypatch.setitem(module.resolve_port.__globals__, "DEV_ROOT", dev)
    return dev / name


def test_linux_selects_exact_identity(module, monkeypatch, tmp_path):
    linux_device(module, monkeypatch, tmp_path, "ttyUSB0", ("0403", "6001", "OTHER"))
    expected = linux_device(module, monkeypatch, tmp_path, "ttyUSB2", ("0403", "6001", "BG02Q5TN"))
    assert module.resolve_port() == str(expected)


@pytest.mark.parametrize(
    "identity",
    [
        ("0404", "6001", "BG02Q5TN"),
        ("0403", "6002", "BG02Q5TN"),
        ("0403", "6001", "BG02Q5TN-extra"),
    ],
)
def test_linux_rejects_nonmatching_identity(module, monkeypatch, tmp_path, identity):
    linux_device(module, monkeypatch, tmp_path, "ttyUSB0", identity)
    with pytest.raises(RuntimeError, match="Found 0"):
        module.resolve_port()


def test_linux_rejects_duplicate_identity(module, monkeypatch, tmp_path):
    for name in ("ttyUSB1", "ttyUSB2"):
        linux_device(module, monkeypatch, tmp_path, name, ("0403", "6001", "BG02Q5TN"))
    with pytest.raises(RuntimeError, match="Found 2"):
        module.resolve_port()


def test_linux_rejects_absent_device_node(module, monkeypatch, tmp_path):
    device = linux_device(module, monkeypatch, tmp_path, "ttyUSB2", ("0403", "6001", "BG02Q5TN"))
    device.unlink()
    with pytest.raises(FileNotFoundError):
        module.resolve_port()


def test_macos_keeps_original_device_path(module, monkeypatch):
    monkeypatch.setattr(module.sys, "platform", "darwin")
    assert module.resolve_port() == "/dev/cu.usbserial-BG02Q5TN"


def test_file_output_replaces_complete_sample_without_stdout(module, tmp_path, capsys):
    output = tmp_path / "feedback.json"
    sample = {
        "session": str(uuid.uuid4()),
        "seq": 1,
        "sent_at": 1234.5,
        "init": 1,
        "state": 0,
        "position": 501,
    }
    module.emit(sample, output)
    with output.open() as old_file:
        sample["seq"] = 2
        sample["position"] = 1000
        module.emit(sample, output)
        assert json.load(old_file)["seq"] == 1
    assert json.loads(output.read_text()) == sample
    assert output.stat().st_mode & 0o777 == 0o644
    assert list(tmp_path.iterdir()) == [output]
    assert capsys.readouterr().out == ""


def test_sigterm_runs_cleanup_and_restores_handler(module, monkeypatch):
    cleaned = []

    @contextlib.contextmanager
    def connect():
        try:
            yield 42
        finally:
            cleaned.append(True)

    def read(fd):
        module.signal.getsignal(module.signal.SIGTERM)(module.signal.SIGTERM, None)

    old_handler = module.signal.getsignal(module.signal.SIGTERM)
    monkeypatch.setitem(module.run.__globals__, "connect", connect)
    monkeypatch.setitem(module.run.__globals__, "read", read)
    with pytest.raises(SystemExit) as error:
        module.main([])
    assert error.value.code == 128 + module.signal.SIGTERM
    assert cleaned == [True]
    assert module.signal.getsignal(module.signal.SIGTERM) is old_handler
