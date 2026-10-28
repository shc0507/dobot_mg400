"""Check the standard-library WebSocket codec and the queued connection."""

import base64
import io
import socket
import struct
import threading
import time

import pytest

from mg400_agent.websocket import (
    BINARY,
    CLOSE,
    CONTINUATION,
    PING,
    PONG,
    TEXT,
    Connection,
    ConnectionClosed,
    MessageReader,
    ProtocolError,
    accept_key,
    encode_close,
    encode_frame,
    exact_reader,
    read_frame,
    valid_client_key,
)


def reader(data):
    return exact_reader(io.BytesIO(data))


def header_only(data):
    """Return read(n) over data. A read past data fails the test instead of raising EOFError.

    The codec must refuse some frames before it reads their mask key or payload.
    """
    stream = io.BytesIO(data)

    def read(count):
        chunk = stream.read(count)
        if len(chunk) != count:
            pytest.fail(f"The codec read {count} bytes after the frame header")
        return chunk

    return read


def client_frames(*frames):
    """Encode (opcode, payload, fin) tuples as masked client frames."""
    return b"".join(
        encode_frame(opcode, payload, fin=fin, mask=True) for opcode, payload, fin in frames
    )


def messages(data, count):
    message_reader = MessageReader(reader(data))
    return [message_reader.next() for _ in range(count)]


def refusal(data, **kwargs):
    with pytest.raises(ProtocolError) as caught:
        MessageReader(reader(data), **kwargs).next()
    return caught.value.code


def test_accept_key_matches_the_rfc_6455_example():
    assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("dGhlIHNhbXBsZSBub25jZQ==", True),
        (base64.b64encode(bytes(range(16))).decode(), True),
        (base64.b64encode(bytes(15)).decode(), False),
        # 17 and 18 bytes also encode to 24 characters.
        (base64.b64encode(bytes(17)).decode(), False),
        (base64.b64encode(bytes(18)).decode(), False),
        ("dGhlIHNhbXBsZSBub25jZQ", False),
        ("dGhlIHNhbXBsZSBub25jZ!==", False),
        ("dGhlIHNhbXBsZSBub25jZ Q=", False),
        ("dGhlIHNhbXBsZSBub25jZé==", False),
        (b"dGhlIHNhbXBsZSBub25jZQ==", False),
        (None, False),
    ],
    ids=[
        "rfc-example",
        "16-bytes",
        "15-bytes",
        "17-bytes",
        "18-bytes",
        "no-padding",
        "bad-character",
        "space",
        "non-ascii",
        "bytes",
        "none",
    ],
)
def test_valid_client_key_accepts_only_base64_for_16_bytes(value, valid):
    assert valid_client_key(value) is valid


@pytest.mark.parametrize("mask", [False, True], ids=["server", "client"])
@pytest.mark.parametrize("length", [0, 125, 126, 65535, 65536])
def test_frames_round_trip_at_the_length_encoding_boundaries(length, mask):
    payload = (bytes(range(256)) * (length // 256 + 1))[:length]
    frame = encode_frame(TEXT, payload, mask=mask)
    extended = 0 if length < 126 else 2 if length < 65536 else 8
    head = 2 + extended + (4 if mask else 0)
    assert len(frame) == head + length
    assert frame[0] == 0x80 | TEXT
    assert bool(frame[1] & 0x80) is mask
    assert frame[1] & 0x7F == {0: length, 2: 126, 8: 127}[extended]
    if extended:
        assert int.from_bytes(frame[2 : 2 + extended], "big") == length
    if mask:
        key = frame[head - 4 : head]
        expected = bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))
        assert frame[head:] == expected
    else:
        assert frame[head:] == payload
    stream = io.BytesIO(frame)
    assert read_frame(exact_reader(stream), masked=mask, max_payload=65536) == (
        True,
        TEXT,
        payload,
    )
    assert stream.tell() == len(frame)


def test_encode_frame_sets_the_fin_bit_and_the_opcode():
    assert encode_frame(TEXT, b"a", fin=False)[:2] == bytes([TEXT, 1])
    assert encode_frame(CONTINUATION, b"b")[:2] == bytes([0x80, 1])
    assert encode_frame(CLOSE, b"")[:2] == bytes([0x88, 0])
    assert encode_frame(PONG, b"xy", mask=True)[:2] == bytes([0x8A, 0x82])


def test_exact_reader_returns_exact_counts_or_raises_eof():
    read = reader(b"abcde")
    assert read(0) == b""
    assert read(2) == b"ab"
    with pytest.raises(EOFError):
        read(4)


@pytest.mark.parametrize("bit", [0x40, 0x20, 0x10], ids=["rsv1", "rsv2", "rsv3"])
def test_read_frame_refuses_reserved_bits(bit):
    with pytest.raises(ProtocolError) as caught:
        read_frame(header_only(bytes([0x80 | bit | TEXT, 0x80])), masked=True, max_payload=10)
    assert caught.value.code == 1002


@pytest.mark.parametrize("opcode", [0x3, 0x4, 0x5, 0x6, 0x7, 0xB, 0xC, 0xD, 0xE, 0xF])
def test_read_frame_refuses_unknown_opcodes(opcode):
    with pytest.raises(ProtocolError) as caught:
        read_frame(header_only(bytes([0x80 | opcode, 0x80])), masked=True, max_payload=10)
    assert caught.value.code == 1002


@pytest.mark.parametrize(
    "masked", [True, False], ids=["unmasked-from-client", "masked-from-server"]
)
def test_read_frame_refuses_the_wrong_mask_direction(masked):
    frame = encode_frame(TEXT, b"hi", mask=not masked)
    with pytest.raises(ProtocolError) as caught:
        read_frame(header_only(frame[:2]), masked=masked, max_payload=10)
    assert caught.value.code == 1002


@pytest.mark.parametrize("opcode", [CLOSE, PING, PONG])
def test_read_frame_refuses_fragmented_control_frames(opcode):
    frame = encode_frame(opcode, b"", fin=False, mask=True)
    with pytest.raises(ProtocolError) as caught:
        read_frame(header_only(frame[:2]), masked=True, max_payload=10)
    assert caught.value.code == 1002


@pytest.mark.parametrize("opcode", [CLOSE, PING, PONG])
def test_read_frame_refuses_control_frames_over_125_bytes(opcode):
    frame = encode_frame(opcode, bytes(126), mask=True)
    with pytest.raises(ProtocolError) as caught:
        read_frame(header_only(frame[:2]), masked=True, max_payload=4096)
    assert caught.value.code == 1002


def test_read_frame_refuses_a_64_bit_length_with_the_high_bit_set():
    head = bytes([0x80 | TEXT, 127]) + struct.pack("!Q", 1 << 63)
    with pytest.raises(ProtocolError) as caught:
        read_frame(header_only(head), masked=False, max_payload=1 << 64)
    assert caught.value.code == 1002


@pytest.mark.parametrize(
    "extended",
    [
        bytes([0x80 | 126]) + struct.pack("!H", 4097),
        bytes([0x80 | 127]) + struct.pack("!Q", 1 << 40),
    ],
    ids=["16-bit", "64-bit"],
)
@pytest.mark.parametrize("opcode", [TEXT, BINARY, CONTINUATION])
def test_read_frame_checks_the_size_limit_before_it_reads_the_payload(extended, opcode):
    # header_only fails the test if the codec asks for the mask key or the payload.
    head = bytes([0x80 | opcode]) + extended
    with pytest.raises(ProtocolError) as caught:
        read_frame(header_only(head), masked=True, max_payload=4096)
    assert caught.value.code == 1009


def test_read_frame_accepts_a_payload_of_exactly_the_size_limit():
    frame = encode_frame(TEXT, b"x" * 4096, mask=True)
    assert read_frame(reader(frame), masked=True, max_payload=4096) == (True, TEXT, b"x" * 4096)


def test_message_reader_assembles_fragments_around_control_frames():
    data = client_frames(
        (TEXT, b"Hel", False),
        (PING, b"p", True),
        (CONTINUATION, b"lo ", False),
        (PONG, b"q", True),
        (CONTINUATION, b"world", True),
        (TEXT, b"again", True),
    )
    assert messages(data, 4) == [
        ("ping", b"p"),
        ("pong", b"q"),
        ("text", "Hello world"),
        ("text", "again"),
    ]


def test_message_reader_accepts_a_ping_while_the_fragment_buffer_is_full():
    data = client_frames(
        (TEXT, b"x" * 4096, False),
        (PING, b"p", True),
        (CONTINUATION, b"", True),
    )
    assert messages(data, 2) == [("ping", b"p"), ("text", "x" * 4096)]


@pytest.mark.parametrize(
    "frames",
    [
        [(CONTINUATION, b"lost", True)],
        [(TEXT, b"done", True), (CONTINUATION, b"more", True)],
    ],
    ids=["first-frame", "after-a-message"],
)
def test_message_reader_refuses_a_continuation_without_a_message(frames):
    data = client_frames(*frames)
    message_reader = MessageReader(reader(data))
    with pytest.raises(ProtocolError) as caught:
        for _ in frames:
            message_reader.next()
    assert caught.value.code == 1002


def test_message_reader_refuses_a_new_message_inside_a_fragmented_one():
    data = client_frames((TEXT, b"one", False), (TEXT, b"two", True))
    assert refusal(data) == 1002


@pytest.mark.parametrize("fin", [True, False], ids=["single", "fragmented"])
def test_message_reader_refuses_binary_messages_with_1003(fin):
    assert refusal(client_frames((BINARY, b"\x00\x01", fin))) == 1003


@pytest.mark.parametrize(
    "frames",
    [
        [(TEXT, b"\xff", True)],
        [(TEXT, b"abc\xe2\x82", True)],
        [(TEXT, b"\xe2\x82", False), (CONTINUATION, b"(", True)],
    ],
    ids=["invalid-byte", "truncated-character", "split-across-fragments"],
)
def test_message_reader_refuses_invalid_utf8_with_1007(frames):
    assert refusal(client_frames(*frames)) == 1007


def test_message_reader_decodes_a_character_split_across_fragments():
    euro = "€".encode()
    data = client_frames((TEXT, euro[:1], False), (CONTINUATION, euro[1:], True))
    assert messages(data, 1) == [("text", "€")]


def test_message_reader_refuses_a_single_frame_over_4096_bytes_with_1009():
    assert refusal(client_frames((TEXT, b"x" * 4097, True))) == 1009
    assert messages(client_frames((TEXT, b"x" * 4096, True)), 1) == [("text", "x" * 4096)]


def test_message_reader_limits_the_assembled_message_before_reading_the_last_fragment():
    first = encode_frame(TEXT, b"x" * 2048, fin=False, mask=True)
    second = encode_frame(CONTINUATION, b"y" * 2049, mask=True)
    # Give only the second frame's header: the limit must not need its payload.
    message_reader = MessageReader(header_only(first + second[:4]))
    with pytest.raises(ProtocolError) as caught:
        message_reader.next()
    assert caught.value.code == 1009


def close_frame(payload):
    return encode_frame(CLOSE, payload, mask=True)


def test_message_reader_reports_a_close_without_a_payload():
    assert messages(close_frame(b""), 1) == [("close", None, "")]


def test_message_reader_refuses_a_one_byte_close_payload():
    assert refusal(close_frame(b"\x03")) == 1002


@pytest.mark.parametrize("code", [0, 999, 1004, 1005, 1006, 1015, 2999, 5000, 65535])
def test_message_reader_refuses_close_codes_a_peer_must_not_send(code):
    assert refusal(close_frame(struct.pack("!H", code))) == 1002


@pytest.mark.parametrize("code", [1000, 1001, 1003, 1007, 1012, 1014, 3000, 4401, 4999])
def test_message_reader_accepts_valid_close_codes_and_reasons(code):
    payload = struct.pack("!H", code) + "bye ✓".encode()
    assert messages(close_frame(payload), 1) == [("close", code, "bye ✓")]


def test_message_reader_refuses_a_close_reason_that_is_not_utf8():
    # RFC 6455 lets the endpoint fail with 1002 or 1007. The implementation uses 1007.
    assert refusal(close_frame(struct.pack("!H", 1000) + b"\xff")) == 1007


def test_encode_close_without_a_code_is_empty():
    assert encode_close() == b""
    assert encode_close(None, "ignored") == b""


@pytest.mark.parametrize(
    ("reason", "kept"),
    [
        ("short", "short"),
        ("a" * 200, "a" * 123),
        ("é" * 70, "é" * 61),
        ("€" * 50, "€" * 41),
        ("😀" * 40, "😀" * 30),
        ("a" + "é" * 70, "a" + "é" * 61),
    ],
    ids=["short", "ascii", "two-byte", "three-byte", "four-byte", "offset-two-byte"],
)
def test_encode_close_cuts_the_reason_to_123_bytes_on_a_character_boundary(reason, kept):
    payload = encode_close(1001, reason)
    assert payload[:2] == struct.pack("!H", 1001)
    assert payload[2:].decode("utf-8") == kept
    assert len(payload) <= 125
    # The payload fits a control frame and decodes as a valid close.
    assert messages(close_frame(payload), 1) == [("close", 1001, kept)]


@pytest.fixture
def pair():
    near, peer = socket.socketpair()
    near.settimeout(5)
    peer.settimeout(5)
    yield near, peer
    near.close()
    peer.close()


def open_connection(sock, **kwargs):
    return Connection(sock, sock.makefile("rb"), sock.makefile("wb"), **kwargs)


def peer_reader(peer, *, masked=False):
    read = exact_reader(peer.makefile("rb"))
    return lambda: read_frame(read, masked=masked, max_payload=1 << 20)


@pytest.mark.parametrize("client", [False, True], ids=["server-role", "client-role"])
def test_connection_writes_queued_frames_in_order(pair, client):
    near, peer = pair
    connection = open_connection(near, client=client)
    for index in range(30):
        connection.send_text(f"m{index}")
    connection.ping(b"p")
    connection.send_text("last")
    connection.close(1000, "done")
    frame = peer_reader(peer, masked=client)
    received = [frame() for _ in range(33)]
    assert received == [
        *[(True, TEXT, f"m{index}".encode()) for index in range(30)],
        (True, PING, b"p"),
        (True, TEXT, b"last"),
        (True, CLOSE, encode_close(1000, "done")),
    ]
    assert connection.close_written.wait(2)
    connection.join(2)
    assert connection.backlog == 0
    # A written close ends the writer. It is not an abort.
    assert not connection.aborted


def test_connection_refuses_sends_after_a_queued_close(pair):
    near, peer = pair
    connection = open_connection(near)
    connection.close(1001, "going")
    assert connection.close_sent
    with pytest.raises(ConnectionClosed):
        connection.send_text("late")
    with pytest.raises(ConnectionClosed):
        connection.ping()
    # A second close is ignored, so a teardown path can always call it.
    connection.close(1000)
    assert connection.close_written.wait(2)
    connection.join(2)
    connection.abort()
    assert peer_reader(peer)() == (True, CLOSE, encode_close(1001, "going"))
    assert peer.recv(1) == b""


class StalledWriter:
    """A wfile whose first write blocks until the test releases it."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.frames = []

    def write(self, data):
        self.entered.set()
        assert self.release.wait(5)
        self.frames.append(data)

    def flush(self):
        pass


def test_connection_queue_overflow_aborts_the_connection_and_raises(pair):
    near, peer = pair
    writer = StalledWriter()
    connection = Connection(near, near.makefile("rb"), writer)
    connection.send_text("first")
    assert writer.entered.wait(2)
    for index in range(64):
        connection.send_text(f"queued {index}")
    assert connection.backlog == 64
    assert not connection.aborted
    with pytest.raises(ConnectionClosed):
        connection.send_text("overflow")
    assert connection.aborted
    # The overflow shut the socket down.
    assert peer.recv(1) == b""
    with pytest.raises(ConnectionClosed):
        connection.send_text("after abort")
    writer.release.set()
    connection.join(2)
    assert writer.frames == [encode_frame(TEXT, b"first")]


def test_connection_abort_ends_a_receive_blocked_on_another_thread(pair):
    near, _peer = pair
    connection = open_connection(near)
    outcome = []

    def receive():
        try:
            outcome.append(connection.receive())
        except (EOFError, OSError) as error:
            outcome.append(error)

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    thread.join(0.1)
    assert thread.is_alive()
    assert not connection.aborted
    connection.abort()
    assert connection.aborted
    thread.join(2)
    assert not thread.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], (EOFError, OSError))
    connection.join(2)


@pytest.mark.parametrize(
    ("payload", "code"),
    [(encode_close(1001, "bye"), 1001), (b"", 1005)],
    ids=["with-code", "without-code"],
)
def test_connection_receive_answers_pings_and_echoes_the_close(pair, payload, code):
    near, peer = pair
    connection = open_connection(near)
    peer.sendall(client_frames((PING, b"hb", True), (TEXT, b"hi", True)))
    assert connection.receive() == "hi"
    frame = peer_reader(peer)
    assert frame() == (True, PONG, b"hb")
    peer.sendall(close_frame(payload))
    assert connection.receive() is None
    assert connection.close_received == code
    # The echo repeats the code without the reason.
    assert frame() == (True, CLOSE, payload[:2])
    assert connection.close_written.wait(2)
    with pytest.raises(ConnectionClosed):
        connection.send_text("late")
    connection.join(2)


def test_connection_receive_sends_no_pong_after_its_close(pair):
    near, peer = pair
    connection = open_connection(near)
    connection.close(1000)
    peer.sendall(client_frames((PING, b"hb", True), (TEXT, b"still read", True)))
    assert connection.receive() == "still read"
    assert connection.close_written.wait(2)
    connection.abort()
    assert peer_reader(peer)() == (True, CLOSE, encode_close(1000))
    assert peer.recv(1) == b""


def test_send_text_never_waits_for_a_peer_that_stops_reading(pair):
    near, peer = pair
    near.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    connection = open_connection(near)
    chunk = "x" * 16384
    # Fill the kernel buffers: the writer then blocks and frames stay queued.
    for _ in range(200):
        connection.send_text(chunk)
        deadline = time.monotonic() + 0.3
        while connection.backlog and time.monotonic() < deadline:
            time.sleep(0.005)
        if connection.backlog:
            break
    assert connection.backlog, "The kernel buffers never filled"
    durations, backlog, overflow = [], None, False
    for _ in range(100):
        started = time.monotonic()
        try:
            connection.send_text(chunk)
        except ConnectionClosed:
            overflow = True
            break
        finally:
            durations.append(time.monotonic() - started)
        backlog = connection.backlog
    assert overflow
    # Every send returned at once until the queue limit of 64 frames.
    assert backlog == 64
    assert max(durations) < 0.5
    connection.join(2)
