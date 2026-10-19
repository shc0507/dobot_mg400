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
    os = namespace["os"]
    original_open = os.open

    def open_directory(path, flags):
        if flags != os.O_RDONLY | os.O_DIRECTORY:
            raise AssertionError("No hardware")
        return original_open(path, flags)

    monkeypatch.setattr(os, "open", open_directory)
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


def test_position_write_uses_documented_frame_and_exact_echo(module, monkeypatch):
    request = bytes.fromhex("01 06 01 03 01 f4 78 21")
    write = Mock(return_value=8)
    monkeypatch.setattr(module.os, "write", write)
    monkeypatch.setattr(module.os, "read", Mock(return_value=request))
    monkeypatch.setattr(module.select, "select", Mock(return_value=([42], [], [])))
    assert module.register(42, 0x0103, 500) == 500
    write.assert_called_once_with(42, request)


@pytest.mark.parametrize(
    "address,value", [(0x0100, 1), (0x0300, 1), (0x0402, 1), (0x0101, 50),
                      (0x0104, 30), (0x0103, -1), (0x0103, 1001), (0x0103, True)],
)
def test_position_transport_rejects_other_writes(module, monkeypatch, address, value):
    write = Mock()
    monkeypatch.setattr(module.os, "write", write)
    with pytest.raises(ValueError, match="not allowed"):
        module.register(42, address, value)
    write.assert_not_called()


@pytest.mark.parametrize("fault", ["wrong_echo", "exception", "bad_crc"])
def test_position_transport_never_retries_uncertain_write(module, monkeypatch, fault):
    response = packet(module, bytes.fromhex("01 06 01 03 01 f4"))
    if fault == "wrong_echo":
        response = packet(module, bytes.fromhex("01 06 01 03 01 f5"))
    elif fault == "exception":
        response = packet(module, bytes.fromhex("01 86 02"))
    else:
        response = response[:-1] + bytes([response[-1] ^ 1])
    write = Mock(return_value=8)
    monkeypatch.setattr(module.os, "write", write)
    monkeypatch.setattr(module.os, "read", Mock(return_value=response))
    monkeypatch.setattr(module.select, "select", Mock(return_value=([42], [], [])))
    with pytest.raises(ValueError):
        module.register(42, 0x0103, 500)
    write.assert_called_once()


@pytest.mark.parametrize("initial_mode", [0, 1])
def test_position_disables_io_preserves_force_speed_and_checks_target(
    module, monkeypatch, initial_mode
):
    values = {0x0101: 50, 0x0104: 30, 0x0402: initial_mode, 0x0103: 0}
    operations = []

    def register(fd, address, value=None):
        operations.append((address, value))
        if value is not None:
            values[address] = value
        return values[address]

    monkeypatch.setitem(module.set_position.__globals__, "read", Mock(return_value=(1, 1, 0)))
    monkeypatch.setitem(module.set_position.__globals__, "register", register)
    module.set_position(42, 501)
    writes = [(address, value) for address, value in operations if value is not None]
    assert writes == ([(0x0402, 0)] if initial_mode else []) + [(0x0103, 501)]
    assert operations[-1] == (0x0103, None)
    assert values[0x0101] == 50 and values[0x0104] == 30


@pytest.mark.parametrize("state", [(0, 1, 500), (1, 3, 500), (1, 1, 1001)])
def test_position_requires_valid_initialized_feedback_before_any_register_access(
    module, monkeypatch, state
):
    register = Mock()
    monkeypatch.setitem(module.set_position.__globals__, "read", Mock(return_value=state))
    monkeypatch.setitem(module.set_position.__globals__, "register", register)
    with pytest.raises(RuntimeError, match="not ready"):
        module.set_position(42, 500)
    register.assert_not_called()


@pytest.mark.parametrize("fault", ["force", "speed", "mode", "mode_readback", "target_readback"])
def test_position_rejects_invalid_settings_or_readback(module, monkeypatch, fault):
    calls = []
    mode_reads = [0]

    def register(fd, address, value=None):
        calls.append((address, value))
        if value is not None:
            return value
        if address == 0x0101:
            return 0 if fault == "force" else 50
        if address == 0x0104:
            return 101 if fault == "speed" else 30
        if address == 0x0402:
            mode_reads[0] += 1
            if fault == "mode":
                return 2
            return 1 if fault == "mode_readback" else 0
        return 499 if fault == "target_readback" else 500

    monkeypatch.setitem(module.set_position.__globals__, "read", Mock(return_value=(1, 1, 0)))
    monkeypatch.setitem(module.set_position.__globals__, "register", register)
    with pytest.raises(RuntimeError):
        module.set_position(42, 500)
    if fault != "target_readback":
        assert (0x0103, 500) not in calls


def test_mailbox_reads_latest_request_once_and_rejects_replay(module, monkeypatch, tmp_path):
    session = str(uuid.uuid4())
    mailbox = module.PositionMailbox(tmp_path / "feedback.json", session)
    send = Mock()
    monkeypatch.setitem(module.PositionMailbox.poll.__globals__, "set_position", send)
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    first = dict(id=str(uuid.uuid4()), session=session, sent_at=1000.0, position=501)
    second = {**first, "id": str(uuid.uuid4()), "position": 502}
    mailbox.path.write_text(json.dumps(first))
    mailbox.path.write_text(json.dumps(second))
    mailbox.poll(42)
    mailbox.poll(42)
    assert mailbox.control["ack"] and mailbox.control["io_mode"] == 0
    assert mailbox.control["command_id"] == second["id"]
    mailbox.path.write_text(json.dumps(first))
    mailbox.poll(42)
    mailbox.path.write_text(json.dumps(second))
    mailbox.poll(42)
    assert [call.args for call in send.call_args_list] == [(42, 502), (42, 501)]


@pytest.mark.parametrize(
    "change", [{"session": str(uuid.uuid4())}, {"id": "bad"}, {"sent_at": 999.249},
               {"sent_at": 1000.01}, {"sent_at": float("nan")}, {"sent_at": True},
               {"position": True}, {"position": 1001}, {"position": -1}],
)
def test_mailbox_rejects_other_sessions_expired_or_invalid_requests(
    module, monkeypatch, tmp_path, change
):
    session = str(uuid.uuid4())
    mailbox = module.PositionMailbox(tmp_path / "feedback.json", session)
    send = Mock()
    monkeypatch.setitem(module.PositionMailbox.poll.__globals__, "set_position", send)
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    request = dict(id=str(uuid.uuid4()), session=session, sent_at=1000.0, position=500)
    mailbox.path.write_text(json.dumps({**request, **change}))
    mailbox.poll(42)
    send.assert_not_called()


def test_mailbox_locks_after_uncertain_write_and_never_retries(module, monkeypatch, tmp_path):
    session = str(uuid.uuid4())
    mailbox = module.PositionMailbox(tmp_path / "feedback.json", session)
    send = Mock(side_effect=TimeoutError("Write reply lost"))
    monkeypatch.setitem(module.PositionMailbox.poll.__globals__, "set_position", send)
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    for position in (501, 502):
        mailbox.path.write_text(json.dumps(dict(
            id=str(uuid.uuid4()), session=session, sent_at=1000.0, position=position,
        )))
        mailbox.poll(42)
    assert mailbox.control["locked"] and not mailbox.control["ack"]
    assert mailbox.control["error"] == "Write reply lost"
    send.assert_called_once_with(42, 501)
    restarted = module.PositionMailbox(tmp_path / "feedback.json", str(uuid.uuid4()))
    assert restarted.control["locked"] and not restarted.control["ack"]
    restarted.poll(42)
    send.assert_called_once()


def test_mailbox_saves_lock_before_sending_and_removes_it_after_success(
    module, monkeypatch, tmp_path
):
    session = str(uuid.uuid4())
    output = tmp_path / "feedback.json"
    mailbox = module.PositionMailbox(output, session)
    request = dict(id=str(uuid.uuid4()), session=session, sent_at=1000.0, position=501)
    mailbox.path.write_text(json.dumps(request))
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    synchronized = []
    fsync = module.os.fsync

    def synchronize(fd):
        synchronized.append(fd)
        fsync(fd)

    def send(fd, position):
        assert len(synchronized) == 2
        assert json.loads(mailbox.lock_path.read_text()) == {
            "command_id": request["id"], "target": 501,
        }

    monkeypatch.setattr(module.os, "fsync", synchronize)
    send = Mock(side_effect=send)
    monkeypatch.setitem(module.PositionMailbox.poll.__globals__, "set_position", send)
    mailbox.poll(42)
    send.assert_called_once_with(42, 501)
    assert mailbox.control["ack"] and not mailbox.control["locked"]
    assert not mailbox.lock_path.exists()
    restarted = module.PositionMailbox(output, str(uuid.uuid4()))
    assert not restarted.control["locked"]
    restarted.poll(42)
    send.assert_called_once()


@pytest.mark.parametrize("stage", ["before_send", "before_ack"])
def test_mailbox_crash_keeps_lock_and_restart_cannot_send(
    module, monkeypatch, tmp_path, stage
):
    output = tmp_path / "feedback.json"
    session = str(uuid.uuid4())
    mailbox = module.PositionMailbox(output, session)
    mailbox.path.write_text(json.dumps(dict(
        id=str(uuid.uuid4()), session=session, sent_at=1000.0, position=501,
    )))
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    send = Mock(side_effect=SystemExit("Interrupted before acknowledgement"))
    monkeypatch.setitem(module.PositionMailbox.poll.__globals__, "set_position", send)
    if stage == "before_send":
        emit = module.emit

        def save_then_exit(*args, **kwargs):
            emit(*args, **kwargs)
            raise SystemExit("Interrupted before sending")

        monkeypatch.setitem(module.PositionMailbox.poll.__globals__, "emit", save_then_exit)
    with pytest.raises(SystemExit):
        mailbox.poll(42)
    assert mailbox.lock_path.exists()
    restarted = module.PositionMailbox(output, str(uuid.uuid4()))
    restarted.path.write_text(json.dumps(dict(
        id=str(uuid.uuid4()), session=restarted.session, sent_at=1000.0, position=502,
    )))
    restarted.poll(42)
    assert restarted.control["locked"] and not restarted.control["ack"]
    assert send.call_count == (stage == "before_ack")


@pytest.mark.parametrize("content", ["", "{", "null", '{"unexpected": true}'])
def test_mailbox_existing_or_damaged_lock_always_blocks_commands(module, tmp_path, content):
    output = tmp_path / "feedback.json"
    output.with_name(output.name + ".command-lock.json").write_text(content)
    mailbox = module.PositionMailbox(output, str(uuid.uuid4()))
    assert mailbox.control["locked"]
    assert "command lock exists" in mailbox.control["error"]


def test_mailbox_cannot_check_lock_fails_closed(module, monkeypatch, tmp_path):
    original_lstat = Path.lstat
    lock = tmp_path / "feedback.json.command-lock.json"

    def lstat(path):
        if path == lock:
            raise PermissionError("No access")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", lstat)
    mailbox = module.PositionMailbox(tmp_path / "feedback.json", str(uuid.uuid4()))
    assert mailbox.control["locked"]
    assert "Cannot check the command lock" in mailbox.control["error"]


def test_mailbox_lock_write_failure_prevents_hardware_command(module, monkeypatch, tmp_path):
    mailbox = module.PositionMailbox(tmp_path / "feedback.json", str(uuid.uuid4()))
    mailbox.path.write_text(json.dumps(dict(
        id=str(uuid.uuid4()), session=mailbox.session, sent_at=1000.0, position=501,
    )))
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    send = Mock()
    monkeypatch.setitem(module.PositionMailbox.poll.__globals__, "set_position", send)
    monkeypatch.setitem(
        module.PositionMailbox.poll.__globals__, "emit", Mock(side_effect=OSError("Disk full"))
    )
    mailbox.poll(42)
    send.assert_not_called()
    assert mailbox.control["locked"] and not mailbox.control["ack"]


def test_mailbox_checks_persistent_lock_after_connect_and_still_emits_feedback(
    module, monkeypatch, tmp_path, capsys
):
    output = tmp_path / "feedback.json"
    lock = output.with_name(output.name + ".command-lock.json")

    @contextlib.contextmanager
    def connect():
        lock.write_text("Previous collector stopped during a command.")
        yield 42

    monkeypatch.setitem(module.run.__globals__, "connect", connect)
    monkeypatch.setitem(
        module.run.__globals__, "read", Mock(side_effect=[(1, 1, 501), TimeoutError("Read failed")])
    )
    send = Mock()
    monkeypatch.setitem(module.PositionMailbox.poll.__globals__, "set_position", send)
    monkeypatch.setattr(module.time, "sleep", Mock())
    assert module.main(["--output", str(output), "--commands"]) == 1
    sample = json.loads(output.read_text())
    assert sample["position"] == 501
    assert sample["control"]["locked"] and not sample["control"]["ack"]
    assert lock.exists()
    send.assert_not_called()
    assert "Read failed" in capsys.readouterr().err


def test_commands_require_shared_output_file(module):
    with pytest.raises(SystemExit) as error:
        module.main(["--commands"])
    assert error.value.code == 2
